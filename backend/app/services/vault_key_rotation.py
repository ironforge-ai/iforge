"""Agent-scoped vault key rotation and companion secret recovery (plan 06-25, OPS-07, D-11..D-13).

**Rotation** is incident response, never a schedule (D-13): an operator adds a key to the keyring,
makes it active, and calls ``POST /v1/admin/agents/{agent_id}/runtime-secrets/rotate`` for each
agent. That re-encrypts every READABLE versioned row of the agent -- its hosted runtime secrets and
its live companion installations' atoms -- under the active version, and reports by name each row
it could not read. The runtime then receives byte-identical values under a new source revision,
because each secret's source identity carries its ciphertext and key version (D-12: the runtime
re-converges, and a companion may restart).

What rotation does NOT reach: the unversioned tables (vault items, channel tokens, AI provider
credentials) have no ``key_version`` column and stay on ``vault.v1``. A compromised ``vault.v1`` is
therefore not retired by rotating agents; that residual is named in the runbook
(``docs/runbooks/vault-key-rotation.md``) and left to a later decision (research F7, D-11).

**Companion recovery** extends ``POST .../runtime-secrets/recover`` (plan 06-01) to the atoms a
companion installation's credentials are composed from. An atom cannot be replaced with an operator
value the way a hosted runtime secret can -- the platform generated it, and some are live
credentials at a provider -- so there are three actions, each refused unless what it replaces is
currently UNREADABLE (a readable atom is never quietly churned):

* ``companion_regenerate`` -- a new random ``db-password``, ``jwt-secret``, ``api-key`` or
  ``admin-password``, written without reading the old one. The runtime applies a new database
  password to the running database with ``ALTER ROLE`` before restarting Postiz.
* ``companion_reprovision`` -- a step's provider credential, revoked and re-minted where the
  provider allows it. It never runs the step's teardown: that empties and deletes the media bucket
  and marks the Temporal namespace deleted, which would destroy the tenant's data to recover a key.
* ``abandon_backups`` -- the ONLY way to replace an unreadable ``backup-cipher-pass``. A repository
  encrypted under a lost pass can never be restored by the platform again, so a new pass and a new
  stanza and repository path (suffix ``-r<n>``) start a fresh repository; the old one stays under
  retention and is named in the response and in ``backup_state`` as no longer restorable.

Every function here handles plaintext only transiently; nothing returns, logs or audits a value.
Rows are taken ``FOR UPDATE`` in one order everywhere -- hosted runtime secrets, then installations,
then companion atoms -- so concurrent rotations and recoveries serialize, and a second rotation
finds every row already current.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from cryptography.exceptions import InvalidTag
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm.attributes import flag_modified

from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.models.hosted_runtime import HostedRuntimeSecret
from app.services.companion_backup_repo import (
    CIPHER_PASS_BYTES,
    backup_generation_repo_path,
    backup_stanza,
    provision_backup_key,
)
from app.services.companion_edge import reissue_edge_tunnel_token
from app.services.companion_install import GENERATED_SECRET_ATOMS, generate_secret_atom
from app.services.companion_lifecycle_steps import (
    CompanionStepError,
    JsonObject,
    LifecycleStepContext,
    store_installation_atoms,
)
from app.services.companion_media_store import reissue_media_credential
from app.services.companion_postiz import (
    BACKUP_APPLICATION_KEY_ATOM,
    BACKUP_CIPHER_PASS_ATOM,
    BACKUP_KEY_ID_ATOM,
    MEDIA_ACCESS_KEY_ID_ATOM,
    MEDIA_SECRET_ACCESS_KEY_ATOM,
    POSTIZ_BACKUP_STEP,
    POSTIZ_EDGE_STEP,
    POSTIZ_MEDIA_STEP,
    POSTIZ_TEMPORAL_STEP,
    TEMPORAL_API_KEY_ATOM,
    TUNNEL_TOKEN_ATOM,
    postiz_step_handles,
)
from app.services.companion_temporal import mint_installation_temporal_jwt
from app.services.hosted_runtime_secrets import load_hosted_runtime_secrets_for_update
from app.services.vault_crypto import (
    VaultKeyVersionUnavailableError,
    active_key_version,
    decrypt_with_key_version,
    encrypt_with_active_key,
)

# The operator-facing name of one companion atom: ``companion:<installation_id>:<secret_name>``.
# Distinct from the render's internal ``secret://companions/postiz/<id>/atoms/<name>`` ref, which
# is never an operator input.
COMPANION_SECRET_REF_PREFIX = "companion"

# The atoms the platform generates itself, and so may regenerate.
REGENERABLE_ATOMS = frozenset(GENERATED_SECRET_ATOMS)

# The operator's step vocabulary -> (the step name the install job records, the atoms it stores).
# `backup-key` is the backup step's KEY only: its cipher pass is replaced by `abandon_backups`.
REPROVISION_STEPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "media": (POSTIZ_MEDIA_STEP, (MEDIA_ACCESS_KEY_ID_ATOM, MEDIA_SECRET_ACCESS_KEY_ATOM)),
    "temporal": (POSTIZ_TEMPORAL_STEP, (TEMPORAL_API_KEY_ATOM,)),
    "edge": (POSTIZ_EDGE_STEP, (TUNNEL_TOKEN_ATOM,)),
    "backup-key": (POSTIZ_BACKUP_STEP, (BACKUP_KEY_ID_ATOM, BACKUP_APPLICATION_KEY_ATOM)),
}
_ATOM_STEP = {
    atom: step for step, (_recorded, atoms) in REPROVISION_STEPS.items() for atom in atoms
}

CREDENTIALS_REGENERATED_AT_KEY = "credentials_regenerated_at"
ABANDONED_REPOSITORIES_KEY = "abandoned_repositories"
# Backup bookkeeping that describes the abandoned repository, moved into its record.
_ABANDONED_BACKUP_STATE_KEYS = (
    "markers",
    "last_backup_at",
    "last_backup_ok",
    "last_backup_label",
    "last_ok_backup_at",
)


# The two tables whose rows record the key version that encrypted them.
type VersionedSecretRow = HostedRuntimeSecret | CompanionInstallationSecret


def companion_secret_ref(installation_id: UUID | str, secret_name: str) -> str:
    return f"{COMPANION_SECRET_REF_PREFIX}:{installation_id}:{secret_name}"


def readable_value(row: VersionedSecretRow) -> str | None:
    """The row's plaintext, or ``None`` when it cannot be read.

    Unreadable means the key its version names cannot authenticate it (``InvalidTag``: lost,
    replaced or tampered) or the keyring no longer holds that version at all -- both are "this
    row's key is gone", which an operator recovers. A vault that is not configured at all still
    raises ``RuntimeError``: that is this process's fault, not the row's.
    """
    try:
        return decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)
    except (InvalidTag, VaultKeyVersionUnavailableError):
        return None


def is_readable(row: VersionedSecretRow) -> bool:
    return readable_value(row) is not None


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


async def load_live_companion_secrets(
    db: AsyncSession, *, environment_id: UUID, for_update: bool
) -> list[CompanionInstallationSecret]:
    """The atoms of the agent's LIVE installations, ordered by installation and name.

    ``for_update`` locks only the secret rows (``FOR UPDATE OF companion_installation_secrets``),
    never the installation, so a lifecycle step holding its installation row is not blocked.
    """
    query = (
        select(CompanionInstallationSecret)
        .join(
            CompanionInstallation,
            CompanionInstallation.id == CompanionInstallationSecret.installation_id,
        )
        .where(
            CompanionInstallation.environment_id == environment_id,
            CompanionInstallation.removed_at.is_(None),
        )
        .order_by(
            CompanionInstallationSecret.installation_id, CompanionInstallationSecret.secret_name
        )
    )
    if for_update:
        query = query.with_for_update(of=CompanionInstallationSecret).execution_options(
            populate_existing=True
        )
    return list((await db.execute(query)).scalars())


# ---------------------------------------------------------------------------
# Rotation.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    active_key_version: str
    reencrypted: list[str]
    already_current: list[str]
    unreadable: list[str]


def _named_rows(
    hosted: Iterable[HostedRuntimeSecret], atoms: Iterable[CompanionInstallationSecret]
) -> list[tuple[str, VersionedSecretRow]]:
    named: list[tuple[str, VersionedSecretRow]] = [(row.secret_ref, row) for row in hosted]
    named.extend((companion_secret_ref(row.installation_id, row.secret_name), row) for row in atoms)
    return named


async def rotate_agent_secrets(db: AsyncSession, *, environment_id: UUID) -> RotationOutcome:
    """Re-encrypt every readable versioned row of one agent under the active key version.

    Refuses up front (``RuntimeError``) when the active version is not in the keyring. Then, under
    ``FOR UPDATE`` on every row it may touch:

    * a readable row on another version is re-encrypted under the active one (``reencrypted``);
    * a readable row already on the active version is left alone (``already_current``);
    * a row that cannot be read under its own version is left untouched (``unreadable``) --
      recover it with ``POST .../runtime-secrets/recover``.

    The caller commits and audits. Idempotent: a second call reports every row already current.
    """
    active = active_key_version()
    hosted = await load_hosted_runtime_secrets_for_update(db, environment_id=environment_id)
    atoms = await load_live_companion_secrets(db, environment_id=environment_id, for_update=True)
    reencrypted: list[str] = []
    already_current: list[str] = []
    unreadable: list[str] = []
    for ref, row in _named_rows(hosted, atoms):
        plaintext = readable_value(row)
        if plaintext is None:
            unreadable.append(ref)
            continue
        if row.key_version == active:
            already_current.append(ref)
            continue
        row.encrypted_value, row.nonce, row.key_version = encrypt_with_active_key(plaintext)
        reencrypted.append(ref)
    return RotationOutcome(
        active_key_version=active,
        reencrypted=sorted(reencrypted),
        already_current=sorted(already_current),
        unreadable=sorted(unreadable),
    )


# ---------------------------------------------------------------------------
# Companion recovery.
# ---------------------------------------------------------------------------


class CompanionRecoveryRefused(Exception):
    """A request refused before anything changed. ``detail`` names refs and codes, never values."""

    def __init__(self, status_code: int, detail: dict[str, object]) -> None:
        super().__init__(str(detail.get("code")))
        self.status_code = status_code
        self.detail = detail


class CompanionReprovisionFailed(Exception):
    """A provider refused a reprovision. Earlier reprovisions of the same call have happened."""

    def __init__(self, done: list[CompanionReprovision], failed: CompanionReprovision, reason: str):
        super().__init__(reason)
        self.done = done
        self.failed = failed
        self.reason = reason


@dataclass(frozen=True, slots=True)
class CompanionRegenerate:
    installation_id: UUID
    secret_name: str

    @property
    def ref(self) -> str:
        return companion_secret_ref(self.installation_id, self.secret_name)


@dataclass(frozen=True, slots=True)
class CompanionReprovision:
    installation_id: UUID
    step: str


@dataclass(frozen=True, slots=True)
class CompanionRecoveryRequest:
    regenerate: tuple[CompanionRegenerate, ...] = ()
    reprovision: tuple[CompanionReprovision, ...] = ()
    abandon_backups: tuple[UUID, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.regenerate or self.reprovision or self.abandon_backups)

    @property
    def installation_ids(self) -> list[UUID]:
        ids = {
            *(item.installation_id for item in self.regenerate),
            *(item.installation_id for item in self.reprovision),
            *self.abandon_backups,
        }
        return sorted(ids, key=str)


@dataclass(frozen=True, slots=True)
class _InstallationFacts:
    """What a reprovision needs of an installation, read once without a lock."""

    installation_id: UUID
    user_id: UUID
    environment_id: UUID
    companion_key: str
    operation: str
    hostname: str
    handles: dict[str, JsonObject]


@dataclass(frozen=True, slots=True)
class CompanionRecoveryPlan:
    request: CompanionRecoveryRequest
    # Companion refs that were unreadable when the request began, read WITHOUT a lock: under the
    # lock, one of these that has become readable was healed by a concurrent call.
    unreadable_at_start: frozenset[str] = frozenset()
    installations: dict[UUID, _InstallationFacts] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AbandonedBackup:
    installation_id: UUID
    abandoned_stanza: str
    abandoned_repo_path: str
    stanza: str
    repo_path: str


@dataclass(slots=True)
class CompanionRecoveryOutcome:
    regenerated: list[str] = field(default_factory=list)
    already_recovered: list[str] = field(default_factory=list)
    abandoned: list[AbandonedBackup] = field(default_factory=list)
    remaining_unreadable: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.regenerated or self.abandoned)


def _refused(status_code: int, code: str, **detail: object) -> CompanionRecoveryRefused:
    return CompanionRecoveryRefused(status_code, {"code": code, **detail})


async def plan_companion_recovery(
    db: AsyncSession, *, environment_id: UUID, request: CompanionRecoveryRequest
) -> CompanionRecoveryPlan:
    """Validate a companion recovery WITHOUT a lock and before anything changes.

    Refusals, in this order: an installation that is not a live installation of this agent (404
    ``companion_installation_not_found``); an atom that is not stored (404
    ``runtime_secret_not_found``); a regenerate of the backup cipher pass (409, abandon backups
    instead) or of a provider credential (409, reprovision its step instead); a step that never
    ran (409 ``companion_step_not_provisioned``); and anything readable (409
    ``runtime_secret_readable``).
    """
    if request.is_empty:
        return CompanionRecoveryPlan(request=request)
    ids = request.installation_ids
    rows = {
        row.id: row
        for row in (
            await db.execute(
                select(CompanionInstallation).where(
                    CompanionInstallation.id.in_(ids),
                    CompanionInstallation.environment_id == environment_id,
                    CompanionInstallation.removed_at.is_(None),
                )
            )
        ).scalars()
    }
    unknown = [str(installation_id) for installation_id in ids if installation_id not in rows]
    if unknown:
        raise _refused(404, "companion_installation_not_found", installation_ids=unknown)
    atoms = {
        (row.installation_id, row.secret_name): row
        for row in (
            await db.execute(
                select(CompanionInstallationSecret).where(
                    CompanionInstallationSecret.installation_id.in_(ids)
                )
            )
        ).scalars()
    }
    unreadable = {key for key, row in atoms.items() if not is_readable(row)}
    installations = {
        installation_id: _InstallationFacts(
            installation_id=row.id,
            user_id=row.user_id,
            environment_id=row.environment_id,
            companion_key=row.companion_key,
            operation=row.operation,
            hostname=row.hostname,
            handles={
                recorded: handles
                for recorded, _atoms in REPROVISION_STEPS.values()
                if (handles := postiz_step_handles(row, recorded)) is not None
            },
        )
        for installation_id, row in rows.items()
    }

    missing = sorted(
        item.ref
        for item in request.regenerate
        if (item.installation_id, item.secret_name) not in atoms
        and item.secret_name not in _ATOM_STEP
    )
    if missing:
        raise _refused(404, "runtime_secret_not_found", secret_refs=missing)
    cipher = sorted(
        item.ref for item in request.regenerate if item.secret_name == BACKUP_CIPHER_PASS_ATOM
    )
    if cipher:
        raise _refused(
            409,
            "companion_backup_cipher_pass_requires_abandon",
            secret_refs=cipher,
            message="A backup cipher pass cannot be regenerated: the repository it encrypts would "
            "become unreadable. Use abandon_backups, which starts a new repository and leaves the "
            "old one under retention, no longer restorable by the platform.",
        )
    provider = sorted(item.ref for item in request.regenerate if item.secret_name in _ATOM_STEP)
    if provider:
        raise _refused(
            409,
            "companion_secret_requires_reprovision",
            secret_refs=provider,
            steps=sorted(
                {
                    _ATOM_STEP[item.secret_name]
                    for item in request.regenerate
                    if item.secret_name in _ATOM_STEP
                }
            ),
            message="A provider credential is re-minted at its provider: use "
            "companion_reprovision with its step.",
        )
    not_regenerable = sorted(
        item.ref for item in request.regenerate if item.secret_name not in REGENERABLE_ATOMS
    )
    if not_regenerable:
        raise _refused(404, "runtime_secret_not_found", secret_refs=not_regenerable)

    unprovisioned: list[dict[str, str]] = [
        {"installation_id": str(item.installation_id), "step": item.step}
        for item in request.reprovision
        if REPROVISION_STEPS[item.step][0] not in installations[item.installation_id].handles
    ]
    unprovisioned.extend(
        {"installation_id": str(installation_id), "step": POSTIZ_BACKUP_STEP}
        for installation_id in request.abandon_backups
        if POSTIZ_BACKUP_STEP not in installations[installation_id].handles
    )
    if unprovisioned:
        raise _refused(409, "companion_step_not_provisioned", steps=unprovisioned)

    cipher_lost = sorted(
        companion_secret_ref(item.installation_id, BACKUP_CIPHER_PASS_ATOM)
        for item in request.reprovision
        if item.step == "backup-key"
        and (item.installation_id, BACKUP_CIPHER_PASS_ATOM) in unreadable
    )
    if cipher_lost:
        raise _refused(
            409,
            "companion_backup_cipher_pass_requires_abandon",
            secret_refs=cipher_lost,
            message="The backup key cannot be re-minted while its repository's cipher pass is "
            "unreadable: abandon_backups first, in its own request.",
        )

    readable: set[str] = {
        item.ref
        for item in request.regenerate
        if (item.installation_id, item.secret_name) not in unreadable
    }
    for item in request.reprovision:
        step_atoms = REPROVISION_STEPS[item.step][1]
        keys = [(item.installation_id, atom) for atom in step_atoms]
        if all(key in atoms and key not in unreadable for key in keys):
            readable.update(companion_secret_ref(*key) for key in keys)
    for installation_id in request.abandon_backups:
        key = (installation_id, BACKUP_CIPHER_PASS_ATOM)
        if key in atoms and key not in unreadable:
            readable.add(companion_secret_ref(*key))
    if readable:
        raise _refused(409, "runtime_secret_readable", secret_refs=sorted(readable))

    return CompanionRecoveryPlan(
        request=request,
        unreadable_at_start=frozenset(companion_secret_ref(*key) for key in unreadable),
        installations=installations,
    )


def _step_context(
    facts: _InstallationFacts, sessionmaker: async_sessionmaker[AsyncSession]
) -> LifecycleStepContext:
    return LifecycleStepContext(
        installation_id=facts.installation_id,
        user_id=facts.user_id,
        environment_id=facts.environment_id,
        companion_key=facts.companion_key,
        operation=facts.operation,
        hostname=facts.hostname,
        sessionmaker=sessionmaker,
    )


async def _reissue_media(context: LifecycleStepContext, _handles: JsonObject) -> JsonObject:
    return await reissue_media_credential(context)


async def _reissue_temporal(context: LifecycleStepContext, handles: JsonObject) -> JsonObject:
    # A Temporal JWT is stateless: a new one is minted for the recorded namespace. The previous
    # one cannot be revoked short of rotating the operator's Temporal signing key (runbook).
    namespace = handles.get("namespace")
    if not isinstance(namespace, str) or not namespace:
        raise CompanionStepError(
            "temporal_handles_invalid", "the recorded temporal handles are incomplete"
        )
    api_key = mint_installation_temporal_jwt(
        installation_id=context.installation_id, namespace=namespace
    )
    await store_installation_atoms(context, {TEMPORAL_API_KEY_ATOM: api_key})
    return {}


async def _reissue_edge(context: LifecycleStepContext, _handles: JsonObject) -> JsonObject:
    await reissue_edge_tunnel_token(context)
    return {}


async def _reissue_backup_key(context: LifecycleStepContext, _handles: JsonObject) -> JsonObject:
    # The step's own provision revokes every key under this installation's key name before it
    # mints one, and keeps the cipher pass. Only the key id is taken from its answer: the
    # recorded stanza and repository path stay what they are (an abandoned repository's suffix).
    resources = await provision_backup_key(context)
    return {"key_id": resources["key_id"]}


_REISSUERS: dict[str, Callable[[LifecycleStepContext, JsonObject], Awaitable[JsonObject]]] = {
    "media": _reissue_media,
    "temporal": _reissue_temporal,
    "edge": _reissue_edge,
    "backup-key": _reissue_backup_key,
}


@dataclass(frozen=True, slots=True)
class ReprovisionResult:
    done: list[CompanionReprovision]
    # (installation id, recorded step name) -> handle updates to merge into the recorded step.
    handle_updates: dict[tuple[UUID, str], JsonObject]


async def reprovision_companion_credentials(
    db: AsyncSession, plan: CompanionRecoveryPlan
) -> ReprovisionResult:
    """Revoke and re-mint each requested step's provider credential. Holds NO row lock.

    Each step stores its new atoms through its own session, exactly as the install job does, so
    this runs before the caller takes its locks (a lock held here would block those writes).
    Raises ``CompanionReprovisionFailed`` naming the step and a value-free reason.
    """
    maker = async_sessionmaker(
        bind=db.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    done: list[CompanionReprovision] = []
    updates: dict[tuple[UUID, str], JsonObject] = {}
    for item in plan.request.reprovision:
        facts = plan.installations[item.installation_id]
        recorded = REPROVISION_STEPS[item.step][0]
        try:
            changed = await _REISSUERS[item.step](
                _step_context(facts, maker), facts.handles[recorded]
            )
        except CompanionStepError as exc:
            raise CompanionReprovisionFailed(done, item, exc.code) from None
        except Exception as exc:  # noqa: BLE001 - reported by class only: never a value.
            raise CompanionReprovisionFailed(done, item, exc.__class__.__name__) from None
        done.append(item)
        if changed:
            updates[(item.installation_id, recorded)] = changed
    return ReprovisionResult(done=done, handle_updates=updates)


def _set_resource(installation: CompanionInstallation, key: str, value: JsonValue) -> None:
    installation.external_resources = {**(installation.external_resources or {}), key: value}
    flag_modified(installation, "external_resources")


def _patch_step_handles(
    installation: CompanionInstallation, step_name: str, updates: JsonObject
) -> None:
    """Merge ``updates`` into the NEWEST recorded ``step_name`` step's resources."""
    raw = (installation.external_resources or {}).get("steps")
    steps = (
        [dict(entry) for entry in raw if isinstance(entry, dict)] if isinstance(raw, list) else []
    )
    for index in range(len(steps) - 1, -1, -1):
        if steps[index].get("name") == step_name:
            resources = steps[index].get("resources")
            current = dict(resources) if isinstance(resources, dict) else {}
            steps[index] = {**steps[index], "resources": {**current, **updates}}
            _set_resource(installation, "steps", cast(list[JsonValue], steps))
            return


