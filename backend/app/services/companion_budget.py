"""The monthly money ceiling on a tenant's companion spend (OPS-06, D-20, D-21).

**Where it is checked.** ``companion_facade.call_companion_tool`` calls ``check_spend_ceiling``
after the policy gate and before the Phase 5 caps and the mint. That is the dispatch point Phase 5
already uses, so a call refused here never becomes a signed pre-authorized call (D-10). The check
sits after the policy gate on purpose: "the account owner's policy for this tool is deny" is the
more specific answer and must not be hidden behind the money ceiling.

**The second place it is checked (plan 06-31).** ``companion_install.create_installation`` calls
``check_spend_ceiling`` before any installation row, secret atom or job exists, and answers 409
``companion_spend_ceiling_reached``: a new companion adds recurring cost, so D-21 refuses new
provisioning at the ceiling as well as agent calls.

**What it compares.** The tenant's month-to-date spend (``month_to_date_spend``) against the
ceiling ``resolve_spend_ceiling`` finds. Spend is every ``usage_events`` row the tenant has this
UTC month (compute and storage rows by their ``period_start``, nested LLM rows of the tenant's
own sessions by their ``ts``; a runaway agent burns all three) PLUS the billed compute hours that
have ended but that the metering worker has not recorded yet, so accrual lag cannot outrun the
ceiling (research Pitfall 10). The check refuses when spend is AT or over the ceiling, so a
ceiling of zero refuses every call and every install. Zero is a value here, as it is for Phase
5's caps. A new UTC month starts the comparison again.

**Where the ceiling comes from.** The tenant's own row; failing that, the operator default
``settings.companion_default_spend_ceiling_usd_micros`` (source ``operator_default``); failing
that, no ceiling (source ``none``). The default is operator configuration with no committed
value (AGENTS.md OSS boundary).

**What it does NOT do (D-21).** It refuses the agent's call or the new install and nothing else.
No unit is stopped, no installation is suspended and no billing line item is touched, so a billing
event cannot become an availability event.

**Its own vocabulary.** ``REASON_SPEND_CEILING_REACHED`` is defined here and not in
``app/core/companion_caps.py``, whose ``CAP_REASONS`` and ``CAP_UNITS`` stay money-free. The façade
records the refusal under its own decision, ``CallDecision.REFUSED_BUDGET_CEILING``. The two
ceilings never share a token, so an activity row says which one fired.

**The agent cannot move it.** Every input here is tenant state read by ``user_id`` plus the
server's own clock. No function takes the call's arguments, so no tool argument can raise the
ceiling, lower the spend or pick a different month. The only writer of a tenant's ceiling is the
web-authenticated ``PUT /v1/companions/spend-ceiling``; the operator default is process
configuration.

**Known lag.** Nested LLM rows arrive when the client pushes a session after it runs, and storage
is recorded daily from measured bytes, so both can still trail real spend. Neither can be accrued
from a server-side clock. The repeated-call breaker (plan 06-23) is the fast signal for a runaway
agent; this ceiling is the money backstop. See ``month_to_date_spend``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.companion_budget import CompanionSpendCeiling
from app.models.companion_installation import CompanionInstallation
from app.models.session import Session
from app.models.usage_event import USAGE_DIMENSION_COMPUTE, USAGE_DIMENSION_LLM, UsageEvent
from app.services.companion_metering import (
    billing_window_end,
    compute_hour_cost,
    month_label,
    month_start_of,
    next_month_start,
)

_HOUR = timedelta(hours=1)

# The machine reason vocabulary, in the `REASON_*` style of `companion_caps`: a stable token kept
# apart from the prose, because the prose will be rewritten and the token is what a test, a
# provenance row and a future UI all match on. Neither value is a Phase 5 token.
REASON_WITHIN_SPEND_CEILING = "within_spend_ceiling"
REASON_SPEND_CEILING_REACHED = "spend_ceiling_reached"
SPEND_CEILING_REASONS = frozenset({REASON_WITHIN_SPEND_CEILING, REASON_SPEND_CEILING_REACHED})

# Where a resolved ceiling came from: the tenant's own row, the operator default (plan 06-31), or
# no ceiling at all. Only `resolve_spend_ceiling` produces these.
SpendCeilingSource = Literal["tenant", "operator_default", "none"]
SPEND_CEILING_SOURCE_TENANT: SpendCeilingSource = "tenant"
SPEND_CEILING_SOURCE_OPERATOR_DEFAULT: SpendCeilingSource = "operator_default"
SPEND_CEILING_SOURCE_NONE: SpendCeilingSource = "none"


@dataclass(frozen=True)
class ResolvedSpendCeiling:
    """The ceiling that applies to a tenant and where it came from.

    ``ceiling_usd_micros`` is ``None`` only when ``source`` is ``none``: no ceiling applies. That
    is a different statement from a ceiling of zero.
    """

    ceiling_usd_micros: int | None
    source: SpendCeilingSource


@dataclass(frozen=True)
class SpendCeilingDecision:
    """The ceiling's answer for one call, with the numbers it was judged on."""

    allowed: bool
    reason: str
    ceiling: ResolvedSpendCeiling
    spent_usd_micros: int
    month: str

    @property
    def reached(self) -> bool:
        return not self.allowed


