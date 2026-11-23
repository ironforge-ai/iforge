"""Per-installation Postiz media: its own R2 bucket, credential and domain (D-18/D-19 at CK-C).

**Why a bucket per installation (research F1).** Postiz writes every upload to its bucket's ROOT
(``Key: ${id}.${extension}``) and takes no prefix, so a per-tenant prefix is impossible and one
shared bucket would hand every tenant's Postiz credential every tenant's media. CK-C
(``bucket-per-installation-lifecycle-export``) therefore gives each installation:

* one bucket, ``postiz-media-<suffix>`` (the hostname label's random suffix),
* one account API token, ``iforge-postiz-media-<installation id>``, whose only policy is
  "Workers R2 Storage Bucket Item Write" on that single bucket; Postiz's S3 access key id is the
  token id and its secret access key the SHA-256 hex of the token value (Cloudflare's documented
  derivation), stored as the ``media-access-key-id`` / ``media-secret-access-key`` atoms,
* one public media domain, ``media-<suffix>.<companion zone>``, attached to the bucket.

**Durability (CK-C amends D-19).** R2 implements neither versioning nor Object Lock (F2), so none
is claimed: a lifecycle rule aborts incomplete multipart uploads after seven days, media is kept
until uninstall, and the export includes the bucket. An object deleted inside Postiz is gone
except from a prior export.

**Retries and failures.** Every name is derived from the installation, so a retried step ADOPTS
what an earlier attempt created: a 409 on the bucket or the domain is confirmed by reading it
back, and a token already carrying this installation's name has its value rolled (the earlier
value was never stored). A step that raises is not recorded, so the worker would never tear it
down; the step therefore undoes what it created or adopted before re-raising. Teardown is
404-tolerant throughout and drops the revoked credential's atoms.

Cloudflare API endpoints (verified against Cloudflare's published OpenAPI schema,
``cloudflare/api-schemas``, and the R2 "API tokens" documentation at execution), all under
``https://api.cloudflare.com/client/v4/accounts/{account_id}``:

* ``POST /r2/buckets`` (``r2-create-bucket``), ``GET`` and ``DELETE /r2/buckets/{bucket}``
* ``PUT /r2/buckets/{bucket}/lifecycle`` (``abortMultipartUploadsTransition``, ``Age`` condition)
* ``POST /r2/buckets/{bucket}/domains/custom``; ``GET`` and ``DELETE .../domains/custom/{domain}``
* ``GET /tokens`` (paged), ``POST /tokens`` (``account-api-tokens-create-token``),
  ``PUT /tokens/{token_id}/value`` (``account-api-tokens-roll-token``), ``DELETE /tokens/{id}``

Teardown empties the bucket through ``create_s3_object_store_client`` -- ``file_store_s3`` stays
the sole boto3 owner -- with the operator's admin S3 key pair. Every operator setting is checked
before the first call, and a missing one fails the step by name. No token value, derived secret or
response body is ever logged or put into an error.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

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
from app.services.companion_postiz import (
    MEDIA_ACCESS_KEY_ID_ATOM,
    MEDIA_SECRET_ACCESS_KEY_ATOM,
    POSTIZ_MEDIA_STEP,
    POSTIZ_R2_REGION,
)
from app.services.file_store_s3 import create_s3_object_store_client

log = logging.getLogger(__name__)

CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4"
# Cloudflare's published id of the "Workers R2 Storage Bucket Item Write" permission group
# (bucket-scoped object read, write and list), from the R2 "API tokens" documentation.
R2_BUCKET_ITEM_WRITE_PERMISSION_GROUP_ID = "2efd5506f9c8494dacb1fa10a3e7d5b6"
R2_DEFAULT_JURISDICTION = "default"
MULTIPART_ABORT_AFTER_SECONDS = 7 * 24 * 60 * 60
MEDIA_LIFECYCLE_RULE_ID = "abort-incomplete-multipart-uploads"
MEDIA_DOMAIN_MIN_TLS = "1.2"
MEDIA_STEP_ATOMS = (MEDIA_ACCESS_KEY_ID_ATOM, MEDIA_SECRET_ACCESS_KEY_ATOM)
_TIMEOUT_SECONDS = 20.0
_TOKEN_PAGE_SIZE = 50
_TOKEN_MAX_PAGES = 200
_NOT_FOUND = frozenset({404})
_CONFLICT = frozenset({409})

# Test seam (the tenant_purge pattern): tests point this at an httpx.MockTransport standing in for
# the Cloudflare API. None means the default network transport.
_transport_override: httpx.AsyncBaseTransport | None = None


def media_bucket_name(suffix: str) -> str:
    return f"postiz-media-{suffix}"


def media_domain_name(suffix: str, zone: str) -> str:
    return f"media-{suffix}.{zone}"


def media_token_name(context: LifecycleStepContext) -> str:
    return f"iforge-postiz-media-{context.installation_id}"


def media_bucket_policy(account_id: str, bucket: str) -> list[dict[str, Any]]:
    """The token's single policy: object read/write/list on this one bucket, nothing else."""
    resource = f"com.cloudflare.edge.r2.bucket.{account_id}_{R2_DEFAULT_JURISDICTION}_{bucket}"
    return [
        {
            "effect": "allow",
            "permission_groups": [{"id": R2_BUCKET_ITEM_WRITE_PERMISSION_GROUP_ID}],
            "resources": {resource: "*"},
        }
    ]


