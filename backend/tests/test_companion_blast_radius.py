"""The blast-radius cap vector: its boundaries, its immunity to arguments, its concurrency.

TOOL-04 asks for "per-call and per-session blast-radius caps that an agent cannot raise". A single
cap NUMBER does not deliver that. ``05-RESEARCH.md`` verified that ``DELETE /api/resources`` is
bulk-capable and that ``PATCH /api/resources`` moves, copies and renames through one endpoint, so
one permitted call can be arbitrarily destructive and a calls-only cap bounds nothing. What is
asserted here is therefore a cap VECTOR over three independent units -- ``calls``, ``records`` and
``bytes`` -- each able to refuse a call on its own.

**Both sides of every boundary, deliberately.** A test that only asserts the over-cap case cannot
distinguish an off-by-one from a correct limit: an implementation refusing at ``cap`` and an
implementation refusing at ``cap + 1`` both pass it, and only one of them is right. Every unit below
is asserted at exactly the cap AND one unit beyond it.

**Zero is a value, not an absence.** A cap of zero means "refuse everything", and the most common
way that guarantee is lost is an implementation reading ``0`` as falsy and treating it as unset --
turning the tightest possible policy into no policy at all. It is asserted per unit for that reason.

**"An agent cannot raise the cap" is proven two independent ways**, because the planner's edge-probe
row for TOOL-04 is ``unclassified``: the testable edges are the boundary and the NEGATIVE property
that no argument path reaches the cap value. One assertion covers the validation rejection (a
cap-shaped argument key is refused because the injected schema forbids extras); the other covers
provenance (mutating the tenant row changes the decision while the arguments stay byte-identical).
Either alone would leave the other path open.

**The concurrency case runs on two independent connections**, under ``committed_db``. A read-then-
write against a stale remainder is the classic double-spend, and it is invisible to any test whose
writes never leave one transaction -- which is every other test in this file's default lane. The
module-level marker puts the whole file on the committed lane, following the shipped example in
``backend/tests/test_mcp_capability_parity.py``; ``seed_user`` cleans the tenant up afterwards.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.core.companion_caps import (
    CAP_REASONS,
    CAP_UNIT_BYTES,
    CAP_UNIT_CALLS,
    CAP_UNIT_RECORDS,
    DEFAULT_CAPS,
    REASON_PER_CALL_CAP_EXCEEDED,
    REASON_SESSION_BUDGET_EXHAUSTED,
    REASON_SESSION_BUDGET_UNRESOLVED,
    REASON_TRUNCATED_AT_PER_CALL_CAP,
    REASON_WITHIN_CAP,
    CapVector,
    check_per_call_cap,
    debit_session_budget,
)
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_session_budget import (
    SESSION_BUDGET_LIFETIME_SECONDS,
    SESSION_BUDGET_WINDOW_SECONDS,
    CompanionCapPolicy,
    CompanionSessionBudget,
)
from app.models.companion_tool_policy import CompanionDestroyEnablement, CompanionToolPolicy
from app.services.companion_facade import (
    _COMPANION_INPUT_SCHEMA,
    _budget_selector,
    _debit_ledger,
    apply_session_debit,
    budget_window,
    call_companion_tool,
    release_session_reservation,
    reset_companion_tools_cache,
    session_budget_cutoff,
)
from tests.hosted_runtime_fixtures import deploy_companion_state

pytestmark = pytest.mark.committed_db

# A deliberately TINY vector. The shipped defaults are megabytes and hundreds of records; asserting
# a boundary against them would mean building a 10 MiB fixture to prove an off-by-one. The cap
# arithmetic is the same at every magnitude, and `test_default_caps_ship_the_documented_six_values`
# is what pins the shipped numbers.
TIGHT = CapVector(
    per_call_calls=1,
    per_call_records=50,
    per_call_bytes=1024,
    per_session_calls=3,
    per_session_records=10,
    per_session_bytes=4096,
)

PER_CALL_AT_CAP = [
    (CAP_UNIT_CALLS, TIGHT.per_call_calls),
    (CAP_UNIT_RECORDS, TIGHT.per_call_records),
    (CAP_UNIT_BYTES, TIGHT.per_call_bytes),
]

READ_TOOL = "filebrowser__list_dir"
DELETE_TOOL = "filebrowser__delete_path"
MOVE_TOOL = "filebrowser__move_path"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


async def _allow(db_session, seed_user, verb: str) -> None:
    """An explicit ``allow`` row, so a refusal below is the CAP and never the policy.

    Every cap assertion that goes through the façade opens both earlier gates first. A cap test
    that passed because the destroy switch or the policy row refused the call would be asserting
    05-03's behaviour under a 05-04 name -- green, and evidence of nothing.
    """
    db_session.add(
        CompanionToolPolicy(
            user_id=seed_user.id, companion_key="filebrowser", verb=verb, mode="allow"
        )
    )
    await db_session.commit()


async def _enable_destroy(db_session, seed_user) -> None:
    """The tenant's destroy switch, so a ``destroy``-class cap test reaches the cap at all."""
    db_session.add(
        CompanionDestroyEnablement(
            user_id=seed_user.id,
            companion_key="filebrowser",
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=seed_user.id,
        )
    )
    await db_session.commit()


async def _cap_policy(db_session, seed_user, **overrides) -> CompanionCapPolicy:
    """This tenant's cap row. The ONLY place a cap value ever comes from."""
    values = {
        "per_call_calls": 1,
        "per_call_records": 50,
        "per_call_bytes": 1024,
        "per_session_calls": 10,
        "per_session_records": 10,
        "per_session_bytes": 4096,
    }
    values.update(overrides)
    row = CompanionCapPolicy(user_id=seed_user.id, companion_key="filebrowser", **values)
    db_session.add(row)
    await db_session.commit()
    return row