async def month_to_date_spend(
    db: AsyncSession, *, user_id: UUID, now: datetime | None = None
) -> int:
    """The tenant's spend this UTC month, in integer micro-dollars, with no accrual lag.

    Two parts:

    * **Recorded spend.** The tenant's compute and storage rows whose period starts this month,
      plus the nested LLM rows of the tenant's own sessions stamped this month. The per-dimension
      breakdown lives in ``companion_metering.companion_spend_for``. The ceiling needs one number,
      and nothing recorded is zero spent; the metering read-out keeps its own null-versus-zero
      distinction.
    * **Compute not yet recorded** (``_unaccrued_compute``). The metering worker records compute
      by the hour, after the hour ends, so recorded rows alone trail real spend by however far the
      worker is behind. A ceiling compared to them could be outrun (research Pitfall 10). So, per
      billed installation, every whole billed hour of this month that has ENDED since its last
      recorded compute hour is added at its hourly cost. This is exactly the set of hours
      ``companion_metering.accrue_missing_compute`` would record at ``now``, at the same price, so
      once the worker catches up the total does not move and nothing is counted twice. The
      in-progress hour is not counted: it is not billed until it ends, and a partial hour at
      either end of the billing window is never billed.

    **Known remaining lag.** Nested LLM rows arrive when the client pushes a session after it
    runs, so LLM spend can still trail real spend by up to a session. There is no server-side
    clock to accrue it from. The repeated-call breaker (plan 06-23) is the fast signal for a
    runaway agent; this ceiling is the money backstop. Storage is recorded daily for the previous
    day from measured bytes, so up to a day of storage can also trail; it is small next to compute
    and cannot be estimated without a measurement.
    """
    moment = now or datetime.now(UTC)
    start = month_start_of(moment)
    end = next_month_start(start)
    companion_total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd_micros), 0)).where(
            UsageEvent.user_id == user_id,
            UsageEvent.dimension != USAGE_DIMENSION_LLM,
            UsageEvent.period_start >= start,
            UsageEvent.period_start < end,
        )
    )
    llm_total = await db.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_usd_micros), 0))
        .join(Session, UsageEvent.session_id == Session.id)
        .where(
            Session.user_id == user_id,
            UsageEvent.dimension == USAGE_DIMENSION_LLM,
            UsageEvent.ts >= start,
            UsageEvent.ts < end,
        )
    )
    unaccrued = await _unaccrued_compute(db, user_id=user_id, month_start=start, now=moment)
    return int(companion_total or 0) + int(llm_total or 0) + unaccrued


