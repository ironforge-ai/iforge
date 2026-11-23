"""The companion install request: one queued installation with platform-generated credentials.

``create_installation`` is the first user-visible step of APP-01 and the place APP-03 is made true
by construction:

* **Every credential is generated here, server-side (D-27).** No request body carries one, so no
  plaintext ever passes through a form or a request -- which is the reason letting the tenant set
  it at install was rejected.
* **Every credential is stored encrypted in ``companion_installation_secrets`` (CK-D,
  ``clawdi-store-set-password-link``: an iforge-owned store).** Never as a tenant vault item,
  which ``/v1/vault/resolve`` would hand in plaintext to the tenant's CLI and to any agent key
  holding ``vault:read``. The response model has no field that could carry a value, and nothing
  here logs or audits one.
* **The installation pins the catalog's current app digest at request time (D-28),** so nothing
  it later runs can drift from what the tenant accepted.

Ownership follows ``routes/runtime.py::_owned_agent_or_404``: another tenant's agent and an
archived agent are both 404, never 403, so a caller that cannot prove ownership learns nothing
about whether the agent exists.
"""

from __future__ import annotations

import base64
import logging
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote, quote_plus
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import JsonValue
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext
from app.core.companion_catalog import CompanionCatalogEntry, catalog_entry
from app.core.config import settings
from app.models.companion_installation import (
    COMPANION_INSTALLATION_STATUS_COMPLETE,
    COMPANION_INSTALLATION_STATUS_FAILED,
    COMPANION_INSTALLATION_STATUS_QUEUED,
    COMPANION_INSTALLATION_STATUS_RUNNING,
    COMPANION_OPERATION_INSTALL,
    COMPANION_OPERATION_UNINSTALL,
    COMPANION_OPERATION_UPDATE,
    HOST_REMOVAL_UNCONFIRMED_KEY,
    REMOVED_REPORTED_AT_KEY,
    CompanionInstallation,
    CompanionInstallationSecret,
    CompanionInstallLogLine,
    installation_is_serving,
)
from app.models.session import AgentEnvironment
from app.schemas.companions import (
    MAX_INSTALL_REPORT_ERROR_CHARS,
    CompanionBackupReport,
    CompanionInstallReport,
)
from app.services.companion_billing import (
    CompanionBillingUnavailableError,
    CompanionQuote,
    CompanionTierNotPricedError,
    companion_billing_unavailable,
    companion_tier_not_priced,
    request_companion_quote,
    verify_companion_quote,
)
from app.services.companion_budget import check_spend_ceiling
from app.services.hosted_owner import hosted_owner_id
from app.services.tenant_export import export_owes_installation
from app.services.vault_crypto import decrypt_with_key_version, encrypt_with_active_key

log = logging.getLogger(__name__)

# The install route's refusal at the tenant's monthly spend ceiling (OPS-06, D-21): its own code,
# distinct from the façade's `spend_ceiling_reached` reason token only by the `companion_` prefix
# every install-route error code carries.
COMPANION_SPEND_CEILING_REACHED = "companion_spend_ceiling_reached"

# A running job that hears nothing from its runtime for this long fails with
# ``RUNTIME_DID_NOT_REPORT`` instead of running forever (T-06-11-05). Every accepted job report
# pushes the deadline out again; the lifecycle worker enforces it.
REPORT_DEADLINE_SECONDS = 1800
RUNTIME_DID_NOT_REPORT = "runtime did not report"

# The step a data-asserted ``ready`` report leaves behind: the job is still ``running`` and due for
# the lifecycle worker, which runs the completion steps and only then marks it ``complete``.
READY_STEP = "ready"

# What a credential becomes in any stored log line or error (T-06-11-02).
REDACTED = "[redacted]"
_PERCENT_ESCAPE = re.compile(r"%[0-9A-F]{2}")

# The log keeps at most this many lines per operation of one installation (T-06-11-04), and the
# backup state keeps the newest this-many markers (plan 06-16 picks a restore target from them).
MAX_LOG_LINES_PER_OPERATION = 5000
MAX_BACKUP_MARKERS = 48

# The label's random part: 12 lowercase RFC 4648 base32 characters, 60 bits of entropy. No tenant
# identifier ever appears in DNS, so a hostname enumerates nothing (T-06-09-03).
HOSTNAME_RANDOM_LENGTH = 12


# The four Postiz credential atoms the platform generates itself (research Pattern 5), and how.
# `admin-password` stays under Postiz's 64-character password cap; `api-key` is compared verbatim
# by Postiz, so it is URL-safe text with no surrounding whitespace. Plan 06-25's recovery
# regenerates exactly these, one at a time, with the same generators.
GENERATED_SECRET_ATOMS: dict[str, Callable[[], str]] = {
    "jwt-secret": lambda: secrets.token_urlsafe(48),  # 64 characters
    "db-password": lambda: secrets.token_hex(24),  # 48 hex characters
    "admin-password": lambda: secrets.token_urlsafe(30),  # 40 characters
    "api-key": lambda: secrets.token_urlsafe(36),  # 48 characters
}


def generate_secret_atom(secret_name: str) -> str:
    """A fresh value for one platform-generated atom. ``KeyError`` for any other name."""
    return GENERATED_SECRET_ATOMS[secret_name]()


def _generate_secret_atoms() -> dict[str, str]:
    """The four Postiz credential atoms, generated with ``secrets``."""
    return {name: generate() for name, generate in GENERATED_SECRET_ATOMS.items()}


def generate_hostname_label(companion_key: str) -> str:
    """``<companion>-<12 lowercase base32 characters>``: one non-guessable DNS label."""
    random_part = base64.b32encode(secrets.token_bytes(8)).decode("ascii").lower()
    return f"{companion_key}-{random_part[:HOSTNAME_RANDOM_LENGTH]}"


