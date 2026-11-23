import hashlib
import hmac
import json
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.main import app
from app.models.hosted_runtime import HostedRuntimeSecret
from app.services.hosted_runtime_secrets import (
    hosted_runtime_secret_values_changed,
    runtime_secret_values_idempotency_identity,
    sync_hosted_runtime_secret_values,
)
from app.services.vault_crypto import decrypt, encrypt
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state
from tests.hosted_runtime_fixtures import (
    CANONICAL_CODEX_TOOL_PROVIDER_ID,
    ensure_canonical_codex_tool_provider,
)


def test_runtime_secret_values_idempotency_identity_is_stable_and_domain_separated() -> None:
    values = {
        "secret://clawdi/auth-token": SecretStr("same-value"),
        "secret://runtime/openclaw/gateway-token": SecretStr("gateway-value"),
    }

    first = runtime_secret_values_idempotency_identity(values)
    reordered = runtime_secret_values_idempotency_identity(dict(reversed(values.items())))
    changed = runtime_secret_values_idempotency_identity(
        {
            **values,
            "secret://clawdi/auth-token": SecretStr("different-value"),
        }
    )

    assert first == reordered
    assert first != changed
    canonical_payload = json.dumps(
        {key: value.get_secret_value() for key, value in values.items()},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    direct_raw_key_hmac = hmac.new(
        bytes.fromhex(settings.vault_encryption_key),
        canonical_payload,
        hashlib.sha256,
    ).hexdigest()
    assert first != direct_raw_key_hmac
    assert first not in {secret.get_secret_value() for secret in values.values()}
    assert settings.vault_encryption_key not in first


def test_hosted_runtime_secret_compare_rejects_unknown_key_version() -> None:
    ciphertext, nonce = encrypt("same-value")
    row = HostedRuntimeSecret(
        id=uuid4(),
        environment_id=uuid4(),
        secret_ref="secret://clawdi/auth-token",
        encrypted_value=ciphertext,
        nonce=nonce,
        key_version="vault.future",
    )

    with pytest.raises(RuntimeError, match="Unsupported hosted runtime secret key version"):
        hosted_runtime_secret_values_changed(
            [row],
            {"secret://clawdi/auth-token": SecretStr("same-value")},
        )


# --- WINDOWS #200 / D-10: replace without reading --------------------------------------------
#
# A vault key loss (or a row written under a key that has since been rotated away) leaves a
# `HostedRuntimeSecret` whose ciphertext this process cannot authenticate. Before this change the
# upsert compared the stored value to the desired one by DECRYPTING it first, so the one request
# that could heal the row raised `InvalidTag` and returned a 500 instead. These tests seed exactly
# that row: real AES-GCM ciphertext produced under a second, unrelated 32-byte key.

_ADMIN_KEY = "test-hosted-runtime-secrets-admin-secret"
_ADMIN_AUTH = {"X-Admin-Key": _ADMIN_KEY}
_CLERK_ISSUER = "https://hosted-runtime-secrets.clerk.example.test"
_AUTH_TOKEN_REF = "secret://clawdi/auth-token"
_GATEWAY_TOKEN_REF = "secret://runtime/openclaw/gateway-token"
_ORIGINAL_SECRET_VALUES = {
    _AUTH_TOKEN_REF: "original-runtime-auth-token",
    _GATEWAY_TOKEN_REF: "original-openclaw-gateway-token",
}
_CODEX_TOOLS = {
    "codex": {
        "enabled": True,
        "provider_id": CANONICAL_CODEX_TOOL_PROVIDER_ID,
        "primary_model": {"provider_id": CANONICAL_CODEX_TOOL_PROVIDER_ID, "model": "gpt-5.5"},
    }
}


@contextmanager
def _foreign_vault_key() -> Iterator[None]:
    """Encrypt under a different, valid 32-byte key for the duration of the block."""
    original = settings.vault_encryption_key
    foreign = secrets.token_hex(32)
    assert foreign != original
    settings.vault_encryption_key = foreign
    try:
        yield
    finally:
        settings.vault_encryption_key = original


def foreign_ciphertext(plaintext: str) -> tuple[bytes, bytes]:
    """Real AES-GCM ciphertext the configured key cannot authenticate (``InvalidTag``)."""
    with _foreign_vault_key():
        return encrypt(plaintext)


def _unreadable_row(secret_ref: str, plaintext: str) -> HostedRuntimeSecret:
    ciphertext, nonce = foreign_ciphertext(plaintext)
    return HostedRuntimeSecret(
        id=uuid4(),
        environment_id=uuid4(),
        secret_ref=secret_ref,
        encrypted_value=ciphertext,
        nonce=nonce,
        key_version="vault.v1",
    )


async def _make_row_unreadable(db_session, *, agent_id: UUID, secret_ref: str) -> None:
    row = (
        await db_session.execute(
            select(HostedRuntimeSecret).where(
                HostedRuntimeSecret.environment_id == agent_id,
                HostedRuntimeSecret.secret_ref == secret_ref,
            )
        )
    ).scalar_one()
    ciphertext, nonce = foreign_ciphertext(_ORIGINAL_SECRET_VALUES[secret_ref])
    row.encrypted_value = ciphertext
    row.nonce = nonce
    await db_session.commit()
    with pytest.raises(InvalidTag):
        decrypt(row.encrypted_value, row.nonce)


async def _stored_plaintext(db_session, *, agent_id: UUID) -> dict[str, str]:
    db_session.expire_all()
    rows = (
        (
            await db_session.execute(
                select(HostedRuntimeSecret)
                .where(HostedRuntimeSecret.environment_id == agent_id)
                .order_by(HostedRuntimeSecret.secret_ref)
            )
        )
        .scalars()
        .all()
    )
    return {row.secret_ref: decrypt(row.encrypted_value, row.nonce) for row in rows}


@pytest_asyncio.fixture
async def secrets_client(db_session, seed_user) -> AsyncIterator[httpx.AsyncClient]:
    """Admin-keyed client that turns an unhandled server exception into the 500 a caller sees.

    ``raise_app_exceptions=False`` is deliberate: the WINDOWS #200 defect is an uncaught
    ``InvalidTag`` escaping the route, and what an operator observes is the 500 -- so that is
    what the tests assert against, rather than an exception re-raised into the test body.
    """

    async def _override_get_session():
        yield db_session

    original_admin_key = settings.admin_api_key
    original_clerk_issuer = settings.clerk_jwt_issuer
    settings.admin_api_key = _ADMIN_KEY
    settings.clerk_jwt_issuer = _CLERK_ISSUER
    app.dependency_overrides[get_session] = _override_get_session
    # Upstream moved the admin and platform routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_control_session, None)
        settings.admin_api_key = original_admin_key
        settings.clerk_jwt_issuer = original_clerk_issuer


def _runtime_payload(
    agent_id: UUID, *, generation: int, secret_values: dict[str, str]
) -> dict[str, object]:
    return {
        "deployment_id": f"dep-{agent_id.hex[:12]}",
        "instance_id": f"iid-{agent_id.hex[:12]}",
        "generation": generation,
        "cli_package_spec": "clawdi@1.2.3-test",
        "locale": {"language": "en", "timezone": "UTC"},
        "system": {},
        "runtimes": {
            "openclaw": {
                "enabled": True,
                "providerMode": "configured",
                "provider_ids": [CANONICAL_CODEX_TOOL_PROVIDER_ID],
                "primary_model": {
                    "provider_id": CANONICAL_CODEX_TOOL_PROVIDER_ID,
                    "model": "gpt-5.5",
                },
                "install": {"source": "official"},
            }
        },
        "live_sync": {"enabled": False, "agents": []},
        "recovery": {"cacheManifest": True, "allowOfflineBoot": True},
        "tools": _CODEX_TOOLS,
        "secretValues": dict(secret_values),
    }


def test_values_changed_is_true_for_an_unreadable_row_even_when_desired_equals_original() -> None:
    row = _unreadable_row(_AUTH_TOKEN_REF, "same-value")

    assert (
        hosted_runtime_secret_values_changed([row], {_AUTH_TOKEN_REF: SecretStr("same-value")})
        is True
    )


def test_values_changed_still_raises_runtime_error_when_the_vault_key_is_missing() -> None:
    ciphertext, nonce = encrypt("same-value")
    row = HostedRuntimeSecret(
        id=uuid4(),
        environment_id=uuid4(),
        secret_ref=_AUTH_TOKEN_REF,
        encrypted_value=ciphertext,
        nonce=nonce,
        key_version="vault.v1",
    )
    original = settings.vault_encryption_key
    settings.vault_encryption_key = ""
    try:
        with pytest.raises(RuntimeError, match="VAULT_ENCRYPTION_KEY not configured"):
            hosted_runtime_secret_values_changed([row], {_AUTH_TOKEN_REF: SecretStr("same-value")})
    finally:
        settings.vault_encryption_key = original


def test_unreadable_refs_names_exactly_the_unreadable_rows_in_sorted_order() -> None:
    from app.services.hosted_runtime_secrets import unreadable_hosted_runtime_secret_refs

    readable_ciphertext, readable_nonce = encrypt("readable-value")
    readable = HostedRuntimeSecret(
        id=uuid4(),
        environment_id=uuid4(),
        secret_ref="secret://runtime/readable",
        encrypted_value=readable_ciphertext,
        nonce=readable_nonce,
        key_version="vault.v1",
    )
    rows = [
        _unreadable_row("secret://runtime/zeta", "z"),
        readable,
        _unreadable_row("secret://runtime/alpha", "a"),
    ]

    assert unreadable_hosted_runtime_secret_refs(rows) == [
        "secret://runtime/alpha",
        "secret://runtime/zeta",
    ]


@pytest.mark.asyncio
async def test_sync_reencrypts_an_unreadable_row_it_keeps_and_deletes_unlisted_rows(
    db_session, seed_user
) -> None:
    env = await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"unreadable-sync-{uuid4().hex[:8]}",
        machine_name="unreadable-sync",
    )
    await create_test_hosted_runtime_state(db_session, env, runtime_name="openclaw")
    kept = _unreadable_row(_AUTH_TOKEN_REF, "old-auth-token")
    dropped = _unreadable_row(_GATEWAY_TOKEN_REF, "old-gateway-token")
    kept.environment_id = env.id
    dropped.environment_id = env.id
    db_session.add_all([kept, dropped])
    await db_session.commit()

    await sync_hosted_runtime_secret_values(
        db_session,
        environment_id=env.id,
        rows=[kept, dropped],
        desired={_AUTH_TOKEN_REF: SecretStr("new-auth-token")},
    )
    await db_session.commit()

    assert await _stored_plaintext(db_session, agent_id=env.id) == {
        _AUTH_TOKEN_REF: "new-auth-token"
    }


