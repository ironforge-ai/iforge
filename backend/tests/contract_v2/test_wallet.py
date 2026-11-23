"""The dashboard's wallet surface (06.2 SC-2, D-13, D-22), against the mock or a hosted target.

Naming, so later plans can select by ``-k``: every case is ``test_wallet_*``; setup-intent and
finalize cases name ``setup``, the auto-reload update names ``auto_reload``, saved cards name
``payment_methods``.

Idempotency (D-22, the client retry contract): the web client retries ``POST /v2/wallet/topup``
and ``POST /v2/wallet/auto-reload/setup-intent`` once on a network error under the same
``Idempotency-Key``, so a repeated key with the same body must answer the original body, and the
same key with a different body is ``409 idempotency_key_reused``. Every case uses a fresh key so a
hosted run never replays a previous run's request.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import httpx
import pytest

from tests.contract_v2.conftest import new_key
from tests.contract_v2.schema import validate_component

CONSENT_VERSION = "wallet_auto_reload_off_session_v2"
AMOUNT_POLICY = "wallet_reload_configured_plus_negative_balance_v1"
SETUP_TERMS = {
    "consent_version": CONSENT_VERSION,
    "auto_reload_threshold_usd": "10.00",
    "auto_reload_amount_cents": 1000,
    "auto_reload_monthly_cap_cents": 5000,
}
SETUP_IDENTITY = re.compile(r"^wsetup_[0-9a-f]{64}$")


def _code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


async def _topup(
    client: httpx.AsyncClient, key: str | None, body: dict[str, Any]
) -> httpx.Response:
    headers = {"Idempotency-Key": key} if key is not None else {}
    return await client.post("/v2/wallet/topup", headers=headers, json=body)


async def _setup_intent(
    client: httpx.AsyncClient, key: str, body: dict[str, Any]
) -> httpx.Response:
    return await client.post(
        "/v2/wallet/auto-reload/setup-intent", headers={"Idempotency-Key": key}, json=body
    )


async def test_wallet_topup_opens_a_checkout_session_and_replays_a_repeated_key(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    key = new_key("wallet-topup")

    first = await _topup(v2_dashboard, key, {"amount_cents": 2500})
    replay = await _topup(v2_dashboard, key, {"amount_cents": 2500})
    reused = await _topup(v2_dashboard, key, {"amount_cents": 2600})

    assert first.status_code == 200, first.text
    body = first.json()
    assert body["flow_type"] == "checkout_session"
    assert isinstance(body["checkout_session_id"], str)
    assert body["checkout_session_id"]
    assert replay.status_code == 200, replay.text
    assert replay.json() == body
    assert reused.status_code == 409, reused.text
    assert _code(reused) == "idempotency_key_reused"


async def test_wallet_topup_requires_an_idempotency_key(v2_dashboard: httpx.AsyncClient) -> None:
    response = await _topup(v2_dashboard, None, {"amount_cents": 2500})

    assert response.status_code == 400, response.text


async def test_wallet_topup_rejects_a_non_positive_amount(v2_dashboard: httpx.AsyncClient) -> None:
    response = await _topup(v2_dashboard, new_key("wallet-topup-zero"), {"amount_cents": 0})

    assert response.status_code == 422, response.text


async def test_wallet_topup_status_reads_the_checkout_session(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    created = await _topup(v2_dashboard, new_key("wallet-topup-status"), {"amount_cents": 2500})
    assert created.status_code == 200, created.text
    assert "checkout_session_id" in created.json(), created.text
    checkout_session_id = created.json()["checkout_session_id"]

    response = await v2_dashboard.get(f"/v2/wallet/topup/checkout/{checkout_session_id}")

    assert response.status_code == 200, response.text
    assert response.json()["checkout_session_id"] == checkout_session_id


async def test_wallet_topup_status_unknown_session_is_not_found(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await v2_dashboard.get("/v2/wallet/topup/checkout/cs_contract_unknown")

    assert response.status_code == 404, response.text
    assert _code(response) == "checkout_session_not_found"


async def test_wallet_setup_intent_echoes_the_consented_terms_and_replays(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    key = new_key("wallet-setup")

    first = await _setup_intent(v2_dashboard, key, SETUP_TERMS)
    replay = await _setup_intent(v2_dashboard, key, SETUP_TERMS)

    assert first.status_code == 200, first.text
    body = first.json()
    assert SETUP_IDENTITY.match(body["setup_identity"])
    assert body["setup_intent_id"].startswith("seti_")
    assert body["consent_version"] == CONSENT_VERSION
    assert body["amount_policy"] == AMOUNT_POLICY
    assert Decimal(body["auto_reload_threshold_usd"]) == Decimal("10.00")
    assert body["auto_reload_amount_cents"] == 1000
    assert body["auto_reload_monthly_cap_cents"] == 5000
    assert replay.status_code == 200, replay.text
    assert replay.json() == body


async def test_wallet_setup_intent_rejects_a_stale_consent_version(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    body = {**SETUP_TERMS, "consent_version": "wallet_auto_reload_off_session_v1"}

    response = await _setup_intent(v2_dashboard, new_key("wallet-setup-stale"), body)

    assert response.status_code == 422, response.text
    assert _code(response) == "consent_version_mismatch"


@pytest.mark.mock_only
async def test_wallet_setup_finalize_enables_auto_reload(v2_dashboard: httpx.AsyncClient) -> None:
    """Mock only: on a hosted target the SetupIntent stays unconfirmed until Stripe.js confirms
    it, so finalize answers ``409 setup_not_complete`` there (proven in the hosted tests, 06.2-16).
    """
    created = await _setup_intent(v2_dashboard, new_key("wallet-setup-finalize"), SETUP_TERMS)
    assert created.status_code == 200, created.text
    setup = created.json()

    response = await v2_dashboard.post(
        "/v2/wallet/auto-reload/setup-intent/finalize",
        json={
            "setup_identity": setup["setup_identity"],
            "setup_intent_id": setup["setup_intent_id"],
        },
    )

    assert response.status_code == 200, response.text
    wallet = response.json()
    assert wallet["auto_reload_enabled"] is True
    assert wallet["auto_reload_status"] == "active"
    assert wallet["auto_reload_amount_cents"] == 1000
    assert wallet["auto_reload_monthly_cap_cents"] == 5000


async def test_wallet_setup_finalize_unknown_setup_is_not_found(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await v2_dashboard.post(
        "/v2/wallet/auto-reload/setup-intent/finalize",
        json={"setup_identity": "wsetup_" + "0" * 64, "setup_intent_id": "seti_contract_unknown"},
    )

    assert response.status_code == 404, response.text
    assert _code(response) == "setup_not_found"


async def test_wallet_auto_reload_update_turns_auto_reload_off(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    response = await v2_dashboard.put("/v2/wallet/auto-reload", json={"auto_reload_enabled": False})

    assert response.status_code == 200, response.text
    wallet = response.json()
    assert wallet["auto_reload_enabled"] is False
    assert wallet["auto_reload_status"] == "off"


async def test_wallet_payment_methods_lists_saved_cards(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    """Card count and pagination depend on the account; an unconfirmed setup saves no card."""
    response = await v2_dashboard.get("/v2/wallet/payment-methods")

    assert response.status_code == 200, response.text
    validate_component("V2WalletSavedCardsResponse", response.json())


@pytest.mark.mock_only
async def test_wallet_payment_methods_lists_the_mock_saved_card(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    """Only the mock seeds exactly one saved default/auto-reload card."""
    response = await v2_dashboard.get("/v2/wallet/payment-methods")

    assert response.status_code == 200, response.text
    body = response.json()
    validate_component("V2WalletSavedCardsResponse", body)
    assert body["has_more"] is False
    assert len(body["items"]) == 1
    card = body["items"][0]
    assert card["id"] == "pm_mock_visa"
    assert card["is_default"] is True
    assert card["is_auto_reload"] is True


@pytest.mark.mock_only
async def test_wallet_transactions_include_a_companion_charge(
    v2_dashboard: httpx.AsyncClient,
) -> None:
    """Mock only (UI-SPEC H4.4, mock first): the item exists only where a companion line has
    billed the wallet, which only the mock's fixture arranges; the hosted ledger proves it in its
    own tests.
    """
    response = await v2_dashboard.get("/v2/wallet/transactions")

    assert response.status_code == 200, response.text
    companion = [item for item in response.json()["items"] if item["kind"] == "companion_charge"]
    assert companion, "no companion_charge item in /v2/wallet/transactions"
    item = companion[0]
    assert item["direction"] == "debit"
    assert item["status"] == "applied"
    assert item["funding"] == "wallet"
    assert item["context"] is None
