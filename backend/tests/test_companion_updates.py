"""Pinned by default, updated only on acceptance, behind a backup gate (APP-06, OPS-02, D-28).

An installation runs the app digest it pinned at install time. A newer catalog digest is an OFFER,
computed on read: the tenant accepts that exact digest (the pin moves and an ``update`` operation
starts) or defers it (the offer is withdrawn until a still-newer release appears). Nothing moves
the pin on its own.

The runtime takes a pgBackRest backup before it swaps the digest and asserts on data afterwards
(``container-companion.ts::applyPostizUpdate``). Whenever it reports the update failed -- the
backup failed, or Postiz failed its post-update assertion and was rolled back -- and whenever it
stops reporting at all, the control plane returns the pin to the previous digest, so the manifest
never points at a digest the runtime refused.

**The worker runs inside the test's own transaction**, exactly as in
``test_companion_install_job``: every worker session is joined to the rollback-isolated
``db_session`` through a SAVEPOINT.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import func, select

from app.core.auth import AuthContext, require_web_auth
from app.core.companion_catalog import COMPANION_CATALOG, catalog_entry
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.companion_installation import CompanionInstallLogLine
from app.services.companion_facade import deployed_companion_environments
from app.services.companion_install import REPORT_DEADLINE_SECONDS
from app.services.companion_lifecycle_worker import (
    CompanionLifecycleWorker,
    LifecycleStepContext,
    ProvisioningStep,
)
from app.services.companion_postiz import project_postiz_entry
from app.services.runtime_source import load_runtime_source_batch
from tests.test_companion_install_job import (
    _complete_installation,
    _post_report,
    _ready_report,
    _reload,
    _running_installation,
    _sessionmaker,
)

ZONE = "companions.example"
NEWER_DIGEST = "e" * 64
NEWER_VERSION = "v2.25.0"


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(settings, "companion_edge_zone_name", ZONE)
    return ZONE


@pytest_asyncio.fixture
async def web(db_session, seed_user) -> AsyncIterator[httpx.AsyncClient]:
    """The owner's dashboard session.

    Only ``require_web_auth`` is overridden -- not ``get_auth`` as the shared ``client`` fixture
    does -- so a runtime report made in the same test still authenticates as the runtime's own
    deployment key.
    """

    async def _session():
        yield db_session

    async def _owner() -> AuthContext:
        return AuthContext(user=seed_user)

    overrides = {get_session: _session, require_web_auth: _owner}
    previous = {key: app.dependency_overrides.get(key) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        for key, value in previous.items():
            if value is None:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _release(monkeypatch: pytest.MonkeyPatch, digest: str, version: str) -> None:
    """Make ``digest`` the catalog's current Postiz app digest (a new release was pinned)."""
    current = COMPANION_CATALOG["postiz"]
    newer = replace(current, version=version, app=replace(current.app, tag=version, sha256=digest))
    monkeypatch.setitem(COMPANION_CATALOG, "postiz", newer)


def _installation_path(installation_id: uuid.UUID) -> str:
    return f"/v1/companions/installations/{installation_id}"


def _update_path(installation_id: uuid.UUID) -> str:
    return f"{_installation_path(installation_id)}/update"


def _update_ready_report(*, applied_digest: str, organizations: int = 1) -> dict:
    return _ready_report(
        applied_digest=applied_digest,
        operation="update",
        assertion={
            "connected": True,
            "organizations": organizations,
            "previous_organizations": organizations,
        },
    )


# ---------------------------------------------------------------------------
# Task 1 (tracer): the tenant accepts an update and it is backed up, swapped and asserted.
# ---------------------------------------------------------------------------


async def test_update_is_backed_up_swapped_and_asserted(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _complete_installation(db_session, seed_user)
    previous_digest = running.pinned_digest
    assert previous_digest != NEWER_DIGEST
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)

    # A newer catalog digest is an offer, never an upgrade.
    shown = await web.get(_installation_path(running.installation_id))
    assert shown.status_code == 200, shown.text
    update = shown.json().get("update") or {}
    assert update.get("state") == "available", shown.json()
    assert update["offered_digest"] == NEWER_DIGEST
    assert update["offered_version"] == NEWER_VERSION
    assert shown.json()["pinned_digest"] == previous_digest

    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": NEWER_DIGEST},
    )
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()
    assert body["pinned_digest"] == NEWER_DIGEST
    assert body["operation"] == "update"
    assert body["status"] == "running"
    assert body["update"]["state"] == "in_progress"

    row = await _reload(db_session, running.installation_id)
    assert row.pinned_digest == NEWER_DIGEST
    assert row.operation == "update"
    assert row.status == "running"
    recorded = row.external_resources["update"]
    assert isinstance(recorded, dict)
    assert recorded["from_digest"] == previous_digest
    assert recorded["to_digest"] == NEWER_DIGEST

    # The projection now emits the accepted digest to the agent's runtime.
    batch = await load_runtime_source_batch(db_session, environment_ids=[running.agent_id])
    (projected,) = batch.companion_installations[running.agent_id]
    assert project_postiz_entry(projected, catalog_entry("postiz"))["artifact"]["sha256"] == (
        NEWER_DIGEST
    )

    # The runtime backed up, swapped and asserted on data: a ready report completes the update.
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )
    assert reported.status_code == 200, reported.text
    assert await running.worker.run_once() == running.installation_id

    row = await _reload(db_session, running.installation_id)
    assert row.status == "complete"
    assert row.operation == "update"
    assert row.applied_digest == NEWER_DIGEST
    assert row.pinned_digest == NEWER_DIGEST

    settled = await web.get(_installation_path(running.installation_id))
    assert settled.json()["update"]["state"] == "up_to_date"
    assert settled.json()["update"]["offered_digest"] is None


