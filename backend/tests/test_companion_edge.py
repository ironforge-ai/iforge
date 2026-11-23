"""The companion's own hostname: an edge-terminated tunnel, routed only after bootstrap (06-14).

D-24: TLS terminates at the Cloudflare edge through one remotely-managed tunnel per installation,
so the companion never holds a certificate and no inbound port opens. D-25: the hostname sits one
label deep in a dedicated companion zone, which shares no origin-TLS path with iforge's own
origins. Pitfall 3: Postiz lets the FIRST registrant own the instance even with registration
disabled, so the tunnel is created unrouted while the install job provisions, and its ingress rule
and DNS record are published only by a completion step -- after the runtime's data-asserted ready
report. Nothing here reaches a live API: ``FakeEdgeApi`` stands in for Cloudflare's tunnel and DNS
API, and for the edge answering on the installation's hostname, behind one mock transport.
"""

from __future__ import annotations

import json
import logging
import ssl
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext
from app.core.config import settings
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.services.companion_install import create_installation
from app.services.companion_lifecycle_worker import (
    COMPLETION_STEPS,
    PROVISIONING_STEPS,
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
)
from app.services.vault_crypto import decrypt
from tests.companion_billing_stub import billing_quote
from tests.test_companion_install_job import (
    _hosted_agent,
    _post_report,
    _ready_report,
    _reload,
    _runtime_key,
    _sessionmaker,
)

ZONE = "companions.example"
ACCOUNT_ID = "acct0edge0123456789abcdef"
ZONE_ID = "zone0edge0123456789abcdef"
TUNNEL_API_TOKEN = "cf-tunnel-api-token-SECRET-0123456789"
CLOUDFLARE_HOST = "api.cloudflare.com"
TUNNEL_PREFIX = f"/client/v4/accounts/{ACCOUNT_ID}/cfd_tunnel"
DNS_PREFIX = f"/client/v4/zones/{ZONE_ID}/dns_records"
IS_CONNECTED_PATH = "/api/public/v1/is-connected"
PUBLISH_EVENTS = ("put configuration", "create dns")


# ---------------------------------------------------------------------------
# The fake edge: Cloudflare's tunnel and DNS API, and the hostname as the internet sees it.
# ---------------------------------------------------------------------------


def _unanswered(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"nothing may probe {request.url} in this test")


