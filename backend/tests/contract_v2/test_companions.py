"""Fork-owned /v2 companion contract (06.2 SC-1, D-11, D-17), against the mock or a hosted target.

Naming, so later plans can select by ``-k``: quote cases ``test_quote_*``, quote reads
``test_quote_read_*``, line starts ``test_line_start_*``, stops ``test_line_stop_*``, list cases
``test_line_items_list_*``, auth cases ``test_companion_routes_*``; every case that needs an
expired quote has ``expired`` in its name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

import httpx
import pytest

from scripts import mock_deploy_api
from tests.contract_v2.conftest import (
    HOSTED,
    new_installation_id,
    new_key,
    quote,
    request_quote,
    start_line,
    stop_line,
)


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


LIST_ITEM_KEYS = {
    "line_item_id",
    "installation_id",
    "owner_user_id",
    "quote_id",
    "monthly_price_usd_micros",
    "started_at",
    "stopped_at",
    "funding_source",
    "charged_cents",
    "credited_cents",
}


def _assert_line_amounts(item: dict, monthly_price_usd_micros: int, *, stopped: bool) -> None:
    """A wallet line was charged its first month and, once stopped, credited at most that; a card
    line's amounts are null (INT-4)."""
    assert item["funding_source"] in {"wallet", "stripe"}
    if item["funding_source"] == "stripe":
        assert (item["charged_cents"], item["credited_cents"]) == (None, None)
        return
    month_cents = monthly_price_usd_micros // 10_000
    assert item["charged_cents"] == month_cents
    if stopped:
        assert 0 <= item["credited_cents"] <= item["charged_cents"]
    else:
        assert item["credited_cents"] == 0


