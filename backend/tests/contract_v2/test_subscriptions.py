"""The dashboard's subscription surface (06.2 RESEARCH F2, F3, F5), against the mock or a hosted
target.

Naming, so later plans can select by ``-k``: plan-change cases are ``test_plan_*``; the
subscription center is ``test_subscription_list_*``, ``test_subscription_reusable_*`` and
``test_subscription_cancel_and_resume_round_trip``; checkout is ``test_checkout_*``; payment
recovery is ``test_fix_payment_*`` and ``test_portal_*``; the purge branch of cancel is
``test_service_cancel_*``.

Plan changes are billing-owned operations polled on a compute path (F3): ``POST
/v2/subscription/plan/quote`` prices the change and names the operation, ``POST
/v2/subscription/plan/change`` applies it under that operation id and answers a
``LongRunningOperation`` whose ``metadata.planChange`` the dashboard parses, and ``GET
/v2/operations/{id}`` answers the same operation. Every plan-change case is ``mock_only``: on a
hosted target a change moves money and needs a deployment bound to compute, which staging does
not have (D-02); hosted plan-change behavior is proven by the hosted service's own tests.

Checkout cases that run on a hosted target use card funding without ``deploy_config``: that opens
a Checkout Session and moves no money. The wallet arm moves money and is ``mock_only``.

``POST /v2/subscription/cancel`` has two branches on one path (F5): the dashboard cancels one
subscription at period end (``{deployment_id?, subscription_id?}``) and the backend's tenant purge
ends every subscription of an owner (``{owner_user_id}``, D-23). Both need a credential; the
hosted service dispatches by principal, never by body shape alone (T-06.2-08-01).
"""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any

import httpx
import pytest

from scripts import mock_deploy_api
from tests.contract_v2.conftest import HOSTED, new_key, new_owner_id
from tests.contract_v2.schema import validate_component

DEV_DEPLOYMENT_ID = "hdep_dev_sidebar"
UNKNOWN_SUBSCRIPTION_ID = 987_654_321
CARD_CHECKOUT = {"plan_slug": "compute_basic", "funding_source": "stripe"}
WALLET_CHECKOUT = {"plan_slug": "compute_basic", "funding_source": "wallet"}
CSUB_PREFIX = "csub_"


def _code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


def _put_dev_deployment_on(plan_slug: str) -> None:
    """Mock only: move the seeded dev deployment onto ``plan_slug``."""
    mock_deploy_api.DEPLOYMENTS[DEV_DEPLOYMENT_ID]["config_info"]["compute_plan_slug"] = plan_slug


