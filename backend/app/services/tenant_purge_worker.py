"""Durable tenant purge worker (ACCESS-04, D-15/D-16).

Cloned from ``PrincipalLifecycleCleanupWorker`` / ``TenantExportWorker``:
claim in an own session, execute in a fresh session, fence completion and
failure on the claim id, record failures with capped exponential backoff,
poll in ``run_forever``. An interrupted purge remains durable retry work,
and two workers can never process one purge row (expected_claim_id
fencing — the ACCESS-04 concurrency edge row).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tenant_purge import (
    TENANT_PURGE_STATUS_COMPLETE,
    TENANT_PURGE_STATUS_FAILED,
    TENANT_PURGE_STATUS_PENDING,
    TENANT_PURGE_STATUS_PURGING,
    TenantPurge,
)
from app.services.audit import record_control_plane_audit
from app.services.file_store import FileStore, get_file_store
from app.services.tenant_purge import execute_tenant_purge

log = logging.getLogger(__name__)

# Purges can tear down many objects; the lease is generous before another
# worker may reclaim a stale purging row.
TENANT_PURGE_CLAIM_LEASE_SECONDS = 15 * 60
TENANT_PURGE_BACKOFF_CAP_SECONDS = 15 * 60


class TenantPurgeClaimLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedTenantPurge:
    purge_id: UUID
    claim_id: UUID
    attempt_count: int


async def claim_tenant_purge(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    lease_seconds: int = TENANT_PURGE_CLAIM_LEASE_SECONDS,
) -> ClaimedTenantPurge | None:
    """Claim one due purge without blocking another worker's row.

    Due work is a pending purge whose grace window has passed, a failed
    purge whose backoff has elapsed, or a purging row whose claim lease
    went stale (crashed worker). A not-yet-due purge is never claimed —
    the grace window is the operator's cancel opportunity (D-15).
    """
    claimed_at = now or datetime.now(UTC)
    stale_before = claimed_at - timedelta(seconds=lease_seconds)
    purge = (
        await db.execute(
            select(TenantPurge)
            .where(
                or_(
                    and_(
                        TenantPurge.status.in_(
                            (TENANT_PURGE_STATUS_PENDING, TENANT_PURGE_STATUS_FAILED)
                        ),
                        TenantPurge.purge_after <= claimed_at,
                        or_(
                            TenantPurge.next_attempt_at.is_(None),
                            TenantPurge.next_attempt_at <= claimed_at,
                        ),
                    ),
                    and_(
                        TenantPurge.status == TENANT_PURGE_STATUS_PURGING,
                        TenantPurge.claimed_at <= stale_before,
                    ),
                )
            )
            .order_by(
                TenantPurge.next_attempt_at,
                TenantPurge.created_at,
                TenantPurge.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if purge is None:
        return None
    claim_id = uuid.uuid4()
    purge.status = TENANT_PURGE_STATUS_PURGING
    purge.claim_id = claim_id
    purge.claimed_at = claimed_at
    purge.attempt_count += 1
    await db.flush()
    return ClaimedTenantPurge(
        purge_id=purge.id,
        claim_id=claim_id,
        attempt_count=purge.attempt_count,
    )


async def _load_purge_for_update(db: AsyncSession, purge_id: UUID) -> TenantPurge | None:
    return (
        await db.execute(
            select(TenantPurge)
            .where(TenantPurge.id == purge_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def complete_tenant_purge(
    db: AsyncSession,
    *,
    purge_id: UUID,
    expected_claim_id: UUID | None = None,
    now: datetime | None = None,
) -> TenantPurge:
    """Mark a claimed purge complete, fenced on the claim id."""
    purge = await _load_purge_for_update(db, purge_id)
    if purge is None:
        raise RuntimeError("tenant purge row disappeared")
    if expected_claim_id is not None and purge.claim_id != expected_claim_id:
        raise TenantPurgeClaimLostError("tenant purge claim changed")
    purge.status = TENANT_PURGE_STATUS_COMPLETE
    purge.error = None
    purge.completed_at = now or datetime.now(UTC)
    purge.claim_id = None
    purge.claimed_at = None
    purge.next_attempt_at = None
    await db.flush()
    return purge


async def record_tenant_purge_failure(
    db: AsyncSession,
    *,
    purge_id: UUID,
    expected_claim_id: UUID | None = None,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Record a failed attempt with capped exponential backoff.

    Returns False when the purge is gone, already complete, or the claim
    id no longer matches (another worker took over)."""
    purge = await _load_purge_for_update(db, purge_id)
    if purge is None or purge.status == TENANT_PURGE_STATUS_COMPLETE:
        return False
    if expected_claim_id is not None and purge.claim_id != expected_claim_id:
        return False
    backoff_seconds = min(
        2 ** min(max(purge.attempt_count - 1, 0), 10),
        TENANT_PURGE_BACKOFF_CAP_SECONDS,
    )
    purge.status = TENANT_PURGE_STATUS_FAILED
    purge.error = error[:1000]
    purge.next_attempt_at = (now or datetime.now(UTC)) + timedelta(seconds=backoff_seconds)
    purge.claim_id = None
    purge.claimed_at = None
    await db.flush()
    return True


