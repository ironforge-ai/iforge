"""Per-installation encrypted backups behind a key that cannot delete (plan 06-15, D-14, D-15).

D-14: every installation's database archives WAL continuously and takes scheduled pgBackRest
backups from the house PostgreSQL image, into a repository path unique to its tenant and
installation. D-15 / OPS-04: the repository lives in a Backblaze B2 bucket in an account separate
from the resources it protects, and the runtime's key can list, read and write -- never delete,
bypass governance or change retention -- inside exactly ``<tenant_id>/<installation_id>/``.
Nothing here reaches a live API: ``FakeB2`` stands in for B2's native API behind one mock
transport, and refuses any call that is not key management.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.services.companion_lifecycle_worker import (
    PROVISIONING_STEPS,
    CompanionLifecycleWorker,
    ProvisioningStep,
)
from app.services.vault_crypto import decrypt
from tests.test_companion_install_job import _reload, _sessionmaker

B2_AUTH_HOST = "api.backblazeb2.com"
B2_STORAGE_API_URL = "https://api005.backblazeb2.com"
OPERATOR_KEY_ID = "005operatorkeyid0000000001"
OPERATOR_APPLICATION_KEY = "K005operator-application-key-SECRET-0123"
B2_ACCOUNT_ID = "b2acct0123456789"
B2_AUTH_TOKEN = "4_b2-account-authorization-token-SECRET"
BUCKET_ID = "e73ede9c9c8412db49f60715"
BUCKET_NAME = "iforge-companion-backups"
S3_ENDPOINT = "s3.us-west-004.backblazeb2.com"
S3_REGION = "us-west-004"
RUNTIME_CAPABILITIES = ["listFiles", "readFiles", "writeFiles"]
# Every B2 call that could remove, hide or re-date data. None may ever be issued.
FILE_DESTROYING_CALLS = (
    "b2_delete_file_version",
    "b2_hide_file",
    "b2_delete_bucket",
    "b2_update_bucket",
    "b2_update_file_retention",
    "b2_update_file_legal_hold",
)


# ---------------------------------------------------------------------------
# The fake B2: key management only, and the answers B2's native API v4 gives.
# ---------------------------------------------------------------------------


@dataclass
class FakeB2:
    """B2's native API (v4) key management as state; anything else fails the test loudly."""

    keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    # (call name, parsed body) in order; the ordering and body assertions read these.
    calls: list[tuple[str, dict[str, Any] | None]] = field(default_factory=list)
    issued_application_keys: list[str] = field(default_factory=list)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = urlsplit(str(request.url))
        assert url.path.startswith("/b2api/v4/"), url.path
        name = url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content) if request.content else None
        self.calls.append((name, body))
        if name == "b2_authorize_account":
            assert url.hostname == B2_AUTH_HOST
            assert request.method == "GET"
            expected = base64.b64encode(
                f"{OPERATOR_KEY_ID}:{OPERATOR_APPLICATION_KEY}".encode()
            ).decode()
            assert request.headers["authorization"] == f"Basic {expected}"
            return httpx.Response(
                200,
                json={
                    "accountId": B2_ACCOUNT_ID,
                    "authorizationToken": B2_AUTH_TOKEN,
                    "apiInfo": {
                        "storageApi": {
                            "apiUrl": B2_STORAGE_API_URL,
                            "downloadUrl": "https://f005.backblazeb2.com",
                            "s3ApiUrl": f"https://{S3_ENDPOINT}",
                            "capabilities": ["listKeys", "writeKeys", "deleteKeys"],
                            "infoType": "storageApi",
                        }
                    },
                    "applicationKeyExpirationTimestamp": None,
                },
            )
        assert f"{url.scheme}://{url.hostname}" == B2_STORAGE_API_URL, str(request.url)
        assert request.method == "POST"
        assert request.headers["authorization"] == B2_AUTH_TOKEN
        assert body is not None
        if name == "b2_list_keys":
            assert body["accountId"] == B2_ACCOUNT_ID
            return httpx.Response(
                200,
                json={
                    "keys": [_public_key(key) for key in self.keys.values()],
                    "nextApplicationKeyId": None,
                },
            )
        if name == "b2_create_key":
            key_id = f"005{uuid.uuid4().hex[:22]}"
            secret = f"K005-backup-application-key-SECRET-{uuid.uuid4().hex}"
            self.issued_application_keys.append(secret)
            self.keys[key_id] = {
                "accountId": body["accountId"],
                "applicationKeyId": key_id,
                "keyName": body["keyName"],
                "capabilities": body["capabilities"],
                "bucketIds": body.get("bucketIds"),
                "namePrefix": body.get("namePrefix"),
                "expirationTimestamp": None,
                "options": ["s3"],
            }
            return httpx.Response(200, json={**self.keys[key_id], "applicationKey": secret})
        if name == "b2_delete_key":
            key = self.keys.pop(body["applicationKeyId"], None)
            if key is None:
                return httpx.Response(
                    400, json={"status": 400, "code": "bad_request", "message": "key not found"}
                )
            return httpx.Response(200, json=_public_key(key))
        raise AssertionError(f"unexpected B2 call {name}")

    def named(self, name: str) -> list[dict[str, Any]]:
        return [body or {} for call, body in self.calls if call == name]


