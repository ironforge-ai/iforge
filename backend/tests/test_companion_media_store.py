"""Per-installation media bucket and Temporal namespace, provisioned by the install job (06-13).

CK-C (``bucket-per-installation-lifecycle-export``): every installation gets its own R2 bucket, an
S3 credential derived from an account API token scoped to that one bucket, and a custom media
domain; durability is a lifecycle rule plus the export, and no versioning is claimed. The Temporal
half follows CK-A: one clean namespace per installation on the shared engine, with a JWT whose only
permission is writer on it.

Nothing here reaches a live API. ``FakeOperatorApis`` stands in for both the Cloudflare API and the
Temporal frontend's HTTP API behind one ``httpx.MockTransport``, holding the state a real API would
(buckets, domains, tokens, namespaces and their search-attribute aliases), so a retry, a conflict
and a teardown are all observed as state, not as a script of canned answers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext
from app.core.config import settings
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.services import companion_media_store, companion_temporal
from app.services.companion_install import create_installation
from app.services.companion_lifecycle_steps import CompanionStepError
from app.services.companion_lifecycle_worker import (
    PROVISIONING_STEPS,
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
)
from app.services.companion_media_store import MEDIA_BUCKET_STEP
from app.services.companion_postiz import compose_postiz_secret_values
from app.services.companion_temporal import TEMPORAL_NAMESPACE_STEP
from app.services.vault_crypto import decrypt
from tests.companion_billing_stub import billing_quote
from tests.test_companion_install_job import _hosted_agent, _reload, _sessionmaker

ZONE = "companions.example"
ACCOUNT_ID = "acct0123456789abcdef"
ZONE_ID = "zone0123456789abcdef"
CLOUDFLARE_ADMIN_TOKEN = "cf-operator-admin-token-SECRET-0123456789"
R2_S3_ENDPOINT = "https://r2-s3.example.test"
R2_ADMIN_ACCESS_KEY_ID = "r2-admin-access-key-id"
R2_ADMIN_SECRET_ACCESS_KEY = "r2-admin-secret-access-key-SECRET"
TEMPORAL_HTTP_URL = "https://temporal-http.example.test"
TEMPORAL_FRONTEND_ADDRESS = "temporal.example.test:7233"
TEMPORAL_KEY_ID = "iforge-test-key"
CLOUDFLARE_HOST = "api.cloudflare.com"
CLOUDFLARE_PREFIX = f"/client/v4/accounts/{ACCOUNT_ID}"
TEXT = "INDEXED_VALUE_TYPE_TEXT"


# ---------------------------------------------------------------------------
# The fake operator APIs.
# ---------------------------------------------------------------------------


@dataclass
class FakeNamespace:
    name: str
    state: str = "NAMESPACE_STATE_REGISTERED"
    retention: str = ""
    # Field -> alias, the SQL-visibility allocation `AddSearchAttributes` itself performs.
    aliases: dict[str, str] = field(default_factory=dict)
    # Attributes a namespace carries that no alias explains (an auto-setup namespace's).
    preexisting: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeOperatorApis:
    """Cloudflare's API and Temporal's HTTP API, as state, behind one mock transport."""

    public_key_pem: bytes
    buckets: dict[str, dict[str, Any]] = field(default_factory=dict)
    domains: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: dict[str, dict[str, Any]] = field(default_factory=dict)
    namespaces: dict[str, FakeNamespace] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    # "METHOD path-suffix" -> status code, answered instead of the state machine.
    fail: dict[str, int] = field(default_factory=dict)
    created_bucket_count: int = 0
    created_namespace_count: int = 0
    token_serial: int = 0
    # Every secret this fake handed out or was handed: nothing may log any of them.
    issued_token_values: list[str] = field(default_factory=list)
    bearers_seen: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # -- dispatch -------------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        path = url.path
        self.calls.append(f"{request.method} {url.hostname}{path}")
        self.bearers_seen.append(request.headers.get("authorization", "").removeprefix("Bearer "))
        for key, status in self.fail.items():
            method, _, suffix = key.partition(" ")
            if request.method == method and path.endswith(suffix):
                return httpx.Response(status, json={"success": False, "errors": []})
        if url.hostname == CLOUDFLARE_HOST:
            assert request.headers["authorization"] == f"Bearer {CLOUDFLARE_ADMIN_TOKEN}"
            assert path.startswith(CLOUDFLARE_PREFIX), path
            return self._cloudflare(request, path[len(CLOUDFLARE_PREFIX) :])
        assert f"https://{url.hostname}" == TEMPORAL_HTTP_URL, str(request.url)
        claims = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            self.public_key_pem,
            algorithms=["RS256"],
        )
        assert claims["permissions"] == ["temporal-system:admin"], claims
        return self._temporal(request, path)

    # -- Cloudflare -----------------------------------------------------------------------------

    def _cloudflare(self, request: httpx.Request, path: str) -> httpx.Response:
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else None
        if parts[:2] == ["r2", "buckets"]:
            return self._r2(request.method, parts[2:], body)
        if parts[0] == "tokens":
            return self._tokens(request, parts[1:], body)
        raise AssertionError(f"unexpected Cloudflare call {request.method} {path}")

    def _r2(self, method: str, parts: list[str], body: Any) -> httpx.Response:
        if not parts and method == "POST":
            name = body["name"]
            if name in self.buckets:
                return _cf_error(409, 10004, "The bucket you tried to create already exists.")
            self.buckets[name] = {"name": name, "lifecycle": None, "objects": 0}
            self.created_bucket_count += 1
            return _cf_ok({"name": name, "location": "ENAM"})
        name = parts[0]
        bucket = self.buckets.get(name)
        rest = parts[1:]
        if not rest:
            if bucket is None:
                return _cf_error(404, 10006, "The specified bucket does not exist.")
            if method == "GET":
                return _cf_ok({"name": name})
            if method == "DELETE":
                if bucket["objects"]:
                    return _cf_error(409, 10008, "The bucket you tried to delete is not empty.")
                del self.buckets[name]
                return _cf_ok({})
        if bucket is None:
            return _cf_error(404, 10006, "The specified bucket does not exist.")
        if rest == ["lifecycle"] and method == "PUT":
            bucket["lifecycle"] = body
            return _cf_ok({})
        if rest[:2] == ["domains", "custom"]:
            if len(rest) == 2 and method == "POST":
                domain = body["domain"]
                if domain in self.domains:
                    return _cf_error(409, 10081, "The custom domain already exists.")
                assert body["zoneId"] == ZONE_ID
                assert body["enabled"] is True
                self.domains[domain] = {"bucket": name, **body}
                return _cf_ok({"domain": domain, "enabled": True, "zoneId": ZONE_ID})
            domain = rest[2]
            record = self.domains.get(domain)
            if record is None or record["bucket"] != name:
                return _cf_error(404, 10083, "The custom domain does not exist.")
            if method == "GET":
                return _cf_ok({"domain": domain, "enabled": True, "zoneId": ZONE_ID})
            if method == "DELETE":
                del self.domains[domain]
                return _cf_ok({"domain": domain})
        raise AssertionError(f"unexpected R2 call {method} {parts}")

    def _tokens(self, request: httpx.Request, parts: list[str], body: Any) -> httpx.Response:
        method = request.method
        if not parts and method == "GET":
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params.get("per_page", "50"))
            listed = sorted(self.tokens.values(), key=lambda token: token["id"])
            chunk = listed[(page - 1) * per_page : page * per_page]
            total_pages = max(1, -(-len(listed) // per_page))
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "errors": [],
                    "messages": [],
                    "result": [{"id": t["id"], "name": t["name"]} for t in chunk],
                    "result_info": {"page": page, "per_page": per_page, "total_pages": total_pages},
                },
            )
        if not parts and method == "POST":
            self.token_serial += 1
            token_id = f"tok{self.token_serial:029d}"
            value = f"token-value-SECRET-{uuid.uuid4().hex}"
            self.issued_token_values.append(value)
            self.tokens[token_id] = {
                "id": token_id,
                "name": body["name"],
                "value": value,
                "policies": body["policies"],
            }
            return _cf_ok({"id": token_id, "name": body["name"], "value": value})
        token = self.tokens.get(parts[0])
        if token is None:
            return _cf_error(404, 1003, "Token not found.")
        if parts[1:] == ["value"] and method == "PUT":
            token["value"] = f"token-value-SECRET-{uuid.uuid4().hex}"
            self.issued_token_values.append(token["value"])
            return _cf_ok(token["value"])
        if not parts[1:] and method == "DELETE":
            del self.tokens[parts[0]]
            return _cf_ok({"id": parts[0]})
        raise AssertionError(f"unexpected token call {method} {parts}")

    # -- Temporal -------------------------------------------------------------------------------

    def _temporal(self, request: httpx.Request, path: str) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content else None
        parts = path.strip("/").split("/")
        assert parts[:3] == ["api", "v1", "namespaces"], path
        rest = parts[3:]
        if not rest and method == "POST":
            name = body["namespace"]
            if name in self.namespaces:
                return _temporal_error(409, 6, "Namespace already exists.")
            self.namespaces[name] = FakeNamespace(
                name=name, retention=body["workflowExecutionRetentionPeriod"]
            )
            self.created_namespace_count += 1
            return httpx.Response(200, json={})
        namespace = self.namespaces.get(rest[0])
        if namespace is None:
            return _temporal_error(404, 5, f"Namespace {rest[0]} is not found.")
        if rest[1:] == [] and method == "GET":
            return httpx.Response(
                200,
                json={
                    "namespaceInfo": {"name": namespace.name, "state": namespace.state},
                    "config": {"customSearchAttributeAliases": dict(namespace.aliases)},
                },
            )
        if rest[1:] == ["search-attributes"] and method == "GET":
            custom = {alias: TEXT for alias in namespace.aliases.values()}
            custom.update(namespace.preexisting)
            return httpx.Response(
                200,
                json={
                    "customAttributes": custom,
                    "systemAttributes": {"WorkflowId": "INDEXED_VALUE_TYPE_KEYWORD"},
                    "storageSchema": {},
                },
            )
        if rest[1:] == ["update"] and method == "POST":
            assert body["namespace"] == namespace.name
            state = (body.get("updateInfo") or {}).get("state")
            if state:
                namespace.state = state
            upsert = (body.get("config") or {}).get("customSearchAttributeAliases") or {}
            for field_name, alias in upsert.items():
                assert field_name in {"Text01", "Text02", "Text03"}, field_name
                if field_name in namespace.aliases:
                    return _temporal_error(400, 3, "Custom search attribute field already set.")
                namespace.aliases[field_name] = alias
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected Temporal call {method} {path}")


def _cf_ok(result: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "errors": [], "messages": [], "result": result}
    )


def _cf_error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"success": False, "errors": [{"code": code, "message": message}], "messages": []},
    )


def _temporal_error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": message, "details": []})


class FakeMediaObjects:
    """The admin S3 client teardown empties a bucket through (``create_s3_object_store_client``)."""

    def __init__(self, apis: FakeOperatorApis) -> None:
        self.apis = apis
        self.opened: list[dict[str, Any]] = []
        self.deleted_prefixes: list[tuple[str, str]] = []
        self.closed = 0

    def factory(self, **kwargs: Any) -> Any:
        self.opened.append(kwargs)
        bucket = kwargs["bucket"]
        outer = self

        class _Client:
            def delete_prefix(self, prefix: str) -> int:
                outer.deleted_prefixes.append((bucket, prefix))
                outer.apis.calls.append(f"PURGE {bucket}/{prefix}")
                record = outer.apis.buckets.get(bucket)
                count = record["objects"] if record else 0
                if record:
                    record["objects"] = 0
                return count

            def close(self) -> None:
                outer.closed += 1

        return _Client()


# ---------------------------------------------------------------------------
# Fixtures: operator settings (all fake) and the fakes wired into both adapters.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def signing_key() -> tuple[str, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem


@pytest.fixture
def operator_settings(monkeypatch: pytest.MonkeyPatch, signing_key: tuple[str, bytes]) -> None:
    """Every operator setting both steps read, set to a fake value (no operator value exists)."""
    private_pem, _public = signing_key
    for name, value in {
        "companion_edge_zone_name": ZONE,
        "cloudflare_account_id": ACCOUNT_ID,
        "cloudflare_r2_api_token": CLOUDFLARE_ADMIN_TOKEN,
        "cloudflare_r2_s3_endpoint": R2_S3_ENDPOINT,
        "cloudflare_r2_admin_access_key_id": R2_ADMIN_ACCESS_KEY_ID,
        "cloudflare_r2_admin_secret_access_key": R2_ADMIN_SECRET_ACCESS_KEY,
        "companion_edge_zone_id": ZONE_ID,
        "companion_temporal_frontend_address": TEMPORAL_FRONTEND_ADDRESS,
        "companion_temporal_http_url": TEMPORAL_HTTP_URL,
        "companion_temporal_tls": True,
        "companion_temporal_jwt_private_key": private_pem,
        "companion_temporal_jwt_key_id": TEMPORAL_KEY_ID,
        "companion_temporal_namespace_retention_days": 7,
    }.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def fake_apis(signing_key) -> FakeOperatorApis:
    return FakeOperatorApis(public_key_pem=signing_key[1])


@pytest.fixture
def media_objects(monkeypatch: pytest.MonkeyPatch, fake_apis: FakeOperatorApis) -> FakeMediaObjects:
    """Both adapters pointed at the fakes: one mock transport, one fake admin S3 client."""
    objects = FakeMediaObjects(fake_apis)
    transport = fake_apis.transport()
    monkeypatch.setattr(companion_media_store, "_transport_override", transport)
    monkeypatch.setattr(companion_temporal, "_transport_override", transport)
    monkeypatch.setattr(companion_media_store, "create_s3_object_store_client", objects.factory)
    return objects


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


async def _new_installation(db_session: AsyncSession, seed_user) -> CompanionInstallation:
    agent_id, _deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        return await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )


async def _decrypted_atoms(db_session: AsyncSession, installation_id: uuid.UUID) -> dict[str, str]:
    rows = (
        (
            await db_session.execute(
                select(CompanionInstallationSecret)
                .where(CompanionInstallationSecret.installation_id == installation_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    return {row.secret_name: decrypt(row.encrypted_value, row.nonce) for row in rows}


def _step_resources(installation: CompanionInstallation, name: str) -> dict[str, Any] | None:
    steps = (installation.external_resources or {}).get("steps") or []
    for entry in reversed(steps):
        if isinstance(entry, dict) and entry.get("name") == name:
            resources = entry.get("resources")
            return resources if isinstance(resources, dict) else None
    return None


def _step_context(
    db_session: AsyncSession, installation: CompanionInstallation
) -> LifecycleStepContext:
    """The context the worker would hand a step, for driving one step directly."""
    return LifecycleStepContext(
        installation_id=installation.id,
        user_id=installation.user_id,
        environment_id=installation.environment_id,
        companion_key=installation.companion_key,
        operation=installation.operation,
        hostname=installation.hostname,
        sessionmaker=_sessionmaker(db_session),
    )


BASE_ATOMS = {"admin-password", "api-key", "db-password", "jwt-secret"}
STEP_ATOMS = {"media-access-key-id", "media-secret-access-key", "temporal-api-key"}


class FailingLaterStep:
    """A step after media and Temporal that refuses, so the install fails and tears down.

    When it runs, both earlier steps have completed: it marks where teardown calls begin and,
    like Postiz would have, leaves objects in the media bucket.
    """

    def __init__(self, apis: FakeOperatorApis) -> None:
        self.apis = apis
        self.calls_before_teardown = 0

    async def _provision(self, context: LifecycleStepContext) -> dict:
        for bucket in self.apis.buckets.values():
            bucket["objects"] = 3
        self.calls_before_teardown = len(self.apis.calls)
        raise RuntimeError("a later step refused")

    async def _teardown(self, context: LifecycleStepContext, resources: dict) -> None:
        raise AssertionError("a step that never completed must not be torn down")

    @property
    def step(self) -> ProvisioningStep:
        return ProvisioningStep("later", self._provision, self._teardown)


def _teardown_sequence(calls: list[str]) -> list[str]:
    """The teardown's external calls, named."""
    named: list[str] = []
    for call in calls:
        if call.startswith("PURGE "):
            named.append("purge objects")
        elif call.startswith("DELETE ") and "/domains/custom/" in call:
            named.append("detach domain")
        elif call.startswith("DELETE ") and "/tokens/" in call:
            named.append("revoke token")
        elif call.startswith("DELETE ") and "/r2/buckets/" in call:
            named.append("delete bucket")
        elif call.startswith("POST ") and call.endswith("/update"):
            named.append("delete namespace")
    return named


