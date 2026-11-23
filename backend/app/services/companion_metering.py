"""Per-tenant companion cost attribution: accrual into ``usage_events`` and the spend read-out.

OPS-05 and D-23. ``usage_events`` is the one cost ledger: a companion installation's compute and
storage are recorded there beside the agent runtime's LLM calls, rather than in a second metering
path that would have to be reconciled against billing.

**Compute** accrues one row per whole UTC hour ``[h, h+1)`` that lies inside the installation's
billing window -- from ``billing_started_at`` (set only when an install completes and its line item
starts, plan 06-17) to ``billing_stopped_at`` or ``removed_at``, whichever comes first. A partial
hour at either end is not billed. The hour costs the tier price the tenant accepted (D-07),
``quoted_monthly_usd_micros``, divided by the hours in that calendar month and FLOORED: the
remainder, under one micro-dollar per hour, is unbilled rounding recorded here rather than carried
into a later hour.

**Storage** accrues one row per whole UTC day inside the same window, from the bytes MEASURED when
it is accrued: the database size the runtime last reported in its backup report
(``backup_state.db_bytes``) plus the listed size of every object in the installation's own media
bucket (plan 06-13), the same bucket the tenant export reads. The day costs those bytes at the
accepted ``quoted_storage_usd_micros_per_gib_month``, pro-rated over the days in that month and
floored. An installation whose database size was never reported is NOT measured, so no storage row
is written for it -- a guessed or zero row would be a fabricated measurement.

Every recorded cost is an integer of micro-dollars fixed when it is accrued (D-17's
representation): a later price change never rewrites a recorded period. The partial unique index
on ``(installation_id, dimension, period_start)`` makes accrual idempotent -- recording a period
twice leaves one row.

**The read-out** is honest about what is not measured: an amount no row was recorded for is null,
never zero, and LLM spend a companion itself originates is not yet attributed (PLAT-03, research
A13), so its ``ai_usage_usd_micros`` is always null.

**The worker** (``CompanionMeteringWorker``) runs in the worker process from an injectable clock.
Each pass accrues, per billed installation, every missing whole hour since the later of billing's
start and the last accrued hour, and the previous UTC day's storage. Several workers racing are
harmless: the unique index turns a second accrual of a period into a no-op.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.companion_installation import CompanionInstallation
from app.models.session import Session
from app.models.usage_event import (
    USAGE_DIMENSION_COMPUTE,
    USAGE_DIMENSION_LLM,
    USAGE_DIMENSION_STORAGE,
    UsageEvent,
)
from app.schemas.companions import (
    CompanionIsolationTierName,
    CompanionSpendLineResponse,
    CompanionSpendResponse,
)
from app.services.tenant_export import (
    MediaClientFactory,
    default_media_client_factory,
    installation_media_bucket,
)

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
GIB = 2**30

# How long after its billing window closes an installation is still visited, so a worker that was
# down when billing stopped still records the final whole hours.
METERING_LOOKBACK = timedelta(days=35)
COMPANION_METERING_POLL_SECONDS = 5 * 60.0

_TIERS: dict[str, CompanionIsolationTierName] = {
    "standard": "standard",
    "dedicated": "dedicated",
    "shared": "shared",
}


def month_start_of(moment: datetime) -> datetime:
    """UTC midnight on the first day of ``moment``'s UTC month."""
    utc = moment.astimezone(UTC)
    return datetime(utc.year, utc.month, 1, tzinfo=UTC)


def next_month_start(month_start: datetime) -> datetime:
    if month_start.month == 12:
        return datetime(month_start.year + 1, 1, 1, tzinfo=UTC)
    return datetime(month_start.year, month_start.month + 1, 1, tzinfo=UTC)


def parse_month(value: str) -> datetime:
    """``YYYY-MM`` as the UTC start of that month."""
    year, month = value.split("-", 1)
    return datetime(int(year), int(month), 1, tzinfo=UTC)


def month_label(month_start: datetime) -> str:
    return f"{month_start.year:04d}-{month_start.month:02d}"


def days_in_month(moment: datetime) -> int:
    utc = moment.astimezone(UTC)
    return calendar.monthrange(utc.year, utc.month)[1]


def hours_in_month(moment: datetime) -> int:
    return days_in_month(moment) * 24


def compute_hour_cost(monthly_usd_micros: int, hour_start: datetime) -> int:
    """One hour of the accepted monthly tier price, floored to a whole micro-dollar."""
    return monthly_usd_micros // hours_in_month(hour_start)