async def _budget_row(db_session, seed_user) -> CompanionSessionBudget | None:
    return (
        await db_session.execute(
            select(CompanionSessionBudget)
            .where(CompanionSessionBudget.user_id == seed_user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The per-call boundary, both sides, for every unit.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("unit", "cap"), PER_CALL_AT_CAP)
def test_a_call_at_exactly_the_per_call_cap_is_allowed(unit: str, cap: int) -> None:
    decision = check_per_call_cap(unit=unit, requested=cap, tool_class="destroy", caps=TIGHT)

    assert decision.allowed is True
    assert decision.unit == unit
    assert decision.requested == cap
    assert decision.truncate_to is None


@pytest.mark.parametrize(("unit", "cap"), PER_CALL_AT_CAP)
def test_one_unit_beyond_the_per_call_cap_is_refused(unit: str, cap: int) -> None:
    """The other half of the boundary. Without it, refusing at ``cap`` would also pass."""
    decision = check_per_call_cap(unit=unit, requested=cap + 1, tool_class="destroy", caps=TIGHT)

    assert decision.allowed is False
    assert decision.reason == REASON_PER_CALL_CAP_EXCEEDED
    assert decision.unit == unit
    assert decision.requested == cap + 1
    assert decision.remaining == cap


def test_a_read_one_byte_beyond_the_per_call_cap_is_truncated_rather_than_refused() -> None:
    """A read that is too big is CLIPPED and says so; refusing it would lose the whole answer.

    Truncation is only safe in this one direction. The agent still gets the first ``cap`` bytes and
    an envelope saying it was cut, which is strictly more useful than a refusal and leaks nothing
    the cap was protecting.
    """
    decision = check_per_call_cap(
        unit=CAP_UNIT_BYTES, requested=TIGHT.per_call_bytes + 1, tool_class="read", caps=TIGHT
    )

    assert decision.allowed is True
    assert decision.truncate_to == TIGHT.per_call_bytes
    assert decision.reason == REASON_TRUNCATED_AT_PER_CALL_CAP
    assert decision.requested == TIGHT.per_call_bytes + 1
    assert decision.remaining == TIGHT.per_call_bytes


def test_a_write_one_byte_beyond_the_per_call_cap_is_refused_rather_than_truncated() -> None:
    """Truncating a WRITE would write half a file and report success. Refuse instead."""
    decision = check_per_call_cap(
        unit=CAP_UNIT_BYTES, requested=TIGHT.per_call_bytes + 1, tool_class="create", caps=TIGHT
    )

    assert decision.allowed is False
    assert decision.truncate_to is None
    assert decision.reason == REASON_PER_CALL_CAP_EXCEEDED


@pytest.mark.parametrize("unit", [CAP_UNIT_CALLS, CAP_UNIT_RECORDS, CAP_UNIT_BYTES])
def test_a_zero_cap_refuses_the_first_call(unit: str) -> None:
    """Zero means "refuse everything", never "unset".

    An implementation that reads ``0`` as falsy and falls back to a default turns the tightest
    possible policy into no policy at all, and it does so silently.
    """
    zeroed = CapVector(
        per_call_calls=0,
        per_call_records=0,
        per_call_bytes=0,
        per_session_calls=0,
        per_session_records=0,
        per_session_bytes=0,
    )

    decision = check_per_call_cap(unit=unit, requested=1, tool_class="read", caps=zeroed)

    assert decision.allowed is False
    assert decision.remaining == 0
    # A read is the case a truncating implementation would get wrong: clipping to zero bytes is
    # not an answer, it is a refusal wearing a success envelope.
    assert decision.truncate_to is None


# ---------------------------------------------------------------------------
# The per-session budget boundary, both sides.
# ---------------------------------------------------------------------------


def test_a_session_at_exactly_its_calls_budget_refuses_the_next_call() -> None:
    decision = debit_session_budget(
        unit=CAP_UNIT_CALLS, requested=1, consumed=TIGHT.per_session_calls, caps=TIGHT
    )

    assert decision.allowed is False
    assert decision.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert decision.remaining == 0


def test_a_session_one_call_below_its_budget_allows_one_more_and_then_refuses() -> None:
    """The transition itself, not just the endpoint either side of it."""
    consumed = TIGHT.per_session_calls - 1

    allowed = debit_session_budget(unit=CAP_UNIT_CALLS, requested=1, consumed=consumed, caps=TIGHT)
    assert allowed.allowed is True
    assert allowed.remaining == 0

    refused = debit_session_budget(
        unit=CAP_UNIT_CALLS, requested=1, consumed=consumed + 1, caps=TIGHT
    )
    assert refused.allowed is False


def test_a_session_budget_never_truncates() -> None:
    """A per-CALL bytes cap clips; a session budget must not.

    Silently shrinking an approved call because of unrelated earlier history would make the same
    request return different amounts of data at different points in a session, with nothing in the
    request explaining why.
    """
    decision = debit_session_budget(
        unit=CAP_UNIT_BYTES, requested=TIGHT.per_session_bytes + 1, consumed=0, caps=TIGHT
    )

    assert decision.allowed is False
    assert decision.truncate_to is None


@pytest.mark.parametrize("unit", [CAP_UNIT_CALLS, CAP_UNIT_RECORDS, CAP_UNIT_BYTES])
def test_every_refusal_carries_its_unit_requested_and_remaining(unit: str) -> None:
    """A refusal that says only "over cap" is not visible enough for TOOL-04's user-facing half.

    The unit says WHICH ceiling was hit, ``requested`` says how much the call wanted, and
    ``remaining`` says how much was left -- which together are what a user needs to decide whether
    to raise the cap, split the task, or stop.
    """
    per_call = check_per_call_cap(unit=unit, requested=10_000_000, tool_class="destroy", caps=TIGHT)
    session = debit_session_budget(unit=unit, requested=10_000_000, consumed=0, caps=TIGHT)

    for decision in (per_call, session):
        assert decision.allowed is False
        assert decision.unit == unit
        assert decision.requested == 10_000_000
        assert isinstance(decision.remaining, int)
        assert decision.remaining >= 0


def test_default_caps_ship_the_documented_six_values() -> None:
    """Research assumption A5, pinned. Changing a number here is a deliberate edit, not a drift."""
    assert DEFAULT_CAPS.per_call_calls == 1
    assert DEFAULT_CAPS.per_call_records == 50
    assert DEFAULT_CAPS.per_call_bytes == 10 * 1024 * 1024
    assert DEFAULT_CAPS.per_session_calls == 200
    assert DEFAULT_CAPS.per_session_records == 500
    assert DEFAULT_CAPS.per_session_bytes == 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# An agent cannot raise a cap. Two independent proofs.
# ---------------------------------------------------------------------------


def test_the_injected_input_schema_declares_no_cap_shaped_property() -> None:
    """The structural half: there is no property to set, and extras are forbidden.

    This is what makes an argument-supplied override impossible rather than merely filtered. A
    filter is a list somebody has to keep complete; a closed schema is not.
    """
    assert _COMPANION_INPUT_SCHEMA["additionalProperties"] is False
    for name in _COMPANION_INPUT_SCHEMA["properties"]:
        lowered = name.lower()
        assert "cap" not in lowered
        assert "max" not in lowered
        assert "limit" not in lowered
        assert "budget" not in lowered


@pytest.mark.parametrize(
    "cap_shaped_key", ["max_records", "cap", "limit_override", "per_session_bytes"]
)
async def test_a_cap_shaped_argument_is_rejected_by_validation(
    db_session, seed_user, companion_stub, cap_shaped_key: str
) -> None:
    """The validation half: the call never reaches the cap check at all.

    The refusal is asserted by the ACCEPTED SET it names rather than by a fixed sentence: plan
    05-06 added the ``approval`` control parameter to every verb's schema, so the exact wording
    moved while the property -- a cap-shaped argument is not in the closed set and is rejected
    before any cap is consulted -- did not. What must never appear in that set is the offending
    key itself, which is what the last assertion pins.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capargs")
    await _cap_policy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        READ_TOOL,
        {"path": "/", cap_shaped_key: 10_000_000},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "rejected its arguments" in text
    assert "only 'path'" in text
    assert cap_shaped_key not in text
    assert companion_stub.requests == []


async def test_mutating_the_tenant_row_changes_the_decision_while_arguments_stay_constant(
    db_session, seed_user, companion_stub
) -> None:
    """The provenance half: the value used came from the tenant row and from nowhere else.

    The arguments are byte-identical across both calls. The only thing that changed is a row the
    agent has no write path to, and the decision followed it -- which is what "an agent cannot raise
    the cap" means operationally.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="caprow")
    policy = await _cap_policy(db_session, seed_user, per_call_calls=0)
    auth = AuthContext(user=seed_user)
    arguments = {"path": "/"}

    refused = await call_companion_tool(
        READ_TOOL, dict(arguments), auth=auth, db=db_session, deadline_seconds=0
    )
    assert refused["isError"] is True
    assert REASON_PER_CALL_CAP_EXCEEDED in refused["content"][0]["text"]

    policy.per_call_calls = 1
    db_session.add(policy)
    await db_session.commit()

    allowed = await call_companion_tool(
        READ_TOOL, dict(arguments), auth=auth, db=db_session, deadline_seconds=0
    )
    # Past the cap gate and into the dispatch queue: the deadline is zero, so the call expires
    # waiting for an executor rather than being refused by a cap.
    assert REASON_PER_CALL_CAP_EXCEEDED not in allowed["content"][0]["text"]
    budget = await _budget_row(db_session, seed_user)
    assert budget is not None
    assert budget.calls_consumed == 1


# ---------------------------------------------------------------------------
# A bulk verb debits what it resolves, not one call.
# ---------------------------------------------------------------------------


async def test_a_bulk_delete_debits_its_resolved_target_count_in_records(
    db_session, seed_user, companion_stub
) -> None:
    """One approved delete must not expand into an unbounded one.

    ``filebrowser__delete_path`` carries ``cap_unit='records'`` precisely because the companion's
    ``DELETE /api/resources`` is bulk-capable. Debiting one ``call`` for a thousand deletions would
    make the session budget a count of REQUESTS, which bounds nothing an agent cares about.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capbulk")
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    await _cap_policy(db_session, seed_user, per_call_records=1000, per_session_records=1000)
    auth = AuthContext(user=seed_user)

    await call_companion_tool(
        DELETE_TOOL,
        {"path": "/projects"},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
        count_targets=lambda entry, arguments: 1000,
    )

    budget = await _budget_row(db_session, seed_user)
    assert budget is not None
    assert budget.records_consumed == 1000
    assert budget.calls_consumed == 0


async def test_a_bulk_delete_beyond_the_records_cap_is_refused_before_it_is_minted(
    db_session, seed_user, companion_stub
) -> None:
    """The refusal is upstream of the mint, so no signed pre-authorized call ever exists."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="capbulkover")
    await _enable_destroy(db_session, seed_user)
    await _allow(db_session, seed_user, "delete_path")
    await _cap_policy(db_session, seed_user, per_call_records=999, per_session_records=10_000)
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        DELETE_TOOL,
        {"path": "/projects"},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
        count_targets=lambda entry, arguments: 1000,
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert REASON_PER_CALL_CAP_EXCEEDED in text
    assert CAP_UNIT_RECORDS in text
    assert "1000" in text
    assert companion_stub.requests == []
    live = (
        (
            await db_session.execute(
                select(CompanionDispatchCall.status).where(
                    CompanionDispatchCall.user_id == seed_user.id
                )
            )
        )
        .scalars()
        .all()
    )
    # Only the refusal record. A queued row would mean a signed call was minted for a capped
    # request and merely not delivered.
    assert set(live) == {"failed"}


async def test_an_exhausted_session_budget_leaves_no_queued_dispatch_row(
    db_session, seed_user, companion_stub
) -> None:
    """The debit is inside the minting transaction, so refusing it unwinds the mint.

    A queued row surviving a refused debit would mean the signed pre-authorized call outlived the
    decision that was supposed to stop it -- and the runtime executor would happily run it.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capbudget")
    await _cap_policy(db_session, seed_user, per_session_calls=0)
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        READ_TOOL, {"path": "/"}, auth=auth, db=db_session, deadline_seconds=0
    )

    assert result["isError"] is True
    assert REASON_SESSION_BUDGET_EXHAUSTED in result["content"][0]["text"]
    statuses = (
        (
            await db_session.execute(
                select(CompanionDispatchCall.status).where(
                    CompanionDispatchCall.user_id == seed_user.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert set(statuses) == {"failed"}
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# The double-spend, on two independent connections.
# ---------------------------------------------------------------------------


async def test_two_concurrent_debits_against_room_for_one_allow_exactly_one(
    db_session, seed_user, engine
) -> None:
    """Two callers, a budget with room for one, exactly one winner.

    Sequenced rather than raced: the second debit is started while the first holds the row lock and
    is only released by the first commit. That makes the assertion deterministic AND makes it a real
    test of the conditional update -- a read-then-write implementation would re-read the remainder
    it saw before the lock and let both through.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="capconc"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def debit(session):
        decision = await apply_session_debit(
            session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=None,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
        )
        await session.commit()
        return decision

    async with sessionmaker() as first, sessionmaker() as second:
        # First connection takes the row lock and holds it open.
        winner = await apply_session_debit(
            first,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=None,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
        )
        contender = asyncio.create_task(debit(second))
        await asyncio.sleep(0.2)
        assert not contender.done(), "the second debit did not block on the first one's row lock"
        await first.commit()
        loser = await contender

    assert [winner.allowed, loser.allowed] == [True, False]
    assert loser.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert loser.unit == CAP_UNIT_CALLS
    assert loser.requested == 1
    assert loser.remaining == 0


async def test_a_second_debit_within_budget_still_succeeds(db_session, seed_user, engine) -> None:
    """The contended case above must not pass because the debit refuses everything.

    Same two independent connections, same sequencing -- only the budget is wider. Without this,
    an implementation whose conditional update never matched would look like perfect concurrency
    control.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="capconcok"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=2,
        per_session_records=10,
        per_session_bytes=4096,
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def debit(session):
        decision = await apply_session_debit(
            session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=None,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
        )
        await session.commit()
        return decision

    async with sessionmaker() as first, sessionmaker() as second:
        assert (await debit(first)).allowed is True
        assert (await debit(second)).allowed is True


# ---------------------------------------------------------------------------
# WR-02a: the durable half fails CLOSED when its selector addresses no row.
# ---------------------------------------------------------------------------


async def _stranger(db_session):
    """A second tenant, committed, so a row can exist that this tenant's selector cannot reach.

    Created here rather than through a fixture because ``seed_user`` deletes exactly one tenant on
    the committed lane, and a second one left behind would outlive the test that made it.
    """
    import uuid as _uuid

    from app.models.user import User

    user = User(
        clerk_id=f"test_stranger_{_uuid.uuid4().hex[:12]}",
        email=f"stranger-{_uuid.uuid4().hex[:12]}@clawdi.local",
        name="Stranger",
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.commit()
    return user


async def _delete_user(db_session, user_id) -> None:
    from app.models.user import User

    await db_session.execute(User.__table__.delete().where(User.id == user_id))
    await db_session.commit()


async def _budget_row_of(db_session, *, user_id, companion_key="filebrowser", session_id=None):
    """``_budget_row`` above takes the tenant OBJECT; these cases need one by identifier.

    Separate rather than widened because the two-tenant case reads a row belonging to a user that
    no fixture owns, and a helper that took ``seed_user`` would not express that.

    ``session_id`` names WHICH of the two ledgers to read, and defaults to the WINDOW row -- the
    one keyed on the tenant, the Agent, the companion and the hour, which after GC-01 every debit
    spends. Before GC-01 a tenant had at most one row per companion and this helper could return
    "the" row; a session-carrying debit now leaves two, and a helper that still matched both would
    raise ``MultipleResultsFound`` at the exact moment the two-ledger shape is what is being
    asserted.
    """
    return (
        await db_session.execute(
            select(CompanionSessionBudget)
            .where(
                CompanionSessionBudget.user_id == user_id,
                CompanionSessionBudget.companion_key == companion_key,
                CompanionSessionBudget.session_id.is_(None)
                if session_id is None
                else CompanionSessionBudget.session_id == session_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def test_a_debit_whose_selector_addresses_no_row_refuses_rather_than_allowing(
    db_session, seed_user
) -> None:
    """A zero-row conditional UPDATE has TWO causes, and only one of them is an empty budget.

    The upsert's conflict target and the selector are not the same predicate. When a row exists
    that satisfies the conflict target but not the selector, the insert is a no-op, the update
    matches nothing, and the re-read comes back ``None`` -- the durable half could not address the
    row it needed. Coercing that ``None`` to ``0`` and handing it to the pure function returns
    ALLOWED with no counter incremented: a fail-open in the one gate whose purpose is to fail
    closed, and an unbounded session budget for as long as the condition holds (WR-02).
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="unresolved"
    )
    stranger = await _stranger(db_session)
    window = budget_window(datetime.now(UTC))
    try:
        # A row satisfying the WINDOW conflict target -- same environment, companion and hour --
        # but belonging to somebody else, so this tenant's selector cannot reach it.
        db_session.add(
            CompanionSessionBudget(
                user_id=stranger.id,
                environment_id=environment_id,
                companion_key="filebrowser",
                session_id=None,
                window_started_at=window,
                calls_consumed=0,
                records_consumed=0,
                bytes_consumed=0,
            )
        )
        await db_session.commit()

        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=None,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=TIGHT,
            window=window,
        )
        await db_session.commit()

        assert decision.allowed is False
        assert decision.reason == REASON_SESSION_BUDGET_UNRESOLVED
        assert decision.reason != REASON_WITHIN_CAP
        assert decision.reason != REASON_SESSION_BUDGET_EXHAUSTED
        assert decision.unit == CAP_UNIT_CALLS
        assert decision.requested == 1
        assert decision.remaining == 0

        # Nobody else's counter moved either: a refusal, not a debit against the wrong row.
        stranger_row = await _budget_row_of(db_session, user_id=stranger.id)
        assert stranger_row is not None
        assert stranger_row.calls_consumed == 0
        assert await _budget_row_of(db_session, user_id=seed_user.id) is None
    finally:
        await _delete_user(db_session, stranger.id)


