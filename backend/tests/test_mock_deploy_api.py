import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from scripts import mock_deploy_api
from tests.contract_v2.schema import validate_component


def test_mock_deployment_read_response_is_projected_from_flat_mutation_record() -> None:
    mutation = mock_deploy_api._deployment()
    expected_agent_id = str(
        uuid.uuid5(
            uuid.UUID("e016a4c8-7943-4ae9-9c53-5f1a5db9f3e1"),
            f"{mutation['id']}:openclaw",
        )
    )

    read = mock_deploy_api._deployment_read_response(mutation)

    assert read["agent_id"] == expected_agent_id
    assert "resource" in read
    assert "id" not in read
    assert read["resource"]["id"] == mutation["id"]
    assert read["resource"]["name"] == mutation["name"]
    assert "name" not in read["resource"]["spec"]
    assert read["resource"]["spec"]["runtime"] == "openclaw"
    assert read["ai_provider_auth_kinds"] == {"openclaw": "api_key"}
    assert read["runtime_ui_endpoint"] == {
        "runtime": "openclaw",
        "role": "control_ui",
        "url": "https://openclaw.dev-preview.local",
    }
    assert read["current_plan_slug"] == "compute_performance"
    assert read["commercial_display"]["latest_funding_fact"] is None

    mutation["config_info"]["clawdi_cloud_environments"] = {}
    read_without_projection = mock_deploy_api._deployment_read_response(mutation)
    assert read_without_projection["clawdi_cloud_environments"] == {}
    assert read_without_projection["agent_id"] == expected_agent_id

    assert mutation["id"] == mock_deploy_api.DEV_V2_DEPLOYMENT_ID
    assert "resource" not in mutation


@pytest.mark.parametrize(
    ("runtime", "status", "failure_reason"),
    [
        ("openclaw", "running", None),
        ("hermes", "stopped", None),
        ("openclaw", "failed", "Runtime readiness timed out"),
        ("hermes", "deleted", None),
    ],
)
def test_mock_deployment_reads_are_pinned_hosted_deployment_reads(
    runtime: str, status: str, failure_reason: str | None
) -> None:
    """Every mock deployment read -- each list item and the single GET -- is a pinned
    ``V2HostedDeploymentReadResponse`` (WINDOWS #256)."""
    deployment = mock_deploy_api._create_deployment_record({"runtime": runtime})
    deployment["status"] = status
    if failure_reason is not None:
        deployment["failure_reason"] = failure_reason
    deployment_id = deployment["id"]
    try:
        with TestClient(mock_deploy_api.app) as client:
            listed = client.get("/v2/deployments")
            assert listed.status_code == 200, listed.text
            items = listed.json()
            assert deployment_id in {item["resource"]["id"] for item in items}
            for item in items:
                validate_component("V2HostedDeploymentReadResponse", item)

            single = client.get(f"/v2/deployments/{deployment_id}")
            assert single.status_code == 200, single.text
            validate_component("V2HostedDeploymentReadResponse", single.json())
    finally:
        mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)


def test_mock_funding_revocation_projects_complete_provenance() -> None:
    mutation = mock_deploy_api._deployment()
    mutation["last_funding_event"] = {
        "type": "compute_subscription_fallback",
        "funding_source": "wallet",
        "reason": "disputed",
        "prior_plan_slug": "compute_basic",
        "occurred_at": "2026-07-18T12:00:00Z",
        "subscription_id": 42,
    }

    fact = mock_deploy_api._deployment_read_response(mutation)["commercial_display"][
        "latest_funding_fact"
    ]

    assert fact["funding_source"] == "wallet"
    assert fact["reason"] == "disputed"
    assert fact["prior_plan_slug"] == "compute_basic"
    assert fact["occurred_at"] == "2026-07-18T12:00:00Z"


