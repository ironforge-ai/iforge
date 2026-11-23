"""Offline regressions for wallet contract assertions, independent of account state."""

import httpx
import pytest

from tests.contract_v2 import test_wallet as wallet_contract


@pytest.mark.parametrize(
    ("card_count", "has_more"),
    [(0, False), (1, False), (2, False), (1, True)],
    ids=["no-saved-cards", "one-saved-card", "multiple-saved-cards", "more-cards"],
)
async def test_payment_methods_contract_accepts_valid_account_state(
    card_count: int, has_more: bool
) -> None:
    items = [
        {
            "id": f"pm_test_{index}",
            "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
            "is_default": index == 0,
            "is_auto_reload": False,
        }
        for index in range(card_count)
    ]

    def payment_methods(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/wallet/payment-methods"
        return httpx.Response(200, json={"items": items, "has_more": has_more})

    async with httpx.AsyncClient(
        base_url="http://wallet.test", transport=httpx.MockTransport(payment_methods)
    ) as client:
        await wallet_contract.test_wallet_payment_methods_lists_saved_cards(client)


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [], "has_more": "false"},
        {"items": [{"id": "pm_incomplete"}], "has_more": False},
    ],
    ids=["invalid-pagination-type", "missing-card-fields"],
)
async def test_payment_methods_contract_rejects_invalid_responses(
    payload: dict[str, object],
) -> None:
    async with httpx.AsyncClient(
        base_url="http://wallet.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
    ) as client:
        with pytest.raises(AssertionError, match="V2WalletSavedCardsResponse"):
            await wallet_contract.test_wallet_payment_methods_lists_saved_cards(client)