async def _owned_agent_or_404(
    db: AsyncSession, *, auth: AuthContext, agent_id: UUID
) -> AgentEnvironment:
    agent = (
        await db.execute(
            select(AgentEnvironment).where(
                AgentEnvironment.id == agent_id,
                AgentEnvironment.user_id == auth.user_id,
                AgentEnvironment.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        # 404 rather than 403: a caller that cannot prove ownership learns nothing about whether
        # the agent exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    return agent


async def _live_installation(
    db: AsyncSession, *, environment_id: UUID, companion_key: str
) -> CompanionInstallation | None:
    return (
        await db.execute(
            select(CompanionInstallation).where(
                CompanionInstallation.environment_id == environment_id,
                CompanionInstallation.companion_key == companion_key,
                CompanionInstallation.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()


def _already_installed() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "companion_already_installed",
            "message": "This agent already has a live installation of this companion.",
        },
    )


def _spend_ceiling_reached() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": COMPANION_SPEND_CEILING_REACHED,
            "message": (
                "This month's companion spend ceiling is reached, so no new companion can be "
                "installed. Companions already installed keep running. Raise the ceiling in "
                "account settings, or wait for the new month."
            ),
        },
    )


def _catalog_entry_or_404(companion_key: str) -> CompanionCatalogEntry:
    try:
        return catalog_entry(companion_key)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Companion not found") from None


async def quote_companion(
    *, auth: AuthContext, companion_key: str, isolation_tier: str
) -> CompanionQuote:
    """The /v2 price of ``companion_key`` at ``isolation_tier`` for the caller (APP-08, D-07).

    The tier is passed straight to /v2, which is the only authority on what is priced: a tier it
    does not price is 422 ``companion_tier_not_priced``, and an unconfigured or unreachable /v2 is
    503 ``companion_billing_unavailable`` -- pricing fails closed, never open. An unknown companion
    is 404 before /v2 is asked anything. The owner on the /v2 wire is the caller's Clerk id
    (``hosted_owner_id``, 06.2 D-09); a caller with none (a partner tenant) has no hosted owner and
    is 503 ``companion_billing_unavailable`` with no /v2 call.
    """
    entry = _catalog_entry_or_404(companion_key)
    owner_id = hosted_owner_id(auth.user)
    if owner_id is None:
        raise companion_billing_unavailable()
    try:
        return await request_companion_quote(
            owner_user_id=owner_id, companion_key=entry.key, isolation_tier=isolation_tier
        )
    except CompanionTierNotPricedError:
        raise companion_tier_not_priced(isolation_tier) from None
    except CompanionBillingUnavailableError:
        raise companion_billing_unavailable() from None


async def create_installation(
    db: AsyncSession,
    *,
    auth: AuthContext,
    agent_id: UUID,
    companion_key: str,
    isolation_tier: str,
    quote_id: str,
) -> CompanionInstallation:
    """Queue one installation of ``companion_key`` on the caller's own agent, at a price they saw.

    Refusals, each by name and before anything is written: an unknown companion is 404; a tier
    the contract admits but this build does not offer is 422 ``companion_tier_not_offered``
    (D-06); an unconfigured companion zone is 503 ``companion_edge_zone_unconfigured``, because an
    installation with no hostname could never be reached; a foreign or archived agent is 404; a
    second live installation of the same kind on the same agent is 409; a tenant at or over its
    monthly spend ceiling is 409 ``companion_spend_ceiling_reached`` (OPS-06, D-21). Last, the
    quote is verified against /v2 (APP-08, T-06-17-01): 409 ``companion_quote_expired`` or
    ``companion_quote_mismatch``, 503 ``companion_billing_unavailable`` -- so nothing is ever
    provisioned for a tenant who has not been shown a current price for exactly this companion,
    tier and account. The account is the caller's Clerk id (``hosted_owner_id``, 06.2 D-09); a
    caller with none (a partner tenant) is 503 ``companion_billing_unavailable`` with no /v2 call.
    The quoted amounts are stored with the installation; the line item that bills them starts only
    when the install completes (``companion_billing.BILLING_START_STEP``).
    """
    entry = _catalog_entry_or_404(companion_key)
    if not entry.tier_offered(isolation_tier):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "companion_tier_not_offered",
                "message": (
                    f"The {isolation_tier} isolation tier is not offered for "
                    f"{entry.display_name} (offered: {', '.join(entry.offered_tiers)})."
                ),
            },
        )
    zone = settings.companion_edge_zone_name.strip().lower().rstrip(".")
    if not zone:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "companion_edge_zone_unconfigured",
                "message": "Companion installs are unavailable: no companion zone is configured.",
            },
        )
    agent = await _owned_agent_or_404(db, auth=auth, agent_id=agent_id)
    if await _live_installation(db, environment_id=agent.id, companion_key=entry.key):
        raise _already_installed()
    # The tenant's monthly money ceiling (OPS-06, D-21): a new install adds recurring cost, so at
    # the ceiling it is refused like an agent's call -- here, before the quote is verified and
    # before any installation row (the job), secret atom or log line exists. The refusal touches
    # no existing installation: companions already running keep running (a billing event is never
    # an availability event). Everything the check reads is tenant state by `auth.user_id`.
    if not (await check_spend_ceiling(db, user_id=auth.user_id)).allowed:
        raise _spend_ceiling_reached()
    owner_id = hosted_owner_id(auth.user)
    if owner_id is None:
        raise companion_billing_unavailable()
    quote = await verify_companion_quote(
        quote_id=quote_id,
        owner_user_id=owner_id,
        companion_key=entry.key,
        isolation_tier=isolation_tier,
    )

    label = generate_hostname_label(entry.key)
    owner_email = auth.user.email or f"owner+{label}@{zone}"
    installation = CompanionInstallation(
        user_id=auth.user_id,
        environment_id=agent.id,
        companion_key=entry.key,
        isolation_tier=isolation_tier,
        operation=COMPANION_OPERATION_INSTALL,
        version=entry.version,
        pinned_digest=entry.app.sha256,
        hostname_label=label,
        hostname=f"{label}.{zone}",
        owner_email=owner_email,
        quote_id=quote.quote_id,
        quoted_monthly_usd_micros=quote.monthly_price_usd_micros,
        quoted_storage_usd_micros_per_gib_month=quote.storage_price_usd_micros_per_gib_month,
    )
    db.add(installation)
    try:
        await db.flush()
    except IntegrityError:
        # Two requests raced past the pre-check; the partial unique index admitted exactly one.
        await db.rollback()
        if await _live_installation(db, environment_id=agent_id, companion_key=entry.key):
            raise _already_installed() from None
        raise

    for secret_name, value in _generate_secret_atoms().items():
        # Under the ACTIVE keyring version, which the row records (plan 06-25, D-27).
        ciphertext, nonce, key_version = encrypt_with_active_key(value)
        db.add(
            CompanionInstallationSecret(
                installation_id=installation.id,
                secret_name=secret_name,
                encrypted_value=ciphertext,
                nonce=nonce,
                key_version=key_version,
            )
        )
    await db.commit()
    await db.refresh(installation)
    return installation


