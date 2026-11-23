"""The provenance chain: one honest row per attempt, refusals included (TOOL-07).

``05-01`` established that every tool-call attempt writes exactly one DISPATCH record, including
attempts refused before a companion is reached. This module holds the same claim one level up, for
the row a tenant owner actually reads: the activity log has to answer **why not**, not only what
happened. A refusal that leaves no row is precisely the attempt somebody opens the log to
understand, so the first group here drives every terminal branch of ``call_companion_tool`` and
asserts a row count of exactly one each time.

**Why the branch list is spelled out in this file rather than discovered.** ``TERMINAL_BRANCHES``
is a literal thirteen-item table written in the test body, and
``test_the_branch_list_covers_the_decision_vocabulary`` asserts it is exactly the decision
vocabulary -- same length, same set. A branch added to the façade later with no provenance write
does not silently pass: closing it needs a fourteenth decision value, which makes the two
collections disagree, or it needs one of the thirteen, which means the new branch is already covered
by a case here. A list sourced from the module under test would assert "the branches I was shown are
the branches that write", which is a tautology rather than a gate -- the T-4-19 shape
``05-RESEARCH.md`` §Pitfall 4 records.

**Each branch case is SETUP then TRIGGER, with the table emptied between them.** Several branches
are only reachable after an earlier attempt has already happened -- a denial needs an approval to
deny -- and those earlier attempts write their own honest rows. Counting from an empty table at the
moment of the trigger is what makes "exactly one row per attempt" a precise claim instead of an
arithmetic one.

**Tenant isolation is asserted in both halves.** A cross-tenant test that only asserted the absence
of foreign rows passes when the query is broken and returns nothing at all, which is the one
outcome that would make the assertion worthless. ``test_tenant_isolation`` therefore also asserts
the caller's own row count is non-zero, and separately asserts the route ACCEPTS no tenant-shaped
parameter -- the strongest available form of "no filter can widen the scope", because a parameter
that does not exist cannot be got wrong.

The module runs on the committed lane: two of the thirteen branches use an executor stand-in writing
a result on an INDEPENDENT connection while the façade polls, which is invisible to any test whose
writes never leave one transaction.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid as uuid_module
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.models.companion_approval import STATUS_DENIED, CompanionApproval
from app.models.companion_budget import CompanionSpendCeiling
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_session_budget import CompanionCapPolicy
from app.models.companion_tool_call import (
    CALL_DECISIONS,
    CallDecision,
    CompanionToolCall,
    decision_check_expression,
)
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.services.audit import SECRETISH_KEY_PARTS
from app.services.companion_dryrun import reset_preview_ledger
from app.services.companion_facade import call_companion_tool, reset_companion_tools_cache
from app.services.companion_provenance import (
    CONTENT_REF_TTL_SECONDS,
    SOURCE_CONTENT_REF_DISCLOSURE,
    prompting_content_ref,
    record_companion_tool_call,
    record_returned_content,
    reset_content_ledger,
)
from app.services.companion_ratelimit import (
    COMPANION_RATE_CAPACITY,
    REPEATED_CALL_THRESHOLD,
    acquire_companion_slot,
    reset_companion_rate_limiter,
)
from app.services.companion_sanitize import UNTRUSTED_OPEN_PREFIX
from tests.companion_stub import COMPANION_STUB_LISTING
from tests.hosted_runtime_fixtures import deploy_companion_state

pytestmark = pytest.mark.committed_db

READ_TOOL = "filebrowser__list_dir"
DELETE_TOOL = "filebrowser__delete_path"
UNREGISTERED_TOOL = "filebrowser__shred_everything"
DOOMED_PATH = "/projects/quarterly-report.md"

ACTIVITY_PATH = "/v1/companions/activity"

# The listing the executor stand-in reports back. Serialized from the SHARED double's own
# constant rather than hand-written here: a preview of a directory the probe described as
# empty is an ERROR rather than a diff, so a second, thinner listing invented in this file
# would quietly turn the `dry_run_completed` case into the `refused_policy` one.
STUB_BODY = json.dumps(COMPANION_STUB_LISTING)


@pytest.fixture(autouse=True)
def _clear_companion_process_state():
    """Every per-process ledger this phase owns, emptied around each case.

    The rate-limit bucket in particular: a branch that drains it deliberately must not leave the
    next case's first call sitting behind a refill it never asked for.
    """
    reset_companion_tools_cache()
    reset_preview_ledger()
    reset_companion_rate_limiter()
    reset_content_ledger()
    yield
    reset_companion_tools_cache()
    reset_preview_ledger()
    reset_companion_rate_limiter()
    reset_content_ledger()


# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


async def _enable_destroy(db_session, user) -> None:
    db_session.add(
        CompanionDestroyEnablement(
            user_id=user.id,
            companion_key="filebrowser",
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=user.id,
        )
    )
    await db_session.commit()


async def _policy(db_session, user, verb: str, mode: str) -> None:
    db_session.add(
        CompanionToolPolicy(user_id=user.id, companion_key="filebrowser", verb=verb, mode=mode)
    )
    await db_session.commit()


async def _cap_policy(db_session, user, **overrides) -> CompanionCapPolicy:
    values: dict[str, int] = {
        "per_call_calls": 20,
        "per_call_records": 5_000,
        "per_call_bytes": 4096,
        "per_session_calls": 50,
        "per_session_records": 50_000,
        "per_session_bytes": 40_960,
    }
    values.update(overrides)
    row = CompanionCapPolicy(user_id=user.id, companion_key="filebrowser", **values)
    db_session.add(row)
    await db_session.commit()
    return row


async def _provenance_rows(db, user_id) -> list[CompanionToolCall]:
    return list(
        (
            await db.execute(
                select(CompanionToolCall)
                .where(CompanionToolCall.user_id == user_id)
                .order_by(CompanionToolCall.created_at, CompanionToolCall.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _clear_provenance(db, user_id) -> None:
    await db.execute(
        CompanionToolCall.__table__.delete().where(CompanionToolCall.user_id == user_id)
    )
    await db.commit()


async def _complete_next_call(sessionmaker, *, environment_id, body: str, attempts: int = 600):
    """An executor stand-in reporting one result on an INDEPENDENT connection.

    Deliberately the thin form ``test_companion_dry_run.py`` uses rather than the HTTP stand-in
    from ``test_companion_tracer.py``: what these two branch cases need is a call that REACHES a
    terminal outcome, not the dispatch channel exercised a third time.
    """
    for _attempt in range(attempts):
        async with sessionmaker() as db:
            row = (
                (
                    await db.execute(
                        select(CompanionDispatchCall)
                        .where(
                            CompanionDispatchCall.environment_id == environment_id,
                            CompanionDispatchCall.status == "queued",
                        )
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                row.status = "completed"
                row.result = {"status": 200, "body": body}
                db.add(row)
                await db.commit()
                return
        await asyncio.sleep(0.01)
    raise AssertionError("no queued dispatch call appeared for the executor stand-in")


# ---------------------------------------------------------------------------
# The thirteen terminal branches, spelled out.
# ---------------------------------------------------------------------------


@dataclass
class BranchContext:
    """Everything a branch case needs, and the state it carries from setup to trigger.

    ``user_id`` is captured as a plain value rather than read off ``user`` when it is needed.
    ``call_companion_tool`` commits and rolls back the caller's session as part of its own
    polling, which EXPIRES every instance attached to it -- so an attribute read after a call
    lazy-loads outside the greenlet and fails with a ``MissingGreenlet`` that reads as a product
    defect and is not. ``AuthContext`` captures ``_user_id`` at construction for the same reason,
    and ``deploy_companion_state`` returns an identifier rather than the ORM object.
    """

    db: Any
    user: Any
    user_id: Any
    auth: AuthContext
    engine: Any
    environment_id: Any = None
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def sessionmaker(self):
        return async_sessionmaker(self.engine, expire_on_commit=False)


@dataclass(frozen=True)
class TerminalBranch:
    """One terminal branch of ``call_companion_tool`` and the decision it must record."""

    name: str
    decision: CallDecision
    setup: Callable[[BranchContext], Awaitable[None]]
    trigger: Callable[[BranchContext], Awaitable[None]]


def _prefix() -> str:
    return f"prov{uuid_module.uuid4().hex[:6]}"


async def _no_setup(ctx: BranchContext) -> None:
    await deploy_companion_state(ctx.db, ctx.user, machine_prefix=_prefix())


async def _deploy_and_open_every_earlier_gate(ctx: BranchContext) -> None:
    """A tenant whose only remaining gate for ``delete_path`` is the one under test."""
    environment_id, _ = await deploy_companion_state(ctx.db, ctx.user, machine_prefix=_prefix())
    ctx.environment_id = environment_id
    await _enable_destroy(ctx.db, ctx.user)
    await _cap_policy(ctx.db, ctx.user)


async def _setup_unknown_tool(ctx: BranchContext) -> None:
    await _no_setup(ctx)


async def _trigger_unknown_tool(ctx: BranchContext) -> None:
    result = await call_companion_tool(
        UNREGISTERED_TOOL, {"path": DOOMED_PATH}, auth=ctx.auth, db=ctx.db, deadline_seconds=0
    )
    assert result["isError"] is True


async def _setup_destroy_disabled(ctx: BranchContext) -> None:
    await _no_setup(ctx)


async def _trigger_destroy_disabled(ctx: BranchContext) -> None:
    result = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=ctx.auth, db=ctx.db, deadline_seconds=0
    )
    assert result["isError"] is True


async def _setup_policy_deny(ctx: BranchContext) -> None:
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "deny")


async def _trigger_policy_deny(ctx: BranchContext) -> None:
    result = await call_companion_tool(
        READ_TOOL, {"path": "/"}, auth=ctx.auth, db=ctx.db, deadline_seconds=0
    )
    assert result["isError"] is True


async def _trigger_ask(ctx: BranchContext) -> None:
    result = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=ctx.auth, db=ctx.db, deadline_seconds=0
    )
    assert result["isError"] is True


async def _setup_approval_refused(ctx: BranchContext) -> None:
    """Ask once so a row exists, then DENY it. The trigger asks again and is refused."""
    await _deploy_and_open_every_earlier_gate(ctx)
    await _trigger_ask(ctx)
    await ctx.db.execute(
        update(CompanionApproval)
        .where(CompanionApproval.user_id == ctx.user_id)
        .values(status=STATUS_DENIED, decided_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    await ctx.db.commit()


async def _setup_cap_refused(ctx: BranchContext) -> None:
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    # Zero is a VALUE, not an absence: a per-call ceiling of zero refuses everything.
    await _cap_policy(ctx.db, ctx.user, per_call_calls=0)


async def _trigger_read(ctx: BranchContext) -> None:
    result = await call_companion_tool(
        READ_TOOL, {"path": "/"}, auth=ctx.auth, db=ctx.db, deadline_seconds=0
    )
    assert result["isError"] is True


async def _setup_spend_ceiling_reached(ctx: BranchContext) -> None:
    """A tenant whose monthly money ceiling is zero: nothing spent is already at the ceiling.

    Zero is a VALUE here exactly as it is for the Phase 5 caps, so no usage rows are needed to
    reach it. The policy row is `allow` so the refusal cannot be the policy gate's.
    """
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    ctx.db.add(CompanionSpendCeiling(user_id=ctx.user_id, ceiling_usd_micros=0))
    await ctx.db.commit()


async def _setup_group_disabled(ctx: BranchContext) -> None:
    """A tenant that blocked filebrowser's whole tool group (plan 06-24).

    The policy row is `allow` so the refusal cannot be the policy gate's, and the block row is
    inserted directly: this table proves the provenance write, and the route that writes the row
    is `test_companion_group_block.py`'s to prove.
    """
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    ctx.db.add(
        CompanionGroupBlock(
            user_id=ctx.user_id,
            companion_key="filebrowser",
            blocked_at=datetime.now(UTC),
            blocked_by_user_id=ctx.user_id,
        )
    )
    await ctx.db.commit()


async def _setup_repeated_call(ctx: BranchContext) -> None:
    """The same read, repeated up to the breaker's threshold, so the trigger is one too many.

    A zero per-call cap refuses each setup call AFTER the breaker has counted it, so nothing is
    minted during setup; the trigger is then stopped by the breaker before it reaches that cap,
    which also proves the breaker sits ahead of the caps.
    """
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    await _cap_policy(ctx.db, ctx.user, per_call_calls=0)
    for _ in range(REPEATED_CALL_THRESHOLD):
        await _trigger_read(ctx)


async def _setup_rate_limited(ctx: BranchContext) -> None:
    await _no_setup(ctx)
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    await _cap_policy(ctx.db, ctx.user)
    for _ in range(COMPANION_RATE_CAPACITY):
        acquire_companion_slot(user_id=ctx.user_id, companion_key="filebrowser")


async def _trigger_rate_limited(ctx: BranchContext) -> None:
    async def _no_wait(_seconds: float) -> None:
        """Skip the real refill so the second acquisition is still empty."""
        return None

    result = await call_companion_tool(
        READ_TOOL,
        {"path": "/"},
        auth=ctx.auth,
        db=ctx.db,
        deadline_seconds=0,
        sleep=_no_wait,
    )
    assert result["isError"] is True


async def _setup_runtime_unreachable(ctx: BranchContext) -> None:
    """Deliberately NO deployment: no runtime of this tenant's has the companion.

    This is also the case that proves the provenance row is not merely the dispatch row renamed.
    ``record_refusal`` returns ``None`` here because ``companion_dispatch_calls.environment_id`` is
    NOT NULL and there is no environment to name -- so the activity log would have nothing at all
    to show for the attempt if it were sourced from that table.
    """
    return None


async def _setup_dry_run(ctx: BranchContext) -> None:
    await _deploy_and_open_every_earlier_gate(ctx)
    await _policy(ctx.db, ctx.user, "delete_path", "allow")


async def _trigger_dry_run(ctx: BranchContext) -> None:
    result, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": "/", "dry_run": True},
            auth=ctx.auth,
            db=ctx.db,
            deadline_seconds=5,
        ),
        _complete_next_call(ctx.sessionmaker, environment_id=ctx.environment_id, body=STUB_BODY),
    )
    assert result.get("isError") is not True, result


async def _setup_executed(ctx: BranchContext) -> None:
    environment_id, _ = await deploy_companion_state(ctx.db, ctx.user, machine_prefix=_prefix())
    ctx.environment_id = environment_id
    await _policy(ctx.db, ctx.user, "list_dir", "allow")
    await _cap_policy(ctx.db, ctx.user)


async def _trigger_executed(ctx: BranchContext) -> None:
    result, _ = await asyncio.gather(
        call_companion_tool(READ_TOOL, {"path": "/"}, auth=ctx.auth, db=ctx.db, deadline_seconds=5),
        _complete_next_call(ctx.sessionmaker, environment_id=ctx.environment_id, body=STUB_BODY),
    )
    assert result.get("isError") is not True, result


# The thirteen. Written here, once, as literals -- never read back out of the façade.
TERMINAL_BRANCHES: tuple[TerminalBranch, ...] = (
    TerminalBranch(
        "unknown_tool",
        CallDecision.REFUSED_UNKNOWN_TOOL,
        _setup_unknown_tool,
        _trigger_unknown_tool,
    ),
    TerminalBranch(
        "destroy_disabled",
        CallDecision.REFUSED_DESTROY_DISABLED,
        _setup_destroy_disabled,
        _trigger_destroy_disabled,
    ),
    TerminalBranch(
        "policy_deny", CallDecision.REFUSED_POLICY, _setup_policy_deny, _trigger_policy_deny
    ),
    TerminalBranch(
        "approval_required",
        CallDecision.PENDING_APPROVAL,
        _deploy_and_open_every_earlier_gate,
        _trigger_ask,
    ),
    TerminalBranch(
        "approval_refused", CallDecision.REFUSED_APPROVAL, _setup_approval_refused, _trigger_ask
    ),
    TerminalBranch("cap_refused", CallDecision.REFUSED_CAP, _setup_cap_refused, _trigger_read),
    TerminalBranch(
        "spend_ceiling_reached",
        CallDecision.REFUSED_BUDGET_CEILING,
        _setup_spend_ceiling_reached,
        _trigger_read,
    ),
    TerminalBranch(
        "repeated_call",
        CallDecision.REFUSED_REPEATED_CALL,
        _setup_repeated_call,
        _trigger_read,
    ),
    TerminalBranch(
        "group_disabled",
        CallDecision.REFUSED_GROUP_DISABLED,
        _setup_group_disabled,
        _trigger_read,
    ),
    TerminalBranch(
        "rate_limited", CallDecision.REFUSED_RATELIMIT, _setup_rate_limited, _trigger_rate_limited
    ),
    TerminalBranch(
        "runtime_unreachable",
        CallDecision.RUNTIME_UNREACHABLE,
        _setup_runtime_unreachable,
        _trigger_read,
    ),
    TerminalBranch(
        "dry_run_completed", CallDecision.DRY_RUN_COMPLETED, _setup_dry_run, _trigger_dry_run
    ),
    TerminalBranch("call_executed", CallDecision.ALLOWED, _setup_executed, _trigger_executed),
)


def test_the_branch_list_covers_the_decision_vocabulary() -> None:
    """The literal branch table and the decision enum are the same size and the same set.

    Both halves. Length alone would pass for a table that listed one branch twice and omitted
    another, which is exactly the shape a careless edit produces.
    """
    assert len(TERMINAL_BRANCHES) == len(CallDecision)
    assert {branch.decision for branch in TERMINAL_BRANCHES} == set(CallDecision)
    assert len({branch.name for branch in TERMINAL_BRANCHES}) == len(TERMINAL_BRANCHES)


@pytest.mark.parametrize(
    "branch", TERMINAL_BRANCHES, ids=[branch.name for branch in TERMINAL_BRANCHES]
)
async def test_every_terminal_branch_writes_exactly_one_row(
    branch: TerminalBranch, db_session, seed_user, engine
) -> None:
    """One attempt, one row, carrying the decision that ended it -- refusals included."""
    user_id = seed_user.id
    ctx = BranchContext(
        db=db_session,
        user=seed_user,
        user_id=user_id,
        auth=AuthContext(user=seed_user),
        engine=engine,
    )
    await branch.setup(ctx)
    await _clear_provenance(db_session, user_id)

    await branch.trigger(ctx)

    rows = await _provenance_rows(db_session, user_id)
    assert len(rows) == 1, [row.decision for row in rows]
    assert rows[0].decision == branch.decision
    assert rows[0].user_id == user_id
    # A row that recorded nothing about WHY is a row of the wrong thing. Every refusal carries the
    # façade's own reason text; the two non-refusals do not have to.
    if branch.decision not in {CallDecision.ALLOWED, CallDecision.DRY_RUN_COMPLETED}:
        assert rows[0].error_text, branch.name


async def test_the_check_constraint_and_the_enum_cannot_disagree(db_session) -> None:
    """``CallDecision`` is the SINGLE vocabulary source, asserted against the live constraint.

    Read from the database rather than from the model's Python, because the constraint that
    actually refuses a bad value is the one PostgreSQL is holding -- a model that regenerated its
    ``CheckConstraint`` from the enum while the migration shipped a stale literal would pass any
    assertion made against the model alone.
    """
    # `pg_get_constraintdef` renders the CHECK exactly as the server holds it.
    definition = await db_session.scalar(
        text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_companion_tool_calls_decision'"
        )
    )
    assert definition is not None, "the decision CHECK constraint is not in the database"
    for member in CallDecision:
        assert f"'{member.value}'" in definition, member.value
    # Exactly the enum, in its declaration order, and nothing else: the list the server holds is
    # the list `decision_check_expression()` would generate. Membership alone would pass a
    # constraint that still admitted a value the enum had dropped.
    assert tuple(re.findall(r"'([a-z_]+)'", definition)) == CALL_DECISIONS
    assert tuple(re.findall(r"'([a-z_]+)'", decision_check_expression())) == CALL_DECISIONS


# ---------------------------------------------------------------------------
# The tenant boundary (T-5-45).
# ---------------------------------------------------------------------------


async def _other_tenant(db_session) -> User:
    other = User(
        clerk_id=f"test_other_{uuid_module.uuid4().hex[:12]}",
        email=f"other-{uuid_module.uuid4().hex[:12]}@clawdi.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


async def test_tenant_isolation(db_session, seed_user, client) -> None:
    """Another tenant's rows are absent AND this tenant's rows are present.

    The second half is not decoration. A query broken so badly that it returns nothing at all
    would satisfy "no foreign rows" perfectly, so an isolation test written with only that half
    passes at exactly the moment the feature stops working.

    The foreign tenant's identifiers are supplied through EVERY parameter the route accepts, and
    the route is separately asserted to accept no tenant-shaped parameter at all -- which is the
    stronger claim, because a parameter that does not exist cannot be got wrong.
    """
    other = await _other_tenant(db_session)
    foreign_session = f"foreign-{uuid_module.uuid4().hex[:8]}"

    await record_companion_tool_call(
        db_session,
        user_id=other.id,
        session_id=foreign_session,
        companion_key="filebrowser",
        verb="delete_path",
        tool_class="destroy",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/theirs"},
    )
    await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/mine"},
    )
    await db_session.commit()

    mine = await client.get(ACTIVITY_PATH)
    assert mine.status_code == 200, mine.text
    own_items = mine.json()["items"]
    assert len(own_items) > 0, "the caller's own rows vanished; isolation would pass vacuously"
    assert all(item["verb"] == "list_dir" for item in own_items), own_items

    # Every accepted parameter, pointed at the other tenant's row.
    narrowed = await client.get(
        ACTIVITY_PATH,
        params={
            "session_id": foreign_session,
            "tool_class": "destroy",
            "decision": CallDecision.ALLOWED.value,
            "companion_key": "filebrowser",
            "limit": 200,
            "offset": 0,
        },
    )
    assert narrowed.status_code == 200, narrowed.text
    assert narrowed.json()["items"] == []


def test_the_activity_route_accepts_no_tenant_identifier() -> None:
    """No parameter on the listing could name a tenant, an owner or an Agent.

    The audit route's precedent is a tenant predicate derived from the auth context; this asserts
    the complementary half -- that there is no supplied value for a handler to prefer by mistake.
    """
    import inspect

    from app.routes.companions import list_companion_tool_calls

    parameters = set(inspect.signature(list_companion_tool_calls).parameters)
    forbidden = {"user_id", "tenant_id", "clerk_id", "target_user_id", "environment_id", "agent_id"}
    assert parameters & forbidden == set(), parameters & forbidden


async def test_a_tenant_with_no_calls_gets_an_empty_list_and_a_success_status(client) -> None:
    """Empty is a successful answer, never a 404.

    A not-found here would tell a new tenant their activity log does not exist, which is a
    different and wrong claim: it exists and nothing has happened in it yet.
    """
    response = await client.get(ACTIVITY_PATH)
    assert response.status_code == 200, response.text
    assert response.json() == {"items": []}


# ---------------------------------------------------------------------------
# Ordering and paging (T-5-48).
# ---------------------------------------------------------------------------


async def _two_rows_sharing_a_timestamp(db_session, user) -> datetime:
    for path in ("/first", "/second"):
        await record_companion_tool_call(
            db_session,
            user_id=user.id,
            companion_key="filebrowser",
            verb="list_dir",
            tool_class="read",
            decision=CallDecision.ALLOWED,
            arguments={"path": path},
        )
    moment = datetime.now(UTC) - timedelta(seconds=5)
    await db_session.execute(
        update(CompanionToolCall)
        .where(CompanionToolCall.user_id == user.id)
        .values(created_at=moment)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    return moment


async def test_identical_timestamps_still_have_a_total_and_stable_order(
    db_session, seed_user, client
) -> None:
    """Creation time alone is not a total order; the row identifier is the tiebreaker.

    Forced to the same instant deliberately. Two rows written inside one millisecond are ordinary
    under an agent's call rate, and a single-key ordering lets them swap between two identical
    queries -- which is the same defect as paging that repeats or skips, arriving through a
    different door.
    """
    await _two_rows_sharing_a_timestamp(db_session, seed_user)

    first = await client.get(ACTIVITY_PATH)
    second = await client.get(ACTIVITY_PATH)
    assert first.status_code == 200 and second.status_code == 200
    order_one = [item["id"] for item in first.json()["items"]]
    order_two = [item["id"] for item in second.json()["items"]]
    assert len(order_one) == 2
    assert order_one == order_two


async def test_paging_across_a_timestamp_tie_neither_repeats_nor_skips(
    db_session, seed_user, client
) -> None:
    """Page size one, straight across the boundary between two rows sharing an instant."""
    await _two_rows_sharing_a_timestamp(db_session, seed_user)

    page_one = await client.get(ACTIVITY_PATH, params={"limit": 1, "offset": 0})
    page_two = await client.get(ACTIVITY_PATH, params={"limit": 1, "offset": 1})
    assert page_one.status_code == 200 and page_two.status_code == 200

    ids = [item["id"] for item in page_one.json()["items"]] + [
        item["id"] for item in page_two.json()["items"]
    ]
    assert len(ids) == 2, ids
    assert len(set(ids)) == 2, f"paging repeated a row: {ids}"

    whole = [item["id"] for item in (await client.get(ACTIVITY_PATH)).json()["items"]]
    assert set(ids) == set(whole), f"paging skipped a row: {ids} vs {whole}"


# ---------------------------------------------------------------------------
# A null session is recorded, not hidden.
# ---------------------------------------------------------------------------


async def test_a_row_with_no_session_identifier_is_returned_rather_than_filtered_out(
    db_session, seed_user, client
) -> None:
    """``tools/call`` delivers no session identifier, so a null session is the ORDINARY case.

    Filtering those rows out would empty the activity log of nearly everything it has to show, and
    fabricating an identifier would be worse: the log would claim a grouping the protocol never
    delivered.
    """
    await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        session_id=None,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/"},
    )
    await db_session.commit()

    response = await client.get(ACTIVITY_PATH)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    # Present and null -- the field is rendered, not omitted, so a renderer can say "not recorded"
    # rather than having to infer absence from a missing key.
    assert "session_id" in items[0]
    assert items[0]["session_id"] is None


# ---------------------------------------------------------------------------
# Redaction (T-5-46).
# ---------------------------------------------------------------------------


async def test_a_secretish_argument_is_redacted_and_the_digest_survives(
    db_session, seed_user
) -> None:
    """A credential passed by mistake is redacted, and the row is still correlatable.

    The key name is built from ``SECRETISH_KEY_PARTS`` imported from ``app.services.audit`` rather
    than restated here, so this test follows the shipped redactor's vocabulary rather than a copy
    of it that would drift the first time a part is added.
    """
    secretish_key = f"companion_{SECRETISH_KEY_PARTS[0]}"
    row = await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/", secretish_key: "sk-live-do-not-store-me"},
    )
    await db_session.commit()

    assert row.arguments[secretish_key] == "[REDACTED]"
    assert "sk-live-do-not-store-me" not in str(row.arguments)
    assert row.arguments["path"] == "/"
    # The digest is what still joins this row to its approval and to a repeat of the same action,
    # so redaction must not cost the correlation.
    assert isinstance(row.arguments_digest, str)
    assert len(row.arguments_digest) == 64


# ---------------------------------------------------------------------------
# The chain survives the approval round trip.
# ---------------------------------------------------------------------------


async def _approve(client, approval_id) -> None:
    response = await client.post(f"/v1/companions/approvals/{approval_id}/approve")
    assert response.status_code == 200, response.text


def _continuation_from(result) -> str:
    from app.services.companion_dispatch import APPROVAL_CONTINUATION_PREFIX

    text = result["content"][0]["text"]
    for line in text.splitlines():
        if line.startswith(APPROVAL_CONTINUATION_PREFIX):
            return line[len(APPROVAL_CONTINUATION_PREFIX) :].strip()
    raise AssertionError(f"no continuation in the refusal: {text}")


async def test_a_call_retried_after_approval_links_back_to_the_refusal_it_followed(
    db_session, seed_user, engine, client
) -> None:
    """The chain has to survive the round trip through a human, or it is not a chain.

    A pending-approval refusal and the execution that follows it are the SAME attempt from the
    tenant's point of view, separated by a decision made in a browser. Without the parent link the
    activity log shows two unrelated rows and the question "what happened to the call I approved"
    has no answer in it.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix=_prefix()
    )
    await _enable_destroy(db_session, seed_user)
    await _cap_policy(db_session, seed_user)
    # Captured BEFORE the first call, for the reason `BranchContext` records.
    user_id = seed_user.id
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    assert asked["isError"] is True
    continuation = _continuation_from(asked)

    pending = await _provenance_rows(db_session, user_id)
    assert [row.decision for row in pending] == [CallDecision.PENDING_APPROVAL]
    pending_id = pending[0].id

    approval = (
        await db_session.execute(
            select(CompanionApproval).where(CompanionApproval.user_id == user_id)
        )
    ).scalar_one()
    approval_id = approval.id
    await _approve(client, approval_id)

    redeemed, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": DOOMED_PATH, "approval": continuation},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _complete_next_call(sessionmaker, environment_id=environment_id, body=STUB_BODY),
    )
    assert redeemed.get("isError") is not True, redeemed

    rows = await _provenance_rows(db_session, user_id)
    assert len(rows) == 2, [row.decision for row in rows]
    executed = rows[-1]
    assert executed.decision == CallDecision.ALLOWED
    assert executed.parent_call_id == pending_id
    assert executed.approval_id == approval_id

    # Following the link reaches the pending row, which is the property a drawer renders.
    parent = await db_session.get(CompanionToolCall, executed.parent_call_id)
    assert parent is not None
    assert parent.decision == CallDecision.PENDING_APPROVAL


