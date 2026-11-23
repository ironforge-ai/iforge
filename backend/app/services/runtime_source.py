from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, TypeGuard
from urllib.parse import quote
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import JsonValue, TypeAdapter, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.core.companion_catalog import catalog_entry
from app.core.companion_registry import (
    COMPANION_REGISTRY,
    companion_kinds_at_or_below,
    companion_min_cli_version,
)
from app.models.agent_plugin import AgentPluginInstallation
from app.models.ai_provider import AiProvider, AiProviderAuthPayload
from app.models.channel import (
    BOT_AGENT_LINK_STATUS_ACTIVE,
    CHANNEL_PROVIDER_DISCORD,
    CHANNEL_PROVIDER_TELEGRAM,
    CHANNEL_PROVIDER_WHATSAPP,
    CHANNEL_STATUS_ACTIVE,
    ChannelAccount,
    ChannelAgentCredential,
    ChannelBotAgentLink,
    ChannelWhatsAppAuthCert,
)
from app.models.companion_installation import (
    COMPANION_INSTALLATION_STATUS_COMPLETE,
    COMPANION_INSTALLATION_STATUS_RUNNING,
    COMPANION_OPERATION_UNINSTALL,
    HOST_REMOVAL_UNCONFIRMED_KEY,
    REMOVED_REPORTED_AT_KEY,
    CompanionInstallation,
    CompanionInstallationSecret,
    serving_installation_filter,
    uninstall_host_side_settled,
)
from app.models.hosted_runtime import (
    HostedRuntimeConfigObservation,
    HostedRuntimeSecret,
    HostedRuntimeState,
)
from app.models.project import Project
from app.models.session import AgentEnvironment
from app.models.skill import Skill
from app.schemas.ai_provider import AiProviderModel
from app.schemas.native_provider import native_provider
from app.schemas.plugin_catalog import RESERVED_AGENT_PLUGIN_NAMES
from app.schemas.runtime import (
    POSTIZ_MAX_RETIRED_INSTALLATIONS,
    HostedAgentPlugins,
    HostedCodexProviderProjection,
    HostedEgressEngine,
    HostedEgressProfiles,
    HostedRuntimeCompanions,
    HostedRuntimeLiveSync,
    HostedRuntimeLocale,
    HostedRuntimeMcp,
    HostedRuntimeName,
    HostedRuntimePlatformMcpServer,
    HostedRuntimeRecovery,
    HostedRuntimeRemoteMcpServer,
    HostedRuntimeSystem,
    HostedRuntimeTools,
    PersistedHostedRuntimeSkills,
    is_canonical_secret_ref,
    validate_clawdi_cli_package_spec,
    validate_hosted_runtime_desired_state,
)
from app.schemas.runtime_observed import HostedRuntimeObservedV2
from app.services.ai_provider_capabilities import effective_provider_api_mode
from app.services.channels import (
    HOSTED_RUNTIME_SINGLE_ACCOUNT_PROVIDERS,
    channel_runtime_account_key,
    channel_runtime_placeholder_token,
    hosted_agent_provider_link_limit_detail,
)
from app.services.companion_postiz import (
    POSTIZ_COMPANION_KEY,
    compose_postiz_secret_values,
    postiz_atom_secret_ref,
    postiz_composed_atom_names,
    postiz_composition_inputs,
    postiz_secret_ref,
    project_postiz_entry,
)
from app.services.hosted_runtime_secrets import validate_hosted_runtime_secret_key_version
from app.services.managed_ai_provider import (
    V2_DEPLOYMENT_MANAGED_AI_PROVIDER_PREFIX,
    V2_MANAGED_AI_PROVIDER_IDS,
    is_managed_provider_id,
    is_v2_deployment_managed_provider_id,
    managed_provider_api_mode,
    runtime_managed_provider_id,
)
from app.services.project_runtime_skills import (
    RUNTIME_PROJECT_SKILL_LOCAL_KEY_PATTERN,
    agent_project_skill_sources,
    project_skill_file_signature,
    project_skill_runtime_identity,
)
from app.services.runtime_generation import resolve_runtime_apply_generation
from app.services.tenant_export import pending_export_requests
from app.services.url_security import UnsafePublicHttpsUrlError, validate_public_https_url
from app.services.vault_crypto import LEGACY_KEY_VERSION, decrypt, decrypt_with_key_version
from app.services.whatsapp_baileys import (
    buffer_json,
    ensure_whatsapp_agent_credential,
    whatsapp_credential_needs_self_identity_repair,
    whatsapp_self_identity_from_config,
)

RUNTIME_BUNDLE_V2_MEDIA_TYPE = "application/vnd.clawdi.runtime-bundle.v2+json"
RUNTIME_BUNDLE_V2_SCHEMA_VERSION = "clawdi.hosted-runtime.bundle.v2"
# Renderer contract: bump this value whenever emitted desired-state material changes.
# A bump makes each Agent render and backfill once on its next manifest poll, spreading
# the fleet work naturally. Forgetting it can make an old ETag return 304 indefinitely.
RUNTIME_SOURCE_RENDERER_REVISION = "runtime-source.v5"
RUNTIME_CAPABILITIES_HEADER = "X-Clawdi-Runtime-Capabilities"
RUNTIME_AGENT_PLUGINS_MANIFEST_CAPABILITY = "agent-plugins-manifest-v1"
RUNTIME_AGENT_PLUGIN_GITHUB_RELEASE_SOURCE_CAPABILITY = "agent-plugin-github-release-source-v1"
LEGACY_BOOTSTRAP_PROJECTION_REVISION = "legacy-bootstrap-cli.v1"
_CLAWDI_AUTH_TOKEN_SECRET_REF = "secret://clawdi/auth-token"
_SUPPORTED_RUNTIMES = {"hermes", "openclaw"}
_MANAGED_PROVIDER_RUNTIME_ENV = "CLAWDI_AI_API_KEY"
_CODEX_TOOL_SECRET_REF = "secret://tool.codex.apiKey"
_CODEX_TOOL_API_MODE = "openai_responses"
_CODEX_PROVIDER_SOURCE_API_MODES = {"openai_chat", "openai_responses"}
_AI_PROVIDER_MODELS_ADAPTER: TypeAdapter[list[AiProviderModel]] = TypeAdapter(list[AiProviderModel])


class RuntimeSourceError(ValueError):
    pass


class RuntimeSourceCapabilityError(RuntimeSourceError):
    """A valid canonical source requires a newer reader capability."""


class RuntimeSourceNotFoundError(RuntimeSourceError):
    pass


class RuntimeSecretUnreadableError(RuntimeSourceError):
    """A secret the manifest needs cannot be authenticated under the configured vault key.

    The manifest stays fail-closed: a partial manifest that silently dropped the secret would boot
    an agent without, say, its gateway token. The message is the same human message the route has
    always returned, so the HTTP response is unchanged; ``secret_refs`` adds the sorted refs an
    operator has to recover, by name only -- never a value.
    """

    def __init__(self, message: str, *, secret_refs: tuple[str, ...]) -> None:
        super().__init__(message)
        self.secret_refs = secret_refs


def expected_runtime_bundle_v2_etag(source_revision: str) -> str:
    """Return the frozen v2 validator derived from its complete source identity."""
    if not re.fullmatch(r"[0-9a-f]{64}", source_revision):
        raise ValueError("runtime bundle source revision must be a SHA-256 digest")
    return f'"sha256:{source_revision}"'


@dataclass(frozen=True)
class RuntimeSourceRow:
    environment: AgentEnvironment
    state: HostedRuntimeState | None
    observation: HostedRuntimeConfigObservation | None = None


@dataclass(frozen=True)
class RuntimeProjectSkill:
    id: UUID
    project_id: UUID
    skill_key: str
    local_skill_key: str
    content_hash: str


@dataclass(frozen=True)
class RuntimeSourceBatch:
    rows: dict[UUID, RuntimeSourceRow]
    providers: dict[tuple[UUID, str], AiProvider]
    auth_payloads: dict[tuple[UUID, str, str], AiProviderAuthPayload]
    channels: dict[UUID, tuple[tuple[ChannelAccount, ChannelBotAgentLink], ...]]
    channel_credentials: dict[UUID, ChannelAgentCredential] = field(default_factory=dict)
    whatsapp_auth_certs: dict[UUID, ChannelWhatsAppAuthCert] = field(default_factory=dict)
    runtime_secrets: dict[UUID, tuple[HostedRuntimeSecret, ...]] = field(default_factory=dict)
    project_skills: dict[UUID, tuple[RuntimeProjectSkill, ...]] = field(default_factory=dict)
    agent_plugins: dict[UUID, tuple[AgentPluginInstallation, ...]] = field(default_factory=dict)
    # Tenant installations (plan 06-09), keyed by environment id: only LIVE ones -- not removed,
    # and whose job is running or complete. They are iforge-owned rows merged into the manifest
    # at render time and never written into `hosted_runtime_states`, because the provisioner
    # replaces that column wholesale on every upsert (research Pattern 1, Pitfall 5).
    companion_installations: dict[UUID, tuple[CompanionInstallation, ...]] = field(
        default_factory=dict
    )
    # Their stored credential atoms, keyed by INSTALLATION id.
    companion_installation_secrets: dict[UUID, tuple[CompanionInstallationSecret, ...]] = field(
        default_factory=dict
    )
    # The tenant export jobs each installation's runtime still owes a database dump (plan 06-19),
    # keyed by INSTALLATION id, oldest first.
    companion_export_requests: dict[UUID, tuple[str, ...]] = field(default_factory=dict)
    # Each agent's retired installations per companion kind (plan 06-42, CR-04), keyed by
    # environment id then kind, newest first: tenant-confirmed uninstalls that completed without
    # the host confirming removal. The live entry of that kind names them.
    companion_retired_installations: dict[UUID, dict[str, tuple[str, ...]]] = field(
        default_factory=dict
    )