def _owned_installations_query(auth: AuthContext):
    # Only installations of the caller's own ACTIVE agents: the tenant predicate comes from the
    # auth context, never from a supplied identifier.
    return (
        select(CompanionInstallation)
        .join(AgentEnvironment, AgentEnvironment.id == CompanionInstallation.environment_id)
        .where(
            CompanionInstallation.user_id == auth.user_id,
            AgentEnvironment.user_id == auth.user_id,
            AgentEnvironment.archived_at.is_(None),
        )
    )


async def list_installations(
    db: AsyncSession, *, auth: AuthContext, agent_id: UUID | None
) -> list[CompanionInstallation]:
    """The caller's live installations, optionally for one of their own agents."""
    query = _owned_installations_query(auth).where(CompanionInstallation.removed_at.is_(None))
    if agent_id is not None:
        await _owned_agent_or_404(db, auth=auth, agent_id=agent_id)
        query = query.where(CompanionInstallation.environment_id == agent_id)
    result = await db.execute(
        query.order_by(CompanionInstallation.created_at.desc(), CompanionInstallation.id.desc())
    )
    return list(result.scalars().all())


async def get_installation(
    db: AsyncSession, *, auth: AuthContext, installation_id: UUID
) -> CompanionInstallation:
    """One of the caller's installations, or 404 -- never 403."""
    installation = (
        await db.execute(
            _owned_installations_query(auth).where(CompanionInstallation.id == installation_id)
        )
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    return installation


async def list_install_log(
    db: AsyncSession,
    *,
    auth: AuthContext,
    installation_id: UUID,
    after_seq: int,
    limit: int,
) -> list[CompanionInstallLogLine]:
    """The owner's install log after ``after_seq``, oldest first, at most ``limit`` lines.

    Ownership is ``get_installation``'s: another tenant's installation is 404, never 403. Every
    stored line was already redacted when it was written, so nothing here can return a credential.
    """
    installation = await get_installation(db, auth=auth, installation_id=installation_id)
    result = await db.execute(
        select(CompanionInstallLogLine)
        .where(
            CompanionInstallLogLine.installation_id == installation.id,
            CompanionInstallLogLine.seq > after_seq,
        )
        .order_by(CompanionInstallLogLine.seq)
        .limit(limit)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Updates (plan 06-18): pinned by default, a newer digest only an offer (D-28, APP-06, OPS-02).
# ---------------------------------------------------------------------------

UPDATE_STATE_UP_TO_DATE = "up_to_date"
UPDATE_STATE_AVAILABLE = "available"
UPDATE_STATE_DEFERRED = "deferred"
UPDATE_STATE_IN_PROGRESS = "in_progress"
UPDATE_STATE_ABORTED_BACKUP_FAILED = "aborted_backup_failed"
UPDATE_STATE_FAILED = "failed"

# Why an update failed. ``backup_failed``: the runtime's pre-update backup failed, so it changed
# nothing. ``update_aborted``: the backup was taken, but the runtime could not read the data it
# must preserve or pull the new image, so it changed nothing either. ``update_rolled_back``: Postiz
# failed its post-update assertion and the runtime restored the pre-update backup under the
# previous digest. ``runtime_did_not_report``: the reporting deadline passed.
UPDATE_ERROR_BACKUP_FAILED = "backup_failed"
UPDATE_ERROR_ABORTED = "update_aborted"
UPDATE_ERROR_ROLLED_BACK = "update_rolled_back"
UPDATE_ERROR_RUNTIME_DID_NOT_REPORT = "runtime_did_not_report"
# The runtime's step names for where an update stopped (``container-companion.ts``).
UPDATE_BACKUP_STEP = "backup"
UPDATE_PRE_UPDATE_STEP = "pre-update"
UPDATE_ASSERTION_STEP = "post-update-assertion"
# The digest the last accepted UPDATE ready recorded as applied (plan 06-44, WR-01). Kept outside
# the ``update`` record, which the next accepted update replaces, so a re-sent ready of a completed
# update is still recognised while a later update runs. An install's ready never sets it.
UPDATE_APPLIED_DIGEST_KEY = "update_applied_digest"

_DIGEST = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True, slots=True)
class CompanionUpdateOffer:
    """Where one installation stands against the catalog's current release."""

    state: str
    offered_version: str | None
    offered_digest: str | None
    deferred_digest: str | None
    last_backup_at: datetime | None
    error_code: str | None


def _operation_in_flight(installation: CompanionInstallation) -> bool:
    """Whether a lifecycle operation is queued, running, or still owes its teardown.

    One operation per installation is in flight at a time (D-26), so nothing new may start while
    this is true.
    """
    if installation.status in (
        COMPANION_INSTALLATION_STATUS_QUEUED,
        COMPANION_INSTALLATION_STATUS_RUNNING,
    ):
        return True
    return (
        installation.status == COMPANION_INSTALLATION_STATUS_FAILED
        and installation.next_attempt_at is not None
    )


def _update_record(installation: CompanionInstallation) -> dict[str, JsonValue]:
    recorded = (installation.external_resources or {}).get("update")
    return dict(recorded) if isinstance(recorded, dict) else {}


def _last_good_backup_at(installation: CompanionInstallation) -> datetime | None:
    state = installation.backup_state or {}
    value = state.get("last_ok_backup_at")
    if not isinstance(value, str) and state.get("last_backup_ok") is True:
        value = state.get("last_backup_at")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def update_offer_for(
    installation: CompanionInstallation, catalog_entry: CompanionCatalogEntry
) -> CompanionUpdateOffer:
    """The installation's update state against ``catalog_entry``. Pure: nothing is written.

    The offer rule (D-28): the catalog's current app digest is offered when it differs from the
    PINNED digest and from the one digest the tenant deferred -- and only to an installation whose
    install completed and that has no operation in flight. An offer equal to the pin is not an
    update (OPS-02 adjacency): nothing is offered, so no backup can ever be taken for it.
    """
    catalog_digest = catalog_entry.app.sha256
    serving = installation_is_serving(installation) and installation.removed_at is None
    in_flight = _operation_in_flight(installation)
    offered = (
        catalog_digest
        if serving
        and not in_flight
        and catalog_digest != installation.pinned_digest
        and catalog_digest != installation.deferred_digest
        else None
    )
    error_code: str | None = None
    if installation.operation == COMPANION_OPERATION_UPDATE and installation.status in (
        COMPANION_INSTALLATION_STATUS_QUEUED,
        COMPANION_INSTALLATION_STATUS_RUNNING,
    ):
        state = UPDATE_STATE_IN_PROGRESS
    elif (
        installation.operation == COMPANION_OPERATION_UPDATE
        and installation.status == COMPANION_INSTALLATION_STATUS_FAILED
    ):
        recorded = _update_record(installation).get("error_code")
        error_code = recorded if isinstance(recorded, str) else UPDATE_ERROR_ROLLED_BACK
        state = (
            UPDATE_STATE_ABORTED_BACKUP_FAILED
            if error_code == UPDATE_ERROR_BACKUP_FAILED
            else UPDATE_STATE_FAILED
        )
    elif offered is not None:
        state = UPDATE_STATE_AVAILABLE
    elif (
        serving
        and installation.deferred_digest == catalog_digest
        and catalog_digest != installation.pinned_digest
    ):
        state = UPDATE_STATE_DEFERRED
    else:
        state = UPDATE_STATE_UP_TO_DATE
    return CompanionUpdateOffer(
        state=state,
        offered_version=catalog_entry.version if offered is not None else None,
        offered_digest=offered,
        deferred_digest=installation.deferred_digest,
        last_backup_at=_last_good_backup_at(installation),
        error_code=error_code,
    )


def update_view(installation: CompanionInstallation) -> CompanionUpdateOffer:
    """``update_offer_for`` against the build's catalog; a kind no longer in it is up to date."""
    try:
        entry = catalog_entry(installation.companion_key)
    except KeyError:
        return CompanionUpdateOffer(
            state=UPDATE_STATE_UP_TO_DATE,
            offered_version=None,
            offered_digest=None,
            deferred_digest=installation.deferred_digest,
            last_backup_at=_last_good_backup_at(installation),
            error_code=None,
        )
    return update_offer_for(installation, entry)


async def record_update_offers(
    db: AsyncSession, installations: Iterable[CompanionInstallation]
) -> None:
    """Store each installation's offer as computed on read, committing only when one changed.

    The rows are re-read after the commit, so a caller can keep rendering them.
    """
    rows = list(installations)
    changed = False
    for installation in rows:
        offered = update_view(installation).offered_digest
        if installation.offered_digest != offered:
            installation.offered_digest = offered
            changed = True
    if changed:
        await db.commit()
        for installation in rows:
            await db.refresh(installation)


def _update_conflict(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": code, "message": message})


async def decide_companion_update(
    db: AsyncSession,
    *,
    auth: AuthContext,
    installation_id: UUID,
    decision: str,
    digest: str,
    now: datetime | None = None,
) -> CompanionInstallation:
    """Accept or defer ONE specific offered digest on the caller's own installation.

    Refusals, each writing nothing: another tenant's installation is 404; any operation in flight
    is 409 ``companion_operation_in_progress`` (one lifecycle operation at a time); a digest that
    is not the one currently offered -- including an update when nothing is offered -- is 409
    ``companion_update_not_offered``. Accepting the digest already applied, or deferring the one
    already deferred, is a 200 no-op.

    Accept records ``external_resources["update"] = {from_digest, to_digest, requested_at, ...}``,
    moves the pin and starts the ``update`` operation running: the projection then emits the new
    digest, and the runtime backs up, swaps and asserts. Defer records the digest, so it is no
    longer offered; a later release is offered the same way.
    """
    moment = now or datetime.now(UTC)
    owned = await get_installation(db, auth=auth, installation_id=installation_id)
    installation = (
        await db.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == owned.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if installation.removed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    if _operation_in_flight(installation):
        raise _update_conflict(
            "companion_operation_in_progress",
            f"The {installation.operation} operation is still in progress; try again when it ends.",
        )
    entry = _catalog_entry_or_404(installation.companion_key)
    serving = installation_is_serving(installation)
    already_applied = (
        decision == "accept"
        and serving
        and installation.pinned_digest == digest
        and installation.applied_digest == digest
    )
    already_deferred = decision == "defer" and serving and installation.deferred_digest == digest
    if already_applied or already_deferred:
        # Nothing to do (OPS-02 idempotency): release the lock, and re-read the row the rollback
        # expired so the caller can still render it.
        await db.rollback()
        await db.refresh(installation)
        return installation
    offer = update_offer_for(installation, entry)
    if offer.offered_digest is None or digest != offer.offered_digest:
        raise _update_conflict(
            "companion_update_not_offered",
            "That version is not the update on offer for this installation.",
        )

    if decision == "defer":
        installation.deferred_digest = digest
        installation.offered_digest = None
    else:
        installation.external_resources = _with_resource(
            installation,
            "update",
            {
                "from_digest": installation.pinned_digest,
                "from_version": installation.version,
                "to_digest": digest,
                "to_version": entry.version,
                "requested_at": _utc_iso(moment),
            },
        )
        installation.pinned_digest = digest
        installation.version = entry.version
        installation.offered_digest = None
        if installation.deferred_digest == digest:
            installation.deferred_digest = None
        installation.operation = COMPANION_OPERATION_UPDATE
        installation.status = COMPANION_INSTALLATION_STATUS_RUNNING
        installation.step = None
        installation.error = None
        installation.operation_started_at = moment
        # The runtime must report within the deadline, or the worker fails the update and the
        # pin goes back (``revert_update_pin``).
        installation.next_attempt_at = moment + timedelta(seconds=REPORT_DEADLINE_SECONDS)
    await db.commit()
    await db.refresh(installation)
    return installation


# ---------------------------------------------------------------------------
# Uninstall (plan 06-20): confirmed by name, billing stopped first, data destroyed only on an
# explicit instruction (research Pitfall 12, APP-07, APP-08).
# ---------------------------------------------------------------------------

# How long the runtime has to report ``removed`` before the control plane tears down without it.
UNINSTALL_REMOVAL_DEADLINE_SECONDS = 30 * 60
# Where the uninstall operation's own progress is recorded in ``external_resources``.
UNINSTALL_RECORD_KEY = "uninstall"


def companion_display_name(companion_key: str) -> str:
    """The name a tenant types to confirm an uninstall.

    The catalog's display name, or the key itself for a kind this build no longer lists, so such
    an installation can still be removed.
    """
    try:
        return catalog_entry(companion_key).display_name
    except KeyError:
        return companion_key


async def request_companion_uninstall(
    db: AsyncSession,
    *,
    auth: AuthContext,
    installation_id: UUID,
    confirm: str,
    now: datetime | None = None,
) -> CompanionInstallation:
    """Start uninstalling the caller's own installation, confirmed by its name.

    Refusals, each writing nothing: another tenant's installation, or one already removed, is 404;
    any operation still queued, running or owing its teardown -- another uninstall included -- is
    409 ``companion_operation_in_progress``; a queued or running tenant export that still owes
    this installation's data (its component ``pending`` or ``uploaded``) is 409
    ``companion_export_in_progress`` -- the uninstall would destroy the database and media bucket
    the export is copying (CR-05), so it waits for the export to end, while the entry keeps
    projecting ``present`` with its ``exportRequests``; a ``confirm`` that is not the companion's
    name, exactly and case-sensitively after trimming surrounding whitespace, is 422
    ``companion_uninstall_confirmation_mismatch``. A tenant with no export in flight is never
    refused for one: the export is an offer, not a precondition.

    Accepted, the installation's operation becomes ``uninstall``, ``running`` and due at once:
    the lifecycle worker stops the billing line item FIRST (APP-08), and the projection starts
    telling the runtime ``lifecycle: "remove"`` -- the only instruction that destroys the
    companion's data on the host. Any ``removed`` report recorded before this request is cleared,
    so only a report that answers THIS uninstall lets the teardown start.
    """
    moment = now or datetime.now(UTC)
    owned = await get_installation(db, auth=auth, installation_id=installation_id)
    installation = (
        await db.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == owned.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if installation.removed_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    if _operation_in_flight(installation):
        raise _update_conflict(
            "companion_operation_in_progress",
            f"The {installation.operation} operation is still in progress; try again when it ends.",
        )
    # Checked while the installation row is locked: an export created concurrently reads the
    # serving installations FOR SHARE, so it either sees this row before the lock (and is seen
    # here) or waits for this transaction and no longer sees the installation as serving.
    if await export_owes_installation(db, user_id=installation.user_id, installation_id=owned.id):
        raise _update_conflict(
            "companion_export_in_progress",
            "A data export that includes this companion is still being prepared. "
            "Uninstall it once the export is ready to download.",
        )
    display_name = companion_display_name(installation.companion_key)
    if confirm.strip() != display_name:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "companion_uninstall_confirmation_mismatch",
                "message": f"Type {display_name} exactly to confirm the uninstall.",
            },
        )

    resources = {
        key: value
        for key, value in (installation.external_resources or {}).items()
        if key not in (REMOVED_REPORTED_AT_KEY, HOST_REMOVAL_UNCONFIRMED_KEY)
    }
    resources[UNINSTALL_RECORD_KEY] = {
        "requested_at": _utc_iso(moment),
        "from_operation": installation.operation,
        "from_status": installation.status,
    }
    installation.external_resources = resources
    installation.operation = COMPANION_OPERATION_UNINSTALL
    installation.status = COMPANION_INSTALLATION_STATUS_RUNNING
    installation.step = None
    installation.error = None
    installation.offered_digest = None
    installation.operation_started_at = moment
    # Due now: stopping the line item is the pipeline's first step and waits for nothing.
    installation.next_attempt_at = moment
    await db.commit()
    await db.refresh(installation)
    return installation


def revert_update_pin(
    installation: CompanionInstallation, *, error_code: str, moment: datetime
) -> None:
    """Return a failed update's pin (and version) to the digest it was accepted from.

    Called for every way an update fails -- a ``failed`` report from the runtime and the reporting
    deadline -- so the manifest never keeps pointing at a digest the runtime refused or rolled
    back (T-06-18-03). The accepted digest is offered again on the next read.
    """
    record = _update_record(installation)
    from_digest = record.get("from_digest")
    if isinstance(from_digest, str) and _DIGEST.match(from_digest):
        installation.pinned_digest = from_digest
    from_version = record.get("from_version")
    if isinstance(from_version, str) and from_version:
        installation.version = from_version
    record["error_code"] = error_code
    record["failed_at"] = _utc_iso(moment)
    installation.external_resources = _with_resource(installation, "update", record)


def _update_error_code(report: CompanionInstallReport) -> str:
    if report.step == UPDATE_BACKUP_STEP:
        return UPDATE_ERROR_BACKUP_FAILED
    if report.step == UPDATE_PRE_UPDATE_STEP:
        return UPDATE_ERROR_ABORTED
    return UPDATE_ERROR_ROLLED_BACK


# ---------------------------------------------------------------------------
# The runtime's report (plan 06-11): progress, log lines and data-asserted readiness.
# ---------------------------------------------------------------------------


# Where the host's raised conditions live (plan 06-44, G-06-2): ``external_resources
# ["host_conditions"][code] = {"detail": <redacted text or null>, "raised_at": <UTC ISO>}``. One
# entry per code, replaced on each raise and removed on clear. Operators read it from the row and
# the backend log; the tenant response does not carry it.
HOST_CONDITIONS_KEY = "host_conditions"


@dataclass(frozen=True, slots=True)
class InstallReportOutcome:
    """What one accepted report left behind: the job status and step, and how many lines landed."""

    status: str
    step: str | None
    stored_lines: int


def _report_conflict(code: str, message: str, **facts: str) -> HTTPException:
    """A 409 whose ``detail`` names the machine facts the runtime decides on (plan 06-40, CR-03).

    The runtime reads ``code`` and ``status``: only ``companion_install_terminal`` with status
    ``complete`` means its report was already recorded; every other 409 is a refusal it must act
    on (a ``ready`` for an update the deadline already failed is rolled back on the host).

    That answer is also given to a re-sent update ``ready`` the control plane already recorded
    while a LATER update runs (plan 06-44, WR-01): the host settles on the digest it runs instead
    of restoring the pre-update backup of an update that completed.
    """
    return HTTPException(
        status.HTTP_409_CONFLICT, detail={"code": code, "message": message, **facts}
    )


async def _installation_for_report(
    db: AsyncSession, *, auth: AuthContext, installation_id: UUID
) -> CompanionInstallation:
    """The installation this runtime key may report on, locked, or 404 -- never 403.

    The binding is the whole authorization (T-06-11-01): the key must be bound to an environment,
    and that environment must be the installation's own agent, owned by the key's tenant and not
    archived. A report from any other agent's key, for a removed installation, or for an
    installation that does not exist are all the same 404, so a key learns nothing about
    installations that are not its own.
    """
    bound = auth.api_key.environment_id if auth.api_key is not None else None
    if bound is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    installation = (
        await db.execute(
            select(CompanionInstallation)
            .join(AgentEnvironment, AgentEnvironment.id == CompanionInstallation.environment_id)
            .where(
                CompanionInstallation.id == installation_id,
                CompanionInstallation.user_id == auth.user_id,
                CompanionInstallation.environment_id == bound,
                CompanionInstallation.removed_at.is_(None),
                AgentEnvironment.user_id == auth.user_id,
                AgentEnvironment.archived_at.is_(None),
            )
            .with_for_update(of=CompanionInstallation)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    return installation


def _check_job_report(installation: CompanionInstallation, report: CompanionInstallReport) -> None:
    """Forward-only (D-26, T-06-11-06): a job report must be for the operation in flight.

    One lifecycle operation per installation is in flight at a time, so a report naming another
    operation is a stale or confused runtime and is refused rather than applied to the wrong job.
    ``complete`` and ``failed`` are terminal for their operation; ``queued`` has not been handed
    to the runtime yet (the projection only emits a running installation), so nothing can report
    on it.

    Update identity (plan 06-50, G-06-4, D-28): an update report that carries ``to_digest`` must
    name the update in flight, ``external_resources["update"]["to_digest"]``, exactly. Any other
    value -- a missing record included -- is a report the host made for another update (typically
    one queued across an outage while the worker deadline-failed that update and the tenant
    accepted a later one) and is refused with ``companion_update_identity_mismatch``. The check
    sits BEFORE the terminal refusal: a stale report is refused as another update's whatever the
    current update's status, never answered as if it were about the current one. A report without
    ``to_digest`` (an older CLI) is not checked. The caller has already answered a re-sent ready
    the control plane recorded (``_is_recorded_update_ready``, CR-01), so that answer is kept.
    """
    if report.operation != installation.operation:
        raise _report_conflict(
            "companion_report_operation_mismatch",
            f"This installation's current operation is {installation.operation}, "
            f"not {report.operation}.",
            operation=installation.operation,
        )
    if (
        report.to_digest is not None
        and installation.operation == COMPANION_OPERATION_UPDATE
        and report.to_digest != _update_record(installation).get("to_digest")
    ):
        raise _report_conflict(
            "companion_update_identity_mismatch",
            "This report is for another update than the one in flight.",
            operation=installation.operation,
            status=installation.status,
        )
    if installation.status in (
        COMPANION_INSTALLATION_STATUS_COMPLETE,
        COMPANION_INSTALLATION_STATUS_FAILED,
    ):
        raise _report_conflict(
            "companion_install_terminal",
            f"The {installation.operation} operation already ended ({installation.status}).",
            operation=installation.operation,
            status=installation.status,
        )
    if installation.status == COMPANION_INSTALLATION_STATUS_QUEUED:
        raise _report_conflict(
            "companion_install_not_running",
            f"The {installation.operation} operation has not started running yet.",
            status=installation.status,
        )


def _ready_assertion_passes(
    installation: CompanionInstallation, report: CompanionInstallReport
) -> bool:
    """The DATA assertion a ``ready`` report must carry (T-06-11-03, plan 06-18).

    An install is ready when Postiz answered connected with the seeded key and exactly one
    organization exists. An update is ready when Postiz answered connected and the organization
    count the runtime took just before the swap is unchanged: Postiz pushes its schema with
    ``--accept-data-loss`` at every start, so an update must prove it lost nothing.
    """
    assertion = report.assertion
    if assertion is None or not assertion.connected:
        return False
    if installation.operation == COMPANION_OPERATION_UPDATE:
        return (
            assertion.previous_organizations is not None
            and assertion.organizations >= 1
            and assertion.organizations == assertion.previous_organizations
        )
    return assertion.organizations == 1


def _is_recorded_update_ready(
    installation: CompanionInstallation, report: CompanionInstallReport
) -> bool:
    """Whether ``report`` re-sends the ready of an update the control plane already recorded.

    Plan 06-44 (WR-01): update A's ready was recorded and A completed, but the host never heard
    the answer; the tenant then accepted update B, so the operation is ``update``/``running`` again
    with a new pin. A's re-sent ready names the digest the control plane records as applied, which
    is not B's pin. Only a digest an UPDATE's ready recorded counts: a deadline-failed update never
    recorded its digest, and an install's ready never records one here, so both still get their
    refusal (and the host its rollback).

    CR-01 (06-REVIEW, gap closure): B may also have ENDED before A's ready arrives -- typically the
    worker failed it on its reporting deadline while the host was offline, and reverted the pin to
    A's digest. The recorded key still identifies A's ready, so it is recognised for any status of
    the current update operation. Only while an update is still running does its own pin
    (a re-sent ready of the running update itself) fall through to normal processing.
    """
    return (
        report.phase == "ready"
        and report.operation == COMPANION_OPERATION_UPDATE
        and installation.operation == COMPANION_OPERATION_UPDATE
        and report.applied_digest is not None
        and report.applied_digest == installation.applied_digest
        and (installation.external_resources or {}).get(UPDATE_APPLIED_DIGEST_KEY)
        == report.applied_digest
        # The update in flight is not the one that recorded this digest (a later one), or the
        # current update already ended: either way the digest runs and nothing must roll back.
        and (
            installation.status != COMPANION_INSTALLATION_STATUS_RUNNING
            or report.applied_digest != installation.pinned_digest
        )
    )


def _with_resource(
    installation: CompanionInstallation, key: str, value: JsonValue
) -> dict[str, JsonValue]:
    # A fresh dict, so the ORM sees the JSONB column change (in-place mutation is not tracked).
    return {**(installation.external_resources or {}), key: value}


async def record_install_report(
    db: AsyncSession,
    *,
    auth: AuthContext,
    installation_id: UUID,
    report: CompanionInstallReport,
    now: datetime | None = None,
) -> InstallReportOutcome:
    """Apply one runtime report to one installation and commit.

    Refusals come first and write nothing: 404 for any key but the installation's own agent's
    (``_installation_for_report``), 409 for a job report on another operation or on a terminal or
    not-yet-running job (the detail names the operation and status, plan 06-40), 409
    ``companion_install_terminal`` with status ``complete`` for a re-sent update ``ready`` the
    control plane already recorded while a later update runs or after it ended (plan 06-44,
    WR-01; CR-01: checked before the terminal refusal, so the host settles on the digest it runs
    instead of rolling back a completed update), 409 ``companion_update_identity_mismatch`` for an
    update report whose ``to_digest`` names another update than the one in flight (plan 06-50,
    G-06-4; the recorded-ready recognition above still answers first, so CR-01 holds even when the
    re-sent ready carries its own, older ``to_digest``), 422 for a ``ready`` whose
    data assertion does not pass, and 409 ``companion_ready_digest_mismatch`` for any other
    ``ready`` whose ``applied_digest`` is not the installation's ``pinned_digest`` (WR-01).

    * ``progress`` moves the step and pushes the reporting deadline out.
    * ``ready`` requires the DATA assertion -- Postiz answered connected with the seeded key and
      exactly one organization exists (T-06-11-03) -- and records the applied digest and the
      Postiz admin user id. The job stays ``running`` at ``READY_STEP``, due for the lifecycle
      worker, which runs the completion steps and only then marks it complete.
    * ``failed`` fails the job with the redacted reason and leaves it due for the worker, which
      tears down every step this operation completed, in reverse.
    * ``backup`` merges into ``backup_state`` in any status; ``removed`` is recorded for the
      uninstall pipeline; ``condition`` (plan 06-44) records a raised host condition under
      ``external_resources["host_conditions"][code]`` with its detail redacted, or removes a
      cleared one, and logs the code and state (never the detail). None of the three changes
      the job: operation, status, step, error and next_attempt_at stay as they were.

    Every line and the error are redacted against the installation's own credentials BEFORE
    anything is inserted (T-06-11-02); the log keeps the newest ``MAX_LOG_LINES_PER_OPERATION``
    lines of each operation (T-06-11-04).
    """
    moment = now or datetime.now(UTC)
    installation = await _installation_for_report(db, auth=auth, installation_id=installation_id)

    if report.phase == "backup":
        assert report.backup is not None  # the schema requires it for this phase
        installation.backup_state = _merged_backup_state(installation, report.backup)
        installation.last_report_at = moment
        await db.commit()
        return InstallReportOutcome(installation.status, installation.step, stored_lines=0)
    if report.phase == "removed":
        installation.external_resources = _with_resource(
            installation, REMOVED_REPORTED_AT_KEY, _utc_iso(moment)
        )
        if (
            installation.operation == COMPANION_OPERATION_UNINSTALL
            and installation.status == COMPANION_INSTALLATION_STATUS_RUNNING
        ):
            # The host side is gone: the uninstall pipeline's teardown is due now (plan 06-20).
            installation.next_attempt_at = moment
        installation.last_report_at = moment
        await db.commit()
        return InstallReportOutcome(installation.status, installation.step, stored_lines=0)
    if report.phase == "condition":
        assert report.condition is not None  # the schema requires it for this phase
        condition = report.condition
        existing = (installation.external_resources or {}).get(HOST_CONDITIONS_KEY)
        conditions: dict[str, JsonValue] = dict(existing) if isinstance(existing, dict) else {}
        if condition.state == "raised":
            detail: str | None = None
            if condition.detail is not None:
                redact = await installation_redactor(db, installation_id=installation.id)
                detail = redact(condition.detail)[:MAX_INSTALL_REPORT_ERROR_CHARS]
            conditions[condition.code] = {"detail": detail, "raised_at": _utc_iso(moment)}
        else:
            conditions.pop(condition.code, None)
        installation.external_resources = _with_resource(
            installation, HOST_CONDITIONS_KEY, conditions
        )
        installation.last_report_at = moment
        await db.commit()
        # The code and state only: the detail is host-composed text and never reaches the log.
        log.warning(
            "companion host condition %s %s",
            condition.code,
            condition.state,
            extra={"installation_id": str(installation.id), "condition_code": condition.code},
        )
        return InstallReportOutcome(installation.status, installation.step, stored_lines=0)

    if _is_recorded_update_ready(installation, report):
        # WR-01 (plan 06-44): this ready was recorded and its update completed; a later update is
        # running now, or already ended (CR-01: deadline-failed with the pin reverted to this
        # digest). Checked BEFORE the terminal refusal, which would answer ``failed`` and make the
        # host restore the pre-update backup of a completed update. Answered as complete, before
        # anything is redacted or stored, so nothing is written and the host settles. Also checked
        # before the update-identity refusal in ``_check_job_report`` (plan 06-50): the re-sent
        # ready carries its own update's ``to_digest``, not the later update's.
        raise _report_conflict(
            "companion_install_terminal",
            "This update's ready was already recorded; the installation runs that digest.",
            operation=installation.operation,
            status=COMPANION_INSTALLATION_STATUS_COMPLETE,
        )
    _check_job_report(installation, report)
    if report.phase == "ready" and not _ready_assertion_passes(installation, report):
        # Reachability is not readiness: the job stays running and nothing is written.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "companion_ready_assertion_failed",
                "message": (
                    "Ready requires Postiz to answer connected with the seeded key and "
                    "the organizations it had before the update."
                    if installation.operation == COMPANION_OPERATION_UPDATE
                    else "Ready requires Postiz to answer connected with the seeded key and "
                    "exactly one organization."
                ),
            },
        )
    if report.phase == "ready" and report.applied_digest != installation.pinned_digest:
        # WR-01 (plan 06-40): the runtime may only claim to run the digest this installation is
        # pinned to. Refused before anything is written, so the pin and what the control plane
        # records as running can never diverge.
        raise _report_conflict(
            "companion_ready_digest_mismatch",
            "The runtime reported a digest other than the one this installation is pinned to.",
        )

    redact = await installation_redactor(db, installation_id=installation.id)
    stored_lines = await _store_log_lines(db, installation, report, redact)

    if report.phase == "failed":
        installation.status = COMPANION_INSTALLATION_STATUS_FAILED
        installation.error = redact(report.error or "")[:MAX_INSTALL_REPORT_ERROR_CHARS]
        if report.step is not None:
            installation.step = report.step
        if installation.operation == COMPANION_OPERATION_UPDATE:
            # The runtime aborted (backup failed) or rolled back: the previous digest is what runs,
            # so the manifest goes back to naming it (T-06-18-03).
            revert_update_pin(installation, error_code=_update_error_code(report), moment=moment)
        # Due now: the worker tears down what this operation provisioned.
        installation.next_attempt_at = moment
    elif report.phase == "ready":
        installation.applied_digest = report.applied_digest
        installation.external_resources = _with_resource(
            installation, "admin_user_id", report.admin_user_id
        )
        if installation.operation == COMPANION_OPERATION_UPDATE:
            # What lets a re-sent copy of this ready be recognised once a later update runs.
            installation.external_resources = _with_resource(
                installation, UPDATE_APPLIED_DIGEST_KEY, report.applied_digest
            )
        installation.step = READY_STEP
        # Due now: the worker completes the job on its next pass.
        installation.next_attempt_at = moment
    elif installation.step != READY_STEP:
        # A progress report after ``ready`` must not move the job back off it.
        if report.step is not None:
            installation.step = report.step
        installation.next_attempt_at = moment + timedelta(seconds=REPORT_DEADLINE_SECONDS)
    installation.last_report_at = moment
    await db.commit()
    return InstallReportOutcome(installation.status, installation.step, stored_lines)


# ---------------------------------------------------------------------------
# Redaction (research Pitfall 9, T-06-11-02) and the bounded log (T-06-11-04).
# ---------------------------------------------------------------------------


def credential_forms(value: str) -> frozenset[str]:
    """Every spelling of one credential a log line can plausibly carry.

    The value itself and its URL-encoded forms (path-style, form-style, and each with lowercase
    percent escapes), because a connection string or a query parameter is exactly where a
    companion prints one. An empty value has no forms: replacing "" would rewrite every line.
    """
    if not value:
        return frozenset()
    encoded = {quote(value, safe=""), quote(value), quote_plus(value)}
    lowered = {_PERCENT_ESCAPE.sub(lambda match: match.group(0).lower(), form) for form in encoded}
    return frozenset({value, *encoded, *lowered})


class InstallationRedactor:
    """Replace every form of every installation credential in a text with ``[redacted]``."""

    def __init__(self, secret_values: Iterable[str]) -> None:
        forms = {form for value in secret_values for form in credential_forms(value)}
        # Longest first, so a form that contains another is replaced whole.
        self._forms = sorted(forms, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        # Bounded re-scan: a replacement can never create a new occurrence in practice, but the
        # guarantee "no stored text contains a form" is checked rather than assumed.
        for _ in range(3):
            found = False
            for form in self._forms:
                if form in text:
                    text = text.replace(form, REDACTED)
                    found = True
            if not found:
                return text
        return REDACTED


async def installation_redactor(db: AsyncSession, *, installation_id: UUID) -> InstallationRedactor:
    """A redactor over this installation's own decrypted credentials, decrypted once per report.

    Fails CLOSED: a credential that cannot be decrypted cannot be redacted, so nothing is stored
    and the report is refused. The values live only inside the redactor and are never logged.
    """
    rows = await db.execute(
        select(
            CompanionInstallationSecret.encrypted_value,
            CompanionInstallationSecret.nonce,
            CompanionInstallationSecret.key_version,
        ).where(CompanionInstallationSecret.installation_id == installation_id)
    )
    try:
        values = [
            decrypt_with_key_version(ciphertext, nonce, key_version)
            for ciphertext, nonce, key_version in rows
        ]
    except Exception:  # noqa: BLE001 - any decrypt failure means we cannot redact; refuse.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "companion_credentials_unreadable",
                "message": "This installation's credentials cannot be read, so nothing is stored.",
            },
        ) from None
    return InstallationRedactor(values)


async def _store_log_lines(
    db: AsyncSession,
    installation: CompanionInstallation,
    report: CompanionInstallReport,
    redact: Callable[[str], str],
) -> int:
    """Insert the report's lines, redacted, with a per-installation monotonically rising ``seq``.

    The installation row is locked for the whole report, so two reports cannot interleave their
    sequence numbers. After the insert, only the newest ``MAX_LOG_LINES_PER_OPERATION`` lines of
    this operation are kept, so a crash-looping unit cannot fill the table.
    """
    if not report.lines:
        return 0
    last_seq = await db.scalar(
        select(func.max(CompanionInstallLogLine.seq)).where(
            CompanionInstallLogLine.installation_id == installation.id
        )
    )
    first = (last_seq or 0) + 1
    db.add_all(
        CompanionInstallLogLine(
            installation_id=installation.id,
            operation=installation.operation,
            seq=first + offset,
            stream=entry.stream,
            line=redact(entry.line),
        )
        for offset, entry in enumerate(report.lines)
    )
    await db.flush()
    oldest_dropped = await db.scalar(
        select(CompanionInstallLogLine.seq)
        .where(
            CompanionInstallLogLine.installation_id == installation.id,
            CompanionInstallLogLine.operation == installation.operation,
        )
        .order_by(CompanionInstallLogLine.seq.desc())
        .offset(MAX_LOG_LINES_PER_OPERATION)
        .limit(1)
    )
    if oldest_dropped is not None:
        await db.execute(
            delete(CompanionInstallLogLine).where(
                CompanionInstallLogLine.installation_id == installation.id,
                CompanionInstallLogLine.operation == installation.operation,
                CompanionInstallLogLine.seq <= oldest_dropped,
            )
        )
    return len(report.lines)


def _merged_backup_state(
    installation: CompanionInstallation, backup: CompanionBackupReport
) -> dict[str, JsonValue]:
    """Merge one backup report into ``backup_state`` without touching the job (plans 06-15/16/18).

    A marker is kept only for a SUCCESSFUL backup, paired with that backup's completion time --
    the pair plan 06-16 needs to pick a restorable target. The newest ``MAX_BACKUP_MARKERS``
    markers are kept, ordered by when they were written; a re-sent marker id replaces its entry.
    """
    state: dict[str, JsonValue] = dict(installation.backup_state or {})
    completed_at = _utc_iso(backup.completed_at)
    state["last_backup_at"] = completed_at
    state["last_backup_ok"] = backup.ok
    state["last_backup_label"] = backup.label
    if backup.ok:
        # The newest SUCCESSFUL backup, which a failed later attempt must not hide (plan 06-18's
        # update card shows it as the backup an update is taken after).
        state["last_ok_backup_at"] = completed_at
    if backup.db_bytes is not None:
        state["db_bytes"] = backup.db_bytes
    existing = state.get("markers")
    markers: list[dict[str, JsonValue]] = (
        [entry for entry in existing if isinstance(entry, dict)]
        if isinstance(existing, list)
        else []
    )
    if backup.ok and backup.marker is not None:
        marker_id = backup.marker.id
        markers = [entry for entry in markers if entry.get("id") != marker_id]
        markers.append(
            {
                "id": marker_id,
                "token": backup.marker.token,
                "written_at": _utc_iso(backup.marker.written_at),
                "backup_completed_at": completed_at,
            }
        )
        # Every stored timestamp is normalized to UTC, so the ISO strings sort chronologically.
        markers.sort(key=lambda entry: str(entry.get("written_at")))
    state["markers"] = cast(list[JsonValue], markers[-MAX_BACKUP_MARKERS:])
    return state


def _utc_iso(moment: datetime) -> str:
    """One spelling for every stored timestamp: UTC, ISO 8601 (a naive value is taken as UTC)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()