def test_mock_v2_create_projects_only_the_resource_name() -> None:
    with TestClient(mock_deploy_api.app) as client:
        response = client.post(
            "/v2/subscription/checkout",
            headers={"Idempotency-Key": f"mock-name-contract-{uuid.uuid4().hex}"},
            json={
                "funding_source": "wallet",
                "deploy_config": {
                    "name": "Canonical deployment name",
                    "runtime": "hermes",
                    "deploy_request_id": "mock-name-contract",
                },
            },
        )

        assert response.status_code == 200
        deployment_id = response.json()["deployment_id"]
        try:
            resource = client.get(f"/v2/deployments/{deployment_id}").json()["resource"]
            assert resource["name"] == "Canonical deployment name"
            assert "name" not in resource["spec"]
            assert "assistant_name" not in resource["spec"]["runtime_configuration"]
        finally:
            mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)


def test_mock_dev_principal_is_the_backend_dev_clerk_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """WR-03: `/v1/me` and the dashboard-owner fallback both name the backend's resolved
    `settings.dev_auth_clerk_id`, the principal the dev bypass authenticates as."""
    monkeypatch.setattr(settings, "dev_auth_clerk_id", "user_custom_dev_principal")
    monkeypatch.delenv("MOCK_DASHBOARD_OWNER_USER_ID", raising=False)

    assert mock_deploy_api._clerk_id() == "user_custom_dev_principal"
    with TestClient(mock_deploy_api.app) as client:
        assert client.get("/v1/me").json()["clerk_id"] == "user_custom_dev_principal"


def test_mock_deploy_and_pay_checkout_is_filed_under_the_dev_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-03: a deploy-and-pay checkout with a non-JWT (dev-bypass) bearer lands under the
    backend's dev Clerk id, never under the deployment's internal `user_id`, so the backend
    gate's `/v2/subscription?owner_user_id=<clerk id>` read sees it."""
    principal = f"user_dev_principal_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(settings, "dev_auth_clerk_id", principal)
    monkeypatch.delenv("MOCK_DASHBOARD_OWNER_USER_ID", raising=False)
    monkeypatch.setattr(mock_deploy_api, "SUBSCRIPTIONS", {})
    with TestClient(mock_deploy_api.app) as client:
        response = client.post(
            "/v2/subscription/checkout",
            headers={
                "Authorization": "Bearer dev-bypass",
                "Idempotency-Key": f"mock-dev-principal-{uuid.uuid4().hex}",
            },
            json={
                "funding_source": "wallet",
                "deploy_config": {"name": "Dev principal deploy", "runtime": "hermes"},
            },
        )

        assert response.status_code == 200, response.text
        deployment_id = response.json()["deployment_id"]
        try:
            assert set(mock_deploy_api.SUBSCRIPTIONS) == {principal}
            gate_read = client.get(
                "/v2/subscription",
                params={"owner_user_id": principal},
                headers={"Authorization": "Bearer mock-unit-test-key"},
            )
            assert gate_read.json()["subscription"]["status"] == "active"
        finally:
            mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)


def test_mock_included_basic_availability_is_deployment_scoped() -> None:
    deployment = mock_deploy_api._create_deployment_record(
        {"runtime": "hermes", "compute_plan_slug": "compute_basic"}
    )
    deployment["compute_subscription"] = {
        "funding_source": None,
        "price_cents": 0,
    }
    deployment_id = deployment["id"]
    try:
        with TestClient(mock_deploy_api.app) as client:
            assert client.get("/v2/subscriptions/included-basic").json() == {
                "total_slots": 1,
                "used_slots": 1,
                "available_slots": 0,
            }

            deployment["status"] = "deleted"
            assert client.get("/v2/subscriptions/included-basic").json() == {
                "total_slots": 1,
                "used_slots": 0,
                "available_slots": 1,
            }
    finally:
        mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)


