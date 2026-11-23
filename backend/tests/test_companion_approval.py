"""Non-blocking approval bound to a canonical ACTION, not to a token (TOOL-03, D-08).

``05-RESEARCH.md`` §Pitfall 2 is the whole reason this file opens where it opens. A perfect
single-use nonce still permits three destructive writes, because a re-planning agent simply
requests a fresh, fully valid token for the same action -- "identifier-local token consumption
cannot prevent fresh reissuance unless the issuer retains monotonic durable state over the
authorized action, confirmation event, and remaining execution budget". A suite whose replay
assertion is "the token cannot be used twice" asserts the property that does NOT hold, so
``test_semantic_replay_is_refused`` is the first test in this module and it covers both halves:
the spent budget refuses redemption, AND a fresh request for the same action hash reuses the spent
row rather than issuing a second authorization.

**What each group proves.**

* *Semantic replay.* One human decision authorizes one canonical action once. Asking again inside
  the window returns the SAME row, spent, rather than a new one.
* *Same token, different action.* A continuation is redeemable only against arguments that hash to
  its recorded action hash, and only against the target count it was approved for. Each refuses
  with its own machine reason, because "you changed the path" and "the directory grew" send an
  agent to fix different things.
* *Terminal states.* Expiry and denial are distinct refusals, and a denied approval's continuation
  is dead.
* *Non-blocking.* The ask response returns while the approval is still undecided and while nothing
  has been minted -- a call awaiting approval holds no credential of any kind.
* *The decision surface.* Approvals are listed and decided only through the dashboard API, scoped
  by a predicate derived from the auth context that no parameter can widen, with a CLI credential
  rejected outright and an already-decided row answering 409.

The module runs on the committed lane because the double-decision case needs an executor stand-in
writing its result on an INDEPENDENT connection while the façade polls -- invisible to any test
whose writes never leave one transaction.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid as uuid_module
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.core.companion_policy import REASON_CLASS_DEFAULT
from app.models.companion_approval import (
    DEFAULT_EXECUTION_BUDGET,
    STATUS_COMMITTED,
    STATUS_DENIED,
    STATUS_ISSUED,
    STATUS_PREPARED,
    CompanionApproval,
)
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_session_budget import CompanionCapPolicy
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import CompanionDestroyEnablement, CompanionToolPolicy
from app.routes import companions as companions_routes
from app.services import companion_facade
from app.services.companion_dispatch import (
    APPROVAL_CONTINUATION_PREFIX,
    APPROVAL_REASONS,
    CONTROL_ARGUMENT_KEYS,
    REASON_ACTION_HASH_MISMATCH,
    REASON_APPROVAL_BUDGET_SPENT,
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_EXPIRED,
    REASON_APPROVAL_READY,
    REASON_APPROVAL_REQUIRED,
    REASON_TARGET_COUNT_MISMATCH,
    canonical_action_arguments,
    canonical_action_hash,
    claim_next_call,
    complete_call,
    dispatch_call_token,
    verify_dispatch_call,
)
from app.services.companion_dryrun import reset_preview_ledger
from app.services.companion_facade import call_companion_tool, reset_companion_tools_cache
from tests.hosted_runtime_fixtures import deploy_companion_state

pytestmark = pytest.mark.committed_db

DELETE_TOOL = "filebrowser__delete_path"
DOOMED_PATH = "/projects/quarterly-report.md"
OTHER_PATH = "/projects/keep-this.md"


@pytest.fixture(autouse=True)
def _clear_companion_caches():
    reset_companion_tools_cache()
    reset_preview_ledger()
    yield
    reset_companion_tools_cache()
    reset_preview_ledger()


# ---------------------------------------------------------------------------
# Fixtures and helpers.
# ---------------------------------------------------------------------------


async def _enable_destroy(db_session, seed_user) -> None:
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
    values = {
        "per_call_calls": 20,
        "per_call_records": 5_000,
        "per_call_bytes": 4096,
        "per_session_calls": 50,
        "per_session_records": 50_000,
        "per_session_bytes": 40_960,
    }
    values.update(overrides)
    row = CompanionCapPolicy(user_id=seed_user.id, companion_key="filebrowser", **values)
    db_session.add(row)
    await db_session.commit()
    return row


async def _ready_tenant(db_session, seed_user, *, prefix: str):
    """A tenant whose only remaining gate for ``delete_path`` is the approval itself.

    Every earlier gate is opened deliberately and separately -- the destroy switch (05-03) and a
    cap ceiling wide enough that no cap can refuse (05-04) -- so a refusal in this module is the
    APPROVAL gate and never one of the two that run before it. A test that left those closed would
    be green for the wrong reason, which is the failure 05-04 recorded when its bulk cap tests were
    silently refused by the destroy switch.
    """
    environment_id, _ = await deploy_companion_state(db_session, seed_user, machine_prefix=prefix)
    await _enable_destroy(db_session, seed_user)
    await _cap_policy(db_session, seed_user)
    return environment_id


def _continuation_from(result) -> str:
    """The signed continuation the refusal carried, or fail naming what came back instead.

    The trailer is a CONTRACT, not prose: an agent has to be able to find the token without
    parsing a sentence, and ``APPROVAL_CONTINUATION_PREFIX`` is the thing both sides agree on.
    """
    text = result["content"][0]["text"]
    for line in text.splitlines():
        if line.startswith(APPROVAL_CONTINUATION_PREFIX):
            return line[len(APPROVAL_CONTINUATION_PREFIX) :].strip()
    raise AssertionError(f"no continuation in the refusal: {text}")


async def _approvals(db_session, seed_user) -> list[CompanionApproval]:
    return list(
        (
            await db_session.execute(
                select(CompanionApproval)
                .where(CompanionApproval.user_id == seed_user.id)
                .order_by(CompanionApproval.created_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _dispatch_rows(db_session, seed_user, *, verb: str) -> list[CompanionDispatchCall]:
    rows = (
        (
            await db_session.execute(
                select(CompanionDispatchCall)
                .where(CompanionDispatchCall.user_id == seed_user.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    # `status == "failed"` rows are REFUSAL records, which every attempt writes including the ones
    # that never minted. Only a non-refusal row means a signed pre-authorized call existed.
    return [row for row in rows if row.verb == verb and row.status != "failed"]


async def _drive_executor_once(sessionmaker, *, environment_id, stub, attempts: int = 600) -> None:
    """Claim one queued call on an INDEPENDENT connection and issue it at the stub.

    Deliberately thinner than ``test_companion_tracer.py``'s HTTP-endpoint stand-in: what this
    module has to measure is how many requests reached the companion, not the dispatch channel a
    second time. It still parses the envelope with the SHIPPED ``verify_dispatch_call``, so an
    envelope change fails here too, and the bytes it sends are real bytes on a real socket.
    """
    for _attempt in range(attempts):
        async with sessionmaker() as db:
            row = await claim_next_call(db, environment_id=environment_id)
            if row is None:
                await db.commit()
            else:
                envelope = verify_dispatch_call(dispatch_call_token(row))
                claim_id = row.poll_claim_id
                jti = row.call_jti
                await db.commit()
                async with httpx.AsyncClient(base_url=stub.base_url) as companion:
                    response = await companion.request(
                        envelope["method"],
                        envelope["path_template"],
                        params=envelope["arguments"],
                        headers={"X-JWT-Assertion": "stand-in"},
                    )
                async with sessionmaker() as report:
                    await complete_call(
                        report,
                        call_jti=jti,
                        poll_claim_id=claim_id,
                        result={"status": response.status_code, "body": response.text},
                    )
                    await report.commit()
                return
        await asyncio.sleep(0.01)
    raise AssertionError("no queued dispatch call appeared for the executor stand-in")


# ---------------------------------------------------------------------------
# Semantic replay: the finding this whole design exists for (T-5-37).
# ---------------------------------------------------------------------------


async def test_semantic_replay_is_refused(db_session, seed_user, engine, companion_stub) -> None:
    """One human decision authorizes one canonical action ONCE, and re-asking does not reissue.

    Both halves are here on purpose. A test that only asserted "the token is single use" would
    assert the property the paper shows is insufficient: the agent's next move is not to replay the
    spent token, it is to ask again and get a fresh valid one. The second half -- the fresh request
    returning the SAME spent row -- is the half that actually closes Pitfall 2.
    """
    environment_id = await _ready_tenant(db_session, seed_user, prefix="replay")
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    user_id = seed_user.id

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    assert asked["isError"] is True
    assert REASON_APPROVAL_REQUIRED in asked["content"][0]["text"]
    continuation = _continuation_from(asked)

    rows = await _approvals(db_session, seed_user)
    assert len(rows) == 1
    approval_id = rows[0].id
    assert rows[0].status == STATUS_ISSUED
    assert rows[0].remaining_budget == DEFAULT_EXECUTION_BUDGET

    await _approve(db_session, approval_id, seed_user)

    # The one authorized execution. The executor stand-in runs concurrently so the call completes
    # rather than timing out -- what matters is that the mint happened and the budget went with it.
    redeemed, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": DOOMED_PATH, "approval": continuation},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _drive_executor_once(sessionmaker, environment_id=environment_id, stub=companion_stub),
    )
    assert redeemed.get("isError") is not True, redeemed
    assert len(companion_stub.requests) == 1
    assert companion_stub.requests[0].method == "DELETE"

    async with sessionmaker() as check:
        spent = (
            await check.execute(
                select(CompanionApproval).where(CompanionApproval.id == approval_id)
            )
        ).scalar_one()
        assert spent.remaining_budget == 0
        assert spent.status == STATUS_COMMITTED

    # Half one: the same token again. Refused for a spent budget, and nothing new is minted.
    replayed = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )
    assert replayed["isError"] is True
    assert REASON_APPROVAL_BUDGET_SPENT in replayed["content"][0]["text"]

    # Half two: ask again, exactly as a re-planning agent would. The SAME row comes back, still
    # spent. No second authorization is minted, and no fresh continuation is handed over.
    reasked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    assert reasked["isError"] is True
    assert REASON_APPROVAL_BUDGET_SPENT in reasked["content"][0]["text"]

    async with sessionmaker() as check:
        after = list(
            (
                await check.execute(
                    select(CompanionApproval).where(CompanionApproval.user_id == user_id)
                )
            )
            .scalars()
            .all()
        )
    assert [row.id for row in after] == [approval_id]
    assert after[0].remaining_budget == 0

    # Exactly one signed pre-authorized delete existed across all four attempts.
    async with sessionmaker() as check:
        minted = (
            (
                await check.execute(
                    select(CompanionDispatchCall).where(
                        CompanionDispatchCall.user_id == user_id,
                        CompanionDispatchCall.verb == "delete_path",
                        CompanionDispatchCall.status != "failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(list(minted)) == 1

    await _drop_dispatch_rows(sessionmaker, environment_id)


# ---------------------------------------------------------------------------
# The same token against a different action (T-5-38, T-5-39).
# ---------------------------------------------------------------------------


async def test_a_continuation_presented_with_mutated_arguments_is_refused(
    db_session, seed_user, companion_stub
) -> None:
    """An approval for one path cannot be redeemed against another, by its own machine reason.

    The refusal is asserted by REASON rather than by the boolean error flag: a generic failure
    would be indistinguishable from an expired token or a spent budget, and 05-07's activity log
    has to be able to answer *why not*.
    """
    await _ready_tenant(db_session, seed_user, prefix="hashdrift")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    await _approve(db_session, rows[0].id, seed_user)

    refused = await call_companion_tool(
        DELETE_TOOL,
        {"path": OTHER_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert refused["isError"] is True
    assert REASON_ACTION_HASH_MISMATCH in refused["content"][0]["text"]
    assert companion_stub.requests == []
    assert await _dispatch_rows(db_session, seed_user, verb="delete_path") == []


async def test_a_continuation_whose_target_count_drifted_is_refused(
    db_session, seed_user, companion_stub
) -> None:
    """A three-file delete cannot be redeemed as a three-thousand-file one (T-5-39).

    The cap ceiling is deliberately wide enough to admit three thousand records, so the refusal
    below is the APPROVAL's bound and not 05-04's. Without that the test would pass for the wrong
    reason and would keep passing with the target-count check deleted.
    """
    await _ready_tenant(db_session, seed_user, prefix="countdrift")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
        count_targets=lambda entry, arguments: 3,
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    assert rows[0].resolved_target_count == 3
    await _approve(db_session, rows[0].id, seed_user)

    refused = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
        count_targets=lambda entry, arguments: 3_000,
    )

    assert refused["isError"] is True
    assert REASON_TARGET_COUNT_MISMATCH in refused["content"][0]["text"]
    assert companion_stub.requests == []
    assert await _dispatch_rows(db_session, seed_user, verb="delete_path") == []


async def test_an_uncounted_bulk_approval_binds_no_target_count_rather_than_one(
    db_session, seed_user, client, companion_stub
) -> None:
    """A count nobody counted is reported as unknown, never as one.

    ``filebrowser__delete_path`` is bulk-capable: ``DELETE /api/resources`` on a directory removes
    what is under it. With no preceding dry run the resolver returns 05-04's single-path FLOOR,
    which is an honest debit and a dishonest CLAIM -- binding ``1`` would put the number 1 in front
    of a person about to authorize an unbounded delete, and would then "match" at redemption
    because the redemption resolves the same floor.

    So the row binds ``None`` and the dashboard says ``null``. This is the plan's own prohibition
    applied to the number a reviewer would lean on hardest: do not display an approval mode the
    server does not actually gate on.
    """
    await _ready_tenant(db_session, seed_user, prefix="uncounted")
    auth = AuthContext(user=seed_user)

    await call_companion_tool(
        DELETE_TOOL, {"path": "/"}, auth=auth, db=db_session, deadline_seconds=0
    )

    rows = await _approvals(db_session, seed_user)
    assert rows[0].resolved_target_count is None

    listed = await client.get("/v1/companions/approvals")
    assert listed.status_code == 200
    assert listed.json()["items"][0]["resolved_target_count"] is None
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# Terminal states: expiry and denial.
# ---------------------------------------------------------------------------


async def test_an_expired_approval_is_refused_and_says_it_expired(
    db_session, seed_user, companion_stub
) -> None:
    """Expiry is its own refusal, and it tells the agent to ask again rather than to retry."""
    await _ready_tenant(db_session, seed_user, prefix="expiry")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    await _approve(db_session, rows[0].id, seed_user)
    await db_session.execute(
        update(CompanionApproval)
        .where(CompanionApproval.id == rows[0].id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await db_session.commit()

    refused = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert refused["isError"] is True
    text = refused["content"][0]["text"]
    assert REASON_APPROVAL_EXPIRED in text
    assert "ask" in text.lower()
    assert companion_stub.requests == []


async def test_a_denied_approval_cannot_be_redeemed(
    db_session, seed_user, client, companion_stub
) -> None:
    """Denying is terminal: the continuation issued before the denial is dead afterwards."""
    await _ready_tenant(db_session, seed_user, prefix="denied")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)

    denied = await client.post(f"/v1/companions/approvals/{rows[0].id}/deny")
    assert denied.status_code == 200
    assert denied.json()["status"] == STATUS_DENIED

    refused = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert refused["isError"] is True
    assert REASON_APPROVAL_DENIED in refused["content"][0]["text"]
    assert companion_stub.requests == []
    assert await _dispatch_rows(db_session, seed_user, verb="delete_path") == []


# ---------------------------------------------------------------------------
# The ask path itself: non-blocking, credential-free, and idempotent per action.
# ---------------------------------------------------------------------------


async def test_the_ask_response_returns_while_the_approval_is_still_undecided(
    db_session, seed_user, companion_stub, monkeypatch
) -> None:
    """D-08's whole reason: nothing holds a connection open waiting on a human.

    The CLI proxy's forward timeout is thirty seconds, so a blocking design breaks on any person
    slower than half a minute. The assertion is structural rather than a stopwatch: the call
    returned, and the row it created is STILL in the undecided state -- which cannot be true of a
    design that waited for the decision.
    """
    await _ready_tenant(db_session, seed_user, prefix="nonblock")
    auth = AuthContext(user=seed_user)
    minted: list[str] = []
    original_mint = companion_facade.mint_dispatch_call

    def _recording_mint(**kwargs):
        minted.append(str(kwargs["verb"]))
        return original_mint(**kwargs)

    monkeypatch.setattr(companion_facade, "mint_dispatch_call", _recording_mint)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    assert asked["isError"] is True
    assert REASON_APPROVAL_REQUIRED in asked["content"][0]["text"]
    assert _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    assert [row.status for row in rows] == [STATUS_ISSUED]
    # A call awaiting approval holds NO minted credential. This is the same structural claim D-10
    # makes about policy, applied to the gate a human actually answers.
    assert minted == [], "an approval-pending call minted a signed pre-authorized call"
    assert companion_stub.requests == []


async def test_a_repeated_request_reuses_the_row_rather_than_issuing_a_second_authorization(
    db_session, seed_user, companion_stub
) -> None:
    """Asking twice before a decision is one authorization, not two.

    The uniqueness is in the DATABASE, over the canonical action hash, rather than in a
    check-then-insert here: two rows for one action would let one human decision be spent twice,
    which is Pitfall 2 arriving through the front door.
    """
    await _ready_tenant(db_session, seed_user, prefix="reask")
    auth = AuthContext(user=seed_user)

    first = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    second = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    rows = await _approvals(db_session, seed_user)
    assert len(rows) == 1
    assert rows[0].remaining_budget == DEFAULT_EXECUTION_BUDGET
    assert _continuation_from(first)
    assert _continuation_from(second)
    assert companion_stub.requests == []


async def test_an_approved_row_asked_about_again_hands_back_a_redeemable_continuation(
    db_session, seed_user, companion_stub
) -> None:
    """The agent learns it was approved by asking, because nothing pushes the decision to it.

    There is no channel from the control plane to a running agent -- ``05-RESEARCH.md`` §Pitfall 3
    verified there is none in either direction -- so re-asking is the only way an approval becomes
    usable, and the reason token has to say "ready" rather than "required" or the agent has no way
    to tell the two apart.
    """
    await _ready_tenant(db_session, seed_user, prefix="readyagain")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    assert REASON_APPROVAL_REQUIRED in asked["content"][0]["text"]
    rows = await _approvals(db_session, seed_user)
    await _approve(db_session, rows[0].id, seed_user)

    reasked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    assert reasked["isError"] is True
    assert REASON_APPROVAL_READY in reasked["content"][0]["text"]
    assert _continuation_from(reasked)
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# The canonical action hash.
# ---------------------------------------------------------------------------


def test_the_action_hash_ignores_key_order_and_the_dry_run_flag() -> None:
    """Two semantically identical requests hash identically, or one action becomes two.

    Key order and the preview flag are the two ways an identical request can arrive spelled
    differently. If either changed the hash, the upsert would create a second row, a second human
    decision would be requested, and the remaining-budget defence would bound nothing.
    """
    tenant = uuid_module.uuid4()
    agent = uuid_module.uuid4()
    first = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments={"path": DOOMED_PATH, "dry_run": True},
        tenant_id=tenant,
        agent_id=agent,
    )
    second = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments={"dry_run": False, "path": DOOMED_PATH},
        tenant_id=tenant,
        agent_id=agent,
    )
    third = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments={"path": DOOMED_PATH},
        tenant_id=tenant,
        agent_id=agent,
    )

    assert first == second == third
    assert len(first) == 64
    # The continuation itself is a control parameter too: a hash that covered the token could never
    # be recomputed from the arguments the token arrives with.
    assert canonical_action_arguments({"path": "/x", "dry_run": True, "approval": "tok"}) == {
        "path": "/x"
    }
    assert CONTROL_ARGUMENT_KEYS == frozenset({"dry_run", "approval"})


def test_the_action_hash_separates_tenants_and_agents() -> None:
    """The same arguments from a different tenant are a different action.

    Without the tenant in the hash, the unique index over it would make one tenant's approval row
    collide with another's -- so one tenant's pending request would be answered by a stranger's
    decision, which is the confused deputy with the authorization attached.
    """
    agent = uuid_module.uuid4()
    arguments = {"path": DOOMED_PATH}
    one = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments=arguments,
        tenant_id=uuid_module.uuid4(),
        agent_id=agent,
    )
    other = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments=arguments,
        tenant_id=uuid_module.uuid4(),
        agent_id=agent,
    )
    tenant = uuid_module.uuid4()
    here = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments=arguments,
        tenant_id=tenant,
        agent_id=uuid_module.uuid4(),
    )
    there = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments=arguments,
        tenant_id=tenant,
        agent_id=uuid_module.uuid4(),
    )

    assert one != other
    assert here != there


def test_the_redemption_compares_in_constant_time_and_pins_its_algorithms() -> None:
    """The two footguns a JWT-backed approval is most likely to ship with, asserted at the source.

    ``==`` on a hash is a timing oracle over a value the agent supplies, and handing the decoder
    the token's own ``alg`` header is the algorithm-confusion hole PyJWT deliberately will not
    close for you. Both are properties of HOW the check is written, which no single call can
    demonstrate -- so they are asserted against the module source, the same shape
    ``test_companion_policy.py`` uses for "this module reads no session".
    """
    from app.services import companion_dispatch

    source = inspect.getsource(companion_dispatch)
    assert "hmac.compare_digest" in source
    assert "algorithms=_APPROVAL_ALGORITHMS" in source
    assert '_APPROVAL_ALGORITHMS = ["HS256"]' in source
    # The approval continuation and the dispatch envelope are DIFFERENT audiences. One decoder
    # accepting the other's tokens would make a continuation redeemable as a pre-authorized call.
    assert companion_dispatch.COMPANION_APPROVAL_AUDIENCE != (
        companion_dispatch.COMPANION_DISPATCH_AUDIENCE
    )


def test_every_refusal_carries_a_distinct_machine_reason() -> None:
    """Five refusals, five reasons. A single generic failure cannot answer *why not*."""
    named = {
        REASON_APPROVAL_BUDGET_SPENT,
        REASON_ACTION_HASH_MISMATCH,
        REASON_TARGET_COUNT_MISMATCH,
        REASON_APPROVAL_EXPIRED,
        REASON_APPROVAL_DENIED,
    }
    assert len(named) == 5
    assert named <= APPROVAL_REASONS
    assert REASON_APPROVAL_REQUIRED in APPROVAL_REASONS
    assert REASON_APPROVAL_READY in APPROVAL_REASONS


# ---------------------------------------------------------------------------
# The durable row's own structure.
# ---------------------------------------------------------------------------


def test_the_approval_table_declares_its_status_check_and_its_action_uniqueness() -> None:
    """The two constraints the design rests on are in the DATABASE, not in a code path.

    Uniqueness enforced by application code is uniqueness under a race; a status vocabulary
    enforced by convention is a vocabulary that grows a sixth value nobody decided on.
    """
    table = CompanionApproval.__table__
    checks = [
        constraint
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    ]
    status_check = next(c for c in checks if c.name == "ck_companion_approvals_status")
    rendered = str(status_check.sqltext)
    for status in (STATUS_ISSUED, STATUS_PREPARED, STATUS_COMMITTED, STATUS_DENIED):
        assert f"'{status}'" in rendered
    assert any(c.name == "ck_companion_approvals_budget" for c in checks)

    unique = [
        index
        for index in table.indexes
        if index.unique and [c.name for c in index.columns] == ["canonical_action_sha256"]
    ]
    assert len(unique) == 1, "the canonical action hash is not uniquely indexed"

    budget = table.c["remaining_budget"]
    assert budget.default is not None
    assert budget.default.arg == DEFAULT_EXECUTION_BUDGET
    # The column that the analog this table copies does NOT have, and the one Pitfall 2 requires.
    assert "replay" in (budget.doc or "").lower()


async def test_a_second_row_for_one_action_hash_is_refused_by_the_database(
    db_session, seed_user
) -> None:
    """Two authorizations for one action are unrepresentable, not merely avoided."""
    action = canonical_action_hash(
        companion_key="filebrowser",
        verb="delete_path",
        arguments={"path": DOOMED_PATH},
        tenant_id=seed_user.id,
        agent_id=None,
    )
    for _ in range(2):
        db_session.add(
            CompanionApproval(
                user_id=seed_user.id,
                companion_key="filebrowser",
                verb="delete_path",
                canonical_action_sha256=action,
                arguments={"path": DOOMED_PATH},
                expires_at=datetime.now(UTC) + timedelta(minutes=15),
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ---------------------------------------------------------------------------
# The dashboard-only decision API.
# ---------------------------------------------------------------------------


async def _approve(db_session, approval_id, seed_user) -> None:
    """Approve through the SHIPPED route handler rather than by writing the row.

    A test that flipped the status directly would keep passing if the decision endpoint stopped
    existing, and the endpoint is half of what "decided only from the dashboard" means.
    """
    from httpx import ASGITransport

    from app.core.database import get_session
    from app.main import app

    async def _override_get_session():
        yield db_session

    async def _override_get_auth() -> AuthContext:
        return AuthContext(user=seed_user)

    from app.core.auth import get_auth, get_auth_short_session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    app.dependency_overrides[get_auth_short_session] = _override_get_auth
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(f"/v1/companions/approvals/{approval_id}/approve")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == STATUS_PREPARED
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def _drop_dispatch_rows(sessionmaker, environment_id) -> None:
    async with sessionmaker() as cleanup:
        await cleanup.execute(
            CompanionDispatchCall.__table__.delete().where(
                CompanionDispatchCall.environment_id == environment_id
            )
        )
        await cleanup.commit()


async def test_approving_the_same_row_twice_returns_a_conflict_and_executes_once(
    db_session, seed_user, engine, client, companion_stub
) -> None:
    """Two browser tabs, one execution (T-5-43).

    The second decision answers 409 rather than approving again, and the conditional UPDATE behind
    it is what makes that true under a race rather than under a check-then-write. Execution is
    counted at the COMPANION -- one recorded request on the stub's real socket -- because a row
    count would not distinguish "minted twice" from "minted once and retried".
    """
    environment_id = await _ready_tenant(db_session, seed_user, prefix="twotabs")
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    approval_id = rows[0].id

    first = await client.post(f"/v1/companions/approvals/{approval_id}/approve")
    second = await client.post(f"/v1/companions/approvals/{approval_id}/approve")
    assert first.status_code == 200
    assert second.status_code == 409, second.text

    redeemed, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": DOOMED_PATH, "approval": continuation},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _drive_executor_once(sessionmaker, environment_id=environment_id, stub=companion_stub),
    )
    assert redeemed.get("isError") is not True, redeemed

    again = await call_companion_tool(
        DELETE_TOOL,
        {"path": DOOMED_PATH, "approval": continuation},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )
    assert again["isError"] is True
    assert REASON_APPROVAL_BUDGET_SPENT in again["content"][0]["text"]

    assert len(companion_stub.requests) == 1
    await _drop_dispatch_rows(sessionmaker, environment_id)


async def test_the_listing_is_scoped_to_the_caller_whatever_parameters_it_is_given(
    db_session, seed_user, client
) -> None:
    """The tenant predicate comes from the auth context and no parameter can widen it (T-5-42).

    The stranger's identifier is supplied through every name a caller might hope the route reads.
    None of them is a parameter this route accepts, which is the point: the scope is not something
    the endpoint could get wrong under a value, because there is no value.
    """
    from app.models.user import User

    await _ready_tenant(db_session, seed_user, prefix="tenantscope")
    stranger = User(
        clerk_id=f"test_stranger_{uuid_module.uuid4().hex[:12]}",
        email=f"stranger-{uuid_module.uuid4().hex[:12]}@clawdi.local",
        name="Stranger",
    )
    db_session.add(stranger)
    await db_session.flush()
    db_session.add(
        CompanionApproval(
            user_id=stranger.id,
            companion_key="filebrowser",
            verb="delete_path",
            canonical_action_sha256=canonical_action_hash(
                companion_key="filebrowser",
                verb="delete_path",
                arguments={"path": "/stranger"},
                tenant_id=stranger.id,
                agent_id=None,
            ),
            arguments={"path": "/stranger"},
            expires_at=datetime.now(UTC) + timedelta(minutes=15),
        )
    )
    await db_session.commit()
    auth = AuthContext(user=seed_user)
    await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    response = await client.get(
        "/v1/companions/approvals",
        params={
            "user_id": str(stranger.id),
            "tenant_id": str(stranger.id),
            "target_user_id": str(stranger.id),
            "environment_id": str(stranger.id),
            "limit": 200,
        },
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items, "the caller's own pending approval was not returned"
    returned = {item["arguments_preview"].get("path") for item in items}
    assert "/stranger" not in returned


def test_no_companion_route_accepts_a_tenant_identifier_parameter() -> None:
    """The structural half of the scoping claim, asserted by reflection.

    The behavioural test above shows today's handlers ignore a supplied identifier. This shows
    there is no parameter through which one could arrive -- the same shape
    ``test_companion_policy.py`` uses to pin "agent text cannot reach the policy decision", and
    the reason a future refactor that adds a convenient ``user_id=`` filter fails here first.
    """
    forbidden = {"user_id", "tenant_id", "target_user_id", "owner_user_id", "clerk_id"}
    for route in companions_routes.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        names = set(inspect.signature(endpoint).parameters)
        assert not (names & forbidden), f"{endpoint.__name__} accepts {names & forbidden}"


async def test_the_approval_decisions_reject_a_cli_credential(
    db_session, seed_user, cli_client
) -> None:
    """The approval surface is outside every channel the agent can reach (T-5-41).

    An agent authenticates with a CLI key. If that key could approve, the gate would be a
    suggestion the agent can grant itself -- which is the failure D-09 puts the decision in the
    dashboard to prevent.
    """
    await _ready_tenant(db_session, seed_user, prefix="cliauth")
    auth = AuthContext(user=seed_user)
    await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    rows = await _approvals(db_session, seed_user)
    approval_id = rows[0].id

    listed = await cli_client.get("/v1/companions/approvals")
    approved = await cli_client.post(f"/v1/companions/approvals/{approval_id}/approve")
    denied = await cli_client.post(f"/v1/companions/approvals/{approval_id}/deny")

    assert listed.status_code == 403
    assert approved.status_code == 403
    assert denied.status_code == 403

    fresh = await _approvals(db_session, seed_user)
    assert fresh[0].status == STATUS_ISSUED


async def test_the_listing_returns_no_continuation_and_no_signing_material(
    db_session, seed_user, client
) -> None:
    """Responses are built field by field, so an internal column cannot reach the dashboard.

    The continuation reaches no column, which is the strongest form of this: there is nothing for a
    serializer to leak. The action hash IS stored and is deliberately absent from the response --
    it is the correlation key the redemption turns on, not something a reader needs.

    **Widened by WR-01.** This assertion passed while the defect shipped, because it checked only
    the ``/approvals`` listing body -- the one surface the token never reached. The two
    ``error_text`` columns and both activity endpoints are asserted here too, because the narrow
    version of this test is what let four docstrings claim a guarantee the code did not hold.
    """
    await _ready_tenant(db_session, seed_user, prefix="noleak")
    auth = AuthContext(user=seed_user)
    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)

    response = await client.get("/v1/companions/approvals")
    assert response.status_code == 200, response.text
    body = response.text
    item = response.json()["items"][0]

    assert set(item) == {
        "id",
        "tool",
        "tool_class",
        "companion_key",
        "verb",
        "arguments_preview",
        "resolved_target_count",
        "remaining_budget",
        "status",
        "expires_at",
        "decided_at",
        "created_at",
    }
    assert item["tool"] == DELETE_TOOL
    assert item["tool_class"] == "destroy"
    assert "canonical_action_sha256" not in body
    assert "continuation" not in body
    assert APPROVAL_CONTINUATION_PREFIX not in body
    # No module-level serialization shortcut: a field-by-field construction is what keeps a column
    # added later from arriving on the dashboard without anyone choosing to put it there.
    source = inspect.getsource(companions_routes)
    assert "from_orm" not in source
    assert "model_validate(" not in source

    # The two columns and the two endpoints the narrow version of this test never looked at.
    calls = await _tool_calls(db_session, seed_user)
    assert calls
    for row in calls:
        assert APPROVAL_CONTINUATION_PREFIX not in (row.error_text or "")
        assert continuation not in (row.error_text or "")
    for refusal in await _refusal_dispatch_rows(db_session, seed_user, verb="delete_path"):
        assert APPROVAL_CONTINUATION_PREFIX not in (refusal.error_text or "")
        assert continuation not in (refusal.error_text or "")

    activity = await client.get("/v1/companions/activity")
    assert activity.status_code == 200, activity.text
    assert APPROVAL_CONTINUATION_PREFIX not in activity.text
    assert continuation not in activity.text

    detail = await client.get(f"/v1/companions/activity/{calls[0].id}")
    assert detail.status_code == 200, detail.text
    assert APPROVAL_CONTINUATION_PREFIX not in detail.text
    assert continuation not in detail.text


async def test_the_listing_is_bounded_and_ordered_deterministically(
    db_session, seed_user, client
) -> None:
    """The same bound the audit route uses, and a total order so paging cannot repeat or skip.

    Creation time alone is not a total order -- two approvals minted in the same millisecond tie --
    so the row identifier is the tiebreaker, exactly as ``routes/audit.py`` does.
    """
    await _ready_tenant(db_session, seed_user, prefix="bounded")
    moment = datetime.now(UTC)
    for index in range(3):
        db_session.add(
            CompanionApproval(
                user_id=seed_user.id,
                companion_key="filebrowser",
                verb="delete_path",
                canonical_action_sha256=canonical_action_hash(
                    companion_key="filebrowser",
                    verb="delete_path",
                    arguments={"path": f"/bounded-{index}"},
                    tenant_id=seed_user.id,
                    agent_id=None,
                ),
                arguments={"path": f"/bounded-{index}"},
                expires_at=moment + timedelta(minutes=15),
            )
        )
    await db_session.commit()

    signature = inspect.signature(companions_routes.list_companion_approvals)
    limit = signature.parameters["limit"].default
    assert limit.default == 50
    assert limit.metadata or True  # Query() carries its own bounds; asserted behaviourally below

    over = await client.get("/v1/companions/approvals", params={"limit": 201})
    under = await client.get("/v1/companions/approvals", params={"limit": 0})
    bounded = await client.get("/v1/companions/approvals", params={"limit": 2})

    assert over.status_code == 422
    assert under.status_code == 422
    assert bounded.status_code == 200
    assert len(bounded.json()["items"]) == 2


def test_the_companion_router_registers_the_listing_and_both_decisions() -> None:
    """Three operations, published on the canonical ``/v1`` surface.

    Asserted over the OpenAPI schema rather than ``app.routes``, following the precedent
    ``test_usage_events.py::test_ingest_route_is_published_on_the_canonical_v1_surface`` set: this
    FastAPI version stores includes as lazy ``_IncludedRouter`` wrappers with no ``.path``, so an
    ``app.routes`` walk silently yields nothing and passes a weaker assertion for free. The schema
    is also the contract the dashboard's client is generated from.
    """
    from app.main import app

    paths = sorted(path for path in app.openapi()["paths"] if "companions" in path)

    assert len(paths) >= 3, paths
    assert "/v1/companions/approvals" in paths
    assert any(path.endswith("/approve") for path in paths)
    assert any(path.endswith("/deny") for path in paths)


async def test_the_approvals_surface_has_no_hidden_api_alias(client) -> None:
    """Canonical-only, asserted by asking rather than by reading the mount table.

    Most routers in ``app/main.py`` mount at ``/v1`` AND at a hidden ``include_in_schema=False``
    ``/api`` alias for binaries shipped before ``/v1`` was canonical. This contract has no released
    consumers, and it is the decision surface for authorizing writes inside a tenant's filesystem
    -- the last route in the backend that should acquire an undocumented second path.

    A live request is the right assertion because the alias is hidden BY DEFINITION: it is absent
    from the OpenAPI schema whether or not it is mounted, so a schema check cannot tell the two
    apart and would pass with the alias present.
    """
    canonical = await client.get("/v1/companions/approvals")
    aliased = await client.get("/api/companions/approvals")

    assert canonical.status_code == 200
    assert aliased.status_code == 404


# ---------------------------------------------------------------------------
# The refusal never reaches a companion, and a redeemed call never carries the token onward.
# ---------------------------------------------------------------------------


async def test_the_continuation_is_not_forwarded_to_the_companion(
    db_session, seed_user, engine, companion_stub
) -> None:
    """A bearer token must not be signed into the envelope or written to the dispatch row.

    ``dry_run`` and ``approval`` are façade CONTROL parameters. Forwarding either to the companion
    would put a query parameter on the wire that the registry's path template never declared, and
    for the continuation it would additionally persist a redeemable credential in a table the
    activity log renders.
    """
    environment_id = await _ready_tenant(db_session, seed_user, prefix="noforward")
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    await _approve(db_session, rows[0].id, seed_user)

    await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": DOOMED_PATH, "approval": continuation},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _drive_executor_once(sessionmaker, environment_id=environment_id, stub=companion_stub),
    )

    assert len(companion_stub.requests) == 1
    recorded = companion_stub.requests[0]
    assert set(recorded.query) == {"path"}
    assert continuation not in recorded.path

    async with sessionmaker() as check:
        row = (
            await check.execute(
                select(CompanionDispatchCall).where(
                    CompanionDispatchCall.environment_id == environment_id,
                    CompanionDispatchCall.verb == "delete_path",
                    CompanionDispatchCall.status != "failed",
                )
            )
        ).scalar_one()
        assert set(row.arguments or {}) == {"path"}

    await _drop_dispatch_rows(sessionmaker, environment_id)


async def test_a_refusal_that_carries_a_continuation_still_names_the_policy_reason(
    db_session, seed_user
) -> None:
    """The approval refusal does not swallow WHY approval was needed.

    ``class_default`` and ``explicit_policy_row`` are different situations for a tenant reading the
    activity log: one is "nobody has decided about this tool yet", the other is "you chose ask".
    Dropping the policy reason to make room for the approval reason would lose that distinction.
    """
    await _ready_tenant(db_session, seed_user, prefix="policyreason")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    text = asked["content"][0]["text"]
    assert REASON_CLASS_DEFAULT in text
    assert REASON_APPROVAL_REQUIRED in text


async def test_an_explicit_deny_row_never_reaches_the_approval_branch(
    db_session, seed_user, companion_stub
) -> None:
    """``deny`` is not ``ask``: a denied tool issues no approval row to be decided.

    Issuing one would put a tool the tenant switched OFF in front of them as something to approve,
    which turns an explicit refusal into a prompt -- the exact shape D-06 refuses for destroy.
    """
    await _ready_tenant(db_session, seed_user, prefix="denyrow")
    db_session.add(
        CompanionToolPolicy(
            user_id=seed_user.id, companion_key="filebrowser", verb="delete_path", mode="deny"
        )
    )
    await db_session.commit()
    auth = AuthContext(user=seed_user)

    refused = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    assert refused["isError"] is True
    assert APPROVAL_CONTINUATION_PREFIX not in refused["content"][0]["text"]
    assert await _approvals(db_session, seed_user) == []
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# WR-01: the continuation reaches the AGENT and reaches no column.
# ---------------------------------------------------------------------------


async def _tool_calls(db_session, seed_user) -> list[CompanionToolCall]:
    """Every provenance row this tenant wrote, oldest first."""
    return list(
        (
            await db_session.execute(
                select(CompanionToolCall)
                .where(CompanionToolCall.user_id == seed_user.id)
                .order_by(CompanionToolCall.created_at)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _refusal_dispatch_rows(
    db_session, seed_user, *, verb: str
) -> list[CompanionDispatchCall]:
    """The REFUSAL half of the queue table -- the rows ``_dispatch_rows`` deliberately filters out.

    ``status == "failed"`` is how ``record_refusal`` journals an attempt that never minted, and its
    ``error_text`` is the second column WR-01 found the bearer continuation sitting in.
    """
    rows = (
        (
            await db_session.execute(
                select(CompanionDispatchCall)
                .where(CompanionDispatchCall.user_id == seed_user.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return [row for row in rows if row.verb == verb and row.status == "failed"]


async def _deny(db_session, approval_id, seed_user) -> None:
    """Deny through the SHIPPED route handler, for the same reason ``_approve`` does."""
    from httpx import ASGITransport

    from app.core.auth import get_auth, get_auth_short_session
    from app.core.database import get_session
    from app.main import app

    async def _override_get_session():
        yield db_session

    async def _override_get_auth() -> AuthContext:
        return AuthContext(user=seed_user)

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    app.dependency_overrides[get_auth_short_session] = _override_get_auth
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(f"/v1/companions/approvals/{approval_id}/deny")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == STATUS_DENIED
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def test_a_pending_approval_hands_the_agent_a_continuation_and_writes_it_to_no_column(
    db_session, seed_user, client
) -> None:
    """The refusal the agent reads and the refusal the log keeps are two different strings (WR-01).

    The continuation is a BEARER value: whoever holds it can redeem the action once the owner has
    approved it. ``error_text`` is a queryable column on two tables and is returned verbatim by
    ``GET /v1/companions/activity`` and ``/activity/{id}``, so a token that reaches it travels with
    every data export, every support read and every database snapshot taken inside its TTL.

    Four surfaces are asserted rather than one, because the committed assertion checked only the
    ``/approvals`` listing body -- which is why four docstrings could claim the token was never
    persisted while it was sitting in two columns.
    """
    await _ready_tenant(db_session, seed_user, prefix="nolog")
    auth = AuthContext(user=seed_user)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    # The agent still gets its token. The fix removes it from the log, not from the answer.
    assert asked["isError"] is True
    continuation = _continuation_from(asked)
    assert continuation

    calls = await _tool_calls(db_session, seed_user)
    assert len(calls) == 1
    logged = calls[0].error_text or ""
    assert APPROVAL_CONTINUATION_PREFIX not in logged
    assert continuation not in logged
    # What the log DOES keep: both machine reasons, and the join the retry follows.
    assert REASON_APPROVAL_REQUIRED in logged
    assert REASON_CLASS_DEFAULT in logged
    assert calls[0].approval_id is not None

    refusals = await _refusal_dispatch_rows(db_session, seed_user, verb="delete_path")
    assert len(refusals) == 1
    queued = refusals[0].error_text or ""
    assert APPROVAL_CONTINUATION_PREFIX not in queued
    assert continuation not in queued

    listing = await client.get("/v1/companions/activity")
    assert listing.status_code == 200, listing.text
    assert APPROVAL_CONTINUATION_PREFIX not in listing.text
    assert continuation not in listing.text

    detail = await client.get(f"/v1/companions/activity/{calls[0].id}")
    assert detail.status_code == 200, detail.text
    assert APPROVAL_CONTINUATION_PREFIX not in detail.text
    assert continuation not in detail.text


async def test_the_continuation_the_agent_received_still_redeems_after_approval(
    db_session, seed_user, engine, companion_stub
) -> None:
    """Splitting the two strings must not hand the agent an EMPTY continuation (T-05-11-05).

    The failure this guards is silent: a refusal that still reads like a refusal, a log that is
    now clean, and an approved action that can never be executed because the token the agent was
    told to retry with was built from the logged string rather than the returned one.
    """
    environment_id = await _ready_tenant(db_session, seed_user, prefix="redeems")
    auth = AuthContext(user=seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    asked = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    continuation = _continuation_from(asked)
    rows = await _approvals(db_session, seed_user)
    await _approve(db_session, rows[0].id, seed_user)

    redeemed, _ = await asyncio.gather(
        call_companion_tool(
            DELETE_TOOL,
            {"path": DOOMED_PATH, "approval": continuation},
            auth=auth,
            db=db_session,
            deadline_seconds=5,
        ),
        _drive_executor_once(sessionmaker, environment_id=environment_id, stub=companion_stub),
    )

    assert redeemed.get("isError") is not True, redeemed
    assert len(companion_stub.requests) == 1
    assert companion_stub.requests[0].method == "DELETE"

    await _drop_dispatch_rows(sessionmaker, environment_id)


async def test_a_refusal_with_no_continuation_logs_exactly_what_it_returned(
    db_session, seed_user
) -> None:
    """With no token to strip, the two strings are the SAME string.

    The split is surgical, not a second message format. A denied refusal carries no continuation,
    so the log keeps the agent's answer verbatim -- if these two ever diverged here, the logged
    text would have become an independently authored surface that nobody reads.
    """
    await _ready_tenant(db_session, seed_user, prefix="denylog")
    auth = AuthContext(user=seed_user)

    await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )
    rows = await _approvals(db_session, seed_user)
    await _deny(db_session, rows[0].id, seed_user)

    refused = await call_companion_tool(
        DELETE_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session, deadline_seconds=0
    )

    text = refused["content"][0]["text"]
    assert REASON_APPROVAL_DENIED in text
    assert APPROVAL_CONTINUATION_PREFIX not in text

    calls = await _tool_calls(db_session, seed_user)
    assert len(calls) == 2
    logged = calls[-1].error_text
    assert logged is not None
    # ``_error`` wraps the message in the MCP error envelope; underneath that wrapper the string
    # the agent read and the string the log kept are one and the same.
    assert text == f"Error: {logged}"
