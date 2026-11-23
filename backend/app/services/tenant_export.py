"""Tenant export assembly (ACCESS-04, D-10/D-11).

Builds one tar.gz per tenant: `manifest.json` at the root, one
`<domain>.jsonl` per non-empty domain, session bodies under
`sessions/`, and skill archives under `skills/`.

A session body takes one of two forms, chosen by the session's content
protocol with the same precedence the serving path applies: an events-v1
session with a committed generation exports
`sessions/<id>.events.ndjson` (its verified chunk bytes in seq order; the
events-v1 head over those lines is the record's `content_hash`), and
otherwise a snapshot-v1 session exports its raw snapshot at
`sessions/<id>.json`, where `<id>` is the record's `id` (the immutable
session id). The Phase 3 member was `sessions/<local_session_id>.json`.
Plan 06.1-20 keyed both forms on the session id, because upstream #1229
made `local_session_id` unique only per origin environment, so two
sessions of one tenant could write one member. A member name is written
at most once, and a repeat fails the export (`ExportMemberCollisionError`).
An events-v1 session's stale `file_key` is never read, so the archive
never carries a stale snapshot beside its events. Each `sessions.jsonl`
record names its `content_protocol` and the `body_member` that carries
its body in this archive (null when the archive has none), so a reader
knows which body pairs with the record's `content_hash`.

No body is dropped silently. Only the events form is verified against
`content_hash`: events-v1 content that is missing or damaged for good is
listed in the manifest's `session_bodies_omitted` with its protocol and a
reason (`missing` or `invalid`). A snapshot body is exported as stored,
and one whose object is missing is recorded as `missing`. The rest of the
export completes either way, since retrying would never recover the body.
A storage outage (`SessionContentUnavailable`) is not an omission: it
fails the job, and the worker retries it. Session rows are loaded once
per export, so the records and the bodies describe the same sessions,
and a session created while the export runs belongs to the next export.

Skill archives follow the same rules. The active skill rows are loaded
once, before any body is read, and feed both `skills.jsonl` and the
`skills/<project_id>/<skill_key>/<content_hash>.tar.gz` members, so a
skill re-uploaded while the export runs cannot pair a record's hash with
another hash's archive. Each skill record names its `archive_member`
(null when the archive has none), and a skill whose stored object is
missing is listed in the manifest's `skill_archives_omitted` with reason
`missing` rather than dropped silently.

Every serializer is an explicit column allowlist — NEVER a generic
SELECT-*/row-dict dump — so a new secretish column added to a model can
never leak into the export by default (D-10). The `EXPORT_COVERAGE`
map records an explicit disposition for every tenant-owned table -- one
carrying a tenant column, or reaching one through foreign keys -- so
nothing is silently dropped (review finding 6, INT-3);
`tests/test_tenant_export.py` sweeps `Base.metadata` and fails on a
missing entry.

File store access goes through the async `FileStore` facade
(`get_file_store()` seam from 03-02). The one exception is a companion's
media (plan 06-19, D-19 at CK-C): it lives in the installation's OWN R2
bucket, not in the platform file store, so it is read through
`create_s3_object_store_client` with the operator's admin keys -- behind
an injectable factory so no test reaches a real bucket.

**Companions (plan 06-19, D-31).** A job created for a tenant with a live
companion installation records one pending component per installation in
its JSONB `manifest` (`companion_components`). The manifest projection
asks the installation's runtime for the database dump (`exportRequests`);
the runtime uploads it through the runtime route, which stores it in the
file store and marks the component `uploaded`. Assembly then adds
`companions/<key>/<installation_id>/database.sql.gz` and every media
object, and the manifest gains a `companions` domain of NON-SECRET
installation facts. An export never completes without the companion data
it promised: a component still owed after 30 minutes, a failed one, or
media over the per-installation bound fails the job by name.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import JsonValue
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext
from app.core.config import settings
from app.models.agent_plugin import AgentPluginInstallation
from app.models.api_key import ApiKey
from app.models.audit import ControlPlaneAuditEvent
from app.models.channel import (
    ChannelAccount,
    ChannelBinding,
    ChannelBindingAlias,
    ChannelBotAgentLink,
    ChannelPairCode,
)
from app.models.companion_approval import CompanionApproval
from app.models.companion_budget import CompanionSpendCeiling
from app.models.companion_installation import (
    CompanionInstallation,
    serving_installation_filter,
)
from app.models.companion_session_budget import CompanionCapPolicy
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.models.hosted_runtime import HostedRuntimeConfigObservation, HostedRuntimeState
from app.models.memory import Memory
from app.models.project import Project
from app.models.session import AgentEnvironment, Session
from app.models.skill import AgentSkillReference, Skill
from app.models.tenant_export import (
    TENANT_EXPORT_STATUS_QUEUED,
    TENANT_EXPORT_STATUS_RUNNING,
    TenantExportJob,
)
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.models.vault import Vault, VaultItem, VaultSecretRequest
from app.services.companion_postiz import (
    POSTIZ_COMPANION_KEY,
    POSTIZ_MEDIA_STEP,
    POSTIZ_R2_REGION,
    postiz_step_handles,
)
from app.services.file_store import FileStore
from app.services.file_store_s3 import S3ObjectStoreClient, create_s3_object_store_client
from app.services.session_content import (
    SessionContentInvalid,
    SessionContentMissing,
    load_session_events_ndjson,
)
from app.services.vault_crypto import decrypt as vault_decrypt

# Presigned download link TTL for completed export artifacts (D-13;
# planner-discretion decision recorded in 03-01). The clock starts when
# the operator fetches the status list — links are presigned on read.
EXPORT_PRESIGNED_TTL_SECONDS = 3600

EXPORT_VERSION = 1

# The manifest key that lists session bodies the archive could not carry (plan 06.1-19). It is
# set only when something was omitted, so an undamaged tenant keeps the Phase 3 manifest keys.
SESSION_BODIES_OMITTED_FIELD = "session_bodies_omitted"
# The same for skill archives whose stored object is missing (06.1 review WR-03). Also set only
# when something was omitted.
SKILL_ARCHIVES_OMITTED_FIELD = "skill_archives_omitted"

# Explicit disposition for every tenant-owned table: either the export
# domain its rows land in ("domain:<name>") or an "excluded:<reason>"
# entry. A table is tenant-owned when it carries a tenant column, or when
# it reaches such a table through foreign keys (INT-3: the 06.1 tables
# hang off agents, skills and vaults without a tenant column of their
# own). D-10's wording is "channel config" — config tables export,
# transport/ephemera and anything secretish stay out, and every table has
# a recorded disposition either way. An entry reading "excluded:not yet
# exported" is a known export gap, recorded so it is visible rather than
# silently dropped.
EXPORT_COVERAGE: dict[str, str] = {
    # --- exported domains ---------------------------------------------------
    "users": "domain:users",
    "projects": "domain:projects",
    "agent_environments": "domain:agents",
    "sessions": "domain:sessions",
    "skills": "domain:skills",
    "memories": "domain:memories",  # embeddings are derived and not exported
    "vaults": "domain:vaults",
    # Item names and sections only; values only on the explicit opt-in (D-10).
    "vault_items": "domain:vaults",
    # Pending field reservations, exported inside their vault's record. The token hash and the
    # per-field baselines (digests over the stored ciphertext) never leave: they are derived from
    # secrets, whatever `include_vault_values` says.
    "vault_secret_requests": "domain:vaults",
    "usage_events": "domain:usage_events",
    "api_keys": "domain:api_keys",  # metadata only; no hash, no key material
    "control_plane_audit_events": "domain:audit",
    # The installation's non-secret facts go in the manifest's `companions` domain (plan 06-19).
    "companion_installations": "domain:companions",
    # The 06.1 agent attachments: which catalog plugins and which project skills each agent uses.
    "agent_plugin_installations": "domain:agent_extensions",
    "agent_skill_references": "domain:agent_extensions",
    # The companion activity log and the approvals it references (phase 05). The `arguments` and
    # `dry_run_diff` JSONB stay out for the reason the audit domain leaves `details` out: they are
    # sanitized at write time, but their keys are caller-defined, and the export allowlists column
    # names only.
    "companion_tool_calls": "domain:companion_activity",
    "companion_approvals": "domain:companion_activity",
    # The tenant's own companion controls: approval modes, switches, blocks, caps and ceiling.
    "companion_tool_policies": "domain:companion_settings",
    "companion_destroy_enablements": "domain:companion_settings",
    "companion_group_blocks": "domain:companion_settings",
    "companion_cap_policies": "domain:companion_settings",
    "companion_spend_ceilings": "domain:companion_settings",
    # --- secretish -----------------------------------------------------------
    "ai_provider_auth_payloads": "excluded:secretish",
    "ai_provider_oauth_attempts": "excluded:secretish",
    "ai_provider_oauth_revoke_tombstones": "excluded:secretish",
    "companion_installation_secrets": "excluded:secretish",
    "device_authorizations": "excluded:secretish",
    "vault_credential_profiles": "excluded:secretish",
    # --- bookkeeping, counters and derived state -------------------------------
    "companion_dispatch_calls": (
        "excluded:transport queue; each attempt is exported from companion_tool_calls"
    ),
    "companion_session_budgets": "excluded:rolling spend counters, not tenant content",
    "companion_repeat_breakers": "excluded:repeat-call breaker counters, not tenant content",
    "companion_install_log_lines": "excluded:install unit output, operational ephemera",
    "billing_reconcile_findings": (
        "excluded:billing reconciliation bookkeeping between local and hosted, not tenant content"
    ),
    "session_message_search": "excluded:derived search index of the exported session bodies",
    "session_sync_suppressions": "excluded:sync bookkeeping for cloud-deleted sessions",
    "sync_subscription_leases": "excluded:ephemeral sync subscription leases",
    "platform_mutation_idempotency": "excluded:idempotency bookkeeping (encrypted responses)",
    "share_redeem_attempts": "excluded:share-link redeem telemetry",
    "tenant_export_jobs": "excluded:the export pipeline's own job bookkeeping",
    "tenant_purges": "excluded:platform lifecycle record, not tenant content",
    "principal_lifecycles": "excluded:platform lifecycle fence, not tenant content",
    "clerk_principal_suspensions": "excluded:platform enforcement record, not tenant content",
    "clerk_webhook_event_receipts": "excluded:identity provider webhook evidence",
    "v2_runtime_environment_fences": "excluded:runtime observation bookkeeping",
    "v2_runtime_observation_inbox": "excluded:runtime observation bookkeeping",
    "v2_runtime_observation_heads": "excluded:runtime observation bookkeeping",
    "v2_runtime_observation_consumer_cursors": "excluded:runtime observation bookkeeping",
    # --- known export gaps ---------------------------------------------------
    "user_settings": "excluded:not yet exported (dashboard preferences)",
    "ai_providers": "excluded:not yet exported (provider configuration metadata)",
    "agent_project_bindings": "excluded:not yet exported (agent-to-project bindings)",
    "project_invitations": "excluded:not yet exported (project sharing)",
    "project_memberships": "excluded:not yet exported (project sharing)",
    "project_share_links": "excluded:not yet exported (project sharing)",
    "session_permissions": "excluded:not yet exported (session sharing)",
    "session_shares": "excluded:not yet exported (session sharing)",
    "vault_project_attachments": "excluded:not yet exported (vault-to-project attachments)",
    "vault_project_slug_aliases": "excluded:not yet exported (vault slug aliases)",
    # Hosted runtime metadata exports as its own domain; rows carry no
    # user column, so enumeration goes through the tenant's
    # AgentEnvironment linkage.
    "hosted_runtime_states": "domain:hosted_runtimes",
    "hosted_runtime_config_observations": "domain:hosted_runtimes",
    "hosted_runtime_secrets": "excluded:secretish",
    # Channel config tables (D-10: channel config is included).
    "channel_accounts": "domain:channels",
    "channel_bot_agent_links": "domain:channels",
    "channel_bindings": "domain:channels",
    "channel_binding_aliases": "domain:channels",
    "channel_pair_codes": "domain:channels",
    # Secretish channel tables — never exported (D-10).
    "channel_secrets": "excluded:secretish",
    "channel_agent_credentials": "excluded:secretish",
    "channel_whatsapp_auth_certs": "excluded:secretish",
    # Transport / ephemera — message payloads and operational state are
    # not channel config (D-10).
    "channel_messages": "excluded:transport message payloads are not channel config (D-10)",
    "channel_deliveries": "excluded:transport delivery ledger, not channel config (D-10)",
    "channel_debug_events": "excluded:operational debug ephemera, not channel config (D-10)",
    "channel_attachment_uploads": (
        "excluded:transient attachment staging, not channel config (D-10)"
    ),
    "channel_scheduled_messages": "excluded:transport queue state, not channel config (D-10)",
    "channel_account_runtime_markers": (
        "excluded:provider runtime ephemera, not channel config (D-10)"
    ),
    "channel_whatsapp_onboarding_sessions": (
        "excluded:device onboarding ephemera, not channel config (D-10)"
    ),
    "channel_agent_references": ("excluded:provider reference ephemera, not channel config (D-10)"),
    # events-v1 session content (upstream sync steps 3-4). The session's CURRENT committed
    # generation is exported as its `sessions/<local_session_id>.events.ndjson` body: each
    # committed generation holds the full event log from seq 0, so the current one is the whole
    # conversation. Staging generations and superseded ones (kept only while a share pins them)
    # are earlier revisions or upload bookkeeping, and are not exported.
    "session_event_generations": "domain:sessions",
    "session_event_chunks": "domain:sessions",
    "session_event_append_receipts": (
        "excluded:append idempotency receipts (ids, counts, hashes) are upload bookkeeping, "
        "not session content"
    ),
}

# Domain order is the manifest order — stable for consumers (03-09
# round-trip, 03-10 admin UI).
EXPORT_DOMAINS = (
    "users",
    "projects",
    "agents",
    "sessions",
    "skills",
    "memories",
    "vaults",
    "channels",
    "usage_events",
    "api_keys",
    "audit",
    "hosted_runtimes",
    "agent_extensions",
    "companion_activity",
    "companion_settings",
)


# ---------------------------------------------------------------------------
# Companions (plan 06-19, D-31, D-19)
# ---------------------------------------------------------------------------

# The domain name a companion-bearing export adds to its manifest. It is NOT in
# `EXPORT_DOMAINS`: a tenant without a companion gets exactly the Phase 3 archive.
COMPANIONS_EXPORT_DOMAIN = "companions"

# The manifest field a job's companion components live in (the job's JSONB `manifest`).
COMPANION_COMPONENTS_FIELD = "companion_components"

COMPONENT_STATE_PENDING = "pending"
COMPONENT_STATE_UPLOADED = "uploaded"
COMPONENT_STATE_FAILED = "failed"

# The runtime's upload body bound (a gzip database dump) and the media bound, both per
# installation. Over either, the export fails by name rather than silently truncating.
COMPANION_EXPORT_UPLOAD_MAX_BYTES = 512 * 1024 * 1024
COMPANION_MEDIA_EXPORT_MAX_BYTES = 512 * 1024 * 1024
# A component the runtime has not delivered within this long fails the whole job.
COMPANION_COMPONENT_DEADLINE_SECONDS = 30 * 60
# How long the worker waits before looking at a job whose component is still owed.
COMPANION_COMPONENT_DEFER_SECONDS = 30
# The manifest contract carries at most this many export requests per installation.
COMPANION_MAX_EXPORT_REQUESTS = 3

# The named reasons a companion-bearing export fails with.
COMPANION_MEDIA_EXCEEDS_EXPORT_BOUND = "companion_media_exceeds_export_bound"
COMPANION_EXPORT_COMPONENT_TIMEOUT = "companion_export_component_timeout"
COMPANION_EXPORT_COMPONENT_FAILED = "companion_export_component_failed"
COMPANION_MEDIA_MEMBER_COLLISION = "companion_media_member_collision"


class CompanionExportError(RuntimeError):
    """A named, terminal companion export failure: retrying the same job cannot fix it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ExportMemberCollisionError(RuntimeError):
    """Two archive members would share one name, so the archive would silently shadow one.

    Extracting a tar keeps only the last entry of a repeated name, so shipping such an archive
    loses a member without a word -- before an irreversible purge, the only copy of it. The job
    fails instead. This is not a `CompanionExportError`: the worker records it as a non-terminal
    failure and retries it, so a job that hit a naming regression completes on its own once the
    fix is deployed. Every name raised here is built by code from ids the platform assigns. A
    collision the tenant's own data can cause -- two companion media keys mapped to one member
    -- is caught when the bucket is listed and fails the job for good, by name
    (`COMPANION_MEDIA_MEMBER_COLLISION`), because no deploy changes a bucket's contents.
    """


