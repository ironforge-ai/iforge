"""The postiz manifest entry and its composed credentials, built from one installation (D-26, D-27).

An installation is an iforge-owned row, not a provisioner declaration, so it reaches the runtime
by being PROJECTED into the rendered manifest (``runtime_source.render_runtime_source``) rather
than by being written into ``hosted_runtime_states``: the provisioner replaces that column on
every upsert and would erase it (research Pattern 1, Pitfall 5).

This module owns the two halves of that projection and nothing else:

* ``project_postiz_entry`` -- the manifest entry, exactly the shape ``HostedPostizCompanion`` and
  the CLI's ``postizCompanionSchema`` accept (plan 06-05), validated before it is returned.
* ``compose_postiz_secret_values`` -- the values the entry's credential refs name, composed
  from the installation's decrypted atoms and its non-secret facts. Composing here means the
  runtime never needs to know Postiz's configuration semantics: it writes each value to a file.

**Injection guard (T-06-10-01).** The two env files are one ``KEY=VALUE`` line per key, values
never quoted. The guard is REFUSAL, not escaping: any input or composed value carrying a newline,
carriage return or NUL is refused with a ``RuntimeSourceError`` that names the composed ref and
never the value, so no credential can add a line to an environment file.

**Scoping (T-06-10-06).** Every ref is ``secret://companions/postiz/<installation_id>/...`` and a
composition reads only the atoms of the installation it is composing.

**The edge tunnel (plan 06-14).** Once the install job's ``edge`` step created this installation's
tunnel and stored its token, the entry names the digest-pinned ``cloudflared`` connector and a
fifth composed ref, ``tunnel-env``: the single line ``TUNNEL_TOKEN=<token>``, which the runtime
delivers to the connector as an encrypted per-unit credential. Before that, neither appears.

**The backup repository (plan 06-15).** Once the install job's ``backup`` step minted this
installation's no-delete B2 key and its repository cipher pass, the entry carries
``backup: {stanza, repoPath}`` and a sixth composed ref, ``backup-env``: every pgBackRest setting
for the installation's encrypted repository, ``PGBACKREST_EXPIRE_AUTO=n`` included. The runtime
delivers it to the database unit as a second encrypted credential. Before that, neither appears.

**Media and workflow engine (plan 06-13).** Once the install job's ``media`` and ``temporal`` steps
have run, their recorded handles (``external_resources["steps"]``) and the atoms they stored add
two key groups to the app env: Postiz's Cloudflare storage keys for this installation's own
bucket (CK-C) and its Temporal keys for this installation's own namespace (CK-A). An installation
whose steps have not run composes without them; one whose step ran but whose atom is missing is
refused like any other missing credential.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn
from urllib.parse import quote
from uuid import UUID

from pydantic import JsonValue

from app.core.companion_catalog import CompanionCatalogEntry
from app.models.companion_installation import (
    COMPANION_INSTALLATION_STATUS_RUNNING,
    COMPANION_OPERATION_UNINSTALL,
    COMPANION_OPERATION_UPDATE,
    CompanionInstallation,
)
from app.schemas.runtime import POSTIZ_PORT, HostedPostizCompanion

POSTIZ_COMPANION_KEY = "postiz"

# The four composed refs the entry's `credentials` object names (plan 06-05's interface).
POSTIZ_APP_ENV = "app-env"
POSTIZ_DB_ENV = "db-env"
POSTIZ_API_KEY = "api-key"
POSTIZ_ADMIN = "admin"
# The tunnel connector's env file (plan 06-14): one line, `TUNNEL_TOKEN=<token>`. Emitted only once
# the install job's `edge` step has created the tunnel and stored its token.
POSTIZ_TUNNEL_ENV = "tunnel-env"

# Which stored atoms each composed value is built from. The render derives each composed ref's
# secret source identity from exactly these atoms, so re-encrypting one changes every ref it
# feeds and nothing else.
POSTIZ_COMPOSED_ATOMS: dict[str, tuple[str, ...]] = {
    POSTIZ_APP_ENV: ("jwt-secret", "db-password"),
    POSTIZ_DB_ENV: ("db-password",),
    POSTIZ_API_KEY: ("api-key",),
    POSTIZ_ADMIN: ("admin-password",),
}

# The install job's steps that provision Postiz's two external dependencies (plan 06-13), the atoms
# they store, and -- per composed ref -- which of those atoms feed it once the step has run.
POSTIZ_MEDIA_STEP = "media"
POSTIZ_TEMPORAL_STEP = "temporal"
MEDIA_ACCESS_KEY_ID_ATOM = "media-access-key-id"
MEDIA_SECRET_ACCESS_KEY_ATOM = "media-secret-access-key"
TEMPORAL_API_KEY_ATOM = "temporal-api-key"
# The edge (plan 06-14): the tunnel is a provisioning step, its publication a completion step that
# runs only after the runtime's ready report, and the tunnel's connector token is one atom.
POSTIZ_EDGE_STEP = "edge"
POSTIZ_EDGE_PUBLISH_STEP = "edge-publish"
TUNNEL_TOKEN_ATOM = "tunnel-token"
# The backup repository (plan 06-15): a provisioning step mints the per-installation B2 key and
# the repository's cipher pass; the runtime receives every pgBackRest setting as ONE encrypted env
# credential, `backup-env`, which the database unit and the runtime's backup commands read.
POSTIZ_BACKUP_STEP = "backup"
POSTIZ_BACKUP_ENV = "backup-env"
BACKUP_KEY_ID_ATOM = "backup-key-id"
BACKUP_APPLICATION_KEY_ATOM = "backup-application-key"
BACKUP_CIPHER_PASS_ATOM = "backup-cipher-pass"
POSTIZ_STEP_COMPOSED_ATOMS: dict[str, dict[str, tuple[str, ...]]] = {
    POSTIZ_APP_ENV: {
        POSTIZ_MEDIA_STEP: (MEDIA_ACCESS_KEY_ID_ATOM, MEDIA_SECRET_ACCESS_KEY_ATOM),
        POSTIZ_TEMPORAL_STEP: (TEMPORAL_API_KEY_ATOM,),
    },
    # A composed ref of its own, present only once the step ran (plan 06-14).
    POSTIZ_TUNNEL_ENV: {POSTIZ_EDGE_STEP: (TUNNEL_TOKEN_ATOM,)},
    # Likewise, once the backup step ran (plan 06-15).
    POSTIZ_BACKUP_ENV: {
        POSTIZ_BACKUP_STEP: (
            BACKUP_KEY_ID_ATOM,
            BACKUP_APPLICATION_KEY_ATOM,
            BACKUP_CIPHER_PASS_ATOM,
        )
    },
}
# The house pattern (`config/deploy.yml`'s postgres accessory), per installation: an encrypted
# S3 repository with path-style addressing, zstd compression, two full backups kept by the
# operator's expire -- and `expire-auto` OFF, because pgBackRest defaults it to on and the
# runtime's key cannot delete anyway, so expiry is only ever the operator's act (D-15).
POSTIZ_BACKUP_SETTINGS: dict[str, str] = {
    "PGBACKREST_COMPRESS_TYPE": "zst",
    "PGBACKREST_EXPIRE_AUTO": "n",
    "PGBACKREST_REPO1_CIPHER_TYPE": "aes-256-cbc",
    "PGBACKREST_REPO1_RETENTION_FULL": "2",
    "PGBACKREST_REPO1_S3_URI_STYLE": "path",
    "PGBACKREST_REPO1_TYPE": "s3",
}
# R2 speaks S3 with a single pseudo-region.
POSTIZ_R2_REGION = "auto"

# The in-network names the runtime's units define (plan 06-08): the database and, under CK-A
# (plan 06-05, own-redis-shared-temporal), a per-installation Redis. Never a shared Redis: Postiz
# keeps OAuth state and PKCE verifiers under bare keys with no prefix option (research F3).
POSTIZ_DB_HOST = "iforge-postiz-db"
POSTIZ_DB_NAME = "postiz"
POSTIZ_DB_USER = "postiz"
POSTIZ_REDIS_URL = "redis://iforge-postiz-redis:6379"
# The house pgBackRest image's data path, as `config/deploy.yml` uses it (pg18 layout).
POSTIZ_PGDATA = "/var/lib/postgresql/18/docker"
# Postiz's own backend inside its container (research Pattern 3, verified at v2.24.0).
POSTIZ_BACKEND_INTERNAL_URL = "http://localhost:3000"

# The admin bootstrap's company field: the agent's name, bounded, or this default.
POSTIZ_ADMIN_COMPANY_DEFAULT = "Postiz"
POSTIZ_ADMIN_COMPANY_MAX_LENGTH = 64

_FORBIDDEN_CHARACTERS = ("\n", "\r", "\x00")


def postiz_secret_ref(installation_id: UUID | str, name: str) -> str:
    """The canonical ref of one composed postiz credential, scoped by installation id."""
    return f"secret://companions/postiz/{installation_id}/{name}"


def postiz_atom_secret_ref(installation_id: UUID | str, secret_name: str) -> str:
    """The ref a stored atom is decrypted under inside the render.

    Never emitted to a runtime: the atoms are composed into the four credential values and
    dropped. It exists so an unreadable atom is NAMED -- installation and atom -- by
    ``RuntimeSecretUnreadableError``, which is what an operator recovers (plan 06-25).
    """
    return postiz_secret_ref(installation_id, f"atoms/{secret_name}")


def postiz_credential_refs(installation_id: UUID | str) -> dict[str, str]:
    return {
        "appEnvSecretRef": postiz_secret_ref(installation_id, POSTIZ_APP_ENV),
        "dbEnvSecretRef": postiz_secret_ref(installation_id, POSTIZ_DB_ENV),
        "apiKeySecretRef": postiz_secret_ref(installation_id, POSTIZ_API_KEY),
        "adminSecretRef": postiz_secret_ref(installation_id, POSTIZ_ADMIN),
    }


def postiz_step_handles(
    installation: CompanionInstallation, step_name: str
) -> dict[str, JsonValue] | None:
    """The resources the newest recorded ``step_name`` step created, or ``None`` if none ran.

    A step recorded for an earlier operation still counts: an update keeps using the bucket and
    namespace its install created. A torn-down step is no longer in ``steps``.
    """
    steps = (installation.external_resources or {}).get("steps")
    if not isinstance(steps, list):
        return None
    for entry in reversed(steps):
        if isinstance(entry, dict) and entry.get("name") == step_name:
            resources = entry.get("resources")
            return dict(resources) if isinstance(resources, dict) else {}
    return None


def postiz_composed_atom_names(installation: CompanionInstallation) -> dict[str, tuple[str, ...]]:
    """Every composed ref's atoms for THIS installation: the base four plus each ran step's.

    A ref that exists only because of a step (the tunnel's env) appears once that step ran.
    """
    names = {ref: list(atoms) for ref, atoms in POSTIZ_COMPOSED_ATOMS.items()}
    for ref, by_step in POSTIZ_STEP_COMPOSED_ATOMS.items():
        for step_name, atoms in by_step.items():
            if postiz_step_handles(installation, step_name) is not None:
                names.setdefault(ref, []).extend(atoms)
    return {ref: tuple(atoms) for ref, atoms in names.items()}


def postiz_has_tunnel(installation: CompanionInstallation) -> bool:
    """Whether the install job created this installation's edge tunnel (plan 06-14)."""
    return postiz_step_handles(installation, POSTIZ_EDGE_STEP) is not None


def postiz_backup_handles(installation: CompanionInstallation) -> dict[str, JsonValue] | None:
    """What the install job's ``backup`` step recorded (plan 06-15), or ``None`` before it ran."""
    return postiz_step_handles(installation, POSTIZ_BACKUP_STEP)


def postiz_recorded_admin_user_id(installation: CompanionInstallation) -> str | None:
    """The admin user id a ready report recorded (plan 06-38), or ``None`` before one."""
    recorded = (installation.external_resources or {}).get("admin_user_id")
    return recorded if isinstance(recorded, str) and recorded else None


_LOWERCASE_SHA256 = re.compile(r"^[a-f0-9]{64}$")


def postiz_update_announcement(installation: CompanionInstallation) -> dict[str, str] | None:
    """The app-digest transition an accepted update asked for, while it runs (plan 06-41, CR-03).

    ``{fromDigest, toDigest}`` from the record ``decide_companion_update`` wrote, but only while
    the update operation is running and its ``to_digest`` is still the pin. After it completes,
    after a failure or deadline returned the pin (``revert_update_pin``), after a defer and on
    every install there is nothing to announce: a runtime still on another digest then has no
    instruction to change it, so a host on the newer images is never walked back to the older.
    """
    if installation.operation != COMPANION_OPERATION_UPDATE:
        return None
    if installation.status != COMPANION_INSTALLATION_STATUS_RUNNING:
        return None
    record = (installation.external_resources or {}).get("update")
    if not isinstance(record, dict):
        return None
    from_digest = record.get("from_digest")
    to_digest = record.get("to_digest")
    if not isinstance(from_digest, str) or _LOWERCASE_SHA256.fullmatch(from_digest) is None:
        return None
    if not isinstance(to_digest, str) or _LOWERCASE_SHA256.fullmatch(to_digest) is None:
        return None
    if to_digest != installation.pinned_digest:
        # A stale record: whatever it named is no longer what the control plane wants to run.
        return None
    return {"fromDigest": from_digest, "toDigest": to_digest}


def project_postiz_entry(
    installation: CompanionInstallation,
    catalog_entry: CompanionCatalogEntry,
    *,
    export_requests: Sequence[str] = (),
    retired_installation_ids: Sequence[str] = (),
) -> dict[str, Any]:
    """The ``companions.postiz`` manifest entry for one live installation.

    The app digest is the installation's PIN, not the catalog's current digest (D-28): a newer
    catalog digest is an offer, never a silent upgrade. The dependency digests come from the
    catalog. Once the install job's ``edge`` step created the tunnel, the entry names the
    ``cloudflared`` connector (the catalog's digest) and its ``tunnelTokenSecretRef``, together
    or not at all (plan 06-14). Once the ``backup`` step minted the repository's key, the entry
    carries ``backup: {stanza, repoPath}`` and ``backupEnvSecretRef``, again together or not at
    all (plan 06-15); before it, ``backup`` is null. ``exportRequests`` names each tenant export
    job still waiting for this installation's database dump (plan 06-19), oldest first; the runtime
    answers each by uploading the dump for that job, after which it disappears from the entry.

    ``lifecycle`` is ``remove`` while the installation's operation is an uninstall (plan 06-20),
    and ``present`` otherwise. ``remove`` is the ONLY instruction on which the runtime destroys the
    companion's data (research Pitfall 12): an entry that merely disappears is withdrawn -- its
    units stop -- and its data is kept. Once the runtime reported ``removed``, or the removal
    deadline passed, the projection drops the entry altogether
    (``uninstall_host_side_settled``).

    ``adminUserId`` is always emitted (plan 06-38): null until a ready report recorded
    ``external_resources.admin_user_id``, that id afterwards. The runtime adopts an existing
    Postiz organization only after proving it is the platform's, and once the installation reached
    ready that proof is this id -- the tenant may since have replaced the generated password.

    ``update`` is always emitted (plan 06-41): ``{fromDigest, toDigest}`` while an accepted update
    runs (``postiz_update_announcement``), null otherwise. The runtime changes the app digest it
    runs only on exactly that announced transition (CR-03).

    ``retiredInstallations`` is always emitted (plan 06-42): the agent's earlier installations of
    this kind whose tenant-confirmed uninstall completed without the host confirming removal,
    newest first (``retired_installation_ids``, from ``runtime_source``). A host still holding one
    of them removes its retained data through the guarded remove path before applying this
    installation; it never removes the data of an installation this list does not name (CR-04).

    Raises ``ValueError`` (a pydantic ``ValidationError``) if the entry would not parse, so a
    malformed installation row fails the render closed instead of reaching a runtime.
    """
    entry: dict[str, Any] = {
        "kind": POSTIZ_COMPANION_KEY,
        "installationId": str(installation.id),
        "isolationTier": installation.isolation_tier,
        "lifecycle": (
            "remove" if installation.operation == COMPANION_OPERATION_UNINSTALL else "present"
        ),
        "version": installation.version,
        "artifact": {"image": catalog_entry.app.image, "sha256": installation.pinned_digest},
        "dependencies": {
            "postgres": {
                "image": catalog_entry.postgres.image,
                "sha256": catalog_entry.postgres.sha256,
            },
            "redis": {
                "image": catalog_entry.redis.image,
                "sha256": catalog_entry.redis.sha256,
            },
        },
        "port": POSTIZ_PORT,
        "hostname": installation.hostname,
        "credentials": postiz_credential_refs(installation.id),
        "backup": None,
        "exportRequests": [{"jobId": str(job_id)} for job_id in export_requests],
        "adminUserId": postiz_recorded_admin_user_id(installation),
        "update": postiz_update_announcement(installation),
        "retiredInstallations": [
            {"installationId": str(retired_id)} for retired_id in retired_installation_ids
        ],
    }
    if postiz_has_tunnel(installation):
        entry["dependencies"]["cloudflared"] = {
            "image": catalog_entry.cloudflared.image,
            "sha256": catalog_entry.cloudflared.sha256,
        }
        entry["credentials"]["tunnelTokenSecretRef"] = postiz_secret_ref(
            installation.id, POSTIZ_TUNNEL_ENV
        )
    backup = postiz_backup_handles(installation)
    if backup is not None:
        entry["backup"] = {"stanza": backup.get("stanza"), "repoPath": backup.get("repo_path")}
        entry["credentials"]["backupEnvSecretRef"] = postiz_secret_ref(
            installation.id, POSTIZ_BACKUP_ENV
        )
    HostedPostizCompanion.model_validate(entry)
    return entry


def postiz_composition_inputs(
    installation: CompanionInstallation,
    catalog_entry: CompanionCatalogEntry,
    *,
    agent_name: str | None,
) -> dict[str, str]:
    """Every NON-secret fact a composed value or the entry depends on.

    The render hashes this into each composed ref's secret source identity, so a change of
    hostname, digest, tier, owner email or company changes the source revision and the runtime
    re-applies. Nothing here is a credential.
    """
    inputs = {
        "installationId": str(installation.id),
        "companionKey": installation.companion_key,
        "isolationTier": installation.isolation_tier,
        "version": installation.version,
        "hostname": installation.hostname,
        "ownerEmail": installation.owner_email,
        "company": postiz_admin_company(agent_name),
        "appDigest": installation.pinned_digest,
        "postgresDigest": catalog_entry.postgres.sha256,
        "redisDigest": catalog_entry.redis.sha256,
    }
    if postiz_has_tunnel(installation):
        # The connector's digest is part of what the runtime runs once the tunnel exists.
        inputs["cloudflaredDigest"] = catalog_entry.cloudflared.sha256
    # The media and Temporal handles are named in the app env, so a changed handle is a new
    # source revision. Present only once their step ran, so older snapshots are unchanged.
    for step_name in (POSTIZ_MEDIA_STEP, POSTIZ_TEMPORAL_STEP, POSTIZ_BACKUP_STEP):
        handles = postiz_step_handles(installation, step_name)
        if handles is not None:
            inputs[f"{step_name}Handles"] = json.dumps(
                handles, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            )
    return inputs


def postiz_admin_company(agent_name: str | None) -> str:
    """The admin bootstrap's company: the agent's name, bounded, or ``Postiz``.

    The name is the one composition input a tenant types, so it is CLEANED rather than refused:
    control characters are dropped (a renamed agent must not be able to wedge its own manifest),
    then the result is stripped and cut to 64 characters. It travels JSON-encoded, so no
    character that survives can break out of its field.
    """
    cleaned = "".join(
        character
        for character in agent_name or ""
        if ord(character) >= 0x20 and ord(character) != 0x7F
    ).strip()
    return cleaned[:POSTIZ_ADMIN_COMPANY_MAX_LENGTH].strip() or POSTIZ_ADMIN_COMPANY_DEFAULT


def compose_postiz_secret_values(
    installation: CompanionInstallation,
    atoms: Mapping[str, str],
    *,
    agent_name: str | None = None,
) -> dict[str, str]:
    """The composed credential values, keyed by their canonical refs.

    The four base values always; the tunnel's one-line env file once the tunnel exists.

    ``atoms`` is this installation's decrypted atoms by secret name. A missing atom, or any
    input or composed value containing a newline, carriage return or NUL, raises
    ``RuntimeSourceError`` naming the composed ref -- never the value.
    """
    installation_id = installation.id
    app_env_ref = postiz_secret_ref(installation_id, POSTIZ_APP_ENV)
    db_env_ref = postiz_secret_ref(installation_id, POSTIZ_DB_ENV)
    api_key_ref = postiz_secret_ref(installation_id, POSTIZ_API_KEY)
    admin_ref = postiz_secret_ref(installation_id, POSTIZ_ADMIN)

    hostname = _checked(app_env_ref, installation.hostname)
    jwt_secret = _atom(app_env_ref, atoms, "jwt-secret")
    db_password = _atom(app_env_ref, atoms, "db-password")
    public_url = f"https://{hostname}"
    app_values = {
        "API_LIMIT": "90",
        "BACKEND_INTERNAL_URL": POSTIZ_BACKEND_INTERNAL_URL,
        "DATABASE_URL": (
            f"postgresql://{POSTIZ_DB_USER}:{quote(db_password, safe='')}"
            f"@{POSTIZ_DB_HOST}:5432/{POSTIZ_DB_NAME}"
        ),
        "DISABLE_REGISTRATION": "true",
        "FRONTEND_URL": public_url,
        "IS_GENERAL": "true",
        "JWT_SECRET": jwt_secret,
        "MAIN_URL": public_url,
        "NEXT_PUBLIC_BACKEND_URL": f"{public_url}/api",
        "REDIS_URL": POSTIZ_REDIS_URL,
        "RUN_CRON": "true",
        "STORAGE_PROVIDER": "cloudflare",
    }
    app_values.update(_media_env(app_env_ref, installation, atoms))
    app_values.update(_temporal_env(app_env_ref, installation, atoms))
    app_env = _env_file(app_env_ref, app_values)
    db_env = _env_file(
        db_env_ref,
        {
            "PGDATA": POSTIZ_PGDATA,
            "POSTGRES_DB": POSTIZ_DB_NAME,
            "POSTGRES_PASSWORD": _atom(db_env_ref, atoms, "db-password"),
            "POSTGRES_USER": POSTIZ_DB_USER,
        },
    )
    api_key = _atom(api_key_ref, atoms, "api-key")
    admin = _checked(
        admin_ref,
        json.dumps(
            {
                "company": postiz_admin_company(agent_name),
                "email": _checked(admin_ref, installation.owner_email),
                "password": _atom(admin_ref, atoms, "admin-password"),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ),
    )
    composed = {app_env_ref: app_env, db_env_ref: db_env, api_key_ref: api_key, admin_ref: admin}
    if postiz_has_tunnel(installation):
        tunnel_ref = postiz_secret_ref(installation_id, POSTIZ_TUNNEL_ENV)
        # cloudflared reads its token from TUNNEL_TOKEN; `_checked`'s refusal keeps it one line.
        composed[tunnel_ref] = _env_file(
            tunnel_ref, {"TUNNEL_TOKEN": _atom(tunnel_ref, atoms, TUNNEL_TOKEN_ATOM)}
        )
    backup = postiz_backup_handles(installation)
    if backup is not None:
        backup_ref = postiz_secret_ref(installation_id, POSTIZ_BACKUP_ENV)
        composed[backup_ref] = _env_file(backup_ref, _backup_env(backup_ref, backup, atoms))
    return composed


def _backup_env(
    ref: str, handles: Mapping[str, JsonValue], atoms: Mapping[str, str]
) -> dict[str, str]:
    """Every pgBackRest setting for this installation's repository (plan 06-15).

    The repository location comes from what the ``backup`` step recorded, never from the current
    operator settings: a repository that already holds this installation's history stays where
    it is even if the operator later points new installations at another bucket.

    ``PGBACKREST_PG1_USER`` is the database's bootstrap superuser: the official image's initdb
    runs as ``POSTGRES_USER``, so no ``postgres`` role exists for pgBackRest's default to find.
    """
    return {
        **POSTIZ_BACKUP_SETTINGS,
        "PGBACKREST_PG1_PATH": POSTIZ_PGDATA,
        "PGBACKREST_PG1_USER": POSTIZ_DB_USER,
        "PGBACKREST_REPO1_CIPHER_PASS": _atom(ref, atoms, BACKUP_CIPHER_PASS_ATOM),
        "PGBACKREST_REPO1_PATH": _handle(ref, handles, "repo_path"),
        "PGBACKREST_REPO1_S3_BUCKET": _handle(ref, handles, "bucket"),
        "PGBACKREST_REPO1_S3_ENDPOINT": _handle(ref, handles, "s3_endpoint"),
        "PGBACKREST_REPO1_S3_KEY": _atom(ref, atoms, BACKUP_KEY_ID_ATOM),
        "PGBACKREST_REPO1_S3_KEY_SECRET": _atom(ref, atoms, BACKUP_APPLICATION_KEY_ATOM),
        "PGBACKREST_REPO1_S3_REGION": _handle(ref, handles, "s3_region"),
        "PGBACKREST_STANZA": _handle(ref, handles, "stanza"),
    }


def _media_env(
    ref: str, installation: CompanionInstallation, atoms: Mapping[str, str]
) -> dict[str, str]:
    """Postiz's Cloudflare storage keys for this installation's own bucket, once it exists."""
    handles = postiz_step_handles(installation, POSTIZ_MEDIA_STEP)
    if handles is None:
        return {}
    domain = _handle(ref, handles, "domain")
    return {
        "CLOUDFLARE_ACCOUNT_ID": _handle(ref, handles, "account_id"),
        "CLOUDFLARE_ACCESS_KEY": _atom(ref, atoms, MEDIA_ACCESS_KEY_ID_ATOM),
        "CLOUDFLARE_SECRET_ACCESS_KEY": _atom(ref, atoms, MEDIA_SECRET_ACCESS_KEY_ATOM),
        "CLOUDFLARE_BUCKETNAME": _handle(ref, handles, "bucket"),
        "CLOUDFLARE_BUCKET_URL": f"https://{domain}/",
        "CLOUDFLARE_REGION": POSTIZ_R2_REGION,
    }


def _temporal_env(
    ref: str, installation: CompanionInstallation, atoms: Mapping[str, str]
) -> dict[str, str]:
    """Postiz's Temporal keys for this installation's own namespace, once it exists."""
    handles = postiz_step_handles(installation, POSTIZ_TEMPORAL_STEP)
    if handles is None:
        return {}
    tls = handles.get("tls")
    if not isinstance(tls, bool):
        _refuse(ref, "has an invalid temporal handle")
    return {
        "TEMPORAL_ADDRESS": _handle(ref, handles, "address"),
        "TEMPORAL_NAMESPACE": _handle(ref, handles, "namespace"),
        "TEMPORAL_API_KEY": _atom(ref, atoms, TEMPORAL_API_KEY_ATOM),
        "TEMPORAL_TLS": "true" if tls else "false",
    }


def _handle(ref: str, handles: Mapping[str, JsonValue], key: str) -> str:
    value = handles.get(key)
    if not isinstance(value, str) or not value:
        _refuse(ref, f"has an invalid {key} handle")
    return _checked(ref, value)


def _env_file(ref: str, values: Mapping[str, str]) -> str:
    # One KEY=VALUE per line, keys sorted, values never quoted: the refusal in `_checked` is
    # what keeps a value to one line, so no quoting convention is needed for the runtime.
    return "".join(f"{key}={_checked(ref, values[key])}\n" for key in sorted(values))


def _atom(ref: str, atoms: Mapping[str, str], secret_name: str) -> str:
    value = atoms.get(secret_name)
    if not value:
        _refuse(ref, "is missing a stored credential")
    return _checked(ref, value)


def _checked(ref: str, value: str) -> str:
    if any(character in value for character in _FORBIDDEN_CHARACTERS):
        _refuse(ref, "contains a newline, carriage return or NUL")
    return value


def _refuse(ref: str, reason: str) -> NoReturn:
    # Imported here, not at module level: `runtime_source` imports this module to project
    # installations, so a module-level import back into it would be a cycle.
    from app.services.runtime_source import RuntimeSourceError

    raise RuntimeSourceError(f"Companion credential {ref} {reason}")
