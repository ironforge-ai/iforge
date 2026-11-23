"""Tenant export coverage (ACCESS-04 export half, D-10..D-13).

Task 1: per-domain allowlist serializers + tar.gz assembly + manifest
(`assemble_tenant_export`), with the D-10 guarantee that the default
archive is never a credentials bundle — every record key is checked
against the audit sanitizer's `_is_secretish_key` predicate, and the
`EXPORT_COVERAGE` map gives every `channel_*` / `hosted_runtime_*`
table an explicit exported/excluded disposition (review finding 6).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_plugin import (
    AgentPluginInstallation,
    PluginCatalogEntry,
    PluginCatalogSnapshot,
)
from app.models.api_key import ApiKey
from app.models.audit import ControlPlaneAuditEvent
from app.models.base import Base
from app.models.channel import ChannelAccount, ChannelSecret
from app.models.companion_approval import CompanionApproval
from app.models.companion_budget import CompanionSpendCeiling
from app.models.companion_session_budget import CompanionCapPolicy
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.models.memory import Memory
from app.models.session import Session, SessionEventChunk, SessionEventGeneration
from app.models.skill import AgentSkillReference, Skill
from app.models.tenant_export import TenantExportJob
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.models.vault import Vault, VaultItem, VaultSecretRequest
from app.routes import session_events as session_events_routes
from app.routes import sessions as sessions_routes
from app.services import tenant_export as tenant_export_service
from app.services import vault_crypto
from app.services.audit import _is_secretish_key, record_control_plane_audit
from app.services.file_store import FileStore, LocalFileStore
from app.services.session_content import SessionContentUnavailable
from app.services.session_events import EMPTY_EVENT_HEAD, advance_event_head
from app.services.tenant_export import (
    EXPORT_COVERAGE,
    EXPORT_PRESIGNED_TTL_SECONDS,
    ExportMemberCollisionError,
    assemble_tenant_export,
)
from app.services.tenant_export_worker import (
    TenantExportClaimLostError,
    TenantExportWorker,
    claim_tenant_export,
    complete_tenant_export,
    record_tenant_export_failure,
)
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state
from tests.test_session_shares import _event, _event_chunk, _seed_event_session

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _tar_members(archive: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            assert extracted is not None
            result[member.name] = extracted.read()
    return result


def _jsonl_records(payload: bytes) -> list[dict]:
    return [json.loads(line) for line in payload.decode("utf-8").splitlines() if line.strip()]


def _iter_keys(value: object) -> Iterator[str]:
    """Yield every mapping key recursively (lists included)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _iter_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_keys(item)


def _manifest(members: dict[str, bytes]) -> dict:
    assert "manifest.json" in members
    return json.loads(members["manifest.json"])


def _domain_rows(manifest: dict) -> dict[str, int]:
    return {entry["name"]: entry["rows"] for entry in manifest["domains"]}


async def _seed_tenant(
    db_session: AsyncSession,
    file_store: LocalFileStore,
    user: User,
    *,
    marker: str,
) -> dict[str, object]:
    """Seed one tenant with data in every export domain.

    `marker` is a distinctive string planted in the session body and
    memory content so cross-tenant bleed is detectable byte-wise.
    """
    env = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"export-env-{uuid.uuid4().hex[:8]}",
        machine_name="Export Test Agent",
        agent_type="hermes",
    )
    runtime_state = await create_test_hosted_runtime_state(db_session, env, runtime_name="hermes")

    local_session_id = f"session-{uuid.uuid4().hex[:12]}"
    session_body = json.dumps([{"role": "user", "content": f"hello {marker}"}]).encode()
    session_file_key = f"sessions/{user.id}/{local_session_id}.json"
    await file_store.put(session_file_key, session_body)
    session = Session(
        user_id=user.id,
        environment_id=env.id,
        local_session_id=local_session_id,
        started_at=datetime.now(UTC),
        file_key=session_file_key,
        content_hash="a" * 64,
    )
    db_session.add(session)
    await db_session.flush()

    usage = UsageEvent(
        session_id=session.id,
        ts=datetime.now(UTC),
        provider="test-provider",
        model="test-model",
        in_tok=10,
        out_tok=20,
        cost_usd_micros=42,
    )
    db_session.add(usage)

    skill_key = f"skill-{uuid.uuid4().hex[:8]}"
    skill_hash = "b" * 64
    skill_archive = f"archive-bytes-{marker}".encode()
    skill_file_key = f"skills/{user.id}/{env.default_project_id}/{skill_key}/{skill_hash}.tar.gz"
    await file_store.put(skill_file_key, skill_archive)
    skill = Skill(
        user_id=user.id,
        project_id=env.default_project_id,
        skill_key=skill_key,
        name="Export Skill",
        content_hash=skill_hash,
        file_key=skill_file_key,
    )
    db_session.add(skill)

    memory = Memory(user_id=user.id, content=f"memory {marker}", category="fact")
    db_session.add(memory)

    vault = Vault(user_id=user.id, slug=f"vault-{uuid.uuid4().hex[:8]}", name="Export Vault")
    db_session.add(vault)
    await db_session.flush()
    ciphertext, nonce = vault_crypto.encrypt(f"super-plaintext-{marker}")
    vault_item = VaultItem(
        vault_id=vault.id,
        item_name="service-credential-item",
        section="prod",
        encrypted_value=ciphertext,
        nonce=nonce,
    )
    db_session.add(vault_item)

    account = ChannelAccount(
        user_id=user.id,
        provider="telegram",
        name=f"bot-{uuid.uuid4().hex[:8]}",
        webhook_secret_hash="0" * 64,
        config={"bot_username": "export_test_bot"},
    )
    db_session.add(account)
    await db_session.flush()
    secret_cipher, secret_nonce = vault_crypto.encrypt(f"channel-secret-{marker}")
    channel_secret = ChannelSecret(
        account_id=account.id,
        user_id=user.id,
        name="bot_token",
        encrypted_value=secret_cipher,
        value_nonce=secret_nonce,
    )
    db_session.add(channel_secret)

    api_key = ApiKey(
        user_id=user.id,
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        key_prefix="clawdi_te",
        label="export-test-key",
    )
    db_session.add(api_key)

    extensions = await _seed_export_extensions(db_session, user, env=env, vault=vault, skill=skill)

    record_control_plane_audit(
        db_session,
        actor_type="admin",
        action="test.export_seed",
        resource_type="user",
        resource_id=str(user.id),
        source="test",
        target_user_id=user.id,
        details={"seed": True},
    )
    await db_session.commit()

    return {
        "env": env,
        "runtime_state": runtime_state,
        "session": session,
        "local_session_id": local_session_id,
        "session_body": session_body,
        "skill": skill,
        "skill_key": skill_key,
        "skill_archive": skill_archive,
        "vault": vault,
        "vault_plaintext": f"super-plaintext-{marker}",
        "account": account,
        **extensions,
    }