class CompanionEmitOmission(StrEnum):
    """Why one companion kind was withheld from a rendered manifest (COMP-03, T-4-26).

    Seven named causes rather than one silent omission. ``STALE_REPORTED_VERSION`` means the
    runtime reported a CLI older than the kind's minimum, and ``UNPARSEABLE_REPORTED_VERSION``
    that it reported a version the comparator cannot read. The four "no usable version" causes
    are not interchangeable to an operator: ``NO_OBSERVATION`` means the runtime has not checked
    in yet and will self-heal on its first report, ``PURGED_OBSERVATION`` means retention deleted
    a payload that did exist, ``NULL_REPORTED_VERSION`` means the runtime reported but could not
    name its own version, and ``INVALID_DIAGNOSTICS`` means the stored payload no longer
    validates -- a control-plane bug, not a fleet state. Collapsing them into silence would
    transfer the diagnosis cost from the system that knows the answer to the operator who cannot
    see it, and would make a companion that never arrives indistinguishable from a bug.

    ``SHADOWED_BY_INSTALLATION`` is not a version cause. It records that the provisioner declared
    a kind the tenant has also installed: the tenant's installation is authoritative for that
    kind, so the manifest carries the installation's entry and the provisioner's entry is
    withheld. An operator seeing it learns that editing the provisioner's declaration of that
    kind has no effect on this agent while the installation is live.
    """

    STALE_REPORTED_VERSION = "stale_reported_version"
    NO_OBSERVATION = "no_observation"
    PURGED_OBSERVATION = "purged_observation"
    NULL_REPORTED_VERSION = "null_reported_version"
    UNPARSEABLE_REPORTED_VERSION = "unparseable_reported_version"
    INVALID_DIAGNOSTICS = "invalid_diagnostics"
    SHADOWED_BY_INSTALLATION = "shadowed_by_installation"


@dataclass(frozen=True)
class CompanionEmitOmissionRecord:
    """One withheld companion kind, its cause, and the two versions that decided it.

    Carries the registry KEY only. A companion entry embeds a live HS256 secret at
    ``auth.secret`` (``HostedFileBrowserCompanion``), so no field here may ever hold any part of
    an entry's value -- the same discipline ``collectUnsupportedCompanions`` applies on the CLI
    side, and for the same reason (T-4-07).
    """

    kind: str
    cause: CompanionEmitOmission
    reported_version: str | None
    required_min_cli_version: str


@dataclass(frozen=True)
class CompanionEmitDecision:
    admitted: tuple[str, ...]
    omissions: tuple[CompanionEmitOmissionRecord, ...]


@dataclass(frozen=True)
class RenderedRuntimeSource:
    manifest: dict[str, Any]
    channel_bindings: list[dict[str, Any]]
    secret_values: dict[str, str]
    source_revision: str
    apply_generation: int | None
    # The emit gate's decision trail for this render. A CONTROL-PLANE diagnostic: it is
    # deliberately absent from `render_runtime_bundle`, because putting it on the wire would be
    # a manifest schema change this phase does not make, and would tell every runtime which
    # kinds it is being denied. Defaults to `()` so every existing construction site is
    # unchanged and an omission-free render is indistinguishable from the pre-gate one.
    companion_omissions: tuple[CompanionEmitOmissionRecord, ...] = ()


@dataclass(frozen=True)
class RuntimeSecretMaterial:
    secret_ref: str
    ciphertext: bytes
    nonce: bytes
    error_message: str
    # The stored row's key version, where the row carries one: the keyring decrypts the material
    # with that version's key (plan 06-25). `None` is an unversioned table's material, which is
    # always vault.v1.
    key_version: str | None = None


def _decrypt_material(material: RuntimeSecretMaterial) -> str:
    """One material's plaintext, under the key its own version names.

    vault.v1 -- every unversioned material, and a versioned row not yet rotated -- goes through
    this module's ``decrypt``, the one the render has always used.
    """
    if material.key_version is None or material.key_version == LEGACY_KEY_VERSION:
        return decrypt(material.ciphertext, material.nonce)
    return decrypt_with_key_version(material.ciphertext, material.nonce, material.key_version)


@dataclass(frozen=True)
class _InstallationProjection:
    """One live installation's manifest entry and the facts its credentials are composed from."""

    installation: CompanionInstallation
    atoms: tuple[CompanionInstallationSecret, ...]
    entry: dict[str, Any]
    composition_inputs: dict[str, str]
    agent_name: str | None


# Some reporters send the npm package spec rather than the bare version. Mirrors
# `project_runtime_skills.py`'s `removeprefix`; an unstripped spec is unparseable, which would
# fail a current runtime closed.
_CLI_PACKAGE_SPEC_PREFIX = "clawdi@"


def _reported_cli_version(
    observation: HostedRuntimeConfigObservation | None,
) -> tuple[str | None, CompanionEmitOmission | None]:
    """The runtime's last self-reported CLI version, or the cause there is not one.

    NEVER raises. Every failure to read a version degrades to a named cause, because the caller
    renders a manifest: raising here would become a ``RuntimeSourceError``, then a 409, and would
    deny the WHOLE manifest to precisely the stale client the gate exists to protect (T-4-10).
    """
    if observation is None:
        return None, CompanionEmitOmission.NO_OBSERVATION
    diagnostics = observation.diagnostics
    if isinstance(diagnostics, dict) and not diagnostics:
        # An empty object is the shape a purge leaves behind -- `_purge_observation_payloads`
        # in `app/services/runtime_observation.py` sets `diagnostics={}` -- and the
        # `payload_purged_at IS NULL OR diagnostics = '{}'::jsonb` constraint on the inbox row
        # keeps it that way. Checked BEFORE model validation so a purge reports as a purge
        # rather than as a corrupt payload.
        return None, CompanionEmitOmission.PURGED_OBSERVATION
    try:
        observed = HostedRuntimeObservedV2.model_validate(diagnostics)
    except (ValidationError, ValueError, TypeError):
        return None, CompanionEmitOmission.INVALID_DIAGNOSTICS
    if observed.active_cli_version is None:
        # `activeCliVersion: str | None` on the wire: "reported, but named no version" is
        # representable and is not the same event as "never reported".
        return None, CompanionEmitOmission.NULL_REPORTED_VERSION
    return observed.active_cli_version.removeprefix(_CLI_PACKAGE_SPEC_PREFIX), None


def _gate_companion_kinds(
    declared_keys: Collection[str],
    observation: HostedRuntimeConfigObservation | None,
) -> CompanionEmitDecision:
    """Decide which declared companion kinds may be emitted to this runtime (D-08).

    The minimum each kind requires comes from the registry embed
    (``app/core/companion_registry.py``), never from a literal here: the registry is pinned to
    ``config/companion-registry.txt`` and to the TypeScript embed by parity suites, and a fourth
    representation beside its consumer is how a bound drifts from the thing it bounds.

    **Fails closed, and deliberately does NOT fall back to the commanded version.**
    ``04-RESEARCH.md`` Q3 recommended falling back to the COMMANDED CLI package spec on the
    hosted runtime state row -- which is always present -- when no observation exists, so a first
    boot is not starved of gated kinds. D-15 supersedes that: the commanded version is what the
    control plane ASKED the runtime to run, not evidence of what it IS running, so "absent means
    current" would hand a stale CLI that never reports exactly the kinds it cannot parse -- the
    failure COMP-03 exists to prevent. That value is deliberately not read here. The accepted
    consequence: a brand-new deployment receives no gated kind until it has reported once. That
    is harmless today because the only registry kind (``filebrowser``) is ungated at ``0.0.0``;
    it becomes operationally visible in Phase 6, which ships the first gated kind.

    A declared key the registry does not know is ADMITTED, not gated. The gate compares against a
    declared minimum and there is none, so such a key is outside its remit and belongs to
    D-05/D-06/D-14's graceful-degradation path. Gating it would also make the
    ``unsupportedCompanions`` observation unreachable: the backend would never send the kind
    whose arrival is the only thing that populates it.
    """
    reported, cause = _reported_cli_version(observation)
    if cause is None:
        try:
            emittable = frozenset(companion_kinds_at_or_below(reported))
        except ValueError:
            # T-4-27: garbage is an omission cause, never an admission. A comparator that read
            # "satisfied" out of input it could not parse would make the gate bypassable.
            cause = CompanionEmitOmission.UNPARSEABLE_REPORTED_VERSION
            emittable = frozenset(companion_kinds_at_or_below(None))
        else:
            cause = CompanionEmitOmission.STALE_REPORTED_VERSION
    else:
        emittable = frozenset(companion_kinds_at_or_below(None))

    admitted: list[str] = []
    omissions: list[CompanionEmitOmissionRecord] = []
    for key in declared_keys:
        if key not in COMPANION_REGISTRY or key in emittable:
            admitted.append(key)
            continue
        omissions.append(
            CompanionEmitOmissionRecord(
                kind=key,
                cause=cause,
                reported_version=reported,
                required_min_cli_version=companion_min_cli_version(key),
            )
        )
    return CompanionEmitDecision(tuple(admitted), tuple(omissions))


def _shadowed_omissions(
    shadowed_keys: tuple[str, ...],
    observation: HostedRuntimeConfigObservation | None,
) -> tuple[CompanionEmitOmissionRecord, ...]:
    """One ``SHADOWED_BY_INSTALLATION`` record per provisioner-declared kind an install replaced.

    Carries the key and the two versions only, never either entry's value (T-4-07). The versions
    are recorded for the same reason every omission records them: the operator reading the
    record should not need a second lookup to know which runtime it concerns.
    """
    if not shadowed_keys:
        return ()
    reported, _cause = _reported_cli_version(observation)
    return tuple(
        CompanionEmitOmissionRecord(
            kind=key,
            cause=CompanionEmitOmission.SHADOWED_BY_INSTALLATION,
            reported_version=reported,
            required_min_cli_version=companion_min_cli_version(key),
        )
        for key in shadowed_keys
    )


