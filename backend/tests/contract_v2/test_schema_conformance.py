"""Responses validate against the filtered contract document (06.2 RESEARCH F2, qualifies D-08).

Naming, so later plans can select by ``-k``: the pin case is ``test_contract_document_is_pinned``;
conformance cases carry ids prefixed by surface -- ``identity_`` (``/v1/me``, agent
environments), ``stub_`` (notifications, wallet binding, usage), ``wallet_`` (every wallet
route) and ``subscription_`` (the subscription center, plan changes, checkout and cancel). Wallet
ids name ``setup``, ``auto_reload`` or ``payment_methods`` when they exercise those.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.contract_v2.conftest import new_key
from tests.contract_v2.schema import (
    CONTRACT_DOCUMENT_PATH,
    CONTRACT_DOCUMENT_SHA256,
    validate_component,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
FILTER_SCRIPT = REPO_ROOT / "scripts" / "filter-deploy-openapi.py"
SPLICED_SPEC = (
    REPO_ROOT
    / ".planning"
    / "phases"
    / "06.1-upstream-sync-to-clawdi-1593"
    / "06.1-spliced-openapi.json"
)


def test_contract_document_is_pinned() -> None:
    document = CONTRACT_DOCUMENT_PATH.read_bytes()

    assert hashlib.sha256(document).hexdigest() == CONTRACT_DOCUMENT_SHA256

    if SPLICED_SPEC.exists():
        # The spliced spec is still in the tree: the fixture must be exactly its filtered form.
        filtered = subprocess.run(
            [sys.executable, str(FILTER_SCRIPT)],
            input=SPLICED_SPEC.read_bytes(),
            capture_output=True,
            check=True,
        ).stdout
        assert filtered == document, (
            "v2-contract-openapi.json is not scripts/filter-deploy-openapi.py applied to "
            "the 06.1 spliced spec"
        )


async def test_wallet_read_conforms(v2_dashboard: httpx.AsyncClient) -> None:
    response = await v2_dashboard.get("/v2/wallet")

    assert response.status_code == 200, response.text
    validate_component("V2WalletResponse", response.json())


@dataclass(frozen=True)
class Conformance:
    """One dashboard call, the component schema its answer must satisfy, the status that answer
    carries (200 unless the contract says otherwise), and any exact field values it must carry."""

    call: Callable[[httpx.AsyncClient], Awaitable[httpx.Response]]
    schema: str
    expect: dict[str, Any] | None = None
    status: int = 200


SETUP_TERMS = {
    "consent_version": "wallet_auto_reload_off_session_v2",
    "auto_reload_threshold_usd": "10.00",
    "auto_reload_amount_cents": 1000,
    "auto_reload_monthly_cap_cents": 5000,
}


async def _created(response: httpx.Response) -> dict:
    assert response.status_code == 200, response.text
    return response.json()


async def _wallet_topup(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/v2/wallet/topup",
        headers={"Idempotency-Key": new_key("conformance-topup")},
        json={"amount_cents": 2500},
    )


async def _wallet_topup_status(client: httpx.AsyncClient) -> httpx.Response:
    created = await _created(await _wallet_topup(client))
    assert "checkout_session_id" in created, created
    return await client.get(f"/v2/wallet/topup/checkout/{created['checkout_session_id']}")


async def _wallet_setup_intent(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        "/v2/wallet/auto-reload/setup-intent",
        headers={"Idempotency-Key": new_key("conformance-setup")},
        json=SETUP_TERMS,
    )


async def _wallet_setup_finalize(client: httpx.AsyncClient) -> httpx.Response:
    setup = await _created(await _wallet_setup_intent(client))
    return await client.post(
        "/v2/wallet/auto-reload/setup-intent/finalize",
        json={
            "setup_identity": setup["setup_identity"],
            "setup_intent_id": setup["setup_intent_id"],
        },
    )


async def _wallet_auto_reload_update(client: httpx.AsyncClient) -> httpx.Response:
    return await client.put("/v2/wallet/auto-reload", json={"auto_reload_enabled": False})


async def _wallet_payment_methods(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/wallet/payment-methods")


async def _wallet_transactions(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/wallet/transactions")


DEV_DEPLOYMENT_ID = "hdep_dev_sidebar"


async def _subscription_plan_quote(client: httpx.AsyncClient) -> httpx.Response:
    """A downgrade of the mock's seeded dev deployment (Performance) to Basic."""
    return await client.post(
        "/v2/subscription/plan/quote",
        json={
            "deployment_id": DEV_DEPLOYMENT_ID,
            "target_plan_slug": "compute_basic",
            "target_billing_term_months": 1,
        },
    )


