"""Schemas for admin endpoints (`/v1/admin/*`).

These run behind the `X-Admin-Key` header gate (require_admin_api_key)
and are used by SaaS batch tooling + ops-side scripts. Kept in a
separate file so they don't pollute user-facing schemas.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    field_serializer,
    field_validator,
    model_validator,
)

from app.schemas.ai_provider import AiProviderAuth, AiProviderModel
from app.schemas.platform import PlatformOwner
from app.schemas.runtime import (
    HostedEgressEngine,
    HostedEgressProfiles,
    HostedRuntimeCompanions,
    HostedRuntimeDesiredState,
    HostedRuntimeLiveSync,
    HostedRuntimeLocale,
    HostedRuntimeMcp,
    HostedRuntimeRecovery,
    HostedRuntimeSecretValues,
    HostedRuntimeSkills,
    HostedRuntimeSystem,
    HostedRuntimeTools,
    is_canonical_secret_ref,
    validate_clawdi_cli_package_spec,
    validate_hosted_runtime_secret_values,
)


class AdminWorkloadClientBootstrap(BaseModel):
    """Register a public assertion key with runtime projection authority only."""

    model_config = ConfigDict(extra="forbid")
    client_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    assertion_kid: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    assertion_algorithm: Literal["RS256", "ES256"]
    public_jwk: dict[str, JsonValue] = Field(max_length=10)
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("reason")
    @classmethod
    def nonempty_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


AdminChannelProvider = Literal["telegram", "discord", "whatsapp"]
AdminChannelVisibility = Literal["private", "public"]
AdminChannelStatus = Literal["active", "disabled"]
_SUPPORTED_HOSTED_RUNTIMES = {"hermes", "openclaw"}
_ADMIN_CLERK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_admin_clerk_id(value: str) -> str:
    if value != value.strip() or not _ADMIN_CLERK_ID_RE.fullmatch(value):
        raise ValueError("target_clerk_id must be a stable Clerk identifier")
    return value


AdminClerkId = Annotated[
    str,
    Field(min_length=1, max_length=200),
    AfterValidator(_validate_admin_clerk_id),
]


class AdminEnvironmentCreate(BaseModel):
    """Body for `POST /v1/admin/environments`. Mirrors the
    user-facing EnvironmentCreate but takes target_clerk_id
    instead of relying on auth context to resolve the user.

    If `environment_id` is set, it is the caller-owned stable agent id.
    Otherwise this uses the legacy self-managed registration key derived from
    `(user, machine_id, agent_type)` for idempotent retries.
    """

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId
    environment_id: UUID | None = None
    machine_id: str
    machine_name: str
    agent_type: str
    agent_version: str | None = None
    os_name: str = "linux"


class AdminAgentCreate(BaseModel):
    """Body for `POST /v1/admin/agents`.

    Agent-first alias of `AdminEnvironmentCreate`; `agent_id` maps to the
    legacy `environment_id` field consumed by the shared handler.
    """

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId
    agent_id: UUID | None = None
    machine_id: str
    machine_name: str
    default_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="Canonical Agent name supplied by the owning control plane.",
    )
    agent_type: str
    agent_version: str | None = None
    os_name: str = "linux"


class AdminApiKeyCreate(BaseModel):
    """Body for `POST /v1/admin/auth/keys` — mint an api_key on
    behalf of a user identified by Clerk id. The route resolves
    `target_clerk_id` to the internal `User.id` and then calls the
    existing `mint_api_key` service, preserving its env-ownership
    invariant.

    `environment_id` is optional — if set, the minted key is bound
    to that env (deploy-key semantics). If null, the key is unbound.

    `scopes` is optional — same API-permission semantics as the user-facing
    `ApiKeyCreate`: `None` means full account access (the default
    for both user-self-mint and admin-mint). Pass an explicit list
    to narrow the minted key for ops tooling that doesn't need
    everything.
    """

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId
    label: str
    environment_id: str | None = None
    scopes: list[str] | None = None
    managed: bool = False


class AdminPrincipalSuspensionUpdate(BaseModel):
    """Set or clear the platform-owned Clerk principal fence."""

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId
    suspended: bool
    reason: str = Field(min_length=1, max_length=191)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if value != value.strip() or not value.isprintable():
            raise ValueError("reason must be a printable canonical value")
        return value


class AdminPrincipalSuspensionResponse(BaseModel):
    target_clerk_id: AdminClerkId
    suspended: bool
    suspended_at: datetime | None
    changed: bool


class AdminRuntimeStateUpsert(BaseModel):
    """Hosted runtime desired state written by the SaaS deploy orchestrator.

    This is deployment-level state only. Native channel credentials and channel
    links are owned by `/v1/channels/*` and must not be embedded here.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    plugin_bundle: Literal["sui"] | None = None
    target_clerk_id: str | None = None
    deployment_id: str = Field(min_length=1, max_length=200)
    instance_id: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=0)
    apply_generation: int | None = Field(default=None, ge=1)
    cli_package_spec: str = Field(min_length=1, max_length=200)
    locale: HostedRuntimeLocale
    system: HostedRuntimeSystem
    egress_engine: HostedEgressEngine | None = None
    companions: HostedRuntimeCompanions | None = None
    runtimes: dict[str, HostedRuntimeDesiredState]
    live_sync: HostedRuntimeLiveSync
    recovery: HostedRuntimeRecovery
    egress_profiles: HostedEgressProfiles | None = None
    mcp: HostedRuntimeMcp | None = None
    skills: HostedRuntimeSkills | None = None
    tools: HostedRuntimeTools
    secret_values: HostedRuntimeSecretValues = Field(alias="secretValues")

    @field_validator("cli_package_spec")
    @classmethod
    def _validate_cli_package_spec(cls, value: str) -> str:
        return validate_clawdi_cli_package_spec(value)

    @field_validator("secret_values")
    @classmethod
    def _validate_secret_values(cls, value: HostedRuntimeSecretValues) -> HostedRuntimeSecretValues:
        return validate_hosted_runtime_secret_values(value)

    @field_validator("runtimes")
    @classmethod
    def _validate_runtimes(
        cls,
        value: dict[str, HostedRuntimeDesiredState],
    ) -> dict[str, HostedRuntimeDesiredState]:
        if not value:
            raise ValueError("runtimes cannot be empty")
        if "channels" in value:
            raise ValueError("channels are not runtime desired state")
        unknown = sorted(set(value) - _SUPPORTED_HOSTED_RUNTIMES)
        if unknown:
            raise ValueError(f"unsupported runtime desired state: {', '.join(unknown)}")
        if len(value) != 1:
            raise ValueError("runtimes must contain exactly one enabled runtime")
        return value


