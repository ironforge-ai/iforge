"""ACCESS-04 round-trip proof (D-17): export → verify → delete → purge → zero.

One synthetic tenant (A) with data in EVERY ``PURGE_COVERAGE`` "purge"
domain travels the full pipeline as one uninterrupted sequence: build,
operator export (03-07), operator delete + fence, due purge (03-08). A
parallel control tenant (B) proves scope — identical row counts and S3
keys before and after every stage.

The pieces are individually tested in ``test_tenant_export.py`` and
``test_tenant_delete_trigger.py``; this module pins the COMPOSITION the
requirement actually states, against the executable /v2 reference
(``scripts/mock_deploy_api.py`` driven in-process via ASGITransport) and
a recording fake identity provider (ADR-0005: Clawdi decides, the
provisioner executes).

The whole round-trip runs once in a module-scoped fixture on the
committed lane (the workers claim from independent sessions); the
stage-named tests assert the captured evidence so the verifier can map
each stage to the requirement's sentence.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.auth import AuthContext, get_auth, get_auth_short_session, optional_web_auth
from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.main import app
from app.models.ai_provider import (
    AiProvider,
    AiProviderAuthPayload,
    AiProviderOAuthAttempt,
    AiProviderOAuthRevokeTombstone,
)
from app.models.api_key import ApiKey
from app.models.audit import ControlPlaneAuditEvent
from app.models.base import Base
from app.models.billing_reconcile_finding import BillingReconcileFinding
from app.models.channel import ChannelAccount, ChannelSecret
from app.models.companion_approval import CompanionApproval
from app.models.companion_budget import CompanionRepeatBreaker, CompanionSpendCeiling
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_installation import (
    CompanionInstallation,
    CompanionInstallationSecret,
    CompanionInstallLogLine,
)
from app.models.companion_session_budget import CompanionCapPolicy, CompanionSessionBudget
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.models.device_authorization import DeviceAuthorization
from app.models.distributed_state import SyncSubscriptionLease
from app.models.hosted_runtime import HostedRuntimeConfigObservation, HostedRuntimeState
from app.models.memory import Memory
from app.models.platform_idempotency import PlatformMutationIdempotency
from app.models.principal_lifecycle import PrincipalLifecycle
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.project_invitation import ProjectInvitation
from app.models.project_membership import ProjectMembership
from app.models.project_share_link import ProjectShareLink
from app.models.session import Session, SessionMessageSearch, SessionSyncSuppression
from app.models.session_permission import PERMISSION_KIND_USER, SessionPermission
from app.models.skill import Skill
from app.models.tenant_export import TenantExportJob
from app.models.tenant_purge import TenantPurge
from app.models.usage_event import UsageEvent
from app.models.user import User, UserSetting
from app.models.vault import Vault, VaultCredentialProfile, VaultItem
from app.services.audit import _is_secretish_key
from app.services.file_store import LocalFileStore
from app.services.principal_lifecycle import (
    PrincipalTerminatedError,
    assert_clerk_principal_active,
)
from app.services.session_events import EMPTY_EVENT_HEAD, advance_event_head
from app.services.session_shares import create_session_share
from app.services.tenant_export_worker import TenantExportWorker

# The zero-rows assertion deliberately reuses the sweep's own scoping
# machinery (PURGE_COVERAGE + _tenant_scope_condition) so the check is
# mechanically tied to the coverage map rather than a hand-list that
# could drift (T-03-26).
from app.services.tenant_purge import (
    PURGE_COVERAGE,
    _delete_covered_rows,
    _tenant_scope_condition,
    tenant_object_prefixes,
)
from app.services.tenant_purge_worker import TenantPurgeWorker
from scripts import mock_deploy_api

# Shared archive-parsing helpers from the 03-07 export suite (reused,
# not duplicated, per the plan's read_first note).
from tests.conftest import _dependency_overrides, create_env_with_project
from tests.test_session_shares import _event, _event_chunk, _seed_event_session
from tests.test_tenant_export import _iter_keys, _jsonl_records, _tar_members

pytestmark = pytest.mark.asyncio

_ISSUER = "https://roundtrip.clerk.example.test"
_ADMIN_KEY = "roundtrip-admin-test-key"
_AUTH = {"X-Admin-Key": _ADMIN_KEY}

# Purge-disposition tables the seed builder intentionally leaves empty at
# stage 1, each with the reason (acceptance criterion: seeded tables must
# be a superset of the "purge" tables minus these explicit exceptions).
_SEED_EXCEPTIONS: dict[str, str] = {
    "tenant_export_jobs": (
        "created by stage 2's export trigger itself; asserted non-empty "
        "before the purge and zero after it"
    ),
}


# The rows `_seed_events_v1_session` writes through the real routes: the committed generation,
# its chunk index rows and the append's idempotency receipt. Named as seeded so stage 1 proves
# each holds tenant rows and stage 4 proves the purge empties it.
_EVENTS_V1_SEEDED_TABLES = frozenset(
    {
        "session_event_generations",
        "session_event_chunks",
        "session_event_append_receipts",
    }
)


class _RecordingProvider:
    """Fake UserProvisioningProvider recording delete_identity calls."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_identity(self, subject: str) -> None:
        self.deleted.append(subject)


def _purge_tables() -> set[str]:
    return {name for name, disposition in PURGE_COVERAGE.items() if disposition == "purge"}


async def _tenant_row_counts(db: AsyncSession, user_id: uuid.UUID) -> dict[str, int]:
    """Per-table row counts for one tenant, iterating PURGE_COVERAGE.

    Covers every table with a ``purge`` or ``via-parent:*`` disposition,
    scoped by the sweep's own ``_tenant_scope_condition`` — the exact set
    the purge is obligated to erase. Tombstone/anonymize/global tables are
    excluded by construction.
    """
    counts: dict[str, int] = {}
    for table_name in sorted(PURGE_COVERAGE):
        disposition = PURGE_COVERAGE[table_name]
        if not (disposition == "purge" or disposition.startswith("via-parent:")):
            continue
        table = Base.metadata.tables.get(table_name)
        if table is None:
            continue
        condition = _tenant_scope_condition(table, user_id)
        if condition is None:
            # Same skip the sweep itself applies (no scoping path); the
            # D-17 completeness guard owns flagging such tables.
            continue
        counts[table_name] = (
            await db.execute(select(func.count()).select_from(table).where(condition))
        ).scalar_one()
    return counts


async def _tenant_prefix_keys(store: LocalFileStore, user_id: uuid.UUID) -> dict[str, list[str]]:
    return {
        prefix: sorted(await store.list_keys(prefix)) for prefix in tenant_object_prefixes(user_id)
    }