@pytest.mark.asyncio
async def test_platform_upsert_replaces_an_unreadable_secret_without_reading_it(
    secrets_client, db_session, seed_user
) -> None:
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    owner = {"kind": "clerk", "ref": seed_user.clerk_id}
    agent_id = uuid4()
    created = await secrets_client.post(
        "/v1/platform/agents",
        headers={**_ADMIN_AUTH, "Idempotency-Key": f"unreadable-agent-{agent_id}"},
        json={
            "owner": owner,
            "agent_id": str(agent_id),
            "machine_id": f"machine-{agent_id.hex[:8]}",
            "machine_name": "unreadable-platform-agent",
            "agent_type": "openclaw",
            "agent_version": "1.0.0",
            "os_name": "linux",
        },
    )
    assert created.status_code == 200, created.text
    first = await secrets_client.put(
        f"/v1/platform/agents/{agent_id}/runtime-state",
        headers={**_ADMIN_AUTH, "Idempotency-Key": f"unreadable-first-{agent_id}"},
        json={
            "owner": owner,
            **_runtime_payload(agent_id, generation=1, secret_values=_ORIGINAL_SECRET_VALUES),
        },
    )
    assert first.status_code == 200, first.text
    await _make_row_unreadable(db_session, agent_id=agent_id, secret_ref=_AUTH_TOKEN_REF)

    healed_values = {**_ORIGINAL_SECRET_VALUES, _AUTH_TOKEN_REF: "replacement-auth-token"}
    healed = await secrets_client.put(
        f"/v1/platform/agents/{agent_id}/runtime-state",
        headers={**_ADMIN_AUTH, "Idempotency-Key": f"unreadable-heal-{agent_id}"},
        json={
            "owner": owner,
            **_runtime_payload(agent_id, generation=2, secret_values=healed_values),
        },
    )

    assert healed.status_code == 200, healed.text
    assert await _stored_plaintext(db_session, agent_id=agent_id) == healed_values


