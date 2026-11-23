"""Billing gate coverage (ACCESS-02, D-09 — enforced server-side in 03-06).

"Through the door" means a real subscription object is active (trialing
counts). `ensure_unlocked` consults the /v2 subscription-state read with a
sticky unlock (`users.billing_unlocked_at`), fails CLOSED on outages
(Pitfall 6 — grant-on-outage is the exact failure mode this pins), and
honors D-07 exemptions (`invite_exempted_at`).

Every test stands in for /v2 with an `httpx.MockTransport` injected through
`billing_gate._transport_override` (the 03-04 transport-injection pattern),
pointing `deploy_api_url` at a fake origin. `billing_gate_enabled` defaults
to False — tests flip it on via monkeypatch so self-hosted deployments stay
byte-for-byte unaffected.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import auth as auth_module
from app.core.auth import (
    API_KEY_PREFIX,
    ClerkClaims,
    require_clerk_claims,
)
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.api_key import ApiKey
from app.models.audit import ControlPlaneAuditEvent
from app.models.user import PRINCIPAL_KIND_PARTNER_TENANT, User
from app.services import billing_gate
from app.services.billing_gate import ensure_unlocked

DEPLOY_API_URL = "http://deploy-api.test"
DEPLOY_API_KEY = "test-deploy-key"


class SubscriptionStub:
    """Counting /v2 stand-in: records every request, answers via `responder`."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._responder = responder

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._responder(request)

        return httpx.MockTransport(handler)


def _subscription_response(status_value: str | None, plan_id: str = "compute_basic"):
    def responder(_request: httpx.Request) -> httpx.Response:
        body = (
            {"subscription": None}
            if status_value is None
            else {"subscription": {"status": status_value, "plan_id": plan_id}}
        )
        return httpx.Response(200, json=body)

    return responder


def _error_response(status_code: int):
    def responder(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "boom"})

    return responder


def _connect_error(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch):
    """Enable the gate against the fake /v2 origin, with a clean TTL cache."""
    monkeypatch.setattr(settings, "billing_gate_enabled", True)
    monkeypatch.setattr(settings, "deploy_api_url", DEPLOY_API_URL)
    monkeypatch.setattr(settings, "deploy_api_key", DEPLOY_API_KEY)
    billing_gate.reset_billing_gate_cache()
    yield
    billing_gate.reset_billing_gate_cache()


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: SubscriptionStub) -> None:
    monkeypatch.setattr(billing_gate, "_transport_override", stub.transport())


# ---------------------------------------------------------------------------
# ensure_unlocked — unlock, refusal, outage, exemptions, disabled
# ---------------------------------------------------------------------------


async def test_active_subscription_unlocks_persists_and_sticks(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/v2 active → passes, billing_unlocked_at persisted with a
    billing.unlocked audit event, and a second request makes NO /v2 call."""
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_unlocked_at is not None
    row = (await db_session.execute(select(User).where(User.id == seed_user.id))).scalar_one()
    assert row.billing_unlocked_at is not None
    audit = (
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.action == "billing.unlocked",
                    ControlPlaneAuditEvent.target_user_id == seed_user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1

    # The read hit /v2 exactly once, with the server-to-server credential
    # and the owner named by Clerk user id (06.2 D-09), never the internal UUID.
    assert stub.calls == 1
    request = stub.requests[0]
    assert request.headers["Authorization"] == f"Bearer {DEPLOY_API_KEY}"
    assert seed_user.clerk_id
    assert request.url.params["owner_user_id"] == seed_user.clerk_id
    assert request.url.path == "/v2/subscription"

    # Sticky: the persisted unlock short-circuits before any network I/O.
    await ensure_unlocked(db_session, seed_user)
    assert stub.calls == 1


async def test_trialing_counts_as_active(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-09: trialing counts — same unlock as active."""
    stub = SubscriptionStub(_subscription_response("trialing"))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_unlocked_at is not None
    assert stub.calls == 1


@pytest.mark.parametrize("status_value", [None, "canceled", "past_due"])
async def test_missing_or_inactive_subscription_refused_403(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    status_value: str | None,
) -> None:
    """No subscription / canceled / past_due → 403 {"code": "billing_required"},
    and the negative outcome is cached: an immediate retry makes no /v2 call."""
    stub = SubscriptionStub(_subscription_response(status_value))
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}
    assert seed_user.billing_unlocked_at is None
    assert stub.calls == 1

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 403
    assert stub.calls == 1  # negative TTL cache kept the hot path off /v2


