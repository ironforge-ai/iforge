"""The wait-versus-refuse decision, and the bucket key that keeps one tenant out of another's.

TOOL-09's shape is not "refuse when over the limit". It is "hitting the limit SLOWS the agent's task
rather than failing it" -- so the interesting assertions are about the boundary between a wait the
caller absorbs and a refusal the agent has to act on, not about the bucket arithmetic, which
``app/services/rate_limiter.py`` already owns and already tests.

**An injected clock, not a real one.** Every case below advances time by assignment. A test that
proved a refill by sleeping through it is a test nobody runs twice, and the refill nobody tests is
the wait that never ends in production. The shipped ``RateLimiter`` takes ``now`` for exactly this
reason and ``reset_companion_rate_limiter`` is how it is threaded in.

**Both halves of the key are asserted, in both directions.** That two tenants do NOT share a bucket
is the containment property (T-5-27); that two calls from one tenant to one companion DO share one
is what makes it a limit at all. An implementation keyed on the tenant alone passes the first and
fails the second; one keyed on the companion alone fails the first. Only asserting both pins the
join.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.companion_caps import retry_after_seconds_from_ms
from app.services.companion_ratelimit import (
    COMPANION_RATE_CAPACITY,
    COMPANION_RATE_REFILL_PER_SECOND,
    OBSERVED_RETRY_AFTER_FIELD,
    SAFE_WAIT_BUDGET_MS,
    SLOT_PROCEED,
    SLOT_REFUSE,
    SLOT_WAIT,
    acquire_companion_slot,
    companion_bucket_key,
    observe_retry_after,
    reset_companion_rate_limiter,
)


class Clock:
    """A monotonic clock a test moves by hand."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


@pytest.fixture
def clock() -> Clock:
    moment = Clock()
    reset_companion_rate_limiter(now=moment)
    yield moment
    reset_companion_rate_limiter()


def _drain(user_id: uuid.UUID, companion_key: str = "filebrowser") -> None:
    """Spend the whole burst so the next acquisition has to wait."""
    for _ in range(COMPANION_RATE_CAPACITY):
        assert (
            acquire_companion_slot(user_id=user_id, companion_key=companion_key).action
            == SLOT_PROCEED
        )


def test_a_call_inside_the_burst_proceeds_without_waiting(clock: Clock) -> None:
    slot = acquire_companion_slot(user_id=uuid.uuid4(), companion_key="filebrowser")

    assert slot.action == SLOT_PROCEED
    assert slot.wait_ms == 0
    assert slot.proceed is True


def test_an_empty_bucket_inside_the_safe_budget_waits_and_then_proceeds(clock: Clock) -> None:
    """The load-bearing case: the limit costs the task time, not the task itself."""
    user_id = uuid.uuid4()
    _drain(user_id)

    waiting = acquire_companion_slot(user_id=user_id, companion_key="filebrowser")
    assert waiting.action == SLOT_WAIT
    assert 0 < waiting.wait_ms <= SAFE_WAIT_BUDGET_MS

    # What the façade does with a wait decision: sleep exactly that long, then ask again. The
    # second ask is a real re-evaluation against the advanced clock, not a rubber stamp.
    clock.advance(waiting.wait_ms / 1000)
    assert acquire_companion_slot(user_id=user_id, companion_key="filebrowser").action == (
        SLOT_PROCEED
    )


def test_a_wait_beyond_the_safe_budget_refuses_with_an_explicit_retry_after(clock: Clock) -> None:
    """Beyond the budget the honest answer is a refusal carrying a number, not a longer wait.

    Waiting here would let the CLI proxy's 30-second timeout fire, and the agent would see a
    transport error it cannot act on instead of a retry-after it can (T-5-29).
    """
    user_id = uuid.uuid4()
    _drain(user_id)
    # A cost the bucket cannot satisfy for many seconds, so the deficit is well past the budget.
    cost = COMPANION_RATE_CAPACITY

    slot = acquire_companion_slot(user_id=user_id, companion_key="filebrowser", cost=cost)

    assert slot.action == SLOT_REFUSE
    assert slot.wait_ms > SAFE_WAIT_BUDGET_MS
    assert slot.proceed is False
    assert slot.retry_after_seconds == retry_after_seconds_from_ms(slot.wait_ms)
    assert slot.retry_after_seconds >= 1
    assert str(slot.retry_after_seconds) in slot.describe()


