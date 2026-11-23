"""The repeated-call circuit breaker: a stuck loop is stopped on REPETITION, not volume (D-22).

OPS-06's fast half. Phase 5's caps bound how MUCH an agent does inside a window: calls, records,
bytes. A stuck agent that repeats one call with the same arguments can stay inside every one of
those caps and still burn a budget, because each repetition is individually cheap and the spend
ceiling (plan 06-22) only learns about cost after the metering worker has recorded it. The breaker
does not wait for cost data. It notices that the SAME call keeps arriving and stops it.

**"The same call" has one definition.** The breaker keys on Phase 5's canonical action hash, the
digest approvals already bind to (sorted keys, tight separators, the façade's control parameters
removed, the tenant and the agent inside the payload), plus the dry-run flag. A second
canonicalizer written for the breaker would disagree with the first about what "same" means.

**It is held in PostgreSQL, per tenant.** The process-wide rate limiter's per-worker looseness is
recorded and accepted for pacing. It is not acceptable for money protection, where a loop spread
over four workers would get four times the allowance.

Every test computes from the module's constants and never restates their values, so the judgement
values (threshold, window, cooldown) can be revisited without editing this file.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.models.companion_session_budget import CompanionCapPolicy
from app.models.companion_tool_call import CALL_DECISIONS, CompanionToolCall
from app.services import companion_facade
from app.services.companion_facade import call_companion_tool, reset_companion_tools_cache
from app.services.companion_ratelimit import reset_companion_rate_limiter
from tests.test_postiz_verbs import _deploy_postiz, _platform_minted_auth

REPEAT_DECISION = "refused_repeated_call"
REPEAT_REASON = "repeated_call_breaker_tripped"

LIST_POSTS = "postiz__list_posts"
LIST_INTEGRATIONS = "postiz__list_integrations"
POSTS_WINDOW = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}
OTHER_WINDOW = {"startDate": "2026-10-02T00:00:00Z", "endDate": "2026-10-03T00:00:00Z"}

# A fixed wall-clock origin. The breaker's window is wall time held in PostgreSQL, so the tests
# drive it with an injected clock instead of sleeping through two minutes.
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_companion_process_state():
    reset_companion_tools_cache()
    reset_companion_rate_limiter()
    yield
    reset_companion_tools_cache()
    reset_companion_rate_limiter()


class WallClock:
    """An injectable UTC clock the test advances by hand."""

    def __init__(self, start: datetime = T0) -> None:
        self.moment = start

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment = self.moment + timedelta(seconds=seconds)


async def _generous_caps(db_session, user_id: uuid.UUID, companion_key: str = "postiz") -> None:
    """Volume caps far above anything these tests do, so only the breaker can stop a call."""
    db_session.add(
        CompanionCapPolicy(
            user_id=user_id,
            companion_key=companion_key,
            per_call_calls=10_000,
            per_call_records=100_000,
            per_call_bytes=1_048_576,
            per_session_calls=10_000,
            per_session_records=1_000_000,
            per_session_bytes=104_857_600,
        )
    )
    await db_session.commit()


def _record_mints(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Wrap the real mint so a test can count which calls reached dispatch."""
    minted: list[dict[str, Any]] = []
    real_mint = companion_facade.mint_dispatch_call

    def _recording_mint(**kwargs: Any) -> Any:
        minted.append(kwargs)
        return real_mint(**kwargs)

    monkeypatch.setattr(companion_facade, "mint_dispatch_call", _recording_mint)
    return minted


