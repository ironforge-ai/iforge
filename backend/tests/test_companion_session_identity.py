"""The session leg of the provenance chain: a real conversation identity, end to end (Gap 1).

``05-VERIFICATION.md`` Gap 1 found ``companion_tool_calls.session_id`` permanently NULL. The column
was threaded with care through twenty call sites, every consumer handled null gracefully and the
dashboard rendered "not recorded" rather than fabricating -- but the single production caller,
``_call_clawdi_mcp_tool``, supplied nothing, and the transport carried nothing to supply. So
TOOL-07's "queryable per tenant AND per session" clause was half unimplementable, and the
prompting-content ledger keyed on ``session_id or ""`` collapsed to one bucket per tenant.

This module is the phase's gap-closure TRACER. The first case drives ONE real tool call through
every layer Gap 1 touches -- the HTTP transport, the façade pipeline, the dispatch queue, the
executor stand-in, the committed companion stub, the provenance row and the activity query -- and
asserts the id the ``initialize`` response minted is the id the row carries.

**The bound is restated here as a literal rather than imported from the module under test.**
``SESSION_ID_RE`` below is an independent statement of what an acceptable session id looks like. A
test that imported ``MCP_SESSION_ID_PATTERN`` would assert "the constant equals itself", which
moves silently when somebody widens the constant; this one goes red instead. The ceiling of 128 is
not arbitrary -- it is ``_SESSION_ID_LENGTH`` in ``app/models/companion_session_budget.py`` and the
``String(128)`` width of ``companion_tool_calls.session_id``. An id the column cannot hold must
never be stored, so the outside-in cases below drive a 4-character and a 200-character value and
assert both record NULL rather than being truncated or refused.

**A malformed value is ABSENT, never a refusal.** Refusing the call would let one client's header
bug break every tool call the tenant makes; truncating would invent an identity. Degrading to the
NULL the column already tolerated is the only option that costs attribution and nothing else.

**GC-01 is asserted here too, and it is the other half of the same fact.** Giving
``companion_tool_calls.session_id`` a real value was right for ATTRIBUTION and wrong for the CAP:
the value is a client-supplied header, so once the per-session budget was keyed on it alone an
agent reset its own ceiling by changing a string. The final section drives TWO SERVER-MINTED ids
through this same route against ``per_session_calls = 1`` and asserts they share one ceiling --
minted rather than invented, because ``initialize`` is unmetered and a *verified* id is therefore a
rotated id with one more round trip. The property is "a key the agent chooses can only ADD a
ceiling, never remove one", and it is asserted through the route rather than against
``apply_session_debit``'s selector, because a shape test would pass against any future refactor
that reintroduced the hole.

The module runs on the committed lane: the executor stand-in writes its result on an INDEPENDENT
connection while the façade polls, which is invisible to any test whose writes never leave one
transaction.
"""

from __future__ import annotations

import asyncio
import re
import uuid as uuid_module
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import AuthContext, get_auth, get_auth_short_session
from app.core.companion_caps import REASON_SESSION_BUDGET_EXHAUSTED
from app.core.database import get_session
from app.main import app
from app.models.api_key import ApiKey
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_session_budget import (
    SESSION_BUDGET_LIFETIME_SECONDS,
    SESSION_BUDGET_WINDOW_SECONDS,
    CompanionCapPolicy,
    CompanionSessionBudget,
)
from app.models.companion_tool_call import CallDecision, CompanionToolCall
from app.models.hosted_runtime import HostedRuntimeState
from app.services.companion_facade import budget_window, reset_companion_tools_cache
from app.services.companion_provenance import reset_content_ledger
from app.services.companion_sanitize import UNTRUSTED_CLOSE_PREFIX, UNTRUSTED_OPEN_PREFIX
from tests.companion_stub import CompanionStub
from tests.hosted_runtime_fixtures import deploy_companion_state
from tests.test_companion_tracer import _drive_executor_once

pytestmark = pytest.mark.committed_db

