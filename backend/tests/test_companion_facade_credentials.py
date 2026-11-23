"""TOOL-01 as a test that can FAIL: the companion credential on every agent-readable surface.

The claim under test is not "we were careful". It is: given a tenant whose companion auth secret
is a sentinel that cannot occur incidentally, that sentinel appears in NONE of the surfaces an
agent can read. The surfaces are an explicit, ITERATED list rather than a fixed set of separate
assertions, because the testable edge of TOOL-01 is the ENUMERATION: if a surface exists that the
list does not name, TOOL-01 is not met no matter how the façade behaves. Adding a surface is one
line here.

**The one surface that DOES carry it, asserted positively.** ``GET /v1/runtime/manifest`` serves
``companions.<kind>.auth.secret`` in plaintext to any caller passing ``require_cli_auth``. That is
a pre-existing Phase 4 provisioning path, not something phase 05 introduced, and it is why
``05-CONTEXT.md`` D-02's broader prose ("the companion credential never transits the control
plane") is not true of the system as shipped after this phase, even though TOOL-01's actual
requirement text ("never in agent-readable config") is met and proven above.

It is held as a POSITIVE assertion -- the sentinel IS present -- rather than left unrecorded, so
that a future credential-flow change trips a loud test instead of passing silently. See the
Phase 6 entry in ``.planning/phases/04-companion-platform-contract/deferred-items.md``, which
names this module as the detector. Registered as ``T-5-02 | Information Disclosure |
GET /v1/runtime/manifest | high | accept`` and signed off at this plan's Task 3 checkpoint.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext, get_auth, get_auth_short_session
from app.core.database import (
    RuntimeManifestSessions,
    get_runtime_manifest_sessions,
    get_session,
)
from app.main import app
from app.models.api_key import ApiKey
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.hosted_runtime import HostedRuntimeState
from app.routes import mcp_bridge
from app.routes import runtime as runtime_routes
from app.routes.mcp_bridge import _list_clawdi_mcp_tools
from app.services.audit import (
    SECRETISH_KEY_PARTS,
    _is_secretish_key,
    _sanitize_audit_details,
)
from app.services.companion_dispatch import mint_dispatch_call
from app.services.companion_facade import call_companion_tool, reset_companion_tools_cache
from app.services.runtime_source import (
    RUNTIME_AGENT_PLUGIN_GITHUB_RELEASE_SOURCE_CAPABILITY,
    RUNTIME_AGENT_PLUGINS_MANIFEST_CAPABILITY,
    RUNTIME_BUNDLE_V2_MEDIA_TYPE,
    RUNTIME_CAPABILITIES_HEADER,
)
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state
from tests.hosted_runtime_fixtures import (
    CANONICAL_CODEX_TOOLS,
    ensure_canonical_codex_tool_provider,
    filebrowser_companion,
)

TRACER_TOOL = "filebrowser__list_dir"

# 46 characters of `[A-Za-z0-9_-]`, inside the manifest schema's 43..128 bound so the fixture is a
# VALID companion auth block and not a shape the parser would have rejected anyway. Chosen so it
# cannot occur incidentally in a listing, a UUID, a base64url segment or a log line.
SENTINEL_SECRET = "SENTINEL_COMPANION_SECRET_DO_NOT_LEAK_00000000"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


async def _tenant_with_sentinel_secret(db_session, seed_user) -> tuple[uuid.UUID, dict[str, Any]]:
    env = await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"cred-{uuid.uuid4().hex[:8]}",
        machine_name="Credential Test Agent",
    )
    state = await create_test_hosted_runtime_state(db_session, env, runtime_name="openclaw")
    companions = filebrowser_companion(state.deployment_id)
    companions["filebrowser"]["auth"]["secret"] = SENTINEL_SECRET
    state.companions = companions
    # The manifest render refuses a state whose terminal tooling is unresolvable, so the
    # canonical Codex tool graph is seeded here too. Without it the manifest assertion below
    # would fail with a 409 that has nothing to do with the credential claim under test.
    state.tools = CANONICAL_CODEX_TOOLS
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    db_session.add(state)
    await db_session.commit()
    return env.id, companions["filebrowser"]["auth"]


async def test_the_sentinel_is_absent_from_every_agent_readable_surface(
    db_session, seed_user, monkeypatch
) -> None:
    async def _no_connector_tools(auth):
        return []

    monkeypatch.setattr(mcp_bridge, "_connector_mcp_tools", _no_connector_tools)
    environment_id, _auth_material = await _tenant_with_sentinel_secret(db_session, seed_user)
    auth = AuthContext(user=seed_user, api_key=ApiKey(user_id=seed_user.id))

    listed = await _list_clawdi_mcp_tools(auth, db=db_session)

    clock = [0.0]

    async def _instant_sleep(seconds: float) -> None:
        clock[0] += seconds

    # Driven through the façade with an injected clock rather than through
    # `_call_clawdi_mcp_tool`, which has no deadline injection point: with no executor claiming,
    # the real path would wait 25 seconds to produce the same refusal. The pipeline that touches
    # the credential surfaces — mint, enqueue, persist — runs identically either way.
    called = await call_companion_tool(
        TRACER_TOOL,
        {"path": "/"},
        auth=auth,
        db=db_session,
        now=lambda: clock[0],
        sleep=_instant_sleep,
    )

    minted = mint_dispatch_call(
        companion_key="filebrowser",
        verb="list_dir",
        method="GET",
        path_template="/api/resources",
        arguments={"path": "/"},
    )
    row = (
        await db_session.execute(
            select(CompanionDispatchCall).where(
                CompanionDispatchCall.environment_id == environment_id
            )
        )
    ).scalar_one()
    persisted = {
        column.name: str(getattr(row, column.name))
        for column in CompanionDispatchCall.__table__.columns
    }
    audited = _sanitize_audit_details(
        {"call": minted.payload, "row": persisted, "tool": TRACER_TOOL}
    )

    # THE ENUMERATION. The failure message names the offending surface, and adding a surface is
    # one entry here rather than a new test nobody remembers to write.
    surfaces: list[tuple[str, str]] = [
        ("tools/list response", json.dumps(listed)),
        ("tools/call result", json.dumps(called)),
        ("minted signed call payload", json.dumps(minted.payload)),
        ("minted signed call token", minted.token),
        ("persisted CompanionDispatchCall row", json.dumps(persisted)),
        ("control-plane audit record", json.dumps(audited)),
    ]
    assert len(surfaces) >= 5
    for name, serialized in surfaces:
        assert SENTINEL_SECRET not in serialized, f"companion secret leaked into {name}"


async def test_no_persisted_dispatch_column_is_named_like_credential_material(
    db_session, seed_user
) -> None:
    # Imported from `app.services.audit` rather than restated: a part added to the shipped
    # redactor must apply here without anyone remembering to copy it.
    assert SECRETISH_KEY_PARTS
    for column in CompanionDispatchCall.__table__.columns:
        assert not _is_secretish_key(column.name), (
            f"companion_dispatch_calls.{column.name} is named like credential material; "
            "either it is credential material, in which case it must not be persisted, or it "
            "is misnamed and the shipped redactor will redact it"
        )
    # And the guard is LIVE, not vacuous: the names credential material would actually be given
    # do trip it. Without this, a redactor that returned False for everything would pass above.
    for name in ("secret", "auth_secret", "api_key", "bearer_token"):
        assert _is_secretish_key(name)


@pytest.mark.committed_db
async def test_the_runtime_manifest_is_the_one_surface_that_still_carries_the_secret(
    db_session, seed_user, engine
) -> None:
    """POSITIVE assertion, deliberately. See this module's docstring and the ledger entry.

    ``.planning/phases/04-companion-platform-contract/deferred-items.md`` carries the Phase 6
    item; this assertion is what makes a change to that provisioning path fail loudly here rather
    than pass silently. If this test goes red because the sentinel is GONE, that is good news --
    close the ledger entry and invert the assertion.
    """
    environment_id, _auth_material = await _tenant_with_sentinel_secret(db_session, seed_user)
    api_key = ApiKey(user_id=seed_user.id, environment_id=environment_id, label="cred-test")

    async def _override_get_session():
        yield db_session

    async def _override_get_auth() -> AuthContext:
        return AuthContext(user=seed_user, api_key=api_key)

    # Upstream #1386/#1388: the manifest route authenticates through its own dependency on a
    # reserved session pair, so both seams are overridden alongside the ordinary ones.
    async def _override_manifest_sessions():
        yield RuntimeManifestSessions(
            auth=db_session,
            snapshots=async_sessionmaker(engine, expire_on_commit=False),
        )

    await db_session.commit()
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    app.dependency_overrides[get_auth_short_session] = _override_get_auth
    app.dependency_overrides[runtime_routes._get_runtime_manifest_auth] = _override_get_auth
    app.dependency_overrides[get_runtime_manifest_sessions] = _override_manifest_sessions
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers={
                "Accept": RUNTIME_BUNDLE_V2_MEDIA_TYPE,
                # Without a capability header upstream answers with the legacy bootstrap
                # manifest, which carries no companions; a current runtime always sends one.
                RUNTIME_CAPABILITIES_HEADER: ", ".join(
                    (
                        "provider-identity-v1",
                        RUNTIME_AGENT_PLUGINS_MANIFEST_CAPABILITY,
                        RUNTIME_AGENT_PLUGIN_GITHUB_RELEASE_SOURCE_CAPABILITY,
                    )
                ),
            },
        ) as client:
            response = await client.get("/v1/runtime/manifest")
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_auth, None)
        app.dependency_overrides.pop(get_auth_short_session, None)
        app.dependency_overrides.pop(runtime_routes._get_runtime_manifest_auth, None)
        app.dependency_overrides.pop(get_runtime_manifest_sessions, None)

    assert response.status_code == 200, response.text
    assert SENTINEL_SECRET in response.text, (
        "The manifest no longer carries the companion secret. That is the Phase 6 change the "
        "deferred-items ledger anticipates — close the entry and invert this assertion."
    )
    companions = response.json()["manifest"]["companions"]
    assert companions["filebrowser"]["auth"]["secret"] == SENTINEL_SECRET


async def test_the_state_row_the_manifest_renders_from_also_holds_it_in_plaintext(
    db_session, seed_user
) -> None:
    """The persistence half of the same known gap: `hosted_runtime_states.companions` JSONB.

    Recorded here so the ledger entry names both halves of the path -- the column and the route
    -- rather than only the one an HTTP test happens to reach.
    """
    environment_id, _auth = await _tenant_with_sentinel_secret(db_session, seed_user)

    state = (
        await db_session.execute(
            select(HostedRuntimeState).where(HostedRuntimeState.environment_id == environment_id)
        )
    ).scalar_one()

    assert state.companions is not None
    assert state.companions["filebrowser"]["auth"]["secret"] == SENTINEL_SECRET
