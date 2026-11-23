"""Stop hosted companion lines that no serving installation records (06.2 D-17 / D-18, SC-4).

The primary orphan (RESEARCH Pattern 5): ``start_companion_line_item`` succeeds at the hosted
service but its response is lost, the billing-start step exhausts ``BILLING_START_MAX_ATTEMPTS``
and fails, and the teardown finds no ``billing_line_item_id`` to stop. The hosted line keeps
billing, and only a list-and-compare finds it. Every hour this sweep lists each owner's ACTIVE
lines at ``/v2`` and stops each one, older than the in-flight guard, that is not both:

* **serving** -- ``installation_is_serving(row)``: complete, or inside an UPDATE, never an
  UNINSTALL. Never the raw ``status`` column: ``running`` means the lifecycle job is in progress,
  and a sweep keyed on it would stop every healthy line (RESEARCH Pitfall 13); and
* **recorded** -- the row's ``billing_line_item_id`` is this line.

The backend stays the source of truth for installation state (D-17). A stop goes out under the
same derived key the uninstall path uses (``_line_stop_key(installation_id)``), so a stop the
uninstall already made is the same stop. Each stop is operator-visible (D-18): a
``companion.billing_orphan_stopped`` audit row, a WARNING log line, and
``companion_billing_orphan_stops_total{reason}``.

The sweep only ever STOPS a line: it never starts, quotes or reprices one. A ``/v2`` outage for one
owner never stops the sweep for the others, and an unconfigured ``/v2`` makes it a logged no-op.
Log lines carry line ids, installation ids and the reason only -- never an email, a credential or
a response body.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.companion_installation import CompanionInstallation, installation_is_serving
from app.models.user import User
from app.services.audit import record_control_plane_audit
from app.services.companion_billing import (
    CompanionBillingUnavailableError,
    CompanionLineItemRecord,
    list_companion_line_items,
    stop_companion_line_item,
)
from app.services.hosted_owner import hosted_owner_id
from app.services.metrics import companion_billing_orphan_stops

log = logging.getLogger(__name__)

COMPANION_ORPHAN_SWEEP_POLL_SECONDS = 60 * 60
# A line younger than this may belong to an install still in flight: its start response may simply
# not have been recorded yet (D-18).
COMPANION_ORPHAN_SWEEP_IN_FLIGHT_GRACE = timedelta(minutes=15)

ORPHAN_STOPPED_ACTION = "companion.billing_orphan_stopped"
ORPHAN_SWEEP_SOURCE = "companion_orphan_sweep"

# Why a line was stopped.
REASON_NO_INSTALLATION = "no_installation"
REASON_NOT_SERVING = "not_serving"
REASON_LINE_NOT_RECORDED = "line_not_recorded"


@dataclass(frozen=True, slots=True)
class CompanionOrphanSweepResult:
    """One sweep's counts. ``owners_scanned`` counts every owner the sweep asked ``/v2`` about,
    including the ``owners_failed`` whose list could not be read."""

    owners_scanned: int = 0
    lines_seen: int = 0
    lines_stopped: int = 0
    owners_failed: int = 0


def orphan_reason(row: CompanionInstallation | None, line: CompanionLineItemRecord) -> str | None:
    """Why ``line`` is an orphan, or ``None`` when its installation is serving and records it."""
    if row is None:
        return REASON_NO_INSTALLATION
    if not installation_is_serving(row):
        return REASON_NOT_SERVING
    if row.billing_line_item_id != line.line_item_id:
        return REASON_LINE_NOT_RECORDED
    return None


class CompanionOrphanSweepWorker:
    """List each owner's active companion lines at ``/v2`` and stop the orphans, hourly."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = COMPANION_ORPHAN_SWEEP_POLL_SECONDS,
        in_flight_grace: timedelta = COMPANION_ORPHAN_SWEEP_IN_FLIGHT_GRACE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._poll_interval_seconds = poll_interval_seconds
        self._in_flight_grace = in_flight_grace
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _owners(self) -> list[tuple[UUID, str]]:
        """``(users.id, hosted owner id)`` for every owner with ANY installation, removed included.

        A removed installation's line is exactly what can be left billing. An owner with no hosted
        id (a partner tenant) was never billed at ``/v2`` and is skipped.
        """
        async with self._sessionmaker() as db:
            users = (
                await db.scalars(
                    select(User)
                    .where(User.id.in_(select(CompanionInstallation.user_id)))
                    .order_by(User.id)
                )
            ).all()
            owners = [(user.id, hosted_owner_id(user)) for user in users]
            await db.rollback()
        return [(user_id, owner) for user_id, owner in owners if owner is not None]

    async def run_once(self) -> CompanionOrphanSweepResult:
        if not settings.deploy_api_url.strip() or not settings.deploy_api_key:
            log.info("companion orphan sweep skipped: /v2 billing is not configured")
            return CompanionOrphanSweepResult()
        now = self._clock().astimezone(UTC)
        owners_scanned = lines_seen = lines_stopped = owners_failed = 0
        for user_id, owner in await self._owners():
            owners_scanned += 1
            try:
                lines = await list_companion_line_items(owner_user_id=owner)
            except CompanionBillingUnavailableError as exc:
                owners_failed += 1
                log.warning(
                    "companion orphan sweep could not list the lines of user %s: %s", user_id, exc
                )
                continue
            lines_seen += len(lines)
            try:
                lines_stopped += await self._sweep_owner(user_id, owner, lines, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one owner's failure must not stop the rest.
                owners_failed += 1
                log.exception("companion orphan sweep failed for user %s: %s", user_id, exc)
        return CompanionOrphanSweepResult(
            owners_scanned=owners_scanned,
            lines_seen=lines_seen,
            lines_stopped=lines_stopped,
            owners_failed=owners_failed,
        )

    async def _sweep_owner(
        self,
        user_id: UUID,
        owner: str,
        lines: list[CompanionLineItemRecord],
        now: datetime,
    ) -> int:
        stopped = 0
        async with self._sessionmaker() as db:
            for line in lines:
                if line.owner_user_id != owner:
                    # /v2 answered a line for an owner it was not asked about: never act on it.
                    log.warning(
                        "companion orphan sweep left line %s alone: listed under another owner",
                        line.line_item_id,
                    )
                    continue
                if now - line.started_at < self._in_flight_grace:
                    continue
                row = await _owned_installation(db, line.installation_id, user_id)
                reason = orphan_reason(row, line)
                if reason is None:
                    continue
                try:
                    stopped_at = await stop_companion_line_item(
                        installation_id=line.installation_id, line_item_id=line.line_item_id
                    )
                except CompanionBillingUnavailableError as exc:
                    log.warning(
                        "companion orphan sweep could not stop line %s (installation %s, %s): %s",
                        line.line_item_id,
                        line.installation_id,
                        reason,
                        exc,
                    )
                    continue
                stopped_at = stopped_at or self._clock().astimezone(UTC)
                if (
                    row is not None
                    and row.billing_line_item_id == line.line_item_id
                    and row.billing_stopped_at is None
                ):
                    row.billing_stopped_at = stopped_at
                record_control_plane_audit(
                    db,
                    actor_type="system",
                    action=ORPHAN_STOPPED_ACTION,
                    resource_type="companion_billing_line_item",
                    source=ORPHAN_SWEEP_SOURCE,
                    target_user_id=user_id,
                    resource_id=line.line_item_id,
                    details={
                        "installation_id": line.installation_id,
                        "reason": reason,
                        "started_at": line.started_at.isoformat(),
                        "stopped_at": stopped_at.isoformat(),
                        "monthly_price_usd_micros": line.monthly_price_usd_micros,
                    },
                )
                companion_billing_orphan_stops.labels(reason=reason).inc()
                log.warning(
                    "companion billing orphan stopped: line %s installation %s reason %s",
                    line.line_item_id,
                    line.installation_id,
                    reason,
                )
                stopped += 1
            await db.commit()
        return stopped

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
            return
        except TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sweep must not stop other workers.
                log.exception("companion orphan sweep worker failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass


async def _owned_installation(
    db: AsyncSession, installation_id: str, user_id: UUID
) -> CompanionInstallation | None:
    """The installation the line names, if it exists AND belongs to this owner.

    An id that is not a UUID names no row; a row owned by another user counts as no row for this
    owner (T-06.2-10-02), so another tenant's installation never keeps this owner's line billing.
    """
    try:
        parsed = UUID(installation_id)
    except ValueError:
        return None
    row = await db.get(CompanionInstallation, parsed)
    if row is None or row.user_id != user_id:
        return None
    return row


__all__ = [
    "COMPANION_ORPHAN_SWEEP_IN_FLIGHT_GRACE",
    "COMPANION_ORPHAN_SWEEP_POLL_SECONDS",
    "ORPHAN_STOPPED_ACTION",
    "REASON_LINE_NOT_RECORDED",
    "REASON_NOT_SERVING",
    "REASON_NO_INSTALLATION",
    "CompanionOrphanSweepResult",
    "CompanionOrphanSweepWorker",
    "orphan_reason",
]
