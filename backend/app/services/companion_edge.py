"""A companion's own hostname: one edge-terminated tunnel per installation (D-24, D-25, APP-02).

**TLS at the edge (D-24).** Each installation gets one remotely-managed Cloudflare Tunnel
(``config_src = "cloudflare"``). The runtime runs ``cloudflared`` inside the installation's own
podman network with the tunnel's token; the connector dials OUT to the edge, so no inbound port
opens on the VM and the companion never holds a certificate. The edge terminates TLS with the
companion zone's Universal SSL certificate, which covers the apex and first-level names only --
hence exactly one generated label under the zone (research Pattern 7).

**A separate registrable zone (D-24, research F9, D-25).** The hostname lives in a dedicated
companion zone, never under iforge's own origins: Postiz sets its ``auth`` cookie on the
registrable domain, so a shared domain would hand that cookie to iforge. A tunnel has no origin
certificate and no zone SSL-mode dependency, so the companion shares nothing with the 999.2
origin-TLS failure.

**Routed only after bootstrap (research Pitfall 3).** Postiz lets the FIRST registrant own the
instance even with ``DISABLE_REGISTRATION=true``. So the two halves are two pipeline steps:

* ``EDGE_TUNNEL_STEP`` (provisioning): create or adopt the tunnel, store its token as the
  ``tunnel-token`` atom. No ingress, no DNS record: the connector can run, but no hostname routes
  to it.
* ``EDGE_PUBLISH_STEP`` (the FIRST completion step): publish the ingress rule
  (``<hostname>`` -> ``http://iforge-postiz:5000``, then a 404 catch-all) and the proxied CNAME
  ``<hostname>`` -> ``<tunnel id>.cfargotunnel.com``. The worker runs completion steps only once
  the runtime's data-asserted ready report is recorded, and the platform is by then the
  registrant.

Teardown deletes the DNS record first, then the tunnel's connections and the tunnel, and drops the
token atom; every delete is 404-tolerant. A step that raises midway undoes what it created (the
worker never tears down an unrecorded step).

**Reachability is measured (APP-02).** ``probe_edge_reachability`` asks the edge for
``https://<hostname>/api/public/v1/is-connected`` with no credential: Postiz's 401 arriving over
valid TLS is ``reachable``, a TLS or certificate failure is ``tls_pending`` (the edge certificate
is not issued yet) and anything else is ``not_answering``. The lifecycle worker probes each
complete installation at most every five minutes and records the result under
``external_resources["edge"]["reachability"]``; plan 06-34 carries it on the installation response.

Cloudflare API endpoints (re-verified at execution against Cloudflare's API reference and the
"Create a tunnel (API)" guide), all under ``https://api.cloudflare.com/client/v4``:

* ``GET /accounts/{account_id}/cfd_tunnel?name=<name>&is_deleted=false`` (adopt on retry)
* ``POST /accounts/{account_id}/cfd_tunnel`` ``{name, config_src: "cloudflare"}`` -> ``{id, token}``
* ``GET /accounts/{account_id}/cfd_tunnel/{tunnel_id}/token`` (an adopted tunnel's token)
* ``PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations`` ``{config: {ingress}}``
* ``GET /zones/{zone_id}/dns_records?type=CNAME&name.exact=<hostname>`` (adopt on retry)
* ``POST /zones/{zone_id}/dns_records`` ``{type: CNAME, name, content, proxied: true, ttl: 1}``
* ``DELETE /zones/{zone_id}/dns_records/{dns_record_id}``
* ``DELETE /accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections``, then
  ``DELETE /accounts/{account_id}/cfd_tunnel/{tunnel_id}``

The bearer is ``CLOUDFLARE_TUNNEL_API_TOKEN`` (Cloudflare Tunnel edit on the account, DNS edit on
the companion zone only). Neither it nor a tunnel token is ever logged or put into an error.
"""

from __future__ import annotations

import logging
import re
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.models.companion_installation import CompanionInstallation
from app.services.companion_lifecycle_steps import (
    CompanionStepError,
    JsonObject,
    LifecycleStepContext,
    ProvisioningStep,
    delete_installation_atoms,
    store_installation_atoms,
)
from app.services.companion_postiz import (
    POSTIZ_EDGE_PUBLISH_STEP,
    POSTIZ_EDGE_STEP,
    TUNNEL_TOKEN_ATOM,
    postiz_step_handles,
)

log = logging.getLogger(__name__)

CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4"
# The app container's in-network name and port (plan 06-08's units): cloudflared joins the same
# per-installation podman network, so the tunnel reaches Postiz without any published port.
TUNNEL_ORIGIN_SERVICE = "http://iforge-postiz:5000"
TUNNEL_CATCH_ALL_SERVICE = "http_status:404"
TUNNEL_CNAME_SUFFIX = "cfargotunnel.com"

REACHABILITY_PATH = "/api/public/v1/is-connected"
REACHABILITY_TIMEOUT_SECONDS = 10.0
REACHABILITY_PROBE_INTERVAL_SECONDS = 5 * 60
REACHABLE = "reachable"
TLS_PENDING = "tls_pending"
NOT_ANSWERING = "not_answering"

_TIMEOUT_SECONDS = 20.0
_NOT_FOUND = frozenset({404})
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

# Test seam (the tenant_purge pattern): tests point this at an httpx.MockTransport standing in for
# the Cloudflare API and the edge. None means the default network transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def edge_tunnel_name(context: LifecycleStepContext) -> str:
    return f"iforge-{context.companion_key}-{context.installation_id}"


def edge_ingress_config(hostname: str) -> dict[str, list[dict[str, str]]]:
    """The tunnel's whole ingress: this hostname to the app, and 404 for anything else."""
    return {
        "ingress": [
            {"hostname": hostname, "service": TUNNEL_ORIGIN_SERVICE},
            {"service": TUNNEL_CATCH_ALL_SERVICE},
        ]
    }


def edge_cname_target(tunnel_id: str) -> str:
    return f"{tunnel_id}.{TUNNEL_CNAME_SUFFIX}"


# ---------------------------------------------------------------------------
# Settings and the hostname: checked before the first external call.
# ---------------------------------------------------------------------------


def _require_settings(*, for_teardown: bool) -> None:
    """Refuse by name, before any call, when an operator setting the step needs is unset."""
    required = {"CLOUDFLARE_TUNNEL_API_TOKEN": settings.cloudflare_tunnel_api_token}
    if not for_teardown:
        # Teardown works from the recorded handles (account, tunnel, zone and record ids).
        required["CLOUDFLARE_ACCOUNT_ID"] = settings.cloudflare_account_id
        required["COMPANION_EDGE_ZONE_ID"] = settings.companion_edge_zone_id
        required["COMPANION_EDGE_ZONE_NAME"] = settings.companion_edge_zone_name
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise CompanionStepError(
            "edge_settings_missing", f"operator settings are unset: {', '.join(missing)}"
        )


def _one_label_under_zone(hostname: str) -> bool:
    """Whether ``hostname`` is exactly one DNS label directly beneath the companion zone.

    One label keeps it inside Universal SSL's coverage; the zone being the dedicated companion
    zone keeps Postiz's registrable-domain cookie away from iforge's origins (F9).
    """
    zone = settings.companion_edge_zone_name.strip().lower().rstrip(".")
    label, _, rest = hostname.partition(".")
    return bool(zone) and rest == zone and _DNS_LABEL.fullmatch(label) is not None


def _companion_hostname(context: LifecycleStepContext) -> str:
    """The installation's hostname, refused unless it is ONE label under the companion zone."""
    hostname = context.hostname
    if not _one_label_under_zone(hostname):
        raise CompanionStepError(
            "edge_hostname_invalid",
            "the installation's hostname is not one label under the companion zone",
        )
    return hostname


# ---------------------------------------------------------------------------
# The Cloudflare API.
# ---------------------------------------------------------------------------


class _Envelope(BaseModel):
    """Cloudflare's v4 response envelope; only these fields are read."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    result: JsonValue = None


class _Tunnel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str = ""
    token: str | None = None


class _DnsRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    content: str = ""


class _TunnelHandles(BaseModel):
    """What the tunnel step recorded; publication and teardown act on exactly these."""

    model_config = ConfigDict(extra="ignore")

    tunnel_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)


class _PublishHandles(BaseModel):
    """What the publish step recorded; its teardown deletes exactly this record."""

    model_config = ConfigDict(extra="ignore")

    dns_record_id: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _Answer:
    status: int
    result: JsonValue


class _CloudflareApi:
    """Cloudflare's v4 API, answering only envelopes it can validate."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        body: object | None = None,
        params: dict[str, str] | None = None,
        accept: frozenset[int] = frozenset(),
    ) -> _Answer:
        """A validated success envelope's result, or an ``accept``-ed status; a named error.

        The error names the operation and the HTTP status only: never the path (it carries the
        operator's account and zone ids), never a response body (a tunnel token travels in one).
        """
        try:
            response = await self._client.request(method, path, json=body, params=params)
        except httpx.HTTPError as exc:
            raise CompanionStepError(
                "cloudflare_unreachable", f"{operation} failed: {exc.__class__.__name__}"
            ) from None
        if response.status_code in accept:
            return _Answer(response.status_code, None)
        if not response.is_success:
            raise CompanionStepError(
                "cloudflare_refused", f"{operation} answered HTTP {response.status_code}"
            )
        try:
            envelope = _Envelope.model_validate_json(response.content)
        except ValidationError:
            envelope = None
        if envelope is None or not envelope.success:
            raise CompanionStepError(
                "cloudflare_invalid_response", f"{operation} answered an invalid envelope"
            )
        return _Answer(response.status_code, envelope.result)