async def load_runtime_source_batch(
    db: AsyncSession,
    *,
    environment_ids: list[UUID],
    owner_user_id: UUID | None = None,
) -> RuntimeSourceBatch:
    if not environment_ids:
        return RuntimeSourceBatch(rows={}, providers={}, auth_payloads={}, channels={})
    env_filters: list[ColumnElement[bool]] = [
        AgentEnvironment.id.in_(environment_ids),
        AgentEnvironment.archived_at.is_(None),
    ]
    if owner_user_id is not None:
        env_filters.append(AgentEnvironment.user_id == owner_user_id)
    env_rows = (
        await db.execute(
            select(
                AgentEnvironment,
                HostedRuntimeState,
                HostedRuntimeConfigObservation,
                AgentPluginInstallation,
            )
            .outerjoin(HostedRuntimeState, HostedRuntimeState.environment_id == AgentEnvironment.id)
            .outerjoin(
                HostedRuntimeConfigObservation,
                HostedRuntimeConfigObservation.environment_id == AgentEnvironment.id,
            )
            .outerjoin(
                AgentPluginInstallation,
                AgentPluginInstallation.environment_id == AgentEnvironment.id,
            )
            .where(*env_filters)
            .order_by(AgentEnvironment.id, AgentPluginInstallation.plugin_name)
        )
    ).all()
    rows: dict[UUID, RuntimeSourceRow] = {}
    agent_plugins: dict[UUID, list[AgentPluginInstallation]] = {}
    for env, state, observation, installation in env_rows:
        rows.setdefault(
            env.id,
            RuntimeSourceRow(environment=env, state=state, observation=observation),
        )
        if installation is not None:
            agent_plugins.setdefault(env.id, []).append(installation)
    user_ids = sorted({row.environment.user_id for row in rows.values()}, key=str)
    if not user_ids:
        return RuntimeSourceBatch(rows=rows, providers={}, auth_payloads={}, channels={})
    providers = list(
        (
            await db.execute(
                select(AiProvider).where(
                    AiProvider.owner_user_id.in_(user_ids),
                    AiProvider.archived_at.is_(None),
                )
            )
        ).scalars()
    )
    auth_payloads = list(
        (
            await db.execute(
                select(AiProviderAuthPayload).where(
                    AiProviderAuthPayload.owner_user_id.in_(user_ids),
                    AiProviderAuthPayload.archived_at.is_(None),
                )
            )
        ).scalars()
    )
    channel_rows = (
        await db.execute(
            select(ChannelBotAgentLink.agent_id, ChannelAccount, ChannelBotAgentLink)
            .join(ChannelAccount, ChannelAccount.id == ChannelBotAgentLink.account_id)
            .where(
                ChannelBotAgentLink.agent_id.in_(list(rows)),
                ChannelBotAgentLink.user_id.in_(user_ids),
                ChannelBotAgentLink.status == BOT_AGENT_LINK_STATUS_ACTIVE,
                ChannelBotAgentLink.archived_at.is_(None),
                ChannelAccount.status == CHANNEL_STATUS_ACTIVE,
                ChannelAccount.archived_at.is_(None),
                ChannelAccount.provider.in_(
                    (
                        CHANNEL_PROVIDER_TELEGRAM,
                        CHANNEL_PROVIDER_DISCORD,
                        CHANNEL_PROVIDER_WHATSAPP,
                    )
                ),
            )
            .order_by(
                ChannelBotAgentLink.agent_id,
                ChannelAccount.provider,
                ChannelAccount.id,
                ChannelBotAgentLink.id,
            )
        )
    ).all()
    channels: dict[UUID, list[tuple[ChannelAccount, ChannelBotAgentLink]]] = {}
    for environment_id, account, link in channel_rows:
        channels.setdefault(environment_id, []).append((account, link))
    whatsapp_links = [
        (account, link)
        for _environment_id, account, link in channel_rows
        if account.provider == CHANNEL_PROVIDER_WHATSAPP
    ]
    credential_rows = (
        list(
            (
                await db.execute(
                    select(ChannelAgentCredential)
                    .where(
                        ChannelAgentCredential.bot_agent_link_id.in_(
                            [link.id for _account, link in whatsapp_links]
                        ),
                        ChannelAgentCredential.provider == CHANNEL_PROVIDER_WHATSAPP,
                        ChannelAgentCredential.revoked_at.is_(None),
                    )
                    .order_by(
                        ChannelAgentCredential.bot_agent_link_id,
                        ChannelAgentCredential.created_at.desc(),
                        ChannelAgentCredential.id.desc(),
                    )
                )
            ).scalars()
        )
        if whatsapp_links
        else []
    )
    channel_credentials: dict[UUID, ChannelAgentCredential] = {}
    for credential in credential_rows:
        channel_credentials.setdefault(credential.bot_agent_link_id, credential)
    auth_cert_rows = (
        list(
            (
                await db.execute(
                    select(ChannelWhatsAppAuthCert).where(
                        ChannelWhatsAppAuthCert.account_id.in_(
                            [account.id for account, _link in whatsapp_links]
                        )
                    )
                )
            ).scalars()
        )
        if whatsapp_links
        else []
    )
    runtime_secret_rows = list(
        (
            await db.execute(
                select(HostedRuntimeSecret)
                .where(HostedRuntimeSecret.environment_id.in_(list(rows)))
                .order_by(HostedRuntimeSecret.environment_id, HostedRuntimeSecret.secret_ref)
            )
        ).scalars()
    )
    runtime_secrets: dict[UUID, list[HostedRuntimeSecret]] = {}
    for secret in runtime_secret_rows:
        runtime_secrets.setdefault(secret.environment_id, []).append(secret)
    companion_installations, companion_installation_secrets = await _load_companion_installations(
        db, environment_ids=list(rows), user_ids=user_ids
    )
    # One more query, and only when an installation is live: a batch without one keeps its count.
    companion_export_requests = await pending_export_requests(
        db,
        user_ids=user_ids,
        installation_ids=[
            installation.id
            for installations in companion_installations.values()
            for installation in installations
        ],
    )
    # One more query, again only for agents with a live installation: only a live entry can name
    # a retired installation, so a batch without one keeps its count.
    companion_retired_installations = await _load_retired_installations(
        db, environment_ids=list(companion_installations), user_ids=user_ids
    )
    project_skill_rows = (
        await db.execute(
            agent_project_skill_sources()
            .where(AgentEnvironment.id.in_(list(rows)))
            .order_by(AgentEnvironment.id, Project.id, Skill.skill_key)
        )
    ).all()
    project_skills: dict[UUID, list[RuntimeProjectSkill]] = {}
    for environment_id, skill in project_skill_rows:
        runtime_row = rows.get(environment_id)
        if runtime_row is None or runtime_row.state is None:
            continue
        project_skills.setdefault(environment_id, []).append(
            RuntimeProjectSkill(
                id=skill.id,
                project_id=skill.project_id,
                skill_key=skill.skill_key,
                local_skill_key=project_skill_runtime_identity(
                    skill.skill_key,
                    skill.name,
                ).local_skill_key,
                content_hash=skill.content_hash,
            )
        )
    return RuntimeSourceBatch(
        rows=rows,
        providers={(item.owner_user_id, item.provider_id): item for item in providers},
        auth_payloads={
            (item.owner_user_id, item.provider_id, item.auth_profile): item
            for item in auth_payloads
        },
        channels={key: tuple(value) for key, value in channels.items()},
        channel_credentials=channel_credentials,
        whatsapp_auth_certs={item.account_id: item for item in auth_cert_rows},
        runtime_secrets={key: tuple(value) for key, value in runtime_secrets.items()},
        project_skills={key: tuple(value) for key, value in project_skills.items()},
        agent_plugins={key: tuple(value) for key, value in agent_plugins.items()},
        companion_installations=companion_installations,
        companion_installation_secrets=companion_installation_secrets,
        companion_export_requests=companion_export_requests,
        companion_retired_installations=companion_retired_installations,
    )


async def _load_retired_installations(
    db: AsyncSession,
    *,
    environment_ids: list[UUID],
    user_ids: list[UUID],
) -> dict[UUID, dict[str, tuple[str, ...]]]:
    """Each agent's retired installations per kind, newest first, at most 8 (plan 06-42, CR-04).

    Retired means: an uninstall the tenant confirmed by typed name (the only way an installation's
    operation becomes ``uninstall``), that completed (``removed_at``) with the host's removal never
    confirmed (``host_removal_unconfirmed`` is JSON true and no ``removed`` report was recorded).
    A confirmed removal is never listed: the host already destroyed that data, and nothing is left
    to authorize. A live installation is never listed. The query is scoped to the rendered agents
    and their owners, and grouped per agent and kind, so an entry can only ever name its own
    agent's installations of its own kind (T-06-42-03).
    """
    if not environment_ids:
        return {}
    rows = (
        await db.execute(
            select(
                CompanionInstallation.id,
                CompanionInstallation.environment_id,
                CompanionInstallation.companion_key,
                CompanionInstallation.removed_at,
            )
            .where(
                CompanionInstallation.environment_id.in_(environment_ids),
                CompanionInstallation.user_id.in_(user_ids),
                CompanionInstallation.removed_at.is_not(None),
                CompanionInstallation.operation == COMPANION_OPERATION_UNINSTALL,
                CompanionInstallation.external_resources.contains(
                    {HOST_REMOVAL_UNCONFIRMED_KEY: True}
                ),
                ~CompanionInstallation.external_resources.has_key(REMOVED_REPORTED_AT_KEY),
            )
            .order_by(CompanionInstallation.removed_at.desc(), CompanionInstallation.id)
        )
    ).all()
    retired: dict[UUID, dict[str, list[str]]] = {}
    for installation_id, environment_id, companion_key, _removed_at in rows:
        ids = retired.setdefault(environment_id, {}).setdefault(companion_key, [])
        if len(ids) < POSTIZ_MAX_RETIRED_INSTALLATIONS:
            ids.append(str(installation_id))
    return {
        environment_id: {key: tuple(ids) for key, ids in by_kind.items()}
        for environment_id, by_kind in retired.items()
    }