# ---------------------------------------------------------------------------
# Task 2: the offer rules, the refusals, and the pin returning when an update fails.
# ---------------------------------------------------------------------------

LATER_DIGEST = "d" * 64
LATER_VERSION = "v2.26.0"


async def _accepted(web, db_session, seed_user, monkeypatch) -> tuple:
    """A complete installation whose tenant accepted ``NEWER_DIGEST``; returns it and its pin."""
    running = await _complete_installation(db_session, seed_user)
    previous_digest = running.pinned_digest
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)
    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": NEWER_DIGEST},
    )
    assert accepted.status_code == 200, accepted.text
    return running, previous_digest


def _failed_update_report(step: str, error: str) -> dict:
    return {"operation": "update", "phase": "failed", "step": step, "error": error}


def _conflict_code(response) -> str:
    assert response.status_code == 409, response.text
    return response.json()["detail"]["code"]


async def _projected_entry(db_session, agent_id: uuid.UUID) -> dict:
    batch = await load_runtime_source_batch(db_session, environment_ids=[agent_id])
    (projected,) = batch.companion_installations[agent_id]
    return project_postiz_entry(projected, catalog_entry("postiz"))


async def _projected_digest(db_session, agent_id: uuid.UUID) -> str:
    return (await _projected_entry(db_session, agent_id))["artifact"]["sha256"]


async def test_equal_digest_is_not_an_offer(web, db_session, seed_user, companion_zone) -> None:
    running = await _complete_installation(db_session, seed_user)
    # The catalog's current digest IS the pin (OPS-02 adjacency): nothing to offer, nothing to
    # back up.
    assert catalog_entry("postiz").app.sha256 == running.pinned_digest

    shown = (await web.get(_installation_path(running.installation_id))).json()
    assert shown["update"]["state"] == "up_to_date"
    assert shown["update"]["offered_digest"] is None
    assert shown["update"]["offered_version"] is None

    deferred = await web.post(
        _update_path(running.installation_id),
        json={"decision": "defer", "digest": running.pinned_digest},
    )
    assert _conflict_code(deferred) == "companion_update_not_offered"
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status, row.offered_digest) == ("install", "complete", None)


async def test_defer_then_newer_release_is_offered_again(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _complete_installation(db_session, seed_user)
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)

    deferred = await web.post(
        _update_path(running.installation_id),
        json={"decision": "defer", "digest": NEWER_DIGEST},
    )
    assert deferred.status_code == 200, deferred.text
    assert deferred.json()["update"] == {
        "state": "deferred",
        "offered_version": None,
        "offered_digest": None,
        "deferred_digest": NEWER_DIGEST,
        "last_backup_at": None,
        "error_code": None,
    }
    # Deferring pins nothing new and starts nothing.
    assert deferred.json()["pinned_digest"] == running.pinned_digest
    assert deferred.json()["operation"] == "install"
    # A deferred release stays deferred on every later read.
    assert (await web.get(_installation_path(running.installation_id))).json()["update"][
        "state"
    ] == "deferred"

    # A still-newer release is offered the same way.
    _release(monkeypatch, LATER_DIGEST, LATER_VERSION)
    offered = (await web.get(_installation_path(running.installation_id))).json()["update"]
    assert offered["state"] == "available"
    assert offered["offered_digest"] == LATER_DIGEST
    assert offered["offered_version"] == LATER_VERSION
    assert offered["deferred_digest"] == NEWER_DIGEST
    assert (await _reload(db_session, running.installation_id)).offered_digest == LATER_DIGEST


async def test_accept_of_a_non_offered_digest_is_409(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _complete_installation(db_session, seed_user)
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)

    refused = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": LATER_DIGEST},
    )
    assert _conflict_code(refused) == "companion_update_not_offered"
    row = await _reload(db_session, running.installation_id)
    assert row.pinned_digest == running.pinned_digest
    assert (row.operation, row.status) == ("install", "complete")