# Builds the admin S3 client for one installation's media bucket. Injectable so tests use a stub.
MediaClientFactory = Callable[[str], S3ObjectStoreClient]


def default_media_client_factory(bucket: str) -> S3ObjectStoreClient:
    """The operator's admin client for one installation's R2 media bucket (CK-C).

    The admin key pair is the one the media step's teardown already empties buckets with; the
    installation's own Postiz credential is never used here (it is the tenant app's, not ours).
    """
    return create_s3_object_store_client(
        bucket=bucket,
        region=POSTIZ_R2_REGION,
        endpoint_url=settings.cloudflare_r2_s3_endpoint,
        access_key_id=settings.cloudflare_r2_admin_access_key_id,
        secret_access_key=settings.cloudflare_r2_admin_secret_access_key,
        force_path_style=True,
    )


def companion_upload_key(
    user_id: UUID, job_id: UUID, companion_key: str, installation_id: UUID
) -> str:
    """Where the runtime's uploaded dump is stored in the platform file store."""
    return f"exports/{user_id}/{job_id}/companions/{companion_key}-{installation_id}.sql.gz"


def companion_database_member(companion_key: str, installation_id: UUID | str) -> str:
    return f"companions/{companion_key}/{installation_id}/database.sql.gz"


def companion_media_member(companion_key: str, installation_id: UUID | str, key: str) -> str:
    return f"companions/{companion_key}/{installation_id}/media/{_safe_member_path(key)}"