def _floor_hour(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _ceil_hour(moment: datetime) -> datetime:
    floored = _floor_hour(moment)
    return floored if floored == moment.astimezone(UTC) else floored + _HOUR


async def _unaccrued_compute(
    db: AsyncSession, *, user_id: UUID, month_start: datetime, now: datetime
) -> int:
    """This month's billed compute hours that have ended but have no recorded row yet.

    Mirrors ``companion_metering.accrue_missing_compute`` without writing: per installation with
    an accepted price and a started billing window, the hours from the one after its last
    recorded compute hour (or from billing's first whole hour) up to the earlier of ``now`` and
    the window's end, clipped to this month. Each costs ``compute_hour_cost`` for this month. One
    query for the tenant's installations, with each one's last recorded hour as a subquery.
    """
    month_end = next_month_start(month_start)
    last_recorded = (
        select(func.max(UsageEvent.period_start))
        .where(
            UsageEvent.installation_id == CompanionInstallation.id,
            UsageEvent.dimension == USAGE_DIMENSION_COMPUTE,
        )
        .correlate(CompanionInstallation)
        .scalar_subquery()
    )
    rows = await db.execute(
        select(CompanionInstallation, last_recorded).where(
            CompanionInstallation.user_id == user_id,
            CompanionInstallation.billing_started_at.is_not(None),
            CompanionInstallation.billing_started_at < now,
            CompanionInstallation.quoted_monthly_usd_micros.is_not(None),
            or_(
                CompanionInstallation.billing_stopped_at.is_(None),
                CompanionInstallation.billing_stopped_at > month_start,
            ),
            or_(
                CompanionInstallation.removed_at.is_(None),
                CompanionInstallation.removed_at > month_start,
            ),
        )
    )
    total = 0
    for installation, last in rows.tuples():
        started = installation.billing_started_at
        monthly = installation.quoted_monthly_usd_micros
        if started is None or monthly is None:
            continue
        first = _ceil_hour(started) if last is None else _floor_hour(last) + _HOUR
        first = max(first, month_start)
        window_end = billing_window_end(installation)
        horizon = min(now, month_end) if window_end is None else min(now, month_end, window_end)
        if horizon <= first:
            continue
        hours = (horizon - first) // _HOUR
        total += hours * compute_hour_cost(monthly, month_start)
    return total


async def resolve_spend_ceiling(db: AsyncSession, *, user_id: UUID) -> ResolvedSpendCeiling:
    """The ceiling that applies to this tenant: its own row, the operator default, or none.

    This is the ONE place a ceiling and its source are decided; the route, the façade and the
    install check all read it here. The tenant's own row wins, above or below the default. With
    no row, ``settings.companion_default_spend_ceiling_usd_micros`` applies when the operator set
    one (source ``operator_default``; zero is a value). Otherwise no ceiling applies (``none``),
    and the tenant is unbounded by money. The repetition breaker (plan 06-23) still applies.
    Nothing here reads a request or a tool argument.
    """
    ceiling = await db.scalar(
        select(CompanionSpendCeiling.ceiling_usd_micros).where(
            CompanionSpendCeiling.user_id == user_id
        )
    )
    if ceiling is not None:
        return ResolvedSpendCeiling(
            ceiling_usd_micros=int(ceiling), source=SPEND_CEILING_SOURCE_TENANT
        )
    default = settings.companion_default_spend_ceiling_usd_micros
    if default is not None:
        return ResolvedSpendCeiling(
            ceiling_usd_micros=int(default), source=SPEND_CEILING_SOURCE_OPERATOR_DEFAULT
        )
    return ResolvedSpendCeiling(ceiling_usd_micros=None, source=SPEND_CEILING_SOURCE_NONE)


async def check_spend_ceiling(
    db: AsyncSession, *, user_id: UUID, now: datetime | None = None
) -> SpendCeilingDecision:
    """Refuse when the month's spend (recorded plus unaccrued compute) is at or over the ceiling.

    No ceiling means allowed. Spend is only read when a ceiling applies, so a tenant with no
    ceiling (no row and no operator default) pays one primary-key lookup per call and nothing
    more.
    """
    moment = now or datetime.now(UTC)
    month = month_label(month_start_of(moment))
    ceiling = await resolve_spend_ceiling(db, user_id=user_id)
    if ceiling.ceiling_usd_micros is None:
        return SpendCeilingDecision(
            allowed=True,
            reason=REASON_WITHIN_SPEND_CEILING,
            ceiling=ceiling,
            spent_usd_micros=0,
            month=month,
        )
    spent = await month_to_date_spend(db, user_id=user_id, now=moment)
    return _judge(ceiling, spent=spent, month=month)


def _judge(ceiling: ResolvedSpendCeiling, *, spent: int, month: str) -> SpendCeilingDecision:
    """The one rule: reached when a ceiling applies and the month's spend is at or over it."""
    reached = ceiling.ceiling_usd_micros is not None and spent >= ceiling.ceiling_usd_micros
    return SpendCeilingDecision(
        allowed=not reached,
        reason=REASON_SPEND_CEILING_REACHED if reached else REASON_WITHIN_SPEND_CEILING,
        ceiling=ceiling,
        spent_usd_micros=spent,
        month=month,
    )


async def spend_ceiling_standing(
    db: AsyncSession, *, user_id: UUID, now: datetime | None = None
) -> SpendCeilingDecision:
    """The tenant's ceiling, its source and this month's spend, for the read-out.

    Unlike ``check_spend_ceiling`` it always reads the spend, because the tenant is shown what it
    has spent whether or not a ceiling is set. The spend is ``month_to_date_spend``'s: recorded
    rows plus compute hours not yet recorded, the same number the ceiling is judged on. The
    ceiling and its source come from ``resolve_spend_ceiling`` and nowhere else, and "reached" is
    the same rule the façade and the install check apply.
    """
    moment = now or datetime.now(UTC)
    ceiling = await resolve_spend_ceiling(db, user_id=user_id)
    spent = await month_to_date_spend(db, user_id=user_id, now=moment)
    return _judge(ceiling, spent=spent, month=month_label(month_start_of(moment)))


async def set_spend_ceiling(
    db: AsyncSession, *, user_id: UUID, ceiling_usd_micros: int, updated_by_user_id: UUID
) -> None:
    """Upsert the tenant's own ceiling row and commit.

    This is the one writer, reached only from the web-authenticated PUT. The tenant is the caller,
    because the request body cannot name one. The primary key on ``user_id`` makes this a single
    atomic upsert, so two racing saves leave one row holding the later value.
    """
    if ceiling_usd_micros < 0:
        raise ValueError("a spend ceiling cannot be negative")
    statement = pg_insert(CompanionSpendCeiling).values(
        user_id=user_id,
        ceiling_usd_micros=ceiling_usd_micros,
        updated_by_user_id=updated_by_user_id,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[CompanionSpendCeiling.user_id],
        set_={
            "ceiling_usd_micros": statement.excluded.ceiling_usd_micros,
            "updated_by_user_id": statement.excluded.updated_by_user_id,
            "updated_at": func.now(),
        },
    )
    await db.execute(statement)
    await db.commit()


def spend_ceiling_refusal_message(name: str) -> str:
    """The refusal an agent reads at the ceiling, written like Phase 5's cap refusals.

    The reason token is in the text so the agent, the provenance row and a test all see the same
    value. The sentence also tells the agent that the ceiling is an account setting and that no
    tool argument can raise it, which stops an agent from retrying with different arguments.
    """
    return (
        f"{name} was refused [{REASON_SPEND_CEILING_REACHED}]: this month's companion spend "
        "ceiling is reached. The ceiling is an account setting and cannot be raised from a tool "
        "argument."
    )


__all__ = [
    "REASON_SPEND_CEILING_REACHED",
    "REASON_WITHIN_SPEND_CEILING",
    "SPEND_CEILING_REASONS",
    "SPEND_CEILING_SOURCE_NONE",
    "SPEND_CEILING_SOURCE_OPERATOR_DEFAULT",
    "SPEND_CEILING_SOURCE_TENANT",
    "ResolvedSpendCeiling",
    "SpendCeilingDecision",
    "SpendCeilingSource",
    "check_spend_ceiling",
    "month_to_date_spend",
    "resolve_spend_ceiling",
    "set_spend_ceiling",
    "spend_ceiling_refusal_message",
    "spend_ceiling_standing",
]