def _suffix(installation: CompanionInstallation) -> str:
    return installation.hostname_label.rsplit("-", 1)[1]


def _env(value: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in value.splitlines())


# ---------------------------------------------------------------------------
# Task 2 (tracer): one install provisions its own bucket and namespace, and Postiz's env names them.
# ---------------------------------------------------------------------------


async def test_install_provisions_media_and_temporal_and_env_names_them(
    db_session, seed_user, operator_settings, fake_apis, media_objects, signing_key
) -> None:
    # The two steps join the install pipeline, media first, then Temporal.
    assert [step.name for step in PROVISIONING_STEPS[:2]] == ["media", "temporal"]

    installation = await _new_installation(db_session, seed_user)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=PROVISIONING_STEPS[:2]
    )
    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "running", row.error
    suffix = _suffix(row)
    bucket = f"postiz-media-{suffix}"
    domain = f"media-{suffix}.{ZONE}"
    namespace = f"postiz-{suffix}"

    # The handles, recorded with the steps that created them.
    media = _step_resources(row, "media")
    assert media is not None
    token_id = media["token_id"]
    assert media == {
        "bucket": bucket,
        "domain": domain,
        "token_id": token_id,
        "account_id": ACCOUNT_ID,
    }
    assert _step_resources(row, "temporal") == {
        "namespace": namespace,
        "address": TEMPORAL_FRONTEND_ADDRESS,
        "tls": True,
    }

    # The resources exist, exactly once each, with the CK-C mechanics.
    assert set(fake_apis.buckets) == {bucket}
    assert fake_apis.buckets[bucket]["lifecycle"] == {
        "rules": [
            {
                "id": "abort-incomplete-multipart-uploads",
                "enabled": True,
                "conditions": {"prefix": ""},
                "abortMultipartUploadsTransition": {
                    "condition": {"type": "Age", "maxAge": 7 * 24 * 3600}
                },
            }
        ]
    }
    assert fake_apis.domains[domain]["bucket"] == bucket
    token = fake_apis.tokens[token_id]
    assert token["name"] == f"iforge-postiz-media-{row.id}"
    assert token["policies"] == [
        {
            "effect": "allow",
            "permission_groups": [{"id": "2efd5506f9c8494dacb1fa10a3e7d5b6"}],
            "resources": {f"com.cloudflare.edge.r2.bucket.{ACCOUNT_ID}_default_{bucket}": "*"},
        }
    ]
    created = fake_apis.namespaces[namespace]
    assert created.retention == f"{7 * 24 * 3600}s"
    assert sorted(created.aliases.values()) == ["organizationId", "postId"]

    # The three new atoms, stored encrypted beside the four the install generated.
    atoms = await _decrypted_atoms(db_session, row.id)
    assert atoms["media-access-key-id"] == token_id
    assert atoms["media-secret-access-key"] == hashlib.sha256(token["value"].encode()).hexdigest()
    claims = jwt.decode(atoms["temporal-api-key"], signing_key[1], algorithms=["RS256"])
    assert claims["permissions"] == [f"{namespace}:writer"]

    # And Postiz's composed environment names this installation's bucket and namespace.
    composed = compose_postiz_secret_values(row, atoms)
    app_env = _env(composed[f"secret://companions/postiz/{row.id}/app-env"])
    assert {
        key: app_env[key]
        for key in (
            "STORAGE_PROVIDER",
            "CLOUDFLARE_ACCOUNT_ID",
            "CLOUDFLARE_ACCESS_KEY",
            "CLOUDFLARE_SECRET_ACCESS_KEY",
            "CLOUDFLARE_BUCKETNAME",
            "CLOUDFLARE_BUCKET_URL",
            "CLOUDFLARE_REGION",
            "TEMPORAL_ADDRESS",
            "TEMPORAL_NAMESPACE",
            "TEMPORAL_API_KEY",
            "TEMPORAL_TLS",
        )
    } == {
        "STORAGE_PROVIDER": "cloudflare",
        "CLOUDFLARE_ACCOUNT_ID": ACCOUNT_ID,
        "CLOUDFLARE_ACCESS_KEY": token_id,
        "CLOUDFLARE_SECRET_ACCESS_KEY": atoms["media-secret-access-key"],
        "CLOUDFLARE_BUCKETNAME": bucket,
        "CLOUDFLARE_BUCKET_URL": f"https://{domain}/",
        "CLOUDFLARE_REGION": "auto",
        "TEMPORAL_ADDRESS": TEMPORAL_FRONTEND_ADDRESS,
        "TEMPORAL_NAMESPACE": namespace,
        "TEMPORAL_API_KEY": atoms["temporal-api-key"],
        "TEMPORAL_TLS": "true",
    }