async def test_a_genuinely_spent_budget_still_refuses_as_exhausted(db_session, seed_user) -> None:
    """The ordinary refusal is unchanged: an empty budget is still an empty budget.

    Without this the fix above could be a blanket refusal wearing a new reason token, and the two
    events the activity log has to distinguish would have collapsed the other way.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="spentstill"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )

    async def debit():
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=None,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
        )
        await db_session.commit()
        return decision

    assert (await debit()).allowed is True
    spent = await debit()

    assert spent.allowed is False
    assert spent.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert spent.remaining == 0


async def test_a_debit_inside_the_budget_is_allowed_and_increments_the_counter(
    db_session, seed_user
) -> None:
    """The allowed path is unchanged, and the counter is READ BACK rather than inferred.

    ``allowed=True`` with no increment is precisely the fail-open WR-02 found, so asserting the
    decision alone would leave the defect's signature undetected on the path it matters most.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="inbudget"
    )

    decision = await apply_session_debit(
        db_session,
        user_id=seed_user.id,
        environment_id=environment_id,
        companion_key="filebrowser",
        session_id=None,
        unit=CAP_UNIT_CALLS,
        requested=1,
        caps=TIGHT,
    )
    await db_session.commit()

    assert decision.allowed is True
    assert decision.reason == REASON_WITHIN_CAP
    row = await _budget_row_of(db_session, user_id=seed_user.id)
    assert row is not None
    assert row.calls_consumed == 1