async def _create_committed_user(db: AsyncSession, *, nonce: str, label: str) -> User:
    """Committed user with the Personal project every tenant carries."""
    user = User(
        clerk_id=f"roundtrip-{label}-{nonce}",
        clerk_issuer=_ISSUER,
        email=f"roundtrip-{label}-{nonce}@clawdi.local",
        name=f"Roundtrip {label.title()} User",
    )
    db.add(user)
    await db.flush()
    db.add(
        Project(
            user_id=user.id,
            name="Personal",
            slug="personal",
            kind=PROJECT_KIND_PERSONAL,
        )
    )
    await db.commit()
    await db.refresh(user)
    return user


async def _seed_hosted_runtime_state(db: AsyncSession, env: Any) -> HostedRuntimeState:
    """HostedRuntimeState + config observation for the export's
    hosted_runtimes domain and the purge's deployment enumeration.

    Deliberately does NOT create a V2RuntimeEnvironmentFence: fence rows
    are DB-trigger permanent (a6d2f4c8b1e7) and would accumulate forever
    in the shared phase database. The purge enumerates deployments from
    the states join whenever lifecycle cleanup has not run — which it
    never does inside this test (cleanup is explicitly deferred at stage
    3), so the states row is the authoritative source here.
    """
    now = datetime.now(UTC)
    deployment_id = f"dep-roundtrip-{uuid.uuid4().hex}"
    state = HostedRuntimeState(
        environment_id=env.id,
        deployment_id=deployment_id,
        instance_id=f"instance-{uuid.uuid4().hex}",
        generation=1,
        cli_package_spec="clawdi@1.2.3-test",
        locale={"language": "en", "timezone": "UTC"},
        system={},
        runtimes={
            "hermes": {
                "enabled": True,
                "providerMode": "unmanaged",
                "provider_ids": [],
                "install": {"source": "official"},
            }
        },
        live_sync={
            "enabled": True,
            "agents": [{"agentType": "hermes", "environmentId": str(env.id)}],
        },
        recovery={"cacheManifest": True, "allowOfflineBoot": True},
        tools={},
    )
    db.add(state)
    await db.flush()
    db.add(
        HostedRuntimeConfigObservation(
            environment_id=env.id,
            observed_at=now,
            observed_config_generation=state.generation,
            observed_manifest_etag='"roundtrip-ready"',
            observed_source_revision="a" * 64,
            diagnostics={
                "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
                "reportedAt": now.isoformat(),
                "runtimeMode": "hosted",
                "status": "ok",
            },
        )
    )
    await db.commit()
    await db.refresh(state)
    return state