# ---------------------------------------------------------------------------
# Task 3: retries adopt, teardown is complete and 404-tolerant, refusals are named and early.
# ---------------------------------------------------------------------------


async def test_retry_adopts_the_existing_bucket(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    """A crashed attempt left its bucket, domain and token; the retry adopts all three by name."""
    installation = await _new_installation(db_session, seed_user)
    suffix = _suffix(installation)
    bucket = f"postiz-media-{suffix}"
    domain = f"media-{suffix}.{ZONE}"
    fake_apis.buckets[bucket] = {"name": bucket, "lifecycle": None, "objects": 0}
    fake_apis.domains[domain] = {"bucket": bucket, "domain": domain, "zoneId": ZONE_ID}
    fake_apis.tokens["tok-left-behind"] = {
        "id": "tok-left-behind",
        "name": f"iforge-postiz-media-{installation.id}",
        "value": "token-value-SECRET-from-the-crashed-attempt",
        "policies": [],
    }
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=(MEDIA_BUCKET_STEP,)
    )

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "running", row.error
    assert fake_apis.created_bucket_count == 0
    assert set(fake_apis.buckets) == {bucket}
    assert set(fake_apis.domains) == {domain}
    # The same token, its value rolled: the crashed attempt's value was never stored anywhere.
    assert set(fake_apis.tokens) == {"tok-left-behind"}
    rolled = fake_apis.tokens["tok-left-behind"]["value"]
    assert rolled != "token-value-SECRET-from-the-crashed-attempt"
    assert _step_resources(row, "media") == {
        "bucket": bucket,
        "domain": domain,
        "token_id": "tok-left-behind",
        "account_id": ACCOUNT_ID,
    }
    atoms = await _decrypted_atoms(db_session, row.id)
    assert atoms["media-access-key-id"] == "tok-left-behind"
    assert atoms["media-secret-access-key"] == hashlib.sha256(rolled.encode()).hexdigest()
    # The lifecycle rule is applied on adoption too.
    assert fake_apis.buckets[bucket]["lifecycle"] is not None


