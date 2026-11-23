"""The single-tenant PITR rehearsal: plan it, hand out a read-only key, record what rows said.

**What counts as evidence (D-16, OPS-03).** A blank Postiz answers HTTP 200, and ``/`` answers 307
with its backend dead, so reachability proves nothing about a restore. A rehearsal restores ONE
installation's database to a chosen instant in a scratch environment and passes only when the
marker row written BEFORE that instant is present and the marker row written AFTER it is absent.
Plan 06-15's runtime writes those markers into ``iforge_rehearsal.marker`` just before each
scheduled backup and reports them; ``backup_state["markers"]`` keeps each with its backup's
completion time.

**Choosing the instant.** The newest marker pair ``(k, k+1)`` where ``k``'s backup completed: the
target is the midpoint of ``backup_completed_at(k)`` and ``written_at(k+1)``. A base backup that
finished before the target and continuous WAL after it cover that instant, and it lies strictly
between the two markers. It is rendered in UTC with microsecond precision and an explicit
``+00:00`` offset, the form pgBackRest's ``--target`` takes without guessing a time zone.

**The credential (OPS-04).** Each rehearsal gets its own B2 key, minted through the backup-repo
adapter: ``listFiles`` and ``readFiles`` only, the installation's prefix, valid for at most an
hour. It is neither the runtime's write key nor the operator's delete-capable key, and it is
revoked when the result is recorded. The runner also restores with ``--archive-mode=off``, so a
scratch restore can never push WAL back into the tenant's repository (T-06-16-01).

**Bookkeeping.** A planned rehearsal is kept in ``backup_state["rehearsals"]`` (the newest
``MAX_REHEARSALS``) with its id, target, the two tokens' SHA-256 hashes, its key id and, once
recorded, its outcome. A result is accepted only for a rehearsal that was planned and is still
pending (T-06-16-04); a second result is refused. Each result is ONE audited control-plane event
naming the installation, the target and the outcome -- never the cipher pass, a key or a token.

**Research assumption A4.** Postiz runs ``prisma db push`` on every start; that it leaves the
``iforge_rehearsal`` schema alone is assumed, not verified. The runner reports whether the schema
survived in the restored database, and a result saying it did not is a FAILED rehearsal that
names A4.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.services import companion_backup_repo
from app.services.audit import record_control_plane_audit
from app.services.companion_lifecycle_steps import CompanionStepError
from app.services.companion_postiz import BACKUP_CIPHER_PASS_ATOM, postiz_backup_handles
from app.services.vault_crypto import decrypt_with_key_version

log = logging.getLogger(__name__)

REHEARSAL_AUDIT_ACTION = "companion.pitr_rehearsal"
MAX_REHEARSALS = 10
MAX_DETAIL_CHARS = 1000

OUTCOME_PASSED = "passed"
OUTCOME_FAILED = "failed"

# Named refusals; the route maps each to its HTTP status.
INSTALLATION_NOT_FOUND = "companion_installation_not_found"
NEEDS_TWO_MARKERS = "companion_rehearsal_needs_two_markers"
NO_BACKUP_REPOSITORY = "companion_rehearsal_no_backup_repository"
BACKUP_PROVIDER_FAILED = "companion_rehearsal_backup_provider_failed"
REHEARSAL_NOT_FOUND = "companion_rehearsal_not_found"
ALREADY_RECORDED = "companion_rehearsal_already_recorded"

# Why a rehearsal failed, most fundamental first. Codes, never free text, reach the audit row.
REASON_SCHEMA_LOST = "assumption_a4_schema_lost"
REASON_SCHEMA_UNCHECKED = "assumption_a4_unchecked"
REASON_PRESENT_MISSING = "present_marker_missing"
REASON_ABSENT_FOUND = "absent_marker_found"
REASON_RUNNER_FAILED = "runner_reported_failure"
REASON_TEXT: dict[str, str] = {
    REASON_SCHEMA_LOST: (
        "the iforge_rehearsal schema did not survive in the restored database; research "
        "assumption A4 (Postiz's prisma db push leaves other schemas alone) does not hold"
    ),
    REASON_SCHEMA_UNCHECKED: (
        "the installation's first rehearsal did not report whether the iforge_rehearsal schema "
        "survived; research assumption A4 must be checked before a rehearsal can pass"
    ),
    REASON_PRESENT_MISSING: "the marker written before the target is missing from the restore",
    REASON_ABSENT_FOUND: "the marker written after the target is present in the restore",
    REASON_RUNNER_FAILED: "the runner reported a failed rehearsal",
}
REDACTED = "[redacted]"


class RehearsalError(Exception):
    """A named refusal the admin route answers with ``status_code`` and ``code``."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# The target: pure, so its precision and its bounds are testable.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RehearsalMarker:
    id: int
    token: str = field(repr=False)
    written_at: datetime
    backup_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class RehearsalTarget:
    target_time: datetime
    present: RehearsalMarker
    absent: RehearsalMarker
    # The OLDEST marker the control plane knows (research A4): if the restore still holds it, the
    # iforge_rehearsal schema survived every Postiz start -- and its prisma db push -- since.
    survivor: RehearsalMarker

    @property
    def target_text(self) -> str:
        return format_rehearsal_target(self.target_time)