async def _seed_full_tenant(
    db: AsyncSession,
    store: LocalFileStore,
    user: User,
    *,
    marker: str,
) -> dict[str, Any]:
    """Seed one tenant with at least one row in every PURGE_COVERAGE
    "purge" domain (minus the explicit ``_SEED_EXCEPTIONS``), plus the
    via-parent children the plan names (usage_events, vault items,
    channel secret) and the three S3 prefixes.

    Returns the seeded-table names so stage 1 can assert coverage at seed
    time (T-03-26: the round-trip must never assert deletion of data it
    never created).
    """
    now = datetime.now(UTC)
    seeded: set[str] = {"users", "projects"}  # user + Personal created upstream

    env = await create_env_with_project(
        db,
        user_id=user.id,
        machine_id=f"roundtrip-env-{uuid.uuid4().hex[:8]}",
        machine_name="Roundtrip Agent",
        agent_type="hermes",
    )
    seeded.add("agent_environments")  # (and a second projects row)

    runtime_state = await _seed_hosted_runtime_state(db, env)

    # One companion tool-call record (phase 05). Seeded rather than excepted: the row carries the
    # tenant's own `arguments` -- a path inside their filesystem -- and a `result` holding their
    # own file listing, which is exactly the kind of content a purge must be PROVEN to erase
    # rather than assumed to.
    db.add(
        CompanionDispatchCall(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="list_dir",
            tool_class="read",
            call_jti=uuid.uuid4().hex,
            arguments={"path": f"/{marker}"},
            status="completed",
            expires_at=now,
            result={"status": 200, "body": f'{{"items":["{marker}"]}}'},
        )
    )
    seeded.add("companion_dispatch_calls")

    # The tenant's approval policy and their destroy switch (phase 05, plan 03). Seeded rather than
    # excepted for the same reason: an `allow` row surviving a purge would silently pre-authorise a
    # write for whoever next holds that identity, so the erasure has to be proven.
    db.add(
        CompanionToolPolicy(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="write_file",
            mode="allow",
        )
    )
    seeded.add("companion_tool_policies")
    db.add(
        CompanionDestroyEnablement(
            user_id=user.id,
            companion_key="filebrowser",
            enabled_at=now,
            enabled_by_user_id=user.id,
        )
    )
    seeded.add("companion_destroy_enablements")
    # The tenant's companion tool-group block (phase 06, plan 24). Seeded rather than excepted: a
    # block surviving the purge would silently hide a companion from whoever next holds this
    # identity, so its erasure is proven.
    db.add(
        CompanionGroupBlock(
            user_id=user.id,
            companion_key="filebrowser",
            blocked_at=now,
            blocked_by_user_id=user.id,
        )
    )
    seeded.add("companion_group_blocks")

    # The tenant's blast-radius ceiling and what has been spent against it (phase 05, plan 04).
    # Seeded rather than excepted for the sharpest version of the same reason: a WIDENED cap row
    # surviving a purge would silently grant a larger blast radius to whoever next holds this
    # identity, and the erasure of a security ceiling is precisely the kind of claim that has to be
    # proven rather than assumed.
    db.add(
        CompanionCapPolicy(
            user_id=user.id,
            companion_key="filebrowser",
            per_call_calls=1,
            per_call_records=50,
            per_call_bytes=1024,
            per_session_calls=200,
            per_session_records=500,
            per_session_bytes=4096,
        )
    )
    seeded.add("companion_cap_policies")
    # The tenant's monthly companion spend ceiling (phase 06, plan 22). Seeded rather than excepted
    # for the reason the cap row above is: a ceiling surviving the purge would silently apply to
    # whoever next holds this identity, so its erasure is proven, not assumed.
    db.add(CompanionSpendCeiling(user_id=user.id, ceiling_usd_micros=25_000_000))
    seeded.add("companion_spend_ceilings")
    # The repeated-call breaker's counter for one of this tenant's calls (phase 06, plan 23),
    # seeded mid-cooldown: a cooldown surviving the purge would stop the same call for whoever
    # next holds this identity.
    db.add(
        CompanionRepeatBreaker(
            user_id=user.id,
            companion_key="filebrowser",
            verb="list_dir",
            action_sha256=hashlib.sha256(f"breaker-{marker}".encode()).hexdigest(),
            dry_run=False,
            window_started_at=now,
            count=6,
            tripped_until=now + timedelta(minutes=2),
        )
    )
    seeded.add("companion_repeat_breakers")
    db.add(
        CompanionSessionBudget(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            session_id=f"session-{marker}",
            window_started_at=now,
            calls_consumed=3,
            records_consumed=0,
            bytes_consumed=0,
        )
    )
    seeded.add("companion_session_budgets")
    # A live approval (phase 05, plan 06), seeded in the `prepared` state on purpose. An approval
    # is not a preference: it carries a remaining execution budget, so one surviving this purge
    # would be a standing authorization to write inside a filesystem, waiting for whoever next
    # holds this Clerk identity. That is the strongest reason in this file to PROVE the erasure
    # rather than except the table from it.
    db.add(
        CompanionApproval(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="delete_path",
            canonical_action_sha256=hashlib.sha256(
                f"roundtrip-{marker}-{user.id}".encode()
            ).hexdigest(),
            arguments={"path": f"/roundtrip-{marker}"},
            resolved_target_count=2,
            remaining_budget=1,
            status="prepared",
            expires_at=now + timedelta(minutes=15),
            decided_at=now,
            decided_by_user_id=user.id,
        )
    )
    seeded.add("companion_approvals")

    # One provenance row (phase 05, plan 07). Seeded rather than excepted for the same reason as
    # the dispatch row above: it carries the tenant's own path in `arguments` and the façade's own
    # sentence about their files in `error_text`, so the erasure has to be PROVEN.
    db.add(
        CompanionToolCall(
            user_id=user.id,
            environment_id=env.id,
            companion_key="filebrowser",
            verb="delete_path",
            tool_class="destroy",
            arguments_digest=hashlib.sha256(f"provenance-{marker}".encode()).hexdigest(),
            arguments={"path": f"/{marker}"},
            decision="refused_policy",
            error_text=f"refused a call against /{marker}",
        )
    )
    seeded.add("companion_tool_calls")

    # One tenant-installed companion with a platform-generated credential and a unit log line
    # (phase 06, plan 09). Seeded rather than excepted: a credential row surviving a purge would be
    # a live Postiz password for an application whose owner is gone, so the erasure of all three
    # tables -- the two children through the FK chain -- has to be PROVEN.
    installation_label = f"postiz-{uuid.uuid4().hex[:12]}"
    installation = CompanionInstallation(
        user_id=user.id,
        environment_id=env.id,
        companion_key="postiz",
        isolation_tier="standard",
        version="v2.24.0",
        pinned_digest=hashlib.sha256(marker.encode()).hexdigest(),
        hostname_label=installation_label,
        hostname=f"{installation_label}.companions.example",
        owner_email=f"owner+{installation_label}@companions.example",
    )
    db.add(installation)
    await db.flush()
    db.add(
        CompanionInstallationSecret(
            installation_id=installation.id,
            secret_name="admin-password",
            encrypted_value=f"ciphertext-{marker}".encode(),
            nonce=b"0" * 12,
        )
    )
    db.add(
        CompanionInstallLogLine(
            installation_id=installation.id,
            operation="install",
            seq=1,
            stream="stdout",
            line=f"installing for {marker}",
        )
    )
    db.add(
        BillingReconcileFinding(
            installation_id=installation.id,
            line_item_id=f"cli_{marker}",
            kind="price_mismatch",
            local_value="9000000",
            hosted_value="9000001",
            first_seen=now,
            last_seen=now,
        )
    )
    seeded.update(
        {
            "companion_installations",
            "companion_installation_secrets",
            "companion_install_log_lines",
            "billing_reconcile_findings",
        }
    )

    personal_project_id = (
        await db.execute(
            select(Project.id).where(
                Project.user_id == user.id, Project.kind == PROJECT_KIND_PERSONAL
            )
        )
    ).scalar_one()

    local_session_id = f"session-{uuid.uuid4().hex[:12]}"
    session_body = f'[{{"role": "user", "content": "hello {marker}"}}]'.encode()
    session_file_key = f"sessions/{user.id}/{local_session_id}.json"
    await store.put(session_file_key, session_body)
    session = Session(
        user_id=user.id,
        environment_id=env.id,
        local_session_id=local_session_id,
        started_at=now,
        file_key=session_file_key,
        # The real body hash: the share below verifies the copied body against it.
        content_hash=hashlib.sha256(session_body).hexdigest(),
    )
    db.add(session)
    await db.flush()
    seeded.add("sessions")

    db.add(
        UsageEvent(
            session_id=session.id,
            ts=now,
            provider="test-provider",
            model="test-model",
            in_tok=10,
            out_tok=20,
            cost_usd_micros=42,
        )
    )

    db.add(
        SessionPermission(
            session_id=session.id,
            kind=PERMISSION_KIND_USER,
            user_id=user.id,
            invited_by=user.id,
            accepted_at=now,
        )
    )
    seeded.add("session_permissions")

    # A cloud-deleted Session the tenant's agents must not re-sync (upstream, sync step 2).
    db.add(
        SessionSyncSuppression(
            user_id=user.id,
            local_session_id=f"suppressed-{uuid.uuid4().hex[:12]}",
        )
    )
    seeded.add("session_sync_suppressions")

    # A snapshot-row entry of the session message search index (upstream, sync step 3).
    db.add(
        SessionMessageSearch(
            user_id=user.id,
            session_id=session.id,
            content_revision="a" * 64,
            position=0,
            role="user",
            content=f"searchable {marker}",
        )
    )
    seeded.add("session_message_search")

    # A sync-subscription lease one of the tenant's daemons holds (upstream, sync step 3).
    db.add(SyncSubscriptionLease(user_id=user.id, expires_at=now + timedelta(minutes=5)))
    seeded.add("sync_subscription_leases")

    skill_key = f"skill-{uuid.uuid4().hex[:8]}"
    skill_hash = "b" * 64
    skill_archive = f"archive-bytes-{marker}".encode()
    skill_file_key = f"skills/{user.id}/{env.default_project_id}/{skill_key}/{skill_hash}.tar.gz"
    await store.put(skill_file_key, skill_archive)
    skill = Skill(
        user_id=user.id,
        project_id=env.default_project_id,
        skill_key=skill_key,
        name="Roundtrip Skill",
        content_hash=skill_hash,
        file_key=skill_file_key,
    )
    db.add(skill)
    seeded.add("skills")

    # Memory WITH an embedding vector — the embedding column dies with
    # the row (PURGE_COVERAGE comment) and the round-trip proves it held
    # a real vector, not NULL.
    db.add(
        Memory(
            user_id=user.id,
            content=f"memory {marker}",
            category="fact",
            embedding=[0.05] * 768,
        )
    )
    seeded.add("memories")

    vault = Vault(user_id=user.id, slug=f"vault-{uuid.uuid4().hex[:8]}", name="Roundtrip Vault")
    db.add(vault)
    await db.flush()
    db.add(
        VaultItem(
            vault_id=vault.id,
            item_name="service-credential-item",
            section="prod",
            encrypted_value=b"opaque-cipher",
            nonce=b"n" * 12,
        )
    )
    seeded.add("vaults")

    db.add(
        VaultCredentialProfile(
            user_id=user.id,
            project_id=env.default_project_id,
            tool="roundtrip-tool",
            encrypted_payload=b"opaque-profile",
            nonce=b"n" * 12,
        )
    )
    seeded.add("vault_credential_profiles")

    account = ChannelAccount(
        user_id=user.id,
        provider="telegram",
        name=f"bot-{uuid.uuid4().hex[:8]}",
        webhook_secret_hash="0" * 64,
        config={"bot_username": "roundtrip_bot"},
    )
    db.add(account)
    await db.flush()
    db.add(
        ChannelSecret(
            account_id=account.id,
            user_id=user.id,
            name="bot_token",
            encrypted_value=b"opaque-secret",
            value_nonce=b"n" * 12,
        )
    )
    seeded.add("channel_accounts")

    db.add(
        ApiKey(
            user_id=user.id,
            key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            key_prefix="clawdi_rt",
            label="roundtrip-key",
        )
    )
    seeded.add("api_keys")

    db.add(UserSetting(user_id=user.id, settings={"theme": "dark"}))
    seeded.add("user_settings")

    db.add(
        ProjectInvitation(
            project_id=personal_project_id,
            invitee_user_id=user.id,
            invitee_email=str(user.email),
            invited_by=user.id,
            resolved_owner_handle="roundtrip",
        )
    )
    seeded.add("project_invitations")

    db.add(
        ProjectMembership(
            project_id=personal_project_id,
            member_user_id=user.id,
            role="viewer",
            joined_via="invite",
            joined_at=now,
            resolved_owner_handle="roundtrip",
        )
    )
    seeded.add("project_memberships")

    db.add(
        ProjectShareLink(
            project_id=personal_project_id,
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            token_prefix="rtshare_",
            created_by=user.id,
            resolved_owner_handle="roundtrip",
        )
    )
    seeded.add("project_share_links")

    db.add(
        DeviceAuthorization(
            device_code=uuid.uuid4().hex + uuid.uuid4().hex,
            user_code=uuid.uuid4().hex[:12],
            user_id=user.id,
            expires_at=now + timedelta(hours=1),
        )
    )
    seeded.add("device_authorizations")

    provider_id = f"rt-provider-{uuid.uuid4().hex[:8]}"
    ai_provider = AiProvider(
        owner_user_id=user.id,
        provider_id=provider_id,
        type="openai_compatible",
        base_url="https://provider.roundtrip.example",
        auth_type="api_key",
    )
    db.add(ai_provider)
    await db.flush()
    seeded.add("ai_providers")

    db.add(
        AiProviderAuthPayload(
            owner_user_id=user.id,
            provider_id=provider_id,
            kind="api_key",
            source="user",
            encrypted_payload=b"opaque-auth",
            nonce=b"n" * 12,
        )
    )
    seeded.add("ai_provider_auth_payloads")

    db.add(
        AiProviderOAuthAttempt(
            owner_user_id=user.id,
            provider_row_id=ai_provider.id,
            provider_id=provider_id,
            oauth_provider="openrouter",
            auth_profile="default",
            flow_kind="authorization_code",
            state_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            encrypted_flow_payload=b"opaque-flow",
            flow_payload_nonce=b"n" * 12,
            expires_at=now + timedelta(hours=1),
        )
    )
    seeded.add("ai_provider_oauth_attempts")

    db.add(
        AiProviderOAuthRevokeTombstone(
            owner_user_id=user.id,
            provider_id=provider_id,
            oauth_provider="openrouter",
            token_type="refresh_token",
            token_sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            encrypted_token=b"opaque-token",
            token_nonce=b"n" * 12,
        )
    )
    seeded.add("ai_provider_oauth_revoke_tombstones")

    db.add(
        PlatformMutationIdempotency(
            operation="roundtrip.seed",
            idempotency_key=uuid.uuid4().hex,
            request_hash="c" * 64,
            owner_user_id=user.id,
            resource_type="roundtrip",
            response_status=200,
            encrypted_response=b"opaque-response",
            response_nonce=b"n" * 12,
        )
    )
    seeded.add("platform_mutation_idempotency")

    # Pre-existing object under the exports/ prefix so the purge's
    # third prefix sweep has seeded content beyond the stage-2 artifact.
    await store.put(f"exports/{user.id}/seed-{uuid.uuid4().hex[:8]}.tar.gz", marker.encode())

    await db.commit()

    # One share of the snapshot session through the production service, which copies the body
    # to `session-share-sources/<user id>/...` -- an object the purge must prove it erases.
    await create_session_share(
        db,
        store,
        session,
        created_by=user.id,
        agent_type=None,
        scope="session",
        position=None,
    )
    seeded.add("session_shares")

    return {
        "tables": seeded,
        "env": env,
        "deployment_id": runtime_state.deployment_id,
        "local_session_id": local_session_id,
        "session_body": session_body,
        "skill_project_id": env.default_project_id,
        "skill_key": skill_key,
        "skill_hash": skill_hash,
        "skill_archive": skill_archive,
    }