async def test_a_media_step_that_fails_midway_leaves_nothing_behind(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    """A raising step is never recorded, so it undoes what it created before it raises."""
    installation = await _new_installation(db_session, seed_user)
    fake_apis.fail["POST /tokens"] = 403
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=(MEDIA_BUCKET_STEP,)
    )

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error is not None
    assert "create media token answered HTTP 403" in row.error
    assert fake_apis.buckets == {}
    assert fake_apis.domains == {}
    assert fake_apis.tokens == {}
    assert set(await _decrypted_atoms(db_session, row.id)) == BASE_ATOMS


async def test_teardown_deletes_objects_domain_bucket_token_and_namespace(
    db_session, seed_user, operator_settings, fake_apis, media_objects
) -> None:
    installation = await _new_installation(db_session, seed_user)
    later = FailingLaterStep(fake_apis)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(MEDIA_BUCKET_STEP, TEMPORAL_NAMESPACE_STEP, later.step),
    )
    suffix = _suffix(installation)
    bucket = f"postiz-media-{suffix}"
    domain = f"media-{suffix}.{ZONE}"
    namespace = f"postiz-{suffix}"

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.next_attempt_at is None
    history = row.external_resources["torn_down"]
    assert [entry["name"] for entry in history] == ["temporal", "media"]
    assert all(entry["teardown"] == "done" for entry in history)

    # Newest step first; within media, the objects before the bucket that holds them.
    assert _teardown_sequence(fake_apis.calls[later.calls_before_teardown :]) == [
        "delete namespace",
        "purge objects",
        "detach domain",
        "delete bucket",
        "revoke token",
    ]
    assert media_objects.deleted_prefixes == [(bucket, "")]
    assert media_objects.opened == [
        {
            "bucket": bucket,
            "region": "auto",
            "endpoint_url": R2_S3_ENDPOINT,
            "access_key_id": R2_ADMIN_ACCESS_KEY_ID,
            "secret_access_key": R2_ADMIN_SECRET_ACCESS_KEY,
            "force_path_style": True,
        }
    ]
    assert media_objects.closed == 1
    assert fake_apis.buckets == {}
    assert fake_apis.domains == {}
    assert fake_apis.tokens == {}
    assert fake_apis.namespaces[namespace].state == "NAMESPACE_STATE_DELETED"
    # The revoked credentials are no longer stored.
    assert set(await _decrypted_atoms(db_session, row.id)) == BASE_ATOMS

    # Tearing down what is already gone is not an error: every answer is a 404.
    context = _step_context(db_session, row)
    await companion_media_store.teardown_media_bucket(
        context,
        {"bucket": bucket, "domain": domain, "token_id": "tok-gone", "account_id": ACCOUNT_ID},
    )
    del fake_apis.namespaces[namespace]
    await companion_temporal.teardown_temporal_namespace(
        context, {"namespace": namespace, "address": TEMPORAL_FRONTEND_ADDRESS, "tls": True}
    )