MCP_PATH = "/v1/mcp/clawdi"
ACTIVITY_PATH = "/v1/companions/activity"
SESSION_HEADER = "Mcp-Session-Id"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

TRACER_TOOL = "filebrowser__list_dir"
# A verb the registry does not carry. It reaches `call_companion_tool`, is refused there as an
# unknown tool, and writes its honest provenance row WITHOUT needing a deployment, a dispatch or
# an executor -- which is exactly what the session-plumbing cases below want to assert against.
UNREGISTERED_TOOL = "filebrowser__shred_everything"

# Well-formed, and nothing ever mints them: a value the server would have to have been TOLD.
SESSION_A = "sess-aaaaaaaaaaaaaaaaaaaa"
SESSION_B = "sess-bbbbbbbbbbbbbbbbbbbb"
TOO_SHORT = "abcd"
TOO_LONG = "z" * 200


@pytest.fixture(autouse=True)
def _clear_companion_process_state() -> Iterator[None]:
    reset_companion_tools_cache()
    reset_content_ledger()
    yield
    reset_companion_tools_cache()
    reset_content_ledger()


# ---------------------------------------------------------------------------
# Harness.
# ---------------------------------------------------------------------------


def _rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


def _tools_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _rpc("tools/call", {"name": name, "arguments": arguments})


@contextmanager
def _signed_in_as(
    user, sessionmaker: async_sessionmaker[AsyncSession], *, as_agent: bool = False
) -> Iterator[None]:
    """Install one tenant identity for the duration of a block.

    Two identities are needed across this file and the split is the system's, not the harness's.
    ``as_agent=True`` carries an ``ApiKey``, which is what an AGENT and the runtime's own
    dispatch-claim route authenticate with. ``as_agent=False`` is the browser: ``require_web_auth``
    REFUSES a context advertising CLI auth (``is_cli`` is ``api_key is not None``), because D-09
    put the activity log in front of a person rather than in front of the agent whose refusals it
    records. A single identity that satisfied both would mean one of those gates was not doing its
    job.
    """

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as runtime_db:
            yield runtime_db

    async def _override_get_auth() -> AuthContext:
        if as_agent:
            return AuthContext(user=user, api_key=ApiKey(user_id=user.id))
        return AuthContext(user=user)

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    app.dependency_overrides[get_auth_short_session] = _override_get_auth
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_auth, None)
        app.dependency_overrides.pop(get_auth_short_session, None)


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _session_rows(db: AsyncSession, user_id) -> list[CompanionToolCall]:
    rows = await db.execute(
        select(CompanionToolCall)
        .where(CompanionToolCall.user_id == user_id)
        .order_by(CompanionToolCall.created_at, CompanionToolCall.id)
    )
    return list(rows.scalars().all())


async def _cleanup(sessionmaker: async_sessionmaker[AsyncSession], environment_id) -> None:
    async with sessionmaker() as cleanup:
        await cleanup.execute(
            CompanionDispatchCall.__table__.delete().where(
                CompanionDispatchCall.environment_id == environment_id
            )
        )
        await cleanup.execute(
            HostedRuntimeState.__table__.delete().where(
                HostedRuntimeState.environment_id == environment_id
            )
        )
        await cleanup.commit()


