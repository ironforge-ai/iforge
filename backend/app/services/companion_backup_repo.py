"""Each installation's pgBackRest repository, behind a key that cannot delete (D-14, D-15, OPS-04).

**Where the backups live (D-15).** One Backblaze B2 bucket, in a provider account separate from
the compute provider and from the Cloudflare account that holds media and the edge, created with
Object Lock and file versioning on. Each installation's repository is the path
``/<tenant_id>/<installation_id>`` inside it, one pgBackRest stanza per installation
(``postiz-<hostname suffix>``), encrypted with ``aes-256-cbc`` under a per-installation cipher pass
that exists only in the installation's encrypted secrets (T-06-15-03).

**The runtime's key cannot delete (T-06-15-01).** The install job's ``backup`` step mints ONE B2
application key per installation, named ``pgbr-<installation_id>``, whose capabilities are exactly
``listFiles``, ``readFiles`` and ``writeFiles`` -- never ``deleteFiles``, ``bypassGovernance`` or
``writeBucketRetentions`` -- restricted to the backup bucket and to the name prefix
``<tenant_id>/<installation_id>/``. The trailing slash is load-bearing: without it, installation
``ab`` could read and write installation ``abc``'s objects (T-06-15-02). With versioning on, the
most a compromised runtime can do is hide a file; every version stays retrievable under Object
Lock. The runtime also runs pgBackRest with ``expire-auto`` off, so retention expiry is only ever
the operator's act with the operator's own credential.

**Teardown never deletes data.** Uninstall and a failed install's teardown revoke the key and drop
its atoms. They issue no file or version deletion, and they keep the cipher pass: the repository
stays under retention, and without its cipher pass it could never be read again.

**Retries and failures.** The key's name is derived from the installation, so a retried step finds
and revokes a key an earlier attempt minted before minting a new one. A step that raises after
minting revokes what it minted (the worker never tears down an unrecorded step), and a key B2
answers wider than requested is revoked and refused. The cipher pass is generated once and never
replaced. Every operator setting is checked before the first call.

B2 native API v4 (re-verified at execution against backblaze.com/apidocs). The operator key
authorizes with HTTP Basic at ``GET https://api.backblazeb2.com/b2api/v4/b2_authorize_account``,
which answers ``accountId``, ``authorizationToken`` and ``apiInfo.storageApi.apiUrl``. Key
management is then, with the token as the ``Authorization`` header:

* ``POST <apiUrl>/b2api/v4/b2_list_keys`` ``{accountId, maxKeyCount, startApplicationKeyId?}``
* ``POST <apiUrl>/b2api/v4/b2_create_key``
  ``{accountId, keyName, capabilities, bucketIds, namePrefix}`` (v4 is the version that takes the
  ``bucketIds`` array), answering the key id, its secret and the restrictions it was given
* ``POST <apiUrl>/b2api/v4/b2_delete_key`` ``{applicationKeyId}``

Nothing else is ever called: no file, version, bucket or retention API. Neither the operator key,
the authorization token, a minted application key nor the cipher pass is ever logged or put into
an error.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
from sqlalchemy import select

from app.core.config import settings
from app.models.companion_installation import CompanionInstallationSecret
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
    BACKUP_APPLICATION_KEY_ATOM,
    BACKUP_CIPHER_PASS_ATOM,
    BACKUP_KEY_ID_ATOM,
    POSTIZ_BACKUP_STEP,
)
from app.services.vault_crypto import decrypt_with_key_version

log = logging.getLogger(__name__)

B2_AUTHORIZE_URL = "https://api.backblazeb2.com/b2api/v4/b2_authorize_account"
B2_API_PATH = "/b2api/v4"
# Exactly these, in this order: list, read and write within the prefix. Nothing that deletes a
# version, bypasses governance, changes retention or touches the bucket itself.
BACKUP_KEY_CAPABILITIES: tuple[str, ...] = ("listFiles", "readFiles", "writeFiles")
# 48 random bytes -> 64 url-safe characters: a pgBackRest cipher pass that needs no quoting.
CIPHER_PASS_BYTES = 48

_TIMEOUT_SECONDS = 20.0

# Test seam (the tenant_purge pattern): tests point this at an httpx.MockTransport standing in for
# B2's native API. None means the default network transport.
_transport_override: httpx.AsyncBaseTransport | None = None


# ---------------------------------------------------------------------------
# Names: every one derived from the installation, so a retry names what an earlier attempt made.
# ---------------------------------------------------------------------------


def backup_key_name(installation_id: UUID | str) -> str:
    return f"pgbr-{installation_id}"


def backup_name_prefix(tenant_id: UUID | str, installation_id: UUID | str) -> str:
    """The key's name prefix, WITH its trailing slash, so no sibling id is a prefix match."""
    return f"{tenant_id}/{installation_id}/"


