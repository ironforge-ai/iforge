"""Blocking a companion's whole tool group, hidden from the list AND refused at the call (APP-05).

A tenant owner blocks one companion's tools as a group. Two things have to be true together:

* **The group disappears from ``tools/list``.** An agent never sees a verb it cannot use.
* **A call by name is refused, at the same enforcement point policy uses.** An agent that fetched
  its tool list before the block, or that simply guesses a name, still cannot invoke a blocked
  group. Filtering the list alone "would make 'disable any group' promise more than it delivers"
  (D-30). The refusal is recorded with its own decision, ``refused_group_disabled``, because
  disabling is a security control and a reader of the activity log has to be able to tell it from
  a policy ``deny``.

The block is tenant state: a row per tenant and companion, mirroring Phase 5's destroy enablement
with the polarity inverted (a row means BLOCKED, because the safe default for an installed
companion is that its tools work). Nothing an agent sends can set or clear it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.core.companion_registry import COMPANION_VERBS
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_tool_call import CALL_DECISIONS, CompanionToolCall
from app.models.companion_tool_policy import CompanionGroupBlock, CompanionToolPolicy
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.services import companion_arguments, companion_facade
from app.services.companion_arguments import argument_contract_for
from app.services.companion_facade import (
    call_companion_tool,
    list_companion_tools,
    reset_companion_tools_cache,
)
from app.services.companion_ratelimit import reset_companion_rate_limiter
from app.services.companion_settings import blocked_companion_keys, set_group_block
from tests.companion_stub import COMPANION_STUB_LISTING
from tests.hosted_runtime_fixtures import deploy_companion_state
from tests.test_postiz_verbs import _deploy_postiz, _platform_minted_auth, _postiz_entry

GROUP_DECISION = "refused_group_disabled"
GROUP_REASON = "companion_group_disabled"

CATALOG_PATH = "/v1/companions/tools"
LIST_POSTS = "postiz__list_posts"
READ_TOOL = "filebrowser__list_dir"
POSTS_WINDOW = {"startDate": "2026-10-01T00:00:00Z", "endDate": "2026-10-02T00:00:00Z"}

# Synthetic kinds the registry does not own, each given rows only inside the test that needs it.
# `postizx` shares a prefix with a real kind; `filebrowser-x` carries a hyphen, which sorts before
# the `__` separator; the verb-less kind proves a block can govern nothing.
PREFIX_TWIN = "postizx"
PREFIX_TWIN_VERB = f"{PREFIX_TWIN}__list_posts"
HYPHEN_TWIN = "filebrowser-x"
HYPHEN_TWIN_VERB = f"{HYPHEN_TWIN}__list_dir"
KIND_WITHOUT_VERBS = "zzz-no-verbs"


def _block_path(companion_key: str) -> str:
    return f"/v1/companions/group-block/{companion_key}"


@pytest.fixture(autouse=True)
def _clear_companion_process_state():
    reset_companion_tools_cache()
    reset_companion_rate_limiter()
    yield
    reset_companion_tools_cache()
    reset_companion_rate_limiter()


def _record_mints(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Wrap the real mint so a test can see which calls reached dispatch."""
    minted: list[dict[str, Any]] = []
    real_mint = companion_facade.mint_dispatch_call

    def _recording_mint(**kwargs: Any) -> Any:
        minted.append(kwargs)
        return real_mint(**kwargs)

    monkeypatch.setattr(companion_facade, "mint_dispatch_call", _recording_mint)
    return minted


def _names(tools: list[dict[str, Any]]) -> set[str]:
    return {tool["name"] for tool in tools}


def _of_kind(names: set[str], companion_key: str) -> set[str]:
    return {name for name in names if name.partition("__")[0] == companion_key}


