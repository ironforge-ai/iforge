"""The companion billing reconciliation compares hosted lines with local billing (INT-4).

Every test drives the REAL ``/v2`` mock (``scripts/mock_deploy_api.py``) in process through
``route_billing_to_mock``; a hosted line is written straight into the mock's store so its window
can be set to the second. The worker runs from a fixed clock inside the test's own transaction
(``_sessionmaker``), so its commits stay test-local. Local compute is accrued by the metering
worker's own ``accrue_compute``, so the comparison is against what metering really records.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.billing_reconcile_finding import BillingReconcileFinding
from app.models.companion_installation import CompanionInstallation
from app.models.usage_event import USAGE_DIMENSION_COMPUTE, UsageEvent
from app.models.user import User
from app.services.companion_billing import CompanionLineItemRecord, list_companion_line_items
from app.services.companion_billing_reconcile import (
    STOP_RECONCILED_ACTION,
    CompanionBillingReconcileResult,
    CompanionBillingReconcileWorker,
    amount_drift,
    window_mismatch,
)
from app.services.companion_metering import HOUR, accrue_compute, compute_hour_cost
from app.services.metrics import companion_billing_reconcile_mismatches
from scripts import mock_deploy_api
from tests.companion_billing_stub import route_billing_to_mock
from tests.conftest import create_env_with_project
from tests.test_companion_install_job import _sessionmaker
from tests.test_vault_key_rotation import _ADMIN_AUTH, _admin_client

MONTHLY = 9_000_000
# A Tuesday noon, on the hour: every window below is measured from it.
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
STARTED = NOW - timedelta(hours=72)
RECONCILE_PATH = "/v1/admin/companions/billing-reconcile"
FINDINGS_PATH = "/v1/admin/companions/billing-reconcile/findings"


def _counter(kind: str) -> float:
    return companion_billing_reconcile_mismatches.labels(kind=kind)._value.get()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _worker(
    db_session: AsyncSession, clock: _Clock | None = None
) -> CompanionBillingReconcileWorker:
    return CompanionBillingReconcileWorker(_sessionmaker(db_session), clock=clock or _Clock(NOW))


async def _installation(
    db_session: AsyncSession,
    user: User,
    *,
    line_item_id: str | None,
    started: datetime | None = STARTED,
    stopped: datetime | None = None,
    monthly: int | None = MONTHLY,
    removed: bool = False,
) -> CompanionInstallation:
    env = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"reconcile-{uuid.uuid4().hex[:8]}",
        machine_name="Billing reconcile host",
    )
    label = f"postiz-{uuid.uuid4().hex[:12]}"
    row = CompanionInstallation(
        user_id=user.id,
        environment_id=env.id,
        companion_key="postiz",
        isolation_tier="standard",
        operation="install",
        status="complete",
        version="v2.24.0",
        pinned_digest=hashlib.sha256(label.encode()).hexdigest(),
        hostname_label=label,
        hostname=f"{label}.companions.example",
        owner_email="owner@companions.example",
        quote_id="quote-billing-reconcile",
        quoted_monthly_usd_micros=monthly,
        billing_line_item_id=line_item_id,
        billing_started_at=started if line_item_id else None,
        billing_stopped_at=stopped,
        completed_at=STARTED,
        removed_at=NOW if removed else None,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _hosted_line(
    owner: str,
    installation_id: uuid.UUID | str,
    *,
    started: datetime = STARTED,
    stopped: datetime | None = None,
    monthly: int = MONTHLY,
    funding_source: str = "wallet",
    line_item_id: str | None = None,
) -> str:
    line_item_id = line_item_id or f"cli_{uuid.uuid4().hex}"
    mock_deploy_api.COMPANION_LINE_ITEMS[line_item_id] = {
        "line_item_id": line_item_id,
        "quote_id": "quote-billing-reconcile",
        "installation_id": str(installation_id),
        "owner_user_id": owner,
        "monthly_price_usd_micros": monthly,
        "started_at": started,
        "stopped_at": stopped,
        "funding_source": funding_source,
    }
    return line_item_id


async def _billed(
    db_session: AsyncSession,
    user: User,
    *,
    hosted_started: datetime = STARTED,
    hosted_stopped: datetime | None = None,
    local_started: datetime = STARTED,
    local_stopped: datetime | None = None,
    hosted_monthly: int = MONTHLY,
    local_monthly: int | None = MONTHLY,
    accrue: bool = True,
) -> tuple[CompanionInstallation, str]:
    """One installation billed by one hosted line, with every closed local hour accrued."""
    line_item_id = f"cli_{uuid.uuid4().hex}"
    row = await _installation(
        db_session,
        user,
        line_item_id=line_item_id,
        started=local_started,
        stopped=local_stopped,
        monthly=local_monthly,
    )
    _hosted_line(
        user.clerk_id,
        row.id,
        started=hosted_started,
        stopped=hosted_stopped,
        monthly=hosted_monthly,
        line_item_id=line_item_id,
    )
    if accrue:
        hour = local_started.replace(minute=0, second=0, microsecond=0)
        while hour + HOUR <= NOW:
            await accrue_compute(db_session, row, hour)
            hour += HOUR
        await db_session.commit()
    return row, line_item_id


async def _findings(db_session: AsyncSession, line_item_id: str) -> list[BillingReconcileFinding]:
    return list(
        (
            await db_session.scalars(
                select(BillingReconcileFinding)
                .where(BillingReconcileFinding.line_item_id == line_item_id)
                .order_by(BillingReconcileFinding.kind)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


async def _kinds(db_session: AsyncSession, line_item_id: str) -> dict[str, bool]:
    """kind -> open, for every finding of the line."""
    return {
        finding.kind: finding.resolved_at is None
        for finding in await _findings(db_session, line_item_id)
    }


def _record(**overrides: Any) -> CompanionLineItemRecord:
    values: dict[str, Any] = {
        "line_item_id": "cli_pure",
        "installation_id": str(uuid.uuid4()),
        "owner_user_id": "user_pure",
        "quote_id": "cq_pure",
        "monthly_price_usd_micros": MONTHLY,
        "started_at": STARTED,
        "stopped_at": None,
        "funding_source": "wallet",
        "charged_cents": None,
        "credited_cents": None,
    }
    values.update(overrides)
    return CompanionLineItemRecord(**values)


# ---------------------------------------------------------------------------
# The list read and the skip.
# ---------------------------------------------------------------------------


async def test_the_reconcile_lists_every_line_since_the_metering_lookback(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _billed(db_session, seed_user)
    seen: list[httpx.Request] = []

    def observe(request: httpx.Request) -> int | None:
        seen.append(request)
        return None

    billing.fail = observe

    result = await _worker(db_session).run_once()

    assert result.lines_seen == 1
    (request,) = [
        request for request in seen if request.url.params.get("owner_user_id") == seed_user.clerk_id
    ]
    assert request.method == "GET"
    assert request.url.params["state"] == "all"
    assert request.url.params["since"] == "2026-09-15T12:00:00Z"


async def test_a_consistent_line_yields_no_finding(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(db_session, seed_user)

    result = await _worker(db_session).run_once()

    assert (result.lines_seen, result.mismatches, result.corrections) == (1, 0, 0)
    assert result.owners_scanned >= 1
    assert await _findings(db_session, line_item_id) == []


async def test_an_unconfigured_v2_skips_the_pass_and_calls_nothing(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    await _billed(db_session, seed_user, local_monthly=1)
    monkeypatch.setattr(settings, "deploy_api_url", "")

    result = await _worker(db_session).run_once()

    assert result == CompanionBillingReconcileResult(skipped=True)
    assert billing.calls == []


async def test_a_v2_outage_for_an_owner_is_counted_and_records_nothing(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    billing = route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(db_session, seed_user, local_monthly=1)
    billing.fail = lambda request: 503

    result = await _worker(db_session).run_once()

    assert result.owners_failed == result.owners_scanned >= 1
    assert result.mismatches == 0
    assert await _findings(db_session, line_item_id) == []


def test_the_line_record_reads_a_list_row_without_the_reconcile_fields() -> None:
    from app.services.companion_billing import _parse_line_item_record

    record = _parse_line_item_record(
        {
            "line_item_id": "cli_old",
            "installation_id": str(uuid.uuid4()),
            "owner_user_id": "user_old",
            "quote_id": "cq_old",
            "monthly_price_usd_micros": MONTHLY,
            "started_at": "2026-10-01T00:00:00Z",
            "stopped_at": None,
        }
    )

    assert (record.funding_source, record.charged_cents, record.credited_cents) == (
        None,
        None,
        None,
    )


async def test_the_mock_lists_the_ledger_amounts_of_a_wallet_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_billing_to_mock(monkeypatch)
    installation_id = uuid.uuid4()
    # October has 31 days: 72 hours of a line started on the 17th leave 672 of its 744 unused.
    stopped = _hosted_line("user_mock_amounts", installation_id, stopped=NOW)
    running = _hosted_line("user_mock_amounts", uuid.uuid4())
    card = _hosted_line("user_mock_amounts", uuid.uuid4(), funding_source="stripe")

    lines = {
        line.line_item_id: line
        for line in await list_companion_line_items(
            owner_user_id="user_mock_amounts", state="all", since=NOW - timedelta(days=35)
        )
    }

    assert (lines[stopped].charged_cents, lines[stopped].credited_cents) == (900, 813)
    assert (lines[running].charged_cents, lines[running].credited_cents) == (900, 0)
    assert lines[card].funding_source == "stripe"
    assert (lines[card].charged_cents, lines[card].credited_cents) == (None, None)
    assert str(installation_id) == lines[stopped].installation_id


# ---------------------------------------------------------------------------
# One case per kind.
# ---------------------------------------------------------------------------


async def test_a_hosted_line_no_installation_records_is_logged_and_counted_only(
    db_session: AsyncSession,
    seed_user: User,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    route_billing_to_mock(monkeypatch)
    # A removed installation is enough for the owner to be enumerated.
    await _installation(db_session, seed_user, line_item_id=None, removed=True)
    orphan_installation = uuid.uuid4()
    line_item_id = _hosted_line(seed_user.clerk_id, orphan_installation, stopped=NOW)
    before = _counter("missing_local")

    with caplog.at_level("WARNING", logger="app.services.companion_billing_reconcile"):
        result = await _worker(db_session).run_once()

    assert result.mismatches == 1
    assert _counter("missing_local") == before + 1
    assert await _findings(db_session, line_item_id) == []
    assert any(
        "missing_local" in message
        and line_item_id in message
        and str(orphan_installation) in message
        for message in caplog.messages
    )


async def test_a_line_in_flight_is_not_judged_missing(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    await _installation(db_session, seed_user, line_item_id=None, removed=True)
    _hosted_line(seed_user.clerk_id, uuid.uuid4(), started=NOW - timedelta(minutes=5))

    result = await _worker(db_session).run_once()

    assert result.mismatches == 0


@pytest.mark.parametrize(("offset_seconds", "mismatched"), [(59, False), (60, False), (61, True)])
async def test_a_start_differing_by_more_than_a_minute_is_a_window_mismatch(
    db_session: AsyncSession,
    seed_user: User,
    monkeypatch: pytest.MonkeyPatch,
    offset_seconds: int,
    mismatched: bool,
) -> None:
    route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(
        db_session, seed_user, hosted_started=STARTED + timedelta(seconds=offset_seconds)
    )

    await _worker(db_session).run_once()

    assert await _kinds(db_session, line_item_id) == (
        {"window_mismatch": True} if mismatched else {}
    )


async def test_a_hosted_stop_the_installation_never_recorded_is_corrected_and_audited(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    hosted_stop = NOW - timedelta(hours=2, minutes=17)
    row, line_item_id = await _billed(
        db_session, seed_user, hosted_stopped=hosted_stop, accrue=False
    )
    before = _counter("window_mismatch")

    result = await _worker(db_session).run_once()

    assert result.corrections == 1
    assert _counter("window_mismatch") == before + 1
    await db_session.refresh(row)
    assert row.billing_stopped_at == hosted_stop
    (finding,) = [
        finding
        for finding in await _findings(db_session, line_item_id)
        if finding.kind == "window_mismatch"
    ]
    assert finding.local_value == f"{STARTED.isoformat()}/open"
    assert finding.hosted_value == f"{STARTED.isoformat()}/{hosted_stop.isoformat()}"
    # The correction removed the only difference, so the finding closes in the same pass.
    assert finding.resolved_at == NOW
    (event,) = (
        await db_session.scalars(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == STOP_RECONCILED_ACTION,
                ControlPlaneAuditEvent.resource_id == line_item_id,
            )
        )
    ).all()
    assert event.actor_type == "system"
    assert event.target_user_id == seed_user.id
    assert event.details["installation_id"] == str(row.id)
    assert event.details["previous_billing_stopped_at"] is None
    assert event.details["billing_stopped_at"] == hosted_stop.isoformat()


@pytest.mark.parametrize(("offset_seconds", "corrected"), [(59, False), (61, True)])
async def test_a_stop_is_corrected_to_the_hosted_one_only_past_the_tolerance(
    db_session: AsyncSession,
    seed_user: User,
    monkeypatch: pytest.MonkeyPatch,
    offset_seconds: int,
    corrected: bool,
) -> None:
    route_billing_to_mock(monkeypatch)
    hosted_stop = NOW - timedelta(hours=3)
    local_stop = hosted_stop + timedelta(seconds=offset_seconds)
    row, _ = await _billed(
        db_session, seed_user, hosted_stopped=hosted_stop, local_stopped=local_stop
    )

    result = await _worker(db_session).run_once()

    await db_session.refresh(row)
    assert result.corrections == (1 if corrected else 0)
    assert row.billing_stopped_at == (hosted_stop if corrected else local_stop)


async def test_a_local_stop_on_a_running_hosted_line_is_reported_never_corrected(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    local_stop = NOW - timedelta(hours=1)
    row, line_item_id = await _billed(db_session, seed_user, local_stopped=local_stop)

    result = await _worker(db_session).run_once()

    await db_session.refresh(row)
    assert result.corrections == 0
    assert row.billing_stopped_at == local_stop
    assert (await _kinds(db_session, line_item_id))["window_mismatch"] is True
    # Nothing at /v2 was stopped either: the job never moves money.
    assert mock_deploy_api.COMPANION_LINE_ITEMS[line_item_id]["stopped_at"] is None


async def test_a_hosted_price_other_than_the_quote_is_a_price_mismatch(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(db_session, seed_user, hosted_monthly=MONTHLY + 1_000_000)

    await _worker(db_session).run_once()

    (finding,) = await _findings(db_session, line_item_id)
    assert finding.kind == "price_mismatch"
    assert (finding.local_value, finding.hosted_value) == (str(MONTHLY), str(MONTHLY + 1_000_000))


async def test_a_closed_hosted_hour_with_no_local_compute_row_is_an_accrual_gap(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user)
    gap = STARTED + timedelta(hours=10)
    await db_session.execute(
        UsageEvent.__table__.delete().where(
            UsageEvent.installation_id == row.id, UsageEvent.period_start == gap
        )
    )
    await db_session.commit()

    await _worker(db_session).run_once()

    (finding,) = await _findings(db_session, line_item_id)
    assert finding.kind == "accrual_gap"
    # 72 hours since the start, less the last one still inside the accrual grace.
    assert (finding.local_value, finding.hosted_value) == ("70", "71")


async def test_an_hour_inside_the_accrual_grace_is_not_a_gap_yet(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user)
    await db_session.execute(
        UsageEvent.__table__.delete().where(
            UsageEvent.installation_id == row.id, UsageEvent.period_start == NOW - HOUR
        )
    )
    await db_session.commit()

    await _worker(db_session).run_once()

    assert await _findings(db_session, line_item_id) == []


async def test_local_accrual_off_the_hosted_net_charge_is_an_amount_drift(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, hosted_stopped=NOW, local_stopped=NOW)
    await db_session.execute(
        update(UsageEvent)
        .where(UsageEvent.installation_id == row.id)
        .values(cost_usd_micros=UsageEvent.cost_usd_micros * 2)
    )
    await db_session.commit()

    await _worker(db_session).run_once()

    (finding,) = await _findings(db_session, line_item_id)
    assert finding.kind == "amount_drift"
    hour_cost = compute_hour_cost(MONTHLY, STARTED)
    # Hosted: 900 cents charged, 813 credited for the 672 unused hours of a 744-hour month.
    assert (finding.local_value, finding.hosted_value) == (str(2 * 72 * hour_cost), "870000")


async def test_a_consistent_stopped_wallet_line_is_within_the_amount_tolerance(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(db_session, seed_user, hosted_stopped=NOW, local_stopped=NOW)

    await _worker(db_session).run_once()

    assert await _findings(db_session, line_item_id) == []


def test_the_amount_tolerance_is_one_hour_of_the_price_plus_one_cent() -> None:
    row = CompanionInstallation(quoted_monthly_usd_micros=MONTHLY)
    line = _record(stopped_at=NOW, charged_cents=900, credited_cents=813)
    hosted = 87 * 10_000
    tolerance = compute_hour_cost(MONTHLY, NOW) + 10_000

    assert amount_drift(row, line, hosted + tolerance) is None
    assert amount_drift(row, line, hosted - tolerance) is None
    assert amount_drift(row, line, hosted + tolerance + 1) is not None
    assert amount_drift(row, line, hosted - tolerance - 1) is not None


def test_amount_drift_judges_only_a_stopped_wallet_line_with_amounts() -> None:
    row = CompanionInstallation(quoted_monthly_usd_micros=MONTHLY)

    assert amount_drift(row, _record(charged_cents=900, credited_cents=0), 0) is None
    assert amount_drift(row, _record(stopped_at=NOW, funding_source="stripe"), 0) is None
    assert amount_drift(row, _record(stopped_at=NOW), 0) is None


def test_the_window_tolerance_edges() -> None:
    def row(stopped: datetime | None) -> CompanionInstallation:
        return CompanionInstallation(billing_started_at=STARTED, billing_stopped_at=stopped)

    assert window_mismatch(row(None), _record()) is None
    assert window_mismatch(row(NOW), _record(stopped_at=NOW + timedelta(seconds=59))) is None
    assert window_mismatch(row(NOW), _record(stopped_at=NOW - timedelta(seconds=60))) is None
    assert window_mismatch(row(NOW), _record(stopped_at=NOW + timedelta(seconds=61))) is not None
    assert window_mismatch(row(None), _record(stopped_at=NOW)) is not None
    assert window_mismatch(row(NOW), _record()) is not None


# ---------------------------------------------------------------------------
# Re-runs: idempotent, and a fix resolves.
# ---------------------------------------------------------------------------


async def test_a_rerun_refreshes_the_same_finding_instead_of_adding_one(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    _, line_item_id = await _billed(db_session, seed_user, local_monthly=MONTHLY - 1)
    clock = _Clock(NOW)
    worker = _worker(db_session, clock)

    await worker.run_once()
    clock.now = NOW + timedelta(minutes=30)
    second = await worker.run_once()

    assert second.mismatches == 1
    (finding,) = await _findings(db_session, line_item_id)
    assert finding.kind == "price_mismatch"
    assert finding.first_seen == NOW
    assert finding.last_seen == NOW + timedelta(minutes=30)
    assert finding.resolved_at is None


async def test_a_finding_no_longer_reproduced_is_resolved_and_leaves_the_open_list(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, local_monthly=MONTHLY - 1)
    clock = _Clock(NOW)
    worker = _worker(db_session, clock)
    await worker.run_once()

    row.quoted_monthly_usd_micros = MONTHLY
    await db_session.commit()
    clock.now = NOW + timedelta(minutes=30)
    second = await worker.run_once()

    assert second.findings_resolved == 1
    (finding,) = await _findings(db_session, line_item_id)
    assert finding.resolved_at == NOW + timedelta(minutes=30)
    assert finding.last_seen == NOW


async def test_a_resolved_finding_that_comes_back_is_reopened_keeping_first_seen(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, local_monthly=MONTHLY - 1)
    clock = _Clock(NOW)
    worker = _worker(db_session, clock)
    await worker.run_once()
    row.quoted_monthly_usd_micros = MONTHLY
    await db_session.commit()
    clock.now = NOW + timedelta(minutes=10)
    await worker.run_once()

    row.quoted_monthly_usd_micros = MONTHLY - 2
    await db_session.commit()
    clock.now = NOW + timedelta(minutes=20)
    await worker.run_once()

    (finding,) = await _findings(db_session, line_item_id)
    assert finding.resolved_at is None
    assert finding.first_seen == NOW
    assert finding.local_value == str(MONTHLY - 2)


# ---------------------------------------------------------------------------
# Admin: on-demand run and the open findings.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def _override_get_session():
        yield db_session

    async with _admin_client(_override_get_session) as client:
        yield client


async def test_admin_runs_a_pass_on_demand_and_reads_the_open_findings(
    admin_client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, local_monthly=MONTHLY - 1)

    ran = await admin_client.post(RECONCILE_PATH, headers=_ADMIN_AUTH)

    assert ran.status_code == 200, ran.text
    assert ran.headers["cache-control"] == "no-store, private"
    body = ran.json()
    assert body["skipped"] is False
    assert body["owners_scanned"] >= 1
    assert body["mismatches"] >= 1
    listed = await admin_client.get(
        FINDINGS_PATH, params={"user_id": str(seed_user.id)}, headers=_ADMIN_AUTH
    )
    assert listed.status_code == 200, listed.text
    # The pass ran on the real clock, so hours after the fixed NOW may also show as a gap.
    (item,) = [item for item in listed.json()["items"] if item["kind"] == "price_mismatch"]
    assert item["line_item_id"] == line_item_id
    assert item["installation_id"] == str(row.id)
    assert item["user_id"] == str(seed_user.id)
    assert item["kind"] == "price_mismatch"
    assert (item["local_value"], item["hosted_value"]) == (str(MONTHLY - 1), str(MONTHLY))
    requested = (
        await db_session.scalars(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "companion.billing_reconcile_requested"
            )
        )
    ).all()
    assert requested and requested[-1].actor_type == "admin"


async def test_admin_run_reports_a_skipped_pass_when_v2_is_unconfigured(
    admin_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "deploy_api_url", "")

    ran = await admin_client.post(RECONCILE_PATH, headers=_ADMIN_AUTH)

    assert ran.status_code == 200, ran.text
    assert ran.json()["skipped"] is True


async def test_admin_reconcile_routes_require_the_admin_key(
    admin_client: httpx.AsyncClient,
) -> None:
    ran = await admin_client.post(RECONCILE_PATH)
    listed = await admin_client.get(FINDINGS_PATH, headers={"X-Admin-Key": "wrong"})

    assert ran.status_code in (401, 403), ran.text
    assert listed.status_code in (401, 403), listed.text


async def test_a_finding_goes_with_its_installation(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, local_monthly=MONTHLY - 1)
    await _worker(db_session).run_once()
    assert len(await _findings(db_session, line_item_id)) == 1

    await db_session.delete(row)
    await db_session.commit()

    assert await _findings(db_session, line_item_id) == []


async def test_compute_rows_of_the_line_are_the_ones_compared(
    db_session: AsyncSession, seed_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only compute rows inside the hosted window count toward the drift and the gap."""
    route_billing_to_mock(monkeypatch)
    row, line_item_id = await _billed(db_session, seed_user, hosted_stopped=NOW, local_stopped=NOW)
    db_session.add(
        UsageEvent(
            dimension=USAGE_DIMENSION_COMPUTE,
            user_id=seed_user.id,
            environment_id=row.environment_id,
            installation_id=row.id,
            companion_key="postiz",
            period_start=STARTED - timedelta(days=3),
            ts=STARTED - timedelta(days=3),
            model="postiz/standard",
            quantity=1,
            cost_usd_micros=5_000_000,
        )
    )
    await db_session.commit()

    await _worker(db_session).run_once()

    assert await _findings(db_session, line_item_id) == []
