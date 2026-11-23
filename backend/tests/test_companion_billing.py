"""A companion is priced before it is provisioned and billed only once it is complete (APP-08).

Money movement lives in the hosted control plane behind ``/v2`` (ADR-0005), so every test here
drives the REAL mock of that contract -- ``scripts/mock_deploy_api.py``, in process through an ASGI
transport -- and records the ``/v2`` calls the backend makes, in order. The order is the property:

* a quote for exactly the chosen (companion, tier) is requested and shown before anything exists;
* the install request verifies that quote against ``/v2`` before a single row is written;
* the line item starts only as a completion step, after the runtime's data-asserted ready report
  and after the hostname is published, so an install that fails or never becomes reachable is
  never billed.

The lifecycle worker runs inside the test's own transaction (``_sessionmaker``), so the worker's
commits stay test-local and the whole test rolls back as one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.auth import AuthContext, get_auth
from app.core.config import settings
from app.main import app
from app.models.companion_installation import CompanionInstallation
from app.models.user import PRINCIPAL_KIND_PARTNER_TENANT, User
from app.services.companion_billing import (
    BILLING_START_STEP,
    start_companion_line_item,
    stop_companion_line_item,
)
from app.services.companion_edge import EDGE_PUBLISH_STEP
from app.services.companion_install import create_installation
from app.services.companion_lifecycle_steps import CompanionStepError, CompanionStepRetryableError
from app.services.companion_lifecycle_worker import (
    COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS,
    COMPLETION_STEPS,
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
)
from scripts import mock_deploy_api
from tests.companion_billing_stub import (
    COMPANION_PRICE,
    BillingMock,
    quote_for,
    route_billing_to_mock,
)
from tests.test_companion_install import _other_tenant
from tests.test_companion_install_job import (
    _hosted_agent,
    _post_report,
    _ready_report,
    _reload,
    _runtime_key,
    _sessionmaker,
)

ZONE = "companions.example"
QUOTES_PATH = "/v1/companions/quotes"
INSTALLATIONS_PATH = "/v1/companions/installations"
LINE_ITEMS_PATH = "/v2/companions/line-items"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


def _edge_publish_stand_in(calls: list[str]) -> ProvisioningStep:
    """The edge publication's place in the completion pipeline, without the operator's API.

    It records into the SAME call log as the ``/v2`` transport, so one list proves the order.
    """

    async def provision(context: LifecycleStepContext) -> dict:
        calls.append(EDGE_PUBLISH_STEP.name)
        return {"published": context.hostname}

    async def teardown(context: LifecycleStepContext, resources: dict) -> None:
        calls.append(f"teardown:{EDGE_PUBLISH_STEP.name}")

    return ProvisioningStep(name=EDGE_PUBLISH_STEP.name, provision=provision, teardown=teardown)


async def _runtime_report(
    db_session, raw_key: str, installation_id: uuid.UUID, body: dict
) -> httpx.Response:
    """The runtime's own report, made with its own key while the dashboard ``client`` is open.

    The ``client`` fixture overrides ``get_auth`` with the signed-in owner for the whole test; the
    runtime authenticates with its deployment key instead, so that override is lifted for exactly
    this call and restored after it.
    """
    dashboard_auth = app.dependency_overrides.pop(get_auth, None)
    try:
        return await _post_report(db_session, raw_key, installation_id, body)
    finally:
        if dashboard_auth is not None:
            app.dependency_overrides[get_auth] = dashboard_auth


def _line_items_for(installation_id: uuid.UUID) -> list[dict]:
    return [
        line
        for line in mock_deploy_api.COMPANION_LINE_ITEMS.values()
        if line["installation_id"] == str(installation_id)
    ]


async def test_install_is_priced_before_provisioning_and_billed_only_on_completion(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The line item is a completion step, and it follows the hostname's publication.
    assert [step.name for step in COMPLETION_STEPS] == [EDGE_PUBLISH_STEP.name, "billing-start"]

    billing = route_billing_to_mock(monkeypatch)
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)

    # 1. The price, for exactly the chosen tier, before anything is provisioned.
    quoted = await client.post(
        QUOTES_PATH, json={"companion_key": "postiz", "isolation_tier": "standard"}
    )
    assert quoted.status_code == 200, quoted.text
    quote = quoted.json()
    assert quote["companion_key"] == "postiz"
    assert quote["isolation_tier"] == "standard"
    assert quote["currency"] == "usd"
    assert quote["monthly_price_usd_micros"] == COMPANION_PRICE.monthly_usd_micros
    assert (
        quote["storage_price_usd_micros_per_gib_month"]
        == COMPANION_PRICE.storage_usd_micros_per_gib_month
    )
    quote_id = quote["quote_id"]

    # 2. The install carries that quote, and the quote is verified against /v2 before any row.
    installed = await client.post(
        INSTALLATIONS_PATH,
        json={
            "agent_id": str(agent_id),
            "companion_key": "postiz",
            "isolation_tier": "standard",
            "quote_id": quote_id,
        },
    )
    assert installed.status_code == 202, installed.text
    installation_id = uuid.UUID(installed.json()["id"])
    row = await _reload(db_session, installation_id)
    assert row.quote_id == quote_id
    assert row.quoted_monthly_usd_micros == COMPANION_PRICE.monthly_usd_micros
    assert (
        row.quoted_storage_usd_micros_per_gib_month
        == COMPANION_PRICE.storage_usd_micros_per_gib_month
    )
    assert billing.calls == [
        "POST /v2/companions/quote",
        f"GET /v2/companions/quotes/{quote_id}",
    ]

    # 3. Provisioned and running -- and still nothing billed.
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(),
        completion_steps=(_edge_publish_stand_in(billing.calls), BILLING_START_STEP),
    )
    assert await worker.run_once() == installation_id
    assert (await _reload(db_session, installation_id)).status == "running"
    runtime_key = await _runtime_key(db_session, seed_user, agent_id, deployment_id)
    reported = await _runtime_report(
        db_session,
        runtime_key,
        installation_id,
        _ready_report(applied_digest=row.pinned_digest),
    )
    assert reported.status_code == 200, reported.text
    # The ready report alone starts no charge: billing is the worker's completion step.
    assert not any(LINE_ITEMS_PATH in call for call in billing.calls), billing.calls
    assert _line_items_for(installation_id) == []

    # 4. The completion steps publish the edge FIRST, then start the line item; the job completes.
    assert await worker.run_once() == installation_id
    assert billing.calls[2:] == [EDGE_PUBLISH_STEP.name, f"POST {LINE_ITEMS_PATH}"]
    row = await _reload(db_session, installation_id)
    assert row.status == "complete"
    lines = _line_items_for(installation_id)
    assert len(lines) == 1, lines
    assert row.billing_line_item_id == lines[0]["line_item_id"]
    assert row.billing_started_at is not None
    assert row.billing_stopped_at is None
    assert lines[0]["monthly_price_usd_micros"] == row.quoted_monthly_usd_micros
    assert lines[0]["stopped_at"] is None


# ---------------------------------------------------------------------------
# Task 2: every quote refusal, and the ordering guarantees around failure.
# ---------------------------------------------------------------------------


async def _installation_count(db_session, agent_id: uuid.UUID) -> int:
    return (
        await db_session.execute(
            select(func.count())
            .select_from(CompanionInstallation)
            .where(CompanionInstallation.environment_id == agent_id)
        )
    ).scalar_one()


def _install_body(agent_id: uuid.UUID, quote_id: str | None) -> dict:
    body = {"agent_id": str(agent_id), "companion_key": "postiz", "isolation_tier": "standard"}
    if quote_id is not None:
        body["quote_id"] = quote_id
    return body


async def test_install_without_a_quote_is_422(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    agent_id, _ = await _hosted_agent(db_session, seed_user)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent_id, None))

    assert response.status_code == 422, response.text
    assert any(error["loc"][-1] == "quote_id" for error in response.json()["detail"])
    assert await _installation_count(db_session, agent_id) == 0
    assert billing.calls == []


async def test_install_with_an_expired_quote_is_409(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    # The quote's window passed while the tenant looked at it.
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    mock_deploy_api.COMPANION_QUOTES[quote_id]["expires_at"] = expired_at
    agent_id, _ = await _hosted_agent(db_session, seed_user)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent_id, quote_id))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "companion_quote_expired"
    assert await _installation_count(db_session, agent_id) == 0


async def test_install_with_another_tiers_quote_is_409(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A /v2 that DOES price the dedicated tier, so the foreign-tier quote is a real one.
    route_billing_to_mock(monkeypatch)
    monkeypatch.setitem(
        mock_deploy_api.COMPANION_PRICE_FIXTURES, ("postiz", "dedicated"), (1_000_000, 1_000)
    )
    cheaper_elsewhere = await quote_for(monkeypatch, seed_user.clerk_id, isolation_tier="dedicated")
    agent_id, _ = await _hosted_agent(db_session, seed_user)

    response = await client.post(
        INSTALLATIONS_PATH, json=_install_body(agent_id, cheaper_elsewhere)
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "companion_quote_mismatch"
    assert await _installation_count(db_session, agent_id) == 0


async def test_install_with_another_owners_quote_is_409(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = await _other_tenant(db_session)
    foreign_quote = await quote_for(monkeypatch, other.clerk_id)
    agent_id, _ = await _hosted_agent(db_session, seed_user)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(agent_id, foreign_quote))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "companion_quote_mismatch"
    assert await _installation_count(db_session, agent_id) == 0


async def test_the_stub_refuses_to_quote_under_the_internal_user_id(
    seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06.2 D-09: tests quote under ``users.clerk_id``; the internal UUID is refused loudly."""
    internal_id = seed_user.id  # the users.id UUID -- never a /v2 owner
    with pytest.raises(TypeError, match=r"users\.clerk_id"):
        await quote_for(monkeypatch, internal_id)


