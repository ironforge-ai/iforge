"""Per-installation Temporal namespace on the standard tier's shared engine (D-04, D-05 at CK-A).

Each installation gets its own namespace, ``postiz-<suffix>``, created CLEAN through the
frontend's HTTP API (research F4: an ``auto-setup`` namespace already carries two Text search
attributes, and with PostgreSQL visibility's three-Text limit Postiz then fails at boot). Postiz's
two search attributes, ``organizationId`` and ``postId`` (Text), are pre-registered here because
``TEMPORAL_TLS=true`` skips Postiz's own registration (``temporal.register.ts:10-12``). The
installation then receives a JWT whose ONLY permission is ``<namespace>:writer``, stored as the
``temporal-api-key`` atom; the frontend's default ClaimMapper and Authorizer enforce it.

**HTTP routes (verified at execution against ``temporalio/api`` v1.50.1 -- the API version the
upstream-tested Temporal server 1.28.1 pins -- and the server's own handlers):**

* RegisterNamespace -- ``POST /api/v1/namespaces`` (``body: "*"``)
* DescribeNamespace -- ``GET /api/v1/namespaces/{namespace}``
* UpdateNamespace -- ``POST /api/v1/namespaces/{namespace}/update`` (``body: "*"``)
* ListSearchAttributes -- ``GET /api/v1/namespaces/{namespace}/search-attributes``

``OperatorService.AddSearchAttributes`` and ``OperatorService.DeleteNamespace`` carry NO HTTP
binding in that version (nor on ``temporalio/api`` master), so neither is callable over HTTP:

* Search attributes are added the way the server's own SQL-visibility ``AddSearchAttributes``
  does it (``operator_handler.go`` ``addSearchAttributesSQL``): DescribeNamespace, then
  UpdateNamespace upserting ``config.customSearchAttributeAliases`` onto free pre-allocated Text
  fields (``Text01``..``Text03``). This is exact for SQL visibility, which research requires
  (Elasticsearch off).
* Teardown moves the namespace to ``NAMESPACE_STATE_DELETED`` with UpdateNamespace
  (``validateStateUpdate`` allows REGISTERED -> DELETED). The namespace then serves no calls and
  its token is useless; reclaiming its stored histories is the gRPC-only DeleteNamespace, an
  operator action recorded as an open item of plan 06-13.

The control plane authenticates with a short-lived admin JWT (``temporal-system:admin``) it mints
with the operator's RS256 key. No JWT is ever logged or put into an error.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from app.core.config import settings
from app.services.companion_lifecycle_steps import (
    CompanionStepError,
    JsonObject,
    LifecycleStepContext,
    ProvisioningStep,
    delete_installation_atoms,
    installation_label_suffix,
    store_installation_atoms,
)
from app.services.companion_postiz import POSTIZ_TEMPORAL_STEP, TEMPORAL_API_KEY_ATOM

log = logging.getLogger(__name__)

POSTIZ_SEARCH_ATTRIBUTES = ("organizationId", "postId")
# The pre-allocated Text columns of Temporal's SQL visibility (`sqlDbCustomSearchAttributes`).
SQL_VISIBILITY_TEXT_FIELDS = ("Text01", "Text02", "Text03")
INDEXED_VALUE_TYPE_TEXT = "INDEXED_VALUE_TYPE_TEXT"
NAMESPACE_STATE_REGISTERED = "NAMESPACE_STATE_REGISTERED"
NAMESPACE_STATE_DELETED = "NAMESPACE_STATE_DELETED"
ADMIN_JWT_LIFETIME = timedelta(minutes=5)
INSTALLATION_JWT_LIFETIME = timedelta(days=365)
SYSTEM_ADMIN_PERMISSION = "temporal-system:admin"
_TIMEOUT_SECONDS = 20.0
_NOT_FOUND = frozenset({404})
_CONFLICT = frozenset({409})

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])

# Test seam (the tenant_purge pattern): tests point this at an httpx.MockTransport standing in for
# the Temporal frontend's HTTP API. None means the default network transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def temporal_namespace_name(suffix: str) -> str:
    return f"postiz-{suffix}"


def _signed(claims: dict[str, object]) -> str:
    return jwt.encode(
        claims,
        settings.companion_temporal_jwt_private_key,
        algorithm="RS256",
        headers={"kid": settings.companion_temporal_jwt_key_id},
    )


def _admin_jwt(now: datetime) -> str:
    return _signed(
        {
            "sub": "iforge-control-plane",
            "permissions": [SYSTEM_ADMIN_PERMISSION],
            "iat": int(now.timestamp()),
            "exp": int((now + ADMIN_JWT_LIFETIME).timestamp()),
        }
    )


def mint_installation_temporal_jwt(
    *, installation_id: object, namespace: str, now: datetime | None = None
) -> str:
    """The installation's token: writer on its own namespace, and nothing else, for one year."""
    moment = now or datetime.now(UTC)
    return _signed(
        {
            "sub": f"postiz:{installation_id}",
            "permissions": [f"{namespace}:writer"],
            "iat": int(moment.timestamp()),
            "exp": int((moment + INSTALLATION_JWT_LIFETIME).timestamp()),
        }
    )


