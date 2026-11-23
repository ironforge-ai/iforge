"""The singular gate read and the service cancel (06.2 D-16, D-24, SC-5).

Naming, so later plans can select by ``-k``: gate-read cases ``test_gate_read_*``, cancel cases
``test_cancel_*``, and ``test_owner_identity_subscription_cross_check`` (RESEARCH Pitfall 1).

Every cancel case cancels a freshly generated throwaway owner, never the configured contract
owner: on the hosted service a service-key cancel is the D-23 tenant purge, so this is a no-op
purge there and a plain cancel on the mock.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx
import pytest

from tests.contract_v2.conftest import HOSTED

GATE_STATUSES = {"active", "trialing", "past_due", "canceled"}
UNLOCKING_STATUSES = {"active", "trialing"}


def _code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


async def _gate_read(client: httpx.AsyncClient, owner: str) -> httpx.Response:
    return await client.get("/v2/subscription", params={"owner_user_id": owner})


async def test_gate_read_owner_without_subscription_is_null(
    v2_service: httpx.AsyncClient, fresh_owner_id: str
) -> None:
    response = await _gate_read(v2_service, fresh_owner_id)

    assert response.status_code == 200, response.text
    assert response.json() == {"subscription": None}


async def test_gate_read_subscribed_owner_has_exactly_status_and_plan_id(
    v2_service: httpx.AsyncClient, owner_id: str, subscribed: Callable[[str], None]
) -> None:
    subscribed(owner_id)

    response = await _gate_read(v2_service, owner_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"subscription"}
    assert set(body["subscription"]) == {"status", "plan_id"}
    assert body["subscription"]["status"] in GATE_STATUSES
    assert isinstance(body["subscription"]["plan_id"], str)
    if HOSTED:
        # Precondition: the configured contract owner holds a live subscription on the target.
        assert body["subscription"]["status"] in UNLOCKING_STATUSES


async def test_gate_read_requires_a_bearer_credential(
    v2_anonymous: httpx.AsyncClient, owner_id: str
) -> None:
    response = await _gate_read(v2_anonymous, owner_id)

    assert response.status_code == 401, response.text
    assert _code(response) == "unauthenticated"


async def test_cancel_requires_a_bearer_credential(
    v2_anonymous: httpx.AsyncClient, fresh_owner_id: str
) -> None:
    response = await v2_anonymous.post(
        "/v2/subscription/cancel", json={"owner_user_id": fresh_owner_id}
    )

    assert response.status_code == 401, response.text
    assert _code(response) == "unauthenticated"


async def test_cancel_throwaway_owner_is_repeatable_and_ends_the_subscription(
    v2_service: httpx.AsyncClient, fresh_owner_id: str, subscribed: Callable[[str], None]
) -> None:
    # On the mock the throwaway owner is seeded active first, so the cancel is observable.
    subscribed(fresh_owner_id)

    first = await v2_service.post("/v2/subscription/cancel", json={"owner_user_id": fresh_owner_id})
    repeat = await v2_service.post(
        "/v2/subscription/cancel", json={"owner_user_id": fresh_owner_id}
    )

    assert first.is_success, first.text
    assert repeat.is_success, repeat.text
    read = await _gate_read(v2_service, fresh_owner_id)
    assert read.status_code == 200, read.text
    subscription = read.json()["subscription"]
    assert subscription is None or subscription["status"] not in UNLOCKING_STATUSES


@pytest.mark.hosted_only
async def test_owner_identity_subscription_cross_check(
    v2_dashboard: httpx.AsyncClient, v2_service: httpx.AsyncClient
) -> None:
    """One person, one owner id (D-09, Pitfall 1): the subscription the dashboard principal sees
    in its own list is the one the backend's gate read unlocks on for the contract owner id.

    Precondition: ``V2_CONTRACT_CLERK_USER_ID`` (or the dashboard bearer's subject) is the same
    Clerk user as ``V2_CONTRACT_OWNER_USER_ID``, and that user holds a live subscription.
    """
    listed = await v2_dashboard.get("/v2/subscriptions")
    gate = await _gate_read(v2_service, os.environ["V2_CONTRACT_OWNER_USER_ID"])

    assert listed.status_code == 200, listed.text
    statuses = {item["status"] for item in listed.json()["items"]}
    assert statuses & UNLOCKING_STATUSES, (
        f"the dashboard owner lists no live subscription: {statuses}"
    )
    assert gate.status_code == 200, gate.text
    subscription = gate.json()["subscription"]
    assert subscription is not None, "the gate read finds no subscription for the same owner id"
    assert subscription["status"] in UNLOCKING_STATUSES