def _public_key(key: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in key.items() if name != "applicationKey"}


# ---------------------------------------------------------------------------
# Wiring and helpers.
# ---------------------------------------------------------------------------

BACKUP_SETTINGS = {
    "companion_backup_b2_key_id": OPERATOR_KEY_ID,
    "companion_backup_b2_application_key": OPERATOR_APPLICATION_KEY,
    "companion_backup_b2_bucket_id": BUCKET_ID,
    "companion_backup_b2_bucket_name": BUCKET_NAME,
    "companion_backup_b2_s3_endpoint": S3_ENDPOINT,
    "companion_backup_b2_region": S3_REGION,
}


def _wire_b2(monkeypatch: pytest.MonkeyPatch, b2: FakeB2 | None = None) -> FakeB2:
    """Every operator setting the backup step reads, set to a fake, and the fake B2 wired in."""
    from app.services import companion_backup_repo

    b2 = b2 or FakeB2()
    # Installations are refused without a companion zone; its hostname names the stanza.
    monkeypatch.setattr(settings, "companion_edge_zone_name", "companions.example")
    for name, value in BACKUP_SETTINGS.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(companion_backup_repo, "_transport_override", b2.transport())
    return b2


def _pipeline_step(steps: tuple[ProvisioningStep, ...], name: str) -> ProvisioningStep:
    matches = [step for step in steps if step.name == name]
    assert len(matches) == 1, [step.name for step in steps]
    return matches[0]


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


def _env_lines(value: str) -> dict[str, str]:
    assert value.endswith("\n"), value
    lines = value[:-1].split("\n")
    parsed = dict(line.split("=", 1) for line in lines)
    assert len(parsed) == len(lines), "a key appears twice"
    return parsed


def _backup_ref(installation_id: uuid.UUID) -> str:
    # Spelled out rather than imported, so a wrong ref shape cannot also be the expectation.
    return f"secret://companions/postiz/{installation_id}/backup-env"


# ---------------------------------------------------------------------------
# Task 1 (tracer): an install provisions a key that cannot delete, and the runtime is told where
# its encrypted repository is.
# ---------------------------------------------------------------------------