async def _rows(db_session, user_id: uuid.UUID) -> list[CompanionToolCall]:
    return list(
        (
            await db_session.execute(
                select(CompanionToolCall)
                .where(CompanionToolCall.user_id == user_id)
                .order_by(CompanionToolCall.created_at, CompanionToolCall.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _call(
    db_session,
    seed_user,
    clock: Callable[[], datetime],
    *,
    name: str = LIST_POSTS,
    arguments: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    return await call_companion_tool(
        name,
        dict(POSTS_WINDOW if arguments is None else arguments),
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
        session_id=session_id,
        utc_now=clock,
    )


def _tripped(result: dict[str, Any]) -> bool:
    return result.get("isError") is True and f"[{REPEAT_REASON}]" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The tracer: a stuck loop is stopped on repetition while its volume caps still have room.
# ---------------------------------------------------------------------------


async def test_a_stuck_loop_is_stopped_before_the_volume_cap(
    db_session, seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The distinct decision is part of the behaviour: a breaker stop recorded under a cap or policy
    # token would leave a reader unable to tell a loop from a limit.
    assert REPEAT_DECISION in CALL_DECISIONS

    from app.services.companion_ratelimit import (
        REPEATED_CALL_THRESHOLD,
        REPEATED_CALL_WINDOW_SECONDS,
    )

    user_id = seed_user.id
    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, user_id)
    minted = _record_mints(monkeypatch)
    clock = WallClock()
    # Spread across the window but inside it: repetition, not burst, is what trips the breaker.
    step = REPEATED_CALL_WINDOW_SECONDS / (REPEATED_CALL_THRESHOLD + 2)

    results = []
    for _ in range(REPEATED_CALL_THRESHOLD + 1):
        results.append(await _call(db_session, seed_user, clock))
        clock.advance(step)

    # Calls 1..threshold reached dispatch; the next identical one did not.
    assert len(minted) == REPEATED_CALL_THRESHOLD
    assert not any(_tripped(result) for result in results[:REPEATED_CALL_THRESHOLD])
    stopped = results[REPEATED_CALL_THRESHOLD]
    assert _tripped(stopped), stopped
    text = stopped["content"][0]["text"]
    assert f"repeated {REPEATED_CALL_THRESHOLD + 1} times" in text
    assert LIST_POSTS in text

    # Partitioned rather than ordered: inside the rollback-isolated transaction every row shares
    # one `now()`, so `created_at` cannot order them.
    rows = await _rows(db_session, user_id)
    assert len(rows) == REPEATED_CALL_THRESHOLD + 1
    stops = [row for row in rows if row.decision == REPEAT_DECISION]
    passed = [row for row in rows if row.decision != REPEAT_DECISION]
    assert len(stops) == 1
    (stop,) = stops
    assert stop.repeat_count == REPEATED_CALL_THRESHOLD + 1
    assert stop.call_jti is None
    assert REPEAT_REASON in (stop.error_text or "")
    # The calls that passed reached the mint, and leave the count null: it is set only on stops.
    assert all(row.call_jti is not None for row in passed)
    assert all(row.repeat_count is None for row in passed)


# ---------------------------------------------------------------------------
# Isolation: only the SAME call, from the SAME tenant, is ever stopped.
# ---------------------------------------------------------------------------


async def _loop_to_the_threshold(db_session, seed_user, clock: WallClock, **call: Any) -> None:
    """Repeat one call exactly up to the threshold: every one of them must still pass."""
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD

    for _ in range(REPEATED_CALL_THRESHOLD):
        result = await _call(db_session, seed_user, clock, **call)
        assert not _tripped(result), result
        clock.advance(1)


async def test_different_arguments_are_not_the_same_call(db_session, seed_user) -> None:
    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, seed_user.id)
    clock = WallClock()
    await _loop_to_the_threshold(db_session, seed_user, clock)

    other = await _call(db_session, seed_user, clock, arguments=OTHER_WINDOW)
    assert not _tripped(other), other
    # The looping call's own count is untouched by the other argument set: one more trips it.
    assert _tripped(await _call(db_session, seed_user, clock))


async def test_different_verb_is_not_the_same_call(db_session, seed_user) -> None:
    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, seed_user.id)
    clock = WallClock()
    await _loop_to_the_threshold(db_session, seed_user, clock)

    other = await _call(db_session, seed_user, clock, name=LIST_INTEGRATIONS, arguments={})
    assert not _tripped(other), other
    assert _tripped(await _call(db_session, seed_user, clock))


async def test_another_tenant_is_unaffected(db_session, seed_user) -> None:
    """Same companion, verb, arguments and agent slot: only the tenant differs, and that suffices.

    The tenant comes from the caller's auth, never from an argument (T-5-08), and it both scopes
    the row and is inside the digest, so one tenant's loop cannot stop another tenant's call.
    """
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD, check_repeated_call
    from tests.test_companion_provenance import _other_tenant

    other = await _other_tenant(db_session)
    call = {
        "companion_key": "postiz",
        "verb": "list_posts",
        "arguments": dict(POSTS_WINDOW),
        "dry_run": False,
        "now": T0,
    }
    for _ in range(REPEATED_CALL_THRESHOLD + 1):
        looping = await check_repeated_call(db_session, user_id=seed_user.id, **call)
    assert not looping.allowed

    stranger = await check_repeated_call(db_session, user_id=other.id, **call)
    assert stranger.allowed
    assert stranger.count == 1


async def test_argument_order_does_not_create_a_new_call(db_session, seed_user) -> None:
    """Reordering the keys is not a new call: the canonical hash sorts them (T-06-23-03)."""
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD

    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, seed_user.id)
    clock = WallClock()
    forward = dict(POSTS_WINDOW)
    backward = dict(reversed(list(POSTS_WINDOW.items())))
    assert list(forward) != list(backward)

    results = []
    for index in range(REPEATED_CALL_THRESHOLD + 1):
        arguments = forward if index % 2 == 0 else backward
        results.append(await _call(db_session, seed_user, clock, arguments=arguments))
        clock.advance(1)

    assert not any(_tripped(result) for result in results[:REPEATED_CALL_THRESHOLD])
    assert _tripped(results[REPEATED_CALL_THRESHOLD])


async def test_dry_run_and_real_calls_are_counted_separately(db_session, seed_user) -> None:
    """A preview and the real call it previews are two different things to count.

    The canonical hash deliberately strips ``dry_run`` (so an approval of the preview covers the
    real call), which is exactly why the breaker keys on the flag as well as on the digest.
    """
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD, check_repeated_call

    call = {
        "user_id": seed_user.id,
        "companion_key": "postiz",
        "verb": "create_post",
        "arguments": {"type": "draft", "date": "2026-10-01T09:00:00Z"},
        "now": T0,
    }
    for _ in range(REPEATED_CALL_THRESHOLD):
        assert (await check_repeated_call(db_session, dry_run=False, **call)).allowed

    preview = await check_repeated_call(db_session, dry_run=True, **call)
    assert preview.allowed
    assert preview.count == 1
    assert not (await check_repeated_call(db_session, dry_run=False, **call)).allowed


# ---------------------------------------------------------------------------
# Time: the window forgets, the cooldown holds.
# ---------------------------------------------------------------------------


def _breaker_call(user_id: uuid.UUID) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "companion_key": "postiz",
        "verb": "list_posts",
        "arguments": dict(POSTS_WINDOW),
        "dry_run": False,
    }


async def test_window_expiry_resets_the_count(db_session, seed_user) -> None:
    from app.services.companion_ratelimit import (
        REPEATED_CALL_THRESHOLD,
        REPEATED_CALL_WINDOW_SECONDS,
        check_repeated_call,
    )

    call = _breaker_call(seed_user.id)
    for _ in range(REPEATED_CALL_THRESHOLD):
        assert (await check_repeated_call(db_session, now=T0, **call)).allowed

    later = T0 + timedelta(seconds=REPEATED_CALL_WINDOW_SECONDS + 1)
    fresh = await check_repeated_call(db_session, now=later, **call)
    assert fresh.allowed
    assert fresh.count == 1


async def test_cooldown_holds_after_a_trip(db_session, seed_user) -> None:
    """Stopped late in its window, a loop stays stopped after the window ends, until it pauses.

    The window elapsing alone must not hand a loop that is still calling a fresh allowance. Once
    the calls stop for the cooldown, the same call proceeds again with its count reset.
    """
    from app.services.companion_ratelimit import (
        REPEATED_CALL_COOLDOWN_SECONDS,
        REPEATED_CALL_THRESHOLD,
        REPEATED_CALL_WINDOW_SECONDS,
        check_repeated_call,
    )

    # The probe below sits after the window and inside the cooldown, which needs room for both.
    assert REPEATED_CALL_COOLDOWN_SECONDS > 2
    call = _breaker_call(seed_user.id)
    for _ in range(REPEATED_CALL_THRESHOLD):
        assert (await check_repeated_call(db_session, now=T0, **call)).allowed
    trip_at = T0 + timedelta(seconds=REPEATED_CALL_WINDOW_SECONDS - 1)
    tripped = await check_repeated_call(db_session, now=trip_at, **call)
    assert not tripped.allowed
    assert tripped.tripped_until == trip_at + timedelta(seconds=REPEATED_CALL_COOLDOWN_SECONDS)

    after_window = T0 + timedelta(seconds=REPEATED_CALL_WINDOW_SECONDS + 1)
    assert after_window < tripped.tripped_until
    still = await check_repeated_call(db_session, now=after_window, **call)
    assert not still.allowed
    assert still.count == REPEATED_CALL_THRESHOLD + 2

    assert still.tripped_until is not None
    released_at = still.tripped_until + timedelta(seconds=1)
    released = await check_repeated_call(db_session, now=released_at, **call)
    assert released.allowed
    assert released.count == 1


# ---------------------------------------------------------------------------
# Sharing: one breaker per tenant and call, across sessions and across workers.
# ---------------------------------------------------------------------------


async def test_two_sessions_share_one_breaker(db_session, seed_user) -> None:
    """Two MCP sessions of one tenant looping the same call are one loop, not two allowances."""
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD

    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, seed_user.id)
    clock = WallClock()

    results = []
    for index in range(REPEATED_CALL_THRESHOLD + 1):
        session = "session-a" if index % 2 == 0 else "session-b"
        results.append(await _call(db_session, seed_user, clock, session_id=session))
        clock.advance(1)

    assert not any(_tripped(result) for result in results[:REPEATED_CALL_THRESHOLD])
    assert _tripped(results[REPEATED_CALL_THRESHOLD])


