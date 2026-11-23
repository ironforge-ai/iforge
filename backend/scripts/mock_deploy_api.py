"""Local v2 deploy API mock for dashboard previews.

Run from ``backend/``:

    uv run python scripts/mock_deploy_api.py --host 0.0.0.0 --port 8001

This is intentionally in-memory and dev-only. It lets the web dashboard exercise
hosted/v2 UI flows without the external hosted runtime service.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import hashlib
import json
import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import settings
from app.services.managed_ai_provider import CLAWDI_MANAGED_PROVIDER_ID

DEV_V2_DEPLOYMENT_ID = "hdep_dev_sidebar"
DEV_V2_APP_ID = "app_dev_sidebar"
DEV_V2_PROVIDER_ID = "openrouter-dev"
DEV_V2_MANAGED_PROVIDER_ID = CLAWDI_MANAGED_PROVIDER_ID
STABLE_UUID_NAMESPACE = uuid.UUID("6a9575fd-7eb5-464a-89e7-e13f090f8de6")
HOSTED_AGENT_UUID_NAMESPACE = uuid.UUID("e016a4c8-7943-4ae9-9c53-5f1a5db9f3e1")
OPERATIONS: dict[str, dict[str, Any]] = {}
DEPLOY_REQUESTS: dict[str, dict[str, Any]] = {}
# Tenant-level subscription state, keyed by owner_user_id. The checkout
# handler populates it (mock semantics: checkout succeeds instantly, mirroring
# deployment records being created as "running"); GET /v2/subscription answers
# from it. This is the executable reference for the /v2 subscription-state
# read the backend billing gate consumes (consumer-first, D-09).
SUBSCRIPTIONS: dict[str, dict[str, Any]] = {}
# Companion pricing and billing (plan 06-17, APP-08). The executable reference for the /v2
# companion contract the backend's ``app/services/companion_billing.py`` consumes (ADR-0005
# consumer-first): quotes by id, line items by id, and the Idempotency-Key each line was started
# under, so a repeated start returns the line it already started.
COMPANION_QUOTES: dict[str, dict[str, Any]] = {}
COMPANION_LINE_ITEMS: dict[str, dict[str, Any]] = {}
COMPANION_LINE_ITEM_KEYS: dict[str, str] = {}
# Wallet (06.2 D-13, SC-2). The single balance every wallet surface reports: /v2/wallet and the
# wallet-funded subscription quote both derive from it, so the two cannot disagree.
MOCK_WALLET_BALANCE_USD = Decimal("42.00")
# The contract's auto-reload consent and amount-policy constants (V2WalletResponse).
AUTO_RELOAD_CONSENT_VERSION = "wallet_auto_reload_off_session_v2"
AUTO_RELOAD_AMOUNT_POLICY = "wallet_reload_configured_plus_negative_balance_v1"
# Wallet Idempotency-Key replay (06.2 D-22): (route, key) -> (sha256 of the canonical body, the
# response first returned). Top-ups by checkout_session_id; auto-reload setups by setup_identity.
WALLET_IDEMPOTENCY: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
WALLET_TOPUPS: dict[str, dict[str, Any]] = {}
WALLET_SETUPS: dict[str, dict[str, Any]] = {}
# Plan-change quotes (06.2-08, RESEARCH F3), keyed by the operation id the quote names. A change
# applies a stored quote and is then polled as that operation on GET /v2/operations/{id}.
PLAN_CHANGE_QUOTES: dict[str, dict[str, Any]] = {}
PLAN_CHANGE_QUOTE_VALIDITY = timedelta(minutes=15)
# Checkout Idempotency-Key replay (06.1 contract: the client retries a checkout on 409/503 with
# Retry-After): key -> (sha256 of the canonical body, the response first returned).
CHECKOUT_IDEMPOTENCY: dict[str, tuple[str, dict[str, Any]]] = {}
# OpenClaw runtime-UI grants (06.4 PROV-14, consumer-first): deployment id -> the grants issued by
# POST .../runtime-ui/credentials since the last POST .../runtime-ui/access/reset.
RUNTIME_UI_GRANTS: dict[str, list[dict[str, Any]]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _stable_uuid(clerk_id: str, label: str) -> str:
    return str(uuid.uuid5(STABLE_UUID_NAMESPACE, f"{clerk_id}:{label}"))


def _stable_agent_id(deployment_id: str, runtime: str) -> str:
    return str(uuid.uuid5(HOSTED_AGENT_UUID_NAMESPACE, f"{deployment_id}:{runtime}"))


def _clerk_id() -> str:
    """The dev principal's Clerk id, read from the backend's own resolved
    ``settings.dev_auth_clerk_id`` (``DEV_AUTH_CLERK_ID`` from the process environment or
    ``backend/.env``, default ``dev_browser``). This is the one identity the backend dev bypass
    authenticates as and the billing gate reads ``/v2/subscription`` for, so ``/v1/me``, the
    stable env ids and a dashboard checkout all name the same principal (06.3 D-04)."""
    return settings.dev_auth_clerk_id


def _openclaw_env_id() -> str:
    return os.getenv("MOCK_OPENCLAW_ENV_ID") or _stable_uuid(_clerk_id(), "hosted-openclaw-env")


def _codex_env_id() -> str:
    return os.getenv("MOCK_CODEX_ENV_ID") or _stable_uuid(_clerk_id(), "hosted-codex-env")


def _hermes_env_id() -> str:
    return os.getenv("MOCK_HERMES_ENV_ID") or _stable_uuid(_clerk_id(), "hosted-hermes-env")


def _web_base_url() -> str:
    return os.getenv("MOCK_WEB_BASE_URL", "http://localhost:3001").rstrip("/")


def _ws_base_url(request: Request) -> str:
    base_url = str(request.base_url).rstrip("/")
    if base_url.startswith("https://"):
        return f"wss://{base_url[len('https://') :]}"
    if base_url.startswith("http://"):
        return f"ws://{base_url[len('http://') :]}"
    return base_url


def _runtime_env_id(agent_type: str) -> str:
    if agent_type == "codex":
        return _codex_env_id()
    if agent_type == "openclaw":
        return _openclaw_env_id()
    if agent_type == "hermes":
        return _hermes_env_id()
    raise HTTPException(status_code=400, detail="Unsupported runtime")


def _ordered_agents(values: set[str]) -> list[str]:
    order = {"codex": 0, "openclaw": 1, "hermes": 2}
    return sorted(values, key=lambda value: order.get(value, 99))


def _primary_model_value(raw: Any) -> str | None:
    if isinstance(raw, dict):
        model = raw.get("model")
        return model.strip() if isinstance(model, str) and model.strip() else None
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def _primary_provider_id(raw: Any) -> str | None:
    if isinstance(raw, dict):
        provider_id = raw.get("provider_id") or raw.get("providerId")
        return provider_id.strip() if isinstance(provider_id, str) and provider_id.strip() else None
    return None


def _primary_model_ref(provider_id: str, model: str) -> dict[str, str]:
    return {"provider_id": provider_id, "model": model}


def _provider_ids(body: dict[str, Any], primary_provider_id: str) -> list[str]:
    raw = body.get("provider_ids") or body.get("providerIds")
    if not isinstance(raw, list):
        return [primary_provider_id]
    provider_ids: list[str] = []
    for value in raw:
        if isinstance(value, str) and value.strip() and value.strip() not in provider_ids:
            provider_ids.append(value.strip())
    if primary_provider_id not in provider_ids:
        provider_ids.insert(0, primary_provider_id)
    return provider_ids or [primary_provider_id]


def _base_config() -> dict[str, Any]:
    return {
        "compute_plan_slug": "compute_performance",
        "mux_enabled": True,
        "telegram_mux_enabled": True,
        "discord_mux_enabled": False,
        "whatsapp_mux_enabled": False,
        "kobb_available": True,
        "channel": "telegram",
        "primary_model": "openai/gpt-4o-mini",
        "ai_provider_id": DEV_V2_PROVIDER_ID,
        "ai_provider_auth_kind": "api_key",
        "runtime": "openclaw",
        "ai_provider_bindings": {
            "codex": {
                "provider_id": DEV_V2_PROVIDER_ID,
                "provider_ids": [DEV_V2_PROVIDER_ID],
                "auth_kind": "api_key",
                "primary_model": _primary_model_ref(DEV_V2_PROVIDER_ID, "openai/gpt-4o-mini"),
            },
            "openclaw": {
                "provider_id": DEV_V2_PROVIDER_ID,
                "provider_ids": [DEV_V2_PROVIDER_ID],
                "auth_kind": "api_key",
                "primary_model": _primary_model_ref(DEV_V2_PROVIDER_ID, "openai/gpt-4o-mini"),
            },
            "hermes": {
                "provider_id": DEV_V2_PROVIDER_ID,
                "provider_ids": [DEV_V2_PROVIDER_ID],
                "auth_kind": "api_key",
                "primary_model": _primary_model_ref(DEV_V2_PROVIDER_ID, "openai/gpt-4o-mini"),
            },
        },
        "telegram_allowed_usernames": ["dev-user", "dev-preview"],
        "telegram_bot_username": "clawdi_dev_bot",
        "telegram_entry_url": "https://t.me/clawdi_dev_bot",
        "telegram_provider": "native",
        "public_ports": [18789, 9119],
        "enable_openclaw": True,
        "enable_hermes": True,
        "onboarded_agents": ["codex", "openclaw", "hermes"],
        "configured_agents": ["codex", "openclaw", "hermes"],
        "clawdi_cloud_environments": {
            "codex": _codex_env_id(),
            "openclaw": _openclaw_env_id(),
            "hermes": _hermes_env_id(),
        },
        "vcpu": 4,
        "ram_gb": 8,
        "disk_gb": 80,
    }


# Term pricing, keyed (plan_slug, billing_term_months). Mirrors the `offers`
# entries served by /v2/subscription/plans — the quotes must agree
# with what the plan picker already showed the user, or the wizard contradicts
# itself between the plan step and the confirm step.
_TERM_PRICE_CENTS: dict[tuple[str, int], int] = {
    ("compute_basic", 1): 900,
    ("compute_basic", 12): 8640,
    ("compute_performance", 1): 1900,
    ("compute_performance", 12): 19000,
}


def _subscription_record(
    *,
    subscription_id: int,
    owner_user_id: str,
    plan_slug: str,
    funding_source: str | None,
    billing_term_months: int,
    deployment_id: str | None,
) -> dict[str, Any]:
    """One compute subscription as the mock keeps it (06.2-08).

    ``status`` and ``plan_id`` are what the gate read (``GET /v2/subscription``) returns; the rest
    serves the dashboard's subscription center. ``subscription_id`` is the integer the plan-change
    surfaces carry and ``public_id`` the ``csub_`` form the list views carry (RESEARCH Pitfall 11).
    A deployment-bound record's plan is its deployment's ``compute_plan_slug``.
    """
    return {
        "subscription_id": subscription_id,
        "public_id": f"csub_mock{subscription_id}",
        "owner_user_id": owner_user_id,
        "status": "active",
        "plan_id": plan_slug,
        "plan_slug": plan_slug,
        "funding_source": funding_source,
        "payment_state": "ok",
        "billing_term_months": billing_term_months,
        "price_cents": _TERM_PRICE_CENTS[(plan_slug, billing_term_months)],
        "currency": "usd",
        "deployment_id": deployment_id,
        "current_period_end": _iso(_now() + timedelta(days=30)),
        "cancel_at_period_end": False,
        "pending_plan_slug": None,
    }


def _deployment(
    *,
    deployment_id: str = DEV_V2_DEPLOYMENT_ID,
    name: str = "dev-sidebar-preview",
    status: str = "running",
    config_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    created = _now() - timedelta(hours=3)
    return {
        "id": deployment_id,
        # The hosted service's internal user id (the same value `/v1/me` answers as `id`), not the
        # Clerk id. A checkout never files its subscription under it (`_checkout_owner_user_id`).
        "user_id": "usr_dev_preview",
        "name": name,
        "app_id": DEV_V2_APP_ID,
        "backend": "mock",
        "status": status,
        "endpoints": [
            "https://codex.dev-preview.local",
            "https://openclaw.dev-preview.local",
            "https://hermes.dev-preview.local",
        ],
        "gateway_token": None,
        "openclaw_control_ui_url": "https://openclaw.dev-preview.local",
        "hermes_control_ui_url": "https://hermes.dev-preview.local",
        "config_info": config_info or _base_config(),
        "created_at": _iso(created),
        "upgrade_available": False,
        "agent_version": "v2-dev",
        "app_image": "clawdi/hosted-runtime:dev",
    }


def _summary_state(status: str) -> str:
    if status == "provisioning":
        return "creating"
    if status == "ready":
        return "running"
    if status in {
        "creating",
        "starting",
        "running",
        "stopping",
        "stopped",
        "restarting",
        "updating",
        "deleting",
        "deleted",
        "failed",
    }:
        return status
    raise ValueError(f"Unsupported mock deployment status: {status}")


def _read_provider_auth_kind(value: Any) -> str:
    if value in {"unmanaged", "managed", "api_key", "codex_oauth"}:
        return str(value)
    raise ValueError(f"Unsupported mock provider auth kind: {value!r}")


def _runtime_configuration(config: dict[str, Any], runtime: str) -> dict[str, Any]:
    preferences = {
        field: config[field]
        for field in ("language", "timezone")
        if isinstance(config.get(field), str) and config[field]
    }
    binding = (config.get("ai_provider_bindings") or {}).get(runtime) or {}
    auth_kind = _read_provider_auth_kind(
        binding.get("auth_kind") or config.get("ai_provider_auth_kind")
    )
    if auth_kind == "unmanaged":
        return {
            **preferences,
            "primary_model": None,
            "providers": [],
            "features": [],
        }

    provider_id = binding.get("provider_id") or config.get("ai_provider_id")
    primary_model = binding.get("primary_model")
    providers = []
    if isinstance(provider_id, str) and provider_id:
        providers.append(
            {
                "provider_id": provider_id,
                "auth_kind": "managed" if auth_kind == "managed" else "secret_reference",
                "models": [],
            }
        )
    return {
        **preferences,
        "primary_model": primary_model if isinstance(primary_model, dict) else None,
        "providers": providers,
        "features": [],
    }


def _funding_fact(record: dict[str, Any]) -> dict[str, Any] | None:
    event = record.get("last_funding_event")
    if not isinstance(event, dict):
        return None
    return {
        "fact_kind": "funding_revoked",
        "commercial_revision": 1,
        "compute_subscription_id": event.get("subscription_id"),
        "compute_plan_slug": None,
        "funding_source": event.get("funding_source"),
        "reason": event.get("reason"),
        "prior_plan_slug": event.get("prior_plan_slug"),
        "occurred_at": event.get("occurred_at"),
        "emitted_at": event.get("occurred_at"),
    }


def _deployment_read_response(record: dict[str, Any]) -> dict[str, Any]:
    """The record as a pinned ``V2HostedDeploymentReadResponse``.

    Ownership stays on the internal record (``record["user_id"]``); the contract resource has no
    owner field, so the read never carries one. Backing infrastructure is reported only through
    ``compute_slot_occupancy``, the contract's place for it.
    """
    config = record["config_info"]
    runtime = config.get("runtime")
    if runtime not in {"openclaw", "hermes"}:
        raise ValueError(f"Unsupported mock deployment runtime: {runtime!r}")
    summary_state = _summary_state(record["status"])
    backing_infra = "absent" if summary_state in {"stopped", "deleted"} else "present"
    binding = (config.get("ai_provider_bindings") or {}).get(runtime) or {}
    provider_auth_kind = _read_provider_auth_kind(
        binding.get("auth_kind") or config.get("ai_provider_auth_kind")
    )
    runtime_ui_url = (
        record.get("openclaw_control_ui_url")
        if runtime == "openclaw"
        else record.get("hermes_control_ui_url")
    )
    failure_reason = record.get("failure_reason")
    failure = (
        {
            "type": "https://api.clawdi.ai/problems/runtime-readiness-timeout",
            "title": failure_reason,
            "status": 504,
            "detail": "The runtime did not become ready before the startup deadline.",
            "instance": record["id"],
            "code": "runtime_readiness_timeout",
            "phase": "readiness",
            "retryable": True,
            "conditionReason": "RuntimeReadinessTimeout",
            "conditionMessage": failure_reason,
            "observedGeneration": 1,
        }
        if isinstance(failure_reason, str) and failure_reason
        else None
    )
    upgrade_available = bool(record.get("upgrade_available", False))
    endpoints = [
        {"name": f"endpoint-{index}", "url": url}
        for index, url in enumerate(record.get("endpoints") or [], start=1)
    ]
    return {
        "agent_id": _stable_agent_id(record["id"], runtime),
        "resource": {
            "id": record["id"],
            "name": record["name"],
            "commercial_revision": 1,
            "deployment_target": "saas",
            "metadata": {
                "generation": record.get("_generation", 1),
                "manifestETag": f"etag_{record['id']}",
                "resourceVersion": record.get("_resource_version", f"rv_{record['id']}"),
                "createdAt": record["created_at"],
                "updatedAt": record["created_at"],
            },
            "spec": {
                "schema_version": 1,
                "desired_lifecycle": (
                    "stopped"
                    if summary_state == "stopped"
                    else "deleted"
                    if summary_state == "deleted"
                    else "running"
                ),
                "runtime": runtime,
                "runtime_version": "dev",
                "resources": {
                    "vcpu": config.get("vcpu", 1),
                    "memory_mib": config.get("ram_gb", 2) * 1024,
                    "disk_gib": config.get("disk_gb", 20),
                },
                "agents": [],
                "ports": [],
                "runtime_configuration": _runtime_configuration(config, runtime),
                "rollout_nonce": 0,
                "secret_references": [],
            },
            "status": {
                "summary_state": summary_state,
                "observedGeneration": 1,
                "conditions": [],
                "failure": failure,
                "driver_acknowledged_generation": 1,
                "driver_applied_generation": 1,
                "driver_observation_sequence": 1,
                "endpoints": endpoints,
            },
        },
        "clawdi_cloud_environments": config.get("clawdi_cloud_environments") or {},
        "ai_provider_auth_kinds": {runtime: provider_auth_kind},
        "runtime_ui_endpoint": (
            {
                "runtime": runtime,
                "role": "control_ui",
                "url": runtime_ui_url,
            }
            if isinstance(runtime_ui_url, str) and runtime_ui_url
            else None
        ),
        "accepted_operation": None,
        "commercial_display": {
            "compute_subscription": record.get("compute_subscription"),
            "latest_funding_fact": _funding_fact(record),
        },
        "current_plan_slug": config["compute_plan_slug"],
        "upgrade_available": upgrade_available,
        "upgrade_eligibility": record.get("upgrade_eligibility")
        or {"eligible": upgrade_available, "reason": None},
        "compute_slot_occupancy": {
            "occupies_slot": backing_infra == "present",
            "backing_infra": backing_infra,
            "reason": (
                "backing_infra_present" if backing_infra == "present" else "authoritative_absence"
            ),
        },
    }


def _get_deployment_record(deployment_id: str) -> dict[str, Any]:
    deployment = DEPLOYMENTS.get(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    return deployment


def _seed_deployments() -> dict[str, dict[str, Any]]:
    """The dev deployment, on a card-funded monthly subscription to its own plan, so the
    dashboard's plan-change and subscription-center flows have something to act on."""
    dev = _deployment()
    dev["compute_subscription"] = _subscription_record(
        subscription_id=1,
        owner_user_id=dev["user_id"],
        plan_slug=dev["config_info"]["compute_plan_slug"],
        funding_source="stripe",
        billing_term_months=1,
        deployment_id=dev["id"],
    )
    return {DEV_V2_DEPLOYMENT_ID: dev}


