"""The black-box /v2 contract suite (06.2 D-08): one suite, two targets, chosen by env alone.

With ``V2_CONTRACT_BASE_URL`` unset the suite drives ``scripts/mock_deploy_api.py`` -- the
executable reference for the fork-owned /v2 contract (ADR-0005) -- in process over an ASGI
transport, so it runs in CI with no network and no database. With it set, the suite drives that
base URL with ``V2_CONTRACT_SERVICE_KEY`` and holds the hosted service to the same assertions.

Nothing target-specific is written here. Hostnames, keys, owners and the priced/unpriced tiers all
come from env; the tiers default to the mock's own fixtures, and amounts are only ever compared
quote-to-line, never against a literal price.

Environment contract:

- ``V2_CONTRACT_BASE_URL`` -- the hosted target; unset means the in-process mock.
- ``V2_CONTRACT_SERVICE_KEY`` -- the service credential (required when hosted).
- ``V2_CONTRACT_OWNER_USER_ID`` / ``V2_CONTRACT_OTHER_OWNER_USER_ID`` -- two owners that already
  hold an active subscription on the target (required when hosted).
- ``V2_CONTRACT_PRICED_COMPANION`` / ``V2_CONTRACT_PRICED_TIER`` / ``V2_CONTRACT_UNPRICED_TIER``.
- ``V2_CONTRACT_DASHBOARD_BEARER``, or ``V2_CONTRACT_CLERK_SECRET_KEY`` with
  ``V2_CONTRACT_CLERK_USER_ID`` -- the dashboard principal (hosted only).
- ``V2_CONTRACT_MAX_EXPIRY_WAIT_SECONDS`` -- how long an expiry case may wait (default 5).
- ``V2_CONTRACT_COMPUTE`` -- ``forwarded`` or ``unset``: whether billing forwards to a compute
  destination; required hosted by the compute-forwarding cases only, which fail without it.

Markers: ``mock_only`` cases need state only the in-process mock can arrange and are skipped when
hosted; ``hosted_only`` cases need a real principal split and are skipped against the mock.
"""

from __future__ import annotations

import asyncio
import copy
import os
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from scripts import mock_deploy_api

HOSTED = bool(os.environ.get("V2_CONTRACT_BASE_URL"))

MOCK_BASE_URL = "http://mock.v2.contract.test"
MOCK_SERVICE_KEY = "contract-test-service-key"
MOCK_DASHBOARD_TOKEN = "contract-dashboard-token"
MOCK_SUBSCRIPTION = {"status": "active", "plan_id": "compute_basic"}

CLERK_API_URL = "https://api.clerk.com/v1"
# Clerk session tokens live 60 seconds; re-mint well before that.
CLERK_TOKEN_MAX_AGE_SECONDS = 45.0
HOSTED_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_EXPIRY_WAIT_SECONDS = 5.0

_REQUIRED_HOSTED_ENV = (
    "V2_CONTRACT_SERVICE_KEY",
    "V2_CONTRACT_OWNER_USER_ID",
    "V2_CONTRACT_OTHER_OWNER_USER_ID",
)
_SUITE_DIR = Path(__file__).resolve().parent
# The deployments the mock seeds at import, before any case mutates them.
_SEEDED_DEPLOYMENTS = copy.deepcopy(mock_deploy_api.DEPLOYMENTS)

# Line items a hosted run started in the current test, stopped again at teardown so a contract run
# never leaves a line billing on the target.
_STARTED_LINES: list[str] = []


def _in_suite(item: pytest.Item) -> bool:
    return _SUITE_DIR in Path(str(item.path)).resolve().parents


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    suite_items = [item for item in items if _in_suite(item)]
    if not suite_items:
        return
    if HOSTED:
        missing = [name for name in _REQUIRED_HOSTED_ENV if not os.environ.get(name)]
        if missing:
            raise pytest.UsageError(
                "V2_CONTRACT_BASE_URL is set, so the /v2 contract suite targets a hosted service, "
                f"but {', '.join(missing)} is unset. A hosted run never silently degrades."
            )
    skip_mock_only = pytest.mark.skip(reason="mock_only: needs state only the in-process mock sets")
    skip_hosted_only = pytest.mark.skip(reason="hosted_only: needs a hosted target (06.2 D-08)")
    for item in suite_items:
        if HOSTED and item.get_closest_marker("mock_only") is not None:
            item.add_marker(skip_mock_only)
        if not HOSTED and item.get_closest_marker("hosted_only") is not None:
            item.add_marker(skip_hosted_only)


def _client(headers: dict[str, str] | None = None, auth: httpx.Auth | None = None) -> Any:
    if HOSTED:
        return httpx.AsyncClient(
            base_url=os.environ["V2_CONTRACT_BASE_URL"],
            headers=headers,
            auth=auth,
            timeout=HOSTED_TIMEOUT_SECONDS,
        )
    return httpx.AsyncClient(
        transport=ASGITransport(app=mock_deploy_api.app),
        base_url=MOCK_BASE_URL,
        headers=headers,
        auth=auth,
    )