@dataclass
class FakeEdgeApi:
    """Cloudflare's tunnel and DNS API as state, plus the edge answering on a companion hostname."""

    tunnels: dict[str, dict[str, Any]] = field(default_factory=dict)
    dns_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Named events, in order; the ordering assertions read these.
    events: list[str] = field(default_factory=list)
    # How the installation's hostname answers a probe through the edge.
    probe: Callable[[httpx.Request], httpx.Response] = _unanswered
    probe_requests: list[httpx.Request] = field(default_factory=list)
    issued_tunnel_tokens: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        if url.hostname != CLOUDFLARE_HOST:
            self.events.append("probe")
            self.probe_requests.append(request)
            return self.probe(request)
        assert request.headers["authorization"] == f"Bearer {TUNNEL_API_TOKEN}"
        if url.path.startswith(TUNNEL_PREFIX):
            return self._tunnel(request, url.path[len(TUNNEL_PREFIX) :])
        if url.path.startswith(DNS_PREFIX):
            return self._dns(request, url.path[len(DNS_PREFIX) :])
        raise AssertionError(f"unexpected Cloudflare call {request.method} {url.path}")

    def _tunnel(self, request: httpx.Request, rest: str) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content else None
        parts = [part for part in rest.split("/") if part]
        if not parts and method == "GET":
            self.events.append("list tunnels")
            assert request.url.params.get("is_deleted") == "false"
            name = request.url.params.get("name")
            listed = [
                {"id": tunnel["id"], "name": tunnel["name"]}
                for tunnel in self.tunnels.values()
                if not tunnel["deleted"] and tunnel["name"] == name
            ]
            return _cf_ok(listed)
        if not parts and method == "POST":
            self.events.append("create tunnel")
            # Remotely managed, and created with no ingress at all.
            assert body["config_src"] == "cloudflare", body
            assert set(body) == {"name", "config_src"}, body
            tunnel_id = str(uuid.uuid4())
            token = f"tunnel-token-SECRET-{uuid.uuid4().hex}"
            self.issued_tunnel_tokens.append(token)
            self.tunnels[tunnel_id] = {
                "id": tunnel_id,
                "name": body["name"],
                "token": token,
                "config": None,
                "deleted": False,
                "connections_cleaned": False,
            }
            return _cf_ok({"id": tunnel_id, "name": body["name"], "token": token})
        tunnel = self.tunnels.get(parts[0])
        if tunnel is None or tunnel["deleted"]:
            return _cf_error(404, 1003, "Tunnel not found.")
        if parts[1:] == ["token"] and method == "GET":
            self.events.append("read tunnel token")
            return _cf_ok(tunnel["token"])
        if parts[1:] == ["configurations"] and method == "PUT":
            self.events.append("put configuration")
            tunnel["config"] = body["config"]
            return _cf_ok({"tunnel_id": tunnel["id"], "config": body["config"]})
        if parts[1:] == ["connections"] and method == "DELETE":
            self.events.append("delete connections")
            tunnel["connections_cleaned"] = True
            return _cf_ok(None)
        if parts[1:] == [] and method == "DELETE":
            self.events.append("delete tunnel")
            tunnel["deleted"] = True
            return _cf_ok({"id": tunnel["id"]})
        raise AssertionError(f"unexpected tunnel call {method} {rest}")

    def _dns(self, request: httpx.Request, rest: str) -> httpx.Response:
        method = request.method
        body = json.loads(request.content) if request.content else None
        parts = [part for part in rest.split("/") if part]
        if not parts and method == "GET":
            self.events.append("list dns")
            kind = request.url.params.get("type")
            name = request.url.params.get("name.exact")
            listed = [
                record
                for record in self.dns_records.values()
                if record["type"] == kind and record["name"] == name
            ]
            return _cf_ok(listed)
        if not parts and method == "POST":
            self.events.append("create dns")
            record_id = uuid.uuid4().hex
            self.dns_records[record_id] = {"id": record_id, **body}
            return _cf_ok(self.dns_records[record_id])
        if len(parts) == 1 and method == "DELETE":
            if parts[0] not in self.dns_records:
                return _cf_error(404, 81044, "Record does not exist.")
            self.events.append("delete dns")
            del self.dns_records[parts[0]]
            return _cf_ok({"id": parts[0]})
        raise AssertionError(f"unexpected DNS call {method} {rest}")

    # -- reading the state ----------------------------------------------------------------------

    def only_tunnel(self) -> dict[str, Any]:
        assert len(self.tunnels) == 1, self.tunnels
        return next(iter(self.tunnels.values()))

    def published(self) -> list[str]:
        return [event for event in self.events if event in PUBLISH_EVENTS]


def _cf_ok(result: Any) -> httpx.Response:
    return httpx.Response(
        200, json={"success": True, "errors": [], "messages": [], "result": result}
    )


def _cf_error(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"success": False, "errors": [{"code": code, "message": message}], "messages": []},
    )


# ---------------------------------------------------------------------------
# Wiring and helpers.
# ---------------------------------------------------------------------------


def _wire_edge(monkeypatch: pytest.MonkeyPatch, api: FakeEdgeApi | None = None) -> FakeEdgeApi:
    """Every operator setting the edge steps read, set to a fake, and the fake edge wired in."""
    from app.services import companion_edge

    api = api or FakeEdgeApi()
    for name, value in {
        "companion_edge_zone_name": ZONE,
        "companion_edge_zone_id": ZONE_ID,
        "cloudflare_account_id": ACCOUNT_ID,
        "cloudflare_tunnel_api_token": TUNNEL_API_TOKEN,
    }.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(companion_edge, "_transport_override", api.transport())
    return api