_MEMBER_SAFE_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)


def _safe_member_path(key: str) -> str:
    """An object key as an archive path that cannot escape its directory.

    The companion holds a write credential for its bucket, so a key is tenant-influenced. A key of
    plain segments is kept as it is; anything else -- an empty, ``.`` or ``..`` segment, a
    backslash, a control character or a ``%`` -- is percent-encoded whole into ONE segment
    containing no ``.`` or ``/`` at all, so extracting the archive can never write outside
    ``media/``.

    The mapping is injective over non-empty keys. A non-plain key always holds a character that
    is encoded, so its member contains ``%``, and a plain key never does, since a literal ``%``
    makes a key non-plain. Without that rule the plain key ``a%5Cb`` and the encoded key ``a\\b``
    would both become ``a%5Cb`` and one object would shadow the other.
    """
    segments = key.split("/")
    plain = (
        "%" not in key
        and all(segment not in ("", ".", "..") for segment in segments)
        and not any(
            character == "\\" or ord(character) < 0x20 or ord(character) == 0x7F
            for character in key
        )
    )
    if key and plain:
        return key
    encoded = "".join(
        character
        if character in _MEMBER_SAFE_CHARACTERS
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in key
    )
    return encoded or "_"


def _utc_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def companion_components(manifest: Mapping[str, JsonValue] | None) -> list[dict[str, JsonValue]]:
    """A job's recorded companion components, each a fresh dict (never the ORM's own)."""
    raw = (manifest or {}).get(COMPANION_COMPONENTS_FIELD)
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def with_companion_components(
    manifest: Mapping[str, JsonValue] | None, components: Sequence[Mapping[str, JsonValue]]
) -> dict[str, JsonValue]:
    """A new manifest dict carrying ``components`` (a fresh dict, so the JSONB change is seen)."""
    return {
        **(manifest or {}),
        COMPANION_COMPONENTS_FIELD: [dict(component) for component in components],
    }


async def live_companion_installations(
    db: AsyncSession, user_id: UUID
) -> list[CompanionInstallation]:
    """The tenant's installations whose data an export must carry: not removed, and serving.

    Read ``FOR SHARE`` (CR-05): an uninstall locks the installation ``FOR UPDATE`` before it
    checks ``export_owes_installation``, so a concurrent export creation either waits for that
    uninstall and then no longer sees the installation as serving (PostgreSQL re-checks the
    predicate on the committed row), or took its share lock first and is seen by the uninstall
    once it commits -- never neither, which would promise data the uninstall is destroying.
    """
    return list(
        (
            await db.scalars(
                select(CompanionInstallation)
                .where(
                    CompanionInstallation.user_id == user_id,
                    CompanionInstallation.removed_at.is_(None),
                    serving_installation_filter(),
                )
                .order_by(CompanionInstallation.companion_key, CompanionInstallation.id)
                .with_for_update(read=True)
            )
        ).all()
    )


async def create_tenant_export_job(
    db: AsyncSession,
    *,
    user_id: UUID,
    include_vault_values: bool = False,
    now: datetime | None = None,
) -> TenantExportJob:
    """Queue one export job, recording a pending component per live companion installation.

    Flushed, not committed: the caller audits and commits in the same transaction.
    """
    moment = now or datetime.now(UTC)
    components: list[dict[str, JsonValue]] = [
        {
            "installation_id": str(installation.id),
            "companion_key": installation.companion_key,
            "state": COMPONENT_STATE_PENDING,
            "requested_at": _utc_iso(moment),
        }
        for installation in await live_companion_installations(db, user_id)
    ]
    job = TenantExportJob(
        user_id=user_id,
        include_vault_values=include_vault_values,
        manifest=with_companion_components(None, components) if components else None,
    )
    db.add(job)
    await db.flush()
    return job


async def pending_export_requests(
    db: AsyncSession, *, user_ids: Sequence[UUID], installation_ids: Sequence[UUID]
) -> dict[UUID, tuple[str, ...]]:
    """The export jobs each installation's runtime still owes a database dump, oldest first.

    Only jobs still in flight (queued or running) ask; at most ``COMPANION_MAX_EXPORT_REQUESTS``
    per installation, which is what the manifest contract carries.
    """
    if not installation_ids:
        return {}
    wanted = {str(installation_id) for installation_id in installation_ids}
    jobs = (
        await db.scalars(
            select(TenantExportJob)
            .where(
                TenantExportJob.user_id.in_(list(user_ids)),
                TenantExportJob.status.in_(
                    (TENANT_EXPORT_STATUS_QUEUED, TENANT_EXPORT_STATUS_RUNNING)
                ),
                TenantExportJob.manifest.is_not(None),
            )
            .order_by(TenantExportJob.created_at, TenantExportJob.id)
        )
    ).all()
    requests: dict[UUID, list[tuple[datetime, str]]] = {}
    for job in jobs:
        for component in companion_components(job.manifest):
            installation_id = component.get("installation_id")
            if component.get("state") != COMPONENT_STATE_PENDING or installation_id not in wanted:
                continue
            requested_at = _parse_time(component.get("requested_at")) or job.created_at
            requests.setdefault(UUID(str(installation_id)), []).append((requested_at, str(job.id)))
    return {
        installation_id: tuple(
            job_id for _, job_id in sorted(pairs)[:COMPANION_MAX_EXPORT_REQUESTS]
        )
        for installation_id, pairs in requests.items()
    }


# The component states an in-flight job still needs the installation's data for.
_OWED_COMPONENT_STATES = (COMPONENT_STATE_PENDING, COMPONENT_STATE_UPLOADED)


async def export_owes_installation(
    db: AsyncSession, *, user_id: UUID, installation_id: UUID
) -> bool:
    """Whether one of the tenant's in-flight exports still needs this installation's data (CR-05).

    True when a ``queued`` or ``running`` job of ``user_id`` carries a component for the
    installation that is ``pending`` or ``uploaded``. An ``uploaded`` component still counts: its
    database dump is stored, but its media is listed and read from the installation's own bucket
    only when the job assembles (``_resolve_companion_exports``), so the bucket must outlive the
    job, not just the upload. A ``failed`` component and an ended job owe nothing. Only the
    tenant's own jobs are read, so another tenant's export can never block (or reveal) anything.
    """
    wanted = str(installation_id)
    jobs = (
        await db.scalars(
            select(TenantExportJob).where(
                TenantExportJob.user_id == user_id,
                TenantExportJob.status.in_(
                    (TENANT_EXPORT_STATUS_QUEUED, TENANT_EXPORT_STATUS_RUNNING)
                ),
                TenantExportJob.manifest.is_not(None),
            )
        )
    ).all()
    return any(
        component.get("installation_id") == wanted
        and component.get("state") in _OWED_COMPONENT_STATES
        for job in jobs
        for component in companion_components(job.manifest)
    )