def _abandon_backups(
    installation: CompanionInstallation,
    cipher_row: CompanionInstallationSecret | None,
    context: LifecycleStepContext,
    *,
    now: str,
) -> tuple[AbandonedBackup, CompanionInstallationSecret | None]:
    """Start a new repository under a new cipher pass; record the old one as unrestorable.

    Nothing at the provider changes: the old repository's files stay under the bucket's
    retention. The new repository is a subdirectory of the original path (``/<t>/<i>/r<n>``), so
    it lies inside the backup key's ``namePrefix`` and the existing key and the rehearsal keys
    keep reaching it (CR-01). The stanza takes the ``-r<n>`` suffix; a stanza is not an object
    path. Returns the abandonment and a NEW cipher-pass row when none existed.
    """
    handles = postiz_step_handles(installation, POSTIZ_BACKUP_STEP) or {}
    state: dict[str, JsonValue] = dict(installation.backup_state or {})
    history_raw = state.get(ABANDONED_REPOSITORIES_KEY)
    history = list(history_raw) if isinstance(history_raw, list) else []
    generation = len(history) + 1
    stanza = f"{backup_stanza(context)}-r{generation}"
    repo_path = backup_generation_repo_path(installation.user_id, installation.id, generation)
    old_stanza = str(handles.get("stanza") or "")
    old_repo_path = str(handles.get("repo_path") or "")
    record: dict[str, JsonValue] = {
        "stanza": old_stanza,
        "repo_path": old_repo_path,
        "bucket": handles.get("bucket"),
        "abandoned_at": now,
        "restorable_by_platform": False,
        "reason": "backup cipher pass unreadable",
    }
    for key in _ABANDONED_BACKUP_STATE_KEYS:
        if key in state:
            record[key] = state.pop(key)
    state["markers"] = []
    state[ABANDONED_REPOSITORIES_KEY] = [*history, record]
    installation.backup_state = state
    flag_modified(installation, "backup_state")
    _patch_step_handles(
        installation, POSTIZ_BACKUP_STEP, {"stanza": stanza, "repo_path": repo_path}
    )

    ciphertext, nonce, key_version = encrypt_with_active_key(
        secrets.token_urlsafe(CIPHER_PASS_BYTES)
    )
    created: CompanionInstallationSecret | None = None
    if cipher_row is None:
        created = CompanionInstallationSecret(
            installation_id=installation.id,
            secret_name=BACKUP_CIPHER_PASS_ATOM,
            encrypted_value=ciphertext,
            nonce=nonce,
            key_version=key_version,
        )
    else:
        cipher_row.encrypted_value, cipher_row.nonce, cipher_row.key_version = (
            ciphertext,
            nonce,
            key_version,
        )
    return (
        AbandonedBackup(
            installation_id=installation.id,
            abandoned_stanza=old_stanza,
            abandoned_repo_path=old_repo_path,
            stanza=stanza,
            repo_path=repo_path,
        ),
        created,
    )


