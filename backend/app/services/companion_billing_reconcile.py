"""Reconcile hosted companion billing with local installations and accrued usage (INT-4).

The hosted control plane bills a companion line from its ``started_at`` to its ``stopped_at`` at
the quoted monthly price; locally, the installation records the same line
(``billing_line_item_id``, ``billing_started_at``, ``billing_stopped_at``, the accepted
``quoted_monthly_usd_micros``) and the metering worker accrues one ``usage_events`` compute row per
whole UTC hour of it. Nothing else checks that the two agree. Once a day this worker lists each
owner's lines at ``/v2`` (``state=all``, ``since`` now minus ``METERING_LOOKBACK``), joins each to
the installation by ``installation_id`` and ``line_item_id`` under the owner's
``hosted_owner_id``, and classifies what differs:

* ``missing_local`` -- no installation of this owner records the line. Logged and counted only:
  there is no installation to attach a finding to, and the orphan sweep already stops a running
  one. A line younger than the orphan sweep's in-flight grace is not judged.
* ``window_mismatch`` -- the hosted start or stop differs from the local one by more than
  ``WINDOW_TOLERANCE``, or only one side has stopped.
* ``price_mismatch`` -- the hosted monthly price is not the quoted one the installation accepted.
* ``accrual_gap`` -- a whole UTC hour inside the hosted window (and the lookback) that closed more
  than ``ACCRUAL_GRACE`` ago has no local compute row.
* ``amount_drift`` -- for a stopped wallet line, the local accrued compute cost differs from the
  hosted net charge (``charged_cents - credited_cents``) by more than one hour's cost plus one
  cent. A running line is never judged: the hosted month is charged in advance.

Each disagreement increments ``companion_billing_reconcile_mismatches_total{kind}`` and logs one
WARNING line on every pass that sees it. Every kind but ``missing_local`` is kept in
``billing_reconcile_findings`` (one row per line and kind): a re-run moves ``last_seen``, and a
finding of an owner whose lines were read that no pass reproduces any more gets ``resolved_at``.

**This job never moves money.** It starts, stops and reprices nothing at ``/v2``. Its single
correction is local and one-directional: when the hosted line has stopped and the local
``billing_stopped_at`` disagrees, the hosted ``stopped_at`` is authoritative and is written to the
installation, with a ``companion.billing_stop_reconciled`` audit row. An unconfigured ``/v2``
makes the pass a logged no-op, and one owner's ``/v2`` failure never stops the others. Log lines
carry line ids, installation ids, kinds and the two compared values only.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.billing_reconcile_finding import (
    FINDING_ACCRUAL_GAP,
    FINDING_AMOUNT_DRIFT,
    FINDING_PRICE_MISMATCH,
    FINDING_WINDOW_MISMATCH,
    BillingReconcileFinding,
)
from app.models.companion_installation import CompanionInstallation
from app.models.usage_event import USAGE_DIMENSION_COMPUTE, UsageEvent
from app.models.user import User
from app.services.audit import record_control_plane_audit
from app.services.companion_billing import (
    CompanionBillingUnavailableError,
    CompanionLineItemRecord,
    list_companion_line_items,
)
from app.services.companion_metering import HOUR, METERING_LOOKBACK, compute_hour_cost
from app.services.companion_orphan_sweep import COMPANION_ORPHAN_SWEEP_IN_FLIGHT_GRACE
from app.services.hosted_owner import hosted_owner_id
from app.services.metrics import companion_billing_reconcile_mismatches

log = logging.getLogger(__name__)

COMPANION_BILLING_RECONCILE_POLL_SECONDS = 24 * 60 * 60
# The first pass runs this long after the worker starts, so a process restarted more often than
# daily still reconciles.
COMPANION_BILLING_RECONCILE_INITIAL_DELAY_SECONDS = 15 * 60
RECONCILE_LOOKBACK = METERING_LOOKBACK
# A start or stop this close on both sides is the same moment.
WINDOW_TOLERANCE = timedelta(seconds=60)
# The metering worker accrues a closed hour within minutes; an hour closed less than this ago may
# simply not be accrued yet.
ACCRUAL_GRACE = timedelta(hours=1)
MICROS_PER_CENT = 10_000

MISMATCH_MISSING_LOCAL = "missing_local"
STOP_RECONCILED_ACTION = "companion.billing_stop_reconciled"
RECONCILE_SOURCE = "companion_billing_reconcile"
FUNDING_WALLET = "wallet"


@dataclass(frozen=True, slots=True)
class Mismatch:
    kind: str
    local_value: str | None
    hosted_value: str | None


@dataclass(frozen=True, slots=True)
class CompanionBillingReconcileResult:
    """One pass's counts. ``skipped`` is a pass that did nothing because ``/v2`` is unconfigured."""

    skipped: bool = False
    owners_scanned: int = 0
    owners_failed: int = 0
    lines_seen: int = 0
    mismatches: int = 0
    corrections: int = 0
    findings_resolved: int = 0


