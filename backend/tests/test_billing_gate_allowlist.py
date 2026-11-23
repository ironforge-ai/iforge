"""Route-tree pin of the billing-exempt route set (06.3 D-03, threat T-06.3-01-01).

The app-wide billing gate runs inside every principal resolver. Exactly one
dependency, `get_auth_billing_exempt`, turns it off, and only the identity
bootstrap read (`GET /auth/me` on `/v1` and its `/api` alias) may use it. A
new route depending on it fails this module until the pin is edited in
review: widening the exempt set is a policy change, never a convenience.

The walk is non-vacuous by construction: it must see at least
`_MIN_API_ROUTES` routes, because a walk that silently found nothing would
report an empty exempt set and pass (06.3 RESEARCH Pitfall 3).
"""

from __future__ import annotations

from app.core.auth import get_auth, get_auth_billing_exempt
from tests.route_tree import dependency_calls, iter_api_routes

_MIN_API_ROUTES = 400
_EXPECTED_EXEMPT = {("GET", "/v1/auth/me"), ("GET", "/api/auth/me")}


def test_route_walk_is_not_vacuous() -> None:
    routes = list(iter_api_routes())
    assert len(routes) >= _MIN_API_ROUTES, (
        f"route walk found only {len(routes)} API routes; the lazy include "
        "wrappers were probably not expanded"
    )


def test_billing_exempt_routes_are_exactly_identity_bootstrap() -> None:
    routes = list(iter_api_routes())
    assert len(routes) >= _MIN_API_ROUTES
    exempt = {
        (method, path)
        for path, route in routes
        if get_auth_billing_exempt in dependency_calls(route.dependant)
        for method in route.methods
    }
    assert exempt == _EXPECTED_EXEMPT


def test_walk_sees_real_gated_dependencies() -> None:
    gated = {
        (method, path)
        for path, route in iter_api_routes()
        if get_auth in dependency_calls(route.dependant)
        for method in route.methods
    }
    assert ("GET", "/v1/agents") in gated
    assert ("GET", "/v1/auth/me") not in gated