async def test_one_request_larger_than_the_whole_budget_is_exhausted_not_unresolved(
    db_session, seed_user
) -> None:
    """The other zero-row cause: the row EXISTS and this one call does not fit in it.

    That path re-reads a real integer, so it must keep reaching the pure function and keep
    reporting the remainder a person needs in order to choose between raising the cap and
    splitting the task.
    """
    environment_id, _ = await deploy_companion_state(db_session, seed_user, machine_prefix="toobig")

    decision = await apply_session_debit(
        db_session,
        user_id=seed_user.id,
        environment_id=environment_id,
        companion_key="filebrowser",
        session_id=None,
        unit=CAP_UNIT_RECORDS,
        requested=TIGHT.per_session_records + 1,
        caps=TIGHT,
    )
    await db_session.commit()

    assert decision.allowed is False
    assert decision.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert decision.remaining == TIGHT.per_session_records


def test_the_cap_reason_vocabulary_carries_the_unresolved_token() -> None:
    """The vocabulary stays CLOSED, so a reason a test asserts is a reason the module declares.

    ``CAP_REASONS`` is what a caps UI, a dispatch row and a test correspond on. A fourth reason
    that existed only as a string literal at one call site would be assertable nowhere.
    """
    assert REASON_SESSION_BUDGET_UNRESOLVED in CAP_REASONS
    assert REASON_SESSION_BUDGET_UNRESOLVED == "session_budget_unresolved"
    assert len(CAP_REASONS) == 5


# ---------------------------------------------------------------------------
# WR-02b: two tenants, one opaque session id, two budgets.
# ---------------------------------------------------------------------------


