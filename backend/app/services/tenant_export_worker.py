"""Durable tenant export worker (ACCESS-04, D-12).

Cloned from ``PrincipalLifecycleCleanupWorker``: claim in an own
session, execute in a fresh session, fence completion/failure on the
claim id, record failures with capped exponential backoff, and poll in
``run_forever``. An interrupted or failed export remains durable retry
work — never lost.

**Companions (plan 06-19).** A job that promised a companion's data does
not assemble while a component is still owed: the worker puts it back in
the queue ``COMPANION_COMPONENT_DEFER_SECONDS`` later (a deferral is not a
failed attempt, so it neither counts nor backs off). A component owed past
its deadline, a failed one, or media over the export bound fails the job
TERMINALLY with a named reason -- ``status='failed'`` with no
``next_attempt_at`` -- because retrying the same job cannot deliver what it
promised; the tenant starts a new export instead.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.tenant_export import (
    TENANT_EXPORT_STATUS_COMPLETE,
    TENANT_EXPORT_STATUS_FAILED,
    TENANT_EXPORT_STATUS_QUEUED,
    TENANT_EXPORT_STATUS_RUNNING,
    TenantExportJob,
)
from app.services.audit import record_control_plane_audit
from app.services.file_store import FileStore, get_file_store
from app.services.tenant_export import (
    COMPANION_COMPONENT_DEFER_SECONDS,
    CompanionExportError,
    MediaClientFactory,
    assemble_tenant_export,
    companion_components,
    evaluate_companion_components,
    with_companion_components,
)

log = logging.getLogger(__name__)

# Exports can be large; the lease is generous before another worker may
# reclaim a stale running job.
TENANT_EXPORT_CLAIM_LEASE_SECONDS = 15 * 60
TENANT_EXPORT_BACKOFF_CAP_SECONDS = 15 * 60


class TenantExportClaimLostError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ClaimedTenantExport:
    job_id: UUID
    user_id: UUID
    claim_id: UUID
    include_vault_values: bool
    attempt_count: int


async def claim_tenant_export(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    lease_seconds: int = TENANT_EXPORT_CLAIM_LEASE_SECONDS,
) -> ClaimedTenantExport | None:
    """Claim one due export job without blocking another worker's row.

    Due work is a queued job, a failed job whose backoff has elapsed, or
    a running job whose claim lease went stale (crashed worker). A failed
    job with NO next attempt failed terminally (a named companion failure,
    plan 06-19) and is never claimed again.
    """
    claimed_at = now or datetime.now(UTC)
    stale_before = claimed_at - timedelta(seconds=lease_seconds)
    job = (
        await db.execute(
            select(TenantExportJob)
            .where(
                or_(
                    and_(
                        TenantExportJob.status == TENANT_EXPORT_STATUS_QUEUED,
                        or_(
                            TenantExportJob.next_attempt_at.is_(None),
                            TenantExportJob.next_attempt_at <= claimed_at,
                        ),
                    ),
                    and_(
                        TenantExportJob.status == TENANT_EXPORT_STATUS_FAILED,
                        TenantExportJob.next_attempt_at <= claimed_at,
                    ),
                    and_(
                        TenantExportJob.status == TENANT_EXPORT_STATUS_RUNNING,
                        TenantExportJob.claimed_at <= stale_before,
                    ),
                )
            )
            .order_by(
                TenantExportJob.next_attempt_at,
                TenantExportJob.created_at,
                TenantExportJob.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    claim_id = uuid.uuid4()
    job.status = TENANT_EXPORT_STATUS_RUNNING
    job.claim_id = claim_id
    job.claimed_at = claimed_at
    job.attempt_count += 1
    await db.flush()
    return ClaimedTenantExport(
        job_id=job.id,
        user_id=job.user_id,
        claim_id=claim_id,
        include_vault_values=job.include_vault_values,
        attempt_count=job.attempt_count,
    )


async def _load_job_for_update(db: AsyncSession, job_id: UUID) -> TenantExportJob | None:
    return (
        await db.execute(
            select(TenantExportJob)
            .where(TenantExportJob.id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def complete_tenant_export(
    db: AsyncSession,
    *,
    job_id: UUID,
    expected_claim_id: UUID | None = None,
    artifact_key: str,
    manifest: dict[str, Any],
    now: datetime | None = None,
) -> TenantExportJob:
    """Mark a claimed job complete, fenced on the claim id."""
    job = await _load_job_for_update(db, job_id)
    if job is None:
        raise RuntimeError("tenant export job disappeared")
    if expected_claim_id is not None and job.claim_id != expected_claim_id:
        raise TenantExportClaimLostError("tenant export claim changed")
    job.status = TENANT_EXPORT_STATUS_COMPLETE
    job.artifact_key = artifact_key
    job.manifest = manifest
    job.error = None
    job.completed_at = now or datetime.now(UTC)
    job.claim_id = None
    job.claimed_at = None
    job.next_attempt_at = None
    await db.flush()
    return job


async def record_tenant_export_failure(
    db: AsyncSession,
    *,
    job_id: UUID,
    expected_claim_id: UUID | None = None,
    error: str,
    now: datetime | None = None,
    terminal: bool = False,
) -> bool:
    """Record a failed attempt with capped exponential backoff.

    ``terminal`` records a failure retrying cannot fix (plan 06-19): no
    next attempt, so the job is never claimed again.

    Returns False when the job is gone, already complete, or the claim
    id no longer matches (another worker took over)."""
    job = await _load_job_for_update(db, job_id)
    if job is None or job.status == TENANT_EXPORT_STATUS_COMPLETE:
        return False
    if expected_claim_id is not None and job.claim_id != expected_claim_id:
        return False
    backoff_seconds = min(
        2 ** min(max(job.attempt_count - 1, 0), 10),
        TENANT_EXPORT_BACKOFF_CAP_SECONDS,
    )
    job.status = TENANT_EXPORT_STATUS_FAILED
    job.error = error[:1000]
    job.next_attempt_at = (
        None if terminal else (now or datetime.now(UTC)) + timedelta(seconds=backoff_seconds)
    )
    job.claim_id = None
    job.claimed_at = None
    await db.flush()
    return True


async def defer_tenant_export(
    db: AsyncSession,
    *,
    job_id: UUID,
    expected_claim_id: UUID,
    until: datetime,
) -> bool:
    """Put a claimed job back in the queue until ``until`` (plan 06-19).

    A job waiting on its companion component is not failing: the claim's
    attempt is given back, so waiting neither counts nor backs off.
    Returns False when the claim changed (another worker took over).
    """
    job = await _load_job_for_update(db, job_id)
    if job is None or job.claim_id != expected_claim_id:
        return False
    job.status = TENANT_EXPORT_STATUS_QUEUED
    job.next_attempt_at = until
    job.claim_id = None
    job.claimed_at = None
    job.attempt_count = max(job.attempt_count - 1, 0)
    await db.flush()
    return True


class TenantExportWorker:
    """Run tenant export jobs durably after route/process failure."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        file_store: FileStore | None = None,
        poll_interval_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
        media_client_factory: MediaClientFactory | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._file_store = file_store
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._media_client_factory = media_client_factory

    def _store(self) -> FileStore:
        if self._file_store is None:
            self._file_store = get_file_store()
        return self._file_store

    async def run_once(self) -> UUID | None:
        now = self._clock()
        async with self._sessionmaker() as db:
            claim = await claim_tenant_export(db, now=now)
            await db.commit()
        if claim is None:
            return None

        try:
            async with self._sessionmaker() as db:
                job = await db.get(TenantExportJob, claim.job_id)
                components = companion_components(job.manifest if job is not None else None)
                verdict = await evaluate_companion_components(
                    db, user_id=claim.user_id, components=components, now=now
                )
                if verdict.failure is not None:
                    raise verdict.failure
                if verdict.pending:
                    deferred = await defer_tenant_export(
                        db,
                        job_id=claim.job_id,
                        expected_claim_id=claim.claim_id,
                        until=now + timedelta(seconds=COMPANION_COMPONENT_DEFER_SECONDS),
                    )
                    await db.commit()
                    if not deferred:
                        log.info(
                            "tenant export deferral ignored after claim changed job_id=%s",
                            claim.job_id,
                        )
                    return claim.job_id
                archive, manifest = await assemble_tenant_export(
                    db,
                    self._store(),
                    claim.user_id,
                    include_vault_values=claim.include_vault_values,
                    companion_components=components,
                    media_client_factory=self._media_client_factory,
                )
                artifact_key = f"exports/{claim.user_id}/{claim.job_id}.tar.gz"
                await self._store().put(artifact_key, archive, "application/gzip")
                await complete_tenant_export(
                    db,
                    job_id=claim.job_id,
                    expected_claim_id=claim.claim_id,
                    artifact_key=artifact_key,
                    # The components stay on the job record (their upload keys and sizes), never
                    # in the archive's own manifest.json.
                    manifest=(
                        with_companion_components(manifest, components) if components else manifest
                    ),
                )
                record_control_plane_audit(
                    db,
                    actor_type="system",
                    action="tenant.export_completed",
                    resource_type="tenant_export_job",
                    resource_id=str(claim.job_id),
                    source="worker.tenant_export",
                    target_user_id=claim.user_id,
                    # Domain counts + artifact key only — never a
                    # presigned URL (T-03-18).
                    details={
                        "job_id": str(claim.job_id),
                        "artifact_key": artifact_key,
                        "domains": {
                            str(entry["name"]): entry["rows"]
                            for entry in manifest.get("domains", [])
                        },
                    },
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every failed export remains durable retry work.
            terminal = isinstance(exc, CompanionExportError)
            if terminal:
                log.warning("tenant export failed job_id=%s reason=%s", claim.job_id, exc)
            else:
                log.exception("tenant export failed job_id=%s", claim.job_id)
            async with self._sessionmaker() as db:
                recorded = await record_tenant_export_failure(
                    db,
                    job_id=claim.job_id,
                    expected_claim_id=claim.claim_id,
                    error=str(exc) or exc.__class__.__name__,
                    now=now,
                    terminal=terminal,
                )
                await db.commit()
            if not recorded:
                log.info(
                    "tenant export failure ignored after claim changed job_id=%s",
                    claim.job_id,
                )
        return claim.job_id

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        while not stop_event.is_set():
            try:
                job_id = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one row cannot stop durable recovery.
                log.exception("tenant export worker iteration failed")
                job_id = None
            if job_id is None:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    pass


__all__ = [
    "TENANT_EXPORT_BACKOFF_CAP_SECONDS",
    "TENANT_EXPORT_CLAIM_LEASE_SECONDS",
    "ClaimedTenantExport",
    "TenantExportClaimLostError",
    "TenantExportWorker",
    "claim_tenant_export",
    "complete_tenant_export",
    "defer_tenant_export",
    "record_tenant_export_failure",
]
