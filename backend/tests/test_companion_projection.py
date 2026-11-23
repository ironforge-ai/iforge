"""A tenant's installation reaches its agent's runtime as a manifest entry (APP-01, APP-03, D-26).

The manifest is the only channel from control-plane configuration to compute. The provisioner
owns ``hosted_runtime_states`` and replaces its ``companions`` column wholesale on every upsert, so
an installation is NOT written there: it lives in iforge-owned tables and is merged into the
rendered manifest at render time, under the same emit gate as a provisioner-declared kind, with
its credentials decrypted in the same fail-closed loop as the hosted runtime secrets (D-27).

These tests drive the real batch loader and renderer against a migrated database, so a query that
silently loaded nothing would fail them rather than pass them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from pydantic import ValidationError
from sqlalchemy import select

from app.core.auth import AuthContext
from app.core.companion_catalog import catalog_entry
from app.core.config import settings
from app.core.database import get_control_session, get_session
from app.main import app
from app.models.companion_installation import CompanionInstallation, CompanionInstallationSecret
from app.models.hosted_runtime import (
    HostedRuntimeConfigObservation,
    HostedRuntimeSecret,
    HostedRuntimeState,
)
from app.models.session import AgentEnvironment
from app.schemas.runtime import HostedPostizCompanion
from app.services.companion_install import create_installation
from app.services.companion_postiz import compose_postiz_secret_values, project_postiz_entry
from app.services.runtime_source import (
    CompanionEmitOmission,
    RenderedRuntimeSource,
    RuntimeSecretUnreadableError,
    RuntimeSourceError,
    load_runtime_source_batch,
    render_runtime_source,
)
from app.services.vault_crypto import decrypt, encrypt
from tests.companion_billing_stub import billing_quote
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state
from tests.doc_contract import REPO_ROOT
from tests.hosted_runtime_fixtures import (
    CANONICAL_CODEX_TOOL_PROVIDER_ID,
    CANONICAL_CODEX_TOOLS,
    ensure_canonical_codex_tool_provider,
    filebrowser_companion,
)
from tests.test_hosted_runtime_secrets import foreign_ciphertext
from tests.test_platform_endpoints import (
    _ADMIN_KEY,
    _CLERK_ISSUER,
    _clerk_owner,
    _create_platform_agent,
    _headers,
    _runtime_body,
)

ZONE = "companions.example"
POSTIZ_MIN_CLI_VERSION = "0.14.0"
# The CLI version every deployed runtime reports today (`packages/cli/package.json`).
DEPLOYED_CLI_VERSION = "0.13.47"
CREDENTIAL_NAMES = ("app-env", "db-env", "api-key", "admin")
FIXTURE_PATH = REPO_ROOT / "test-fixtures" / "companions" / "postiz-manifest-entry.json"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


def _ref(installation_id: UUID, name: str) -> str:
    # Spelled out here rather than imported from the module under test, so a wrong ref shape in
    # the implementation cannot also be the expectation.
    return f"secret://companions/postiz/{installation_id}/{name}"


async def _agent_with_runtime(
    db_session, seed_user, *, cli_version: str = POSTIZ_MIN_CLI_VERSION
) -> UUID:
    """One agent with a renderable hosted runtime state and a runtime reporting ``cli_version``."""
    env = await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"projection-{uuid.uuid4().hex[:12]}",
        machine_name="Projection Agent",
    )
    environment_id = env.id
    state = await create_test_hosted_runtime_state(db_session, env, runtime_name="openclaw")
    # The render refuses a state whose terminal tooling is unresolvable.
    state.tools = CANONICAL_CODEX_TOOLS
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    observation = await db_session.get(HostedRuntimeConfigObservation, environment_id)
    assert observation is not None
    observation.diagnostics = {**observation.diagnostics, "activeCliVersion": cli_version}
    await db_session.commit()
    return environment_id


async def _installation(
    db_session, seed_user, environment_id: UUID, *, status: str = "running"
) -> UUID:
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=environment_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    installation_id = installation.id
    installation.status = status
    await db_session.commit()
    return installation_id


async def _atoms(db_session, installation_id: UUID) -> dict[str, str]:
    rows = (
        (
            await db_session.execute(
                select(CompanionInstallationSecret).where(
                    CompanionInstallationSecret.installation_id == installation_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.secret_name: decrypt(row.encrypted_value, row.nonce) for row in rows}


async def _render(
    db_session, environment_id: UUID, *, decrypt_secrets: bool = True
) -> RenderedRuntimeSource:
    batch = await load_runtime_source_batch(db_session, environment_ids=[environment_id])
    return render_runtime_source(
        batch,
        environment_id=environment_id,
        public_api_url="https://cloud.test",
        vault_key_identity="test-key-version",
        decrypt_secrets=decrypt_secrets,
    )


async def test_running_installation_is_projected_with_its_credentials(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)
    installation = await db_session.get(CompanionInstallation, installation_id)
    assert installation is not None

    source = await _render(db_session, environment_id)

    companions = source.manifest.get("companions", {})
    assert "postiz" in companions, "a running installation must reach its agent's manifest"
    entry = HostedPostizCompanion.model_validate(companions["postiz"])
    assert entry.installationId == str(installation_id)
    assert entry.isolationTier == "standard"
    assert entry.lifecycle == "present"
    assert entry.hostname == installation.hostname
    assert entry.artifact.sha256 == installation.pinned_digest
    expected_refs = {
        "appEnvSecretRef": _ref(installation_id, "app-env"),
        "dbEnvSecretRef": _ref(installation_id, "db-env"),
        "apiKeySecretRef": _ref(installation_id, "api-key"),
        "adminSecretRef": _ref(installation_id, "admin"),
    }
    assert entry.credentials.model_dump(exclude_none=True) == expected_refs
    for ref in expected_refs.values():
        assert ref in source.secret_values, f"{ref} is referenced but carries no value"
    atoms = await _atoms(db_session, installation_id)
    assert source.secret_values[_ref(installation_id, "api-key")] == atoms["api-key"]


# ---------------------------------------------------------------------------
# What is and is not projected: status, clobber, gate and shadowing (Task 2).
# ---------------------------------------------------------------------------


def _postiz_refs_in(source: RenderedRuntimeSource) -> list[str]:
    return sorted(ref for ref in source.secret_values if ref.startswith("secret://companions/"))


@pytest.mark.parametrize("case", ["queued", "failed", "removed"])
async def test_queued_failed_and_removed_installations_are_not_projected(
    db_session, seed_user, companion_zone, case: str
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(
        db_session, seed_user, environment_id, status="running" if case == "removed" else case
    )
    if case == "removed":
        installation = await db_session.get(CompanionInstallation, installation_id)
        assert installation is not None
        installation.removed_at = datetime.now(UTC)
        await db_session.commit()

    source = await _render(db_session, environment_id)

    assert "postiz" not in source.manifest.get("companions", {})
    assert _postiz_refs_in(source) == []
    assert source.companion_omissions == ()


async def test_a_complete_installation_is_projected(db_session, seed_user, companion_zone) -> None:
    # `complete` is the other live state: the install job finished and the companion runs.
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="complete")

    source = await _render(db_session, environment_id)

    assert source.manifest["companions"]["postiz"]["installationId"] == str(installation_id)


@pytest_asyncio.fixture
async def platform_client(db_session) -> AsyncIterator[httpx.AsyncClient]:
    """The provisioner's own write path, driven for real (mirrors test_platform_endpoints)."""

    async def _override_get_session():
        yield db_session

    original_admin_key = settings.admin_api_key
    original_clerk_issuer = settings.clerk_jwt_issuer
    settings.admin_api_key = _ADMIN_KEY
    settings.clerk_jwt_issuer = _CLERK_ISSUER
    app.dependency_overrides[get_session] = _override_get_session
    # Upstream moved the admin and platform routes onto the control-plane session.
    app.dependency_overrides[get_control_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        settings.admin_api_key = original_admin_key
        settings.clerk_jwt_issuer = original_clerk_issuer


async def _report_cli_version(db_session, environment_id: UUID, version: str) -> None:
    """Record that this agent's runtime reported ``version`` (the emit gate's only input)."""
    now = datetime.now(UTC)
    db_session.add(
        HostedRuntimeConfigObservation(
            environment_id=environment_id,
            observed_at=now,
            diagnostics={
                "schemaVersion": "clawdi.hostedRuntimeObserved.v2",
                "reportedAt": now.isoformat(),
                "runtimeMode": "hosted",
                "status": "ok",
                "activeCliVersion": version,
                "applied": None,
                "boot": None,
                "cli": None,
            },
        )
    )
    await db_session.commit()


async def test_provisioner_upsert_does_not_remove_an_installed_companion(
    platform_client, db_session, seed_user, companion_zone
) -> None:
    owner = _clerk_owner(seed_user)
    agent_id = uuid.uuid4()
    await ensure_canonical_codex_tool_provider(db_session, seed_user)
    created = await _create_platform_agent(platform_client, owner, agent_id, key="clobber-agent")
    assert created.status_code == 200, created.text
    first = await platform_client.put(
        f"/v1/platform/agents/{agent_id}/runtime-state",
        headers=_headers("clobber-first"),
        # The exact v2 deployment source the render requires since #981 (sync step 1).
        json=_runtime_body(owner, agent_id, provider_id=CANONICAL_CODEX_TOOL_PROVIDER_ID),
    )
    assert first.status_code == 200, first.text
    await _report_cli_version(db_session, agent_id, POSTIZ_MIN_CLI_VERSION)
    installation_id = await _installation(db_session, seed_user, agent_id)

    # The provisioner's next write declares ONLY filebrowser and replaces the column wholesale.
    filebrowser = filebrowser_companion("deployment-1")
    upsert = await platform_client.put(
        f"/v1/platform/agents/{agent_id}/runtime-state",
        headers=_headers("clobber-second"),
        json={
            **_runtime_body(owner, agent_id, provider_id=CANONICAL_CODEX_TOOL_PROVIDER_ID),
            "generation": 2,
            "companions": filebrowser,
        },
    )
    assert upsert.status_code == 200, upsert.text

    db_session.expire_all()
    source = await _render(db_session, agent_id)

    companions = source.manifest["companions"]
    assert set(companions) == {"filebrowser", "postiz"}
    assert companions["filebrowser"] == filebrowser["filebrowser"]
    assert companions["postiz"]["installationId"] == str(installation_id)
    # The installation was never written into the provisioner-owned tables (the prohibition):
    # the upsert did not have to erase anything to leave it standing.
    state = await db_session.get(HostedRuntimeState, agent_id)
    assert state is not None
    assert set(state.companions or {}) == {"filebrowser"}
    provisioner_refs = (
        (
            await db_session.execute(
                select(HostedRuntimeSecret.secret_ref).where(
                    HostedRuntimeSecret.environment_id == agent_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert provisioner_refs
    assert not [ref for ref in provisioner_refs if ref.startswith("secret://companions/")]


async def test_stale_runtime_does_not_receive_postiz(db_session, seed_user, companion_zone) -> None:
    environment_id = await _agent_with_runtime(
        db_session, seed_user, cli_version=DEPLOYED_CLI_VERSION
    )
    await _installation(db_session, seed_user, environment_id)

    source = await _render(db_session, environment_id)

    assert "postiz" not in source.manifest.get("companions", {})
    # Its credentials do not travel without the entry either.
    assert _postiz_refs_in(source) == []
    assert len(source.companion_omissions) == 1
    record = source.companion_omissions[0]
    assert record.kind == "postiz"
    assert record.cause is CompanionEmitOmission.STALE_REPORTED_VERSION
    assert record.reported_version == DEPLOYED_CLI_VERSION
    assert record.required_min_cli_version == POSTIZ_MIN_CLI_VERSION


async def test_a_current_runtime_receives_postiz(db_session, seed_user, companion_zone) -> None:
    environment_id = await _agent_with_runtime(
        db_session, seed_user, cli_version=POSTIZ_MIN_CLI_VERSION
    )
    await _installation(db_session, seed_user, environment_id)

    source = await _render(db_session, environment_id)

    assert "postiz" in source.manifest["companions"]
    assert source.companion_omissions == ()
    assert len(_postiz_refs_in(source)) == len(CREDENTIAL_NAMES)


def _fixture_entry() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text())


async def test_installation_shadows_a_provisioner_declared_postiz(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    state = await db_session.get(HostedRuntimeState, environment_id)
    assert state is not None
    provisioner_postiz = _fixture_entry()
    # A value no omission record may carry, planted in the shadowed entry's value.
    provisioner_postiz["hostname"] = "shadowed-sentinel.companions.example"
    state.companions = {**filebrowser_companion(state.deployment_id), "postiz": provisioner_postiz}
    await db_session.commit()
    installation_id = await _installation(db_session, seed_user, environment_id)
    installation = await db_session.get(CompanionInstallation, installation_id)
    assert installation is not None

    source = await _render(db_session, environment_id)

    emitted = source.manifest["companions"]["postiz"]
    assert emitted == project_postiz_entry(installation, catalog_entry("postiz"))
    assert emitted["installationId"] == str(installation_id)
    assert emitted["installationId"] != provisioner_postiz["installationId"]
    assert [(record.kind, record.cause.value) for record in source.companion_omissions] == [
        ("postiz", "shadowed_by_installation")
    ]
    record = source.companion_omissions[0]
    assert record.reported_version == POSTIZ_MIN_CLI_VERSION
    assert record.required_min_cli_version == POSTIZ_MIN_CLI_VERSION
    assert "shadowed-sentinel" not in repr(source.companion_omissions)


async def test_projected_manifest_changes_revision_when_an_atom_is_reencrypted(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)
    before = await _render(db_session, environment_id)

    row = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == installation_id,
                CompanionInstallationSecret.secret_name == "api-key",
            )
        )
    ).scalar_one()
    # The same plaintext under a fresh nonce: only the ciphertext identity moves.
    row.encrypted_value, row.nonce = encrypt(decrypt(row.encrypted_value, row.nonce))
    await db_session.commit()
    after = await _render(db_session, environment_id)

    assert after.secret_values == before.secret_values
    assert after.source_revision != before.source_revision
    # Identity is ciphertext-derived, so the same holds without decrypting anything.
    hidden = await _render(db_session, environment_id, decrypt_secrets=False)
    assert hidden.source_revision == after.source_revision
    assert hidden.secret_values == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "postiz-zzzzzzzzzzzz.companions.example"),
        ("pinned_digest", "f" * 64),
    ],
)
async def test_projected_manifest_changes_revision_when_a_composition_input_changes(
    db_session, seed_user, companion_zone, field: str, value: str
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)
    before = await _render(db_session, environment_id, decrypt_secrets=False)

    installation = await db_session.get(CompanionInstallation, installation_id)
    assert installation is not None
    setattr(installation, field, value)
    await db_session.commit()
    after = await _render(db_session, environment_id, decrypt_secrets=False)

    assert after.source_revision != before.source_revision