def test_mock_v2_rejects_retired_names_and_updates_runtime_configuration() -> None:
    deployment = mock_deploy_api._create_deployment_record(
        {"name": "Stable resource name", "runtime": "hermes"}
    )
    deployment_id = deployment["id"]
    resource_version = f"rv_{deployment_id}"
    headers = {
        "Idempotency-Key": "mock-runtime-update",
        "If-Match": f'"{resource_version}"',
    }
    try:
        with TestClient(mock_deploy_api.app) as client:
            updated = client.patch(
                f"/v2/deployments/{deployment_id}",
                headers=headers,
                json={"language": "zh-CN", "timezone": "Asia/Shanghai"},
            )
            assert updated.status_code == 200
            resource = updated.json()["response"]["deployment"]
            assert resource["name"] == "Stable resource name"
            assert resource["spec"]["runtime_configuration"]["language"] == "zh-CN"
            assert resource["spec"]["runtime_configuration"]["timezone"] == ("Asia/Shanghai")

            next_version = resource["metadata"]["resourceVersion"]
            for field in ("name", "assistant_name"):
                rejected = client.patch(
                    f"/v2/deployments/{deployment_id}",
                    headers={
                        "Idempotency-Key": f"mock-reject-{field}",
                        "If-Match": f'"{next_version}"',
                    },
                    json={field: "Rejected name"},
                )
                assert rejected.status_code == 422
    finally:
        mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)


def test_mock_v2_create_rejects_retired_assistant_name() -> None:
    with TestClient(mock_deploy_api.app) as client:
        for deploy_config in (
            {"assistant_name": "Retired"},
            {"config": {"assistant_name": "Retired"}},
        ):
            response = client.post(
                "/v2/subscription/checkout",
                headers={"Idempotency-Key": f"mock-retired-name-{uuid.uuid4().hex}"},
                json={"funding_source": "wallet", "deploy_config": deploy_config},
            )
            assert response.status_code == 422


def test_mock_serves_the_hosted_profile_at_the_contract_path() -> None:
    """`/v1/me` is the path the dashboard and the `/v1` contract actually use.

    `apps/web/src/lib/hosted-product-access.ts` and
    `apps/web/src/hosted/billing/billing-client.ts` both request `GET /v1/me`, and
    `packages/shared/src/api/deploy.generated.ts` declares it as the contract path.
    `hostedApiBaseUrl` strips only a trailing `/v2`, so the mock's `/me` was
    unreachable from the dashboard and `/deploy` rendered HostedProductGate's
    "Couldn't verify access" panel instead of resolving the profile (G-03-4).
    """
    with TestClient(mock_deploy_api.app) as client:
        response = client.get("/v1/me")

        assert response.status_code == 200
        profile = response.json()
        # The field the dashboard gates the hosted surfaces on.
        assert profile["capabilities"]["can_use_v2"] is True
        assert profile["clerk_id"] == mock_deploy_api._clerk_id()


def test_mock_keeps_the_legacy_profile_path_working() -> None:
    """`/me` is retained only so an out-of-repo consumer is not broken.

    No in-repo caller references it. Compared field-wise rather than whole-body:
    the payload carries an `updated_at` stamped at request time, so two calls are
    not byte-identical.
    """
    with TestClient(mock_deploy_api.app) as client:
        legacy = client.get("/me")
        contract = client.get("/v1/me")

        assert legacy.status_code == 200
        assert legacy.json()["capabilities"] == contract.json()["capabilities"]
        assert legacy.json()["clerk_id"] == contract.json()["clerk_id"]
        assert legacy.json()["settings"] == contract.json()["settings"]


def test_mock_quote_agrees_with_the_plan_offers_it_already_advertised() -> None:
    """The quote must not contradict the price the plan picker already showed.

    `/v2/subscription/plans` advertises per-term `offers`; the deploy wizard shows
    those, then calls `POST /v2/subscription/quote` on the confirm step. If the two
    disagree the wizard contradicts itself mid-flow, so this pins them to the same
    numbers rather than asserting a hardcoded constant twice.
    """
    with TestClient(mock_deploy_api.app) as client:
        plans = {plan["slug"]: plan for plan in client.get("/v2/subscription/plans").json()}

        for slug, plan in plans.items():
            for offer in plan["offers"]:
                term = offer["billing_term_months"]
                response = client.post(
                    "/v2/subscription/quote",
                    json={
                        "plan_slug": slug,
                        "billing_term_months": term,
                        "funding_source": "stripe",
                    },
                )

                assert response.status_code == 200, (slug, term, response.text)
                quote = response.json()
                assert quote["term_price_cents"] == offer["price_cents"]
                assert quote["plan_slug"] == slug
                assert quote["billing_term_months"] == term
                assert quote["currency"] == "usd"