def _cloudflare_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=CLOUDFLARE_API_BASE_URL,
        transport=_transport_override,
        timeout=_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {settings.cloudflare_tunnel_api_token}",
            "User-Agent": "iforge-backend/1.0",
        },
    )


def _invalid(operation: str) -> CompanionStepError:
    return CompanionStepError(
        "cloudflare_invalid_response", f"{operation} answered no usable result"
    )


def _listed(answer: _Answer, operation: str) -> list[JsonValue]:
    if not isinstance(answer.result, list):
        raise _invalid(operation)
    return answer.result


async def _tunnel_token(api: _CloudflareApi, account_id: str, tunnel_id: str) -> str:
    answer = await api.call(
        "GET",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
        operation="read tunnel token",
    )
    if not isinstance(answer.result, str) or not answer.result:
        raise _invalid("read tunnel token")
    return answer.result


async def _ensure_tunnel(api: _CloudflareApi, account_id: str, name: str) -> tuple[str, str]:
    """This installation's tunnel id and token: an earlier attempt's is adopted, else created."""
    listed = await api.call(
        "GET",
        f"/accounts/{account_id}/cfd_tunnel",
        operation="list tunnels",
        params={"name": name, "is_deleted": "false"},
    )
    try:
        existing = [
            tunnel
            for tunnel in (_Tunnel.model_validate(item) for item in _listed(listed, "list tunnels"))
            if tunnel.name == name
        ]
    except ValidationError:
        raise _invalid("list tunnels") from None
    if existing:
        tunnel_id = existing[0].id
        return tunnel_id, await _tunnel_token(api, account_id, tunnel_id)
    created = await api.call(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        operation="create tunnel",
        # Remotely managed, and deliberately created with NO ingress: nothing routes to it yet.
        body={"name": name, "config_src": "cloudflare"},
    )
    try:
        tunnel = _Tunnel.model_validate(created.result)
    except ValidationError:
        raise _invalid("create tunnel") from None
    token = tunnel.token or await _tunnel_token(api, account_id, tunnel.id)
    return tunnel.id, token


async def _ensure_cname(api: _CloudflareApi, zone_id: str, hostname: str, target: str) -> str:
    """The proxied CNAME's record id: an earlier attempt's is adopted, else created."""
    listed = await api.call(
        "GET",
        f"/zones/{zone_id}/dns_records",
        operation="list dns records",
        params={"type": "CNAME", "name.exact": hostname},
    )
    try:
        records = [_DnsRecord.model_validate(item) for item in _listed(listed, "list dns records")]
    except ValidationError:
        raise _invalid("list dns records") from None
    for record in records:
        if record.content == target:
            return record.id
    if records:
        raise CompanionStepError(
            "edge_hostname_taken", "the hostname already points somewhere other than its tunnel"
        )
    created = await api.call(
        "POST",
        f"/zones/{zone_id}/dns_records",
        operation="create dns record",
        body={"type": "CNAME", "name": hostname, "content": target, "proxied": True, "ttl": 1},
    )
    try:
        return _DnsRecord.model_validate(created.result).id
    except ValidationError:
        raise _invalid("create dns record") from None


async def _delete_tunnel(api: _CloudflareApi, account_id: str, tunnel_id: str) -> None:
    """Clean up the tunnel's connector connections, then delete it; already gone is done."""
    tunnel_path = f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}"
    await api.call(
        "DELETE",
        f"{tunnel_path}/connections",
        operation="clean up tunnel connections",
        accept=_NOT_FOUND,
    )
    await api.call("DELETE", tunnel_path, operation="delete tunnel", accept=_NOT_FOUND)


async def _delete_dns_record(api: _CloudflareApi, zone_id: str, record_id: str) -> None:
    await api.call(
        "DELETE",
        f"/zones/{zone_id}/dns_records/{record_id}",
        operation="delete dns record",
        accept=_NOT_FOUND,
    )


# ---------------------------------------------------------------------------
# The steps.
# ---------------------------------------------------------------------------