DEPLOYMENTS: dict[str, dict[str, Any]] = _seed_deployments()

app = FastAPI(title="Clawdi local deploy API mock")
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=".*",
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# `/v1/me` is the contract path (`packages/shared/src/api/deploy.generated.ts`) and
# the one both in-repo consumers request. `/me` is retained only as a legacy alias
# so an out-of-repo consumer still pointing at it is not broken.
@app.get("/v1/me")
@app.get("/me")
async def me() -> dict[str, Any]:
    now = _iso(_now() - timedelta(days=14))
    return {
        "id": "usr_dev_preview",
        "clerk_id": _clerk_id(),
        "email": os.getenv("DEV_AUTH_EMAIL", "e2e-byok-pi-0606-0425@example.com"),
        "name": os.getenv("DEV_AUTH_NAME", "E2E BYOK PI User"),
        "created_at": now,
        "updated_at": _iso(_now()),
        "settings": {
            "v2": {"enabled": True, "source": "rule_clerk_id"},
            "hosted_agents": {"enabled": True, "source": "rule_clerk_id"},
        },
        "evm_wallet_address": None,
        "capabilities": {"can_use_v1": True, "can_use_v2": True},
    }


@app.get("/v2/deployments")
async def list_deployments() -> list[dict[str, Any]]:
    return [_deployment_read_response(record) for record in DEPLOYMENTS.values()]


def _create_deployment_record(body: dict[str, Any]) -> dict[str, Any]:
    nested_config = body.get("config")
    if "assistant_name" in body or (
        isinstance(nested_config, dict) and "assistant_name" in nested_config
    ):
        raise HTTPException(
            status_code=422,
            detail="assistant_name is not part of the V2 deployment contract",
        )
    deployment_id = f"hdep_dev_{uuid.uuid4().hex[:8]}"
    requested_runtime = body.get("runtime", "openclaw")
    if not isinstance(requested_runtime, str) or requested_runtime not in {
        "openclaw",
        "hermes",
    }:
        raise HTTPException(status_code=422, detail="unsupported V2 runtime")
    enabled_optional = [requested_runtime]
    agents = ["codex", *enabled_optional]
    config = _base_config()
    config["runtime"] = requested_runtime
    config["compute_plan_slug"] = body.get("compute_plan_slug", "compute_basic")
    config["enable_openclaw"] = "openclaw" in enabled_optional
    config["enable_hermes"] = "hermes" in enabled_optional
    config["onboarded_agents"] = agents
    config["configured_agents"] = agents
    config["clawdi_cloud_environments"] = {runtime: _runtime_env_id(runtime) for runtime in agents}
    primary_model_raw = body.get("primary_model")
    primary_model = _primary_model_value(primary_model_raw) or "openai/gpt-4o-mini"
    primary_provider_id = (
        body.get("ai_provider_id") or _primary_provider_id(primary_model_raw) or DEV_V2_PROVIDER_ID
    )
    if primary_provider_id == "managed":
        primary_provider_id = DEV_V2_MANAGED_PROVIDER_ID
    provider_ids = _provider_ids(body, primary_provider_id)
    config["primary_model"] = primary_model
    config["ai_provider_id"] = primary_provider_id
    config["ai_provider_auth_kind"] = body.get("ai_provider_auth_kind") or "managed"
    config["ai_provider_bindings"] = {
        runtime: {
            "provider_id": primary_provider_id,
            "provider_ids": provider_ids,
            "auth_kind": config["ai_provider_auth_kind"],
            "primary_model": _primary_model_ref(primary_provider_id, primary_model),
        }
        for runtime in agents
    }
    requested_name = body.get("name")
    deployment_name = (
        requested_name.strip()
        if isinstance(requested_name, str) and requested_name.strip()
        else f"dev-agent-{deployment_id[-4:]}"
    )
    created = _deployment(
        deployment_id=deployment_id,
        name=deployment_name,
        status="provisioning",
        config_info=config,
    )
    DEPLOYMENTS[deployment_id] = created
    return created