async def test_two_tenants_presenting_one_session_id_hold_two_separate_budgets(
    db_session, seed_user
) -> None:
    """A session id is a string a CLIENT supplies. It cannot be allowed to address a shared row.

    ``uq_companion_session_budget`` shipped without a tenant column while ``_budget_selector``
    always required one, so the second tenant to present a given id upserted nothing it could then
    address -- and every debit of theirs took the unaddressable branch. Latent while ``session_id``
    was always NULL; live since 05-10 delivered a real one (T-05-11-03).

    Exhausting one tenant's budget entirely is the part that matters: it is what proves the two
    rows are independent rather than merely both present.

    **Each tenant gets its OWN Agent**, which this case originally did not do -- it handed the
    stranger ``seed_user``'s ``environment_id``. That shortcut was invisible while a
    session-carrying debit touched only the session row, and it is impossible in production:
    ``environment_id`` is
    control-plane issued and an Agent belongs to exactly one tenant, which is the entire reason
    ``uq_companion_window_budget`` carries no ``user_id`` column. Once GC-01 made every debit also
    spend the WINDOW row, the shared id made the stranger's window upsert collide with a row their
    own selector could not address, and the case reported ``session_budget_unresolved`` -- a correct
    answer to an impossible question. Two Agents is the faithful fixture, and the regression this
    case guards is untouched by it: ``uq_companion_session_budget`` names no environment, so
    removing its ``user_id`` column still collapses the two session budgets into one.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="twotenant"
    )
    stranger = await _stranger(db_session)
    stranger_environment_id, _ = await deploy_companion_state(
        db_session, stranger, machine_prefix="twotenantb"
    )
    shared_session = f"mcp-session-{uuid.uuid4().hex}"
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )

    async def debit(user_id, environment):
        decision = await apply_session_debit(
            db_session,
            user_id=user_id,
            environment_id=environment,
            companion_key="filebrowser",
            session_id=shared_session,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
        )
        await db_session.commit()
        return decision

    try:
        # Tenant A spends its whole budget, then hits the wall.
        assert (await debit(seed_user.id, environment_id)).allowed is True
        spent = await debit(seed_user.id, environment_id)
        assert spent.allowed is False
        assert spent.reason == REASON_SESSION_BUDGET_EXHAUSTED

        # Tenant B, presenting the identical id, is untouched by any of that.
        before = await _budget_row_of(db_session, user_id=stranger.id, session_id=shared_session)
        assert before is None
        allowed = await debit(stranger.id, stranger_environment_id)
        assert allowed.allowed is True
        assert allowed.reason == REASON_WITHIN_CAP

        # Two rows, one counter each. Neither tenant spent the other's.
        mine = await _budget_row_of(db_session, user_id=seed_user.id, session_id=shared_session)
        theirs = await _budget_row_of(db_session, user_id=stranger.id, session_id=shared_session)
        assert mine is not None and theirs is not None
        assert mine.id != theirs.id
        assert mine.session_id == theirs.session_id == shared_session
        assert mine.calls_consumed == 1
        assert theirs.calls_consumed == 1
    finally:
        await _delete_user(db_session, stranger.id)


def test_the_session_budget_index_names_the_tenant() -> None:
    """The declared index and the upsert's conflict target are ONE list, asserted as one.

    A conflict target naming columns that are not an index is a runtime error; a conflict target
    naming a NARROWER index than the selector reads with is silent, and is the shape this whole
    defect had. Pinning both here means they cannot drift apart again without a red test.
    """
    index = next(
        item
        for item in CompanionSessionBudget.__table__.indexes
        if item.name == "uq_companion_session_budget"
    )
    assert [column.name for column in index.columns] == [
        "user_id",
        "session_id",
        "companion_key",
    ]
    assert index.unique is True
    assert "session_id IS NOT NULL" in str(index.dialect_options["postgresql"]["where"])

    window = next(
        item
        for item in CompanionSessionBudget.__table__.indexes
        if item.name == "uq_companion_window_budget"
    )
    assert [column.name for column in window.columns] == [
        "environment_id",
        "companion_key",
        "window_started_at",
    ]

    source = inspect.getsource(apply_session_debit)
    assert 'index_elements=["user_id", "session_id", "companion_key"]' in source


# ---------------------------------------------------------------------------
# GC-01: two ledgers, and the one the agent cannot address is the one that always applies.
# ---------------------------------------------------------------------------


async def _session_rows_of(db_session, *, user_id, companion_key="filebrowser"):
    """Every SESSION-keyed budget row this tenant holds for one companion, oldest first."""
    return list(
        (
            await db_session.execute(
                select(CompanionSessionBudget)
                .where(
                    CompanionSessionBudget.user_id == user_id,
                    CompanionSessionBudget.companion_key == companion_key,
                    CompanionSessionBudget.session_id.is_not(None),
                )
                .order_by(CompanionSessionBudget.created_at, CompanionSessionBudget.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def test_three_invented_session_ids_cannot_outspend_one_window(db_session, seed_user) -> None:
    """The unit-level statement of GC-01: inventing keys buys nothing.

    ``per_session_calls`` of 2 and three distinct session id strings. Exactly two debits are
    allowed, because the ceiling that always applies is addressed by ``user_id``,
    ``environment_id``, ``companion_key`` and ``window_started_at`` -- and the agent supplies none
    of those four. Before this fix all three were allowed, and a fourth, and a thousandth.

    The window counter is read back rather than inferred, because ``allowed=False`` with the
    metering happening somewhere the agent CAN address is the shape the defect had.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc1units"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=2,
        per_session_records=10,
        per_session_bytes=4096,
    )
    window = budget_window(datetime.now(UTC))
    invented = [f"invented-{index}-{uuid.uuid4().hex}" for index in range(3)]

    async def debit(session_id: str):
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=session_id,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await db_session.commit()
        return decision

    decisions = [await debit(session_id) for session_id in invented]

    assert [decision.allowed for decision in decisions] == [True, True, False]
    assert decisions[2].reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert decisions[2].remaining == 0

    # The ceiling was metered where the agent cannot reach it.
    window_row = await _budget_row_of(db_session, user_id=seed_user.id)
    assert window_row is not None
    assert window_row.calls_consumed == 2

    # Per-session accounting SURVIVED rather than being deleted: one row per session that actually
    # spent, each carrying its own counter. The third id never reached the session ledger because
    # the window refused first, which is the short-circuit -- there is nothing to undo when nothing
    # downstream was written.
    session_rows = await _session_rows_of(db_session, user_id=seed_user.id)
    assert sorted(row.session_id for row in session_rows) == sorted(invented[:2])
    assert [row.calls_consumed for row in session_rows] == [1, 1]