async def test_install_provisions_a_no_delete_backup_key_and_projects_backup_settings(
    db_session, seed_user, monkeypatch
) -> None:
    from app.schemas.runtime import HostedPostizCompanion
    from tests.test_companion_projection import _agent_with_runtime, _installation, _render

    # The backup key is a provisioning step directly after the unrouted edge tunnel.
    provisioning = [step.name for step in PROVISIONING_STEPS]
    assert "backup" in provisioning, provisioning
    assert provisioning.index("backup") == provisioning.index("edge") + 1, provisioning

    b2 = _wire_b2(monkeypatch)
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="queued")
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(_pipeline_step(PROVISIONING_STEPS, "backup"),),
        completion_steps=(),
    )
    assert await worker.run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "running", row.error
    tenant_id = row.user_id

    # Exactly one key, with exactly the three capabilities, one bucket and the trailing-slash
    # prefix that no sibling installation's prefix can share.
    (create,) = b2.named("b2_create_key")
    assert create == {
        "accountId": B2_ACCOUNT_ID,
        "keyName": f"pgbr-{installation_id}",
        "capabilities": RUNTIME_CAPABILITIES,
        "bucketIds": [BUCKET_ID],
        "namePrefix": f"{tenant_id}/{installation_id}/",
    }
    (key,) = b2.keys.values()
    for call in FILE_DESTROYING_CALLS:
        assert b2.named(call) == []

    # The key id, its application key and a generated cipher pass are stored encrypted.
    atoms = await _decrypted_atoms(db_session, installation_id)
    assert atoms["backup-key-id"] == key["applicationKeyId"]
    assert atoms["backup-application-key"] == b2.issued_application_keys[0]
    cipher_pass = atoms["backup-cipher-pass"]
    assert len(cipher_pass) == 64
    assert all(character.isalnum() or character in "-_" for character in cipher_pass)

    stanza = f"postiz-{row.hostname_label.rpartition('-')[2]}"
    repo_path = f"/{tenant_id}/{installation_id}"
    assert _step_resources(row, "backup") == {
        "key_id": key["applicationKeyId"],
        "stanza": stanza,
        "repo_path": repo_path,
        "bucket_id": BUCKET_ID,
        "bucket": BUCKET_NAME,
        "s3_endpoint": S3_ENDPOINT,
        "s3_region": S3_REGION,
    }

    # The runtime is told the stanza and repository, and gets every pgBackRest setting as one
    # encrypted env credential -- with expire-auto OFF, so only the operator can expire.
    source = await _render(db_session, environment_id)
    entry = source.manifest["companions"]["postiz"]
    HostedPostizCompanion.model_validate(entry)
    assert entry["backup"] == {"stanza": stanza, "repoPath": repo_path}
    ref = _backup_ref(installation_id)
    assert entry["credentials"].get("backupEnvSecretRef") == ref
    assert _env_lines(source.secret_values[ref]) == {
        "PGBACKREST_COMPRESS_TYPE": "zst",
        "PGBACKREST_EXPIRE_AUTO": "n",
        "PGBACKREST_PG1_PATH": "/var/lib/postgresql/18/docker",
        "PGBACKREST_PG1_USER": "postiz",
        "PGBACKREST_REPO1_CIPHER_PASS": cipher_pass,
        "PGBACKREST_REPO1_CIPHER_TYPE": "aes-256-cbc",
        "PGBACKREST_REPO1_PATH": repo_path,
        "PGBACKREST_REPO1_RETENTION_FULL": "2",
        "PGBACKREST_REPO1_S3_BUCKET": BUCKET_NAME,
        "PGBACKREST_REPO1_S3_ENDPOINT": S3_ENDPOINT,
        "PGBACKREST_REPO1_S3_KEY": key["applicationKeyId"],
        "PGBACKREST_REPO1_S3_KEY_SECRET": b2.issued_application_keys[0],
        "PGBACKREST_REPO1_S3_REGION": S3_REGION,
        "PGBACKREST_REPO1_S3_URI_STYLE": "path",
        "PGBACKREST_REPO1_TYPE": "s3",
        "PGBACKREST_STANZA": stanza,
    }
    # No key and no cipher pass reach the manifest itself; each lives in the one composed ref.
    manifest_text = json.dumps(source.manifest)
    for secret in (cipher_pass, b2.issued_application_keys[0]):
        assert secret not in manifest_text
        assert [name for name, value in source.secret_values.items() if secret in value] == [ref]


# ---------------------------------------------------------------------------
# Task 2: the key's hard limits, the settings, and a teardown that never deletes data.
# ---------------------------------------------------------------------------

FORBIDDEN_CAPABILITIES = (
    "deleteFiles",
    "bypassGovernance",
    "writeBucketRetentions",
    "writeBuckets",
    "deleteBuckets",
)


@pytest.mark.parametrize(
    ("tenant_id", "installation_id"),
    [
        (uuid.UUID(int=0), uuid.UUID(int=1)),
        (uuid.uuid4(), uuid.uuid4()),
        ("0b6d2c1e-7f3a-4e59-8c14-9a2b3c4d5e6f", "5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90"),
    ],
)
def test_backup_key_never_requests_a_delete_capability(tenant_id, installation_id) -> None:
    from app.services.companion_backup_repo import backup_key_request

    request = backup_key_request(
        account_id=B2_ACCOUNT_ID,
        bucket_id=BUCKET_ID,
        tenant_id=tenant_id,
        installation_id=installation_id,
    )

    assert request["capabilities"] == RUNTIME_CAPABILITIES
    for capability in FORBIDDEN_CAPABILITIES:
        assert capability not in request["capabilities"]
    # Restricted to one bucket, and never valid for a bucket it was not minted for.
    assert request["bucketIds"] == [BUCKET_ID]