@pytest.mark.asyncio
async def test_admin_upsert_replaces_an_unreadable_secret_without_reading_it(
    secrets_client, db_session, seed_user
) -> None:
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    agent_id = uuid4()
    created = await secrets_client.post(
        "/v1/admin/agents",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": seed_user.clerk_id,
            "agent_id": str(agent_id),
            "machine_id": f"unreadable-admin-{agent_id.hex[:8]}",
            "machine_name": "unreadable-admin-agent",
            "agent_type": "openclaw",
            "agent_version": "1.0.0",
            "os_name": "linux",
        },
    )
    assert created.status_code == 200, created.text
    first = await secrets_client.put(
        f"/v1/admin/agents/{agent_id}/runtime-state",
        headers=_ADMIN_AUTH,
        json=_runtime_payload(agent_id, generation=1, secret_values=_ORIGINAL_SECRET_VALUES),
    )
    assert first.status_code == 200, first.text
    await _make_row_unreadable(db_session, agent_id=agent_id, secret_ref=_AUTH_TOKEN_REF)

    healed_values = {**_ORIGINAL_SECRET_VALUES, _AUTH_TOKEN_REF: "replacement-auth-token"}
    healed = await secrets_client.put(
        f"/v1/admin/agents/{agent_id}/runtime-state",
        headers=_ADMIN_AUTH,
        json=_runtime_payload(agent_id, generation=2, secret_values=healed_values),
    )

    assert healed.status_code == 200, healed.text
    assert await _stored_plaintext(db_session, agent_id=agent_id) == healed_values


