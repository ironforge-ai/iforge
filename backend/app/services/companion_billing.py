"""Companion pricing and billing against the hosted control plane's ``/v2`` (APP-08, D-07).

ADR-0005 puts money movement in the hosted control plane, so this module holds no price and moves
no money: it asks ``/v2`` for a quote, checks one, starts and stops a line item, and lists an
owner's active lines for the orphan sweep (06.2 D-17, ``companion_orphan_sweep``). The contract's
executable reference is ``scripts/mock_deploy_api.py``; the real per-tier prices are the hosted
service's configuration.

The lifecycle properties this module makes true:

* **Priced before provisioning.** ``create_installation`` calls ``verify_companion_quote`` before
  it writes a row, so nothing exists for an install whose tenant has not been shown a current price
  for exactly that companion, tier and owner.
* **Billed only on completion.** ``BILLING_START_STEP`` is a completion step of the lifecycle
  worker, after ``EDGE_PUBLISH_STEP``: completion steps run only for a job the runtime reported
  ready, and the hostname is published first, so a failed or unreachable install is never billed.
* **Idempotent.** A line item is started and stopped under Idempotency-Keys derived from the
  installation id, so a retried call returns the line (or the stop) it already made.
* **Fail closed.** An unconfigured, unreachable or erroring ``/v2`` is
  ``companion_billing_unavailable``: no quote, so no install.
* **One owner key (06.2 D-09).** Every ``owner_user_id`` sent to ``/v2`` is the owner's Clerk user
  id (``users.clerk_id``), derived only by ``hosted_owner.hosted_owner_id`` -- the key the hosted
  service stores the dashboard's subscription and wallet under -- never the internal ``users.id``.
  A principal with no Clerk id (a partner tenant) has no hosted owner and is never quoted or
  billed: callers refuse it before any ``/v2`` call.

The deploy API key is sent only as the Authorization header to the configured base URL and is
never logged; nothing here logs a response body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote as url_quote
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select, update

from app.core.config import settings
from app.models.companion_installation import CompanionInstallation
from app.models.user import User
from app.services.companion_lifecycle_steps import (
    CompanionStepError,
    CompanionStepRetryableError,
    JsonObject,
    LifecycleStepContext,
    ProvisioningStep,
)
from app.services.hosted_owner import hosted_owner_id

_DEPLOY_API_TIMEOUT_SECONDS = 10.0

# Test seam (the 03-04/03-06 transport-injection pattern ``tenant_purge`` uses): tests point this
# at an ASGI transport over the mock /v2 app. None means the default network transport.
_transport_override: httpx.AsyncBaseTransport | None = None

BILLING_START_STEP_NAME = "billing-start"
# A /v2 outage at billing start is retried this many times in all (with the worker's backoff)
# before the install fails and is torn down -- it never completes unbilled.
BILLING_START_MAX_ATTEMPTS = 3


class CompanionBillingUnavailableError(RuntimeError):
    """``/v2`` is unconfigured, unreachable, or answered in a way this client cannot trust."""


class CompanionTierNotPricedError(RuntimeError):
    """``/v2`` has no price for this companion at this tier, so it cannot be installed."""


class CompanionQuoteExpiredError(RuntimeError):
    """The quote's validity window has passed; the tenant must be quoted again."""