async def test_update_with_no_offer_is_refused(web, db_session, seed_user, companion_zone) -> None:
    running = await _complete_installation(db_session, seed_user)

    refused = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": NEWER_DIGEST},
    )
    assert _conflict_code(refused) == "companion_update_not_offered"
    row = await _reload(db_session, running.installation_id)
    assert row.pinned_digest == running.pinned_digest
    assert (row.operation, row.status) == ("install", "complete")


async def test_accept_while_an_operation_runs_is_409(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, _previous = await _accepted(web, db_session, seed_user, monkeypatch)
    # A still-newer release arrives while the first update is running: nothing may start.
    _release(monkeypatch, LATER_DIGEST, LATER_VERSION)
    for decision in ("accept", "defer"):
        refused = await web.post(
            _update_path(running.installation_id),
            json={"decision": decision, "digest": LATER_DIGEST},
        )
        assert _conflict_code(refused) == "companion_operation_in_progress"
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status, row.pinned_digest) == ("update", "running", NEWER_DIGEST)


async def test_accept_of_the_running_digest_is_a_noop(
    web, db_session, seed_user, companion_zone
) -> None:
    running = await _complete_installation(db_session, seed_user)
    before = await _reload(db_session, running.installation_id)
    started_at = before.operation_started_at

    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": running.pinned_digest},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["update"]["state"] == "up_to_date"
    row = await _reload(db_session, running.installation_id)
    # Nothing restarted: no operation began, so no runtime backs up or swaps anything.
    assert (row.operation, row.status) == ("install", "complete")
    assert row.operation_started_at == started_at
    assert "update" not in (row.external_resources or {})


async def test_backup_failure_report_reverts_the_pin_and_names_backup_failed(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)

    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _failed_update_report("backup", "pgbackrest diff backup failed: repository unreachable"),
    )
    assert reported.status_code == 200, reported.text

    row = await _reload(db_session, running.installation_id)
    assert row.status == "failed"
    assert row.pinned_digest == previous_digest, "a refused update must return the pin"
    assert row.version != NEWER_VERSION
    shown = (await web.get(_installation_path(running.installation_id))).json()
    assert shown["update"]["state"] == "aborted_backup_failed"
    assert shown["update"]["error_code"] == "backup_failed"
    # The previous digest keeps running: the installation is still projected, at that digest.
    assert await _projected_digest(db_session, running.agent_id) == previous_digest

    # Nothing to tear down; once the worker settles the job, the tenant can try again.
    assert await running.worker.run_once() == running.installation_id
    retry = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": NEWER_DIGEST},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["update"]["state"] == "in_progress"


async def test_post_update_assertion_failure_reverts_the_pin_and_names_rollback(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)

    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _failed_update_report(
            "post-update-assertion",
            "Postiz did not pass its post-update check; the pre-update backup was restored",
        ),
    )
    assert reported.status_code == 200, reported.text

    row = await _reload(db_session, running.installation_id)
    assert row.status == "failed"
    assert row.pinned_digest == previous_digest
    shown = (await web.get(_installation_path(running.installation_id))).json()
    assert shown["update"]["state"] == "failed"
    assert shown["update"]["error_code"] == "update_rolled_back"
    assert await _projected_digest(db_session, running.agent_id) == previous_digest
    # The rolled-back companion still serves its tools.
    deployed = await deployed_companion_environments(AuthContext(user=seed_user), db_session)
    assert deployed.get("postiz") == [running.agent_id]


async def test_update_deadline_reverts_the_pin(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )

    assert await worker.run_once() == running.installation_id

    row = await _reload(db_session, running.installation_id)
    assert row.status == "failed"
    assert row.pinned_digest == previous_digest
    shown = (await web.get(_installation_path(running.installation_id))).json()
    assert shown["update"]["state"] == "failed"
    assert shown["update"]["error_code"] == "runtime_did_not_report"


async def test_an_accepted_update_is_announced_only_while_it_runs(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """Plan 06-41 (CR-03): the runtime changes the app digest only on the announced transition.

    While the accepted update runs, the entry names exactly the digest it moves from and the pin
    it moves to. Once the worker fails it by deadline and the pin goes back, nothing is announced,
    so a host still running the newer images has no instruction to move back down.
    """
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)

    entry = await _projected_entry(db_session, running.agent_id)
    assert entry["artifact"]["sha256"] == NEWER_DIGEST
    assert entry.get("update") == {"fromDigest": previous_digest, "toDigest": NEWER_DIGEST}

    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id

    reverted = await _projected_entry(db_session, running.agent_id)
    assert reverted["artifact"]["sha256"] == previous_digest
    assert "update" in reverted
    assert reverted["update"] is None