async def _load_companion_installations(
    db: AsyncSession,
    *,
    environment_ids: list[UUID],
    user_ids: list[UUID],
) -> tuple[
    dict[UUID, tuple[CompanionInstallation, ...]],
    dict[UUID, tuple[CompanionInstallationSecret, ...]],
]:
    """The batch's live installations and their credential atoms (D-26, D-27).

    Live means not removed AND a job that is running or complete: a queued install has not been
    started and a failed one must not be half-applied, so neither reaches a runtime. A failed
    UPDATE is live too (plan 06-18): its install completed and its previous digest still serves.
    The owner predicate is repeated on the installation row itself, so an installation can only
    ever be projected into an agent of the tenant that owns it (T-06-10-06).

    ONE query, installations outer-joined to their atoms, so the batch's query count stays
    constant however many agents and installations it covers.
    """
    joined_rows = (
        await db.execute(
            select(CompanionInstallation, CompanionInstallationSecret)
            .outerjoin(
                CompanionInstallationSecret,
                CompanionInstallationSecret.installation_id == CompanionInstallation.id,
            )
            .where(
                CompanionInstallation.environment_id.in_(environment_ids),
                CompanionInstallation.user_id.in_(user_ids),
                CompanionInstallation.removed_at.is_(None),
                or_(
                    CompanionInstallation.status.in_(
                        (
                            COMPANION_INSTALLATION_STATUS_RUNNING,
                            COMPANION_INSTALLATION_STATUS_COMPLETE,
                        )
                    ),
                    # A failed UPDATE keeps serving its previous digest (plan 06-18): the runtime
                    # aborted or rolled back, and the pin is back on the digest that runs.
                    serving_installation_filter(),
                ),
            )
            .order_by(
                CompanionInstallation.environment_id,
                CompanionInstallation.companion_key,
                CompanionInstallation.id,
                CompanionInstallationSecret.secret_name,
            )
        )
    ).all()
    installations: dict[UUID, list[CompanionInstallation]] = {}
    seen: set[UUID] = set()
    atoms: dict[UUID, list[CompanionInstallationSecret]] = {}
    for installation, atom in joined_rows:
        if installation.id not in seen:
            seen.add(installation.id)
            installations.setdefault(installation.environment_id, []).append(installation)
        if atom is not None:
            atoms.setdefault(installation.id, []).append(atom)
    return (
        {key: tuple(value) for key, value in installations.items()},
        {key: tuple(value) for key, value in atoms.items()},
    )


def _agent_display_name(environment: AgentEnvironment) -> str | None:
    """The agent's human name -- the dashboard override, else its assigned default -- or None."""
    return (
        (environment.display_name or "").strip() or (environment.default_name or "").strip() or None
    )


def _project_companion_installations(
    installations: tuple[CompanionInstallation, ...],
    *,
    atoms_by_installation: dict[UUID, tuple[CompanionInstallationSecret, ...]],
    agent_name: str | None,
    export_requests: dict[UUID, tuple[str, ...]] | None = None,
    retired_installations: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, _InstallationProjection]:
    """Each live installation's manifest entry, keyed by companion kind.

    Fails closed: a kind with no projection, or an installation row the entry contract refuses,
    raises rather than being skipped, because a silently skipped installation is a tenant's
    companion disappearing with nothing to diagnose it by.
    """
    projections: dict[str, _InstallationProjection] = {}
    for installation in installations:
        if uninstall_host_side_settled(installation):
            # Deliberately not projected (plan 06-20): the runtime already reported the companion
            # removed, or the removal deadline passed and the control plane finishes without it.
            # Until then the entry carries `lifecycle: "remove"`.
            continue
        key = installation.companion_key
        if key != POSTIZ_COMPANION_KEY:
            raise RuntimeSourceError(f"No manifest projection exists for companion {key}")
        if key in projections:
            raise RuntimeSourceError(f"More than one live installation of companion {key}")
        try:
            catalog = catalog_entry(key)
        except KeyError as exc:
            raise RuntimeSourceError(f"Companion {key} is not in the catalog") from exc
        try:
            entry = project_postiz_entry(
                installation,
                catalog,
                export_requests=(export_requests or {}).get(installation.id, ()),
                # This agent's retired installations of this kind only (plan 06-42).
                retired_installation_ids=(retired_installations or {}).get(key, ()),
            )
        except ValidationError as exc:
            raise RuntimeSourceError("Companion installation projection is invalid") from exc
        projections[key] = _InstallationProjection(
            installation=installation,
            # Only this installation's own atoms: a composition can never read another's.
            atoms=atoms_by_installation.get(installation.id, ()),
            entry=entry,
            composition_inputs=postiz_composition_inputs(
                installation, catalog, agent_name=agent_name
            ),
            agent_name=agent_name,
        )
    return projections


async def ensure_runtime_whatsapp_credentials(
    db: AsyncSession,
    *,
    environment_id: UUID,
    owner_user_id: UUID,
    link_ids: tuple[UUID, ...],
) -> None:
    if not link_ids:
        return
    rows = (
        await db.execute(
            select(ChannelAccount, ChannelBotAgentLink)
            .join(ChannelBotAgentLink, ChannelBotAgentLink.account_id == ChannelAccount.id)
            .where(
                ChannelBotAgentLink.id.in_(link_ids),
                ChannelBotAgentLink.agent_id == environment_id,
                ChannelBotAgentLink.user_id == owner_user_id,
                ChannelBotAgentLink.status == BOT_AGENT_LINK_STATUS_ACTIVE,
                ChannelBotAgentLink.archived_at.is_(None),
                ChannelAccount.provider == CHANNEL_PROVIDER_WHATSAPP,
                ChannelAccount.status == CHANNEL_STATUS_ACTIVE,
                ChannelAccount.archived_at.is_(None),
            )
            .order_by(ChannelAccount.id, ChannelBotAgentLink.id)
        )
    ).all()
    try:
        for account, link in rows:
            await ensure_whatsapp_agent_credential(db, account=account, link=link)
    except Exception as exc:
        raise RuntimeSourceError("Hosted runtime WhatsApp credential source is invalid") from exc


def runtime_whatsapp_credential_repair_link_ids(
    batch: RuntimeSourceBatch,
    *,
    environment_id: UUID,
) -> tuple[UUID, ...]:
    repair_link_ids: list[UUID] = []
    try:
        for account, link in batch.channels.get(environment_id, ()):
            if account.provider != CHANNEL_PROVIDER_WHATSAPP:
                continue
            credential = batch.channel_credentials.get(link.id)
            if credential is None or account.id not in batch.whatsapp_auth_certs:
                repair_link_ids.append(link.id)
                continue
            if whatsapp_credential_needs_self_identity_repair(
                credential,
                self_identity=whatsapp_self_identity_from_config(account.config),
            ):
                repair_link_ids.append(link.id)
    except Exception as exc:
        raise RuntimeSourceError("Hosted runtime WhatsApp credential source is invalid") from exc
    return tuple(repair_link_ids)


def _project_runtime_skills(
    workspace_skills: dict[str, Any] | None,
    project_skills: tuple[RuntimeProjectSkill, ...],
    *,
    environment_id: UUID,
    public_api_url: str,
    signing_key: str,
) -> dict[str, Any] | None:
    """Compose Workspace and linked-Project intent without key precedence."""
    entries: dict[str, Any] = {}
    if workspace_skills is not None:
        raw_entries = workspace_skills.get("entries")
        if not _is_string_object_dict(raw_entries):
            raise RuntimeSourceError("Hosted runtime skills state is invalid")
        entries.update(raw_entries)

    base_url = public_api_url.rstrip("/")
    owners: dict[str, UUID] = {}
    for skill in project_skills:
        if RUNTIME_PROJECT_SKILL_LOCAL_KEY_PATTERN.fullmatch(skill.local_skill_key) is None:
            raise RuntimeSourceError(
                f'Project Skill "{skill.skill_key}" has an incompatible SKILL.md name'
            )
        if skill.local_skill_key in entries:
            raise RuntimeSourceError(
                f'Project Skill "{skill.local_skill_key}" conflicts with this Agent\'s Workspace'
            )
        existing_project = owners.get(skill.local_skill_key)
        if existing_project is not None:
            raise RuntimeSourceError(
                f'Project Skill "{skill.local_skill_key}" is provided by more than one linked '
                "Project"
            )
        owners[skill.local_skill_key] = skill.project_id
        signature = project_skill_file_signature(
            signing_key=signing_key,
            agent_id=environment_id,
            skill_id=skill.id,
            content_hash=skill.content_hash,
        )
        encoded_key = quote(skill.local_skill_key, safe="")
        entries[skill.local_skill_key] = {
            "enabled": True,
            "source": {
                "type": "project",
                "projectId": str(skill.project_id),
                "contentHash": skill.content_hash,
                "archiveUrl": (
                    f"{base_url}/v1/runtime/project-skill-archives/{environment_id}/"
                    f"{skill.project_id}/{skill.id}/{skill.content_hash}/{signature}/"
                    f"{encoded_key}.tar.gz"
                ),
                "installUrl": (
                    f"{base_url}/v1/runtime/project-skill-files/{environment_id}/"
                    f"{skill.id}/{skill.content_hash}/{signature}/SKILL.md"
                ),
            },
        }
    return {"entries": entries} if entries else None


def _project_agent_plugins(
    installations: tuple[AgentPluginInstallation, ...],
    *,
    include_github_release_sources: bool = True,
) -> HostedAgentPlugins | None:
    projected = tuple(
        installation
        for installation in installations
        if installation.plugin_name not in RESERVED_AGENT_PLUGIN_NAMES
        and (include_github_release_sources or installation.source.get("type") != "github-release")
    )
    if not projected:
        return None
    return HostedAgentPlugins.model_validate(
        {
            "schemaVersion": 1,
            "installations": {
                installation.plugin_name: {
                    "installationId": str(installation.id),
                    "version": installation.version,
                    "agentPluginsSchema": installation.agent_plugins_schema,
                    "source": installation.source,
                    "contentDigest": installation.content_digest,
                }
                for installation in projected
            },
        }
    )