# ---------------------------------------------------------------------------
# The count reaches the activity log.
# ---------------------------------------------------------------------------


async def test_activity_detail_carries_the_repeat_count(db_session, seed_user, client) -> None:
    """A breaker stop's detail carries how often the call repeated; every other row carries null.

    Null, not zero: "this was not a breaker stop" is not a count, and the detail sentence
    (06-UI-SPEC S7) must never render "repeated 0 times".
    """
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD

    await _deploy_postiz(db_session, seed_user)
    await _generous_caps(db_session, seed_user.id)
    clock = WallClock()
    for _ in range(REPEATED_CALL_THRESHOLD + 1):
        await _call(db_session, seed_user, clock)
        clock.advance(1)

    rows = await _rows(db_session, seed_user.id)
    details = {}
    for row in rows:
        response = await client.get(f"/v1/companions/activity/{row.id}")
        assert response.status_code == 200, response.text
        details[row.id] = response.json()

    stops = [body for body in details.values() if body["decision"] == REPEAT_DECISION]
    others = [body for body in details.values() if body["decision"] != REPEAT_DECISION]
    assert len(stops) == 1
    assert "repeat_count" in stops[0]
    assert stops[0]["repeat_count"] == REPEATED_CALL_THRESHOLD + 1
    assert others
    for body in others:
        assert "repeat_count" in body
        assert body["repeat_count"] is None