async def test_transport_outage_fails_closed_503_with_negative_cache(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/v2 unreachable → 503 {"code": "billing_unavailable"} — NEVER a grant
    (Pitfall 6). The unavailable outcome is cached for the TTL window."""
    stub = SubscriptionStub(_connect_error)
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {"code": "billing_unavailable"}
    assert seed_user.billing_unlocked_at is None
    assert stub.calls == 1

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 503
    assert stub.calls == 1  # second immediate request inside the 60s TTL


async def test_5xx_from_deploy_api_fails_closed_503(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = SubscriptionStub(_error_response(500))
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {"code": "billing_unavailable"}


async def test_exempt_user_never_gated(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-07: grandfathered/exempt users pass with ZERO /v2 calls."""
    seed_user.invite_exempted_at = datetime.now(UTC)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 0


async def test_already_unlocked_user_makes_no_calls(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.billing_unlocked_at = datetime.now(UTC)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 0


# ---------------------------------------------------------------------------
# INT-1 lapse re-check: one-day grace, then lock (supersedes 06.3 D-03 sticky)
# ---------------------------------------------------------------------------


async def _audit_actions(db_session: AsyncSession, user: User) -> list[str]:
    rows = (
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent)
                .where(
                    ControlPlaneAuditEvent.target_user_id == user.id,
                    ControlPlaneAuditEvent.action.like("billing.%"),
                )
                .order_by(ControlPlaneAuditEvent.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [row.action for row in rows]


def _hours_ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


async def test_recheck_of_paid_unlocked_user_records_the_read(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unlock older than the re-check interval is re-read once; a paid
    answer keeps access and stamps billing_checked_at without a lapse."""
    seed_user.billing_unlocked_at = _hours_ago(2)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 1
    assert seed_user.billing_checked_at is not None
    assert seed_user.billing_lapsed_at is None
    assert seed_user.billing_unlocked_at is not None
    # Inside the interval again: no further read.
    billing_gate.reset_billing_gate_cache()
    await ensure_unlocked(db_session, seed_user)
    assert stub.calls == 1


@pytest.mark.parametrize("status_value", [None, "canceled", "past_due"])
async def test_lapse_is_observed_and_access_continues(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    status_value: str | None,
) -> None:
    seed_user.billing_unlocked_at = _hours_ago(2)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response(status_value))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_unlocked_at is not None
    assert seed_user.billing_lapsed_at is not None
    assert await _audit_actions(db_session, seed_user) == ["billing.lapse_observed"]
    # The read slot defers an immediate retry in this process.
    await ensure_unlocked(db_session, seed_user)
    assert stub.calls == 1


async def test_lapse_inside_the_grace_keeps_access(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.billing_unlocked_at = _hours_ago(48)
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(23)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response("canceled"))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 1
    assert seed_user.billing_unlocked_at is not None
    assert await _audit_actions(db_session, seed_user) == []


async def test_lapse_past_the_one_day_grace_relocks(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlocked_at = _hours_ago(48)
    seed_user.billing_unlocked_at = unlocked_at
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(25)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response("canceled"))
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}

    row = (await db_session.execute(select(User).where(User.id == seed_user.id))).scalar_one()
    assert row.billing_unlocked_at is None
    assert row.billing_lapsed_at is None
    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "billing.relocked",
                ControlPlaneAuditEvent.target_user_id == seed_user.id,
            )
        )
    ).scalar_one()
    assert audit.details["previous_billing_unlocked_at"] == unlocked_at.isoformat()
    assert audit.details["subscription_status"] == "canceled"

    # Locked like any unpaid user: the cached refusal answers without /v2.
    with pytest.raises(HTTPException):
        await ensure_unlocked(db_session, seed_user)
    assert stub.calls == 1


async def test_paying_again_inside_the_grace_clears_the_lapse(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.billing_unlocked_at = _hours_ago(48)
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(12)
    await db_session.flush()
    _install_stub(monkeypatch, SubscriptionStub(_subscription_response("active")))

    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_lapsed_at is None
    assert seed_user.billing_unlocked_at is not None
    assert await _audit_actions(db_session, seed_user) == ["billing.lapse_cleared"]


async def test_relocked_user_unlocks_again_after_paying(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.billing_unlocked_at = _hours_ago(48)
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(30)
    await db_session.flush()
    _install_stub(monkeypatch, SubscriptionStub(_subscription_response(None)))
    with pytest.raises(HTTPException):
        await ensure_unlocked(db_session, seed_user)

    billing_gate.reset_billing_gate_cache()
    _install_stub(monkeypatch, SubscriptionStub(_subscription_response("active")))
    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_unlocked_at is not None
    assert seed_user.billing_checked_at is not None
    assert seed_user.billing_lapsed_at is None


@pytest.mark.parametrize("responder", [_connect_error, _error_response(500)])
async def test_recheck_outage_keeps_access_and_defers_the_retry(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request], httpx.Response],
) -> None:
    """A paid user is not locked out by a /v2 outage during the re-check; the
    retry waits out the 60s window rather than hammering /v2."""
    seed_user.billing_unlocked_at = _hours_ago(2)
    await db_session.flush()
    stub = SubscriptionStub(responder)
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)
    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 1
    assert seed_user.billing_unlocked_at is not None
    assert seed_user.billing_checked_at is None


@pytest.mark.parametrize("responder", [_connect_error, _error_response(500)])
async def test_recheck_outage_past_the_grace_relocks(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request], httpx.Response],
) -> None:
    """The outage tolerance never extends a lapse past its grace: an
    unanswered re-read then relocks, so failing /v2 reads cannot keep access."""
    seed_user.billing_unlocked_at = _hours_ago(48)
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(25)
    await db_session.flush()
    _install_stub(monkeypatch, SubscriptionStub(responder))

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 403

    row = (await db_session.execute(select(User).where(User.id == seed_user.id))).scalar_one()
    assert row.billing_unlocked_at is None
    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "billing.relocked",
                ControlPlaneAuditEvent.target_user_id == seed_user.id,
            )
        )
    ).scalar_one()
    assert audit.details["v2_unavailable"] is True


async def test_recheck_outage_inside_the_grace_keeps_access(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.billing_unlocked_at = _hours_ago(48)
    seed_user.billing_checked_at = _hours_ago(2)
    seed_user.billing_lapsed_at = _hours_ago(12)
    await db_session.flush()
    _install_stub(monkeypatch, SubscriptionStub(_connect_error))

    await ensure_unlocked(db_session, seed_user)

    assert seed_user.billing_unlocked_at is not None


async def test_stale_unlocked_snapshot_follows_the_cleared_row(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An API-key-cache snapshot still carrying an unlock that another worker
    cleared is treated as the locked user the row now is."""
    seed_user.billing_unlocked_at = None
    await db_session.flush()
    snapshot = User(
        id=seed_user.id,
        principal_kind=seed_user.principal_kind,
        clerk_id=seed_user.clerk_id,
        clerk_issuer=seed_user.clerk_issuer,
        email=seed_user.email,
        billing_unlocked_at=_hours_ago(48),
        billing_checked_at=_hours_ago(2),
    )
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, snapshot)
    assert exc_info.value.status_code == 403
    assert snapshot.billing_unlocked_at is None


async def test_principal_without_clerk_id_is_refused_without_a_v2_call(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """06.2 D-09: a principal with no Clerk id (a partner tenant) has no
    hosted owner, so it never reaches hosted billing. With the gate on and no
    exemption it answers 403 billing_required, as `subscription: null` did,
    and /v2 is never called."""
    partner = User(
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        clerk_id=None,
        clerk_issuer=None,
        partner_tenant_ref=f"partner-billing-{test_identity}",
        email=f"partner-billing-{test_identity}@clawdi.local",
    )
    db_session.add(partner)
    await db_session.flush()
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, partner)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}
    assert partner.billing_unlocked_at is None
    assert stub.calls == 0


async def test_gate_disabled_is_a_no_op(
    db_session: AsyncSession,
    seed_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """billing_gate_enabled=false (default) → no /v2 call is ever made, so
    the gate ships dark until clawdi-hosted implements the state read (A2)."""
    billing_gate.reset_billing_gate_cache()
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)

    await ensure_unlocked(db_session, seed_user)

    assert stub.calls == 0
    assert seed_user.billing_unlocked_at is None


# ---------------------------------------------------------------------------
# ensure_unlocked(fresh=True) — the access-status poll skips a stale negative
# outcome after a per-user 3 s minimum interval (06.3 D-03/D-04, T-03-15)
# ---------------------------------------------------------------------------


class _FakeClock:
    """Settable stand-in for `billing_gate.monotonic`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _SwitchableResponder:
    """A /v2 responder the test can repoint between calls."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        return self.responder(request)


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    clock = _FakeClock()
    monkeypatch.setattr(billing_gate, "monotonic", clock)
    return clock


async def _cache_billing_required(
    db_session: AsyncSession,
    user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[SubscriptionStub, _SwitchableResponder]:
    """Lock `user` through one real /v2 read so billing_required is cached."""
    responder = _SwitchableResponder(_subscription_response(None))
    stub = SubscriptionStub(responder)
    _install_stub(monkeypatch, stub)
    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, user)
    assert exc_info.value.status_code == 403
    assert stub.calls == 1
    return stub, responder


async def test_non_fresh_caller_keeps_the_60s_negative_cache(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers without `fresh` answer the cached billing_required even after
    /v2 would now say active: the 60 s cache that protects /v2 is unchanged."""
    stub, responder = await _cache_billing_required(db_session, seed_user, monkeypatch)
    responder.responder = _subscription_response("active")
    fake_clock.advance(10.0)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}
    assert stub.calls == 1


async def test_fresh_read_after_min_interval_unlocks_past_cached_refusal(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fresh=True ignores a cached billing_required once 3 s have passed since
    the user's last /v2 read: one new /v2 call, sticky unlock, one audit row."""
    stub, responder = await _cache_billing_required(db_session, seed_user, monkeypatch)
    responder.responder = _subscription_response("active")
    fake_clock.advance(3.0)

    await ensure_unlocked(db_session, seed_user, fresh=True)

    assert stub.calls == 2
    row = (await db_session.execute(select(User).where(User.id == seed_user.id))).scalar_one()
    assert row.billing_unlocked_at is not None
    audit = (
        (
            await db_session.execute(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.action == "billing.unlocked",
                    ControlPlaneAuditEvent.target_user_id == seed_user.id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(audit) == 1


async def test_fresh_read_inside_min_interval_answers_cached_outcome(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the 3 s window fresh=True answers the cached outcome without a
    /v2 call, so a tight poll cannot hammer /v2 (T-03-15)."""
    stub, responder = await _cache_billing_required(db_session, seed_user, monkeypatch)
    responder.responder = _subscription_response("active")
    fake_clock.advance(2.9)

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user, fresh=True)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}
    assert stub.calls == 1


async def test_fresh_read_rereads_past_cached_unavailable(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cached billing_unavailable is re-read after 3 s on the fresh path;
    with no subscription the answer is 403 billing_required, never a grant."""
    responder = _SwitchableResponder(_connect_error)
    stub = SubscriptionStub(responder)
    _install_stub(monkeypatch, stub)
    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user)
    assert exc_info.value.status_code == 503
    assert stub.calls == 1

    responder.responder = _subscription_response(None)
    fake_clock.advance(3.0)
    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, seed_user, fresh=True)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"code": "billing_required"}
    assert stub.calls == 2
    assert seed_user.billing_unlocked_at is None


class _YieldingSession:
    """Stand-in for the AsyncSession that yields to the event loop inside
    `get`, as a real session does when it must load the row (WR-01)."""

    def __init__(self, user: User) -> None:
        self._user = user

    async def get(self, _model: type[User], _ident: object) -> User:
        await asyncio.sleep(0)
        return self._user


async def test_concurrent_fresh_reads_make_one_v2_call(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A burst of concurrent fresh polls from one locked user makes one /v2
    read, not one per poll: the read slot is claimed before the first await,
    so the yield inside `db.get` cannot let every poll pass the "due" check."""
    stub, _responder = await _cache_billing_required(db_session, seed_user, monkeypatch)
    fake_clock.advance(3.0)
    session = _YieldingSession(seed_user)

    results = await asyncio.gather(
        *(ensure_unlocked(session, seed_user, fresh=True) for _ in range(8)),  # type: ignore[arg-type]
        return_exceptions=True,
    )

    assert all(isinstance(result, HTTPException) for result in results)
    assert {result.status_code for result in results} == {403}  # type: ignore[union-attr]
    assert stub.calls == 2


async def test_reset_clears_negative_cache_and_last_read_times(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    fake_clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset_billing_gate_cache() clears both the negative cache and the
    per-user last /v2 read times."""
    await _cache_billing_required(db_session, seed_user, monkeypatch)
    last_reads = getattr(billing_gate, "_last_v2_read", {})
    assert seed_user.id in last_reads
    assert seed_user.id in billing_gate._negative_cache

    billing_gate.reset_billing_gate_cache()

    assert billing_gate._negative_cache == {}
    assert getattr(billing_gate, "_last_v2_read", {}) == {}


# ---------------------------------------------------------------------------
# Auth-path enforcement — the gate holds on every authenticated surface
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _real_auth_client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Client whose requests run the REAL get_auth chain (no auth override),
    with only the DB session pinned to the test's rollback session."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_api_key(db_session: AsyncSession, user: User) -> str:
    raw = f"{API_KEY_PREFIX}{secrets.token_hex(24)}"
    db_session.add(
        ApiKey(
            user_id=user.id,
            key_hash=sha256(raw.encode()).hexdigest(),
            key_prefix=raw[:12],
            label="billing-gate-test",
        )
    )
    await db_session.commit()
    return raw


async def test_api_key_auth_path_identifies_but_refuses_locked_user(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """06.3 D-03: a provisioned but unpaid user is identified by GET
    /v1/auth/me (the one billing-exempt route) without any /v2 call, and is
    refused 403 billing_required on every other route on the API-key path, so
    the gate still closes CLI and MCP."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    raw_key = await _seed_api_key(db_session, seed_user)
    headers = {"Authorization": f"Bearer {raw_key}"}
    async with _real_auth_client(db_session) as client:
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["id"] == str(seed_user.id)
        assert stub.calls == 0

        refused = await client.get("/v1/agents", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {"code": "billing_required"}
    assert stub.calls == 1


async def test_api_key_auth_path_passes_after_unlock(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unlock persists through the auth path: the first gated request unlocks
    via /v2, the next one passes without any further /v2 call."""
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)
    raw_key = await _seed_api_key(db_session, seed_user)
    headers = {"Authorization": f"Bearer {raw_key}"}
    async with _real_auth_client(db_session) as client:
        first = await client.get("/v1/agents", headers=headers)
        assert first.status_code == 200, first.text
        assert stub.calls == 1
        second = await client.get("/v1/agents", headers=headers)
        assert second.status_code == 200, second.text
        assert stub.calls == 1


async def test_api_key_revoked_during_the_gate_commit_is_refused(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gate's commit releases the authority lock; the API-key path takes it
    again and rereads the key, so a revoke that lands then refuses the request
    instead of letting the route run on the old authority."""
    seed_user.billing_unlocked_at = datetime.now(UTC) - timedelta(hours=2)
    await db_session.commit()
    raw_key = await _seed_api_key(db_session, seed_user)
    _install_stub(monkeypatch, SubscriptionStub(_subscription_response("active")))
    real_gate = auth_module.ensure_unlocked

    async def gate_then_revoke(db: AsyncSession, user: User, **kwargs: object) -> None:
        await real_gate(db, user, **kwargs)
        key = (await db.execute(select(ApiKey).where(ApiKey.user_id == user.id))).scalar_one()
        key.revoked_at = datetime.now(UTC)
        await db.commit()

    monkeypatch.setattr(auth_module, "ensure_unlocked", gate_then_revoke)
    async with _real_auth_client(db_session) as client:
        resp = await client.get("/v1/agents", headers={"Authorization": f"Bearer {raw_key}"})

    assert resp.status_code == 401, resp.text


async def test_dev_bypass_path_identifies_but_refuses_locked_user(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """The interactive (non-API-key) resolver is gated the same way: a fresh
    dev principal is provisioned and identified by GET /v1/auth/me, and every
    other route answers 403 billing_required."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"dev_billing_{test_identity}"
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "dev_auth_token", f"dev-{test_identity}")
    monkeypatch.setattr(settings, "dev_auth_clerk_id", clerk_id)
    headers = {"Authorization": f"Bearer dev-{test_identity}"}

    async with _real_auth_client(db_session) as client:
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        row = (
            await db_session.execute(select(User).where(User.clerk_id == clerk_id))
        ).scalar_one_or_none()
        assert row is not None
        assert me.json()["id"] == str(row.id)

        refused = await client.get("/v1/agents", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["detail"] == {"code": "billing_required"}
    assert stub.calls == 1


# ---------------------------------------------------------------------------
# GET /v1/access/status — the post-checkout poll path stays un-gated but
# attempts a live unlock for billing_required users
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _claims_client(
    db_session: AsyncSession, claims: ClerkClaims
) -> AsyncIterator[httpx.AsyncClient]:
    """Client authenticated as claims-only, mirroring test_access_status.py."""

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _override_claims() -> ClerkClaims:
        return claims

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[require_clerk_claims] = _override_claims
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _claims_for(user: User) -> ClerkClaims:
    assert user.clerk_id is not None
    return ClerkClaims(
        subject=user.clerk_id, issuer=None, email=user.email, name=None, avatar_url=None
    )


async def test_access_status_flips_locked_user_to_provisioned(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-checkout poll: a locked user whose /v2 subscription reads
    active gets `provisioned` AND the unlock persisted."""
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)

    async with _claims_client(db_session, _claims_for(seed_user)) as client:
        response = await client.get("/v1/access/status")

    assert response.status_code == 200
    assert response.json() == {"state": "provisioned"}
    row = (await db_session.execute(select(User).where(User.id == seed_user.id))).scalar_one()
    assert row.billing_unlocked_at is not None


async def test_access_status_stays_billing_required_without_subscription(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 403 is caught into the billing_required state — the status route
    reports, it does not refuse."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)

    async with _claims_client(db_session, _claims_for(seed_user)) as client:
        response = await client.get("/v1/access/status")

    assert response.status_code == 200
    assert response.json() == {"state": "billing_required"}


async def test_access_status_propagates_outage_as_503(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outage during the status poll fails closed — the 503 propagates so the
    client renders billing_unavailable, never a false `provisioned`."""
    stub = SubscriptionStub(_connect_error)
    _install_stub(monkeypatch, stub)

    async with _claims_client(db_session, _claims_for(seed_user)) as client:
        response = await client.get("/v1/access/status")

    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "billing_unavailable"}


async def test_access_status_exempt_user_is_provisioned_without_calls(
    db_session: AsyncSession,
    seed_user: User,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_user.invite_exempted_at = datetime.now(UTC)
    await db_session.commit()
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)

    async with _claims_client(db_session, _claims_for(seed_user)) as client:
        response = await client.get("/v1/access/status")

    assert response.status_code == 200
    assert response.json() == {"state": "provisioned"}
    assert stub.calls == 0


# ---------------------------------------------------------------------------
# Boundary hygiene
# ---------------------------------------------------------------------------


def test_no_stripe_import_in_billing_gate() -> None:
    """ADR-0005 boundary: the gate reads /v2 state only — Stripe stays
    hosted-side. Mirrors the plan's repo-wide grep for `import stripe` /
    `from stripe` (prose mentions of the boundary are fine; imports are not)."""
    import inspect

    source = inspect.getsource(billing_gate)
    for line in source.splitlines():
        stripped = line.strip()
        assert not stripped.startswith("import stripe"), line
        assert not stripped.startswith("from stripe"), line


def test_settings_expose_deploy_api_vocabulary() -> None:
    """Generic names (not billing-specific) — 03-08's purge worker reuses
    deploy_api_url / deploy_api_key for DELETE /v2/deployments/{id}."""
    assert hasattr(settings, "deploy_api_url")
    assert hasattr(settings, "deploy_api_key")
    field_defaults = {name: field.default for name, field in type(settings).model_fields.items()}
    assert field_defaults.get("deploy_api_url") == ""
    assert field_defaults.get("deploy_api_key") == ""