MEDIA_SETTINGS = (
    ("cloudflare_account_id", "CLOUDFLARE_ACCOUNT_ID"),
    ("cloudflare_r2_api_token", "CLOUDFLARE_R2_API_TOKEN"),
    ("cloudflare_r2_s3_endpoint", "CLOUDFLARE_R2_S3_ENDPOINT"),
    ("cloudflare_r2_admin_access_key_id", "CLOUDFLARE_R2_ADMIN_ACCESS_KEY_ID"),
    ("cloudflare_r2_admin_secret_access_key", "CLOUDFLARE_R2_ADMIN_SECRET_ACCESS_KEY"),
    ("companion_edge_zone_id", "COMPANION_EDGE_ZONE_ID"),
    ("companion_edge_zone_name", "COMPANION_EDGE_ZONE_NAME"),
)
# The teardown works from its recorded handles, so it needs no zone to find the domain.
TEARDOWN_UNNEEDED = {"companion_edge_zone_id", "companion_edge_zone_name"}


@pytest.mark.parametrize(("setting", "env_name"), MEDIA_SETTINGS)
async def test_missing_settings_fail_the_step_before_any_call(
    db_session,
    seed_user,
    operator_settings,
    fake_apis,
    media_objects,
    monkeypatch,
    setting: str,
    env_name: str,
) -> None:
    installation = await _new_installation(db_session, seed_user)
    monkeypatch.setattr(settings, setting, "")
    context = _step_context(db_session, installation)

    with pytest.raises(CompanionStepError) as provision_refusal:
        await companion_media_store.provision_media_bucket(context)
    assert provision_refusal.value.code == "media_settings_missing"
    assert env_name in str(provision_refusal.value)

    if setting not in TEARDOWN_UNNEEDED:
        with pytest.raises(CompanionStepError) as teardown_refusal:
            await companion_media_store.teardown_media_bucket(
                context,
                {"bucket": "b", "domain": "d", "token_id": "t", "account_id": ACCOUNT_ID},
            )
        assert teardown_refusal.value.code == "media_settings_missing"
        assert env_name in str(teardown_refusal.value)

    # Refused before any external call: nothing reached Cloudflare or the object store.
    assert fake_apis.calls == []
    assert media_objects.opened == []