@pytest.mark.committed_db
async def test_concurrent_calls_count_exactly(seed_user, engine) -> None:
    """N workers counting the same call at once leave a count of exactly N.

    Each arrival runs on its own connection and commits, as separate API workers would. The
    upsert serializes on the row, so no increment is lost and exactly the arrivals past the
    threshold are stopped.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.companion_budget import CompanionRepeatBreaker
    from app.services.companion_ratelimit import REPEATED_CALL_THRESHOLD, check_repeated_call

    arrivals = REPEATED_CALL_THRESHOLD + 3
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    call = _breaker_call(seed_user.id)

    async def arrive() -> Any:
        async with sessionmaker() as worker_db:
            decision = await check_repeated_call(worker_db, now=T0, **call)
            await worker_db.commit()
            return decision

    decisions = await asyncio.gather(*(arrive() for _ in range(arrivals)))

    assert sorted(decision.count for decision in decisions) == list(range(1, arrivals + 1))
    assert sum(decision.allowed for decision in decisions) == REPEATED_CALL_THRESHOLD
    async with sessionmaker() as check_db:
        stored = (
            await check_db.execute(
                select(CompanionRepeatBreaker.count).where(
                    CompanionRepeatBreaker.user_id == seed_user.id
                )
            )
        ).scalar_one()
    assert stored == arrivals