def test_the_retry_after_is_rounded_up_never_down() -> None:
    """A wait rounded DOWN arrives before the token exists, turning one refusal into two."""
    assert retry_after_seconds_from_ms(1) == 1
    assert retry_after_seconds_from_ms(1000) == 1
    assert retry_after_seconds_from_ms(1001) == 2
    assert retry_after_seconds_from_ms(0) == 0


def test_two_tenants_against_one_companion_do_not_share_a_bucket(clock: Clock) -> None:
    """The containment half of A9: one tenant cannot exhaust another's budget (T-5-27)."""
    noisy = uuid.uuid4()
    quiet = uuid.uuid4()
    _drain(noisy)

    assert acquire_companion_slot(user_id=noisy, companion_key="filebrowser").action == SLOT_WAIT
    assert acquire_companion_slot(user_id=quiet, companion_key="filebrowser").action == (
        SLOT_PROCEED
    )


def test_two_concurrent_calls_from_one_tenant_to_one_companion_share_one_bucket(
    clock: Clock,
) -> None:
    """The other half. Without it, a per-call bucket would pass the isolation test above."""
    user_id = uuid.uuid4()
    _drain(user_id)

    assert acquire_companion_slot(user_id=user_id, companion_key="filebrowser").action == SLOT_WAIT


def test_one_tenant_s_two_companions_do_not_share_a_bucket(clock: Clock) -> None:
    """Keyed on the tenant alone, a busy companion would throttle a quiet one."""
    user_id = uuid.uuid4()
    _drain(user_id, "filebrowser")

    assert acquire_companion_slot(user_id=user_id, companion_key="filebrowser").action == SLOT_WAIT
    assert acquire_companion_slot(user_id=user_id, companion_key="notebook").action == SLOT_PROCEED


def test_the_bucket_key_joins_the_tenant_and_the_companion() -> None:
    """Stated directly as well as behaviourally: the behaviour above is WHY, this is WHAT."""
    user_id = uuid.uuid4()

    key = companion_bucket_key(user_id=user_id, companion_key="filebrowser")

    assert str(user_id) in key
    assert "filebrowser" in key
    assert key != companion_bucket_key(user_id=user_id, companion_key="notebook")
    assert key != companion_bucket_key(user_id=uuid.uuid4(), companion_key="filebrowser")


def test_an_observed_retry_after_drains_this_tenant_s_bucket_and_nobody_else_s(
    clock: Clock,
) -> None:
    """The companion's own answer is authoritative; the control plane's capacity is a guess.

    Reported back by ``packages/cli/src/runtime/companion-executor.ts`` under
    ``OBSERVED_RETRY_AFTER_FIELD``, so the next call waits here rather than walking into a second
    429 at the companion.
    """
    throttled = uuid.uuid4()
    other = uuid.uuid4()

    drained = observe_retry_after(
        user_id=throttled, companion_key="filebrowser", retry_after_seconds=7
    )

    assert drained == COMPANION_RATE_CAPACITY
    assert acquire_companion_slot(user_id=throttled, companion_key="filebrowser").action == (
        SLOT_WAIT
    )
    assert acquire_companion_slot(user_id=other, companion_key="filebrowser").action == (
        SLOT_PROCEED
    )


def test_an_absent_retry_after_observation_changes_nothing(clock: Clock) -> None:
    """Zero is "the companion said nothing", not "drain the bucket"."""
    user_id = uuid.uuid4()

    drained = observe_retry_after(
        user_id=user_id, companion_key="filebrowser", retry_after_seconds=0
    )

    assert drained == 0
    assert acquire_companion_slot(user_id=user_id, companion_key="filebrowser").action == (
        SLOT_PROCEED
    )


def test_the_observed_field_name_is_the_one_the_executor_reports() -> None:
    """Pinned so the two halves of the wire contract cannot drift to two spellings."""
    from app.schemas.companions import CompanionDispatchResultRequest

    assert OBSERVED_RETRY_AFTER_FIELD in CompanionDispatchResultRequest.model_fields


def test_the_bucket_refills_at_the_configured_rate(clock: Clock) -> None:
    """The shipped constants are reachable configuration, not decoration."""
    user_id = uuid.uuid4()
    _drain(user_id)

    clock.advance(COMPANION_RATE_CAPACITY / COMPANION_RATE_REFILL_PER_SECOND)

    assert (
        acquire_companion_slot(
            user_id=user_id, companion_key="filebrowser", cost=COMPANION_RATE_CAPACITY
        ).action
        == SLOT_PROCEED
    )
