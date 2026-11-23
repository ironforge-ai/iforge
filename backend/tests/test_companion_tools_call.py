"""The call-time half of the destroy gate, and the refusals that must not become forwards.

D-06 has two halves, and this file is deliberately the SECOND one. A ``destroy`` verb is absent from
the tool list for a tenant that has not enabled destroy tools -- that half lives in
``backend/tests/test_companion_tools_list.py`` -- AND it is refused by name at call time for that
same tenant, which is this file. Hiding a tool is what an agent can be steered around; the call-time
refusal is what covers a name the agent hallucinated, remembered from an older session, or was fed
by injected content (T-5-18).

**This module never calls ``list_companion_tools``, and that is a rule rather than an accident.**
Every tool name here is a literal written in the test body. A test that sourced the name from the
endpoint under test would pass vacuously the moment the list filter worked -- it would assert "the
verb I was not shown is refused", which is a tautology, not a gate. Phase 4 registered that exact
shape as T-4-19, and the two halves live in two files so the mistake cannot be made by accident in a
later edit. ``05-RESEARCH.md`` §Pitfall 4 is the long-form version.

**Refusals are asserted against the shipped stub's request log, not only against the response.** An
``isError`` result proves what the agent was told; ``companion_stub.requests == []`` proves nothing
reached the tenant's filesystem. Those are different claims, and only the second one is the security
property -- a façade that called the companion and then returned an error would satisfy the first.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.auth import AuthContext
from app.core.companion_policy import REASON_CLASS_DEFAULT, REASON_DESTROY_DISABLED
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import CompanionDestroyEnablement, CompanionToolPolicy
from app.routes import mcp_bridge
from app.routes.mcp_bridge import _call_clawdi_mcp_tool
from app.services import companion_facade
from app.services.companion_facade import (
    _dispatch_arguments,
    call_companion_tool,
    reset_companion_tools_cache,
)
from tests.hosted_runtime_fixtures import deploy_companion_state

# Spelled as literals, here, once. NEVER read from a `tools/list` response -- see the module
# docstring. `filebrowser__delete_path` is the only `destroy`-class verb the registry ships.
DESTROY_TOOL = "filebrowser__delete_path"
MOVE_TOOL = "filebrowser__move_path"
READ_TOOL = "filebrowser__list_dir"
UNREGISTERED_TOOL = "filebrowser__shred_everything"
DOOMED_PATH = "/projects/quarterly-report.md"
MOVE_SOURCE = "/notes.md"
MOVE_DESTINATION = "/archive/notes.md"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


async def _enable_destroy(db_session, seed_user, *, companion_key: str = "filebrowser") -> None:
    db_session.add(
        CompanionDestroyEnablement(
            user_id=seed_user.id,
            companion_key=companion_key,
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=seed_user.id,
        )
    )
    await db_session.commit()


# ---------------------------------------------------------------------------
# The second half of D-06.
# ---------------------------------------------------------------------------


async def test_destroy_verb_refused_by_name(db_session, seed_user, companion_stub) -> None:
    """Naming the hidden verb directly gets a refusal, and nothing reaches the companion.

    The tenant has the companion deployed and has NOT enabled destroy tools. The tool name is a
    literal in this test body, exactly as an agent would produce it from memory or from injected
    content -- the point is that the façade never learns whether the agent was "supposed" to know
    the name, because that question has no bearing on the decision.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="destroycall")
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        DESTROY_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    # The machine reason, not a phrase from the prose. The prose is a human surface that will be
    # rewritten; the reason token is the contract a refusal can be asserted against.
    assert REASON_DESTROY_DISABLED in text, text
    # The security property, stated separately from what the agent was told: a façade that called
    # the companion and then returned an error would satisfy the assertion above and fail this one.
    assert companion_stub.requests == []


