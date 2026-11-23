"""Canonical /v1 routing and the legacy /api alias.

Legacy routes are mounted canonically under /v1 and aliased under /api for
clients built before the versioned-prefix migration. New contracts are
canonical-only. The OpenAPI schema (which the web/CLI typed-client codegen
consumes) advertises legacy `/v1` plus the explicitly direct declarative-v2
runtime companion; `/api` never aliases the v2 router.
"""

import httpx
import pytest
from fastapi.routing import iter_route_contexts
from httpx import ASGITransport

from app.main import app

_CANONICAL_ONLY_V1_PREFIXES = (
    "/v1/platform/",
    "/v1/runtime/",
    "/v1/admin/channels/whatsapp/pairing-sessions",
    "/v1/webhooks/clerk",
    # The companion approvals surface (phase 05, plan 06). Canonical-only for the ordinary reason
    # -- it ships with this milestone and has no binaries predating `/v1` to keep working -- and
    # for one specific to it: this is where a person authorizes a write inside their own
    # filesystem, and a second, undocumented, unversioned path to that decision is a second thing
    # to audit forever. `app/main.py`'s `_CANONICAL_ONLY_ROUTERS` is the other half of this entry;
    # `test_companion_approval.py::test_the_approvals_surface_has_no_hidden_api_alias` asserts the
    # alias genuinely does not respond, rather than merely that this list excuses it.
    "/v1/companions/",
    # The tenant's own exports (plan 06-19): new in this milestone, consumed only by the dashboard
    # in this repo. `test_tenant_self_export.py::test_tenant_exports_have_no_hidden_api_alias`
    # asserts the alias genuinely does not respond.
    "/v1/me/exports",
)


def _routes_by_path() -> dict[str, set[str]]:
    routes: dict[str, set[str]] = {}
    for route in iter_route_contexts(app.routes):
        path = getattr(route, "path", "")
        methods = set(getattr(route, "methods", None) or {"WEBSOCKET"})
        routes.setdefault(path, set()).update(methods)
    return routes


def test_every_legacy_v1_route_has_api_alias():
    routes = _routes_by_path()
    v1_paths = [
        path
        for path in routes
        if path.startswith("/v1/") and not path.startswith(_CANONICAL_ONLY_V1_PREFIXES)
    ]
    assert v1_paths, "expected /v1 routes to be mounted"
    missing = [
        path
        for path in sorted(v1_paths)
        if not routes[path] <= routes.get(f"/api{path.removeprefix('/v1')}", set())
    ]
    assert not missing, f"routes missing the /api legacy alias: {missing}"


@pytest.mark.parametrize(
    "path",
    [
        "/me/invitations",
        "/memories",
        "/projects",
        "/sessions",
        "/settings",
        "/vault",
        "/connectors",
        "/skills",
        "/agents",
        "/environments",
    ],
)
async def test_api_alias_dispatches_to_the_same_handler(path):
    """The alias must behave exactly like /v1, not merely exist.

    Unauthenticated requests exercise the full middleware + dependency
    stack; identical status and body prove both prefixes dispatch to the
    same handler chain.
    """
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        canonical = await client.get(f"/v1{path}")
        legacy = await client.get(f"/api{path}")
    assert legacy.status_code == canonical.status_code
    assert legacy.content == canonical.content
    assert canonical.status_code != 404


async def test_runtime_routes_have_no_api_alias():
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.get("/api/runtime/manifest"),
            await client.get(
                "/api/runtime/project-skill-archives/"
                "00000000-0000-0000-0000-000000000000/"
                "00000000-0000-0000-0000-000000000000/"
                "00000000-0000-0000-0000-000000000000/"
                f"{'0' * 64}/{'0' * 64}/example.tar.gz"
            ),
            await client.get(
                "/api/runtime/project-skill-files/"
                "00000000-0000-0000-0000-000000000000/"
                "00000000-0000-0000-0000-000000000000/"
                f"{'0' * 64}/{'0' * 64}/SKILL.md"
            ),
        ]
    assert [response.status_code for response in responses] == [404, 404, 404]


def test_openapi_schema_advertises_only_v1_and_direct_runtime_v2():
    spec = app.openapi()
    assert "/v1/webhooks/clerk" not in spec["paths"]

    routes = _routes_by_path()
    assert routes["/v1/webhooks/clerk"] == {"POST"}

    non_v1 = {
        path
        for path in spec["paths"]
        if not path.startswith("/v1/") and path not in {"/health", "/ready"}
    }
    assert non_v1
    assert all(path.startswith("/v2/runtime/") for path in non_v1)
    assert all(not path.startswith("/api/") for path in spec["paths"])

    for path in non_v1:
        assert path not in {
            f"/v1{path.removeprefix('/v2')}",
            f"/api{path.removeprefix('/v2')}",
        }
        assert f"/v1{path.removeprefix('/v2')}" not in routes
        assert f"/api{path.removeprefix('/v2')}" not in routes