def _require_settings(*, for_teardown: bool) -> None:
    """Refuse by name, before any call, when an operator setting the step needs is unset."""
    required = {
        "COMPANION_TEMPORAL_HTTP_URL": settings.companion_temporal_http_url,
        "COMPANION_TEMPORAL_JWT_PRIVATE_KEY": settings.companion_temporal_jwt_private_key,
        "COMPANION_TEMPORAL_JWT_KEY_ID": settings.companion_temporal_jwt_key_id,
    }
    if not for_teardown:
        # Teardown dials only the HTTP API; the address tenant VMs use is not needed to delete.
        required["COMPANION_TEMPORAL_FRONTEND_ADDRESS"] = (
            settings.companion_temporal_frontend_address
        )
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise CompanionStepError(
            "temporal_settings_missing", f"operator settings are unset: {', '.join(missing)}"
        )


class _TemporalHttp:
    """The frontend's HTTP API, answering only JSON objects; named errors otherwise."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def call(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        body: object | None = None,
        accept: frozenset[int] = frozenset(),
    ) -> tuple[int, dict[str, JsonValue]]:
        """``(status, body)`` for a success or an ``accept``-ed status; a named error otherwise.

        The error names the operation and the HTTP status only, never a response body.
        """
        try:
            response = await self._client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise CompanionStepError(
                "temporal_unreachable", f"{operation} failed: {exc.__class__.__name__}"
            ) from None
        if response.status_code in accept:
            return response.status_code, {}
        if not response.is_success:
            raise CompanionStepError(
                "temporal_refused", f"{operation} answered HTTP {response.status_code}"
            )
        try:
            return response.status_code, _JSON_OBJECT.validate_json(response.content)
        except ValidationError:
            raise CompanionStepError(
                "temporal_invalid_response", f"{operation} answered an invalid body"
            ) from None


def _temporal_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.companion_temporal_http_url.strip().rstrip("/"),
        transport=_transport_override,
        timeout=_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {_admin_jwt(datetime.now(UTC))}",
            "User-Agent": "iforge-backend/1.0",
        },
    )


class _NamespaceConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # Field -> alias: the pre-allocated SQL visibility columns this namespace has named.
    custom_search_attribute_aliases: dict[str, str] = Field(
        default_factory=dict, alias="customSearchAttributeAliases"
    )


class _NamespaceInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: JsonValue = None


class _DescribedNamespace(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    namespace_info: _NamespaceInfo = Field(default_factory=_NamespaceInfo, alias="namespaceInfo")
    config: _NamespaceConfig = Field(default_factory=_NamespaceConfig)


class _SearchAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    # Name -> IndexedValueType, for the attributes this namespace can use (aliases included).
    custom_attributes: dict[str, JsonValue] = Field(default_factory=dict, alias="customAttributes")


def _is_text(indexed_value_type: JsonValue) -> bool:
    # protojson names the enum; the shorthand name and the wire number are accepted as well.
    return indexed_value_type in (INDEXED_VALUE_TYPE_TEXT, "Text", 1)


def _is_registered(state: JsonValue) -> bool:
    return state in (NAMESPACE_STATE_REGISTERED, "Registered", 1)


async def _describe(temporal: _TemporalHttp, namespace: str) -> _DescribedNamespace:
    _status, described = await temporal.call(
        "GET", f"/api/v1/namespaces/{namespace}", operation="describe namespace"
    )
    try:
        return _DescribedNamespace.model_validate(described)
    except ValidationError:
        raise CompanionStepError(
            "temporal_invalid_response", "describe namespace answered an invalid namespace"
        ) from None


async def _refuse_unclean(temporal: _TemporalHttp, namespace: str) -> None:
    """F4: Postiz fails at boot if its two Text attributes do not fit beside what is there.

    SQL visibility allows three Text attributes per namespace, so more than one Text attribute
    that is not Postiz's own -- an ``auto-setup`` namespace carries two -- is refused by name. An
    attribute of Postiz's name but another type is refused too: Postiz could never register it.
    """
    _status, listed = await temporal.call(
        "GET",
        f"/api/v1/namespaces/{namespace}/search-attributes",
        operation="list search attributes",
    )
    try:
        custom = _SearchAttributes.model_validate(listed).custom_attributes
    except ValidationError:
        raise CompanionStepError(
            "temporal_invalid_response", "list search attributes answered an invalid list"
        ) from None
    foreign_text = [
        name
        for name, kind in custom.items()
        if name not in POSTIZ_SEARCH_ATTRIBUTES and _is_text(kind)
    ]
    mistyped = [
        name for name in POSTIZ_SEARCH_ATTRIBUTES if name in custom and not _is_text(custom[name])
    ]
    if len(foreign_text) > 1 or mistyped:
        raise CompanionStepError(
            "temporal_namespace_not_clean",
            f"the namespace already holds {len(foreign_text)} other Text search attributes"
            f"{' and a mistyped Postiz attribute' if mistyped else ''}; Postiz needs a clean one",
        )


async def _add_search_attributes(
    temporal: _TemporalHttp, namespace: str, described: _DescribedNamespace
) -> None:
    """SQL visibility's AddSearchAttributes: alias free Text fields to Postiz's attributes."""
    aliases = described.config.custom_search_attribute_aliases
    free = [field for field in SQL_VISIBILITY_TEXT_FIELDS if field not in aliases]
    missing = [name for name in POSTIZ_SEARCH_ATTRIBUTES if name not in aliases.values()]
    if len(missing) > len(free):
        raise CompanionStepError(
            "temporal_namespace_not_clean",
            "the namespace has no free Text search attribute for Postiz",
        )
    upsert = dict(zip(free, missing, strict=False))
    if upsert:
        await temporal.call(
            "POST",
            f"/api/v1/namespaces/{namespace}/update",
            operation="add search attributes",
            body={"namespace": namespace, "config": {"customSearchAttributeAliases": upsert}},
        )


async def _mark_deleted(temporal: _TemporalHttp, namespace: str) -> None:
    """UpdateNamespace to DELETED (DeleteNamespace has no HTTP binding); a 404 is already gone."""
    await temporal.call(
        "POST",
        f"/api/v1/namespaces/{namespace}/update",
        operation="delete namespace",
        body={"namespace": namespace, "updateInfo": {"state": NAMESPACE_STATE_DELETED}},
        accept=_NOT_FOUND,
    )


async def provision_temporal_namespace(context: LifecycleStepContext) -> JsonObject:
    """Create (or adopt) this installation's clean namespace, add Postiz's attributes, mint its JWT.

    A 409 on register means an earlier attempt of this step created the namespace (its name is
    this installation's): it is adopted only while REGISTERED and clean. A namespace this call
    created is marked deleted again if a later part of the step fails; one it adopted is left
    exactly as it was found.
    """
    _require_settings(for_teardown=False)
    namespace = temporal_namespace_name(installation_label_suffix(context))
    retention_seconds = settings.companion_temporal_namespace_retention_days * 24 * 60 * 60
    async with _temporal_client() as client:
        temporal = _TemporalHttp(client)
        status, _body = await temporal.call(
            "POST",
            "/api/v1/namespaces",
            operation="register namespace",
            body={
                "namespace": namespace,
                "description": f"iforge postiz installation {context.installation_id}",
                "workflowExecutionRetentionPeriod": f"{retention_seconds}s",
            },
            accept=_CONFLICT,
        )
        created = status not in _CONFLICT
        try:
            described = await _describe(temporal, namespace)
            if not _is_registered(described.namespace_info.state):
                raise CompanionStepError(
                    "temporal_namespace_unavailable",
                    "the installation's namespace exists but is not registered",
                )
            await _refuse_unclean(temporal, namespace)
            await _add_search_attributes(temporal, namespace, described)
            api_key = mint_installation_temporal_jwt(
                installation_id=context.installation_id, namespace=namespace
            )
            await store_installation_atoms(context, {TEMPORAL_API_KEY_ATOM: api_key})
        except Exception:
            # Not recorded, so never torn down by the worker: undo what THIS call created.
            if created:
                await _undo_created(context, temporal, namespace)
            raise
    log.info(
        "companion temporal namespace provisioned installation_id=%s namespace=%s",
        context.installation_id,
        namespace,
    )
    return {
        "namespace": namespace,
        "address": settings.companion_temporal_frontend_address,
        "tls": settings.companion_temporal_tls,
    }


async def _undo_created(
    context: LifecycleStepContext, temporal: _TemporalHttp, namespace: str
) -> None:
    try:
        await _mark_deleted(temporal, namespace)
        await delete_installation_atoms(context, (TEMPORAL_API_KEY_ATOM,))
    except Exception as exc:  # noqa: BLE001 - the step's own failure is what gets reported.
        log.warning(
            "companion temporal partial provision not undone installation_id=%s error=%s",
            context.installation_id,
            exc.__class__.__name__,
        )


class _TemporalHandles(BaseModel):
    model_config = ConfigDict(extra="ignore")

    namespace: str = Field(min_length=1)


async def teardown_temporal_namespace(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Mark the namespace deleted: it serves nothing more, and its token opens nothing."""
    _require_settings(for_teardown=True)
    try:
        namespace = _TemporalHandles.model_validate(resources).namespace
    except ValidationError:
        raise CompanionStepError(
            "temporal_handles_invalid", "the recorded temporal handles are incomplete"
        ) from None
    async with _temporal_client() as client:
        await _mark_deleted(_TemporalHttp(client), namespace)
    await delete_installation_atoms(context, (TEMPORAL_API_KEY_ATOM,))
    log.info(
        "companion temporal namespace torn down installation_id=%s namespace=%s",
        context.installation_id,
        namespace,
    )


TEMPORAL_NAMESPACE_STEP = ProvisioningStep(
    name=POSTIZ_TEMPORAL_STEP,
    provision=provision_temporal_namespace,
    teardown=teardown_temporal_namespace,
)


__all__ = [
    "POSTIZ_SEARCH_ATTRIBUTES",
    "TEMPORAL_NAMESPACE_STEP",
    "mint_installation_temporal_jwt",
    "provision_temporal_namespace",
    "teardown_temporal_namespace",
    "temporal_namespace_name",
]