def _pipeline_step(steps: tuple[ProvisioningStep, ...], name: str) -> ProvisioningStep:
    matches = [step for step in steps if step.name == name]
    assert len(matches) == 1, [step.name for step in steps]
    return matches[0]


@dataclass
class _Install:
    installation: CompanionInstallation
    agent_id: uuid.UUID
    deployment_id: str


async def _new_installation(db_session: AsyncSession, seed_user) -> _Install:
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    return _Install(installation, agent_id, deployment_id)


async def _report_ready(db_session: AsyncSession, seed_user, install: _Install) -> None:
    runtime_key = await _runtime_key(db_session, seed_user, install.agent_id, install.deployment_id)
    reported = await _post_report(
        db_session,
        runtime_key,
        install.installation.id,
        _ready_report(applied_digest=install.installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text


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


# ---------------------------------------------------------------------------
# Task 1 (tracer): the tunnel exists from provisioning; the hostname goes live only after the
# runtime reported the bootstrap done.
# ---------------------------------------------------------------------------


async def test_ingress_is_never_published_before_the_ready_report(
    db_session, seed_user, monkeypatch
) -> None:
    # The tunnel is a provisioning step after the Temporal namespace; publication is the FIRST
    # completion step, and completion steps run only after a data-asserted ready report.
    provisioning = [step.name for step in PROVISIONING_STEPS]
    assert "edge" in provisioning, provisioning
    assert provisioning.index("edge") > provisioning.index("temporal"), provisioning
    assert [step.name for step in COMPLETION_STEPS][:1] == ["edge-publish"]

    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    installation_id = install.installation.id
    # One label directly beneath the dedicated companion zone (D-24, research F9).
    assert install.installation.hostname == f"{install.installation.hostname_label}.{ZONE}"

    def worker() -> CompanionLifecycleWorker:
        return CompanionLifecycleWorker(
            _sessionmaker(db_session),
            provisioning_steps=(_pipeline_step(PROVISIONING_STEPS, "edge"),),
            completion_steps=(COMPLETION_STEPS[0],),
        )

    assert await worker().run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "running", row.error

    # Provisioning created the tunnel, remotely managed and named for the installation, and
    # stored its token -- and published nothing: no ingress, no DNS record.
    tunnel = api.only_tunnel()
    assert tunnel["name"] == f"iforge-postiz-{installation_id}"
    assert tunnel["config"] is None
    assert api.dns_records == {}
    assert api.published() == []
    assert (await _decrypted_atoms(db_session, installation_id))["tunnel-token"] == tunnel["token"]
    assert _step_resources(row, "edge") == {"tunnel_id": tunnel["id"], "account_id": ACCOUNT_ID}

    # No report yet: two more passes, then a restarted worker -- still nothing is published.
    for _ in range(2):
        assert await worker().run_once() is None
    restarted = worker()
    assert await restarted.run_once() is None
    assert api.published() == []
    assert tunnel["config"] is None
    assert (await _reload(db_session, installation_id)).status == "running"

    await _report_ready(db_session, seed_user, install)
    api.events.append("ready report recorded")

    assert await restarted.run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "complete", row.error

    # The ingress, then the proxied CNAME, both strictly after the report.
    assert api.published() == ["put configuration", "create dns"]
    report_at = api.events.index("ready report recorded")
    assert min(api.events.index(event) for event in PUBLISH_EVENTS) > report_at
    assert tunnel["config"] == {
        "ingress": [
            {"hostname": row.hostname, "service": "http://iforge-postiz:5000"},
            {"service": "http_status:404"},
        ]
    }
    (record,) = api.dns_records.values()
    assert record == {
        "id": record["id"],
        "type": "CNAME",
        "name": row.hostname,
        "content": f"{tunnel['id']}.cfargotunnel.com",
        "proxied": True,
        "ttl": 1,
    }
    assert _step_resources(row, "edge-publish") == {
        "dns_record_id": record["id"],
        "zone_id": ZONE_ID,
        "hostname": row.hostname,
    }


# ---------------------------------------------------------------------------
# Teardown: the DNS record goes before the tunnel, and nothing secret is logged.
# ---------------------------------------------------------------------------


async def test_teardown_deletes_the_dns_record_before_the_tunnel(
    db_session, seed_user, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    installation_id = install.installation.id

    async def refuse(context: LifecycleStepContext) -> dict:
        raise RuntimeError("a later completion step refused")

    async def never(context: LifecycleStepContext, resources: dict) -> None:
        raise AssertionError("a step that never completed must not be torn down")

    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(_pipeline_step(PROVISIONING_STEPS, "edge"),),
        completion_steps=(COMPLETION_STEPS[0], ProvisioningStep("later", refuse, never)),
    )
    assert await worker.run_once() == installation_id
    await _report_ready(db_session, seed_user, install)
    before_teardown = len(api.events)

    # The completion fails after publication: the install fails and tears down in this pass.
    assert await worker.run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "failed"
    assert row.next_attempt_at is None

    # This pass published, then failed and tore down: the deletes, in order.
    assert api.published() == ["put configuration", "create dns"]
    teardown = api.events[before_teardown:]
    assert [event for event in teardown if event.startswith("delete")] == [
        "delete dns",
        "delete connections",
        "delete tunnel",
    ]
    assert api.dns_records == {}
    assert api.only_tunnel()["deleted"] is True
    assert "tunnel-token" not in await _decrypted_atoms(db_session, installation_id)
    assert [entry["name"] for entry in row.external_resources["torn_down"]] == [
        "edge-publish",
        "edge",
    ]

    # No token -- the tunnel's or the operator's -- ever reached the log or the stored error.
    secrets = [TUNNEL_API_TOKEN, *api.issued_tunnel_tokens]
    for secret in secrets:
        assert secret not in caplog.text
        assert secret not in (row.error or "")


async def test_teardown_edge_is_404_tolerant_and_orders_dns_first(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_edge import (
        provision_edge_tunnel,
        publish_edge_ingress,
        teardown_edge,
    )

    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    context = _step_context(db_session, install.installation)
    tunnel = await provision_edge_tunnel(context)
    await _record_step(db_session, install.installation.id, "edge", tunnel)
    published = await publish_edge_ingress(context)

    before = len(api.events)
    await teardown_edge(context, tunnel, published)
    assert api.events[before:] == ["delete dns", "delete connections", "delete tunnel"]

    # Already gone: a second teardown tolerates every 404 and changes nothing.
    await teardown_edge(context, tunnel, published)
    assert api.dns_records == {}
    assert api.only_tunnel()["deleted"] is True


def _step_context(db_session: AsyncSession, installation: CompanionInstallation):
    return LifecycleStepContext(
        installation_id=installation.id,
        user_id=installation.user_id,
        environment_id=installation.environment_id,
        companion_key=installation.companion_key,
        operation=installation.operation,
        hostname=installation.hostname,
        sessionmaker=_sessionmaker(db_session),
    )


async def _record_step(
    db_session: AsyncSession, installation_id: uuid.UUID, name: str, resources: dict
) -> None:
    """Record a step the way the worker does, for driving the steps directly."""
    row = await _reload(db_session, installation_id)
    steps = list((row.external_resources or {}).get("steps") or [])
    steps.append({"name": name, "operation": row.operation, "resources": resources})
    row.external_resources = {**(row.external_resources or {}), "steps": steps}
    await db_session.commit()


# ---------------------------------------------------------------------------
# Reachability: measured through the edge, never assumed.
# ---------------------------------------------------------------------------


def _tls_failure(request: httpx.Request) -> httpx.Response:
    try:
        raise ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate")
    except ssl.SSLError as exc:
        raise httpx.ConnectError("TLS handshake failed", request=request) from exc


def _timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("answer", "state"),
    [
        (lambda request: httpx.Response(401, json={"msg": "Invalid API key"}), "reachable"),
        (_tls_failure, "tls_pending"),
        (lambda request: httpx.Response(502, text="Bad gateway"), "not_answering"),
        (lambda request: httpx.Response(530, text="origin unreachable"), "not_answering"),
        (lambda request: httpx.Response(200, json={"connected": True}), "not_answering"),
        (_timeout, "not_answering"),
    ],
    ids=["401", "tls", "502", "530", "200", "timeout"],
)
async def test_probe_classifies_what_the_edge_answers(
    db_session, seed_user, monkeypatch, answer, state
) -> None:
    from app.services.companion_edge import probe_edge_reachability

    api = _wire_edge(monkeypatch, FakeEdgeApi(probe=answer))
    install = await _new_installation(db_session, seed_user)

    result = await probe_edge_reachability(install.installation)

    assert result["state"] == state
    assert isinstance(result["checked_at"], str) and result["checked_at"]
    (request,) = api.probe_requests
    # Through the edge on the installation's own hostname, with no credential at all.
    assert str(request.url) == f"https://{install.installation.hostname}{IS_CONNECTED_PATH}"
    assert request.method == "GET"
    assert "authorization" not in request.headers
    assert request.extensions["timeout"]["connect"] == 10.0
    assert request.extensions["timeout"]["read"] == 10.0


async def test_worker_probes_a_complete_installation_at_most_every_five_minutes(
    db_session, seed_user, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    api = _wire_edge(monkeypatch, FakeEdgeApi(probe=lambda request: httpx.Response(401)))
    install = await _new_installation(db_session, seed_user)
    installation_id = install.installation.id
    started = datetime.now(UTC)
    clock = [started]
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), clock=lambda: clock[0], provisioning_steps=()
    )

    # Not complete yet: nothing to probe.
    assert await worker.probe_reachability_once() is None
    row = await _reload(db_session, installation_id)
    row.status = "complete"
    await db_session.commit()

    assert await worker.probe_reachability_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.external_resources["edge"]["reachability"] == {
        "state": "reachable",
        "checked_at": started.isoformat(),
    }

    # Within five minutes of the last check nothing is due; after them it is probed again.
    clock[0] = started + timedelta(seconds=299)
    assert await worker.probe_reachability_once() is None
    api.probe = lambda request: httpx.Response(502)
    clock[0] = started + timedelta(seconds=301)
    assert await worker.probe_reachability_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.external_resources["edge"]["reachability"] == {
        "state": "not_answering",
        "checked_at": clock[0].isoformat(),
    }
    assert len(api.probe_requests) == 2


# ---------------------------------------------------------------------------
# Refusals and retries.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unset",
    [
        "cloudflare_tunnel_api_token",
        "cloudflare_account_id",
        "companion_edge_zone_id",
        "companion_edge_zone_name",
    ],
)
async def test_missing_settings_fail_the_edge_step_before_any_call(
    db_session, seed_user, monkeypatch, unset
) -> None:
    from app.services.companion_edge import provision_edge_tunnel
    from app.services.companion_lifecycle_steps import CompanionStepError

    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    monkeypatch.setattr(settings, unset, "")

    with pytest.raises(CompanionStepError) as refused:
        await provision_edge_tunnel(_step_context(db_session, install.installation))

    assert refused.value.code == "edge_settings_missing"
    assert unset.upper() in str(refused.value)
    assert api.events == []