async def _rows(db_session, user_id: uuid.UUID) -> list[CompanionToolCall]:
    return list(
        (
            await db_session.execute(
                select(CompanionToolCall)
                .where(CompanionToolCall.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _dispatch_rows(db_session, user_id: uuid.UUID) -> list[CompanionDispatchCall]:
    return list(
        (
            await db_session.execute(
                select(CompanionDispatchCall)
                .where(CompanionDispatchCall.user_id == user_id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def _other_tenant(db_session) -> User:
    other = User(
        clerk_id=f"test_other_{uuid.uuid4().hex[:12]}",
        email=f"other-{uuid.uuid4().hex[:12]}@clawdi.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


def _refused_for_group(result: dict[str, Any]) -> bool:
    return result.get("isError") is True and f"[{GROUP_REASON}]" in result["content"][0]["text"]


# ---------------------------------------------------------------------------
# The tracer: a blocked group is hidden from the list AND refused by name.
# ---------------------------------------------------------------------------


async def test_blocked_group_is_hidden_and_refused_by_name(
    db_session, seed_user, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The distinct decision is part of the behaviour: a block recorded as `refused_policy` would
    # leave a reader unable to tell "the owner blocked this companion" from "this tool is deny".
    assert GROUP_DECISION in CALL_DECISIONS

    user_id = seed_user.id
    await _deploy_postiz(db_session, seed_user)
    agent = _platform_minted_auth(seed_user)

    # The agent's list is warmed BEFORE the block, so the cache is holding postiz's verbs: this is
    # the agent that "holds a cached tool list".
    before = _names(await list_companion_tools(agent, db_session))
    postiz_verbs = _of_kind(before, "postiz")
    filebrowser_verbs = _of_kind(before, "filebrowser")
    assert LIST_POSTS in postiz_verbs
    assert filebrowser_verbs, "the fixture deploys filebrowser beside postiz"

    blocked = await client.put(_block_path("postiz"), json={"blocked": True})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["companion_key"] == "postiz"
    assert blocked.json()["blocked"] is True

    # The next list is read fresh (the block reset the tenant's cache): postiz is gone and
    # filebrowser is untouched.
    during = _names(await list_companion_tools(agent, db_session))
    assert _of_kind(during, "postiz") == set()
    assert _of_kind(during, "filebrowser") == filebrowser_verbs

    # A call by name, as an agent holding the earlier list would make it, is refused.
    minted = _record_mints(monkeypatch)
    result = await call_companion_tool(
        LIST_POSTS, dict(POSTS_WINDOW), auth=agent, db=db_session, deadline_seconds=0
    )
    assert _refused_for_group(result), result
    assert LIST_POSTS in result["content"][0]["text"]
    assert minted == [], "a blocked call must never be signed"

    rows = await _rows(db_session, user_id)
    assert [row.decision for row in rows] == [GROUP_DECISION]
    (row,) = rows
    assert row.call_jti is None
    assert GROUP_REASON in (row.error_text or "")
    # Nothing was queued for a runtime: the only dispatch-table row a refusal leaves is the
    # terminal `failed` journal entry, never a claimable one.
    assert all(call.status == "failed" for call in await _dispatch_rows(db_session, user_id))

    allowed = await client.put(_block_path("postiz"), json={"blocked": False})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["blocked"] is False

    after = _names(await list_companion_tools(agent, db_session))
    assert _of_kind(after, "postiz") == postiz_verbs
    assert _of_kind(after, "filebrowser") == filebrowser_verbs

    # And the same call now proceeds past the block to the mint.
    await call_companion_tool(
        LIST_POSTS, dict(POSTS_WINDOW), auth=agent, db=db_session, deadline_seconds=0
    )
    assert len(minted) == 1


# ---------------------------------------------------------------------------
# The edges: timing, exact keys, empty groups, ordering and the tenant boundary.
# ---------------------------------------------------------------------------


async def test_block_takes_effect_on_the_next_list_via_cache_reset(
    db_session, seed_user, client
) -> None:
    """The block reaches the very next ``tools/list`` because it resets the cache, not because a
    minute passed. The clock is frozen, so every list below is inside one cache entry's TTL.

    The control in the middle is what makes the claim precise: a block row written BEHIND the
    route's back is not seen by the cached list, which proves the cache is live and that the reset
    is what the route adds. That same stale list is the "agent holding a cached tool list", and
    the call-time half refuses it anyway.
    """
    user_id = seed_user.id
    await _deploy_postiz(db_session, seed_user)
    agent = _platform_minted_auth(seed_user)

    def frozen() -> float:
        return 1_000.0

    warm = _names(await list_companion_tools(agent, db_session, now=frozen))
    assert LIST_POSTS in warm

    # Control: the row alone, with no reset, leaves the cached entry serving postiz ...
    db_session.add(
        CompanionGroupBlock(
            user_id=user_id,
            companion_key="postiz",
            blocked_at=datetime.now(UTC),
            blocked_by_user_id=user_id,
        )
    )
    await db_session.commit()
    stale = _names(await list_companion_tools(agent, db_session, now=frozen))
    assert LIST_POSTS in stale, "the cache is not live, so this test would prove nothing"
    # ... and a call made from that stale list is refused all the same.
    refused = await call_companion_tool(
        LIST_POSTS, dict(POSTS_WINDOW), auth=agent, db=db_session, deadline_seconds=0
    )
    assert _refused_for_group(refused), refused
    await db_session.execute(
        delete(CompanionGroupBlock).where(CompanionGroupBlock.user_id == user_id)
    )
    await db_session.commit()

    # Through the route, the same frozen instant sees the change in both directions.
    assert (await client.put(_block_path("postiz"), json={"blocked": True})).status_code == 200
    assert _of_kind(
        _names(await list_companion_tools(agent, db_session, now=frozen)), "postiz"
    ) == (set())
    assert (await client.put(_block_path("postiz"), json={"blocked": False})).status_code == 200
    assert LIST_POSTS in _names(await list_companion_tools(agent, db_session, now=frozen))


@pytest.mark.committed_db
async def test_call_minted_before_the_block_completes(db_session, seed_user, engine) -> None:
    """A call signed before the block commits finishes; the NEXT call is refused.

    The block gates the decision to mint. A call that already passed every gate and was handed to
    a runtime is not revoked mid-flight, and its result is accepted. Committed lane, because the
    executor stand-in and the block write each need their own connection while the façade polls.
    """
    user_id = seed_user.id
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix=f"gb{uuid.uuid4().hex[:6]}"
    )
    db_session.add(
        CompanionToolPolicy(
            user_id=user_id, companion_key="filebrowser", verb="list_dir", mode="allow"
        )
    )
    await db_session.commit()
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    auth = AuthContext(user=seed_user)
    block_order: list[str] = []

    async def _block_then_complete() -> None:
        for _attempt in range(600):
            async with sessionmaker() as runtime_db:
                row = (
                    (
                        await runtime_db.execute(
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
                if row is None:
                    await asyncio.sleep(0.01)
                    continue
                # The call is minted and queued. Block the group NOW, committed, before the
                # runtime reports.
                async with sessionmaker() as owner_db:
                    summary = await set_group_block(
                        owner_db, auth=auth, companion_key="filebrowser", blocked=True
                    )
                assert summary.blocked is True
                block_order.append("blocked")
                row.status = "completed"
                row.result = {"status": 200, "body": json.dumps(COMPANION_STUB_LISTING)}
                runtime_db.add(row)
                await runtime_db.commit()
                block_order.append("completed")
                return
        raise AssertionError("no queued dispatch call appeared for the executor stand-in")

    async with sessionmaker() as facade_db:
        result, _ = await asyncio.gather(
            call_companion_tool(
                READ_TOOL, {"path": "/"}, auth=auth, db=facade_db, deadline_seconds=5
            ),
            _block_then_complete(),
        )
    assert block_order == ["blocked", "completed"]
    assert result.get("isError") is not True, result

    async with sessionmaker() as next_db:
        following = await call_companion_tool(
            READ_TOOL, {"path": "/"}, auth=auth, db=next_db, deadline_seconds=0
        )
    assert _refused_for_group(following), following

    async with sessionmaker() as check_db:
        decisions = sorted(row.decision for row in await _rows(check_db, user_id))
    assert decisions == sorted(["allowed", GROUP_DECISION])


async def test_blocking_is_exact_key_match(
    db_session, seed_user, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Blocking ``postiz`` leaves ``postizx`` alone: keys match by equality, never by prefix.

    ``postizx`` is synthetic -- the registry owns no such kind -- so it is given one real verb row
    and postiz's argument contract for the length of this test. That is the smallest honest fixture
    for a deployed kind whose key merely begins with a blocked one.
    """
    monkeypatch.setitem(COMPANION_VERBS, PREFIX_TWIN_VERB, COMPANION_VERBS[LIST_POSTS])
    monkeypatch.setitem(
        companion_arguments._CONTRACTS, PREFIX_TWIN, argument_contract_for("postiz")
    )
    await deploy_companion_state(
        db_session,
        seed_user,
        machine_prefix="exactkey",
        extra_companions={"postiz": _postiz_entry(), PREFIX_TWIN: _postiz_entry()},
    )
    agent = _platform_minted_auth(seed_user)
    assert PREFIX_TWIN_VERB in _names(await list_companion_tools(agent, db_session))

    assert (await client.put(_block_path("postiz"), json={"blocked": True})).status_code == 200

    visible = _names(await list_companion_tools(agent, db_session))
    assert LIST_POSTS not in visible
    assert PREFIX_TWIN_VERB in visible
    minted = _record_mints(monkeypatch)
    twin = await call_companion_tool(
        PREFIX_TWIN_VERB, dict(POSTS_WINDOW), auth=agent, db=db_session, deadline_seconds=0
    )
    assert not _refused_for_group(twin), twin
    assert len(minted) == 1

    catalog = (await client.get(CATALOG_PATH)).json()
    flags = {row["companion_key"]: row["blocked"] for row in catalog["companions"]}
    assert flags["postiz"] is True
    assert flags[PREFIX_TWIN] is False


async def test_block_for_a_companion_with_no_verbs_is_harmless(
    db_session, seed_user, client
) -> None:
    """A deployed kind with no registered verbs can be blocked; it hides nothing else."""
    await deploy_companion_state(
        db_session,
        seed_user,
        machine_prefix="noverbs",
        extra_companions={KIND_WITHOUT_VERBS: {"version": "v0", "port": 9199}},
    )
    agent = _platform_minted_auth(seed_user)
    before = _names(await list_companion_tools(agent, db_session))
    assert before, "filebrowser should be visible; an empty list would prove nothing"

    response = await client.put(_block_path(KIND_WITHOUT_VERBS), json={"blocked": True})
    assert response.status_code == 200, response.text
    assert response.json()["blocked"] is True
    assert response.json()["visible_verb_count"] == 0

    assert _names(await list_companion_tools(agent, db_session)) == before
    catalog = (await client.get(CATALOG_PATH)).json()
    flags = {row["companion_key"]: row["blocked"] for row in catalog["companions"]}
    assert flags == {"filebrowser": False, KIND_WITHOUT_VERBS: True}


async def test_settings_readout_orders_groups_and_tools_and_flags_blocked(
    db_session, seed_user, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Groups in ascending companion key, tools in ascending ``(companion key, verb)``, stable.

    ``filebrowser-x`` is synthetic and chosen for its hyphen: ``-`` sorts BEFORE ``_``, so ordering
    by the composite tool name would put ``filebrowser-x__list_dir`` ahead of every
    ``filebrowser__`` verb while the groups list ``filebrowser`` first. The read-out must order its
    tools the way it orders its groups.
    """
    monkeypatch.setitem(COMPANION_VERBS, HYPHEN_TWIN_VERB, COMPANION_VERBS[READ_TOOL])
    await deploy_companion_state(
        db_session,
        seed_user,
        machine_prefix="ordering",
        extra_companions={"postiz": _postiz_entry(), HYPHEN_TWIN: {"version": "v0", "port": 9197}},
    )
    assert (await client.put(_block_path("postiz"), json={"blocked": True})).status_code == 200

    first = (await client.get(CATALOG_PATH)).json()
    second = (await client.get(CATALOG_PATH)).json()
    assert first == second

    keys = [row["companion_key"] for row in first["companions"]]
    assert keys == sorted(keys)
    assert set(keys) == {"filebrowser", HYPHEN_TWIN, "postiz"}
    pairs = [(row["companion_key"], row["verb"]) for row in first["tools"]]
    assert pairs == sorted(pairs)
    # Every tool the agent has is listed, blocked or not; each group's tools are contiguous.
    assert {row["tool"] for row in first["tools"]} >= {LIST_POSTS, HYPHEN_TWIN_VERB, READ_TOOL}
    assert [key for key, _ in pairs] == sorted(key for key, _ in pairs)

    flags = {row["companion_key"]: row["blocked"] for row in first["companions"]}
    assert flags == {"filebrowser": False, HYPHEN_TWIN: False, "postiz": True}
    postiz = next(row for row in first["companions"] if row["companion_key"] == "postiz")
    assert postiz["visible_verb_count"] == 0
    assert all(
        row["exposed_to_agent"] is False
        for row in first["tools"]
        if row["companion_key"] == "postiz"
    )


async def test_block_is_tenant_scoped(db_session, seed_user, client) -> None:
    """Another tenant's postiz is unaffected, in its list and at its calls."""
    await _deploy_postiz(db_session, seed_user)
    other = await _other_tenant(db_session)
    await deploy_companion_state(
        db_session,
        other,
        machine_prefix="othertenant",
        extra_companions={"postiz": _postiz_entry()},
    )
    mine = _platform_minted_auth(seed_user)
    theirs = _platform_minted_auth(other)
    # Both lists warmed, so the reset must be scoped to the blocking tenant's entry.
    assert LIST_POSTS in _names(await list_companion_tools(theirs, db_session))

    assert (await client.put(_block_path("postiz"), json={"blocked": True})).status_code == 200

    assert LIST_POSTS not in _names(await list_companion_tools(mine, db_session))
    assert LIST_POSTS in _names(await list_companion_tools(theirs, db_session))
    mine_result = await call_companion_tool(
        LIST_POSTS, dict(POSTS_WINDOW), auth=mine, db=db_session, deadline_seconds=0
    )
    theirs_result = await call_companion_tool(
        LIST_POSTS, dict(POSTS_WINDOW), auth=theirs, db=db_session, deadline_seconds=0
    )
    assert _refused_for_group(mine_result), mine_result
    assert not _refused_for_group(theirs_result), theirs_result
    assert await blocked_companion_keys(db_session, user_id=other.id) == frozenset()
    assert await blocked_companion_keys(db_session, user_id=seed_user.id) == frozenset({"postiz"})


# ---------------------------------------------------------------------------
# The boundary: only the account owner's browser session moves the block (T-06-24-03).
# ---------------------------------------------------------------------------


async def test_a_cli_credential_cannot_move_the_block(db_session, seed_user, cli_client) -> None:
    """A CLI key -- the agent's credential -- can neither set nor clear a block.

    An agent that could unblock a group would make the block a preference it can overrule.
    """
    await _deploy_postiz(db_session, seed_user)

    for position in (True, False):
        response = await cli_client.put(_block_path("postiz"), json={"blocked": position})
        assert response.status_code == 403
    assert await blocked_companion_keys(db_session, user_id=seed_user.id) == frozenset()


async def test_the_block_route_is_strict_and_deployment_scoped(
    db_session, seed_user, client
) -> None:
    """The body is strict and the companion must be deployed.

    ``StrictBool`` with ``extra="forbid"``, so a JSON string or a smuggled field is a 422 at the
    boundary; a malformed key is a 422; a companion the tenant has not deployed is a 404, as for the
    destroy switch. None of these leave a block row behind.
    """
    await _deploy_postiz(db_session, seed_user)

    assert (await client.put(_block_path("postiz"), json={"blocked": "true"})).status_code == 422
    smuggled = await client.put(
        _block_path("postiz"), json={"blocked": True, "user_id": str(uuid.uuid4())}
    )
    assert smuggled.status_code == 422
    assert (await client.put(_block_path("Postiz"), json={"blocked": True})).status_code == 422
    assert (await client.put(_block_path("ghost"), json={"blocked": True})).status_code == 404
    assert await blocked_companion_keys(db_session, user_id=seed_user.id) == frozenset()

    # The catalog carries the flag on every group, false until the owner blocks one.
    catalog = (await client.get(CATALOG_PATH)).json()
    assert {row["companion_key"]: row["blocked"] for row in catalog["companions"]} == {
        "filebrowser": False,
        "postiz": False,
    }