async def _seed_events_v1_session(
    session_factory: async_sessionmaker[AsyncSession],
    user: User,
    *,
    marker: str,
) -> dict[str, Any]:
    """Seed one committed events-v1 session through the production routes.

    Register an agent, batch, stage a two-event generation, PUT its chunk, commit, then append a
    third event: the chunk keys come from the real writer (`routes/session_events.py`), never from
    this test, so the purge is checked against the keys production actually writes. Each request
    gets its own session from ``session_factory``, as the app's own dependency does.
    """

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as request_db:
            yield request_db

    async def _override_get_auth() -> AuthContext:
        return AuthContext(user=user)

    overrides = {
        get_control_session: _override_get_session,
        get_session: _override_get_session,
        get_auth: _override_get_auth,
        get_auth_short_session: _override_get_auth,
        optional_web_auth: _override_get_auth,
    }
    events = [
        _event(0, "user", "user", f"events question {marker}"),
        _event(1, "assistant", "assistant", f"events answer {marker}"),
    ]
    with _dependency_overrides(overrides):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            session_id, local_id, environment_id, generation = await _seed_event_session(
                client, events=events
            )
            base = await client.get(f"/v1/sessions/{session_id}/events")
            assert base.status_code == 200, base.text
            appended_event = _event(2, "assistant-2", "assistant", f"events later {marker}")
            data, content_hash = _event_chunk([appended_event])
            final_head = advance_event_head(base.json()["head_hash"], [appended_event])
            appended = await client.post(
                f"/v1/sessions/{local_id}/events/append",
                data={
                    "environment_id": environment_id,
                    "append_id": str(uuid.uuid4()),
                    "generation": generation,
                    "base_revision": str(base.json()["revision"]),
                    "base_count": str(base.json()["count"]),
                    "base_head_hash": base.json()["head_hash"],
                    "final_count": "3",
                    "final_head_hash": final_head,
                    "content_hash": content_hash,
                },
                files={"file": ("2.ndjson", data, "application/x-ndjson")},
            )
            assert appended.status_code == 200, appended.text
    return {
        "local_session_id": local_id,
        "session_id": session_id,
        "events": [*events, appended_event],
    }