def _iso(moment: datetime | None) -> str:
    return "open" if moment is None else moment.astimezone(UTC).isoformat()


def _window(started_at: datetime | None, stopped_at: datetime | None) -> str:
    return f"{_iso(started_at)}/{_iso(stopped_at)}"


def _differs(local: datetime | None, hosted: datetime | None) -> bool:
    if local is None or hosted is None:
        return local is not hosted
    return abs(local - hosted) > WINDOW_TOLERANCE


def window_mismatch(row: CompanionInstallation, line: CompanionLineItemRecord) -> Mismatch | None:
    if not (
        _differs(row.billing_started_at, line.started_at)
        or _differs(row.billing_stopped_at, line.stopped_at)
    ):
        return None
    return Mismatch(
        FINDING_WINDOW_MISMATCH,
        _window(row.billing_started_at, row.billing_stopped_at),
        _window(line.started_at, line.stopped_at),
    )


def price_mismatch(row: CompanionInstallation, line: CompanionLineItemRecord) -> Mismatch | None:
    if row.quoted_monthly_usd_micros == line.monthly_price_usd_micros:
        return None
    local = row.quoted_monthly_usd_micros
    return Mismatch(
        FINDING_PRICE_MISMATCH,
        None if local is None else str(local),
        str(line.monthly_price_usd_micros),
    )


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _ceil_hour(moment: datetime) -> datetime:
    floor = _floor_hour(moment)
    return floor if floor == moment else floor + HOUR


def expected_hours(
    line: CompanionLineItemRecord, *, since: datetime, now: datetime
) -> list[datetime]:
    """Every whole UTC hour inside the hosted window and the lookback that is due accrued."""
    end = min(line.stopped_at or now, now - ACCRUAL_GRACE)
    hour = _ceil_hour(max(line.started_at, since))
    hours: list[datetime] = []
    while hour + HOUR <= end:
        hours.append(hour)
        hour += HOUR
    return hours


def accrual_gap(
    line: CompanionLineItemRecord,
    recorded_hours: set[datetime],
    *,
    since: datetime,
    now: datetime,
) -> Mismatch | None:
    expected = expected_hours(line, since=since, now=now)
    recorded = sum(1 for hour in expected if hour in recorded_hours)
    if recorded == len(expected):
        return None
    return Mismatch(FINDING_ACCRUAL_GAP, str(recorded), str(len(expected)))


def amount_drift(
    row: CompanionInstallation, line: CompanionLineItemRecord, accrued_usd_micros: int
) -> Mismatch | None:
    """Local accrued compute cost against the hosted net charge, for a stopped wallet line."""
    if (
        line.stopped_at is None
        or line.funding_source != FUNDING_WALLET
        or line.charged_cents is None
        or line.credited_cents is None
    ):
        return None
    hosted = (line.charged_cents - line.credited_cents) * MICROS_PER_CENT
    monthly = row.quoted_monthly_usd_micros or line.monthly_price_usd_micros
    tolerance = compute_hour_cost(monthly, line.stopped_at) + MICROS_PER_CENT
    if abs(accrued_usd_micros - hosted) <= tolerance:
        return None
    return Mismatch(FINDING_AMOUNT_DRIFT, str(accrued_usd_micros), str(hosted))


