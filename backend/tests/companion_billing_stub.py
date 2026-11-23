"""The /v2 companion-billing double every companion test reuses: the REAL mock, in process.

ONE double, reused (the ``companion_stub`` rule). ``scripts/mock_deploy_api.py`` is the executable
reference for the /v2 companion contract (ADR-0005 consumer-first), so tests drive THAT app through
an ASGI transport rather than inventing a second idea of what /v2 answers. Nothing here reaches a
network: ``companion_billing._transport_override`` routes every call into the mock, and the mock's
in-memory stores are replaced with fresh ones for the test and restored after it.

The transport RECORDS each call as ``"<METHOD> <path>"`` into ``BillingMock.calls`` -- the list a
test asserts order against -- and can inject a failure for any request ``fail`` matches.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.core.config import settings
from app.services import companion_billing
from scripts import mock_deploy_api

V2_URL = "http://v2.companion-billing.test"
V2_KEY = "companion-billing-test-deploy-key"


@dataclass(frozen=True, slots=True)
class _Price:
    monthly_usd_micros: int
    storage_usd_micros_per_gib_month: int


# The mock's fixture price for postiz at the standard tier -- read from the mock, never restated.
COMPANION_PRICE = _Price(*mock_deploy_api.COMPANION_PRICE_FIXTURES[("postiz", "standard")])


@dataclass
class BillingMock:
    calls: list[str] = field(default_factory=list)
    # Return a status code to answer that request with it instead of the mock; None passes through.
    fail: Callable[[httpx.Request], int | None] | None = None

    def line_items_for(self, installation_id: uuid.UUID | str) -> list[dict[str, Any]]:
        return [
            line
            for line in mock_deploy_api.COMPANION_LINE_ITEMS.values()
            if line["installation_id"] == str(installation_id)
        ]


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, mock: BillingMock) -> None:
        self.mock = mock
        self._inner = ASGITransport(app=mock_deploy_api.app)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.mock.calls.append(f"{request.method} {request.url.path}")
        injected = self.mock.fail(request) if self.mock.fail is not None else None
        if injected is not None:
            return httpx.Response(injected, json={"detail": "injected by the test"})
        if request.method == "POST" and request.url.path == "/v2/companions/quote":
            _subscribe_quoted_owner(request)
        return await self._inner.handle_async_request(request)


def _subscribe_quoted_owner(request: httpx.Request) -> None:
    """An owner who asks /v2 for a companion quote is a subscribed owner in the test world.

    The hosted service refuses a line for an owner with no live compute subscription (06.2 D-11),
    and the mock now does too. A quote can come from ``quote_for`` or from the backend's own
    ``POST /v1/companions/quotes``, so the seeding sits on the quote call itself. ``setdefault``
    keeps any subscription state a test set on purpose.
    """
    try:
        owner = json.loads(request.content).get("owner_user_id")
    except (ValueError, AttributeError):
        return
    if isinstance(owner, str):
        mock_deploy_api.SUBSCRIPTIONS.setdefault(
            owner, {"status": "active", "plan_id": "compute_basic"}
        )


def route_billing_to_mock(monkeypatch: pytest.MonkeyPatch) -> BillingMock:
    """Point the backend's /v2 billing client at the in-process mock, once per test.

    Idempotent within a test: a second call returns the same ``BillingMock`` instead of wiping the
    quotes the first one issued.
    """
    existing = companion_billing._transport_override
    if isinstance(existing, _RecordingTransport):
        return existing.mock
    monkeypatch.setattr(settings, "deploy_api_url", V2_URL)
    monkeypatch.setattr(settings, "deploy_api_key", V2_KEY)
    monkeypatch.setattr(mock_deploy_api, "SUBSCRIPTIONS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_QUOTES", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEMS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEM_KEYS", {})
    mock = BillingMock()
    monkeypatch.setattr(companion_billing, "_transport_override", _RecordingTransport(mock))
    return mock


def _hosted_owner(owner: object) -> str:
    """The /v2 owner a test quotes under: the Clerk user id, never the internal ``users.id``.

    06.2 D-09 keys every hosted owner by ``users.clerk_id`` (``hosted_owner_id``). A test that
    passes the internal UUID would quote under an owner the backend never names on the wire, so
    it is refused loudly here rather than failing later as an unexplained quote mismatch.
    """
    if not isinstance(owner, str):
        raise TypeError("pass the hosted owner id (users.clerk_id), not users.id")
    return owner


async def quote_for(
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    *,
    companion_key: str = "postiz",
    isolation_tier: str = "standard",
) -> str:
    """A current /v2 quote id for this owner, companion and tier -- what an install must carry.

    ``owner`` is the hosted owner id (``users.clerk_id``, 06.2 D-09); a ``uuid.UUID`` raises
    ``TypeError``. A quoted owner is a subscribed owner in the test world: the hosted service
    refuses a line for an owner with no live compute subscription (06.2 D-11), so the owner is
    seeded active here.
    """
    owner_id = _hosted_owner(owner)
    route_billing_to_mock(monkeypatch)
    mock_deploy_api.SUBSCRIPTIONS.setdefault(
        owner_id, {"status": "active", "plan_id": "compute_basic"}
    )
    quote = await companion_billing.request_companion_quote(
        owner_user_id=owner_id, companion_key=companion_key, isolation_tier=isolation_tier
    )
    return quote.quote_id


@asynccontextmanager
async def billing_quote(
    owner: str,
    *,
    companion_key: str = "postiz",
    isolation_tier: str = "standard",
) -> AsyncIterator[str]:
    """``quote_for`` for a helper with no ``monkeypatch`` of its own: routed only inside the block.

    ``owner`` is the hosted owner id (``users.clerk_id``); a ``uuid.UUID`` raises ``TypeError``
    before anything is routed. Wrap the install request in it; nothing after the request needs /v2
    unless the test runs the billing completion step, which routes billing for the whole test
    instead.
    """
    owner_id = _hosted_owner(owner)
    with pytest.MonkeyPatch.context() as patch:
        yield await quote_for(
            patch, owner_id, companion_key=companion_key, isolation_tier=isolation_tier
        )