class TenantPurgeWorker:
    """Run due tenant purges durably after route/process failure."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        file_store: FileStore | None = None,
        poll_interval_seconds: float = 1.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._file_store = file_store
        self._poll_interval_seconds = poll_interval_seconds

    def _store(self) -> FileStore:
        if self._file_store is None:
            self._file_store = get_file_store()
        return self._file_store

    async def run_once(self) -> UUID | None:
        async with self._sessionmaker() as db:
            claim = await claim_tenant_purge(db)
            await db.commit()
        if claim is None:
            return None

        try:
            async with self._sessionmaker() as db:
                purge = await db.get(TenantPurge, claim.purge_id)
                if purge is None:
                    return claim.purge_id
                # Re-verify the claim before the first irreversible step. The
                # claim was committed in the session above, so this fresh
                # session's plain MVCC read can observe a row another actor has
                # since taken over (stale-lease reclaim) or a status that is no
                # longer ours. `complete_tenant_purge` already fences on the
                # same claim id at the END; without this the sweep would still
                # have run. A plain read is deliberate: holding FOR UPDATE
                # across the whole sweep would make a concurrent cancel block
                # for the sweep's full duration instead of getting its 409
                # immediately, and the primary fix for that race lives in the
                # cancel endpoint.
                if purge.claim_id != claim.claim_id or purge.status != TENANT_PURGE_STATUS_PURGING:
                    log.info(
                        "tenant purge abandoned before execution purge_id=%s "
                        "claim_id=%s row_claim_id=%s row_status=%s",
                        claim.purge_id,
                        claim.claim_id,
                        purge.claim_id,
                        purge.status,
                    )
                    return claim.purge_id
                purged_user_id = purge.user_id
                result = await execute_tenant_purge(db, self._store(), purge)
                await complete_tenant_purge(
                    db,
                    purge_id=claim.purge_id,
                    expected_claim_id=claim.claim_id,
                )
                record_control_plane_audit(
                    db,
                    actor_type="system",
                    action="tenant.purged",
                    resource_type="tenant_purge",
                    resource_id=str(claim.purge_id),
                    source="worker.tenant_purge",
                    # target_user_id stays None: the users row no longer
                    # exists, so a FK reference would fail — the purged
                    # user id travels in the details instead.
                    details={
                        "purge_id": str(claim.purge_id),
                        "user_id": str(purged_user_id),
                        "tables": result["tables"],
                        "prefixes": result["prefixes"],
                        "deployments": result["deployments"],
                        # Ids /v2 answered 404 for and the sweep took as already gone.
                        "deployments_not_found": result["deployments_not_found"],
                        # Which D-16 branch the sweep took (WR-04). Without
                        # it `deployments: []` is ambiguous between "this
                        # tenant had none" and "we never asked".
                        "hosted_teardown": result["hosted_teardown"],
                    },
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failed purge remains durable retry work.
            log.exception("tenant purge failed purge_id=%s", claim.purge_id)
            async with self._sessionmaker() as db:
                recorded = await record_tenant_purge_failure(
                    db,
                    purge_id=claim.purge_id,
                    expected_claim_id=claim.claim_id,
                    error=str(exc) or exc.__class__.__name__,
                )
                await db.commit()
            if not recorded:
                log.info(
                    "tenant purge failure ignored after claim changed purge_id=%s",
                    claim.purge_id,
                )
        return claim.purge_id

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        while not stop_event.is_set():
            try:
                purge_id = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one row cannot stop durable recovery.
                log.exception("tenant purge worker iteration failed")
                purge_id = None
            if purge_id is None:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    pass


__all__ = [
    "TENANT_PURGE_BACKOFF_CAP_SECONDS",
    "TENANT_PURGE_CLAIM_LEASE_SECONDS",
    "ClaimedTenantPurge",
    "TenantPurgeClaimLostError",
    "TenantPurgeWorker",
    "claim_tenant_purge",
    "complete_tenant_purge",
    "record_tenant_purge_failure",
]