# ---------------------------------------------------------------------------
# The prompting-content correlation, labelled as the heuristic it is (T-5-49).
# ---------------------------------------------------------------------------


async def test_the_prompting_content_link_is_present_and_labelled_best_effort(
    db_session, seed_user, engine, client
) -> None:
    """A second call in the same session is attributed to the companion response before it.

    And the attribution is LABELLED. The façade cannot know that a later call was caused by an
    earlier response -- it knows only that the response came first. The dashboard renders a
    disclosure line derived from this, so the label has to travel with the value rather than live
    in a docstring the renderer never reads.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix=_prefix()
    )
    await _policy(db_session, seed_user, "list_dir", "allow")
    await _cap_policy(db_session, seed_user)
    user_id = seed_user.id
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    session_id = f"mcp-{uuid_module.uuid4().hex[:12]}"

    first, _ = await asyncio.gather(
        call_companion_tool(
            READ_TOOL,
            {"path": "/"},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
            session_id=session_id,
        ),
        _complete_next_call(sessionmaker, environment_id=environment_id, body=STUB_BODY),
    )
    assert first.get("isError") is not True, first
    # The identifier the agent saw in the envelope is the identifier the next row points at.
    assert UNTRUSTED_OPEN_PREFIX in first["content"][0]["text"]

    second, _ = await asyncio.gather(
        call_companion_tool(
            READ_TOOL,
            {"path": "/projects"},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
            session_id=session_id,
        ),
        _complete_next_call(sessionmaker, environment_id=environment_id, body=STUB_BODY),
    )
    assert second.get("isError") is not True, second

    rows = await _provenance_rows(db_session, user_id)
    assert len(rows) == 2, [row.decision for row in rows]
    assert rows[0].source_content_ref is None, "nothing preceded the first call"
    assert rows[1].source_content_ref == rows[0].call_jti

    # `call_companion_tool` rolls the caller's session back between polls, which EXPIRES
    # every instance attached to it -- including `seed_user`, which the `client` fixture
    # rebuilds an `AuthContext` from on EVERY request. Refreshed here so the route call below
    # exercises the route rather than the harness.
    await db_session.refresh(seed_user)
    detail = await client.get(f"{ACTIVITY_PATH}/{rows[1].id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["source_content_ref"] == rows[0].call_jti
    assert payload["source_content_disclosure"] == SOURCE_CONTENT_REF_DISCLOSURE
    assert "best-effort" in payload["source_content_disclosure"].lower()


async def test_stored_companion_output_keeps_its_untrusted_framing_on_the_read_route(
    db_session, seed_user, engine, client
) -> None:
    """Companion bytes leaving through the activity log are still delimited and datamarked.

    The dashboard reviewer has to see what the agent saw. Handing the same bytes out unwrapped
    here would make this route the one place in the system where companion-controlled text arrives
    without its envelope -- and the reviewer is the person least able to tell.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix=_prefix()
    )
    await _policy(db_session, seed_user, "list_dir", "allow")
    await _cap_policy(db_session, seed_user)
    user_id = seed_user.id
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    result, _ = await asyncio.gather(
        call_companion_tool(READ_TOOL, {"path": "/"}, auth=auth, db=db_session, deadline_seconds=5),
        _complete_next_call(sessionmaker, environment_id=environment_id, body=STUB_BODY),
    )
    assert result.get("isError") is not True, result

    rows = await _provenance_rows(db_session, user_id)
    # `call_companion_tool` rolls the caller's session back between polls, which EXPIRES
    # every instance attached to it -- including `seed_user`, which the `client` fixture
    # rebuilds an `AuthContext` from on EVERY request. Refreshed here so the route call below
    # exercises the route rather than the harness.
    await db_session.refresh(seed_user)
    detail = await client.get(f"{ACTIVITY_PATH}/{rows[0].id}")
    assert detail.status_code == 200, detail.text
    content = detail.json()["companion_content"]
    assert content is not None
    assert UNTRUSTED_OPEN_PREFIX in content


