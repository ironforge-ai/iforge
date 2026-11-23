"""Access-status route (06.3 D-01, D-03, D-04).

`GET /access/status` runs on `require_clerk_claims` -- the claims-only auth
dependency that verifies a Clerk JWT WITHOUT provisioning -- because it must
answer for authenticated identities that have no `users` row yet. It is the
only claims-only route, and it never provisions: `GET /v1/auth/me` on the
billing-exempt dependency provisions through the full JWT path, so a second
provisioning surface here would only be a second thing to keep in step
(RESEARCH anti-pattern).

It answers exactly one of three states:

- `terminated` -- the principal is fenced or banned; checked first, always.
- `billing_required` -- the billing gate is on and the caller has no active
  subscription, including a caller with no row yet (RESEARCH Pitfall 4).
- `provisioned` -- everything else.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ClerkClaims, require_clerk_claims
from app.core.config import settings
from app.core.database import get_session
from app.models.user import PRINCIPAL_KIND_CLERK, User
from app.services.billing_gate import ensure_unlocked
from app.services.principal_lifecycle import (
    PrincipalTerminatedError,
    assert_clerk_principal_active,
    assert_user_authority_active,
)

router = APIRouter(tags=["access"])


class AccessStatusResponse(BaseModel):
    state: Literal["provisioned", "billing_required", "terminated"]


def _post_provisioning_state(user: User) -> Literal["provisioned", "billing_required"]:
    """Billing-gate branch for a provisioned user (D-09; 03-06 enforces it).
    Exempted users (grandfathered / operator-exempted / partner) are never billing-gated."""
    if (
        settings.billing_gate_enabled
        and user.billing_unlocked_at is None
        and user.invite_exempted_at is None
    ):
        return "billing_required"
    return "provisioned"


@router.get("/access/status", response_model=AccessStatusResponse)
async def access_status(
    claims: ClerkClaims = Depends(require_clerk_claims),
    db: AsyncSession = Depends(get_session),
) -> AccessStatusResponse:
    """Answer for an authenticated identity WITHOUT creating a User row."""
    user = (
        await db.execute(
            select(User).where(
                User.principal_kind == PRINCIPAL_KIND_CLERK,
                User.clerk_id == claims.subject,
            )
        )
    ).scalar_one_or_none()
    # The fence check runs for BOTH branches. `fence_principal_deleted` does
    # not remove the `users` row -- only the purge sweep does, a grace window
    # later -- so a surviving row is exactly the population `terminated`
    # exists for. Checking only the row-absent branch answered `provisioned`
    # (or `billing_required`) to every operator-deleted tenant for the whole
    # window, leaving the S2 deactivated screen unreachable. Order is
    # principal-then-user, matching the lock order `fence_principal_deleted`
    # takes, so the two paths cannot deadlock against each other.
    try:
        await assert_clerk_principal_active(
            db,
            subject=claims.subject,
            issuer=claims.issuer,
        )
        if user is not None:
            await assert_user_authority_active(db, user.id)
    except PrincipalTerminatedError:
        return AccessStatusResponse(state="terminated")
    if user is None:
        # No row yet: the first gated request will provision one, and it will
        # be unpaid. Answering `provisioned` here while the gate is on would
        # send a new identity into a dashboard every request of which is
        # refused (RESEARCH Pitfall 4).
        if settings.billing_gate_enabled:
            return AccessStatusResponse(state="billing_required")
        return AccessStatusResponse(state="provisioned")
    state = _post_provisioning_state(user)
    # Post-checkout poll path (03-06, 06.3 D-04): a locked user attempts the
    # live /v2 unlock so a completed checkout flips the answer to
    # `provisioned` on the next poll. `fresh=True` skips a negative outcome the
    # user's own gated requests cached, bounded to one /v2 read per user per
    # 3 s per process (the bound lives in process memory). An unlocked user
    # runs the INT-1 lapse re-check, so this answer agrees with the gated
    # routes once a lapse outlasts its grace. The 403 is caught into the
    # billing_required STATE (this route reports, it does not refuse); the
    # fail-closed 503 billing_unavailable propagates.
    try:
        await ensure_unlocked(db, user, fresh=state == "billing_required")
    except HTTPException as exc:
        if exc.status_code == status.HTTP_403_FORBIDDEN:
            return AccessStatusResponse(state="billing_required")
        raise
    return AccessStatusResponse(state="provisioned")