def render_runtime_source(
    batch: RuntimeSourceBatch,
    *,
    environment_id: UUID,
    public_api_url: str,
    vault_key_identity: str,
    decrypt_secrets: bool,
    project_provider_identity: bool = True,
    project_agent_plugins: bool = True,
    project_agent_plugin_github_release_sources: bool = True,
) -> RenderedRuntimeSource:
    row = batch.rows.get(environment_id)
    if row is None:
        raise RuntimeSourceNotFoundError("Agent environment not found")
    if row.state is None:
        raise RuntimeSourceNotFoundError("Hosted runtime state not found")
    state = row.state
    user_id = row.environment.user_id
    try:
        locale = HostedRuntimeLocale.model_validate(state.locale)
        system = HostedRuntimeSystem.model_validate(state.system)
        live_sync = HostedRuntimeLiveSync.model_validate(state.live_sync)
        recovery = HostedRuntimeRecovery.model_validate(state.recovery)
    except ValidationError as exc:
        raise RuntimeSourceError(
            "Hosted runtime locale, system, live sync, or recovery state "
            "is invalid or not configured"
        ) from exc
    try:
        egress_engine = (
            HostedEgressEngine.model_validate(state.egress_engine)
            if state.egress_engine is not None
            else None
        )
        egress_profiles = (
            HostedEgressProfiles.model_validate(state.egress_profiles)
            if state.egress_profiles is not None
            else None
        )
    except ValidationError as exc:
        raise RuntimeSourceError("Hosted runtime egress state is invalid") from exc
    try:
        companions = (
            HostedRuntimeCompanions.model_validate(state.companions)
            if state.companions is not None
            else None
        )
    except ValidationError as exc:
        raise RuntimeSourceError("Hosted runtime companion state is invalid") from exc
    try:
        tools = HostedRuntimeTools.model_validate(state.tools)
    except ValidationError as exc:
        raise RuntimeSourceError("Hosted runtime tools state is invalid") from exc
    try:
        mcp_document = HostedRuntimeMcp.model_validate(state.mcp) if state.mcp is not None else None
        workspace_skills = (
            PersistedHostedRuntimeSkills.model_validate(state.skills).model_dump(
                exclude_none=True,
                exclude_unset=True,
                mode="json",
            )
            if state.skills is not None
            else None
        )
    except (ValidationError, ValueError) as exc:
        raise RuntimeSourceError("Hosted runtime MCP or skills state is invalid") from exc
    try:
        agent_plugins = _project_agent_plugins(
            batch.agent_plugins.get(environment_id, ()),
            include_github_release_sources=project_agent_plugin_github_release_sources,
        )
    except ValidationError as exc:
        raise RuntimeSourceError("Clawdi Agent Plugins desired state is invalid") from exc
    if mcp_document is None:
        mcp = None
    else:
        clawdi_mcp = mcp_document.servers.get("clawdi")
        public_clawdi_mcp_url = f"{public_api_url.rstrip('/')}/v1/mcp/clawdi"
        if isinstance(clawdi_mcp, HostedRuntimePlatformMcpServer):
            mcp_document.servers["clawdi"] = HostedRuntimeRemoteMcpServer(
                url=public_clawdi_mcp_url,
                transport=clawdi_mcp.transport,
                headers=clawdi_mcp.headers,
            )
        elif isinstance(clawdi_mcp, HostedRuntimeRemoteMcpServer):
            clawdi_mcp.url = public_clawdi_mcp_url
        mcp = mcp_document.model_dump(mode="json")
    skills = _project_runtime_skills(
        workspace_skills,
        batch.project_skills.get(environment_id, ()),
        environment_id=environment_id,
        public_api_url=public_api_url,
        signing_key=vault_key_identity,
    )
    runtime_name, runtime = _runtime(state.runtimes)
    try:
        cli_package_spec = validate_clawdi_cli_package_spec(state.cli_package_spec)
    except ValueError as exc:
        raise RuntimeSourceError("Hosted runtime CLI package spec is invalid") from exc
    bound_runtime_provider_ids = list(runtime["provider_ids"])
    runtime = _agent_runtime_binding(runtime)
    dashboard_auth = system.hermesDashboardAuth
    if runtime_name == "hermes":
        if dashboard_auth is None:
            raise RuntimeSourceError("Hermes direct dashboard requires official authentication")
        if dashboard_auth.activation.enabled is not True:
            raise RuntimeSourceError("Hermes dashboard authentication must be explicitly enabled")
        if runtime.get("services", {}).get("dashboard", {}).get("args") != [
            "dashboard",
            "--host",
            "0.0.0.0",
            "--port",
            "9119",
            "--no-open",
        ]:
            raise RuntimeSourceError("Hermes dashboard must bind directly to 0.0.0.0:9119")
    elif dashboard_auth is not None:
        raise RuntimeSourceError("Hermes dashboard auth is only valid for Hermes runtimes")

    providers: dict[str, Any] = {}
    secrets: dict[str, str] = {}
    secret_sources: dict[str, dict[str, str]] = {}
    secret_materials: list[RuntimeSecretMaterial] = []
    for runtime_secret in batch.runtime_secrets.get(environment_id, ()):
        if not is_canonical_secret_ref(runtime_secret.secret_ref):
            raise RuntimeSourceError("Hosted runtime secret reference is invalid")
        try:
            validate_hosted_runtime_secret_key_version(runtime_secret.key_version)
        except RuntimeError as exc:
            raise RuntimeSourceError("Hosted runtime secret source is invalid") from exc
        _add_secret_source(
            secret_sources,
            runtime_secret.secret_ref,
            _secret_identity(
                runtime_secret.id,
                runtime_secret.encrypted_value,
                runtime_secret.nonce,
                vault_key_identity,
                "runtime-state-secret",
                key_version=runtime_secret.key_version,
            ),
        )
        secret_materials.append(
            RuntimeSecretMaterial(
                secret_ref=runtime_secret.secret_ref,
                ciphertext=runtime_secret.encrypted_value,
                nonce=runtime_secret.nonce,
                error_message="Hosted runtime secret source is invalid",
                key_version=runtime_secret.key_version,
            )
        )
    codex_tool = tools.codex
    if not is_v2_deployment_managed_provider_id(codex_tool.provider_id):
        raise RuntimeSourceError("Hosted Codex tool provider must use its exact deployment source")
    codex_agent_provider_id = runtime_managed_provider_id(codex_tool.provider_id)
    provider_sources: dict[str, str] = {}
    for bound_provider_id in [*bound_runtime_provider_ids, codex_tool.provider_id]:
        agent_provider_id = runtime_managed_provider_id(bound_provider_id)
        source_provider_id = _exact_provider_source_id(
            bound_provider_id=bound_provider_id,
        )
        existing_source = provider_sources.get(agent_provider_id)
        if existing_source is not None and existing_source != source_provider_id:
            raise RuntimeSourceError(
                f"multiple provider bindings project to agent provider {agent_provider_id}"
            )
        provider_sources[agent_provider_id] = source_provider_id
    provider_material: dict[str, dict[str, Any]] = {}
    runtime_provider_ids = set(runtime["provider_ids"])
    primary_model = runtime.get("primary_model")
    selected_models = (
        {primary_model["provider_id"]: primary_model["model"]} if primary_model else {}
    )
    for agent_provider_id, source_provider_id in sorted(provider_sources.items()):
        provider = batch.providers.get((user_id, source_provider_id))
        is_codex_provider = agent_provider_id == codex_agent_provider_id
        if provider is None:
            if is_codex_provider:
                raise RuntimeSourceError("Hosted Codex tool provider is missing or archived")
            consumer = runtime_name if agent_provider_id in runtime_provider_ids else "codex tool"
            provider_material[agent_provider_id] = _unhealthy_provider(agent_provider_id, consumer)
            continue
        try:
            validate_public_https_url(provider.base_url, label="AI Provider base_url")
        except UnsafePublicHttpsUrlError as exc:
            raise RuntimeSourceError(str(exc)) from exc
        payload = _selected_auth_payload(
            batch,
            provider,
            environment_id=environment_id,
            runtime_name=runtime_name,
            allow_oauth=agent_provider_id in runtime_provider_ids and not is_codex_provider,
        )
        if is_codex_provider and (
            provider.managed_by != "clawdi"
            or payload is None
            or payload.kind != "api_key"
            or payload.source != "managed"
        ):
            raise RuntimeSourceError(
                "Hosted Codex tool provider must use a Clawdi-managed provider auth payload"
            )
        secret_ref = None
        if payload is not None:
            secret_ref = (
                _CODEX_TOOL_SECRET_REF
                if is_codex_provider
                else _provider_oauth_secret_ref(source_provider_id)
                if payload.kind in {"agent_profile", "oauth_profile"}
                else _provider_secret_ref(source_provider_id)
            )
        provider_entry = _provider_entry(
            provider,
            runtime_name=runtime_name,
            secret_ref=secret_ref,
            credential_revision=(
                payload.credential_revision
                if payload is not None and payload.kind in {"agent_profile", "oauth_profile"}
                else None
            ),
            selected_model=selected_models.get(agent_provider_id),
        )
        if is_codex_provider and (
            provider.api_mode not in _CODEX_PROVIDER_SOURCE_API_MODES
            or provider_entry.get("runtimeEnvName") != _MANAGED_PROVIDER_RUNTIME_ENV
        ):
            raise RuntimeSourceError(
                "Hosted Codex tool provider must use a supported managed OpenAI projection"
            )
        provider_material[agent_provider_id] = provider_entry
        if payload is not None and secret_ref is not None:
            _add_secret_source(
                secret_sources,
                secret_ref,
                _secret_identity(
                    payload.id,
                    payload.encrypted_payload,
                    payload.nonce,
                    vault_key_identity,
                    (
                        "tool-codex-api-key"
                        if is_codex_provider
                        else "provider-oauth-profile"
                        if payload.kind in {"agent_profile", "oauth_profile"}
                        else "provider-api-key"
                    ),
                ),
            )
            secret_materials.append(
                RuntimeSecretMaterial(
                    secret_ref=secret_ref,
                    ciphertext=payload.encrypted_payload,
                    nonce=payload.nonce,
                    error_message="Hosted runtime provider secret source is invalid",
                )
            )

    providers = {
        provider_id: provider_material[provider_id] for provider_id in runtime["provider_ids"]
    }
    if primary_model and providers.get(primary_model["provider_id"], {}).get(
        "configurationMode"
    ) in {"connection", "custom"}:
        runtime["primary_model"] = None
        primary_model = None
    if runtime.get("providerMode") == "configured" and not primary_model:
        native_chat = [
            entry
            for entry in providers.values()
            if entry.get("configurationMode") in {"native", "connection", "custom"}
        ]
        if len(native_chat) == 1:
            # Native-owned chat consumes managed bindings only for embeddings.
            # Keep the shared source intact for the independently bound Codex tool.
            for provider_id, entry in providers.items():
                if entry.get("managed_by") != "clawdi":
                    continue
                embedding_models = [
                    model
                    for model in entry.get("models", [])
                    if model.get("capabilities", {}).get("embeddings") is True
                    and model.get("capabilities", {}).get("chat") is False
                ]
                if embedding_models:
                    providers[provider_id] = {**entry, "models": embedding_models}
        catalog_chat = [
            entry
            for entry in providers.values()
            if entry.get("configurationMode") not in {"native", "connection", "custom"}
            and not (
                entry.get("managed_by") == "clawdi"
                and entry.get("models")
                and all(
                    model.get("capabilities", {}).get("embeddings") is True
                    for model in entry["models"]
                )
            )
        ]
        if len(native_chat) != 1 or catalog_chat:
            raise RuntimeSourceError(
                "Configured chat requires a primary model or native credentials"
            )
    tool_projection = tools.model_dump(
        exclude={"codex"},
        exclude_none=True,
        exclude_unset=True,
        mode="json",
    )
    codex_provider_material = provider_material[codex_agent_provider_id]
    codex_provider_input = {
        "kind": "openai-compatible",
        "type": codex_provider_material.get("type"),
        "baseUrl": codex_provider_material.get("baseUrl"),
        "apiMode": _CODEX_TOOL_API_MODE,
        "managed_by": codex_provider_material.get("managed_by"),
        "runtimeEnvName": _MANAGED_PROVIDER_RUNTIME_ENV,
        "apiKeySecretRef": codex_provider_material.get("apiKeySecretRef"),
    }
    try:
        codex_provider = HostedCodexProviderProjection.model_validate(
            codex_provider_input
        ).model_dump(exclude_none=True, mode="json")
    except ValidationError as exc:
        raise RuntimeSourceError("Hosted Codex tool provider projection is invalid") from exc
    terminal_tooling = {
        "codex": {
            **_agent_codex_tool(codex_tool.model_dump(mode="json")),
            "provider": codex_provider,
        }
    }

    manifest: dict[str, Any] = {
        "schemaVersion": "clawdi.hosted-runtime.manifest.v1",
        "deploymentId": state.deployment_id,
        "environmentId": str(environment_id),
        "instanceId": state.instance_id,
        "generation": state.generation,
        "issuedAt": runtime_manifest_issued_at(state),
        "runtime": runtime_name,
        "locale": locale.model_dump(),
        "system": system.model_dump(
            exclude={"hermesDashboardAuth"}, exclude_none=True, mode="json"
        ),
        "controlPlane": {"cloudApiUrl": public_api_url.rstrip("/")},
        "clawdiCli": {
            "source": "npm:clawdi",
            "packageSpec": cli_package_spec,
            "registry": "https://registry.npmjs.org",
        },
        "runtimes": {runtime_name: runtime},
        "providers": providers,
        "liveSync": live_sync.model_dump(mode="json"),
        "recovery": recovery.model_dump(mode="json"),
    }
    handoffs: list[dict[str, Any]] = []
    for (provider_owner, provider_id), provider in batch.providers.items():
        if provider_owner != user_id or not provider.identity_handoff_pending:
            continue
        from app.schemas.provider_environment_repair import ProviderIdentityHandoffReceipt

        pending = ProviderIdentityHandoffReceipt.model_validate(provider.identity_handoff)
        for proof in pending.intent.proofs:
            if proof.binding.environment_id != environment_id:
                continue
            handoffs.append(
                {
                    "handoffId": str(pending.handoff_id),
                    "providerId": provider_id,
                    "cloudIdentity": {
                        "providerUuid": str(pending.provider_uuid),
                        "incarnationId": str(pending.incarnation_id),
                    },
                    "runtime": proof.binding.runtime,
                    "envName": pending.native_env_name,
                    "baseUrl": provider.base_url,
                    "apiMode": effective_provider_api_mode(provider.type, provider.api_mode),
                    "expectedJournalSha256": proof.journal_sha256,
                    "expectedConfigSha256": proof.config_sha256,
                    "expectedEnvSha256": proof.native_env_sha256,
                    "journalEnvName": proof.journal_env_name,
                    "owned": proof.native_env_name is not None,
                }
            )
    if handoffs:
        if not project_provider_identity:
            raise RuntimeSourceCapabilityError(
                "Pending provider handoff requires an identity-capable CLI"
            )
        manifest["providerHandoffs"] = sorted(handoffs, key=lambda item: item["providerId"])
    if not project_provider_identity:
        for entry in providers.values():
            if entry.get("cloudIdentity") is not None:
                raise RuntimeSourceCapabilityError(
                    "Provider identity requires an identity-capable CLI"
                )
    if dashboard_auth is not None:
        manifest["system"]["hermesDashboardAuth"] = dashboard_auth.model_dump(
            exclude_none=True, mode="json"
        )
    if egress_engine is not None:
        manifest["egressEngine"] = egress_engine.model_dump(
            exclude_none=True, exclude_unset=True, mode="json"
        )
    companion_omissions: tuple[CompanionEmitOmissionRecord, ...] = ()
    installation_projections = _project_companion_installations(
        batch.companion_installations.get(environment_id, ()),
        atoms_by_installation=batch.companion_installation_secrets,
        agent_name=_agent_display_name(row.environment),
        export_requests=batch.companion_export_requests,
        retired_installations=batch.companion_retired_installations.get(environment_id),
    )
    emitted_installations: list[_InstallationProjection] = []
    if companions is not None or installation_projections:
        # The pre-change condition additionally required a non-null filebrowser entry, so it
        # dropped the whole block whenever no filebrowser entry was present -- and an admitted
        # non-filebrowser kind could therefore never be emitted at all (T-4-28). That is the
        # opposite of COMP-01's intent and is fixed here, not preserved for "compatibility".
        declared = (
            companions.model_dump(exclude_none=True, exclude_unset=True, mode="json")
            if companions is not None
            else {}
        )
        # Tenant installations merge in BEFORE the gate, so a projected kind is gated exactly
        # like a provisioner-declared one. A kind both sources declare is emitted once, from
        # the installation: the tenant's install is authoritative for its own kind (plan
        # 06-09's add-alongside decision). The replaced provisioner entry is recorded as an
        # omission by KEY alone, like every other omission (T-4-07).
        shadowed = tuple(key for key in installation_projections if key in declared)
        for key, projection in installation_projections.items():
            declared[key] = projection.entry
        # `tuple(declared)` is the KEYS alone, copied. The gate is never handed a mapping that
        # holds entry values, so a companion's HS256 secret is structurally unreachable from an
        # omission record rather than merely absent from it (T-4-07).
        gate = _gate_companion_kinds(tuple(declared), row.observation)
        companion_omissions = _shadowed_omissions(shadowed, row.observation) + gate.omissions
        admitted = frozenset(gate.admitted)
        emitted = {key: value for key, value in declared.items() if key in admitted}
        # Assign only when something survives. An empty `companions: {}` is a POSITIVE
        # instruction to converge to zero companions and would stop a running filebrowser --
        # the availability regression D-14 rejects, arriving through the emit path instead.
        if emitted:
            # Same dump flags, same key order, so a filebrowser-only payload's rendered bytes
            # are unmoved: the projection is a filter over the identical dict (COMP-01/D-04).
            manifest["companions"] = emitted
        emitted_installations = [
            projection for key, projection in installation_projections.items() if key in emitted
        ]
    # An installation's credentials travel only with an entry that was actually emitted: a
    # runtime the gate withholds the kind from has no use for them.
    installation_atom_refs = _add_installation_secret_sources(
        emitted_installations,
        secret_sources=secret_sources,
        secret_materials=secret_materials,
        vault_key_identity=vault_key_identity,
    )
    if egress_profiles is not None:
        manifest["egressProfiles"] = egress_profiles.model_dump(
            exclude_none=True, exclude_unset=True, mode="json"
        )
    if mcp:
        manifest["mcp"] = mcp
    if skills is not None:
        manifest["skills"] = skills
    if project_agent_plugins and agent_plugins is not None:
        manifest["agentPlugins"] = agent_plugins.model_dump(mode="json")
    if tool_projection:
        manifest["tools"] = tool_projection
    manifest["terminalTooling"] = terminal_tooling

    bindings: list[dict[str, Any]] = []
    channel_rows = batch.channels.get(environment_id, ())
    projected_providers: set[str] = set()
    for account, link in channel_rows:
        if (
            account.provider in HOSTED_RUNTIME_SINGLE_ACCOUNT_PROVIDERS
            and account.provider in projected_providers
        ):
            raise RuntimeSourceError(
                hosted_agent_provider_link_limit_detail(account.provider, duplicate=True)
            )
        projected_providers.add(account.provider)
        if not link.encrypted_agent_token or not link.agent_token_nonce:
            raise RuntimeSourceError("Active runtime channel link has no token material")
        account_key = channel_runtime_account_key(account.id)
        if account.provider == CHANNEL_PROVIDER_WHATSAPP:
            agent_ref = f"secret://channels/whatsapp/{account_key}/links/{link.id}/agent-token"
            placeholder_ref = (
                f"secret://channels/whatsapp/{account_key}/links/{link.id}/egress-capability"
            )
            credential = batch.channel_credentials.get(link.id)
            auth_cert = batch.whatsapp_auth_certs.get(account.id)
            if credential is None or auth_cert is None:
                raise RuntimeSourceError(
                    "Active runtime WhatsApp Link has no synthetic credential material"
                )
            credential_ref = (
                f"secret://channels/whatsapp/{account_key}/credentials/{credential.id}/creds-json"
            )
            bindings.append(
                {
                    "provider": CHANNEL_PROVIDER_WHATSAPP,
                    "accountId": str(account.id),
                    "accountKey": account_key,
                    "linkId": str(link.id),
                    "agentTokenSecretRef": agent_ref,
                    "placeholderTokenSecretRef": placeholder_ref,
                    "credential": {
                        "id": str(credential.id),
                        "credsSecretRef": credential_ref,
                        "authCert": {
                            "SERIAL": auth_cert.serial,
                            "ISSUER": "clawdi",
                            "PUBLIC_KEY": buffer_json(auth_cert.root_public_key),
                        },
                    },
                }
            )
            _add_secret_source(
                secret_sources,
                credential_ref,
                _secret_identity(
                    credential.id,
                    credential.encrypted_credentials,
                    credential.credential_nonce,
                    vault_key_identity,
                    "channel-whatsapp-synthetic-credential",
                ),
            )
            secret_materials.append(
                RuntimeSecretMaterial(
                    secret_ref=credential_ref,
                    ciphertext=credential.encrypted_credentials,
                    nonce=credential.credential_nonce,
                    error_message="Hosted runtime WhatsApp credential source is invalid",
                )
            )
        else:
            agent_ref = f"secret://channels/{account.provider}/{account_key}/agent-token"
            placeholder_ref = (
                f"secret://channels/{account.provider}/{account_key}/placeholder-token"
            )
            bindings.append(
                {
                    "provider": account.provider,
                    "accountKey": account_key,
                    "agentTokenSecretRef": agent_ref,
                    "placeholderTokenSecretRef": placeholder_ref,
                }
            )
        _add_secret_source(
            secret_sources,
            agent_ref,
            _secret_identity(
                link.id,
                link.encrypted_agent_token,
                link.agent_token_nonce,
                vault_key_identity,
                "channel-agent-token",
            ),
        )
        secret_materials.append(
            RuntimeSecretMaterial(
                secret_ref=agent_ref,
                ciphertext=link.encrypted_agent_token,
                nonce=link.agent_token_nonce,
                error_message="Hosted runtime channel secret source is invalid",
            )
        )

    if decrypt_secrets:
        # Walk the WHOLE list before failing on an unreadable secret, so the error names every
        # ref an operator must recover rather than one per retry. Any other failure keeps its
        # original first-failure behaviour.
        unreadable_refs: set[str] = set()
        first_unreadable_message: str | None = None
        for material in secret_materials:
            try:
                secrets[material.secret_ref] = _decrypt_material(material)
            except InvalidTag:
                unreadable_refs.add(material.secret_ref)
                if first_unreadable_message is None:
                    first_unreadable_message = material.error_message
            except Exception as exc:
                raise RuntimeSourceError(material.error_message) from exc
        if first_unreadable_message is not None:
            raise RuntimeSecretUnreadableError(
                first_unreadable_message,
                secret_refs=tuple(sorted(unreadable_refs)),
            )
        for projection, atom_refs in installation_atom_refs:
            # The atoms were decrypted in the loop above, fail-closed with every other secret.
            # They are composed into the credential values and dropped: an atom ref is never
            # itself a secret value the runtime receives.
            atoms = {name: secrets.pop(ref) for name, ref in atom_refs.items()}
            secrets.update(
                compose_postiz_secret_values(
                    projection.installation, atoms, agent_name=projection.agent_name
                )
            )
        for binding in bindings:
            placeholder_ref = binding["placeholderTokenSecretRef"]
            secrets[placeholder_ref] = channel_runtime_placeholder_token(
                binding["provider"],
                binding["accountKey"],
                link_id=(
                    UUID(binding["linkId"])
                    if binding["provider"] == CHANNEL_PROVIDER_WHATSAPP
                    else None
                ),
            )

    descriptor: dict[str, object] = {
        "rendererRevision": RUNTIME_SOURCE_RENDERER_REVISION,
        "schemaVersion": RUNTIME_BUNDLE_V2_SCHEMA_VERSION,
        "manifest": manifest,
        "channelBindings": bindings,
        "secretSources": secret_sources,
    }
    if state.apply_generation is not None:
        descriptor["applyGeneration"] = state.apply_generation
    source_revision = hashlib.sha256(_canonical(descriptor).encode()).hexdigest()
    return RenderedRuntimeSource(
        manifest=manifest,
        channel_bindings=bindings,
        secret_values=secrets,
        source_revision=source_revision,
        apply_generation=state.apply_generation,
        companion_omissions=companion_omissions,
    )


