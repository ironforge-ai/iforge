"""The install job: its state machine, its log, and tools registered on completion (APP-01, 04).

An installation moves ``queued -> running -> complete`` (or ``failed``) and never backwards (D-26).
The lifecycle worker claims queued work with a lease, runs the ordered provisioning pipeline and
only then marks the job ``running`` -- the status at which plan 06-10's projection starts emitting
the companion to its agent's runtime. The runtime reports progress, log lines and a DATA-asserted
readiness through ``POST /v1/runtime/companion-installations/{id}/report`` with its own deployment
key; the worker completes the job and, in that same action, the tenant's agent can list Postiz's
verbs (APP-04: no separate registration step).

**The worker runs inside the test's own transaction.** ``_sessionmaker`` binds every worker
session to the connection the rollback-isolated ``db_session`` already owns, joined through a
SAVEPOINT, so the worker's commits stay test-local and the whole test still rolls back as one.
Nothing here depends on another test's committed rows, and nothing leaks into the next test.

**Every expected tool name comes from the registry, never from the endpoint under test** (T-4-19):
the six postiz verbs are the registry's ``postiz__*`` rows, pinned to six by count.
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.api_scopes import RUNTIME_DEPLOYMENT_KEY_SCOPES, RUNTIME_MCP_SCOPES
from app.core.auth import AuthContext, get_auth
from app.core.companion_registry import COMPANION_VERBS
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.api_key import ApiKey
from app.models.companion_installation import (
    CompanionInstallation,
    CompanionInstallationSecret,
    CompanionInstallLogLine,
)
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.routes import mcp_bridge
from app.routes.mcp_bridge import _list_clawdi_mcp_tools
from app.services.api_key import mint_api_key
from app.services.companion_facade import (
    _environment_with_companion,
    deployed_companion_environments,
    reset_companion_tools_cache,
)
from app.services.companion_install import create_installation
from app.services.companion_lifecycle_worker import (
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
)
from app.services.vault_crypto import decrypt, encrypt
from tests.companion_billing_stub import billing_quote
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state
from tests.test_companion_runtime_scopes import _mint_platform_key

ZONE = "companions.example"

# The registry's postiz rows -- the expectation is built from the registry, and pinned by count so
# a verb silently dropped from the registry cannot shrink the expectation along with it.
POSTIZ_VERBS = frozenset(name for name in COMPANION_VERBS if name.startswith("postiz__"))


def report_path(installation_id: uuid.UUID) -> str:
    return f"/v1/runtime/companion-installations/{installation_id}/report"


@pytest.fixture(autouse=True)
def _clear_companion_tools_cache():
    reset_companion_tools_cache()
    yield
    reset_companion_tools_cache()


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


@pytest.fixture
def no_connector_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runtime keys carry `connectors:read`, which would otherwise reach Composio."""

    async def _none(auth):
        return []

    monkeypatch.setattr(mcp_bridge, "_connector_mcp_tools", _none)


