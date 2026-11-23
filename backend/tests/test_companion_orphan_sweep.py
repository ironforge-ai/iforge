"""The backend's orphan sweep stops hosted companion lines no serving installation records.

06.2 D-17 / D-18 / SC-4. A line the hosted service started whose response the backend never saw
(``BILLING_START_MAX_ATTEMPTS`` exhausted) has no ``billing_line_item_id`` behind it, so the
failed install's teardown finds nothing to stop and the line keeps billing. Only a list-and-compare
sweep finds it. Every test drives the REAL ``/v2`` mock (``scripts/mock_deploy_api.py``) in process
through ``route_billing_to_mock``, and the worker runs inside the test's own transaction
(``_sessionmaker``), so its commits stay test-local.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_installation import CompanionInstallation
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.services import companion_billing
from app.services.companion_billing import (
    CompanionBillingUnavailableError,
    list_companion_line_items,
)
from app.services.companion_orphan_sweep import (
    CompanionOrphanSweepResult,
    CompanionOrphanSweepWorker,
)
from scripts import mock_deploy_api
from tests.companion_billing_stub import BillingMock, quote_for, route_billing_to_mock
from tests.conftest import create_env_with_project
from tests.test_companion_install_job import _sessionmaker

LINE_ITEMS_PATH = "/v2/companions/line-items"
ORPHAN_STOPPED = "companion.billing_orphan_stopped"
# Older than the sweep's 15-minute in-flight guard (D-18).
PAST_GRACE = timedelta(minutes=20)


async def _installation(
    db_session: AsyncSession,
    user: User,
    *,
    status: str = "complete",
    operation: str = "install",
    billing_line_item_id: str | None = None,
    removed: bool = False,
) -> CompanionInstallation:
    """One installation row for ``user``; nothing about it is billed unless a line id is given."""
    env = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"orphan-{uuid.uuid4().hex[:8]}",
        machine_name="Orphan sweep host",
    )
    label = f"postiz-{uuid.uuid4().hex[:12]}"
    now = datetime.now(UTC)
    row = CompanionInstallation(
        user_id=user.id,
        environment_id=env.id,
        companion_key="postiz",
        isolation_tier="standard",
        operation=operation,
        status=status,
        version="v2.24.0",
        pinned_digest=hashlib.sha256(label.encode()).hexdigest(),
        hostname_label=label,
        hostname=f"{label}.companions.example",
        owner_email="owner@companions.example",
        quote_id="quote-orphan-sweep",
        quoted_monthly_usd_micros=9_000_000,
        billing_line_item_id=billing_line_item_id,
        billing_started_at=now if billing_line_item_id else None,
        completed_at=now if status == "complete" else None,
        removed_at=now if removed else None,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _hosted_line(
    monkeypatch: pytest.MonkeyPatch,
    owner: str,
    installation_id: uuid.UUID | str,
    *,
    age: timedelta = PAST_GRACE,
) -> str:
    """Start a line at the mock for ``installation_id`` under ``owner`` and age it by ``age``."""
    quote_id = await quote_for(monkeypatch, owner)
    line = await companion_billing.start_companion_line_item(
        installation_id=installation_id, quote_id=quote_id, owner_user_id=owner
    )
    record = mock_deploy_api.COMPANION_LINE_ITEMS[line.line_item_id]
    record["started_at"] = record["started_at"] - age
    return line.line_item_id


def _sweep(db_session: AsyncSession) -> CompanionOrphanSweepWorker:
    return CompanionOrphanSweepWorker(_sessionmaker(db_session))


async def _orphan_events(
    db_session: AsyncSession, line_item_id: str
) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db_session.scalars(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.action == ORPHAN_STOPPED,
                    ControlPlaneAuditEvent.resource_id == line_item_id,
                )
            )
        ).all()
    )


def _record_stop_keys(billing: BillingMock) -> list[str]:
    """Record the Idempotency-Key of every stop the backend sends, passing each through."""
    keys: list[str] = []

    def observe(request: httpx.Request) -> int | None:
        if request.method == "DELETE":
            keys.append(request.headers.get("Idempotency-Key", ""))
        return None

    billing.fail = observe
    return keys


# ---------------------------------------------------------------------------
# Task 1: the tracer -- a line with no installation behind it is found and stopped.
# ---------------------------------------------------------------------------


async def test_a_hosted_line_with_no_installation_behind_it_is_stopped_by_the_sweep(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    # A removed installation is enough for the owner to be enumerated.
    await _installation(
        db_session, seed_user, status="complete", operation="uninstall", removed=True
    )
    # The primary orphan case: the hosted service started a line whose response was lost, so no
    # row records it -- here, no row exists for its installation id at all.
    orphan_installation = uuid.uuid4()
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, orphan_installation)
    stop_keys = _record_stop_keys(billing)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 1
    assert f"GET {LINE_ITEMS_PATH}" in billing.calls
    assert f"DELETE {LINE_ITEMS_PATH}/{line_item_id}" in billing.calls
    assert stop_keys == [f"companion-line-stop-{orphan_installation}"]
    assert mock_deploy_api.COMPANION_LINE_ITEMS[line_item_id]["stopped_at"] is not None
    (event,) = await _orphan_events(db_session, line_item_id)
    assert event.details["reason"] == "no_installation"
    assert event.details["installation_id"] == str(orphan_installation)
    assert event.target_user_id == seed_user.id
    assert event.actor_type == "system"


# ---------------------------------------------------------------------------
# The list client: parsed records, fail closed, follows the cursor.
# ---------------------------------------------------------------------------


def _list_item(line_item_id: str, *, stopped_at: str | None = None) -> dict:
    return {
        "line_item_id": line_item_id,
        "installation_id": str(uuid.uuid4()),
        "owner_user_id": "user_owner",
        "quote_id": "cq_list",
        "monthly_price_usd_micros": 9_000_000,
        "started_at": "2026-09-01T10:00:00Z",
        "stopped_at": stopped_at,
    }


def _route_list_to(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    monkeypatch.setattr(settings, "deploy_api_url", "http://v2.orphan-sweep.test")
    monkeypatch.setattr(settings, "deploy_api_key", "orphan-sweep-test-key")
    monkeypatch.setattr(companion_billing, "_transport_override", httpx.MockTransport(record))
    return seen


async def test_listing_lines_parses_each_record_with_aware_utc_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _list_item("cli_one")
    seen = _route_list_to(
        monkeypatch,
        lambda request: httpx.Response(200, json={"items": [item], "next_cursor": None}),
    )

    (record,) = await list_companion_line_items(owner_user_id="user_owner")

    assert record.line_item_id == "cli_one"
    assert record.installation_id == item["installation_id"]
    assert record.owner_user_id == "user_owner"
    assert record.quote_id == "cq_list"
    assert record.monthly_price_usd_micros == 9_000_000
    assert record.started_at == datetime(2026, 9, 1, 10, tzinfo=UTC)
    assert record.started_at.tzinfo is not None
    assert record.stopped_at is None
    (request,) = seen
    assert request.url.path == LINE_ITEMS_PATH
    assert request.url.params["owner_user_id"] == "user_owner"
    assert request.url.params["state"] == "active"
    assert request.headers["Authorization"] == "Bearer orphan-sweep-test-key"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, json={"detail": "down"}),
        httpx.Response(200, json={"items": "not-a-list", "next_cursor": None}),
        httpx.Response(200, json={"items": [{"line_item_id": "cli_bad"}], "next_cursor": None}),
        httpx.Response(200, text="not json"),
    ],
    ids=["non-200", "items-not-a-list", "item-missing-fields", "not-json"],
)
async def test_listing_lines_fails_closed_on_an_unreadable_answer(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    _route_list_to(monkeypatch, lambda request: response)

    with pytest.raises(CompanionBillingUnavailableError):
        await list_companion_line_items(owner_user_id="user_owner")


async def test_listing_lines_follows_next_cursor_to_the_last_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pages(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("cursor") == "page-2":
            return httpx.Response(200, json={"items": [_list_item("cli_two")], "next_cursor": None})
        return httpx.Response(200, json={"items": [_list_item("cli_one")], "next_cursor": "page-2"})

    seen = _route_list_to(monkeypatch, pages)

    records = await list_companion_line_items(owner_user_id="user_owner")

    assert [record.line_item_id for record in records] == ["cli_one", "cli_two"]
    assert [request.url.params.get("cursor") for request in seen] == [None, "page-2"]


# ---------------------------------------------------------------------------
# Task 2: the decision matrix -- every keep case pinned, every stop case reasoned (D-17, D-18).
# ---------------------------------------------------------------------------


async def _record_line(db_session: AsyncSession, row: CompanionInstallation, line_id: str) -> None:
    """What ``_start_billing`` writes once a line starts: the row records the line."""
    row.billing_line_item_id = line_id
    row.billing_started_at = datetime.now(UTC)
    await db_session.commit()


def _extra_line(owner: str, installation_id: uuid.UUID, *, age: timedelta = PAST_GRACE) -> str:
    """A second hosted line for an installation, as only a lost-and-retried start could leave.

    The start key is derived from the installation id, so the client cannot make a second line
    for one installation; the hosted record is written directly instead.
    """
    line_item_id = f"cli_{uuid.uuid4().hex}"
    mock_deploy_api.COMPANION_LINE_ITEMS[line_item_id] = {
        "line_item_id": line_item_id,
        "quote_id": "cq_extra",
        "installation_id": str(installation_id),
        "owner_user_id": owner,
        "monthly_price_usd_micros": 9_000_000,
        "started_at": datetime.now(UTC) - age,
        "stopped_at": None,
    }
    return line_item_id


async def _second_owner(db_session: AsyncSession) -> User:
    ref = uuid.uuid4().hex[:12]
    other = User(clerk_id=f"test_orphan_{ref}", email=f"orphan-{ref}@iforge.local", name="Other")
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


def _stops(billing: BillingMock, line_item_id: str) -> int:
    return billing.calls.count(f"DELETE {LINE_ITEMS_PATH}/{line_item_id}")


def _active(line_item_id: str) -> bool:
    return mock_deploy_api.COMPANION_LINE_ITEMS[line_item_id]["stopped_at"] is None


async def test_a_line_younger_than_the_in_flight_guard_is_kept(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _installation(db_session, seed_user, operation="uninstall", removed=True)
    # An install still in flight: its line exists, its start response is not recorded yet.
    line_item_id = await _hosted_line(
        monkeypatch, seed_user.clerk_id, uuid.uuid4(), age=timedelta(minutes=10)
    )

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 0
    assert _stops(billing, line_item_id) == 0
    assert _active(line_item_id)
    assert await _orphan_events(db_session, line_item_id) == []


async def test_a_serving_installation_recording_its_line_is_kept(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    row = await _installation(db_session, seed_user, status="complete", operation="install")
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, row.id)
    await _record_line(db_session, row, line_item_id)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 0
    assert _stops(billing, line_item_id) == 0
    assert _active(line_item_id)
    assert await _orphan_events(db_session, line_item_id) == []


async def test_an_installation_inside_an_update_recording_its_line_is_kept(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    # ``running`` is the lifecycle job in progress, not "not serving": the previous digest keeps
    # serving through an update (RESEARCH Pitfall 13).
    row = await _installation(db_session, seed_user, status="running", operation="update")
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, row.id)
    await _record_line(db_session, row, line_item_id)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 0
    assert _stops(billing, line_item_id) == 0
    assert _active(line_item_id)


async def test_an_uninstalling_installation_has_its_recorded_line_stopped(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    row = await _installation(db_session, seed_user, status="running", operation="uninstall")
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, row.id)
    await _record_line(db_session, row, line_item_id)
    stop_keys = _record_stop_keys(billing)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 1
    assert stop_keys == [f"companion-line-stop-{row.id}"]
    assert not _active(line_item_id)
    (event,) = await _orphan_events(db_session, line_item_id)
    assert event.details["reason"] == "not_serving"
    await db_session.refresh(row)
    assert row.billing_stopped_at is not None


async def test_a_failed_install_with_an_unrecorded_line_has_it_stopped(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    # The primary orphan on a real row: the start response was lost, the step failed for good,
    # and the row records no line.
    row = await _installation(db_session, seed_user, status="failed", operation="install")
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, row.id)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 1
    assert _stops(billing, line_item_id) == 1
    assert not _active(line_item_id)
    (event,) = await _orphan_events(db_session, line_item_id)
    assert event.details["reason"] == "not_serving"
    await db_session.refresh(row)
    # The row never recorded this line, so the sweep leaves its billing columns alone.
    assert row.billing_line_item_id is None
    assert row.billing_stopped_at is None


async def test_a_line_a_serving_installation_does_not_record_is_stopped_and_its_own_kept(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    row = await _installation(db_session, seed_user, status="complete", operation="install")
    recorded = await _hosted_line(monkeypatch, seed_user.clerk_id, row.id)
    await _record_line(db_session, row, recorded)
    extra = _extra_line(seed_user.clerk_id, row.id)

    result = await _sweep(db_session).run_once()

    assert result.lines_stopped == 1
    assert _stops(billing, extra) == 1
    assert not _active(extra)
    (event,) = await _orphan_events(db_session, extra)
    assert event.details["reason"] == "line_not_recorded"
    assert _stops(billing, recorded) == 0
    assert _active(recorded)
    await db_session.refresh(row)
    assert row.billing_line_item_id == recorded
    assert row.billing_stopped_at is None


async def test_a_line_naming_another_tenants_installation_counts_as_having_none(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _installation(db_session, seed_user, operation="uninstall", removed=True)
    other = await _second_owner(db_session)
    theirs = await _installation(db_session, other, status="complete", operation="install")
    # A line listed under this owner that names the other tenant's serving installation.
    line_item_id = _extra_line(seed_user.clerk_id, theirs.id)

    await _sweep(db_session).run_once()

    assert _stops(billing, line_item_id) == 1
    (event,) = await _orphan_events(db_session, line_item_id)
    assert event.details["reason"] == "no_installation"
    assert event.target_user_id == seed_user.id


async def test_one_owners_list_outage_does_not_stop_the_sweep_for_the_next(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    other = await _second_owner(db_session)
    await _installation(db_session, seed_user, operation="uninstall", removed=True)
    await _installation(db_session, other, operation="uninstall", removed=True)
    unreachable = await _hosted_line(monkeypatch, seed_user.clerk_id, uuid.uuid4())
    orphan = await _hosted_line(monkeypatch, other.clerk_id, uuid.uuid4())

    def outage(request: httpx.Request) -> int | None:
        if (
            request.method == "GET"
            and request.url.params.get("owner_user_id") == seed_user.clerk_id
        ):
            return 503
        return None

    billing.fail = outage

    result = await _sweep(db_session).run_once()

    assert result.owners_failed == 1
    assert _stops(billing, orphan) == 1
    assert not _active(orphan)
    assert _stops(billing, unreachable) == 0
    assert _active(unreachable)


async def test_an_unconfigured_v2_makes_the_sweep_a_no_op(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _installation(db_session, seed_user, operation="uninstall", removed=True)
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, uuid.uuid4())
    monkeypatch.setattr(settings, "deploy_api_url", "")
    billing.calls.clear()

    result = await _sweep(db_session).run_once()

    assert result == CompanionOrphanSweepResult()
    assert billing.calls == []
    assert _active(line_item_id)


async def test_a_second_sweep_after_a_stop_issues_no_second_stop(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _installation(db_session, seed_user, operation="uninstall", removed=True)
    line_item_id = await _hosted_line(monkeypatch, seed_user.clerk_id, uuid.uuid4())
    sweep = _sweep(db_session)

    first = await sweep.run_once()
    second = await sweep.run_once()

    assert first.lines_stopped == 1
    assert second.lines_stopped == 0
    assert _stops(billing, line_item_id) == 1
    assert len(await _orphan_events(db_session, line_item_id)) == 1