async def test_a_long_conversation_is_refused_on_its_own_budget_in_a_fresh_window(
    db_session, seed_user
) -> None:
    """The two ceilings are INDEPENDENT, and this is the only reachable path to the compensation.

    One session id, ``per_session_calls`` of 1, two different windows. The second debit finds a
    completely empty window row and a completely spent session row, so the session gate is what
    refuses -- which is the whole reason the session row deliberately carries no window term. A
    conversation is bounded for its lifetime; the window is bounded per hour across all of them.

    The fresh window's counter must be back at 0. A refused call that left the window incremented
    would charge the tenant for a call that never happened, and would do it invisibly.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc1long"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )
    conversation = f"one-conversation-{uuid.uuid4().hex}"
    first_hour = budget_window(datetime.now(UTC))
    next_hour = first_hour + timedelta(seconds=SESSION_BUDGET_WINDOW_SECONDS)

    async def debit(window):
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=conversation,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await db_session.commit()
        return decision

    assert (await debit(first_hour)).allowed is True
    refused = await debit(next_hour)

    assert refused.allowed is False
    assert refused.reason == REASON_SESSION_BUDGET_EXHAUSTED

    fresh_window = (
        await db_session.execute(
            select(CompanionSessionBudget)
            .where(
                CompanionSessionBudget.user_id == seed_user.id,
                CompanionSessionBudget.session_id.is_(None),
                CompanionSessionBudget.window_started_at == next_hour,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh_window.calls_consumed == 0, "a refused call left the window counter moved"


async def test_an_unresolved_window_refuses_before_the_session_ledger_is_touched(
    db_session, seed_user
) -> None:
    """WR-02's fail-closed branch, now reached with a session id in hand.

    A row satisfying the WINDOW conflict target but belonging to somebody else makes this tenant's
    window upsert a no-op against a row its own selector cannot address. The refusal must still be
    ``session_budget_unresolved`` rather than ``within_cap``, and -- the new half -- the session
    ledger must not have been written at all, because the short-circuit returns before it.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc1unres"
    )
    stranger = await _stranger(db_session)
    window = budget_window(datetime.now(UTC))
    carried = f"carried-{uuid.uuid4().hex}"
    try:
        db_session.add(
            CompanionSessionBudget(
                user_id=stranger.id,
                environment_id=environment_id,
                companion_key="filebrowser",
                session_id=None,
                window_started_at=window,
                calls_consumed=0,
                records_consumed=0,
                bytes_consumed=0,
            )
        )
        await db_session.commit()

        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=carried,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=TIGHT,
            window=window,
        )
        await db_session.commit()

        assert decision.allowed is False
        assert decision.reason == REASON_SESSION_BUDGET_UNRESOLVED
        assert decision.reason != REASON_WITHIN_CAP
        assert decision.reason != REASON_SESSION_BUDGET_EXHAUSTED
        assert await _session_rows_of(db_session, user_id=seed_user.id) == []
    finally:
        await _delete_user(db_session, stranger.id)


async def test_the_unresolved_discrimination_survives_on_the_session_ledger_too(
    db_session, seed_user
) -> None:
    """Asserted at the ``_debit_ledger`` seam, and the reason for that is worth recording.

    ``apply_session_debit``'s session leg CANNOT be driven into this branch any more: since 05-11
    put the tenant into ``uq_companion_session_budget``, the session conflict target and the session
    selector name the identical three columns, so no row can satisfy one and fail the other. That
    is 05-11's fix working, not a gap -- but it means the only way to assert that the extraction
    preserved the discrimination on the session ledger's own code path is to hand ``_debit_ledger``
    a deliberately disagreeing pair and watch it refuse rather than allow.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc1seam"
    )
    stranger = await _stranger(db_session)
    shared = f"seam-{uuid.uuid4().hex}"
    window = budget_window(datetime.now(UTC))
    column = CompanionSessionBudget.calls_consumed
    try:
        db_session.add(
            CompanionSessionBudget(
                user_id=stranger.id,
                environment_id=environment_id,
                companion_key="filebrowser",
                session_id=shared,
                window_started_at=window,
                calls_consumed=0,
                records_consumed=0,
                bytes_consumed=0,
            )
        )
        await db_session.commit()

        # The conflict target omits the tenant -- the pre-05-11 shape -- while the selector requires
        # it. The insert is a no-op against the stranger's row and the update addresses nothing.
        decision = await _debit_ledger(
            db_session,
            values={
                "id": uuid.uuid4(),
                "user_id": seed_user.id,
                "environment_id": environment_id,
                "companion_key": "filebrowser",
                "session_id": shared,
                "window_started_at": window,
                "calls_consumed": 0,
                "records_consumed": 0,
                "bytes_consumed": 0,
            },
            index_elements=["user_id", "session_id", "companion_key"],
            index_where="session_id IS NOT NULL",
            selector=[
                CompanionSessionBudget.user_id == seed_user.id,
                CompanionSessionBudget.companion_key == "filebrowser",
                CompanionSessionBudget.session_id == shared,
                # The disagreement: nothing of this tenant's carries the stranger's environment.
                CompanionSessionBudget.environment_id == uuid.uuid4(),
            ],
            column=column,
            requested=1,
            budget=TIGHT.per_session_calls,
            unit=CAP_UNIT_CALLS,
            caps=TIGHT,
        )
        await db_session.commit()

        assert decision.allowed is False
        assert decision.reason == REASON_SESSION_BUDGET_UNRESOLVED
        assert decision.remaining == 0
    finally:
        await _delete_user(db_session, stranger.id)


async def test_a_bytes_refund_comes_off_both_ledgers(db_session, seed_user) -> None:
    """A reservation taken from two rows has to be given back to two rows.

    ``bytes`` reserves the whole per-call ceiling because a response's size is unknowable before it
    arrives, and the settlement refunds the unused part. Refunding only the window would make a
    one-byte read cost a whole per-call ceiling against the SESSION budget -- the exact failure the
    release exists to prevent, reintroduced on the ledger nobody was looking at.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc1bytes"
    )
    conversation = f"refund-{uuid.uuid4().hex}"
    window = budget_window(datetime.now(UTC))
    reserved = TIGHT.per_call_bytes
    refund = reserved - 1

    allowed = await apply_session_debit(
        db_session,
        user_id=seed_user.id,
        environment_id=environment_id,
        companion_key="filebrowser",
        session_id=conversation,
        unit=CAP_UNIT_BYTES,
        requested=reserved,
        caps=TIGHT,
        window=window,
    )
    await db_session.commit()
    assert allowed.allowed is True

    await release_session_reservation(
        db_session,
        user_id=seed_user.id,
        environment_id=environment_id,
        companion_key="filebrowser",
        session_id=conversation,
        unit=CAP_UNIT_BYTES,
        refund=refund,
        window=window,
    )

    window_row = await _budget_row_of(db_session, user_id=seed_user.id)
    session_row = await _budget_row_of(db_session, user_id=seed_user.id, session_id=conversation)
    assert window_row is not None and session_row is not None
    assert window_row.bytes_consumed == reserved - refund
    assert session_row.bytes_consumed == reserved - refund