async def test_an_unreadable_atom_blocks_the_manifest_and_is_named(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)
    row = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == installation_id,
                CompanionInstallationSecret.secret_name == "db-password",
            )
        )
    ).scalar_one()
    row.encrypted_value, row.nonce = foreign_ciphertext("unrecoverable")
    await db_session.commit()

    with pytest.raises(RuntimeSecretUnreadableError) as raised:
        await _render(db_session, environment_id)

    assert raised.value.secret_refs == (_ref(installation_id, "atoms/db-password"),)
    assert "unrecoverable" not in str(raised.value)


def test_the_projection_matches_the_shared_postiz_fixture() -> None:
    """The backend emits exactly the entry both manifest parsers are pinned to (plan 06-05)."""
    fixture = _fixture_entry()
    installation = CompanionInstallation(
        id=UUID(fixture["installationId"]),
        companion_key="postiz",
        isolation_tier=fixture["isolationTier"],
        version=fixture["version"],
        pinned_digest=fixture["artifact"]["sha256"],
        hostname=fixture["hostname"],
    )

    assert project_postiz_entry(installation, catalog_entry("postiz")) == fixture


def test_a_ready_installation_projects_its_recorded_admin_user_id() -> None:
    """``adminUserId`` is null until a ready report recorded the admin user id (plan 06-38).

    ``record_install_report`` stores the ready report's ``admin_user_id`` in
    ``external_resources``; the runtime proves ownership of an existing Postiz organization by
    that id once the tenant may have replaced the generated password (CK-D).
    """
    installation = _unit_installation()
    assert project_postiz_entry(installation, catalog_entry("postiz"))["adminUserId"] is None

    installation.external_resources = {}
    assert project_postiz_entry(installation, catalog_entry("postiz"))["adminUserId"] is None

    installation.external_resources = {"admin_user_id": ""}
    assert project_postiz_entry(installation, catalog_entry("postiz"))["adminUserId"] is None

    installation.external_resources = {"admin_user_id": "postiz-user-1"}
    assert project_postiz_entry(installation, catalog_entry("postiz"))["adminUserId"] == (
        "postiz-user-1"
    )