def _service_key() -> str:
    return os.environ.get("V2_CONTRACT_SERVICE_KEY") or MOCK_SERVICE_KEY


@pytest.fixture
async def v2_service() -> AsyncIterator[httpx.AsyncClient]:
    """The trusted backend principal: ``Authorization: Bearer <service key>``."""
    async with _client(headers={"Authorization": f"Bearer {_service_key()}"}) as client:
        yield client


@pytest.fixture
async def v2_anonymous() -> AsyncIterator[httpx.AsyncClient]:
    """The same target with no credential at all."""
    async with _client() as client:
        yield client


class ClerkSessionTokenAuth(httpx.Auth):
    """A Clerk session token minted through the Backend API, re-minted before it expires.

    Creates one session for the configured user on first use, then mints a fresh session token
    from it whenever the cached one is older than ``CLERK_TOKEN_MAX_AGE_SECONDS``.
    """

    def __init__(self, secret_key: str, user_id: str) -> None:
        self._secret_key = secret_key
        self._user_id = user_id
        self._session_id: str | None = None
        self._token: str | None = None
        self._minted_at = 0.0

    async def _current_token(self) -> str:
        now = time.monotonic()
        if self._token is not None and now - self._minted_at < CLERK_TOKEN_MAX_AGE_SECONDS:
            return self._token
        async with httpx.AsyncClient(
            base_url=CLERK_API_URL,
            headers={"Authorization": f"Bearer {self._secret_key}"},
            timeout=HOSTED_TIMEOUT_SECONDS,
        ) as clerk:
            if self._session_id is None:
                session = await clerk.post("/sessions", json={"user_id": self._user_id})
                session.raise_for_status()
                self._session_id = session.json()["id"]
            minted = await clerk.post(f"/sessions/{self._session_id}/tokens", json={})
            minted.raise_for_status()
        self._token = minted.json()["jwt"]
        self._minted_at = now
        return self._token

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._current_token()}"
        yield request

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        raise RuntimeError("ClerkSessionTokenAuth is async-only")


@pytest.fixture
async def v2_dashboard() -> AsyncIterator[httpx.AsyncClient]:
    """The dashboard principal: a Clerk session token on hosted, a placeholder bearer on the mock.

    The mock does not authenticate dashboard routes, so its bearer is only a shape.
    """
    if not HOSTED:
        headers = {"Authorization": f"Bearer {MOCK_DASHBOARD_TOKEN}"}
        async with _client(headers=headers) as client:
            yield client
        return
    bearer = os.environ.get("V2_CONTRACT_DASHBOARD_BEARER")
    if bearer:
        async with _client(headers={"Authorization": f"Bearer {bearer}"}) as client:
            yield client
        return
    secret_key = os.environ.get("V2_CONTRACT_CLERK_SECRET_KEY")
    user_id = os.environ.get("V2_CONTRACT_CLERK_USER_ID")
    if not secret_key or not user_id:
        pytest.fail(
            "A hosted dashboard case needs V2_CONTRACT_DASHBOARD_BEARER, or "
            "V2_CONTRACT_CLERK_SECRET_KEY with V2_CONTRACT_CLERK_USER_ID; neither is configured."
        )
    async with _client(auth=ClerkSessionTokenAuth(secret_key, user_id)) as client:
        yield client


@pytest.fixture(autouse=True)
def _fresh_mock_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every mock case starts from empty subscription, companion, wallet and runtime-UI grant
    stores, and from the deployments and operations the mock seeds at import (a plan change
    rewrites a deployment)."""
    if HOSTED:
        return
    monkeypatch.setattr(mock_deploy_api, "DEPLOYMENTS", copy.deepcopy(_SEEDED_DEPLOYMENTS))
    monkeypatch.setattr(mock_deploy_api, "OPERATIONS", {})
    monkeypatch.setattr(mock_deploy_api, "DEPLOY_REQUESTS", {})
    monkeypatch.setattr(mock_deploy_api, "PLAN_CHANGE_QUOTES", {})
    monkeypatch.setattr(mock_deploy_api, "CHECKOUT_IDEMPOTENCY", {})
    monkeypatch.setattr(mock_deploy_api, "SUBSCRIPTIONS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_QUOTES", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEMS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEM_KEYS", {})
    monkeypatch.setattr(mock_deploy_api, "WALLET_IDEMPOTENCY", {})
    monkeypatch.setattr(mock_deploy_api, "WALLET_TOPUPS", {})
    monkeypatch.setattr(mock_deploy_api, "WALLET_SETUPS", {})
    monkeypatch.setattr(mock_deploy_api, "RUNTIME_UI_GRANTS", {})


@pytest.fixture(autouse=True)
async def _stop_lines_started_on_hosted() -> AsyncIterator[None]:
    """On a hosted target, stop every line the test started so nothing keeps billing."""
    _STARTED_LINES.clear()
    yield
    if not HOSTED or not _STARTED_LINES:
        return
    async with _client(headers={"Authorization": f"Bearer {_service_key()}"}) as client:
        for line_item_id in _STARTED_LINES:
            await client.delete(
                f"/v2/companions/line-items/{line_item_id}",
                headers={"Idempotency-Key": f"contract-cleanup-{line_item_id}"},
            )
    _STARTED_LINES.clear()


def new_owner_id() -> str:
    return f"user_contract_{uuid.uuid4().hex}"


@pytest.fixture
def owner_id() -> str:
    """The owner most cases act as; hosted: an owner that already holds an active subscription."""
    if HOSTED:
        return os.environ["V2_CONTRACT_OWNER_USER_ID"]
    return new_owner_id()


@pytest.fixture
def other_owner_id() -> str:
    """A second, distinct owner; hosted: one that also holds an active subscription."""
    if HOSTED:
        return os.environ["V2_CONTRACT_OTHER_OWNER_USER_ID"]
    return new_owner_id()


@pytest.fixture
def fresh_owner_id() -> str:
    """A brand-new owner with no subscription anywhere, on either target."""
    return new_owner_id()


@pytest.fixture
def subscribed() -> Callable[[str], None]:
    """Make an owner subscribed: seeds an active subscription on the mock.

    On a hosted target this is a no-op. Precondition there: the configured contract owners
    (``V2_CONTRACT_OWNER_USER_ID`` and ``V2_CONTRACT_OTHER_OWNER_USER_ID``) already hold an
    active subscription on the target -- the suite never creates a paid subscription itself.
    """

    def _subscribe(owner: str) -> None:
        if HOSTED:
            return
        mock_deploy_api.SUBSCRIPTIONS[owner] = dict(MOCK_SUBSCRIPTION)

    return _subscribe


@pytest.fixture
def priced_pair() -> tuple[str, str]:
    """(companion, tier) the target prices; defaults to the mock's own priced fixture."""
    return (
        os.environ.get("V2_CONTRACT_PRICED_COMPANION") or "postiz",
        os.environ.get("V2_CONTRACT_PRICED_TIER") or "standard",
    )