def storage_day_cost(
    measured_bytes: int, usd_micros_per_gib_month: int, day_start: datetime
) -> int:
    """One day of the measured bytes at the accepted per-GiB-month price, floored."""
    return measured_bytes * usd_micros_per_gib_month // (GIB * days_in_month(day_start))


def _require_aligned(moment: datetime, *, unit: timedelta) -> datetime:
    if moment.tzinfo is None:
        raise ValueError("an accrual period must be timezone-aware")
    utc = moment.astimezone(UTC)
    if utc.minute or utc.second or utc.microsecond or (unit == DAY and utc.hour):
        raise ValueError(
            f"an accrual period must start on a whole UTC {'day' if unit == DAY else 'hour'}"
        )
    return utc


def billing_window_end(installation: CompanionInstallation) -> datetime | None:
    """When the installation stopped being billable: its line item stopped or it was removed."""
    ends = [end for end in (installation.billing_stopped_at, installation.removed_at) if end]
    return min(ends) if ends else None


def period_is_billed(
    installation: CompanionInstallation, period_start: datetime, length: timedelta
) -> bool:
    """Whether the WHOLE period ``[start, start + length)`` lies inside the billing window."""
    started = installation.billing_started_at
    if started is None or started > period_start:
        return False
    end = billing_window_end(installation)
    return end is None or end >= period_start + length


async def _record(
    db: AsyncSession,
    installation: CompanionInstallation,
    *,
    dimension: str,
    period_start: datetime,
    cost_usd_micros: int,
    quantity: int,
) -> bool:
    """Insert one period's row; False when that period is already recorded."""
    statement = (
        pg_insert(UsageEvent)
        .values(
            id=uuid4(),
            dimension=dimension,
            user_id=installation.user_id,
            environment_id=installation.environment_id,
            installation_id=installation.id,
            companion_key=installation.companion_key,
            period_start=period_start,
            ts=period_start,
            model=f"{installation.companion_key}/{installation.isolation_tier}",
            quantity=quantity,
            cost_usd_micros=cost_usd_micros,
        )
        .on_conflict_do_nothing(
            index_elements=["installation_id", "dimension", "period_start"],
            # The partial index's own predicate as a LITERAL. Bound as a parameter, PostgreSQL
            # cannot infer the arbiter index once asyncpg's prepared statement is planned
            # generically (from its sixth execution), and every accrual after that fails.
            index_where=text("dimension <> 'llm'"),
        )
        .returning(UsageEvent.id)
    )
    return (await db.execute(statement)).scalar_one_or_none() is not None


async def accrue_compute(
    db: AsyncSession, installation: CompanionInstallation, hour_start: datetime
) -> bool:
    """Record one whole billed hour of compute at the accepted tier price.

    Returns True when a row was written; False when the hour is outside the billing window, the
    installation carries no accepted price, or the hour is already recorded. The caller commits.
    """
    hour = _require_aligned(hour_start, unit=HOUR)
    monthly = installation.quoted_monthly_usd_micros
    if monthly is None or not period_is_billed(installation, hour, HOUR):
        return False
    return await _record(
        db,
        installation,
        dimension=USAGE_DIMENSION_COMPUTE,
        period_start=hour,
        cost_usd_micros=compute_hour_cost(monthly, hour),
        quantity=1,
    )


def _reported_db_bytes(installation: CompanionInstallation) -> int | None:
    """The database size from the runtime's last backup report, or None when never reported."""
    value = (installation.backup_state or {}).get("db_bytes")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


async def measure_media_bytes(
    installation: CompanionInstallation,
    *,
    media_client_factory: MediaClientFactory = default_media_client_factory,
) -> int:
    """The summed listed size of every object in the installation's media bucket.

    Zero for an installation without a media bucket: it holds no media to measure. Sizes come from
    the listing alone; no object is read. A listing failure propagates, so a day is never recorded
    from a partial count.
    """
    bucket = installation_media_bucket(installation)
    if bucket is None:
        return 0

    def _list() -> list[tuple[str, int]]:
        client = media_client_factory(bucket)
        try:
            return client.list_object_sizes("")
        finally:
            client.close()

    return sum(size for _, size in await asyncio.to_thread(_list))


async def _period_recorded(
    db: AsyncSession, installation: CompanionInstallation, dimension: str, period_start: datetime
) -> bool:
    return (
        await db.scalar(
            select(UsageEvent.id)
            .where(
                UsageEvent.installation_id == installation.id,
                UsageEvent.dimension == dimension,
                UsageEvent.period_start == period_start,
            )
            .limit(1)
        )
    ) is not None