def backup_repo_path(tenant_id: UUID | str, installation_id: UUID | str) -> str:
    """pgBackRest's ``repo1-path``: object names under it start with ``backup_name_prefix``."""
    return f"/{tenant_id}/{installation_id}"


def backup_generation_repo_path(
    tenant_id: UUID | str, installation_id: UUID | str, generation: int
) -> str:
    """The ``repo1-path`` of a repository started when backups were abandoned (CR-01).

    A generation is a SUBDIRECTORY of the installation's repository, ``/<t>/<i>/r<n>``, so every
    object pgBackRest writes under it still starts with ``backup_name_prefix`` and the existing
    no-delete key and every rehearsal key keep reaching it without being widened or re-minted.
    A sibling such as ``/<t>/<i>-r<n>`` would fall outside that prefix and be refused by B2.
    """
    if generation < 1:
        raise ValueError("a backup repository generation starts at 1")
    return f"{backup_repo_path(tenant_id, installation_id)}/r{generation}"


def backup_stanza(context: LifecycleStepContext) -> str:
    return f"{context.companion_key}-{installation_label_suffix(context)}"


def backup_key_request(
    *,
    account_id: str,
    bucket_id: str,
    tenant_id: UUID | str,
    installation_id: UUID | str,
) -> dict[str, JsonValue]:
    """The whole ``b2_create_key`` body for one installation. Pure, so its limits are testable.

    No ``validDurationInSeconds``: the key lives as long as the installation and is revoked by
    its teardown.
    """
    return {
        "accountId": account_id,
        "keyName": backup_key_name(installation_id),
        "capabilities": list(BACKUP_KEY_CAPABILITIES),
        "bucketIds": [bucket_id],
        "namePrefix": backup_name_prefix(tenant_id, installation_id),
    }


# ---------------------------------------------------------------------------
# B2's native API.
# ---------------------------------------------------------------------------


class _StorageApi(BaseModel):
    model_config = ConfigDict(extra="ignore")

    apiUrl: str = Field(min_length=1)


class _ApiInfo(BaseModel):
    model_config = ConfigDict(extra="ignore")

    storageApi: _StorageApi


class _Authorization(BaseModel):
    model_config = ConfigDict(extra="ignore")

    accountId: str = Field(min_length=1)
    authorizationToken: str = Field(min_length=1)
    apiInfo: _ApiInfo


class _KeyDescription(BaseModel):
    """An application key as B2 describes it (without its secret)."""

    model_config = ConfigDict(extra="ignore")

    applicationKeyId: str = Field(min_length=1)
    keyName: str = ""
    capabilities: list[str] = Field(default_factory=list)
    bucketIds: list[str] | None = None
    namePrefix: str | None = None
    # Milliseconds since the epoch; present only for a key minted with a validity (plan 06-16).
    expirationTimestamp: int | None = None


class _CreatedKey(_KeyDescription):
    applicationKey: str = Field(min_length=1)


class _KeyPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    keys: list[_KeyDescription]
    nextApplicationKeyId: str | None = None