async def provision_edge_tunnel(context: LifecycleStepContext) -> JsonObject:
    """Create or adopt this installation's tunnel and store its token. Publishes NOTHING."""
    _require_settings(for_teardown=False)
    _companion_hostname(context)
    account_id = settings.cloudflare_account_id
    tunnel_id: str | None = None
    try:
        async with _cloudflare_client() as client:
            tunnel_id, token = await _ensure_tunnel(
                _CloudflareApi(client), account_id, edge_tunnel_name(context)
            )
        await store_installation_atoms(context, {TUNNEL_TOKEN_ATOM: token})
    except Exception:
        # Not recorded, so never torn down by the worker: undo it here, then fail as before.
        if tunnel_id is not None:
            await _undo_tunnel(context, _TunnelHandles(tunnel_id=tunnel_id, account_id=account_id))
        raise
    log.info(
        "companion edge tunnel provisioned installation_id=%s tunnel_id=%s",
        context.installation_id,
        tunnel_id,
    )
    return {"tunnel_id": tunnel_id, "account_id": account_id}


async def _undo_tunnel(context: LifecycleStepContext, handles: _TunnelHandles) -> None:
    """Best-effort compensation for a tunnel step that raised; never masks the original error."""
    try:
        await _teardown_tunnel(context, handles)
    except Exception as exc:  # noqa: BLE001 - the step's own failure is what gets reported.
        log.warning(
            "companion edge tunnel partial provision not undone installation_id=%s error=%s",
            context.installation_id,
            exc.__class__.__name__,
        )


async def _recorded_tunnel(context: LifecycleStepContext) -> _TunnelHandles:
    """The tunnel the provisioning step recorded for this installation."""
    async with context.sessionmaker() as db:
        installation = await db.scalar(
            select(CompanionInstallation).where(CompanionInstallation.id == context.installation_id)
        )
        handles = (
            postiz_step_handles(installation, POSTIZ_EDGE_STEP)
            if installation is not None
            else None
        )
        await db.rollback()
    if handles is None:
        raise CompanionStepError(
            "edge_tunnel_missing", "no edge tunnel was provisioned for this installation"
        )
    try:
        return _TunnelHandles.model_validate(handles)
    except ValidationError:
        raise CompanionStepError(
            "edge_handles_invalid", "the recorded edge tunnel handles are incomplete"
        ) from None


async def publish_edge_ingress(context: LifecycleStepContext) -> JsonObject:
    """Route the hostname: the tunnel's ingress, then the proxied CNAME to it.

    A COMPLETION step: the worker runs it only after the runtime's data-asserted ready report,
    so the platform is already Postiz's first registrant when the hostname goes live. No
    compensation is needed: once the CNAME exists nothing else in this step can fail, and an
    ingress rule without a DNS record routes nothing (the failed job's teardown deletes the
    tunnel with it).
    """
    _require_settings(for_teardown=False)
    hostname = _companion_hostname(context)
    tunnel = await _recorded_tunnel(context)
    zone_id = settings.companion_edge_zone_id
    async with _cloudflare_client() as client:
        api = _CloudflareApi(client)
        await api.call(
            "PUT",
            f"/accounts/{tunnel.account_id}/cfd_tunnel/{tunnel.tunnel_id}/configurations",
            operation="publish tunnel ingress",
            body={"config": edge_ingress_config(hostname)},
        )
        record_id = await _ensure_cname(api, zone_id, hostname, edge_cname_target(tunnel.tunnel_id))
    log.info(
        "companion edge published installation_id=%s hostname=%s",
        context.installation_id,
        hostname,
    )
    return {"dns_record_id": record_id, "zone_id": zone_id, "hostname": hostname}


async def _teardown_tunnel(context: LifecycleStepContext, handles: _TunnelHandles) -> None:
    async with _cloudflare_client() as client:
        await _delete_tunnel(_CloudflareApi(client), handles.account_id, handles.tunnel_id)
    await delete_installation_atoms(context, (TUNNEL_TOKEN_ATOM,))


def _validated[HandlesT: BaseModel](model: type[HandlesT], resources: JsonObject) -> HandlesT:
    try:
        return model.model_validate(resources)
    except ValidationError:
        raise CompanionStepError(
            "edge_handles_invalid", "the recorded edge handles are incomplete"
        ) from None