# These cases are repeated value for value in `packages/cli/src/runtime/manifest-contract.test.ts`,
# so a drift between the Pydantic and the Zod parser fails one side (plan 06-38).
ADMIN_USER_IDS_ACCEPTED = [None, "7c1d9a64-2b3e-4f5a-8c6d-0e1f2a3b4c5d", "a" * 64]
ADMIN_USER_IDS_REFUSED = ["", "a" * 65]


@pytest.mark.parametrize("admin_user_id", ADMIN_USER_IDS_ACCEPTED)
def test_the_admin_user_id_admits_null_and_a_bounded_string(admin_user_id: str | None) -> None:
    entry = HostedPostizCompanion.model_validate({**_fixture_entry(), "adminUserId": admin_user_id})

    assert entry.adminUserId == admin_user_id


def test_an_absent_admin_user_id_defaults_to_null() -> None:
    fixture = _fixture_entry()
    fixture.pop("adminUserId", None)

    assert HostedPostizCompanion.model_validate(fixture).adminUserId is None


@pytest.mark.parametrize("admin_user_id", ADMIN_USER_IDS_REFUSED)
def test_the_admin_user_id_refuses_an_empty_or_overlong_string(admin_user_id: str) -> None:
    with pytest.raises(ValidationError) as exc:
        HostedPostizCompanion.model_validate({**_fixture_entry(), "adminUserId": admin_user_id})

    errors = exc.value.errors()
    assert [".".join(str(part) for part in error["loc"]) for error in errors] == ["adminUserId"]
    assert errors[0]["type"] in {"string_too_short", "string_too_long"}