@pytest.fixture
def unpriced_pair(priced_pair: tuple[str, str]) -> tuple[str, str]:
    """The same companion at a tier the target does not price."""
    return (priced_pair[0], os.environ.get("V2_CONTRACT_UNPRICED_TIER") or "dedicated")


@pytest.fixture
def expire_quote() -> Callable[[dict[str, Any]], Awaitable[None]]:
    """Make a quote's validity window pass (RESEARCH Pitfall 6).

    Mock: move its ``expires_at`` one second into the past. Hosted: wait until ``expires_at``
    passes when that is at most ``V2_CONTRACT_MAX_EXPIRY_WAIT_SECONDS`` away, otherwise skip.
    """

    async def _expire(quote: dict[str, Any]) -> None:
        if not HOSTED:
            record = mock_deploy_api.COMPANION_QUOTES[quote["quote_id"]]
            record["expires_at"] = mock_deploy_api._now() - timedelta(seconds=1)
            return
        window = float(
            os.environ.get("V2_CONTRACT_MAX_EXPIRY_WAIT_SECONDS") or DEFAULT_MAX_EXPIRY_WAIT_SECONDS
        )
        expires_at = datetime.fromisoformat(quote["expires_at"].replace("Z", "+00:00"))
        remaining = (expires_at - datetime.now(UTC)).total_seconds()
        if remaining > window:
            pytest.skip("target quote validity exceeds the wait window")
        await asyncio.sleep(max(remaining, 0.0) + 1.0)

    return _expire


async def request_quote(
    client: httpx.AsyncClient, owner: str, pair: tuple[str, str]
) -> httpx.Response:
    companion_key, isolation_tier = pair
    return await client.post(
        "/v2/companions/quote",
        json={
            "owner_user_id": owner,
            "companion_key": companion_key,
            "isolation_tier": isolation_tier,
        },
    )


async def quote(client: httpx.AsyncClient, owner: str, pair: tuple[str, str]) -> dict[str, Any]:
    """A current quote for this owner; fails the test if the target will not quote."""
    response = await request_quote(client, owner, pair)
    assert response.status_code == 200, response.text
    return response.json()


async def start_line(
    client: httpx.AsyncClient,
    quote_id: str,
    installation_id: str,
    owner: str,
    key: str,
) -> httpx.Response:
    """POST a line start under ``Idempotency-Key: key``.

    A line this starts on a hosted target is stopped again when the test ends.
    """
    response = await client.post(
        "/v2/companions/line-items",
        headers={"Idempotency-Key": key},
        json={"quote_id": quote_id, "installation_id": installation_id, "owner_user_id": owner},
    )
    if response.status_code == 200:
        line_item_id = response.json().get("line_item_id")
        if isinstance(line_item_id, str) and line_item_id not in _STARTED_LINES:
            _STARTED_LINES.append(line_item_id)
    return response


async def stop_line(client: httpx.AsyncClient, line_item_id: str, key: str) -> httpx.Response:
    return await client.delete(
        f"/v2/companions/line-items/{line_item_id}", headers={"Idempotency-Key": key}
    )


def new_key(label: str) -> str:
    return f"contract-{label}-{uuid.uuid4().hex}"


def new_installation_id() -> str:
    return str(uuid.uuid4())