async def test_no_response_carries_signing_material_or_a_continuation(
    db_session, seed_user, client
) -> None:
    """The detail route hands over the provenance chain and nothing redeemable.

    ``call_jti`` and the argument digest are deliberately present -- they are what a reviewer
    correlates on. A continuation is deliberately absent, and it is not merely omitted from the
    serializer: it is never persisted on this row at all, so there is no column for a future
    field-by-field construction to pick up by accident.
    """
    row = await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/"},
    )
    await db_session.commit()

    detail = await client.get(f"{ACTIVITY_PATH}/{row.id}")
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["arguments_digest"] == row.arguments_digest
    serialized = str(payload).lower()
    for forbidden in ("continuation", "secret", "assertion", "bearer", "signing"):
        assert forbidden not in serialized, forbidden


async def test_another_tenants_call_detail_is_a_not_found(db_session, seed_user, client) -> None:
    """A foreign row answers 404, never 403.

    A 403 would confirm the row exists, which is a membership oracle over somebody else's
    activity -- the same reasoning the approvals decision route already applies.
    """
    other = await _other_tenant(db_session)
    row = await record_companion_tool_call(
        db_session,
        user_id=other.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/theirs"},
    )
    await db_session.commit()

    detail = await client.get(f"{ACTIVITY_PATH}/{row.id}")
    assert detail.status_code == 404, detail.text