# ---------------------------------------------------------------------------
# The composed environment is exactly what Postiz needs, and nothing can inject into it (Task 3).
# ---------------------------------------------------------------------------

HOSTNAME = "postiz-abcdefghij23.companions.example"
INSTALLATION_ID = UUID("5f0c6d2e-8a41-4b7f-9e3c-2d1a7b6e4c90")
ATOMS = {
    "jwt-secret": "jwt-secret-value-0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLM",
    "db-password": "0123456789abcdef0123456789abcdef0123456789abcdef",
    "admin-password": "admin-password-value-0123456789abcdefghij",
    "api-key": "api-key-value-0123456789abcdefghijklmnopqrstuvwx",
}
# Research Pattern 3 at Postiz v2.24.0. Temporal and media keys arrive with plan 06-13; until
# then this set is exact, not a floor.
APP_ENV_KEYS = {
    "MAIN_URL",
    "FRONTEND_URL",
    "NEXT_PUBLIC_BACKEND_URL",
    "BACKEND_INTERNAL_URL",
    "JWT_SECRET",
    "DATABASE_URL",
    "REDIS_URL",
    "IS_GENERAL",
    "DISABLE_REGISTRATION",
    "RUN_CRON",
    "API_LIMIT",
    "STORAGE_PROVIDER",
}