async def test_a_running_install_announces_no_update(
    web, db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)

    entry = await _projected_entry(db_session, running.agent_id)

    assert entry["artifact"]["sha256"] == running.pinned_digest
    assert entry["update"] is None


async def test_a_completed_update_is_no_longer_announced(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, _previous = await _accepted(web, db_session, seed_user, monkeypatch)
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )
    assert reported.status_code == 200, reported.text
    assert await running.worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "complete"

    entry = await _projected_entry(db_session, running.agent_id)

    assert entry["artifact"]["sha256"] == NEWER_DIGEST
    assert entry["update"] is None


async def test_an_update_the_runtime_reported_failed_is_no_longer_announced(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _failed_update_report(
            "post-update-assertion",
            "Postiz did not pass its post-update check; the pre-update backup was restored",
        ),
    )
    assert reported.status_code == 200, reported.text

    entry = await _projected_entry(db_session, running.agent_id)

    # The pin went back, and nothing asks a host still on the newer images to move to it.
    assert entry["artifact"]["sha256"] == previous_digest
    assert entry["update"] is None


async def test_a_deferred_update_is_not_announced(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _complete_installation(db_session, seed_user)
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)
    deferred = await web.post(
        _update_path(running.installation_id),
        json={"decision": "defer", "digest": NEWER_DIGEST},
    )
    assert deferred.status_code == 200, deferred.text

    entry = await _projected_entry(db_session, running.agent_id)

    assert entry["artifact"]["sha256"] == running.pinned_digest
    assert entry["update"] is None


async def test_a_stale_update_record_is_not_announced(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """T-06-41-02: a record whose ``to_digest`` is not the pin announces nothing."""
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status) == ("update", "running")
    record = dict((row.external_resources or {})["update"])
    assert project_postiz_entry(row, catalog_entry("postiz"))["update"] == {
        "fromDigest": previous_digest,
        "toDigest": NEWER_DIGEST,
    }

    row.external_resources = {
        **(row.external_resources or {}),
        "update": {**record, "to_digest": LATER_DIGEST},
    }

    assert project_postiz_entry(row, catalog_entry("postiz"))["update"] is None


async def test_update_completion_runs_no_install_completion_steps(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """The hostname is already published and the line item already bills: never twice."""
    running, _previous = await _accepted(web, db_session, seed_user, monkeypatch)
    provisioned: list[str] = []

    async def provision(context: LifecycleStepContext) -> dict:
        provisioned.append(context.operation)
        return {}

    async def teardown(context: LifecycleStepContext, resources: dict) -> None:
        return None

    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        provisioning_steps=(),
        completion_steps=(
            ProvisioningStep(name="billing-start", provision=provision, teardown=teardown),
        ),
    )
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )
    assert reported.status_code == 200, reported.text
    assert await worker.run_once() == running.installation_id

    assert (await _reload(db_session, running.installation_id)).status == "complete"
    assert provisioned == []


# ---------------------------------------------------------------------------
# Plan 06-40 (CR-03): every 409 names the facts the runtime decides on.
# ---------------------------------------------------------------------------


async def test_report_refusals_name_the_operation_and_status(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """A 409 carries the operation and its status, so the runtime can tell done from refused.

    Only ``companion_install_terminal`` with status ``complete`` means "already recorded"; the
    same code with ``failed`` is a refusal the runtime must act on (roll an update back), and an
    operation mismatch names the operation the control plane is actually running.
    """
    running = await _complete_installation(db_session, seed_user)

    resent = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _ready_report(applied_digest=running.pinned_digest),
    )
    assert _conflict_code(resent) == "companion_install_terminal"
    assert resent.json()["detail"] == {
        "code": "companion_install_terminal",
        "message": "The install operation already ended (complete).",
        "operation": "install",
        "status": "complete",
    }

    other = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {"operation": "update", "phase": "progress", "step": "swap"},
    )
    assert _conflict_code(other) == "companion_report_operation_mismatch"
    assert other.json()["detail"] == {
        "code": "companion_report_operation_mismatch",
        "message": "This installation's current operation is install, not update.",
        "operation": "install",
    }

    # The tenant accepts an update and the runtime stops reporting past the deadline.
    _release(monkeypatch, NEWER_DIGEST, NEWER_VERSION)
    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": NEWER_DIGEST},
    )
    assert accepted.status_code == 200, accepted.text
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "failed"

    late = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )
    assert _conflict_code(late) == "companion_install_terminal"
    assert late.json()["detail"] == {
        "code": "companion_install_terminal",
        "message": "The update operation already ended (failed).",
        "operation": "update",
        "status": "failed",
    }