def test_mock_quote_returns_wallet_math_only_for_wallet_funding() -> None:
    """Wallet fields are `string | null` in the contract and must stay null for stripe.

    A client that reads `balance_after_usd` unconditionally would render a bogus
    balance on the card path, so the discrimination is the point of the assertion.
    """
    with TestClient(mock_deploy_api.app) as client:
        body = {"plan_slug": "compute_basic", "billing_term_months": 1}

        stripe = client.post(
            "/v2/subscription/quote", json={**body, "funding_source": "stripe"}
        ).json()
        wallet_quote = client.post(
            "/v2/subscription/quote", json={**body, "funding_source": "wallet"}
        ).json()

        assert stripe["debit_amount_usd"] is None
        assert stripe["balance_before_usd"] is None
        assert stripe["balance_after_usd"] is None
        assert stripe["preview_invoice_id"] is not None

        # 900 cents against the $42.00 balance /v2/wallet reports.
        wallet_balance = client.get("/v2/wallet").json()
        expected_before = Decimal(wallet_balance["balance_usd"])
        assert expected_before == Decimal("42.00")
        assert wallet_quote["balance_before_usd"] == f"{expected_before:.2f}"
        assert wallet_quote["debit_amount_usd"] == "9.00"
        assert wallet_quote["balance_after_usd"] == "33.00"
        assert wallet_quote["preview_invoice_id"] is None


def test_mock_quote_rejects_an_unknown_plan_or_term() -> None:
    """Guards the `_TERM_PRICE_CENTS` lookup — an unknown key must 422, not KeyError."""
    with TestClient(mock_deploy_api.app) as client:
        unknown_plan = client.post(
            "/v2/subscription/quote",
            json={
                "plan_slug": "compute_enormous",
                "billing_term_months": 1,
                "funding_source": "stripe",
            },
        )
        unknown_term = client.post(
            "/v2/subscription/quote",
            json={
                "plan_slug": "compute_basic",
                "billing_term_months": 7,
                "funding_source": "stripe",
            },
        )

        assert unknown_plan.status_code == 422
        assert unknown_term.status_code == 422


def test_mock_serves_the_managed_model_catalog() -> None:
    """`GET /v2/ai-providers/managed/models` backs the model-binding picker.

    Without it the picker renders "Couldn't load Clawdi AI models — Not Found".
    Exactly one model must be default, or the picker has no unambiguous initial
    selection.
    """
    with TestClient(mock_deploy_api.app) as client:
        response = client.get("/v2/ai-providers/managed/models")

        assert response.status_code == 200
        models = response.json()["models"]
        assert len(models) >= 1
        assert sum(1 for model in models if model["is_default"]) == 1
        for model in models:
            assert model["provider_id"] == mock_deploy_api.DEV_V2_MANAGED_PROVIDER_ID
            assert model["capabilities"]["context_window"] > 0
            assert set(model["capabilities"]["input_modalities"]) <= {"text", "image"}


def test_mock_agent_environments_match_the_runtime_env_ids() -> None:
    """`GET /v1/agent-environments` must agree with the ids /v2 already advertises.

    Derived from `_runtime_env_id` rather than restated, so the legacy surface
    cannot drift from the deployment record's `clawdi_cloud_environments`.
    """
    with TestClient(mock_deploy_api.app) as client:
        response = client.get("/v1/agent-environments")

        assert response.status_code == 200
        environment_ids = response.json()["environment_ids"]
        assert environment_ids == [
            mock_deploy_api._runtime_env_id(runtime) for runtime in ("codex", "openclaw", "hermes")
        ]
        assert len(set(environment_ids)) == len(environment_ids)


def _fresh_companion_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mock_deploy_api, "SUBSCRIPTIONS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_QUOTES", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEMS", {})
    monkeypatch.setattr(mock_deploy_api, "COMPANION_LINE_ITEM_KEYS", {})