def render_runtime_bundle(source: RenderedRuntimeSource) -> dict[str, Any]:
    bundle: dict[str, object] = {
        "schemaVersion": RUNTIME_BUNDLE_V2_SCHEMA_VERSION,
        "sourceRevision": source.source_revision,
        "manifest": source.manifest,
        "channelBindings": source.channel_bindings,
        "secretValues": source.secret_values,
    }
    if source.apply_generation is not None:
        bundle["applyGeneration"] = source.apply_generation
    return bundle


def render_legacy_bootstrap_runtime_bundle(
    state: HostedRuntimeState,
    *,
    environment_id: UUID,
) -> dict[str, Any]:
    try:
        cli_package_spec = validate_clawdi_cli_package_spec(state.cli_package_spec)
    except ValueError as exc:
        raise RuntimeSourceError("Hosted runtime CLI package spec is invalid") from exc
    bundle: dict[str, Any] = {
        "schemaVersion": RUNTIME_BUNDLE_V2_SCHEMA_VERSION,
        "applyGeneration": resolve_runtime_apply_generation(
            generation=state.generation,
            apply_generation=state.apply_generation,
        ),
        "manifest": {
            "schemaVersion": "clawdi.hosted-runtime.manifest.v1",
            "environmentId": str(environment_id),
            "clawdiCli": {
                "source": "npm:clawdi",
                "packageSpec": cli_package_spec,
                "registry": "https://registry.npmjs.org",
            },
        },
        "channelBindings": [],
        "secretValues": {},
    }
    source_revision = hashlib.sha256(
        _canonical(
            {
                "projectionRevision": LEGACY_BOOTSTRAP_PROJECTION_REVISION,
                **bundle,
            }
        ).encode()
    ).hexdigest()
    return {
        "schemaVersion": bundle["schemaVersion"],
        "sourceRevision": source_revision,
        "applyGeneration": bundle["applyGeneration"],
        "manifest": bundle["manifest"],
        "channelBindings": bundle["channelBindings"],
        "secretValues": bundle["secretValues"],
    }