@dataclass(frozen=True, slots=True)
class _B2Session:
    """An authorized operator session: the account, its API URL and token."""

    client: httpx.AsyncClient
    account_id: str
    api_url: str
    token: str

    async def call(self, name: str, body: JsonObject, *, operation: str) -> httpx.Response:
        """POST one key-management call; a named error that never carries a body or a secret."""
        try:
            response = await self.client.post(
                f"{self.api_url}{B2_API_PATH}/{name}",
                json=body,
                headers={"Authorization": self.token},
            )
        except httpx.HTTPError as exc:
            raise CompanionStepError(
                "b2_unreachable", f"{operation} failed: {exc.__class__.__name__}"
            ) from None
        return response


def _b2_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport_override,
        timeout=_TIMEOUT_SECONDS,
        headers={"User-Agent": "iforge-backend/1.0"},
    )


def _refused(operation: str, response: httpx.Response) -> CompanionStepError:
    return CompanionStepError("b2_refused", f"{operation} answered HTTP {response.status_code}")


def _invalid(operation: str) -> CompanionStepError:
    return CompanionStepError("b2_invalid_response", f"{operation} answered no usable result")


async def _authorize(client: httpx.AsyncClient) -> _B2Session:
    """Authorize the operator key (HTTP Basic); the session every key call goes through."""
    try:
        response = await client.get(
            B2_AUTHORIZE_URL,
            auth=(
                settings.companion_backup_b2_key_id,
                settings.companion_backup_b2_application_key,
            ),
        )
    except httpx.HTTPError as exc:
        raise CompanionStepError(
            "b2_unreachable", f"authorize account failed: {exc.__class__.__name__}"
        ) from None
    if not response.is_success:
        raise _refused("authorize account", response)
    try:
        authorization = _Authorization.model_validate_json(response.content)
    except ValidationError:
        raise _invalid("authorize account") from None
    return _B2Session(
        client=client,
        account_id=authorization.accountId,
        api_url=authorization.apiInfo.storageApi.apiUrl.rstrip("/"),
        token=authorization.authorizationToken,
    )


async def _create_key(session: _B2Session, request: dict[str, JsonValue]) -> _CreatedKey:
    response = await session.call("b2_create_key", request, operation="create backup key")
    if not response.is_success:
        raise _refused("create backup key", response)
    try:
        return _CreatedKey.model_validate_json(response.content)
    except ValidationError:
        raise _invalid("create backup key") from None


# One page is the account's whole key list at this deployment's scale, but paging is followed so
# a stale key is never missed; the bound stops a misbehaving API from looping forever.
_LIST_KEYS_PAGE_SIZE = 1000
_LIST_KEYS_MAX_PAGES = 100


async def _find_keys(
    session: _B2Session, *, key_name: str, key_id: str | None = None
) -> list[_KeyDescription]:
    """Every key in the account named ``key_name`` (or with id ``key_id``)."""
    found: list[_KeyDescription] = []
    start: str | None = None
    for _ in range(_LIST_KEYS_MAX_PAGES):
        body: JsonObject = {"accountId": session.account_id, "maxKeyCount": _LIST_KEYS_PAGE_SIZE}
        if start is not None:
            body["startApplicationKeyId"] = start
        response = await session.call("b2_list_keys", body, operation="list backup keys")
        if not response.is_success:
            raise _refused("list backup keys", response)
        try:
            page = _KeyPage.model_validate_json(response.content)
        except ValidationError:
            raise _invalid("list backup keys") from None
        found.extend(
            key for key in page.keys if key.keyName == key_name or key.applicationKeyId == key_id
        )
        if not page.nextApplicationKeyId:
            return found
        start = page.nextApplicationKeyId
    raise _invalid("list backup keys")


async def _delete_key(session: _B2Session, key_id: str) -> None:
    response = await session.call(
        "b2_delete_key", {"applicationKeyId": key_id}, operation="revoke backup key"
    )
    if not response.is_success:
        raise _refused("revoke backup key", response)


