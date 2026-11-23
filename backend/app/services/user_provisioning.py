"""Shared lazy-create flow for `users` + their Personal project.

Clerk alone decides who signs up (06.3 D-02): a verified Clerk session with
no `users` row is provisioned here on first touch, with no further admission
step. Clerk callers hit this code path:
1. `_auth_via_clerk_jwt` and `_auth_via_dev_bypass` (auth.py) — first
   verified session. Has email/name from the claims when Clerk sent them,
   commits at the end.
2. `_resolve_or_create_user` (admin.py) — first interaction is via the
   admin endpoint, behind X-Admin-Key, before the user has ever signed in
   directly. No email/name in context.

Both paths share:
- race-safe User insert (clerk_id unique constraint),
- Personal project insert with its own defensive try/except,
- log-on-failure paths.

The race semantics and project invariant MUST stay identical across
callers — downstream resolvers assume every user has a Personal
project. Centralizing here means there's one place to maintain that
contract.

PartnerTenant provisioning uses a separate stable reference and never creates
or accepts a synthetic Clerk id.

Billing exemption (06.3 D-01, ROADMAP SC-5): every partner-tenant row is
stamped `invite_exempted_at` at creation, with one `billing.exempt_provisioned`
audit event; a race loser that adopts an existing row writes nothing. A Clerk
row is never stamped here, whichever path creates it. Exempting a Clerk user
is an explicit operator action (`PUT /v1/admin/auth/billing-exemptions`), not
a side effect of which call reached the user first.
"""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import (
    PRINCIPAL_KIND_CLERK,
    PRINCIPAL_KIND_PARTNER_TENANT,
    User,
)
from app.services.audit import record_control_plane_audit
from app.services.principal_lifecycle import (
    PrincipalIdentityConflictError,
    PrincipalTerminatedError,
    assert_clerk_principal_active,
    load_clerk_user_for_issuer,
)

logger = logging.getLogger(__name__)

BILLING_EXEMPT_PROVISIONED_ACTION = "billing.exempt_provisioned"
# Operator grant/revoke of a Clerk user's exemption (admin.py WR-02 route).
BILLING_EXEMPT_GRANTED_ACTION = "billing.exempt_granted"
BILLING_EXEMPT_REVOKED_ACTION = "billing.exempt_revoked"


def _record_billing_exempt_provisioned(db: AsyncSession, *, user: User, reason: str) -> None:
    record_control_plane_audit(
        db,
        actor_type="system",
        action=BILLING_EXEMPT_PROVISIONED_ACTION,
        resource_type="user",
        resource_id=str(user.id),
        source="user_provisioning",
        target_user_id=user.id,
        details={"reason": reason},
    )