def _start_line_for(client: TestClient, owner_user_id: str) -> Any:
    auth = {"Authorization": "Bearer mock-unit-test-key"}
    quote = client.post(
        "/v2/companions/quote",
        headers=auth,
        json={
            "owner_user_id": owner_user_id,
            "companion_key": "postiz",
            "isolation_tier": "standard",
        },
    )
    assert quote.status_code == 200
    return client.post(
        "/v2/companions/line-items",
        headers={**auth, "Idempotency-Key": f"unit-start-{uuid.uuid4().hex}"},
        json={
            "quote_id": quote.json()["quote_id"],
            "installation_id": str(uuid.uuid4()),
            "owner_user_id": owner_user_id,
        },
    )


@pytest.mark.parametrize("subscription_status", [None, "canceled"])
def test_mock_line_item_start_refuses_owner_without_live_subscription(
    monkeypatch: pytest.MonkeyPatch, subscription_status: str | None
) -> None:
    _fresh_companion_stores(monkeypatch)
    owner = "user_mock_unit_unsubscribed"
    if subscription_status is not None:
        mock_deploy_api.SUBSCRIPTIONS[owner] = {
            "status": subscription_status,
            "plan_id": "compute_basic",
        }
    client = TestClient(mock_deploy_api.app)

    response = _start_line_for(client, owner)

    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "no_active_subscription"}
    assert mock_deploy_api.COMPANION_LINE_ITEMS == {}


@pytest.mark.parametrize("subscription_status", ["active", "trialing", "past_due"])
def test_mock_line_item_start_allows_a_live_subscription(
    monkeypatch: pytest.MonkeyPatch, subscription_status: str
) -> None:
    _fresh_companion_stores(monkeypatch)
    owner = "user_mock_unit_subscribed"
    mock_deploy_api.SUBSCRIPTIONS[owner] = {
        "status": subscription_status,
        "plan_id": "compute_basic",
    }
    client = TestClient(mock_deploy_api.app)

    response = _start_line_for(client, owner)

    assert response.status_code == 200
    assert response.json()["line_item_id"].startswith("cli_")


def test_mock_line_items_list_view_keys() -> None:
    started_at = mock_deploy_api._now()
    record = {
        "line_item_id": "cli_unit",
        "quote_id": "cq_unit",
        "installation_id": "inst_unit",
        "owner_user_id": "user_unit",
        "monthly_price_usd_micros": 1,
        "started_at": started_at,
        "stopped_at": None,
    }

    active = mock_deploy_api._line_item_list_view(record)
    stopped = mock_deploy_api._line_item_list_view({**record, "stopped_at": started_at})

    assert set(active) == {
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
    # A record written without a funding source is wallet-funded (INT-4).
    assert (active["funding_source"], active["charged_cents"], active["credited_cents"]) == (
        "wallet",
        0,
        0,
    )
    assert active["stopped_at"] is None
    assert active["started_at"].endswith("Z")
    assert stopped["stopped_at"] == active["started_at"]
    assert set(mock_deploy_api._line_item_view(record)) == {
        "line_item_id",
        "started_at",
        "monthly_price_usd_micros",
    }


def test_mock_gate_read_and_cancel_require_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    owner = "user_mock_unit_gate"
    monkeypatch.setattr(
        mock_deploy_api,
        "SUBSCRIPTIONS",
        {owner: {"status": "active", "plan_id": "compute_basic"}},
    )
    client = TestClient(mock_deploy_api.app)
    auth = {"Authorization": "Bearer mock-unit-test-key"}

    anonymous_read = client.get("/v2/subscription", params={"owner_user_id": owner})
    anonymous_cancel = client.post("/v2/subscription/cancel", json={"owner_user_id": owner})

    assert anonymous_read.status_code == 401
    assert anonymous_read.json()["detail"] == {"code": "unauthenticated"}
    assert anonymous_cancel.status_code == 401
    assert anonymous_cancel.json()["detail"] == {"code": "unauthenticated"}
    assert mock_deploy_api.SUBSCRIPTIONS[owner]["status"] == "active"

    read = client.get("/v2/subscription", params={"owner_user_id": owner}, headers=auth)
    assert read.status_code == 200
    assert read.json() == {"subscription": {"status": "active", "plan_id": "compute_basic"}}
    cancel = client.post("/v2/subscription/cancel", json={"owner_user_id": owner}, headers=auth)
    assert cancel.status_code == 200
    assert mock_deploy_api.SUBSCRIPTIONS[owner]["status"] == "canceled"