async def test_a_retried_tunnel_step_adopts_the_tunnel_it_created(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_edge import provision_edge_tunnel

    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    context = _step_context(db_session, install.installation)

    first = await provision_edge_tunnel(context)
    second = await provision_edge_tunnel(context)

    assert second == first
    tunnel = api.only_tunnel()
    assert api.events.count("create tunnel") == 1
    assert api.events[-2:] == ["list tunnels", "read tunnel token"]
    atoms = await _decrypted_atoms(db_session, install.installation.id)
    assert atoms["tunnel-token"] == tunnel["token"]


async def test_publish_refuses_a_hostname_that_points_elsewhere(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_edge import provision_edge_tunnel, publish_edge_ingress
    from app.services.companion_lifecycle_steps import CompanionStepError

    api = _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    context = _step_context(db_session, install.installation)
    tunnel = await provision_edge_tunnel(context)
    await _record_step(db_session, install.installation.id, "edge", tunnel)
    api.dns_records["foreign"] = {
        "id": "foreign",
        "type": "CNAME",
        "name": install.installation.hostname,
        "content": "someone-else.cfargotunnel.com",
        "proxied": True,
    }

    with pytest.raises(CompanionStepError) as refused:
        await publish_edge_ingress(context)

    assert refused.value.code == "edge_hostname_taken"
    assert "create dns" not in api.events
    assert api.dns_records["foreign"]["content"] == "someone-else.cfargotunnel.com"


# ---------------------------------------------------------------------------
# Task 2: the runtime is told to run the tunnel once it exists, with its token as a credential.
# ---------------------------------------------------------------------------


def _tunnel_ref(installation_id: uuid.UUID) -> str:
    # Spelled out rather than imported, so a wrong ref shape cannot also be the expectation.
    return f"secret://companions/postiz/{installation_id}/tunnel-env"


async def test_the_rendered_entry_carries_the_tunnel_once_the_tunnel_exists(
    db_session, seed_user, monkeypatch
) -> None:
    from app.core.companion_catalog import catalog_entry
    from app.schemas.runtime import HostedPostizCompanion
    from tests.test_companion_projection import _agent_with_runtime, _installation, _render

    api = _wire_edge(monkeypatch)
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="queued")
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(_pipeline_step(PROVISIONING_STEPS, "edge"),),
        completion_steps=(),
    )
    assert await worker.run_once() == installation_id

    source = await _render(db_session, environment_id)

    entry = source.manifest["companions"]["postiz"]
    HostedPostizCompanion.model_validate(entry)
    cloudflared = catalog_entry("postiz").cloudflared
    # The connector is pinned by the catalog's digest (plan 06-09), never a tag.
    assert entry["dependencies"].get("cloudflared") == {
        "image": cloudflared.image,
        "sha256": cloudflared.sha256,
    }
    ref = _tunnel_ref(installation_id)
    assert entry["credentials"].get("tunnelTokenSecretRef") == ref
    token = api.only_tunnel()["token"]
    # One line, the only form cloudflared reads it in; and it reaches the runtime nowhere else.
    assert source.secret_values[ref] == f"TUNNEL_TOKEN={token}\n"
    assert token not in json.dumps(source.manifest)
    assert [name for name, value in source.secret_values.items() if token in value] == [ref]


