"""Admin endpoints — gated by `X-Admin-Key` shared secret.

Used by upstream-SaaS batch tooling and ops-side scripts that don't
have a per-user Clerk JWT available (e.g. catching up legacy
deployments that pre-date live sync, account-deletion webhooks, fleet
revocation). Disabled by default — `settings.admin_api_key` must be
set to a strong secret to enable.

**Trust model:** admin-minted keys carry the same authority as keys
the user mints for themselves via `POST /v1/auth/keys` — full
account access by default. The X-Admin-Key is therefore a root
credential: a leak grants an attacker the ability to mint full-power
keys for any user. Protect it like a database password (rotate on
suspicion, restrict to SaaS backend egress IPs at the infra layer,
audit log access).

The product reasoning: a user's hosted pod is the user's agent
running on our infrastructure — it must be able to do everything
the user can do on their own laptop. Capping admin-minted keys
below user-mint power would make hosted strictly weaker than
self-managed (vault reads, memory reads, etc. would silently fail).

Surface kept minimal: just the operations that batch tooling
genuinely can't accomplish via per-user Clerk JWTs. Future admin
endpoints (list users, view any deployment, audit-log query, etc.)
can land in this file under the same auth dep.
"""

import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Never, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from pydantic import JsonValue, SecretStr, ValidationError
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.core.auth import require_admin_api_key
from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.models.ai_provider import AiProvider, AiProviderAuthPayload
from app.models.api_key import ApiKey
from app.models.app_setting import AppSetting
from app.models.billing_reconcile_finding import BillingReconcileFinding
from app.models.channel import (
    BOT_AGENT_LINK_STATUS_ACTIVE,
    CHANNEL_PROVIDER_DISCORD,
    CHANNEL_PROVIDER_TELEGRAM,
    CHANNEL_PROVIDER_WHATSAPP,
    CHANNEL_PROVIDERS,
    CHANNEL_VISIBILITY_PUBLIC,
    ChannelAccount,
    ChannelBotAgentLink,
)
from app.models.companion_installation import CompanionInstallation
from app.models.hosted_runtime import HostedRuntimeSecret, HostedRuntimeState
from app.models.principal_lifecycle import ClerkPrincipalSuspension, PrincipalLifecycle
from app.models.session import AgentEnvironment
from app.models.tenant_export import (
    TENANT_EXPORT_STATUS_COMPLETE,
    TENANT_EXPORT_STATUS_QUEUED,
    TenantExportJob,
)
from app.models.tenant_purge import (
    TENANT_PURGE_STATUS_COMPLETE,
    TENANT_PURGE_STATUS_FAILED,
    TENANT_PURGE_STATUS_PENDING,
    TENANT_PURGE_STATUS_PURGING,
    TenantPurge,
)
from app.models.user import (
    PRINCIPAL_KIND_CLERK,
    PRINCIPAL_KIND_PARTNER_TENANT,
    User,
)
from app.schemas.admin import (
    AdminAgentCreate,
    AdminAiProviderArchiveReceipt,
    AdminAiProviderArchiveRequest,
    AdminAiProviderRemovalAuthorityResponse,
    AdminApiKeyCreate,
    AdminAppSettingListResponse,
    AdminAppSettingResponse,
    AdminAppSettingUpsert,
    AdminChannelCreate,
    AdminChannelCreatedResponse,
    AdminChannelResponse,
    AdminChannelUpdate,
    AdminChannelVisibility,
    AdminChannelWebhookSecretResponse,
    AdminCompanionAbandonedBackup,
    AdminCompanionRehearsalBundle,
    AdminCompanionRehearsalRecorded,
    AdminCompanionRehearsalResult,
    AdminCompanionReprovision,
    AdminDeploymentManagedAiProviderCleanup,
    AdminDeploymentManagedAiProviderCleanupReceipt,
    AdminDeploymentManagedAiProviderResponse,
    AdminDeploymentManagedAiProviderRuntimeMetadataReplace,
    AdminDeploymentManagedAiProviderUpsert,
    AdminEnvironmentCreate,
    AdminManagedAiProviderResponse,
    AdminManagedAiProviderUpsert,
    AdminPlatformWhatsAppPairingSessionCreate,
    AdminPrincipalSuspensionResponse,
    AdminPrincipalSuspensionUpdate,
    AdminRuntimeSecretHealthItem,
    AdminRuntimeSecretHealthResponse,
    AdminRuntimeSecretRecoverRequest,
    AdminRuntimeSecretRecoverResponse,
    AdminRuntimeSecretRotateResponse,
    AdminRuntimeStateResponse,
    AdminRuntimeStateUpsert,
    AdminWorkloadClientBootstrap,
    AdminWorkloadSignerBootstrap,
    AdminWorkloadSignerReceipt,
)
from app.schemas.admin_billing_reconcile import (
    AdminBillingReconcileFinding,
    AdminBillingReconcileFindingList,
    AdminBillingReconcileRunResponse,
    BillingReconcileFindingKind,
)
from app.schemas.admin_export import (
    AdminTenantExportCreate,
    AdminTenantExportCreatedResponse,
    AdminTenantExportDownloadResponse,
    AdminTenantExportListItem,
    AdminTenantExportListResponse,
    AdminUserListItem,
    AdminUserListResponse,
    TenantExportStatus,
)
from app.schemas.admin_tenant_lifecycle import (
    AdminBillingExemptionResponse,
    AdminBillingExemptionUpdate,
    AdminPurgeCancelResponse,
    AdminTenantDeleteResponse,
    AdminTenantLifecycleResponse,
    AdminTenantPurgeInfo,
    TenantPurgeStatus,
)
from app.schemas.ai_provider import (
    AiProviderDeleteResponse,
    ai_provider_auth_from_persistence,
)
from app.schemas.api_key import ApiKeyCreated, ApiKeyRevokeResponse
from app.schemas.channel import (
    ChannelCommandSyncRequest,
    ChannelCommandSyncResponse,
    ChannelWhatsAppOnboardingSessionResponse,
)
from app.schemas.platform import PlatformOwner, RuntimeSourceAuthorityResponse
from app.schemas.provider_environment_repair import (
    ProviderEnvironmentVerifierAccess,
    ProviderEnvironmentVerifierGrant,
)
from app.schemas.session import EnvironmentCreatedResponse
from app.services.agent_environments import (
    AgentEnvironmentIdConflict,
    clear_connected_agent_registration,
    local_machine_registration_key,
    register_agent_environment,
)
from app.services.agent_lifecycle import (
    AgentLifecycleBoundaryError,
    active_agent_filter,
    archive_agent_and_project,
)
from app.services.ai_provider_auth_transition import transition_ai_provider_auth
from app.services.ai_provider_connection_ownership import require_custom_provider_cli
from app.services.ai_provider_credentials import (
    OAuthCredentialClaimConflict,
    lock_ai_provider_owner,
    reconcile_runtime_oauth_claims,
    release_runtime_oauth_claims,
)
from app.services.api_key import mint_api_key
from app.services.app_setting_registry import (
    APP_SETTING_SPEC_BY_KEY,
    AppSettingValueError,
    get_app_setting_spec,
)
from app.services.app_settings import stage_app_setting_upsert
from app.services.audit import record_control_plane_audit
from app.services.channel_config import (
    discord_interactions_config_error,
    validate_channel_account_config_urls,
    validate_required_discord_interactions_config,
)
from app.services.channels import (
    RUNTIME_CHANNEL_PROVIDERS,
    archive_channel_account,
    build_channel_account,
    channel_webhook_url,
    configure_discord_application,
    configure_telegram_provider_webhook,
    discord_application_id_from_config,
    discord_config_without_unverified_install_state,
    encrypt_optional_token,
    ensure_discord_application_identity_available,
    generate_webhook_secret,
    get_telegram_bot_username,
    hash_token,
    mark_discord_reserved_commands_current,
    mark_telegram_reserved_commands_current,
    normalize_telegram_bot_username,
    rearm_discord_command_reconciliation,
    require_unchanged_discord_application_identity,
    store_channel_secrets,
    sync_channel_commands,
    upsert_channel_secrets,
    verify_discord_application_token_identity,
)
from app.services.companion_billing_reconcile import CompanionBillingReconcileWorker
from app.services.companion_rehearsal import (
    RehearsalError,
    plan_rehearsal,
    record_rehearsal_result,
)
from app.services.file_store import get_file_store
from app.services.hosted_runtime_secrets import (
    hosted_runtime_secret_values_changed,
    load_hosted_runtime_secrets_for_update,
    sync_hosted_runtime_secret_values,
)
from app.services.managed_ai_provider import (
    MANAGED_AI_PROVIDER_API_MODE,
    MANAGED_AI_PROVIDER_LABEL,
    MANAGED_AI_PROVIDER_PROFILE,
    MANAGED_AI_PROVIDER_RUNTIME_ENV,
    MANAGED_AI_PROVIDER_SCOPE,
    MANAGED_AI_PROVIDER_TYPE,
    V2_MANAGED_AI_PROVIDER_ID,
    V2_MANAGED_AI_PROVIDER_IDS,
    DeploymentManagedProviderCleanupInvariantError,
    DeploymentManagedProviderCleanupMismatchError,
    archive_clawdi_managed_provider,
    cleanup_deployment_managed_provider,
    find_clawdi_managed_provider,
    is_runtime_metadata_managed_provider_id,
    is_supported_managed_provider_runtime_contract,
    is_v2_deployment_managed_provider_id,
    lock_deployment_managed_provider_mutation,
    lock_runtime_metadata_managed_provider_mutation,
    replace_managed_provider_runtime_metadata,
    upsert_clawdi_managed_provider,
)
from app.services.platform_contract import (
    lock_platform_idempotency,
    platform_request_hash,
    read_platform_replay,
    store_platform_response,
)
from app.services.platform_workload_auth import (
    PlatformWorkloadKeyResolver,
    get_platform_workload_key_resolver,
)
from app.services.principal_lifecycle import (
    PrincipalIdentityConflictError,
    PrincipalLifecycleConfigurationError,
    PrincipalTerminatedError,
    assert_user_authority_active,
    configured_clerk_issuer,
    fence_principal_deleted,
    load_clerk_user_for_issuer,
    resolve_clerk_owner_issuer,
    set_clerk_principal_suspension,
)
from app.services.project_runtime_skills import (
    assert_agent_workspace_skill_write_compatible,
)
from app.services.provider_environment_verifier_access import (
    bootstrap_workload_client,
    inspect_verifier_access,
    register_workload_signer,
    update_verifier_access,
)
from app.services.runtime_generation import (
    RuntimeApplyGenerationUpdateError,
    resolve_runtime_apply_generation_update,
)
from app.services.runtime_manifest_resources import (
    enabled_runtime_manifest_skill_ids,
    lock_runtime_manifest_skill_reservations,
)
from app.services.runtime_observation import RuntimeObservationProtocolError
from app.services.runtime_source import RuntimeSourceError, RuntimeSourceNotFoundError
from app.services.runtime_source_authority import load_runtime_source_authority
from app.services.runtime_state_cleanup import lock_runtime_state_write_fence
from app.services.sync_events import (
    notify_sync_subscriptions_changed,
    queue_environment_runtime_manifest_changed,
    queue_provider_runtime_manifest_changed,
    queue_runtime_manifest_changed,
    queue_runtime_manifests_changed,
    runtime_manifest_provider_non_auth_signature,
)
from app.services.tenant_export import EXPORT_PRESIGNED_TTL_SECONDS, create_tenant_export_job
from app.services.user_prov import get_user_provisioning_provider
from app.services.user_provisioning import (
    BILLING_EXEMPT_GRANTED_ACTION,
    BILLING_EXEMPT_REVOKED_ACTION,
    lazy_create_partner_user_with_personal_project,
    lazy_create_user_with_personal_project,
)
from app.services.vault_crypto import active_key_version, encrypt_with_active_key
from app.services.vault_key_rotation import (
    CompanionRecoveryRefused,
    CompanionRecoveryRequest,
    CompanionRegenerate,
    CompanionReprovision,
    CompanionReprovisionFailed,
    ReprovisionResult,
    companion_secret_ref,
    is_readable,
    load_live_companion_secrets,
    plan_companion_recovery,
    recover_companion_secrets,
    reprovision_companion_credentials,
    rotate_agent_secrets,
)
from app.services.whatsapp_device_onboarding import require_whatsapp_logout_for_archive
from app.services.whatsapp_managed_onboarding import (
    cancel_platform_whatsapp_pairing,
    get_platform_whatsapp_pairing,
    start_platform_whatsapp_pairing,
)
from app.services.whatsapp_sidecar_registry import get_active_whatsapp_sidecar_clients

logger = logging.getLogger(__name__)

# `include_in_schema=False`: a codegen/contract exclusion, not secrecy.
# Excluding the admin surface from /openapi.json stops `bun run
# generate-api` (web/CLI typed-client codegen) from emitting bindings
# for it, keeping admin endpoints out of the generated public API
# contract. Path secrecy is NOT the security boundary: the operator
# dashboard (apps/web/src/lib/admin-client.ts) deliberately hand-rolls
# its client against these endpoints, so admin paths DO appear in the
# shipped web bundle. The boundary is `require_admin_api_key`
# (X-Admin-Key) on every route. The routes themselves stay live.
router = APIRouter(prefix="/admin", tags=["admin"], include_in_schema=False)
whatsapp_pairing_router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    include_in_schema=False,
)

_AI_PROVIDER_ARCHIVE_OPERATION = "admin.ai_provider.archive"


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"


def _admin_app_setting_response(setting: AppSetting) -> AdminAppSettingResponse:
    spec = get_app_setting_spec(setting.key)
    return AdminAppSettingResponse(
        key=setting.key,
        value=setting.value_json,
        description=spec.description,
        created_at=setting.created_at,
        updated_at=setting.updated_at,
    )


def _setting_enabled(value: JsonValue | None) -> bool | None:
    if isinstance(value, dict):
        enabled = value.get("enabled")
        if isinstance(enabled, bool):
            return enabled
    return None