def format_rehearsal_target(moment: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``: UTC, microseconds, the offset spelled out.

    A naive value is taken as UTC. pgBackRest hands ``--target`` to PostgreSQL's
    ``recovery_target_time``; an explicit offset means no server time zone is ever consulted.
    """
    utc = (moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)).astimezone(UTC)
    return f"{utc.strftime('%Y-%m-%dT%H:%M:%S.%f')}+00:00"


def _moment(value: JsonValue | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _markers(value: JsonValue | None) -> list[RehearsalMarker]:
    """The well-formed markers in ``backup_state["markers"]``, oldest written first."""
    if not isinstance(value, list):
        return []
    markers: list[RehearsalMarker] = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        marker_id, token = entry.get("id"), entry.get("token")
        written_at = _moment(entry.get("written_at"))
        if (
            not isinstance(marker_id, int)
            or isinstance(marker_id, bool)
            or not isinstance(token, str)
            or not token
            or written_at is None
        ):
            continue
        markers.append(
            RehearsalMarker(
                id=marker_id,
                token=token,
                written_at=written_at,
                backup_completed_at=_moment(entry.get("backup_completed_at")),
            )
        )
    markers.sort(key=lambda marker: marker.written_at)
    return markers


def choose_rehearsal_target(markers: JsonValue | None) -> RehearsalTarget | None:
    """The newest restorable instant between two markers, or ``None`` when there is none.

    For consecutive markers ``(k, k+1)``, newest first: ``k``'s backup must have completed, and
    the target is the midpoint of that completion and ``k+1``'s write -- strictly between both.
    The survivor is the oldest marker, which was written before the target too.
    """
    parsed = _markers(markers)
    for earlier, later in reversed(list(zip(parsed, parsed[1:], strict=False))):
        low = earlier.backup_completed_at
        if low is None:
            continue
        high = later.written_at
        midpoint = low + (high - low) / 2
        if low < midpoint < high:
            return RehearsalTarget(
                target_time=midpoint, present=earlier, absent=later, survivor=parsed[0]
            )
    return None


def rehearsal_outcome(
    *,
    passed: bool,
    present_found: bool,
    absent_found: bool,
    schema_survived: bool | None,
    schema_checked_before: bool = False,
) -> tuple[str, str | None]:
    """The recorded outcome and, for a failure, its reason code. Rows decide; a claim cannot.

    A pass needs the earlier marker present, the later one absent, the schema not reported lost
    -- and, until one rehearsal of the installation has shown it survived, reported at all
    (research A4) -- AND the runner's own verdict: the runner can fail a rehearsal, never pass
    one the rows fail.
    """
    if schema_survived is False:
        return OUTCOME_FAILED, REASON_SCHEMA_LOST
    if not passed and schema_survived is None:
        # The runner never reached the rows (an operational error): its failure is the reason.
        return OUTCOME_FAILED, REASON_RUNNER_FAILED
    if not present_found:
        return OUTCOME_FAILED, REASON_PRESENT_MISSING
    if absent_found:
        return OUTCOME_FAILED, REASON_ABSENT_FOUND
    if schema_survived is None and not schema_checked_before:
        return OUTCOME_FAILED, REASON_SCHEMA_UNCHECKED
    if not passed:
        return OUTCOME_FAILED, REASON_RUNNER_FAILED
    return OUTCOME_PASSED, None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Planning a rehearsal.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RehearsalBundle:
    """What the operator's runner needs. ``read_key`` and ``cipher_pass`` are never logged."""

    rehearsal_id: UUID
    installation_id: UUID
    stanza: str
    repo_path: str
    s3_endpoint: str
    s3_region: str
    bucket: str
    read_key_id: str
    target_time: str
    expect_present_token: str
    expect_absent_token: str
    expect_survivor_token: str
    expires_at: datetime
    read_key: str = field(repr=False)
    cipher_pass: str = field(repr=False)


_REPOSITORY_HANDLES = ("stanza", "repo_path", "bucket_id", "bucket", "s3_endpoint", "s3_region")


async def _installation(
    db: AsyncSession, installation_id: UUID, *, for_update: bool
) -> CompanionInstallation:
    query = select(CompanionInstallation).where(CompanionInstallation.id == installation_id)
    if for_update:
        query = query.with_for_update()
    installation = await db.scalar(query.execution_options(populate_existing=True))
    if installation is None or installation.removed_at is not None:
        raise RehearsalError(404, INSTALLATION_NOT_FOUND, "no live installation has that id")
    return installation


async def _cipher_pass(db: AsyncSession, installation_id: UUID) -> str | None:
    row = await db.scalar(
        select(CompanionInstallationSecret).where(
            CompanionInstallationSecret.installation_id == installation_id,
            CompanionInstallationSecret.secret_name == BACKUP_CIPHER_PASS_ATOM,
        )
    )
    if row is None:
        return None
    # Under the key version recorded on the row: a rotated atom is on the active version (06-25).
    return decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)


