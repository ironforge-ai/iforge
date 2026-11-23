"""Route-tree helpers for tests that pin which routes reach which dependency.

FastAPI 0.141 stores `include_router` results as lazy wrappers, so
`app.router.routes` holds only a handful of `APIRoute`s and many included
routers (06.3 RESEARCH Pitfall 3). A test that enumerates `app.routes` naively
checks almost nothing and passes vacuously. These helpers walk every wrapper's
`original_router.routes`, prefixing each path with the wrapper's
`include_context.prefix`, and expand a route's full dependency closure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute


def iter_api_routes(
    routes: Iterable[Any] | None = None, prefix: str = ""
) -> Iterator[tuple[str, APIRoute]]:
    """Yield `(full_path, route)` for every `APIRoute` reachable from `routes`.

    With `routes=None` the walk starts at `app.main.app.router.routes`. Any
    route object carrying an `original_router` is recursed into, adding its
    `include_context.prefix` (or the empty string) to the path.
    """
    if routes is None:
        from app.main import app

        routes = app.router.routes
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        original_router = getattr(route, "original_router", None)
        if original_router is None:
            continue
        include_context = getattr(route, "include_context", None)
        added = getattr(include_context, "prefix", "") or ""
        yield from iter_api_routes(original_router.routes, prefix + added)


def dependency_calls(dependant: Dependant) -> set[Callable[..., Any]]:
    """Every `.call` reachable through `dependant.dependencies`, recursively."""
    calls: set[Callable[..., Any]] = set()
    stack = list(dependant.dependencies)
    while stack:
        sub = stack.pop()
        if sub.call is not None:
            calls.add(sub.call)
        stack.extend(sub.dependencies)
    return calls