class CompanionBillingRefusedError(RuntimeError):
    """``/v2`` refused a line-item call with a 4xx: retrying the same call cannot succeed."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"/v2 refused the call with {status_code}")
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class CompanionQuote:
    quote_id: str
    owner_user_id: str
    companion_key: str
    isolation_tier: str
    currency: str
    monthly_price_usd_micros: int
    storage_price_usd_micros_per_gib_month: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CompanionLineItem:
    line_item_id: str
    started_at: datetime
    monthly_price_usd_micros: int


@dataclass(frozen=True, slots=True)
class CompanionLineItemRecord:
    """One line as ``GET /v2/companions/line-items`` lists it: the orphan sweep (D-17) and the
    billing reconciliation's (INT-4) input.

    ``funding_source`` and, for a wallet line, ``charged_cents`` / ``credited_cents`` (what the
    hosted ledger debited and credited for the line) are additive fields: ``None`` when /v2 does
    not send them, and both amounts ``None`` for a card line, whose money Stripe holds.
    """

    line_item_id: str
    installation_id: str
    owner_user_id: str
    quote_id: str | None
    monthly_price_usd_micros: int
    started_at: datetime
    stopped_at: datetime | None
    funding_source: str | None = None
    charged_cents: int | None = None
    credited_cents: int | None = None


def _deploy_headers() -> dict[str, str]:
    # Twin of ``tenant_purge._deploy_headers``: the same /v2 credential, sent the same way.
    return {
        "Authorization": f"Bearer {settings.deploy_api_key}",
        "User-Agent": "iforge-backend/1.0",
    }


def _base_url() -> str:
    base = settings.deploy_api_url.strip().rstrip("/")
    if not base or not settings.deploy_api_key:
        raise CompanionBillingUnavailableError("companion billing is not configured")
    return base


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_DEPLOY_API_TIMEOUT_SECONDS, transport=_transport_override)


def _parse_time(raw: Any) -> datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp is not a string")
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _as_int(raw: Any) -> int:
    # ``bool`` is an ``int``; a price is never one.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("amount is not a non-negative integer")
    return raw


def _parse_quote(response: httpx.Response) -> CompanionQuote:
    try:
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("quote is not an object")
        return CompanionQuote(
            quote_id=str(payload["quote_id"]),
            owner_user_id=str(payload["owner_user_id"]),
            companion_key=str(payload["companion_key"]),
            isolation_tier=str(payload["isolation_tier"]),
            currency=str(payload["currency"]),
            monthly_price_usd_micros=_as_int(payload["monthly_price_usd_micros"]),
            storage_price_usd_micros_per_gib_month=_as_int(
                payload["storage_price_usd_micros_per_gib_month"]
            ),
            expires_at=_parse_time(payload["expires_at"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CompanionBillingUnavailableError("/v2 answered with an unreadable quote") from exc


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        code = detail.get("code")
        return code if isinstance(code, str) else None
    code = payload.get("code") if isinstance(payload, dict) else None
    return code if isinstance(code, str) else None


async def request_companion_quote(
    *, owner_user_id: str, companion_key: str, isolation_tier: str
) -> CompanionQuote:
    """Ask /v2 for the price of one companion at one tier for one owner (D-07: tier is the price).

    Raises ``CompanionTierNotPricedError`` for a tier /v2 does not price and
    ``CompanionBillingUnavailableError`` for anything else that is not a readable quote.
    """
    base = _base_url()
    try:
        async with _client() as client:
            response = await client.post(
                f"{base}/v2/companions/quote",
                headers=_deploy_headers(),
                json={
                    "owner_user_id": owner_user_id,
                    "companion_key": companion_key,
                    "isolation_tier": isolation_tier,
                },
            )
    except httpx.HTTPError as exc:
        raise CompanionBillingUnavailableError("/v2 is unreachable") from exc
    if (
        response.status_code == httpx.codes.UNPROCESSABLE_ENTITY
        and _error_code(response) == "tier_not_priced"
    ):
        raise CompanionTierNotPricedError(isolation_tier)
    if response.status_code != httpx.codes.OK:
        raise CompanionBillingUnavailableError(
            f"/v2 answered the quote with {response.status_code}"
        )
    return _parse_quote(response)


async def read_companion_quote(quote_id: str) -> CompanionQuote | None:
    """The quote as /v2 holds it now; ``None`` when /v2 does not know it.

    Raises ``CompanionQuoteExpiredError`` when /v2 answers 410 (its validity window has passed).
    """
    base = _base_url()
    try:
        async with _client() as client:
            response = await client.get(
                f"{base}/v2/companions/quotes/{url_quote(quote_id, safe='')}",
                headers=_deploy_headers(),
            )
    except httpx.HTTPError as exc:
        raise CompanionBillingUnavailableError("/v2 is unreachable") from exc
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    if response.status_code == httpx.codes.GONE:
        raise CompanionQuoteExpiredError(quote_id)
    if response.status_code != httpx.codes.OK:
        raise CompanionBillingUnavailableError(
            f"/v2 answered the quote with {response.status_code}"
        )
    return _parse_quote(response)


async def lowest_offered_monthly_price(
    *, owner_user_id: str, companion_key: str, offered_tiers: tuple[str, ...]
) -> int | None:
    """The lowest monthly price /v2 quotes for any OFFERED tier; ``None`` when it prices none.

    What the catalog shows as "from" (APP-08 empty edge): a tier /v2 does not price contributes
    nothing, and an unconfigured or unreachable /v2 means no price at all -- never an invented one.
    """
    prices: list[int] = []
    for tier in offered_tiers:
        try:
            quote = await request_companion_quote(
                owner_user_id=owner_user_id, companion_key=companion_key, isolation_tier=tier
            )
        except CompanionTierNotPricedError:
            continue
        except CompanionBillingUnavailableError:
            return None
        prices.append(quote.monthly_price_usd_micros)
    return min(prices) if prices else None


def companion_billing_unavailable() -> HTTPException:
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "companion_billing_unavailable",
            "message": "Companion pricing is unavailable right now, so nothing can be installed.",
        },
    )


def companion_tier_not_priced(isolation_tier: str) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={
            "code": "companion_tier_not_priced",
            "message": f"The {isolation_tier} tier has no price, so it cannot be installed.",
        },
    )


def _quote_refused(code: str, message: str) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, detail={"code": code, "message": message})


async def verify_companion_quote(
    *,
    quote_id: str,
    owner_user_id: str,
    companion_key: str,
    isolation_tier: str,
    now: datetime | None = None,
) -> CompanionQuote:
    """The quote if it is CURRENT and for exactly this companion, tier and owner; else a refusal.

    Called by ``create_installation`` before any row is written (T-06-17-01): 409
    ``companion_quote_expired`` for a quote past its window (re-quote), 409
    ``companion_quote_mismatch`` for an unknown quote or one for another companion, tier or owner,
    503 ``companion_billing_unavailable`` when /v2 cannot answer -- fail closed, never open.
    """
    moment = now or datetime.now(UTC)
    try:
        quote = await read_companion_quote(quote_id)
    except CompanionQuoteExpiredError:
        quote = None
        expired = True
    except CompanionBillingUnavailableError:
        raise companion_billing_unavailable() from None
    else:
        expired = quote is not None and quote.expires_at <= moment
    if expired:
        raise _quote_refused(
            "companion_quote_expired",
            "This price quote has expired. Request a new quote and confirm again.",
        )
    if (
        quote is None
        or quote.owner_user_id != owner_user_id
        or quote.companion_key != companion_key
        or quote.isolation_tier != isolation_tier
    ):
        raise _quote_refused(
            "companion_quote_mismatch",
            "This price quote was not issued for this companion, tier and account.",
        )
    return quote


# ---------------------------------------------------------------------------
# Line items: started on completion, stopped on demand, both idempotent (T-06-17-03).
# ---------------------------------------------------------------------------


def _line_start_key(installation_id: UUID | str) -> str:
    return f"companion-line-{installation_id}"


def _line_stop_key(installation_id: UUID | str) -> str:
    return f"companion-line-stop-{installation_id}"


async def start_companion_line_item(
    *, installation_id: UUID | str, quote_id: str, owner_user_id: str
) -> CompanionLineItem:
    """Start the installation's line item at its quoted amount; a repeat returns the same line.

    Raises ``CompanionBillingUnavailableError`` for a transport failure or a 5xx (worth retrying)
    and ``CompanionBillingRefusedError`` for a 4xx (not worth retrying).
    """
    base = _base_url()
    try:
        async with _client() as client:
            response = await client.post(
                f"{base}/v2/companions/line-items",
                headers={
                    **_deploy_headers(),
                    "Idempotency-Key": _line_start_key(installation_id),
                },
                json={
                    "quote_id": quote_id,
                    "installation_id": str(installation_id),
                    "owner_user_id": owner_user_id,
                },
            )
    except httpx.HTTPError as exc:
        raise CompanionBillingUnavailableError("/v2 is unreachable") from exc
    if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
        raise CompanionBillingUnavailableError(
            f"/v2 answered the line item with {response.status_code}"
        )
    if response.status_code != httpx.codes.OK:
        raise CompanionBillingRefusedError(response.status_code)
    try:
        payload = response.json()
        return CompanionLineItem(
            line_item_id=str(payload["line_item_id"]),
            started_at=_parse_time(payload["started_at"]),
            monthly_price_usd_micros=_as_int(payload["monthly_price_usd_micros"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise CompanionBillingUnavailableError("/v2 answered with an unreadable line item") from exc


async def stop_companion_line_item(
    *, installation_id: UUID | str, line_item_id: str
) -> datetime | None:
    """Stop the line item; a repeat returns the same ``stopped_at``.

    ``None`` when /v2 does not know the line (404): it is already gone, which is what stopping
    wants. Raises ``CompanionBillingUnavailableError`` for anything else that is not a stop.
    """
    base = _base_url()
    try:
        async with _client() as client:
            response = await client.delete(
                f"{base}/v2/companions/line-items/{url_quote(line_item_id, safe='')}",
                headers={
                    **_deploy_headers(),
                    "Idempotency-Key": _line_stop_key(installation_id),
                },
            )
    except httpx.HTTPError as exc:
        raise CompanionBillingUnavailableError("/v2 is unreachable") from exc
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    if response.status_code != httpx.codes.OK:
        raise CompanionBillingUnavailableError(
            f"/v2 answered the line-item stop with {response.status_code}"
        )
    try:
        return _parse_time(response.json()["stopped_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CompanionBillingUnavailableError("/v2 answered with an unreadable stop") from exc


# The orphan sweep reads at most this many pages of one owner's lines before it gives up on them.
LINE_ITEM_LIST_MAX_PAGES = 50


def _as_text(raw: Any) -> str:
    if not isinstance(raw, str) or not raw:
        raise ValueError("field is not a non-empty string")
    return raw


def _optional_int(raw: Any) -> int | None:
    return None if raw is None else _as_int(raw)


def _parse_line_item_record(item: Any) -> CompanionLineItemRecord:
    if not isinstance(item, dict):
        raise ValueError("line item is not an object")
    quote_id = item.get("quote_id")
    stopped_at = item.get("stopped_at")
    funding_source = item.get("funding_source")
    return CompanionLineItemRecord(
        line_item_id=_as_text(item["line_item_id"]),
        installation_id=_as_text(item["installation_id"]),
        owner_user_id=_as_text(item["owner_user_id"]),
        quote_id=None if quote_id is None else _as_text(quote_id),
        monthly_price_usd_micros=_as_int(item["monthly_price_usd_micros"]),
        started_at=_parse_time(item["started_at"]),
        stopped_at=None if stopped_at is None else _parse_time(stopped_at),
        funding_source=None if funding_source is None else _as_text(funding_source),
        charged_cents=_optional_int(item.get("charged_cents")),
        credited_cents=_optional_int(item.get("credited_cents")),
    )


LineItemListState = Literal["active", "all"]


def _wire_time(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


async def list_companion_line_items(
    *,
    owner_user_id: str,
    state: LineItemListState = "active",
    since: datetime | None = None,
) -> list[CompanionLineItemRecord]:
    """Every line /v2 holds for this owner in ``state``, following ``next_cursor`` (06.2 D-17).

    ``state="active"`` (the orphan sweep's read) lists running lines only; ``state="all"`` adds
    stopped ones, and ``since`` keeps only the lines still running at or after it (the billing
    reconciliation's read, INT-4). Raises ``CompanionBillingUnavailableError`` for an unconfigured
    or unreachable /v2, a non-200, an unreadable page, or more than ``LINE_ITEM_LIST_MAX_PAGES``
    pages -- a partial list is never returned, so no caller decides on half an answer.
    """
    base = _base_url()
    records: list[CompanionLineItemRecord] = []
    cursor: str | None = None
    async with _client() as client:
        for _ in range(LINE_ITEM_LIST_MAX_PAGES):
            params = {"owner_user_id": owner_user_id, "state": state}
            if since is not None:
                params["since"] = _wire_time(since)
            if cursor is not None:
                params["cursor"] = cursor
            try:
                response = await client.get(
                    f"{base}/v2/companions/line-items", headers=_deploy_headers(), params=params
                )
            except httpx.HTTPError as exc:
                raise CompanionBillingUnavailableError("/v2 is unreachable") from exc
            if response.status_code != httpx.codes.OK:
                raise CompanionBillingUnavailableError(
                    f"/v2 answered the line list with {response.status_code}"
                )
            try:
                payload = response.json()
                if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    raise ValueError("line list is not an object with an items list")
                records.extend(_parse_line_item_record(item) for item in payload["items"])
                next_cursor = payload.get("next_cursor")
                if next_cursor is not None and not isinstance(next_cursor, str):
                    raise ValueError("next_cursor is not a string")
            except (KeyError, TypeError, ValueError) as exc:
                raise CompanionBillingUnavailableError(
                    "/v2 answered with an unreadable line list"
                ) from exc
            if not next_cursor:
                return records
            cursor = next_cursor
    raise CompanionBillingUnavailableError(
        f"/v2 listed more than {LINE_ITEM_LIST_MAX_PAGES} pages of line items"
    )


# ---------------------------------------------------------------------------
# The completion step (plan 06-17): the lifecycle worker appends it after ``EDGE_PUBLISH_STEP``.
# ---------------------------------------------------------------------------


async def _start_billing(context: LifecycleStepContext) -> JsonObject:
    """Start the installation's line item at the amount the tenant confirmed, and record it.

    The line is started under the owner's hosted id (``hosted_owner_id``, 06.2 D-09) so it lands
    on the same hosted owner as the dashboard's subscription. An owner with none (a partner
    tenant) is refused for good -- ``companion_billing_refused``, not retried -- before any /v2
    call.
    """
    async with context.sessionmaker() as db:
        row = (
            await db.execute(
                select(
                    CompanionInstallation.quote_id,
                    CompanionInstallation.quoted_monthly_usd_micros,
                ).where(CompanionInstallation.id == context.installation_id)
            )
        ).one()
        owner = await db.get(User, context.user_id)
        owner_id = hosted_owner_id(owner) if owner is not None else None
        await db.rollback()
    quote_id, quoted_monthly = row.quote_id, row.quoted_monthly_usd_micros
    if not quote_id or quoted_monthly is None:
        raise CompanionStepError(
            "companion_quote_missing",
            "the installation carries no confirmed quote, so it cannot be billed",
        )
    if owner_id is None:
        raise CompanionStepError(
            "companion_billing_refused", "the installation's owner has no hosted billing identity"
        )
    try:
        line = await start_companion_line_item(
            installation_id=context.installation_id,
            quote_id=quote_id,
            owner_user_id=owner_id,
        )
    except CompanionBillingUnavailableError as exc:
        # Transient (T-06-17-04): the worker retries up to BILLING_START_MAX_ATTEMPTS with backoff,
        # the job still running; then it fails and tears down rather than run unbilled.
        raise CompanionStepRetryableError(
            "companion_billing_unavailable", "the billing service did not start the line item"
        ) from exc
    except CompanionBillingRefusedError as exc:
        raise CompanionStepError(
            "companion_billing_refused",
            f"the billing service refused the line item ({exc.status_code})",
        ) from exc
    if line.monthly_price_usd_micros != quoted_monthly:
        # Never bill an amount different from the quote the tenant saw: stop it at once. A stop
        # that cannot reach /v2 is retried like a start (the start is idempotent, so the retry
        # finds this same line and stops it again).
        try:
            await stop_companion_line_item(
                installation_id=context.installation_id, line_item_id=line.line_item_id
            )
        except CompanionBillingUnavailableError as exc:
            raise CompanionStepRetryableError(
                "companion_billing_unavailable",
                "the billing service did not stop a line item started at the wrong amount",
            ) from exc
        raise CompanionStepError(
            "companion_billing_amount_mismatch",
            "the billing service started the line item at an amount other than the quote",
        )
    async with context.sessionmaker() as db:
        await db.execute(
            update(CompanionInstallation)
            .where(CompanionInstallation.id == context.installation_id)
            .values(
                billing_line_item_id=line.line_item_id,
                billing_started_at=line.started_at,
                billing_stopped_at=None,
            )
        )
        await db.commit()
    return {
        "line_item_id": line.line_item_id,
        "monthly_price_usd_micros": line.monthly_price_usd_micros,
    }


async def _stop_billing(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Stop the installation's line item and record when; nothing to stop is not an error.

    Exported as the step's teardown so the uninstall pipeline (plan 06-20) stops billing with the
    same call a failed install's teardown makes.
    """
    recorded = resources.get("line_item_id")
    async with context.sessionmaker() as db:
        row = (
            await db.execute(
                select(
                    CompanionInstallation.billing_line_item_id,
                    CompanionInstallation.billing_stopped_at,
                ).where(CompanionInstallation.id == context.installation_id)
            )
        ).one()
        await db.rollback()
    line_item_id = recorded if isinstance(recorded, str) and recorded else row.billing_line_item_id
    if not line_item_id:
        return
    stopped_at = await stop_companion_line_item(
        installation_id=context.installation_id, line_item_id=line_item_id
    )
    async with context.sessionmaker() as db:
        await db.execute(
            update(CompanionInstallation)
            .where(CompanionInstallation.id == context.installation_id)
            .values(billing_stopped_at=stopped_at or row.billing_stopped_at or datetime.now(UTC))
        )
        await db.commit()


BILLING_START_STEP = ProvisioningStep(
    name=BILLING_START_STEP_NAME,
    provision=_start_billing,
    teardown=_stop_billing,
    max_attempts=BILLING_START_MAX_ATTEMPTS,
)


__all__ = [
    "BILLING_START_MAX_ATTEMPTS",
    "BILLING_START_STEP",
    "BILLING_START_STEP_NAME",
    "CompanionBillingRefusedError",
    "CompanionBillingUnavailableError",
    "CompanionLineItem",
    "CompanionLineItemRecord",
    "CompanionQuote",
    "CompanionQuoteExpiredError",
    "CompanionTierNotPricedError",
    "companion_billing_unavailable",
    "companion_tier_not_priced",
    "list_companion_line_items",
    "lowest_offered_monthly_price",
    "read_companion_quote",
    "request_companion_quote",
    "start_companion_line_item",
    "stop_companion_line_item",
    "verify_companion_quote",
]