@dataclass(frozen=True, slots=True)
class CompanionExportUploadTarget:
    """What an authorized upload writes to: plain values, safe to hold across a rollback."""

    user_id: UUID
    installation_id: UUID
    companion_key: str
    job_id: UUID
    object_key: str


def _component_not_requested() -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "companion_export_not_requested",
            "message": "This export is not waiting for this installation's data.",
        },
    )


def _pending_component_index(
    job: TenantExportJob, installation_id: UUID
) -> tuple[list[dict[str, JsonValue]], int]:
    """The job's components and the index of this installation's PENDING one, or a 409."""
    if job.status not in (TENANT_EXPORT_STATUS_QUEUED, TENANT_EXPORT_STATUS_RUNNING):
        raise _component_not_requested()
    components = companion_components(job.manifest)
    for index, component in enumerate(components):
        if (
            component.get("installation_id") == str(installation_id)
            and component.get("state") == COMPONENT_STATE_PENDING
        ):
            return components, index
    raise _component_not_requested()


async def authorize_companion_export_upload(
    db: AsyncSession, *, auth: AuthContext, installation_id: UUID, job_id: UUID
) -> CompanionExportUploadTarget:
    """Admit one runtime upload, or refuse it before a byte of the body is read.

    The key must be bound to the installation's OWN agent, owned by the key's tenant and not
    archived, and the installation must not be removed (T-06-19-02): any other key, a removed
    installation and an unknown one are the same 404, as for the install report. The job must be
    the same tenant's (404 otherwise), still in flight, and waiting for exactly this
    installation's component (409 otherwise).
    """
    bound = auth.api_key.environment_id if auth.api_key is not None else None
    if bound is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    installation = (
        await db.execute(
            select(CompanionInstallation)
            .join(AgentEnvironment, AgentEnvironment.id == CompanionInstallation.environment_id)
            .where(
                CompanionInstallation.id == installation_id,
                CompanionInstallation.user_id == auth.user_id,
                CompanionInstallation.environment_id == bound,
                CompanionInstallation.removed_at.is_(None),
                AgentEnvironment.user_id == auth.user_id,
                AgentEnvironment.archived_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if installation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Installation not found")
    job = (
        await db.execute(
            select(TenantExportJob).where(
                TenantExportJob.id == job_id, TenantExportJob.user_id == auth.user_id
            )
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export job not found")
    _pending_component_index(job, installation_id)
    return CompanionExportUploadTarget(
        user_id=auth.user_id,
        installation_id=installation.id,
        companion_key=installation.companion_key,
        job_id=job.id,
        object_key=companion_upload_key(
            auth.user_id, job.id, installation.companion_key, installation.id
        ),
    )


async def record_companion_export_upload(
    db: AsyncSession,
    *,
    target: CompanionExportUploadTarget,
    size: int,
    now: datetime | None = None,
) -> None:
    """Mark the component uploaded, fenced on it STILL being pending (409 otherwise).

    Re-checked under the job's row lock after the body was stored: the job may have failed, been
    deferred past its deadline, or received the same upload from a concurrent request meanwhile.
    """
    job = (
        await db.execute(
            select(TenantExportJob)
            .where(
                TenantExportJob.id == target.job_id,
                TenantExportJob.user_id == target.user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export job not found")
    components, index = _pending_component_index(job, target.installation_id)
    components[index] = {
        **components[index],
        "state": COMPONENT_STATE_UPLOADED,
        "object_key": target.object_key,
        "bytes": size,
        "uploaded_at": _utc_iso(now or datetime.now(UTC)),
    }
    job.manifest = with_companion_components(job.manifest, components)
    await db.flush()


@dataclass(frozen=True, slots=True)
class CompanionComponentVerdict:
    """What the worker should do with a job's companion components right now."""

    # A named terminal failure, or None.
    failure: CompanionExportError | None
    # Components still owed and not yet past their deadline.
    pending: int


async def evaluate_companion_components(
    db: AsyncSession,
    *,
    user_id: UUID,
    components: Sequence[Mapping[str, JsonValue]],
    now: datetime,
) -> CompanionComponentVerdict:
    """Fail by name, defer, or proceed: the export never completes without what it promised."""
    pending = 0
    for component in components:
        installation_id = str(component.get("installation_id"))
        state = component.get("state")
        if state == COMPONENT_STATE_FAILED:
            return CompanionComponentVerdict(
                CompanionExportError(
                    COMPANION_EXPORT_COMPONENT_FAILED,
                    f"installation {installation_id} could not deliver its data "
                    f"({component.get('error') or 'failed'})",
                ),
                0,
            )
        if state == COMPONENT_STATE_UPLOADED:
            continue
        requested_at = _parse_time(component.get("requested_at")) or now
        if now - requested_at >= timedelta(seconds=COMPANION_COMPONENT_DEADLINE_SECONDS):
            return CompanionComponentVerdict(
                CompanionExportError(
                    COMPANION_EXPORT_COMPONENT_TIMEOUT,
                    f"installation {installation_id} did not deliver its database within "
                    f"{COMPANION_COMPONENT_DEADLINE_SECONDS // 60} minutes",
                ),
                0,
            )
        installation = await _component_installation(db, user_id=user_id, component=component)
        if installation is None or installation.removed_at is not None:
            return CompanionComponentVerdict(
                CompanionExportError(
                    COMPANION_EXPORT_COMPONENT_FAILED,
                    f"installation {installation_id} was removed before it delivered its data",
                ),
                0,
            )
        pending += 1
    return CompanionComponentVerdict(None, pending)


async def _component_installation(
    db: AsyncSession, *, user_id: UUID, component: Mapping[str, JsonValue]
) -> CompanionInstallation | None:
    try:
        installation_id = UUID(str(component.get("installation_id")))
    except ValueError:
        return None
    return (
        await db.scalars(
            select(CompanionInstallation).where(
                CompanionInstallation.id == installation_id,
                CompanionInstallation.user_id == user_id,
            )
        )
    ).one_or_none()


@dataclass(frozen=True, slots=True)
class _MediaObject:
    key: str
    size: int
    member: str


@dataclass(frozen=True, slots=True)
class _CompanionExport:
    """One uploaded component, resolved: its facts, its database object and its media listing."""

    installation: CompanionInstallation
    database_key: str
    database_member: str
    database_bytes: int
    bucket: str | None
    media: tuple[_MediaObject, ...]


async def _resolve_companion_exports(
    db: AsyncSession,
    *,
    user_id: UUID,
    components: Sequence[Mapping[str, JsonValue]],
    media_client_factory: MediaClientFactory,
) -> list[_CompanionExport]:
    """Each uploaded component with its media listed and bounded BEFORE anything is read."""
    exports: list[_CompanionExport] = []
    for component in components:
        if component.get("state") != COMPONENT_STATE_UPLOADED:
            continue
        installation = await _component_installation(db, user_id=user_id, component=component)
        if installation is None:
            raise CompanionExportError(
                COMPANION_EXPORT_COMPONENT_FAILED,
                f"installation {component.get('installation_id')} no longer exists",
            )
        database_key = component.get("object_key")
        if not isinstance(database_key, str) or not database_key:
            raise CompanionExportError(
                COMPANION_EXPORT_COMPONENT_FAILED,
                f"installation {installation.id} has no stored database dump",
            )
        size = component.get("bytes")
        bucket = installation_media_bucket(installation)
        media = await _list_media(installation, bucket, media_client_factory) if bucket else ()
        exports.append(
            _CompanionExport(
                installation=installation,
                database_key=database_key,
                database_member=companion_database_member(
                    installation.companion_key, installation.id
                ),
                database_bytes=size if isinstance(size, int) else 0,
                bucket=bucket,
                media=media,
            )
        )
    return exports


def installation_media_bucket(installation: CompanionInstallation) -> str | None:
    """The installation's own media bucket, as its ``media`` step recorded it, or None.

    Public because the export and the storage metering (plan 06-21) must read the SAME bucket:
    the bytes a tenant is billed for are the bytes a tenant can take away.
    """
    if installation.companion_key != POSTIZ_COMPANION_KEY:
        return None
    handles = postiz_step_handles(installation, POSTIZ_MEDIA_STEP)
    bucket = (handles or {}).get("bucket")
    return bucket if isinstance(bucket, str) and bucket else None


async def _list_media(
    installation: CompanionInstallation, bucket: str, factory: MediaClientFactory
) -> tuple[_MediaObject, ...]:
    """Every object in the bucket with its size, refused whole when over the export bound.

    The bound is checked against the LISTED sizes, before a single object is read, so an
    oversize bucket costs one listing rather than half a gigabyte of reads.
    """

    def _list() -> list[tuple[str, int]]:
        client = factory(bucket)
        try:
            return client.list_object_sizes("")
        finally:
            client.close()

    listed = await asyncio.to_thread(_list)
    total = sum(size for _, size in listed)
    if total > COMPANION_MEDIA_EXPORT_MAX_BYTES:
        raise CompanionExportError(
            COMPANION_MEDIA_EXCEEDS_EXPORT_BOUND,
            f"installation {installation.id} holds {total} bytes of media, over the "
            f"{COMPANION_MEDIA_EXPORT_MAX_BYTES} byte export bound",
        )
    media: list[_MediaObject] = []
    # The key that first produced each member. The bucket's contents decide these names, so a
    # repeat fails the job for good, by name, before any object is read: a retry would list the
    # same bucket and fail at the same member (not the retried `ExportMemberCollisionError`).
    key_by_member: dict[str, str] = {}
    for key, size in sorted(listed):
        member = companion_media_member(installation.companion_key, installation.id, key)
        first = key_by_member.get(member)
        if first is not None:
            raise CompanionExportError(
                COMPANION_MEDIA_MEMBER_COLLISION,
                f"installation {installation.id} media keys {first!r} and {key!r} would share "
                f"archive member {member!r}",
            )
        key_by_member[member] = key
        media.append(_MediaObject(key=key, size=size, member=member))
    return tuple(media)


def _companion_manifest_record(export: _CompanionExport) -> dict[str, Any]:
    """The installation's NON-SECRET facts: never a credential ref, a handle secret or a value."""
    installation = export.installation
    return {
        "installation_id": str(installation.id),
        "companion_key": installation.companion_key,
        "isolation_tier": installation.isolation_tier,
        "version": installation.version,
        "hostname": installation.hostname,
        "database_member": export.database_member,
        "database_bytes": export.database_bytes,
        "media_bytes": sum(item.size for item in export.media),
        "media": [
            {"key": item.key, "size": item.size, "member": item.member} for item in export.media
        ],
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _sid(value: object | None) -> str | None:
    return str(value) if value is not None else None


async def _serialize_users(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    rows = (await db.scalars(select(User).where(User.id == user_id))).all()
    return [
        {
            "id": str(row.id),
            "principal_kind": row.principal_kind,
            "clerk_id": row.clerk_id,
            "email": row.email,
            "name": row.name,
            "avatar_url": row.avatar_url,
            "invite_exempted_at": _iso(row.invite_exempted_at),
            "billing_unlocked_at": _iso(row.billing_unlocked_at),
            "billing_checked_at": _iso(row.billing_checked_at),
            "billing_lapsed_at": _iso(row.billing_lapsed_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_projects(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(select(Project).where(Project.user_id == user_id).order_by(Project.id))
    ).all()
    return [
        {
            "id": str(row.id),
            "name": row.name,
            "slug": row.slug,
            "kind": row.kind,
            "description": row.description,
            "origin_environment_id": _sid(row.origin_environment_id),
            "archived_at": _iso(row.archived_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_agents(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(
            select(AgentEnvironment)
            .where(AgentEnvironment.user_id == user_id)
            .order_by(AgentEnvironment.id)
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "machine_id": row.machine_id,
            "machine_name": row.machine_name,
            "agent_type": row.agent_type,
            "agent_version": row.agent_version,
            "os": row.os,
            "default_name": row.default_name,
            "display_name": row.display_name,
            "default_project_id": str(row.default_project_id),
            "archived_at": _iso(row.archived_at),
            "last_seen_at": _iso(row.last_seen_at),
            "last_sync_at": _iso(row.last_sync_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _serialize_sessions(
    rows: Sequence[Session],
    *,
    member_by_session: Mapping[UUID, str],
) -> list[dict[str, Any]]:
    # `rows` are the same rows the bodies were collected from, so every record describes a
    # session whose body this archive already accounts for.
    # Token counters are exported under the usage_events naming
    # (in_tok/out_tok/cache_read): the model's `*_tokens` column names
    # would trip the secretish-key predicate ("token"), and the export
    # contract checks every record key against it (D-10).
    return [
        {
            "id": str(row.id),
            "environment_id": _sid(row.environment_id),
            "local_session_id": row.local_session_id,
            "project_path": row.project_path,
            "started_at": _iso(row.started_at),
            "ended_at": _iso(row.ended_at),
            "last_activity_at": _iso(row.last_activity_at),
            "duration_seconds": row.duration_seconds,
            "message_count": row.message_count,
            "in_tok": row.input_tokens,
            "out_tok": row.output_tokens,
            "cache_read": row.cache_read_tokens,
            "tool_call_count": row.tool_call_count,
            "model": row.model,
            "models_used": row.models_used,
            "summary": row.summary,
            "tags": row.tags,
            "status": row.status,
            "file_key": row.file_key,
            "content_hash": row.content_hash,
            # Which body form pairs with `content_hash`, and the archive member carrying it
            # (None when this archive has no body for the session).
            "content_protocol": row.content_protocol,
            "body_member": member_by_session.get(row.id),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


def _serialize_skills(
    rows: Sequence[Skill],
    *,
    member_by_skill: Mapping[UUID, str],
) -> list[dict[str, Any]]:
    # `rows` are the same rows the archives were collected from, so every record describes a
    # skill whose archive this export already accounts for.
    return [
        {
            "id": str(row.id),
            "project_id": str(row.project_id),
            "skill_key": row.skill_key,
            "name": row.name,
            "description": row.description,
            "version": row.version,
            "source": row.source,
            "authority": row.authority,
            "authority_agent_id": _sid(row.authority_agent_id),
            "agent_types": row.agent_types,
            "content_hash": row.content_hash,
            "file_key": row.file_key,
            "source_repo": row.source_repo,
            "file_count": row.file_count,
            # The archive member carrying this skill's bytes (None when this archive has none).
            "archive_member": member_by_skill.get(row.id),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_memories(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await db.scalars(select(Memory).where(Memory.user_id == user_id).order_by(Memory.id))
    ).all()
    # Embedding vectors are deliberately excluded — the manifest notes
    # "embeddings: derived, not exported".
    return [
        {
            "id": str(row.id),
            "content": row.content,
            "category": row.category,
            "source": row.source,
            "source_session_id": _sid(row.source_session_id),
            "source_environment_id": _sid(row.source_environment_id),
            "tags": row.tags,
            "metadata": row.metadata_,
            "access_count": row.access_count,
            "last_accessed_at": _iso(row.last_accessed_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_vaults(
    db: AsyncSession,
    user_id: UUID,
    *,
    include_vault_values: bool,
) -> list[dict[str, Any]]:
    vaults = (
        await db.scalars(select(Vault).where(Vault.user_id == user_id).order_by(Vault.id))
    ).all()
    records: list[dict[str, Any]] = []
    for vault in vaults:
        items = (
            await db.scalars(
                select(VaultItem).where(VaultItem.vault_id == vault.id).order_by(VaultItem.id)
            )
        ).all()
        item_records: list[dict[str, Any]] = []
        for item in items:
            item_record: dict[str, Any] = {
                "item_name": item.item_name,
                "section": item.section,
            }
            if include_vault_values:
                # Opt-in only (D-10): decryption goes through the vault
                # crypto service, never raw column reads leaking out.
                item_record["value"] = vault_decrypt(item.encrypted_value, item.nonce)
            item_records.append(item_record)
        requests = (
            await db.scalars(
                select(VaultSecretRequest)
                .where(VaultSecretRequest.vault_id == vault.id)
                .order_by(VaultSecretRequest.created_at, VaultSecretRequest.id)
            )
        ).all()
        # Field names and lifecycle only. `token_hash` and `field_baselines` (digests over the
        # stored ciphertext) are derived from secrets and stay out even on the vault opt-in: the
        # opt-in exports the values themselves, never request-local snapshots of them.
        request_records = [
            {
                "id": str(request.id),
                "project_id": str(request.project_id),
                "section": request.section,
                "fields": request.fields,
                "extra_fields": request.extra_fields,
                "expires_at": _iso(request.expires_at),
                "supplied_at": _iso(request.supplied_at),
                "conflicted_at": _iso(request.conflicted_at),
                "created_at": _iso(request.created_at),
            }
            for request in requests
        ]
        records.append(
            {
                "id": str(vault.id),
                "slug": vault.slug,
                "name": vault.name,
                "created_at": _iso(vault.created_at),
                "items": item_records,
                "field_requests": request_records,
            }
        )
    return records


async def _serialize_channels(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Channel config only — credentials/secrets are excluded by default
    (see EXPORT_COVERAGE for per-table dispositions)."""
    records: list[dict[str, Any]] = []
    accounts = (
        await db.scalars(
            select(ChannelAccount)
            .where(ChannelAccount.user_id == user_id)
            .order_by(ChannelAccount.id)
        )
    ).all()
    for row in accounts:
        records.append(
            {
                "table": "channel_accounts",
                "id": str(row.id),
                "provider": row.provider,
                "name": row.name,
                "status": row.status,
                "visibility": row.visibility,
                "config": row.config,
                "archived_at": _iso(row.archived_at),
                "created_at": _iso(row.created_at),
            }
        )
    links = (
        await db.scalars(
            select(ChannelBotAgentLink)
            .where(ChannelBotAgentLink.user_id == user_id)
            .order_by(ChannelBotAgentLink.id)
        )
    ).all()
    for link in links:
        records.append(
            {
                "table": "channel_bot_agent_links",
                "id": str(link.id),
                "account_id": str(link.account_id),
                "agent_id": str(link.agent_id),
                "status": link.status,
                "config": link.config,
                "archived_at": _iso(link.archived_at),
                "created_at": _iso(link.created_at),
            }
        )
    bindings = (
        await db.scalars(
            select(ChannelBinding)
            .where(ChannelBinding.user_id == user_id)
            .order_by(ChannelBinding.id)
        )
    ).all()
    for binding in bindings:
        records.append(
            {
                "table": "channel_bindings",
                "id": str(binding.id),
                "account_id": str(binding.account_id),
                "bot_agent_link_id": str(binding.bot_agent_link_id),
                "external_chat_id": binding.external_chat_id,
                "external_chat_type": binding.external_chat_type,
                "external_chat_name": binding.external_chat_name,
                "paired_external_user_id": binding.paired_external_user_id,
                "status": binding.status,
                "created_at": _iso(binding.created_at),
            }
        )
    aliases = (
        await db.scalars(
            select(ChannelBindingAlias)
            .where(ChannelBindingAlias.user_id == user_id)
            .order_by(ChannelBindingAlias.id)
        )
    ).all()
    for alias in aliases:
        records.append(
            {
                "table": "channel_binding_aliases",
                "id": str(alias.id),
                "account_id": str(alias.account_id),
                "bot_agent_link_id": str(alias.bot_agent_link_id),
                "binding_id": str(alias.binding_id),
                "alias_external_chat_id": alias.alias_external_chat_id,
                "alias_kind": alias.alias_kind,
                "created_at": _iso(alias.created_at),
            }
        )
    pair_codes = (
        await db.scalars(
            select(ChannelPairCode)
            .where(ChannelPairCode.user_id == user_id)
            .order_by(ChannelPairCode.id)
        )
    ).all()
    # code_hash is deliberately excluded — pairing metadata only.
    for code in pair_codes:
        records.append(
            {
                "table": "channel_pair_codes",
                "id": str(code.id),
                "account_id": str(code.account_id),
                "bot_agent_link_id": str(code.bot_agent_link_id),
                "status": code.status,
                "expires_at": _iso(code.expires_at),
                "claimed_at": _iso(code.claimed_at),
                "claimed_external_chat_id": code.claimed_external_chat_id,
                "claimed_external_user_id": code.claimed_external_user_id,
                "created_at": _iso(code.created_at),
            }
        )
    return records


async def _serialize_usage_events(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The tenant's whole cost ledger: its sessions' llm rows AND its companions' compute and
    storage rows (plan 06-21). A compute or storage row has no session, so it is selected by the
    tenant column it carries instead of through ``sessions``."""
    session_ids = select(Session.id).where(Session.user_id == user_id)
    rows = (
        await db.scalars(
            select(UsageEvent)
            .where(
                or_(
                    UsageEvent.session_id.in_(session_ids),
                    and_(UsageEvent.session_id.is_(None), UsageEvent.user_id == user_id),
                )
            )
            .order_by(UsageEvent.id)
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "dimension": row.dimension,
            "session_id": _sid(row.session_id),
            "ts": _iso(row.ts),
            "provider": row.provider,
            "model": row.model,
            "in_tok": row.in_tok,
            "out_tok": row.out_tok,
            "cache_read": row.cache_read,
            "cache_create": row.cache_create,
            "cost_usd_micros": row.cost_usd_micros,
            "error_kind": row.error_kind,
            "environment_id": _sid(row.environment_id),
            "installation_id": _sid(row.installation_id),
            "companion_key": row.companion_key,
            "period_start": _iso(row.period_start),
            "quantity": row.quantity,
        }
        for row in rows
    ]


async def _serialize_api_keys(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Metadata only — no hashes, no key material."""
    rows = (
        await db.scalars(select(ApiKey).where(ApiKey.user_id == user_id).order_by(ApiKey.id))
    ).all()
    return [
        {
            "id": str(row.id),
            "key_prefix": row.key_prefix,
            "label": row.label,
            "managed": row.managed,
            "scopes": row.scopes,
            "environment_id": _sid(row.environment_id),
            "last_used_at": _iso(row.last_used_at),
            "expires_at": _iso(row.expires_at),
            "revoked_at": _iso(row.revoked_at),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_audit(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Metadata only — `details` stays out (already sanitized at write
    time, but its keys are caller-defined and the export allowlists
    column names only)."""
    rows = (
        await db.scalars(
            select(ControlPlaneAuditEvent)
            .where(
                or_(
                    ControlPlaneAuditEvent.target_user_id == user_id,
                    ControlPlaneAuditEvent.actor_user_id == user_id,
                )
            )
            .order_by(ControlPlaneAuditEvent.created_at, ControlPlaneAuditEvent.id)
        )
    ).all()
    return [
        {
            "id": str(row.id),
            "actor_type": row.actor_type,
            "source": row.source,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "environment_id": _sid(row.environment_id),
            "created_at": _iso(row.created_at),
        }
        for row in rows
    ]


async def _serialize_hosted_runtimes(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """Deployment/observation metadata only — never secret values, never
    the manifest JSONB blobs. Enumerated via the tenant's
    AgentEnvironment linkage (these tables carry no user column)."""
    env_ids = (
        await db.scalars(select(AgentEnvironment.id).where(AgentEnvironment.user_id == user_id))
    ).all()
    if not env_ids:
        return []
    records: list[dict[str, Any]] = []
    states = (
        await db.scalars(
            select(HostedRuntimeState)
            .where(HostedRuntimeState.environment_id.in_(env_ids))
            .order_by(HostedRuntimeState.environment_id)
        )
    ).all()
    for state in states:
        records.append(
            {
                "table": "hosted_runtime_states",
                "environment_id": str(state.environment_id),
                "deployment_id": state.deployment_id,
                "instance_id": state.instance_id,
                "generation": state.generation,
                "apply_generation": state.apply_generation,
                "cli_package_spec": state.cli_package_spec,
                "created_at": _iso(state.created_at),
            }
        )
    observations = (
        await db.scalars(
            select(HostedRuntimeConfigObservation)
            .where(HostedRuntimeConfigObservation.environment_id.in_(env_ids))
            .order_by(HostedRuntimeConfigObservation.environment_id)
        )
    ).all()
    for observation in observations:
        records.append(
            {
                "table": "hosted_runtime_config_observations",
                "environment_id": str(observation.environment_id),
                "observed_at": _iso(observation.observed_at),
                "observed_config_generation": observation.observed_config_generation,
                "observed_manifest_etag": observation.observed_manifest_etag,
                "observed_source_revision": observation.observed_source_revision,
                "created_at": _iso(observation.created_at),
            }
        )
    return records


async def _serialize_agent_extensions(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The catalog plugins and project skills each of the tenant's agents uses (06.1). Neither
    table carries a tenant column, so both are enumerated through the tenant's agents. The
    plugin's `source` descriptor stays out: it is the catalog's, named by revision and version."""
    agent_ids = select(AgentEnvironment.id).where(AgentEnvironment.user_id == user_id)
    records: list[dict[str, Any]] = []
    installations = (
        await db.scalars(
            select(AgentPluginInstallation)
            .where(AgentPluginInstallation.environment_id.in_(agent_ids))
            .order_by(AgentPluginInstallation.id)
        )
    ).all()
    for installation in installations:
        records.append(
            {
                "table": "agent_plugin_installations",
                "id": str(installation.id),
                "environment_id": str(installation.environment_id),
                "plugin_name": installation.plugin_name,
                "catalog_revision": installation.catalog_revision,
                "version": installation.version,
                "agent_plugins_schema": installation.agent_plugins_schema,
                "content_digest": installation.content_digest,
                "created_at": _iso(installation.created_at),
                "updated_at": _iso(installation.updated_at),
            }
        )
    references = (
        await db.scalars(
            select(AgentSkillReference)
            .where(AgentSkillReference.agent_id.in_(agent_ids))
            .order_by(AgentSkillReference.agent_id, AgentSkillReference.skill_id)
        )
    ).all()
    for reference in references:
        records.append(
            {
                "table": "agent_skill_references",
                "agent_id": str(reference.agent_id),
                "skill_id": str(reference.skill_id),
                "created_at": _iso(reference.created_at),
            }
        )
    return records


async def _serialize_companion_activity(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The companion activity log and the approvals it references (phase 05). The `arguments`
    and `dry_run_diff` JSONB stay out, as `details` does in the audit domain: sanitized at write
    time, but their keys are caller-defined and the export allowlists column names only. The
    argument digest identifies the call instead."""
    records: list[dict[str, Any]] = []
    calls = (
        await db.scalars(
            select(CompanionToolCall)
            .where(CompanionToolCall.user_id == user_id)
            .order_by(CompanionToolCall.created_at, CompanionToolCall.id)
        )
    ).all()
    for call in calls:
        records.append(
            {
                "table": "companion_tool_calls",
                "id": str(call.id),
                "environment_id": _sid(call.environment_id),
                "session_id": call.session_id,
                "companion_key": call.companion_key,
                "verb": call.verb,
                "tool_class": call.tool_class,
                "arguments_digest": call.arguments_digest,
                "decision": call.decision,
                "cap_unit": call.cap_unit,
                "cap_consumed": call.cap_consumed,
                "cap_remaining_after": call.cap_remaining_after,
                "dry_run": call.dry_run,
                "source_content_ref": call.source_content_ref,
                "call_jti": call.call_jti,
                "approval_id": _sid(call.approval_id),
                "approved_by_user_id": _sid(call.approved_by_user_id),
                "parent_call_id": _sid(call.parent_call_id),
                "error_text": call.error_text,
                "repeat_count": call.repeat_count,
                "created_at": _iso(call.created_at),
            }
        )
    approvals = (
        await db.scalars(
            select(CompanionApproval)
            .where(CompanionApproval.user_id == user_id)
            .order_by(CompanionApproval.created_at, CompanionApproval.id)
        )
    ).all()
    for approval in approvals:
        records.append(
            {
                "table": "companion_approvals",
                "id": str(approval.id),
                "environment_id": _sid(approval.environment_id),
                "companion_key": approval.companion_key,
                "verb": approval.verb,
                "canonical_action_sha256": approval.canonical_action_sha256,
                "resolved_target_count": approval.resolved_target_count,
                "remaining_budget": approval.remaining_budget,
                "status": approval.status,
                "expires_at": _iso(approval.expires_at),
                "decided_at": _iso(approval.decided_at),
                "decided_by_user_id": _sid(approval.decided_by_user_id),
                "created_at": _iso(approval.created_at),
            }
        )
    return records


async def _serialize_companion_settings(db: AsyncSession, user_id: UUID) -> list[dict[str, Any]]:
    """The tenant's own companion controls: per-verb approval modes (revoked ones included, as
    the history of loosening and tightening), destroy switches, group blocks, cap vectors and the
    monthly spend ceiling."""
    records: list[dict[str, Any]] = []
    policies = (
        await db.scalars(
            select(CompanionToolPolicy)
            .where(CompanionToolPolicy.user_id == user_id)
            .order_by(CompanionToolPolicy.created_at, CompanionToolPolicy.id)
        )
    ).all()
    for policy in policies:
        records.append(
            {
                "table": "companion_tool_policies",
                "id": str(policy.id),
                "environment_id": _sid(policy.environment_id),
                "companion_key": policy.companion_key,
                "verb": policy.verb,
                "mode": policy.mode,
                "revoked_at": _iso(policy.revoked_at),
                "created_at": _iso(policy.created_at),
            }
        )
    enablements = (
        await db.scalars(
            select(CompanionDestroyEnablement)
            .where(CompanionDestroyEnablement.user_id == user_id)
            .order_by(CompanionDestroyEnablement.id)
        )
    ).all()
    for enablement in enablements:
        records.append(
            {
                "table": "companion_destroy_enablements",
                "id": str(enablement.id),
                "companion_key": enablement.companion_key,
                "enabled_at": _iso(enablement.enabled_at),
                "enabled_by_user_id": _sid(enablement.enabled_by_user_id),
            }
        )
    blocks = (
        await db.scalars(
            select(CompanionGroupBlock)
            .where(CompanionGroupBlock.user_id == user_id)
            .order_by(CompanionGroupBlock.id)
        )
    ).all()
    for block in blocks:
        records.append(
            {
                "table": "companion_group_blocks",
                "id": str(block.id),
                "companion_key": block.companion_key,
                "blocked_at": _iso(block.blocked_at),
                "blocked_by_user_id": _sid(block.blocked_by_user_id),
            }
        )
    caps = (
        await db.scalars(
            select(CompanionCapPolicy)
            .where(CompanionCapPolicy.user_id == user_id)
            .order_by(CompanionCapPolicy.id)
        )
    ).all()
    for cap in caps:
        records.append(
            {
                "table": "companion_cap_policies",
                "id": str(cap.id),
                "companion_key": cap.companion_key,
                "per_call_calls": cap.per_call_calls,
                "per_call_records": cap.per_call_records,
                "per_call_bytes": cap.per_call_bytes,
                "per_session_calls": cap.per_session_calls,
                "per_session_records": cap.per_session_records,
                "per_session_bytes": cap.per_session_bytes,
                "created_at": _iso(cap.created_at),
                "updated_at": _iso(cap.updated_at),
            }
        )
    ceiling = await db.get(CompanionSpendCeiling, user_id)
    if ceiling is not None:
        records.append(
            {
                "table": "companion_spend_ceilings",
                "ceiling_usd_micros": ceiling.ceiling_usd_micros,
                "updated_by_user_id": _sid(ceiling.updated_by_user_id),
                "created_at": _iso(ceiling.created_at),
                "updated_at": _iso(ceiling.updated_at),
            }
        )
    return records


def _session_body_member(session: Session) -> str | None:
    """Archive member for a session's body, with `load_session_content_projection`'s precedence.

    events-v1 with a committed generation wins; `file_key` is the snapshot-v1 fallback. The
    member is keyed on the immutable session id (the primary key), never on the local session
    id: since upstream #1229 the local id is unique only per origin environment, so two agents
    of one tenant can hold sessions with the same local id and different bodies.
    """
    if session.content_protocol == "events-v1" and session.event_generation_id is not None:
        return f"sessions/{session.id}.events.ndjson"
    if session.file_key:
        return f"sessions/{session.id}.json"
    return None


@dataclass(frozen=True)
class _SessionBodies:
    """Every session body accounted for: exported as a member, or omitted with a reason."""

    members: list[tuple[str, bytes]]
    member_by_session: dict[UUID, str]
    omitted: list[dict[str, str]]


def _omitted_body(session: Session, reason: str) -> dict[str, str]:
    return {
        "session_id": str(session.id),
        "local_session_id": session.local_session_id,
        "content_protocol": session.content_protocol,
        "reason": reason,
    }


async def _collect_session_bodies(
    db: AsyncSession,
    file_store: FileStore,
    sessions: Sequence[Session],
) -> _SessionBodies:
    # Runs no session query of its own: `sessions` is the export's one load of the tenant's
    # rows. `db` is kept for the events-v1 loader's chunk reads.
    bodies = _SessionBodies(members=[], member_by_session={}, omitted=[])
    # Every member name is computed before any body is read. A repeat fails the export here,
    # even when one of the two bodies would later be omitted (and so never reach the tar writer's
    # own guard). `session_by_member` keeps the first session that computed each name.
    session_by_member: dict[str, UUID] = {}
    planned: list[tuple[Session, str]] = []
    for session in sessions:
        member = _session_body_member(session)
        if member is None:
            continue
        first = session_by_member.get(member)
        if first is not None:
            raise ExportMemberCollisionError(
                f"archive member {member!r} would carry the bodies of sessions {first} and "
                f"{session.id}"
            )
        session_by_member[member] = session.id
        planned.append((session, member))
    for session, member in planned:
        if member.endswith(".events.ndjson"):
            # Content gone or damaged for good is recorded, since a retry would never recover it.
            # `SessionContentUnavailable` (a storage outage) and anything else propagate and fail
            # the job, so the worker retries it.
            try:
                data = await load_session_events_ndjson(session, file_store, db)
            except SessionContentMissing:
                bodies.omitted.append(_omitted_body(session, "missing"))
                continue
            except SessionContentInvalid:  # includes SessionContentChanged
                bodies.omitted.append(_omitted_body(session, "invalid"))
                continue
        else:
            file_key = session.file_key
            if file_key is None or not await file_store.exists(file_key):
                bodies.omitted.append(_omitted_body(session, "missing"))
                continue
            data = await file_store.get(file_key)
        bodies.members.append((member, data))
        bodies.member_by_session[session.id] = member
    return bodies


def _skill_archive_member(skill: Skill) -> str | None:
    """Archive member for a skill's stored bytes, or None for a skill that has none."""
    if not skill.file_key:
        return None
    return f"skills/{skill.project_id}/{skill.skill_key}/{skill.content_hash}.tar.gz"


@dataclass(frozen=True)
class _SkillArchives:
    """Every stored skill archive accounted for: exported as a member, or omitted with a reason."""

    members: list[tuple[str, bytes]]
    member_by_skill: dict[UUID, str]
    omitted: list[dict[str, str]]


async def _collect_skill_archives(
    file_store: FileStore,
    skills: Sequence[Skill],
) -> _SkillArchives:
    # Runs no skill query of its own: `skills` is the export's one load of the tenant's active
    # skill rows, so each archive's name and bytes come from the row its record describes.
    archives = _SkillArchives(members=[], member_by_skill={}, omitted=[])
    for skill in skills:
        member = _skill_archive_member(skill)
        file_key = skill.file_key
        if member is None or file_key is None:
            continue
        if not await file_store.exists(file_key):
            # The bytes are gone for good, so a retry would never recover them: recorded, not
            # dropped, so the archive that precedes a purge says what it lacks.
            archives.omitted.append(
                {
                    "skill_id": str(skill.id),
                    "project_id": str(skill.project_id),
                    "skill_key": skill.skill_key,
                    "content_hash": skill.content_hash,
                    "reason": "missing",
                }
            )
            continue
        archives.members.append((member, await file_store.get(file_key)))
        archives.member_by_skill[skill.id] = member
    return archives


async def assemble_tenant_export(
    db: AsyncSession,
    file_store: FileStore,
    user_id: UUID,
    include_vault_values: bool = False,
    *,
    companion_components: Sequence[Mapping[str, JsonValue]] = (),
    media_client_factory: MediaClientFactory | None = None,
) -> tuple[bytes, dict[str, Any]]:
    """Assemble one tenant's export artifact (D-11).

    Read-only over tenant data: no tenant row or object is mutated.
    Returns ``(tar_gz_bytes, manifest_dict)``.

    ``companion_components`` are the job's recorded components (plan 06-19). Each UPLOADED one
    adds its database dump and every object of the installation's media bucket under
    ``companions/``, and a ``companions`` manifest domain of the installation's non-secret facts
    and media keys with sizes; with none, the archive is exactly the Phase 3 one. Media over
    ``COMPANION_MEDIA_EXPORT_MAX_BYTES`` raises ``CompanionExportError`` before anything is read.
    """
    created_at = datetime.now(UTC)
    # One load of the tenant's session rows feeds both the bodies and the records, so a session
    # created while the export runs has neither and belongs to the next export. Bodies first:
    # each session record names the member that carries its body. Skill rows follow the same
    # rule, and are loaded before any body is read so they describe the same moment.
    session_rows = (
        await db.scalars(select(Session).where(Session.user_id == user_id).order_by(Session.id))
    ).all()
    skill_rows = (
        await db.scalars(
            select(Skill)
            .where(Skill.user_id == user_id, Skill.is_active.is_(True))
            .order_by(Skill.id)
        )
    ).all()
    session_bodies = await _collect_session_bodies(db, file_store, session_rows)
    skill_archives = await _collect_skill_archives(file_store, skill_rows)
    domains: dict[str, list[dict[str, Any]]] = {
        "users": await _serialize_users(db, user_id),
        "projects": await _serialize_projects(db, user_id),
        "agents": await _serialize_agents(db, user_id),
        "sessions": _serialize_sessions(
            session_rows, member_by_session=session_bodies.member_by_session
        ),
        "skills": _serialize_skills(skill_rows, member_by_skill=skill_archives.member_by_skill),
        "memories": await _serialize_memories(db, user_id),
        "vaults": await _serialize_vaults(db, user_id, include_vault_values=include_vault_values),
        "channels": await _serialize_channels(db, user_id),
        "usage_events": await _serialize_usage_events(db, user_id),
        "api_keys": await _serialize_api_keys(db, user_id),
        "audit": await _serialize_audit(db, user_id),
        "hosted_runtimes": await _serialize_hosted_runtimes(db, user_id),
        "agent_extensions": await _serialize_agent_extensions(db, user_id),
        "companion_activity": await _serialize_companion_activity(db, user_id),
        "companion_settings": await _serialize_companion_settings(db, user_id),
    }

    manifest: dict[str, Any] = {
        "export_version": EXPORT_VERSION,
        "user_id": str(user_id),
        "created_at": created_at.isoformat(),
        "vault_values_included": include_vault_values,
        "notes": {"embeddings": "derived, not exported"},
        "domains": [{"name": name, "rows": len(domains[name])} for name in EXPORT_DOMAINS],
    }
    if session_bodies.omitted:
        manifest[SESSION_BODIES_OMITTED_FIELD] = session_bodies.omitted
    if skill_archives.omitted:
        manifest[SKILL_ARCHIVES_OMITTED_FIELD] = skill_archives.omitted

    media_factory = media_client_factory or default_media_client_factory
    companions = await _resolve_companion_exports(
        db,
        user_id=user_id,
        components=companion_components,
        media_client_factory=media_factory,
    )
    if companions:
        manifest["domains"].append({"name": COMPANIONS_EXPORT_DOMAIN, "rows": len(companions)})
        manifest[COMPANIONS_EXPORT_DOMAIN] = [
            _companion_manifest_record(export) for export in companions
        ]

    binary_members = [*session_bodies.members, *skill_archives.members]

    buffer = io.BytesIO()
    mtime = int(created_at.timestamp())

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        written: set[str] = set()

        def _add(name: str, data: bytes) -> None:
            # Extraction keeps only the last of a repeated name, so a repeat would silently
            # shadow a member: the manifest, a domain, a session body, a skill or a companion.
            if name in written:
                raise ExportMemberCollisionError(f"archive member {name!r} would be written twice")
            written.add(name)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(data))

        _add("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))
        for name in EXPORT_DOMAINS:
            records = domains[name]
            if not records:
                continue
            payload = "\n".join(json.dumps(record) for record in records) + "\n"
            _add(f"{name}.jsonl", payload.encode("utf-8"))
        for member_name, data in binary_members:
            _add(member_name, data)
        for export in companions:
            _add(export.database_member, await file_store.get(export.database_key))
            if not export.media or export.bucket is None:
                continue
            # One client per bucket; each object is written into the archive before the next is
            # read, so no second copy of the bucket is built beside the archive buffer.
            client = await asyncio.to_thread(media_factory, export.bucket)
            try:
                for item in export.media:
                    _add(item.member, await asyncio.to_thread(client.get, item.key))
            finally:
                await asyncio.to_thread(client.close)

    return buffer.getvalue(), manifest