# ---------------------------------------------------------------------------
# Plan 06-40 (WR-01): a ready report must name the digest the installation is pinned to.
# ---------------------------------------------------------------------------


async def _log_line_count(db_session, installation_id: uuid.UUID) -> int:
    count = await db_session.scalar(
        select(func.count())
        .select_from(CompanionInstallLogLine)
        .where(CompanionInstallLogLine.installation_id == installation_id)
    )
    return int(count or 0)


async def test_a_ready_report_for_a_digest_other_than_the_pin_is_refused(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    before = await _reload(db_session, running.installation_id)
    snapshot = (before.status, before.step, before.applied_digest, before.next_attempt_at)
    lines_before = await _log_line_count(db_session, running.installation_id)

    # The update is to NEWER_DIGEST; a runtime that says it runs the previous digest is refused.
    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=previous_digest),
    )

    assert _conflict_code(reported) == "companion_ready_digest_mismatch"
    after = await _reload(db_session, running.installation_id)
    assert (after.status, after.step, after.applied_digest, after.next_attempt_at) == snapshot
    assert after.pinned_digest == NEWER_DIGEST
    assert await _log_line_count(db_session, running.installation_id) == lines_before


async def test_an_install_ready_for_the_pinned_digest_is_accepted(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)

    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _ready_report(applied_digest=running.pinned_digest),
    )

    assert reported.status_code == 200, reported.text
    row = await _reload(db_session, running.installation_id)
    assert row.applied_digest == running.pinned_digest
    assert row.step == "ready"


# ---------------------------------------------------------------------------
# Plan 06-44 (WR-01 in 06-REVIEW, G-06-1): a re-sent ready of an update the control plane already
# recorded is recognised as complete, never refused. The runtime rolls back on every refused update
# ready, so refusing it would restore the pre-update backup of a COMPLETED update.
# ---------------------------------------------------------------------------


async def _second_update_running(web, db_session, seed_user, monkeypatch):
    """Update A (to NEWER) completed, then the tenant accepted update B (to LATER): B runs."""
    running, _ = await _accepted(web, db_session, seed_user, monkeypatch)
    ready = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )
    assert ready.status_code == 200, ready.text
    assert await running.worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "complete"

    _release(monkeypatch, LATER_DIGEST, LATER_VERSION)
    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": LATER_DIGEST},
    )
    assert accepted.status_code == 200, accepted.text
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status) == ("update", "running")
    assert (row.pinned_digest, row.applied_digest) == (LATER_DIGEST, NEWER_DIGEST)
    return running


async def _job_snapshot(db_session, installation_id: uuid.UUID) -> tuple:
    row = await _reload(db_session, installation_id)
    return (
        row.operation,
        row.status,
        row.step,
        row.error,
        row.next_attempt_at,
        row.pinned_digest,
        row.applied_digest,
        await _log_line_count(db_session, installation_id),
    )


async def test_a_resent_ready_of_a_completed_update_is_recognised_not_refused(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)

    # The host never heard update A's answer and re-sends its ready after B was accepted.
    resent = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )

    assert _conflict_code(resent) == "companion_install_terminal"
    detail = resent.json()["detail"]
    assert detail["operation"] == "update"
    assert detail["status"] == "complete"
    # Nothing was written: B still runs toward LATER, and A's digest is still what runs.
    assert await _job_snapshot(db_session, running.installation_id) == before


async def test_a_resent_ready_after_the_later_update_deadline_failed_is_still_recognised(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """CR-01 (06-REVIEW, gap closure): the host stays offline past update B's deadline.

    The worker fails B and reverts the pin to A's digest, so the row is ``update``/``failed`` with
    pin and applied both NEWER. A's re-sent ready must still be answered ``complete``: refused as
    ``failed``, the host would restore A's pre-update backup and lose every write since.
    """
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    failed = await _reload(db_session, running.installation_id)
    assert (failed.operation, failed.status) == ("update", "failed")
    assert (failed.pinned_digest, failed.applied_digest) == (NEWER_DIGEST, NEWER_DIGEST)
    before = await _job_snapshot(db_session, running.installation_id)

    resent = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )

    assert _conflict_code(resent) == "companion_install_terminal"
    detail = resent.json()["detail"]
    assert detail["operation"] == "update"
    assert detail["status"] == "complete"
    assert await _job_snapshot(db_session, running.installation_id) == before


# WR-01 (06-REVIEW), G-06-1: only an update ready the control plane already recorded is recognised.
# Every refusal plan 06-40's host rollback depends on is unchanged.

THIRD_DIGEST = "c" * 64