def test_the_window_budget_index_and_the_null_selector_name_one_column_list() -> None:
    """The sibling of ``test_the_session_budget_index_names_the_tenant``, for the other ledger.

    The session pair drifted apart once and it cost a fail-open (WR-02). The window pair is now
    load-bearing for every single call, so pinning its declared index, its conflict target and the
    selector that reads it as ONE list is what keeps the same drift from happening twice.
    """
    window = next(
        item
        for item in CompanionSessionBudget.__table__.indexes
        if item.name == "uq_companion_window_budget"
    )
    declared = [column.name for column in window.columns]
    assert declared == ["environment_id", "companion_key", "window_started_at"]
    assert window.unique is True
    assert "session_id IS NULL" in str(window.dialect_options["postgresql"]["where"])

    debit_source = inspect.getsource(apply_session_debit)
    assert 'index_elements=["environment_id", "companion_key", "window_started_at"]' in debit_source
    assert 'index_where="session_id IS NULL"' in debit_source

    selector_source = inspect.getsource(_budget_selector)
    null_branch = selector_source.partition("if session_id is None:")[2].partition("else:")[0]
    assert "CompanionSessionBudget.session_id.is_(None)" in null_branch
    for name in declared:
        # `companion_key` is in the shared base terms above the branch, so the whole function is
        # what has to carry all three -- the branch alone carries the two that discriminate.
        assert f"CompanionSessionBudget.{name} ==" in selector_source


# ---------------------------------------------------------------------------
# GC2-03: the session ledger is bounded in TIME as well as in volume.
# ---------------------------------------------------------------------------


async def _seed_session_row(
    db_session,
    *,
    user_id,
    environment_id,
    session_id: str,
    window_started_at,
    calls_consumed: int,
    companion_key: str = "filebrowser",
) -> None:
    """One SESSION-keyed budget row, planted at an explicit age and an explicit spend.

    The cases below need a row that is already spent AND already old, and no sequence of debits can
    produce one inside a test run: the lifetime is four hours. Planting the row is the only way to
    put the clock where the assertion needs it without making the suite wait, and it is also the
    honest shape -- in production these rows are found aged, never aged in place.
    """
    db_session.add(
        CompanionSessionBudget(
            user_id=user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            session_id=session_id,
            window_started_at=window_started_at,
            calls_consumed=calls_consumed,
            records_consumed=0,
            bytes_consumed=0,
        )
    )
    await db_session.commit()