# --- F6: the manifest stays fail-closed and NAMES what is unreadable --------------------------
#
# The manifest must never become a partial one that silently drops a secret it cannot read: an
# agent booting without its gateway token is worse than one that does not boot. What changes is
# that the failure now carries the refs an operator has to recover, by name only.

_RUNTIME_SECRET_ERROR = "Hosted runtime secret source is invalid"
_CHANNEL_SECRET_ERROR = "Hosted runtime channel secret source is invalid"
_READABLE_PLAINTEXTS = {
    "runtime-auth": "readable-runtime-auth-token",
    "gateway": "readable-openclaw-gateway-token",
    "provider": "sk-readable-provider-key",
    "channel": "readable-channel-agent-token",
}


def _really_encrypted_batch():
    """The runtime-source unit batch, with every ciphertext replaced by real AES-GCM output."""
    from tests.test_runtime_source import ENV_ID, _batch

    batch = _batch()
    auth_secret, gateway_secret = batch.runtime_secrets[ENV_ID]
    auth_secret.encrypted_value, auth_secret.nonce = encrypt(_READABLE_PLAINTEXTS["runtime-auth"])
    gateway_secret.encrypted_value, gateway_secret.nonce = encrypt(_READABLE_PLAINTEXTS["gateway"])
    for payload in batch.auth_payloads.values():
        payload.encrypted_payload, payload.nonce = encrypt(_READABLE_PLAINTEXTS["provider"])
    for _account, link in batch.channels[ENV_ID]:
        link.encrypted_agent_token, link.agent_token_nonce = encrypt(
            _READABLE_PLAINTEXTS["channel"]
        )
    return batch, ENV_ID


def _render_with_secrets(batch, environment_id):
    from app.services.runtime_source import render_runtime_source

    return render_runtime_source(
        batch,
        environment_id=environment_id,
        public_api_url="https://cloud.test/",
        vault_key_identity="vault-key-generation-1",
        decrypt_secrets=True,
    )


def _assert_names_refs_and_no_value(error: Exception, *, message: str, refs: tuple[str, ...]):
    from app.services.runtime_source import RuntimeSecretUnreadableError, RuntimeSourceError

    assert isinstance(error, RuntimeSecretUnreadableError)
    assert isinstance(error, RuntimeSourceError)
    assert error.secret_refs == refs
    assert str(error) == message
    for plaintext in _READABLE_PLAINTEXTS.values():
        assert plaintext not in str(error)
        assert plaintext not in repr(error.args)


def test_really_encrypted_batch_renders_every_secret_when_all_rows_are_readable() -> None:
    batch, environment_id = _really_encrypted_batch()

    source = _render_with_secrets(batch, environment_id)

    assert (
        source.secret_values["secret://clawdi/auth-token"] == _READABLE_PLAINTEXTS["runtime-auth"]
    )


def test_manifest_render_names_an_unreadable_runtime_secret_and_stays_fail_closed() -> None:
    batch, environment_id = _really_encrypted_batch()
    _auth_secret, gateway_secret = batch.runtime_secrets[environment_id]
    gateway_secret.encrypted_value, gateway_secret.nonce = foreign_ciphertext("lost-gateway-token")

    with pytest.raises(Exception) as raised:
        _render_with_secrets(batch, environment_id)

    _assert_names_refs_and_no_value(
        raised.value,
        message=_RUNTIME_SECRET_ERROR,
        refs=(_GATEWAY_TOKEN_REF,),
    )