class CompanionBillingReconcileWorker:
    """Compare every owner's hosted companion lines with local billing, daily (INT-4)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = COMPANION_BILLING_RECONCILE_POLL_SECONDS,
        initial_delay_seconds: float = COMPANION_BILLING_RECONCILE_INITIAL_DELAY_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._poll_interval_seconds = poll_interval_seconds
        self._initial_delay_seconds = initial_delay_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _owners(self) -> list[tuple[UUID, str]]:
        """``(users.id, hosted owner id)`` for every owner with any companion installation."""
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

    async def run_once(self) -> CompanionBillingReconcileResult:
        if not settings.deploy_api_url.strip() or not settings.deploy_api_key:
            log.info("companion billing reconcile skipped: /v2 billing is not configured")
            return CompanionBillingReconcileResult(skipped=True)
        now = self._clock().astimezone(UTC)
        since = now - RECONCILE_LOOKBACK
        owners_scanned = owners_failed = lines_seen = 0
        mismatches = corrections = resolved = 0
        for user_id, owner in await self._owners():
            owners_scanned += 1
            try:
                lines = await list_companion_line_items(
                    owner_user_id=owner, state="all", since=since
                )
            except CompanionBillingUnavailableError as exc:
                owners_failed += 1
                log.warning(
                    "companion billing reconcile could not list the lines of user %s: %s",
                    user_id,
                    exc,
                )
                continue
            lines_seen += len(lines)
            try:
                found, corrected, closed = await self._reconcile_owner(
                    user_id, owner, lines, now=now, since=since
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one owner's failure must not stop the rest.
                owners_failed += 1
                log.exception("companion billing reconcile failed for user %s: %s", user_id, exc)
                continue
            mismatches += found
            corrections += corrected
            resolved += closed
        return CompanionBillingReconcileResult(
            owners_scanned=owners_scanned,
            owners_failed=owners_failed,
            lines_seen=lines_seen,
            mismatches=mismatches,
            corrections=corrections,
            findings_resolved=resolved,
        )

    async def _reconcile_owner(
        self,
        user_id: UUID,
        owner: str,
        lines: list[CompanionLineItemRecord],
        *,
        now: datetime,
        since: datetime,
    ) -> tuple[int, int, int]:
        """Classify, record and correct one owner's lines; returns (mismatches, corrections,
        findings resolved). Commits once."""
        found = corrected = 0
        seen: set[tuple[str, str]] = set()
        async with self._sessionmaker() as db:
            installations = {
                row.id: row
                for row in (
                    await db.scalars(
                        select(CompanionInstallation).where(
                            CompanionInstallation.user_id == user_id
                        )
                    )
                ).all()
            }
            for line in lines:
                if line.owner_user_id != owner:
                    log.warning(
                        "companion billing reconcile left line %s alone: listed under another "
                        "owner",
                        line.line_item_id,
                    )
                    continue
                row = _recorded_by(installations, line)
                if row is None:
                    if now - line.started_at >= COMPANION_ORPHAN_SWEEP_IN_FLIGHT_GRACE:
                        found += 1
                        _report(
                            Mismatch(MISMATCH_MISSING_LOCAL, None, line.installation_id),
                            line,
                            None,
                        )
                    continue
                observed = await self._classify(db, row, line, now=now, since=since)
                for mismatch in observed:
                    found += 1
                    seen.add((line.line_item_id, mismatch.kind))
                    _report(mismatch, line, row.id)
                    await _record_finding(db, row.id, line.line_item_id, mismatch, now)
                if _correct_stop(db, row, line, user_id):
                    corrected += 1
                    if window_mismatch(row, line) is None:
                        await db.execute(
                            update(BillingReconcileFinding)
                            .where(
                                BillingReconcileFinding.installation_id == row.id,
                                BillingReconcileFinding.line_item_id == line.line_item_id,
                                BillingReconcileFinding.kind == FINDING_WINDOW_MISMATCH,
                            )
                            .values(resolved_at=now)
                        )
            closed = await _resolve_unreproduced(db, list(installations), seen, now)
            await db.commit()
        return found, corrected, closed

    async def _classify(
        self,
        db: AsyncSession,
        row: CompanionInstallation,
        line: CompanionLineItemRecord,
        *,
        now: datetime,
        since: datetime,
    ) -> list[Mismatch]:
        accrued = (
            await db.execute(
                select(UsageEvent.period_start, UsageEvent.cost_usd_micros).where(
                    UsageEvent.installation_id == row.id,
                    UsageEvent.dimension == USAGE_DIMENSION_COMPUTE,
                    UsageEvent.period_start >= _floor_hour(line.started_at),
                    *(
                        ()
                        if line.stopped_at is None
                        else (UsageEvent.period_start < line.stopped_at,)
                    ),
                )
            )
        ).all()
        recorded_hours = {
            period_start.astimezone(UTC) for period_start, _ in accrued if period_start
        }
        accrued_micros = sum(cost for _, cost in accrued)
        checks = (
            window_mismatch(row, line),
            price_mismatch(row, line),
            accrual_gap(line, recorded_hours, since=since, now=now),
            amount_drift(row, line, accrued_micros),
        )
        return [mismatch for mismatch in checks if mismatch is not None]

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        delay = self._initial_delay_seconds
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the job must not stop other workers.
                log.exception("companion billing reconcile worker failed: %s", exc)
            delay = self._poll_interval_seconds


def _recorded_by(
    installations: dict[UUID, CompanionInstallation], line: CompanionLineItemRecord
) -> CompanionInstallation | None:
    """The owner's installation the line names, if it records this very line."""
    try:
        row = installations.get(UUID(line.installation_id))
    except ValueError:
        return None
    if row is None or row.billing_line_item_id != line.line_item_id:
        return None
    return row