def _rehearsal_entries(installation: CompanionInstallation) -> list[dict[str, JsonValue]]:
    entries = (installation.backup_state or {}).get("rehearsals")
    if not isinstance(entries, list):
        return []
    return [dict(entry) for entry in entries if isinstance(entry, dict)]


async def _revoke(key_id: str, rehearsal_id: UUID | str) -> bool:
    """Revoke one rehearsal key; ``False`` (logged) when B2 could not be told. Never raises.

    A key that could not be revoked still expires within the hour it was minted for.
    """
    try:
        await companion_backup_repo.revoke_rehearsal_key(key_id=key_id, rehearsal_id=rehearsal_id)
    except Exception as exc:  # noqa: BLE001 - the key's own expiry is the backstop.
        log.warning(
            "companion rehearsal key not revoked rehearsal_id=%s error=%s",
            rehearsal_id,
            exc.__class__.__name__,
        )
        return False
    return True


async def plan_rehearsal(
    db: AsyncSession, installation_id: UUID, *, now: datetime | None = None
) -> RehearsalBundle:
    """Choose a target, mint a read-only key and record the rehearsal as pending.

    Refusals: 404 for an unknown or removed installation; 409 ``NEEDS_TWO_MARKERS`` without a
    restorable marker pair; 409 ``NO_BACKUP_REPOSITORY`` when the backup step never recorded a
    repository or its cipher pass is gone; 502 when the backup provider refuses the key.
    """
    moment = now or datetime.now(UTC)
    installation = await _installation(db, installation_id, for_update=False)
    target = choose_rehearsal_target((installation.backup_state or {}).get("markers"))
    if target is None:
        raise RehearsalError(
            409,
            NEEDS_TWO_MARKERS,
            "a rehearsal needs two markers, the earlier with a completed backup",
        )
    handles = postiz_backup_handles(installation) or {}
    repository = {name: handles.get(name) for name in _REPOSITORY_HANDLES}
    cipher_pass = await _cipher_pass(db, installation_id)
    if not all(isinstance(value, str) and value for value in repository.values()) or not (
        cipher_pass
    ):
        raise RehearsalError(
            409, NO_BACKUP_REPOSITORY, "the installation has no readable backup repository"
        )
    values = cast(dict[str, str], repository)
    tenant_id = installation.user_id
    await db.rollback()

    rehearsal_id = uuid.uuid4()
    try:
        key = await companion_backup_repo.mint_rehearsal_key(
            bucket_id=values["bucket_id"],
            tenant_id=tenant_id,
            installation_id=installation_id,
            rehearsal_id=rehearsal_id,
        )
    except CompanionStepError as exc:
        raise RehearsalError(
            502, BACKUP_PROVIDER_FAILED, f"the backup provider refused the key: {exc.code}"
        ) from None

    evicted: list[dict[str, JsonValue]] = []
    try:
        installation = await _installation(db, installation_id, for_update=True)
        entry: dict[str, JsonValue] = {
            "id": str(rehearsal_id),
            "target_time": target.target_text,
            "present_marker_id": target.present.id,
            "absent_marker_id": target.absent.id,
            "present_token_sha256": _token_hash(target.present.token),
            "absent_token_sha256": _token_hash(target.absent.token),
            "survivor_marker_id": target.survivor.id,
            "survivor_token_sha256": _token_hash(target.survivor.token),
            "key_id": key.key_id,
            "created_at": moment.isoformat(),
            "expires_at": key.expires_at.isoformat(),
            "outcome": None,
        }
        entries = sorted(
            [*_rehearsal_entries(installation), entry],
            key=lambda item: str(item.get("created_at")),
        )
        kept, dropped = entries[-MAX_REHEARSALS:], entries[:-MAX_REHEARSALS]
        evicted = [item for item in dropped if item.get("outcome") is None]
        installation.backup_state = {
            **(installation.backup_state or {}),
            "rehearsals": cast(list[JsonValue], kept),
        }
        await db.commit()
    except BaseException:
        await db.rollback()
        await _revoke(key.key_id, rehearsal_id)
        raise

    # A pending rehearsal pushed out of the list can no longer report; its key goes now.
    for item in evicted:
        key_id = item.get("key_id")
        if isinstance(key_id, str) and key_id:
            await _revoke(key_id, str(item.get("id")))
    log.info(
        "companion rehearsal planned installation_id=%s rehearsal_id=%s target=%s",
        installation_id,
        rehearsal_id,
        target.target_text,
    )
    return RehearsalBundle(
        rehearsal_id=rehearsal_id,
        installation_id=installation_id,
        stanza=values["stanza"],
        repo_path=values["repo_path"],
        s3_endpoint=values["s3_endpoint"],
        s3_region=values["s3_region"],
        bucket=values["bucket"],
        read_key_id=key.key_id,
        target_time=target.target_text,
        expect_present_token=target.present.token,
        expect_absent_token=target.absent.token,
        expect_survivor_token=target.survivor.token,
        expires_at=key.expires_at,
        read_key=key.application_key,
        cipher_pass=cipher_pass,
    )


