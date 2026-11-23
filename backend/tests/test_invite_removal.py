"""Phase-level removal sweep for the invite system (06.3 D-01, T-06.3-12-02, T-06.3-12-04).

Clerk alone decides who signs up, so nothing of the invite-code system survives: no route, module,
schema, model, table, setting or purge entry. Project-sharing invitations are a different, upstream
feature and must outlive the sweep.

Routes are enumerated through `tests.route_tree.iter_api_routes`, which walks FastAPI's lazy
include wrappers; `app.routes` alone would check almost nothing (06.3 RESEARCH Pitfall 3).
"""

from __future__ import annotations

import importlib.util

from app.core.config import Settings, settings
from app.main import app
from app.models.base import Base
from app.services.tenant_purge import PURGE_COVERAGE
from tests.route_tree import iter_api_routes

_MIN_API_ROUTES = 400
_REMOVED_MODULES = ("app.routes.invites", "app.models.invite_code", "app.schemas.admin_invite")
_SHARING_ROUTES = {
    ("GET", "/v1/me/invitations"),
    ("GET", "/v1/projects/{project_id}/invitations"),
}


def _walked_routes() -> set[tuple[str, str]]:
    walked = {(method, path) for path, route in iter_api_routes() for method in route.methods or ()}
    # A vacuous walk would pass every absence check below.
    assert len({path for _method, path in walked}) >= _MIN_API_ROUTES
    return walked


def test_no_invite_surface_remains() -> None:
    openapi_paths = [path for path in app.openapi()["paths"] if "/invites" in path]
    assert openapi_paths == []

    walked_paths = sorted({path for _method, path in _walked_routes() if "/invites" in path})
    assert walked_paths == []

    surviving_modules = [name for name in _REMOVED_MODULES if importlib.util.find_spec(name)]
    assert surviving_modules == []

    assert "invite_codes" not in Base.metadata.tables
    assert "invite_codes" not in PURGE_COVERAGE

    invite_settings = sorted(name for name in Settings.model_fields if "invite" in name.lower())
    assert invite_settings == []
    assert [name for name in dir(settings) if "invite" in name.lower()] == []


def test_project_sharing_invitations_are_untouched() -> None:
    assert _SHARING_ROUTES <= _walked_routes()