@pytest_asyncio.fixture(scope="module")
async def roundtrip(engine, tmp_path_factory: pytest.TempPathFactory) -> Any:
    """Run the full ACCESS-04 round-trip once; yield the captured evidence."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    store = LocalFileStore(str(tmp_path_factory.mktemp("roundtrip-files")))
    nonce = uuid.uuid4().hex[:12]
    result = SimpleNamespace()
    mp = pytest.MonkeyPatch()
    previous_overrides = dict(app.dependency_overrides)
    registered_deployments: list[str] = []
    subscription_keys: list[str] = []
    user_a: User | None = None
    user_b: User | None = None
    job_id: uuid.UUID | None = None
    purge_id: uuid.UUID | None = None

    try:
        try:
            # ---- wiring: settings, provider seam, /v2 reference mock ----
            from app.routes import admin as admin_routes
            from app.services import tenant_purge as tenant_purge_service

            provider = _RecordingProvider()
            mp.setattr(settings, "admin_api_key", _ADMIN_KEY)
            mp.setattr(settings, "deploy_api_url", "http://deploy.roundtrip.test")
            mp.setattr(settings, "deploy_api_key", "roundtrip-deploy-key")
            mp.setattr(admin_routes, "get_user_provisioning_provider", lambda: provider)
            mp.setattr(tenant_purge_service, "get_user_provisioning_provider", lambda: provider)
            # Exercise the REAL mock handlers: the executable /v2 contract
            # (GET-then-DELETE with If-Match + Idempotency-Key must be a
            # call shape the reference accepts).
            mp.setattr(
                tenant_purge_service,
                "_transport_override",
                ASGITransport(app=mock_deploy_api.app),
            )
            # Every object the real routes write must land in the store the workers and the
            # assertions read; `mp.undo()` in the `finally` restores the module globals.
            from app.routes import session_events as session_events_routes
            from app.routes import sessions as sessions_routes
            from app.routes import skills as skills_routes

            for routes_module in (session_events_routes, sessions_routes, skills_routes):
                mp.setattr(routes_module, "file_store", store)

            # ---- Stage 1: build tenants A (target) and B (control) ------
            # Each tenant is seeded completely before the next, and the whole store is
            # snapshotted between them, so every object is attributed to the seeding that
            # wrote it. The erasure assertion compares these whole-store sets -- it never
            # reads the purge's own prefix list.
            marker_a = f"target-{nonce}"
            marker_b = f"CONTROL-{nonce}"
            async with session_factory() as db:
                user_a = await _create_committed_user(db, nonce=nonce, label="target")
                user_b = await _create_committed_user(db, nonce=nonce, label="control")
                seeded_a = await _seed_full_tenant(db, store, user_a, marker=marker_a)
            events_a = await _seed_events_v1_session(session_factory, user_a, marker=marker_a)
            seeded_a["tables"].update(_EVENTS_V1_SEEDED_TABLES)
            keys_seeded_a = set(await store.list_keys(""))
            async with session_factory() as db:
                seeded_b = await _seed_full_tenant(db, store, user_b, marker=marker_b)
            events_b = await _seed_events_v1_session(session_factory, user_b, marker=marker_b)
            seeded_b["tables"].update(_EVENTS_V1_SEEDED_TABLES)
            keys_seeded_all = set(await store.list_keys(""))
            result.keys_seeded_a = keys_seeded_a
            result.keys_seeded_b = keys_seeded_all - keys_seeded_a
            result.events_a = events_a
            result.events_b = events_b
            result.marker_a = marker_a
            result.marker_b = marker_b

            for record, label in ((seeded_a, "target"), (seeded_b, "control")):
                deployment_id = str(record["deployment_id"])
                mock_deploy_api.DEPLOYMENTS[deployment_id] = mock_deploy_api._deployment(
                    deployment_id=deployment_id,
                    name=f"roundtrip-{label}",
                    status="running",
                )
                registered_deployments.append(deployment_id)
            # 06.2 D-09: the hosted service keys every owner by Clerk user id,
            # so the mock subscriptions live under that key (target first).
            for user in (user_a, user_b):
                key = user.clerk_id
                assert key, "round-trip tenants are Clerk principals"
                mock_deploy_api.SUBSCRIPTIONS[key] = {
                    "status": "active",
                    "plan_id": "compute_basic",
                }
                subscription_keys.append(key)

            result.user_a_id = user_a.id
            result.user_b_id = user_b.id
            result.subject_a = user_a.clerk_id
            result.seeded_tables_a = set(seeded_a["tables"])
            result.seeded_tables_b = set(seeded_b["tables"])
            result.deployment_a = str(seeded_a["deployment_id"])
            result.deployment_b = str(seeded_b["deployment_id"])
            result.seeded_a = seeded_a

            async with session_factory() as db:
                result.counts_a_after_seed = await _tenant_row_counts(db, user_a.id)
                result.counts_b_after_seed = await _tenant_row_counts(db, user_b.id)
                result.embedded_memories_a = (
                    await db.execute(
                        select(func.count())
                        .select_from(Memory)
                        .where(Memory.user_id == user_a.id, Memory.embedding.is_not(None))
                    )
                ).scalar_one()
            result.keys_b_after_seed = await _tenant_prefix_keys(store, user_b.id)

            # ---- Stages 2 + 3 drive the real admin surface --------------
            async def _override_get_session() -> Any:
                async with session_factory() as request_db:
                    yield request_db

            app.dependency_overrides[get_session] = _override_get_session
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # Stage 2: operator-triggered export.
                resp = await client.post(
                    f"/v1/admin/tenants/{user_a.id}/exports", headers=_AUTH, json={}
                )
                result.export_trigger_status = resp.status_code
                result.export_trigger_body = resp.json()
                job_id = uuid.UUID(result.export_trigger_body["job_id"])
                result.export_job_id = job_id

                export_worker = TenantExportWorker(session_factory, file_store=store)
                job: TenantExportJob | None = None
                for _ in range(5):
                    await export_worker.run_once()
                    async with session_factory() as db:
                        job = await db.get(TenantExportJob, job_id)
                    if job is not None and job.status == "complete":
                        break
                assert job is not None, "export job row disappeared"
                result.export_job_status = job.status
                result.export_artifact_key = job.artifact_key
                result.export_manifest = job.manifest
                assert job.artifact_key is not None
                result.archive_members = _tar_members(await store.get(job.artifact_key))

                async with session_factory() as db:
                    result.counts_a_after_export = await _tenant_row_counts(db, user_a.id)
                    result.counts_b_after_export = await _tenant_row_counts(db, user_b.id)
                result.keys_b_after_export = await _tenant_prefix_keys(store, user_b.id)

                # Stage 3: operator delete → immediate fence + scheduled purge.
                result.delete_requested_before = datetime.now(UTC)
                resp = await client.post(f"/v1/admin/tenants/{user_a.id}/delete", headers=_AUTH)
                result.delete_status = resp.status_code
                result.delete_body = resp.json()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)

            result.provider_deleted_after_trigger = list(provider.deleted)

            async with session_factory() as db:
                fenced = False
                try:
                    await assert_clerk_principal_active(
                        db, subject=str(user_a.clerk_id), issuer=_ISSUER
                    )
                except PrincipalTerminatedError:
                    fenced = True
                result.fenced_after_delete = fenced

                purge_row = (
                    await db.execute(select(TenantPurge).where(TenantPurge.user_id == user_a.id))
                ).scalar_one()
                purge_id = purge_row.id
                result.purge_id = purge_id
                result.purge_status_after_delete = purge_row.status
                result.purge_after = purge_row.purge_after
                result.purge_subject = purge_row.subject
                result.purge_issuer = purge_row.issuer
                result.purge_lifecycle_id = purge_row.lifecycle_id

                # Defer lifecycle cleanup far out: the round-trip pins the
                # states-join deployment enumeration, and a concurrently
                # running cleanup worker must not race the purge.
                await db.execute(
                    update(PrincipalLifecycle)
                    .where(PrincipalLifecycle.id == purge_row.lifecycle_id)
                    .values(next_cleanup_attempt_at=datetime.now(UTC) + timedelta(days=365))
                )
                await db.commit()

            async with session_factory() as db:
                result.counts_a_before_purge = await _tenant_row_counts(db, user_a.id)

            # ---- Stage 4: force the purge due and run the worker --------
            async with session_factory() as db:
                now = datetime.now(UTC)
                await db.execute(
                    update(TenantPurge)
                    .where(TenantPurge.id == purge_id)
                    .values(purge_after=now - timedelta(hours=1), next_attempt_at=now)
                )
                await db.commit()

            purge_worker = TenantPurgeWorker(session_factory, file_store=store)
            final_purge: TenantPurge | None = None
            for _ in range(5):
                await purge_worker.run_once()
                async with session_factory() as db:
                    final_purge = await db.get(TenantPurge, purge_id)
                if final_purge is not None and final_purge.status == "complete":
                    break
            assert final_purge is not None, "purge tombstone row disappeared"
            result.purge_final_status = final_purge.status
            result.purge_error = final_purge.error

            result.prefix_keys_a_after_purge = await _tenant_prefix_keys(store, user_a.id)
            result.keys_after_purge = set(await store.list_keys(""))
            result.keys_b_after_purge = await _tenant_prefix_keys(store, user_b.id)
            result.deployment_status_a = mock_deploy_api.DEPLOYMENTS[result.deployment_a]["status"]
            result.deployment_status_b = mock_deploy_api.DEPLOYMENTS[result.deployment_b]["status"]
            subscription_key_a, subscription_key_b = subscription_keys
            result.subscription_a = dict(mock_deploy_api.SUBSCRIPTIONS[subscription_key_a])
            result.subscription_b = dict(mock_deploy_api.SUBSCRIPTIONS[subscription_key_b])
            result.provider_deleted_final = list(provider.deleted)

            async with session_factory() as db:
                result.counts_a_after_purge = await _tenant_row_counts(db, user_a.id)
                result.counts_b_after_purge = await _tenant_row_counts(db, user_b.id)
                result.lifecycle_tombstone_present = (
                    await db.get(PrincipalLifecycle, result.purge_lifecycle_id)
                ) is not None
                fenced_after_purge = False
                try:
                    await assert_clerk_principal_active(
                        db, subject=str(result.subject_a), issuer=_ISSUER
                    )
                except PrincipalTerminatedError:
                    fenced_after_purge = True
                result.fenced_after_purge = fenced_after_purge
        finally:
            mp.undo()
            app.dependency_overrides.clear()
            app.dependency_overrides.update(previous_overrides)
            for deployment_id in registered_deployments:
                mock_deploy_api.DEPLOYMENTS.pop(deployment_id, None)
            for key in subscription_keys:
                mock_deploy_api.SUBSCRIPTIONS.pop(key, None)

        yield result
    finally:
        # Committed-lane cleanup: control tenant rows via the sweep's own
        # machinery, then tenant A's retained tombstones and the audit
        # rows this module created. (The v2 fence tables are never
        # touched: this module deliberately creates no fence rows.)
        async with session_factory() as db:
            if user_b is not None:
                await _delete_covered_rows(db, user_b.id)
            if user_a is not None:
                await _delete_covered_rows(db, user_a.id)
                await db.execute(delete(TenantPurge).where(TenantPurge.user_id == user_a.id))
                audit_resource_ids = [str(user_a.id)]
                if job_id is not None:
                    audit_resource_ids.append(str(job_id))
                if purge_id is not None:
                    audit_resource_ids.append(str(purge_id))
                await db.execute(
                    delete(ControlPlaneAuditEvent).where(
                        ControlPlaneAuditEvent.action.in_(
                            (
                                "tenant.export_requested",
                                "tenant.export_completed",
                                "tenant.delete_requested",
                                "tenant.purged",
                            )
                        ),
                        ControlPlaneAuditEvent.resource_id.in_(audit_resource_ids),
                    )
                )
                await db.execute(
                    delete(PrincipalLifecycle).where(
                        PrincipalLifecycle.issuer == _ISSUER,
                        PrincipalLifecycle.subject == str(user_a.clerk_id),
                    )
                )
            await db.commit()


# ==========================================================================
# Stage 1: build — every purge domain seeded, coverage asserted at seed time
# ==========================================================================


async def test_stage1_build_seeds_every_purge_coverage_domain(roundtrip) -> None:
    """Seeded tables ⊇ PURGE_COVERAGE 'purge' tables minus the explicit
    exceptions (each carrying a reason), and every seeded table actually
    holds tenant-A rows — the round-trip never asserts deletion of data
    it did not create (T-03-26)."""
    purge_tables = _purge_tables()
    assert set(_SEED_EXCEPTIONS) <= purge_tables

    missing = purge_tables - roundtrip.seeded_tables_a - set(_SEED_EXCEPTIONS)
    assert not missing, (
        f"purge-disposition tables not seeded and not excepted: {sorted(missing)} — "
        "extend _seed_full_tenant or add an explicit exception with a reason"
    )

    for table_name in sorted(roundtrip.seeded_tables_a):
        assert roundtrip.counts_a_after_seed.get(table_name, 0) > 0, (
            f"{table_name} was marked seeded but holds no tenant-A rows"
        )

    # The plan's named seed minimum, explicitly.
    for table_name in (
        "sessions",
        "skills",
        "memories",
        "vaults",
        "channel_accounts",
        "usage_events",
        "api_keys",
        "projects",
        "agent_environments",
        "hosted_runtime_states",
    ):
        assert roundtrip.counts_a_after_seed[table_name] > 0, table_name
    assert roundtrip.embedded_memories_a == 1  # memory WITH embedding vector


async def test_stage1_seed_exercises_every_tenant_object_prefix(roundtrip) -> None:
    """Tenant A's seeding wrote at least one object under every registered prefix, so no
    registry entry's erasure is asserted vacuously."""
    empty = [
        prefix
        for prefix in tenant_object_prefixes(roundtrip.user_a_id)
        if not any(key.startswith(prefix) for key in roundtrip.keys_seeded_a)
    ]
    assert empty == [], f"tenant A's seeding wrote nothing under: {empty}"