async def _quote(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    return await client.post("/v2/subscription/plan/quote", json=body)


async def _quoted(client: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await _quote(client, **body)
    assert response.status_code == 200, response.text
    quote = response.json()
    validate_component("V2ComputePlanChangeQuoteResponse", quote)
    return quote


async def _change(client: httpx.AsyncClient, operation_id: str, key: str) -> httpx.Response:
    return await client.post(
        "/v2/subscription/plan/change",
        headers={"Idempotency-Key": key},
        json={"operation_id": operation_id},
    )


@pytest.mark.mock_only
async def test_plan_quote_for_a_more_expensive_plan_is_an_immediate_upgrade(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_basic")

    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )

    assert isinstance(quote["subscription_id"], int)
    assert quote["current_plan_slug"] == "compute_basic"
    assert quote["target_plan_slug"] == "compute_performance"
    assert quote["change_kind"] == "immediate_upgrade"
    assert quote["billing_effect"] == "immediate_proration"
    assert quote["amount_cents"] > 0


@pytest.mark.mock_only
async def test_plan_quote_for_a_cheaper_plan_is_a_scheduled_downgrade(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_performance")

    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_basic",
        target_billing_term_months=1,
    )

    assert quote["change_kind"] == "scheduled_downgrade"
    assert quote["billing_effect"] == "period_end"
    assert quote["amount_cents"] == 0


@pytest.mark.mock_only
async def test_plan_quote_for_another_funding_source_is_a_funding_source_switch(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_performance")

    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
        funding_source="wallet",
    )

    assert quote["change_kind"] == "funding_source_switch"
    assert quote["billing_effect"] == "future_renewals"
    assert quote["funding_source"] == "wallet"


@pytest.mark.mock_only
async def test_plan_quote_for_an_unknown_subscription_is_not_found(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await _quote(
        v2_dashboard,
        subscription_id=UNKNOWN_SUBSCRIPTION_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )

    assert response.status_code == 404, response.text
    assert _code(response) == "subscription_not_found"


@pytest.mark.mock_only
async def test_plan_change_applies_the_quote_as_an_operation_and_replays_a_repeated_key(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_basic")
    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )
    key = quote["operation_id"]

    first = await _change(v2_dashboard, quote["operation_id"], key)
    replay = await _change(v2_dashboard, quote["operation_id"], key)

    assert first.status_code == 202, first.text
    operation = first.json()
    validate_component("LongRunningOperation", operation)
    assert operation["name"] == f"operations/{quote['operation_id']}"
    assert operation["done"] is True
    metadata = operation["metadata"]
    assert metadata["verb"] == "plan_change"
    assert metadata["deploymentId"] == DEV_DEPLOYMENT_ID
    progress = metadata["planChange"]
    assert progress["operationId"] == quote["operation_id"]
    assert progress["subscriptionId"] == quote["subscription_id"]
    assert progress["changeKind"] == "immediate_upgrade"
    assert progress["state"] == "complete"
    assert operation["response"]["deployment"]["id"] == DEV_DEPLOYMENT_ID
    assert replay.status_code == 202, replay.text
    assert replay.json() == operation


@pytest.mark.mock_only
async def test_plan_change_operation_is_readable_on_the_operations_path(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_basic")
    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )
    changed = await _change(v2_dashboard, quote["operation_id"], new_key("plan-change"))
    assert changed.status_code == 202, changed.text

    polled = await v2_dashboard.get(f"/v2/operations/{quote['operation_id']}")

    assert polled.status_code == 200, polled.text
    validate_component("LongRunningOperation", polled.json())
    assert polled.json() == changed.json()


@pytest.mark.mock_only
async def test_plan_change_for_an_unknown_quote_is_not_found(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await _change(v2_dashboard, "ops_plan_unknown", new_key("plan-change"))

    assert response.status_code == 404, response.text
    assert _code(response) == "plan_change_quote_not_found"


@pytest.mark.mock_only
async def test_plan_change_for_an_expired_quote_is_gone(v2_dashboard: httpx.AsyncClient) -> None:
    _put_dev_deployment_on("compute_basic")
    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )
    record = mock_deploy_api.PLAN_CHANGE_QUOTES[quote["operation_id"]]
    record["expires_at"] = mock_deploy_api._now() - timedelta(seconds=1)

    response = await _change(v2_dashboard, quote["operation_id"], new_key("plan-change"))

    assert response.status_code == 410, response.text
    assert _code(response) == "plan_change_quote_expired"


@pytest.mark.mock_only
async def test_plan_cancel_scheduled_change_clears_the_pending_plan(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    _put_dev_deployment_on("compute_performance")
    quote = await _quoted(
        v2_dashboard,
        deployment_id=DEV_DEPLOYMENT_ID,
        target_plan_slug="compute_basic",
        target_billing_term_months=1,
    )
    scheduled = await _change(v2_dashboard, quote["operation_id"], new_key("plan-change"))
    assert scheduled.status_code == 202, scheduled.text
    assert scheduled.json()["metadata"]["planChange"]["state"] == "scheduled"

    response = await v2_dashboard.post(
        "/v2/subscription/plan/cancel-scheduled-change",
        json={"deployment_id": DEV_DEPLOYMENT_ID},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    validate_component("V2ComputeSubscriptionActionResponse", body)
    assert body["pending_plan_slug"] is None


async def _checkout(
    client: httpx.AsyncClient, key: str | None, body: dict[str, Any]
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return await client.post("/v2/subscription/checkout", headers=headers, json=body)


async def _wallet_subscription(client: httpx.AsyncClient) -> dict[str, Any]:
    """Mock only: activate a wallet-funded subscription with no deployment (an orphan)."""
    response = await _checkout(client, new_key("wallet-checkout"), WALLET_CHECKOUT)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "subscription_id" in body, body
    return body


async def _listed(client: httpx.AsyncClient, path: str, schema: str) -> list[dict[str, Any]]:
    response = await client.get(path)
    assert response.status_code == 200, response.text
    body = response.json()
    validate_component(schema, body)
    return body["items"]


async def test_checkout_card_opens_a_session_and_replays_a_repeated_key(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    key = new_key("checkout-card")

    first = await _checkout(v2_dashboard, key, CARD_CHECKOUT)
    replay = await _checkout(v2_dashboard, key, CARD_CHECKOUT)
    reused = await _checkout(
        v2_dashboard, key, {**CARD_CHECKOUT, "plan_slug": "compute_performance"}
    )

    assert first.status_code == 200, first.text
    body = first.json()
    validate_component("V2CheckoutResponse", body)
    assert body["flow_type"] == "checkout_session"
    if not HOSTED:
        assert body["client_secret"] is None
    assert replay.status_code == 200, replay.text
    assert replay.json() == body
    assert reused.status_code == 409, reused.text
    assert _code(reused) == "idempotency_key_reused"


async def test_checkout_requires_an_idempotency_key(v2_dashboard: httpx.AsyncClient) -> None:
    response = await _checkout(v2_dashboard, None, CARD_CHECKOUT)

    assert response.status_code == 400, response.text


@pytest.mark.mock_only
async def test_checkout_wallet_activates_a_subscription(v2_dashboard: httpx.AsyncClient) -> None:
    body = await _wallet_subscription(v2_dashboard)

    validate_component("V2CheckoutResponse", body)
    assert body["flow_type"] == "subscription_activation"
    assert body["checkout_url"] == ""
    assert body["subscription_id"].startswith(CSUB_PREFIX)


def _unsigned_jwt(sub: str) -> str:
    """A JWT-shaped bearer (header.payload.signature) the mock decodes without verifying."""

    def segment(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment({'sub': sub})}.unsigned"


async def _gate_status(service: httpx.AsyncClient, owner_user_id: str) -> str | None:
    response = await service.get("/v2/subscription", params={"owner_user_id": owner_user_id})
    assert response.status_code == 200, response.text
    subscription = response.json()["subscription"]
    return None if subscription is None else subscription["status"]


@pytest.mark.mock_only
async def test_plan_only_checkout_is_filed_under_the_bearer_owner(
    v2_dashboard: httpx.AsyncClient,
    v2_service: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """06.3 D-04: a plan-only checkout lands under the principal the backend gate reads.

    The owner is, in order, the body's owner_user_id, the sub of a JWT-shaped bearer, then
    MOCK_DASHBOARD_OWNER_USER_ID for a non-JWT bearer.
    """
    monkeypatch.setenv("MOCK_DASHBOARD_OWNER_USER_ID", "user_env_owner")

    by_bearer = await v2_dashboard.post(
        "/v2/subscription/checkout",
        headers={
            "Authorization": f"Bearer {_unsigned_jwt('user_contract_owner')}",
            "Idempotency-Key": new_key("bearer-owner"),
        },
        json=WALLET_CHECKOUT,
    )
    assert by_bearer.status_code == 200, by_bearer.text
    assert await _gate_status(v2_service, "user_contract_owner") == "active"

    by_env = await _checkout(v2_dashboard, new_key("env-owner"), WALLET_CHECKOUT)
    assert by_env.status_code == 200, by_env.text
    assert await _gate_status(v2_service, "user_env_owner") == "active"

    explicit = await v2_dashboard.post(
        "/v2/subscription/checkout",
        headers={
            "Authorization": f"Bearer {_unsigned_jwt('user_bearer_loses')}",
            "Idempotency-Key": new_key("body-owner"),
        },
        json={**WALLET_CHECKOUT, "owner_user_id": "user_body_owner"},
    )
    assert explicit.status_code == 200, explicit.text
    assert await _gate_status(v2_service, "user_body_owner") == "active"
    assert await _gate_status(v2_service, "user_bearer_loses") is None


async def test_subscription_list_conforms(v2_dashboard: httpx.AsyncClient) -> None:
    await _listed(v2_dashboard, "/v2/subscriptions", "V2ComputeSubscriptionListResponse")


@pytest.mark.mock_only
async def test_subscription_list_includes_a_wallet_checkout(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    activated = await _wallet_subscription(v2_dashboard)

    items = await _listed(v2_dashboard, "/v2/subscriptions", "V2ComputeSubscriptionListResponse")

    listed = [item for item in items if item["subscription_id"] == activated["subscription_id"]]
    assert len(listed) == 1, items
    assert listed[0]["status"] == "active"
    assert listed[0]["funding_source"] == "wallet"
    assert listed[0]["is_orphan"] is True


async def test_subscription_reusable_conforms(v2_dashboard: httpx.AsyncClient) -> None:
    await _listed(
        v2_dashboard, "/v2/subscriptions/reusable", "V2ComputeReusableSubscriptionsResponse"
    )


@pytest.mark.mock_only
async def test_subscription_reusable_lists_an_orphan(v2_dashboard: httpx.AsyncClient) -> None:
    activated = await _wallet_subscription(v2_dashboard)

    items = await _listed(
        v2_dashboard, "/v2/subscriptions/reusable", "V2ComputeReusableSubscriptionsResponse"
    )

    assert activated["subscription_id"] in {item["subscription_id"] for item in items}
    assert DEV_DEPLOYMENT_ID not in {item.get("deployment_id") for item in items}


@pytest.mark.mock_only
async def test_plan_quote_for_an_orphan_subscription_needs_a_deployment(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    activated = await _wallet_subscription(v2_dashboard)
    subscription_id = int(activated["subscription_id"].removeprefix("csub_mock"))

    response = await _quote(
        v2_dashboard,
        subscription_id=subscription_id,
        target_plan_slug="compute_performance",
        target_billing_term_months=1,
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "deployment_required"


async def test_subscription_cancel_and_resume_round_trip(v2_dashboard: httpx.AsyncClient) -> None:
    """Cancel one live subscription at period end, then resume it, leaving the owner as found."""
    items = await _listed(v2_dashboard, "/v2/subscriptions", "V2ComputeSubscriptionListResponse")
    live = [
        item
        for item in items
        if item["status"] in {"active", "trialing"} and not item["cancel_at_period_end"]
    ]
    assert live, f"precondition: the dashboard owner holds a live subscription: {items}"
    target = (
        {"deployment_id": live[0]["deployment_id"]}
        if live[0].get("deployment_id")
        else {"subscription_id": live[0]["subscription_id"]}
    )

    canceled = await v2_dashboard.post("/v2/subscription/cancel", json=target)
    resumed = await v2_dashboard.post("/v2/subscription/resume", json=target)

    assert canceled.status_code == 200, canceled.text
    validate_component("V2ComputeSubscriptionActionResponse", canceled.json())
    assert canceled.json()["cancel_at_period_end"] is True
    assert canceled.json()["status"] in {"canceling", "active"}
    assert resumed.status_code == 200, resumed.text
    validate_component("V2ComputeSubscriptionActionResponse", resumed.json())
    assert resumed.json()["cancel_at_period_end"] is False


async def test_fix_payment_opens_the_billing_portal(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.post("/v2/subscription/fix-payment", json={})

    assert response.status_code == 200, response.text
    validate_component("V2PortalResponse", response.json())


async def test_portal_opens_for_the_wallet(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.post("/v2/subscription/portal", json={"return_context": "wallet"})

    assert response.status_code == 200, response.text
    validate_component("V2PortalResponse", response.json())


async def test_service_cancel_answers_a_subscription_action(
    v2_service: httpx.AsyncClient,
) -> None:
    """The purge branch: a throwaway owner, so on a hosted target this is a no-op purge."""
    response = await v2_service.post(
        "/v2/subscription/cancel", json={"owner_user_id": new_owner_id()}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    validate_component("V2ComputeSubscriptionActionResponse", body)
    assert body["cancel_at_period_end"] is False