def _sessionmaker(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Worker sessions on the test's own connection, each joined through a SAVEPOINT."""
    return async_sessionmaker(
        bind=db_session.bind,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


async def _hosted_agent(db_session: AsyncSession, seed_user) -> tuple[uuid.UUID, str]:
    """One live Agent with strict-v2 hosted state and NO provisioner-declared companions.

    With no declared companion at all, every postiz tool the agent later sees can only have come
    from the installation.
    """
    env = await create_env_with_project(
        db_session,
        user_id=seed_user.id,
        machine_id=f"install-job-{uuid.uuid4().hex[:10]}",
        machine_name="Install Job Agent",
    )
    state = await create_test_hosted_runtime_state(db_session, env, runtime_name="openclaw")
    return env.id, state.deployment_id


async def _runtime_key(db_session: AsyncSession, seed_user, agent_id, deployment_id) -> str:
    """The agent's own runtime deployment key: managed, environment-bound, deployment-fenced."""
    minted = await mint_api_key(
        db_session,
        user_id=seed_user.id,
        label="runtime-deployment",
        scopes=list(RUNTIME_DEPLOYMENT_KEY_SCOPES),
        environment_id=agent_id,
        runtime_deployment_id=deployment_id,
        managed=True,
    )
    return minted.raw_key


@asynccontextmanager
async def _runtime_client(
    db_session: AsyncSession, raw_key: str
) -> AsyncIterator[httpx.AsyncClient]:
    """A real bearer-token client: only the session is overridden, auth is the key itself."""

    async def _override_get_session():
        yield db_session

    # Restored rather than popped, so a dashboard ``client`` fixture's own session override
    # survives a runtime report made in the middle of the same test.
    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = _override_get_session
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {raw_key}"},
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous


async def _post_report(
    db_session: AsyncSession, raw_key: str, installation_id: uuid.UUID, body: dict
) -> httpx.Response:
    async with _runtime_client(db_session, raw_key) as client:
        return await client.post(report_path(installation_id), json=body)


async def _reload(db_session: AsyncSession, installation_id: uuid.UUID) -> CompanionInstallation:
    row = (
        await db_session.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == installation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


def _ready_report(*, applied_digest: str, **overrides) -> dict:
    body = {
        "operation": "install",
        "phase": "ready",
        "step": "ready",
        "lines": [{"stream": "stdout", "line": "postiz answered with its organization"}],
        "assertion": {"connected": True, "organizations": 1},
        "applied_digest": applied_digest,
        "admin_user_id": "postiz-user-1",
    }
    body.update(overrides)
    return body


async def _names(auth: AuthContext, db_session: AsyncSession) -> set[str]:
    return {str(tool["name"]) for tool in await _list_clawdi_mcp_tools(auth, db=db_session)}


async def test_install_completes_and_registers_tools_in_the_same_action(
    db_session, seed_user, companion_zone, no_connector_tools
) -> None:
    assert len(POSTIZ_VERBS) == 6, sorted(POSTIZ_VERBS)
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    installation_id = installation.id
    pinned_digest = installation.pinned_digest

    # The credential the platform mints for an install, driven through the real platform route.
    _raw, platform_key_id = await _mint_platform_key(db_session, seed_user, agent_id)
    platform_key = await db_session.get(ApiKey, platform_key_id)
    assert platform_key is not None
    platform_auth = AuthContext(user=seed_user, api_key=platform_key)
    # Control: before the install completes, the agent is shown no postiz verb at all.
    assert (await _names(platform_auth, db_session)).isdisjoint(POSTIZ_VERBS)

    # No pipeline steps: this test is about the state machine and tool registration, and the
    # real steps (plans 06-13, 06-14) would call operator APIs.
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=(), completion_steps=()
    )
    assert await worker.run_once() == installation_id
    assert (await _reload(db_session, installation_id)).status == "running"

    runtime_key = await _runtime_key(db_session, seed_user, agent_id, deployment_id)
    reported = await _post_report(
        db_session, runtime_key, installation_id, _ready_report(applied_digest=pinned_digest)
    )
    assert reported.status_code == 200, reported.text

    assert await worker.run_once() == installation_id
    row = await _reload(db_session, installation_id)
    assert row.status == "complete"
    assert row.completed_at is not None
    assert row.applied_digest == pinned_digest

    # The same action registered the tools: no separate step, no waiting out a cache TTL.
    assert POSTIZ_VERBS <= await _names(platform_auth, db_session)


# ---------------------------------------------------------------------------
# Task 2: the report route refuses everything it must, and redacts before storing.
# ---------------------------------------------------------------------------

SECRET_NAMES = ("admin-password", "api-key", "db-password", "jwt-secret")
REDACTED = "[redacted]"


class _StepRecorder:
    """Fake pipeline steps that record provision and teardown order, and nothing else."""

    def __init__(self, *names: str) -> None:
        self.events: list[str] = []
        self.steps = tuple(self._step(name) for name in names)

    def _step(self, name: str) -> ProvisioningStep:
        async def provision(context: LifecycleStepContext) -> dict:
            self.events.append(f"provision:{name}")
            return {"created": name}

        async def teardown(context: LifecycleStepContext, resources: dict) -> None:
            assert resources == {"created": name}, resources
            self.events.append(f"teardown:{name}")

        return ProvisioningStep(name=name, provision=provision, teardown=teardown)


class _Running:
    """A running installation, its agent's runtime key and the worker that advanced it."""

    def __init__(self, installation_id, runtime_key, agent_id, pinned_digest, worker) -> None:
        self.installation_id = installation_id
        self.runtime_key = runtime_key
        self.agent_id = agent_id
        self.pinned_digest = pinned_digest
        self.worker = worker


async def _running_installation(
    db_session: AsyncSession,
    seed_user,
    *,
    provisioning_steps: tuple[ProvisioningStep, ...] = (),
) -> _Running:
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    # No completion steps: the real edge publication (plan 06-14) would call operator APIs.
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), provisioning_steps=provisioning_steps, completion_steps=()
    )
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "running"
    runtime_key = await _runtime_key(db_session, seed_user, agent_id, deployment_id)
    return _Running(installation.id, runtime_key, agent_id, installation.pinned_digest, worker)