def _restricted_as_requested(key: _KeyDescription, request: dict[str, JsonValue]) -> bool:
    """Whether B2 minted exactly the key that was asked for -- nothing wider."""
    return (
        key.capabilities == request["capabilities"]
        and key.bucketIds == request["bucketIds"]
        and key.namePrefix == request["namePrefix"]
    )


# ---------------------------------------------------------------------------
# Settings: checked before the first external call.
# ---------------------------------------------------------------------------


def _require_settings(*, for_teardown: bool) -> None:
    """Refuse by name, before any call, when an operator setting the step needs is unset."""
    required = {
        "COMPANION_BACKUP_B2_KEY_ID": settings.companion_backup_b2_key_id,
        "COMPANION_BACKUP_B2_APPLICATION_KEY": settings.companion_backup_b2_application_key,
    }
    if not for_teardown:
        # Revocation needs only the operator key; minting also needs where the repository lives.
        required["COMPANION_BACKUP_B2_BUCKET_ID"] = settings.companion_backup_b2_bucket_id
        required["COMPANION_BACKUP_B2_BUCKET_NAME"] = settings.companion_backup_b2_bucket_name
        required["COMPANION_BACKUP_B2_S3_ENDPOINT"] = settings.companion_backup_b2_s3_endpoint
        required["COMPANION_BACKUP_B2_REGION"] = settings.companion_backup_b2_region
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise CompanionStepError(
            "backup_settings_missing", f"operator settings are unset: {', '.join(missing)}"
        )


# ---------------------------------------------------------------------------
# The step.
# ---------------------------------------------------------------------------


async def _stored_cipher_pass(context: LifecycleStepContext) -> str | None:
    """This installation's existing cipher pass, if an earlier attempt or operation stored one.

    Never regenerated once it exists: a repository encrypted under it would otherwise become
    unreadable.
    """
    async with context.sessionmaker() as db:
        row = await db.scalar(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == context.installation_id,
                CompanionInstallationSecret.secret_name == BACKUP_CIPHER_PASS_ATOM,
            )
        )
        value = (
            decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)
            if row is not None
            else None
        )
        await db.rollback()
    return value or None


async def provision_backup_key(context: LifecycleStepContext) -> JsonObject:
    """Mint this installation's no-delete backup key; store it with the repository cipher pass.

    A key an earlier attempt minted under the same name is revoked first: its secret was never
    recorded as live, and a key that can still write must not outlive the attempt that made it.
    A step that raises after minting revokes what it minted (the worker never tears down an
    unrecorded step), and a key B2 answers wider than requested is revoked and refused.
    """
    _require_settings(for_teardown=False)
    stanza = backup_stanza(context)
    bucket_id = settings.companion_backup_b2_bucket_id.strip()
    key_name = backup_key_name(context.installation_id)
    async with _b2_client() as client:
        session = await _authorize(client)
        for stale in await _find_keys(session, key_name=key_name):
            await _delete_key(session, stale.applicationKeyId)
        request = backup_key_request(
            account_id=session.account_id,
            bucket_id=bucket_id,
            tenant_id=context.user_id,
            installation_id=context.installation_id,
        )
        created = await _create_key(session, request)
        try:
            if not _restricted_as_requested(created, request):
                raise CompanionStepError(
                    "b2_key_not_restricted",
                    "the minted backup key is not restricted exactly as requested",
                )
            cipher_pass = await _stored_cipher_pass(context) or secrets.token_urlsafe(
                CIPHER_PASS_BYTES
            )
            await store_installation_atoms(
                context,
                {
                    BACKUP_KEY_ID_ATOM: created.applicationKeyId,
                    BACKUP_APPLICATION_KEY_ATOM: created.applicationKey,
                    BACKUP_CIPHER_PASS_ATOM: cipher_pass,
                },
            )
        except Exception:
            await _undo_key(session, context, created.applicationKeyId)
            raise
    log.info(
        "companion backup key provisioned installation_id=%s key_id=%s stanza=%s",
        context.installation_id,
        created.applicationKeyId,
        stanza,
    )
    return {
        "key_id": created.applicationKeyId,
        "stanza": stanza,
        "repo_path": backup_repo_path(context.user_id, context.installation_id),
        "bucket_id": bucket_id,
        "bucket": settings.companion_backup_b2_bucket_name.strip(),
        "s3_endpoint": settings.companion_backup_b2_s3_endpoint.strip(),
        "s3_region": settings.companion_backup_b2_region.strip(),
    }