async def test_a_ready_after_the_deadline_failed_the_update_is_still_refused(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    failed = await _reload(db_session, running.installation_id)
    # The deadline never recorded NEWER as applied: the pin went back to what still runs.
    assert (failed.status, failed.pinned_digest) == ("failed", previous_digest)
    assert failed.applied_digest == previous_digest
    before = await _job_snapshot(db_session, running.installation_id)

    late = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=NEWER_DIGEST),
    )

    assert _conflict_code(late) == "companion_install_terminal"
    assert late.json()["detail"]["status"] == "failed"
    assert await _job_snapshot(db_session, running.installation_id) == before


async def test_a_ready_for_neither_the_recorded_nor_the_pinned_digest_is_refused(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)

    foreign = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=THIRD_DIGEST),
    )

    assert _conflict_code(foreign) == "companion_ready_digest_mismatch"
    assert await _job_snapshot(db_session, running.installation_id) == before


async def test_the_ready_of_the_running_update_is_still_accepted(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)

    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _update_ready_report(applied_digest=LATER_DIGEST),
    )

    assert reported.status_code == 200, reported.text
    row = await _reload(db_session, running.installation_id)
    assert row.applied_digest == LATER_DIGEST
    assert row.step == "ready"
    assert row.status == "running"


async def test_an_install_operation_is_never_recognised_as_a_completed_update(
    db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    # Every other condition of the recognition holds: the ready's digest is the recorded applied
    # digest (even as an update's), and it differs from the pin. Only the operation is install.
    row = await _reload(db_session, running.installation_id)
    row.applied_digest = THIRD_DIGEST
    row.external_resources = {
        **(row.external_resources or {}),
        "update_applied_digest": THIRD_DIGEST,
    }
    await db_session.commit()
    assert row.pinned_digest != THIRD_DIGEST
    before = await _job_snapshot(db_session, running.installation_id)

    reported = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _ready_report(applied_digest=THIRD_DIGEST),
    )

    assert _conflict_code(reported) == "companion_ready_digest_mismatch"
    assert await _job_snapshot(db_session, running.installation_id) == before


# ---------------------------------------------------------------------------
# Plan 06-50 (G-06-4, 06-UAT test 1, D-28): an update report carries the identity of the update it
# was made for (``to_digest``), and a report naming another update than the one in flight is
# refused and writes nothing. The host queues an update's ``failed`` report across an outage; if
# the outage outlasts the reporting deadline, the worker fails that update and the tenant may
# accept a later one before the host returns, so the queued report must never land on the later
# update.
# ---------------------------------------------------------------------------


async def _stale_update_running(web, db_session, seed_user, monkeypatch):
    """Update N (to NEWER) deadline-failed while the host was away; update M (to LATER) runs."""
    running, previous_digest = await _accepted(web, db_session, seed_user, monkeypatch)
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    failed = await _reload(db_session, running.installation_id)
    assert (failed.status, failed.pinned_digest) == ("failed", previous_digest)

    _release(monkeypatch, LATER_DIGEST, LATER_VERSION)
    accepted = await web.post(
        _update_path(running.installation_id),
        json={"decision": "accept", "digest": LATER_DIGEST},
    )
    assert accepted.status_code == 200, accepted.text
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status, row.pinned_digest) == ("update", "running", LATER_DIGEST)
    assert row.external_resources["update"]["to_digest"] == LATER_DIGEST
    return running, previous_digest


async def _update_record_of(db_session, installation_id: uuid.UUID) -> dict:
    return dict((await _reload(db_session, installation_id)).external_resources["update"])


async def test_a_stale_failed_report_of_an_earlier_update_is_refused(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, _ = await _stale_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)
    record_before = await _update_record_of(db_session, running.installation_id)

    # N's failed report, exactly as the host queued it before the outage, now naming N.
    stale = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {
            **_failed_update_report(
                "backup", "pgbackrest diff backup failed; the update was not applied"
            ),
            "to_digest": NEWER_DIGEST,
        },
    )

    assert _conflict_code(stale) == "companion_update_identity_mismatch"
    detail = stale.json()["detail"]
    assert detail["operation"] == "update"
    assert detail["status"] == "running"
    # Nothing was written: M still runs toward LATER with its own record and deadline.
    assert await _job_snapshot(db_session, running.installation_id) == before
    record_after = await _update_record_of(db_session, running.installation_id)
    assert record_after == record_before
    assert record_after["to_digest"] == LATER_DIGEST
    assert "error_code" not in record_after
    assert "failed_at" not in record_after
    shown = (await web.get(_installation_path(running.installation_id))).json()
    assert (shown["operation"], shown["status"]) == ("update", "running")
    assert shown["pinned_digest"] == LATER_DIGEST
    assert shown["update"]["state"] == "in_progress"
    assert shown["update"]["error_code"] is None


