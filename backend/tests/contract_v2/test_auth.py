"""Principal separation on the hosted service (06.2 D-24): every case here is ``hosted_only``.

A service key is refused on a dashboard route and a Clerk token is refused on a service route.
The mock authenticates neither principal, so these collect against the mock and run against a
hosted target (staging in plan 06.2-26).

Naming, so later plans can select the routes a hosted build already serves by ``-k``:
``test_auth_me_*``, ``test_auth_gate_read_*``, ``test_auth_companion_quote_*``.
"""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.hosted_only

REFUSED = {401, 403}


async def test_auth_me_dashboard_principal_can_use_v2(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.get("/v1/me")

    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["can_use_v2"] is True


async def test_auth_me_refuses_the_service_key(v2_service: httpx.AsyncClient) -> None:
    response = await v2_service.get("/v1/me")

    assert response.status_code in REFUSED, response.text


async def test_auth_me_refuses_an_anonymous_caller(v2_anonymous: httpx.AsyncClient) -> None:
    response = await v2_anonymous.get("/v1/me")

    assert response.status_code == 401, response.text


async def test_auth_gate_read_refuses_a_dashboard_token(
    v2_dashboard: httpx.AsyncClient, owner_id: str
) -> None:
    response = await v2_dashboard.get("/v2/subscription", params={"owner_user_id": owner_id})

    assert response.status_code in REFUSED, response.text


async def test_auth_companion_quote_refuses_a_dashboard_token(
    v2_dashboard: httpx.AsyncClient, owner_id: str, priced_pair: tuple[str, str]
) -> None:
    companion_key, isolation_tier = priced_pair
    response = await v2_dashboard.post(
        "/v2/companions/quote",
        json={
            "owner_user_id": owner_id,
            "companion_key": companion_key,
            "isolation_tier": isolation_tier,
        },
    )

    assert response.status_code in REFUSED, response.text