OPERATOR_SETTINGS = {
    "cloudflare_account_id": "",
    "cloudflare_r2_api_token": "",
    "cloudflare_r2_s3_endpoint": "",
    "cloudflare_r2_admin_access_key_id": "",
    "cloudflare_r2_admin_secret_access_key": "",
    "companion_edge_zone_id": "",
    "companion_temporal_frontend_address": "",
    "companion_temporal_http_url": "",
    "companion_temporal_tls": True,
    "companion_temporal_jwt_private_key": "",
    "companion_temporal_jwt_key_id": "",
    "companion_temporal_namespace_retention_days": 7,
}


def test_operator_settings_default_empty_and_are_listed_empty_in_env_example() -> None:
    """No operator value is committed (OSS boundary): empty defaults, empty example lines."""
    fields = type(settings).model_fields
    assert {name: fields[name].default for name in OPERATOR_SETTINGS} == OPERATOR_SETTINGS
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text().splitlines()
    for name, default in OPERATOR_SETTINGS.items():
        expected = f"{name.upper()}=" if default == "" else f"{name.upper()}={str(default).lower()}"
        assert expected in example, f"{expected!r} is not a line of backend/.env.example"
    with pytest.raises(ValidationError):
        type(settings)(companion_temporal_namespace_retention_days=0)
    with pytest.raises(ValidationError):
        type(settings)(companion_temporal_namespace_retention_days=91)