def media_lifecycle_rules() -> dict[str, Any]:
    """Lifecycle hygiene (CK-C): abort incomplete multipart uploads after seven days."""
    return {
        "rules": [
            {
                "id": MEDIA_LIFECYCLE_RULE_ID,
                "enabled": True,
                "conditions": {"prefix": ""},
                "abortMultipartUploadsTransition": {
                    "condition": {"type": "Age", "maxAge": MULTIPART_ABORT_AFTER_SECONDS}
                },
            }
        ]
    }


def s3_secret_access_key(token_value: str) -> str:
    """Cloudflare's derivation: an R2 secret access key is the SHA-256 hex of the token value."""
    return hashlib.sha256(token_value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Settings: every one is checked before the first external call.
# ---------------------------------------------------------------------------


def _require_settings(*, for_teardown: bool) -> None:
    """Refuse by name, before any call, when an operator setting the step needs is unset."""
    required = {
        "CLOUDFLARE_ACCOUNT_ID": settings.cloudflare_account_id,
        "CLOUDFLARE_R2_API_TOKEN": settings.cloudflare_r2_api_token,
        "CLOUDFLARE_R2_S3_ENDPOINT": settings.cloudflare_r2_s3_endpoint,
        "CLOUDFLARE_R2_ADMIN_ACCESS_KEY_ID": settings.cloudflare_r2_admin_access_key_id,
        "CLOUDFLARE_R2_ADMIN_SECRET_ACCESS_KEY": settings.cloudflare_r2_admin_secret_access_key,
    }
    if not for_teardown:
        # Teardown works from the recorded handles, so it needs no zone to find the domain.
        required["COMPANION_EDGE_ZONE_ID"] = settings.companion_edge_zone_id
        required["COMPANION_EDGE_ZONE_NAME"] = settings.companion_edge_zone_name
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise CompanionStepError(
            "media_settings_missing", f"operator settings are unset: {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# The Cloudflare API.
# ---------------------------------------------------------------------------


class _Envelope(BaseModel):
    """Cloudflare's v4 response envelope; only these fields are read."""

    model_config = ConfigDict(extra="ignore")

    success: bool
    result: JsonValue = None
    result_info: dict[str, JsonValue] | None = None


class _CreatedToken(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    value: str = Field(min_length=1)


class _ListedToken(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1)
    name: str


class _PageInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_pages: int = 1


class _MediaHandles(BaseModel):
    """What the media step recorded; teardown acts on exactly these."""

    model_config = ConfigDict(extra="ignore")

    bucket: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    token_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _Answer:
    status: int
    envelope: _Envelope | None


class _CloudflareAccount:
    """One account's slice of the Cloudflare API, answering only envelopes it can validate."""

    def __init__(self, client: httpx.AsyncClient, account_id: str) -> None:
        self._client = client
        self._prefix = f"/accounts/{account_id}"

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
        """A validated success envelope, or an ``accept``-ed status; a named error otherwise.

        The error names the operation and the HTTP status only: never the path (it carries the
        operator's account id), never a response body.
        """
        try:
            response = await self._client.request(
                method, f"{self._prefix}{path}", json=body, params=params
            )
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
        return _Answer(response.status_code, envelope)


def _cloudflare_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=CLOUDFLARE_API_BASE_URL,
        transport=_transport_override,
        timeout=_TIMEOUT_SECONDS,
        headers={
            "Authorization": f"Bearer {settings.cloudflare_r2_api_token}",
            "User-Agent": "iforge-backend/1.0",
        },
    )


def _result(answer: _Answer) -> JsonValue:
    return answer.envelope.result if answer.envelope is not None else None


async def _ensure_bucket(account: _CloudflareAccount, bucket: str) -> None:
    """Create the bucket, or adopt it when an earlier attempt already did."""
    created = await account.call(
        "POST",
        "/r2/buckets",
        operation="create media bucket",
        body={"name": bucket},
        accept=_CONFLICT,
    )
    if created.status in _CONFLICT:
        # Bucket names are account-scoped: a 409 that reads back is this account's own bucket,
        # and this installation's name means an earlier attempt of this step created it.
        await account.call("GET", f"/r2/buckets/{bucket}", operation="read media bucket")


async def _ensure_domain(account: _CloudflareAccount, bucket: str, domain: str) -> None:
    """Attach the media domain, or adopt it when it is already attached to THIS bucket."""
    attached = await account.call(
        "POST",
        f"/r2/buckets/{bucket}/domains/custom",
        operation="attach media domain",
        body={
            "domain": domain,
            "zoneId": settings.companion_edge_zone_id,
            "enabled": True,
            "minTLS": MEDIA_DOMAIN_MIN_TLS,
        },
        accept=_CONFLICT,
    )
    if attached.status in _CONFLICT:
        existing = await account.call(
            "GET",
            f"/r2/buckets/{bucket}/domains/custom/{domain}",
            operation="read media domain",
            accept=_NOT_FOUND,
        )
        if existing.status in _NOT_FOUND:
            raise CompanionStepError(
                "media_domain_taken", "the media domain is attached to a different bucket"
            )


async def _find_token(account: _CloudflareAccount, name: str) -> str | None:
    """The id of the account token carrying this installation's token name, if one exists."""
    page = 1
    while page <= _TOKEN_MAX_PAGES:
        answer = await account.call(
            "GET",
            "/tokens",
            operation="list media tokens",
            params={"page": str(page), "per_page": str(_TOKEN_PAGE_SIZE)},
        )
        try:
            listed = [_ListedToken.model_validate(item) for item in _list(_result(answer))]
            info = _PageInfo.model_validate(
                answer.envelope.result_info if answer.envelope is not None else {}
            )
        except ValidationError:
            raise CompanionStepError(
                "cloudflare_invalid_response", "list media tokens answered an invalid page"
            ) from None
        for token in listed:
            if token.name == name:
                return token.id
        if page >= info.total_pages or not listed:
            return None
        page += 1
    raise CompanionStepError("cloudflare_invalid_response", "list media tokens did not end")


def _list(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        raise CompanionStepError("cloudflare_invalid_response", "a token list was not a list")
    return value


async def _issue_token(
    account: _CloudflareAccount, context: LifecycleStepContext, account_id: str, bucket: str
) -> tuple[str, str]:
    """This installation's bucket-scoped token: an earlier attempt's is rolled, else created."""
    name = media_token_name(context)
    existing_id = await _find_token(account, name)
    if existing_id is not None:
        rolled = await account.call(
            "PUT", f"/tokens/{existing_id}/value", operation="roll media token", body={}
        )
        value = _result(rolled)
        if not isinstance(value, str) or not value:
            raise CompanionStepError(
                "cloudflare_invalid_response", "roll media token answered no value"
            )
        return existing_id, value
    created = await account.call(
        "POST",
        "/tokens",
        operation="create media token",
        body={"name": name, "policies": media_bucket_policy(account_id, bucket)},
    )
    try:
        token = _CreatedToken.model_validate(_result(created))
    except ValidationError:
        raise CompanionStepError(
            "cloudflare_invalid_response", "create media token answered no token"
        ) from None
    return token.id, token.value


# ---------------------------------------------------------------------------
# The step.
# ---------------------------------------------------------------------------


async def provision_media_bucket(context: LifecycleStepContext) -> JsonObject:
    """Create or adopt this installation's bucket, lifecycle, media domain and credential."""
    _require_settings(for_teardown=False)
    suffix = installation_label_suffix(context)
    account_id = settings.cloudflare_account_id
    zone = settings.companion_edge_zone_name.strip().lower().rstrip(".")
    bucket = media_bucket_name(suffix)
    domain = media_domain_name(suffix, zone)
    bucket_ready = domain_ready = False
    token_id: str | None = None
    try:
        async with _cloudflare_client() as client:
            account = _CloudflareAccount(client, account_id)
            await _ensure_bucket(account, bucket)
            bucket_ready = True
            await account.call(
                "PUT",
                f"/r2/buckets/{bucket}/lifecycle",
                operation="put media bucket lifecycle",
                body=media_lifecycle_rules(),
            )
            await _ensure_domain(account, bucket, domain)
            domain_ready = True
            token_id, token_value = await _issue_token(account, context, account_id, bucket)
        await store_installation_atoms(
            context,
            {
                MEDIA_ACCESS_KEY_ID_ATOM: token_id,
                MEDIA_SECRET_ACCESS_KEY_ATOM: s3_secret_access_key(token_value),
            },
        )
    except Exception:
        # Not recorded, so never torn down by the worker: undo it here, then fail as before.
        # (A cancelled worker is not undone: its retry adopts what it left, by name.)
        await _undo_partial(
            context,
            _MediaTarget(account_id, bucket, domain, token_id),
            bucket_ready=bucket_ready,
            domain_ready=domain_ready,
        )
        raise
    log.info(
        "companion media bucket provisioned installation_id=%s bucket=%s",
        context.installation_id,
        bucket,
    )
    return {"bucket": bucket, "domain": domain, "token_id": token_id, "account_id": account_id}


@dataclass(frozen=True, slots=True)
class _MediaTarget:
    """The resources one teardown acts on; ``token_id`` is ``None`` when none was issued."""

    account_id: str
    bucket: str
    domain: str
    token_id: str | None


async def _undo_partial(
    context: LifecycleStepContext,
    target: _MediaTarget,
    *,
    bucket_ready: bool,
    domain_ready: bool,
) -> None:
    """Best-effort compensation for a step that raised midway; never masks the original error."""
    if not (bucket_ready or domain_ready or target.token_id):
        return
    try:
        await _delete_media(context, target, bucket_ready=bucket_ready, domain_ready=domain_ready)
    except Exception as exc:  # noqa: BLE001 - the step's own failure is what gets reported.
        log.warning(
            "companion media partial provision not fully undone installation_id=%s error=%s",
            context.installation_id,
            exc.__class__.__name__,
        )


def _purge_bucket_objects(bucket: str) -> int:
    client = create_s3_object_store_client(
        bucket=bucket,
        region=POSTIZ_R2_REGION,
        endpoint_url=settings.cloudflare_r2_s3_endpoint,
        access_key_id=settings.cloudflare_r2_admin_access_key_id,
        secret_access_key=settings.cloudflare_r2_admin_secret_access_key,
        force_path_style=True,
    )
    try:
        return client.delete_prefix("")
    finally:
        client.close()


async def _delete_media(
    context: LifecycleStepContext,
    target: _MediaTarget,
    *,
    bucket_ready: bool = True,
    domain_ready: bool = True,
) -> None:
    """Objects, then the domain, the bucket and the token -- each already-gone one skipped."""
    async with _cloudflare_client() as client:
        account = _CloudflareAccount(client, target.account_id)
        bucket_path = f"/r2/buckets/{target.bucket}"
        exists = bucket_ready and (
            (
                await account.call(
                    "GET", bucket_path, operation="read media bucket", accept=_NOT_FOUND
                )
            ).status
            not in _NOT_FOUND
        )
        if exists:
            # R2 deletes only an empty bucket; a torn-down installation's media goes with it.
            purged = await asyncio.to_thread(_purge_bucket_objects, target.bucket)
            log.info(
                "companion media objects deleted installation_id=%s count=%d",
                context.installation_id,
                purged,
            )
        if domain_ready:
            await account.call(
                "DELETE",
                f"{bucket_path}/domains/custom/{target.domain}",
                operation="detach media domain",
                accept=_NOT_FOUND,
            )
        if exists:
            await account.call(
                "DELETE", bucket_path, operation="delete media bucket", accept=_NOT_FOUND
            )
        if target.token_id:
            await account.call(
                "DELETE",
                f"/tokens/{target.token_id}",
                operation="revoke media token",
                accept=_NOT_FOUND,
            )
    await delete_installation_atoms(context, MEDIA_STEP_ATOMS)


async def teardown_media_bucket(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Delete the bucket's objects, its domain, the bucket and the token; 404s are already done."""
    _require_settings(for_teardown=True)
    try:
        handles = _MediaHandles.model_validate(resources)
    except ValidationError:
        raise CompanionStepError(
            "media_handles_invalid", "the recorded media handles are incomplete"
        ) from None
    await _delete_media(
        context,
        _MediaTarget(handles.account_id, handles.bucket, handles.domain, handles.token_id),
    )
    log.info(
        "companion media bucket torn down installation_id=%s bucket=%s",
        context.installation_id,
        handles.bucket,
    )


MEDIA_BUCKET_STEP = ProvisioningStep(
    name=POSTIZ_MEDIA_STEP,
    provision=provision_media_bucket,
    teardown=teardown_media_bucket,
)


async def reissue_media_credential(context: LifecycleStepContext) -> JsonObject:
    """Revoke the installation's media credential and mint a new one; the media stays (06-25).

    Operator recovery of an unreadable media atom. The bucket, its objects and its domain are
    never touched -- unlike this step's teardown, which empties and deletes the bucket. The
    installation's token is found by name and its value ROLLED, which invalidates the old value
    at Cloudflare; a token that no longer exists is created again with the same one-bucket policy.
    Returns the (possibly new) token id for the recorded step handles.
    """
    _require_settings(for_teardown=False)
    account_id = settings.cloudflare_account_id
    bucket = media_bucket_name(installation_label_suffix(context))
    async with _cloudflare_client() as client:
        token_id, token_value = await _issue_token(
            _CloudflareAccount(client, account_id), context, account_id, bucket
        )
    await store_installation_atoms(
        context,
        {
            MEDIA_ACCESS_KEY_ID_ATOM: token_id,
            MEDIA_SECRET_ACCESS_KEY_ATOM: s3_secret_access_key(token_value),
        },
    )
    log.info(
        "companion media credential reissued installation_id=%s bucket=%s",
        context.installation_id,
        bucket,
    )
    return {"token_id": token_id}


__all__ = [
    "MEDIA_BUCKET_STEP",
    "media_bucket_name",
    "media_bucket_policy",
    "media_domain_name",
    "media_lifecycle_rules",
    "provision_media_bucket",
    "reissue_media_credential",
    "s3_secret_access_key",
    "teardown_media_bucket",
]