def test_name_prefix_cannot_address_a_sibling_installation() -> None:
    from app.services.companion_backup_repo import backup_key_request, backup_repo_path

    tenant_id = "0b6d2c1e-7f3a-4e59-8c14-9a2b3c4d5e6f"
    # Two ids where one is a leading substring of the other: without the trailing slash the
    # shorter one's prefix would match every object of the longer one.
    short, long = "5f0c6d2e-8a41", "5f0c6d2e-8a41-4b7f"
    prefixes = [
        str(
            backup_key_request(
                account_id=B2_ACCOUNT_ID,
                bucket_id=BUCKET_ID,
                tenant_id=tenant_id,
                installation_id=installation_id,
            )["namePrefix"]
        )
        for installation_id in (short, long)
    ]

    assert prefixes == [f"{tenant_id}/{short}/", f"{tenant_id}/{long}/"]
    assert not prefixes[1].startswith(prefixes[0])
    assert not prefixes[0].startswith(prefixes[1])
    # pgBackRest's object names are the repo path, a slash, then the repository's own files, so
    # every object the runtime writes lies under exactly its own prefix.
    for installation_id, prefix in zip((short, long), prefixes, strict=True):
        assert f"{backup_repo_path(tenant_id, installation_id).lstrip('/')}/" == prefix


def test_every_backup_generation_stays_inside_the_installation_prefix() -> None:
    """CR-01: an abandoned-backups generation is a subdirectory, never a sibling, of the repo."""
    from app.services.companion_backup_repo import (
        backup_generation_repo_path,
        backup_name_prefix,
        backup_repo_path,
    )

    tenant_id = "0b6d2c1e-7f3a-4e59-8c14-9a2b3c4d5e6f"
    installation_id = "5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90"
    prefix = backup_name_prefix(tenant_id, installation_id)
    original = backup_repo_path(tenant_id, installation_id)

    # Object names are the repo path without its leading slash, a slash, then pgBackRest's files.
    assert f"{original.lstrip('/')}/".startswith(prefix)
    for generation in (1, 2, 3):
        path = backup_generation_repo_path(tenant_id, installation_id, generation)
        assert path == f"{original}/r{generation}"
        assert f"{path.lstrip('/')}/".startswith(prefix), path
    # The pre-fix sibling form lies outside the key's prefix: B2 would refuse every write to it.
    assert not f"{original.lstrip('/')}-r1/".startswith(prefix)
    for generation in (0, -1):
        with pytest.raises(ValueError):
            backup_generation_repo_path(tenant_id, installation_id, generation)


async def _backup_installation(db_session: AsyncSession, seed_user) -> CompanionInstallation:
    from tests.test_companion_edge import _new_installation

    install = await _new_installation(db_session, seed_user)
    return install.installation


def _step_context(db_session: AsyncSession, installation: CompanionInstallation):
    from tests.test_companion_edge import _step_context as edge_step_context

    return edge_step_context(db_session, installation)


async def test_teardown_revokes_the_key_and_deletes_no_file(
    db_session, seed_user, monkeypatch
) -> None:
    b2 = _wire_b2(monkeypatch)
    installation = await _backup_installation(db_session, seed_user)
    installation_id = installation.id

    async def refuse(context) -> dict:
        raise RuntimeError("a later provisioning step refused")

    async def never(context, resources: dict) -> None:
        raise AssertionError("a step that never completed must not be torn down")

    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(
            _pipeline_step(PROVISIONING_STEPS, "backup"),
            ProvisioningStep("later", refuse, never),
        ),
        completion_steps=(),
    )
    # The install fails after the key was minted, and tears the backup step down in this pass.
    assert await worker.run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "failed"
    assert [entry["name"] for entry in row.external_resources["torn_down"]] == ["backup"]
    assert _step_resources(row, "backup") is None

    # The key is revoked -- by id -- and nothing else is deleted: no file, no version, no bucket.
    (minted,) = b2.named("b2_create_key")
    assert b2.keys == {}, "the runtime's key must be revoked"
    assert len(b2.named("b2_delete_key")) == 1
    for call in FILE_DESTROYING_CALLS:
        assert b2.named(call) == []
    assert {name for name, _ in b2.calls} <= {
        "b2_authorize_account",
        "b2_list_keys",
        "b2_create_key",
        "b2_delete_key",
    }
    assert minted["keyName"] == f"pgbr-{installation_id}"

    # The key's atoms go with it; the cipher pass stays, or the retained repository could never
    # be read again.
    atoms = await _decrypted_atoms(db_session, installation_id)
    assert "backup-key-id" not in atoms
    assert "backup-application-key" not in atoms
    assert len(atoms["backup-cipher-pass"]) == 64


