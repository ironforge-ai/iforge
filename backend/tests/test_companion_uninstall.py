"""Uninstall: confirmed by name, billing stopped first, data destroyed only when told to.

APP-07's uninstall and APP-08's "stopped on deprovision" close the companion lifecycle (plan 06-20):

* The tenant confirms by typing the companion's name; the check is server-side, exact and
  case-sensitive after trimming surrounding whitespace.
* The line item is stopped as the FIRST step of an accepted uninstall, before anything is torn down.
* The manifest carries ``lifecycle: "remove"`` for the installation until the runtime reports
  ``removed`` (research Pitfall 12): destroying data is that explicit instruction, never a side
  effect of an entry disappearing.
* Once the runtime reports ``removed`` -- or 30 minutes pass without it -- every external resource
  the installation's steps created is torn down, newest first, and the installation is removed.
  The backup key is revoked but no backup file is deleted: retained backups age out on retention.

Nothing here reaches a live API. The tracer drives the REAL provisioning, completion and teardown
steps against the stateful fakes the step suites already use (one double, reused): the fake edge,
the fake B2, the fake Cloudflare/Temporal operator APIs and the in-process /v2 mock. Every call
any of them receives is also appended to one shared timeline, so ordering across services is
asserted, not inferred.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.auth import AuthContext
from app.core.config import settings
from app.models.companion_installation import CompanionInstallation
from app.models.hosted_runtime import HostedRuntimeState
from app.models.tenant_export import (
    TENANT_EXPORT_STATUS_COMPLETE,
    TENANT_EXPORT_STATUS_FAILED,
    TENANT_EXPORT_STATUS_RUNNING,
    TenantExportJob,
)
from app.models.tenant_purge import TenantPurge
from app.services import (
    companion_backup_repo,
    companion_edge,
    companion_lifecycle_worker,
    companion_media_store,
    companion_temporal,
    tenant_purge,
)
from app.services.companion_billing import BILLING_START_STEP
from app.services.companion_install import UNINSTALL_RECORD_KEY, create_installation
from app.services.companion_lifecycle_worker import (
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
    teardown_installation_resources,
)
from app.services.file_store import LocalFileStore
from app.services.tenant_export import (
    COMPONENT_STATE_PENDING,
    CompanionExportUploadTarget,
    companion_components,
    companion_upload_key,
    create_tenant_export_job,
    record_companion_export_upload,
    with_companion_components,
)
from tests import test_companion_edge as edge_tests
from tests import test_companion_media_store as media_tests
from tests import test_companion_updates as update_tests
from tests.companion_billing_stub import BillingMock, quote_for, route_billing_to_mock
from tests.test_companion_backup_repo import FILE_DESTROYING_CALLS, FakeB2, _wire_b2
from tests.test_companion_edge import FakeEdgeApi, _wire_edge
from tests.test_companion_install_job import (
    _other_tenant,
    _post_report,
    _ready_report,
    _reload,
    _runtime_key,
    _sessionmaker,
)
from tests.test_companion_media_store import FakeOperatorApis
from tests.test_companion_projection import _agent_with_runtime, _installation, _render

# The shared fixtures: the fake operator APIs with both adapters wired to them, the operator
# settings with the test's own signing key, and the owner's dashboard session.
signing_key = media_tests.signing_key
operator_settings = media_tests.operator_settings
fake_apis = media_tests.fake_apis
media_objects = media_tests.media_objects
web = update_tests.web

UNINSTALL_CONFIRMATION = "Postiz"
LINE_ITEMS_PATH = "/v2/companions/line-items"


def _uninstall_path(installation_id: uuid.UUID) -> str:
    return f"/v1/companions/installations/{installation_id}/uninstall"


def _removed_report() -> dict[str, str]:
    return {"operation": "uninstall", "phase": "removed"}


# ---------------------------------------------------------------------------
# Every operator double at once, on one timeline.
# ---------------------------------------------------------------------------


class Operators:
    """The four operator doubles one real install touches, and the order every call reached them."""

    def __init__(
        self,
        *,
        edge: FakeEdgeApi,
        b2: FakeB2,
        apis: FakeOperatorApis,
        billing: BillingMock,
    ) -> None:
        self.edge = edge
        self.b2 = b2
        self.apis = apis
        self.billing = billing
        self.timeline: list[str] = []

    def recording(
        self, service: str, handle: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.MockTransport:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.timeline.append(f"{service} {request.method} {urlsplit(str(request.url)).path}")
            return handle(request)

        return httpx.MockTransport(_handle)

    def since(self, mark: int) -> list[str]:
        return self.timeline[mark:]


def _named_teardown(call: str) -> str | None:
    """A timeline call named as the teardown action it is, or ``None`` if it is not one."""
    service, method, path = call.split(" ", 2)
    if service == "billing" and method == "DELETE" and path.startswith(LINE_ITEMS_PATH):
        return "stop line"
    if service == "edge" and method == "DELETE" and "/dns_records/" in path:
        return "delete dns"
    if service == "edge" and method == "DELETE" and path.endswith("/connections"):
        return "delete tunnel connections"
    if service == "edge" and method == "DELETE" and "/cfd_tunnel/" in path:
        return "delete tunnel"
    if service == "b2" and path.endswith("/b2_delete_key"):
        return "revoke backup key"
    if service == "operator" and method == "POST" and path.endswith("/update"):
        return "delete namespace"
    if service == "s3" and method == "PURGE":
        return "purge objects"
    if service == "operator" and method == "DELETE" and "/domains/custom/" in path:
        return "detach domain"
    if service == "operator" and method == "DELETE" and "/r2/buckets/" in path:
        return "delete bucket"
    if service == "operator" and method == "DELETE" and "/tokens/" in path:
        return "revoke media token"
    return None


@pytest.fixture
def operators(
    monkeypatch: pytest.MonkeyPatch,
    operator_settings,
    fake_apis: FakeOperatorApis,
    media_objects,
) -> Operators:
    """Every operator the real steps call, wired to its fake and recorded on one timeline.

    The edge fake and the media fake both model Cloudflare, each with its own account and zone;
    one install runs both steps under ONE account and zone, so the edge fake is pointed at the
    media fake's (its paths are module constants read at call time).
    """
    billing = route_billing_to_mock(monkeypatch)
    b2 = _wire_b2(monkeypatch)
    edge = _wire_edge(monkeypatch)
    monkeypatch.setattr(settings, "cloudflare_account_id", media_tests.ACCOUNT_ID)
    monkeypatch.setattr(settings, "companion_edge_zone_id", media_tests.ZONE_ID)
    monkeypatch.setattr(
        edge_tests, "TUNNEL_PREFIX", f"/client/v4/accounts/{media_tests.ACCOUNT_ID}/cfd_tunnel"
    )
    monkeypatch.setattr(
        edge_tests, "DNS_PREFIX", f"/client/v4/zones/{media_tests.ZONE_ID}/dns_records"
    )
    ops = Operators(edge=edge, b2=b2, apis=fake_apis, billing=billing)
    monkeypatch.setattr(companion_edge, "_transport_override", ops.recording("edge", edge.handle))
    monkeypatch.setattr(
        companion_backup_repo, "_transport_override", ops.recording("b2", b2.handle)
    )
    operator_transport = ops.recording("operator", fake_apis.handle)
    monkeypatch.setattr(companion_media_store, "_transport_override", operator_transport)
    monkeypatch.setattr(companion_temporal, "_transport_override", operator_transport)

    open_objects = companion_media_store.create_s3_object_store_client

    def _recorded_objects(**kwargs: Any) -> Any:
        client = open_objects(**kwargs)
        purge = client.delete_prefix

        def _delete_prefix(prefix: str) -> int:
            ops.timeline.append(f"s3 PURGE {kwargs['bucket']}/{prefix}")
            return purge(prefix)

        client.delete_prefix = _delete_prefix
        return client

    monkeypatch.setattr(companion_media_store, "create_s3_object_store_client", _recorded_objects)

    def _record_billing(request: httpx.Request) -> None:
        ops.timeline.append(f"billing {request.method} {request.url.path}")
        return None

    billing.fail = _record_billing
    return ops


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


class _Installed:
    def __init__(self, installation_id, environment_id, runtime_key, worker) -> None:
        self.installation_id: uuid.UUID = installation_id
        self.environment_id: uuid.UUID = environment_id
        self.runtime_key: str = runtime_key
        self.worker: CompanionLifecycleWorker = worker


async def _installed_for_real(db_session, seed_user, monkeypatch: pytest.MonkeyPatch) -> _Installed:
    """A complete installation, provisioned and completed by the REAL pipeline against the fakes."""
    environment_id = await _agent_with_runtime(db_session, seed_user)
    deployment_id = await db_session.scalar(
        select(HostedRuntimeState.deployment_id).where(
            HostedRuntimeState.environment_id == environment_id
        )
    )
    assert deployment_id is not None
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    installation = await create_installation(
        db_session,
        auth=AuthContext(user=seed_user),
        agent_id=environment_id,
        companion_key="postiz",
        isolation_tier="standard",
        quote_id=quote_id,
    )
    worker = CompanionLifecycleWorker(_sessionmaker(db_session))
    assert await worker.run_once() == installation.id
    row = await _reload(db_session, installation.id)
    assert row.status == "running", row.error
    runtime_key = await _runtime_key(db_session, seed_user, environment_id, deployment_id)
    reported = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        _ready_report(applied_digest=installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text
    assert await worker.run_once() == installation.id
    row = await _reload(db_session, installation.id)
    assert row.status == "complete", row.error
    return _Installed(installation.id, environment_id, runtime_key, worker)


def _recorded_step_names(row) -> list[str]:
    steps = (row.external_resources or {}).get("steps") or []
    return [str(entry.get("name")) for entry in steps if isinstance(entry, dict)]


# ---------------------------------------------------------------------------
# Task 1 (tracer): the tenant uninstalls, billing stops, data and resources are removed.
# ---------------------------------------------------------------------------


async def test_uninstall_stops_billing_removes_data_and_tears_down(
    web, db_session, seed_user, operators: Operators, monkeypatch
) -> None:
    installed = await _installed_for_real(db_session, seed_user, monkeypatch)
    installation_id = installed.installation_id
    row = await _reload(db_session, installation_id)
    # Every provisioning and completion step ran and is recorded, in execution order.
    assert _recorded_step_names(row) == [
        "media",
        "temporal",
        "edge",
        "backup",
        "edge-publish",
        "billing-start",
    ]
    line_item_id = row.billing_line_item_id
    assert line_item_id is not None
    assert row.billing_stopped_at is None
    source = await _render(db_session, installed.environment_id)
    assert source.manifest["companions"]["postiz"]["lifecycle"] == "present"

    # The tenant confirms by the companion's name; the request is accepted as a running uninstall.
    accepted = await web.post(
        _uninstall_path(installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["operation"] == "uninstall"
    assert accepted.json()["status"] == "running"

    # The worker's first pass stops the line item -- and touches nothing else yet.
    mark = len(operators.timeline)
    assert await installed.worker.run_once() == installation_id
    first_pass = operators.since(mark)
    assert first_pass == [f"billing DELETE {LINE_ITEMS_PATH}/{line_item_id}"], first_pass
    row = await _reload(db_session, installation_id)
    assert row.billing_stopped_at is not None
    assert row.removed_at is None
    assert row.status == "running"
    stopped = operators.billing.line_items_for(installation_id)
    assert [line.get("stopped_at") is not None for line in stopped] == [True]

    # Destruction is an explicit instruction to the runtime, never the entry disappearing.
    source = await _render(db_session, installed.environment_id)
    assert source.manifest["companions"]["postiz"]["lifecycle"] == "remove"

    # Nothing is torn down before the runtime says the host side is gone.
    assert await installed.worker.run_once() is None
    assert operators.since(mark) == first_pass

    reported = await _post_report(
        db_session, installed.runtime_key, installation_id, _removed_report()
    )
    assert reported.status_code == 200, reported.text
    assert await installed.worker.run_once() == installation_id

    teardown = [
        name
        for name in (_named_teardown(call) for call in operators.since(mark))
        if name is not None
    ]
    # The line stop precedes every teardown call; then each recorded step, newest first.
    assert teardown[0] == "stop line"
    destructive = [name for name in teardown if name != "stop line"]
    assert destructive == [
        "delete dns",
        "revoke backup key",
        "delete tunnel connections",
        "delete tunnel",
        "delete namespace",
        "purge objects",
        "detach domain",
        "delete bucket",
        "revoke media token",
    ], teardown
    first_destructive = next(
        index
        for index, call in enumerate(operators.since(mark))
        if _named_teardown(call) not in (None, "stop line")
    )
    assert operators.since(mark)[0].startswith("billing DELETE"), operators.since(mark)
    assert first_destructive > 0

    # The backup key is revoked and no backup file is touched: backups age out on retention.
    for call in FILE_DESTROYING_CALLS:
        assert operators.b2.named(call) == []
    assert operators.b2.keys == {}
    assert operators.edge.dns_records == {}
    assert all(tunnel["deleted"] for tunnel in operators.edge.tunnels.values())
    assert operators.apis.buckets == {}
    assert operators.apis.domains == {}
    assert operators.apis.tokens == {}
    assert all(
        namespace.state == "NAMESPACE_STATE_DELETED"
        for namespace in operators.apis.namespaces.values()
    )

    row = await _reload(db_session, installation_id)
    assert row.removed_at is not None
    assert row.operation == "uninstall"
    assert row.status == "complete"
    assert _recorded_step_names(row) == []

    # A fresh render no longer carries the companion at all.
    source = await _render(db_session, installed.environment_id)
    assert "postiz" not in (source.manifest.get("companions") or {})


# ---------------------------------------------------------------------------
# Task 3: the deadline, the refusals, the last companion, a repeated teardown, and tenant purge.
# ---------------------------------------------------------------------------


class _Clock:
    """The worker's clock, moved by hand.

    Starts a second ahead of the wall clock, because the routes stamp their times with the wall
    clock and the worker must see them as due.
    """

    def __init__(self) -> None:
        self.now = datetime.now(UTC) + timedelta(seconds=1)

    def __call__(self) -> datetime:
        return self.now


class _RecordingStep:
    """A stand-in for a real provisioning step, under the real step's NAME, recording on a timeline.

    Its teardown also records whether the installation row still existed when it ran, which is
    how the purge test proves teardown happens before any row is deleted.
    """

    def __init__(self, name: str, timeline: list[str], *, fail_teardown: bool = False) -> None:
        self.name = name
        self.timeline = timeline
        self.fail_teardown = fail_teardown

    async def _provision(self, context: LifecycleStepContext) -> dict[str, Any]:
        self.timeline.append(f"provision {self.name}")
        return {"handle": f"{self.name}-{context.installation_id}"}

    async def _teardown(self, context: LifecycleStepContext, resources: dict[str, Any]) -> None:
        assert resources == {"handle": f"{self.name}-{context.installation_id}"}
        async with context.sessionmaker() as session:
            still_there = await session.scalar(
                select(CompanionInstallation.id).where(
                    CompanionInstallation.id == context.installation_id
                )
            )
            await session.rollback()
        self.timeline.append(f"teardown {self.name}" + ("" if still_there else " (row gone)"))
        if self.fail_teardown:
            raise RuntimeError(f"{self.name} teardown refused")

    @property
    def step(self) -> ProvisioningStep:
        return ProvisioningStep(self.name, self._provision, self._teardown)


async def _complete_with(
    db_session,
    seed_user,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provisioning_steps: tuple[ProvisioningStep, ...] = (),
    completion_steps: tuple[ProvisioningStep, ...] = (),
    clock: Callable[[], datetime] | None = None,
) -> _Installed:
    """A complete installation on a renderable agent, driven by the given (stand-in) steps."""
    monkeypatch.setattr(settings, "companion_edge_zone_name", media_tests.ZONE)
    environment_id = await _agent_with_runtime(db_session, seed_user)
    deployment_id = await db_session.scalar(
        select(HostedRuntimeState.deployment_id).where(
            HostedRuntimeState.environment_id == environment_id
        )
    )
    assert deployment_id is not None
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    installation = await create_installation(
        db_session,
        auth=AuthContext(user=seed_user),
        agent_id=environment_id,
        companion_key="postiz",
        isolation_tier="standard",
        quote_id=quote_id,
    )
    if clock is None:
        worker = CompanionLifecycleWorker(
            _sessionmaker(db_session),
            provisioning_steps=provisioning_steps,
            completion_steps=completion_steps,
        )
    else:
        worker = CompanionLifecycleWorker(
            _sessionmaker(db_session),
            provisioning_steps=provisioning_steps,
            completion_steps=completion_steps,
            clock=clock,
        )
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "running"
    runtime_key = await _runtime_key(db_session, seed_user, environment_id, deployment_id)
    reported = await _post_report(
        db_session,
        runtime_key,
        installation.id,
        _ready_report(applied_digest=installation.pinned_digest),
    )
    assert reported.status_code == 200, reported.text
    assert await worker.run_once() == installation.id
    row = await _reload(db_session, installation.id)
    assert row.status == "complete", row.error
    return _Installed(installation.id, environment_id, runtime_key, worker)


def _error_code(response: httpx.Response) -> str | None:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else None


async def test_uninstall_without_runtime_confirmation_still_tears_down_after_30_minutes(
    web, db_session, seed_user, monkeypatch
) -> None:
    timeline: list[str] = []
    # Names no real step uses: the render composes a real step's credential atoms, which these
    # stand-ins never store.
    steps = (_RecordingStep("alpha", timeline), _RecordingStep("beta", timeline))
    clock = _Clock()
    installed = await _complete_with(
        db_session,
        seed_user,
        monkeypatch,
        provisioning_steps=tuple(step.step for step in steps),
        clock=clock,
    )
    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    started = (await _reload(db_session, installed.installation_id)).operation_started_at

    # The first pass stops billing (this installation never billed: nothing to call) and waits.
    clock.now = started + timedelta(seconds=5)
    assert await installed.worker.run_once() == installed.installation_id
    row = await _reload(db_session, installed.installation_id)
    assert isinstance(row.external_resources["uninstall"].get("billing_stopped_at"), str)

    # 29 minutes in, with no report: nothing is due and nothing is torn down.
    clock.now = started + timedelta(minutes=29)
    assert await installed.worker.run_once() is None
    assert [event for event in timeline if event.startswith("teardown")] == []
    source = await _render(db_session, installed.environment_id)
    assert source.manifest["companions"]["postiz"]["lifecycle"] == "remove"

    # Past 30 minutes: the control plane finishes without the runtime, and says so.
    clock.now = started + timedelta(minutes=30, seconds=1)
    assert await installed.worker.run_once() == installed.installation_id
    assert [event for event in timeline if event.startswith("teardown")] == [
        "teardown beta",
        "teardown alpha",
    ]
    row = await _reload(db_session, installed.installation_id)
    assert row.external_resources["host_removal_unconfirmed"] is True
    assert "removed_reported_at" not in row.external_resources
    assert row.removed_at is not None
    assert row.status == "complete"
    source = await _render(db_session, installed.environment_id)
    assert "postiz" not in (source.manifest.get("companions") or {})


async def _uninstall_without_host_confirmation(
    web, db_session, installed: _Installed, clock: _Clock
) -> None:
    """Uninstall ``installed`` and let the removal deadline pass with no ``removed`` report."""
    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    started = (await _reload(db_session, installed.installation_id)).operation_started_at
    clock.now = started + timedelta(seconds=5)
    assert await installed.worker.run_once() == installed.installation_id
    clock.now = started + timedelta(minutes=30, seconds=1)
    assert await installed.worker.run_once() == installed.installation_id
    row = await _reload(db_session, installed.installation_id)
    assert row.external_resources["host_removal_unconfirmed"] is True
    assert row.removed_at is not None


async def _start_install_on(
    db_session,
    seed_user,
    monkeypatch: pytest.MonkeyPatch,
    environment_id: uuid.UUID,
    worker: CompanionLifecycleWorker,
    clock: _Clock,
) -> uuid.UUID:
    """A new installation of Postiz on an EXISTING agent, started by the worker (so it is live)."""
    clock.now = datetime.now(UTC) + timedelta(seconds=1)
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    installation = await create_installation(
        db_session,
        auth=AuthContext(user=seed_user),
        agent_id=environment_id,
        companion_key="postiz",
        isolation_tier="standard",
        quote_id=quote_id,
    )
    assert await worker.run_once() == installation.id
    assert (await _reload(db_session, installation.id)).status == "running"
    return installation.id


async def test_reinstall_after_an_unconfirmed_removal_lists_the_retired_installation(
    web, db_session, seed_user, monkeypatch
) -> None:
    # CR-04 (plan 06-42): an uninstall the tenant confirmed but the host never confirmed leaves a
    # withdrawn receipt -- and the data -- on the host. The next installation's entry names it, so
    # the host removes that data because the tenant confirmed it, and applies the new install.
    clock = _Clock()
    installed = await _complete_with(db_session, seed_user, monkeypatch, clock=clock)
    await _uninstall_without_host_confirmation(web, db_session, installed, clock)

    reinstall_id = await _start_install_on(
        db_session, seed_user, monkeypatch, installed.environment_id, installed.worker, clock
    )

    source = await _render(db_session, installed.environment_id)
    entry = source.manifest["companions"]["postiz"]
    assert entry["installationId"] == str(reinstall_id)
    assert entry["lifecycle"] == "present"
    assert entry.get("retiredInstallations") == [{"installationId": str(installed.installation_id)}]


async def _retire_without_host_confirmation(
    db_session, installation_id: uuid.UUID, *, removed_at: datetime
) -> None:
    """Put an installation in the state the uninstall pipeline leaves when its deadline passed."""
    row = await _reload(db_session, installation_id)
    row.operation = "uninstall"
    row.status = "complete"
    row.removed_at = removed_at
    row.external_resources = {**(row.external_resources or {}), "host_removal_unconfirmed": True}
    await db_session.commit()


async def _retired_listed(db_session, environment_id: uuid.UUID) -> list[str]:
    source = await _render(db_session, environment_id)
    entry = source.manifest["companions"]["postiz"]
    return [item["installationId"] for item in entry["retiredInstallations"]]


async def test_a_removal_the_runtime_confirmed_is_never_listed(
    web, db_session, seed_user, monkeypatch
) -> None:
    clock = _Clock()
    installed = await _complete_with(db_session, seed_user, monkeypatch, clock=clock)
    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    started = (await _reload(db_session, installed.installation_id)).operation_started_at
    clock.now = started + timedelta(seconds=5)
    assert await installed.worker.run_once() == installed.installation_id
    reported = await _post_report(
        db_session, installed.runtime_key, installed.installation_id, _removed_report()
    )
    assert reported.status_code == 200, reported.text
    assert await installed.worker.run_once() == installed.installation_id
    row = await _reload(db_session, installed.installation_id)
    assert row.removed_at is not None
    assert "host_removal_unconfirmed" not in row.external_resources

    await _start_install_on(
        db_session, seed_user, monkeypatch, installed.environment_id, installed.worker, clock
    )

    # The host already destroyed that data: there is nothing left for the tenant to authorize.
    assert await _retired_listed(db_session, installed.environment_id) == []


async def test_another_agents_or_another_kinds_retired_installation_is_never_listed(
    db_session, seed_user, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "companion_edge_zone_name", media_tests.ZONE)
    agent = await _agent_with_runtime(db_session, seed_user)
    other_agent = await _agent_with_runtime(db_session, seed_user)
    retired_here = await _installation(db_session, seed_user, agent)
    await _retire_without_host_confirmation(
        db_session, retired_here, removed_at=datetime.now(UTC) - timedelta(hours=1)
    )
    retired_elsewhere = await _installation(db_session, seed_user, other_agent)
    await _retire_without_host_confirmation(
        db_session, retired_elsewhere, removed_at=datetime.now(UTC) - timedelta(hours=1)
    )
    # A retired row of another kind on this very agent: the entry names only its own kind's.
    other_kind = await _installation(db_session, seed_user, agent)
    await _retire_without_host_confirmation(
        db_session, other_kind, removed_at=datetime.now(UTC) - timedelta(minutes=30)
    )
    other_kind_row = await _reload(db_session, other_kind)
    other_kind_row.companion_key = "filebrowser"
    await db_session.commit()
    live_here = await _installation(db_session, seed_user, agent)
    live_elsewhere = await _installation(db_session, seed_user, other_agent)

    here = await _retired_listed(db_session, agent)
    elsewhere = await _retired_listed(db_session, other_agent)

    assert here == [str(retired_here)]
    assert elsewhere == [str(retired_elsewhere)]
    # Never a live installation, never another agent's, never another kind's.
    for listed in (here, elsewhere):
        assert str(live_here) not in listed
        assert str(live_elsewhere) not in listed
        assert str(other_kind) not in listed


async def test_more_than_eight_retirements_list_the_newest_eight(
    db_session, seed_user, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "companion_edge_zone_name", media_tests.ZONE)
    agent = await _agent_with_runtime(db_session, seed_user)
    base = datetime.now(UTC) - timedelta(days=1)
    retired: list[uuid.UUID] = []
    for index in range(9):
        installation_id = await _installation(db_session, seed_user, agent)
        await _retire_without_host_confirmation(
            db_session, installation_id, removed_at=base + timedelta(minutes=index)
        )
        retired.append(installation_id)
    await _installation(db_session, seed_user, agent)

    listed = await _retired_listed(db_session, agent)

    newest_first = [str(installation_id) for installation_id in reversed(retired)]
    assert listed == newest_first[:8]
    assert str(retired[0]) not in listed


async def test_uninstall_confirmation_must_match_exactly(
    web, db_session, seed_user, monkeypatch
) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    path = _uninstall_path(installed.installation_id)
    for wrong in ("Posttiz", "postiz", "POSTIZ", "Postiz!", "", "P o s t i z"):
        refused = await web.post(path, json={"confirm": wrong})
        assert refused.status_code == 422, (wrong, refused.text)
        assert _error_code(refused) == "companion_uninstall_confirmation_mismatch", wrong
        row = await _reload(db_session, installed.installation_id)
        assert (row.operation, row.status) == ("install", "complete"), wrong

    # Surrounding whitespace is not part of the name.
    accepted = await web.post(path, json={"confirm": "  Postiz \n"})
    assert accepted.status_code == 202, accepted.text
    row = await _reload(db_session, installed.installation_id)
    assert (row.operation, row.status) == ("uninstall", "running")


async def test_second_uninstall_is_409(web, db_session, seed_user, monkeypatch) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    path = _uninstall_path(installed.installation_id)
    first = await web.post(path, json={"confirm": UNINSTALL_CONFIRMATION})
    assert first.status_code == 202, first.text
    started = (await _reload(db_session, installed.installation_id)).operation_started_at

    second = await web.post(path, json={"confirm": UNINSTALL_CONFIRMATION})
    assert second.status_code == 409, second.text
    assert _error_code(second) == "companion_operation_in_progress"
    # The running uninstall is untouched: its deadline still counts from the first request.
    row = await _reload(db_session, installed.installation_id)
    assert row.operation_started_at == started


# ---------------------------------------------------------------------------
# Plan 06-36 (CR-05): an uninstall never destroys the data an in-flight export is copying.
# ---------------------------------------------------------------------------


async def _start_export(web: httpx.AsyncClient) -> uuid.UUID:
    """The tenant starts their own export (``POST /v1/me/exports``), as the dashboard does."""
    started = await web.post("/v1/me/exports")
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["job_id"])


async def _reload_job(db_session, job_id: uuid.UUID) -> TenantExportJob:
    return (
        await db_session.execute(
            select(TenantExportJob)
            .where(TenantExportJob.id == job_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _installation_state(row: CompanionInstallation) -> tuple[Any, ...]:
    """Everything an accepted uninstall writes; a refusal must leave every one of them as it was."""
    return (
        row.operation,
        row.status,
        row.step,
        row.operation_started_at,
        row.next_attempt_at,
        dict(row.external_resources or {}),
        row.removed_at,
    )


def _pending_component(installation_id: uuid.UUID | str) -> dict[str, Any]:
    return {
        "installation_id": str(installation_id),
        "companion_key": "postiz",
        "state": COMPONENT_STATE_PENDING,
        "requested_at": datetime.now(UTC).isoformat(),
    }


async def test_uninstall_is_refused_while_an_export_owes_this_companion(
    web, db_session, seed_user, monkeypatch
) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    installation_id = installed.installation_id
    path = _uninstall_path(installation_id)

    # The tenant takes the export offer: the job owes this installation's data.
    job_id = await _start_export(web)
    job = await _reload_job(db_session, job_id)
    assert [
        (component["installation_id"], component["state"])
        for component in companion_components(job.manifest)
    ] == [(str(installation_id), COMPONENT_STATE_PENDING)]
    before = _installation_state(await _reload(db_session, installation_id))

    # Then confirms the uninstall by name: refused by name, and nothing is written.
    refused = await web.post(path, json={"confirm": UNINSTALL_CONFIRMATION})
    assert refused.status_code == 409, refused.text
    assert _error_code(refused) == "companion_export_in_progress"
    row = await _reload(db_session, installation_id)
    assert _installation_state(row) == before
    assert UNINSTALL_RECORD_KEY not in (row.external_resources or {})
    # The runtime is still told the companion is present, and still asked for the dump.
    source = await _render(db_session, installed.environment_id)
    entry = source.manifest["companions"]["postiz"]
    assert entry["lifecycle"] == "present"
    assert entry["exportRequests"] == [{"jobId": str(job_id)}]

    # The dump is uploaded but the job has not assembled yet: the media is still read from the
    # installation's bucket at assembly, so the uninstall still waits.
    await record_companion_export_upload(
        db_session,
        target=CompanionExportUploadTarget(
            user_id=seed_user.id,
            installation_id=installation_id,
            companion_key="postiz",
            job_id=job_id,
            object_key=companion_upload_key(seed_user.id, job_id, "postiz", installation_id),
        ),
        size=128,
    )
    job = await _reload_job(db_session, job_id)
    job.status = TENANT_EXPORT_STATUS_RUNNING
    await db_session.commit()
    still_refused = await web.post(path, json={"confirm": UNINSTALL_CONFIRMATION})
    assert still_refused.status_code == 409, still_refused.text
    assert _error_code(still_refused) == "companion_export_in_progress"
    assert _installation_state(await _reload(db_session, installation_id)) == before

    # The export is complete: it no longer owes anything, and the same request is accepted.
    job = await _reload_job(db_session, job_id)
    job.status = TENANT_EXPORT_STATUS_COMPLETE
    await db_session.commit()
    accepted = await web.post(path, json={"confirm": UNINSTALL_CONFIRMATION})
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["operation"] == "uninstall"
    source = await _render(db_session, installed.environment_id)
    assert source.manifest["companions"]["postiz"]["lifecycle"] == "remove"


async def test_a_failed_export_does_not_block_uninstall(
    web, db_session, seed_user, monkeypatch
) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    job_id = await _start_export(web)
    job = await _reload_job(db_session, job_id)
    # Still pending in its manifest, but the job itself has ended: nothing is owed any more.
    assert [component["state"] for component in companion_components(job.manifest)] == [
        COMPONENT_STATE_PENDING
    ]
    job.status = TENANT_EXPORT_STATUS_FAILED
    await db_session.commit()

    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    row = await _reload(db_session, installed.installation_id)
    assert (row.operation, row.status) == ("uninstall", "running")


async def test_an_export_owing_another_installation_does_not_block_uninstall(
    web, db_session, seed_user, monkeypatch
) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    # The same tenant's queued export owes a DIFFERENT installation's data.
    db_session.add(
        TenantExportJob(
            user_id=seed_user.id,
            manifest=with_companion_components(None, [_pending_component(uuid.uuid4())]),
        )
    )
    # Another tenant's queued export names THIS installation: it is not this tenant's export.
    other = await _other_tenant(db_session)
    db_session.add(
        TenantExportJob(
            user_id=other.id,
            manifest=with_companion_components(
                None, [_pending_component(installed.installation_id)]
            ),
        )
    )
    await db_session.commit()

    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    row = await _reload(db_session, installed.installation_id)
    assert (row.operation, row.status) == ("uninstall", "running")


# PostgreSQL's SQLSTATE for ``lock_timeout`` expiring (asyncpg ``LockNotAvailableError``).
LOCK_NOT_AVAILABLE = "55P03"


@pytest.mark.committed_db
async def test_export_creation_waits_for_an_uninstall_holding_the_installation_row(
    engine, db_session, seed_user, monkeypatch
) -> None:
    """Export creation and uninstall serialize on the installation row (CR-05).

    The uninstall locks the installation ``FOR UPDATE`` before asking whether an export owes it.
    An export created meanwhile must wait for that transaction rather than read the row past it --
    otherwise the uninstall sees no export and the export promises data the uninstall destroys.
    Two independent connections, as two API workers would be.
    """
    monkeypatch.setattr(settings, "companion_edge_zone_name", media_tests.ZONE)
    environment_id = await _agent_with_runtime(db_session, seed_user)
    installation_id = await _installation(db_session, seed_user, environment_id, status="complete")
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async with sessions() as uninstall, sessions() as export:
        # The uninstall holds the installation row, uncommitted.
        await uninstall.execute(
            select(CompanionInstallation.id)
            .where(CompanionInstallation.id == installation_id)
            .with_for_update()
        )

        # The export waits on it: bounded here by a short lock timeout, which it hits.
        await export.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(DBAPIError) as blocked:
            await create_tenant_export_job(export, user_id=seed_user.id)
        assert getattr(blocked.value.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE, blocked.value
        await export.rollback()

        # Once the uninstall's transaction ends, the same request goes through and records the
        # installation it will carry.
        await uninstall.rollback()
        await export.execute(text("SET LOCAL lock_timeout = '2s'"))
        job = await create_tenant_export_job(export, user_id=seed_user.id)
        assert [
            (component["installation_id"], component["state"])
            for component in companion_components(job.manifest)
        ] == [(str(installation_id), COMPONENT_STATE_PENDING)]
        await export.rollback()


async def test_last_companion_removal_omits_companions_from_the_manifest(
    web, db_session, seed_user, monkeypatch
) -> None:
    installed = await _complete_with(db_session, seed_user, monkeypatch)
    source = await _render(db_session, installed.environment_id)
    # The only companion the agent has is this installation (no provisioner-declared one).
    assert set(source.manifest["companions"]) == {"postiz"}

    accepted = await web.post(
        _uninstall_path(installed.installation_id), json={"confirm": UNINSTALL_CONFIRMATION}
    )
    assert accepted.status_code == 202, accepted.text
    assert await installed.worker.run_once() == installed.installation_id
    reported = await _post_report(
        db_session, installed.runtime_key, installed.installation_id, _removed_report()
    )
    assert reported.status_code == 200, reported.text
    assert await installed.worker.run_once() == installed.installation_id
    assert (await _reload(db_session, installed.installation_id)).removed_at is not None

    # No `companions: {}` -- that would be a positive instruction to converge to zero; the key is
    # absent, and the runtime withdraws what it no longer sees (Pitfall 12).
    source = await _render(db_session, installed.environment_id)
    assert "companions" not in source.manifest


async def test_teardown_is_safe_to_run_twice(db_session, seed_user, monkeypatch) -> None:
    timeline: list[str] = []
    steps = (_RecordingStep("media", timeline), _RecordingStep("temporal", timeline))
    installed = await _complete_with(
        db_session,
        seed_user,
        monkeypatch,
        provisioning_steps=tuple(step.step for step in steps),
    )
    row = await _reload(db_session, installed.installation_id)
    stand_ins = tuple(step.step for step in steps)

    first = await teardown_installation_resources(db_session, row, steps=stand_ins)
    assert first == ["temporal", "media"]
    second = await teardown_installation_resources(
        db_session, row, steps=stand_ins, sessionmaker=_sessionmaker(db_session)
    )
    assert second == []
    assert [event for event in timeline if event.startswith("teardown")] == [
        "teardown temporal",
        "teardown media",
    ]
    row = await _reload(db_session, installed.installation_id)
    assert row.external_resources["steps"] == []
    assert [entry["name"] for entry in row.external_resources["torn_down"]] == [
        "temporal",
        "media",
    ]


def _purge_row(seed_user) -> TenantPurge:
    now = datetime.now(UTC)
    return TenantPurge(
        id=uuid.uuid4(),
        lifecycle_id=uuid.uuid4(),
        user_id=seed_user.id,
        subject=seed_user.clerk_id,
        issuer="https://issuer.companion-uninstall.test",
        purge_after=now - timedelta(days=1),
    )


class _PurgeProvider:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_identity(self, subject: str) -> None:
        self.deleted.append(subject)


def _wire_purge(monkeypatch: pytest.MonkeyPatch) -> None:
    """The purge's own /v2 calls (deployments, subscription) and identity provider, faked."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v2/subscription/cancel":
            return httpx.Response(200, json={"status": "canceled"})
        return httpx.Response(404)

    monkeypatch.setattr(tenant_purge, "_transport_override", httpx.MockTransport(handler))
    monkeypatch.setattr(tenant_purge, "get_user_provisioning_provider", _PurgeProvider)