async def _refuse_once(
    client: httpx.AsyncClient, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """One `tools/call` that is refused at the registry, so it writes a row and stops.

    The refusal is the point: the session id has already been read off the transport and handed to
    ``call_companion_tool`` by the time the verb is looked up, so the row this writes carries the
    session exactly as an executed call would -- without a deployment or a dispatch to arrange.
    """
    return await client.post(
        MCP_PATH, json=_tools_call(UNREGISTERED_TOOL, {"path": "/"}), headers=headers or {}
    )


async def _drive_a_real_call(
    *,
    db_session: AsyncSession,
    seed_user,
    engine,
    companion_stub: CompanionStub,
) -> tuple[Any, str, dict[str, Any]]:
    """`initialize`, then one REAL `filebrowser__list_dir` echoing the minted id.

    Returns the environment id, the session id the server minted, and the `tools/call` result.
    """
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"sid{uuid_module.uuid4().hex[:6]}"
    )
    companion_auth = companions["filebrowser"]["auth"]
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    with _signed_in_as(seed_user, sessionmaker, as_agent=True):
        async with _client() as client:
            handshake = await client.post(MCP_PATH, json=_rpc("initialize"))
            assert handshake.status_code == 200, handshake.text
            minted = handshake.headers.get(SESSION_HEADER)
            assert minted is not None, "initialize answered with no Mcp-Session-Id header"
            assert SESSION_ID_RE.match(minted), minted

            called, _ = await asyncio.gather(
                client.post(
                    MCP_PATH,
                    json=_tools_call(TRACER_TOOL, {"path": "/"}),
                    headers={SESSION_HEADER: minted},
                ),
                _drive_executor_once(
                    client,
                    environment_id=str(environment_id),
                    companion_auth=companion_auth,
                    stub=companion_stub,
                ),
            )
    assert called.status_code == 200, called.text
    return environment_id, minted, called.json()


# ---------------------------------------------------------------------------
# The tracer: one real call, from the transport to a non-NULL row.
# ---------------------------------------------------------------------------


