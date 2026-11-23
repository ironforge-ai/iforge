"""A platform-minted runtime key sees and calls companion tools (D-09, WINDOWS #199).

Phase 5's UAT only passed by minting an UNSCOPED owner credential, which no real install can
obtain: the key the platform mints for an Agent carries ``RUNTIME_MCP_SCOPES`` and nothing else,
and that bundle had no ``companions:*`` member. These cases drive the credential an install
actually gets -- minted through ``POST /v1/platform/auth/keys`` with its default scopes -- through
the MCP façade and the runtime dispatch channel.

The end-to-end case reuses the Phase 5 harness (``deploy_companion_state``, the shared filebrowser
stub, and the executor stand-in in ``test_companion_tracer.py``) rather than writing a second one.
Unlike the tracer, the executor stand-in here authenticates with the minted key's raw bearer
token, so the claim and result routes' ``companions:read`` check is exercised for real rather
than bypassed by a dependency override.

**Every assertion here was confirmed able to fail (T-4-19).** A scope test that cannot fail is a
vacuous parity check, so each mutation below was applied to the shipped code, the module run, and
the mutation reverted. The failing tests are recorded against each:

- M1 -- restore the pre-06-02 class map (``read`` -> ``companions:read``, ``create``/``mutate`` ->
  ``companions:write``, ``destroy`` -> ``companions:destroy``): fails
  ``test_key_without_companions_invoke_is_refused_and_recorded`` and
  ``test_platform_key_destroy_verb_hidden_and_refused_until_enabled``.
- M2 -- move the ``companions:read`` check in ``list_companion_tools`` BELOW the cache read: fails
  ``test_list_scope_is_checked_before_the_cache``.
- M3 -- delete the ``companions:read`` list check: fails
  ``test_key_without_companions_read_sees_no_companion_tools`` and
  ``test_list_scope_is_checked_before_the_cache``.
- M4 -- replace the list check with one that fails closed on a NULL-scope owner key: fails
  ``test_owner_credential_behaviour_unchanged``.
- M5 -- drop the destroy-enablement filter from ``is_exposed_to_agent``: fails
  ``test_platform_key_destroy_verb_hidden_and_refused_until_enabled`` and
  ``test_owner_credential_behaviour_unchanged``.
- M6 -- skip the destroy-disabled branch of ``evaluate_policy``: fails
  ``test_platform_key_destroy_verb_hidden_and_refused_until_enabled``.
- M7 -- drop ``companions:read`` from ``RUNTIME_MCP_SCOPES``: fails
  ``test_platform_minted_key_lists_and_calls_a_companion_tool``,
  ``test_runtime_deployment_key_can_claim_dispatch`` and the three cases built on the bundle.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.api_scopes import RUNTIME_DEPLOYMENT_KEY_SCOPES, RUNTIME_MCP_SCOPES
from app.core.auth import AuthContext
from app.core.companion_policy import REASON_CLASS_DEFAULT, REASON_DESTROY_DISABLED
from app.core.companion_registry import COMPANION_VERBS
from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.main import app
from app.models.api_key import ApiKey
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_tool_call import CallDecision, CompanionToolCall
from app.models.companion_tool_policy import CompanionDestroyEnablement
from app.models.hosted_runtime import HostedRuntimeState
from app.routes import mcp_bridge
from app.routes.mcp_bridge import (
    _NATIVE_TOOL_REGISTRY,
    _call_clawdi_mcp_tool,
    _list_clawdi_mcp_tools,
)
from app.schemas.platform import PLATFORM_RUNTIME_KEY_SCOPES
from app.services.api_key import mint_api_key
from app.services.companion_facade import (
    call_companion_tool,
    list_companion_tools,
    reset_companion_tools_cache,
)
from app.services.runtime_observation import provision_runtime_environment_fence
from tests.hosted_runtime_fixtures import deploy_companion_state
from tests.test_companion_tracer import TRACER_TOOL, _drive_executor_once

_ADMIN_KEY = "test-companion-runtime-scopes-admin-secret"
_CLERK_ISSUER = "https://companion-scopes.clerk.example.test"

# Spelled as a literal, never read from a `tools/list` response: a destroy name sourced from the
# list under test would make "the verb I was not shown is refused" a tautology (T-4-19).
DESTROY_TOOL = "filebrowser__delete_path"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


@pytest.fixture
def _no_connector_tools(monkeypatch):
    """Runtime keys carry `connectors:read`, which would otherwise reach Composio."""

    async def _none(auth):
        return []

    monkeypatch.setattr(mcp_bridge, "_connector_mcp_tools", _none)


async def _mint_platform_key(
    db_session, seed_user, environment_id: uuid.UUID
) -> tuple[str, uuid.UUID]:
    """Mint a key exactly as the platform does for an install: default scopes, managed, bound."""

    async def _override_get_session():
        yield db_session

    original_admin_key = settings.admin_api_key
    original_clerk_issuer = settings.clerk_jwt_issuer
    settings.admin_api_key = _ADMIN_KEY
    settings.clerk_jwt_issuer = _CLERK_ISSUER
    app.dependency_overrides[get_session] = _override_get_session
    # Upstream moved the platform and admin routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            minted = await client.post(
                "/v1/platform/auth/keys",
                headers={
                    "X-Admin-Key": _ADMIN_KEY,
                    "Idempotency-Key": f"companion-scopes-{uuid.uuid4().hex}",
                },
                json={
                    "owner": {"kind": "clerk", "ref": seed_user.clerk_id},
                    "label": "platform-runtime",
                    "environment_id": str(environment_id),
                },
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_control_session, None)
        settings.admin_api_key = original_admin_key
        settings.clerk_jwt_issuer = original_clerk_issuer
    assert minted.status_code == 200, minted.text
    body = minted.json()
    return body["raw_key"], uuid.UUID(body["id"])


@pytest.mark.committed_db
async def test_platform_minted_key_lists_and_calls_a_companion_tool(
    db_session, seed_user, engine, companion_stub, _no_connector_tools
) -> None:
    environment_id, companions = await deploy_companion_state(
        db_session, seed_user, machine_prefix="scopes", machine_name="Scopes Agent"
    )
    companion_auth = companions["filebrowser"]["auth"]
    raw_key, key_id = await _mint_platform_key(db_session, seed_user, environment_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert api_key is not None
    # The credential under test is the platform's own default, not a hand-built scope list.
    assert api_key.managed is True
    assert api_key.scopes == list(PLATFORM_RUNTIME_KEY_SCOPES)
    auth = AuthContext(user=seed_user, api_key=api_key)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override_get_session():
        async with sessionmaker() as runtime_db:
            yield runtime_db

    app.dependency_overrides[get_session] = _override_get_session
    # Upstream moved the platform and admin routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = _override_get_session
    try:
        async with sessionmaker() as list_db:
            tools = await _list_clawdi_mcp_tools(auth, db=list_db)
        assert TRACER_TOOL in [tool["name"] for tool in tools]

        async with (
            sessionmaker() as facade_db,
            httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url="http://test",
                headers={"Authorization": f"Bearer {raw_key}"},
            ) as runtime_client,
        ):
            # `return_exceptions` so a façade refusal is reported by its own text: the executor
            # stand-in raises a bare 403 when the claim route refuses the same missing scope.
            result, executor_outcome = await asyncio.gather(
                _call_clawdi_mcp_tool(TRACER_TOOL, {"path": "/"}, auth=auth, db=facade_db),
                _drive_executor_once(
                    runtime_client,
                    environment_id=str(environment_id),
                    companion_auth=companion_auth,
                    stub=companion_stub,
                ),
                return_exceptions=True,
            )

        assert not isinstance(result, BaseException), result
        assert result.get("isError") is not True, result
        if isinstance(executor_outcome, BaseException):
            raise executor_outcome
        async with sessionmaker() as check_db:
            decisions = list(
                (
                    await check_db.execute(
                        select(CompanionToolCall.decision).where(
                            CompanionToolCall.user_id == seed_user.id
                        )
                    )
                ).scalars()
            )
        assert decisions == ["allowed"]
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_control_session, None)
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


# ---------------------------------------------------------------------------
# Each scope gates exactly one surface (D-09), and destroy keeps every Phase 5 gate.
# ---------------------------------------------------------------------------


def _runtime_key_without(seed_user, missing: str) -> ApiKey:
    """A managed runtime key holding every runtime scope EXCEPT ``missing``.

    Derived from ``RUNTIME_MCP_SCOPES`` rather than a hand-copied list, so these cases track the
    constant: a scope added to the bundle later is held here too, and only ``missing`` is absent.
    """
    assert missing in RUNTIME_MCP_SCOPES
    return ApiKey(
        user_id=seed_user.id,
        managed=True,
        scopes=[scope for scope in RUNTIME_MCP_SCOPES if scope != missing],
    )


def _instant_clock() -> tuple[list[float], object]:
    clock = [0.0]

    async def _instant_sleep(seconds: float) -> None:
        # A call that passes every gate waits for an executor that never comes; the deadline is
        # proved with an injected clock rather than by waiting it out.
        clock[0] += seconds

    return clock, _instant_sleep


async def _decisions(db_session, user_id: uuid.UUID) -> list[tuple[str, str | None]]:
    rows = await db_session.execute(
        select(CompanionToolCall.decision, CompanionToolCall.error_text).where(
            CompanionToolCall.user_id == user_id
        )
    )
    return [(decision, error_text) for decision, error_text in rows]


async def test_key_without_companions_read_sees_no_companion_tools(
    db_session, seed_user, _no_connector_tools
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="noread")

    # Control first, with the full bundle: the companion IS listable for this tenant, so the empty
    # result below is the scope gate and not a deployment that never resolved.
    full = AuthContext(
        user=seed_user,
        api_key=ApiKey(user_id=seed_user.id, managed=True, scopes=list(RUNTIME_MCP_SCOPES)),
    )
    assert TRACER_TOOL in [
        tool["name"] for tool in await _list_clawdi_mcp_tools(full, db=db_session)
    ]
    reset_companion_tools_cache()

    auth = AuthContext(user=seed_user, api_key=_runtime_key_without(seed_user, "companions:read"))
    names = [tool["name"] for tool in await _list_clawdi_mcp_tools(auth, db=db_session)]

    assert set(names).isdisjoint(COMPANION_VERBS)
    # The native tools are still listed: the refusal is scoped to companions, not a blank toolset.
    assert names
    assert set(names) <= set(_NATIVE_TOOL_REGISTRY)


async def test_list_scope_is_checked_before_the_cache(db_session, seed_user) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="cachefirst")

    # An owner credential warms the per-tenant cache. The cache is keyed by tenant, not by
    # credential, so the scoped key below would be served this exact list if the check ran after
    # the cache read.
    warmed = await list_companion_tools(AuthContext(user=seed_user), db_session)
    assert TRACER_TOOL in [tool["name"] for tool in warmed]

    auth = AuthContext(user=seed_user, api_key=_runtime_key_without(seed_user, "companions:read"))

    assert await list_companion_tools(auth, db_session) == []


async def test_key_without_companions_invoke_is_refused_and_recorded(
    db_session, seed_user, companion_stub
) -> None:
    user_id = seed_user.id
    await deploy_companion_state(db_session, seed_user, machine_prefix="noinvoke")
    auth = AuthContext(user=seed_user, api_key=_runtime_key_without(seed_user, "companions:invoke"))
    clock, instant_sleep = _instant_clock()

    result = await call_companion_tool(
        TRACER_TOOL,
        {"path": "/"},
        auth=auth,
        db=db_session,
        now=lambda: clock[0],
        sleep=instant_sleep,
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "companions:invoke" in text, text
    # Recorded exactly as Phase 5 records a scope refusal: one row, `refused_policy`, naming it.
    decisions = await _decisions(db_session, user_id)
    assert [decision for decision, _ in decisions] == [CallDecision.REFUSED_POLICY.value]
    assert "companions:invoke" in (decisions[0][1] or "")
    assert companion_stub.requests == []


async def test_platform_key_destroy_verb_hidden_and_refused_until_enabled(
    db_session, seed_user, companion_stub, _no_connector_tools
) -> None:
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="destroyscope"
    )
    _raw_key, key_id = await _mint_platform_key(db_session, seed_user, environment_id)
    api_key = await db_session.get(ApiKey, key_id)
    assert api_key is not None
    auth = AuthContext(user=seed_user, api_key=api_key)

    before = [tool["name"] for tool in await _list_clawdi_mcp_tools(auth, db=db_session)]
    assert TRACER_TOOL in before
    assert DESTROY_TOOL not in before

    refused = await call_companion_tool(
        DESTROY_TOOL, {"path": "/projects/report.md"}, auth=auth, db=db_session
    )
    assert refused["isError"] is True
    # Refused by the enablement gate, not by a scope: companions:invoke is held, so the remap let
    # the call through to the gate that was always meant to stop it.
    assert REASON_DESTROY_DISABLED in refused["content"][0]["text"], refused

    db_session.add(
        CompanionDestroyEnablement(
            user_id=seed_user.id,
            companion_key="filebrowser",
            enabled_at=datetime.now(UTC),
            enabled_by_user_id=seed_user.id,
        )
    )
    await db_session.commit()
    reset_companion_tools_cache()

    after = [tool["name"] for tool in await _list_clawdi_mcp_tools(auth, db=db_session)]
    assert DESTROY_TOOL in after

    gated = await call_companion_tool(
        DESTROY_TOOL, {"path": "/projects/report.md"}, auth=auth, db=db_session
    )
    # Enabled, and STILL policy-gated: destroy's class default is `ask`, so the call is held
    # rather than run. The remap removed no gate between a platform key and a delete.
    assert gated["isError"] is True
    gated_text = gated["content"][0]["text"]
    assert REASON_DESTROY_DISABLED not in gated_text, gated_text
    assert REASON_CLASS_DEFAULT in gated_text, gated_text
    assert companion_stub.requests == []


async def test_owner_credential_behaviour_unchanged(
    db_session, seed_user, companion_stub, _no_connector_tools
) -> None:
    user_id = seed_user.id
    await deploy_companion_state(db_session, seed_user, machine_prefix="ownerkey")
    # The Phase 5 owner credential: an API key with NULL scopes, i.e. full account access.
    auth = AuthContext(user=seed_user, api_key=ApiKey(user_id=seed_user.id))

    names = [tool["name"] for tool in await _list_clawdi_mcp_tools(auth, db=db_session)]
    # The fixture deploys filebrowser only, so the expected set is filebrowser's rows -- built from
    # the deployed kind, never from every kind the registry carries (06-06 added postiz rows).
    deployed = {name for name in COMPANION_VERBS if name.startswith("filebrowser__")}
    assert {name for name in names if name in COMPANION_VERBS} == deployed - {DESTROY_TOOL}

    clock, instant_sleep = _instant_clock()
    result = await call_companion_tool(
        TRACER_TOOL,
        {"path": "/"},
        auth=auth,
        db=db_session,
        now=lambda: clock[0],
        sleep=instant_sleep,
    )

    # Past every scope and policy gate to the dispatch queue, where no executor is listening.
    assert result["isError"] is True
    assert "not reachable" in result["content"][0]["text"], result
    decisions = [decision for decision, _ in await _decisions(db_session, user_id)]
    assert CallDecision.REFUSED_POLICY.value not in decisions


async def test_runtime_deployment_key_can_claim_dispatch(db_session, seed_user) -> None:
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="deploykey"
    )
    # The Agent's own deployment: its environment is permanently bound to it, and a strict-v2
    # credential needs that fence to exist first, exactly as the v2 mint route provisions it.
    deployment_id = await db_session.scalar(
        select(HostedRuntimeState.deployment_id).where(
            HostedRuntimeState.environment_id == environment_id
        )
    )
    assert deployment_id
    await provision_runtime_environment_fence(
        db_session,
        environment_id=environment_id,
        owner_id=seed_user.id,
        deployment_id=deployment_id,
    )

    async def _mint(scopes: list[str]) -> str:
        minted = await mint_api_key(
            db_session,
            user_id=seed_user.id,
            label="runtime-deployment",
            scopes=scopes,
            environment_id=environment_id,
            runtime_deployment_id=deployment_id,
            managed=True,
        )
        return minted.raw_key

    deployment_key = await _mint(list(RUNTIME_DEPLOYMENT_KEY_SCOPES))
    # The same key shape minus companions:read -- the control that shows the 200 below is the
    # scope being held, not a route that checks nothing.
    unread_key = await _mint(
        [scope for scope in RUNTIME_DEPLOYMENT_KEY_SCOPES if scope != "companions:read"]
    )

    async def _override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    # Upstream moved the platform and admin routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            claimed = await client.post(
                "/v1/runtime/companion-dispatch/claim",
                headers={"Authorization": f"Bearer {deployment_key}"},
                json={"environment_id": str(environment_id), "wait_seconds": 0},
            )
            refused = await client.post(
                "/v1/runtime/companion-dispatch/claim",
                headers={"Authorization": f"Bearer {unread_key}"},
                json={"environment_id": str(environment_id), "wait_seconds": 0},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_control_session, None)

    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["call"] is None
    assert refused.status_code == 403
    assert "companions:read" in refused.text