def _report(
    mismatch: Mismatch, line: CompanionLineItemRecord, installation_id: UUID | None
) -> None:
    companion_billing_reconcile_mismatches.labels(kind=mismatch.kind).inc()
    log.warning(
        "companion billing reconcile mismatch: kind %s line %r installation %r local %r hosted %r",
        mismatch.kind,
        line.line_item_id,
        installation_id if installation_id is not None else line.installation_id,
        mismatch.local_value,
        mismatch.hosted_value,
    )


async def _record_finding(
    db: AsyncSession, installation_id: UUID, line_item_id: str, mismatch: Mismatch, now: datetime
) -> None:
    """Open the finding, or refresh it (reopening it if it had been resolved)."""
    statement = pg_insert(BillingReconcileFinding).values(
        installation_id=installation_id,
        line_item_id=line_item_id,
        kind=mismatch.kind,
        local_value=mismatch.local_value,
        hosted_value=mismatch.hosted_value,
        first_seen=now,
        last_seen=now,
        resolved_at=None,
    )
    await db.execute(
        statement.on_conflict_do_update(
            constraint="uq_billing_reconcile_findings_line_kind",
            set_={
                "installation_id": statement.excluded.installation_id,
                "local_value": statement.excluded.local_value,
                "hosted_value": statement.excluded.hosted_value,
                "last_seen": statement.excluded.last_seen,
                "resolved_at": None,
            },
        )
    )


def _correct_stop(
    db: AsyncSession, row: CompanionInstallation, line: CompanionLineItemRecord, user_id: UUID
) -> bool:
    """Take the hosted ``stopped_at`` as authoritative for a stopped line; True when it changed
    the installation. The only correction this job makes, and it moves no money."""
    if line.stopped_at is None or not _differs(row.billing_stopped_at, line.stopped_at):
        return False
    previous = row.billing_stopped_at
    row.billing_stopped_at = line.stopped_at
    record_control_plane_audit(
        db,
        actor_type="system",
        action=STOP_RECONCILED_ACTION,
        resource_type="companion_billing_line_item",
        source=RECONCILE_SOURCE,
        target_user_id=user_id,
        resource_id=line.line_item_id,
        details={
            "installation_id": str(row.id),
            "previous_billing_stopped_at": None if previous is None else previous.isoformat(),
            "billing_stopped_at": line.stopped_at.isoformat(),
        },
    )
    log.warning(
        "companion billing reconcile set billing_stopped_at of installation %s to the hosted stop "
        "of line %s",
        row.id,
        line.line_item_id,
    )
    return True


async def _resolve_unreproduced(
    db: AsyncSession,
    installation_ids: list[UUID],
    seen: set[tuple[str, str]],
    now: datetime,
) -> int:
    """Resolve every open finding of these installations that this pass did not reproduce."""
    if not installation_ids:
        return 0
    condition = [
        BillingReconcileFinding.installation_id.in_(installation_ids),
        BillingReconcileFinding.resolved_at.is_(None),
    ]
    if seen:
        condition.append(
            tuple_(BillingReconcileFinding.line_item_id, BillingReconcileFinding.kind).not_in(
                list(seen)
            )
        )
    result = await db.execute(
        update(BillingReconcileFinding)
        .where(*condition)
        .values(resolved_at=now)
        .returning(BillingReconcileFinding.id)
    )
    return len(result.all())


__all__ = [
    "ACCRUAL_GRACE",
    "COMPANION_BILLING_RECONCILE_POLL_SECONDS",
    "MISMATCH_MISSING_LOCAL",
    "RECONCILE_LOOKBACK",
    "STOP_RECONCILED_ACTION",
    "WINDOW_TOLERANCE",
    "CompanionBillingReconcileResult",
    "CompanionBillingReconcileWorker",
    "Mismatch",
    "accrual_gap",
    "amount_drift",
    "expected_hours",
    "price_mismatch",
    "window_mismatch",
]