async def test_stage1_build_control_tenant_has_parallel_set(roundtrip) -> None:
    assert roundtrip.seeded_tables_b == roundtrip.seeded_tables_a
    for table_name in sorted(roundtrip.seeded_tables_b):
        assert roundtrip.counts_b_after_seed.get(table_name, 0) > 0, table_name
    assert all(keys for keys in roundtrip.keys_b_after_seed.values())


# ==========================================================================
# Stage 2: export — manifest matches the seeds, archive is secrets-clean
# ==========================================================================


async def test_stage2_export_manifest_counts_match_stage1_seeds(roundtrip) -> None:
    assert roundtrip.export_trigger_status == 201
    assert roundtrip.export_job_status == "complete"
    manifest = roundtrip.export_manifest
    assert manifest["export_version"] == 1
    assert manifest["user_id"] == str(roundtrip.user_a_id)
    assert manifest["vault_values_included"] is False

    rows = {entry["name"]: entry["rows"] for entry in manifest["domains"]}
    assert rows["users"] == 1
    # Personal + the seeded env's project + the env-local project `/v1/agents` creates when the
    # events-v1 seed registers its agent.
    assert rows["projects"] == 3
    # The seeded env + the agent the events-v1 seed registers through `/v1/agents`.
    assert rows["agents"] == 2
    # The snapshot-v1 session + the events-v1 session the events seed commits.
    assert rows["sessions"] == 2
    assert rows["skills"] == 1
    assert rows["memories"] == 1
    assert rows["vaults"] == 1
    assert rows["channels"] == 1
    assert rows["usage_events"] == 1
    assert rows["api_keys"] == 1
    assert rows["audit"] == 1  # the export_requested audit row itself
    assert rows["hosted_runtimes"] == 2  # state + config observation
    # INT-3: the activity log with its approval, and the tenant's companion controls.
    assert rows["companion_activity"] == 2  # tool call + approval
    assert rows["companion_settings"] == 5  # policy, destroy, block, cap, ceiling

    members = roundtrip.archive_members
    for name, count in rows.items():
        if count > 0:
            assert f"{name}.jsonl" in members, f"missing {name}.jsonl"
            assert len(_jsonl_records(members[f"{name}.jsonl"])) == count

    seeded = roundtrip.seeded_a
    # The body member is named by the immutable session id (06.1-20), read through the record.
    (session_record,) = [
        record
        for record in _jsonl_records(members["sessions.jsonl"])
        if record["local_session_id"] == seeded["local_session_id"]
    ]
    session_member = f"sessions/{session_record['id']}.json"
    assert session_record["body_member"] == session_member
    assert members[session_member] == seeded["session_body"]
    skill_member = (
        f"skills/{seeded['skill_project_id']}/{seeded['skill_key']}/{seeded['skill_hash']}.tar.gz"
    )
    assert members[skill_member] == seeded["skill_archive"]

    assert roundtrip.export_artifact_key == (
        f"exports/{roundtrip.user_a_id}/{roundtrip.export_job_id}.tar.gz"
    )