def vault_key_identity(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _runtime(
    value: dict[str, JsonValue] | None,
) -> tuple[HostedRuntimeName, dict[str, Any]]:
    if not isinstance(value, dict) or len(value) != 1:
        raise RuntimeSourceError("hosted runtime state must select exactly one enabled runtime")
    name, raw = next(iter(value.items()))
    if name not in _SUPPORTED_RUNTIMES:
        raise RuntimeSourceError(f"unsupported enabled runtime: {name}")
    try:
        desired = validate_hosted_runtime_desired_state(raw)
    except ValidationError as exc:
        raise RuntimeSourceError(f"hosted runtime state for {name} is invalid") from exc
    runtime_name: HostedRuntimeName = "hermes" if name == "hermes" else "openclaw"
    return runtime_name, desired.model_dump(exclude_none=True, mode="json")


def _agent_runtime_binding(runtime: dict[str, Any]) -> dict[str, Any]:
    provider_ids = [
        runtime_managed_provider_id(provider_id) for provider_id in runtime["provider_ids"]
    ]
    if len(provider_ids) != len(set(provider_ids)):
        duplicate = next(
            provider_id
            for index, provider_id in enumerate(provider_ids)
            if provider_id in provider_ids[:index]
        )
        raise RuntimeSourceError(
            f"multiple provider bindings project to agent provider {duplicate}"
        )
    projected = {**runtime, "provider_ids": provider_ids}
    primary_model = runtime.get("primary_model")
    if _is_string_object_dict(primary_model):
        provider_id = primary_model.get("provider_id")
        if not isinstance(provider_id, str):
            raise RuntimeSourceError("Runtime primary model provider id is invalid")
        projected["primary_model"] = {
            **primary_model,
            "provider_id": runtime_managed_provider_id(provider_id),
        }
    return projected


def _is_string_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_object_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _agent_codex_tool(codex_tool: dict[str, Any]) -> dict[str, Any]:
    provider_id = runtime_managed_provider_id(codex_tool["provider_id"])
    return {
        **codex_tool,
        "provider_id": provider_id,
        "primary_model": {
            **codex_tool["primary_model"],
            "provider_id": provider_id,
        },
    }


def _exact_provider_source_id(
    *,
    bound_provider_id: str,
) -> str:
    """Validate and return the exact provider credential/catalog identity."""

    if bound_provider_id in V2_MANAGED_AI_PROVIDER_IDS:
        raise RuntimeSourceError(
            "Hosted v2 managed provider binding must use its exact deployment source"
        )
    if bound_provider_id.startswith(V2_DEPLOYMENT_MANAGED_AI_PROVIDER_PREFIX) and not (
        is_v2_deployment_managed_provider_id(bound_provider_id)
    ):
        raise RuntimeSourceError("Hosted v2 managed provider source is invalid")
    return bound_provider_id


def _selected_auth_payload(
    batch: RuntimeSourceBatch,
    provider: AiProvider,
    *,
    environment_id: UUID,
    runtime_name: HostedRuntimeName,
    allow_oauth: bool,
) -> AiProviderAuthPayload | None:
    metadata = provider.auth_metadata or {}
    is_managed_api_key = provider.auth_type == "api_key" and metadata.get("source") == "managed"
    is_managed_oauth = provider.auth_type in {"agent_profile", "oauth_profile"}
    if not is_managed_api_key and not is_managed_oauth:
        return None
    if is_managed_oauth and not allow_oauth:
        return None
    raw = metadata.get("profile")
    profile = raw if isinstance(raw, str) else "default"
    payload = batch.auth_payloads.get((provider.owner_user_id, provider.provider_id, profile))
    if payload is None:
        return None
    if is_managed_oauth and (
        payload.consumer_environment_id != environment_id
        or payload.consumer_runtime != runtime_name
    ):
        return None
    return payload


def _provider_entry(
    provider: AiProvider,
    *,
    secret_ref: str | None,
    credential_revision: str | None,
    selected_model: str | None,
    runtime_name: HostedRuntimeName = "openclaw",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": "openai-compatible",
        "type": provider.type,
        "baseUrl": provider.base_url,
    }
    managed = is_managed_provider_id(provider.provider_id) or (
        provider.managed_by == "clawdi"
        and provider.auth_type == "api_key"
        and (provider.auth_metadata or {}).get("source") == "managed"
    )
    api_mode = (
        managed_provider_api_mode(provider.provider_id) or provider.api_mode
        if managed
        else provider.api_mode
    )
    runtime_env = _MANAGED_PROVIDER_RUNTIME_ENV if managed else provider.runtime_env_name
    if provider.configuration_mode == "native":
        try:
            routing = native_provider(provider.native_provider, provider.native_variant)
        except ValueError as exc:
            raise RuntimeSourceError("Stored native provider identity is invalid") from exc
        if (
            managed
            or provider.models
            or any(
                getattr(provider, field) != getattr(routing, field)
                for field in ("type", "base_url", "api_mode", "runtime_env_name")
            )
        ):
            raise RuntimeSourceError("Native provider must contain only credential routing")
        runtime_routing = getattr(routing, runtime_name)
        result["type"] = runtime_routing.type or routing.type
        result["baseUrl"] = runtime_routing.base_url or routing.base_url
        api_mode = runtime_routing.api_mode or routing.api_mode
        result["configurationMode"] = "native"
        result["nativeProvider"] = runtime_routing.provider
        runtime_env = runtime_routing.env or routing.runtime_env_name
    if provider.configuration_mode in {"connection", "custom"}:
        api_mode = effective_provider_api_mode(provider.type, api_mode)
        if (
            managed
            or provider.managed_by != "user"
            or provider.auth_type not in {"api_key", "secret_ref"}
            or not runtime_env
            or not api_mode
            or not secret_ref
            or provider.native_provider
            or provider.native_variant
        ):
            raise RuntimeSourceError("Connection management requires user API-key routing")
        if provider.native_credential_authority:
            result["credentialAuthority"] = "native"
        result["configurationMode"] = provider.configuration_mode
        if provider.projects_cloud_identity:
            result["cloudIdentity"] = {
                "providerUuid": str(provider.id),
                "incarnationId": str(provider.incarnation_id),
            }
        result["managed_by"] = "user"
    if api_mode:
        result["apiMode"] = api_mode
    if provider.managed_by == "clawdi":
        result["managed_by"] = provider.managed_by
    models: list[dict[str, Any]] = []
    if provider.models is not None and provider.configuration_mode not in {"connection", "custom"}:
        try:
            models = [
                model.model_dump(exclude_none=True)
                for model in _AI_PROVIDER_MODELS_ADAPTER.validate_python(provider.models)
            ]
        except ValidationError as exc:
            raise RuntimeSourceError("Stored AI provider model metadata is invalid") from exc
    if (
        provider.configuration_mode not in {"native", "connection", "custom"}
        and selected_model
        and not any(model["id"] == selected_model for model in models)
    ):
        models.insert(0, {"id": selected_model})
    if models:
        result["models"] = models
    if runtime_env:
        result["runtimeEnvName"] = runtime_env
    if provider.auth_type in {"api_key", "secret_ref"} and not secret_ref:
        result["apiKeyRequired"] = True
        result["status"] = "error"
        result["error"] = {
            "code": "provider_secret_unavailable",
            "message": "provider requires an API key but no runtime secret value is available",
        }
    if secret_ref and provider.auth_type in {"api_key", "secret_ref"}:
        result["apiKeySecretRef"] = secret_ref
    metadata = provider.auth_metadata or {}
    if provider.auth_type == "agent_profile" and metadata.get("tool") == "codex":
        profile = metadata.get("profile")
        if isinstance(profile, str) and profile.strip():
            result["auth"] = {
                "type": "agent_profile",
                "tool": "codex",
                "profile": profile.strip(),
            }
            if secret_ref and credential_revision:
                result["auth"]["credentialSecretRef"] = secret_ref
                result["auth"]["credentialRevision"] = credential_revision
            else:
                result["status"] = "error"
                result["error"] = {
                    "code": "provider_oauth_credential_unavailable",
                    "message": "provider OAuth credential is not owned by this runtime",
                }
    return result


def _unhealthy_provider(provider_id: str, consumer: str) -> dict[str, Any]:
    return {
        "kind": "openai-compatible",
        "status": "error",
        "error": {
            "code": "provider_not_found",
            "message": f"provider required by {consumer} is missing or archived",
        },
    }


def _provider_secret_ref(value: str) -> str:
    return f"secret://provider.{value}.apiKey"


def _provider_oauth_secret_ref(value: str) -> str:
    return f"secret://provider.{value}.oauthProfile"


def _secret_identity(
    row_id: UUID,
    ciphertext: bytes,
    nonce: bytes,
    key_identity: str,
    kind: str,
    *,
    key_version: str | None = None,
) -> dict[str, str]:
    identity = {
        "kind": kind,
        "codecVersion": "aes-256-gcm.v1",
        "keyIdentity": key_identity,
        "rowIdentity": str(row_id),
        "ciphertextSha256": hashlib.sha256(ciphertext).hexdigest(),
        "nonceSha256": hashlib.sha256(nonce).hexdigest(),
    }
    if key_version is not None:
        identity["keyVersion"] = key_version
    return identity


def _add_secret_source(
    sources: dict[str, dict[str, str]],
    secret_ref: str,
    identity: dict[str, str],
) -> None:
    if secret_ref in sources:
        raise RuntimeSourceError(f"Runtime secret reference collision: {secret_ref}")
    sources[secret_ref] = identity


def _add_installation_secret_sources(
    projections: list[_InstallationProjection],
    *,
    secret_sources: dict[str, dict[str, str]],
    secret_materials: list[RuntimeSecretMaterial],
    vault_key_identity: str,
) -> list[tuple[_InstallationProjection, dict[str, str]]]:
    """Queue each emitted installation's atoms for the shared decrypt and record its sources.

    Every stored atom becomes one ``RuntimeSecretMaterial`` in the SAME list the hosted runtime
    secrets use, so an unreadable atom fails the manifest closed and is named, by its atom ref,
    through ``RuntimeSecretUnreadableError`` exactly like any other secret (plan 06-01).

    Each composed ref's source identity is derived from the identities of the atoms it is built
    from -- each carrying its key version (D-27) -- plus a digest of the non-secret composition
    inputs, so re-encrypting an atom, or changing a hostname or a digest, changes the source
    revision without the render decrypting anything. Returns, per installation, the atom refs to
    compose from once they are decrypted.
    """
    pending: list[tuple[_InstallationProjection, dict[str, str]]] = []
    for projection in projections:
        installation = projection.installation
        atom_identities: dict[str, dict[str, str]] = {}
        atom_refs: dict[str, str] = {}
        for atom in projection.atoms:
            try:
                validate_hosted_runtime_secret_key_version(atom.key_version)
            except RuntimeError as exc:
                raise RuntimeSourceError("Companion installation secret source is invalid") from exc
            atom_ref = postiz_atom_secret_ref(installation.id, atom.secret_name)
            atom_identities[atom.secret_name] = _secret_identity(
                atom.id,
                atom.encrypted_value,
                atom.nonce,
                vault_key_identity,
                "companion-installation-secret",
                key_version=atom.key_version,
            )
            atom_refs[atom.secret_name] = atom_ref
            secret_materials.append(
                RuntimeSecretMaterial(
                    secret_ref=atom_ref,
                    ciphertext=atom.encrypted_value,
                    nonce=atom.nonce,
                    error_message="Companion installation secret source is invalid",
                    key_version=atom.key_version,
                )
            )
        inputs_sha256 = hashlib.sha256(
            _canonical(projection.composition_inputs).encode()
        ).hexdigest()
        # The base atoms plus those of each install step that has run (plan 06-13's media and
        # Temporal credentials), so re-encrypting any of them is a new source revision.
        for name, atom_names in postiz_composed_atom_names(installation).items():
            composed_ref = postiz_secret_ref(installation.id, name)
            identity = {
                "kind": "companion-postiz-composed",
                "compositionInputsSha256": inputs_sha256,
            }
            for atom_name in atom_names:
                atom_identity = atom_identities.get(atom_name)
                if atom_identity is None:
                    # Refused here, before any decrypt, so the render fails the same way with
                    # or without `decrypt_secrets`.
                    raise RuntimeSourceError(
                        f"Companion credential {composed_ref} is missing a stored credential"
                    )
                identity[f"atom.{atom_name}.sha256"] = hashlib.sha256(
                    _canonical(atom_identity).encode()
                ).hexdigest()
                identity[f"atom.{atom_name}.keyVersion"] = atom_identity["keyVersion"]
            _add_secret_source(secret_sources, composed_ref, identity)
        pending.append((projection, atom_refs))
    return pending


def runtime_manifest_issued_at(state: HostedRuntimeState) -> str:
    value = _runtime_manifest_timestamp(state.updated_at, state.created_at)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _runtime_manifest_timestamp(updated_at: object, created_at: datetime) -> datetime:
    return updated_at if isinstance(updated_at, datetime) else created_at


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
