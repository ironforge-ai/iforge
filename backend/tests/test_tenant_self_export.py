"""Self-service tenant export with the companion's database and media (APP-07, D-31, D-19).

A tenant starts their own export at ``POST /v1/me/exports``. When they have Postiz installed, the
job records one pending companion component per live installation; the manifest projects it to the
installation's runtime as ``exportRequests``; the runtime dumps the database on the host and uploads
it through ``PUT /v1/runtime/companion-installations/{id}/exports/{job_id}`` with the installation's
own agent key; and the export worker assembles the Phase 3 archive with the dump and every object
in the installation's media bucket beside it.

These tests drive the real routes, projection and worker against a migrated database. The worker
runs inside the test's own transaction (``_sessionmaker``), so nothing here commits past the test.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_installation import CompanionInstallation
from app.models.hosted_runtime import HostedRuntimeState
from app.models.tenant_export import TenantExportJob
from app.models.vault import Vault, VaultItem
from app.routes import runtime as runtime_routes
from app.routes import tenant_exports as tenant_exports_routes
from app.services import tenant_export as tenant_export_service
from app.services import vault_crypto
from app.services.api_key import mint_api_key
from app.services.file_store import LocalFileStore
from app.services.tenant_export import EXPORT_PRESIGNED_TTL_SECONDS
from app.services.tenant_export_worker import TenantExportWorker
from tests.test_companion_install_job import (
    _dashboard_as,
    _other_tenant,
    _runtime_client,
    _runtime_key,
    _sessionmaker,
)
from tests.test_companion_projection import _agent_with_runtime, _installation, _render

ZONE = "companions.example"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


@pytest.fixture
def export_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> LocalFileStore:
    """One local file store shared by the upload route and the export worker."""
    store = LocalFileStore(str(tmp_path / "export-files"))
    monkeypatch.setattr(runtime_routes, "get_file_store", lambda: store)
    return store


def upload_path(installation_id: UUID | str, job_id: UUID | str) -> str:
    return f"/v1/runtime/companion-installations/{installation_id}/exports/{job_id}"


def _tar_members(archive: bytes) -> dict[str, bytes]:
    members: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            assert extracted is not None
            members[member.name] = extracted.read()
    return members


async def _reload_job(db_session: AsyncSession, job_id: UUID) -> TenantExportJob:
    return (
        await db_session.execute(
            select(TenantExportJob)
            .where(TenantExportJob.id == job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


class _Companion:
    def __init__(self, environment_id: UUID, installation_id: UUID, runtime_key: str) -> None:
        self.environment_id = environment_id
        self.installation_id = installation_id
        self.runtime_key = runtime_key


async def _tenant_with_postiz(db_session: AsyncSession, seed_user) -> _Companion:
    """A renderable agent with a complete Postiz installation and its own runtime key."""
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="complete")
    state = await db_session.get(HostedRuntimeState, environment_id)
    assert state is not None
    runtime_key = await _runtime_key(db_session, seed_user, environment_id, state.deployment_id)
    return _Companion(environment_id, installation_id, runtime_key)


async def _start_export(db_session: AsyncSession, user) -> UUID:
    async with _dashboard_as(db_session, user) as dashboard:
        started = await dashboard.post("/v1/me/exports")
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["status"] == "queued"
    return UUID(body["job_id"])


def _dump_bytes(marker: str = "postiz") -> bytes:
    return gzip.compress(
        (
            "SET session_replication_role = replica;\n"
            f'COPY "public"."Organization" ("id", "name", "apiKey") FROM stdin;\n'
            f"org-1\t{marker}\t\\N\n\\.\n"
        ).encode()
    )


async def test_tenant_export_includes_postiz_database_and_media(
    db_session, seed_user, companion_zone, export_store, monkeypatch
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    job_id = await _start_export(db_session, seed_user)

    # The pending component reaches the installation's runtime as an export request.
    source = await _render(db_session, companion.environment_id)
    entry = source.manifest["companions"]["postiz"]
    assert entry["exportRequests"] == [{"jobId": str(job_id)}]

    # The worker does not assemble while the database is still owed: it defers the job.
    worker = TenantExportWorker(_sessionmaker(db_session), file_store=export_store)
    assert await worker.run_once() == job_id
    deferred = await _reload_job(db_session, job_id)
    assert deferred.status == "queued"
    assert deferred.artifact_key is None
    deferred.next_attempt_at = None
    await db_session.commit()

    dump = _dump_bytes()
    async with _runtime_client(db_session, companion.runtime_key) as runtime:
        uploaded = await runtime.put(
            upload_path(companion.installation_id, job_id),
            content=dump,
            headers={"content-type": "application/gzip"},
        )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["state"] == "uploaded"

    # Answered: the request is no longer projected.
    source = await _render(db_session, companion.environment_id)
    assert source.manifest["companions"]["postiz"]["exportRequests"] == []

    # The media half: the installation's own bucket, as its media step recorded it.
    await _give_media_bucket(db_session, companion.installation_id)
    bucket = _StubBucket(
        {
            "a1b2c3.png": b"\x89PNG-media-one",
            "nested/dir/clip.mp4": b"mp4-media-two" * 50,
            "../escape.txt": b"must stay inside media/",
        }
    )
    _stub_media(monkeypatch, bucket)

    assert await worker.run_once() == job_id
    job = await _reload_job(db_session, job_id)
    assert job.status == "complete", job.error
    assert job.artifact_key is not None
    members = _tar_members(await export_store.get(job.artifact_key))

    database_member = f"companions/postiz/{companion.installation_id}/database.sql.gz"
    assert members[database_member] == dump
    # The Phase 3 domains are still there.
    assert "users.jsonl" in members

    manifest = json.loads(members["manifest.json"])
    assert {"name": "companions", "rows": 1} in manifest["domains"]
    [facts] = manifest["companions"]
    assert facts["installation_id"] == str(companion.installation_id)
    assert facts["companion_key"] == "postiz"
    assert facts["isolation_tier"] == "standard"
    assert facts["version"].startswith("v")
    assert facts["hostname"].endswith(ZONE)
    assert facts["database_member"] == database_member
    assert facts["database_bytes"] == len(dump)
    # Non-secret facts only: no credential ref ever travels in the manifest.
    assert "secret://" not in members["manifest.json"].decode()

    # Every object in the bucket, byte-identical, under media/, with its key and size listed.
    prefix = f"companions/postiz/{companion.installation_id}/media/"
    assert set(bucket.buckets) == {MEDIA_BUCKET}
    media_members = {name: data for name, data in members.items() if name.startswith(prefix)}
    assert sorted(media_members.values()) == sorted(bucket.objects.values())
    assert members[f"{prefix}a1b2c3.png"] == bucket.objects["a1b2c3.png"]
    assert members[f"{prefix}nested/dir/clip.mp4"] == bucket.objects["nested/dir/clip.mp4"]
    # A tenant-influenced key cannot climb out of media/ when the archive is extracted.
    for name in media_members:
        assert ".." not in name.split("/"), name
    listed = {item["key"]: item for item in facts["media"]}
    assert set(listed) == set(bucket.objects)
    for key, data in bucket.objects.items():
        assert listed[key]["size"] == len(data)
        assert members[listed[key]["member"]] == data
    assert facts["media_bytes"] == sum(len(data) for data in bucket.objects.values())


# ---------------------------------------------------------------------------
# Media, bounds, deadlines, scoping (Task 3).
# ---------------------------------------------------------------------------

MEDIA_BUCKET = "postiz-media-abcdefghij12"


class _StubBucket:
    """A media bucket standing in for the installation's R2 bucket: nothing reaches a network."""

    def __init__(self, objects: dict[str, bytes], *, listed_sizes: dict[str, int] | None = None):
        self.objects = objects
        self.listed_sizes = listed_sizes or {}
        self.buckets: list[str] = []
        self.reads: list[str] = []

    def factory(self, bucket: str) -> _StubBucket:
        self.buckets.append(bucket)
        return self

    def list_object_sizes(self, prefix: str) -> list[tuple[str, int]]:
        return [
            (key, self.listed_sizes.get(key, len(data)))
            for key, data in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def get(self, key: str) -> bytes:
        self.reads.append(key)
        return self.objects[key]

    def close(self) -> None:
        return None


def _stub_media(monkeypatch: pytest.MonkeyPatch, bucket: _StubBucket) -> None:
    # `raising=False`: the export reads media through this module-level seam.
    monkeypatch.setattr(
        tenant_export_service, "default_media_client_factory", bucket.factory, raising=False
    )


async def _give_media_bucket(db_session: AsyncSession, installation_id: UUID) -> None:
    """Record the install job's ``media`` step, as plan 06-13's provisioning step does."""
    installation = await db_session.get(CompanionInstallation, installation_id)
    assert installation is not None
    installation.external_resources = {
        **(installation.external_resources or {}),
        "steps": [
            {
                "name": "media",
                "resources": {
                    "bucket": MEDIA_BUCKET,
                    "domain": f"media-abcdefghij12.{ZONE}",
                    "token_id": "token-id",
                    "account_id": "account-id",
                },
            }
        ],
    }
    await db_session.commit()


async def _upload(companion: _Companion, db_session: AsyncSession, job_id: UUID, body: bytes):
    async with _runtime_client(db_session, companion.runtime_key) as runtime:
        return await runtime.put(
            upload_path(companion.installation_id, job_id),
            content=body,
            headers={"content-type": "application/gzip"},
        )


async def test_companion_media_over_the_bound_fails_the_export_by_name(
    db_session, seed_user, companion_zone, export_store, monkeypatch
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    job_id = await _start_export(db_session, seed_user)
    assert (await _upload(companion, db_session, job_id, _dump_bytes())).status_code == 200
    await _give_media_bucket(db_session, companion.installation_id)
    # Two objects that together exceed 512 MiB, as the bucket LISTS them: nothing is read.
    bucket = _StubBucket(
        {"big-1.mp4": b"x", "big-2.mp4": b"y"},
        listed_sizes={"big-1.mp4": 300 * 1024 * 1024, "big-2.mp4": 300 * 1024 * 1024},
    )
    _stub_media(monkeypatch, bucket)

    worker = TenantExportWorker(_sessionmaker(db_session), file_store=export_store)
    assert await worker.run_once() == job_id

    job = await _reload_job(db_session, job_id)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("companion_media_exceeds_export_bound")
    assert job.artifact_key is None
    assert bucket.reads == []
    # Terminal: the same job is never retried into the same failure.
    assert job.next_attempt_at is None
    assert await worker.run_once() is None


# Each pair holds a key the mapping percent-encodes and a plain key spelling that encoding.
_ENCODED_AND_PLAIN_KEY_PAIRS = (
    ("a\\b", "a%5Cb"),
    ("photos/", "photos%2F"),
    ("x/../y", "x%2F%2E%2E%2Fy"),
)


@pytest.mark.parametrize(("encoded_key", "plain_key"), _ENCODED_AND_PLAIN_KEY_PAIRS)
def test_media_member_names_are_injective(encoded_key: str, plain_key: str) -> None:
    installation_id = uuid.uuid4()
    encoded = tenant_export_service.companion_media_member("postiz", installation_id, encoded_key)
    plain = tenant_export_service.companion_media_member("postiz", installation_id, plain_key)
    assert encoded != plain


async def test_media_keys_that_spell_an_encoding_export_as_distinct_members(
    db_session, seed_user, companion_zone, export_store, monkeypatch
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    job_id = await _start_export(db_session, seed_user)
    assert (await _upload(companion, db_session, job_id, _dump_bytes())).status_code == 200
    await _give_media_bucket(db_session, companion.installation_id)
    bucket = _StubBucket({"a\\b": b"backslash-key", "a%5Cb": b"percent-key"})
    _stub_media(monkeypatch, bucket)

    worker = TenantExportWorker(_sessionmaker(db_session), file_store=export_store)
    assert await worker.run_once() == job_id

    job = await _reload_job(db_session, job_id)
    assert job.status == "complete", job.error
    assert job.artifact_key is not None
    archive = await export_store.get(job.artifact_key)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = tar.getnames()
    assert len(names) == len(set(names)), names
    members = _tar_members(archive)
    manifest = json.loads(members["manifest.json"])
    [facts] = manifest["companions"]
    listed = {item["key"]: item["member"] for item in facts["media"]}
    assert set(listed) == set(bucket.objects)
    assert listed["a\\b"] != listed["a%5Cb"]
    for key, data in bucket.objects.items():
        assert members[listed[key]] == data


class _RepeatingBucket(_StubBucket):
    """A listing that names one key twice, so two media objects map to one archive member."""

    def list_object_sizes(self, prefix: str) -> list[tuple[str, int]]:
        listed = super().list_object_sizes(prefix)
        return listed + listed


async def test_a_media_member_collision_fails_the_export_for_good_by_name(
    db_session, seed_user, companion_zone, export_store, monkeypatch
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    job_id = await _start_export(db_session, seed_user)
    assert (await _upload(companion, db_session, job_id, _dump_bytes())).status_code == 200
    await _give_media_bucket(db_session, companion.installation_id)
    bucket = _RepeatingBucket({"clip.mp4": b"clip"})
    _stub_media(monkeypatch, bucket)

    worker = TenantExportWorker(_sessionmaker(db_session), file_store=export_store)
    assert await worker.run_once() == job_id

    job = await _reload_job(db_session, job_id)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith(tenant_export_service.COMPANION_MEDIA_MEMBER_COLLISION)
    assert job.artifact_key is None
    # Caught at listing time: no media object was read.
    assert bucket.reads == []
    # Terminal: the bucket's contents decide the names, so a retry would fail the same way.
    assert job.next_attempt_at is None
    assert await worker.run_once() is None


async def test_pending_companion_component_fails_the_export_after_30_minutes(
    db_session, seed_user, companion_zone, export_store
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    started_at = datetime.now(UTC)
    job_id = await _start_export(db_session, seed_user)

    clock = {"now": started_at + timedelta(minutes=29)}
    worker = TenantExportWorker(
        _sessionmaker(db_session), file_store=export_store, clock=lambda: clock["now"]
    )
    # At 29 minutes the database is still owed: the job waits.
    assert await worker.run_once() == job_id
    assert (await _reload_job(db_session, job_id)).status == "queued"

    clock["now"] = started_at + timedelta(minutes=31)
    assert await worker.run_once() == job_id
    job = await _reload_job(db_session, job_id)
    assert job.status == "failed"
    assert job.error is not None
    assert job.error.startswith("companion_export_component_timeout")
    assert str(companion.installation_id) in job.error
    assert job.next_attempt_at is None
    assert await worker.run_once() is None

    # A failed job asks the runtime for nothing any more, and refuses a late upload.
    source = await _render(db_session, companion.environment_id)
    assert source.manifest["companions"]["postiz"]["exportRequests"] == []
    late = await _upload(companion, db_session, job_id, _dump_bytes())
    assert late.status_code == 409, late.text


async def test_upload_from_another_agents_key_is_404(
    db_session, seed_user, companion_zone, export_store
) -> None:
    companion = await _tenant_with_postiz(db_session, seed_user)
    job_id = await _start_export(db_session, seed_user)
    # A second agent of the SAME tenant with its own genuine runtime key: tenant ownership alone
    # would admit it, the agent binding must not.
    other_agent_id = await _agent_with_runtime(db_session, seed_user)
    other_state = await db_session.get(HostedRuntimeState, other_agent_id)
    assert other_state is not None
    other_key = await _runtime_key(db_session, seed_user, other_agent_id, other_state.deployment_id)
    # And the tenant's own unbound key, bound to no agent at all.
    owner_key = (await mint_api_key(db_session, user_id=seed_user.id, label="owner")).raw_key

    for key in (other_key, owner_key):
        async with _runtime_client(db_session, key) as runtime:
            refused = await runtime.put(
                upload_path(companion.installation_id, job_id),
                content=_dump_bytes(),
                headers={"content-type": "application/gzip"},
            )
        assert refused.status_code == 404, refused.text
    assert not await export_store.exists(
        f"exports/{seed_user.id}/{job_id}/companions/postiz-{companion.installation_id}.sql.gz"
    )

    # Control: the installation's own agent key is accepted for the same upload.
    accepted = await _upload(companion, db_session, job_id, _dump_bytes())
    assert accepted.status_code == 200, accepted.text


async def test_upload_for_an_unrequested_component_is_409(
    db_session, seed_user, companion_zone, export_store
) -> None:
    # A job started BEFORE the installation existed asked for no companion data.
    earlier_job_id = await _start_export(db_session, seed_user)
    companion = await _tenant_with_postiz(db_session, seed_user)

    refused = await _upload(companion, db_session, earlier_job_id, _dump_bytes())
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "companion_export_not_requested"

    # A job that asked, once answered, does not take a second upload.
    job_id = await _start_export(db_session, seed_user)
    assert (await _upload(companion, db_session, job_id, _dump_bytes())).status_code == 200
    again = await _upload(companion, db_session, job_id, _dump_bytes())
    assert again.status_code == 409, again.text

    # Another tenant's job is not this key's to answer at all.
    other = await _other_tenant(db_session)
    other_job_id = await _start_export(db_session, other)
    foreign = await _upload(companion, db_session, other_job_id, _dump_bytes())
    assert foreign.status_code == 404, foreign.text


async def test_self_service_export_never_includes_vault_values(
    db_session, seed_user, export_store
) -> None:
    vault = Vault(user_id=seed_user.id, slug=f"vault-{uuid.uuid4().hex[:8]}", name="Self Vault")
    db_session.add(vault)
    await db_session.flush()
    plaintext = f"self-export-plaintext-{uuid.uuid4().hex}"
    ciphertext, nonce = vault_crypto.encrypt(plaintext)
    db_session.add(
        VaultItem(
            vault_id=vault.id,
            item_name="service-credential-item",
            section="prod",
            encrypted_value=ciphertext,
            nonce=nonce,
        )
    )
    await db_session.commit()

    # Even a body asking for vault values is not a way in: the route has no such field.
    async with _dashboard_as(db_session, seed_user) as dashboard:
        started = await dashboard.post("/v1/me/exports", json={"include_vault_values": True})
    assert started.status_code == 202, started.text
    job_id = UUID(started.json()["job_id"])
    assert (await _reload_job(db_session, job_id)).include_vault_values is False

    worker = TenantExportWorker(_sessionmaker(db_session), file_store=export_store)
    assert await worker.run_once() == job_id
    job = await _reload_job(db_session, job_id)
    assert job.status == "complete", job.error
    assert job.artifact_key is not None
    archive = await export_store.get(job.artifact_key)
    members = _tar_members(archive)
    manifest = json.loads(members["manifest.json"])
    assert manifest["vault_values_included"] is False
    vault_records = [
        json.loads(line) for line in members["vaults.jsonl"].decode().splitlines() if line
    ]
    assert all("value" not in item for record in vault_records for item in record["items"])
    for data in members.values():
        assert plaintext.encode() not in data


class _RecordingPresignStore(LocalFileStore):
    def __init__(self, base_path: str) -> None:
        super().__init__(base_path)
        self.presign_calls: list[tuple[str, int]] = []

    async def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        self.presign_calls.append((key, expires_in_seconds))
        return f"https://presigned.example/{key}?ttl={expires_in_seconds}"


async def _jobs_for(db_session: AsyncSession, user_id: UUID) -> tuple[UUID, UUID]:
    """One complete job with an artifact and one still queued."""
    now = datetime.now(UTC)
    complete = TenantExportJob(
        user_id=user_id,
        status="complete",
        artifact_key=f"exports/{user_id}/done.tar.gz",
        completed_at=now,
        created_at=now - timedelta(minutes=2),
    )
    queued = TenantExportJob(user_id=user_id, status="queued", created_at=now)
    db_session.add_all([complete, queued])
    await db_session.commit()
    return complete.id, queued.id


async def test_tenant_lists_and_downloads_only_their_own_exports(
    db_session, seed_user, tmp_path, monkeypatch
) -> None:
    store = _RecordingPresignStore(str(tmp_path / "presign-files"))
    monkeypatch.setattr(tenant_exports_routes, "get_file_store", lambda: store)
    own_complete, own_queued = await _jobs_for(db_session, seed_user.id)
    other = await _other_tenant(db_session)
    other_complete, _ = await _jobs_for(db_session, other.id)

    async with _dashboard_as(db_session, seed_user) as dashboard:
        listed = await dashboard.get("/v1/me/exports")
        downloaded = await dashboard.get(f"/v1/me/exports/{own_complete}/download")
        not_ready = await dashboard.get(f"/v1/me/exports/{own_queued}/download")
        foreign = await dashboard.get(f"/v1/me/exports/{other_complete}/download")

    assert listed.status_code == 200, listed.text
    assert listed.headers["cache-control"] == "no-store"
    items = listed.json()["items"]
    # Only the caller's jobs, newest first, with the same states and flags as the admin list.
    assert [item["job_id"] for item in items] == [str(own_queued), str(own_complete)]
    assert [item["status"] for item in items] == ["queued", "complete"]
    assert [item["downloadable"] for item in items] == [False, True]
    assert set(items[0]) == {
        "job_id",
        "status",
        "created_at",
        "completed_at",
        "downloadable",
        "error",
    }

    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["cache-control"] == "no-store"
    body = downloaded.json()
    assert body["url"] == (
        f"https://presigned.example/exports/{seed_user.id}/done.tar.gz"
        f"?ttl={EXPORT_PRESIGNED_TTL_SECONDS}"
    )
    expires_at = datetime.fromisoformat(body["expires_at"])
    assert (
        timedelta(seconds=EXPORT_PRESIGNED_TTL_SECONDS - 60)
        < expires_at - datetime.now(UTC)
        <= timedelta(seconds=EXPORT_PRESIGNED_TTL_SECONDS)
    )
    # One link minted, for the caller's own artifact only, and audited once.
    assert store.presign_calls == [
        (f"exports/{seed_user.id}/done.tar.gz", EXPORT_PRESIGNED_TTL_SECONDS)
    ]
    issued = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.export_link_issued",
                ControlPlaneAuditEvent.resource_id == str(own_complete),
            )
        )
    ).scalar_one()
    assert "http" not in str(issued.details)

    assert not_ready.status_code == 409, not_ready.text
    assert not_ready.json()["detail"]["code"] == "export_not_downloadable"
    assert foreign.status_code == 404, foreign.text

    # The list and download require the dashboard: an API key is not a way in.
    owner_key = (await mint_api_key(db_session, user_id=seed_user.id, label="owner")).raw_key
    async with _runtime_client(db_session, owner_key) as keyed:
        assert (await keyed.get("/v1/me/exports")).status_code == 403
        assert (await keyed.post("/v1/me/exports")).status_code == 403


async def test_operator_export_of_a_companion_tenant_promises_the_companion_data(
    db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """The operator's export is the same job: it waits for the companion's data too."""
    import httpx
    from httpx import ASGITransport

    from app.core.database import get_session
    from app.main import app

    companion = await _tenant_with_postiz(db_session, seed_user)
    admin_key = f"test-admin-{uuid.uuid4().hex}"
    monkeypatch.setattr(settings, "admin_api_key", admin_key)

    async def _override_get_session():
        yield db_session

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as admin:
            created = await admin.post(
                f"/v1/admin/tenants/{seed_user.id}/exports",
                json={},
                headers={"X-Admin-Key": admin_key},
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
    assert created.status_code == 201, created.text
    job_id = UUID(created.json()["job_id"])

    source = await _render(db_session, companion.environment_id)
    assert source.manifest["companions"]["postiz"]["exportRequests"] == [{"jobId": str(job_id)}]


async def test_tenant_exports_have_no_hidden_api_alias(db_session, seed_user) -> None:
    """Canonical-only: the `/api` spelling of the tenant export routes does not exist."""
    async with _dashboard_as(db_session, seed_user) as dashboard:
        assert (await dashboard.post("/api/me/exports")).status_code == 404
        assert (await dashboard.get("/api/me/exports")).status_code == 404
        assert (await dashboard.get("/v1/me/exports")).status_code == 200