async def _undo_key(session: _B2Session, context: LifecycleStepContext, key_id: str) -> None:
    """Best-effort revocation of a key the failing step minted; never masks the step's error."""
    try:
        await _delete_key(session, key_id)
    except Exception as exc:  # noqa: BLE001 - the step's own failure is what gets reported.
        log.warning(
            "companion backup key partial provision not revoked installation_id=%s error=%s",
            context.installation_id,
            exc.__class__.__name__,
        )


async def revoke_backup_key(context: LifecycleStepContext, resources: JsonObject) -> None:
    """Revoke the installation's backup key and drop its atoms. Deletes NO backup data.

    Every key with this installation's id or key name is revoked; one already gone is done. No
    file, version or bucket call is ever made, and the cipher pass is kept: the repository stays
    under retention and must stay readable (UI-SPEC: backups kept to their normal schedule).
    """
    _require_settings(for_teardown=True)
    recorded = resources.get("key_id")
    key_id = recorded if isinstance(recorded, str) and recorded else None
    async with _b2_client() as client:
        session = await _authorize(client)
        keys = await _find_keys(
            session, key_name=backup_key_name(context.installation_id), key_id=key_id
        )
        for key in keys:
            await _delete_key(session, key.applicationKeyId)
    await delete_installation_atoms(context, (BACKUP_KEY_ID_ATOM, BACKUP_APPLICATION_KEY_ATOM))
    log.info(
        "companion backup key revoked installation_id=%s revoked=%d",
        context.installation_id,
        len(keys),
    )


BACKUP_REPO_STEP = ProvisioningStep(
    name=POSTIZ_BACKUP_STEP,
    provision=provision_backup_key,
    teardown=revoke_backup_key,
)


# ---------------------------------------------------------------------------
# The rehearsal's read-only key (plan 06-16, D-16, OPS-04).
#
# A PITR rehearsal reads the repository with a key of its own: ``listFiles`` and ``readFiles``
# only -- it can neither write (so a scratch restore can never push WAL into the tenant's
# repository, T-06-16-01) nor delete -- restricted to the installation's prefix, and valid for at
# most an hour. It is distinct from the runtime's write key above and from the operator's
# delete-capable key, and the rehearsal revokes it as soon as its result is recorded.
# ---------------------------------------------------------------------------

REHEARSAL_KEY_CAPABILITIES: tuple[str, ...] = ("listFiles", "readFiles")
REHEARSAL_KEY_MAX_SECONDS = 3600
# B2 computes the expiry on its own clock; this much disagreement with ours is tolerated before a
# key is judged to live longer than requested.
_EXPIRY_CLOCK_SKEW_SECONDS = 300


def rehearsal_key_name(rehearsal_id: UUID | str) -> str:
    return f"pgbr-rh-{rehearsal_id}"


def rehearsal_key_request(
    *,
    account_id: str,
    bucket_id: str,
    tenant_id: UUID | str,
    installation_id: UUID | str,
    rehearsal_id: UUID | str,
    valid_seconds: int = REHEARSAL_KEY_MAX_SECONDS,
) -> dict[str, JsonValue]:
    """The whole ``b2_create_key`` body for one rehearsal's read key. Pure, so it is testable."""
    if not 1 <= valid_seconds <= REHEARSAL_KEY_MAX_SECONDS:
        raise ValueError(f"a rehearsal key is valid for 1..{REHEARSAL_KEY_MAX_SECONDS} seconds")
    return {
        "accountId": account_id,
        "keyName": rehearsal_key_name(rehearsal_id),
        "capabilities": list(REHEARSAL_KEY_CAPABILITIES),
        "bucketIds": [bucket_id],
        "namePrefix": backup_name_prefix(tenant_id, installation_id),
        "validDurationInSeconds": valid_seconds,
    }