def _require_mutation_headers(request: Request, deployment: dict[str, Any]) -> str:
    """400 without an Idempotency-Key, 428 without an If-Match, 412 when it names another version.

    The 428 matches the pinned contract (every lifecycle mutation lists it) and the hosted compute
    role (``if_match_required``); only a present header that is not the current version is 412.
    """
    idempotency_key = request.headers.get("Idempotency-Key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    if_match = request.headers.get("If-Match")
    if if_match is None or not if_match.strip():
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    resource_version = deployment.get("_resource_version", f"rv_{deployment['id']}")
    if if_match != f'"{resource_version}"':
        raise HTTPException(status_code=412, detail="resource_version_mismatch")
    return idempotency_key


def _accept_operation(
    deployment: dict[str, Any],
    *,
    verb: str,
    idempotency_key: str,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Accept a deployment operation and store it for ``GET /v2/operations/{id}``.

    ``operation_id`` names the operation when the caller already issued its id (a plan change is
    polled under the id its quote named); otherwise it derives from the deployment, verb and key.
    """
    deployment["_generation"] = int(deployment.get("_generation", 1)) + 1
    deployment["_resource_version"] = f"rv_{deployment['id']}_{uuid.uuid4().hex[:8]}"
    now = _iso(_now())
    if operation_id is None:
        operation_id = uuid.uuid5(
            STABLE_UUID_NAMESPACE,
            f"{deployment['id']}:{verb}:{idempotency_key}",
        ).hex
    operation = {
        "name": f"operations/{operation_id}",
        "metadata": {
            "@type": "type.googleapis.com/clawdi.v2.DeploymentOperationMetadata",
            "deploymentId": deployment["id"],
            "verb": verb,
            "targetGeneration": deployment["_generation"],
            "manifestETag": f"etag_{deployment['id']}",
            "createTime": now,
            "updateTime": now,
        },
        "done": True,
        "response": {
            "@type": "type.googleapis.com/clawdi.v2.DeploymentOperationResponse",
            "deployment": _deployment_read_response(deployment)["resource"],
        },
    }
    OPERATIONS[operation_id] = operation
    return operation


@app.post("/v2/deployments", status_code=202)
async def create_deployment(request: Request) -> dict[str, Any]:
    body = await request.json()
    idempotency_key = request.headers.get("Idempotency-Key") or uuid.uuid4().hex
    created = _create_deployment_record(body)
    if created["config_info"]["compute_plan_slug"] == "compute_basic":
        created["compute_subscription"] = {
            "subscription_id": len(DEPLOYMENTS),
            "status": "active",
            "funding_source": None,
            "payment_state": "ok",
            "billing_term_months": 1,
            "price_cents": 0,
            "currency": "usd",
            "cancel_at_period_end": False,
            "current_period_end": _iso(_now() + timedelta(days=30)),
        }
    return _accept_operation(created, verb="create", idempotency_key=idempotency_key)


@app.get("/v2/deployments/{deployment_id}")
async def get_deployment(deployment_id: str) -> dict[str, Any]:
    return _deployment_read_response(_get_deployment_record(deployment_id))


@app.patch("/v2/deployments/{deployment_id}")
async def update_deployment(deployment_id: str, request: Request) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    body = await request.json()
    if "name" in body or "assistant_name" in body:
        raise HTTPException(
            status_code=422,
            detail="deployment names are not mutable runtime configuration",
        )
    config = deployment.get("config_info")
    if isinstance(config, dict):
        for field in ("language", "timezone"):
            if field in body:
                config[field] = body[field]
    return _accept_operation(
        deployment,
        verb="update",
        idempotency_key=idempotency_key,
    )


@app.delete("/v2/deployments/{deployment_id}")
async def delete_deployment(deployment_id: str, request: Request) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    deployment["status"] = "deleted"
    return _accept_operation(
        deployment,
        verb="delete",
        idempotency_key=idempotency_key,
    )


@app.patch("/v2/deployments/{deployment_id}/agents/{agent_type}")
async def set_agent_enabled(
    deployment_id: str,
    agent_type: str,
    request: Request,
) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    body = await request.json()
    enabled = bool(body.get("enabled"))
    config = deployment["config_info"]
    if agent_type not in {"codex", "openclaw", "hermes"}:
        raise HTTPException(status_code=400, detail="Unsupported runtime")
    if agent_type == "codex":
        if not enabled:
            raise HTTPException(status_code=400, detail="Codex is always enabled")
        onboarded = set(config.get("onboarded_agents") or [])
        configured = set(config.get("configured_agents") or [])
        onboarded.add("codex")
        configured.add("codex")
        config["onboarded_agents"] = _ordered_agents(onboarded)
        config["configured_agents"] = _ordered_agents(configured)
        envs = dict(config.get("clawdi_cloud_environments") or {})
        envs["codex"] = _runtime_env_id("codex")
        config["clawdi_cloud_environments"] = envs
        return deployment
    config[f"enable_{agent_type}"] = enabled
    onboarded = set(config.get("onboarded_agents") or [])
    if enabled:
        onboarded.add(agent_type)
    else:
        onboarded.discard(agent_type)
    config["onboarded_agents"] = _ordered_agents(onboarded)
    return deployment


@app.patch("/v2/deployments/{deployment_id}/agents/{agent_type}/ai-provider")
async def set_agent_ai_provider(
    deployment_id: str,
    agent_type: str,
    request: Request,
) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    body = await request.json()
    if agent_type not in {"codex", "openclaw", "hermes"}:
        raise HTTPException(status_code=400, detail="Unsupported runtime")
    config = deployment["config_info"]
    bindings = dict(config.get("ai_provider_bindings") or {})
    primary_model_raw = body.get("primary_model")
    primary_model = _primary_model_value(primary_model_raw) or "openai/gpt-4o-mini"
    primary_provider_id = (
        body.get("ai_provider_id")
        or _primary_provider_id(primary_model_raw)
        or DEV_V2_MANAGED_PROVIDER_ID
    )
    if primary_provider_id == "managed":
        primary_provider_id = DEV_V2_MANAGED_PROVIDER_ID
    bindings[agent_type] = {
        "provider_id": primary_provider_id,
        "provider_ids": _provider_ids(body, primary_provider_id),
        "auth_kind": body.get("ai_provider_auth_kind") or "managed",
        "primary_model": _primary_model_ref(primary_provider_id, primary_model),
    }
    config["ai_provider_bindings"] = bindings
    config["ai_provider_id"] = bindings[agent_type]["provider_id"]
    config["ai_provider_auth_kind"] = bindings[agent_type]["auth_kind"]
    config["primary_model"] = primary_model
    return deployment


@app.post("/v2/deployments/{deployment_id}/onboard-agent")
async def onboard_agent(deployment_id: str, request: Request) -> dict[str, Any]:
    body = await request.json()
    deployment = _get_deployment_record(deployment_id)
    agent_type = body.get("agent_type", "openclaw")
    if agent_type not in {"codex", "openclaw", "hermes"}:
        raise HTTPException(status_code=400, detail="Unsupported runtime")
    config = deployment["config_info"]
    bindings = dict(config.get("ai_provider_bindings") or {})
    explicit_provider = any(
        body.get(key)
        for key in ("ai_provider_auth_kind", "ai_provider_id", "ai_provider_bootstrap")
    )
    if agent_type not in bindings:
        source = next(
            (
                bindings.get(runtime)
                for runtime in ("codex", "openclaw", "hermes")
                if runtime != agent_type and isinstance(bindings.get(runtime), dict)
            ),
            None,
        )
        if explicit_provider or source is None:
            primary_model_raw = body.get("primary_model")
            primary_model = (
                _primary_model_value(primary_model_raw)
                or _primary_model_value(config.get("primary_model"))
                or "openai/gpt-4o-mini"
            )
            primary_provider_id = (
                body.get("ai_provider_id")
                or _primary_provider_id(primary_model_raw)
                or DEV_V2_MANAGED_PROVIDER_ID
            )
            if primary_provider_id == "managed":
                primary_provider_id = DEV_V2_MANAGED_PROVIDER_ID
            binding = {
                "provider_id": primary_provider_id,
                "provider_ids": _provider_ids(body, primary_provider_id),
                "auth_kind": body.get("ai_provider_auth_kind") or "managed",
                "primary_model": _primary_model_ref(primary_provider_id, primary_model),
            }
            bootstrap = body.get("ai_provider_bootstrap")
        else:
            primary_model_raw = body.get("primary_model")
            primary_model = (
                _primary_model_value(primary_model_raw)
                or _primary_model_value(source.get("primary_model"))
                or _primary_model_value(config.get("primary_model"))
                or "openai/gpt-4o-mini"
            )
            primary_provider_id = (
                _primary_provider_id(primary_model_raw)
                or _primary_provider_id(source.get("primary_model"))
                or source.get("provider_id")
                or DEV_V2_MANAGED_PROVIDER_ID
            )
            if primary_provider_id == "managed":
                primary_provider_id = DEV_V2_MANAGED_PROVIDER_ID
            binding = {
                "provider_id": primary_provider_id,
                "provider_ids": source.get("provider_ids") or [primary_provider_id],
                "auth_kind": source.get("auth_kind") or "managed",
                "primary_model": _primary_model_ref(primary_provider_id, primary_model),
            }
            bootstrap = source.get("bootstrap")
        if isinstance(bootstrap, dict):
            binding["bootstrap"] = bootstrap
        bindings[agent_type] = binding
        config["ai_provider_bindings"] = bindings
        config["ai_provider_id"] = binding["provider_id"]
        config["ai_provider_auth_kind"] = binding["auth_kind"]
        config["primary_model"] = _primary_model_value(binding["primary_model"])
    if agent_type != "codex":
        config[f"enable_{agent_type}"] = True
    onboarded = set(config.get("onboarded_agents") or [])
    onboarded.add(agent_type)
    config["onboarded_agents"] = _ordered_agents(onboarded)
    configured = set(config.get("configured_agents") or [])
    configured.add(agent_type)
    config["configured_agents"] = _ordered_agents(configured)
    envs = dict(config.get("clawdi_cloud_environments") or {})
    envs[agent_type] = _runtime_env_id(agent_type)
    config["clawdi_cloud_environments"] = envs
    return deployment


@app.post("/v2/deployments/{deployment_id}/terminal")
async def create_terminal_session(
    deployment_id: str,
    request: Request,
) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    if deployment.get("status") not in {"running", "ready"}:
        raise HTTPException(
            status_code=409,
            detail="Terminal is available only while the deployment is running.",
        )
    session_id = f"term_{uuid.uuid4().hex[:10]}"
    fragment = urlencode({"token": session_id})
    websocket_url = f"{_ws_base_url(request)}/v2/deployments/{deployment_id}/terminal/ws#{fragment}"
    return {
        "deployment_id": deployment_id,
        "websocket_url": websocket_url,
        "expires_at": _iso(_now() + timedelta(minutes=30)),
    }


@app.websocket("/v2/deployments/{deployment_id}/terminal/ws")
async def mock_terminal_ws(websocket: WebSocket, deployment_id: str) -> None:
    deployment = DEPLOYMENTS.get(deployment_id)
    if deployment is None or deployment.get("status") not in {"running", "ready"}:
        await websocket.close(code=1008)
        return

    protocols = websocket.headers.get("sec-websocket-protocol", "")
    subprotocol = "tty" if "tty" in {value.strip() for value in protocols.split(",")} else None
    await websocket.accept(subprotocol=subprotocol)
    await websocket.send_text(
        f"0Clawdi mock terminal - {deployment_id}\r\nwhoami: clawdi\r\ncwd: /home/clawdi\r\n$ "
    )

    buffer = ""
    try:
        while True:
            message = await websocket.receive_text()
            if not message or message[0] in {"{", "1"}:
                continue
            if message[0] != "0":
                continue

            data = message[1:]
            for char in data:
                if char == "\x03":
                    buffer = ""
                    await websocket.send_text("0^C\r\n$ ")
                    continue
                if char in {"\r", "\n"}:
                    command = buffer.strip()
                    buffer = ""
                    await websocket.send_text(f"0\r\n{_mock_terminal_command(command)}$ ")
                    continue
                if char == "\x7f":
                    buffer = buffer[:-1]
                    await websocket.send_text("0\b \b")
                    continue
                buffer += char
                await websocket.send_text(f"0{char}")
    except WebSocketDisconnect:
        return


def _mock_terminal_command(command: str) -> str:
    if not command:
        return ""
    if command == "pwd":
        return "/home/clawdi\r\n"
    if command == "whoami":
        return "clawdi\r\n"
    if command == "clear":
        return "\x1b[2J\x1b[H"
    return f"mock: command received: {command}\r\n"


@app.post("/v2/deployments/{deployment_id}/restart")
async def restart_deployment(deployment_id: str, request: Request) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    deployment["status"] = "running"
    return _accept_operation(
        deployment,
        verb="restart",
        idempotency_key=idempotency_key,
    )


@app.post("/v2/deployments/{deployment_id}/stop")
async def stop_deployment(deployment_id: str, request: Request) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    deployment["status"] = "stopped"
    return _accept_operation(
        deployment,
        verb="stop",
        idempotency_key=idempotency_key,
    )


@app.post("/v2/deployments/{deployment_id}/start")
async def start_deployment(deployment_id: str, request: Request) -> dict[str, Any]:
    deployment = _get_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    deployment["status"] = "running"
    return _accept_operation(
        deployment,
        verb="start",
        idempotency_key=idempotency_key,
    )


def _runtime_ui_deployment_record(deployment_id: str) -> dict[str, Any]:
    """The deployment a runtime-UI route acts on, or the contract's ``deployment_not_found``."""
    deployment = DEPLOYMENTS.get(deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail={"code": "deployment_not_found"})
    return deployment


def _runtime_ui_credentials_response(deployment: dict[str, Any]) -> dict[str, Any]:
    """Issue one mock grant pinned to the deployment's current resourceVersion.

    The resourceVersion and the runtime-UI url come from the same read the deployment GET serves,
    so a credential can never name a version or url the read does not. The token is a
    recognisable fake (``oru_mock_``), not secret material: single-use redemption is enforced at
    the compute edge (06.4-09), so the mock only records what it issued until a reset clears it.

    The handoff is OpenClaw's native owner bootstrap (D-42 option A), the shape the dashboard's
    ``isOpenClawLaunchHandoff`` accepts: the clean url, then a fragment carrying
    ``bootstrapToken``, ``bootstrapProfile=owner`` and ``gatewayUrl``. The hosted compute side
    takes the bootstrap from the runtime's own ``openclaw dashboard --json``; the mock mints a
    recognisable fake (``oru_boot_mock_``) per issuance, distinct from the grant token. The
    bootstrap belongs to the runtime, not the edge, so the mock does not record it.
    """
    read = _deployment_read_response(deployment)
    endpoint = read["runtime_ui_endpoint"]
    if endpoint is None or endpoint["runtime"] != "openclaw":
        raise HTTPException(status_code=409, detail={"code": "runtime_ui_not_available"})
    resource_version = read["resource"]["metadata"]["resourceVersion"]
    token = f"oru_mock_{uuid.uuid4().hex}"
    RUNTIME_UI_GRANTS.setdefault(deployment["id"], []).append(
        {"token": token, "resource_version": resource_version, "issued_at": _iso(_now())}
    )
    bootstrap = f"oru_boot_mock_{uuid.uuid4().hex}"
    url = endpoint["url"]
    clean = urlsplit(url)
    gateway_url = f"wss://{clean.netloc}{clean.path.rstrip('/')}"
    fragment = urlencode(
        [
            ("bootstrapToken", bootstrap),
            ("bootstrapProfile", "owner"),
            ("gatewayUrl", gateway_url),
        ]
    )
    return {
        "runtime": "openclaw",
        "url": url,
        "deployment_resource_version": resource_version,
        "auth_mode": "openclaw_token",
        "token": token,
        "handoff_url": f"{url}#{fragment}",
    }


@app.post("/v2/deployments/{deployment_id}/runtime-ui/credentials")
async def create_runtime_ui_credentials(deployment_id: str, request: Request) -> dict[str, Any]:
    """PROV-14 issuance: ``If-Match`` pins the grant to the current resourceVersion, so any other
    version -- the one just before a redeploy included -- is ``412``."""
    deployment = _runtime_ui_deployment_record(deployment_id)
    if_match = request.headers.get("If-Match")
    if not if_match:
        raise HTTPException(status_code=428, detail={"code": "if_match_required"})
    resource_version = deployment.get("_resource_version", f"rv_{deployment['id']}")
    if if_match.strip().strip('"') != resource_version:
        raise HTTPException(status_code=412, detail="resource_version_mismatch")
    return _runtime_ui_credentials_response(deployment)


@app.post("/v2/deployments/{deployment_id}/runtime-ui/access/reset")
async def reset_runtime_ui_access(deployment_id: str, request: Request) -> dict[str, Any]:
    """Invalidate every grant issued for the deployment. Idempotent: a reset with nothing issued
    is accepted like any other, and issuing again afterwards works against the new version."""
    deployment = _runtime_ui_deployment_record(deployment_id)
    idempotency_key = _require_mutation_headers(request, deployment)
    RUNTIME_UI_GRANTS.pop(deployment_id, None)
    return _accept_operation(
        deployment,
        verb="reset_runtime_ui_access",
        idempotency_key=idempotency_key,
    )


@app.get("/v2/operations/{operation_id}")
async def get_operation(operation_id: str) -> dict[str, Any]:
    operation = OPERATIONS.get(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    return operation


@app.get("/v2/subscription/plans")
async def plans() -> list[dict[str, Any]]:
    return [
        {
            "slug": "compute_basic",
            "name": "Basic",
            "price_cents": 0,
            "points_per_usd": 100,
            "signup_grant_credits": 500,
            "subscription_grant_credits": 0,
            "vcpu": 1,
            "ram_gb": 2,
            "disk_size": 20,
            "instance_type": "dev-free",
            "offers": [
                {
                    "billing_term_months": 1,
                    "price_cents": 900,
                    "effective_monthly_price_cents": 900,
                    "discount_percent": 0,
                },
                {
                    "billing_term_months": 12,
                    "price_cents": 8640,
                    "effective_monthly_price_cents": 720,
                    "discount_percent": 20,
                },
            ],
        },
        {
            "slug": "compute_performance",
            "name": "Performance",
            "price_cents": 1900,
            "points_per_usd": 100,
            "signup_grant_credits": 500,
            "subscription_grant_credits": 500,
            "vcpu": 4,
            "ram_gb": 8,
            "disk_size": 80,
            "instance_type": "dev-performance",
            "offers": [
                {
                    "billing_term_months": 1,
                    "price_cents": 1900,
                    "effective_monthly_price_cents": 1900,
                    "discount_percent": 0,
                },
                {
                    "billing_term_months": 12,
                    "price_cents": 19000,
                    "effective_monthly_price_cents": 1583,
                    "discount_percent": 17,
                },
            ],
        },
    ]


class ComputeSubscriptionQuoteRequest(BaseModel):
    """Body for POST /v2/subscription/quote (V2ComputeSubscriptionQuoteRequest)."""

    plan_slug: Literal["compute_basic", "compute_performance"]
    billing_term_months: Literal[1, 12] = 1
    funding_source: Literal["stripe", "wallet"]


class ComputeSubscriptionQuoteResponse(BaseModel):
    """V2ComputeSubscriptionQuoteResponse-Output.

    The wallet-funded fields are decimal-USD strings and null for stripe
    funding, matching the generated contract's `string | null`.
    """

    plan_slug: Literal["compute_basic", "compute_performance"]
    billing_term_months: Literal[1, 12]
    funding_source: Literal["stripe", "wallet"]
    currency: str = "usd"
    term_price_cents: int
    preview_invoice_id: str | None = None
    expires_at: str
    debit_amount_usd: str | None = None
    balance_before_usd: str | None = None
    balance_after_usd: str | None = None


@app.post("/v2/subscription/quote")
async def quote_subscription(
    body: ComputeSubscriptionQuoteRequest,
) -> ComputeSubscriptionQuoteResponse:
    """Price a compute subscription before checkout.

    Consumed by `apps/web/src/hosted/billing/billing-client.ts`; the deploy
    wizard calls this between plan selection and checkout, so without it the
    confirm step cannot render a price at all.
    """
    term_price_cents = _TERM_PRICE_CENTS[(body.plan_slug, body.billing_term_months)]
    quote = ComputeSubscriptionQuoteResponse(
        plan_slug=body.plan_slug,
        billing_term_months=body.billing_term_months,
        funding_source=body.funding_source,
        term_price_cents=term_price_cents,
        expires_at=_iso(_now() + timedelta(minutes=15)),
    )
    if body.funding_source == "stripe":
        quote.preview_invoice_id = f"in_mock_{body.plan_slug}_{body.billing_term_months}m"
        return quote
    # Wallet funding: derive from MOCK_WALLET_BALANCE_USD, the balance /v2/wallet
    # reports, so the two surfaces cannot disagree.
    balance_before_usd = MOCK_WALLET_BALANCE_USD
    debit_usd = (Decimal(term_price_cents) / Decimal(100)).quantize(Decimal("0.01"))
    quote.debit_amount_usd = f"{debit_usd:.2f}"
    quote.balance_before_usd = f"{balance_before_usd:.2f}"
    quote.balance_after_usd = f"{balance_before_usd - debit_usd:.2f}"
    return quote


@app.get("/v2/ai-providers/managed/models")
async def managed_model_catalog() -> dict[str, Any]:
    """Clawdi AI managed model catalog (V2ManagedModelCatalogResponse).

    Consumed by the model-binding picker; without it the picker renders
    "Couldn't load Clawdi AI models".
    """
    return {
        "models": [
            {
                "id": "claude-opus-5",
                "display_name": "Claude Opus 5",
                "provider_id": DEV_V2_MANAGED_PROVIDER_ID,
                "is_default": True,
                "is_featured": True,
                "description": "Most capable model. Best for complex, multi-step work.",
                "capabilities": {
                    "context_window": 200_000,
                    "max_context_window": 1_000_000,
                    "max_input_tokens": 200_000,
                    "max_output_tokens": 64_000,
                    "input_modalities": ["text", "image"],
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_tools": True,
                },
            },
            {
                "id": "claude-sonnet-5",
                "display_name": "Claude Sonnet 5",
                "provider_id": DEV_V2_MANAGED_PROVIDER_ID,
                "is_default": False,
                "is_featured": True,
                "description": "Balanced speed and capability for everyday agent work.",
                "capabilities": {
                    "context_window": 200_000,
                    "max_context_window": 1_000_000,
                    "max_input_tokens": 200_000,
                    "max_output_tokens": 64_000,
                    "input_modalities": ["text", "image"],
                    "supports_vision": True,
                    "supports_reasoning": True,
                    "supports_tools": True,
                },
            },
            {
                "id": "claude-haiku-4-5-20251001",
                "display_name": "Claude Haiku 4.5",
                "provider_id": DEV_V2_MANAGED_PROVIDER_ID,
                "is_default": False,
                "is_featured": False,
                "description": "Fastest and cheapest. Best for high-volume, simple tasks.",
                "capabilities": {
                    "context_window": 200_000,
                    "max_context_window": None,
                    "max_input_tokens": 200_000,
                    "max_output_tokens": 32_000,
                    "input_modalities": ["text", "image"],
                    "supports_vision": True,
                    "supports_reasoning": False,
                    "supports_tools": True,
                },
            },
        ]
    }


@app.get("/v1/agent-environments")
async def agent_environments() -> dict[str, list[str]]:
    """Legacy v1 environment ids (V1AgentEnvironmentsResponse).

    Consumed by `getLegacyAgentEnvironments`. Returns the same ids the dev
    deployment record advertises, so the legacy surface agrees with /v2.
    """
    return {
        "environment_ids": [_runtime_env_id(runtime) for runtime in ("codex", "openclaw", "hermes")]
    }


class SubscriptionInfo(BaseModel):
    """Tenant compute-subscription state as read by the billing gate."""

    status: Literal["active", "trialing", "past_due", "canceled"]
    plan_id: str


class SubscriptionReadResponse(BaseModel):
    """Response for GET /v2/subscription — null when the owner has none."""

    subscription: SubscriptionInfo | None = None


@app.get("/v2/subscription")
async def get_subscription(request: Request, owner_user_id: str) -> SubscriptionReadResponse:
    """The owner's compute subscription as the billing gate reads it, or null for none. The hosted
    service accepts only the service credential here (06.2 D-24), so the reference checks for one.
    """
    _require_bearer(request)
    record = SUBSCRIPTIONS.get(owner_user_id)
    if record is None:
        return SubscriptionReadResponse(subscription=None)
    return SubscriptionReadResponse(
        subscription=SubscriptionInfo(status=record["status"], plan_id=record["plan_id"])
    )


def _bearer_subject(request: Request) -> str | None:
    """The ``sub`` of a JWT-shaped bearer, decoded WITHOUT verifying its signature.

    The mock is the executable reference, never an authenticator: the hosted service derives the
    dashboard owner from a verified Clerk token (06.2-12, 06.2-17). Anything that is not three
    dot-separated segments with a JSON payload naming a non-empty string ``sub`` answers None.
    """
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme != "Bearer":
        return None
    segments = token.strip().split(".")
    if len(segments) != 3:
        return None
    payload_segment = segments[1]
    try:
        raw = base64.urlsafe_b64decode(payload_segment + "=" * (-len(payload_segment) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError):
        return None
    subject = payload.get("sub") if isinstance(payload, dict) else None
    if isinstance(subject, str) and subject.strip():
        return subject.strip()
    return None


def _dashboard_owner_user_id(request: Request) -> str:
    """The dashboard principal a checkout acts for, in order: the unverified ``sub`` of a
    JWT-shaped bearer, ``MOCK_DASHBOARD_OWNER_USER_ID``, then the backend's resolved dev Clerk
    id (``_clerk_id()``), so a local dashboard against the mock unlocks the same principal the
    backend billing gate reads (06.3 D-04)."""
    subject = _bearer_subject(request)
    if subject is not None:
        return subject
    override = os.getenv("MOCK_DASHBOARD_OWNER_USER_ID")
    if override and override.strip():
        return override.strip()
    return _clerk_id()


def _checkout_owner_user_id(body: dict[str, Any], request: Request) -> str:
    """Owner for the subscription record, in order: the body's ``owner_user_id`` (additive,
    existing callers unaffected), then the dashboard principal from
    ``_dashboard_owner_user_id(request)``.

    06.3 D-04: every checkout, plan-only or deploy-and-pay, must land under the principal the
    backend gate reads with ``GET /v2/subscription?owner_user_id=<clerk id>``. A created
    deployment's ``user_id`` is the hosted internal user id, not the Clerk id, so it never names
    the owner. The real hosted service derives the owner from its verified session; the mock
    trusts an unverified bearer because it is a local and CI reference that is never deployed
    (T-06.3-12-01).
    """
    raw = body.get("owner_user_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return _dashboard_owner_user_id(request)


def _checkout_plan_id(body: dict[str, Any], deployment: dict[str, Any] | None) -> str:
    plan_slug = body.get("plan_slug")
    if isinstance(plan_slug, str) and plan_slug.strip():
        return plan_slug.strip()
    if deployment is not None:
        config = deployment.get("config_info")
        if isinstance(config, dict):
            config_slug = config.get("compute_plan_slug")
            if isinstance(config_slug, str) and config_slug:
                return config_slug
    return "compute_basic"


@app.get("/v2/subscriptions/included-basic")
async def included_basic_availability() -> dict[str, int]:
    used_slots = sum(
        1
        for deployment in DEPLOYMENTS.values()
        if deployment["status"] != "deleted"
        and deployment["config_info"].get("compute_plan_slug") == "compute_basic"
        and isinstance((subscription := deployment.get("compute_subscription")), dict)
        and subscription.get("funding_source") is None
        and subscription.get("price_cents") == 0
    )
    total_slots = 1
    return {
        "total_slots": total_slots,
        "used_slots": used_slots,
        "available_slots": max(0, total_slots - used_slots),
    }


def _json_body_hash(body: Any) -> str:
    """sha256 of a JSON body in canonical form; Idempotency-Key replay compares these."""
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


@app.post("/v2/subscription/checkout")
async def checkout(request: Request) -> dict[str, Any]:
    """Start a compute subscription (V2CheckoutResponse).

    Card funding answers the Checkout Session arm, wallet funding the activation arm. Both
    activate instantly in the mock, so the subscription record is written before the split. A
    repeated Idempotency-Key with the same body answers the original response; with a different
    body it is ``409 idempotency_key_reused``.
    """
    key = _require_idempotency_key(request)
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="checkout body must be an object")
    body_hash = _json_body_hash(body)
    stored = CHECKOUT_IDEMPOTENCY.get(key)
    if stored is not None:
        stored_hash, stored_response = stored
        if stored_hash != body_hash:
            raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
        return dict(stored_response)
    funding_source = body.get("funding_source")
    billing_term_months = body.get("billing_term_months", 1)
    if funding_source not in {"stripe", "wallet"} or billing_term_months not in {1, 12}:
        raise HTTPException(status_code=422, detail="unsupported funding source or billing term")
    deploy_config = body.get("deploy_config")
    deployment = None
    deploy_request_id = None
    if isinstance(deploy_config, dict):
        deployment = _create_deployment_record(deploy_config)
        deployment["status"] = "running"
        deploy_request_id = deploy_config.get("deploy_request_id")
        if isinstance(deploy_request_id, str) and deploy_request_id:
            operation = _accept_operation(
                deployment,
                verb="create",
                idempotency_key=deploy_request_id,
            )
            DEPLOY_REQUESTS[deploy_request_id] = {
                "deploy_request_id": deploy_request_id,
                "request_status": "succeeded",
                "lineage_tail": {
                    "deployment_id": deployment["id"],
                    "lineage_version": 1,
                    "lineage_state": "succeeded",
                    "operation": operation,
                },
            }
        else:
            deploy_request_id = None
    plan_slug = _checkout_plan_id(body, deployment)
    if (plan_slug, billing_term_months) not in _TERM_PRICE_CENTS:
        raise HTTPException(status_code=422, detail="unsupported plan")
    record = _subscription_record(
        subscription_id=_next_subscription_id(),
        owner_user_id=_checkout_owner_user_id(body, request),
        plan_slug=plan_slug,
        funding_source=funding_source,
        billing_term_months=billing_term_months,
        deployment_id=deployment["id"] if deployment is not None else None,
    )
    SUBSCRIPTIONS[record["owner_user_id"]] = record
    if deployment is not None:
        deployment["config_info"]["compute_plan_slug"] = plan_slug
        deployment["compute_subscription"] = record
    if funding_source == "wallet":
        debit_usd = (Decimal(record["price_cents"]) / Decimal(100)).quantize(Decimal("0.01"))
        response: dict[str, Any] = {
            "flow_type": "subscription_activation",
            "funding_source": "wallet",
            "action_url": None,
            "checkout_url": "",
            "client_secret": None,
            "trial_period_days": None,
            "subscription_id": record["public_id"],
            "invoice_id": None,
            "deployment_id": deployment["id"] if deployment is not None else None,
            "agent_id": (
                _stable_agent_id(deployment["id"], deployment["config_info"]["runtime"])
                if deployment is not None
                else None
            ),
            "deployment_name": deployment["name"] if deployment is not None else None,
            "metadata_generation": (
                int(deployment.get("_generation", 1)) if deployment is not None else None
            ),
            "deploy_request_id": deploy_request_id,
            "debited_usd": f"{debit_usd:.2f}",
            "balance_after_usd": f"{MOCK_WALLET_BALANCE_USD - debit_usd:.2f}",
            "current_period_start": _iso(_now()),
            "current_period_end": record["current_period_end"],
            "entitled_until": record["current_period_end"],
        }
    else:
        deployment_query = f"&deployment_id={deployment['id']}" if deployment is not None else ""
        # The Checkout Session arm (additionalProperties: false): everything the session has not
        # produced yet is null, and the deployment is only named in the return URL.
        response = {
            "flow_type": "checkout_session",
            "funding_source": "stripe",
            "action_url": None,
            "checkout_url": f"{_web_base_url()}/deploy?mockCheckout=1{deployment_query}",
            "client_secret": None,
            "trial_period_days": None,
            "subscription_id": None,
            "invoice_id": None,
            "deployment_id": None,
            "agent_id": None,
            "deployment_name": None,
            "metadata_generation": None,
            "deploy_request_id": None,
            "debited_usd": None,
            "balance_after_usd": None,
            "current_period_start": None,
            "current_period_end": None,
            "entitled_until": None,
        }
    CHECKOUT_IDEMPOTENCY[key] = (body_hash, dict(response))
    return response


@app.get("/v2/deployments/by-request/{deploy_request_id}")
async def get_deployment_by_request(deploy_request_id: str) -> dict[str, Any]:
    status = DEPLOY_REQUESTS.get(deploy_request_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Deployment request not found")
    return status


@app.post("/v2/subscription/cancel")
async def cancel_subscription(request: Request) -> dict[str, Any]:
    """Cancel compute subscriptions (V2ComputeSubscriptionActionResponse); two branches, one path.

    - Service branch, ``{owner_user_id}``: the backend's tenant purge (03-08, D-23) ends every
      subscription of that owner now. Invoices are retained -- cancellation ends the
      subscription, it does not erase billing history (RESEARCH A3). An owner with nothing to
      cancel answers the same shape.
    - Dashboard branch, ``{deployment_id?, subscription_id?}``: cancels one subscription at the
      end of its period.

    Both need a credential (06.2 D-24), so the reference checks for one. The mock has no Clerk
    and dispatches by body; the hosted service must dispatch by principal (a service key reaches
    only the purge branch, a Clerk session only its own subscriptions), never by body shape alone
    (RESEARCH F5, T-06.2-08-01).
    """
    _require_bearer(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty/malformed body cancels nothing specific.
        body = {}
    if not isinstance(body, dict):
        body = {}
    owner = body.get("owner_user_id")
    if "owner_user_id" in body:
        return _purge_owner_subscriptions(owner if isinstance(owner, str) else None)
    try:
        target = ComputeSubscriptionTargetRequest.model_validate(body)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=error.errors()) from error
    record = _targeted_record(target)
    record["cancel_at_period_end"] = True
    return _action_response(record)


def _purge_owner_subscriptions(owner: str | None) -> dict[str, Any]:
    """The service branch of cancel: every subscription the owner holds ends now (D-23)."""
    owned = [record for record in _subscription_records() if record["owner_user_id"] == owner]
    # A record seeded into the gate store alone (status and plan only) belongs to the owner too.
    gate_record = SUBSCRIPTIONS.get(owner) if owner is not None else None
    if gate_record is not None and all(gate_record is not record for record in owned):
        owned.append(gate_record)
    for record in owned:
        record["status"] = "canceled"
        record["cancel_at_period_end"] = False
    last = owned[-1] if owned else {}
    return {
        **_action_response(
            {
                "status": "canceled",
                "billing_term_months": last.get("billing_term_months", 1),
                "cancel_at_period_end": False,
                "funding_source": last.get("funding_source"),
                "current_period_end": last.get("current_period_end"),
            }
        ),
        "message": "invoices retained",
    }


# ---------------------------------------------------------------------------
# The subscription center and plan changes (06.2-08, RESEARCH F2, F3).
#
# Plan changes are billing-owned operations polled on a compute path: the quote names an
# operation id, the change applies the quote as a ``plan_change`` operation under that id, and
# ``GET /v2/operations/{id}`` answers it. The mock completes a change at once: an upgrade or a
# funding switch is ``complete``, a downgrade is ``scheduled`` for the period end (the dashboard
# accepts no other terminal state for either).
# ---------------------------------------------------------------------------


def _subscription_records() -> list[dict[str, Any]]:
    """Every subscription record the mock holds, once each: the owner store and the ones bound
    to deployments (a checkout-created record is in both)."""
    seen: set[int] = set()
    records: list[dict[str, Any]] = []
    candidates = [
        *SUBSCRIPTIONS.values(),
        *(deployment.get("compute_subscription") for deployment in DEPLOYMENTS.values()),
    ]
    for record in candidates:
        if isinstance(record, dict) and "public_id" in record and id(record) not in seen:
            seen.add(id(record))
            records.append(record)
    return records


def _public_subscription_number(public_id: str | None) -> int | None:
    """The integer behind a ``csub_mock<n>`` id; None for no id, 404 for one the mock never
    issued (RESEARCH Pitfall 11: one subscription, two id forms)."""
    if public_id is None:
        return None
    digits = public_id.removeprefix("csub_mock")
    if digits == public_id or not digits.isdigit():
        raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
    return int(digits)


def _subscription_record_for(
    subscription_id: int | None, deployment_id: str | None
) -> dict[str, Any]:
    """The subscription named by its integer id, else the one bound to ``deployment_id``."""
    for record in _subscription_records():
        if subscription_id is not None:
            if record["subscription_id"] == subscription_id:
                return record
        elif deployment_id is not None and record.get("deployment_id") == deployment_id:
            return record
    raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})


def _record_plan_slug(record: dict[str, Any]) -> str:
    """A deployment-bound subscription is on its deployment's plan; an orphan keeps its own."""
    deployment = DEPLOYMENTS.get(record.get("deployment_id") or "")
    if deployment is not None:
        return str(deployment["config_info"]["compute_plan_slug"])
    return str(record["plan_slug"])


def _set_record_plan(record: dict[str, Any], plan_slug: str, billing_term_months: int) -> None:
    record["plan_slug"] = plan_slug
    record["plan_id"] = plan_slug
    record["billing_term_months"] = billing_term_months
    record["price_cents"] = _TERM_PRICE_CENTS[(plan_slug, billing_term_months)]
    deployment = DEPLOYMENTS.get(record.get("deployment_id") or "")
    if deployment is not None:
        deployment["config_info"]["compute_plan_slug"] = plan_slug


def _display_status(record: dict[str, Any]) -> str:
    """The dashboard's status: a live subscription set to end at period end is ``canceling``.

    The record keeps ``active`` meanwhile, so the gate read (which has no ``canceling``) still
    unlocks until the period ends (RESEARCH Pitfall 5).
    """
    status = str(record["status"])
    if status in {"active", "trialing"} and record.get("cancel_at_period_end"):
        return "canceling"
    return status


def _action_response(record: dict[str, Any]) -> dict[str, Any]:
    """V2ComputeSubscriptionActionResponse for one subscription."""
    return {
        "status": _display_status(record),
        "funding_source": record.get("funding_source"),
        "billing_term_months": record.get("billing_term_months", 1),
        "cancel_at_period_end": bool(record.get("cancel_at_period_end", False)),
        "current_period_end": record.get("current_period_end"),
        "cancel_at": None,
        "invoice_due_at": None,
        "recovery_action": None,
        "pending_plan_slug": record.get("pending_plan_slug"),
        "action_state": None,
        "message": None,
    }


def _contract_deployment_resource(deployment: dict[str, Any]) -> dict[str, Any]:
    """The deployment as a contract ``HostedDeploymentResource``: the read's own resource."""
    return json.loads(json.dumps(_deployment_read_response(deployment)["resource"]))


class ComputePlanChangeQuoteRequest(BaseModel):
    """Body for POST /v2/subscription/plan/quote (V2ComputePlanChangeQuoteRequest)."""

    model_config = ConfigDict(extra="forbid")

    subscription_id: int | None = Field(default=None, gt=0)
    deployment_id: str | None = Field(default=None, pattern=r"^hdep_.+")
    target_plan_slug: Literal["compute_basic", "compute_performance"]
    target_billing_term_months: Literal[1, 12]
    funding_source: Literal["stripe", "wallet"] | None = None


class ComputePlanChangeRequest(BaseModel):
    """Body for POST /v2/subscription/plan/change (V2ComputePlanChangeRequest)."""

    model_config = ConfigDict(extra="forbid")

    operation_id: str = Field(min_length=1, max_length=191)


class ComputeSubscriptionTargetRequest(BaseModel):
    """Which subscription a dashboard action names: by deployment or by its ``csub_`` id."""

    model_config = ConfigDict(extra="forbid")

    deployment_id: str | None = Field(default=None, pattern=r"^hdep_.+")
    subscription_id: str | None = Field(default=None, pattern=r"^csub_.+")


def _targeted_record(body: ComputeSubscriptionTargetRequest) -> dict[str, Any]:
    return _subscription_record_for(
        _public_subscription_number(body.subscription_id), body.deployment_id
    )


@app.post("/v2/subscription/plan/quote")
async def quote_plan_change(body: ComputePlanChangeQuoteRequest) -> dict[str, Any]:
    """Price a plan change (V2ComputePlanChangeQuoteResponse).

    A more expensive offer is an immediate prorated upgrade, a cheaper one a downgrade at the
    period end, and the same offer under another funding source a switch for future renewals.
    """
    record = _subscription_record_for(body.subscription_id, body.deployment_id)
    deployment_id = record.get("deployment_id")
    if deployment_id is None or deployment_id not in DEPLOYMENTS:
        raise HTTPException(status_code=409, detail={"code": "deployment_required"})
    current_plan = _record_plan_slug(record)
    current_term = int(record["billing_term_months"])
    current_funding = record.get("funding_source") or "stripe"
    target_funding = body.funding_source or current_funding
    current_price = _TERM_PRICE_CENTS[(current_plan, current_term)]
    target_price = _TERM_PRICE_CENTS[(body.target_plan_slug, body.target_billing_term_months)]
    now = _now()
    period_end = str(record["current_period_end"])
    if (current_plan, current_term) == (body.target_plan_slug, body.target_billing_term_months):
        if target_funding == current_funding:
            raise HTTPException(status_code=409, detail={"code": "plan_unchanged"})
        change_kind, billing_effect, effective_at = (
            "funding_source_switch",
            "future_renewals",
            period_end,
        )
    elif target_price > current_price:
        change_kind, billing_effect, effective_at = (
            "immediate_upgrade",
            "immediate_proration",
            _iso(now),
        )
    else:
        change_kind, billing_effect, effective_at = "scheduled_downgrade", "period_end", period_end
    amount_cents = target_price - current_price if change_kind == "immediate_upgrade" else 0
    operation_id = f"ops_plan_{uuid.uuid4().hex}"
    expires_at = now + PLAN_CHANGE_QUOTE_VALIDITY
    quote = {
        "operation_id": operation_id,
        "subscription_id": record["subscription_id"],
        "funding_source": target_funding,
        "current_plan_slug": current_plan,
        "target_plan_slug": body.target_plan_slug,
        "current_billing_term_months": current_term,
        "target_billing_term_months": body.target_billing_term_months,
        "change_kind": change_kind,
        "billing_effect": billing_effect,
        "status": "quoted",
        "effective_at": effective_at,
        "proration_date": effective_at,
        "expires_at": _iso(expires_at),
        "amount_cents": amount_cents,
        "amount_usd": f"{Decimal(amount_cents) / Decimal(100):.2f}",
        "currency": "usd",
        "stripe_invoice_preview_id": None,
    }
    PLAN_CHANGE_QUOTES[operation_id] = {
        "quote": quote,
        "deployment_id": deployment_id,
        "expires_at": expires_at,
    }
    return quote


@app.post("/v2/subscription/plan/change")
async def change_plan(
    request: Request, response: Response, body: ComputePlanChangeRequest
) -> dict[str, Any]:
    """Apply a quoted plan change as a ``plan_change`` operation named by the quote's id (202).

    A repeat for an already-applied quote answers the stored operation, so a retried change (the
    dashboard keys it by the operation id) never applies twice.
    """
    response.status_code = 202
    key = _require_idempotency_key(request)
    stored = PLAN_CHANGE_QUOTES.get(body.operation_id)
    if stored is None:
        raise HTTPException(status_code=404, detail={"code": "plan_change_quote_not_found"})
    applied = OPERATIONS.get(body.operation_id)
    if applied is not None:
        return applied
    if stored["expires_at"] <= _now():
        raise HTTPException(status_code=410, detail={"code": "plan_change_quote_expired"})
    quote = stored["quote"]
    deployment = _get_deployment_record(stored["deployment_id"])
    record = _subscription_record_for(quote["subscription_id"], None)
    change_kind = quote["change_kind"]
    if change_kind == "immediate_upgrade":
        _set_record_plan(record, quote["target_plan_slug"], quote["target_billing_term_months"])
        record["pending_plan_slug"] = None
    elif change_kind == "scheduled_downgrade":
        record["pending_plan_slug"] = quote["target_plan_slug"]
    record["funding_source"] = quote["funding_source"]
    operation = _accept_operation(
        deployment,
        verb="plan_change",
        idempotency_key=key,
        operation_id=body.operation_id,
    )
    operation["metadata"]["planChange"] = {
        "@type": "type.googleapis.com/clawdi.v2.ComputePlanChangeProgress",
        "operationId": body.operation_id,
        "subscriptionId": quote["subscription_id"],
        "fundingSource": quote["funding_source"],
        "changeKind": change_kind,
        "billingEffect": quote["billing_effect"],
        "sourcePlanSlug": quote["current_plan_slug"],
        "targetPlanSlug": quote["target_plan_slug"],
        "targetBillingTermMonths": quote["target_billing_term_months"],
        "state": "scheduled" if change_kind == "scheduled_downgrade" else "complete",
        "effectiveAt": quote["effective_at"],
    }
    operation["response"]["deployment"] = _contract_deployment_resource(deployment)
    return operation


@app.post("/v2/subscription/plan/cancel-scheduled-change")
async def cancel_scheduled_plan_change(body: ComputeSubscriptionTargetRequest) -> dict[str, Any]:
    """Drop a scheduled downgrade (V2ComputeSubscriptionActionResponse)."""
    record = _targeted_record(body)
    record["pending_plan_slug"] = None
    return _action_response(record)


def _next_subscription_id() -> int:
    """One more than every integer subscription id the mock holds, bound or not."""
    used = [
        subscription["subscription_id"]
        for subscription in [
            *SUBSCRIPTIONS.values(),
            *(deployment.get("compute_subscription") for deployment in DEPLOYMENTS.values()),
        ]
        if isinstance(subscription, dict) and isinstance(subscription.get("subscription_id"), int)
    ]
    return max(used, default=0) + 1


def _bound_deployment(record: dict[str, Any]) -> dict[str, Any] | None:
    deployment = DEPLOYMENTS.get(record.get("deployment_id") or "")
    if deployment is None or deployment.get("status") == "deleted":
        return None
    return deployment


def _subscription_list_item(record: dict[str, Any]) -> dict[str, Any]:
    """V2ComputeSubscriptionListItem: every required key, nulls where the mock has no state."""
    deployment = _bound_deployment(record)
    plan_slug = _record_plan_slug(record)
    return {
        "subscription_id": record["public_id"],
        "subscription_kind": "paid",
        "plan_slug": plan_slug,
        "funding_source": record.get("funding_source"),
        "status": _display_status(record),
        "price_cents": _TERM_PRICE_CENTS[(plan_slug, record["billing_term_months"])],
        "currency": "usd",
        "billing_term_months": record["billing_term_months"],
        "current_period_end": record.get("current_period_end"),
        "cancel_at_period_end": bool(record.get("cancel_at_period_end")),
        "deployment_id": deployment["id"] if deployment is not None else None,
        "agent_name": deployment["name"] if deployment is not None else None,
        "is_orphan": deployment is None,
        "payment_state": record.get("payment_state", "ok"),
        "latest_failed_invoice_hosted_url": None,
        "next_payment_attempt_at": None,
        "recovery_action": None,
        "pending_plan_slug": record.get("pending_plan_slug"),
        "lifecycle_status": None,
        "recovery_blocked_reason": None,
        "actions": None,
    }


def _ordered_subscription_records() -> list[dict[str, Any]]:
    return sorted(_subscription_records(), key=lambda record: record["subscription_id"])


@app.get("/v2/subscriptions")
async def list_subscriptions(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    """The subscription center (V2ComputeSubscriptionListResponse).

    The mock has no Clerk, so its dashboard routes are single-tenant and this lists every record;
    the hosted service scopes the list to the session's owner.
    """
    items = [_subscription_list_item(record) for record in _ordered_subscription_records()]
    return {"items": items[: max(limit, 0)], "has_more": len(items) > limit, "next_cursor": None}


@app.get("/v2/subscriptions/reusable")
async def reusable_subscriptions(limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
    """Live subscriptions with no deployment, which a new deploy can reuse
    (V2ComputeReusableSubscriptionsResponse)."""
    items = [
        {
            "subscription_id": record["public_id"],
            "plan_slug": _record_plan_slug(record),
            "billing_term_months": record["billing_term_months"],
            "funding_source": _line_item_funding(record),
            "status": _display_status(record),
            "price_cents": _TERM_PRICE_CENTS[
                (_record_plan_slug(record), record["billing_term_months"])
            ],
            "currency": "usd",
            "current_period_end": record.get("current_period_end"),
            "entitled_until": record["current_period_end"],
            "cancel_at_period_end": bool(record.get("cancel_at_period_end")),
        }
        for record in _ordered_subscription_records()
        if _bound_deployment(record) is None
        and record.get("funding_source") in {"stripe", "wallet"}
        and _display_status(record) in {"trialing", "active", "canceling"}
    ]
    return {"items": items[: max(limit, 0)], "has_more": len(items) > limit, "next_cursor": None}


@app.post("/v2/subscription/resume")
async def resume_subscription(body: ComputeSubscriptionTargetRequest) -> dict[str, Any]:
    """Undo a cancel at period end (V2ComputeSubscriptionActionResponse)."""
    record = _targeted_record(body)
    if record["status"] == "canceled":
        raise HTTPException(status_code=409, detail={"code": "subscription_canceled"})
    record["cancel_at_period_end"] = False
    record["status"] = "active"
    return _action_response(record)


class ComputeFixPaymentRequest(BaseModel):
    """Body for POST /v2/subscription/fix-payment (V2ComputeFixPaymentRequest)."""

    model_config = ConfigDict(extra="forbid")

    locale: str | None = None
    deployment_id: str | None = Field(default=None, pattern=r"^hdep_.+")


@app.post("/v2/subscription/fix-payment")
async def fix_payment(body: ComputeFixPaymentRequest | None = None) -> dict[str, Any]:
    """Recover a failed payment (V2PortalResponse); the mock sends the user to the portal."""
    return await portal()


# ---------------------------------------------------------------------------
# Companion quotes and line items (plan 06-17, APP-08, D-07).
#
# Server to server: the backend sends ``Authorization: Bearer <DEPLOY_API_KEY>`` and names the
# owner in the body, as ``POST /v2/subscription/cancel`` already takes it. A quote is per
# (companion, isolation tier) because the tier is what drives real cost, and it is valid for
# ``COMPANION_QUOTE_VALIDITY``. A line item carries the quoted monthly amount and is started and
# stopped under Idempotency-Keys derived from the installation id, so a retried call is a no-op.
#
# ``GET /v2/companions/line-items`` lists one owner's lines for the backend's orphan sweep (06.2
# D-17): a line whose start response was lost keeps billing until a list-and-compare finds it. It
# lands here first, per ADR-0005, and like every route in this block it is service-key only.
#
# The same list feeds the backend's companion billing reconciliation (INT-4): ``state=all`` with
# ``since`` lists every line still running at or after that instant, and each row carries the
# line's ``funding_source`` plus, for a wallet line, ``charged_cents`` / ``credited_cents`` -- what
# the wallet ledger debited (the month charged at start) and credited (the unused share of that
# month at stop, half up to the cent) for it. A card line's amounts are null: Stripe holds them.
# The mock models no monthly renewal, so a wallet line here is charged one month.
# ---------------------------------------------------------------------------

COMPANION_QUOTE_VALIDITY = timedelta(minutes=15)
# The compute-subscription states that can fund a companion line (06.2 D-11).
COMPANION_FUNDING_STATUSES = frozenset({"active", "trialing", "past_due"})

# TEST FIXTURES, NOT PRICES. Real per-tier prices are the hosted service's configuration (a
# cross-repo user-setup action); these exist only so the contract can be exercised. Only the
# standard tier is priced here -- dedicated and shared answer ``tier_not_priced``, which is how an
# unpriced tier looks on the wire. Values: (monthly USD micros, storage USD micros per GiB-month).
COMPANION_PRICE_FIXTURES: dict[tuple[str, str], tuple[int, int]] = {
    ("postiz", "standard"): (9_000_000, 120_000),
}


class CompanionQuoteRequest(BaseModel):
    owner_user_id: str
    companion_key: str
    isolation_tier: str


class CompanionLineItemStartRequest(BaseModel):
    quote_id: str
    installation_id: str
    owner_user_id: str


def _require_bearer(request: Request) -> None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme != "Bearer" or not token.strip():
        raise HTTPException(status_code=401, detail={"code": "unauthenticated"})


def _require_idempotency_key(request: Request) -> str:
    idempotency_key = request.headers.get("Idempotency-Key", "").strip()
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    return idempotency_key


def _quote_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "quote_id": record["quote_id"],
        "owner_user_id": record["owner_user_id"],
        "companion_key": record["companion_key"],
        "isolation_tier": record["isolation_tier"],
        "currency": "usd",
        "monthly_price_usd_micros": record["monthly_price_usd_micros"],
        "storage_price_usd_micros_per_gib_month": record["storage_price_usd_micros_per_gib_month"],
        "expires_at": _iso(record["expires_at"]),
    }


def _line_item_view(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "line_item_id": record["line_item_id"],
        "started_at": _iso(record["started_at"]),
        "monthly_price_usd_micros": record["monthly_price_usd_micros"],
    }


def _one_month_after(moment: datetime) -> datetime:
    """The same instant one calendar month later, clamped to the month's last day."""
    year, month = (moment.year + 1, 1) if moment.month == 12 else (moment.year, moment.month + 1)
    last_day = calendar.monthrange(year, month)[1]
    return moment.replace(year=year, month=month, day=min(moment.day, last_day))


def _line_item_funding(record: dict[str, Any]) -> str:
    """The line's funding; a record written without one is wallet-funded, as a start records."""
    return record.get("funding_source") or "wallet"


def _line_item_amounts(record: dict[str, Any]) -> tuple[int | None, int | None]:
    """(charged, credited) cents for a wallet line, (None, None) for a card line."""
    if _line_item_funding(record) != "wallet":
        return None, None
    cents = record["monthly_price_usd_micros"] // 10_000
    stopped_at = record["stopped_at"]
    if stopped_at is None:
        return cents, 0
    started_at = record["started_at"]
    period_end = _one_month_after(started_at)
    period = (period_end - started_at) // timedelta(microseconds=1)
    remaining = (period_end - stopped_at) // timedelta(microseconds=1)
    if period <= 0 or remaining <= 0:
        return cents, 0
    remaining = min(remaining, period)
    return cents, (2 * cents * remaining + period) // (2 * period)


def _line_item_list_view(record: dict[str, Any]) -> dict[str, Any]:
    stopped_at = record["stopped_at"]
    charged_cents, credited_cents = _line_item_amounts(record)
    return {
        "line_item_id": record["line_item_id"],
        "installation_id": record["installation_id"],
        "owner_user_id": record["owner_user_id"],
        "quote_id": record["quote_id"],
        "monthly_price_usd_micros": record["monthly_price_usd_micros"],
        "started_at": _iso(record["started_at"]),
        "stopped_at": _iso(stopped_at) if stopped_at is not None else None,
        "funding_source": _line_item_funding(record),
        "charged_cents": charged_cents,
        "credited_cents": credited_cents,
    }


@app.post("/v2/companions/quote")
async def quote_companion(request: Request, body: CompanionQuoteRequest) -> dict[str, Any]:
    """Price one companion at one isolation tier for one owner; 422 for a tier with no price."""
    _require_bearer(request)
    price = COMPANION_PRICE_FIXTURES.get((body.companion_key, body.isolation_tier))
    if price is None:
        raise HTTPException(status_code=422, detail={"code": "tier_not_priced"})
    monthly, storage = price
    quote_id = f"cq_{uuid.uuid4().hex}"
    COMPANION_QUOTES[quote_id] = {
        "quote_id": quote_id,
        "owner_user_id": body.owner_user_id,
        "companion_key": body.companion_key,
        "isolation_tier": body.isolation_tier,
        "monthly_price_usd_micros": monthly,
        "storage_price_usd_micros_per_gib_month": storage,
        "expires_at": _now() + COMPANION_QUOTE_VALIDITY,
    }
    return _quote_view(COMPANION_QUOTES[quote_id])


@app.get("/v2/companions/quotes/{quote_id}")
async def read_companion_quote(request: Request, quote_id: str) -> dict[str, Any]:
    """The quote, or 404 for an unknown one, or 410 once its validity window has passed."""
    _require_bearer(request)
    record = COMPANION_QUOTES.get(quote_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "quote_not_found"})
    if record["expires_at"] <= _now():
        raise HTTPException(status_code=410, detail={"code": "quote_expired"})
    return _quote_view(record)


@app.post("/v2/companions/line-items")
async def start_line_item(request: Request, body: CompanionLineItemStartRequest) -> dict[str, Any]:
    """Start billing one installation at its quoted amount; a repeated key returns the same line.

    The quote's validity window governs the install CONFIRMATION, not this call: an install takes
    minutes to become ready, and the tenant is billed the price they confirmed, however long the
    provisioning took. The quote must still exist and belong to the owner named. A companion line
    follows the owner's compute subscription funding (06.2 D-11), so an owner with none is refused.
    """
    _require_bearer(request)
    idempotency_key = _require_idempotency_key(request)
    existing_id = COMPANION_LINE_ITEM_KEYS.get(idempotency_key)
    if existing_id is not None:
        existing = COMPANION_LINE_ITEMS[existing_id]
        if existing["installation_id"] != body.installation_id:
            raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
        return _line_item_view(existing)
    quote = COMPANION_QUOTES.get(body.quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail={"code": "quote_not_found"})
    if quote["owner_user_id"] != body.owner_user_id:
        raise HTTPException(status_code=409, detail={"code": "quote_owner_mismatch"})
    subscription = SUBSCRIPTIONS.get(body.owner_user_id)
    if subscription is None or subscription.get("status") not in COMPANION_FUNDING_STATUSES:
        raise HTTPException(status_code=409, detail={"code": "no_active_subscription"})
    line_item_id = f"cli_{uuid.uuid4().hex}"
    COMPANION_LINE_ITEMS[line_item_id] = {
        "line_item_id": line_item_id,
        "quote_id": body.quote_id,
        "installation_id": body.installation_id,
        "owner_user_id": body.owner_user_id,
        "monthly_price_usd_micros": quote["monthly_price_usd_micros"],
        "started_at": _now(),
        "stopped_at": None,
        # The line follows the owner's subscription funding (06.2 D-11); a seeded subscription
        # that names none is wallet-funded, as the hosted contract owners are.
        "funding_source": subscription.get("funding_source") or "wallet",
    }
    COMPANION_LINE_ITEM_KEYS[idempotency_key] = line_item_id
    return _line_item_view(COMPANION_LINE_ITEMS[line_item_id])


@app.get("/v2/companions/line-items")
async def list_line_items(
    request: Request,
    owner_user_id: str,
    state: Literal["active", "all"] = "active",
    cursor: str | None = None,
    since: datetime | None = None,
) -> dict[str, Any]:
    """One owner's companion lines, oldest first; ``state=all`` includes stopped lines, and
    ``since`` (a naive time is UTC) keeps only the lines still running at or after it.

    The mock serves a single page and ignores ``cursor``, so ``next_cursor`` is always null; the
    hosted service pages and a client follows ``next_cursor`` until it is null.
    """
    _require_bearer(request)
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    records = [
        record
        for record in COMPANION_LINE_ITEMS.values()
        if record["owner_user_id"] == owner_user_id
        and (state == "all" or record["stopped_at"] is None)
        and (since is None or record["stopped_at"] is None or record["stopped_at"] >= since)
    ]
    records.sort(key=lambda record: (record["started_at"], record["line_item_id"]))
    return {"items": [_line_item_list_view(record) for record in records], "next_cursor": None}


@app.delete("/v2/companions/line-items/{line_item_id}")
async def stop_line_item(request: Request, line_item_id: str) -> dict[str, Any]:
    """Stop one line item; a repeat returns the same ``stopped_at``, 404 for an unknown line.

    A line started and stopped at once is zero-length, never negative: ``stopped_at`` is never
    earlier than ``started_at``.
    """
    _require_bearer(request)
    _require_idempotency_key(request)
    record = COMPANION_LINE_ITEMS.get(line_item_id)
    if record is None:
        raise HTTPException(status_code=404, detail={"code": "line_item_not_found"})
    if record["stopped_at"] is None:
        record["stopped_at"] = max(_now(), record["started_at"])
    return {"line_item_id": line_item_id, "stopped_at": _iso(record["stopped_at"])}


@app.post("/v2/subscription/portal")
async def portal() -> dict[str, Any]:
    portal_url = f"{_web_base_url()}/?settings=billing-plan&mockPortal=1"
    return {
        "url": portal_url,
        "portal_url": portal_url,
        "status": "mock",
    }


@app.get("/v2/usage")
async def usage() -> dict[str, Any]:
    today = _now().date()
    return {
        "period_start": str(today - timedelta(days=6)),
        "period_end": str(today),
        "availability": "complete",
        "unavailable_sections": [],
        "total_usd": "12.80",
        "total_requests": 94,
        "by_agent": [
            {
                "agent_id": DEV_V2_DEPLOYMENT_ID,
                "agent_name": "dev-sidebar-preview",
                "amount_usd": "9.80",
                "requests": 72,
            },
            {
                "agent_id": None,
                "agent_name": None,
                "amount_usd": "3.00",
                "requests": 22,
            },
        ],
        "by_model": [
            {
                "model": "openai/gpt-4o-mini",
                "provider": DEV_V2_PROVIDER_ID,
                "amount_usd": "9.80",
                "requests": 72,
            },
            {
                "model": "managed/default",
                "provider": "clawdi",
                "amount_usd": "3.00",
                "requests": 22,
            },
        ],
        "by_day": [
            {
                "date": str(today - timedelta(days=i)),
                "amount_usd": f"{(120 + i * 25) / 100:.2f}",
            }
            for i in reversed(range(7))
        ],
    }


def _next_month_start(now: datetime) -> datetime:
    """The first instant of the UTC month after ``now``."""
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, tzinfo=UTC)


@app.get("/v2/wallet")
async def wallet() -> dict[str, Any]:
    """The wallet read (V2WalletResponse): a prepaid USD balance with auto-reload active.

    x402 stays deferred (06.2 D-19): ``x402_enabled: false``, no payment authority, status
    ``idle`` and no attempt. That exact quadruple keeps the dashboard's x402 card in its
    deferred "Coming soon" state.
    """
    now = _now()
    return {
        "balance_usd": f"{MOCK_WALLET_BALANCE_USD:.2f}",
        "x402_enabled": False,
        "x402_payment_authority": None,
        "x402_payment_status": "idle",
        "x402_payment_attempt": None,
        "auto_reload_enabled": True,
        "auto_reload_has_payment_method": True,
        "auto_reload_card": {
            "brand": "visa",
            "last4": "4242",
            "exp_month": 12,
            "exp_year": now.year + 3,
        },
        "auto_reload_currency": "usd",
        "auto_reload_required_consent_version": AUTO_RELOAD_CONSENT_VERSION,
        "auto_reload_amount_policy": AUTO_RELOAD_AMOUNT_POLICY,
        "auto_reload_consent_version": AUTO_RELOAD_CONSENT_VERSION,
        "auto_reload_consented_at": _iso(now - timedelta(days=7)),
        "auto_reload_threshold_usd": "10.00",
        "auto_reload_amount_cents": 1000,
        "auto_reload_monthly_cap_cents": 5000,
        "auto_reload_monthly_spent_cents": 0,
        "auto_reload_period_end": _iso(_next_month_start(now)),
        "auto_reload_status": "active",
        "auto_reload_action": None,
    }


@app.get("/v2/wallet/transactions")
async def wallet_transactions() -> dict[str, Any]:
    now = _now()
    page: dict[str, Any] = {
        "items": [
            {
                "id": "wallet:txn_dev_topup",
                "kind": "topup",
                "occurred_at": _iso(now - timedelta(days=2)),
                "amount": "50.00",
                "currency": "usd",
                "direction": "credit",
                "status": "applied",
                "funding": "wallet",
                "payment_reference": "pi_dev_topup",
                "receipt_url": "https://example.invalid/receipt/dev",
                "context": None,
            },
            {
                "id": "wallet:txn_dev_compute",
                "kind": "compute_charge",
                "occurred_at": _iso(now - timedelta(hours=4)),
                "amount": "20.00",
                "currency": "usd",
                "direction": "debit",
                "status": "applied",
                "funding": "wallet",
                "context": {
                    "plan": "compute_performance",
                    "period_start": _iso(now - timedelta(hours=4)),
                    "period_end": _iso(now + timedelta(days=30)),
                    "agent_name": "Hermes",
                    "deployment_id": "hdep_dev_mock",
                },
            },
            {
                # A companion line billed against the wallet (UI-SPEC H4.4, mock first).
                "id": "wallet:txn_dev_companion",
                "kind": "companion_charge",
                "occurred_at": _iso(now - timedelta(hours=1)),
                "amount": "9.00",
                "currency": "usd",
                "direction": "debit",
                "status": "applied",
                "funding": "wallet",
                "context": None,
            },
        ],
        "has_more": False,
        "next_cursor": None,
    }
    # Newest first, as the hosted list orders it.
    page["items"].sort(key=lambda item: item["occurred_at"], reverse=True)
    return page


class WalletTopupRequest(BaseModel):
    """V2WalletTopupRequest."""

    amount_cents: int = Field(gt=0)
    locale: str | None = None


class WalletAutoReloadSetupRequest(BaseModel):
    """V2WalletAutoReloadSetupRequest. ``consent_version`` is checked by hand so a stale version
    answers ``consent_version_mismatch`` rather than a generic validation error."""

    consent_version: str
    auto_reload_threshold_usd: Decimal = Field(ge=1)
    auto_reload_amount_cents: int = Field(ge=500, le=50_000)
    auto_reload_monthly_cap_cents: int = Field(ge=0, le=2_147_483_647)


class WalletAutoReloadSetupFinalizeRequest(BaseModel):
    """V2WalletAutoReloadSetupFinalizeRequest."""

    setup_identity: str
    setup_intent_id: str


class WalletAutoReloadRequest(BaseModel):
    """V2WalletAutoReloadRequest: every field optional, null leaves it unchanged."""

    auto_reload_enabled: bool | None = None
    auto_reload_threshold_usd: Decimal | None = Field(default=None, ge=1)
    auto_reload_amount_cents: int | None = Field(default=None, ge=500, le=50_000)
    auto_reload_monthly_cap_cents: int | None = Field(default=None, ge=0, le=2_147_483_647)


def _body_hash(body: BaseModel) -> str:
    return _json_body_hash(body.model_dump(mode="json"))


def _wallet_replay(route: str, key: str, body_hash: str) -> dict[str, Any] | None:
    """The response first returned under ``key`` for an identical body (D-22).

    The same key with a different body is ``409 idempotency_key_reused``; an unseen key is None.
    """
    stored = WALLET_IDEMPOTENCY.get((route, key))
    if stored is None:
        return None
    stored_hash, response = stored
    if stored_hash != body_hash:
        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
    return dict(response)


def _wallet_remember(route: str, key: str, body_hash: str, response: dict[str, Any]) -> None:
    WALLET_IDEMPOTENCY[(route, key)] = (body_hash, dict(response))


@app.post("/v2/wallet/topup")
async def topup(request: Request, body: WalletTopupRequest) -> dict[str, Any]:
    """Start a wallet top-up (V2WalletTopupResponse); a repeated key replays the original.

    The mock answers ``succeeded`` with no client secret: the top-up dialog treats ``succeeded``
    as paid without mounting Stripe Elements against a fake secret. The hosted service answers
    ``open`` with a real Checkout Session secret; both conform.
    """
    key = _require_idempotency_key(request)
    body_hash = _body_hash(body)
    replayed = _wallet_replay("topup", key, body_hash)
    if replayed is not None:
        return replayed
    amount_usd = Decimal(body.amount_cents) / Decimal(100)
    response = {
        "checkout_session_id": f"cs_mock_{uuid.uuid4().hex}",
        "status": "succeeded",
        "flow_type": "checkout_session",
        "payment_intent_id": None,
        "client_secret": None,
        "amount_usd": f"{amount_usd:.2f}",
    }
    WALLET_TOPUPS[response["checkout_session_id"]] = dict(response)
    _wallet_remember("topup", key, body_hash, response)
    return response


@app.get("/v2/wallet/topup/checkout/{checkout_session_id}")
async def topup_status(checkout_session_id: str) -> dict[str, Any]:
    """Poll a top-up's Checkout Session (V2WalletTopupResponse)."""
    topup_record = WALLET_TOPUPS.get(checkout_session_id)
    if topup_record is None:
        raise HTTPException(status_code=404, detail={"code": "checkout_session_not_found"})
    return dict(topup_record)


@app.post("/v2/wallet/auto-reload/setup-intent")
async def auto_reload_setup_intent(
    request: Request, body: WalletAutoReloadSetupRequest
) -> dict[str, Any]:
    """Start saving a card for off-session auto-reload under the consented terms.

    A repeated key replays the original; a consent version other than the current one is
    ``422 consent_version_mismatch``.
    """
    key = _require_idempotency_key(request)
    if body.consent_version != AUTO_RELOAD_CONSENT_VERSION:
        raise HTTPException(status_code=422, detail={"code": "consent_version_mismatch"})
    body_hash = _body_hash(body)
    replayed = _wallet_replay("setup-intent", key, body_hash)
    if replayed is not None:
        return replayed
    response = {
        "setup_identity": f"wsetup_{secrets.token_hex(32)}",
        "setup_intent_id": f"seti_mock_{uuid.uuid4().hex}",
        "client_secret": f"seti_mock_{uuid.uuid4().hex}_secret_mock",
        "customer_session_client_secret": None,
        "status": "requires_payment_method",
        "currency": "usd",
        "consent_version": AUTO_RELOAD_CONSENT_VERSION,
        "amount_policy": AUTO_RELOAD_AMOUNT_POLICY,
        "auto_reload_threshold_usd": f"{body.auto_reload_threshold_usd:.2f}",
        "auto_reload_amount_cents": body.auto_reload_amount_cents,
        "auto_reload_monthly_cap_cents": body.auto_reload_monthly_cap_cents,
    }
    WALLET_SETUPS[response["setup_identity"]] = dict(response)
    _wallet_remember("setup-intent", key, body_hash, response)
    return response


@app.post("/v2/wallet/auto-reload/setup-intent/finalize")
async def auto_reload_setup_finalize(body: WalletAutoReloadSetupFinalizeRequest) -> dict[str, Any]:
    """Finish an auto-reload setup: the wallet view with auto-reload active on its terms.

    Mock semantics: the SetupIntent counts as confirmed at once. A hosted SetupIntent stays
    unconfirmed until Stripe.js confirms it, and the hosted service answers
    ``409 setup_not_complete`` until then.
    """
    setup = WALLET_SETUPS.get(body.setup_identity)
    if setup is None or setup["setup_intent_id"] != body.setup_intent_id:
        raise HTTPException(status_code=404, detail={"code": "setup_not_found"})
    state = await wallet()
    state.update(
        {
            "auto_reload_enabled": True,
            "auto_reload_status": "active",
            "auto_reload_threshold_usd": setup["auto_reload_threshold_usd"],
            "auto_reload_amount_cents": setup["auto_reload_amount_cents"],
            "auto_reload_monthly_cap_cents": setup["auto_reload_monthly_cap_cents"],
        }
    )
    return state


@app.put("/v2/wallet/auto-reload")
async def auto_reload(body: WalletAutoReloadRequest) -> dict[str, Any]:
    """Update auto-reload settings (V2WalletResponse); turning it off reports status ``off``."""
    state = await wallet()
    if body.auto_reload_enabled is not None:
        state["auto_reload_enabled"] = body.auto_reload_enabled
        if not body.auto_reload_enabled:
            state["auto_reload_status"] = "off"
    if body.auto_reload_threshold_usd is not None:
        state["auto_reload_threshold_usd"] = f"{body.auto_reload_threshold_usd:.2f}"
    if body.auto_reload_amount_cents is not None:
        state["auto_reload_amount_cents"] = body.auto_reload_amount_cents
    if body.auto_reload_monthly_cap_cents is not None:
        state["auto_reload_monthly_cap_cents"] = body.auto_reload_monthly_cap_cents
    return state


@app.get("/v2/wallet/payment-methods")
async def wallet_payment_methods() -> dict[str, Any]:
    """Saved cards (V2WalletSavedCardsResponse): the one card auto-reload uses."""
    state = await wallet()
    return {
        "items": [
            {
                "id": "pm_mock_visa",
                "card": state["auto_reload_card"],
                "is_default": True,
                "is_auto_reload": True,
            }
        ],
        "has_more": False,
    }


# ---------------------------------------------------------------------------
# Honest stubs (06.2 D-04, D-19)
#
# Each stub answers the shape the dashboard already handles and gets real behavior from its
# owning phase: account notifications have no producer yet (D-04), and x402 is deferred (D-19).
# Nothing binds a wallet address and nothing records an attempt. Every refusal is a 4xx, never a
# 5xx, so the dashboard's retry predicate surfaces it at once instead of retrying. The x402
# top-up exchange itself stays unrouted.
# ---------------------------------------------------------------------------

X402_NOT_AVAILABLE = {
    "code": "x402_not_available",
    "message": "USDC top-up is not available yet.",
}


@app.get("/v1/me/notifications")
async def account_notifications(limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
    """No notification exists yet: one empty page (AccountNotificationListResponse)."""
    return {"items": [], "unread_count": 0, "next_cursor": None}


@app.post("/v1/me/notifications/read-all")
async def account_notifications_read_all() -> dict[str, int]:
    """Nothing unread to mark (AccountNotificationReadAllResponse)."""
    return {"updated_count": 0}


@app.patch("/v1/me/notifications/{notification_id}")
@app.delete("/v1/me/notifications/{notification_id}")
async def account_notification_mutation(notification_id: str) -> None:
    """No notification exists, so marking or deleting one is ``404 notification_not_found``."""
    raise HTTPException(status_code=404, detail={"code": "notification_not_found"})


@app.get("/v2/wallet-binding")
async def wallet_binding() -> dict[str, Any]:
    """No wallet is bound (V2WalletBindingResponse)."""
    return {"bound": False, "address": None, "verified_at": None}


@app.delete("/v2/wallet-binding", status_code=204)
async def delete_wallet_binding() -> Response:
    """Nothing is bound, so unbinding is an idempotent no-op."""
    return Response(status_code=204)


@app.post("/v2/wallet-binding/challenge")
@app.post("/v2/wallet-binding")
@app.post("/v2/x402/attempts")
async def x402_not_available() -> None:
    """Binding a wallet and starting an x402 attempt are deferred (D-19): ``409``."""
    raise HTTPException(status_code=409, detail=X402_NOT_AVAILABLE)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)