async def test_a_real_tool_call_records_the_session_the_transport_minted(
    db_session, seed_user, engine, companion_stub
) -> None:
    """Gap 1's headline claim, asserted through the real route rather than around it.

    Nothing here sets ``session_id`` by hand. The value asserted on the row is the value the
    ``initialize`` RESPONSE handed back, which is the only way to tell a real plumbing from a test
    that passes the answer to itself.
    """
    environment_id, minted, body = await _drive_a_real_call(
        db_session=db_session, seed_user=seed_user, engine=engine, companion_stub=companion_stub
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        result = body["result"]
        assert result.get("isError") is not True, result
        text = result["content"][0]["text"]
        assert UNTRUSTED_OPEN_PREFIX in text
        assert UNTRUSTED_CLOSE_PREFIX in text
        assert "notes.md" in text

        async with sessionmaker() as check_db:
            dispatch = (
                await check_db.execute(
                    select(CompanionDispatchCall).where(
                        CompanionDispatchCall.environment_id == environment_id
                    )
                )
            ).scalar_one()
            assert dispatch.status == "completed"

            rows = await _session_rows(check_db, seed_user.id)
            assert len(rows) == 1, [row.decision for row in rows]
            assert rows[0].session_id is not None, "the session leg is still null"
            assert rows[0].session_id == minted
    finally:
        await _cleanup(sessionmaker, environment_id)


async def test_the_activity_log_matches_on_that_session_and_on_no_other(
    db_session, seed_user, engine, companion_stub
) -> None:
    """TOOL-07's per-session clause, exercised rather than merely present.

    The negative half matters as much as the positive one: a filter that matched everything would
    satisfy the first assertion alone.
    """
    environment_id, minted, _ = await _drive_a_real_call(
        db_session=db_session, seed_user=seed_user, engine=engine, companion_stub=companion_stub
    )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with _signed_in_as(seed_user, sessionmaker):
            async with _client() as client:
                matched = await client.get(ACTIVITY_PATH, params={"session_id": minted})
                assert matched.status_code == 200, matched.text
                items = matched.json()["items"]
                assert len(items) == 1, items
                assert items[0]["session_id"] == minted

                # `call_jti` lives on the detail route, so the join back to the dispatch row the
                # stand-in actually drove is asserted there rather than inferred from the list.
                detail = await client.get(f"{ACTIVITY_PATH}/{items[0]['id']}")
                assert detail.status_code == 200, detail.text

                other = await client.get(ACTIVITY_PATH, params={"session_id": SESSION_B})
                assert other.status_code == 200, other.text
                assert other.json()["items"] == []

        async with sessionmaker() as check_db:
            dispatch = (
                await check_db.execute(
                    select(CompanionDispatchCall).where(
                        CompanionDispatchCall.environment_id == environment_id
                    )
                )
            ).scalar_one()
        assert detail.json()["call_jti"] == dispatch.call_jti
    finally:
        await _cleanup(sessionmaker, environment_id)


# ---------------------------------------------------------------------------
# The mint, and what an echoed value does.
# ---------------------------------------------------------------------------


async def test_initialize_mints_a_bounded_id_and_a_different_one_each_time(
    db_session, seed_user, engine
) -> None:
    """Two conversations for one tenant get two identities.

    A per-REQUEST mint would close the NULL and leave the cross-attribution wide open; a
    per-TENANT mint would be the same bucket this gap exists to split. The mint happens once per
    ``initialize``, which is what makes "same conversation, same id" and "different conversations,
    different ids" both true.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            first = await client.post(MCP_PATH, json=_rpc("initialize"))
            second = await client.post(MCP_PATH, json=_rpc("initialize"))

    assert first.status_code == 200 and second.status_code == 200
    one = first.headers.get(SESSION_HEADER)
    two = second.headers.get(SESSION_HEADER)
    assert one is not None and two is not None
    assert SESSION_ID_RE.match(one), one
    assert SESSION_ID_RE.match(two), two
    assert one != two


async def test_initialize_reuses_a_session_the_client_already_holds(
    db_session, seed_user, engine
) -> None:
    """A client that re-initializes inside one conversation keeps its identity.

    Minting a second id here would split one conversation's provenance across two buckets, which
    is the same failure as sharing one bucket across two conversations, read from the other side.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            again = await client.post(
                MCP_PATH, json=_rpc("initialize"), headers={SESSION_HEADER: SESSION_A}
            )

    assert again.status_code == 200, again.text
    assert again.headers.get(SESSION_HEADER) == SESSION_A


async def test_two_calls_echoing_one_id_share_it_and_two_ids_stay_apart(
    db_session, seed_user, engine
) -> None:
    """The stability contract, asserted on the rows rather than on the header."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            await _refuse_once(client, headers={SESSION_HEADER: SESSION_A})
            await _refuse_once(client, headers={SESSION_HEADER: SESSION_A})
            await _refuse_once(client, headers={SESSION_HEADER: SESSION_B})

    async with sessionmaker() as check_db:
        recorded = [row.session_id for row in await _session_rows(check_db, seed_user.id)]

    assert recorded == [SESSION_A, SESSION_A, SESSION_B], recorded


async def test_a_call_with_no_session_header_records_null(db_session, seed_user, engine) -> None:
    """The ordinary case for any client that never ran an ``initialize``.

    NULL is the honest value: no conversation identity was observed, so none is claimed.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            response = await _refuse_once(client)

    assert response.status_code == 200, response.text
    async with sessionmaker() as check_db:
        rows = await _session_rows(check_db, seed_user.id)
    assert len(rows) == 1
    assert rows[0].session_id is None


@pytest.mark.parametrize("malformed", [TOO_SHORT, TOO_LONG, "not a session id", "x" * 15])
async def test_a_malformed_session_header_is_absent_rather_than_refused_or_truncated(
    db_session, seed_user, engine, malformed: str
) -> None:
    """A value outside the bound records NULL, and the call still runs.

    Three outcomes were available and two of them are wrong. Refusing the call would let one
    client's header bug break every tool call the tenant makes. Truncating a 200-character value
    to the column width would invent an identity nobody issued and then let the activity log
    present it as observed fact. Treating it as absent costs attribution and nothing else.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            response = await _refuse_once(client, headers={SESSION_HEADER: malformed})

    assert response.status_code == 200, response.text
    # The refusal the registry gives, not a transport-level rejection: the call was RUN.
    assert response.json()["result"]["isError"] is True

    async with sessionmaker() as check_db:
        rows = await _session_rows(check_db, seed_user.id)
    assert len(rows) == 1
    assert rows[0].session_id is None, f"{malformed!r} was stored"


async def test_a_batched_initialize_mints_one_id_for_the_whole_batch(
    db_session, seed_user, engine
) -> None:
    """One batch is one client speaking once, so it gets one identity.

    JSON-RPC batching is a transport detail. A batch that minted per item would hand one
    conversation several identities for no reason a reader of the activity log could explain.
    """
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    with _signed_in_as(seed_user, sessionmaker):
        async with _client() as client:
            batched = await client.post(
                MCP_PATH, json=[_rpc("initialize"), _tools_call(UNREGISTERED_TOOL, {"path": "/"})]
            )

    assert batched.status_code == 200, batched.text
    minted = batched.headers.get(SESSION_HEADER)
    assert minted is not None
    assert SESSION_ID_RE.match(minted), minted
    assert len(batched.json()) == 2


# ---------------------------------------------------------------------------
# GC-01: a key the agent chooses can only ADD a ceiling, never remove one.
# ---------------------------------------------------------------------------


async def _tighten_to_one_call(db_session, seed_user) -> None:
    """A cap row whose ``per_session_calls`` is 1 and whose other five do not interfere.

    Written as an explicit vector rather than by mutating ``DEFAULT_CAPS``: a refusal below has to
    be THIS number, and a test that leaned on the shipped defaults would change meaning the next
    time A5 is retuned.
    """
    db_session.add(
        CompanionCapPolicy(
            user_id=seed_user.id,
            companion_key="filebrowser",
            per_call_calls=1,
            per_call_records=50,
            per_call_bytes=1_048_576,
            per_session_calls=1,
            per_session_records=500,
            per_session_bytes=209_715_200,
        )
    )
    await db_session.commit()


async def _minted_session_id(client: httpx.AsyncClient) -> str:
    """One ``initialize``, and the id the SERVER chose for it."""
    handshake = await client.post(MCP_PATH, json=_rpc("initialize"))
    assert handshake.status_code == 200, handshake.text
    minted = handshake.headers.get(SESSION_HEADER)
    assert minted is not None, "initialize answered with no Mcp-Session-Id header"
    assert SESSION_ID_RE.match(minted), minted
    return minted


async def test_two_minted_session_ids_cannot_together_outspend_one_window(
    db_session, seed_user, engine, companion_stub
) -> None:
    """GC-01, asserted as the PROPERTY rather than as the selector's SQL shape.

    Both ids here were minted by ``initialize``, so this case cannot be dismissed as a forgery that
    verifying the header would have caught: nothing meters ``initialize``, so an agent wanting a
    fresh budget simply asks for a fresh id. What must hold is that asking does not help --
    ``per_session_calls`` of 1 means ONE call for this tenant, this Agent, this companion and this
    hour, whatever strings the client puts on the wire.

    Driven through ``POST /v1/mcp/clawdi`` rather than against ``apply_session_debit`` directly. A
    shape test over the selector would pass against any future refactor that reintroduced the hole,
    which is how GC-01 got in.
    """
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"gc1a{uuid_module.uuid4().hex[:6]}"
    )
    companion_auth = companions["filebrowser"]["auth"]
    await _tighten_to_one_call(db_session, seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with _signed_in_as(seed_user, sessionmaker, as_agent=True):
            async with _client() as client:
                first_id = await _minted_session_id(client)
                second_id = await _minted_session_id(client)
                assert first_id != second_id

                allowed, _ = await asyncio.gather(
                    client.post(
                        MCP_PATH,
                        json=_tools_call(TRACER_TOOL, {"path": "/"}),
                        headers={SESSION_HEADER: first_id},
                    ),
                    _drive_executor_once(
                        client,
                        environment_id=str(environment_id),
                        companion_auth=companion_auth,
                        stub=companion_stub,
                    ),
                )
                assert allowed.status_code == 200, allowed.text
                assert allowed.json()["result"].get("isError") is not True, allowed.text

                refused = await client.post(
                    MCP_PATH,
                    json=_tools_call(TRACER_TOOL, {"path": "/"}),
                    headers={SESSION_HEADER: second_id},
                )

        assert refused.status_code == 200, refused.text
        result = refused.json()["result"]
        assert result.get("isError") is True, result
        text = result["content"][0]["text"]
        assert "blast-radius cap" in text, text
        assert REASON_SESSION_BUDGET_EXHAUSTED in text, text

        async with sessionmaker() as check_db:
            rows = await _session_rows(check_db, seed_user.id)
        assert [row.decision for row in rows] == [
            CallDecision.ALLOWED,
            CallDecision.REFUSED_CAP,
        ], [row.decision for row in rows]
        assert rows[0].session_id == first_id
        assert rows[1].session_id == second_id
    finally:
        await _cleanup(sessionmaker, environment_id)


async def test_a_second_call_echoing_the_same_id_is_refused_for_the_same_reason(
    db_session, seed_user, engine, companion_stub
) -> None:
    """The control for the case above: an implementation that refused only on a session CHANGE.

    Without this, a fix that compared the incoming id against the last one seen would pass the
    two-id case while metering nothing at all.
    """
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"gc1b{uuid_module.uuid4().hex[:6]}"
    )
    companion_auth = companions["filebrowser"]["auth"]
    await _tighten_to_one_call(db_session, seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with _signed_in_as(seed_user, sessionmaker, as_agent=True):
            async with _client() as client:
                minted = await _minted_session_id(client)

                allowed, _ = await asyncio.gather(
                    client.post(
                        MCP_PATH,
                        json=_tools_call(TRACER_TOOL, {"path": "/"}),
                        headers={SESSION_HEADER: minted},
                    ),
                    _drive_executor_once(
                        client,
                        environment_id=str(environment_id),
                        companion_auth=companion_auth,
                        stub=companion_stub,
                    ),
                )
                assert allowed.status_code == 200, allowed.text
                assert allowed.json()["result"].get("isError") is not True, allowed.text

                refused = await client.post(
                    MCP_PATH,
                    json=_tools_call(TRACER_TOOL, {"path": "/"}),
                    headers={SESSION_HEADER: minted},
                )

        assert refused.status_code == 200, refused.text
        result = refused.json()["result"]
        assert result.get("isError") is True, result
        assert REASON_SESSION_BUDGET_EXHAUSTED in result["content"][0]["text"]
    finally:
        await _cleanup(sessionmaker, environment_id)


async def test_a_first_call_under_an_unseen_session_id_is_still_allowed(
    db_session, seed_user, engine, companion_stub
) -> None:
    """The fix is a SECOND ceiling, not a blanket refusal.

    A brand-new conversation on a tenant that has spent nothing must still get its first call. This
    is the case that goes red if the two-ledger debit ever becomes "refuse anything carrying a
    session id", which would satisfy the two cases above and break every real client.
    """
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"gc1c{uuid_module.uuid4().hex[:6]}"
    )
    companion_auth = companions["filebrowser"]["auth"]
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        with _signed_in_as(seed_user, sessionmaker, as_agent=True):
            async with _client() as client:
                minted = await _minted_session_id(client)
                allowed, _ = await asyncio.gather(
                    client.post(
                        MCP_PATH,
                        json=_tools_call(TRACER_TOOL, {"path": "/"}),
                        headers={SESSION_HEADER: minted},
                    ),
                    _drive_executor_once(
                        client,
                        environment_id=str(environment_id),
                        companion_auth=companion_auth,
                        stub=companion_stub,
                    ),
                )

        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["result"].get("isError") is not True, allowed.text
        async with sessionmaker() as check_db:
            rows = await _session_rows(check_db, seed_user.id)
        assert [row.decision for row in rows] == [CallDecision.ALLOWED]
        assert rows[0].session_id == minted
    finally:
        await _cleanup(sessionmaker, environment_id)


# ---------------------------------------------------------------------------
# GC2-03: a conversation older than its session budget's lifetime is not walled.
# ---------------------------------------------------------------------------


async def test_a_conversation_older_than_the_session_lifetime_is_allowed_again(
    db_session, seed_user, engine, companion_stub
) -> None:
    """GC2-03 as the PROPERTY, driven through the route a real agent uses.

    ``iforge mcp`` mints one session id per process and holds it for the life of that process --
    hours to days, recorded as LOCKED by 05-10 (WINDOWS #179). The session ledger 05-15 shipped
    carried the same ``per_session_calls`` ceiling as the hourly window and no time term at all, so
    an operator dogfooding ``filebrowser`` at thirty calls an hour met a permanent wall around hour
    seven: the 201st call refused, every call after it refused, the caps card showing thirty of two
    hundred, and neither waiting nor retrying helping. That is not an agent raising a cap; it is a
    cap ending a working day, which is the failure mode a blast-radius cap exists to avoid.

    So: one tenant, ``per_session_calls`` of 1, a server-minted id, and a budget row for that id
    planted a lifetime and a window in the past with its whole budget already spent. The next call
    must SUCCEED. Asserted through ``POST /v1/mcp/clawdi`` rather than against
    ``apply_session_debit``, because a unit test of the debit would pass against a route that never
    reached it.

    The row is read back for the thing "allowed" cannot distinguish: the reset must be a RESET and
    not an increment, so the counter reads 1 -- this call's own debit against a zeroed row -- and
    never the seeded value plus one.
    """
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"gc203{uuid_module.uuid4().hex[:6]}"
    )
    companion_auth = companions["filebrowser"]["auth"]
    await _tighten_to_one_call(db_session, seed_user)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    current_window = budget_window(datetime.now(UTC))
    stale_stamp = current_window - timedelta(
        seconds=SESSION_BUDGET_LIFETIME_SECONDS + SESSION_BUDGET_WINDOW_SECONDS
    )
    try:
        with _signed_in_as(seed_user, sessionmaker, as_agent=True):
            async with _client() as client:
                minted = await _minted_session_id(client)

                # The conversation as it stands at hour seven: its whole budget spent, and opened
                # long enough ago that the budget it spent belongs to a period that has passed.
                db_session.add(
                    CompanionSessionBudget(
                        user_id=seed_user.id,
                        environment_id=environment_id,
                        companion_key="filebrowser",
                        session_id=minted,
                        window_started_at=stale_stamp,
                        calls_consumed=1,
                        records_consumed=0,
                        bytes_consumed=0,
                    )
                )
                await db_session.commit()

                allowed, _ = await asyncio.gather(
                    client.post(
                        MCP_PATH,
                        json=_tools_call(TRACER_TOOL, {"path": "/"}),
                        headers={SESSION_HEADER: minted},
                    ),
                    _drive_executor_once(
                        client,
                        environment_id=str(environment_id),
                        companion_auth=companion_auth,
                        stub=companion_stub,
                    ),
                )

        assert allowed.status_code == 200, allowed.text
        result = allowed.json()["result"]
        assert result.get("isError") is not True, (
            "an aged-out conversation is still walled at its session cap: " + allowed.text
        )

        async with sessionmaker() as check_db:
            rows = await _session_rows(check_db, seed_user.id)
            budget = (
                await check_db.execute(
                    select(CompanionSessionBudget).where(
                        CompanionSessionBudget.user_id == seed_user.id,
                        CompanionSessionBudget.session_id == minted,
                    )
                )
            ).scalar_one()

        assert [row.decision for row in rows] == [CallDecision.ALLOWED], [
            row.decision for row in rows
        ]
        assert rows[0].session_id == minted
        assert budget.calls_consumed == 1, "the stale row was incremented rather than reset"
        assert budget.window_started_at == current_window
    finally:
        await _cleanup(sessionmaker, environment_id)