async def accrue_storage(
    db: AsyncSession,
    installation: CompanionInstallation,
    day_start: datetime,
    *,
    media_client_factory: MediaClientFactory = default_media_client_factory,
) -> bool:
    """Record one whole billed UTC day of storage from the bytes measured now.

    Returns True when a row was written; False when the day is outside the billing window, the
    installation carries no accepted storage price, its database size was never reported (storage
    is then unmeasured, never a zero), or the day is already recorded -- checked before the media
    listing, so a repeated accrual does not list the bucket again. The caller commits.
    """
    day = _require_aligned(day_start, unit=DAY)
    price = installation.quoted_storage_usd_micros_per_gib_month
    db_bytes = _reported_db_bytes(installation)
    if price is None or db_bytes is None or not period_is_billed(installation, day, DAY):
        return False
    if await _period_recorded(db, installation, USAGE_DIMENSION_STORAGE, day):
        return False
    measured = db_bytes + await measure_media_bytes(
        installation, media_client_factory=media_client_factory
    )
    return await _record(
        db,
        installation,
        dimension=USAGE_DIMENSION_STORAGE,
        period_start=day,
        cost_usd_micros=storage_day_cost(measured, price, day),
        quantity=measured,
    )


def _sum_or_none(values: list[int | None]) -> int | None:
    measured = [value for value in values if value is not None]
    return sum(measured) if measured else None


async def companion_spend_for(
    db: AsyncSession, *, user_id: UUID, month_start: datetime
) -> CompanionSpendResponse:
    """The tenant's month-to-date spend per companion, plus its agents' nested LLM spend.

    Scoped to the caller's own installations and sessions only (T-06-21-04). An installation is
    listed when it recorded spend this month or is live and billed; its amounts are null for
    every dimension nothing was recorded for.
    """
    start = month_start_of(month_start)
    end = next_month_start(start)

    sums = (
        await db.execute(
            select(
                UsageEvent.installation_id,
                UsageEvent.dimension,
                func.sum(UsageEvent.cost_usd_micros),
            )
            .where(
                UsageEvent.user_id == user_id,
                UsageEvent.dimension != USAGE_DIMENSION_LLM,
                UsageEvent.period_start >= start,
                UsageEvent.period_start < end,
            )
            .group_by(UsageEvent.installation_id, UsageEvent.dimension)
        )
    ).all()
    recorded: dict[tuple[UUID, str], int] = {}
    for installation_id, dimension, total in sums:
        if installation_id is not None and total is not None:
            recorded[(installation_id, dimension)] = int(total)
    recorded_ids = {installation_id for installation_id, _ in recorded}

    installations = (
        await db.scalars(
            select(CompanionInstallation)
            .where(
                CompanionInstallation.user_id == user_id,
                or_(
                    CompanionInstallation.id.in_(recorded_ids),
                    and_(
                        CompanionInstallation.removed_at.is_(None),
                        CompanionInstallation.billing_started_at.is_not(None),
                        CompanionInstallation.billing_started_at < end,
                    ),
                ),
            )
            .order_by(CompanionInstallation.created_at, CompanionInstallation.id)
        )
    ).all()

    lines: list[CompanionSpendLineResponse] = []
    for installation in installations:
        compute = recorded.get((installation.id, USAGE_DIMENSION_COMPUTE))
        storage = recorded.get((installation.id, USAGE_DIMENSION_STORAGE))
        lines.append(
            CompanionSpendLineResponse(
                installation_id=installation.id,
                agent_id=installation.environment_id,
                companion_key=installation.companion_key,
                isolation_tier=_TIERS[installation.isolation_tier],
                total_usd_micros=_sum_or_none([compute, storage]),
                compute_usd_micros=compute,
                storage_usd_micros=storage,
                ai_usage_usd_micros=None,
            )
        )

    llm_total, llm_rows = (
        await db.execute(
            select(func.sum(UsageEvent.cost_usd_micros), func.count(UsageEvent.id))
            .join(Session, UsageEvent.session_id == Session.id)
            .where(
                Session.user_id == user_id,
                UsageEvent.dimension == USAGE_DIMENSION_LLM,
                UsageEvent.ts >= start,
                UsageEvent.ts < end,
            )
        )
    ).one()
    nested_llm = int(llm_total) if llm_rows and llm_total is not None else None

    return CompanionSpendResponse(
        month=month_label(start),
        currency="usd",
        companions=lines,
        nested_llm_usd_micros=nested_llm,
        total_usd_micros=_sum_or_none([line.total_usd_micros for line in lines] + [nested_llm]),
    )


def _floor_hour(moment: datetime) -> datetime:
    utc = moment.astimezone(UTC)
    return utc.replace(minute=0, second=0, microsecond=0)