async def test_stage2_export_carries_events_v1_session_body(roundtrip) -> None:
    """A synced events-v1 session is in the archive as NDJSON whose events-v1 head equals the
    session record's `content_hash` (Phase 3 D-10/D-11), and no stale snapshot member stands
    beside it."""
    members = roundtrip.archive_members
    local_id = roundtrip.events_a["local_session_id"]
    session_id = roundtrip.events_a["session_id"]
    member = f"sessions/{session_id}.events.ndjson"
    assert member in members, f"events-v1 session body missing from the archive: {member}"
    body = members[member]
    lines = body.splitlines()
    assert len(lines) == 3
    assert roundtrip.marker_a.encode() in body
    assert roundtrip.marker_b.encode() not in body
    assert [json.loads(line) for line in lines] == roundtrip.events_a["events"]

    records = [
        record
        for record in _jsonl_records(members["sessions.jsonl"])
        if record["local_session_id"] == local_id
    ]
    assert len(records) == 1
    assert records[0]["id"] == session_id
    assert records[0]["body_member"] == member
    assert (
        advance_event_head(EMPTY_EVENT_HEAD, [json.loads(line) for line in lines])
        == records[0]["content_hash"]
    )
    assert f"sessions/{session_id}.json" not in members
    # No member is named by the local id any more (06.1-20).
    assert not any(local_id in name for name in members if name.startswith("sessions/"))