# G-06-4 (06-UAT test 1): the identity edges. Only the update a report names can be moved by it;
# a matching report is processed exactly as before. Two decisions are recorded here and in the
# ``CompanionInstallReport`` docstring: a report WITHOUT ``to_digest`` (an older CLI) is applied to
# the update in flight unchecked, and ``to_digest`` alone is the update identity (no acceptance
# id; a re-acceptance of the same digest is a documented residual, not a guarantee, so it is not
# asserted as a behaviour). CR-01 holds: a re-sent ready the control plane recorded is still
# answered complete when it carries its own update's ``to_digest``.


def _stale_line() -> list[dict]:
    return [{"stream": "stdout", "line": "update N: swapping the app digest"}]


async def test_a_stale_progress_report_of_an_earlier_update_moves_nothing(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, _ = await _stale_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)
    record_before = await _update_record_of(db_session, running.installation_id)

    stale = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {
            "operation": "update",
            "phase": "progress",
            "step": "swap",
            "lines": _stale_line(),
            "to_digest": NEWER_DIGEST,
        },
    )

    assert _conflict_code(stale) == "companion_update_identity_mismatch"
    # M's step and reporting deadline did not move, and no log line was stored.
    assert await _job_snapshot(db_session, running.installation_id) == before
    assert await _update_record_of(db_session, running.installation_id) == record_before


async def test_a_stale_ready_of_an_unrecorded_update_is_refused_as_another_update(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """N never recorded a ready, so the refusal stands and the host rolls N back."""
    running, _ = await _stale_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)
    record_before = await _update_record_of(db_session, running.installation_id)

    stale = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {**_update_ready_report(applied_digest=NEWER_DIGEST), "to_digest": NEWER_DIGEST},
    )

    assert _conflict_code(stale) == "companion_update_identity_mismatch"
    assert stale.json()["detail"]["status"] == "running"
    assert await _job_snapshot(db_session, running.installation_id) == before
    assert await _update_record_of(db_session, running.installation_id) == record_before


@pytest.mark.parametrize("outcome", ["failed", "ready"])
async def test_reports_naming_the_update_in_flight_are_processed(
    outcome, web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running, previous_digest = await _stale_update_running(web, db_session, seed_user, monkeypatch)
    deadline_before = (await _reload(db_session, running.installation_id)).next_attempt_at

    progress = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {"operation": "update", "phase": "progress", "step": "swap", "to_digest": LATER_DIGEST},
    )
    assert progress.status_code == 200, progress.text
    row = await _reload(db_session, running.installation_id)
    assert (row.status, row.step) == ("running", "swap")
    assert row.next_attempt_at is not None and deadline_before is not None
    assert row.next_attempt_at >= deadline_before

    if outcome == "failed":
        failed = await _post_report(
            db_session,
            running.runtime_key,
            running.installation_id,
            {
                **_failed_update_report("backup", "pgbackrest diff backup failed"),
                "to_digest": LATER_DIGEST,
            },
        )
        assert failed.status_code == 200, failed.text
        row = await _reload(db_session, running.installation_id)
        assert row.status == "failed"
        # M's pin goes back to M's own from_digest.
        record = await _update_record_of(db_session, running.installation_id)
        assert record["from_digest"] == previous_digest
        assert row.pinned_digest == previous_digest
        assert record["error_code"] == "backup_failed"
        shown = (await web.get(_installation_path(running.installation_id))).json()
        assert shown["update"]["error_code"] == "backup_failed"
    else:
        ready = await _post_report(
            db_session,
            running.runtime_key,
            running.installation_id,
            {**_update_ready_report(applied_digest=LATER_DIGEST), "to_digest": LATER_DIGEST},
        )
        assert ready.status_code == 200, ready.text
        row = await _reload(db_session, running.installation_id)
        assert row.applied_digest == LATER_DIGEST
        assert (row.status, row.step) == ("running", "ready")


async def test_a_report_without_to_digest_is_applied_to_the_update_in_flight(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """The recorded older-CLI decision: a report without ``to_digest`` is not checked.

    CLIs before plan 06-52 never send ``to_digest``; refusing their reports would fail every
    update they run at its reporting deadline. So N's stale failed report, sent WITHOUT the field,
    still fails M and reverts M's pin, exactly as before plan 06-50. The residual lasts until the
    host runs the plan 06-52 CLI, which sends ``to_digest`` on every update report.
    """
    running, previous_digest = await _stale_update_running(web, db_session, seed_user, monkeypatch)

    legacy = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        _failed_update_report("backup", "pgbackrest diff backup failed"),
    )

    assert legacy.status_code == 200, legacy.text
    row = await _reload(db_session, running.installation_id)
    assert (row.operation, row.status) == ("update", "failed")
    assert row.pinned_digest == previous_digest
    record = await _update_record_of(db_session, running.installation_id)
    assert record["to_digest"] == LATER_DIGEST
    assert record["error_code"] == "backup_failed"