async def test_a_preview_persists_the_diff_the_agent_was_shown(
    db_session, seed_user, client
) -> None:
    """The dry-run diff survives as a column, not only as bytes returned to the agent.

    05-07 recorded `dry_run` as a bare boolean, so the structured payload `compute_dry_run`
    produces reached the agent and was discarded. Plan 05-09 renders that payload -- "What would
    change", the before/after list, the "Nothing would change." empty state -- and had nowhere to
    read it from.

    Recomputing it at render time is NOT the same artifact and is why this is a column: the probe
    would re-read the target as it is NOW, so a reviewer opening the log after the write landed
    would be shown a diff that never existed. An audit row has to say what was seen, not what is.
    """
    diff = {
        "tool": "filebrowser__delete_path",
        "tool_class": "destroy",
        "target": "/notes",
        "target_count": 2,
        "before": [{"path": "/notes/a.md"}, {"path": "/notes/b.md"}],
        "after": [],
        "undetermined": [],
    }
    row = await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        companion_key="filebrowser",
        verb="delete_path",
        tool_class="destroy",
        decision=CallDecision.DRY_RUN_COMPLETED,
        arguments={"path": "/notes"},
        dry_run=True,
        dry_run_diff=diff,
    )
    await db_session.commit()

    assert row.dry_run_diff == diff

    detail = await client.get(f"{ACTIVITY_PATH}/{row.id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["dry_run_diff"] == diff


async def test_a_call_that_was_never_previewed_has_no_diff(db_session, seed_user, client) -> None:
    """Null, not an empty diff. "Nothing would change" and "nobody looked" are different answers,
    and a renderer that cannot tell them apart will show the first when it means the second."""
    row = await record_companion_tool_call(
        db_session,
        user_id=seed_user.id,
        companion_key="filebrowser",
        verb="list_dir",
        tool_class="read",
        decision=CallDecision.ALLOWED,
        arguments={"path": "/"},
    )
    await db_session.commit()

    assert row.dry_run_diff is None
    detail = await client.get(f"{ACTIVITY_PATH}/{row.id}")
    assert detail.json()["dry_run_diff"] is None


# ---------------------------------------------------------------------------
# The prompting-content ledger's identity: (tenant, session), with no shared bucket (plan 05-10).
#
# Before this, `_ledger_key` returned `(str(user_id), session_id or "")`, so every call in a tenant
# that carried no session -- which was every call, because nothing supplied one -- landed in one
# bucket. Once plan 05-10 made session ids real, that fallback stopped being a harmless
# placeholder and became a live cross-attribution: two concurrent agent conversations in one
# tenant would read each other's "prompted by" reference while `SOURCE_CONTENT_REF_DISCLOSURE`
# asserted "in the same session", a scope the server could not observe.
#
# The generalization taken is PROMOTION, not a second variant: an absent session is
# UNATTRIBUTABLE rather than demoted to a shared bucket. A bucket that cannot be attributed to a
# session cannot honestly answer a claim about a session, so the shared bucket is made
# UNREPRESENTABLE rather than kept as a fallback.
# ---------------------------------------------------------------------------


def test_two_concurrent_sessions_for_one_tenant_never_read_each_others_prompting_content() -> None:
    """The invariant test for the promotion. Goes red the instant a shared bucket returns.

    Two agent conversations under one tenant are ordinary -- an Agent can run several at once --
    and the whole value of the "prompted by" link is that it says which companion response THIS
    conversation just read. A ledger that answered session B with session A's content would be
    making a false statement in the one place the dashboard presents as provenance.
    """
    tenant = uuid_module.uuid4()
    record_returned_content(user_id=tenant, session_id="session-A-aaaaaaaaaa", content_id="c-a")

    assert prompting_content_ref(user_id=tenant, session_id="session-B-bbbbbbbbbb") is None


def test_an_absent_session_records_nothing_and_resolves_to_nothing() -> None:
    """No session observed, so no attribution offered.

    "Not recorded" is a true statement about a call whose conversation the server never saw. A
    reference drawn from a shared bucket would have been a false one, and false is strictly worse
    than absent here: the dashboard renders this next to the word provenance.
    """
    tenant = uuid_module.uuid4()
    record_returned_content(user_id=tenant, session_id=None, content_id="c-orphan")

    assert prompting_content_ref(user_id=tenant, session_id=None) is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_session_behaves_exactly_as_an_absent_one(blank: str) -> None:
    """An empty string must not become a bucket key.

    This is the old shared bucket spelled a different way: `session_id or ""` produced exactly
    this key for every null session, so admitting a blank string would reintroduce the collapse
    through the back door rather than through the front.
    """
    tenant = uuid_module.uuid4()
    record_returned_content(user_id=tenant, session_id=blank, content_id="c-blank")

    assert prompting_content_ref(user_id=tenant, session_id=blank) is None


def test_the_same_session_still_resolves_and_still_expires() -> None:
    """The promotion narrows the key; it does not change what a real session gets.

    The TTL half is asserted here too because a key change is exactly the kind of edit that
    silently drops an expiry check along with the bucket it was reading.
    """
    tenant = uuid_module.uuid4()
    clock = [1_000.0]
    record_returned_content(
        user_id=tenant, session_id="session-A-aaaaaaaaaa", content_id="c-a", now=lambda: clock[0]
    )

    live = prompting_content_ref(
        user_id=tenant, session_id="session-A-aaaaaaaaaa", now=lambda: clock[0]
    )
    assert live == "c-a"

    clock[0] += CONTENT_REF_TTL_SECONDS + 1.0
    stale = prompting_content_ref(
        user_id=tenant, session_id="session-A-aaaaaaaaaa", now=lambda: clock[0]
    )
    assert stale is None


def test_two_tenants_holding_the_same_session_string_do_not_share_a_bucket() -> None:
    """The tenant half of the key is not optional, and the promotion must not have eaten it.

    Session ids are CLIENT-SUPPLIED and unverified by design, so a tenant can present any string
    it likes -- including one it observed elsewhere. Without the tenant half that would be a
    cross-tenant read of companion content, which is the most sensitive leak this log could
    produce (T-05-10-01).
    """
    one = uuid_module.uuid4()
    two = uuid_module.uuid4()
    shared = "session-shared-xxxxxx"
    record_returned_content(user_id=one, session_id=shared, content_id="c-one")
    record_returned_content(user_id=two, session_id=shared, content_id="c-two")

    assert prompting_content_ref(user_id=one, session_id=shared) == "c-one"
    assert prompting_content_ref(user_id=two, session_id=shared) == "c-two"