async def lazy_create_user_with_personal_project(
    db: AsyncSession,
    *,
    clerk_id: str,
    clerk_issuer: str | None = None,
    email: str | None,
    name: str | None,
    avatar_url: str | None = None,
    race_loser_status: int,
    inactive_status: int = status.HTTP_403_FORBIDDEN,
) -> User:
    """Insert a User row + Personal project for `clerk_id`, race-safe.

    A verified Clerk identity is provisioned on first touch (06.3 D-02).
    Whether the new user may use gated routes is the billing gate's
    decision, not this helper's.

    Args:
        db: open session. Caller MUST `commit()` after — the helper
            leaves the rows flushed but not committed so caller can
            bundle other writes (admin endpoints add ApiKey / env in
            the same txn; auth.py commits after refresh).
        clerk_id: Clerk user id; must be already-authenticated by
            the caller's context (JWT verify or X-Admin-Key trust).
        email / name / avatar_url: identity fields if the caller has
            them. The admin path leaves them None; the JWT path
            forwards whatever Clerk handed it. Backfill of admin-
            created rows happens later in `_auth_via_clerk_jwt`'s
            backfill branch.
        race_loser_status: HTTP status code to raise if the concurrent-
            create race produces a winner row that then vanishes (a
            pathological case — would require a concurrent delete).
            JWT path passes 401 (fail-closed for the user's session);
            admin path passes 500 (operational anomaly the admin caller
            should surface differently).

    Returns:
        The freshly-inserted User row, OR the winner's row if a
        concurrent insert raced ahead. Personal project is present in
        either case (the winner created its own).

    Raises:
        HTTPException with `inactive_status` — the principal has been
        terminated.
        HTTPException with `race_loser_status` — race winner row not
        findable after rollback (concurrent delete or programming
        error). Treat as fatal.
        HTTPException 500 — Personal project insert failed. Bizarre
        states only (partial unique index, mid-flush connection drop).
    """
    try:
        issuer = await assert_clerk_principal_active(
            db,
            subject=clerk_id,
            issuer=clerk_issuer,
        )
    except PrincipalTerminatedError:
        raise HTTPException(inactive_status, "account has been terminated") from None

    new_user = User(
        clerk_id=clerk_id,
        clerk_issuer=issuer,
        principal_kind=PRINCIPAL_KIND_CLERK,
        partner_tenant_ref=None,
        email=email,
        name=name,
        avatar_url=avatar_url,
    )
    db.add(new_user)

    # User flush: the ONLY racy point. `users.clerk_id` is unique, so
    # two concurrent first-touches for the same clerk_id race here.
    # Loser rolls back, re-queries by clerk_id, and adopts whatever
    # the winner committed.
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        try:
            if issuer is None:
                target = (
                    await db.execute(select(User).where(User.clerk_id == clerk_id))
                ).scalar_one_or_none()
            else:
                await assert_clerk_principal_active(db, subject=clerk_id, issuer=issuer)
                target = await load_clerk_user_for_issuer(
                    db,
                    issuer=issuer,
                    subject=clerk_id,
                    bind_legacy=True,
                )
        except (PrincipalIdentityConflictError, PrincipalTerminatedError):
            raise HTTPException(inactive_status, "account has been terminated") from None
        if target is None:
            raise HTTPException(
                race_loser_status,
                "could not create or load user",
            ) from None
        return target

    await _create_personal_project(
        db,
        user=new_user,
        identity_log=f"clerk_id={clerk_id}",
    )
    return new_user


async def lazy_create_partner_user_with_personal_project(
    db: AsyncSession,
    *,
    partner_tenant_ref: str,
    race_loser_status: int,
) -> User:
    """Insert a PartnerTenant-owned User row + Personal project, race-safe.

    Partner tenants have no hosted owner and cannot pay, so every new row is
    billing-exempt (06.3 D-01, ROADMAP SC-5, RESEARCH Pitfall 11) with a
    billing.exempt_provisioned audit event; a race loser writes nothing.
    """
    new_user = User(
        clerk_id=None,
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        partner_tenant_ref=partner_tenant_ref,
        email=None,
        name=None,
        avatar_url=None,
        invite_exempted_at=datetime.now(UTC),
    )
    db.add(new_user)

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        target = (
            await db.execute(
                select(User).where(
                    User.principal_kind == PRINCIPAL_KIND_PARTNER_TENANT,
                    User.partner_tenant_ref == partner_tenant_ref,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            raise HTTPException(
                race_loser_status,
                "could not create or load partner tenant user",
            ) from None
        return target

    _record_billing_exempt_provisioned(db, user=new_user, reason="partner_provisioned")

    await _create_personal_project(
        db,
        user=new_user,
        identity_log=f"partner_tenant_ref={partner_tenant_ref}",
    )
    return new_user


async def _create_personal_project(
    db: AsyncSession,
    *,
    user: User,
    identity_log: str,
) -> None:
    # Cannot race on `user_id` (just generated), so a partial-unique
    # kind=personal index is the only realistic failure mode.
    personal = Project(
        user_id=user.id,
        name="Personal",
        slug="personal",
        kind=PROJECT_KIND_PERSONAL,
    )
    db.add(personal)
    try:
        await db.flush()
    except Exception:
        logger.exception(
            "personal_project_create_failed %s user_id=%s",
            identity_log,
            user.id,
        )
        await db.rollback()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal server error",
        ) from None