async def test_unpriced_tier_cannot_be_quoted(client, monkeypatch: pytest.MonkeyPatch) -> None:
    billing = route_billing_to_mock(monkeypatch)

    response = await client.post(
        QUOTES_PATH, json={"companion_key": "postiz", "isolation_tier": "dedicated"}
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "companion_tier_not_priced"
    assert billing.calls == ["POST /v2/companions/quote"]
    assert mock_deploy_api.COMPANION_QUOTES == {}


class _Clock:
    """The worker's clock, moved forward by hand: never behind real time, so reports stay due."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def __call__(self) -> datetime:
        return datetime.now(UTC) + self.offset

    def advance(self, seconds: int) -> None:
        self.offset += timedelta(seconds=seconds)


async def _billed_install(
    db_session, seed_user, monkeypatch: pytest.MonkeyPatch, billing: BillingMock, clock: _Clock
):
    """An install through quote, request and provisioning, with the real billing completion step."""
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    installation = await create_installation(
        db_session,
        auth=AuthContext(user=seed_user),
        agent_id=agent_id,
        companion_key="postiz",
        isolation_tier="standard",
        quote_id=quote_id,
    )
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=clock,
        provisioning_steps=(),
        completion_steps=(_edge_publish_stand_in(billing.calls), BILLING_START_STEP),
    )
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "running"
    runtime_key = await _runtime_key(db_session, seed_user, agent_id, deployment_id)
    return installation, worker, runtime_key


def _line_item_starts(billing: BillingMock) -> int:
    return billing.calls.count(f"POST {LINE_ITEMS_PATH}")


async def test_failed_install_never_starts_a_line_item(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, runtime_key = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )

    failed = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        {
            "operation": "install",
            "phase": "failed",
            "step": "migrate",
            "lines": [],
            "error": "postiz did not start",
        },
    )
    assert failed.status_code == 200, failed.text
    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert _line_item_starts(billing) == 0, billing.calls
    assert billing.line_items_for(installation.id) == []
    assert row.billing_line_item_id is None
    assert row.billing_started_at is None


async def test_billing_start_failure_keeps_running_then_fails_and_tears_down(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    clock = _Clock()
    installation, worker, runtime_key = await _billed_install(
        db_session, seed_user, monkeypatch, billing, clock
    )
    reported = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        _ready_report(applied_digest=installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text

    # /v2 is down for line items: every start answers 503.
    def line_items_down(request: httpx.Request) -> int | None:
        if request.method == "POST" and request.url.path == LINE_ITEMS_PATH:
            return 503
        return None

    billing.fail = line_items_down

    # First and second attempts: the job keeps running, due again later, nothing torn down.
    for attempt in (1, 2):
        assert await worker.run_once() == installation.id
        row = await _reload(db_session, installation.id)
        assert row.status == "running", (attempt, row.error)
        assert row.step == "ready"
        assert row.next_attempt_at is not None and row.next_attempt_at > clock()
        assert _line_item_starts(billing) == attempt
        assert "torn_down" not in (row.external_resources or {})
        # Backing off: not due until the backoff has elapsed.
        assert await worker.run_once() is None
        clock.advance(COMPANION_LIFECYCLE_BACKOFF_CAP_SECONDS + 1)

    # The third attempt is the bound: the job fails and is torn down in the same pass.
    assert await worker.run_once() == installation.id
    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error is not None and "companion_billing_unavailable" in row.error
    assert _line_item_starts(billing) == 3
    torn_down = [entry["name"] for entry in row.external_resources["torn_down"]]
    assert torn_down == [EDGE_PUBLISH_STEP.name]
    assert billing.calls.count(EDGE_PUBLISH_STEP.name) == 1
    assert billing.calls[-1] == f"teardown:{EDGE_PUBLISH_STEP.name}"
    assert billing.line_items_for(installation.id) == []
    assert row.billing_line_item_id is None


# ---------------------------------------------------------------------------
# Task 3: idempotent start and stop, fail-closed pricing, and the catalog price.
# ---------------------------------------------------------------------------

CATALOG_PATH = "/v1/companions/catalog"


async def test_line_item_start_is_idempotent(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, _runtime = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )
    context = worker._context(await _reload(db_session, installation.id))

    first = await BILLING_START_STEP.provision(context)
    # A worker that crashed after /v2 answered, before recording the step, starts it again.
    second = await BILLING_START_STEP.provision(context)
    direct = await start_companion_line_item(
        installation_id=installation.id,
        quote_id=installation.quote_id,
        owner_user_id=seed_user.clerk_id,
    )

    assert first == second
    assert direct.line_item_id == first["line_item_id"]
    assert _line_item_starts(billing) == 3
    lines = billing.line_items_for(installation.id)
    assert [line["line_item_id"] for line in lines] == [first["line_item_id"]]


async def test_billing_start_for_an_owner_without_a_clerk_id_is_refused_without_a_v2_call(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """06.2 D-09: the line item is started under the owner's Clerk id, and an owner without one
    (a partner tenant) is refused for good -- not retried -- before any /v2 call."""
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, _runtime = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )
    ref = uuid.uuid4().hex[:12]
    partner = User(
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        clerk_id=None,
        clerk_issuer=None,
        partner_tenant_ref=f"partner-billing-{ref}",
        email=f"partner-billing-{ref}@iforge.local",
    )
    db_session.add(partner)
    await db_session.flush()
    row = await _reload(db_session, installation.id)
    row.user_id = partner.id
    await db_session.commit()
    context = worker._context(await _reload(db_session, installation.id))
    calls_before = list(billing.calls)

    with pytest.raises(CompanionStepError) as refused:
        await BILLING_START_STEP.provision(context)

    assert refused.value.code == "companion_billing_refused"
    assert not isinstance(refused.value, CompanionStepRetryableError)
    assert billing.calls == calls_before
    assert billing.line_items_for(installation.id) == []


async def test_line_item_stop_is_idempotent_and_tolerates_404(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, _runtime = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )
    context = worker._context(await _reload(db_session, installation.id))
    started = await BILLING_START_STEP.provision(context)
    line_item_id = started["line_item_id"]

    first = await stop_companion_line_item(
        installation_id=installation.id, line_item_id=line_item_id
    )
    second = await stop_companion_line_item(
        installation_id=installation.id, line_item_id=line_item_id
    )
    unknown = await stop_companion_line_item(
        installation_id=installation.id, line_item_id="cli_never_started"
    )

    assert first is not None and first == second
    assert unknown is None
    (line,) = billing.line_items_for(installation.id)
    # Started and stopped at once: a zero-length line at most, never a negative one.
    assert line["stopped_at"] >= line["started_at"]
    assert billing.calls.count(f"DELETE {LINE_ITEMS_PATH}/{line_item_id}") == 2

    # The step's teardown is the uninstall plan's stop: it records when, and repeats harmlessly.
    await BILLING_START_STEP.teardown(context, started)
    await BILLING_START_STEP.teardown(context, started)
    row = await _reload(db_session, installation.id)
    assert row.billing_stopped_at is not None
    assert row.billing_stopped_at >= row.billing_started_at
    assert len(billing.line_items_for(installation.id)) == 1


async def test_quote_route_fails_closed_without_deploy_api(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    # Then /v2 goes away: no URL configured at all.
    monkeypatch.setattr(settings, "deploy_api_url", "")
    agent_id, _ = await _hosted_agent(db_session, seed_user)

    quoted = await client.post(
        QUOTES_PATH, json={"companion_key": "postiz", "isolation_tier": "standard"}
    )
    installed = await client.post(INSTALLATIONS_PATH, json=_install_body(agent_id, quote_id))

    assert quoted.status_code == 503, quoted.text
    assert quoted.json()["detail"]["code"] == "companion_billing_unavailable"
    assert installed.status_code == 503, installed.text
    assert installed.json()["detail"]["code"] == "companion_billing_unavailable"
    assert await _installation_count(db_session, agent_id) == 0


async def test_catalog_shows_the_offered_tier_price_or_null(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def postiz_price() -> int | None:
        response = await client.get(CATALOG_PATH)
        assert response.status_code == 200, response.text
        items = {item["companion_key"]: item for item in response.json()["items"]}
        postiz = items["postiz"]
        # The field is always present: null is the answer "no price", never a missing key.
        assert "from_monthly_price_usd_micros" in postiz, sorted(postiz)
        return postiz["from_monthly_price_usd_micros"]

    # No /v2 configured: no price, and none invented.
    monkeypatch.setattr(settings, "deploy_api_url", "")
    assert await postiz_price() is None

    # /v2 prices the offered tier: that is the price shown.
    route_billing_to_mock(monkeypatch)
    assert await postiz_price() == COMPANION_PRICE.monthly_usd_micros

    # /v2 prices only a tier this build does not offer: still no price.
    monkeypatch.delitem(mock_deploy_api.COMPANION_PRICE_FIXTURES, ("postiz", "standard"))
    monkeypatch.setitem(
        mock_deploy_api.COMPANION_PRICE_FIXTURES, ("postiz", "dedicated"), (1_000_000, 1_000)
    )
    assert await postiz_price() is None


async def test_billed_amount_equals_the_quoted_amount(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, runtime_key = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )
    reported = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        _ready_report(applied_digest=installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "complete"
    (line,) = billing.line_items_for(installation.id)
    assert line["monthly_price_usd_micros"] == row.quoted_monthly_usd_micros
    assert line["monthly_price_usd_micros"] == COMPANION_PRICE.monthly_usd_micros
    recorded = next(
        step for step in row.external_resources["steps"] if step["name"] == "billing-start"
    )
    assert recorded["resources"]["monthly_price_usd_micros"] == row.quoted_monthly_usd_micros


async def test_a_line_started_at_another_amount_is_stopped_and_the_install_fails(
    db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    installation, worker, runtime_key = await _billed_install(
        db_session, seed_user, monkeypatch, billing, _Clock()
    )
    # The hosted side changed the quote's amount after the tenant confirmed it.
    mock_deploy_api.COMPANION_QUOTES[installation.quote_id]["monthly_price_usd_micros"] += 1
    reported = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        _ready_report(applied_digest=installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error is not None and "companion_billing_amount_mismatch" in row.error
    (line,) = billing.line_items_for(installation.id)
    assert line["stopped_at"] is not None
    assert row.billing_line_item_id is None
