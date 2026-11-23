"""The tenant's monthly money ceiling, enforced where an agent's companion work is dispatched.

OPS-06's ceiling half, per D-20 and D-21. The ceiling is its own ledger. Phase 5's caps count
calls, records and bytes (``CAP_UNITS``) and are never denominated in money, so the ceiling is not
a fourth cap unit. It is checked in ``call_companion_tool`` after the policy gate and before the
caps and the mint, so a refused call never becomes a signed pre-authorized call.

**What is refused, and what is NOT.** At the ceiling the agent's tool call is refused with the
distinct decision ``refused_budget_ceiling`` and the distinct reason token
``spend_ceiling_reached``. Neither is a Phase 5 token, so an activity row says exactly which
ceiling fired. The companion itself is not touched: a billing event must never become an
availability event (T-06-22-03), so the installation stays ``complete`` with its units and its
billing untouched. The tracer asserts that explicitly rather than trusting the absence of code.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select

from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_installation import CompanionInstallation
from app.models.companion_tool_call import CALL_DECISIONS, CallDecision, CompanionToolCall
from app.models.companion_tool_policy import CompanionToolPolicy
from app.models.hosted_runtime import HostedRuntimeState
from app.models.usage_event import USAGE_DIMENSION_COMPUTE, UsageEvent
from app.services import companion_facade
from app.services.companion_facade import call_companion_tool, reset_companion_tools_cache
from app.services.companion_ratelimit import reset_companion_rate_limiter
from tests.test_postiz_verbs import LIST_INTEGRATIONS, _deploy_postiz, _platform_minted_auth

CEILING_DECISION = "refused_budget_ceiling"
CEILING_REASON = "spend_ceiling_reached"


@pytest.fixture(autouse=True)
def _clear_companion_process_state():
    reset_companion_tools_cache()
    reset_companion_rate_limiter()
    yield
    reset_companion_tools_cache()
    reset_companion_rate_limiter()


def _this_hour() -> datetime:
    """The current whole UTC hour: always inside the current UTC month, which is what counts."""
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


async def _complete_postiz_installation(
    db_session,
    seed_user,
    environment_id: uuid.UUID,
    *,
    started: datetime | None = None,
    billing_stopped_at: datetime | None = None,
) -> uuid.UUID:
    """A live, billed postiz installation on the deployed Agent. Returns its identifier."""
    label = f"ceiling-{uuid.uuid4().hex[:8]}"
    started = started or _this_hour()
    installation = CompanionInstallation(
        user_id=seed_user.id,
        environment_id=environment_id,
        companion_key="postiz",
        isolation_tier="standard",
        status="complete",
        version="v2.24.0",
        pinned_digest=hashlib.sha256(label.encode()).hexdigest(),
        hostname_label=label,
        hostname=f"{label}.companions.example",
        owner_email="owner@companions.example",
        quote_id="quote-ceiling",
        quoted_monthly_usd_micros=30_000_000,
        quoted_storage_usd_micros_per_gib_month=100_000,
        billing_line_item_id="line-item-ceiling",
        billing_started_at=started,
        billing_stopped_at=billing_stopped_at,
        completed_at=started,
        external_resources={"units": ["iforge-companion-postiz.service"]},
        backup_state={"db_bytes": 1024},
    )
    db_session.add(installation)
    await db_session.commit()
    return installation.id


async def _record_compute_spend(
    db_session,
    seed_user,
    *,
    environment_id: uuid.UUID,
    installation_id: uuid.UUID,
    micros: int,
    period: datetime | None = None,
) -> None:
    """One recorded compute row (this hour by default), as the metering worker would write it."""
    period = period or _this_hour()
    db_session.add(
        UsageEvent(
            dimension=USAGE_DIMENSION_COMPUTE,
            user_id=seed_user.id,
            environment_id=environment_id,
            installation_id=installation_id,
            companion_key="postiz",
            period_start=period,
            ts=period,
            model="postiz/standard",
            quantity=1,
            cost_usd_micros=micros,
        )
    )
    await db_session.commit()


async def _set_ceiling(db_session, user_id: uuid.UUID, micros: int) -> None:
    # Imported here: the ceiling table is this plan's, and the module-level imports above are the
    # shipped surface the tracer drives.
    from app.models.companion_budget import CompanionSpendCeiling

    db_session.add(CompanionSpendCeiling(user_id=user_id, ceiling_usd_micros=micros))
    await db_session.commit()


async def _installation_snapshot(db_session, installation_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await db_session.execute(
            select(CompanionInstallation)
            .where(CompanionInstallation.id == installation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return {
        "status": row.status,
        "operation": row.operation,
        "external_resources": row.external_resources,
        "billing_stopped_at": row.billing_stopped_at,
        "removed_at": row.removed_at,
    }


async def _deployed_companion_keys(db_session, environment_id: uuid.UUID) -> set[str]:
    state = (
        await db_session.execute(
            select(HostedRuntimeState)
            .where(HostedRuntimeState.environment_id == environment_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return set(state.companions or {})


def _forbid_mint(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    minted: list[object] = []

    def _recording_mint(**kwargs: object) -> object:
        minted.append(kwargs)
        raise AssertionError("a call refused at the spend ceiling must never be minted")

    monkeypatch.setattr(companion_facade, "mint_dispatch_call", _recording_mint)
    return minted


async def test_agent_call_is_refused_at_the_ceiling_and_the_companion_keeps_running(
    db_session, seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The distinct decision is part of the behavior: a ceiling refusal recorded under a Phase 5
    # token would repeat the WINDOWS #186 ambiguity about which ceiling fired.
    assert CEILING_DECISION in CALL_DECISIONS

    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=1_500_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    before = await _installation_snapshot(db_session, installation_id)
    minted = _forbid_mint(monkeypatch)

    result = await call_companion_tool(
        LIST_INTEGRATIONS,
        {},
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert f"[{CEILING_REASON}]" in text
    assert "cannot be raised from a tool argument" in text
    assert minted == []

    rows = list(
        (
            await db_session.execute(
                select(CompanionToolCall).where(CompanionToolCall.user_id == user_id)
            )
        ).scalars()
    )
    assert [row.decision for row in rows] == [CEILING_DECISION]
    assert rows[0].call_jti is None
    assert CEILING_REASON in (rows[0].error_text or "")

    # Nothing was queued for an executor. The only dispatch-table row is the refusal's own
    # terminal journal entry, which is `failed` from birth and never claimable.
    dispatch = list(
        (
            await db_session.execute(
                select(CompanionDispatchCall).where(CompanionDispatchCall.user_id == user_id)
            )
        ).scalars()
    )
    assert all(row.status == "failed" for row in dispatch), [row.status for row in dispatch]

    # D-21: the companion keeps running. Status, operation, units and billing are unchanged, and
    # the Agent still declares the companion.
    assert await _installation_snapshot(db_session, installation_id) == before
    assert before["status"] == "complete"
    assert "postiz" in await _deployed_companion_keys(db_session, environment_id)


async def test_a_call_under_the_ceiling_proceeds_to_the_phase_5_gates(
    db_session, seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the ceiling the call is not stopped by it: the next gate (a zero cap) answers."""
    from app.models.companion_session_budget import CompanionCapPolicy

    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=500_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    # Zero is a VALUE: a per-call ceiling of zero calls refuses everything, so reaching it proves
    # the spend ceiling let the call through.
    db_session.add(
        CompanionCapPolicy(
            user_id=user_id,
            companion_key="postiz",
            per_call_calls=0,
            per_call_records=50,
            per_call_bytes=4096,
            per_session_calls=50,
            per_session_records=500,
            per_session_bytes=40_960,
        )
    )
    await db_session.commit()
    minted = _forbid_mint(monkeypatch)

    result = await call_companion_tool(
        LIST_INTEGRATIONS,
        {},
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    assert CEILING_REASON not in result["content"][0]["text"]
    assert minted == []
    decisions = (
        await db_session.execute(
            select(CompanionToolCall.decision).where(CompanionToolCall.user_id == user_id)
        )
    ).scalars()
    assert list(decisions) == [CallDecision.REFUSED_CAP.value]


async def test_the_ceiling_is_checked_after_the_policy_gate(db_session, seed_user) -> None:
    """A call the tenant's policy denies is recorded as a policy refusal, even over the ceiling.

    The ceiling sits after the policy gate: "you told agents never to do this" is the more
    specific answer, and it must not be masked by the money ceiling.
    """
    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=1_500_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    db_session.add(
        CompanionToolPolicy(
            user_id=user_id, companion_key="postiz", verb="list_integrations", mode="deny"
        )
    )
    await db_session.commit()

    result = await call_companion_tool(
        LIST_INTEGRATIONS,
        {},
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    decisions = (
        await db_session.execute(
            select(CompanionToolCall.decision).where(CompanionToolCall.user_id == user_id)
        )
    ).scalars()
    assert list(decisions) == [CallDecision.REFUSED_POLICY.value]


# ---------------------------------------------------------------------------
# The tenant reads and sets its own ceiling (web auth only).
# ---------------------------------------------------------------------------

SPEND_CEILING_PATH = "/v1/companions/spend-ceiling"


def _current_month() -> str:
    now = datetime.now(UTC)
    return f"{now.year:04d}-{now.month:02d}"


async def test_tenant_reads_and_sets_the_ceiling(client, db_session, seed_user) -> None:
    from app.models.companion_budget import CompanionSpendCeiling
    from tests.test_companion_metering import _other_tenant

    other = await _other_tenant(db_session)
    other_id = other.id
    db_session.add(CompanionSpendCeiling(user_id=other_id, ceiling_usd_micros=7_000_000))
    await db_session.commit()

    # Before any PUT: no ceiling applies, and the answer says so rather than inventing one.
    before = await client.get(SPEND_CEILING_PATH)
    assert before.status_code == 200, before.text
    assert before.json() == {
        "ceiling_usd_micros": None,
        "source": "none",
        "spent_usd_micros": 0,
        "reached": False,
        "month": _current_month(),
    }

    # Zero is a VALUE: it is accepted, round-trips as zero, and is reached at once.
    zero = await client.put(SPEND_CEILING_PATH, json={"ceiling_usd_micros": 0})
    assert zero.status_code == 200, zero.text
    assert zero.json()["ceiling_usd_micros"] == 0
    assert zero.json()["source"] == "tenant"
    assert zero.json()["reached"] is True

    after = await client.get(SPEND_CEILING_PATH)
    assert after.status_code == 200, after.text
    assert after.json()["ceiling_usd_micros"] == 0
    assert after.json()["source"] == "tenant"

    # A second PUT replaces the tenant's one row and records who set it.
    raised = await client.put(SPEND_CEILING_PATH, json={"ceiling_usd_micros": 25_000_000})
    assert raised.status_code == 200, raised.text
    assert raised.json()["ceiling_usd_micros"] == 25_000_000
    assert raised.json()["reached"] is False
    rows = list(
        (
            await db_session.execute(
                select(CompanionSpendCeiling)
                .where(CompanionSpendCeiling.user_id == seed_user.id)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )
    assert [(row.ceiling_usd_micros, row.updated_by_user_id) for row in rows] == [
        (25_000_000, seed_user.id)
    ]

    # The boundary refuses a negative amount, a non-integer, a missing field and any extra field.
    for body in (
        {"ceiling_usd_micros": -1},
        {"ceiling_usd_micros": 1.5},
        {"ceiling_usd_micros": "100"},
        {},
        {"ceiling_usd_micros": 10, "user_id": str(other_id)},
    ):
        refused = await client.put(SPEND_CEILING_PATH, json=body)
        assert refused.status_code == 422, (body, refused.text)

    # Another tenant's ceiling is neither read nor written by this tenant's calls.
    other_row = (
        await db_session.execute(
            select(CompanionSpendCeiling)
            .where(CompanionSpendCeiling.user_id == other_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert other_row.ceiling_usd_micros == 7_000_000
    assert (await client.get(SPEND_CEILING_PATH)).json()["ceiling_usd_micros"] == 25_000_000


async def test_the_ceiling_read_out_reports_recorded_spend(client, db_session, seed_user) -> None:
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=1_500_000,
    )

    put = await client.put(SPEND_CEILING_PATH, json={"ceiling_usd_micros": 1_000_000})
    assert put.status_code == 200, put.text

    body = (await client.get(SPEND_CEILING_PATH)).json()
    assert body["spent_usd_micros"] == 1_500_000
    assert body["reached"] is True
    assert body["month"] == _current_month()


# ---------------------------------------------------------------------------
# D-21's other half: at the ceiling a NEW install is refused too (plan 06-31).
# ---------------------------------------------------------------------------

INSTALLATIONS_PATH = "/v1/companions/installations"
INSTALL_CEILING_CODE = "companion_spend_ceiling_reached"


async def _install_footprint(db_session, user_id: uuid.UUID) -> dict[str, int]:
    """How many installation, secret and install-log rows the tenant has.

    The installation row IS the install job: the lifecycle worker claims ``queued`` installations,
    so "no job written" means "no new installation row". Secrets and log lines are counted through
    the tenant's installations, so a stray row for any of them shows up here.
    """
    from app.models.companion_installation import (
        CompanionInstallationSecret,
        CompanionInstallLogLine,
    )

    owned = select(CompanionInstallation.id).where(CompanionInstallation.user_id == user_id)
    counts: dict[str, int] = {}
    for label, model, predicate in (
        ("installations", CompanionInstallation, CompanionInstallation.user_id == user_id),
        (
            "secrets",
            CompanionInstallationSecret,
            CompanionInstallationSecret.installation_id.in_(owned),
        ),
        ("log_lines", CompanionInstallLogLine, CompanionInstallLogLine.installation_id.in_(owned)),
    ):
        total = await db_session.scalar(select(func.count()).select_from(model).where(predicate))
        counts[label] = int(total or 0)
    return counts


async def _second_agent(db_session, user_id: uuid.UUID) -> uuid.UUID:
    from tests.conftest import create_env_with_project

    agent = await create_env_with_project(
        db_session,
        user_id=user_id,
        machine_id=f"machine-{uuid.uuid4().hex[:12]}",
        machine_name="second-postiz-host",
    )
    return agent.id


def _install_body(agent_id: uuid.UUID, quote_id: str) -> dict[str, str]:
    return {
        "agent_id": str(agent_id),
        "companion_key": "postiz",
        "isolation_tier": "standard",
        "quote_id": quote_id,
    }


@pytest.fixture
def companion_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "companion_edge_zone_name", "companions.example")


async def test_install_is_refused_at_the_ceiling(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the ceiling a second companion cannot be installed, and the first keeps running (D-21).

    A new install adds recurring cost, so it is refused like an agent's call: 409 with its own
    code, before any installation row, secret atom or install log line is written. The tenant
    already has a complete installation, and it is left exactly as it was.
    """
    from tests.companion_billing_stub import quote_for

    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=1_500_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    second_agent_id = await _second_agent(db_session, user_id)
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    before = await _installation_snapshot(db_session, installation_id)
    footprint = await _install_footprint(db_session, user_id)

    response = await client.post(INSTALLATIONS_PATH, json=_install_body(second_agent_id, quote_id))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == INSTALL_CEILING_CODE
    # Nothing was written: no installation (the job), no secret atom, no install log line.
    assert await _install_footprint(db_session, user_id) == footprint
    assert footprint["installations"] == 1
    # D-21: the companion the tenant already has keeps running, untouched.
    assert await _installation_snapshot(db_session, installation_id) == before
    assert before["status"] == "complete"
    assert "postiz" in await _deployed_companion_keys(db_session, environment_id)


# ---------------------------------------------------------------------------
# The spend the ceiling sees cannot be outrun, and the defaults are explicit (plan 06-31).
# ---------------------------------------------------------------------------

# A fixed moment, passed as `now`, so the hour arithmetic never depends on when the suite runs.
# 31-day March: 744 hours, so an hour of the 30 USD/month tier costs 40_322 micro-dollars.
MARCH_MOMENT = datetime(2026, 3, 15, 12, 30, tzinfo=UTC)
MONTHLY_USD_MICROS = 30_000_000
HALF_HOUR = timedelta(minutes=30)


def _hour(day: int, hour: int, *, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=UTC)


async def _record_hours(
    db_session,
    seed_user,
    *,
    environment_id: uuid.UUID,
    installation_id: uuid.UUID,
    hours: list[datetime],
    micros: int,
) -> None:
    for period in hours:
        await _record_compute_spend(
            db_session,
            seed_user,
            environment_id=environment_id,
            installation_id=installation_id,
            micros=micros,
            period=period,
        )


async def test_unaccrued_compute_counts_toward_the_ceiling(db_session, seed_user) -> None:
    """Compute the metering worker has not recorded yet still counts (research Pitfall 10).

    Compute is recorded by the hour, after the hour ends. A ceiling compared to recorded rows
    alone could be outrun by however many hours the worker is behind. Here three hours
    (09:00-12:00) have ended with no row, and the in-progress 12:00 hour is not counted.
    """
    from app.services.companion_budget import check_spend_ceiling, month_to_date_spend
    from app.services.companion_metering import accrue_missing_compute, compute_hour_cost

    user_id = seed_user.id
    agent_id = await _second_agent(db_session, user_id)
    installation_id = await _complete_postiz_installation(
        db_session, seed_user, agent_id, started=_hour(15, 6)
    )
    hour_cost = compute_hour_cost(MONTHLY_USD_MICROS, MARCH_MOMENT)
    await _record_hours(
        db_session,
        seed_user,
        environment_id=agent_id,
        installation_id=installation_id,
        hours=[_hour(15, 6), _hour(15, 7), _hour(15, 8)],
        micros=hour_cost,
    )
    recorded = 3 * hour_cost

    spent = await month_to_date_spend(db_session, user_id=user_id, now=MARCH_MOMENT)
    assert spent == recorded + 3 * hour_cost

    # A ceiling above the recorded rows, but below recorded plus the unaccrued hours, is reached.
    await _set_ceiling(db_session, user_id, recorded + hour_cost)
    decision = await check_spend_ceiling(db_session, user_id=user_id, now=MARCH_MOMENT)
    assert decision.reached
    assert decision.spent_usd_micros == recorded + 3 * hour_cost

    # The term is exactly what the worker would record: once it accrues those hours, the spend the
    # ceiling sees does not move, because nothing is counted twice.
    installation = await db_session.get(CompanionInstallation, installation_id)
    assert installation is not None
    assert await accrue_missing_compute(db_session, installation, MARCH_MOMENT) == 3
    await db_session.commit()
    assert await month_to_date_spend(db_session, user_id=user_id, now=MARCH_MOMENT) == spent


async def test_unaccrued_compute_stops_where_billing_stopped(db_session, seed_user) -> None:
    """Hours after an installation's line item stopped are not billable, so they never count."""
    from app.services.companion_budget import month_to_date_spend
    from app.services.companion_metering import compute_hour_cost

    user_id = seed_user.id
    agent_id = await _second_agent(db_session, user_id)
    installation_id = await _complete_postiz_installation(
        db_session, seed_user, agent_id, started=_hour(15, 6), billing_stopped_at=_hour(15, 10)
    )
    hour_cost = compute_hour_cost(MONTHLY_USD_MICROS, MARCH_MOMENT)
    await _record_hours(
        db_session,
        seed_user,
        environment_id=agent_id,
        installation_id=installation_id,
        hours=[_hour(15, 6), _hour(15, 7)],
        micros=hour_cost,
    )

    # 08:00 and 09:00 were billed and not yet recorded; from 10:00 nothing is billed.
    spent = await month_to_date_spend(db_session, user_id=user_id, now=MARCH_MOMENT)
    assert spent == 4 * hour_cost


async def test_zero_ceiling_refuses_everything(
    client, db_session, seed_user, companion_zone, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero is a value, not "no ceiling": an agent call and a new install are both refused."""
    from tests.companion_billing_stub import quote_for

    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _set_ceiling(db_session, user_id, 0)
    minted = _forbid_mint(monkeypatch)

    result = await call_companion_tool(
        LIST_INTEGRATIONS,
        {},
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
    )
    assert result["isError"] is True
    assert f"[{CEILING_REASON}]" in result["content"][0]["text"]
    assert minted == []

    second_agent_id = await _second_agent(db_session, user_id)
    quote_id = await quote_for(monkeypatch, seed_user.clerk_id)
    footprint = await _install_footprint(db_session, user_id)
    response = await client.post(INSTALLATIONS_PATH, json=_install_body(second_agent_id, quote_id))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == INSTALL_CEILING_CODE
    assert await _install_footprint(db_session, user_id) == footprint


async def test_absent_ceiling_uses_the_operator_default_or_none(
    client, db_session, seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tenant row: the operator's default when one is configured, otherwise unbounded.

    The GET read-out says which, through the one resolver. The tenant's own row always wins over
    the default. That holds in both directions, so a tenant can set a ceiling above or below it.
    """
    from app.core.config import settings
    from app.services.companion_budget import check_spend_ceiling, resolve_spend_ceiling

    user_id = seed_user.id
    setting = "companion_default_spend_ceiling_usd_micros"
    assert setting in type(settings).model_fields, "the operator default must be a real setting"

    # Unset: no ceiling applies, and the answer says so rather than inventing one.
    monkeypatch.setattr(settings, setting, None, raising=False)
    resolved = await resolve_spend_ceiling(db_session, user_id=user_id)
    assert (resolved.ceiling_usd_micros, resolved.source) == (None, "none")
    unset = (await client.get(SPEND_CEILING_PATH)).json()
    assert (unset["ceiling_usd_micros"], unset["source"]) == (None, "none")

    # Configured: the default applies, reported as the operator's.
    monkeypatch.setattr(settings, setting, 5_000_000, raising=False)
    resolved = await resolve_spend_ceiling(db_session, user_id=user_id)
    assert (resolved.ceiling_usd_micros, resolved.source) == (5_000_000, "operator_default")
    configured = (await client.get(SPEND_CEILING_PATH)).json()
    assert (configured["ceiling_usd_micros"], configured["source"]) == (
        5_000_000,
        "operator_default",
    )
    assert configured["reached"] is False

    # A default of zero is a value too: with no tenant row, every call is refused.
    monkeypatch.setattr(settings, setting, 0, raising=False)
    zero = await check_spend_ceiling(db_session, user_id=user_id)
    assert not zero.allowed
    assert zero.ceiling.source == "operator_default"

    # The tenant's own row wins, above the default as well as below it.
    put = await client.put(SPEND_CEILING_PATH, json={"ceiling_usd_micros": 25_000_000})
    assert put.status_code == 200, put.text
    own = (await client.get(SPEND_CEILING_PATH)).json()
    assert (own["ceiling_usd_micros"], own["source"]) == (25_000_000, "tenant")
    assert (await check_spend_ceiling(db_session, user_id=user_id)).allowed


def test_the_operator_default_is_a_setting_with_no_committed_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AGENTS.md's OSS boundary: the default is operator configuration, never a committed value."""
    from pydantic import ValidationError

    from app.core.config import Settings
    from tests.doc_contract import REPO_ROOT

    env = "COMPANION_DEFAULT_SPEND_CEILING_USD_MICROS"
    field = Settings.model_fields.get("companion_default_spend_ceiling_usd_micros")
    assert field is not None, "the operator default must be a real setting"
    assert field.default is None
    example = (REPO_ROOT / "backend" / ".env.example").read_text().splitlines()
    assert f"{env}=" in example, "the example must carry the setting with an EMPTY value"

    # The empty line the example ships parses as unset, not as an error and not as zero.
    monkeypatch.setenv(env, "")
    assert Settings(_env_file=None).companion_default_spend_ceiling_usd_micros is None
    monkeypatch.setenv(env, "2500000")
    assert Settings(_env_file=None).companion_default_spend_ceiling_usd_micros == 2_500_000
    monkeypatch.setenv(env, "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


async def test_new_month_resets_the_comparison(db_session, seed_user) -> None:
    """March's spend, recorded or not yet recorded, is not April's."""
    from app.services.companion_budget import check_spend_ceiling
    from app.services.companion_metering import compute_hour_cost

    user_id = seed_user.id
    agent_id = await _second_agent(db_session, user_id)
    installation_id = await _complete_postiz_installation(
        db_session, seed_user, agent_id, started=_hour(30, 0)
    )
    # March is over the ceiling on recorded spend. The worker last recorded 21:00 on 31 March.
    await _record_hours(
        db_session,
        seed_user,
        environment_id=agent_id,
        installation_id=installation_id,
        hours=[_hour(31, 21)],
        micros=5_000_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    march_hour = compute_hour_cost(MONTHLY_USD_MICROS, _hour(31, 22))

    march = await check_spend_ceiling(db_session, user_id=user_id, now=_hour(31, 23) + HALF_HOUR)
    assert march.month == "2026-03"
    assert march.reached
    # 22:00 has ended with no row; 23:00 has not ended.
    assert march.spent_usd_micros == 5_000_000 + march_hour

    # In April the comparison starts again. March's unrecorded 22:00 and 23:00 are March's, and
    # April's own 00:00 and 01:00 have ended with no row.
    april_hour = compute_hour_cost(MONTHLY_USD_MICROS, _hour(1, 0, month=4))
    april = await check_spend_ceiling(
        db_session, user_id=user_id, now=_hour(1, 2, month=4) + HALF_HOUR
    )
    assert april.month == "2026-04"
    assert april.spent_usd_micros == 2 * april_hour
    assert april.allowed


async def test_ceiling_is_not_a_tool_argument(
    db_session, seed_user, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No tool argument can raise or bypass the ceiling (D-21, T-06-31-03).

    A call carrying ceiling-like arguments is refused by the kind's argument contract first,
    before the ceiling is even consulted, and the tenant's ceiling is unchanged afterwards. A
    well-formed call is then still refused at the ceiling.
    """
    from app.models.companion_budget import CompanionSpendCeiling
    from app.services.companion_budget import resolve_spend_ceiling

    user_id = seed_user.id
    environment_id = await _deploy_postiz(db_session, seed_user)
    installation_id = await _complete_postiz_installation(db_session, seed_user, environment_id)
    await _record_compute_spend(
        db_session,
        seed_user,
        environment_id=environment_id,
        installation_id=installation_id,
        micros=1_500_000,
    )
    await _set_ceiling(db_session, user_id, 1_000_000)
    minted = _forbid_mint(monkeypatch)
    smuggled: list[dict[str, Any]] = [
        {"ceiling_usd_micros": 10**12},
        {"spend_ceiling": {"ceiling_usd_micros": 10**12, "source": "tenant"}},
        {"budget_usd_micros": 10**12, "override_spend_ceiling": True},
    ]

    for arguments in smuggled:
        result = await call_companion_tool(
            LIST_INTEGRATIONS,
            arguments,
            auth=_platform_minted_auth(seed_user),
            db=db_session,
            deadline_seconds=0,
        )
        assert result["isError"] is True
        text = result["content"][0]["text"]
        assert "rejected its arguments" in text, text
        assert CEILING_REASON not in text

    decisions = (
        await db_session.execute(
            select(CompanionToolCall.decision)
            .where(CompanionToolCall.user_id == user_id)
            .order_by(CompanionToolCall.created_at)
        )
    ).scalars()
    assert list(decisions) == [CallDecision.REFUSED_POLICY.value] * len(smuggled)
    assert minted == []

    # The ceiling is exactly what the tenant set, and it still applies to a well-formed call.
    resolved = await resolve_spend_ceiling(db_session, user_id=user_id)
    assert (resolved.ceiling_usd_micros, resolved.source) == (1_000_000, "tenant")
    row = (
        await db_session.execute(
            select(CompanionSpendCeiling)
            .where(CompanionSpendCeiling.user_id == user_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert (row.ceiling_usd_micros, row.updated_by_user_id) == (1_000_000, None)
    result = await call_companion_tool(
        LIST_INTEGRATIONS,
        {},
        auth=_platform_minted_auth(seed_user),
        db=db_session,
        deadline_seconds=0,
    )
    assert f"[{CEILING_REASON}]" in result["content"][0]["text"]
