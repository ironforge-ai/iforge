"""x402 stays deferred (06.2 D-19, SC-6), against the mock or a hosted target.

The dashboard's ``X402Card`` renders its deferred "Coming soon" state only while the wallet reads
exactly ``x402_enabled: false``, ``x402_payment_authority: null``, ``x402_payment_status: idle``
and ``x402_payment_attempt: null``; any other combination starts loading the binding or polling a
payment.

Nothing binds and nothing is attempted: the binding read answers unbound, unbinding is a no-op
204, and every binding or attempt mutation is ``409 x402_not_available`` -- a 4xx, so the
dashboard's retry predicate does not spin on it. ``POST /v2/x402/topup`` stays unrouted.

Naming, so later plans can select by ``-k``: the wallet-read case is
``test_wallet_reports_the_deferred_x402_quadruple``; it is the only case here that needs
``/v2/wallet``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

import httpx
import pytest

X402_NOT_AVAILABLE = {
    "code": "x402_not_available",
    "message": "USDC top-up is not available yet.",
}


async def test_wallet_reports_the_deferred_x402_quadruple(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await v2_dashboard.get("/v2/wallet")

    assert response.status_code == 200, response.text
    wallet = response.json()
    for key in (
        "x402_enabled",
        "x402_payment_authority",
        "x402_payment_status",
        "x402_payment_attempt",
    ):
        assert key in wallet, f"/v2/wallet omits {key}"
    assert wallet["x402_enabled"] is False
    assert wallet["x402_payment_authority"] is None
    assert wallet["x402_payment_status"] == "idle"
    assert wallet["x402_payment_attempt"] is None


async def test_x402_binding_read_is_unbound(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.get("/v2/wallet-binding")

    assert response.status_code == 200, response.text
    assert response.json() == {"bound": False, "address": None, "verified_at": None}


async def test_x402_binding_delete_is_no_content(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.delete("/v2/wallet-binding")

    assert response.status_code == 204, response.text
    assert response.content == b""


async def _binding_challenge(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/v2/wallet-binding/challenge",
        json={"address": "0x0000000000000000000000000000000000000001"},
    )


async def _binding_verify(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/v2/wallet-binding",
        json={"challenge_id": str(uuid.uuid4()), "signature": "0x" + "ab" * 65},
    )


async def _attempt(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v2/x402/attempts")


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(_binding_challenge, id="binding_challenge"),
        pytest.param(_binding_verify, id="binding_verify"),
        pytest.param(_attempt, id="attempt"),
    ],
)
async def test_x402_mutation_is_not_available(
    call: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]],
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await call(v2_dashboard)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == X402_NOT_AVAILABLE


async def test_x402_topup_stays_unrouted(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.post("/v2/x402/topup", json={})

    assert response.status_code in {404, 405}, response.text