async def _complete_installation(db_session: AsyncSession, seed_user) -> _Running:
    running = await _running_installation(db_session, seed_user)
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _ready_report(applied_digest=running.pinned_digest),
    )
    assert reported.status_code == 200, reported.text
    assert await running.worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "complete"
    return running


async def _atoms(db_session: AsyncSession, installation_id: uuid.UUID) -> dict[str, str]:
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
    atoms = {row.secret_name: decrypt(row.encrypted_value, row.nonce) for row in rows}
    assert sorted(atoms) == list(SECRET_NAMES)
    return atoms


async def _stored_lines(
    db_session: AsyncSession, installation_id: uuid.UUID
) -> list[CompanionInstallLogLine]:
    return list(
        (
            await db_session.execute(
                select(CompanionInstallLogLine)
                .where(CompanionInstallLogLine.installation_id == installation_id)
                .order_by(CompanionInstallLogLine.seq)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


def _progress(*lines: str, stream: str = "stdout", operation: str = "install") -> dict:
    return {
        "operation": operation,
        "phase": "progress",
        "step": "pulling",
        "lines": [{"stream": stream, "line": line} for line in lines],
    }


async def test_report_from_another_agents_key_is_404(db_session, seed_user, companion_zone) -> None:
    running = await _running_installation(db_session, seed_user)
    # A second agent of the SAME tenant, with its own genuine deployment key: the sharpest case,
    # because tenant ownership alone would admit it.
    other_agent_id, other_deployment_id = await _hosted_agent(db_session, seed_user)
    other_key = await _runtime_key(db_session, seed_user, other_agent_id, other_deployment_id)
    # And the tenant's own unbound, full-access key, which is bound to no agent at all.
    owner_key = (await mint_api_key(db_session, user_id=seed_user.id, label="owner")).raw_key

    for key in (other_key, owner_key):
        refused = await _post_report(db_session, key, running.installation_id, _progress("hello"))
        assert refused.status_code == 404, refused.text

    # Control: the installation's own agent's key is accepted for the same report.
    accepted = await _post_report(
        db_session, running.runtime_key, running.installation_id, _progress("hello")
    )
    assert accepted.status_code == 200, accepted.text


async def test_report_for_removed_installation_is_404(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    row = await _reload(db_session, running.installation_id)
    row.removed_at = datetime.now(UTC)
    await db_session.commit()

    for body in (_progress("late"), {"operation": "install", "phase": "removed"}):
        refused = await _post_report(db_session, running.runtime_key, running.installation_id, body)
        assert refused.status_code == 404, refused.text


@pytest.mark.parametrize("terminal", ["complete", "failed"])
async def test_report_after_terminal_state_is_409(
    db_session, seed_user, companion_zone, terminal: str
) -> None:
    running = await _running_installation(db_session, seed_user)
    row = await _reload(db_session, running.installation_id)
    row.status = terminal
    await db_session.commit()

    for body in (
        _progress("after the end"),
        _ready_report(applied_digest=running.pinned_digest),
        {"operation": "install", "phase": "failed", "error": "late failure"},
    ):
        refused = await _post_report(db_session, running.runtime_key, running.installation_id, body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "companion_install_terminal"
    assert (await _reload(db_session, running.installation_id)).status == terminal


async def test_report_for_another_operation_is_409(db_session, seed_user, companion_zone) -> None:
    running = await _running_installation(db_session, seed_user)

    refused = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _progress("an update log line", operation="update"),
    )

    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["code"] == "companion_report_operation_mismatch"
    assert await _stored_lines(db_session, running.installation_id) == []


async def test_backup_report_updates_backup_state_without_changing_status(
    db_session, seed_user, companion_zone
) -> None:
    complete = await _complete_installation(db_session, seed_user)
    started = datetime(2026, 9, 1, tzinfo=UTC)

    for index in range(1, 51):
        written_at = started + timedelta(hours=index)
        response = await _post_report(
            db_session,
            complete.runtime_key,
            complete.installation_id,
            {
                # The operation is not checked for a backup: scheduled backups run while the
                # install is complete, whatever operation last ran.
                "operation": "install",
                "phase": "backup",
                "backup": {
                    "ok": True,
                    "label": f"20260901-{index:04d}F",
                    "started_at": (written_at + timedelta(minutes=1)).isoformat(),
                    "completed_at": (written_at + timedelta(minutes=2)).isoformat(),
                    "db_bytes": 1000 + index,
                    "marker": {
                        "id": index,
                        "token": f"{index:032x}",
                        "written_at": written_at.isoformat(),
                    },
                },
            },
        )
        assert response.status_code == 200, response.text

    row = await _reload(db_session, complete.installation_id)
    assert row.status == "complete"
    state = row.backup_state
    assert state["last_backup_ok"] is True
    assert state["last_backup_label"] == "20260901-0050F"
    assert state["db_bytes"] == 1050
    last_completed = started + timedelta(hours=50, minutes=2)
    assert datetime.fromisoformat(state["last_backup_at"]) == last_completed
    markers = state["markers"]
    # The newest 48, each with the completion time of the backup taken right after it.
    assert [marker["id"] for marker in markers] == list(range(3, 51))
    assert markers[-1]["token"] == f"{50:032x}"
    assert datetime.fromisoformat(markers[-1]["backup_completed_at"]) == last_completed


async def test_removed_report_is_recorded(db_session, seed_user, companion_zone) -> None:
    complete = await _complete_installation(db_session, seed_user)

    response = await _post_report(
        db_session,
        complete.runtime_key,
        complete.installation_id,
        {"operation": "install", "phase": "removed"},
    )

    assert response.status_code == 200, response.text
    row = await _reload(db_session, complete.installation_id)
    assert row.status == "complete"
    assert datetime.fromisoformat(row.external_resources["removed_reported_at"])


@pytest.mark.parametrize(
    "assertion",
    [
        {"connected": False, "organizations": 1},
        {"connected": True, "organizations": 0},
        {"connected": True, "organizations": 2},
    ],
)
async def test_ready_without_a_passing_assertion_is_refused(
    db_session, seed_user, companion_zone, assertion: dict
) -> None:
    running = await _running_installation(db_session, seed_user)

    refused = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _ready_report(applied_digest=running.pinned_digest, assertion=assertion),
    )

    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"]["code"] == "companion_ready_assertion_failed"
    row = await _reload(db_session, running.installation_id)
    assert row.status == "running"
    assert row.step != "ready"
    assert row.applied_digest is None
    # Reachability alone never completes an install: the worker has nothing due to complete.
    assert await running.worker.run_once() is None
    assert (await _reload(db_session, running.installation_id)).status == "running"


async def test_failed_report_fails_the_job_with_a_redacted_reason_and_tears_down(
    db_session, seed_user, companion_zone
) -> None:
    recorder = _StepRecorder("tunnel", "bucket", "dns")
    running = await _running_installation(db_session, seed_user, provisioning_steps=recorder.steps)
    assert recorder.events == ["provision:tunnel", "provision:bucket", "provision:dns"]
    atoms = await _atoms(db_session, running.installation_id)

    failed = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {
            "operation": "install",
            "phase": "failed",
            "error": f"prisma: authentication failed for {atoms['db-password']}",
        },
    )

    assert failed.status_code == 200, failed.text
    row = await _reload(db_session, running.installation_id)
    assert row.status == "failed"
    assert row.error == f"prisma: authentication failed for {REDACTED}"

    assert await running.worker.run_once() == running.installation_id
    # Every provisioning step this operation completed, undone in reverse order.
    assert recorder.events[3:] == ["teardown:dns", "teardown:bucket", "teardown:tunnel"]
    row = await _reload(db_session, running.installation_id)
    assert row.status == "failed"
    assert row.next_attempt_at is None
    assert [entry["name"] for entry in row.external_resources["torn_down"]] == [
        "dns",
        "bucket",
        "tunnel",
    ]
    # Nothing is due any more: the teardown ran once and is recorded as done.
    assert await running.worker.run_once() is None


async def test_log_lines_are_redacted_against_every_installation_secret(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    # One atom rewritten to a value whose URL-encoded form DIFFERS from it, so the encoded-form
    # redaction is exercised rather than coinciding with the plain one.
    secret = (
        await db_session.execute(
            select(CompanionInstallationSecret).where(
                CompanionInstallationSecret.installation_id == running.installation_id,
                CompanionInstallationSecret.secret_name == "db-password",
            )
        )
    ).scalar_one()
    special = f"p@ss/w:rd+{uuid.uuid4().hex}=&?"
    secret.encrypted_value, secret.nonce = encrypt(special)
    await db_session.commit()
    atoms = await _atoms(db_session, running.installation_id)
    encoded = quote(atoms["db-password"], safe="")
    assert encoded != atoms["db-password"]

    lines = [f"{name}={value} (seen)" for name, value in sorted(atoms.items())]
    lines.append(f"DATABASE_URL=postgresql://postiz:{encoded}@iforge-postiz-db:5432/postiz")
    lines.append("an ordinary line with nothing to hide")
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _progress(*lines, stream="stderr"),
    )
    assert reported.status_code == 200, reported.text
    assert reported.json()["stored_lines"] == len(lines)

    failed = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {
            "operation": "install",
            "phase": "failed",
            "error": "boot failed: " + " ".join(atoms[name] for name in SECRET_NAMES),
        },
    )
    assert failed.status_code == 200, failed.text

    stored = await _stored_lines(db_session, running.installation_id)
    assert len(stored) == len(lines)
    assert [row.seq for row in stored] == list(range(1, len(lines) + 1))
    assert {row.stream for row in stored} == {"stderr"}
    assert stored[-1].line == "an ordinary line with nothing to hide"
    error = (await _reload(db_session, running.installation_id)).error or ""
    forbidden = [*atoms.values(), encoded]
    for text in [*(row.line for row in stored), error]:
        for value in forbidden:
            assert value not in text, "an installation credential reached storage"
    for row in stored[:-1]:
        assert REDACTED in row.line, row.line
    assert error == "boot failed: " + " ".join([REDACTED] * len(SECRET_NAMES))


async def test_overlong_line_is_refused(db_session, seed_user, companion_zone) -> None:
    running = await _running_installation(db_session, seed_user)

    refused = await _post_report(
        db_session, running.runtime_key, running.installation_id, _progress("x" * 2001)
    )

    assert refused.status_code == 422, refused.text
    assert await _stored_lines(db_session, running.installation_id) == []


async def test_log_keeps_the_newest_5000_lines_per_operation(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)

    for batch in range(26):  # 26 reports x 200 lines = 5200 lines
        response = await _post_report(
            db_session,
            running.runtime_key,
            running.installation_id,
            _progress(*(f"batch {batch} line {index}" for index in range(200))),
        )
        assert response.status_code == 200, response.text

    count = await db_session.scalar(
        select(func.count())
        .select_from(CompanionInstallLogLine)
        .where(CompanionInstallLogLine.installation_id == running.installation_id)
    )
    oldest = await db_session.scalar(
        select(func.min(CompanionInstallLogLine.seq)).where(
            CompanionInstallLogLine.installation_id == running.installation_id
        )
    )
    newest = await db_session.scalar(
        select(func.max(CompanionInstallLogLine.seq)).where(
            CompanionInstallLogLine.installation_id == running.installation_id
        )
    )
    assert count == 5000
    assert (oldest, newest) == (201, 5200)


# ---------------------------------------------------------------------------
# Task 3: the tenant's log route, the worker in the worker process, and the reporting deadline.
# ---------------------------------------------------------------------------


def logs_path(installation_id: uuid.UUID) -> str:
    return f"/v1/companions/installations/{installation_id}/logs"


@asynccontextmanager
async def _dashboard_as(db_session: AsyncSession, user) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in dashboard session for ``user`` (Clerk JWT stand-in, no API key)."""

    async def _override_get_session():
        yield db_session

    async def _override_get_auth() -> AuthContext:
        return AuthContext(user=user)

    previous = dict(app.dependency_overrides)
    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[get_auth] = _override_get_auth
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


async def _other_tenant(db_session: AsyncSession) -> User:
    other = User(
        clerk_id=f"test_other_{uuid.uuid4().hex[:12]}",
        email=f"other-{uuid.uuid4().hex[:12]}@iforge.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


async def test_tenant_reads_install_log_after_seq(db_session, seed_user, companion_zone) -> None:
    running = await _running_installation(db_session, seed_user)
    # Companion text is shown as it was written: markup is not interpreted, only redacted.
    written = [
        ("stdout", "pulling ghcr.io/gitroomhq/postiz-app"),
        ("stderr", "<b>warn</b> prisma db push --accept-data-loss"),
        ("stdout", "**not markdown** either"),
        ("stderr", "backend listening on :3000"),
        ("stdout", "ready"),
    ]
    for stream, line in written:
        response = await _post_report(
            db_session,
            running.runtime_key,
            running.installation_id,
            _progress(line, stream=stream),
        )
        assert response.status_code == 200, response.text

    async with _dashboard_as(db_session, seed_user) as dashboard:
        first = await dashboard.get(
            logs_path(running.installation_id), params={"after_seq": 0, "limit": 2}
        )
        assert first.status_code == 200, first.text
        page = first.json()
        assert [(item["seq"], item["stream"], item["line"]) for item in page["items"]] == [
            (1, *written[0]),
            (2, *written[1]),
        ]
        assert page["next_after_seq"] == 2
        assert all(item["created_at"] for item in page["items"])

        rest = (
            await dashboard.get(
                logs_path(running.installation_id),
                params={"after_seq": page["next_after_seq"]},
            )
        ).json()
        assert [(item["seq"], item["stream"], item["line"]) for item in rest["items"]] == [
            (3, *written[2]),
            (4, *written[3]),
            (5, *written[4]),
        ]
        assert rest["next_after_seq"] == 5

        # Caught up: an empty page keeps the cursor where it was.
        empty = (
            await dashboard.get(logs_path(running.installation_id), params={"after_seq": 5})
        ).json()
        assert empty == {"items": [], "next_after_seq": 5}

        for bad in ({"limit": 0}, {"limit": 501}, {"after_seq": -1}):
            refused = await dashboard.get(logs_path(running.installation_id), params=bad)
            assert refused.status_code == 422, (bad, refused.text)


async def test_log_route_is_tenant_scoped(db_session, seed_user, companion_zone) -> None:
    running = await _running_installation(db_session, seed_user)
    response = await _post_report(
        db_session, running.runtime_key, running.installation_id, _progress("mine")
    )
    assert response.status_code == 200, response.text
    other = await _other_tenant(db_session)

    async with _dashboard_as(db_session, other) as dashboard:
        refused = await dashboard.get(logs_path(running.installation_id))
        assert refused.status_code == 404, refused.text
        assert "mine" not in refused.text
    # Control: the owner reads the same log.
    async with _dashboard_as(db_session, seed_user) as dashboard:
        owned = await dashboard.get(logs_path(running.installation_id))
        assert owned.status_code == 200, owned.text
        assert [item["line"] for item in owned.json()["items"]] == ["mine"]
    # The agent's own runtime key is not a dashboard session.
    async with _runtime_client(db_session, running.runtime_key) as runtime:
        forbidden = await runtime.get(logs_path(running.installation_id))
        assert forbidden.status_code == 403, forbidden.text


async def test_running_job_without_a_report_fails_after_the_deadline(
    db_session, seed_user, companion_zone
) -> None:
    recorder = _StepRecorder("tunnel", "dns")
    started = datetime.now(UTC)
    clock = [started]
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: clock[0],
        provisioning_steps=recorder.steps,
    )
    agent_id, _deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "running"

    clock[0] = started + timedelta(seconds=1799)
    assert await worker.run_once() is None
    assert (await _reload(db_session, installation.id)).status == "running"

    clock[0] = started + timedelta(seconds=1801)
    assert await worker.run_once() == installation.id
    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error == "runtime did not report"
    assert recorder.events == [
        "provision:tunnel",
        "provision:dns",
        "teardown:dns",
        "teardown:tunnel",
    ]
    assert row.next_attempt_at is None
    assert await worker.run_once() is None


async def test_a_report_pushes_the_deadline_out(db_session, seed_user, companion_zone) -> None:
    # The job went running 1000 s ago by the worker's clock; the runtime reports now.
    started = datetime.now(UTC) - timedelta(seconds=1000)
    clock = [started]
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session), clock=lambda: clock[0], provisioning_steps=()
    )
    agent_id, deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    assert await worker.run_once() == installation.id
    runtime_key = await _runtime_key(db_session, seed_user, agent_id, deployment_id)
    response = await _post_report(db_session, runtime_key, installation.id, _progress("alive"))
    assert response.status_code == 200, response.text

    # Past the ORIGINAL deadline, inside the one the report set.
    clock[0] = started + timedelta(seconds=1801)
    assert await worker.run_once() is None
    assert (await _reload(db_session, installation.id)).status == "running"

    clock[0] = started + timedelta(seconds=2900)
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "failed"


async def test_a_failing_provisioning_step_fails_the_install_and_tears_down(
    db_session, seed_user, companion_zone
) -> None:
    agent_id, _deployment_id = await _hosted_agent(db_session, seed_user)
    async with billing_quote(seed_user.clerk_id) as quote_id:
        installation = await create_installation(
            db_session,
            auth=AuthContext(user=seed_user),
            agent_id=agent_id,
            companion_key="postiz",
            isolation_tier="standard",
            quote_id=quote_id,
        )
    atoms = await _atoms(db_session, installation.id)
    recorder = _StepRecorder("tunnel", "bucket")

    async def refuse(context: LifecycleStepContext) -> dict:
        raise RuntimeError(f"edge API refused token {atoms['api-key']}")

    async def never(context: LifecycleStepContext, resources: dict) -> None:
        raise AssertionError("a step that never completed must not be torn down")

    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(*recorder.steps, ProvisioningStep("dns", refuse, never)),
    )

    assert await worker.run_once() == installation.id

    row = await _reload(db_session, installation.id)
    assert row.status == "failed"
    assert row.error is not None
    assert "dns" in row.error
    assert atoms["api-key"] not in row.error
    assert REDACTED in row.error
    assert recorder.events == [
        "provision:tunnel",
        "provision:bucket",
        "teardown:bucket",
        "teardown:tunnel",
    ]
    assert row.next_attempt_at is None
    # The projection never saw it: it went straight from queued to failed.
    assert row.completed_at is None


async def test_tools_list_is_not_cached_while_an_install_is_in_flight(
    db_session, seed_user, companion_zone, no_connector_tools
) -> None:
    """The completion's cache reset only reaches ITS process; another process must not be stale.

    The worker runs in the worker process, the MCP endpoint in the API process, so the reset the
    worker performs cannot clear the API process's cache. A tenant with an installation in
    flight is therefore not cached: the list that follows completion is read fresh wherever it
    is served. Simulated here by completing the job WITHOUT the worker (no in-process reset).
    """
    running = await _running_installation(db_session, seed_user)
    auth = AuthContext(
        user=seed_user,
        api_key=ApiKey(user_id=seed_user.id, managed=True, scopes=list(RUNTIME_MCP_SCOPES)),
    )
    assert (await _names(auth, db_session)).isdisjoint(POSTIZ_VERBS)

    row = await _reload(db_session, running.installation_id)
    row.status = "complete"
    row.completed_at = datetime.now(UTC)
    await db_session.commit()

    assert POSTIZ_VERBS <= await _names(auth, db_session)


async def test_completed_installation_is_placed_on_its_agent(
    db_session, seed_user, companion_zone
) -> None:
    """Listed and routable agree: the dashboard catalog and call routing name the same agent."""
    complete = await _complete_installation(db_session, seed_user)
    auth = AuthContext(user=seed_user)

    placements = await deployed_companion_environments(auth, db_session)
    assert placements["postiz"] == [complete.agent_id]
    assert await _environment_with_companion(auth, db_session, companion_key="postiz") == (
        complete.agent_id
    )

    async with _dashboard_as(db_session, seed_user) as dashboard:
        catalog = await dashboard.get("/v1/companions/tools")
    assert catalog.status_code == 200, catalog.text
    postiz = [item for item in catalog.json()["companions"] if item["companion_key"] == "postiz"]
    assert postiz, catalog.json()
    assert postiz[0]["environment_ids"] == [str(complete.agent_id)]


def test_worker_is_registered_in_the_worker_process() -> None:
    from app.workers import channels as channel_workers

    workers = channel_workers.build_channel_workers()
    assert any(isinstance(worker, CompanionLifecycleWorker) for worker in workers)
    source = inspect.getsource(channel_workers.build_channel_workers)
    assert "CompanionLifecycleWorker(async_session_factory)" in source
