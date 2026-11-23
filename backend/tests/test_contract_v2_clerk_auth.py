"""Offline coverage for the hosted contract suite's Clerk session-token helper."""

import json

import httpx
import pytest

from tests.contract_v2 import conftest as contract


async def test_session_token_mint_uses_json_and_refreshes_the_same_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []
    tokens = iter(("first-token", "refreshed-token"))

    def clerk(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-secret"
        if request.url.path == "/v1/sessions":
            assert json.loads(request.content) == {"user_id": "user_test"}
            return httpx.Response(200, json={"id": "sess_test"})
        assert request.url.path == "/v1/sessions/sess_test/tokens"
        if request.headers.get("Content-Type") != "application/json":
            return httpx.Response(415, json={"error": "JSON content type required"})
        assert json.loads(request.content) == {}
        return httpx.Response(200, json={"jwt": next(tokens)})

    client = httpx.AsyncClient
    transport = httpx.MockTransport(clerk)
    monkeypatch.setattr(
        contract.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=transport, **kwargs),
    )
    auth = contract.ClerkSessionTokenAuth("test-secret", "user_test")

    assert await auth._current_token() == "first-token"
    assert await auth._current_token() == "first-token"
    assert len(requests) == 2

    auth._minted_at -= contract.CLERK_TOKEN_MAX_AGE_SECONDS + 1
    assert await auth._current_token() == "refreshed-token"
    assert len(requests) == 3
    assert sum(request.url.path == "/v1/sessions" for request in requests) == 1