class AdminRuntimeStateResponse(BaseModel):
    environment_id: UUID
    deployment_id: str
    instance_id: str
    generation: int
    apply_generation: int | None = None


class AdminRuntimeSecretHealthItem(BaseModel):
    """One stored runtime secret, described without its value."""

    secret_ref: str
    key_version: str
    readable: bool


class AdminRuntimeSecretHealthResponse(BaseModel):
    agent_id: UUID
    secrets: list[AdminRuntimeSecretHealthItem]


class AdminCompanionRegenerate(BaseModel):
    """One platform-generated companion atom to regenerate without reading it (plan 06-25)."""

    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    secret_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")


class AdminCompanionReprovision(BaseModel):
    """One install step whose provider credential is revoked and re-minted (plan 06-25)."""

    model_config = ConfigDict(extra="forbid")

    installation_id: UUID
    step: Literal["media", "temporal", "edge", "backup-key"]


class AdminRuntimeSecretRecoverRequest(BaseModel):
    """Delete or replace NAMED runtime secrets the vault key can no longer read.

    Only currently-unreadable refs are accepted; the route refuses a readable one with 409, so
    this cannot be used as a quiet way around the audited runtime-state upsert. Plan 06-25 adds
    the companion actions -- ``companion_regenerate``, ``companion_reprovision`` and
    ``abandon_backups`` -- under the same rule: each is refused unless what it replaces is
    unreadable.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    delete_refs: list[str] = Field(default_factory=list, max_length=128)
    replace: dict[str, SecretStr] = Field(default_factory=dict)
    companion_regenerate: list[AdminCompanionRegenerate] = Field(
        default_factory=list, max_length=64
    )
    companion_reprovision: list[AdminCompanionReprovision] = Field(
        default_factory=list, max_length=16
    )
    abandon_backups: list[UUID] = Field(default_factory=list, max_length=16)

    @field_validator("delete_refs")
    @classmethod
    def _validate_delete_refs(cls, value: list[str]) -> list[str]:
        for secret_ref in value:
            if len(secret_ref) > 1000 or not is_canonical_secret_ref(secret_ref):
                raise ValueError("delete_refs must be canonical secret:// references")
        return sorted(set(value))

    @field_validator("replace")
    @classmethod
    def _validate_replace(cls, value: HostedRuntimeSecretValues) -> HostedRuntimeSecretValues:
        return validate_hosted_runtime_secret_values(value)

    @model_validator(mode="after")
    def _validate_request(self) -> AdminRuntimeSecretRecoverRequest:
        if not (
            self.delete_refs
            or self.replace
            or self.companion_regenerate
            or self.companion_reprovision
            or self.abandon_backups
        ):
            raise ValueError(
                "recover requires at least one of delete_refs, replace, companion_regenerate, "
                "companion_reprovision or abandon_backups"
            )
        overlap = sorted(set(self.delete_refs) & set(self.replace))
        if overlap:
            raise ValueError("a secret ref cannot be both deleted and replaced")
        regenerate = [
            (item.installation_id, item.secret_name) for item in self.companion_regenerate
        ]
        reprovision = [(item.installation_id, item.step) for item in self.companion_reprovision]
        if (
            len(set(regenerate)) != len(regenerate)
            or len(set(reprovision)) != len(reprovision)
            or len(set(self.abandon_backups)) != len(self.abandon_backups)
        ):
            raise ValueError("a companion recovery action is named more than once")
        return self


class AdminCompanionAbandonedBackup(BaseModel):
    """A repository the platform can no longer restore, and the one that replaced it."""

    installation_id: UUID
    abandoned_stanza: str
    abandoned_repo_path: str
    stanza: str
    repo_path: str
    restorable_by_platform: Literal[False] = False


class AdminRuntimeSecretRecoverResponse(BaseModel):
    agent_id: UUID
    deleted: list[str]
    replaced: list[str]
    already_recovered: list[str]
    remaining_unreadable: list[str]
    # Plan 06-25's companion actions: refs as `companion:<installation_id>:<secret_name>`.
    regenerated: list[str] = Field(default_factory=list)
    reprovisioned: list[AdminCompanionReprovision] = Field(default_factory=list)
    abandoned_backups: list[AdminCompanionAbandonedBackup] = Field(default_factory=list)


class AdminRuntimeSecretRotateResponse(BaseModel):
    """One agent's versioned secrets after a rotation, by ref -- never a value (plan 06-25).

    Hosted runtime secrets are named by their ``secret://`` ref; companion installation atoms as
    ``companion:<installation_id>:<secret_name>``.
    """

    agent_id: UUID
    active_key_version: str
    reencrypted: list[str]
    already_current: list[str]
    unreadable: list[str]


class AdminManagedAiProviderUpsert(BaseModel):
    """Create or rotate the first-party managed AI provider for a user."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    target_clerk_id: str
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr = Field(min_length=1)
    default_model: str | None = Field(default=None, max_length=300)
    models: list[AiProviderModel] | None = None
    label: str | None = Field(default=None, max_length=200)
    capabilities: dict[str, Any] | None = None