def _unit_installation(**overrides: Any) -> CompanionInstallation:
    values: dict[str, Any] = {
        "id": INSTALLATION_ID,
        "companion_key": "postiz",
        "isolation_tier": "standard",
        "version": "v2.24.0",
        "pinned_digest": catalog_entry("postiz").app.sha256,
        "hostname": HOSTNAME,
        "owner_email": "owner@example.test",
    }
    values.update(overrides)
    return CompanionInstallation(**values)


def _compose(
    atoms: dict[str, str] | None = None, *, agent_name: str | None = None, **overrides: Any
) -> dict[str, str]:
    return compose_postiz_secret_values(
        _unit_installation(**overrides), atoms or dict(ATOMS), agent_name=agent_name
    )


def _env_lines(value: str) -> list[tuple[str, str]]:
    lines = value.splitlines()
    assert all("=" in line for line in lines), "every line of an env file is KEY=VALUE"
    return [(key, rest) for key, _, rest in (line.partition("=") for line in lines)]


def _env(value: str) -> dict[str, str]:
    pairs = _env_lines(value)
    keys = [key for key, _ in pairs]
    assert len(keys) == len(set(keys)), "an env file names each key once"
    return dict(pairs)


def test_composition_produces_exactly_the_four_credential_refs() -> None:
    composed = _compose()

    assert set(composed) == {_ref(INSTALLATION_ID, name) for name in CREDENTIAL_NAMES}
    assert composed[_ref(INSTALLATION_ID, "api-key")] == ATOMS["api-key"]