async def test_stage2_export_default_archive_passes_secretish_sweep(roundtrip) -> None:
    """Every record key across every JSONL member of the default archive
    fails `_is_secretish_key` — the export is never a credentials bundle
    (D-10), re-proven over the full-coverage synthetic tenant."""
    swept = 0
    for name, payload in roundtrip.archive_members.items():
        if not name.endswith(".jsonl"):
            continue
        for record in _jsonl_records(payload):
            for key in _iter_keys(record):
                swept += 1
                assert not _is_secretish_key(key), f"secretish key {key!r} in {name}"
    assert swept > 0


# ==========================================================================
# Stage 3: delete — immediate fence, scheduled purge, identity deletion
# ==========================================================================


async def test_stage3_delete_fences_immediately_and_schedules_purge(roundtrip) -> None:
    assert roundtrip.delete_status == 200
    assert roundtrip.fenced_after_delete is True

    assert roundtrip.purge_status_after_delete == "pending"
    assert roundtrip.purge_subject == roundtrip.subject_a
    assert roundtrip.purge_issuer == _ISSUER
    assert str(roundtrip.purge_lifecycle_id) == roundtrip.delete_body["lifecycle_id"]
    expected_after = roundtrip.delete_requested_before + timedelta(
        days=settings.tenant_purge_grace_days
    )
    assert abs((roundtrip.purge_after - expected_after).total_seconds()) < 300

    # Identity deleted through the provider seam at trigger time
    # (ADR-0005: Clawdi decides, the provisioner executes).
    assert roundtrip.provider_deleted_after_trigger == [roundtrip.subject_a]


# ==========================================================================
# Stage 4: purge — zero rows outside the tombstone set, empty prefixes,
# external teardown observed at the executable /v2 reference
# ==========================================================================


async def test_stage4_purge_zero_rows_outside_tombstone_set(roundtrip) -> None:
    """After the purge, every PURGE_COVERAGE-scoped table holds zero
    tenant-A rows. The iteration comes from the coverage map itself
    (purge + via-parent dispositions; tombstone/anonymize/global excluded
    by construction), and every seeded table is proven non-empty
    immediately before the purge so the zero cannot be vacuous."""
    for table_name in sorted(roundtrip.seeded_tables_a | set(_SEED_EXCEPTIONS)):
        assert roundtrip.counts_a_before_purge.get(table_name, 0) > 0, (
            f"{table_name} was empty before the purge — vacuous deletion assertion"
        )

    leftovers = {
        table_name: count
        for table_name, count in roundtrip.counts_a_after_purge.items()
        if count != 0
    }
    assert leftovers == {}, f"tenant rows survived the purge: {leftovers}"

    assert roundtrip.purge_final_status == "complete", roundtrip.purge_error
    # The fence tombstone survives and keeps rejecting the identity.
    assert roundtrip.lifecycle_tombstone_present is True
    assert roundtrip.fenced_after_purge is True


async def test_stage4_purge_empties_s3_prefixes(roundtrip) -> None:
    assert roundtrip.prefix_keys_a_after_purge == {
        prefix: [] for prefix in tenant_object_prefixes(roundtrip.user_a_id)
    }


async def test_stage4_purge_leaves_only_the_control_tenants_objects(roundtrip) -> None:
    """After the purge the whole store holds exactly the objects the control tenant's seeding
    wrote (Phase 3 D-16). This compares whole-store key sets and deliberately reads no prefix
    list from the purge, so a writer prefix the purge does not know about fails here instead
    of hiding."""
    keys_a = roundtrip.keys_seeded_a
    keys_b = roundtrip.keys_seeded_b
    assert keys_a, "tenant A's seeding wrote no objects -- vacuous erasure assertion"
    assert keys_b, "tenant B's seeding wrote no objects -- vacuous scope assertion"
    assert keys_a.isdisjoint(keys_b)
    events_prefix = f"session-events/v1/{roundtrip.user_a_id}/"
    assert any(key.startswith(events_prefix) for key in keys_a), (
        f"tenant A's seeding wrote no {events_prefix} key -- the events-v1 seed is vacuous"
    )

    survivors = sorted(roundtrip.keys_after_purge - keys_b)
    lost = sorted(keys_b - roundtrip.keys_after_purge)
    assert survivors == [], f"objects outside the control tenant survived the purge: {survivors}"
    assert lost == [], f"the purge deleted control-tenant objects: {lost}"


async def test_stage4_purge_tears_down_deployment_subscription_and_identity(roundtrip) -> None:
    """The mock deploy API — the executable /v2 reference — received and
    accepted DELETE /v2/deployments/{id} for tenant A's deployment and
    the subscription cancel; the fake provider recorded the purge's
    404-tolerant identity re-delete."""
    assert roundtrip.deployment_status_a == "deleted"
    assert roundtrip.subscription_a["status"] == "canceled"
    # Trigger-time delete + purge-time re-issue, both for A's subject only.
    assert roundtrip.provider_deleted_final == [roundtrip.subject_a, roundtrip.subject_a]


# ==========================================================================
# Stage 5: control — tenant B survives every stage byte-identically
# ==========================================================================


async def test_stage5_control_tenant_survives_untouched(roundtrip) -> None:
    """Tenant B's row counts and S3 keys are identical after the export
    AND after the purge; its deployment, subscription and identity were
    never touched (purge over-reach guard, RESEARCH threat table)."""
    assert roundtrip.counts_b_after_export == roundtrip.counts_b_after_seed
    assert roundtrip.counts_b_after_purge == roundtrip.counts_b_after_seed
    assert roundtrip.keys_b_after_export == roundtrip.keys_b_after_seed
    assert roundtrip.keys_b_after_purge == roundtrip.keys_b_after_seed

    assert roundtrip.deployment_status_b == "running"
    assert roundtrip.subscription_b["status"] == "active"
    subject_b_deleted = [
        subject for subject in roundtrip.provider_deleted_final if subject != roundtrip.subject_a
    ]
    assert subject_b_deleted == []