async def test_teardown_of_an_already_revoked_key_is_done(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_backup_repo import provision_backup_key, revoke_backup_key

    b2 = _wire_b2(monkeypatch)
    installation = await _backup_installation(db_session, seed_user)
    context = _step_context(db_session, installation)
    resources = await provision_backup_key(context)

    await revoke_backup_key(context, resources)
    # Revoked already (by an earlier pass, or by hand): a second teardown finds nothing to do.
    await revoke_backup_key(context, resources)

    assert b2.keys == {}
    assert len(b2.named("b2_delete_key")) == 1


@pytest.mark.parametrize("unset", sorted(BACKUP_SETTINGS))
async def test_missing_backup_settings_fail_the_step_before_any_call(
    db_session, seed_user, monkeypatch, unset
) -> None:
    from app.services.companion_backup_repo import provision_backup_key
    from app.services.companion_lifecycle_steps import CompanionStepError

    b2 = _wire_b2(monkeypatch)
    installation = await _backup_installation(db_session, seed_user)
    monkeypatch.setattr(settings, unset, "")

    with pytest.raises(CompanionStepError) as refused:
        await provision_backup_key(_step_context(db_session, installation))

    assert refused.value.code == "backup_settings_missing"
    assert unset.upper() in str(refused.value)
    assert b2.calls == []
    assert "backup-key-id" not in await _decrypted_atoms(db_session, installation.id)


async def test_no_key_or_cipher_pass_is_logged(db_session, seed_user, monkeypatch, caplog) -> None:
    import logging

    from app.services.companion_backup_repo import provision_backup_key, revoke_backup_key

    caplog.set_level(logging.DEBUG)
    b2 = _wire_b2(monkeypatch)
    installation = await _backup_installation(db_session, seed_user)
    context = _step_context(db_session, installation)

    resources = await provision_backup_key(context)
    atoms = await _decrypted_atoms(db_session, installation.id)
    await revoke_backup_key(context, resources)

    secrets = [
        OPERATOR_APPLICATION_KEY,
        B2_AUTH_TOKEN,
        atoms["backup-cipher-pass"],
        *b2.issued_application_keys,
    ]
    assert caplog.records, "the step logs what it did"
    for secret in secrets:
        assert secret not in caplog.text
        assert secret not in json.dumps(resources)


async def test_a_retried_step_revokes_the_stale_key_and_keeps_the_cipher_pass(
    db_session, seed_user, monkeypatch
) -> None:
    from app.services.companion_backup_repo import provision_backup_key

    b2 = _wire_b2(monkeypatch)
    installation = await _backup_installation(db_session, seed_user)
    context = _step_context(db_session, installation)

    first = await provision_backup_key(context)
    cipher_pass = (await _decrypted_atoms(db_session, installation.id))["backup-cipher-pass"]
    # A worker that crashed after minting but before recording retries the step: the earlier
    # key (whose secret was never recorded as live) is revoked, never left able to write.
    second = await provision_backup_key(context)

    assert second["key_id"] != first["key_id"]
    assert list(b2.keys) == [second["key_id"]]
    assert [body["applicationKeyId"] for body in b2.named("b2_delete_key")] == [first["key_id"]]
    atoms = await _decrypted_atoms(db_session, installation.id)
    assert atoms["backup-key-id"] == second["key_id"]
    # The cipher pass is never regenerated: a repository written under it must stay readable.
    assert atoms["backup-cipher-pass"] == cipher_pass


async def test_a_key_b2_widened_is_revoked_and_refused(db_session, seed_user, monkeypatch) -> None:
    from app.services.companion_backup_repo import provision_backup_key
    from app.services.companion_lifecycle_steps import CompanionStepError

    b2 = FakeB2()
    original = b2.handle

    def widening(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path.endswith("/b2_create_key"):
            body = json.loads(response.content)
            body["capabilities"] = [*body["capabilities"], "deleteFiles"]
            return httpx.Response(200, json=body)
        return response

    monkeypatch.setattr(b2, "handle", widening)
    _wire_b2(monkeypatch, b2)
    installation = await _backup_installation(db_session, seed_user)

    with pytest.raises(CompanionStepError) as refused:
        await provision_backup_key(_step_context(db_session, installation))

    assert refused.value.code == "b2_key_not_restricted"
    # Never stored, and revoked at once: a key that can delete must not survive the step.
    assert b2.keys == {}
    assert "backup-key-id" not in await _decrypted_atoms(db_session, installation.id)


def test_the_backup_module_is_a_governed_httpx_owner() -> None:
    from scripts.outbound_api_governance import EXPECTED_EXTERNAL_IMPORTS

    assert "app/services/companion_backup_repo.py" in EXPECTED_EXTERNAL_IMPORTS["httpx"]