async def test_no_secret_value_is_logged(
    db_session,
    seed_user,
    operator_settings,
    fake_apis,
    media_objects,
    signing_key,
    caplog,
    monkeypatch,
) -> None:
    """A full provision, a failure and its teardown log no token, derived secret or JWT."""
    caplog.set_level(logging.DEBUG)
    installation = await _new_installation(db_session, seed_user)
    later = FailingLaterStep(fake_apis)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(MEDIA_BUCKET_STEP, TEMPORAL_NAMESPACE_STEP, later.step),
    )
    # Every atom value either step stores, captured before it is encrypted.
    stored: dict[str, str] = {}
    for module in (companion_media_store, companion_temporal):
        original = module.store_installation_atoms

        async def _capturing(context, values, _original=original):
            stored.update(values)
            await _original(context, values)

        monkeypatch.setattr(module, "store_installation_atoms", _capturing)

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert set(stored) == STEP_ATOMS
    private_key_body = "".join(signing_key[0].strip().splitlines()[1:-1])
    secrets = {
        CLOUDFLARE_ADMIN_TOKEN,
        R2_ADMIN_SECRET_ACCESS_KEY,
        private_key_body[:64],
        *fake_apis.issued_token_values,
        *(hashlib.sha256(value.encode()).hexdigest() for value in fake_apis.issued_token_values),
        *(bearer for bearer in fake_apis.bearers_seen if bearer),
        *stored.values(),
    }
    assert all(len(secret) >= 16 for secret in secrets)
    logged = [caplog.text, *(record.getMessage() for record in caplog.records)]
    for secret in secrets:
        assert all(secret not in text for text in logged), "a secret value reached the log"
        assert secret not in (row.error or "")