def test_app_env_keys_are_exactly_the_interface_set() -> None:
    app_env = _env(_compose()[_ref(INSTALLATION_ID, "app-env")])

    assert set(app_env) == APP_ENV_KEYS
    assert app_env["DISABLE_REGISTRATION"] == "true"
    assert app_env["IS_GENERAL"] == "true"
    assert app_env["RUN_CRON"] == "true"
    assert app_env["API_LIMIT"] == "90"
    assert app_env["STORAGE_PROVIDER"] == "cloudflare"
    assert app_env["JWT_SECRET"] == ATOMS["jwt-secret"]
    # The runtime sets the npm dlx cache age on the unit itself (plan 06-08), not as a secret.
    assert "npm_config_dlx_cache_max_age" not in app_env


def test_app_env_urls_point_at_the_installation_hostname() -> None:
    app_env = _env(_compose()[_ref(INSTALLATION_ID, "app-env")])

    assert app_env["MAIN_URL"] == f"https://{HOSTNAME}"
    assert app_env["FRONTEND_URL"] == f"https://{HOSTNAME}"
    assert app_env["NEXT_PUBLIC_BACKEND_URL"] == f"https://{HOSTNAME}/api"
    assert app_env["BACKEND_INTERNAL_URL"] == "http://localhost:3000"


def test_app_env_names_the_in_network_database_and_the_per_installation_redis() -> None:
    app_env = _env(_compose()[_ref(INSTALLATION_ID, "app-env")])

    assert app_env["DATABASE_URL"] == (
        f"postgresql://postiz:{ATOMS['db-password']}@iforge-postiz-db:5432/postiz"
    )
    # CK-A (plan 06-05): a per-installation Redis, never a shared one (research F3).
    assert app_env["REDIS_URL"] == "redis://iforge-postiz-redis:6379"


def test_env_files_are_sorted_unquoted_key_value_lines() -> None:
    composed = _compose()
    for name in ("app-env", "db-env"):
        pairs = _env_lines(composed[_ref(INSTALLATION_ID, name)])
        keys = [key for key, _ in pairs]
        assert keys == sorted(keys), f"{name} keys are sorted"
        for key, value in pairs:
            assert value, f"{name} {key} is empty"
            assert value[0] not in "\"'", f"{name} {key} is quoted"


def test_db_env_is_the_house_postgres_contract() -> None:
    db_env = _env(_compose()[_ref(INSTALLATION_ID, "db-env")])

    assert db_env == {
        "POSTGRES_USER": "postiz",
        "POSTGRES_DB": "postiz",
        "POSTGRES_PASSWORD": ATOMS["db-password"],
        # The house pgBackRest image's pg18 data path, as config/deploy.yml uses it.
        "PGDATA": "/var/lib/postgresql/18/docker",
    }
    deploy = (REPO_ROOT / "config" / "deploy.yml").read_text()
    assert db_env["PGDATA"] in deploy