async def test_a_policy_row_saying_allow_does_not_reopen_a_disabled_destroy_group(
    db_session, seed_user, companion_stub
) -> None:
    """Precedence at the façade, not only inside the pure function.

    ``test_destroy_enablement_off_beats_a_policy_row_that_says_allow`` asserts the order in
    ``evaluate_policy``. This asserts the caller actually loads both facts and hands them over in
    that order -- a façade that consulted the row and skipped the switch would leave that unit test
    green while shipping an exploitable gate.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="destroyallow")
    db_session.add(
        CompanionToolPolicy(
            user_id=seed_user.id, companion_key="filebrowser", verb="delete_path", mode="allow"
        )
    )
    await db_session.commit()
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        DESTROY_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session
    )

    assert result["isError"] is True
    assert REASON_DESTROY_DISABLED in result["content"][0]["text"]
    assert companion_stub.requests == []


async def test_enabling_destroy_moves_the_refusal_from_the_gate_to_the_class_default(
    db_session, seed_user, companion_stub
) -> None:
    """Enabling changes WHICH refusal happens, and that difference is the gate doing its job.

    Still refused -- `destroy`'s class default is `ask` and the approval flow lands in 05-06 -- but
    refused for a different, later reason. If enabling changed nothing, the switch would be
    decorative; if enabling made the call run, "ask-on-write" would not be the default TOOL-03 asks
    for.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="destroyenabled")
    await _enable_destroy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        DESTROY_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert REASON_DESTROY_DISABLED not in text, text
    assert REASON_CLASS_DEFAULT in text, text
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# A refusal must never become a forward (T-5-05).
# ---------------------------------------------------------------------------


async def test_unknown_tool_name_is_refused_not_forwarded(
    db_session, seed_user, monkeypatch, companion_stub
) -> None:
    """A well-shaped `kind__verb` name with no registry row stops AT the façade.

    The failure this guards against is not "the call succeeds" -- it is the call quietly falling
    through to the Composio connector path, where a name the companion contract never classified
    would be executed by an entirely different authority with entirely different rules.
    """
    invoked: list[str] = []

    async def _recording_connector_call(name, arguments, *, auth):
        invoked.append(name)
        return {"content": [], "isError": False}

    monkeypatch.setattr(mcp_bridge, "_tool_connector_call", _recording_connector_call)
    await deploy_companion_state(db_session, seed_user, machine_prefix="unknowncall")
    auth = AuthContext(user=seed_user)

    result = await _call_clawdi_mcp_tool(
        UNREGISTERED_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session
    )

    assert result["isError"] is True
    assert invoked == [], f"an unregistered companion name reached the connector: {invoked}"
    assert companion_stub.requests == []


# ---------------------------------------------------------------------------
# The cache is a staleness bound, not an exploit window (T-5-19).
# ---------------------------------------------------------------------------


async def test_stale_list_is_not_exploitable(db_session, seed_user, companion_stub) -> None:
    """A tool list that still names the verb does not make the verb callable.

    The snapshot is planted directly into the façade's cache rather than produced by calling the
    list endpoint, for two reasons. It keeps this module's "no `list_companion_tools` call" rule
    intact, and it is the STRONGER construction: the cache is made to claim the verb is available
    regardless of what the real resolver would say, so the only thing standing between the agent and
    the call is the call-time gate itself.

    This is what makes the sixty-second TTL an honest staleness bound. Enforcement that lived in the
    list would leave a tenant who revokes enablement exposed for up to a minute.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="stalelist")
    await _enable_destroy(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    stale_snapshot = [{"name": DESTROY_TOOL, "description": "stale", "inputSchema": {}}]
    companion_facade._companion_tools_cache[seed_user.clerk_id] = (
        time.monotonic() + 10_000.0,
        stale_snapshot,
    )

    # Revoking is deleting the row: the switch has no "disabled" state distinct from absence.
    await db_session.execute(
        CompanionDestroyEnablement.__table__.delete().where(
            CompanionDestroyEnablement.user_id == seed_user.id
        )
    )
    await db_session.commit()

    result = await call_companion_tool(
        DESTROY_TOOL, {"path": DOOMED_PATH}, auth=auth, db=db_session
    )

    assert result["isError"] is True
    assert REASON_DESTROY_DISABLED in result["content"][0]["text"]
    assert companion_stub.requests == []
    # The snapshot is UNTOUCHED: the refusal came from the call-time gate, not from a list refresh
    # that happened to drop the name. If this assertion ever fails, the test above it has started
    # proving something weaker than it claims.
    cached = companion_facade._companion_tools_cache.get(seed_user.clerk_id)
    assert cached is not None and cached[1] == stale_snapshot


# ---------------------------------------------------------------------------
# 05-13 (WR-06a): `filebrowser__move_path` can state where it moves to, and every bound `path`
# carries is applied to `destination` too.
#
# The refusals below are asserted against the shipped stub's request log as well as the response,
# for the reason the module docstring gives: `isError` proves what the agent was told, and
# `companion_stub.requests == []` proves nothing reached the tenant's filesystem.
# ---------------------------------------------------------------------------


async def _move(db_session, seed_user, arguments: dict[str, object]) -> dict[str, object]:
    auth = AuthContext(user=seed_user)
    return await call_companion_tool(
        MOVE_TOOL, arguments, auth=auth, db=db_session, deadline_seconds=0
    )


async def _provenance_row_count(db_session, seed_user) -> int:
    rows = await db_session.execute(
        select(CompanionToolCall.id)
        .where(CompanionToolCall.user_id == seed_user.id)
        .execution_options(populate_existing=True)
    )
    return len(list(rows.scalars().all()))


async def test_a_move_without_a_destination_is_refused_naming_the_missing_argument(
    db_session, seed_user, companion_stub
) -> None:
    """The argument is REQUIRED, so its absence is a refusal rather than an undetermined preview.

    Before this plan the schema admitted only ``path``, so a move could not say where it moves to
    and TOOL-05's "see exactly what would change" was inexpressible for the one verb that most
    needed it.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="movemissing")

    result = await _move(db_session, seed_user, {"path": MOVE_SOURCE})

    assert result["isError"] is True
    assert "destination" in result["content"][0]["text"]
    assert companion_stub.requests == []
    # The refusal is JOURNALLED: exactly one attempt, exactly one row.
    assert await _provenance_row_count(db_session, seed_user) == 1