async def _two_lines_one_stopped(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> tuple[dict, dict, dict, dict]:
    """Start two lines for the owner and one for the other owner, then stop one of the owner's.

    Returns (quote, active line, stopped line, other owner's line), each line carrying the
    ``installation_id`` it was started for.
    """
    subscribed(owner_id)
    subscribed(other_owner_id)
    owner_quote = await quote(v2_service, owner_id, priced_pair)
    other_quote = await quote(v2_service, other_owner_id, priced_pair)
    lines = []
    for owner, line_quote in ((owner_id, owner_quote), (owner_id, owner_quote)):
        installation_id = new_installation_id()
        started = await start_line(
            v2_service, line_quote["quote_id"], installation_id, owner, new_key("start")
        )
        assert started.status_code == 200, started.text
        lines.append({**started.json(), "installation_id": installation_id})
    other_installation_id = new_installation_id()
    other = await start_line(
        v2_service,
        other_quote["quote_id"],
        other_installation_id,
        other_owner_id,
        new_key("start"),
    )
    assert other.status_code == 200, other.text
    other_line = {**other.json(), "installation_id": other_installation_id}
    active, stopped = lines
    stop = await stop_line(v2_service, stopped["line_item_id"], new_key("stop"))
    assert stop.status_code == 200, stop.text
    return owner_quote, active, stopped, other_line


async def test_line_items_list_returns_only_the_owners_active_lines(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    owner_quote, active, stopped, other_line = await _two_lines_one_stopped(
        v2_service, owner_id, other_owner_id, priced_pair, subscribed
    )

    response = await v2_service.get("/v2/companions/line-items", params={"owner_user_id": owner_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is None
    items = body["items"]
    for item in items:
        assert set(item) == LIST_ITEM_KEYS
        assert item["owner_user_id"] == owner_id
        assert item["stopped_at"] is None
    listed_ids = {item["line_item_id"] for item in items}
    assert stopped["line_item_id"] not in listed_ids
    assert other_line["line_item_id"] not in listed_ids
    ours = [item for item in items if item["line_item_id"] == active["line_item_id"]]
    assert len(ours) == 1
    (item,) = ours
    assert item["installation_id"] == active["installation_id"]
    assert item["quote_id"] == owner_quote["quote_id"]
    assert item["started_at"] == active["started_at"]
    assert item["monthly_price_usd_micros"] == owner_quote["monthly_price_usd_micros"]
    _assert_line_amounts(item, owner_quote["monthly_price_usd_micros"], stopped=False)
    if not HOSTED:
        # The mock owner is fresh, so the active line is the only one it has.
        assert len(items) == 1


async def test_line_items_list_state_all_includes_stopped_lines(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, active, stopped, other_line = await _two_lines_one_stopped(
        v2_service, owner_id, other_owner_id, priced_pair, subscribed
    )

    response = await v2_service.get(
        "/v2/companions/line-items", params={"owner_user_id": owner_id, "state": "all"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    by_id = {item["line_item_id"]: item for item in body["items"]}
    assert {active["line_item_id"], stopped["line_item_id"]} <= set(by_id)
    assert other_line["line_item_id"] not in by_id
    assert all(item["owner_user_id"] == owner_id for item in body["items"])
    assert by_id[active["line_item_id"]]["stopped_at"] is None
    stopped_at = by_id[stopped["line_item_id"]]["stopped_at"]
    assert isinstance(stopped_at, str)
    assert stopped_at.endswith("Z")
    if not HOSTED:
        assert len(body["items"]) == 2


async def test_line_items_list_state_all_carries_each_lines_funding_and_amounts(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    owner_quote, active, stopped, _ = await _two_lines_one_stopped(
        v2_service, owner_id, other_owner_id, priced_pair, subscribed
    )

    response = await v2_service.get(
        "/v2/companions/line-items", params={"owner_user_id": owner_id, "state": "all"}
    )

    assert response.status_code == 200, response.text
    by_id = {item["line_item_id"]: item for item in response.json()["items"]}
    for item in by_id.values():
        assert set(item) == LIST_ITEM_KEYS
    monthly = owner_quote["monthly_price_usd_micros"]
    _assert_line_amounts(by_id[active["line_item_id"]], monthly, stopped=False)
    _assert_line_amounts(by_id[stopped["line_item_id"]], monthly, stopped=True)


async def test_line_items_list_since_keeps_the_lines_running_at_or_after_it(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, active, stopped, _ = await _two_lines_one_stopped(
        v2_service, owner_id, other_owner_id, priced_pair, subscribed
    )
    every = await v2_service.get(
        "/v2/companions/line-items", params={"owner_user_id": owner_id, "state": "all"}
    )
    assert every.status_code == 200, every.text
    stopped_at = {item["line_item_id"]: item for item in every.json()["items"]}[
        stopped["line_item_id"]
    ]["stopped_at"]
    after = (_parse_time(stopped_at) + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")

    at_stop = await v2_service.get(
        "/v2/companions/line-items",
        params={"owner_user_id": owner_id, "state": "all", "since": stopped_at},
    )
    past_stop = await v2_service.get(
        "/v2/companions/line-items",
        params={"owner_user_id": owner_id, "state": "all", "since": after},
    )

    assert at_stop.status_code == 200, at_stop.text
    assert past_stop.status_code == 200, past_stop.text
    at_ids = {item["line_item_id"] for item in at_stop.json()["items"]}
    past_ids = {item["line_item_id"] for item in past_stop.json()["items"]}
    assert {active["line_item_id"], stopped["line_item_id"]} <= at_ids
    assert active["line_item_id"] in past_ids
    assert stopped["line_item_id"] not in past_ids
    for item in past_stop.json()["items"]:
        assert item["stopped_at"] is None or _parse_time(item["stopped_at"]) >= _parse_time(after)


async def test_line_items_list_requires_a_bearer_credential(
    v2_anonymous: httpx.AsyncClient, owner_id: str
) -> None:
    response = await v2_anonymous.get(
        "/v2/companions/line-items", params={"owner_user_id": owner_id}
    )

    assert response.status_code == 401, response.text
    assert response.json()["detail"]["code"] == "unauthenticated"


async def test_line_items_list_requires_owner_user_id(v2_service: httpx.AsyncClient) -> None:
    response = await v2_service.get("/v2/companions/line-items")

    assert response.status_code == 422, response.text


QUOTE_KEYS = {
    "quote_id",
    "owner_user_id",
    "companion_key",
    "isolation_tier",
    "currency",
    "monthly_price_usd_micros",
    "storage_price_usd_micros_per_gib_month",
    "expires_at",
}
LINE_START_KEYS = {"line_item_id", "started_at", "monthly_price_usd_micros"}
LINE_STOP_KEYS = {"line_item_id", "stopped_at"}
IDEMPOTENCY_KEY_REQUIRED = "Idempotency-Key is required"


def _code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


async def _started_line(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> tuple[dict, dict]:
    """A subscribed owner's quote and the line started on it."""
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)
    started = await start_line(
        v2_service, line_quote["quote_id"], new_installation_id(), owner_id, new_key("start")
    )
    assert started.status_code == 200, started.text
    return line_quote, started.json()


# --- quote -----------------------------------------------------------------------------------


async def test_quote_returns_the_quote_view(
    v2_service: httpx.AsyncClient, owner_id: str, priced_pair: tuple[str, str]
) -> None:
    response = await request_quote(v2_service, owner_id, priced_pair)

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == QUOTE_KEYS
    assert body["quote_id"].startswith("cq_")
    assert body["owner_user_id"] == owner_id
    assert (body["companion_key"], body["isolation_tier"]) == priced_pair
    assert body["currency"] == "usd"
    assert isinstance(body["monthly_price_usd_micros"], int)
    assert isinstance(body["storage_price_usd_micros_per_gib_month"], int)
    assert body["expires_at"].endswith("Z")


async def test_quote_unpriced_tier_is_tier_not_priced(
    v2_service: httpx.AsyncClient, owner_id: str, unpriced_pair: tuple[str, str]
) -> None:
    response = await request_quote(v2_service, owner_id, unpriced_pair)

    assert response.status_code == 422, response.text
    assert _code(response) == "tier_not_priced"


# --- quote read ------------------------------------------------------------------------------


async def test_quote_read_returns_the_same_quote_view(
    v2_service: httpx.AsyncClient, owner_id: str, priced_pair: tuple[str, str]
) -> None:
    issued = await quote(v2_service, owner_id, priced_pair)

    response = await v2_service.get(f"/v2/companions/quotes/{issued['quote_id']}")

    assert response.status_code == 200, response.text
    assert response.json() == issued


async def test_quote_read_unknown_quote_is_quote_not_found(v2_service: httpx.AsyncClient) -> None:
    response = await v2_service.get(f"/v2/companions/quotes/cq_{new_key('missing')}")

    assert response.status_code == 404, response.text
    assert _code(response) == "quote_not_found"


async def test_quote_read_expired_quote_is_quote_expired(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    expire_quote: Callable[[dict], Awaitable[None]],
) -> None:
    issued = await quote(v2_service, owner_id, priced_pair)
    await expire_quote(issued)

    response = await v2_service.get(f"/v2/companions/quotes/{issued['quote_id']}")

    assert response.status_code == 410, response.text
    assert _code(response) == "quote_expired"


# --- line start ------------------------------------------------------------------------------


async def test_line_start_returns_the_line_at_the_quoted_amount(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    line_quote, line = await _started_line(v2_service, owner_id, priced_pair, subscribed)

    assert set(line) == LINE_START_KEYS
    assert line["line_item_id"].startswith("cli_")
    assert line["started_at"].endswith("Z")
    assert line["monthly_price_usd_micros"] == line_quote["monthly_price_usd_micros"]


async def test_line_start_replay_same_installation_returns_the_same_line(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)
    installation_id = new_installation_id()
    key = new_key("start")
    first = await start_line(v2_service, line_quote["quote_id"], installation_id, owner_id, key)
    assert first.status_code == 200, first.text

    replay = await start_line(v2_service, line_quote["quote_id"], installation_id, owner_id, key)

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


async def test_line_start_replay_different_installation_is_idempotency_key_reused(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)
    key = new_key("start")
    first = await start_line(
        v2_service, line_quote["quote_id"], new_installation_id(), owner_id, key
    )
    assert first.status_code == 200, first.text

    reused = await start_line(
        v2_service, line_quote["quote_id"], new_installation_id(), owner_id, key
    )

    assert reused.status_code == 409, reused.text
    assert _code(reused) == "idempotency_key_reused"


async def test_line_start_unknown_quote_is_quote_not_found(
    v2_service: httpx.AsyncClient, owner_id: str, subscribed: Callable[[str], None]
) -> None:
    subscribed(owner_id)

    response = await start_line(
        v2_service, f"cq_{new_key('missing')}", new_installation_id(), owner_id, new_key("start")
    )

    assert response.status_code == 404, response.text
    assert _code(response) == "quote_not_found"


async def test_line_start_other_owners_quote_is_quote_owner_mismatch(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    other_owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    subscribed(owner_id)
    subscribed(other_owner_id)
    other_quote = await quote(v2_service, other_owner_id, priced_pair)

    response = await start_line(
        v2_service, other_quote["quote_id"], new_installation_id(), owner_id, new_key("start")
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "quote_owner_mismatch"


async def test_line_start_on_expired_quote_is_honored(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
    expire_quote: Callable[[dict], Awaitable[None]],
) -> None:
    """The quote window governs the install confirmation, not the line start."""
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)
    await expire_quote(line_quote)

    response = await start_line(
        v2_service, line_quote["quote_id"], new_installation_id(), owner_id, new_key("start")
    )

    assert response.status_code == 200, response.text
    assert response.json()["monthly_price_usd_micros"] == line_quote["monthly_price_usd_micros"]


async def test_line_start_without_idempotency_key_is_400(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)

    response = await v2_service.post(
        "/v2/companions/line-items",
        json={
            "quote_id": line_quote["quote_id"],
            "installation_id": new_installation_id(),
            "owner_user_id": owner_id,
        },
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == IDEMPOTENCY_KEY_REQUIRED


async def test_line_start_owner_without_subscription_is_no_active_subscription(
    v2_service: httpx.AsyncClient, fresh_owner_id: str, priced_pair: tuple[str, str]
) -> None:
    """A companion line follows the owner's compute subscription (06.2 D-11, Pitfall 10)."""
    line_quote = await quote(v2_service, fresh_owner_id, priced_pair)

    response = await start_line(
        v2_service,
        line_quote["quote_id"],
        new_installation_id(),
        fresh_owner_id,
        new_key("start"),
    )

    assert response.status_code == 409, response.text
    assert _code(response) == "no_active_subscription"


@pytest.mark.mock_only
async def test_line_start_replay_after_subscription_canceled_returns_the_same_line(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    """A replay never re-checks the subscription, so a retried start after a cancel converges."""
    subscribed(owner_id)
    line_quote = await quote(v2_service, owner_id, priced_pair)
    installation_id = new_installation_id()
    key = new_key("start")
    first = await start_line(v2_service, line_quote["quote_id"], installation_id, owner_id, key)
    assert first.status_code == 200, first.text
    mock_deploy_api.SUBSCRIPTIONS[owner_id]["status"] = "canceled"

    replay = await start_line(v2_service, line_quote["quote_id"], installation_id, owner_id, key)

    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


# --- line stop -------------------------------------------------------------------------------


async def test_line_stop_returns_stopped_at(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, line = await _started_line(v2_service, owner_id, priced_pair, subscribed)

    response = await stop_line(v2_service, line["line_item_id"], new_key("stop"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == LINE_STOP_KEYS
    assert body["line_item_id"] == line["line_item_id"]
    assert body["stopped_at"].endswith("Z")


async def test_line_stop_repeat_returns_the_same_stopped_at(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, line = await _started_line(v2_service, owner_id, priced_pair, subscribed)
    first = await stop_line(v2_service, line["line_item_id"], new_key("stop"))
    assert first.status_code == 200, first.text

    repeat = await stop_line(v2_service, line["line_item_id"], new_key("stop"))

    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["stopped_at"] == first.json()["stopped_at"]


async def test_line_stop_stopped_at_never_precedes_started_at(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, line = await _started_line(v2_service, owner_id, priced_pair, subscribed)

    response = await stop_line(v2_service, line["line_item_id"], new_key("stop"))

    assert response.status_code == 200, response.text
    assert _parse_time(response.json()["stopped_at"]) >= _parse_time(line["started_at"])


async def test_line_stop_unknown_line_is_line_item_not_found(
    v2_service: httpx.AsyncClient,
) -> None:
    response = await stop_line(v2_service, f"cli_{new_key('missing')}", new_key("stop"))

    assert response.status_code == 404, response.text
    assert _code(response) == "line_item_not_found"


async def test_line_stop_without_idempotency_key_is_400(
    v2_service: httpx.AsyncClient,
    owner_id: str,
    priced_pair: tuple[str, str],
    subscribed: Callable[[str], None],
) -> None:
    _, line = await _started_line(v2_service, owner_id, priced_pair, subscribed)

    response = await v2_service.delete(f"/v2/companions/line-items/{line['line_item_id']}")

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == IDEMPOTENCY_KEY_REQUIRED


# --- auth ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v2/companions/quote"),
        ("GET", "/v2/companions/quotes/cq_contract_anonymous"),
        ("POST", "/v2/companions/line-items"),
        ("DELETE", "/v2/companions/line-items/cli_contract_anonymous"),
        ("GET", "/v2/companions/line-items"),
    ],
)
async def test_companion_routes_require_a_bearer_credential(
    v2_anonymous: httpx.AsyncClient, owner_id: str, method: str, path: str
) -> None:
    body = None
    if method == "POST" and path.endswith("/quote"):
        body = {"owner_user_id": owner_id, "companion_key": "postiz", "isolation_tier": "standard"}
    elif method == "POST":
        body = {
            "quote_id": "cq_contract_anonymous",
            "installation_id": new_installation_id(),
            "owner_user_id": owner_id,
        }
    params = (
        {"owner_user_id": owner_id} if method == "GET" and path.endswith("line-items") else None
    )

    response = await v2_anonymous.request(
        method,
        path,
        json=body,
        params=params,
        headers={"Idempotency-Key": new_key("anonymous")},
    )

    assert response.status_code == 401, response.text
    assert _code(response) == "unauthenticated"