async def test_tenant_purge_tears_down_live_installations_before_rows(
    db_session, seed_user, monkeypatch, tmp_path
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    # One timeline: the /v2 calls and the stand-in teardowns, in the order they happened.
    steps = (_RecordingStep("media", billing.calls), _RecordingStep("temporal", billing.calls))
    stand_ins = tuple(step.step for step in steps)
    # The purge tears down with the pipeline's own steps; these stand in for them by name.
    monkeypatch.setattr(companion_lifecycle_worker, "PROVISIONING_STEPS", stand_ins)
    installed = await _complete_with(
        db_session,
        seed_user,
        monkeypatch,
        provisioning_steps=stand_ins,
        completion_steps=(BILLING_START_STEP,),
    )
    row = await _reload(db_session, installed.installation_id)
    line_item_id = row.billing_line_item_id
    assert line_item_id is not None
    _wire_purge(monkeypatch)

    mark = len(billing.calls)
    result = await tenant_purge.execute_tenant_purge(
        db_session, LocalFileStore(str(tmp_path / "purge-files")), _purge_row(seed_user)
    )
    after = billing.calls[mark:]

    stop = f"DELETE {LINE_ITEMS_PATH}/{line_item_id}"
    assert stop in after, after
    teardowns = [event for event in after if event.startswith("teardown")]
    # Every recorded step torn down, newest first, each while the installation row still existed.
    assert teardowns == ["teardown temporal", "teardown media"], after
    assert after.index(stop) < after.index("teardown temporal"), after
    stopped = billing.line_items_for(installed.installation_id)
    assert [line.get("stopped_at") is not None for line in stopped] == [True]
    assert result["companion_installations"] == [str(installed.installation_id)]
    # Then the rows go.
    remaining = await db_session.scalar(
        select(CompanionInstallation.id).where(CompanionInstallation.user_id == seed_user.id)
    )
    assert remaining is None


async def test_tenant_purge_fails_while_a_companion_teardown_fails(
    db_session, seed_user, monkeypatch, tmp_path
) -> None:
    timeline: list[str] = []
    steps = (
        _RecordingStep("media", timeline, fail_teardown=True),
        _RecordingStep("temporal", timeline),
    )
    stand_ins = tuple(step.step for step in steps)
    monkeypatch.setattr(companion_lifecycle_worker, "PROVISIONING_STEPS", stand_ins)
    installed = await _complete_with(
        db_session, seed_user, monkeypatch, provisioning_steps=stand_ins
    )
    _wire_purge(monkeypatch)

    # Never "complete" on a partial irreversible operation: the purge fails, and retries later.
    with pytest.raises(RuntimeError, match="media teardown refused"):
        await tenant_purge.execute_tenant_purge(
            db_session, LocalFileStore(str(tmp_path / "purge-files")), _purge_row(seed_user)
        )
    row = await _reload(db_session, installed.installation_id)
    assert _recorded_step_names(row) == ["media"]
    assert [entry["name"] for entry in row.external_resources["torn_down"]] == ["temporal"]