@pytest.mark.parametrize(
    ("destination", "expected"),
    [
        ("https://elsewhere/x", "companion-relative"),
        ("/archive/https://elsewhere/x", "scheme or authority"),
        ("//elsewhere/x", "scheme or authority"),
        ("archive/notes.md", "companion-relative"),
        ("", "non-empty string"),
        (17, "non-empty string"),
        ("/" + "a" * 2048, "exceeds"),
    ],
    ids=["url", "scheme", "authority", "relative", "empty", "non-string", "too-long"],
)
async def test_a_destination_gets_every_bound_path_already_gets(
    db_session, seed_user, companion_stub, destination: object, expected: str
) -> None:
    """Identical bounds, in the same order, with the same message shapes (T-05-13-01).

    ``destination`` is a new agent-supplied string that reaches the companion. Giving it a weaker
    bound set than ``path`` would make the older argument's hardening decorative.

    The ORDER is part of the claim, and the first two cases are what pin it: a bare URL is refused
    for not being companion-relative (the ``/`` check runs before the scheme check, exactly as it
    does for ``path``), so the scheme case has to be a value that passes the ``/`` check to reach
    the branch it is named for.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="movebound")

    result = await _move(db_session, seed_user, {"path": MOVE_SOURCE, "destination": destination})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "destination" in text, text
    assert expected in text, text
    assert companion_stub.requests == []


async def test_a_move_to_its_own_location_is_refused_rather_than_previewed_as_clean(
    db_session, seed_user, companion_stub
) -> None:
    """The one bound ``path`` does not have. A move to itself changes nothing.

    Previewing it as a clean diff would show an approver a change that is not a change, and the
    approval they granted would then be for an action with no effect.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="moveself")

    result = await _move(db_session, seed_user, {"path": MOVE_SOURCE, "destination": MOVE_SOURCE})

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "destination" in text, text
    assert "changes nothing" in text, text
    assert companion_stub.requests == []


async def test_a_destination_on_a_read_verb_is_an_unaccepted_argument(
    db_session, seed_user, companion_stub
) -> None:
    """The same polarity ``dry_run`` already has: rejected, never silently ignored.

    A read has no second address, so a ``destination`` on one is either a mistake or an attempt to
    reach a verb the agent was not given -- and an ignored argument is the worst of the outcomes,
    because the agent believes it was honoured.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="readdest")
    auth = AuthContext(user=seed_user)

    result = await call_companion_tool(
        READ_TOOL,
        {"path": "/", "destination": MOVE_DESTINATION},
        auth=auth,
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    assert companion_stub.requests == []


def test_the_arguments_that_travel_carry_the_destination_and_neither_control_parameter() -> None:
    """``destination`` is an ACTION argument; ``dry_run`` and ``approval`` are instructions here.

    ``_dispatch_arguments`` strips the two control parameters by name, so the destination reaches
    the companion with no edit -- and this pins that, because a filter that switched to an
    allow-list would drop it silently.
    """
    travelling = _dispatch_arguments(
        {
            "path": MOVE_SOURCE,
            "destination": MOVE_DESTINATION,
            "dry_run": True,
            "approval": "continuation-token",
        }
    )

    assert travelling == {"path": MOVE_SOURCE, "destination": MOVE_DESTINATION}