def test_manifest_render_names_every_unreadable_runtime_secret_in_sorted_order() -> None:
    batch, environment_id = _really_encrypted_batch()
    auth_secret, gateway_secret = batch.runtime_secrets[environment_id]
    gateway_secret.encrypted_value, gateway_secret.nonce = foreign_ciphertext("lost-gateway")
    auth_secret.encrypted_value, auth_secret.nonce = foreign_ciphertext("lost-auth")

    with pytest.raises(Exception) as raised:
        _render_with_secrets(batch, environment_id)

    _assert_names_refs_and_no_value(
        raised.value,
        message=_RUNTIME_SECRET_ERROR,
        refs=tuple(sorted((_AUTH_TOKEN_REF, _GATEWAY_TOKEN_REF))),
    )


def test_manifest_render_names_an_unreadable_channel_agent_token() -> None:
    batch, environment_id = _really_encrypted_batch()
    ((_account, link),) = batch.channels[environment_id]
    link.encrypted_agent_token, link.agent_token_nonce = foreign_ciphertext("lost-channel-token")

    with pytest.raises(Exception) as raised:
        _render_with_secrets(batch, environment_id)

    (channel_ref,) = raised.value.secret_refs
    assert channel_ref.startswith("secret://channels/telegram/")
    assert channel_ref.endswith("/agent-token")
    _assert_names_refs_and_no_value(
        raised.value,
        message=_CHANNEL_SECRET_ERROR,
        refs=(channel_ref,),
    )


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_runtime_manifest_route_still_fails_closed_for_an_unreadable_secret(
    secrets_client, db_session, seed_user
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.core.auth import AuthContext, get_auth
    from app.core.database import RuntimeManifestSessions, engine, get_runtime_manifest_sessions
    from app.models.api_key import ApiKey
    from app.routes import runtime as runtime_routes
    from app.services.runtime_source import (
        RUNTIME_AGENT_PLUGIN_GITHUB_RELEASE_SOURCE_CAPABILITY,
        RUNTIME_AGENT_PLUGINS_MANIFEST_CAPABILITY,
        RUNTIME_BUNDLE_V2_MEDIA_TYPE,
        RUNTIME_CAPABILITIES_HEADER,
    )

    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    agent_id = uuid4()
    created = await secrets_client.post(
        "/v1/admin/agents",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": seed_user.clerk_id,
            "agent_id": str(agent_id),
            "machine_id": f"unreadable-manifest-{agent_id.hex[:8]}",
            "machine_name": "unreadable-manifest-agent",
            "agent_type": "openclaw",
            "agent_version": "1.0.0",
            "os_name": "linux",
        },
    )
    assert created.status_code == 200, created.text
    first = await secrets_client.put(
        f"/v1/admin/agents/{agent_id}/runtime-state",
        headers=_ADMIN_AUTH,
        json=_runtime_payload(agent_id, generation=1, secret_values=_ORIGINAL_SECRET_VALUES),
    )
    assert first.status_code == 200, first.text
    await _make_row_unreadable(db_session, agent_id=agent_id, secret_ref=_GATEWAY_TOKEN_REF)

    api_key = ApiKey(user_id=seed_user.id, environment_id=agent_id, label="unreadable-manifest")

    async def _override_session():
        yield db_session

    async def _override_auth():
        return AuthContext(user=seed_user, api_key=api_key)

    # Upstream #1386/#1388: the manifest route authenticates through its own dependency on a
    # reserved session pair, and without a capability header it answers with the legacy
    # bootstrap manifest, which reads no secrets.
    async def _override_manifest_sessions():
        yield RuntimeManifestSessions(
            auth=db_session,
            snapshots=async_sessionmaker(engine, expire_on_commit=False),
        )

    await db_session.commit()
    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_auth] = _override_auth
    app.dependency_overrides[runtime_routes._get_runtime_manifest_auth] = _override_auth
    app.dependency_overrides[get_runtime_manifest_sessions] = _override_manifest_sessions
    try:
        response = await secrets_client.get(
            "/v1/runtime/manifest",
            headers={
                "Accept": RUNTIME_BUNDLE_V2_MEDIA_TYPE,
                RUNTIME_CAPABILITIES_HEADER: ", ".join(
                    (
                        "provider-identity-v1",
                        RUNTIME_AGENT_PLUGINS_MANIFEST_CAPABILITY,
                        RUNTIME_AGENT_PLUGIN_GITHUB_RELEASE_SOURCE_CAPABILITY,
                    )
                ),
            },
        )
    finally:
        app.dependency_overrides.pop(runtime_routes._get_runtime_manifest_auth, None)
        app.dependency_overrides.pop(get_runtime_manifest_sessions, None)

    assert response.status_code == 409, response.text
    assert response.json() == {"detail": _RUNTIME_SECRET_ERROR}
    for plaintext in _ORIGINAL_SECRET_VALUES.values():
        assert plaintext not in response.text