async def recover_companion_secrets(
    db: AsyncSession,
    *,
    environment_id: UUID,
    plan: CompanionRecoveryPlan,
    handle_updates: dict[tuple[UUID, str], JsonObject] | None = None,
) -> CompanionRecoveryOutcome:
    """Apply a validated companion recovery under ``FOR UPDATE``; the caller audits and commits.

    Locks the named installations, then every live atom of the agent (the caller has already
    locked the hosted runtime secrets, so the order matches rotation). A named atom that was
    unreadable when the request began but is readable now was healed by a concurrent call and is
    reported under ``already_recovered``. ``remaining_unreadable`` lists every atom of the agent
    still unreadable afterwards.
    """
    request = plan.request
    installations: dict[UUID, CompanionInstallation] = {}
    if request.installation_ids:
        installations = {
            row.id: row
            for row in (
                await db.execute(
                    select(CompanionInstallation)
                    .where(CompanionInstallation.id.in_(request.installation_ids))
                    .order_by(CompanionInstallation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
        }
    atoms = await load_live_companion_secrets(db, environment_id=environment_id, for_update=True)
    by_key = {(row.installation_id, row.secret_name): row for row in atoms}
    outcome = CompanionRecoveryOutcome()
    now = _utcnow_iso()
    maker = async_sessionmaker(bind=db.bind, expire_on_commit=False)

    for item in request.regenerate:
        row = by_key.get((item.installation_id, item.secret_name))
        if row is None or is_readable(row):
            if item.ref in plan.unreadable_at_start:
                outcome.already_recovered.append(item.ref)
            continue
        row.encrypted_value, row.nonce, row.key_version = encrypt_with_active_key(
            generate_secret_atom(item.secret_name)
        )
        _set_resource(installations[item.installation_id], CREDENTIALS_REGENERATED_AT_KEY, now)
        outcome.regenerated.append(item.ref)

    for installation_id in request.abandon_backups:
        cipher_row = by_key.get((installation_id, BACKUP_CIPHER_PASS_ATOM))
        ref = companion_secret_ref(installation_id, BACKUP_CIPHER_PASS_ATOM)
        if cipher_row is not None and is_readable(cipher_row):
            if ref in plan.unreadable_at_start:
                outcome.already_recovered.append(ref)
            continue
        abandoned, created = _abandon_backups(
            installations[installation_id],
            cipher_row,
            _step_context(plan.installations[installation_id], maker),
            now=now,
        )
        if created is not None:
            db.add(created)
            by_key[(installation_id, BACKUP_CIPHER_PASS_ATOM)] = created
        outcome.abandoned.append(abandoned)

    for (installation_id, step_name), updates in (handle_updates or {}).items():
        installation = installations.get(installation_id)
        if installation is not None:
            _patch_step_handles(installation, step_name, updates)

    outcome.regenerated.sort()
    outcome.already_recovered.sort()
    outcome.remaining_unreadable = sorted(
        companion_secret_ref(*key) for key, row in by_key.items() if not is_readable(row)
    )
    return outcome


__all__ = [
    "COMPANION_SECRET_REF_PREFIX",
    "REGENERABLE_ATOMS",
    "REPROVISION_STEPS",
    "AbandonedBackup",
    "CompanionRecoveryOutcome",
    "CompanionRecoveryPlan",
    "CompanionRecoveryRefused",
    "CompanionRecoveryRequest",
    "CompanionRegenerate",
    "CompanionReprovision",
    "CompanionReprovisionFailed",
    "ReprovisionResult",
    "RotationOutcome",
    "companion_secret_ref",
    "is_readable",
    "load_live_companion_secrets",
    "plan_companion_recovery",
    "readable_value",
    "recover_companion_secrets",
    "reprovision_companion_credentials",
    "rotate_agent_secrets",
]