async def teardown_edge_tunnel(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Delete the tunnel (connections first) and drop its token atom; 404s are already done."""
    _require_settings(for_teardown=True)
    handles = _validated(_TunnelHandles, resources)
    await _teardown_tunnel(context, handles)
    log.info(
        "companion edge tunnel torn down installation_id=%s tunnel_id=%s",
        context.installation_id,
        handles.tunnel_id,
    )


async def teardown_edge_publication(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Delete the hostname's DNS record; a 404 means it is already gone."""
    _require_settings(for_teardown=True)
    handles = _validated(_PublishHandles, resources)
    async with _cloudflare_client() as client:
        await _delete_dns_record(_CloudflareApi(client), handles.zone_id, handles.dns_record_id)
    log.info("companion edge unpublished installation_id=%s", context.installation_id)


async def reissue_edge_tunnel_token(context: LifecycleStepContext) -> None:
    """Store the recorded tunnel's connector token again (operator recovery, plan 06-25).

    The token belongs to the tunnel, so an unreadable ``tunnel-token`` atom is recovered by
    reading it back from Cloudflare: the tunnel, its ingress and the hostname's DNS record are
    untouched, so nothing moves. It is a re-read, not a revocation -- the connector token is the
    tunnel's own secret, and replacing it means a new tunnel and a republished hostname.
    """
    _require_settings(for_teardown=True)
    handles = await _recorded_tunnel(context)
    async with _cloudflare_client() as client:
        token = await _tunnel_token(_CloudflareApi(client), handles.account_id, handles.tunnel_id)
    await store_installation_atoms(context, {TUNNEL_TOKEN_ATOM: token})
    log.info(
        "companion edge tunnel token reissued installation_id=%s tunnel_id=%s",
        context.installation_id,
        handles.tunnel_id,
    )


async def teardown_edge(
    context: LifecycleStepContext,
    tunnel_resources: JsonObject,
    publish_resources: JsonObject | None = None,
) -> None:
    """Take the whole edge down: the DNS record first, then the tunnel (T-06-14-06).

    Deleting DNS first means a name never points at a tunnel that no longer exists.
    """
    if publish_resources is not None:
        await teardown_edge_publication(context, publish_resources)
    await teardown_edge_tunnel(context, tunnel_resources)


EDGE_TUNNEL_STEP = ProvisioningStep(
    name=POSTIZ_EDGE_STEP,
    provision=provision_edge_tunnel,
    teardown=teardown_edge_tunnel,
)
EDGE_PUBLISH_STEP = ProvisioningStep(
    name=POSTIZ_EDGE_PUBLISH_STEP,
    provision=publish_edge_ingress,
    teardown=teardown_edge_publication,
)


# ---------------------------------------------------------------------------
# Reachability.
# ---------------------------------------------------------------------------


def _is_tls_failure(exc: BaseException) -> bool:
    """Whether a transport failure was the TLS handshake or the certificate."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLError | ssl.CertificateError):
            return True
        current = current.__cause__ or current.__context__
    return False


async def probe_edge_reachability(
    installation: CompanionInstallation, *, now: datetime | None = None
) -> JsonObject:
    """Ask the edge whether the installation answers on its hostname; ``{state, checked_at}``.

    No credential is sent: Postiz answers ``is-connected`` without a key with 401, so a 401 that
    arrived over valid TLS proves the whole path -- DNS, edge certificate, tunnel and app. Nothing
    is followed or retried; one answer is one measurement.
    """
    checked_at = (now or datetime.now(UTC)).isoformat()
    hostname = installation.hostname
    if not _one_label_under_zone(hostname):
        # Only a companion hostname is ever probed, never an arbitrary host.
        return {"state": NOT_ANSWERING, "checked_at": checked_at}
    try:
        async with httpx.AsyncClient(
            transport=_transport_override,
            timeout=REACHABILITY_TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"User-Agent": "iforge-backend/1.0"},
        ) as client:
            response = await client.get(f"https://{hostname}{REACHABILITY_PATH}")
    except httpx.HTTPError as exc:
        state = TLS_PENDING if _is_tls_failure(exc) else NOT_ANSWERING
    else:
        state = REACHABLE if response.status_code == 401 else NOT_ANSWERING
    return {"state": state, "checked_at": checked_at}


__all__ = [
    "EDGE_PUBLISH_STEP",
    "EDGE_TUNNEL_STEP",
    "NOT_ANSWERING",
    "REACHABILITY_PROBE_INTERVAL_SECONDS",
    "REACHABLE",
    "TLS_PENDING",
    "edge_cname_target",
    "edge_ingress_config",
    "edge_tunnel_name",
    "probe_edge_reachability",
    "provision_edge_tunnel",
    "publish_edge_ingress",
    "reissue_edge_tunnel_token",
    "teardown_edge",
    "teardown_edge_publication",
    "teardown_edge_tunnel",
]