class AdminManagedAiProviderResponse(BaseModel):
    owner_user_id: UUID
    owner_clerk_id: str | None
    provider_id: str
    api_mode: str
    runtime_env_name: str
    base_url: str
    models: list[dict[str, Any]] | None = None
    has_api_key: bool


class AdminDeploymentManagedAiProviderUpsert(BaseModel):
    """Create or rotate one deployment-scoped first-party managed provider."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    owner: PlatformOwner
    base_url: str = Field(min_length=1, max_length=1000)
    api_key: SecretStr = Field(min_length=1)
    default_model: str | None = Field(default=None, max_length=300)
    models: list[AiProviderModel] | None = None
    label: str | None = Field(default=None, max_length=200)
    capabilities: dict[str, Any] | None = None


class AdminDeploymentManagedAiProviderRuntimeMetadataReplace(BaseModel):
    """Replace runtime metadata without changing managed credentials."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    owner: PlatformOwner
    base_url: str = Field(min_length=1, max_length=1000)
    models: list[AiProviderModel] | None


class AdminDeploymentManagedAiProviderCleanup(BaseModel):
    """Archive or prove one exact deployment-scoped managed provider."""

    model_config = ConfigDict(extra="forbid")

    owner: PlatformOwner
    expected_provider_uuid: UUID
    provisioning_discovery_key: str = Field(min_length=1, max_length=191)

    @field_validator("provisioning_discovery_key")
    @classmethod
    def _validate_discovery_key(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("provisioning_discovery_key must be canonical")
        return value


class AdminAiProviderArchiveRequest(BaseModel):
    """Archive one owner-bound provider after Hosted retracts its references."""

    model_config = ConfigDict(extra="forbid")

    owner: PlatformOwner
    expected_incarnation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdminAiProviderRemovalAuthorityResponse(BaseModel):
    """Opaque Cloud CAS authority for one owner-bound provider id."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "archived", "not_found"]
    provider_id: str = Field(min_length=1, max_length=80)
    incarnation_token: str = Field(pattern=r"^[0-9a-f]{64}$")


class AdminAiProviderArchiveReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["archived", "already_archived", "not_found"]
    provider_id: str = Field(min_length=1, max_length=80)
    remote_revoke_status: Literal["pending", "not_required"] = "not_required"


class AdminDeploymentManagedAiProviderCleanupReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["archived", "already_archived"]
    authority: Literal["active_owner", "completed_principal_cleanup"]
    owner: PlatformOwner
    provider_id: str
    provider_uuid: UUID
    provisioning_discovery_key: str
    archived_at: datetime
    principal_cleanup_completed_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_authority_receipt(self) -> AdminDeploymentManagedAiProviderCleanupReceipt:
        completed = self.principal_cleanup_completed_at is not None
        if completed != (self.authority == "completed_principal_cleanup"):
            raise ValueError("cleanup authority receipt is inconsistent")
        if completed and self.status != "already_archived":
            raise ValueError("completed principal cleanup can only prove an archived provider")
        return self


class AdminDeploymentManagedAiProviderResponse(BaseModel):
    id: UUID
    owner: PlatformOwner
    owner_user_id: UUID
    owner_clerk_id: str | None
    provider_id: str
    scope: Literal["account_global"]
    type: Literal["custom_openai_compatible"]
    label: str
    api_mode: str
    auth: AiProviderAuth
    managed_by: Literal["clawdi"]
    runtime_env_name: str
    base_url: str
    capabilities: dict[str, Any] | None = None
    models: list[dict[str, Any]] | None = None
    has_api_key: bool


class AdminChannelCreate(BaseModel):
    """Create a provider bot account through the admin control plane.

    Private accounts require a target tenant owner. Public accounts are
    platform inventory and reject a target tenant owner.
    """

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId | None = None
    provider: AdminChannelProvider
    name: str = Field(min_length=1, max_length=120)
    visibility: AdminChannelVisibility = "public"
    provider_token: str | None = Field(default=None, min_length=1, max_length=2000)
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @field_validator("secrets")
    @classmethod
    def _validate_secrets(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _clean_channel_secret_values(value)

    @model_validator(mode="after")
    def _validate_ownership(self) -> AdminChannelCreate:
        if self.visibility == "private" and self.target_clerk_id is None:
            raise ValueError("private channels require target_clerk_id")
        if self.visibility == "public" and self.target_clerk_id is not None:
            raise ValueError("public channels must not specify target_clerk_id")
        return self


class AdminPlatformWhatsAppPairingSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: UUID
    request_id: UUID
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped


class AdminChannelUpdate(BaseModel):
    """Patch provider bot metadata and credentials.

    Omitted fields are left unchanged. Passing `provider_token: null` clears the
    provider token; passing `config: null` clears bot config.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    status: AdminChannelStatus | None = None
    visibility: AdminChannelVisibility | None = None
    provider_token: str | None = Field(default=None, min_length=1, max_length=2000)
    config: dict[str, Any] | None = None
    secrets: dict[str, str] | None = None

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("name cannot be blank")
        return stripped

    @field_validator("secrets")
    @classmethod
    def _validate_secrets(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return _clean_channel_secret_values(value)


class AdminChannelResponse(BaseModel):
    id: UUID
    owner_user_id: UUID | None
    owner_clerk_id: str | None
    provider: str
    name: str
    status: str
    visibility: AdminChannelVisibility
    has_provider_token: bool
    webhook_url: str
    config: dict[str, Any] | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime | None = None


class AdminChannelCreatedResponse(AdminChannelResponse):
    webhook_secret: str


class AdminChannelWebhookSecretResponse(BaseModel):
    id: UUID
    webhook_secret: str


class AdminAppSettingUpsert(BaseModel):
    """Replace one registered global setting as a single JSON value."""

    model_config = ConfigDict(extra="forbid", strict=True)

    value: JsonValue


class AdminAppSettingResponse(BaseModel):
    key: str
    value: JsonValue
    description: str
    created_at: datetime
    updated_at: datetime


class AdminAppSettingListResponse(BaseModel):
    items: list[AdminAppSettingResponse]


class AdminCompanionRehearsalBundle(BaseModel):
    """What the operator's PITR rehearsal runner needs (plan 06-16).

    ``read_key`` and ``cipher_pass`` are ``SecretStr`` so no repr, log line or validation error
    ever renders them; the JSON body the runner reads carries them in plain text, because it
    cannot restore without them. The route answers ``Cache-Control: no-store``.
    """

    rehearsal_id: UUID
    installation_id: UUID
    stanza: str
    repo_path: str
    s3_endpoint: str
    s3_region: str
    bucket: str
    read_key_id: str
    read_key: SecretStr
    cipher_pass: SecretStr
    # ``YYYY-MM-DDTHH:MM:SS.ffffff+00:00``, exactly as pgBackRest's --target is given it.
    target_time: str
    expect_present_token: str
    expect_absent_token: str
    # The oldest known marker: present in the restore iff the iforge_rehearsal schema survived
    # every Postiz start since it was written (research assumption A4).
    expect_survivor_token: str
    expires_at: datetime

    @field_serializer("read_key", "cipher_pass", when_used="json")
    def _reveal_to_runner(self, value: SecretStr) -> str:
        return value.get_secret_value()


class AdminCompanionRehearsalResult(BaseModel):
    """The runner's report of what the restored rows said."""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    passed: bool
    present_found: bool
    absent_found: bool
    # Whether the iforge_rehearsal schema survived in the restore (research assumption A4);
    # ``None`` when the runner never got far enough to look.
    schema_survived: bool | None
    restored_to: datetime | None = Field(default=None, strict=False)
    detail: str | None = Field(default=None, max_length=1000)


class AdminCompanionRehearsalRecorded(BaseModel):
    rehearsal_id: UUID
    installation_id: UUID
    target_time: str
    outcome: Literal["passed", "failed"]
    reason: str | None
    reason_text: str | None
    key_revoked: bool


def _clean_channel_secret_values(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    cleaned: dict[str, str] = {}
    for key, secret in value.items():
        name = key.strip()
        if not name or len(name) > 80 or not name.replace("_", "").isalnum():
            raise ValueError("secret names must be alphanumeric or underscore")
        if not secret:
            raise ValueError("secret values cannot be blank")
        cleaned[name] = secret
    return cleaned


class AdminWorkloadSignerBootstrap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kid: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    algorithm: Literal["RS256", "ES256"]
    public_jwk: dict[str, JsonValue] = Field(max_length=10)
    not_before: AwareDatetime
    expires_at: AwareDatetime
    reason: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def valid_window(self) -> AdminWorkloadSignerBootstrap:
        if self.expires_at <= self.not_before or not self.reason.strip():
            raise ValueError("Invalid validity window or reason")
        return self


class AdminWorkloadSignerReceipt(BaseModel):
    kid: str
    algorithm: str
    public_key_ref: str
    not_before: datetime
    expires_at: datetime
    status: Literal["active"] = "active"