async def test_a_resent_ready_naming_its_update_is_still_recognised_while_a_later_update_runs(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)
    before = await _job_snapshot(db_session, running.installation_id)

    # A's re-sent ready names A (NEWER), not B (LATER): the recognition answers before identity.
    resent = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {**_update_ready_report(applied_digest=NEWER_DIGEST), "to_digest": NEWER_DIGEST},
    )

    assert _conflict_code(resent) == "companion_install_terminal"
    assert resent.json()["detail"]["status"] == "complete"
    assert await _job_snapshot(db_session, running.installation_id) == before


async def test_a_resent_ready_naming_its_update_is_still_recognised_after_the_later_update_deadline_failed(  # noqa: E501
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    running = await _second_update_running(web, db_session, seed_user, monkeypatch)
    later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 60)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "failed"
    before = await _job_snapshot(db_session, running.installation_id)

    resent = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {**_update_ready_report(applied_digest=NEWER_DIGEST), "to_digest": NEWER_DIGEST},
    )

    assert _conflict_code(resent) == "companion_install_terminal"
    assert resent.json()["detail"]["status"] == "complete"
    assert await _job_snapshot(db_session, running.installation_id) == before


async def test_the_identity_answer_is_the_same_whatever_the_update_s_status(
    web, db_session, seed_user, companion_zone, monkeypatch
) -> None:
    """M deadline-failed too: N's report is still refused as another update's, not as terminal."""
    running, _ = await _stale_update_running(web, db_session, seed_user, monkeypatch)
    much_later = datetime.now(UTC) + timedelta(seconds=REPORT_DEADLINE_SECONDS + 120)
    worker = CompanionLifecycleWorker(
        _sessionmaker(db_session),
        clock=lambda: much_later,
        provisioning_steps=(),
        completion_steps=(),
    )
    assert await worker.run_once() == running.installation_id
    assert (await _reload(db_session, running.installation_id)).status == "failed"
    before = await _job_snapshot(db_session, running.installation_id)
    record_before = await _update_record_of(db_session, running.installation_id)

    stale = await _post_report(
        db_session,
        running.runtime_key,
        running.installation_id,
        {
            **_failed_update_report("backup", "pgbackrest diff backup failed"),
            "to_digest": NEWER_DIGEST,
        },
    )

    assert _conflict_code(stale) == "companion_update_identity_mismatch"
    assert stale.json()["detail"] == {
        "code": "companion_update_identity_mismatch",
        "message": "This report is for another update than the one in flight.",
        "operation": "update",
        "status": "failed",
    }
    assert await _job_snapshot(db_session, running.installation_id) == before
    assert await _update_record_of(db_session, running.installation_id) == record_before


_A_BACKUP = {
    "ok": True,
    "started_at": "2026-09-27T20:00:00Z",
    "completed_at": "2026-09-27T20:01:00Z",
}


@pytest.mark.parametrize(
    "body",
    [
        # An install job report has no update identity.
        {"operation": "install", "phase": "progress", "step": "pull", "to_digest": NEWER_DIGEST},
        {"operation": "install", "phase": "failed", "error": "no", "to_digest": NEWER_DIGEST},
        {"operation": "uninstall", "phase": "progress", "to_digest": NEWER_DIGEST},
        # The non-job phases never carry it, even on an update.
        {"operation": "update", "phase": "backup", "backup": _A_BACKUP, "to_digest": NEWER_DIGEST},
        {"operation": "update", "phase": "removed", "to_digest": NEWER_DIGEST},
        {
            "operation": "update",
            "phase": "condition",
            "condition": {"code": "schema_digest_mismatch", "state": "cleared"},
            "to_digest": NEWER_DIGEST,
        },
        # Malformed: not 64 lowercase hex.
        {"operation": "update", "phase": "progress", "to_digest": "E" * 64},
        {"operation": "update", "phase": "progress", "to_digest": "e" * 63},
        {"operation": "update", "phase": "progress", "to_digest": 64},
    ],
    ids=[
        "install-progress",
        "install-failed",
        "uninstall-progress",
        "update-backup",
        "update-removed",
        "update-condition",
        "uppercase",
        "short",
        "not-a-string",
    ],
)
async def test_to_digest_is_refused_outside_an_update_job_report(
    body, db_session, seed_user, companion_zone
) -> None:
    running = await _running_installation(db_session, seed_user)
    before = await _job_snapshot(db_session, running.installation_id)

    reported = await _post_report(db_session, running.runtime_key, running.installation_id, body)

    assert reported.status_code == 422, reported.text
    assert "to_digest" in reported.text
    assert await _job_snapshot(db_session, running.installation_id) == before