@dataclass(frozen=True, slots=True)
class RehearsalReadKey:
    """A minted read-only key. ``application_key`` is its secret and is never logged."""

    key_id: str
    expires_at: datetime
    application_key: str = field(repr=False)


async def mint_rehearsal_key(
    *,
    bucket_id: str,
    tenant_id: UUID | str,
    installation_id: UUID | str,
    rehearsal_id: UUID | str,
) -> RehearsalReadKey:
    """Mint one rehearsal's read-only, prefix-restricted, one-hour key.

    B2's answer is checked: a key that is wider than requested, or that carries no expiry or one
    beyond the hour, is revoked and refused (``b2_key_not_restricted``).
    """
    _require_settings(for_teardown=True)
    async with _b2_client() as client:
        session = await _authorize(client)
        request = rehearsal_key_request(
            account_id=session.account_id,
            bucket_id=bucket_id,
            tenant_id=tenant_id,
            installation_id=installation_id,
            rehearsal_id=rehearsal_id,
        )
        created = await _create_key(session, request)
        latest = datetime.now(UTC) + timedelta(
            seconds=REHEARSAL_KEY_MAX_SECONDS + _EXPIRY_CLOCK_SKEW_SECONDS
        )
        expires_at = (
            datetime.fromtimestamp(created.expirationTimestamp / 1000, tz=UTC)
            if created.expirationTimestamp is not None
            else None
        )
        if (
            not _restricted_as_requested(created, request)
            or expires_at is None
            or expires_at > latest
        ):
            try:
                await _delete_key(session, created.applicationKeyId)
            except Exception as exc:  # noqa: BLE001 - the refusal below is what gets reported.
                log.warning(
                    "companion rehearsal key over-wide and not revoked installation_id=%s error=%s",
                    installation_id,
                    exc.__class__.__name__,
                )
            raise CompanionStepError(
                "b2_key_not_restricted",
                "the minted rehearsal key is not restricted exactly as requested",
            )
    log.info(
        "companion rehearsal key minted installation_id=%s rehearsal_id=%s key_id=%s",
        installation_id,
        rehearsal_id,
        created.applicationKeyId,
    )
    return RehearsalReadKey(
        key_id=created.applicationKeyId,
        application_key=created.applicationKey,
        expires_at=expires_at,
    )


async def revoke_rehearsal_key(*, key_id: str, rehearsal_id: UUID | str) -> int:
    """Revoke a rehearsal's key by id or by its name; one already gone (or expired) is done.

    Returns how many keys were revoked. Never touches a file, a version or the bucket.
    """
    _require_settings(for_teardown=True)
    async with _b2_client() as client:
        session = await _authorize(client)
        keys = await _find_keys(session, key_name=rehearsal_key_name(rehearsal_id), key_id=key_id)
        for key in keys:
            await _delete_key(session, key.applicationKeyId)
    log.info("companion rehearsal key revoked rehearsal_id=%s revoked=%d", rehearsal_id, len(keys))
    return len(keys)


__all__ = [
    "BACKUP_KEY_CAPABILITIES",
    "BACKUP_REPO_STEP",
    "REHEARSAL_KEY_CAPABILITIES",
    "REHEARSAL_KEY_MAX_SECONDS",
    "RehearsalReadKey",
    "backup_key_name",
    "backup_generation_repo_path",
    "backup_key_request",
    "backup_name_prefix",
    "backup_repo_path",
    "backup_stanza",
    "mint_rehearsal_key",
    "provision_backup_key",
    "rehearsal_key_name",
    "rehearsal_key_request",
    "revoke_backup_key",
    "revoke_rehearsal_key",
]