def test_admin_is_email_password_and_the_agents_name_as_company() -> None:
    admin = json.loads(_compose(agent_name="Launch Crew")[_ref(INSTALLATION_ID, "admin")])

    assert admin == {
        "email": "owner@example.test",
        "password": ATOMS["admin-password"],
        "company": "Launch Crew",
    }


def test_admin_company_is_truncated_to_64_characters() -> None:
    name = "A" * 40 + "B" * 40
    admin = json.loads(_compose(agent_name=name)[_ref(INSTALLATION_ID, "admin")])

    assert admin["company"] == name[:64]


@pytest.mark.parametrize("agent_name", [None, "", "   "])
def test_admin_company_defaults_to_postiz_when_the_agent_has_no_name(
    agent_name: str | None,
) -> None:
    admin = json.loads(_compose(agent_name=agent_name)[_ref(INSTALLATION_ID, "admin")])

    assert admin["company"] == "Postiz"


def test_admin_company_drops_control_characters_from_the_agent_name() -> None:
    # A tenant-chosen display name must not be able to wedge the whole manifest (it is the only
    # composition input a tenant types), so it is cleaned rather than refused.
    admin = json.loads(_compose(agent_name="Launch\nCrew\x00")[_ref(INSTALLATION_ID, "admin")])

    assert admin["company"] == "LaunchCrew"


@pytest.mark.parametrize("character", ["\n", "\r", "\x00"])
@pytest.mark.parametrize(
    ("atom", "composed_name"),
    [
        ("jwt-secret", "app-env"),
        ("db-password", "app-env"),
        ("api-key", "api-key"),
        ("admin-password", "admin"),
    ],
)
def test_a_forbidden_character_in_an_atom_is_refused_naming_the_ref(
    atom: str, composed_name: str, character: str
) -> None:
    poisoned = f"prefix{character}INJECTED=1"
    atoms = {**ATOMS, atom: poisoned}

    with pytest.raises(RuntimeSourceError) as raised:
        _compose(atoms)

    message = str(raised.value)
    assert _ref(INSTALLATION_ID, composed_name) in message
    assert poisoned not in message
    assert "INJECTED" not in message


def test_a_forbidden_character_in_the_hostname_is_refused() -> None:
    with pytest.raises(RuntimeSourceError) as raised:
        _compose(hostname="postiz.example\nEVIL=1")

    assert _ref(INSTALLATION_ID, "app-env") in str(raised.value)
    assert "EVIL" not in str(raised.value)


def test_a_missing_atom_is_refused_naming_the_ref() -> None:
    atoms = {name: value for name, value in ATOMS.items() if name != "admin-password"}

    with pytest.raises(RuntimeSourceError) as raised:
        _compose(atoms)

    assert _ref(INSTALLATION_ID, "admin") in str(raised.value)


async def test_a_poisoned_stored_atom_fails_the_render_closed_naming_the_ref(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id)
    row = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == installation_id,
                CompanionInstallationSecret.secret_name == "jwt-secret",
            )
        )
    ).scalar_one()
    row.encrypted_value, row.nonce = encrypt("secret\nDISABLE_REGISTRATION=false")
    await db_session.commit()

    with pytest.raises(RuntimeSourceError) as raised:
        await _render(db_session, environment_id)

    assert _ref(installation_id, "app-env") in str(raised.value)
    assert "DISABLE_REGISTRATION" not in str(raised.value)


async def test_the_rendered_admin_company_is_the_agents_display_name(
    db_session, seed_user, companion_zone
) -> None:
    environment_id = await _agent_with_runtime(db_session, seed_user)
    environment = await db_session.get(AgentEnvironment, environment_id)
    assert environment is not None
    environment.display_name = "Launch Crew"
    await db_session.commit()
    installation_id = await _installation(db_session, seed_user, environment_id)

    source = await _render(db_session, environment_id)

    admin = json.loads(source.secret_values[_ref(installation_id, "admin")])
    assert admin["company"] == "Launch Crew"