async def test_a_session_row_older_than_its_lifetime_is_reset_rather_than_refused(
    db_session, seed_user
) -> None:
    """GC2-03 at the unit seam: an aged budget is STALE, not spent.

    One session id, ``per_session_calls`` of 1, two debits a lifetime and a window apart. Before
    this fix the second was refused ``session_budget_exhausted`` and every call after it was too,
    for the life of the proxy process -- a cap with no in-product remedy, which is the failure mode
    a blast-radius cap exists to avoid rather than to cause.

    The row is read back because "allowed" alone would also be true of an implementation that
    created a SECOND row per session, which would be GC-01 reopened on the ledger that closed it.
    One row, reset in place, and its stamp moved to the later window.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc203reset"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )
    conversation = f"aged-{uuid.uuid4().hex}"
    opened = budget_window(datetime.now(UTC))
    beyond = opened + timedelta(
        seconds=SESSION_BUDGET_LIFETIME_SECONDS + SESSION_BUDGET_WINDOW_SECONDS
    )

    async def debit(window):
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=conversation,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await db_session.commit()
        return decision

    assert (await debit(opened)).allowed is True
    revived = await debit(beyond)

    assert revived.allowed is True, "a conversation older than its lifetime is still walled"
    assert revived.reason == REASON_WITHIN_CAP

    rows = await _session_rows_of(db_session, user_id=seed_user.id)
    assert [row.session_id for row in rows] == [conversation], "the reset created a second row"
    assert rows[0].calls_consumed == 1, (
        "the reset incremented the old counter instead of zeroing it"
    )
    assert rows[0].window_started_at == beyond


async def test_a_session_row_exactly_at_its_lifetime_is_spent_rather_than_stale(
    db_session, seed_user
) -> None:
    """The other side of the same boundary -- this file asserts both, always.

    The staleness comparison is STRICT: a row is stale when its ``window_started_at`` is earlier
    than ``moment - SESSION_BUDGET_LIFETIME_SECONDS``, not when it is at or earlier. A second debit
    at exactly one lifetime therefore still meets the ceiling. An implementation refusing at the
    lifetime and one resetting at the lifetime both pass a test that only asserts the far side.

    This case is NOT a guard against retuning the lifetime. Its second debit is computed FROM the
    constant, so the moment travels with it and the case stays green at any value -- exactly as
    ``test_a_long_conversation_is_refused_on_its_own_budget_in_a_fresh_window`` does. Nothing in
    this suite fails if the lifetime is collapsed to one window; WINDOWS #197 carries that alone.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc203edge"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )
    conversation = f"edge-{uuid.uuid4().hex}"
    opened = budget_window(datetime.now(UTC))
    at_the_boundary = opened + timedelta(seconds=SESSION_BUDGET_LIFETIME_SECONDS)

    async def debit(window):
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=conversation,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await db_session.commit()
        return decision

    assert (await debit(opened)).allowed is True
    refused = await debit(at_the_boundary)

    assert refused.allowed is False
    assert refused.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert refused.remaining == 0

    rows = await _session_rows_of(db_session, user_id=seed_user.id)
    assert [row.calls_consumed for row in rows] == [1]
    assert rows[0].window_started_at == opened, "a row at the boundary was reset"

    # The refused call must leave the fresh window untouched, exactly as the pre-existing
    # long-conversation regression requires. The compensation is the only thing that makes that so.
    fresh_window = (
        await db_session.execute(
            select(CompanionSessionBudget)
            .where(
                CompanionSessionBudget.user_id == seed_user.id,
                CompanionSessionBudget.session_id.is_(None),
                CompanionSessionBudget.window_started_at == at_the_boundary,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert fresh_window.calls_consumed == 0, "a refused call left the window counter moved"


async def test_rotating_ids_over_stale_rows_still_cannot_outspend_one_window(
    db_session, seed_user
) -> None:
    """GC-01 is not weakened by the reset, asserted where it would break if it were.

    ``per_session_calls`` of 2, three distinct ids, and every one of them already holding a spent
    row old enough to reset. If a reset handed back budget the WINDOW ledger had not authorised,
    all three debits would be allowed. The window ledger is untouched by this plan -- same key,
    same ceiling, same conflict target, same ``ON CONFLICT DO NOTHING``, still debited first and
    still short-circuiting -- so exactly two are.

    The third id's own row is read back as well: the window refused before the session ledger was
    reached, so that row must still carry its stale stamp and its old counter. A reset that had
    run anyway would be a write on a refused call.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc203rot"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=2,
        per_session_records=10,
        per_session_bytes=4096,
    )
    window = budget_window(datetime.now(UTC))
    stale_stamp = window - timedelta(
        seconds=SESSION_BUDGET_LIFETIME_SECONDS + SESSION_BUDGET_WINDOW_SECONDS
    )
    rotated = [f"rotated-{index}-{uuid.uuid4().hex}" for index in range(3)]
    for session_id in rotated:
        await _seed_session_row(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            session_id=session_id,
            window_started_at=stale_stamp,
            calls_consumed=caps.per_session_calls,
        )

    async def debit(session_id: str):
        decision = await apply_session_debit(
            db_session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=session_id,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await db_session.commit()
        return decision

    decisions = [await debit(session_id) for session_id in rotated]

    assert [decision.allowed for decision in decisions] == [True, True, False]
    assert decisions[2].reason == REASON_SESSION_BUDGET_EXHAUSTED

    window_row = await _budget_row_of(db_session, user_id=seed_user.id)
    assert window_row is not None
    assert window_row.calls_consumed == 2, "a stale-row reset spent budget the window never granted"

    by_id = {
        row.session_id: row for row in await _session_rows_of(db_session, user_id=seed_user.id)
    }
    assert sorted(by_id) == sorted(rotated)
    for session_id in rotated[:2]:
        assert by_id[session_id].calls_consumed == 1
        assert by_id[session_id].window_started_at == window
    assert by_id[rotated[2]].calls_consumed == caps.per_session_calls
    assert by_id[rotated[2]].window_started_at == stale_stamp, "a refused call still reset a row"


async def test_two_concurrent_debits_against_one_stale_row_reset_it_once(
    db_session, seed_user, engine
) -> None:
    """The reset lands ONCE under contention, on two independent connections.

    Under ``READ COMMITTED`` a second caller conflicting on a row the first has already updated
    blocks on that row's lock, and PostgreSQL re-evaluates the ``DO UPDATE`` predicate against the
    LATEST version of the row once the lock is released. The first caller's reset refreshed
    ``window_started_at``, so the second caller's staleness test is false and no second reset lands.
    That paragraph is the design argument; this case is the evidence for it.

    The two callers pass DIFFERENT windows -- an hour apart -- so each has its own fresh window row
    with room for one and the single stale SESSION row is the only contended resource. That is also
    a real shape: one conversation whose two calls straddle an hour boundary.

    Sequenced rather than raced, following this file's existing idiom: the second debit is started
    while the first holds the row lock and is released only by the first commit.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="gc203conc"
    )
    caps = CapVector(
        per_call_calls=1,
        per_call_records=50,
        per_call_bytes=1024,
        per_session_calls=1,
        per_session_records=10,
        per_session_bytes=4096,
    )
    conversation = f"contended-{uuid.uuid4().hex}"
    first_hour = budget_window(datetime.now(UTC))
    next_hour = first_hour + timedelta(seconds=SESSION_BUDGET_WINDOW_SECONDS)
    await _seed_session_row(
        db_session,
        user_id=seed_user.id,
        environment_id=environment_id,
        session_id=conversation,
        window_started_at=first_hour
        - timedelta(seconds=SESSION_BUDGET_LIFETIME_SECONDS + SESSION_BUDGET_WINDOW_SECONDS),
        calls_consumed=caps.per_session_calls,
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def debit(session, window):
        decision = await apply_session_debit(
            session,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=conversation,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        await session.commit()
        return decision

    async with sessionmaker() as first, sessionmaker() as second:
        winner = await apply_session_debit(
            first,
            user_id=seed_user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=conversation,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=first_hour,
        )
        contender = asyncio.create_task(debit(second, next_hour))
        await asyncio.sleep(0.2)
        assert not contender.done(), "the second debit did not block on the stale row's lock"
        await first.commit()
        loser = await contender

    assert [winner.allowed, loser.allowed] == [True, False], "the stale row was reset twice"
    assert loser.reason == REASON_SESSION_BUDGET_EXHAUSTED
    assert loser.remaining == 0

    rows = await _session_rows_of(db_session, user_id=seed_user.id)
    assert [row.calls_consumed for row in rows] == [1]
    assert rows[0].window_started_at == first_hour


def test_the_session_selector_carries_no_time_term_and_the_reset_rides_the_upsert() -> None:
    """Where the time bound lives is the design, and this is the pin on it.

    ``05-REVIEW.md``'s proposed fix put a freshness predicate in ``_budget_selector``'s session
    branch. That shape is REJECTED and must stay rejected, for two independent reasons:

    * The session upsert's conflict target is ``uq_companion_session_budget`` over
      ``(user_id, session_id, companion_key)`` -- no time term. A stale row therefore still
      collides, ``ON CONFLICT`` leaves it where it is, a selector carrying ``window_started_at >=
      cutoff`` then addresses nothing, the conditional UPDATE matches no row and ``_debit_ledger``'s
      ``consumed is None`` branch fires. The long legitimate conversation is refused
      ``session_budget_unresolved`` instead of ``session_budget_exhausted``: the same wall, now
      reported under the reason word that means "this gate is broken". That is the exact
      conflict-target/selector mismatch class that produced WR-02.
    * ``_budget_selector`` is shared with ``release_session_reservation``. A freshness term there
      would silently stop a ``bytes`` refund from reaching a row that aged between the debit and
      the settlement.

    So the bound lives in the UPSERT. Asserted against the extracted ``else:`` branch rather than
    against the whole function, which is the shape GC2-08 says the sibling window pin should have
    had; and against ``apply_session_debit``'s two call sites, because the reset argument riding
    the WINDOW ledger would be code that can never run -- that row's key already contains the hour.
    """
    selector_source = inspect.getsource(_budget_selector)
    session_branch = selector_source.partition("else:")[2]
    assert "CompanionSessionBudget.session_id ==" in session_branch
    for forbidden in ("window_started_at", "session_budget_cutoff", "reset_if_opened_before"):
        assert forbidden not in session_branch, (
            f"the session selector grew a {forbidden!r} term -- see this test's docstring"
        )

    # Everything after the docstring's closing quotes: the prose above is allowed to NAME the
    # parameter, so a gate that searched the whole source could be satisfied by a sentence.
    body = inspect.getsource(apply_session_debit).split('"""')[2]
    window_half, _, session_half = body.partition("session_selector = _budget_selector(")
    assert session_half, "apply_session_debit no longer builds a session selector"
    assert "reset_if_opened_before" not in window_half, "the WINDOW ledger was given a reset"
    assert "reset_if_opened_before=session_budget_cutoff(moment)" in session_half

    moment = budget_window(datetime.now(UTC))
    assert session_budget_cutoff(moment) == moment - timedelta(
        seconds=SESSION_BUDGET_LIFETIME_SECONDS
    )
