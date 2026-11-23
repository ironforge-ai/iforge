"""Operator recovery of hosted runtime secrets the vault key can no longer read (OPS-07).

A lost or rotated vault key used to wedge an agent: its manifest failed closed on a secret nobody
could decrypt, and the only fix was editing the database by hand. These tests pin the two admin
routes that replace that hand edit:

- ``GET  /v1/admin/agents/{agent_id}/runtime-secrets/health`` lists refs, key versions and a
  readable flag -- never a value.
- ``POST /v1/admin/agents/{agent_id}/runtime-secrets/recover`` deletes or replaces NAMED refs that
  are currently unreadable, refuses a readable ref with 409 before touching anything, serializes
  concurrent calls on the rows' ``FOR UPDATE`` lock, and writes one audit event of ref names.

"Unreadable" is always real here: the rows carry AES-GCM ciphertext produced under a second,
unrelated 32-byte key, so the configured key raises ``InvalidTag``. Plan 06-25 extends this module
with vault key rotation (the keyring and ``POST .../runtime-secrets/rotate``) and with recovery of
a companion installation's unreadable secrets.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from cryptography.exceptions import InvalidTag
from httpx import ASGITransport
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.main import app
from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.models.hosted_runtime import HostedRuntimeSecret
from app.models.vault import Vault, VaultItem
from app.services import vault_crypto
from app.services.companion_backup_repo import backup_name_prefix, provision_backup_key
from app.services.companion_media_store import provision_media_bucket
from app.services.companion_postiz import postiz_step_handles
from app.services.hosted_runtime_secrets import sync_hosted_runtime_secret_values
from app.services.runtime_source import load_runtime_source_batch, render_runtime_source
from app.services.vault_crypto import decrypt
from tests import test_companion_media_store as media_tests
from tests.doc_contract import REPO_ROOT
from tests.hosted_runtime_fixtures import (
    CANONICAL_CODEX_TOOL_PROVIDER_ID,
    ensure_canonical_codex_tool_provider,
)
from tests.test_companion_backup_repo import _wire_b2 as wire_b2
from tests.test_companion_edge import _record_step as record_step
from tests.test_companion_edge import _step_context as edge_step_context
from tests.test_companion_projection import ZONE, _agent_with_runtime, _installation
from tests.test_hosted_runtime_secrets import foreign_ciphertext

_ADMIN_KEY = "test-vault-key-rotation-admin-secret"
_ADMIN_AUTH = {"X-Admin-Key": _ADMIN_KEY}
_CLERK_ISSUER = "https://vault-key-rotation.clerk.example.test"
_AUTH_TOKEN_REF = "secret://clawdi/auth-token"
_GATEWAY_TOKEN_REF = "secret://runtime/openclaw/gateway-token"
_ORIGINAL_VALUES = {
    _AUTH_TOKEN_REF: "original-runtime-auth-token",
    _GATEWAY_TOKEN_REF: "original-openclaw-gateway-token",
}
_REPLACEMENT_VALUE = "operator-supplied-replacement-token"
_CODEX_TOOLS = {
    "codex": {
        "enabled": True,
        "provider_id": CANONICAL_CODEX_TOOL_PROVIDER_ID,
        "primary_model": {"provider_id": CANONICAL_CODEX_TOOL_PROVIDER_ID, "model": "gpt-5.5"},
    }
}
_RECOVER_ACTION = "hosted_runtime_secrets.recover"


def _health_path(agent_id: UUID) -> str:
    return f"/v1/admin/agents/{agent_id}/runtime-secrets/health"


def _recover_path(agent_id: UUID) -> str:
    return f"/v1/admin/agents/{agent_id}/runtime-secrets/recover"


@asynccontextmanager
async def _admin_client(get_db: Callable[[], AsyncIterator[AsyncSession]]):
    original_admin_key = settings.admin_api_key
    original_clerk_issuer = settings.clerk_jwt_issuer
    settings.admin_api_key = _ADMIN_KEY
    settings.clerk_jwt_issuer = _CLERK_ISSUER
    app.dependency_overrides[get_session] = get_db
    # Upstream moved the admin and platform routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = get_db
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


@pytest_asyncio.fixture
async def admin_client(db_session) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_get_session():
        yield db_session

    async with _admin_client(_override_get_session) as client:
        yield client


def _runtime_payload(agent_id: UUID) -> dict[str, object]:
    return {
        "deployment_id": f"dep-{agent_id.hex[:12]}",
        "instance_id": f"iid-{agent_id.hex[:12]}",
        "generation": 1,
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
        "secretValues": dict(_ORIGINAL_VALUES),
    }


async def _deploy_agent(client: httpx.AsyncClient, db_session, seed_user) -> UUID:
    """An agent with runtime state and two readable secrets, through the real admin routes."""
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    await db_session.commit()
    agent_id = uuid4()
    created = await client.post(
        "/v1/admin/agents",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": seed_user.clerk_id,
            "agent_id": str(agent_id),
            "machine_id": f"vault-recover-{agent_id.hex[:8]}",
            "machine_name": "vault-recover-agent",
            "agent_type": "openclaw",
            "agent_version": "1.0.0",
            "os_name": "linux",
        },
    )
    assert created.status_code == 200, created.text
    upserted = await client.put(
        f"/v1/admin/agents/{agent_id}/runtime-state",
        headers=_ADMIN_AUTH,
        json=_runtime_payload(agent_id),
    )
    assert upserted.status_code == 200, upserted.text
    return agent_id


async def _secret_rows(db_session, agent_id: UUID) -> dict[str, HostedRuntimeSecret]:
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(HostedRuntimeSecret).where(HostedRuntimeSecret.environment_id == agent_id)
        )
    ).scalars()
    return {row.secret_ref: row for row in rows}


async def _make_unreadable(db_session, agent_id: UUID, secret_ref: str) -> bytes:
    """Re-encrypt one row under a foreign key; returns its (now unreadable) ciphertext."""
    row = (await _secret_rows(db_session, agent_id))[secret_ref]
    row.encrypted_value, row.nonce = foreign_ciphertext(_ORIGINAL_VALUES[secret_ref])
    await db_session.commit()
    with pytest.raises(InvalidTag):
        decrypt(row.encrypted_value, row.nonce)
    return row.encrypted_value


async def _row_identities(db_session, agent_id: UUID) -> dict[str, tuple[bytes, bytes, str]]:
    return {
        ref: (row.encrypted_value, row.nonce, row.key_version)
        for ref, row in (await _secret_rows(db_session, agent_id)).items()
    }


async def _recover_events(db_session, agent_id: UUID) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent)
                .where(
                    ControlPlaneAuditEvent.action == _RECOVER_ACTION,
                    ControlPlaneAuditEvent.environment_id == agent_id,
                )
                .order_by(ControlPlaneAuditEvent.created_at)
            )
        ).scalars()
    )


async def _render_manifest(db_session, agent_id: UUID):
    batch = await load_runtime_source_batch(db_session, environment_ids=[agent_id])
    return render_runtime_source(
        batch,
        environment_id=agent_id,
        public_api_url="https://cloud.test",
        vault_key_identity="test-key-version",
        decrypt_secrets=True,
    )


@pytest.mark.asyncio
async def test_health_lists_refs_with_readable_flags_and_no_values(
    admin_client, db_session, seed_user
) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)

    response = await admin_client.get(_health_path(agent_id), headers=_ADMIN_AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "agent_id": str(agent_id),
        "secrets": [
            {"secret_ref": _AUTH_TOKEN_REF, "key_version": "vault.v1", "readable": True},
            {"secret_ref": _GATEWAY_TOKEN_REF, "key_version": "vault.v1", "readable": False},
        ],
    }
    for plaintext in _ORIGINAL_VALUES.values():
        assert plaintext not in response.text


@pytest.mark.asyncio
async def test_recover_replaces_an_unreadable_secret_without_decrypting_it(
    admin_client, db_session, seed_user, monkeypatch
) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    unreadable_ciphertext = await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)

    # Spy on the decrypt primitive, recording what each call produced. Every read of a vault.v1
    # row -- legacy or through the keyring's `decrypt_with_key_version` -- goes through it.
    real_decrypt = vault_crypto.decrypt
    calls: list[tuple[bytes, str]] = []

    def spy_decrypt(ciphertext: bytes, nonce: bytes) -> str:
        try:
            plaintext = real_decrypt(ciphertext, nonce)
        except InvalidTag:
            calls.append((ciphertext, "InvalidTag"))
            raise
        calls.append((ciphertext, "plaintext"))
        return plaintext

    monkeypatch.setattr(vault_crypto, "decrypt", spy_decrypt)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={"replace": {_GATEWAY_TOKEN_REF: _REPLACEMENT_VALUE}},
    )
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert response.json() == {
        "agent_id": str(agent_id),
        "deleted": [],
        "replaced": [_GATEWAY_TOKEN_REF],
        "already_recovered": [],
        "remaining_unreadable": [],
        # Plan 06-25's companion actions: additive, empty when none was asked for.
        "regenerated": [],
        "reprovisioned": [],
        "abandoned_backups": [],
    }
    assert _REPLACEMENT_VALUE not in response.text
    rows = await _secret_rows(db_session, agent_id)
    replaced = rows[_GATEWAY_TOKEN_REF]
    assert decrypt(replaced.encrypted_value, replaced.nonce) == _REPLACEMENT_VALUE
    assert replaced.key_version == "vault.v1"
    # The replaced row's ciphertext never yielded plaintext: every touch of it was the
    # authentication probe that CLASSIFIED it unreadable, and it raised. The route then wrote
    # the new value with encrypt alone -- it never read back the ciphertext it wrote.
    touches_of_replaced_row = [outcome for ct, outcome in calls if ct == unreadable_ciphertext]
    assert touches_of_replaced_row, "the route must classify the row before replacing it"
    assert set(touches_of_replaced_row) == {"InvalidTag"}
    assert not [ct for ct, _ in calls if ct == replaced.encrypted_value]


@pytest.mark.asyncio
async def test_recover_deletes_a_named_unreadable_secret(
    admin_client, db_session, seed_user
) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
    with pytest.raises(Exception, match="Hosted runtime secret source is invalid"):
        await _render_manifest(db_session, agent_id)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={"delete_refs": [_GATEWAY_TOKEN_REF]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == [_GATEWAY_TOKEN_REF]
    assert response.json()["remaining_unreadable"] == []
    assert set(await _secret_rows(db_session, agent_id)) == {_AUTH_TOKEN_REF}
    source = await _render_manifest(db_session, agent_id)
    assert source.secret_values[_AUTH_TOKEN_REF] == _ORIGINAL_VALUES[_AUTH_TOKEN_REF]
    assert _GATEWAY_TOKEN_REF not in source.secret_values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"delete_refs": [_AUTH_TOKEN_REF, _GATEWAY_TOKEN_REF]},
        {"delete_refs": [_GATEWAY_TOKEN_REF], "replace": {_AUTH_TOKEN_REF: _REPLACEMENT_VALUE}},
    ],
    ids=["readable-in-delete_refs", "readable-in-replace"],
)
async def test_recover_refuses_a_readable_ref(admin_client, db_session, seed_user, body) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
    before = await _row_identities(db_session, agent_id)

    response = await admin_client.post(_recover_path(agent_id), headers=_ADMIN_AUTH, json=body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "runtime_secret_readable",
        "secret_refs": [_AUTH_TOKEN_REF],
    }
    assert await _row_identities(db_session, agent_id) == before
    assert await _recover_events(db_session, agent_id) == []


@pytest.mark.asyncio
async def test_recover_unknown_ref_is_404(admin_client, db_session, seed_user) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
    before = await _row_identities(db_session, agent_id)
    missing_ref = "secret://runtime/never-stored"

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={"delete_refs": [_GATEWAY_TOKEN_REF, missing_ref]},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == {
        "code": "runtime_secret_not_found",
        "secret_refs": [missing_ref],
    }
    assert await _row_identities(db_session, agent_id) == before


@pytest.mark.asyncio
async def test_recover_unknown_agent_is_404(admin_client) -> None:
    unknown = uuid4()

    recover = await admin_client.post(
        _recover_path(unknown),
        headers=_ADMIN_AUTH,
        json={"delete_refs": [_GATEWAY_TOKEN_REF]},
    )
    health = await admin_client.get(_health_path(unknown), headers=_ADMIN_AUTH)

    assert recover.status_code == 404, recover.text
    assert health.status_code == 404, health.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"delete_refs": [], "replace": {}},
        {
            "delete_refs": [_GATEWAY_TOKEN_REF],
            "replace": {_GATEWAY_TOKEN_REF: _REPLACEMENT_VALUE},
        },
        {"delete_refs": ["not-a-secret-ref"]},
    ],
    ids=["empty", "empty-lists", "ref-in-both", "non-canonical-ref"],
)
async def test_recover_rejects_an_invalid_request_body(admin_client, body) -> None:
    response = await admin_client.post(_recover_path(uuid4()), headers=_ADMIN_AUTH, json=body)

    assert response.status_code == 422, response.text
    assert _REPLACEMENT_VALUE not in response.text


@pytest.mark.asyncio
async def test_recover_writes_one_audit_event_without_values(
    admin_client, db_session, seed_user
) -> None:
    owner_id = seed_user.id
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
    await _make_unreadable(db_session, agent_id, _AUTH_TOKEN_REF)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "delete_refs": [_AUTH_TOKEN_REF],
            "replace": {_GATEWAY_TOKEN_REF: _REPLACEMENT_VALUE},
        },
    )

    assert response.status_code == 200, response.text
    (event,) = await _recover_events(db_session, agent_id)
    assert event.actor_type == "admin"
    assert event.source == "api.admin"
    assert event.resource_type == "hosted_runtime_secret"
    assert event.resource_id == str(agent_id)
    assert event.target_user_id == owner_id
    assert event.details == {
        "deleted": [_AUTH_TOKEN_REF],
        "replaced": [_GATEWAY_TOKEN_REF],
        "already_recovered": [],
    }
    serialized = repr(event.details)
    assert _REPLACEMENT_VALUE not in serialized
    for plaintext in _ORIGINAL_VALUES.values():
        assert plaintext not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path_for", "body"),
    [
        ("GET", _health_path, None),
        ("POST", _recover_path, {"delete_refs": [_GATEWAY_TOKEN_REF]}),
    ],
    ids=["health", "recover"],
)
@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Admin-Key": "wrong-admin-key"}],
    ids=["missing-key", "wrong-key"],
)
async def test_runtime_secret_routes_require_admin_key(
    admin_client, method, path_for, body, headers
) -> None:
    response = await admin_client.request(method, path_for(uuid4()), headers=headers, json=body)

    assert response.status_code == 401, response.text


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_concurrent_recovers_serialize(engine, db_session, seed_user, monkeypatch) -> None:
    from app.routes import admin as admin_routes

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _independent_session():
        async with session_factory() as session:
            yield session

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_lock_attempted = asyncio.Event()
    lock_calls = 0
    original_lock = admin_routes.load_hosted_runtime_secrets_for_update

    async def observed_lock(*args, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        call = lock_calls
        if call == 2:
            second_lock_attempted.set()
        rows = await original_lock(*args, **kwargs)
        if call == 1:
            first_locked.set()
            await release_first.wait()
        return rows

    agent_id: UUID | None = None
    try:
        async with _admin_client(_independent_session) as client:
            agent_id = await _deploy_agent(client, db_session, seed_user)
            await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
            monkeypatch.setattr(
                admin_routes, "load_hosted_runtime_secrets_for_update", observed_lock
            )
            body = {"replace": {_GATEWAY_TOKEN_REF: _REPLACEMENT_VALUE}}

            first = asyncio.create_task(
                client.post(_recover_path(agent_id), headers=_ADMIN_AUTH, json=body)
            )
            await asyncio.wait_for(first_locked.wait(), timeout=5)
            second = asyncio.create_task(
                client.post(_recover_path(agent_id), headers=_ADMIN_AUTH, json=body)
            )
            await asyncio.wait_for(second_lock_attempted.wait(), timeout=5)
            # Give the second request time to reach PostgreSQL and block on the row lock the
            # first one holds, then let the first commit.
            await asyncio.sleep(0.2)
            assert not second.done()
            release_first.set()
            first_response, second_response = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=10
            )

        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        assert first_response.json()["replaced"] == [_GATEWAY_TOKEN_REF]
        assert first_response.json()["already_recovered"] == []
        assert second_response.json()["replaced"] == []
        assert second_response.json()["already_recovered"] == [_GATEWAY_TOKEN_REF]
        row = (await _secret_rows(db_session, agent_id))[_GATEWAY_TOKEN_REF]
        assert decrypt(row.encrypted_value, row.nonce) == _REPLACEMENT_VALUE
        assert len(await _recover_events(db_session, agent_id)) == 2
    finally:
        if agent_id is not None:
            await db_session.rollback()
            await db_session.execute(
                delete(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.environment_id == agent_id
                )
            )
            await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_concurrent_abandon_backups_create_one_generation(
    engine, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """G-06-3: two overlapping ``abandon_backups`` recoveries of ONE installation make one ``r1``.

    The first request is held after ``recover_companion_secrets`` returns: it has locked the
    installation and the agent's atoms and abandoned the lost repository, but has not committed.
    The second request blocks on those locks, then finds the cipher pass readable and reports it
    under ``already_recovered`` instead of re-keying it into a second generation (06-35; OPS-07
    per D-11; every generation stays inside the backup key's prefix per D-15).
    """
    from app.routes import admin as admin_routes

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _independent_session():
        async with session_factory() as session:
            yield session

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_lock_attempted = asyncio.Event()
    lock_calls = 0
    recover_calls = 0
    original_lock = admin_routes.load_hosted_runtime_secrets_for_update
    original_recover = admin_routes.recover_companion_secrets

    async def observed_lock(*args, **kwargs):
        # Pass-through: only marks the moment the second request asks for its first row lock.
        nonlocal lock_calls
        lock_calls += 1
        if lock_calls == 2:
            second_lock_attempted.set()
        return await original_lock(*args, **kwargs)

    async def held_recover(*args, **kwargs):
        nonlocal recover_calls
        recover_calls += 1
        call = recover_calls
        outcome = await original_recover(*args, **kwargs)
        if call == 1:
            # Locks taken and the abandon applied; the route has not committed yet.
            first_locked.set()
            await release_first.wait()
        return outcome

    b2 = wire_b2(monkeypatch)
    agent_id: UUID | None = None
    try:
        agent_id, installation_id = await _rotated_agent(db_session, seed_user)
        installation = await _installation_row(db_session, installation_id)
        resources = await provision_backup_key(edge_step_context(db_session, installation))
        await record_step(db_session, installation_id, "backup", resources)
        old_stanza, old_repo_path = resources["stanza"], resources["repo_path"]
        await _make_atom_unreadable(db_session, installation_id, "backup-cipher-pass")
        ref = _companion_ref(installation_id, "backup-cipher-pass")
        b2_calls_before = len(b2.calls)
        monkeypatch.setattr(admin_routes, "load_hosted_runtime_secrets_for_update", observed_lock)
        monkeypatch.setattr(admin_routes, "recover_companion_secrets", held_recover)
        body = {"abandon_backups": [str(installation_id)]}

        async with _admin_client(_independent_session) as client:
            first = asyncio.create_task(
                client.post(_recover_path(agent_id), headers=_ADMIN_AUTH, json=body)
            )
            await asyncio.wait_for(first_locked.wait(), timeout=5)
            second = asyncio.create_task(
                client.post(_recover_path(agent_id), headers=_ADMIN_AUTH, json=body)
            )
            await asyncio.wait_for(second_lock_attempted.wait(), timeout=5)
            # Give the second request time to reach PostgreSQL and block on the row locks the
            # first one holds, then let the first commit.
            await asyncio.sleep(0.2)
            assert not second.done()
            assert recover_calls == 1
            release_first.set()
            first_response, second_response = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=10
            )

        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        first_body, second_body = first_response.json(), second_response.json()
        (entry,) = first_body["abandoned_backups"]
        assert entry["installation_id"] == str(installation_id)
        assert entry["abandoned_stanza"] == old_stanza
        assert entry["abandoned_repo_path"] == old_repo_path
        assert entry["stanza"] == f"{old_stanza}-r1"
        assert entry["repo_path"] == f"{old_repo_path}/r1"
        assert first_body["already_recovered"] == []
        assert second_body["abandoned_backups"] == []
        assert second_body["already_recovered"] == [ref]
        assert second_body["remaining_unreadable"] == []

        installation = await _installation_row(db_session, installation_id)
        (record,) = installation.backup_state["abandoned_repositories"]
        assert record["stanza"] == old_stanza
        assert record["repo_path"] == old_repo_path
        handles = postiz_step_handles(installation, "backup")
        assert handles is not None
        assert handles["stanza"] == f"{old_stanza}-r1"
        assert handles["repo_path"] == f"{old_repo_path}/r1"
        # Counted straight from the table: ``_companion_rows`` keys by name and would hide a twin.
        cipher_rows = list(
            (
                await db_session.execute(
                    select(CompanionInstallationSecret).where(
                        CompanionInstallationSecret.installation_id == installation_id,
                        CompanionInstallationSecret.secret_name == "backup-cipher-pass",
                    )
                )
            ).scalars()
        )
        assert len(cipher_rows) == 1
        # The surviving pass is the one the first request minted; no response carries it.
        cipher_pass = decrypt(cipher_rows[0].encrypted_value, cipher_rows[0].nonce)
        assert cipher_pass not in first_response.text
        assert cipher_pass not in second_response.text
        assert len(await _recover_events(db_session, agent_id)) == 2
        # Nothing at the provider changes: the old repository stays under retention.
        assert len(b2.calls) == b2_calls_before
    finally:
        if agent_id is not None:
            await db_session.rollback()
            await db_session.execute(
                delete(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.environment_id == agent_id
                )
            )
            await db_session.commit()


# ---------------------------------------------------------------------------
# Vault key rotation (plan 06-25, D-11, D-12, D-13 -- the rotate half of OPS-07).
#
# The keyring maps key versions to keys: the legacy ``VAULT_ENCRYPTION_KEY`` is always
# ``vault.v1``, ``VAULT_ENCRYPTION_KEYS`` adds more, and ``VAULT_ACTIVE_KEY_VERSION`` picks the one
# new versioned writes use. Rotation re-encrypts ONE agent's versioned rows -- its hosted runtime
# secrets and its live companion installations' atoms -- under the active version, and the
# runtime receives the same values under a new source revision.
# ---------------------------------------------------------------------------

_ROTATE_ACTION = "hosted_runtime_secrets.rotate"
_V1 = "vault.v1"
_V2 = "vault.v2"
_HOSTED_VALUES = {
    _AUTH_TOKEN_REF: "rotation-runtime-auth-token",
    _GATEWAY_TOKEN_REF: "rotation-openclaw-gateway-token",
}


def _rotate_path(agent_id: UUID) -> str:
    return f"/v1/admin/agents/{agent_id}/runtime-secrets/rotate"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    # An installation's hostname is minted under the operator's companion zone.
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


def _set_setting(monkeypatch: pytest.MonkeyPatch, name: str, value: object) -> None:
    # Through the instance ``__dict__`` so a keyring field is set the same way whether or not the
    # settings model declares it yet; ``monkeypatch`` restores it after the test.
    monkeypatch.setitem(settings.__dict__, name, value)


def _configure_keyring(
    monkeypatch: pytest.MonkeyPatch, *, active: str, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """``vault.v1`` is the legacy key; ``vault.v2`` a fresh one unless ``extra`` says otherwise."""
    keys = {_V2: secrets.token_hex(32)} if extra is None else extra
    _set_setting(monkeypatch, "vault_encryption_keys", keys)
    _set_setting(monkeypatch, "vault_active_key_version", active)
    return keys


def _companion_ref(installation_id: UUID, secret_name: str) -> str:
    # Spelled out rather than imported from the module under test.
    return f"companion:{installation_id}:{secret_name}"


async def _store_hosted_secrets(db_session, environment_id: UUID) -> None:
    await sync_hosted_runtime_secret_values(
        db_session,
        environment_id=environment_id,
        rows=[],
        desired={ref: SecretStr(value) for ref, value in _HOSTED_VALUES.items()},
    )
    await db_session.commit()


async def _companion_rows(
    db_session, installation_id: UUID
) -> dict[str, CompanionInstallationSecret]:
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == installation_id
            )
        )
    ).scalars()
    return {row.secret_name: row for row in rows}


async def _rotated_agent(db_session, seed_user) -> tuple[UUID, UUID]:
    """An agent with two hosted runtime secrets and a complete Postiz installation, all vault.v1."""
    environment_id = await _agent_with_runtime(db_session, seed_user)
    await _store_hosted_secrets(db_session, environment_id)
    installation_id = await _installation(db_session, seed_user, environment_id, status="complete")
    return environment_id, installation_id


async def _events(db_session, agent_id: UUID, action: str) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent)
                .where(
                    ControlPlaneAuditEvent.action == action,
                    ControlPlaneAuditEvent.environment_id == agent_id,
                )
                .order_by(ControlPlaneAuditEvent.created_at)
            )
        ).scalars()
    )


@pytest.mark.asyncio
async def test_operator_rotates_an_agent_and_the_manifest_still_renders(
    admin_client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    before = await _render_manifest(db_session, agent_id)
    assert any(ref.startswith("secret://companions/") for ref in before.secret_values)
    atom_names = sorted(await _companion_rows(db_session, installation_id))
    assert atom_names, "the installation must have stored atoms to rotate"
    expected_refs = sorted(
        [*_HOSTED_VALUES, *(_companion_ref(installation_id, name) for name in atom_names)]
    )

    _configure_keyring(monkeypatch, active=_V2)
    response = await admin_client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "agent_id": str(agent_id),
        "active_key_version": _V2,
        "reencrypted": expected_refs,
        "already_current": [],
        "unreadable": [],
    }
    hosted = await _secret_rows(db_session, agent_id)
    assert {row.key_version for row in hosted.values()} == {_V2}
    atoms = await _companion_rows(db_session, installation_id)
    assert {row.key_version for row in atoms.values()} == {_V2}
    after = await _render_manifest(db_session, agent_id)
    assert after.secret_values == before.secret_values
    assert after.source_revision != before.source_revision
    (event,) = await _events(db_session, agent_id, _ROTATE_ACTION)
    assert event.actor_type == "admin"
    assert event.source == "api.admin"
    assert event.details == {
        "active_key_version": _V2,
        "reencrypted": expected_refs,
        "already_current": [],
        "unreadable": [],
    }


@pytest.mark.asyncio
async def test_unversioned_vault_items_still_decrypt_after_the_active_version_changes(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.vault_crypto import (
        active_key_version,
        decrypt_with_key_version,
        encrypt_with_active_key,
        keyring_versions,
    )

    vault = Vault(user_id=seed_user.id, slug=f"rotation-{uuid4().hex[:8]}", name="Rotation")
    db_session.add(vault)
    await db_session.flush()
    ciphertext, nonce = vault_crypto.encrypt("vault-item-plaintext")
    item = VaultItem(vault_id=vault.id, item_name="ITEM", encrypted_value=ciphertext, nonce=nonce)
    db_session.add(item)
    await db_session.commit()
    item_id = item.id

    _configure_keyring(monkeypatch, active=_V2)
    db_session.expire_all()
    stored = await db_session.get(VaultItem, item_id)
    assert stored is not None

    assert active_key_version() == _V2
    assert keyring_versions() == (_V1, _V2)
    # The unversioned table has no column naming another key: it stays on vault.v1.
    assert decrypt(stored.encrypted_value, stored.nonce) == "vault-item-plaintext"
    legacy_ciphertext, legacy_nonce = vault_crypto.encrypt("still-legacy")
    assert decrypt_with_key_version(legacy_ciphertext, legacy_nonce, _V1) == "still-legacy"
    with pytest.raises(InvalidTag):
        decrypt_with_key_version(legacy_ciphertext, legacy_nonce, _V2)
    # A versioned write uses the active key and says so.
    active_ciphertext, active_nonce, version = encrypt_with_active_key("versioned")
    assert version == _V2
    assert decrypt_with_key_version(active_ciphertext, active_nonce, _V2) == "versioned"
    with pytest.raises(InvalidTag):
        decrypt(active_ciphertext, active_nonce)


# ---------------------------------------------------------------------------
# Recovering a companion installation's unreadable secrets (plan 06-25, Task 2).
#
# A companion atom cannot be "replaced" with an operator value the way a hosted runtime secret
# can: the platform generated it, and some of them are live credentials at a provider. So recover
# grows three companion actions, each refused unless the atoms it replaces are unreadable:
# regenerate a platform-generated atom, reprovision a step's provider credential, and -- the only
# way to replace a lost backup cipher pass -- abandon the installation's backups.
# ---------------------------------------------------------------------------

# The media and B2 fakes, re-used as fixtures here (pytest collects them by module attribute).
signing_key = media_tests.signing_key
operator_settings = media_tests.operator_settings
fake_apis = media_tests.fake_apis
media_objects = media_tests.media_objects


async def _make_atom_unreadable(db_session, installation_id: UUID, secret_name: str) -> bytes:
    row = (await _companion_rows(db_session, installation_id))[secret_name]
    row.encrypted_value, row.nonce = foreign_ciphertext("lost-with-its-key")
    await db_session.commit()
    return row.encrypted_value


async def _decrypted_atoms(db_session, installation_id: UUID) -> dict[str, str]:
    return {
        name: decrypt(row.encrypted_value, row.nonce)
        for name, row in (await _companion_rows(db_session, installation_id)).items()
    }


async def _installation_row(db_session, installation_id: UUID) -> CompanionInstallation:
    db_session.expire_all()
    row = await db_session.get(CompanionInstallation, installation_id)
    assert row is not None
    return row


def _env(value: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in value.splitlines() if line)


@pytest.mark.asyncio
async def test_health_lists_companion_atoms_with_readable_flags(
    admin_client, db_session, seed_user, companion_zone
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    atom_names = sorted(await _companion_rows(db_session, installation_id))
    plaintexts = await _decrypted_atoms(db_session, installation_id)
    await _make_atom_unreadable(db_session, installation_id, "db-password")

    response = await admin_client.get(_health_path(agent_id), headers=_ADMIN_AUTH)

    assert response.status_code == 200, response.text
    listed = {
        item["secret_ref"]: (item["key_version"], item["readable"])
        for item in response.json()["secrets"]
    }
    expected = {ref: (_V1, True) for ref in _HOSTED_VALUES}
    for name in atom_names:
        expected[_companion_ref(installation_id, name)] = (_V1, name != "db-password")
    assert listed == expected
    for plaintext in [*plaintexts.values(), *_HOSTED_VALUES.values()]:
        assert plaintext not in response.text


@pytest.mark.asyncio
async def test_regenerate_db_password_without_reading_it(
    admin_client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    old_ciphertext = await _make_atom_unreadable(db_session, installation_id, "db-password")
    ref = _companion_ref(installation_id, "db-password")

    real_decrypt = vault_crypto.decrypt
    touches: list[tuple[bytes, str]] = []

    def spy_decrypt(ciphertext: bytes, nonce: bytes) -> str:
        try:
            plaintext = real_decrypt(ciphertext, nonce)
        except InvalidTag:
            touches.append((ciphertext, "InvalidTag"))
            raise
        touches.append((ciphertext, "plaintext"))
        return plaintext

    monkeypatch.setattr(vault_crypto, "decrypt", spy_decrypt)
    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_regenerate": [
                {"installation_id": str(installation_id), "secret_name": "db-password"}
            ]
        },
    )
    monkeypatch.setattr(vault_crypto, "decrypt", real_decrypt)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["regenerated"] == [ref]
    assert body["remaining_unreadable"] == []
    # The old ciphertext never yielded plaintext: every touch was the probe that classified it.
    assert {outcome for ciphertext, outcome in touches if ciphertext == old_ciphertext} == {
        "InvalidTag"
    }
    new_password = (await _decrypted_atoms(db_session, installation_id))["db-password"]
    assert new_password not in response.text
    installation = await _installation_row(db_session, installation_id)
    assert isinstance(installation.external_resources.get("credentials_regenerated_at"), str)
    source = await _render_manifest(db_session, agent_id)
    db_env = _env(source.secret_values[f"secret://companions/postiz/{installation_id}/db-env"])
    assert db_env["POSTGRES_PASSWORD"] == new_password
    (event,) = await _events(db_session, agent_id, _RECOVER_ACTION)
    assert event.details["regenerated"] == [ref]
    assert new_password not in repr(event.details)


@pytest.mark.asyncio
async def test_regenerate_refuses_a_readable_atom(
    admin_client, db_session, seed_user, companion_zone
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    before = await _decrypted_atoms(db_session, installation_id)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_regenerate": [
                {"installation_id": str(installation_id), "secret_name": "jwt-secret"}
            ]
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "runtime_secret_readable",
        "secret_refs": [_companion_ref(installation_id, "jwt-secret")],
    }
    assert await _decrypted_atoms(db_session, installation_id) == before


@pytest.mark.asyncio
async def test_companion_recovery_of_another_agents_installation_is_404(
    admin_client, db_session, seed_user, companion_zone
) -> None:
    agent_id, _installation_id = await _rotated_agent(db_session, seed_user)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_regenerate": [{"installation_id": str(uuid4()), "secret_name": "api-key"}]
        },
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["code"] == "companion_installation_not_found"


@pytest.mark.asyncio
async def test_reprovision_media_revokes_and_remints(
    admin_client,
    db_session,
    seed_user,
    companion_zone,
    operator_settings,
    fake_apis,
    media_objects,
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    installation = await _installation_row(db_session, installation_id)
    resources = await provision_media_bucket(edge_step_context(db_session, installation))
    await record_step(db_session, installation_id, "media", resources)
    token_id = resources["token_id"]
    old_value = fake_apis.tokens[token_id]["value"]
    fake_apis.buckets[resources["bucket"]]["objects"] = 3  # the tenant's media
    await _make_atom_unreadable(db_session, installation_id, "media-secret-access-key")
    calls_before = len(fake_apis.calls)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_reprovision": [{"installation_id": str(installation_id), "step": "media"}]
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["reprovisioned"] == [
        {"installation_id": str(installation_id), "step": "media"}
    ]
    new_value = fake_apis.tokens[token_id]["value"]
    assert new_value != old_value, "the old token value must be revoked (rolled)"
    atoms = await _decrypted_atoms(db_session, installation_id)
    assert atoms["media-access-key-id"] == token_id
    assert atoms["media-secret-access-key"] == hashlib.sha256(new_value.encode()).hexdigest()
    # The credential moved; the tenant's media did not.
    assert fake_apis.buckets[resources["bucket"]]["objects"] == 3
    recovery_calls = fake_apis.calls[calls_before:]
    assert not [call for call in recovery_calls if call.startswith(("DELETE", "PURGE"))]
    for value in (old_value, new_value):
        assert value not in response.text


@pytest.mark.asyncio
async def test_reprovision_refuses_a_step_whose_credentials_are_readable(
    admin_client, db_session, seed_user, companion_zone
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)

    response = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_reprovision": [{"installation_id": str(installation_id), "step": "temporal"}]
        },
    )

    # No temporal step ever ran for this installation, so there is nothing to reprovision.
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "companion_step_not_provisioned"


@pytest.mark.asyncio
async def test_backup_cipher_pass_is_only_replaced_by_abandoning_backups(
    admin_client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    b2 = wire_b2(monkeypatch)
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    installation = await _installation_row(db_session, installation_id)
    resources = await provision_backup_key(edge_step_context(db_session, installation))
    await record_step(db_session, installation_id, "backup", resources)
    old_stanza, old_repo_path = resources["stanza"], resources["repo_path"]
    installation = await _installation_row(db_session, installation_id)
    installation.backup_state = {"markers": [{"id": "m1", "token": "t"}], "db_bytes": 10}
    await db_session.commit()
    await _make_atom_unreadable(db_session, installation_id, "backup-cipher-pass")
    ref = _companion_ref(installation_id, "backup-cipher-pass")
    b2_calls_before = len(b2.calls)

    plain = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={
            "companion_regenerate": [
                {"installation_id": str(installation_id), "secret_name": "backup-cipher-pass"}
            ]
        },
    )

    assert plain.status_code == 409, plain.text
    assert plain.json()["detail"] == {
        "code": "companion_backup_cipher_pass_requires_abandon",
        "secret_refs": [ref],
        "message": plain.json()["detail"]["message"],
    }
    assert "abandon_backups" in plain.json()["detail"]["message"]

    abandoned = await admin_client.post(
        _recover_path(agent_id),
        headers=_ADMIN_AUTH,
        json={"abandon_backups": [str(installation_id)]},
    )

    assert abandoned.status_code == 200, abandoned.text
    (entry,) = abandoned.json()["abandoned_backups"]
    assert entry == {
        "installation_id": str(installation_id),
        "abandoned_stanza": old_stanza,
        "abandoned_repo_path": old_repo_path,
        "stanza": f"{old_stanza}-r1",
        "repo_path": f"{old_repo_path}/r1",
        "restorable_by_platform": False,
    }
    cipher_pass = (await _decrypted_atoms(db_session, installation_id))["backup-cipher-pass"]
    assert cipher_pass not in abandoned.text
    installation = await _installation_row(db_session, installation_id)
    (record,) = installation.backup_state["abandoned_repositories"]
    assert record["stanza"] == old_stanza
    assert record["repo_path"] == old_repo_path
    # The abandoned repository's restore points go with it; the new one starts with none.
    assert record["markers"] == [{"id": "m1", "token": "t"}]
    assert installation.backup_state.get("markers", []) == []
    source = await _render_manifest(db_session, agent_id)
    backup_env = _env(
        source.secret_values[f"secret://companions/postiz/{installation_id}/backup-env"]
    )
    assert backup_env["PGBACKREST_STANZA"] == f"{old_stanza}-r1"
    assert backup_env["PGBACKREST_REPO1_PATH"] == f"{old_repo_path}/r1"
    assert backup_env["PGBACKREST_REPO1_CIPHER_PASS"] == cipher_pass
    # Nothing at the provider changes: the old repository stays under retention, the key stays.
    assert len(b2.calls) == b2_calls_before


@pytest.mark.asyncio
async def test_abandoned_backup_repositories_stay_inside_the_key_prefix(
    admin_client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """CR-01: every repository generation lies under the backup key's ``namePrefix`` (D-15).

    The runtime's key is restricted to ``<tenant>/<installation>/``, trailing slash included, so a
    repository at the sibling ``/<tenant>/<installation>-r1`` would be refused by B2 on every
    stanza-create, backup and WAL push, and the rehearsal key could not read it either. An abandon
    starts its new repository as a subdirectory of the original one, re-uses the same key, and the
    rendered manifest carries that one path in the entry and in the backup env.
    """
    b2 = wire_b2(monkeypatch)
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    installation = await _installation_row(db_session, installation_id)
    prefix = backup_name_prefix(installation.user_id, installation_id)
    resources = await provision_backup_key(edge_step_context(db_session, installation))
    await record_step(db_session, installation_id, "backup", resources)
    old_repo_path = resources["repo_path"]
    (key_request,) = b2.named("b2_create_key")
    assert key_request["namePrefix"] == prefix
    assert "deleteFiles" not in key_request["capabilities"]
    # Generation 0, the original repository, is inside the prefix ...
    assert f"{old_repo_path.lstrip('/')}/".startswith(prefix)
    # ... and the pre-fix sibling form is not: the negative control that catches CR-01.
    assert not f"{old_repo_path.lstrip('/')}-r1/".startswith(prefix)
    b2_calls_before = len(b2.calls)
    backup_env_ref = f"secret://companions/postiz/{installation_id}/backup-env"

    previous = old_repo_path
    for generation in (1, 2):
        await _make_atom_unreadable(db_session, installation_id, "backup-cipher-pass")
        response = await admin_client.post(
            _recover_path(agent_id),
            headers=_ADMIN_AUTH,
            json={"abandon_backups": [str(installation_id)]},
        )

        assert response.status_code == 200, response.text
        (entry,) = response.json()["abandoned_backups"]
        repo_path = entry["repo_path"]
        assert entry["abandoned_repo_path"] == previous
        assert f"{repo_path.lstrip('/')}/".startswith(prefix), repo_path
        assert repo_path == f"{old_repo_path}/r{generation}"
        source = await _render_manifest(db_session, agent_id)
        postiz = source.manifest["companions"]["postiz"]
        assert postiz["backup"]["repoPath"] == repo_path
        assert _env(source.secret_values[backup_env_ref])["PGBACKREST_REPO1_PATH"] == repo_path
        previous = repo_path

    # The key is neither widened nor re-minted: an abandon makes no B2 call at all.
    assert len(b2.calls) == b2_calls_before


# ---------------------------------------------------------------------------
# Hardening (plan 06-25, Task 3): safe to repeat, safe to race, never a value -- and the operator
# knows the downtime and the residual before an incident needs them.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_rotation_reports_everything_already_current(
    admin_client, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    _configure_keyring(monkeypatch, active=_V2)
    first = await admin_client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH)
    assert first.status_code == 200, first.text
    after_first = await _row_identities(db_session, agent_id)
    revision = (await _render_manifest(db_session, agent_id)).source_revision

    second = await admin_client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH)

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["reencrypted"] == []
    assert body["unreadable"] == []
    assert body["already_current"] == first.json()["reencrypted"]
    assert await _row_identities(db_session, agent_id) == after_first
    assert (await _render_manifest(db_session, agent_id)).source_revision == revision
    assert len(await _events(db_session, agent_id, _ROTATE_ACTION)) == 2


@pytest.mark.asyncio
async def test_row_unreadable_under_every_key_is_reported_not_touched(
    admin_client, db_session, seed_user, monkeypatch
) -> None:
    agent_id = await _deploy_agent(admin_client, db_session, seed_user)
    await _make_unreadable(db_session, agent_id, _GATEWAY_TOKEN_REF)
    # And one whose key version the keyring does not hold at all: its key is gone too.
    orphan = (await _secret_rows(db_session, agent_id))[_AUTH_TOKEN_REF]
    orphan.key_version = "vault.v9"
    await db_session.commit()
    before = await _row_identities(db_session, agent_id)
    _configure_keyring(monkeypatch, active=_V2)

    response = await admin_client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["unreadable"] == [_AUTH_TOKEN_REF, _GATEWAY_TOKEN_REF]
    assert response.json()["reencrypted"] == []
    assert await _row_identities(db_session, agent_id) == before
    health = await admin_client.get(_health_path(agent_id), headers=_ADMIN_AUTH)
    assert health.status_code == 200, health.text
    assert {item["secret_ref"]: item["readable"] for item in health.json()["secrets"]} == {
        _AUTH_TOKEN_REF: False,
        _GATEWAY_TOKEN_REF: False,
    }


def test_keyring_entry_disagreeing_with_the_legacy_key_is_a_startup_error() -> None:
    from pydantic import ValidationError

    from app.core.config import Settings

    legacy, other = secrets.token_hex(32), secrets.token_hex(32)

    with pytest.raises(ValidationError) as disagreeing:
        Settings(
            _env_file=None,
            vault_encryption_key=legacy,
            vault_encryption_keys={_V1: other},
        )
    with pytest.raises(ValidationError) as inactive:
        Settings(
            _env_file=None,
            vault_encryption_key=legacy,
            vault_encryption_keys={_V2: other},
            vault_active_key_version="vault.v3",
        )
    with pytest.raises(ValidationError):
        Settings(_env_file=None, vault_encryption_keys={_V2: "not-a-key"})

    # Refused by name, never by printing the keys it was given.
    for raised in (disagreeing, inactive):
        assert legacy not in str(raised.value)
        assert other not in str(raised.value)
    assert "VAULT_ENCRYPTION_KEY" in str(disagreeing.value)
    assert "vault.v3" in str(inactive.value)
    # The same key under vault.v1 agrees, and an empty environment line means "no more keys".
    assert Settings(
        _env_file=None, vault_encryption_key=legacy, vault_encryption_keys={_V1: legacy}
    )
    assert Settings(_env_file=None, vault_encryption_keys="").vault_encryption_keys == {}
    parsed = Settings(
        _env_file=None,
        vault_encryption_key=legacy,
        vault_encryption_keys=f'{{"{_V2}": "{other}"}}',
        vault_active_key_version=_V2,
    )
    assert parsed.vault_encryption_keys == {_V2: other}


@pytest.mark.asyncio
@pytest.mark.committed_db
async def test_concurrent_rotations_serialize(engine, db_session, seed_user, monkeypatch) -> None:
    from app.services import vault_key_rotation

    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def _independent_session():
        async with session_factory() as session:
            yield session

    first_locked = asyncio.Event()
    release_first = asyncio.Event()
    second_lock_attempted = asyncio.Event()
    lock_calls = 0
    original_lock = vault_key_rotation.load_hosted_runtime_secrets_for_update

    async def observed_lock(*args, **kwargs):
        nonlocal lock_calls
        lock_calls += 1
        call = lock_calls
        if call == 2:
            second_lock_attempted.set()
        rows = await original_lock(*args, **kwargs)
        if call == 1:
            first_locked.set()
            await release_first.wait()
        return rows

    agent_id: UUID | None = None
    try:
        async with _admin_client(_independent_session) as client:
            agent_id = await _deploy_agent(client, db_session, seed_user)
            _configure_keyring(monkeypatch, active=_V2)
            monkeypatch.setattr(
                vault_key_rotation, "load_hosted_runtime_secrets_for_update", observed_lock
            )

            first = asyncio.create_task(client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH))
            await asyncio.wait_for(first_locked.wait(), timeout=5)
            second = asyncio.create_task(client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH))
            await asyncio.wait_for(second_lock_attempted.wait(), timeout=5)
            # The second request reaches PostgreSQL and blocks on the rows the first holds.
            await asyncio.sleep(0.2)
            assert not second.done()
            release_first.set()
            first_response, second_response = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=10
            )

        both_refs = sorted(_ORIGINAL_VALUES)
        assert first_response.status_code == 200, first_response.text
        assert second_response.status_code == 200, second_response.text
        assert first_response.json()["reencrypted"] == both_refs
        assert second_response.json()["reencrypted"] == []
        assert second_response.json()["already_current"] == both_refs
        rows = await _secret_rows(db_session, agent_id)
        assert {row.key_version for row in rows.values()} == {_V2}
        assert len(await _events(db_session, agent_id, _ROTATE_ACTION)) == 2
    finally:
        if agent_id is not None:
            await db_session.rollback()
            await db_session.execute(
                delete(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.environment_id == agent_id
                )
            )
            await db_session.commit()


@pytest.mark.asyncio
async def test_rotate_recover_and_health_never_expose_values(
    admin_client, db_session, seed_user, companion_zone, monkeypatch, caplog
) -> None:
    caplog.set_level(logging.DEBUG)
    agent_id, installation_id = await _rotated_agent(db_session, seed_user)
    values = [
        *_HOSTED_VALUES.values(),
        *(await _decrypted_atoms(db_session, installation_id)).values(),
    ]
    _configure_keyring(monkeypatch, active=_V2)
    keys = [settings.vault_encryption_key, *settings.vault_encryption_keys.values()]

    bodies = [(await admin_client.get(_health_path(agent_id), headers=_ADMIN_AUTH)).text]
    bodies.append((await admin_client.post(_rotate_path(agent_id), headers=_ADMIN_AUTH)).text)
    await _make_atom_unreadable(db_session, installation_id, "api-key")
    bodies.append(
        (
            await admin_client.post(
                _recover_path(agent_id),
                headers=_ADMIN_AUTH,
                json={
                    "companion_regenerate": [
                        {"installation_id": str(installation_id), "secret_name": "api-key"}
                    ]
                },
            )
        ).text
    )
    bodies.append((await admin_client.get(_health_path(agent_id), headers=_ADMIN_AUTH)).text)
    values.append(
        decrypt_with_key_version_of((await _companion_rows(db_session, installation_id))["api-key"])
    )

    audit = repr(
        [
            event.details
            for action in (_ROTATE_ACTION, _RECOVER_ACTION)
            for event in await _events(db_session, agent_id, action)
        ]
    )
    for secret in [*values, *keys]:
        for body in bodies:
            assert secret not in body
        assert secret not in caplog.text
        assert secret not in audit


def decrypt_with_key_version_of(row: CompanionInstallationSecret) -> str:
    return vault_crypto.decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)


def test_runbook_sets_downtime_and_residual_expectations() -> None:
    runbook = (REPO_ROOT / "docs" / "runbooks" / "vault-key-rotation.md").read_text()
    headings = [line.lstrip("#").strip() for line in runbook.splitlines() if line.startswith("## ")]
    for section in (
        "When to rotate",
        "Add a key and make it active",
        "Rotate an agent",
        "Recover unreadable secrets",
        "Downtime",
        "Residual",
    ):
        assert section in headings, f"the runbook has no '## {section}' section"
    for route in ("runtime-secrets/rotate", "runtime-secrets/recover", "runtime-secrets/health"):
        assert route in runbook
    for action in ("companion_regenerate", "companion_reprovision", "abandon_backups"):
        assert action in runbook
    residual = runbook.split("## Residual", 1)[1]
    for table in ("vault items", "channel tokens", "AI provider credentials"):
        assert table in residual


def test_env_example_declares_the_keyring_empty() -> None:
    example = (REPO_ROOT / "backend" / ".env.example").read_text().splitlines()
    assert "VAULT_ENCRYPTION_KEYS=" in example
    assert "VAULT_ACTIVE_KEY_VERSION=vault.v1" in example