async def _seed_export_extensions(
    db_session: AsyncSession, user: User, *, env, vault: Vault, skill: Skill
) -> dict[str, object]:
    """Seed the INT-3 tables: the 06.1 agent attachments and vault field request, the companion
    activity log with its approval, and the tenant's companion controls. Flushed, not committed.

    Every secret-derived column carries a distinctive value so a test can prove it never reaches
    the archive: the request's token hash and per-field baselines, and the JSONB arguments."""
    now = datetime.now(UTC)
    await db_session.flush()

    revision = hashlib.sha1(uuid.uuid4().bytes).hexdigest()
    db_session.add(
        PluginCatalogSnapshot(revision=revision, schema_version=2, entry_count=1, fetched_at=now)
    )
    await db_session.flush()
    plugin_source = {"type": "github", "url": "https://example.test/store", "commit": revision}
    db_session.add(
        PluginCatalogEntry(
            snapshot_revision=revision,
            name="acme-tools",
            version="1.0.0",
            agent_plugins_schema="https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            source=plugin_source,
            content_digest=f"sha256-tree-v1:{'c' * 64}",
            public_metadata={"display_name": "Acme Tools"},
            compatible_runtimes=["hermes"],
        )
    )
    await db_session.flush()
    plugin = AgentPluginInstallation(
        environment_id=env.id,
        plugin_name="acme-tools",
        catalog_revision=revision,
        version="1.0.0",
        agent_plugins_schema="https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
        source=plugin_source,
        content_digest=f"sha256-tree-v1:{'c' * 64}",
    )
    db_session.add(plugin)
    db_session.add(AgentSkillReference(agent_id=env.id, skill_id=skill.id))

    token_hash = hashlib.sha256(f"request-token-{uuid.uuid4()}".encode()).hexdigest()
    baseline = hashlib.sha256(f"request-baseline-{uuid.uuid4()}".encode()).hexdigest()
    vault_request = VaultSecretRequest(
        vault_id=vault.id,
        project_id=env.default_project_id,
        section="prod",
        fields=["SERVICE_URL"],
        field_baselines={"SERVICE_URL": baseline},
        extra_fields=[],
        token_hash=token_hash,
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(vault_request)

    approval = CompanionApproval(
        user_id=user.id,
        environment_id=env.id,
        companion_key="filebrowser",
        verb="delete_path",
        canonical_action_sha256=hashlib.sha256(f"approval-{uuid.uuid4()}".encode()).hexdigest(),
        arguments={"path": "/approval-argument-marker"},
        resolved_target_count=1,
        remaining_budget=1,
        status="prepared",
        expires_at=now + timedelta(minutes=15),
        decided_at=now,
        decided_by_user_id=user.id,
    )
    db_session.add(approval)
    await db_session.flush()
    db_session.add(
        CompanionToolCall(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="delete_path",
            tool_class="destroy",
            arguments_digest=hashlib.sha256(f"call-{uuid.uuid4()}".encode()).hexdigest(),
            arguments={"path": "/call-argument-marker"},
            decision="allowed",
            approval_id=approval.id,
            approved_by_user_id=user.id,
        )
    )
    db_session.add(
        CompanionToolPolicy(
            user_id=user.id, companion_key="filebrowser", verb="write_file", mode="allow"
        )
    )
    db_session.add(
        CompanionDestroyEnablement(
            user_id=user.id, companion_key="filebrowser", enabled_at=now, enabled_by_user_id=user.id
        )
    )
    db_session.add(
        CompanionGroupBlock(
            user_id=user.id, companion_key="filebrowser", blocked_at=now, blocked_by_user_id=user.id
        )
    )
    db_session.add(CompanionCapPolicy(user_id=user.id, companion_key="filebrowser"))
    db_session.add(CompanionSpendCeiling(user_id=user.id, ceiling_usd_micros=25_000_000))
    await db_session.flush()
    return {
        "plugin_installation": plugin,
        "vault_request": vault_request,
        "vault_request_token_hash": token_hash,
        "vault_request_baseline": baseline,
        "approval": approval,
    }


@pytest.fixture
def file_store(tmp_path) -> LocalFileStore:
    return LocalFileStore(str(tmp_path / "files"))


# --------------------------------------------------------------------------
# Assembly + manifest
# --------------------------------------------------------------------------


async def test_assembly_manifest_counts_match_seeded_rows(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """The archive contains manifest.json plus one .jsonl per non-empty
    domain, with manifest row counts matching what was seeded."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="alpha")

    archive, manifest = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=False
    )
    members = _tar_members(archive)
    assert _manifest(members) == manifest
    assert manifest["export_version"] == 1
    assert manifest["user_id"] == str(seed_user.id)
    assert manifest["vault_values_included"] is False

    rows = _domain_rows(manifest)
    assert rows["users"] == 1
    assert rows["projects"] == 2  # Personal + env-local project
    assert rows["sessions"] == 1
    assert rows["skills"] == 1
    assert rows["memories"] == 1
    assert rows["vaults"] == 1
    assert rows["channels"] == 1
    assert rows["usage_events"] == 1
    assert rows["agents"] == 1
    assert rows["api_keys"] == 1
    assert rows["audit"] == 1
    assert rows["hosted_runtimes"] == 2  # state + config observation
    assert rows["agent_extensions"] == 2  # plugin installation + skill reference
    assert rows["companion_activity"] == 2  # tool call + approval
    assert rows["companion_settings"] == 5  # policy, destroy, block, cap, ceiling

    for name, count in rows.items():
        if count > 0:
            assert f"{name}.jsonl" in members, f"missing {name}.jsonl"
            assert len(_jsonl_records(members[f"{name}.jsonl"])) == count

    # Session bodies and skill archives travel as binary members, each body named by its
    # immutable session id (06.1-20).
    assert members[f"sessions/{seeded['session'].id}.json"] == seeded["session_body"]
    skill = seeded["skill"]
    skill_member = f"skills/{skill.project_id}/{seeded['skill_key']}/{skill.content_hash}.tar.gz"
    assert members[skill_member] == seeded["skill_archive"]


async def test_serialized_session_and_usage_fields_are_allowlisted(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """Spot-check serializer allowlists: sessions carry token counts under
    non-secretish names; usage events carry the recorded cost integer."""
    await _seed_tenant(db_session, file_store, seed_user, marker="fields")
    archive, _ = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=False
    )
    members = _tar_members(archive)

    (session_record,) = _jsonl_records(members["sessions.jsonl"])
    assert session_record["local_session_id"].startswith("session-")
    assert "in_tok" in session_record and "out_tok" in session_record
    assert "input_tokens" not in session_record  # secretish-shaped name renamed

    (usage_record,) = _jsonl_records(members["usage_events.jsonl"])
    assert usage_record["cost_usd_micros"] == 42
    assert usage_record["model"] == "test-model"


# --------------------------------------------------------------------------
# Secretish guard (D-10)
# --------------------------------------------------------------------------


async def test_default_archive_has_no_secretish_keys_and_no_vault_values(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """Iterating every JSONL record's keys across the default archive, no
    key satisfies `_is_secretish_key`; vaults.jsonl carries item names and
    sections but no value/plaintext fields (D-10)."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="sec")
    archive, _ = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=False
    )
    members = _tar_members(archive)

    for name, payload in members.items():
        if not name.endswith(".jsonl"):
            continue
        for record in _jsonl_records(payload):
            for key in _iter_keys(record):
                assert not _is_secretish_key(key), f"secretish key {key!r} in {name}"

    (vault_record,) = _jsonl_records(members["vaults.jsonl"])
    assert vault_record["name"] == "Export Vault"
    (item,) = vault_record["items"]
    assert item["item_name"] == "service-credential-item"
    assert item["section"] == "prod"
    assert "value" not in item
    assert "plaintext" not in item

    # The decrypted vault plaintext must not appear anywhere in the archive.
    plaintext = str(seeded["vault_plaintext"]).encode()
    assert plaintext not in archive
    for payload in members.values():
        assert plaintext not in payload


async def test_excluded_secretish_table_columns_never_appear(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """No column of an `excluded:secretish` table (channel_secrets,
    channel_agent_credentials, channel_whatsapp_auth_certs,
    hosted_runtime_secrets) appears as a record key anywhere (secretish
    exclusion is structural, not just name-based)."""
    await _seed_tenant(db_session, file_store, seed_user, marker="excl")
    archive, _ = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=False
    )
    members = _tar_members(archive)

    forbidden_keys = {
        "encrypted_value",
        "value_nonce",
        "nonce",
        "secret_ref",
        "encrypted_credentials",
        "credential_nonce",
        "identity_public_key",
        "identity_pub_key_hash",
        "root_public_key",
        "encrypted_root_private_key",
        "encrypted_intermediate_private_key",
        "key_hash",
        "webhook_secret_hash",
    }
    for name, payload in members.items():
        if not name.endswith(".jsonl"):
            continue
        for record in _jsonl_records(payload):
            present = forbidden_keys.intersection(_iter_keys(record))
            assert not present, f"excluded column keys {present} leaked into {name}"


# --------------------------------------------------------------------------
# Vault opt-in (D-10)
# --------------------------------------------------------------------------


async def test_include_vault_values_decrypts_items_and_flips_manifest(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """include_vault_values=True adds decrypted vault values via the vault
    crypto service and flips manifest.vault_values_included."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="optin")
    archive, manifest = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=True
    )
    assert manifest["vault_values_included"] is True

    members = _tar_members(archive)
    (vault_record,) = _jsonl_records(members["vaults.jsonl"])
    (item,) = vault_record["items"]
    assert item["value"] == seeded["vault_plaintext"]


# --------------------------------------------------------------------------
# Cross-tenant scoping (T-03-20)
# --------------------------------------------------------------------------


async def test_scoping_second_tenant_data_never_appears(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore, test_identity: str
) -> None:
    """A control tenant's data never appears in the first tenant's
    archive — every serializer filters on user_id."""
    control_user = User(
        clerk_id=f"control_{test_identity}",
        email=f"control-{test_identity}@clawdi.local",
        name="Control User",
    )
    db_session.add(control_user)
    await db_session.flush()
    from app.models.project import PROJECT_KIND_PERSONAL, Project

    db_session.add(
        Project(
            user_id=control_user.id,
            name="Personal",
            slug="personal",
            kind=PROJECT_KIND_PERSONAL,
        )
    )
    await db_session.commit()

    await _seed_tenant(db_session, file_store, seed_user, marker="mine")
    control_seed = await _seed_tenant(db_session, file_store, control_user, marker="FOREIGN")

    archive, _ = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=True
    )
    assert b"FOREIGN" not in archive
    members = _tar_members(archive)
    for payload in members.values():
        assert b"FOREIGN" not in payload
        assert str(control_user.id).encode() not in payload
        assert str(control_seed["local_session_id"]).encode() not in payload


# --------------------------------------------------------------------------
# Read-only guarantee (RESEARCH anti-pattern)
# --------------------------------------------------------------------------


async def _read_only_tenant_counts(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Per-table row counts for ONE tenant, over every table the export reads.

    Mirrors `_tenant_row_counts` in test_tenant_export_delete_roundtrip.py:
    every count carries a tenant predicate. Five tables have a direct
    `user_id`; the other three are reached the same way their serializers
    reach them — `VaultItem` through its `Vault`, `UsageEvent` through its
    `Session`, `ChannelSecret` through its `ChannelAccount`. `ChannelSecret`
    also carries a nullable denormalised `user_id`, but the account join is
    the ownership path the purge uses and does not depend on that column
    being populated.
    """
    counts: dict[str, int] = {}
    for label, model in (
        ("sessions", Session),
        ("skills", Skill),
        ("memories", Memory),
        ("channel_accounts", ChannelAccount),
        ("api_keys", ApiKey),
    ):
        counts[label] = (
            await db.execute(
                select(func.count()).select_from(model).where(model.user_id == user_id)
            )
        ).scalar_one()

    counts["vault_items"] = (
        await db.execute(
            select(func.count())
            .select_from(VaultItem)
            .join(Vault, VaultItem.vault_id == Vault.id)
            .where(Vault.user_id == user_id)
        )
    ).scalar_one()
    counts["usage_events"] = (
        await db.execute(
            select(func.count())
            .select_from(UsageEvent)
            .join(Session, UsageEvent.session_id == Session.id)
            .where(Session.user_id == user_id)
        )
    ).scalar_one()
    counts["channel_secrets"] = (
        await db.execute(
            select(func.count())
            .select_from(ChannelSecret)
            .join(ChannelAccount, ChannelSecret.account_id == ChannelAccount.id)
            .where(ChannelAccount.user_id == user_id)
        )
    ).scalar_one()
    return counts


async def test_assembly_is_read_only_over_tenant_tables(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore, test_identity: str
) -> None:
    """The export mutates NOTHING belonging to the tenant under test — and a
    concurrent writer elsewhere cannot make that claim pass or fail by
    accident (WR-12).

    Every count is scoped to `seed_user`, so this is now both stronger and
    stabler than the global count it replaces. Stronger: a global count
    would still have passed if the export mutated a *different* tenant's
    rows in a count-preserving way, which is precisely the failure the test
    exists to catch. Stabler: under READ COMMITTED with pytest-xdist, a
    sibling worker committing any row in these eight tables between the two
    snapshots flipped the old assertion.

    The interference case below is that sibling, made deterministic: a
    second tenant's full row set is committed BETWEEN the snapshots. At the
    unscoped shape it fails; scoped, it cannot.
    """
    await _seed_tenant(db_session, file_store, seed_user, marker="ro")
    # Pinned BEFORE the rollback below: a rolled-back session expires its
    # identity map, and re-reading `seed_user.id` afterwards would trigger a
    # lazy refresh outside the async greenlet.
    seed_user_id = seed_user.id

    before = await _read_only_tenant_counts(db_session, seed_user_id)

    # --- interference: an unrelated tenant commits rows mid-test ---------
    from app.models.project import PROJECT_KIND_PERSONAL, Project

    noisy_user = User(
        clerk_id=f"noisy_{test_identity}",
        email=f"noisy-{test_identity}@clawdi.local",
        name="Noisy Neighbour",
    )
    db_session.add(noisy_user)
    await db_session.flush()
    db_session.add(
        Project(
            user_id=noisy_user.id,
            name="Personal",
            slug="personal",
            kind=PROJECT_KIND_PERSONAL,
        )
    )
    await db_session.commit()
    await _seed_tenant(db_session, file_store, noisy_user, marker="NOISE")
    noisy_counts = await _read_only_tenant_counts(db_session, noisy_user.id)
    # The interference is real: it wrote to every table under assertion.
    assert all(count > 0 for count in noisy_counts.values()), noisy_counts

    await assemble_tenant_export(db_session, file_store, seed_user_id, include_vault_values=False)
    await db_session.rollback()

    assert await _read_only_tenant_counts(db_session, seed_user_id) == before


# --------------------------------------------------------------------------
# EXPORT_COVERAGE completeness (review finding 6)
# --------------------------------------------------------------------------


async def test_export_coverage_completeness_and_secretish_dispositions() -> None:
    """Every tenant-owned table in Base.metadata -- a tenant column of its own, or a foreign key
    path to one (INT-3) -- and every `channel_*` / `hosted_runtime_*` / `session_event_*` table
    has an explicit EXPORT_COVERAGE disposition; regression pins on the named secretish
    exclusions and the domain mappings."""
    # The purge guard's inventory, so export and purge sweep the same set of tables. Importing it
    # registers every model module the way alembic's env.py does.
    from app.services.tenant_export import COMPANIONS_EXPORT_DOMAIN, EXPORT_DOMAINS
    from tests.test_tenant_lifecycle_completeness import tenant_owned_tables

    owned = tenant_owned_tables()
    prefixed = {
        name
        for name in Base.metadata.tables
        if name.startswith(("channel_", "hosted_runtime_", "session_event_"))
    }
    assert prefixed, "expected channel_*/hosted_runtime_*/session_event_* tables in metadata"
    assert any(name.startswith("session_event_") for name in prefixed), sorted(prefixed)

    missing = (set(owned) | prefixed) - set(EXPORT_COVERAGE)
    assert not missing, (
        "EXPORT_COVERAGE is missing dispositions (add one to "
        f"backend/app/services/tenant_export.py): "
        f"{sorted((name, owned.get(name, 'prefix')) for name in missing)}"
    )
    stale = set(EXPORT_COVERAGE) - set(Base.metadata.tables)
    assert not stale, f"EXPORT_COVERAGE names tables not in metadata: {sorted(stale)}"

    known_domains = {*EXPORT_DOMAINS, COMPANIONS_EXPORT_DOMAIN}
    for table, disposition in EXPORT_COVERAGE.items():
        assert disposition.startswith(("domain:", "excluded:")), (table, disposition)
        if disposition.startswith("domain:"):
            assert disposition.split(":", 1)[1] in known_domains, (table, disposition)
    # Every declared domain is fed by at least one table.
    fed = {d.split(":", 1)[1] for d in EXPORT_COVERAGE.values() if d.startswith("domain:")}
    assert set(EXPORT_DOMAINS) <= fed, sorted(set(EXPORT_DOMAINS) - fed)

    # INT-3 pins: the activity log, approvals, companion controls and the 06.1 tables export.
    assert EXPORT_COVERAGE["companion_tool_calls"] == "domain:companion_activity"
    assert EXPORT_COVERAGE["companion_approvals"] == "domain:companion_activity"
    for name in (
        "companion_tool_policies",
        "companion_destroy_enablements",
        "companion_group_blocks",
        "companion_cap_policies",
        "companion_spend_ceilings",
    ):
        assert EXPORT_COVERAGE[name] == "domain:companion_settings", name
    assert EXPORT_COVERAGE["agent_plugin_installations"] == "domain:agent_extensions"
    assert EXPORT_COVERAGE["agent_skill_references"] == "domain:agent_extensions"
    assert EXPORT_COVERAGE["vault_secret_requests"] == "domain:vaults"
    for name in (
        "ai_provider_auth_payloads",
        "companion_installation_secrets",
        "device_authorizations",
        "vault_credential_profiles",
    ):
        assert EXPORT_COVERAGE[name] == "excluded:secretish", name

    # Regression pins (review finding 6).
    assert EXPORT_COVERAGE["hosted_runtime_states"] == "domain:hosted_runtimes"
    assert EXPORT_COVERAGE["hosted_runtime_config_observations"] == "domain:hosted_runtimes"
    assert EXPORT_COVERAGE["hosted_runtime_secrets"] == "excluded:secretish"
    assert EXPORT_COVERAGE["channel_secrets"] == "excluded:secretish"
    assert EXPORT_COVERAGE["channel_agent_credentials"] == "excluded:secretish"
    assert EXPORT_COVERAGE["channel_whatsapp_auth_certs"] == "excluded:secretish"
    assert EXPORT_COVERAGE["channel_accounts"] == "domain:channels"
    # The events-v1 tables (upstream sync steps 3-4): the current committed generation is the
    # session's exported body; the append receipts are upload bookkeeping.
    assert EXPORT_COVERAGE["session_event_generations"] == "domain:sessions"
    assert EXPORT_COVERAGE["session_event_chunks"] == "domain:sessions"
    assert EXPORT_COVERAGE["session_event_append_receipts"].startswith("excluded:")


async def test_int3_domains_export_allowlisted_records_and_no_secret_derived_values(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """INT-3: the activity log, approvals, companion controls, the agent attachments and the
    vault field requests are in the archive, and nothing derived from a secret or carrying
    caller-defined keys is: no request token hash, no per-field baseline, no JSONB arguments --
    not even with the vault opt-in."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="int3")
    await db_session.commit()
    for include_vault_values in (False, True):
        archive, _ = await assemble_tenant_export(
            db_session, file_store, seed_user.id, include_vault_values=include_vault_values
        )
        members = _tar_members(archive)

        (vault_record,) = _jsonl_records(members["vaults.jsonl"])
        (field_request,) = vault_record["field_requests"]
        assert field_request["id"] == str(seeded["vault_request"].id)
        assert field_request["fields"] == ["SERVICE_URL"]
        assert field_request["section"] == "prod"
        assert set(field_request) == {
            "id",
            "project_id",
            "section",
            "fields",
            "extra_fields",
            "expires_at",
            "supplied_at",
            "conflicted_at",
            "created_at",
        }
        for forbidden in (
            str(seeded["vault_request_token_hash"]).encode(),
            str(seeded["vault_request_baseline"]).encode(),
            b"approval-argument-marker",
            b"call-argument-marker",
        ):
            assert forbidden not in archive
            assert not any(forbidden in payload for payload in members.values()), forbidden

        extensions = _jsonl_records(members["agent_extensions.jsonl"])
        assert {record["table"] for record in extensions} == {
            "agent_plugin_installations",
            "agent_skill_references",
        }
        plugin_record = next(r for r in extensions if r["table"] == "agent_plugin_installations")
        assert plugin_record["plugin_name"] == "acme-tools"
        assert "source" not in plugin_record
        reference = next(r for r in extensions if r["table"] == "agent_skill_references")
        assert reference["skill_id"] == str(seeded["skill"].id)

        activity = _jsonl_records(members["companion_activity.jsonl"])
        call = next(r for r in activity if r["table"] == "companion_tool_calls")
        approval = next(r for r in activity if r["table"] == "companion_approvals")
        assert call["approval_id"] == approval["id"] == str(seeded["approval"].id)
        assert call["decision"] == "allowed"
        for record in activity:
            assert "arguments" not in record and "dry_run_diff" not in record

        settings_tables = sorted(
            record["table"] for record in _jsonl_records(members["companion_settings.jsonl"])
        )
        assert settings_tables == [
            "companion_cap_policies",
            "companion_destroy_enablements",
            "companion_group_blocks",
            "companion_spend_ceilings",
            "companion_tool_policies",
        ]


async def test_hosted_runtimes_domain_exports_metadata_only(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """hosted_runtimes.jsonl appears when the tenant has hosted runtime
    state; records carry deployment metadata, never secret values or
    manifest JSONB blobs."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="hr")
    archive, manifest = await assemble_tenant_export(
        db_session, file_store, seed_user.id, include_vault_values=False
    )
    members = _tar_members(archive)
    assert "hosted_runtimes.jsonl" in members

    records = _jsonl_records(members["hosted_runtimes.jsonl"])
    tables = {record["table"] for record in records}
    assert tables == {"hosted_runtime_states", "hosted_runtime_config_observations"}
    state_record = next(r for r in records if r["table"] == "hosted_runtime_states")
    runtime_state = seeded["runtime_state"]
    assert state_record["deployment_id"] == runtime_state.deployment_id
    assert state_record["environment_id"] == str(runtime_state.environment_id)
    # JSONB manifest blobs stay out of the export (metadata-only allowlist).
    assert "runtimes" not in state_record
    assert "companions" not in state_record
    assert "live_sync" not in state_record
    assert _domain_rows(manifest)["hosted_runtimes"] == len(records)


# --------------------------------------------------------------------------
# Task 2: TenantExportWorker — durable claim / complete / retry (D-12)
# --------------------------------------------------------------------------


class _FailingFileStore(LocalFileStore):
    """File store whose put() always raises — forces assembly failure."""

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        raise RuntimeError("synthetic storage outage")


async def _create_committed_user(session_factory, *, nonce: str) -> User:
    async with session_factory() as db:
        user = User(
            clerk_id=f"export-worker-{nonce}",
            email=f"export-worker-{nonce}@clawdi.local",
            name="Export Worker User",
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def _create_committed_job(
    session_factory, *, user_id, include_vault_values: bool = False
) -> TenantExportJob:
    async with session_factory() as db:
        job = TenantExportJob(user_id=user_id, include_vault_values=include_vault_values)
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job


async def _cleanup_worker_rows(session_factory, *, user_id) -> None:
    from sqlalchemy import delete

    async with session_factory() as db:
        await db.execute(delete(TenantExportJob).where(TenantExportJob.user_id == user_id))
        await db.execute(
            delete(ControlPlaneAuditEvent).where(ControlPlaneAuditEvent.target_user_id == user_id)
        )
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.committed_db
async def test_worker_run_once_claims_assembles_stores_and_completes(engine, tmp_path) -> None:
    """run_once claims a queued job, assembles, stores the artifact at
    exports/{user_id}/{job_id}.tar.gz, sets status complete with
    artifact_key + manifest, and audits tenant.export_completed."""
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user = await _create_committed_user(session_factory, nonce=nonce)
    store = LocalFileStore(str(tmp_path / "worker-files"))
    try:
        job = await _create_committed_job(session_factory, user_id=user.id)
        worker = TenantExportWorker(session_factory, file_store=store)

        result = await worker.run_once()
        assert result == job.id

        async with session_factory() as db:
            refreshed = await db.get(TenantExportJob, job.id)
            assert refreshed is not None
            assert refreshed.status == "complete"
            expected_key = f"exports/{user.id}/{job.id}.tar.gz"
            assert refreshed.artifact_key == expected_key
            assert refreshed.manifest is not None
            assert refreshed.manifest["user_id"] == str(user.id)
            assert refreshed.completed_at is not None
            assert refreshed.claim_id is None
            assert refreshed.error is None
            assert await store.exists(expected_key)

            audit = (
                await db.execute(
                    select(ControlPlaneAuditEvent).where(
                        ControlPlaneAuditEvent.action == "tenant.export_completed",
                        ControlPlaneAuditEvent.resource_id == str(job.id),
                    )
                )
            ).scalar_one()
            details_text = str(audit.details)
            assert expected_key in details_text
            assert "http" not in details_text  # never a presigned URL
            await db.execute(
                delete(ControlPlaneAuditEvent).where(ControlPlaneAuditEvent.id == audit.id)
            )
            await db.commit()
    finally:
        await _cleanup_worker_rows(session_factory, user_id=user.id)


@pytest.mark.committed_db
async def test_worker_run_once_without_queued_job_returns_none(engine, tmp_path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    worker = TenantExportWorker(
        session_factory, file_store=LocalFileStore(str(tmp_path / "idle-files"))
    )
    assert await worker.run_once() is None


@pytest.mark.committed_db
async def test_worker_failure_records_backoff_and_later_retry_succeeds(engine, tmp_path) -> None:
    """Assembly failure records status failed with error + backoff
    (next_attempt_at in the future, attempt_count incremented); a later
    run_once retries and succeeds — an interrupted job is durable retry
    work, never lost (ACCESS-04)."""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user = await _create_committed_user(session_factory, nonce=nonce)
    try:
        job = await _create_committed_job(session_factory, user_id=user.id)

        failing_worker = TenantExportWorker(
            session_factory, file_store=_FailingFileStore(str(tmp_path / "broken"))
        )
        assert await failing_worker.run_once() == job.id

        async with session_factory() as db:
            failed = await db.get(TenantExportJob, job.id)
            assert failed is not None
            assert failed.status == "failed"
            assert failed.error is not None
            assert failed.attempt_count == 1
            assert failed.next_attempt_at is not None
            assert failed.next_attempt_at > datetime.now(UTC)
            assert failed.claim_id is None

        # Make the retry due now, then run a healthy worker.
        async with session_factory() as db:
            await db.execute(
                update(TenantExportJob)
                .where(TenantExportJob.id == job.id)
                .values(next_attempt_at=datetime.now(UTC))
            )
            await db.commit()

        healthy_worker = TenantExportWorker(
            session_factory, file_store=LocalFileStore(str(tmp_path / "healthy"))
        )
        assert await healthy_worker.run_once() == job.id

        async with session_factory() as db:
            done = await db.get(TenantExportJob, job.id)
            assert done is not None
            assert done.status == "complete"
            assert done.attempt_count == 2
            assert done.artifact_key is not None
    finally:
        await _cleanup_worker_rows(session_factory, user_id=user.id)


@pytest.mark.committed_db
async def test_stale_claim_id_fencing_exactly_one_executes(engine) -> None:
    """expected_claim_id fencing: completing or failure-recording with a
    stale claim id is rejected, so exactly one racing worker executes."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user = await _create_committed_user(session_factory, nonce=nonce)
    try:
        job = await _create_committed_job(session_factory, user_id=user.id)

        async with session_factory() as db:
            claim = await claim_tenant_export(db)
            assert claim is not None
            assert claim.job_id == job.id
            await db.commit()

        stale_claim_id = uuid.uuid4()
        assert stale_claim_id != claim.claim_id

        async with session_factory() as db:
            with pytest.raises(TenantExportClaimLostError):
                await complete_tenant_export(
                    db,
                    job_id=job.id,
                    expected_claim_id=stale_claim_id,
                    artifact_key="exports/nope.tar.gz",
                    manifest={"export_version": 1},
                )
            await db.rollback()

        async with session_factory() as db:
            recorded = await record_tenant_export_failure(
                db,
                job_id=job.id,
                expected_claim_id=stale_claim_id,
                error="stale worker",
            )
            assert recorded is False
            await db.rollback()

        async with session_factory() as db:
            await complete_tenant_export(
                db,
                job_id=job.id,
                expected_claim_id=claim.claim_id,
                artifact_key=f"exports/{user.id}/{job.id}.tar.gz",
                manifest={"export_version": 1},
            )
            await db.commit()

        async with session_factory() as db:
            done = await db.get(TenantExportJob, job.id)
            assert done is not None
            assert done.status == "complete"
            assert done.claim_id is None
    finally:
        await _cleanup_worker_rows(session_factory, user_id=user.id)


async def test_tenant_export_worker_registered_in_channel_worker_fleet() -> None:
    """workers/channels.py registers TenantExportWorker in the same fleet
    list as PrincipalLifecycleCleanupWorker."""
    import inspect

    from app.workers import channels as channel_workers

    source = inspect.getsource(channel_workers.build_channel_workers)
    assert "TenantExportWorker" in source
    assert "PrincipalLifecycleCleanupWorker" in source


# --------------------------------------------------------------------------
# Task 3: Admin trigger + status endpoints with presigned delivery (D-13)
# --------------------------------------------------------------------------

_ADMIN_KEY = "test-admin-secret-do-not-use-in-prod"
_AUTH = {"X-Admin-Key": _ADMIN_KEY}


class _RecordingPresignStore(LocalFileStore):
    """Records presign calls so tests can pin key + TTL forwarding."""

    def __init__(self, base_path: str) -> None:
        super().__init__(base_path)
        self.presign_calls: list[tuple[str, int]] = []

    async def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        self.presign_calls.append((key, expires_in_seconds))
        return f"https://presigned.example/{key}?ttl={expires_in_seconds}"


@pytest.fixture
def presign_store(tmp_path, monkeypatch: pytest.MonkeyPatch) -> _RecordingPresignStore:
    from app.routes import admin as admin_routes

    store = _RecordingPresignStore(str(tmp_path / "admin-files"))
    monkeypatch.setattr(admin_routes, "get_file_store", lambda: store)
    return store


@pytest.fixture
async def export_admin_client(db_session):
    """Admin client that exercises the X-Admin-Key gate directly (same
    pattern as test_admin_tenant_console.py)."""
    import httpx
    from httpx import ASGITransport

    from app.core.config import settings
    from app.core.database import get_session
    from app.main import app

    async def _override_get_session():
        yield db_session

    original_admin_key = settings.admin_api_key
    settings.admin_api_key = _ADMIN_KEY
    app.dependency_overrides[get_session] = _override_get_session
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        settings.admin_api_key = original_admin_key


async def test_admin_export_trigger_creates_queued_job_and_audits(
    export_admin_client, db_session: AsyncSession, seed_user: User
) -> None:
    """POST /v1/admin/tenants/{user_id}/exports → 201 queued job row;
    audit tenant.export_requested carries include_vault_values."""
    resp = await export_admin_client.post(
        f"/v1/admin/tenants/{seed_user.id}/exports", headers=_AUTH, json={}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"

    job = await db_session.get(TenantExportJob, uuid.UUID(body["job_id"]))
    assert job is not None
    assert job.user_id == seed_user.id
    assert job.include_vault_values is False
    assert job.status == "queued"

    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.export_requested",
                ControlPlaneAuditEvent.resource_id == body["job_id"],
            )
        )
    ).scalar_one()
    assert audit.target_user_id == seed_user.id
    assert audit.details["include_vault_values"] is False


async def test_admin_export_trigger_unknown_user_404(export_admin_client) -> None:
    resp = await export_admin_client.post(
        f"/v1/admin/tenants/{uuid.uuid4()}/exports", headers=_AUTH, json={}
    )
    assert resp.status_code == 404, resp.text


async def test_admin_export_endpoints_require_admin_key(
    export_admin_client, seed_user: User
) -> None:
    resp = await export_admin_client.post(f"/v1/admin/tenants/{seed_user.id}/exports", json={})
    assert resp.status_code == 401, resp.text

    resp = await export_admin_client.get(f"/v1/admin/tenants/{seed_user.id}/exports")
    assert resp.status_code == 401, resp.text

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{seed_user.id}/exports/{uuid.uuid4()}/download"
    )
    assert resp.status_code == 401, resp.text

    resp = await export_admin_client.get("/v1/admin/users", params={"email": "x"})
    assert resp.status_code == 401, resp.text


async def test_admin_export_vault_opt_in_round_trips(
    export_admin_client, db_session: AsyncSession, seed_user: User
) -> None:
    """include_vault_values=true round-trips into the job row, the audit
    details, and the status listing."""
    resp = await export_admin_client.post(
        f"/v1/admin/tenants/{seed_user.id}/exports",
        headers=_AUTH,
        json={"include_vault_values": True},
    )
    assert resp.status_code == 201, resp.text
    job_id = resp.json()["job_id"]

    job = await db_session.get(TenantExportJob, uuid.UUID(job_id))
    assert job is not None
    assert job.include_vault_values is True

    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.export_requested",
                ControlPlaneAuditEvent.resource_id == job_id,
            )
        )
    ).scalar_one()
    assert audit.details["include_vault_values"] is True

    resp = await export_admin_client.get(f"/v1/admin/tenants/{seed_user.id}/exports", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["job_id"] == job_id
    assert item["include_vault_values"] is True


async def _seed_export_jobs(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[TenantExportJob, TenantExportJob, TenantExportJob, str]:
    """One complete (with artifact), one failed and one queued job."""
    from datetime import timedelta

    now = datetime.now(UTC)
    artifact_key = f"exports/{user_id}/complete-job.tar.gz"
    complete_job = TenantExportJob(
        user_id=user_id,
        status="complete",
        artifact_key=artifact_key,
        manifest={"export_version": 1},
        completed_at=now,
        created_at=now - timedelta(minutes=3),
    )
    failed_job = TenantExportJob(
        user_id=user_id,
        status="failed",
        error="synthetic failure",
        created_at=now - timedelta(minutes=2),
    )
    queued_job = TenantExportJob(
        user_id=user_id,
        status="queued",
        created_at=now - timedelta(minutes=1),
    )
    db.add_all([complete_job, failed_job, queued_job])
    await db.commit()
    return complete_job, failed_job, queued_job, artifact_key


async def _link_issued_count(db: AsyncSession, user_id: uuid.UUID) -> int:
    """`tenant.export_link_issued` rows for ONE tenant."""
    return (
        await db.execute(
            select(func.count())
            .select_from(ControlPlaneAuditEvent)
            .where(
                ControlPlaneAuditEvent.action == "tenant.export_link_issued",
                ControlPlaneAuditEvent.target_user_id == user_id,
            )
        )
    ).scalar_one()


async def test_admin_export_list_is_a_pure_read_that_mints_nothing(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
) -> None:
    """W-03 / WR-02: the list endpoint is a READ.

    Ten consecutive calls — the console polls this every 4 seconds —
    presign nothing, hand out no `download_url`, and write ZERO
    `tenant.export_link_issued` audit rows. Jobs still come back
    newest-first, and `downloadable` reports which ones the operator can
    ask a link for.
    """
    complete_job, failed_job, queued_job, _ = await _seed_export_jobs(db_session, seed_user.id)
    audits_before = await _link_issued_count(db_session, seed_user.id)

    for _poll in range(10):
        resp = await export_admin_client.get(
            f"/v1/admin/tenants/{seed_user.id}/exports", headers=_AUTH
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert [item["job_id"] for item in items] == [
            str(queued_job.id),
            str(failed_job.id),
            str(complete_job.id),
        ]
        by_id = {item["job_id"]: item for item in items}
        # No item — complete included — carries a live credential.
        assert [item["download_url"] for item in items] == [None, None, None]
        assert by_id[str(queued_job.id)]["downloadable"] is False
        assert by_id[str(failed_job.id)]["downloadable"] is False
        assert by_id[str(failed_job.id)]["error"] == "synthetic failure"
        assert by_id[str(complete_job.id)]["downloadable"] is True

    # The whole finding, in two assertions.
    assert presign_store.presign_calls == []
    assert await _link_issued_count(db_session, seed_user.id) == audits_before


async def test_admin_export_list_unknown_user_404(export_admin_client) -> None:
    resp = await export_admin_client.get(f"/v1/admin/tenants/{uuid.uuid4()}/exports", headers=_AUTH)
    assert resp.status_code == 404, resp.text


async def test_admin_export_download_presigns_once_and_audits_the_issuance(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
) -> None:
    """The assertions that used to live on the list endpoint: presigning
    with `EXPORT_PRESIGNED_TTL_SECONDS`, and exactly one
    `tenant.export_link_issued` row whose details never carry the URL
    (T-03-18). They now hang off one deliberate operator act."""
    complete_job, _failed, _queued, artifact_key = await _seed_export_jobs(db_session, seed_user.id)

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{seed_user.id}/exports/{complete_job.id}/download", headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["download_url"] is not None
    assert artifact_key in body["download_url"]
    assert body["expires_in_seconds"] == EXPORT_PRESIGNED_TTL_SECONDS == 3600
    assert presign_store.presign_calls == [(artifact_key, 3600)]

    audits = (
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.action == "tenant.export_link_issued",
                    ControlPlaneAuditEvent.resource_id == str(complete_job.id),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert audits[0].target_user_id == seed_user.id
    assert audits[0].details["job_id"] == str(complete_job.id)
    assert audits[0].details["artifact_key"] == artifact_key
    assert audits[0].details["ttl_seconds"] == 3600
    # The presigned URL never lands in audit details (T-03-18).
    assert "http" not in str(audits[0].details)


async def test_admin_export_download_twice_records_two_issuances(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
) -> None:
    """Issuance is per explicit request — the audit trail records operator
    intent, so two asks are two rows, not a deduped one."""
    complete_job, _failed, _queued, artifact_key = await _seed_export_jobs(db_session, seed_user.id)
    before = await _link_issued_count(db_session, seed_user.id)

    for _ in range(2):
        resp = await export_admin_client.get(
            f"/v1/admin/tenants/{seed_user.id}/exports/{complete_job.id}/download", headers=_AUTH
        )
        assert resp.status_code == 200, resp.text

    assert await _link_issued_count(db_session, seed_user.id) == before + 2
    assert presign_store.presign_calls == [(artifact_key, 3600), (artifact_key, 3600)]


async def test_admin_export_download_refuses_jobs_with_no_artifact(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
) -> None:
    """A queued, running or failed job is REFUSED rather than answered with
    a null URL, so the console cannot render a dead control."""
    from datetime import timedelta

    _complete, failed_job, queued_job, _key = await _seed_export_jobs(db_session, seed_user.id)
    running_job = TenantExportJob(
        user_id=seed_user.id,
        status="running",
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    # A complete job whose artifact never landed is refused on the same code.
    artifactless_job = TenantExportJob(
        user_id=seed_user.id,
        status="complete",
        completed_at=datetime.now(UTC),
    )
    db_session.add_all([running_job, artifactless_job])
    await db_session.commit()
    before = await _link_issued_count(db_session, seed_user.id)

    for job in (queued_job, running_job, failed_job, artifactless_job):
        resp = await export_admin_client.get(
            f"/v1/admin/tenants/{seed_user.id}/exports/{job.id}/download", headers=_AUTH
        )
        assert resp.status_code == 409, (job.status, resp.text)
        assert resp.json()["detail"]["code"] == "export_not_downloadable"

    assert presign_store.presign_calls == []
    assert await _link_issued_count(db_session, seed_user.id) == before


async def test_admin_export_download_cross_tenant_job_is_404(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
    test_identity: str,
) -> None:
    """T-03-66: the route carries two independently-supplied identifiers.
    Another tenant's complete job id, presented under this tenant's path,
    is a 404 — never a minted link for someone else's export."""
    other_user = User(
        clerk_id=f"otherexp_{test_identity}",
        email=f"otherexp-{test_identity}@clawdi.local",
        name="Other Export Tenant",
    )
    db_session.add(other_user)
    await db_session.flush()
    foreign_job, _f, _q, _key = await _seed_export_jobs(db_session, other_user.id)
    before = await _link_issued_count(db_session, other_user.id)

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{seed_user.id}/exports/{foreign_job.id}/download", headers=_AUTH
    )
    assert resp.status_code == 404, resp.text
    assert presign_store.presign_calls == []
    assert await _link_issued_count(db_session, other_user.id) == before


async def test_admin_export_download_unknown_user_or_job_is_404(
    export_admin_client, db_session: AsyncSession, seed_user: User
) -> None:
    complete_job, _f, _q, _key = await _seed_export_jobs(db_session, seed_user.id)

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{uuid.uuid4()}/exports/{complete_job.id}/download", headers=_AUTH
    )
    assert resp.status_code == 404, resp.text

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{seed_user.id}/exports/{uuid.uuid4()}/download", headers=_AUTH
    )
    assert resp.status_code == 404, resp.text


async def test_admin_users_lookup_by_email(
    export_admin_client, db_session: AsyncSession, seed_user: User, test_identity: str
) -> None:
    """GET /v1/admin/users?email= ILIKE-matches for the 03-10 tenant
    picker, capped at 20 items."""
    resp = await export_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"email": f"test-{test_identity}"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(item["id"] == str(seed_user.id) for item in items)
    match = next(item for item in items if item["id"] == str(seed_user.id))
    assert match["email"] == seed_user.email
    assert match["name"] == seed_user.name
    assert len(items) <= 20

    resp = await export_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"email": "no-such-user-anywhere-zz"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_admin_export_download_forbids_caching_the_presigned_url(
    export_admin_client,
    db_session: AsyncSession,
    seed_user: User,
    presign_store: _RecordingPresignStore,
) -> None:
    """CR-01: the minted URL is bearer-equivalent — anyone holding it can
    fetch the tenant's whole archive for an hour, including the decrypted
    credentials bundle when the job carried `include_vault_values`.

    Nothing else marks this response uncacheable: `SecurityHeadersMiddleware`
    sets nosniff/frame/referrer only, `main.py`'s `no-store` is scoped to
    other routers, and because this route authenticates with a custom
    `X-Admin-Key` rather than `Authorization`, RFC 9111 section 3.5's
    shared-cache protection does not apply either. The module already has
    `_no_store` and already applies it to the pairing responses.
    """
    complete_job, _failed, _queued, _artifact_key = await _seed_export_jobs(
        db_session, seed_user.id
    )

    resp = await export_admin_client.get(
        f"/v1/admin/tenants/{seed_user.id}/exports/{complete_job.id}/download", headers=_AUTH
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["download_url"] is not None
    assert resp.headers["cache-control"] == "no-store, private"
    assert resp.headers["pragma"] == "no-cache"


# --------------------------------------------------------------------------
# Plan 06-19: a tenant with no companion gets exactly the Phase 3 archive
# --------------------------------------------------------------------------

# The archive's top-level manifest keys before companions existed.
PHASE_3_MANIFEST_KEYS = {
    "export_version",
    "user_id",
    "created_at",
    "vault_values_included",
    "notes",
    "domains",
}


async def test_tenant_without_companions_gets_the_phase_3_export_unchanged(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """A job created the plan-06-19 way, for a tenant with no companion, builds the same members,
    the same manifest keys and the same domain list the Phase 3 export always did."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.tenant_export import EXPORT_DOMAINS, create_tenant_export_job

    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="phase-3-guard")
    job = await create_tenant_export_job(db_session, user_id=seed_user.id)
    await db_session.commit()
    assert job.manifest is None  # no companion component was recorded

    worker = TenantExportWorker(
        async_sessionmaker(
            bind=db_session.bind,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        ),
        file_store=file_store,
    )
    assert await worker.run_once() == job.id
    refreshed = (
        await db_session.execute(
            select(TenantExportJob)
            .where(TenantExportJob.id == job.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert refreshed.status == "complete", refreshed.error
    assert refreshed.artifact_key is not None
    members = _tar_members(await file_store.get(refreshed.artifact_key))

    skill = seeded["skill"]
    expected = {
        "manifest.json",
        *(f"{name}.jsonl" for name in EXPORT_DOMAINS),
        f"sessions/{seeded['session'].id}.json",
        f"skills/{skill.project_id}/{skill.skill_key}/{skill.content_hash}.tar.gz",
    }
    assert set(members) == expected
    manifest = _manifest(members)
    assert set(manifest) == PHASE_3_MANIFEST_KEYS
    assert [entry["name"] for entry in manifest["domains"]] == list(EXPORT_DOMAINS)
    assert not any(name.startswith("companions/") for name in members)
    assert "companion_components" not in (refreshed.manifest or {})


# --------------------------------------------------------------------------
# Plan 06.1-19: every session body is exported, recorded as omitted, or fails the job
# --------------------------------------------------------------------------


async def _seed_events_v1_rows(
    db_session: AsyncSession,
    file_store: LocalFileStore,
    user: User,
    env: object,
    *,
    marker: str,
    stale_snapshot: bool = False,
) -> dict[str, object]:
    """One committed events-v1 session built through the ORM: a generation of 3 events in two
    chunks (seq 0-1, then seq 2 on base head h2), each chunk stored where the real writer puts
    it. With ``stale_snapshot`` the session also keeps a ``file_key`` whose snapshot body is not
    the events content -- the leftover of a session that moved from snapshot-v1 to events-v1."""
    environment_id = getattr(env, "id")
    session_id = uuid.uuid4()
    generation_id = uuid.uuid4()
    local_session_id = f"events-{uuid.uuid4().hex[:12]}"
    first = [
        _event(0, f"{marker}-0", "user", f"events question {marker}"),
        _event(1, f"{marker}-1", "assistant", f"events answer {marker}"),
    ]
    second = [_event(2, f"{marker}-2", "assistant", f"events later {marker}")]
    first_bytes, first_hash = _event_chunk(first)
    second_bytes, second_hash = _event_chunk(second)
    head_2 = advance_event_head(EMPTY_EVENT_HEAD, first)
    head_3 = advance_event_head(head_2, second)

    snapshot_key: str | None = None
    if stale_snapshot:
        snapshot_key = f"sessions/{user.id}/{local_session_id}.json"
        await file_store.put(
            snapshot_key, json.dumps([{"role": "user", "content": f"stale {marker}"}]).encode()
        )
    session = Session(
        id=session_id,
        user_id=user.id,
        environment_id=environment_id,
        local_session_id=local_session_id,
        started_at=datetime.now(UTC),
        file_key=snapshot_key,
        content_hash=head_3,
        content_protocol="events-v1",
        event_generation_id=generation_id,
        event_revision=1,
        event_count=3,
        event_head_hash=head_3,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        SessionEventGeneration(
            id=generation_id,
            session_id=session_id,
            append_id=uuid.uuid4(),
            status="committed",
            base_revision=0,
            base_count=0,
            base_head_hash=EMPTY_EVENT_HEAD,
            final_count=3,
            final_head_hash=head_3,
        )
    )
    await db_session.flush()
    chunk_keys: list[str] = []
    for start, end, data, content_hash, base_head, result_head in (
        (0, 1, first_bytes, first_hash, EMPTY_EVENT_HEAD, head_2),
        (2, 2, second_bytes, second_hash, head_2, head_3),
    ):
        key = (
            f"session-events/v1/{user.id}/{session_id}/{generation_id}/chunks/"
            f"{start}-{end}-{content_hash}.ndjson"
        )
        await file_store.put(key, data)
        chunk_keys.append(key)
        db_session.add(
            SessionEventChunk(
                session_id=session_id,
                generation_id=generation_id,
                start_seq=start,
                end_seq=end,
                event_count=end - start + 1,
                base_head_hash=base_head,
                result_head_hash=result_head,
                content_hash=content_hash,
                file_key=key,
            )
        )
    await db_session.commit()
    return {
        "session": session,
        "local_session_id": local_session_id,
        "body": first_bytes + second_bytes,
        "chunk_keys": chunk_keys,
        "snapshot_key": snapshot_key,
    }


def _session_records(members: dict[str, bytes]) -> dict[str, dict]:
    return {
        record["local_session_id"]: record for record in _jsonl_records(members["sessions.jsonl"])
    }


async def test_events_v1_session_exports_verified_ndjson_and_never_the_stale_snapshot(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """An events-v1 session exports its verified chunk bytes; its leftover snapshot is never
    read, so the archive carries no body its `content_hash` does not describe (06.1-18)."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="stale")
    events = await _seed_events_v1_rows(
        db_session, file_store, seed_user, seeded["env"], marker="stale", stale_snapshot=True
    )
    archive, _ = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)

    session = events["session"]
    assert isinstance(session, Session)
    assert members[f"sessions/{session.id}.events.ndjson"] == events["body"]
    assert f"sessions/{session.id}.json" not in members
    local = str(events["local_session_id"])
    assert not any(local in name for name in members if name.startswith("sessions/"))


async def test_session_record_names_its_protocol_and_archive_member(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """Each sessions.jsonl record says which content protocol it uses and which archive member
    carries its body, or null when the archive has none."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="record")
    events = await _seed_events_v1_rows(
        db_session, file_store, seed_user, seeded["env"], marker="record"
    )
    bare_local = f"bare-{uuid.uuid4().hex[:12]}"
    db_session.add(
        Session(
            user_id=seed_user.id,
            environment_id=getattr(seeded["env"], "id"),
            local_session_id=bare_local,
            started_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)
    records = _session_records(members)
    for local, record in records.items():
        assert {"content_protocol", "body_member"} <= set(record), (local, sorted(record))

    events_local = events["local_session_id"]
    events_session = events["session"]
    assert isinstance(events_session, Session)
    assert records[events_local]["content_protocol"] == "events-v1"
    assert records[events_local]["body_member"] == f"sessions/{events_session.id}.events.ndjson"
    snapshot_local = seeded["local_session_id"]
    assert records[snapshot_local]["content_protocol"] == "snapshot-v1"
    assert records[snapshot_local]["body_member"] == f"sessions/{seeded['session'].id}.json"
    assert records[bare_local]["body_member"] is None
    for record in records.values():
        if record["body_member"] is not None:
            assert record["body_member"] in members
    # A body that is absent for no fault of the store is not an omission.
    assert "session_bodies_omitted" not in manifest
    # The new keys stay outside the D-10 secretish predicate.
    for key in (
        "content_protocol",
        "body_member",
        "session_bodies_omitted",
        "session_id",
        "local_session_id",
        "reason",
    ):
        assert not _is_secretish_key(key), key


async def test_damaged_events_v1_content_is_omitted_and_recorded(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """Content that is gone or damaged for good is listed in the manifest with a reason, and the
    rest of the export completes: retrying would never succeed, so one corrupt session must not
    block the tenant's export forever (Phase 3 D-10)."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="damaged")
    events = await _seed_events_v1_rows(
        db_session, file_store, seed_user, seeded["env"], marker="damaged"
    )
    session = events["session"]
    assert isinstance(session, Session)
    local = events["local_session_id"]
    second_key = events["chunk_keys"][1]
    skill = seeded["skill"]
    skill_member = f"skills/{skill.project_id}/{seeded['skill_key']}/{skill.content_hash}.tar.gz"

    for damage, reason in (("delete", "missing"), ("overwrite", "invalid")):
        if damage == "delete":
            await file_store.delete(second_key)
        else:
            other, _ = _event_chunk([_event(2, "damaged-other", "assistant", "not the original")])
            await file_store.put(second_key, other)

        archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
        members = _tar_members(archive)

        assert not any(name.startswith(f"sessions/{session.id}.") for name in members), damage
        assert _session_records(members)[local].get("body_member", "absent") is None, damage
        assert manifest.get("session_bodies_omitted") == [
            {
                "session_id": str(session.id),
                "local_session_id": local,
                "content_protocol": "events-v1",
                "reason": reason,
            }
        ], damage
        assert _manifest(members) == manifest
        # Everything else is unaffected.
        assert members[f"sessions/{seeded['session'].id}.json"] == seeded["session_body"]
        assert members[skill_member] == seeded["skill_archive"]
        rows = _domain_rows(manifest)
        assert rows["sessions"] == 2
        assert rows["memories"] == 1
        assert rows["usage_events"] == 1


async def test_missing_snapshot_body_is_recorded_not_silently_dropped(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """A snapshot-v1 session whose stored object does not exist is recorded as missing instead
    of vanishing from the archive without a word."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="nosnap")
    local = f"nosnap-{uuid.uuid4().hex[:12]}"
    session = Session(
        user_id=seed_user.id,
        environment_id=getattr(seeded["env"], "id"),
        local_session_id=local,
        started_at=datetime.now(UTC),
        file_key=f"sessions/{seed_user.id}/{local}.json",
        content_hash="c" * 64,
    )
    db_session.add(session)
    await db_session.commit()

    archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)

    assert f"sessions/{session.id}.json" not in members
    assert manifest.get("session_bodies_omitted") == [
        {
            "session_id": str(session.id),
            "local_session_id": local,
            "content_protocol": "snapshot-v1",
            "reason": "missing",
        }
    ]
    assert _session_records(members)[local].get("body_member", "absent") is None
    assert members[f"sessions/{seeded['session'].id}.json"] == seeded["session_body"]


class _SessionEventsOutageStore:
    """Delegates to a real store, except that reading any events-v1 chunk is a provider outage."""

    def __init__(self, inner: FileStore) -> None:
        self._inner = inner

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await self._inner.put(key, data, content_type)

    async def get(self, key: str) -> bytes:
        if key.startswith("session-events/"):
            raise RuntimeError("synthetic object-store outage")
        return await self._inner.get(key)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        return await self._inner.presigned_get_url(key, expires_in_seconds)

    async def list_keys(self, prefix: str) -> list[str]:
        return await self._inner.list_keys(prefix)

    async def delete_prefix(self, prefix: str) -> int:
        return await self._inner.delete_prefix(prefix)


async def test_storage_outage_fails_the_export_for_retry(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """A storage outage is not an omission: the export raises, so the worker records the failure
    and retries it once the store is back."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="outage")
    await _seed_events_v1_rows(db_session, file_store, seed_user, seeded["env"], marker="outage")

    with pytest.raises(SessionContentUnavailable):
        await assemble_tenant_export(
            db_session, _SessionEventsOutageStore(file_store), seed_user.id
        )


# --------------------------------------------------------------------------
# Plan 06.1-20: one archive member per session body, named by the immutable session id
# --------------------------------------------------------------------------


async def _register_agent(
    client: httpx.AsyncClient, machine_id: str, agent_type: str = "claude-code"
) -> str:
    """Register one agent through the real route and return its environment id."""
    response = await client.post(
        "/v1/environments",
        json={
            "machine_id": machine_id,
            "machine_name": f"mac-{machine_id}",
            "agent_type": agent_type,
            "agent_version": "0.1.0",
            "os": "darwin",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _tar_names(archive: bytes) -> list[str]:
    """Every member name in write order, repeats included (``_tar_members`` collapses them)."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return tar.getnames()


@pytest.mark.parametrize("protocol", ["snapshot-v1", "events-v1"], ids=["snapshot-v1", "events-v1"])
async def test_equal_local_ids_from_two_agents_export_both_bodies(
    protocol: str,
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upstream #1229 made `local_session_id` unique per origin, not per tenant. Two agents of
    one user can each store a session under the same local id with different bodies, so the
    archive must carry both bodies, each in its own member tied to its own record (Phase 3
    ACCESS-04 D-11). A shared member name would let the second body shadow the first, and the
    purge that follows the export would delete the only copy of the first conversation."""
    monkeypatch.setattr(sessions_routes, "file_store", file_store)
    monkeypatch.setattr(session_events_routes, "file_store", file_store)

    expected_by_env: dict[str, bytes] = {}
    if protocol == "snapshot-v1":
        shared_local = f"shared-{uuid.uuid4().hex[:12]}"
        started = datetime.now(UTC).isoformat()
        for agent, agent_type in (("one", "claude-code"), ("two", "codex")):
            env_id = await _register_agent(
                client, f"export-{agent}-{uuid.uuid4().hex[:8]}", agent_type
            )
            body = json.dumps(
                [{"role": "user", "content": f"agent {agent} conversation {uuid.uuid4().hex}"}]
            ).encode()
            content_hash = hashlib.sha256(body).hexdigest()
            batch = await client.post(
                "/v1/sessions/batch",
                json={
                    "sessions": [
                        {
                            "environment_id": env_id,
                            "local_session_id": shared_local,
                            "started_at": started,
                            "message_count": 1,
                            "model": "test-model",
                            "content_hash": content_hash,
                        }
                    ]
                },
            )
            assert batch.status_code == 200, batch.text
            upload = await client.post(
                f"/v1/sessions/{shared_local}/upload",
                data={"environment_id": env_id, "expected_content_hash": content_hash},
                files={"file": (f"{shared_local}.json", body, "application/json")},
            )
            assert upload.status_code == 200, upload.text
            expected_by_env[env_id] = body
    else:
        local_ids: set[str] = set()
        for agent in ("one", "two"):
            events = [
                _event(0, f"{agent}-user", "user", f"agent {agent} question"),
                _event(1, f"{agent}-assistant", "assistant", f"agent {agent} answer"),
            ]
            # The returned session id is looked up by local id, so on the second call it may
            # name the other agent's session; the returned environment id is exact.
            _, local_id, env_id, _ = await _seed_event_session(client, events=events)
            local_ids.add(local_id)
            expected_by_env[env_id] = _event_chunk(events)[0]
        assert len(local_ids) == 1, local_ids
        (shared_local,) = local_ids

    archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)

    # Precondition: the two sessions really share one local id under two agents.
    records = [
        record
        for record in _jsonl_records(members["sessions.jsonl"])
        if record["local_session_id"] == shared_local
    ]
    assert len(records) == 2, records
    assert {record["environment_id"] for record in records} == set(expected_by_env)

    suffix = ".json" if protocol == "snapshot-v1" else ".events.ndjson"
    body_members = [record["body_member"] for record in records]
    assert all(member is not None for member in body_members), body_members
    assert len(set(body_members)) == 2, body_members
    for record in records:
        assert record["body_member"] == f"sessions/{record['id']}{suffix}", record
        assert members[record["body_member"]] == expected_by_env[record["environment_id"]], record

    names = _tar_names(archive)
    assert len(names) == len(set(names)), sorted({name for name in names if names.count(name) > 1})
    assert "session_bodies_omitted" not in manifest


class _SessionReadRecordingStore(LocalFileStore):
    """A local store that records every `sessions/` key the export checks or reads."""

    def __init__(self, base_path: str) -> None:
        super().__init__(base_path)
        self.session_reads: list[str] = []

    async def get(self, key: str) -> bytes:
        if key.startswith("sessions/"):
            self.session_reads.append(key)
        return await super().get(key)

    async def exists(self, key: str) -> bool:
        if key.startswith("sessions/"):
            self.session_reads.append(key)
        return await super().exists(key)


async def test_a_repeated_session_body_member_fails_before_any_body_is_read(
    db_session: AsyncSession,
    seed_user: User,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the collection guard can catch this repeat. The second session's body is never
    stored, so without the guard it would be omitted and the tar writer would see the member
    once: the export would complete and silently pair two records with one body name."""
    file_store = _SessionReadRecordingStore(str(tmp_path / "files"))
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="repeat")
    local = f"repeat-{uuid.uuid4().hex[:12]}"
    unstored = Session(
        user_id=seed_user.id,
        environment_id=getattr(seeded["env"], "id"),
        local_session_id=local,
        started_at=datetime.now(UTC),
        # Never put: this body would be omitted as missing.
        file_key=f"sessions/{seed_user.id}/{local}.json",
        content_hash="d" * 64,
    )
    db_session.add(unstored)
    await db_session.commit()
    repeated = "sessions/one-name-for-every-body.json"

    def _one_member(session: Session) -> str | None:
        return repeated if session.file_key else None

    monkeypatch.setattr(tenant_export_service, "_session_body_member", _one_member)
    file_store.session_reads.clear()

    with pytest.raises(ExportMemberCollisionError) as excinfo:
        await assemble_tenant_export(db_session, file_store, seed_user.id)
    # The collection guard's own message names both sessions; the tar writer's does not.
    message = str(excinfo.value)
    assert repeated in message
    assert str(seeded["session"].id) in message
    assert str(unstored.id) in message
    assert file_store.session_reads == []


async def test_a_repeated_archive_member_fails_at_the_tar_writer(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A member name written twice makes extraction keep only the last entry, silently dropping
    the first. The export fails instead of shipping that archive."""
    await _seed_tenant(db_session, file_store, seed_user, marker="repeat")
    repeated = "users.jsonl"

    async def _shadowing_skill_archives(*_args: object) -> tenant_export_service._SkillArchives:
        return tenant_export_service._SkillArchives(
            members=[(repeated, b"not the users domain\n")], member_by_skill={}, omitted=[]
        )

    monkeypatch.setattr(tenant_export_service, "_collect_skill_archives", _shadowing_skill_archives)

    with pytest.raises(
        ExportMemberCollisionError, match=re.escape(f"archive member {repeated!r} would be written")
    ):
        await assemble_tenant_export(db_session, file_store, seed_user.id)


class _LateSessionStore:
    """Delegates to a real store. The first read of a `sessions/` key adds one more snapshot
    session to the tenant, as a sync landing while the export runs would."""

    def __init__(
        self,
        inner: FileStore,
        db: AsyncSession,
        *,
        user_id: uuid.UUID,
        environment_id: uuid.UUID,
        local_session_id: str,
        file_key: str,
        content_hash: str,
    ) -> None:
        self._inner = inner
        self._db = db
        self._late = {
            "user_id": user_id,
            "environment_id": environment_id,
            "local_session_id": local_session_id,
            "file_key": file_key,
            "content_hash": content_hash,
        }
        self.fired = False

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await self._inner.put(key, data, content_type)

    async def get(self, key: str) -> bytes:
        if not self.fired and key.startswith("sessions/"):
            self._db.add(Session(started_at=datetime.now(UTC), **self._late))
            await self._db.flush()
            self.fired = True
        return await self._inner.get(key)

    async def delete(self, key: str) -> None:
        await self._inner.delete(key)

    async def exists(self, key: str) -> bool:
        return await self._inner.exists(key)

    async def presigned_get_url(self, key: str, expires_in_seconds: int) -> str:
        return await self._inner.presigned_get_url(key, expires_in_seconds)

    async def list_keys(self, prefix: str) -> list[str]:
        return await self._inner.list_keys(prefix)

    async def delete_prefix(self, prefix: str) -> int:
        return await self._inner.delete_prefix(prefix)


async def test_a_session_created_mid_export_is_not_left_unaccounted(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """Records and bodies describe the same sessions. A session created while the export runs
    belongs to the next export: it must not get a record that claims content, names no member
    and is missing from `session_bodies_omitted` (06.1 review R2 WR-01)."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="late")
    before = set(
        await db_session.scalars(
            select(Session.local_session_id).where(Session.user_id == seed_user.id)
        )
    )
    late_local = f"late-{uuid.uuid4().hex[:12]}"
    late_body = json.dumps([{"role": "user", "content": f"late {late_local}"}]).encode()
    late_key = f"sessions/{seed_user.id}/{late_local}.json"
    await file_store.put(late_key, late_body)
    store = _LateSessionStore(
        file_store,
        db_session,
        user_id=seed_user.id,
        environment_id=getattr(seeded["env"], "id"),
        local_session_id=late_local,
        file_key=late_key,
        content_hash=hashlib.sha256(late_body).hexdigest(),
    )

    archive, manifest = await assemble_tenant_export(db_session, store, seed_user.id)
    members = _tar_members(archive)

    # Non-vacuity: the late session exists, so any later query would have seen it.
    assert store.fired
    late_count = await db_session.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.user_id == seed_user.id, Session.local_session_id == late_local)
    )
    assert late_count == 1

    records = _jsonl_records(members["sessions.jsonl"])
    omitted_ids = {entry["session_id"] for entry in manifest.get("session_bodies_omitted", [])}
    for record in records:
        if record["file_key"] is not None or record["content_protocol"] == "events-v1":
            assert record["body_member"] is not None or record["id"] in omitted_ids, record

    # The export is a snapshot of the sessions it loaded when it began.
    assert {record["local_session_id"] for record in records} == before
    assert late_body not in members.values()


async def test_skill_records_and_archives_are_one_snapshot(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skill records and skill archives come from one load of the skill rows. A skill created,
    or one re-uploaded, while the export runs must not leave an archive no record names, or a
    record whose hash names an archive that is not there (06.1 review WR-03)."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="late-skill")
    skill = seeded["skill"]
    assert isinstance(skill, Skill)
    project_id = skill.project_id
    skill_key = skill.skill_key
    old_hash = skill.content_hash
    skill_id = skill.id

    reuploaded_hash = "e" * 64
    reuploaded_bytes = b"re-uploaded skill bytes"
    reuploaded_key = f"skills/{seed_user.id}/{project_id}/{skill_key}/{reuploaded_hash}.tar.gz"
    await file_store.put(reuploaded_key, reuploaded_bytes)
    late_key_name = f"late-{uuid.uuid4().hex[:8]}"
    late_hash = "f" * 64
    late_bytes = b"late skill bytes"
    late_file_key = f"skills/{seed_user.id}/{project_id}/{late_key_name}/{late_hash}.tar.gz"
    await file_store.put(late_file_key, late_bytes)

    fired: list[bool] = []
    real_serialize_memories = tenant_export_service._serialize_memories

    async def _memories_then_skill_changes(db: AsyncSession, user_id: uuid.UUID):
        # Another transaction's work, landing while the export runs: a Core UPDATE does not
        # touch the export's loaded rows, as a concurrent re-upload would not.
        if not fired:
            await db.execute(
                update(Skill.__table__)
                .where(Skill.__table__.c.id == skill_id)
                .values(content_hash=reuploaded_hash, file_key=reuploaded_key)
            )
            db.add(
                Skill(
                    user_id=seed_user.id,
                    project_id=project_id,
                    skill_key=late_key_name,
                    name="Late Skill",
                    content_hash=late_hash,
                    file_key=late_file_key,
                )
            )
            await db.flush()
            fired.append(True)
        return await real_serialize_memories(db, user_id)

    monkeypatch.setattr(tenant_export_service, "_serialize_memories", _memories_then_skill_changes)

    archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)

    # Non-vacuity: both changes reached the database while the export ran.
    assert fired
    stored_hash = await db_session.scalar(
        select(Skill.__table__.c.content_hash).where(Skill.__table__.c.id == skill_id)
    )
    assert stored_hash == reuploaded_hash
    late_count = await db_session.scalar(
        select(func.count()).select_from(Skill).where(Skill.skill_key == late_key_name)
    )
    assert late_count == 1

    records = _jsonl_records(members["skills.jsonl"])
    assert [record["id"] for record in records] == [str(skill_id)]
    [record] = records
    expected_member = f"skills/{project_id}/{skill_key}/{old_hash}.tar.gz"
    assert record["content_hash"] == old_hash
    assert record["archive_member"] == expected_member
    assert members[expected_member] == seeded["skill_archive"]

    # Every skill member is named by exactly one record, and nothing else was carried.
    skill_members = {name for name in members if name.startswith("skills/")}
    assert skill_members == {expected_member}
    assert reuploaded_bytes not in members.values()
    assert late_bytes not in members.values()
    assert "skill_archives_omitted" not in manifest


async def test_a_skill_whose_archive_is_missing_is_recorded_not_dropped(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore
) -> None:
    """A skill whose stored object is gone is listed in `skill_archives_omitted`, and its
    record names no member, so the archive that precedes a purge says what it lacks."""
    seeded = await _seed_tenant(db_session, file_store, seed_user, marker="missing-skill")
    skill = seeded["skill"]
    assert isinstance(skill, Skill)
    assert skill.file_key is not None
    await file_store.delete(skill.file_key)

    archive, manifest = await assemble_tenant_export(db_session, file_store, seed_user.id)
    members = _tar_members(archive)

    [record] = _jsonl_records(members["skills.jsonl"])
    assert record["id"] == str(skill.id)
    assert record["archive_member"] is None
    assert not any(name.startswith("skills/") for name in members)
    assert manifest["skill_archives_omitted"] == [
        {
            "skill_id": str(skill.id),
            "project_id": str(skill.project_id),
            "skill_key": skill.skill_key,
            "content_hash": skill.content_hash,
            "reason": "missing",
        }
    ]