# ---------------------------------------------------------------------------
# Recording a result.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RecordedRehearsal:
    rehearsal_id: UUID
    installation_id: UUID
    target_time: str
    outcome: str
    reason: str | None
    reason_text: str | None
    key_revoked: bool


def _redacted(detail: str | None, secrets: Iterable[str]) -> str | None:
    if not detail:
        return None
    for secret in secrets:
        if secret:
            detail = detail.replace(secret, REDACTED)
    return detail[:MAX_DETAIL_CHARS]


async def record_rehearsal_result(
    db: AsyncSession,
    rehearsal_id: UUID,
    *,
    passed: bool,
    present_found: bool,
    absent_found: bool,
    schema_survived: bool | None,
    restored_to: datetime | None,
    detail: str | None,
    now: datetime | None = None,
) -> RecordedRehearsal:
    """Record one pending rehearsal's outcome, revoke its key and write its audit event.

    404 for a rehearsal never planned; 409 ``ALREADY_RECORDED`` for a second result.
    """
    moment = now or datetime.now(UTC)
    installation = await db.scalar(
        select(CompanionInstallation)
        .where(
            CompanionInstallation.backup_state.contains({"rehearsals": [{"id": str(rehearsal_id)}]})
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    entries = _rehearsal_entries(installation) if installation is not None else []
    match = [entry for entry in entries if entry.get("id") == str(rehearsal_id)]
    if installation is None or not match:
        await db.rollback()
        raise RehearsalError(404, REHEARSAL_NOT_FOUND, "no rehearsal was planned with that id")
    entry = match[0]
    if entry.get("outcome") is not None:
        await db.rollback()
        raise RehearsalError(409, ALREADY_RECORDED, "this rehearsal's result is already recorded")

    outcome, reason = rehearsal_outcome(
        passed=passed,
        present_found=present_found,
        absent_found=absent_found,
        schema_survived=schema_survived,
        schema_checked_before="rehearsal_schema_survived_at" in (installation.backup_state or {}),
    )
    key_id = entry.get("key_id")
    key_revoked = await _revoke(key_id, rehearsal_id) if isinstance(key_id, str) else False
    cipher_pass = await _cipher_pass(db, installation.id)
    target_time = str(entry.get("target_time"))
    recorded_at = moment.isoformat()
    restored_text = format_rehearsal_target(restored_to) if restored_to is not None else None
    updated: dict[str, JsonValue] = {
        **entry,
        "outcome": outcome,
        "reason": reason,
        "recorded_at": recorded_at,
        "restored_to": restored_text,
        "present_found": present_found,
        "absent_found": absent_found,
        "schema_survived": schema_survived,
        "key_revoked": key_revoked,
        "detail": _redacted(detail, [cipher_pass or ""]),
    }
    state: dict[str, JsonValue] = {
        **(installation.backup_state or {}),
        "rehearsals": cast(
            list[JsonValue],
            [updated if item.get("id") == str(rehearsal_id) else item for item in entries],
        ),
        "last_rehearsal": {
            "id": str(rehearsal_id),
            "target_time": target_time,
            "outcome": outcome,
            "recorded_at": recorded_at,
        },
    }
    if schema_survived is True and "rehearsal_schema_survived_at" not in state:
        state["rehearsal_schema_survived_at"] = recorded_at
    installation.backup_state = state

    record_control_plane_audit(
        db,
        actor_type="admin",
        action=REHEARSAL_AUDIT_ACTION,
        resource_type="companion_installation",
        resource_id=str(installation.id),
        environment_id=installation.environment_id,
        target_user_id=installation.user_id,
        source="api.admin",
        details={
            "rehearsal_id": str(rehearsal_id),
            "installation_id": str(installation.id),
            "target_time": target_time,
            "outcome": outcome,
            "reason": reason,
            "present_found": present_found,
            "absent_found": absent_found,
            "schema_survived": schema_survived,
            "restored_to": restored_text,
            "key_revoked": key_revoked,
        },
    )
    await db.commit()
    log.info(
        "companion rehearsal recorded installation_id=%s rehearsal_id=%s outcome=%s reason=%s",
        installation.id,
        rehearsal_id,
        outcome,
        reason,
    )
    return RecordedRehearsal(
        rehearsal_id=rehearsal_id,
        installation_id=installation.id,
        target_time=target_time,
        outcome=outcome,
        reason=reason,
        reason_text=REASON_TEXT.get(reason) if reason is not None else None,
        key_revoked=key_revoked,
    )


__all__ = [
    "ALREADY_RECORDED",
    "MAX_REHEARSALS",
    "NEEDS_TWO_MARKERS",
    "REHEARSAL_AUDIT_ACTION",
    "RecordedRehearsal",
    "RehearsalBundle",
    "RehearsalError",
    "RehearsalTarget",
    "choose_rehearsal_target",
    "format_rehearsal_target",
    "plan_rehearsal",
    "record_rehearsal_result",
    "rehearsal_outcome",
]