async def _subscription_plan_change(client: httpx.AsyncClient) -> httpx.Response:
    quote = await _created(await _subscription_plan_quote(client))
    assert "operation_id" in quote, quote
    return await client.post(
        "/v2/subscription/plan/change",
        headers={"Idempotency-Key": quote["operation_id"]},
        json={"operation_id": quote["operation_id"]},
    )


async def _subscription_plan_operation(client: httpx.AsyncClient) -> httpx.Response:
    changed = await _subscription_plan_change(client)
    assert changed.status_code == 202, changed.text
    name = changed.json()["name"]
    return await client.get(f"/v2/operations/{name.removeprefix('operations/')}")


async def _subscription_plan_cancel_scheduled_change(client: httpx.AsyncClient) -> httpx.Response:
    changed = await _subscription_plan_change(client)
    assert changed.status_code == 202, changed.text
    return await client.post(
        "/v2/subscription/plan/cancel-scheduled-change",
        json={"deployment_id": DEV_DEPLOYMENT_ID},
    )


async def _subscription_list(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/subscriptions")


async def _subscription_reusable(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/subscriptions/reusable")


async def _subscription_checkout(client: httpx.AsyncClient, funding_source: str) -> httpx.Response:
    return await client.post(
        "/v2/subscription/checkout",
        headers={"Idempotency-Key": new_key(f"conformance-checkout-{funding_source}")},
        json={"plan_slug": "compute_basic", "funding_source": funding_source},
    )


async def _subscription_checkout_card(client: httpx.AsyncClient) -> httpx.Response:
    return await _subscription_checkout(client, "stripe")


async def _subscription_checkout_wallet(client: httpx.AsyncClient) -> httpx.Response:
    return await _subscription_checkout(client, "wallet")


async def _subscription_fix_payment(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v2/subscription/fix-payment", json={})


async def _subscription_portal(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v2/subscription/portal", json={"return_context": "wallet"})


async def _identity_me(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v1/me")


async def _identity_agent_environments(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v1/agent-environments")


async def _stub_notifications_list(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v1/me/notifications", params={"limit": 20})


async def _stub_notifications_read_all(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post("/v1/me/notifications/read-all")


async def _stub_binding_read(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/wallet-binding")


async def _stub_usage(client: httpx.AsyncClient) -> httpx.Response:
    return await client.get("/v2/usage")


CASES = [
    pytest.param(Conformance(_identity_me, "V1UserResponse"), id="identity_me"),
    pytest.param(
        Conformance(_identity_agent_environments, "V1AgentEnvironmentsResponse"),
        id="identity_agent_environments",
    ),
    pytest.param(
        Conformance(
            _stub_notifications_list,
            "AccountNotificationListResponse",
            expect={"items": [], "unread_count": 0, "next_cursor": None},
        ),
        id="stub_notifications_list",
    ),
    pytest.param(
        Conformance(
            _stub_notifications_read_all,
            "AccountNotificationReadAllResponse",
            expect={"updated_count": 0},
        ),
        id="stub_notifications_read_all",
    ),
    pytest.param(
        Conformance(_stub_binding_read, "V2WalletBindingResponse"), id="stub_binding_read"
    ),
    pytest.param(Conformance(_stub_usage, "V2HostedUsageSummaryResponse"), id="stub_usage"),
    pytest.param(Conformance(_wallet_topup, "V2WalletTopupResponse"), id="wallet_topup"),
    pytest.param(
        Conformance(_wallet_topup_status, "V2WalletTopupResponse"), id="wallet_topup_status"
    ),
    pytest.param(
        Conformance(_wallet_setup_intent, "V2WalletAutoReloadSetupResponse"),
        id="wallet_auto_reload_setup_intent",
    ),
    # Mock only: a hosted SetupIntent is unconfirmed until Stripe.js confirms it (06.2-16).
    pytest.param(
        Conformance(_wallet_setup_finalize, "V2WalletResponse"),
        id="wallet_auto_reload_setup_finalize",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(_wallet_auto_reload_update, "V2WalletResponse"),
        id="wallet_auto_reload_update",
    ),
    pytest.param(
        Conformance(_wallet_payment_methods, "V2WalletSavedCardsResponse"),
        id="wallet_payment_methods",
    ),
    pytest.param(
        Conformance(_wallet_transactions, "V2WalletTransactionsResponse"),
        id="wallet_transactions",
    ),
    # Mock only: a hosted plan change moves money and needs a deployment bound to compute
    # (06.2-08 decision; hosted plan-change behavior is proven by the hosted tests, 06.2-22).
    pytest.param(
        Conformance(_subscription_plan_quote, "V2ComputePlanChangeQuoteResponse"),
        id="subscription_plan_quote",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(
            _subscription_plan_change,
            "LongRunningOperation",
            expect={"done": True},
            status=202,
        ),
        id="subscription_plan_change",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(_subscription_plan_operation, "LongRunningOperation", expect={"done": True}),
        id="subscription_plan_operation",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(
            _subscription_plan_cancel_scheduled_change,
            "V2ComputeSubscriptionActionResponse",
            expect={"pending_plan_slug": None},
        ),
        id="subscription_plan_cancel_scheduled_change",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(_subscription_list, "V2ComputeSubscriptionListResponse"),
        id="subscription_list",
    ),
    pytest.param(
        Conformance(_subscription_reusable, "V2ComputeReusableSubscriptionsResponse"),
        id="subscription_reusable",
    ),
    # Card funding without deploy_config opens a Checkout Session and moves no money.
    pytest.param(
        Conformance(
            _subscription_checkout_card,
            "V2CheckoutResponse",
            expect={"flow_type": "checkout_session"},
        ),
        id="subscription_checkout_card",
    ),
    # Mock only: the wallet arm debits the wallet (06.2-08 decision; hosted: 06.2-17).
    pytest.param(
        Conformance(
            _subscription_checkout_wallet,
            "V2CheckoutResponse",
            expect={"flow_type": "subscription_activation", "checkout_url": ""},
        ),
        id="subscription_checkout_wallet",
        marks=pytest.mark.mock_only,
    ),
    pytest.param(
        Conformance(_subscription_fix_payment, "V2PortalResponse"), id="subscription_fix_payment"
    ),
    pytest.param(Conformance(_subscription_portal, "V2PortalResponse"), id="subscription_portal"),
]


@pytest.mark.parametrize("case", CASES)
async def test_response_conforms(case: Conformance, v2_dashboard: httpx.AsyncClient) -> None:
    response = await case.call(v2_dashboard)

    assert response.status_code == case.status, response.text
    body = response.json()
    validate_component(case.schema, body)
    for key, value in (case.expect or {}).items():
        assert key in body, f"response omits {key}"
        assert body[key] == value, f"{key}: {body[key]!r} != {value!r}"


@pytest.mark.parametrize(
    "method",
    [
        pytest.param("PATCH", id="stub_notification_update"),
        pytest.param("DELETE", id="stub_notification_delete"),
    ],
)
async def test_notification_mutation_is_not_found(
    method: str, v2_dashboard: httpx.AsyncClient
) -> None:
    """No notification exists yet (06.2 D-04), so marking or deleting one is a 404."""
    path = f"/v1/me/notifications/{uuid.uuid4()}"
    if method == "PATCH":
        response = await v2_dashboard.patch(path, json={"read": True})
    else:
        response = await v2_dashboard.delete(path)

    assert response.status_code == 404, response.text
    detail = response.json().get("detail")
    assert isinstance(detail, dict), response.text
    assert detail.get("code") == "notification_not_found"