@router.put(
    "/auth/suspensions",
    response_model=AdminPrincipalSuspensionResponse,
)
async def admin_set_principal_suspension(
    body: AdminPrincipalSuspensionUpdate,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminPrincipalSuspensionResponse:
    """Idempotently set or clear the platform-owned authentication fence."""

    _no_store(response)
    try:
        issuer = configured_clerk_issuer()
        result = await set_clerk_principal_suspension(
            db,
            issuer=issuer,
            subject=body.target_clerk_id,
            suspended=body.suspended,
            reason=body.reason,
        )
    except PrincipalLifecycleConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Clerk principal issuer is not configured",
        ) from None
    except PrincipalTerminatedError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Principal has been terminated",
        ) from None

    record_control_plane_audit(
        db,
        actor_type="admin",
        action=("principal.suspend" if body.suspended else "principal.unsuspend"),
        resource_type="clerk_principal",
        resource_id=body.target_clerk_id,
        target_user_id=result.user_id,
        source="api.admin",
        details={
            "issuer": issuer,
            "reason": body.reason,
            "changed": result.changed,
        },
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return AdminPrincipalSuspensionResponse(
        target_clerk_id=body.target_clerk_id,
        suspended=result.suspended,
        suspended_at=result.suspended_at,
        changed=result.changed,
    )


@router.put(
    "/auth/billing-exemptions",
    response_model=AdminBillingExemptionResponse,
)
async def admin_set_billing_exemption(
    body: AdminBillingExemptionUpdate,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminBillingExemptionResponse:
    """Idempotently grant or revoke a Clerk user's billing exemption.

    This is the only admin route that sets `users.invite_exempted_at` for a
    Clerk user (06.3 D-01, ROADMAP SC-5, WR-02). An exempt user passes the
    billing gate without a /v2 read until the exemption is revoked. The user
    row is lazy-created if absent, so an operator can exempt someone before
    their first sign-in.

    A revoke that changes the exemption also clears the sticky unlock
    (`users.billing_unlocked_at`), so the user's next gated request re-reads
    /v2 (06.3 G-06.3-R2-4, WR-01 Option A, D-03). An account with an active
    subscription unlocks again on that read; one without a subscription is
    locked. An unchanged revoke and a grant never touch the unlock, and every
    audit row records whether one was cleared (`billing_unlock_cleared`).
    No route writes the unlock back, so a clearing revoke also records the
    cleared value (`previous_billing_unlocked_at`, ISO 8601; null otherwise)
    for an operator to restore by hand after a mistaken revoke (06.3 WR-03).
    """

    _no_store(response)
    target = await _resolve_or_create_user(db, body.target_clerk_id)
    currently_exempt = target.invite_exempted_at is not None
    changed = currently_exempt != body.exempt
    unlock_cleared = False  # iforge (06.3 G-06.3-R2-4)
    previous_unlock = target.billing_unlocked_at  # iforge (06.3 WR-03)
    if changed:
        target.invite_exempted_at = datetime.now(UTC) if body.exempt else None
        if not body.exempt:  # iforge (06.3 G-06.3-R2-4)
            unlock_cleared = previous_unlock is not None  # iforge (06.3 G-06.3-R2-4)
            target.billing_unlocked_at = None  # iforge (06.3 G-06.3-R2-4)
    record_control_plane_audit(
        db,
        actor_type="admin",
        action=(BILLING_EXEMPT_GRANTED_ACTION if body.exempt else BILLING_EXEMPT_REVOKED_ACTION),
        resource_type="user",
        resource_id=str(target.id),
        target_user_id=target.id,
        source="api.admin",
        details={
            "target_clerk_id": body.target_clerk_id,
            "reason": body.reason,
            "changed": changed,
            "billing_unlock_cleared": unlock_cleared,  # iforge (06.3 G-06.3-R2-4)
            # iforge (06.3 WR-03): nothing else records the cleared value, and only
            # a fresh /v2 read can stamp it again, so keep it for a manual restore.
            "previous_billing_unlocked_at": (
                previous_unlock.isoformat() if unlock_cleared and previous_unlock else None
            ),
        },
    )
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return AdminBillingExemptionResponse(
        target_clerk_id=body.target_clerk_id,
        user_id=target.id,
        exempt=target.invite_exempted_at is not None,
        exempted_at=target.invite_exempted_at,
        changed=changed,
    )


@router.get("/settings", response_model=AdminAppSettingListResponse)
async def admin_list_app_settings(
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminAppSettingListResponse:
    """List registered global application settings."""

    rows = list(
        (
            await db.execute(
                select(AppSetting)
                .where(AppSetting.key.in_(APP_SETTING_SPEC_BY_KEY))
                .order_by(AppSetting.key)
            )
        ).scalars()
    )
    return AdminAppSettingListResponse(
        items=[_admin_app_setting_response(setting) for setting in rows]
    )


@router.get("/settings/{key}", response_model=AdminAppSettingResponse)
async def admin_get_app_setting(
    key: str,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminAppSettingResponse:
    """Read one registered global application setting."""

    try:
        get_app_setting_spec(key)
    except AppSettingValueError as error:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App setting not found") from error
    setting = await db.get(AppSetting, key)
    if setting is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "App setting not found")
    return _admin_app_setting_response(setting)


@router.put("/settings/{key}", response_model=AdminAppSettingResponse)
async def admin_upsert_app_setting(
    key: str,
    body: AdminAppSettingUpsert,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminAppSettingResponse:
    """Atomically replace one registered global setting and audit the mutation."""

    try:
        setting, previous, created = await stage_app_setting_upsert(
            db,
            key=key,
            value=body.value,
        )
    except AppSettingValueError as error:
        if key not in APP_SETTING_SPEC_BY_KEY:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "App setting not found") from error
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid app setting value") from error

    current = setting.value_json
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="app_setting.upsert",
        resource_type="app_setting",
        resource_id=key,
        source="api.admin",
        details={
            "created": created,
            "previous_enabled": _setting_enabled(previous),
            "enabled": _setting_enabled(current),
            "schema_version": (
                current.get("schema_version") if isinstance(current, dict) else None
            ),
        },
    )
    await db.commit()
    await db.refresh(setting)
    return _admin_app_setting_response(setting)


@router.post(
    "/tenants/{user_id}/exports",
    response_model=AdminTenantExportCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_trigger_tenant_export(
    user_id: UUID,
    body: AdminTenantExportCreate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminTenantExportCreatedResponse:
    """Queue a tenant export job (ACCESS-04, D-12/D-13).

    The TenantExportWorker picks the row up asynchronously; the operator
    polls the list endpoint for status and the presigned download link.
    Vault secret values enter the archive only behind the explicit
    `include_vault_values` flag (D-10).

    Created through the same helper as the tenant's own export (plan 06-19), so an operator's
    export of a tenant with a live companion promises that companion's data too, rather than
    completing without it.
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    job = await create_tenant_export_job(
        db, user_id=user_id, include_vault_values=body.include_vault_values
    )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="tenant.export_requested",
        resource_type="tenant_export_job",
        resource_id=str(job.id),
        source="api.admin",
        target_user_id=user_id,
        details={
            "job_id": str(job.id),
            "include_vault_values": body.include_vault_values,
        },
    )
    await db.commit()
    return AdminTenantExportCreatedResponse(
        job_id=job.id,
        status=TENANT_EXPORT_STATUS_QUEUED,
    )


@router.get(
    "/tenants/{user_id}/exports",
    response_model=AdminTenantExportListResponse,
)
async def admin_list_tenant_exports(
    response: Response,
    user_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminTenantExportListResponse:
    """List a tenant's export jobs newest-first (D-13).

    A pure read: it presigns nothing, writes nothing and commits nothing.
    The console polls this every 4 seconds for the life of a running
    export, and presigning on read meant every poll minted a fresh live
    download URL — plus a `tenant.export_link_issued` audit row — for
    every complete job in the tenant's history, including
    `include_vault_values` archives nobody had asked to download
    (W-03 / WR-02, T-03-64 / T-03-65). `downloadable` reports which jobs
    the download endpoint below will answer; minting is that endpoint's
    job, and one operator ask is one audit row.
    """
    _no_store(response)
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    jobs = (
        await db.scalars(
            select(TenantExportJob)
            .where(TenantExportJob.user_id == user_id)
            .order_by(TenantExportJob.created_at.desc(), TenantExportJob.id.desc())
        )
    ).all()

    return AdminTenantExportListResponse(
        items=[
            AdminTenantExportListItem(
                job_id=job.id,
                status=cast(TenantExportStatus, job.status),
                created_at=job.created_at,
                completed_at=job.completed_at,
                include_vault_values=job.include_vault_values,
                downloadable=_export_job_is_downloadable(job),
                error=job.error,
            )
            for job in jobs
        ]
    )


def _export_job_is_downloadable(job: TenantExportJob) -> bool:
    """The single predicate behind the list's flag and the download route.

    One definition, so the flag cannot promise a link the endpoint refuses.
    """
    return job.status == TENANT_EXPORT_STATUS_COMPLETE and job.artifact_key is not None


@router.get(
    "/tenants/{user_id}/exports/{job_id}/download",
    response_model=AdminTenantExportDownloadResponse,
)
async def admin_download_tenant_export(
    user_id: UUID,
    job_id: UUID,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminTenantExportDownloadResponse:
    """Mint one presigned download URL for one export job (D-13, T-03-18).

    This is the only place a link is issued, and each call writes exactly
    one `tenant.export_link_issued` audit row — so the audit trail records
    deliberate operator intent rather than a polling client. The row
    carries the job id, the artifact key and the TTL, never the URL.

    The job is loaded scoped by BOTH path identifiers in one query: a route
    taking a `user_id` and a `job_id` must not let the second be honoured
    on its own, which would mint a link for another tenant's export
    (T-03-66). A job that is not complete, or complete without an artifact,
    is REFUSED rather than answered with a null URL.

    The response is `no-store` (CR-01): the minted URL is bearer-equivalent
    for its whole TTL and reaches the tenant's entire archive — the decrypted
    credentials bundle included, when the job carried `include_vault_values`.
    Nothing else on this route marks it uncacheable, and the `X-Admin-Key`
    scheme puts it outside RFC 9111 3.5's shared-cache protection for
    `Authorization`.
    """
    _no_store(response)
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    job = (
        await db.scalars(
            select(TenantExportJob).where(
                TenantExportJob.id == job_id,
                TenantExportJob.user_id == user_id,
            )
        )
    ).one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export job not found")

    if not _export_job_is_downloadable(job):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "export_not_downloadable", "status": job.status},
        )

    assert job.artifact_key is not None  # narrowed by _export_job_is_downloadable
    download_url = await get_file_store().presigned_get_url(
        job.artifact_key, EXPORT_PRESIGNED_TTL_SECONDS
    )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="tenant.export_link_issued",
        resource_type="tenant_export_job",
        resource_id=str(job.id),
        source="api.admin",
        target_user_id=user_id,
        details={
            "job_id": str(job.id),
            "artifact_key": job.artifact_key,
            "ttl_seconds": EXPORT_PRESIGNED_TTL_SECONDS,
        },
    )
    await db.commit()
    return AdminTenantExportDownloadResponse(
        job_id=job.id,
        download_url=download_url,
        expires_in_seconds=EXPORT_PRESIGNED_TTL_SECONDS,
    )


def _admin_user_lookup_term(
    q: str | None = Query(default=None, min_length=1, max_length=320),
    email: str | None = Query(default=None, min_length=1, max_length=320),
) -> str:
    """Resolve the single search term for the tenant picker.

    `q` takes precedence when both are supplied; `email` is kept for the
    existing callers. Neither is a 422 raised through FastAPI's own
    validation channel rather than a hand-rolled 400: the endpoint has no
    unfiltered mode, and an unfiltered listing of every tenant is not a
    capability this adds.
    """
    term = q if q is not None else email
    if term is None:
        raise RequestValidationError(
            [
                {
                    "type": "missing",
                    "loc": ("query", "q"),
                    "msg": "Provide q (user id, clerk id or email fragment) or email",
                    "input": None,
                }
            ]
        )
    return term


@router.get("/users", response_model=AdminUserListResponse)
async def admin_lookup_users(
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
    term: str = Depends(_admin_user_lookup_term),
) -> AdminUserListResponse:
    """Tenant picker for the admin UI (03-10), capped at 20 rows.

    Matches an escaped case-insensitive substring of `email` OR an exact
    `id` OR an exact `clerk_id`. `email` is nullable — admin-provisioned
    rows carry NULL until first direct sign-in and partner-tenant rows never
    have one — so an email-only predicate left both ACCESS-04 data-rights
    capabilities unreachable for exactly those tenants, and unreachable by
    the `user_id` an operator holds after an incident (W-05 / IN-04).

    Deliberately does NOT filter by principal kind: partner tenants are one
    of the populations the finding names. The id branch parses defensively —
    an unparseable term simply contributes no predicate rather than raising.
    """
    _no_store(response)
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    predicates: list[ColumnElement[bool]] = [
        User.email.ilike(f"%{escaped}%", escape="\\"),
        User.clerk_id == term,
    ]
    try:
        predicates.append(User.id == UUID(term))
    except ValueError:
        pass
    rows = (
        await db.scalars(
            # NULLs sort last on ASC in Postgres, which is the order wanted
            # here: named tenants first, id-only tenants after. `User.id` is
            # the tiebreak so the page is deterministic.
            select(User).where(or_(*predicates)).order_by(User.email, User.id).limit(20)
        )
    ).all()
    return AdminUserListResponse(
        items=[
            AdminUserListItem(
                id=row.id,
                email=row.email,
                # The lookup already MATCHES on clerk_id; emitting it makes the
                # third accepted identifier obtainable rather than merely
                # guessable (G-03-62).
                clerk_id=row.clerk_id,
                name=row.name,
                created_at=row.created_at,
                billing_exempt=row.invite_exempted_at is not None,
            )
            for row in rows
        ]
    )


async def _open_tenant_purge(db: AsyncSession, lifecycle_id: UUID) -> TenantPurge | None:
    """The at-most-one non-complete purge row for a lifecycle."""
    return (
        await db.execute(
            select(TenantPurge).where(
                TenantPurge.lifecycle_id == lifecycle_id,
                TenantPurge.status != TENANT_PURGE_STATUS_COMPLETE,
            )
        )
    ).scalar_one_or_none()


@router.post("/tenants/{user_id}/delete", response_model=AdminTenantDeleteResponse)
async def admin_delete_tenant(
    user_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminTenantDeleteResponse:
    """Operator-triggered tenant deletion (ACCESS-04, D-14/D-15).

    Fences the tenant IMMEDIATELY through `fence_principal_deleted` — the
    lifecycle-fence core shared with the Clerk webhook — schedules a
    cancellable purge at now()+tenant_purge_grace_days, then deletes the
    provider identity via the D-02 seam. This path writes NO
    ClerkWebhookEventReceipt: receipts stay genuine webhook evidence; the
    operator evidence trail is the `tenant.delete_requested` audit event.
    The Clerk webhook fired by the identity deletion later converges
    through `fence_clerk_user_deleted` as reconciliation, by design.

    The identity provider is RESOLVED up front — before the fence, before the
    purge row, before the commit — even though `delete_identity` is still
    called after that commit. `get_user_provisioning_provider()` raises for an
    unknown `settings.user_prov` and for the not-yet-implemented `"supa"`, and
    resolving it last meant a config value the seam cannot dispatch produced a
    tenant that was irreversibly fenced, a one-way purge that was durably
    scheduled, a Clerk identity that still existed, and an operator reading a
    500 as "the delete did not land" (WR-09). Construction is pure and cheap,
    so hoisting it costs nothing and changes no happy path: a misconfigured
    seam now refuses the delete outright instead of half-applying it. Plan
    03-15 closed the other half of the same guarantee by validating
    `user_prov` at settings load; this ordering holds regardless of how a
    `Settings` object was built.
    """
    provider = get_user_provisioning_provider()
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    subject = target.clerk_id
    if target.principal_kind != PRINCIPAL_KIND_CLERK or subject is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "User is not a Clerk principal")
    issuer = target.clerk_issuer or settings.clerk_jwt_issuer
    if not issuer:
        raise HTTPException(status.HTTP_409_CONFLICT, "Clerk issuer cannot be resolved for user")

    now = datetime.now(UTC)
    lifecycle, fence_created = await fence_principal_deleted(
        db,
        issuer=issuer,
        subject=subject,
        occurred_at=now,
        now=now,
    )
    purge = await _open_tenant_purge(db, lifecycle.id)
    if purge is None:
        grace_days = settings.tenant_purge_grace_days
        purge_after = now + timedelta(days=grace_days)
        purge = TenantPurge(
            lifecycle_id=lifecycle.id,
            user_id=user_id,
            subject=subject,
            issuer=issuer,
            purge_after=purge_after,
            next_attempt_at=purge_after,
        )
        db.add(purge)
        await db.flush()
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="tenant.delete_requested",
            resource_type="tenant_purge",
            resource_id=str(purge.id),
            source="api.admin",
            target_user_id=user_id,
            details={
                "lifecycle_id": str(lifecycle.id),
                "purge_id": str(purge.id),
                "purge_after": purge.purge_after.isoformat(),
                # The window that PRODUCED the timestamp above, from the same
                # local, so the schedule an operator authorised is auditable
                # rather than inferred from a config file read at a later date.
                "grace_days": grace_days,
                "fence_created": fence_created,
            },
        )
    await db.commit()

    # Identity deletion AFTER the durable fence+schedule commit: a provider
    # failure here surfaces loudly (500) but the fence and purge persist —
    # the purge re-issues the identity delete 404-tolerantly at sweep time,
    # and Clerk's own user.deleted webhook remains the reconciliation net.
    # Only the RESOLUTION moved to the top of the function (WR-09); this call
    # site is deliberately unchanged.
    await provider.delete_identity(subject)
    return AdminTenantDeleteResponse(lifecycle_id=lifecycle.id, purge_after=purge.purge_after)


@router.post("/tenants/{user_id}/purge/cancel", response_model=AdminPurgeCancelResponse)
async def admin_cancel_tenant_purge(
    user_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminPurgeCancelResponse:
    """Cancel a pending purge (D-15: cancellable, 30-day default window).

    Deletes the pending row only — the tenant STAYS fenced; cancel undoes
    the purge, never the deletion. 404 with no pending purge; 409 when the
    purge is already running or complete (deleting a mid-run purge row
    would race the worker; a complete purge is historical fact).

    The select takes `FOR UPDATE`. Without it this was check-then-act: the
    worker claims with `FOR UPDATE SKIP LOCKED` and COMMITS the claim before
    executing, so an unlocked cancel could read `pending`, lose the race, and
    still delete the row by primary key — returning `{"canceled": true}` while
    the irreversible sweep tore down deployments, S3 prefixes and rows. Under
    the row lock the cancel blocks on the claim transaction and then re-reads
    `purging`, which falls into the 409 below. The window was realistic
    exactly when cancel matters, because `failed` rows are re-claimed on every
    elapsed backoff.
    """
    purges = list(
        (
            await db.scalars(
                select(TenantPurge)
                .where(TenantPurge.user_id == user_id)
                .order_by(TenantPurge.created_at.desc())
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    cancellable = [
        row
        for row in purges
        if row.status in (TENANT_PURGE_STATUS_PENDING, TENANT_PURGE_STATUS_FAILED)
    ]
    if not cancellable:
        if any(
            row.status in (TENANT_PURGE_STATUS_PURGING, TENANT_PURGE_STATUS_COMPLETE)
            for row in purges
        ):
            raise HTTPException(status.HTTP_409_CONFLICT, "Purge is already running or complete")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending purge for user")

    for row in cancellable:
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="tenant.purge_canceled",
            resource_type="tenant_purge",
            resource_id=str(row.id),
            source="api.admin",
            target_user_id=user_id,
            details={
                "lifecycle_id": str(row.lifecycle_id),
                "purge_after": row.purge_after.isoformat(),
                "status_at_cancel": row.status,
                # Canceling a `failed` row destroys the only record that a
                # partial sweep already ran. The row is still deleted (that is
                # what stops `claim_tenant_purge` re-claiming it), so the
                # attempt count and last error are carried into the audit event
                # before they go with it (WR-02).
                "attempt_count": row.attempt_count,
                "error": row.error,
            },
        )
        await db.delete(row)
    await db.commit()
    return AdminPurgeCancelResponse(canceled=True)


@router.get("/tenants/{user_id}/lifecycle", response_model=AdminTenantLifecycleResponse)
async def admin_tenant_lifecycle_status(
    response: Response,
    user_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminTenantLifecycleResponse:
    """Fence + purge status view for the 03-10 deletion panel.

    Answers even after the purge has erased the users row: the purge row
    (retained tombstone) carries the user id captured at schedule time.

    Reports the purge's `error`, `attempt_count` and `claimed_at` so the panel
    can say what already ran instead of labelling a FAILED purge "Purge
    scheduled" (W-01), and the live `grace_days` so the confirmation dialog
    never asserts a window only the server knows (WR-13).
    """
    _no_store(response)
    target = await db.get(User, user_id)
    purge = (
        await db.execute(
            select(TenantPurge)
            .where(TenantPurge.user_id == user_id)
            .order_by(TenantPurge.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if target is None and purge is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if purge is not None:
        fenced = True  # A purge row exists only for a fenced lifecycle.
    else:
        fenced = (
            await db.scalar(
                select(PrincipalLifecycle.id).where(PrincipalLifecycle.user_id == user_id)
            )
        ) is not None
    return AdminTenantLifecycleResponse(
        fenced=fenced,
        purge=AdminTenantPurgeInfo(
            status=cast(TenantPurgeStatus, purge.status),
            purge_after=purge.purge_after,
            completed_at=purge.completed_at,
            error=purge.error,
            attempt_count=purge.attempt_count,
            claimed_at=purge.claimed_at,
        )
        if purge is not None
        else None,
        grace_days=settings.tenant_purge_grace_days,
    )


async def _resolve_or_create_user(
    db: AsyncSession,
    clerk_id: str,
) -> User:
    """Resolve a user by clerk_id, lazy-creating the row + Personal
    project if needed.

    The lazy-create exists for the common SaaS-deploy entry path
    where a user clicks Deploy on clawdi.ai before ever signing
    into cloud.clawdi.ai directly. Without it the admin endpoint
    would 404, the SaaS would catch the error, the pod would deploy
    without sync, and the user would have to redeploy after their
    first direct visit.

    Trust model: `clerk_id` here is value the SaaS already
    authenticated against the user's Clerk session — the shared
    `X-Admin-Key` gate trusts first-party server-to-server callers.
    Email/name are unknown (no JWT available); the row starts with
    `email=None` and the JWT path backfills on first direct sign-in.

    Race-loser status is 500 (not 401): admin callers are
    first-party SaaS code, so a vanishing-winner-row situation is
    an operational anomaly worth a loud failure rather than a
    user-flow retry.
    """
    try:
        issuer = await resolve_clerk_owner_issuer(
            db,
            subject=clerk_id,
        )
        target = await load_clerk_user_for_issuer(
            db,
            issuer=issuer,
            subject=clerk_id,
            bind_legacy=False,
        )
    except (PrincipalIdentityConflictError, PrincipalTerminatedError):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner has been terminated") from None
    except PrincipalLifecycleConfigurationError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Clerk owner issuer is not configured",
        ) from None
    if target is not None:
        try:
            await assert_user_authority_active(db, target.id)
        except PrincipalTerminatedError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner has been terminated") from None
        return target

    user = await lazy_create_user_with_personal_project(
        db,
        clerk_id=clerk_id,
        clerk_issuer=issuer,
        email=None,
        name=None,
        race_loser_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        # No billing exemption here: creating the row is a side effect of
        # whichever admin call (often hosted deploy orchestration) reached
        # the user first. Exemption is the explicit operator action
        # `PUT /auth/billing-exemptions` (06.3 WR-02).
    )
    logger.info("admin_lazy_create_user clerk_id=%s user_id=%s", clerk_id, user.id)
    return user


async def _find_admin_owner(
    db: AsyncSession,
    owner: PlatformOwner,
) -> User:
    if owner.kind == PRINCIPAL_KIND_CLERK:
        try:
            issuer = await resolve_clerk_owner_issuer(
                db,
                subject=owner.ref,
            )
            target = await load_clerk_user_for_issuer(
                db,
                issuer=issuer,
                subject=owner.ref,
                bind_legacy=False,
            )
        except (PrincipalIdentityConflictError, PrincipalTerminatedError):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner has been terminated") from None
        except PrincipalLifecycleConfigurationError:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Clerk owner issuer is not configured",
            ) from None
    else:
        target = (
            await db.execute(
                select(User).where(
                    User.principal_kind == PRINCIPAL_KIND_PARTNER_TENANT,
                    User.partner_tenant_ref == owner.ref,
                    User.clerk_id.is_(None),
                )
            )
        ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Owner not found")
    try:
        await assert_user_authority_active(db, target.id)
    except PrincipalTerminatedError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner has been terminated") from None
    return target


async def _resolve_or_create_admin_owner(
    db: AsyncSession,
    owner: PlatformOwner,
) -> User:
    if owner.kind == PRINCIPAL_KIND_CLERK:
        return await _resolve_or_create_user(db, owner.ref)

    target = (
        await db.execute(
            select(User).where(
                User.principal_kind == PRINCIPAL_KIND_PARTNER_TENANT,
                User.partner_tenant_ref == owner.ref,
                User.clerk_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if target is not None:
        try:
            await assert_user_authority_active(db, target.id)
        except PrincipalTerminatedError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Owner has been terminated") from None
        return target
    return await lazy_create_partner_user_with_personal_project(
        db,
        partner_tenant_ref=owner.ref,
        race_loser_status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _require_managed_provider_contract(provider: AiProvider) -> None:
    if not is_supported_managed_provider_runtime_contract(provider):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Stored managed AI provider contract is invalid",
        )


async def _admin_deployment_managed_provider_response(
    db: AsyncSession,
    *,
    provider: AiProvider,
    owner: PlatformOwner,
    target: User,
) -> AdminDeploymentManagedAiProviderResponse:
    _require_managed_provider_contract(provider)
    try:
        auth = ai_provider_auth_from_persistence(
            provider.auth_type,
            provider.auth_ref,
            provider.auth_metadata,
        )
    except (ValidationError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Stored managed AI provider auth metadata is invalid",
        ) from exc
    has_api_key = (
        await db.scalar(
            select(AiProviderAuthPayload.id).where(
                AiProviderAuthPayload.owner_user_id == target.id,
                AiProviderAuthPayload.provider_id == provider.provider_id,
                AiProviderAuthPayload.auth_profile == MANAGED_AI_PROVIDER_PROFILE,
                AiProviderAuthPayload.kind == "api_key",
                AiProviderAuthPayload.source == "managed",
                AiProviderAuthPayload.archived_at.is_(None),
            )
        )
        is not None
    )
    return AdminDeploymentManagedAiProviderResponse(
        id=provider.id,
        owner=owner,
        owner_user_id=target.id,
        owner_clerk_id=target.clerk_id,
        provider_id=provider.provider_id,
        scope=MANAGED_AI_PROVIDER_SCOPE,
        type=MANAGED_AI_PROVIDER_TYPE,
        label=provider.label or MANAGED_AI_PROVIDER_LABEL,
        api_mode=MANAGED_AI_PROVIDER_API_MODE,
        auth=auth,
        managed_by="clawdi",
        runtime_env_name=MANAGED_AI_PROVIDER_RUNTIME_ENV,
        base_url=provider.base_url,
        capabilities=provider.capabilities,
        models=provider.models,
        has_api_key=has_api_key,
    )


def _record_deployment_managed_provider_audit(
    db: AsyncSession,
    *,
    action: str,
    owner: PlatformOwner,
    owner_user_id: UUID | None,
    provider_id: str,
    outcome: str,
    provider_uuid: UUID | None = None,
) -> None:
    details: dict[str, Any] = {
        "auth_method": "x_admin_key",
        "owner": owner.model_dump(mode="json"),
        "owner_user_id": str(owner_user_id) if owner_user_id is not None else None,
        "provider_id": provider_id,
        "outcome": outcome,
    }
    if provider_uuid is not None:
        details["provider_uuid"] = str(provider_uuid)
    record_control_plane_audit(
        db,
        actor_type="admin",
        action=action,
        resource_type="ai_provider",
        resource_id=provider_id,
        target_user_id=owner_user_id,
        source="api.admin",
        details=details,
    )


def _ai_provider_removal_incarnation_token(
    *,
    owner_user_id: UUID,
    provider_id: str,
    provider: AiProvider | None,
) -> str:
    """Return an opaque provider-incarnation CAS token."""

    if provider is None:
        authority: dict[str, JsonValue] = {
            "owner_user_id": str(owner_user_id),
            "provider_id": provider_id,
            "authority": "absent",
        }
    else:
        authority = {
            "owner_user_id": str(owner_user_id),
            "provider_row_id": str(provider.id),
            "incarnation_id": str(provider.incarnation_id),
        }
    return platform_request_hash(authority)


async def _find_deployment_managed_provider_owner(
    db: AsyncSession,
    *,
    owner: PlatformOwner,
    provider_id: str,
    action: str,
) -> User:
    try:
        return await _find_admin_owner(db, owner)
    except HTTPException:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=None,
            provider_id=provider_id,
            outcome="failed",
        )
        await db.commit()
        raise


async def _find_deployment_managed_provider_cleanup_owner(
    db: AsyncSession,
    *,
    owner: PlatformOwner,
    provider_id: str,
    action: str,
) -> tuple[User, datetime | None]:
    """Resolve retained ownership or an exact completed Clerk cleanup fence."""

    try:
        return await _find_admin_owner(db, owner), None
    except HTTPException as exc:
        if owner.kind != PRINCIPAL_KIND_CLERK or exc.status_code != status.HTTP_403_FORBIDDEN:
            _record_deployment_managed_provider_audit(
                db,
                action=action,
                owner=owner,
                owner_user_id=None,
                provider_id=provider_id,
                outcome="failed",
            )
            await db.commit()
            raise

        issuer = configured_clerk_issuer()
        suspended_target = await db.scalar(
            select(User)
            .join(
                ClerkPrincipalSuspension,
                ClerkPrincipalSuspension.user_id == User.id,
            )
            .where(
                User.principal_kind == PRINCIPAL_KIND_CLERK,
                User.clerk_id == owner.ref,
                ClerkPrincipalSuspension.issuer == issuer,
                ClerkPrincipalSuspension.subject == owner.ref,
                ~select(PrincipalLifecycle.id)
                .where(
                    PrincipalLifecycle.issuer == issuer,
                    PrincipalLifecycle.subject == owner.ref,
                )
                .exists(),
            )
        )
        if suspended_target is not None:
            return suspended_target, None
        row = (
            await db.execute(
                select(User, PrincipalLifecycle.cleanup_completed_at)
                .join(PrincipalLifecycle, PrincipalLifecycle.user_id == User.id)
                .where(
                    User.principal_kind == PRINCIPAL_KIND_CLERK,
                    User.clerk_id == owner.ref,
                    User.clerk_issuer == issuer,
                    PrincipalLifecycle.issuer == issuer,
                    PrincipalLifecycle.subject == owner.ref,
                    PrincipalLifecycle.cleanup_completed_at.is_not(None),
                )
            )
        ).one_or_none()
        if row is None:
            _record_deployment_managed_provider_audit(
                db,
                action=action,
                owner=owner,
                owner_user_id=None,
                provider_id=provider_id,
                outcome="failed",
            )
            await db.commit()
            raise
        target, cleanup_completed_at = row
        return target, cleanup_completed_at


async def _raise_deployment_managed_provider_scope_denied(
    db: AsyncSession,
    *,
    action: str,
    owner: PlatformOwner,
    owner_user_id: UUID,
    provider_id: str,
) -> Never:
    # Deliberately do not probe by provider id alone. A scoped miss is reported
    # uniformly, so the admin contract neither leaks nor crosses another owner.
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=owner_user_id,
        provider_id=provider_id,
        outcome="cross_owner_denied",
    )
    await db.commit()
    raise HTTPException(status.HTTP_404_NOT_FOUND, "managed AI provider not found")


def _deployment_managed_provider_query_owner(
    *,
    kind: str | None,
    ref: str | None,
) -> PlatformOwner:
    if kind is None or ref is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "owner is required")
    try:
        return PlatformOwner.model_validate({"kind": kind, "ref": ref})
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "owner is invalid",
        ) from exc


async def _assert_admin_target_owns_environment(
    db: AsyncSession,
    *,
    env: AgentEnvironment,
    target_clerk_id: str | None,
) -> UUID:
    if target_clerk_id is None:
        return env.user_id

    try:
        target = await _find_admin_owner(
            db,
            PlatformOwner(kind=PRINCIPAL_KIND_CLERK, ref=target_clerk_id),
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_404_NOT_FOUND:
            raise
        target = None
    if target is None or env.user_id != target.id:
        logger.warning(
            "admin_environment_owner_rejected target_clerk_id=%s env_id=%s owner_user_id=%s",
            target_clerk_id,
            env.id,
            env.user_id,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Agent environment is not owned by target user",
        )
    return target.id


async def _assert_admin_cleanup_target_owns_environment(
    db: AsyncSession,
    *,
    env: AgentEnvironment,
    target_clerk_id: str | None,
) -> UUID:
    """Verify retained ownership without requiring an active principal."""

    if target_clerk_id is None:
        return env.user_id

    target_user_id = await db.scalar(
        select(User.id).where(
            User.principal_kind == PRINCIPAL_KIND_CLERK,
            User.clerk_id == target_clerk_id,
        )
    )
    if target_user_id is None or env.user_id != target_user_id:
        logger.warning(
            "admin_environment_owner_rejected target_clerk_id=%s env_id=%s owner_user_id=%s",
            target_clerk_id,
            env.id,
            env.user_id,
        )
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Agent environment is not owned by target user",
        )
    return target_user_id


@router.post("/auth/keys", response_model=ApiKeyCreated)
async def admin_mint_api_key(
    body: AdminApiKeyCreate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> ApiKeyCreated:
    """Mint an api_key on behalf of a user identified by Clerk id.

    Used by upstream-SaaS batch tooling: each legacy deployment that
    didn't have live sync wired up needs a fresh api_key bound to a
    fresh env, but the migration script has no per-user Clerk JWT.

    User row is lazy-created if absent — handles the common entry
    path of a user whose first interaction with cloud-api is via
    SaaS-side admin calls. See `_resolve_or_create_user` for the
    safety model.
    """
    target = await _resolve_or_create_user(db, body.target_clerk_id)

    env_uuid: UUID | None = None
    if body.environment_id:
        try:
            env_uuid = UUID(body.environment_id)
        except (TypeError, ValueError) as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "environment_id is not a valid UUID"
            ) from e

    # `scopes=None` is full API permission access — same default as
    # user-self-mint via `POST /v1/auth/keys`. Callers may pass a
    # narrower permission list to lock the minted key down (e.g. ops
    # tooling that only needs to push sessions); the route doesn't
    # impose a ceiling.
    try:
        minted = await mint_api_key(
            db,
            user_id=target.id,
            label=body.label,
            scopes=body.scopes,
            environment_id=env_uuid,
            managed=body.managed,
            # Key row and its audit event must land in one transaction:
            # a key that exists without the caller learning its id is an
            # untrackable, unrevokable credential.
            commit=False,
        )
    except ValueError as e:
        # `mint_api_key` raises ValueError for cross-tenant
        # environment_id (env not owned by target user). Surface
        # as 403 — admin can't bypass the service-layer invariant.
        # Log so an operator debugging "why is the mint failing?"
        # can grep cloud-api logs directly without correlating with
        # the SaaS side.
        logger.warning(
            "admin_mint_rejected reason=cross_tenant_env target_clerk_id=%s env=%s",
            body.target_clerk_id,
            env_uuid,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(e)) from e

    api_key = minted.api_key
    logger.info(
        "admin_api_key_minted target_clerk_id=%s key_id=%s environment_id=%s managed=%s",
        body.target_clerk_id,
        api_key.id,
        api_key.environment_id,
        api_key.managed,
    )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="api_key.mint",
        resource_type="api_key",
        resource_id=str(api_key.id),
        environment_id=api_key.environment_id,
        target_user_id=target.id,
        source="api.admin",
        details={
            "label": api_key.label,
            "key_prefix": api_key.key_prefix,
            "managed": api_key.managed,
            "has_environment_binding": api_key.environment_id is not None,
            "scope_count": None if api_key.scopes is None else len(api_key.scopes),
        },
    )
    await db.commit()
    return ApiKeyCreated(
        id=str(api_key.id),
        label=api_key.label,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        expires_at=api_key.expires_at,
        revoked_at=api_key.revoked_at,
        raw_key=minted.raw_key,
    )


@router.delete("/auth/keys/{key_id}", response_model=ApiKeyRevokeResponse)
async def admin_revoke_api_key(
    key_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> ApiKeyRevokeResponse:
    """Revoke any user's api_key. Used by SaaS admin/account-deletion
    paths (which don't have the user's Clerk JWT) to close the
    orphan-key gap from the cross-PR audit."""
    api_key = (await db.execute(select(ApiKey).where(ApiKey.id == key_id))).scalar_one_or_none()
    if api_key is None:
        # 404 = idempotent success for the caller. The migration
        # script can re-run after a partial failure without
        # special-casing already-revoked keys.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "API key not found")

    if api_key.revoked_at is not None:
        # Already revoked — idempotent, return existing state.
        return ApiKeyRevokeResponse(status="revoked")

    api_key.revoked_at = datetime.now(UTC)
    await notify_sync_subscriptions_changed(db, [api_key.user_id])
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="api_key.revoke",
        resource_type="api_key",
        resource_id=str(api_key.id),
        environment_id=api_key.environment_id,
        target_user_id=api_key.user_id,
        source="api.admin",
        details={
            "label": api_key.label,
            "key_prefix": api_key.key_prefix,
            "managed": api_key.managed,
            "has_environment_binding": api_key.environment_id is not None,
        },
    )
    await db.commit()
    logger.info(
        "admin_api_key_revoked target_user_id=%s key_id=%s",
        api_key.user_id,
        api_key.id,
    )
    return ApiKeyRevokeResponse(status="revoked")


@router.get(
    "/ai-providers/{provider_id}",
    response_model=AdminDeploymentManagedAiProviderResponse,
    response_model_exclude_none=True,
)
async def admin_get_clawdi_managed_ai_provider(
    provider_id: str,
    owner_kind: Annotated[str | None, Query(alias="kind")] = None,
    owner_ref: Annotated[str | None, Query(alias="ref")] = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminDeploymentManagedAiProviderResponse:
    """Read one first-party managed provider within an explicit owner scope."""

    if not is_runtime_metadata_managed_provider_id(provider_id):
        raise HTTPException(
            status.HTTP_405_METHOD_NOT_ALLOWED,
            "Method Not Allowed",
            headers={"Allow": "PUT"},
        )
    owner = _deployment_managed_provider_query_owner(kind=owner_kind, ref=owner_ref)
    action = "ai_provider.managed.read"
    target = await _find_deployment_managed_provider_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action=action,
    )
    provider = await find_clawdi_managed_provider(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    if provider is None:
        await _raise_deployment_managed_provider_scope_denied(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
        )
    try:
        response = await _admin_deployment_managed_provider_response(
            db,
            provider=provider,
            owner=owner,
            target=target,
        )
    except HTTPException:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            provider_uuid=provider.id,
            outcome="failed",
        )
        await db.commit()
        raise
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    await db.commit()
    return response


@router.put(
    "/ai-providers/{provider_id}",
    response_model=AdminManagedAiProviderResponse | AdminDeploymentManagedAiProviderResponse,
)
async def admin_upsert_clawdi_managed_ai_provider(
    provider_id: str,
    body: AdminManagedAiProviderUpsert | AdminDeploymentManagedAiProviderUpsert,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminManagedAiProviderResponse | AdminDeploymentManagedAiProviderResponse:
    """Upsert the first-party managed AI provider for a target user.

    This intentionally does not expose a generic admin AI-provider write API.
    Hosted deploy orchestration can install either the fixed imperative
    provider or one deployment-scoped declarative provider and rotate its key.
    """
    if provider_id in V2_MANAGED_AI_PROVIDER_IDS:
        if not isinstance(body, AdminManagedAiProviderUpsert):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "target_clerk_id is required for fixed managed AI providers",
            )
        # Legacy route ids remain accepted during the cross-service rollout,
        # but fixed managed-provider writes and responses are canonical.
        target = await _resolve_or_create_user(db, body.target_clerk_id)
        await lock_ai_provider_owner(db, target.id)
        existing = await find_clawdi_managed_provider(
            db,
            owner_user_id=target.id,
            provider_id=V2_MANAGED_AI_PROVIDER_ID,
            include_archived=True,
        )
        previous_non_auth_signature = runtime_manifest_provider_non_auth_signature(existing)
        try:
            provider = await upsert_clawdi_managed_provider(
                db,
                user=target,
                provider_id=V2_MANAGED_AI_PROVIDER_ID,
                base_url=body.base_url,
                api_key=body.api_key.get_secret_value(),
                default_model=body.default_model,
                models=(
                    [model.model_dump(exclude_none=True) for model in body.models]
                    if body.models is not None
                    else None
                ),
                label=body.label,
                capabilities=body.capabilities,
            )
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e

        if previous_non_auth_signature != runtime_manifest_provider_non_auth_signature(provider):
            await queue_provider_runtime_manifest_changed(db, target.id, provider.provider_id)
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="ai_provider.managed.upsert",
            resource_type="ai_provider",
            resource_id=provider.provider_id,
            target_user_id=target.id,
            source="api.admin",
            details={
                "provider_id": provider.provider_id,
                "api_mode": MANAGED_AI_PROVIDER_API_MODE,
                "runtime_env_name": MANAGED_AI_PROVIDER_RUNTIME_ENV,
                "models": provider.models,
                "has_capabilities": body.capabilities is not None,
            },
        )
        await db.commit()
        await db.refresh(provider)
        logger.info(
            "admin_managed_ai_provider_upserted target_clerk_id=%s provider_id=%s",
            body.target_clerk_id,
            provider.provider_id,
        )
        return AdminManagedAiProviderResponse(
            owner_user_id=target.id,
            owner_clerk_id=target.clerk_id,
            provider_id=provider.provider_id,
            api_mode=provider.api_mode or "",
            runtime_env_name=provider.runtime_env_name or "",
            base_url=provider.base_url,
            models=provider.models,
            has_api_key=True,
        )

    if not is_v2_deployment_managed_provider_id(provider_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "managed AI provider not found")
    if not isinstance(body, AdminDeploymentManagedAiProviderUpsert):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "owner is required for deployment-scoped managed AI providers",
        )
    owner = body.owner
    target = await _resolve_or_create_admin_owner(db, owner)
    # This must precede the first provider/auth lookup in the upsert service.
    await lock_deployment_managed_provider_mutation(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    existing = await find_clawdi_managed_provider(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
        include_archived=True,
    )
    previous_non_auth_signature = runtime_manifest_provider_non_auth_signature(existing)
    try:
        provider = await upsert_clawdi_managed_provider(
            db,
            user=target,
            provider_id=provider_id,
            base_url=body.base_url,
            api_key=body.api_key.get_secret_value(),
            default_model=body.default_model,
            models=(
                [model.model_dump(exclude_none=True) for model in body.models]
                if body.models is not None
                else None
            ),
            label=body.label,
            capabilities=body.capabilities,
        )
    except ValueError as e:
        _record_deployment_managed_provider_audit(
            db,
            action="ai_provider.managed.upsert",
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            outcome="failed",
        )
        await db.commit()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e

    await db.flush()
    if previous_non_auth_signature != runtime_manifest_provider_non_auth_signature(provider):
        await queue_provider_runtime_manifest_changed(db, target.id, provider.provider_id)
    _record_deployment_managed_provider_audit(
        db,
        action="ai_provider.managed.upsert",
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider.provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    _record_deployment_managed_provider_audit(
        db,
        action="ai_provider.managed.credential.rotate",
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider.provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    await db.commit()
    await db.refresh(provider)
    logger.info(
        "admin_managed_ai_provider_upserted owner_kind=%s owner_ref=%s provider_id=%s",
        owner.kind,
        owner.ref,
        provider.provider_id,
    )
    return await _admin_deployment_managed_provider_response(
        db,
        provider=provider,
        owner=owner,
        target=target,
    )


@router.put(
    "/ai-providers/{provider_id}/runtime-metadata",
    response_model=AdminDeploymentManagedAiProviderResponse,
)
async def admin_replace_deployment_managed_ai_provider_metadata(
    provider_id: str,
    body: AdminDeploymentManagedAiProviderRuntimeMetadataReplace,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminDeploymentManagedAiProviderResponse:
    """Replace deployment-managed runtime metadata without changing auth state."""

    if not is_runtime_metadata_managed_provider_id(provider_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "managed AI provider not found")
    owner = body.owner
    action = "ai_provider.managed.runtime_metadata.replace"
    target = await _find_deployment_managed_provider_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action=action,
    )
    await lock_runtime_metadata_managed_provider_mutation(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    provider = await find_clawdi_managed_provider(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    if provider is None:
        await _raise_deployment_managed_provider_scope_denied(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
        )
    try:
        _require_managed_provider_contract(provider)
        changed = replace_managed_provider_runtime_metadata(
            provider,
            base_url=body.base_url,
            models=(
                [model.model_dump(exclude_none=True) for model in body.models]
                if body.models is not None
                else None
            ),
        )
    except (HTTPException, ValueError) as exc:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            provider_uuid=provider.id,
            outcome="failed",
        )
        await db.commit()
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if changed:
        await db.flush()
        await queue_provider_runtime_manifest_changed(db, target.id, provider_id)
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    await db.commit()
    await db.refresh(provider)
    return await _admin_deployment_managed_provider_response(
        db,
        provider=provider,
        owner=owner,
        target=target,
    )


@router.get(
    "/ai-providers/{provider_id}/removal-authority",
    response_model=AdminAiProviderRemovalAuthorityResponse,
)
async def admin_get_ai_provider_removal_authority(
    provider_id: str = Path(..., min_length=1, max_length=80),
    owner_kind: Annotated[str | None, Query(alias="kind")] = None,
    owner_ref: Annotated[str | None, Query(alias="ref")] = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminAiProviderRemovalAuthorityResponse:
    """Read the exact Cloud incarnation to which removal must compare-and-set."""

    owner = _deployment_managed_provider_query_owner(kind=owner_kind, ref=owner_ref)
    target = await _find_deployment_managed_provider_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action="ai_provider.removal_authority.read",
    )
    provider = (
        await db.execute(
            select(AiProvider)
            .where(
                AiProvider.owner_user_id == target.id,
                AiProvider.provider_id == provider_id,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    provider_status = (
        "not_found"
        if provider is None
        else "archived"
        if provider.archived_at is not None
        else "active"
    )
    return AdminAiProviderRemovalAuthorityResponse(
        status=provider_status,
        provider_id=provider_id,
        incarnation_token=_ai_provider_removal_incarnation_token(
            owner_user_id=target.id,
            provider_id=provider_id,
            provider=provider,
        ),
    )


@router.post(
    "/ai-providers/{provider_id}/archive",
    response_model=AdminAiProviderArchiveReceipt,
)
async def admin_archive_ai_provider(
    body: AdminAiProviderArchiveRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    provider_id: str = Path(..., min_length=1, max_length=80),
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminAiProviderArchiveReceipt:
    """Archive exactly the Cloud incarnation confirmed before Hosted Unset."""

    owner = body.owner
    action = "ai_provider.archive"
    target = await _find_deployment_managed_provider_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action=action,
    )
    request_hash = platform_request_hash(
        {
            "owner": owner.model_dump(mode="json"),
            "provider_id": provider_id,
            "expected_incarnation_token": body.expected_incarnation_token,
        }
    )
    replay_row = await lock_platform_idempotency(
        db,
        operation=_AI_PROVIDER_ARCHIVE_OPERATION,
        idempotency_key=idempotency_key,
    )
    if replay_row is not None:
        if replay_row.owner_user_id != target.id or replay_row.request_hash != request_hash:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Idempotency-Key was already used with a different archive request",
            )
        return AdminAiProviderArchiveReceipt.model_validate(read_platform_replay(replay_row).body)

    await lock_ai_provider_owner(db, target.id)
    provider = (
        await db.execute(
            select(AiProvider)
            .where(
                AiProvider.owner_user_id == target.id,
                AiProvider.provider_id == provider_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    current_token = _ai_provider_removal_incarnation_token(
        owner_user_id=target.id,
        provider_id=provider_id,
        provider=provider,
    )
    if current_token != body.expected_incarnation_token:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            provider_uuid=None if provider is None else provider.id,
            outcome="incarnation_mismatch",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "code": "provider_incarnation_mismatch",
                "message": "AI provider changed after removal was confirmed.",
            },
        )

    remote_revoke_status = "not_required"
    if provider is None:
        archive_status = "not_found"
    elif provider.archived_at is not None:
        archive_status = "already_archived"
    else:
        transition = await transition_ai_provider_auth(
            db,
            owner_user_id=target.id,
            provider=provider,
            auth_type=provider.auth_type,
            auth_ref=provider.auth_ref,
            auth_metadata=provider.auth_metadata,
            archive_provider=True,
        )
        archive_status = "archived"
        remote_revoke_status = transition.remote_revoke_status

    receipt = AdminAiProviderArchiveReceipt(
        status=archive_status,
        provider_id=provider_id,
        remote_revoke_status=remote_revoke_status,
    )
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=None if provider is None else provider.id,
        outcome=archive_status,
    )
    store_platform_response(
        db,
        operation=_AI_PROVIDER_ARCHIVE_OPERATION,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        owner_user_id=target.id,
        resource_type="ai_provider",
        resource_id=provider_id,
        response_status=status.HTTP_200_OK,
        response_body=receipt.model_dump(mode="json"),
    )
    await db.commit()
    return receipt


@router.post(
    "/ai-providers/{provider_id}/cleanup",
    response_model=AdminDeploymentManagedAiProviderCleanupReceipt,
)
async def admin_cleanup_deployment_managed_ai_provider(
    provider_id: str,
    body: AdminDeploymentManagedAiProviderCleanup,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminDeploymentManagedAiProviderCleanupReceipt:
    """Archive or prove one exact deployment-scoped managed provider."""

    if not is_v2_deployment_managed_provider_id(provider_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "managed AI provider not found")
    owner = body.owner
    action = "ai_provider.managed.cleanup"
    target, principal_cleanup_completed_at = await _find_deployment_managed_provider_cleanup_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action=action,
    )
    authority = (
        "active_owner" if principal_cleanup_completed_at is None else "completed_principal_cleanup"
    )
    try:
        result = await cleanup_deployment_managed_provider(
            db,
            owner_user_id=target.id,
            provider_id=provider_id,
            expected_provider_uuid=body.expected_provider_uuid,
            provisioning_discovery_key=body.provisioning_discovery_key,
            authority=authority,
        )
    except DeploymentManagedProviderCleanupMismatchError as exc:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            outcome="identity_mismatch",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Managed AI provider cleanup identity did not match",
        ) from exc
    except DeploymentManagedProviderCleanupInvariantError as exc:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            outcome="invariant_violation",
        )
        await db.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Managed AI provider cleanup invariant did not hold",
        ) from exc
    if result is None:
        await _raise_deployment_managed_provider_scope_denied(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
        )

    provider = result.provider
    if result.status == "archived":
        await queue_provider_runtime_manifest_changed(db, target.id, provider_id)
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=provider.id,
        outcome=result.status,
    )
    await db.commit()
    if provider.archived_at is None:
        raise RuntimeError("managed AI provider cleanup did not archive provider")
    return AdminDeploymentManagedAiProviderCleanupReceipt(
        status=result.status,
        authority=authority,
        owner=owner,
        provider_id=provider.provider_id,
        provider_uuid=provider.id,
        provisioning_discovery_key=body.provisioning_discovery_key,
        archived_at=provider.archived_at,
        principal_cleanup_completed_at=principal_cleanup_completed_at,
    )


@router.delete(
    "/ai-providers/{provider_id}",
    response_model=AiProviderDeleteResponse,
)
async def admin_delete_clawdi_managed_ai_provider(
    provider_id: str,
    owner_kind: Annotated[str | None, Query(alias="kind")] = None,
    owner_ref: Annotated[str | None, Query(alias="ref")] = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AiProviderDeleteResponse:
    """Archive one first-party managed provider within an explicit owner scope."""

    if not is_v2_deployment_managed_provider_id(provider_id):
        raise HTTPException(
            status.HTTP_405_METHOD_NOT_ALLOWED,
            "Method Not Allowed",
            headers={"Allow": "PUT"},
        )
    owner = _deployment_managed_provider_query_owner(kind=owner_kind, ref=owner_ref)
    action = "ai_provider.managed.delete"
    target = await _find_deployment_managed_provider_owner(
        db,
        owner=owner,
        provider_id=provider_id,
        action=action,
    )
    # Use the same transaction lock as PUT before checking archived state.
    await lock_deployment_managed_provider_mutation(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    provider = await find_clawdi_managed_provider(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    if provider is None:
        await _raise_deployment_managed_provider_scope_denied(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
        )
    try:
        _require_managed_provider_contract(provider)
    except HTTPException:
        _record_deployment_managed_provider_audit(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
            provider_uuid=provider.id,
            outcome="failed",
        )
        await db.commit()
        raise
    archived = await archive_clawdi_managed_provider(
        db,
        owner_user_id=target.id,
        provider_id=provider_id,
    )
    if archived is None:
        await _raise_deployment_managed_provider_scope_denied(
            db,
            action=action,
            owner=owner,
            owner_user_id=target.id,
            provider_id=provider_id,
        )
    await queue_provider_runtime_manifest_changed(db, target.id, provider_id)
    _record_deployment_managed_provider_audit(
        db,
        action=action,
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    _record_deployment_managed_provider_audit(
        db,
        action="ai_provider.managed.credential.archive",
        owner=owner,
        owner_user_id=target.id,
        provider_id=provider_id,
        provider_uuid=provider.id,
        outcome="success",
    )
    await db.commit()
    logger.info(
        "admin_managed_ai_provider_deleted owner_kind=%s owner_ref=%s provider_id=%s",
        owner.kind,
        owner.ref,
        provider_id,
    )
    return AiProviderDeleteResponse(status="deleted", provider_id=provider_id)


@router.get("/channels", response_model=list[AdminChannelResponse])
async def admin_list_channels(
    provider: str | None = None,
    visibility: AdminChannelVisibility | None = None,
    include_archived: bool = False,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> list[AdminChannelResponse]:
    if provider is not None and provider not in CHANNEL_PROVIDERS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unsupported provider")
    filters: list[ColumnElement[bool]] = []
    if provider is not None:
        filters.append(ChannelAccount.provider == provider)
    if visibility is not None:
        filters.append(ChannelAccount.visibility == visibility)
    if not include_archived:
        filters.append(ChannelAccount.archived_at.is_(None))

    result = await db.execute(
        select(ChannelAccount, User)
        .outerjoin(User, User.id == ChannelAccount.user_id)
        .where(*filters)
        .order_by(ChannelAccount.provider, ChannelAccount.visibility, ChannelAccount.name)
    )
    return [_admin_channel_response(account, owner) for account, owner in result.all()]


@router.post(
    "/channels",
    response_model=AdminChannelCreatedResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_create_channel(
    body: AdminChannelCreate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminChannelCreatedResponse:
    await validate_channel_account_config_urls(provider=body.provider, config=body.config)
    if body.provider == CHANNEL_PROVIDER_WHATSAPP and body.visibility == CHANNEL_VISIBILITY_PUBLIC:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Public WhatsApp accounts require a physical pairing session.",
        )
    if body.provider == CHANNEL_PROVIDER_DISCORD:
        if body.provider_token is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Discord channels require a bot token.",
            )
        validate_required_discord_interactions_config(body.config)
    target = (
        await _resolve_or_create_user(db, body.target_clerk_id)
        if body.target_clerk_id is not None
        else None
    )
    ciphertext, nonce = encrypt_optional_token(body.provider_token)
    webhook_secret = generate_webhook_secret()
    account_config = (
        discord_config_without_unverified_install_state(body.config)
        if body.provider == CHANNEL_PROVIDER_DISCORD
        else body.config
    )
    account = build_channel_account(
        owner_user_id=target.id if target is not None else None,
        provider=body.provider,
        name=body.name,
        visibility=body.visibility,
        encrypted_provider_token=ciphertext,
        provider_token_nonce=nonce,
        webhook_secret_hash=hash_token(webhook_secret),
        config=account_config,
    )
    db.add(account)
    try:
        await db.flush()
        if body.provider == CHANNEL_PROVIDER_TELEGRAM and body.provider_token:
            bot_username = await configure_telegram_provider_webhook(
                provider_token=body.provider_token,
                webhook_url=channel_webhook_url(account.id, body.provider),
                webhook_secret=webhook_secret,
            )
            if body.visibility == "public" and bot_username is None:
                raise HTTPException(
                    status.HTTP_502_BAD_GATEWAY,
                    "Telegram bot identity has no valid bot username",
                )
            if bot_username is not None:
                config = dict(account.config) if isinstance(account.config, dict) else {}
                config["bot_username"] = bot_username
                account.config = config
        await store_channel_secrets(db, account=account, secrets_by_name=body.secrets)
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="channel.account.create",
            resource_type="channel_account",
            resource_id=str(account.id),
            channel_account_id=account.id,
            target_user_id=target.id if target is not None else None,
            source="api.admin",
            details={
                "provider": account.provider,
                "visibility": account.visibility,
                "has_provider_credential": body.provider_token is not None,
                "secret_names": sorted((body.secrets or {}).keys()),
            },
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "channel name already exists for this provider and owner",
        ) from exc
    if (
        account.provider == CHANNEL_PROVIDER_DISCORD
        and account.visibility == CHANNEL_VISIBILITY_PUBLIC
    ):
        # Discord validates the PATCH with a signed PING, so the account and
        # public key must be committed and webhook-visible first. Bot-pool
        # eligibility remains false until the readiness marker commits below.
        await ensure_discord_application_identity_available(db, account=account)
        await configure_discord_application(account)
        await rearm_discord_command_reconciliation(db, account=account)
        await sync_channel_commands(
            account=account,
            use_configured_discord_guild=False,
        )
        config = dict(account.config) if isinstance(account.config, dict) else {}
        config["discord_interactions_configured"] = True
        account.config = config
        mark_discord_reserved_commands_current(account)
        await db.commit()
    await db.refresh(account)
    logger.info(
        "admin_channel_created channel_id=%s provider=%s visibility=%s owner_user_id=%s",
        account.id,
        account.provider,
        account.visibility,
        account.user_id,
    )
    return AdminChannelCreatedResponse(
        **_admin_channel_response(account, target).model_dump(),
        webhook_secret=webhook_secret,
    )


@whatsapp_pairing_router.post(
    "/channels/whatsapp/pairing-sessions",
    response_model=ChannelWhatsAppOnboardingSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def admin_start_platform_whatsapp_pairing(
    body: AdminPlatformWhatsAppPairingSessionCreate,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> ChannelWhatsAppOnboardingSessionResponse:
    _no_store(response)
    result = await start_platform_whatsapp_pairing(
        db,
        account_id=body.account_id,
        request_id=body.request_id,
        name=body.name,
        registry=get_active_whatsapp_sidecar_clients(),
    )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="channel.whatsapp.pairing.start",
        resource_type="channel_whatsapp_pairing_session",
        resource_id=str(result.id),
        channel_account_id=result.channel_account_id,
        source="api.admin",
        details={"account_id": str(body.account_id), "state": result.state},
    )
    await db.commit()
    return result


@whatsapp_pairing_router.get(
    "/channels/whatsapp/pairing-sessions/{session_id}",
    response_model=ChannelWhatsAppOnboardingSessionResponse,
)
async def admin_get_platform_whatsapp_pairing(
    session_id: UUID,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> ChannelWhatsAppOnboardingSessionResponse:
    _no_store(response)
    result = await get_platform_whatsapp_pairing(
        db, session_id=session_id, registry=get_active_whatsapp_sidecar_clients()
    )
    return result


@whatsapp_pairing_router.delete(
    "/channels/whatsapp/pairing-sessions/{session_id}",
    response_model=ChannelWhatsAppOnboardingSessionResponse,
)
async def admin_cancel_platform_whatsapp_pairing(
    session_id: UUID,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> ChannelWhatsAppOnboardingSessionResponse:
    _no_store(response)
    result = await cancel_platform_whatsapp_pairing(
        db, session_id=session_id, registry=get_active_whatsapp_sidecar_clients()
    )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="channel.whatsapp.pairing.cancel",
        resource_type="channel_whatsapp_pairing_session",
        resource_id=str(result.id),
        channel_account_id=result.channel_account_id,
        source="api.admin",
        details={"state": result.state},
    )
    await db.commit()
    return result


@router.get("/channels/{account_id}", response_model=AdminChannelResponse)
async def admin_get_channel(
    account_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminChannelResponse:
    account, owner = await _admin_get_channel_row(db, account_id=account_id, include_archived=True)
    return _admin_channel_response(account, owner)


@router.patch("/channels/{account_id}", response_model=AdminChannelResponse)
async def admin_update_channel(
    account_id: UUID,
    body: AdminChannelUpdate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminChannelResponse:
    account, owner = await _admin_get_channel_row(db, account_id=account_id)
    updates = set(body.model_fields_set)
    previous_status = account.status
    if "name" in updates:
        account.name = body.name or account.name
    if "status" in updates and body.status is not None:
        account.status = body.status
    if "visibility" in updates:
        if body.visibility != account.visibility:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Channel visibility cannot be changed in place; recreate the channel.",
            )
        updates.remove("visibility")
    current_telegram_bot_username = (
        normalize_telegram_bot_username(
            account.config.get("bot_username") if isinstance(account.config, dict) else None
        )
        if account.provider == CHANNEL_PROVIDER_TELEGRAM
        else None
    )
    telegram_bot_username = current_telegram_bot_username
    if account.provider == CHANNEL_PROVIDER_DISCORD and "config" in updates:
        require_unchanged_discord_application_identity(account, body.config)
    if (
        account.provider == CHANNEL_PROVIDER_DISCORD
        and "provider_token" in updates
        and body.provider_token is not None
    ):
        application_id = discord_application_id_from_config(account.config)
        if application_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Discord application identity is missing; recreate the channel.",
            )
        await verify_discord_application_token_identity(
            application_id=application_id,
            provider_token=body.provider_token,
            config=account.config,
        )
    if "provider_token" in updates:
        if account.provider == CHANNEL_PROVIDER_TELEGRAM and body.provider_token:
            if current_telegram_bot_username is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        "Telegram bot identity is missing; recreate the channel "
                        "to replace its token."
                    ),
                )
            telegram_bot_username = await get_telegram_bot_username(body.provider_token)
            if telegram_bot_username.casefold() != current_telegram_bot_username.casefold():
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Telegram provider token belongs to a different bot.",
                )
        ciphertext, nonce = encrypt_optional_token(body.provider_token)
        account.encrypted_provider_token = ciphertext
        account.provider_token_nonce = nonce
        if account.provider == CHANNEL_PROVIDER_DISCORD and body.provider_token is not None:
            await rearm_discord_command_reconciliation(db, account=account)
    if "config" in updates:
        await validate_channel_account_config_urls(provider=account.provider, config=body.config)
        account.config = (
            discord_config_without_unverified_install_state(body.config)
            if account.provider == CHANNEL_PROVIDER_DISCORD
            else body.config
        )
    elif account.provider == CHANNEL_PROVIDER_DISCORD and "provider_token" in updates:
        # A replacement token may belong to a different application. Force the
        # next pairing request through /applications/@me identity and install
        # capability verification before exposing install URLs or commands.
        account.config = discord_config_without_unverified_install_state(account.config)
    if account.provider == CHANNEL_PROVIDER_TELEGRAM and (
        "provider_token" in updates or "config" in updates
    ):
        config = dict(account.config) if isinstance(account.config, dict) else {}
        if "provider_token" in updates and body.provider_token is None:
            config.pop("bot_username", None)
        elif telegram_bot_username is not None:
            config["bot_username"] = telegram_bot_username
        account.config = config
    reconcile_public_discord = bool(
        account.provider == CHANNEL_PROVIDER_DISCORD
        and account.visibility == CHANNEL_VISIBILITY_PUBLIC
        and updates.intersection({"visibility", "provider_token", "config"})
    )
    if reconcile_public_discord:
        if not account.encrypted_provider_token or not account.provider_token_nonce:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Public Discord channels require a bot token.",
            )
        config_error = discord_interactions_config_error(account.config)
        if config_error is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=config_error)
        config = dict(account.config) if isinstance(account.config, dict) else {}
        config["discord_interactions_configured"] = False
        account.config = config
        await ensure_discord_application_identity_available(db, account=account)
    try:
        if "secrets" in updates:
            await upsert_channel_secrets(db, account=account, secrets_by_name=body.secrets)
        if account.status != previous_status and account.provider in RUNTIME_CHANNEL_PROVIDERS:
            rows = (
                await db.execute(
                    select(ChannelBotAgentLink.user_id, ChannelBotAgentLink.agent_id).where(
                        ChannelBotAgentLink.account_id == account.id,
                        ChannelBotAgentLink.status == BOT_AGENT_LINK_STATUS_ACTIVE,
                        ChannelBotAgentLink.archived_at.is_(None),
                    )
                )
            ).all()
            targets = [(user_id, agent_id) for user_id, agent_id in rows]
            await queue_runtime_manifests_changed(db, targets)
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="channel.account.update",
            resource_type="channel_account",
            resource_id=str(account.id),
            channel_account_id=account.id,
            target_user_id=account.user_id,
            source="api.admin",
            details=_admin_channel_update_audit_details(account, updates, body),
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "channel name already exists for this provider and owner",
        ) from exc
    if reconcile_public_discord:
        await configure_discord_application(account)
        await rearm_discord_command_reconciliation(db, account=account)
        await sync_channel_commands(
            account=account,
            use_configured_discord_guild=False,
        )
        config = dict(account.config) if isinstance(account.config, dict) else {}
        config["discord_interactions_configured"] = True
        account.config = config
        mark_discord_reserved_commands_current(account)
        await db.commit()
    await db.refresh(account)
    return _admin_channel_response(account, owner)


@router.post(
    "/channels/{account_id}/webhook-secret/rotate",
    response_model=AdminChannelWebhookSecretResponse,
)
async def admin_rotate_channel_webhook_secret(
    account_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminChannelWebhookSecretResponse:
    account, _owner = await _admin_get_channel_row(db, account_id=account_id)
    webhook_secret = generate_webhook_secret()
    account.webhook_secret_hash = hash_token(webhook_secret)
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="channel.webhook_secret.rotate",
        resource_type="channel_account",
        resource_id=str(account.id),
        channel_account_id=account.id,
        target_user_id=account.user_id,
        source="api.admin",
        details={"provider": account.provider},
    )
    await db.commit()
    logger.info("admin_channel_webhook_secret_rotated channel_id=%s", account.id)
    return AdminChannelWebhookSecretResponse(id=account.id, webhook_secret=webhook_secret)


@router.post("/channels/{account_id}/commands/sync", response_model=ChannelCommandSyncResponse)
async def admin_sync_channel_commands(
    account_id: UUID,
    body: ChannelCommandSyncRequest,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> ChannelCommandSyncResponse:
    account, _owner = await _admin_get_channel_row(db, account_id=account_id)
    commands = (
        [command.model_dump(exclude_none=True) for command in body.commands]
        if body.commands is not None
        else None
    )
    if account.provider == CHANNEL_PROVIDER_DISCORD and commands is None:
        await ensure_discord_application_identity_available(db, account=account)
        await configure_discord_application(account)
        await rearm_discord_command_reconciliation(db, account=account)
    synced = await sync_channel_commands(
        account=account,
        commands=commands,
        guild_id=body.guild_id,
    )
    if account.provider == CHANNEL_PROVIDER_DISCORD and commands is None:
        config = dict(account.config) if isinstance(account.config, dict) else {}
        config["discord_interactions_configured"] = True
        account.config = config
        if body.guild_id is None:
            mark_discord_reserved_commands_current(account)
        await db.commit()
    if account.provider == CHANNEL_PROVIDER_TELEGRAM:
        mark_telegram_reserved_commands_current(account)
        await db.commit()
    return ChannelCommandSyncResponse(provider=account.provider, commands=synced)


@router.delete("/channels/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_channel(
    account_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> None:
    account, _owner = await _admin_get_channel_row(db, account_id=account_id)
    active_links = list(
        (
            await db.execute(
                select(ChannelBotAgentLink).where(
                    ChannelBotAgentLink.account_id == account.id,
                    ChannelBotAgentLink.status == BOT_AGENT_LINK_STATUS_ACTIVE,
                    ChannelBotAgentLink.archived_at.is_(None),
                )
            )
        ).scalars()
    )
    await require_whatsapp_logout_for_archive(
        db,
        account=account,
        registry=get_active_whatsapp_sidecar_clients(),
    )
    await archive_channel_account(db, account=account)
    if account.provider in RUNTIME_CHANNEL_PROVIDERS:
        for link in active_links:
            await queue_environment_runtime_manifest_changed(
                db,
                link.user_id,
                link.agent_id,
            )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="channel.account.archive",
        resource_type="channel_account",
        resource_id=str(account.id),
        channel_account_id=account.id,
        target_user_id=account.user_id,
        source="api.admin",
        details={"provider": account.provider, "visibility": account.visibility},
    )
    await db.commit()
    logger.info("admin_channel_archived channel_id=%s", account.id)


async def _admin_register_environment(
    body: AdminEnvironmentCreate,
    db: AsyncSession,
    *,
    default_name: str | None = None,
) -> EnvironmentCreatedResponse:
    target = await _resolve_or_create_user(db, body.target_clerk_id)
    try:
        registered = await register_agent_environment(
            db,
            user_id=target.id,
            machine_id=body.machine_id,
            machine_name=body.machine_name,
            agent_type=body.agent_type,
            agent_version=body.agent_version,
            os_name=body.os_name,
            sort_order=await _next_environment_sort_order(db, target.id),
            environment_id=body.environment_id,
            registration_key=None
            if body.environment_id is not None
            else local_machine_registration_key(body.machine_id, body.agent_type),
            default_name=default_name,
            commit=False,
        )
        env = registered.env
        # Admin registration is a hosted/managed origin, including the
        # historical implicit Legacy V1 shape that had registration_key.
        clear_connected_agent_registration(env)
        await db.commit()
    except AgentEnvironmentIdConflict as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "concurrent registration race; retry the request",
        ) from None

    logger.info(
        "admin_environment_registered target_clerk_id=%s env_id=%s machine_id=%s explicit_id=%s",
        body.target_clerk_id,
        env.id,
        body.machine_id,
        body.environment_id is not None,
    )
    return EnvironmentCreatedResponse(id=str(env.id))


@router.post("/agents", response_model=EnvironmentCreatedResponse)
async def admin_register_agent(
    body: AdminAgentCreate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> EnvironmentCreatedResponse:
    return await _admin_register_environment(
        AdminEnvironmentCreate(
            target_clerk_id=body.target_clerk_id,
            environment_id=body.agent_id,
            machine_id=body.machine_id,
            machine_name=body.machine_name,
            agent_type=body.agent_type,
            agent_version=body.agent_version,
            os_name=body.os_name,
        ),
        db,
        default_name=body.default_name,
    )


@router.post(
    "/environments",
    response_model=EnvironmentCreatedResponse,
    deprecated=True,
)
async def admin_register_environment(
    body: AdminEnvironmentCreate,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> EnvironmentCreatedResponse:
    """Register an AgentEnvironment row on behalf of a target user.

    Migration tooling needs to seed env_id for legacy deployments
    where no per-user Clerk JWT is in project. The user-facing
    `POST /v1/environments` requires a Clerk-authed or Agent environment
    api_key request; this admin variant is gated by the shared
    `X-Admin-Key` header instead.

    If `environment_id` is supplied, it is the stable agent id and
    machine metadata is refreshed in place. If omitted, legacy callers
    remain idempotent through the local machine registration key.

    User row is lazy-created if absent — see `_resolve_or_create_user`.
    """
    return await _admin_register_environment(body, db)


async def _admin_delete_environment(
    environment_id: UUID,
    target_clerk_id: str | None,
    db: AsyncSession,
) -> None:
    env = (
        await db.execute(select(AgentEnvironment).where(AgentEnvironment.id == environment_id))
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    target_user_id = await _assert_admin_cleanup_target_owns_environment(
        db,
        env=env,
        target_clerk_id=target_clerk_id,
    )
    await lock_ai_provider_owner(db, target_user_id)
    env = (
        await db.execute(
            select(AgentEnvironment)
            .where(
                AgentEnvironment.id == environment_id,
                AgentEnvironment.user_id == target_user_id,
                active_agent_filter(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="agent_environment.delete",
        resource_type="agent_environment",
        resource_id=str(env.id),
        environment_id=env.id,
        target_user_id=target_user_id,
        source="api.admin",
        details={
            "agent_type": env.agent_type,
            "machine_id": env.machine_id,
            "explicit_identity": env.registration_key is None,
        },
    )
    await queue_environment_runtime_manifest_changed(db, env.user_id, environment_id)
    try:
        await archive_agent_and_project(db, agent=env)
    except AgentLifecycleBoundaryError:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Agent Project ownership could not be proven; no resources were archived.",
        ) from None
    await db.commit()
    logger.info(
        "admin_environment_deleted target_clerk_id=%s env_id=%s",
        target_clerk_id,
        environment_id,
    )


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_agent(
    agent_id: UUID,
    target_clerk_id: str | None = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> None:
    await _admin_delete_environment(agent_id, target_clerk_id, db)


@router.delete(
    "/environments/{environment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    deprecated=True,
)
async def admin_delete_environment(
    environment_id: UUID,
    target_clerk_id: str | None = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> None:
    """Delete an AgentEnvironment row on behalf of first-party hosted infra.

    Sessions keep their history via ON DELETE SET NULL, matching the
    user-facing `/v1/environments/{id}` semantics. Unlike the dashboard route,
    first-party cleanup may delete explicit-identity rows.
    """
    await _admin_delete_environment(environment_id, target_clerk_id, db)


async def _next_environment_sort_order(db: AsyncSession, user_id: UUID) -> int:
    value = (
        await db.execute(
            select(func.coalesce(func.max(AgentEnvironment.sort_order), -1) + 1).where(
                AgentEnvironment.user_id == user_id
            )
        )
    ).scalar_one()
    return int(value)


async def _admin_upsert_runtime_state(
    environment_id: UUID,
    body: AdminRuntimeStateUpsert,
    db: AsyncSession,
) -> AdminRuntimeStateResponse:
    env = (
        await db.execute(select(AgentEnvironment).where(AgentEnvironment.id == environment_id))
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent environment not found")
    target_user_id = await _assert_admin_target_owns_environment(
        db,
        env=env,
        target_clerk_id=body.target_clerk_id,
    )
    await lock_ai_provider_owner(db, target_user_id)
    try:
        await lock_runtime_state_write_fence(
            db,
            environment_id=environment_id,
            owner_id=target_user_id,
            deployment_id=body.deployment_id,
        )
    except RuntimeObservationProtocolError as exc:
        await db.rollback()
        raise HTTPException(exc.status_code, exc.detail()) from exc
    env = (
        await db.execute(
            select(AgentEnvironment)
            .where(
                AgentEnvironment.id == environment_id,
                AgentEnvironment.user_id == target_user_id,
                active_agent_filter(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent environment not found")

    new_workspace_skill_keys: set[str] = (
        set(body.skills.entries) if body.skills is not None else set()
    )
    await assert_agent_workspace_skill_write_compatible(
        db,
        agent_id=environment_id,
        skill_keys=new_workspace_skill_keys,
    )

    # Lock the parent before the optional child row so concurrent first creates
    # serialize even when there is no HostedRuntimeState row to lock yet.
    state = (
        await db.execute(
            select(HostedRuntimeState)
            .where(HostedRuntimeState.environment_id == environment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    existing_state = state
    await require_custom_provider_cli(
        db,
        owner_user_id=target_user_id,
        provider_ids=[
            provider_id
            for runtime in body.runtimes.values()
            for provider_id in runtime.provider_ids
        ],
        cli_package_spec=body.cli_package_spec,
    )
    secret_rows = await load_hosted_runtime_secrets_for_update(
        db,
        environment_id=environment_id,
    )
    secret_values_changed = hosted_runtime_secret_values_changed(
        secret_rows,
        body.secret_values,
    )
    old_skill_ids = enabled_runtime_manifest_skill_ids(state.skills if state is not None else None)
    new_skill_ids = enabled_runtime_manifest_skill_ids(
        body.skills.model_dump(mode="json", exclude_none=True) if body.skills is not None else None
    )
    await lock_runtime_manifest_skill_reservations(
        db,
        user_id=target_user_id,
        project_id=env.default_project_id,
        skill_ids=old_skill_ids | new_skill_ids,
    )
    previous_generation = state.generation if state is not None else None
    previous_apply_generation = state.apply_generation if state is not None else None
    if existing_state is not None and body.generation < existing_state.generation:
        current_generation = existing_state.generation
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": "stale_generation",
                "current_generation": current_generation,
            },
        )
    try:
        apply_generation = resolve_runtime_apply_generation_update(
            current=previous_apply_generation,
            requested=body.apply_generation,
            explicitly_set="apply_generation" in body.model_fields_set,
        )
    except RuntimeApplyGenerationUpdateError as exc:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "code": exc.code,
                "current_apply_generation": exc.current_apply_generation,
            },
        ) from exc
    desired_state = _runtime_state_values(body, apply_generation=apply_generation)
    changed_fields = _runtime_state_changed_fields(existing_state, desired_state)
    try:
        await reconcile_runtime_oauth_claims(
            db,
            owner_user_id=target_user_id,
            environment_id=environment_id,
            runtimes=desired_state["runtimes"],
        )
    except OAuthCredentialClaimConflict as exc:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if secret_values_changed:
        changed_fields.append("secretValues")
    if body.plugin_bundle is not None:
        from app.services.plugin_bundle import initialize_plugin_bundle

        try:
            await initialize_plugin_bundle(
                db, agent=env, bundle=body.plugin_bundle, runtime=next(iter(body.runtimes))
            )
        except HTTPException:
            await db.rollback()
            raise
    if existing_state is not None and body.generation == existing_state.generation:
        current_generation = existing_state.generation
        material_changes = [
            field for field in changed_fields if field not in {"generation", "apply_generation"}
        ]
        if material_changes:
            await db.rollback()
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "code": "generation_conflict",
                    "current_generation": current_generation,
                },
            )
        if not changed_fields:
            await db.commit()
            return AdminRuntimeStateResponse(
                environment_id=environment_id,
                deployment_id=body.deployment_id,
                instance_id=body.instance_id,
                generation=body.generation,
                apply_generation=apply_generation,
            )
    if state is None:
        state = HostedRuntimeState(environment_id=environment_id)
        db.add(state)

    for field, value in desired_state.items():
        setattr(state, field, value)
    if secret_values_changed:
        await db.flush()
        await sync_hosted_runtime_secret_values(
            db,
            environment_id=environment_id,
            rows=secret_rows,
            desired=body.secret_values,
        )
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="hosted_runtime_state.upsert",
        resource_type="hosted_runtime_state",
        resource_id=str(environment_id),
        environment_id=environment_id,
        target_user_id=target_user_id,
        source="api.admin",
        details={
            "deployment_id": body.deployment_id,
            "instance_id": body.instance_id,
            "generation": body.generation,
            "previous_generation": previous_generation,
            "apply_generation": apply_generation,
            "previous_apply_generation": previous_apply_generation,
            "cli_package_spec": body.cli_package_spec,
            "locale": body.locale.model_dump(),
            "enabled_runtimes": _enabled_runtime_names(desired_state["runtimes"]),
            "has_mcp": body.mcp is not None,
            "has_skills": body.skills is not None,
            "has_tools": True,
            "has_secret_values": bool(body.secret_values),
            "secret_refs": sorted(body.secret_values),
            "changed_fields": changed_fields,
        },
    )
    if changed_fields:
        await queue_runtime_manifest_changed(db, env.user_id, environment_id)
    await db.commit()
    logger.info(
        "admin_runtime_state_upserted target_clerk_id=%s environment_id=%s "
        "deployment_id=%s generation=%s",
        body.target_clerk_id,
        environment_id,
        body.deployment_id,
        body.generation,
    )
    return AdminRuntimeStateResponse(
        environment_id=environment_id,
        deployment_id=body.deployment_id,
        instance_id=body.instance_id,
        generation=body.generation,
        apply_generation=apply_generation,
    )


@router.get(
    "/agents/{agent_id}/runtime-state",
    response_model=RuntimeSourceAuthorityResponse,
    include_in_schema=False,
)
async def admin_get_runtime_source_authority(
    agent_id: UUID,
    owner: Annotated[PlatformOwner, Query()],
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> RuntimeSourceAuthorityResponse:
    target = await _find_admin_owner(db, owner)
    try:
        authority = await load_runtime_source_authority(
            environment_id=agent_id,
            owner_user_id=target.id,
        )
    except RuntimeSourceNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Runtime source not found") from None
    except RuntimeSourceError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Runtime source is invalid",
        ) from None
    return RuntimeSourceAuthorityResponse(
        environmentId=authority.environment_id,
        deploymentId=authority.deployment_id,
        instanceId=authority.instance_id,
        sourceRevision=authority.source_revision,
        etag=authority.etag,
    )


@router.put(
    "/agents/{agent_id}/runtime-state",
    response_model=AdminRuntimeStateResponse,
)
async def admin_upsert_agent_runtime_state(
    agent_id: UUID,
    body: AdminRuntimeStateUpsert,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminRuntimeStateResponse:
    return await _admin_upsert_runtime_state(agent_id, body, db)


@router.put(
    "/environments/{environment_id}/runtime-state",
    response_model=AdminRuntimeStateResponse,
    deprecated=True,
)
async def admin_upsert_runtime_state(
    environment_id: UUID,
    body: AdminRuntimeStateUpsert,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> AdminRuntimeStateResponse:
    return await _admin_upsert_runtime_state(environment_id, body, db)


# --- Runtime secret recovery (OPS-07, WINDOWS #200) -------------------------------------------
#
# A lost or rotated vault key leaves rows whose ciphertext fails authentication, and the manifest
# fails closed on them -- correctly. These two routes are how an operator with no database shell
# heals that: list what is unreadable, then delete or replace exactly those refs. Neither route
# ever returns, logs or audits a value, and recover never reads back a row it replaces: the only
# decrypt attempt against such a row is the authentication probe that classifies it unreadable.
# Canonical `/agents/...` spellings only -- a new route does not acquire a deprecated alias.


async def _runtime_secret_agent(db: AsyncSession, agent_id: UUID) -> AgentEnvironment:
    env = (
        await db.execute(select(AgentEnvironment).where(AgentEnvironment.id == agent_id))
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent environment not found")
    return env


@router.get(
    "/agents/{agent_id}/runtime-secrets/health",
    response_model=AdminRuntimeSecretHealthResponse,
)
async def admin_runtime_secret_health(
    agent_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminRuntimeSecretHealthResponse:
    await _runtime_secret_agent(db, agent_id)
    rows = list(
        (
            await db.execute(
                select(HostedRuntimeSecret)
                .where(HostedRuntimeSecret.environment_id == agent_id)
                .order_by(HostedRuntimeSecret.secret_ref)
            )
        ).scalars()
    )
    # Plan 06-25: the agent's live companion atoms too, named `companion:<installation>:<name>`.
    # A row whose key version the keyring no longer holds is listed as unreadable, not a 500.
    atoms = await load_live_companion_secrets(db, environment_id=agent_id, for_update=False)
    return AdminRuntimeSecretHealthResponse(
        agent_id=agent_id,
        secrets=[
            *(
                AdminRuntimeSecretHealthItem(
                    secret_ref=row.secret_ref,
                    key_version=row.key_version,
                    readable=is_readable(row),
                )
                for row in rows
            ),
            *(
                AdminRuntimeSecretHealthItem(
                    secret_ref=companion_secret_ref(row.installation_id, row.secret_name),
                    key_version=row.key_version,
                    readable=is_readable(row),
                )
                for row in atoms
            ),
        ],
    )


def _companion_recovery_request(body: AdminRuntimeSecretRecoverRequest) -> CompanionRecoveryRequest:
    return CompanionRecoveryRequest(
        regenerate=tuple(
            CompanionRegenerate(installation_id=item.installation_id, secret_name=item.secret_name)
            for item in body.companion_regenerate
        ),
        reprovision=tuple(
            CompanionReprovision(installation_id=item.installation_id, step=item.step)
            for item in body.companion_reprovision
        ),
        abandon_backups=tuple(body.abandon_backups),
    )


def _reprovisioned_payload(items: list[CompanionReprovision]) -> list[AdminCompanionReprovision]:
    return [
        AdminCompanionReprovision(
            installation_id=item.installation_id,
            step=cast(Literal["media", "temporal", "edge", "backup-key"], item.step),
        )
        for item in items
    ]


@router.post(
    "/agents/{agent_id}/runtime-secrets/recover",
    response_model=AdminRuntimeSecretRecoverResponse,
)
async def admin_recover_runtime_secrets(
    agent_id: UUID,
    body: AdminRuntimeSecretRecoverRequest,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminRuntimeSecretRecoverResponse:
    env = await _runtime_secret_agent(db, agent_id)
    owner_id = env.user_id
    named_refs = sorted({*body.delete_refs, *body.replace})

    # Which named refs were unreadable when this request began. Read WITHOUT a lock, so a
    # concurrent recover holding the rows cannot block it: after we win the lock below, a ref
    # that was unreadable here but is readable (or gone) there was healed by that concurrent
    # call, and is reported as already recovered rather than refused.
    rows_at_start = {
        row.secret_ref: row
        for row in (
            await db.execute(
                select(HostedRuntimeSecret).where(
                    HostedRuntimeSecret.environment_id == agent_id,
                    HostedRuntimeSecret.secret_ref.in_(named_refs),
                )
            )
        ).scalars()
    }
    unreadable_at_start = {ref for ref, row in rows_at_start.items() if not is_readable(row)}
    # Refused before anything changes -- including a companion reprovision below, which acts at a
    # provider before the lock is taken. The same checks run again under the lock.
    missing_at_start = sorted(ref for ref in named_refs if ref not in rows_at_start)
    if missing_at_start:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "runtime_secret_not_found", "secret_refs": missing_at_start},
        )
    readable_at_start = sorted(ref for ref in named_refs if ref not in unreadable_at_start)
    if readable_at_start:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "runtime_secret_readable", "secret_refs": readable_at_start},
        )
    try:
        companion_plan = await plan_companion_recovery(
            db, environment_id=agent_id, request=_companion_recovery_request(body)
        )
    except CompanionRecoveryRefused as refused:
        raise HTTPException(refused.status_code, detail=refused.detail) from None

    # A provider credential is revoked and re-minted BEFORE any row lock: each step stores its
    # new atoms through its own session, which a lock held here would block.
    reprovision = ReprovisionResult(done=[], handle_updates={})
    if companion_plan.request.reprovision:
        try:
            reprovision = await reprovision_companion_credentials(db, companion_plan)
        except CompanionReprovisionFailed as failed:
            record_control_plane_audit(
                db,
                actor_type="admin",
                action="hosted_runtime_secrets.recover",
                resource_type="hosted_runtime_secret",
                resource_id=str(agent_id),
                environment_id=agent_id,
                target_user_id=owner_id,
                source="api.admin",
                details={
                    "reprovisioned": [
                        f"{item.installation_id}:{item.step}" for item in failed.done
                    ],
                    "reprovision_failed": f"{failed.failed.installation_id}:{failed.failed.step}",
                    "reason": failed.reason,
                },
            )
            if failed.done:
                await queue_runtime_manifest_changed(db, owner_id, agent_id)
            await db.commit()
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": "companion_reprovision_failed",
                    "installation_id": str(failed.failed.installation_id),
                    "step": failed.failed.step,
                    "reason": failed.reason,
                    "reprovisioned": [
                        {"installation_id": str(item.installation_id), "step": item.step}
                        for item in failed.done
                    ],
                },
            ) from None

    # FOR UPDATE on every secret row of the agent: this is what serializes concurrent recovers
    # (and a recover against a concurrent runtime-state upsert, which takes the same lock).
    rows = await load_hosted_runtime_secrets_for_update(db, environment_id=agent_id)
    rows_by_ref = {row.secret_ref: row for row in rows}
    unreadable_now = {row.secret_ref for row in rows if not is_readable(row)}
    already_recovered = sorted(
        ref for ref in named_refs if ref in unreadable_at_start and ref not in unreadable_now
    )
    missing = sorted(
        ref for ref in named_refs if ref not in rows_by_ref and ref not in already_recovered
    )
    if missing:
        await db.rollback()
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            detail={"code": "runtime_secret_not_found", "secret_refs": missing},
        )
    readable = sorted(
        ref for ref in named_refs if ref not in unreadable_now and ref not in already_recovered
    )
    if readable:
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "runtime_secret_readable", "secret_refs": readable},
        )
    companion = await recover_companion_secrets(
        db,
        environment_id=agent_id,
        plan=companion_plan,
        handle_updates=reprovision.handle_updates,
    )

    deleted: list[str] = []
    for secret_ref in body.delete_refs:
        if secret_ref in already_recovered:
            continue
        await db.delete(rows_by_ref[secret_ref])
        deleted.append(secret_ref)
    replaced: list[str] = []
    for secret_ref, value in sorted(body.replace.items()):
        if secret_ref in already_recovered:
            continue
        row = rows_by_ref[secret_ref]
        row.encrypted_value, row.nonce, row.key_version = encrypt_with_active_key(
            value.get_secret_value()
        )
        replaced.append(secret_ref)

    already_recovered = sorted({*already_recovered, *companion.already_recovered})
    details: dict[str, object] = {
        "deleted": sorted(deleted),
        "replaced": sorted(replaced),
        "already_recovered": already_recovered,
    }
    # Companion actions are named only when asked for, so a hosted-only recovery audits exactly
    # what it did before plan 06-25. Refs and installation ids only -- never a value.
    if companion.regenerated:
        details["regenerated"] = companion.regenerated
    if reprovision.done:
        details["reprovisioned"] = [
            f"{item.installation_id}:{item.step}" for item in reprovision.done
        ]
    if companion.abandoned:
        details["abandoned_backups"] = [
            {
                "installation_id": str(item.installation_id),
                "abandoned_stanza": item.abandoned_stanza,
                "abandoned_repo_path": item.abandoned_repo_path,
            }
            for item in companion.abandoned
        ]
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="hosted_runtime_secrets.recover",
        resource_type="hosted_runtime_secret",
        resource_id=str(agent_id),
        environment_id=agent_id,
        target_user_id=owner_id,
        source="api.admin",
        details=details,
    )
    if deleted or replaced or companion.changed or reprovision.done:
        await queue_runtime_manifest_changed(db, owner_id, agent_id)
    await db.commit()
    logger.info(
        "admin_runtime_secrets_recovered environment_id=%s deleted=%d replaced=%d "
        "already_recovered=%d regenerated=%d reprovisioned=%d abandoned_backups=%d",
        agent_id,
        len(deleted),
        len(replaced),
        len(already_recovered),
        len(companion.regenerated),
        len(reprovision.done),
        len(companion.abandoned),
    )
    return AdminRuntimeSecretRecoverResponse(
        agent_id=agent_id,
        deleted=sorted(deleted),
        replaced=sorted(replaced),
        already_recovered=already_recovered,
        remaining_unreadable=sorted(
            {*(unreadable_now - {*deleted, *replaced}), *companion.remaining_unreadable}
        ),
        regenerated=companion.regenerated,
        reprovisioned=_reprovisioned_payload(reprovision.done),
        abandoned_backups=[
            AdminCompanionAbandonedBackup(
                installation_id=item.installation_id,
                abandoned_stanza=item.abandoned_stanza,
                abandoned_repo_path=item.abandoned_repo_path,
                stanza=item.stanza,
                repo_path=item.repo_path,
            )
            for item in companion.abandoned
        ],
    )


def _vault_keyring_unavailable() -> HTTPException:
    # Names the setting, never a key.
    return HTTPException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "vault_keyring_unavailable",
            "message": "VAULT_ACTIVE_KEY_VERSION is not usable: add its key to "
            "VAULT_ENCRYPTION_KEYS (see docs/runbooks/vault-key-rotation.md).",
        },
    )


@router.post(
    "/agents/{agent_id}/runtime-secrets/rotate",
    response_model=AdminRuntimeSecretRotateResponse,
)
async def admin_rotate_runtime_secrets(
    agent_id: UUID,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminRuntimeSecretRotateResponse:
    """Re-encrypt one agent's readable versioned secrets under the active key (OPS-07, D-11).

    Incident response only (D-13): nothing schedules this. The response and the audit event
    carry refs and the active version -- never a value. Rotating changes each secret's source
    identity, so the runtime re-converges on its next manifest and a companion may restart
    (D-12).
    """
    env = await _runtime_secret_agent(db, agent_id)
    try:
        active_key_version()
    except RuntimeError:
        raise _vault_keyring_unavailable() from None
    outcome = await rotate_agent_secrets(db, environment_id=agent_id)
    details = {
        "active_key_version": outcome.active_key_version,
        "reencrypted": outcome.reencrypted,
        "already_current": outcome.already_current,
        "unreadable": outcome.unreadable,
    }
    record_control_plane_audit(
        db,
        actor_type="admin",
        action="hosted_runtime_secrets.rotate",
        resource_type="hosted_runtime_secret",
        resource_id=str(agent_id),
        environment_id=agent_id,
        target_user_id=env.user_id,
        source="api.admin",
        details=details,
    )
    if outcome.reencrypted:
        await queue_runtime_manifest_changed(db, env.user_id, agent_id)
    await db.commit()
    logger.info(
        "admin_runtime_secrets_rotated environment_id=%s active_key_version=%s reencrypted=%d "
        "already_current=%d unreadable=%d",
        agent_id,
        outcome.active_key_version,
        len(outcome.reencrypted),
        len(outcome.already_current),
        len(outcome.unreadable),
    )
    return AdminRuntimeSecretRotateResponse(
        agent_id=agent_id,
        active_key_version=outcome.active_key_version,
        reencrypted=outcome.reencrypted,
        already_current=outcome.already_current,
        unreadable=outcome.unreadable,
    )


# ---------------------------------------------------------------------------
# Companion PITR rehearsals (plan 06-16, D-16, OPS-03). The operator's runner asks for a bundle,
# restores into scratch, asserts on rows and posts the result here.
# ---------------------------------------------------------------------------


def _rehearsal_refused(exc: RehearsalError) -> HTTPException:
    return HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message})


@router.post(
    "/companions/installations/{installation_id}/rehearsals",
    response_model=AdminCompanionRehearsalBundle,
    status_code=status.HTTP_201_CREATED,
)
async def admin_plan_companion_rehearsal(
    installation_id: UUID,
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminCompanionRehearsalBundle:
    """Plan one PITR rehearsal: a target between two markers and a one-hour read-only key."""
    _no_store(response)
    try:
        bundle = await plan_rehearsal(db, installation_id)
    except RehearsalError as exc:
        raise _rehearsal_refused(exc) from None
    return AdminCompanionRehearsalBundle(
        rehearsal_id=bundle.rehearsal_id,
        installation_id=bundle.installation_id,
        stanza=bundle.stanza,
        repo_path=bundle.repo_path,
        s3_endpoint=bundle.s3_endpoint,
        s3_region=bundle.s3_region,
        bucket=bundle.bucket,
        read_key_id=bundle.read_key_id,
        read_key=SecretStr(bundle.read_key),
        cipher_pass=SecretStr(bundle.cipher_pass),
        target_time=bundle.target_time,
        expect_present_token=bundle.expect_present_token,
        expect_absent_token=bundle.expect_absent_token,
        expect_survivor_token=bundle.expect_survivor_token,
        expires_at=bundle.expires_at,
    )


@router.post(
    "/companions/rehearsals/{rehearsal_id}/result",
    response_model=AdminCompanionRehearsalRecorded,
)
async def admin_record_companion_rehearsal(
    rehearsal_id: UUID,
    body: AdminCompanionRehearsalResult,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
) -> AdminCompanionRehearsalRecorded:
    """Record a pending rehearsal's outcome, revoke its key and audit it; a second result is 409."""
    try:
        recorded = await record_rehearsal_result(
            db,
            rehearsal_id,
            passed=body.passed,
            present_found=body.present_found,
            absent_found=body.absent_found,
            schema_survived=body.schema_survived,
            restored_to=body.restored_to,
            detail=body.detail,
        )
    except RehearsalError as exc:
        raise _rehearsal_refused(exc) from None
    return AdminCompanionRehearsalRecorded(
        rehearsal_id=recorded.rehearsal_id,
        installation_id=recorded.installation_id,
        target_time=recorded.target_time,
        outcome="passed" if recorded.outcome == "passed" else "failed",
        reason=recorded.reason,
        reason_text=recorded.reason_text,
        key_revoked=recorded.key_revoked,
    )


def _billing_reconcile_sessionmaker(
    db: AsyncSession = Depends(get_session),
) -> async_sessionmaker[AsyncSession]:
    """Worker sessions on the request session's bind: the engine in a deployment, so the pass
    runs on its own connections exactly as the daily worker does."""
    return async_sessionmaker(
        bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


@router.post(
    "/companions/billing-reconcile",
    response_model=AdminBillingReconcileRunResponse,
)
async def admin_run_companion_billing_reconcile(
    response: Response,
    _: None = Depends(require_admin_api_key),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(_billing_reconcile_sessionmaker),
) -> AdminBillingReconcileRunResponse:
    """Run one companion billing reconciliation pass now (INT-4) and answer its counts.

    The same pass the daily worker runs: it records findings and may set a local
    `billing_stopped_at` to the hosted stop, and moves no money. Unconfigured `/v2` billing is a
    `skipped` pass, not an error.
    """
    _no_store(response)
    result = await CompanionBillingReconcileWorker(sessionmaker).run_once()
    async with sessionmaker() as db:
        record_control_plane_audit(
            db,
            actor_type="admin",
            action="companion.billing_reconcile_requested",
            resource_type="companion_billing_reconcile",
            source="api.admin",
            details={
                "skipped": result.skipped,
                "owners_scanned": result.owners_scanned,
                "mismatches": result.mismatches,
                "corrections": result.corrections,
            },
        )
        await db.commit()
    return AdminBillingReconcileRunResponse(
        skipped=result.skipped,
        owners_scanned=result.owners_scanned,
        owners_failed=result.owners_failed,
        lines_seen=result.lines_seen,
        mismatches=result.mismatches,
        corrections=result.corrections,
        findings_resolved=result.findings_resolved,
    )


@router.get(
    "/companions/billing-reconcile/findings",
    response_model=AdminBillingReconcileFindingList,
)
async def admin_list_companion_billing_findings(
    response: Response,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_session),
    user_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> AdminBillingReconcileFindingList:
    """The open companion billing reconciliation findings, most recently seen first (INT-4)."""
    _no_store(response)
    query = (
        select(BillingReconcileFinding, CompanionInstallation.user_id)
        .join(
            CompanionInstallation,
            CompanionInstallation.id == BillingReconcileFinding.installation_id,
        )
        .where(BillingReconcileFinding.resolved_at.is_(None))
        .order_by(BillingReconcileFinding.last_seen.desc(), BillingReconcileFinding.id)
        .limit(limit)
    )
    if user_id is not None:
        query = query.where(CompanionInstallation.user_id == user_id)
    rows = (await db.execute(query)).all()
    return AdminBillingReconcileFindingList(
        items=[
            AdminBillingReconcileFinding(
                finding_id=finding.id,
                user_id=owner_id,
                installation_id=finding.installation_id,
                line_item_id=finding.line_item_id,
                kind=cast(BillingReconcileFindingKind, finding.kind),
                local_value=finding.local_value,
                hosted_value=finding.hosted_value,
                first_seen=finding.first_seen,
                last_seen=finding.last_seen,
            )
            for finding, owner_id in rows
        ]
    )


async def _admin_delete_runtime_state(
    environment_id: UUID,
    target_clerk_id: str | None,
    db: AsyncSession,
) -> None:
    env = (
        await db.execute(select(AgentEnvironment).where(AgentEnvironment.id == environment_id))
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent environment not found")
    target_user_id = await _assert_admin_cleanup_target_owns_environment(
        db,
        env=env,
        target_clerk_id=target_clerk_id,
    )
    await lock_ai_provider_owner(db, target_user_id)
    env = (
        await db.execute(
            select(AgentEnvironment)
            .where(
                AgentEnvironment.id == environment_id,
                AgentEnvironment.user_id == target_user_id,
                active_agent_filter(),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if env is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Agent environment not found")

    state = (
        await db.execute(
            select(HostedRuntimeState)
            .where(HostedRuntimeState.environment_id == environment_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    details: dict[str, object] = {"existed": state is not None}
    if state is not None:
        details.update(
            {
                "deployment_id": state.deployment_id,
                "instance_id": state.instance_id,
                "generation": state.generation,
                "cli_package_spec": state.cli_package_spec,
                "enabled_runtimes": _enabled_runtime_names(state.runtimes),
                "has_mcp": state.mcp is not None,
                "has_skills": state.skills is not None,
                "has_tools": state.tools is not None,
            }
        )
        await release_runtime_oauth_claims(
            db,
            owner_user_id=env.user_id,
            environment_id=environment_id,
        )
        await db.delete(state)
        await queue_runtime_manifest_changed(db, env.user_id, environment_id)

    record_control_plane_audit(
        db,
        actor_type="admin",
        action="hosted_runtime_state.delete",
        resource_type="hosted_runtime_state",
        resource_id=str(environment_id),
        environment_id=environment_id,
        target_user_id=target_user_id,
        source="api.admin",
        details=details,
    )
    await db.commit()
    logger.info(
        "admin_runtime_state_deleted target_clerk_id=%s environment_id=%s existed=%s",
        target_clerk_id,
        environment_id,
        state is not None,
    )


@router.delete(
    "/agents/{agent_id}/runtime-state",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def admin_delete_agent_runtime_state(
    agent_id: UUID,
    target_clerk_id: str | None = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> None:
    await _admin_delete_runtime_state(agent_id, target_clerk_id, db)


@router.delete(
    "/environments/{environment_id}/runtime-state",
    status_code=status.HTTP_204_NO_CONTENT,
    deprecated=True,
)
async def admin_delete_runtime_state(
    environment_id: UUID,
    target_clerk_id: str | None = None,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> None:
    await _admin_delete_runtime_state(environment_id, target_clerk_id, db)


async def _admin_get_channel_row(
    db: AsyncSession,
    *,
    account_id: UUID,
    include_archived: bool = False,
) -> tuple[ChannelAccount, User | None]:
    filters = [ChannelAccount.id == account_id]
    if not include_archived:
        filters.append(ChannelAccount.archived_at.is_(None))
    result = await db.execute(
        select(ChannelAccount, User)
        .outerjoin(User, User.id == ChannelAccount.user_id)
        .where(*filters)
    )
    row = result.one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "channel not found")
    account, owner = row
    return account, owner


def _admin_channel_response(account: ChannelAccount, owner: User | None) -> AdminChannelResponse:
    return AdminChannelResponse(
        id=account.id,
        owner_user_id=account.user_id,
        owner_clerk_id=owner.clerk_id if owner is not None else None,
        provider=account.provider,
        name=account.name,
        status=account.status,
        visibility=cast(AdminChannelVisibility, account.visibility),
        has_provider_token=bool(account.encrypted_provider_token and account.provider_token_nonce),
        webhook_url=channel_webhook_url(account.id, account.provider),
        config=account.config,
        archived_at=account.archived_at,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


def _enabled_runtime_names(runtimes: Mapping[str, JsonValue]) -> list[str]:
    return sorted(
        name
        for name, value in runtimes.items()
        if isinstance(value, dict) and value.get("enabled") is True
    )


def _runtime_state_values(
    body: AdminRuntimeStateUpsert,
    *,
    apply_generation: int | None = None,
) -> dict[str, Any]:
    def optional_wire_value(field: str) -> Any:
        value = getattr(body, field)
        if value is None:
            return None
        return value.model_dump(exclude_none=True, exclude_unset=True, mode="json")

    return {
        "deployment_id": body.deployment_id,
        "instance_id": body.instance_id,
        "generation": body.generation,
        "apply_generation": apply_generation,
        "cli_package_spec": body.cli_package_spec,
        "locale": body.locale.model_dump(mode="json"),
        "system": body.system.model_dump(exclude_none=True, mode="json"),
        "egress_engine": optional_wire_value("egress_engine"),
        "companions": optional_wire_value("companions"),
        "runtimes": {
            name: runtime.model_dump(exclude_none=True, mode="json")
            for name, runtime in body.runtimes.items()
        },
        "live_sync": body.live_sync.model_dump(mode="json"),
        "recovery": body.recovery.model_dump(mode="json"),
        "egress_profiles": optional_wire_value("egress_profiles"),
        "mcp": optional_wire_value("mcp"),
        "skills": optional_wire_value("skills"),
        "tools": body.tools.model_dump(exclude_none=True, exclude_unset=True, mode="json"),
    }


def _runtime_state_changed_fields(
    state: HostedRuntimeState | None,
    desired_state: dict[str, Any],
) -> list[str]:
    return [
        field
        for field, value in desired_state.items()
        if (state is None and (field != "apply_generation" or value is not None))
        or (state is not None and getattr(state, field) != value)
    ]


def _admin_channel_update_audit_details(
    account: ChannelAccount,
    updates: set[str],
    body: AdminChannelUpdate,
) -> dict[str, object]:
    changed_fields = sorted(updates)
    details: dict[str, object] = {
        "provider": account.provider,
        "visibility": account.visibility,
        "changed_fields": changed_fields,
    }
    if "status" in updates and body.status is not None:
        details["status"] = body.status
    if "provider_token" in updates:
        details["provider_credential_changed"] = True
    if "secrets" in updates:
        details["secret_names"] = sorted((body.secrets or {}).keys())
    return details


@router.post(
    "/platform/workload-clients",
    status_code=status.HTTP_201_CREATED,
    response_model=ProviderEnvironmentVerifierAccess,
)
async def register_workload_client(
    body: AdminWorkloadClientBootstrap,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> ProviderEnvironmentVerifierAccess:
    return await bootstrap_workload_client(
        db,
        body=body,
        idempotency_key=idempotency_key,
        request_id=str(request.state.request_id),
    )


@router.get(
    "/platform/workload-clients/{client_id}/provider-environment-verifier",
    response_model=ProviderEnvironmentVerifierAccess,
)
async def inspect_native_environment_verifier(
    client_id: str,
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> ProviderEnvironmentVerifierAccess:
    return await inspect_verifier_access(db, client_id)


@router.put(
    "/platform/workload-clients/{client_id}/provider-environment-verifier",
    response_model=ProviderEnvironmentVerifierAccess,
)
async def configure_native_environment_verifier(
    client_id: str,
    body: ProviderEnvironmentVerifierGrant,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
) -> ProviderEnvironmentVerifierAccess:
    return await update_verifier_access(
        db,
        client_id=client_id,
        body=body,
        idempotency_key=idempotency_key,
        request_id=str(request.state.request_id),
    )


@router.post("/platform/signing-keys", status_code=201, response_model=AdminWorkloadSignerReceipt)
async def bootstrap_signing_key(
    body: AdminWorkloadSignerBootstrap,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=200),
    _: None = Depends(require_admin_api_key),
    db: AsyncSession = Depends(get_control_session),
    resolver: PlatformWorkloadKeyResolver = Depends(get_platform_workload_key_resolver),
) -> AdminWorkloadSignerReceipt:
    return await register_workload_signer(
        db,
        body=body,
        idempotency_key=idempotency_key,
        request_id=str(request.state.request_id),
        resolver=resolver,
    )
