"""The companion rate limit: the second half of the blast-radius boundary (TOOL-09).

Caps bound how much one call may TOUCH. This bounds how FAST calls may arrive. The two are separate
gates over the same boundary and neither substitutes for the other: a tenant inside every cap can
still hammer a companion flat, and a tenant issuing one call a minute can still delete everything.

**Hitting the limit SLOWS the agent's task rather than failing it.** An empty bucket whose refill
lands inside the remaining safe budget makes the façade wait and then proceed -- the agent's task
takes longer and still completes, which is what a rate limit should feel like. Only a wait that
would outlive the proxy's budget returns an error, and that error carries an explicit retry-after in
seconds so the agent self-corrects instead of seeing an unexplained transport timeout.

**One bucket implementation, not two.** ``app/services/rate_limiter.py`` already ships
``TokenBucket`` and ``RateLimiter`` with an injectable clock, and its ``retry_after_ms`` is computed
with the same ceiling rounding ``app/core/companion_caps.retry_after_seconds_from_ms`` uses. A
second bucket written here would drift from it, and the first symptom of that drift would be a wait
one millisecond short of the refill -- a retry that fails for a reason nobody can reproduce.

**The key joins the tenant AND the companion (research assumption A9).** Per tenant alone breaks the
moment a second companion kind exists -- one busy companion would throttle a quiet one. Per
companion alone is a cross-tenant denial of service: one tenant's agent could exhaust the budget
every other tenant shares (T-5-27). Joining both means two tenants against one companion do not
share a bucket and two concurrent calls from one tenant to one companion do, which is exactly the
containment TOOL-09 asks for.

**The bucket LEARNS the companion's real limit.** ``observe_retry_after`` is called from
``app/routes/runtime.py`` when a runtime executor reports back the ``Retry-After`` it saw on a 429
from the companion itself. The control plane's own capacity below is a guess; the companion's answer
is authoritative, so an observed refusal drains this tenant's bucket and the next call waits rather
than walking into a second 429. ``packages/cli/src/runtime/companion-executor.ts`` is what reports
it, under the field name ``OBSERVED_RETRY_AFTER_FIELD``.

**The repeated-call breaker (plan 06-23, D-22) lives here too, and it is not a bucket.** The bucket
above bounds how FAST calls arrive; the caps bound how MUCH one call touches. Neither notices an
agent stuck in a loop, repeating the same call with the same arguments: every repetition is cheap
and slow enough to pass both, and the spend ceiling only sees the cost once it has been metered.
``check_repeated_call`` counts repetitions of ONE call, identified by the canonical action hash
approvals already bind to plus the dry-run flag, and stops the call once it repeats more than
``REPEATED_CALL_THRESHOLD`` times inside ``REPEATED_CALL_WINDOW_SECONDS``. Unlike the bucket it is
held in PostgreSQL, one row per tenant and call: the bucket's per-worker looseness is accepted for
pacing, but a loop spread across four workers must not get four allowances of money.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import and_, case, or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.companion_caps import retry_after_seconds_from_ms
from app.models.companion_budget import CompanionRepeatBreaker
from app.services.companion_dispatch import canonical_action_hash
from app.services.rate_limiter import RateLimiter

# The shipped bucket shape. Research assumption, not a measurement: a burst of twenty calls, then a
# sustained two per second. Tunable in the same sense `DEFAULT_CAPS` is -- revisit against a real
# agent's call pattern rather than treating either number as settled.
COMPANION_RATE_CAPACITY = 20
COMPANION_RATE_REFILL_PER_SECOND = 2.0

# How long the façade may sleep inside one call before the wait itself becomes the problem. The CLI
# proxy budgets `MCP_FORWARD_TIMEOUT_MS = 30_000` and the façade's own dispatch deadline is 25
# seconds, so five seconds is the headroom a wait may consume before the call even starts. A wait
# beyond it would let the 30-second proxy timeout fire, and the agent would see a transport error
# instead of a refusal it could act on (T-5-29).
SAFE_WAIT_BUDGET_MS = 5_000

# The field the runtime executor reports an observed `Retry-After` back under. Named here, beside
# the consumer, so the two halves of the wire contract are pinned to one spelling.
OBSERVED_RETRY_AFTER_FIELD = "retry_after_seconds"

SLOT_PROCEED = "proceed"
SLOT_WAIT = "wait"
SLOT_REFUSE = "refuse"

REASON_RATE_LIMIT_WAIT = "rate_limited_waiting"
REASON_RATE_LIMIT_REFUSED = "rate_limited_retry_after"

# The repeated-call breaker's shape (D-22). JUDGEMENT VALUES, not measurements, recorded as such
# here and in 06-23-SUMMARY.md the way WINDOWS #197 records `SESSION_BUDGET_LIFETIME_WINDOWS`:
#
# * THRESHOLD 5: a legitimate agent re-reads the same listing a handful of times while it works
#   (a retry after a transient failure, a re-check after a write). Six identical calls with the
#   same arguments inside two minutes is past any pattern we have seen that was not a loop.
# * WINDOW 120 s: long enough to catch a loop slowed by the rate limiter's waits (a waited call
#   still arrives within seconds), short enough that a workflow revisiting one read over an hour
#   is never counted as a loop.
# * COOLDOWN 120 s: once stopped, the same call stays stopped for this long after its latest
#   stop. A loop that keeps calling through the cooldown keeps extending it; a loop that pauses
#   comes back with its count reset.
#
# No test guards the values themselves; every test computes from these names. Revisit them against
# a real agent's call pattern, as `COMPANION_RATE_CAPACITY` asks for its own guess.
REPEATED_CALL_THRESHOLD = 5
REPEATED_CALL_WINDOW_SECONDS = 120
REPEATED_CALL_COOLDOWN_SECONDS = 120

REASON_REPEATED_CALL_BREAKER_TRIPPED = "repeated_call_breaker_tripped"


@dataclass(frozen=True)
class RateLimitSlot:
    """What the façade should do about this call: go, wait this long, or refuse with a retry-after.

    Three outcomes rather than a boolean, because "not allowed" conflates two very different
    answers. A wait is a slowdown the agent never has to know about; a refusal is information the
    agent needs in order to come back later instead of immediately.
    """

    action: str
    key: str
    wait_ms: int = 0
    retry_after_seconds: int = 0

    @property
    def proceed(self) -> bool:
        return self.action == SLOT_PROCEED

    def describe(self) -> str:
        """The refusal text an agent reads, carrying the machine reason and the wait verbatim."""
        return (
            f"[{REASON_RATE_LIMIT_REFUSED}] the companion is rate limited; "
            f"retry after {self.retry_after_seconds} seconds"
        )


def companion_bucket_key(*, user_id: UUID, companion_key: str) -> str:
    """The bucket key: the tenant identifier joined with the companion key (A9).

    ``user_id`` is re-derived by the caller from ``AuthContext`` on every call and never read from
    an argument, so this key cannot be pointed at another tenant's bucket (T-5-08).
    """
    return f"{user_id}:{companion_key}"


_limiter: RateLimiter | None = None


def companion_rate_limiter() -> RateLimiter:
    """The process-wide bucket set, created on first use.

    Process-wide rather than per-request: a limiter rebuilt for each call would start full every
    time, which is a limiter that limits nothing. Multi-process deployments therefore get one bucket
    per worker -- a known and accepted looseness, recorded rather than hidden, because moving the
    bucket into PostgreSQL or Redis is a decision with its own cost and TOOL-09's containment
    property (one tenant cannot drain another's) holds either way.
    """
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(COMPANION_RATE_CAPACITY, COMPANION_RATE_REFILL_PER_SECOND)
    return _limiter


def reset_companion_rate_limiter(*, now: Callable[[], float] | None = None) -> RateLimiter:
    """Replace the process-wide bucket set, optionally with an injected clock. Tests only.

    Returned so a test can hold the same instance it installed; the injectable ``now`` is what makes
    "waits and then proceeds" assertable without sleeping through a real refill.
    """
    global _limiter
    _limiter = RateLimiter(COMPANION_RATE_CAPACITY, COMPANION_RATE_REFILL_PER_SECOND, now=now)
    return _limiter


def acquire_companion_slot(
    *,
    user_id: UUID,
    companion_key: str,
    cost: int = 1,
    safe_budget_ms: int = SAFE_WAIT_BUDGET_MS,
    limiter: RateLimiter | None = None,
) -> RateLimitSlot:
    """Take a slot for one call, or say how long to wait, or refuse with a retry-after.

    The wait-versus-refuse line is ``safe_budget_ms``. Inside it the caller sleeps and asks again --
    the bucket has an injectable clock, so the second ask is a real re-evaluation rather than a
    rubber stamp. Beyond it the caller gets a refusal carrying whole seconds, rounded UP by
    ``retry_after_seconds_from_ms`` so a retry never arrives before the token exists.
    """
    bucket = limiter if limiter is not None else companion_rate_limiter()
    key = companion_bucket_key(user_id=user_id, companion_key=companion_key)
    result = bucket.try_consume(key, cost)
    if result.allowed:
        return RateLimitSlot(action=SLOT_PROCEED, key=key)
    if result.retry_after_ms <= safe_budget_ms:
        return RateLimitSlot(
            action=SLOT_WAIT,
            key=key,
            wait_ms=result.retry_after_ms,
            retry_after_seconds=retry_after_seconds_from_ms(result.retry_after_ms),
        )
    return RateLimitSlot(
        action=SLOT_REFUSE,
        key=key,
        wait_ms=result.retry_after_ms,
        retry_after_seconds=retry_after_seconds_from_ms(result.retry_after_ms),
    )


def observe_retry_after(
    *,
    user_id: UUID,
    companion_key: str,
    retry_after_seconds: int,
    limiter: RateLimiter | None = None,
) -> int:
    """Teach this tenant's bucket that the companion itself refused, and how long it asked for.

    The companion's own ``Retry-After`` is authoritative and the control plane's configured capacity
    is a guess, so an observed refusal DRAINS the bucket: the next call from this tenant to this
    companion waits or is refused by the control plane rather than walking into a second 429.

    Drained through the shipped ``try_consume`` rather than by reaching into ``TokenBucket``'s
    internals, and bounded by the capacity, so this module still declares no bucket of its own.
    Returns how many tokens it removed, which is what a test asserts against.
    """
    if retry_after_seconds <= 0:
        return 0
    bucket = limiter if limiter is not None else companion_rate_limiter()
    key = companion_bucket_key(user_id=user_id, companion_key=companion_key)
    drained = 0
    for _ in range(COMPANION_RATE_CAPACITY):
        if not bucket.try_consume(key, 1).allowed:
            break
        drained += 1
    return drained


@dataclass(frozen=True)
class RepeatedCallDecision:
    """Whether this call may proceed past the repeated-call breaker, and how often it has repeated.

    ``count`` is the number of times this exact call has arrived in the current run of
    repetitions, this arrival included. It is what a stopped call's provenance row records as
    ``repeat_count`` and what the activity log's detail sentence reads.
    """

    allowed: bool
    count: int
    tripped_until: datetime | None = None

    @property
    def reason(self) -> str | None:
        return None if self.allowed else REASON_REPEATED_CALL_BREAKER_TRIPPED

    def describe(self, tool: str) -> str:
        """The refusal text an agent reads: the machine reason, the count, and what to do next."""
        return (
            f"{tool} was stopped [{REASON_REPEATED_CALL_BREAKER_TRIPPED}]: this call repeated "
            f"{self.count} times with the same arguments within {REPEATED_CALL_WINDOW_SECONDS} "
            "seconds. The loop was broken before it spent more of your budget. Change the "
            "arguments or wait before retrying."
        )


async def check_repeated_call(
    db: AsyncSession,
    *,
    user_id: UUID,
    companion_key: str,
    verb: str,
    arguments: dict[str, JsonValue] | None,
    dry_run: bool,
    agent_id: UUID | None = None,
    now: datetime | None = None,
) -> RepeatedCallDecision:
    """Count one arrival of this call and say whether the breaker stops it.

    **One statement, no read-then-write.** The count is an ``INSERT ... ON CONFLICT DO UPDATE`` on
    ``(user_id, companion_key, verb, action_sha256, dry_run)`` that either starts the row at one or
    updates it in place, and ``RETURNING`` hands back the result. PostgreSQL locks the conflicting
    row for the update, so two workers counting the same call at the same instant are serialized
    and N concurrent arrivals leave a count of exactly N.

    **The window resets only when it has elapsed AND no cooldown is running.** Resetting during a
    cooldown would let a loop that keeps calling get a fresh allowance the moment the window rolled
    over, which is precisely the loop this exists to stop.

    **The trip.** A call is stopped when its count exceeds ``REPEATED_CALL_THRESHOLD`` or a cooldown
    is still running. Every stop by count sets ``tripped_until`` to ``now`` plus the cooldown, in
    the same statement.

    ``user_id`` is ``auth.user_id``, re-derived by the caller, and never an argument: it scopes
    the row AND is inside the digest, so one tenant's loop cannot stop another tenant's call
    (T-5-08, T-06-23-02). The digest is ``canonical_action_hash``, so reordering the arguments or
    adding the façade's control parameters does not make a "new" call (T-06-23-03).

    The caller commits. The façade commits right after this returns, so a count survives whatever
    the call does next, including a downstream refusal that rolls its own work back.
    """
    moment = now if now is not None else datetime.now(UTC)
    action_sha256 = canonical_action_hash(
        companion_key=companion_key,
        verb=verb,
        arguments=arguments,
        tenant_id=user_id,
        agent_id=agent_id,
    )
    window_cutoff = moment - timedelta(seconds=REPEATED_CALL_WINDOW_SECONDS)
    cooldown_until = moment + timedelta(seconds=REPEATED_CALL_COOLDOWN_SECONDS)
    table = CompanionRepeatBreaker.__table__
    existing = table.c

    resets = and_(
        existing.window_started_at <= window_cutoff,
        or_(existing.tripped_until.is_(None), existing.tripped_until <= moment),
    )
    next_count = case((resets, 1), else_=existing.count + 1)
    statement = (
        pg_insert(table)
        .values(
            user_id=user_id,
            companion_key=companion_key,
            verb=verb,
            action_sha256=action_sha256,
            dry_run=dry_run,
            window_started_at=moment,
            count=1,
            # Only a threshold below one trips on the first arrival; the shipped value never does.
            tripped_until=cooldown_until if REPEATED_CALL_THRESHOLD < 1 else None,
        )
        .on_conflict_do_update(
            index_elements=[
                existing.user_id,
                existing.companion_key,
                existing.verb,
                existing.action_sha256,
                existing.dry_run,
            ],
            set_={
                "count": next_count,
                "window_started_at": case((resets, moment), else_=existing.window_started_at),
                "tripped_until": case(
                    (next_count > REPEATED_CALL_THRESHOLD, cooldown_until),
                    else_=existing.tripped_until,
                ),
                "updated_at": moment,
            },
        )
        .returning(existing.count, existing.tripped_until)
    )
    count, tripped_until = (await db.execute(statement)).one()
    stopped = count > REPEATED_CALL_THRESHOLD or (
        tripped_until is not None and tripped_until > moment
    )
    return RepeatedCallDecision(allowed=not stopped, count=count, tripped_until=tripped_until)