def _ceil_hour(moment: datetime) -> datetime:
    floored = _floor_hour(moment)
    return floored if floored == moment.astimezone(UTC) else floored + HOUR


def _floor_day(moment: datetime) -> datetime:
    return _floor_hour(moment).replace(hour=0)


@dataclass(frozen=True)
class MeteringRunResult:
    compute_rows: int = 0
    storage_rows: int = 0
    failed_installations: int = 0


async def _last_accrued_hour(
    db: AsyncSession, installation: CompanionInstallation
) -> datetime | None:
    return await db.scalar(
        select(func.max(UsageEvent.period_start)).where(
            UsageEvent.installation_id == installation.id,
            UsageEvent.dimension == USAGE_DIMENSION_COMPUTE,
        )
    )


async def accrue_missing_compute(
    db: AsyncSession, installation: CompanionInstallation, now: datetime
) -> int:
    """Every whole billed hour that has ENDED by ``now`` and is not yet recorded."""
    started = installation.billing_started_at
    if started is None or installation.quoted_monthly_usd_micros is None:
        return 0
    last = await _last_accrued_hour(db, installation)
    hour = _ceil_hour(started) if last is None else _floor_hour(last) + HOUR
    end = billing_window_end(installation)
    horizon = now if end is None else min(now, end)
    written = 0
    while hour + HOUR <= horizon:
        if await accrue_compute(db, installation, hour):
            written += 1
        hour += HOUR
    return written


class CompanionMeteringWorker:
    """Accrue companion compute and storage into ``usage_events`` on a schedule (OPS-05)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        poll_interval_seconds: float = COMPANION_METERING_POLL_SECONDS,
        clock: Callable[[], datetime] | None = None,
        media_client_factory: MediaClientFactory | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._poll_interval_seconds = poll_interval_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._media_client_factory = media_client_factory or default_media_client_factory

    async def _billed_installation_ids(self, now: datetime) -> list[UUID]:
        cutoff = now - METERING_LOOKBACK
        async with self._sessionmaker() as db:
            rows = await db.scalars(
                select(CompanionInstallation.id)
                .where(
                    CompanionInstallation.billing_started_at.is_not(None),
                    CompanionInstallation.billing_started_at <= now,
                    or_(
                        CompanionInstallation.billing_stopped_at.is_(None),
                        CompanionInstallation.billing_stopped_at > cutoff,
                    ),
                    or_(
                        CompanionInstallation.removed_at.is_(None),
                        CompanionInstallation.removed_at > cutoff,
                    ),
                )
                .order_by(CompanionInstallation.id)
            )
            return list(rows.all())

    async def _meter_one(self, installation_id: UUID, now: datetime) -> tuple[int, int]:
        async with self._sessionmaker() as db:
            installation = await db.get(CompanionInstallation, installation_id)
            if installation is None:
                return 0, 0
            # Compute is committed before storage is measured, so a failing media listing costs
            # this installation its storage day and nothing else.
            compute = await accrue_missing_compute(db, installation, now)
            await db.commit()
            yesterday = _floor_day(now) - DAY
            storage = await accrue_storage(
                db, installation, yesterday, media_client_factory=self._media_client_factory
            )
            await db.commit()
            return compute, int(storage)

    async def run_once(self, now: datetime | None = None) -> MeteringRunResult:
        moment = (now or self._clock()).astimezone(UTC)
        compute_rows = storage_rows = failed = 0
        for installation_id in await self._billed_installation_ids(moment):
            try:
                compute, storage = await self._meter_one(installation_id, moment)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one installation must not stop the others' metering.
                failed += 1
                log.exception("companion metering failed installation_id=%s", installation_id)
                continue
            compute_rows += compute
            storage_rows += storage
        return MeteringRunResult(
            compute_rows=compute_rows, storage_rows=storage_rows, failed_installations=failed
        )

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop_event = stop or asyncio.Event()
        while not stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - metering must not stop other workers.
                log.exception("companion metering worker failed: %s", exc)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self._poll_interval_seconds)
            except TimeoutError:
                pass


__all__ = [
    "COMPANION_METERING_POLL_SECONDS",
    "DAY",
    "GIB",
    "HOUR",
    "METERING_LOOKBACK",
    "CompanionMeteringWorker",
    "MeteringRunResult",
    "accrue_compute",
    "accrue_missing_compute",
    "accrue_storage",
    "billing_window_end",
    "companion_spend_for",
    "compute_hour_cost",
    "days_in_month",
    "hours_in_month",
    "measure_media_bytes",
    "month_label",
    "month_start_of",
    "next_month_start",
    "parse_month",
    "period_is_billed",
    "storage_day_cost",
]