async def test_the_entry_has_no_tunnel_before_the_tunnel_step_ran(
    db_session, seed_user, monkeypatch
) -> None:
    from tests.test_companion_projection import _agent_with_runtime, _installation, _render

    _wire_edge(monkeypatch)
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)

    source = await _render(db_session, environment_id)

    entry = source.manifest["companions"]["postiz"]
    assert "cloudflared" not in entry["dependencies"]
    assert "tunnelTokenSecretRef" not in entry["credentials"]
    assert _tunnel_ref(installation_id) not in source.secret_values


async def test_the_tunnel_token_is_refused_if_it_would_add_a_line(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_postiz import compose_postiz_secret_values
    from app.services.runtime_source import RuntimeSourceError

    _wire_edge(monkeypatch)
    install = await _new_installation(db_session, seed_user)
    await _record_step(
        db_session,
        install.installation.id,
        "edge",
        {"tunnel_id": str(uuid.uuid4()), "account_id": ACCOUNT_ID},
    )
    row = await _reload(db_session, install.installation.id)
    atoms = await _decrypted_atoms(db_session, row.id)

    with pytest.raises(RuntimeSourceError) as refused:
        compose_postiz_secret_values(row, {**atoms, "tunnel-token": "abc\nEVIL=1"})
    assert _tunnel_ref(row.id) in str(refused.value)
    assert "EVIL" not in str(refused.value)

    with pytest.raises(RuntimeSourceError) as missing:
        compose_postiz_secret_values(row, atoms)
    assert _tunnel_ref(row.id) in str(missing.value)
