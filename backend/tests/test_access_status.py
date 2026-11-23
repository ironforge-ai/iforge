"""GET /access/status: the claims-only access read (06.3 D-01, D-03, D-04).

The route runs on `require_clerk_claims`, which verifies a Clerk JWT without
provisioning, so it can answer for identities that have no `users` row yet.
It answers exactly one of `provisioned`, `billing_required` or `terminated`
and never creates a row (T-06.3-07-02).

- `terminated` keeps precedence over every other answer: the fence check runs
  before the user check, for a missing row and a surviving row alike
  (T-06.3-07-05).
- An identity with no row answers `billing_required` while the billing gate
  is on (RESEARCH Pitfall 4, T-06.3-07-01) and `provisioned` while it is off.
- For a provisioned unpaid user the poll calls `ensure_unlocked(fresh=True)`,
  so a completed checkout flips the answer within one poll even after the
  user's own gated requests cached `billing_required` (D-04).

The redeem endpoint that shared this router is gone (D-01), and the
claims-only route set is pinned to the two access-status paths
(T-06.3-07-03).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from httpx import ASGITransport
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import ClerkClaims, require_clerk_claims
from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.principal_lifecycle import ClerkPrincipalAuthority, PrincipalLifecycle
from app.models.user import User
from app.services import billing_gate
from app.services.billing_gate import ensure_unlocked
from app.services.principal_lifecycle import fence_principal_deleted
from tests.route_tree import dependency_calls, iter_api_routes

DEPLOY_API_URL = "http://deploy-api.test"
DEPLOY_API_KEY = "test-deploy-key"
_MIN_API_ROUTES = 400
_EXPECTED_CLAIMS_ONLY = {("GET", "/v1/access/status"), ("GET", "/api/access/status")}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _claims_client(
    db_session: AsyncSession, claims: ClerkClaims
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client authenticated as a claims-only identity.

    Overrides `require_clerk_claims`, the dependency that verifies a Clerk JWT
    WITHOUT provisioning, so the access-status route sees an
    authenticated-but-possibly-row-less identity, exactly like a fresh
    sign-up. Cleanup relies on conftest's autouse
    `_restore_dependency_overrides`.
    """

    async def _override_get_session() -> AsyncIterator[AsyncSession]:
        yield db_session

    async def _override_claims() -> ClerkClaims:
        return claims

    app.dependency_overrides[get_session] = _override_get_session
    app.dependency_overrides[require_clerk_claims] = _override_claims
    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _claims(subject: str, issuer: str | None = None) -> ClerkClaims:
    return ClerkClaims(subject=subject, issuer=issuer, email=None, name=None, avatar_url=None)


class SubscriptionStub:
    """Counting /v2 stand-in whose responder the test can repoint."""

    def __init__(self, responder: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self.responder = responder

    @property
    def calls(self) -> int:
        return len(self.requests)

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self.responder(request)

        return httpx.MockTransport(handler)


def _subscription_response(status_value: str | None):
    def responder(_request: httpx.Request) -> httpx.Response:
        body = (
            {"subscription": None}
            if status_value is None
            else {"subscription": {"status": status_value, "plan_id": "compute_basic"}}
        )
        return httpx.Response(200, json=body)

    return responder


def _connect_error(_request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused")


class _FakeClock:
    """Settable stand-in for `billing_gate.monotonic`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def gate_on(monkeypatch: pytest.MonkeyPatch):
    """Enable the billing gate against a fake /v2 origin, with a clean cache."""
    monkeypatch.setattr(settings, "billing_gate_enabled", True)
    monkeypatch.setattr(settings, "deploy_api_url", DEPLOY_API_URL)
    monkeypatch.setattr(settings, "deploy_api_key", DEPLOY_API_KEY)
    billing_gate.reset_billing_gate_cache()
    yield
    billing_gate.reset_billing_gate_cache()


@pytest.fixture
def gate_off(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "billing_gate_enabled", False)


def _install_stub(monkeypatch: pytest.MonkeyPatch, stub: SubscriptionStub) -> None:
    monkeypatch.setattr(billing_gate, "_transport_override", stub.transport())


async def _row_for(db: AsyncSession, clerk_id: str) -> User | None:
    return (await db.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Unprovisioned identities (no users row)
# ---------------------------------------------------------------------------


async def test_unprovisioned_identity_with_gate_on_is_billing_required(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """Pitfall 4: an identity with no row is not `provisioned` while the gate
    is on. The answer is billing_required, no /v2 call is made and no row is
    created."""
    stub = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_new_gated_{test_identity}"

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "billing_required"}
    assert stub.calls == 0
    assert await _row_for(db_session, clerk_id) is None


async def test_unprovisioned_identity_with_gate_off_is_provisioned(
    db_session: AsyncSession, gate_off: None, test_identity: str
) -> None:
    clerk_id = f"status_new_open_{test_identity}"

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "provisioned"}
    assert await _row_for(db_session, clerk_id) is None


# ---------------------------------------------------------------------------
# terminated keeps precedence
# ---------------------------------------------------------------------------


async def test_fenced_identity_without_row_is_terminated(
    db_session: AsyncSession, gate_on: None, test_identity: str
) -> None:
    """A fenced identity with no row reads terminated, not billing_required."""
    clerk_id = f"status_fenced_{test_identity}"
    now = datetime.now(UTC)
    db_session.add(
        PrincipalLifecycle(
            issuer="https://clerk.fence.example",
            subject=clerk_id,
            terminated_at=now,
            next_cleanup_attempt_at=now,
        )
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "terminated"}


async def test_fenced_user_with_surviving_row_is_terminated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_identity: str
) -> None:
    """`fence_principal_deleted` does NOT delete the `users` row -- only the
    purge sweep does, a grace window later -- so for the entire window the
    fenced tenant IS a provisioned user with a live row. Checking the fence
    only on the row-absent branch would answer `provisioned` here, leaving the
    S2 deactivated screen unreachable for exactly the population it serves."""
    issuer = "https://clerk.surviving.example"
    monkeypatch.setattr(settings, "clerk_jwt_issuer", issuer)
    clerk_id = f"status_fenced_live_{test_identity}"
    user = User(clerk_id=clerk_id, clerk_issuer=issuer, email=None, name=None)
    db_session.add(user)
    await db_session.flush()

    now = datetime.now(UTC)
    lifecycle, created = await fence_principal_deleted(
        db_session,
        issuer=issuer,
        subject=clerk_id,
        occurred_at=now,
        now=now,
    )
    await db_session.commit()

    assert created is True
    # The fence binds the surviving user; the row must still be there, or this
    # collapses into the row-absent case above and proves nothing.
    assert lifecycle.user_id == user.id
    assert await db_session.get(User, user.id) is not None

    async with _claims_client(db_session, _claims(clerk_id, issuer)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "terminated"}
    # access/status reports; it never provisions or reaps.
    assert await db_session.get(User, user.id) is not None


async def test_banned_authority_is_terminated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_identity: str
) -> None:
    """A reversible Clerk ban (no lifecycle tombstone) also reads terminated."""
    issuer = "https://clerk.banned.example"
    monkeypatch.setattr(settings, "clerk_jwt_issuer", issuer)
    clerk_id = f"status_banned_{test_identity}"
    now = datetime.now(UTC)
    db_session.add(User(clerk_id=clerk_id, clerk_issuer=issuer, email=None, name=None))
    db_session.add(
        ClerkPrincipalAuthority(
            issuer=issuer,
            subject=clerk_id,
            banned=True,
            authority_updated_at=now,
            message_id=f"msg_{test_identity}",
            payload_sha256="0" * 64,
        )
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id, issuer)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "terminated"}


async def test_user_bound_fence_under_drifted_issuer_is_terminated(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_identity: str
) -> None:
    """The fence is bound to `user_id` but was written under a previously
    configured issuer, so the (issuer, subject) lookup misses it.
    `assert_user_authority_active` keys on the user and still refuses -- the
    same authority the auth paths enforce, reported by access/status."""
    current_issuer = "https://clerk.current.example"
    stale_issuer = "https://clerk.previous.example"
    monkeypatch.setattr(settings, "clerk_jwt_issuer", current_issuer)
    clerk_id = f"status_drift_{test_identity}"
    user = User(clerk_id=clerk_id, clerk_issuer=current_issuer, email=None, name=None)
    db_session.add(user)
    await db_session.flush()

    now = datetime.now(UTC)
    db_session.add(
        PrincipalLifecycle(
            issuer=stale_issuer,
            subject=clerk_id,
            user_id=user.id,
            terminated_at=now,
            next_cleanup_attempt_at=now,
        )
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id, current_issuer)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "terminated"}


# ---------------------------------------------------------------------------
# Provisioned users
# ---------------------------------------------------------------------------


async def test_exempt_user_is_provisioned_without_v2_calls(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_exempt_{test_identity}"
    db_session.add(
        User(clerk_id=clerk_id, email=None, name=None, invite_exempted_at=datetime.now(UTC))
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "provisioned"}
    assert stub.calls == 0


async def test_unlocked_user_is_provisioned_without_v2_calls(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_unlocked_{test_identity}"
    db_session.add(
        User(clerk_id=clerk_id, email=None, name=None, billing_unlocked_at=datetime.now(UTC))
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "provisioned"}
    assert stub.calls == 0


async def test_lapsed_user_past_the_grace_reports_billing_required(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """INT-1: the status poll runs the lapse re-check, so it agrees with the
    gated routes once a lapse outlasts the one-day grace."""
    stub = SubscriptionStub(_subscription_response("canceled"))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_lapsed_{test_identity}"
    now = datetime.now(UTC)
    db_session.add(
        User(
            clerk_id=clerk_id,
            email=None,
            name=None,
            billing_unlocked_at=now - timedelta(days=40),
            billing_checked_at=now - timedelta(hours=2),
            billing_lapsed_at=now - timedelta(hours=25),
        )
    )
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "billing_required"}
    assert stub.calls == 1


async def test_unpaid_user_without_subscription_is_billing_required(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """The /v2 403 is caught into the billing_required state: the status route
    reports, it does not refuse."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_unpaid_{test_identity}"
    db_session.add(User(clerk_id=clerk_id, email=None, name=None))
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "billing_required"}
    assert stub.calls == 1


async def test_poll_reads_v2_fresh_past_a_cached_refusal(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """D-04: the user's own gated requests cached billing_required; the
    checkout then completes. Once the 3 s per-user interval has passed, the
    next poll re-reads /v2, answers provisioned and persists the unlock."""
    clock = _FakeClock()
    monkeypatch.setattr(billing_gate, "monotonic", clock)
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_fresh_{test_identity}"
    user = User(clerk_id=clerk_id, email=None, name=None)
    db_session.add(user)
    await db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await ensure_unlocked(db_session, user)
    assert exc_info.value.status_code == 403
    assert stub.calls == 1

    stub.responder = _subscription_response("active")
    clock.advance(3.0)

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"state": "provisioned"}
    assert stub.calls == 2
    row = await _row_for(db_session, clerk_id)
    assert row is not None
    await db_session.refresh(row)
    assert row.billing_unlocked_at is not None


async def test_v2_outage_propagates_billing_unavailable(
    db_session: AsyncSession,
    gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """Fail closed: an outage during the poll propagates as 503, never a false
    `provisioned`."""
    stub = SubscriptionStub(_connect_error)
    _install_stub(monkeypatch, stub)
    clerk_id = f"status_outage_{test_identity}"
    db_session.add(User(clerk_id=clerk_id, email=None, name=None))
    await db_session.commit()

    async with _claims_client(db_session, _claims(clerk_id)) as ac:
        resp = await ac.get("/v1/access/status")

    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"] == {"code": "billing_unavailable"}


# ---------------------------------------------------------------------------
# Route surface
# ---------------------------------------------------------------------------


async def test_invite_redeem_route_is_gone(db_session: AsyncSession, test_identity: str) -> None:
    """D-01: the redeem endpoint is removed on both prefixes and from OpenAPI."""
    async with _claims_client(db_session, _claims(f"redeem_gone_{test_identity}")) as ac:
        canonical = await ac.post("/v1/invites/redeem", json={})
        alias = await ac.post("/api/invites/redeem", json={})

    assert canonical.status_code == 404, canonical.text
    assert alias.status_code == 404, alias.text
    assert not [path for path in app.openapi()["paths"] if "/invites" in path]


def test_claims_only_routes_are_exactly_access_status() -> None:
    """T-06.3-07-03: a route on the claims-only dependency answers for
    identities with no row and skips the billing gate. Widening this set is a
    policy change made in review, never a convenience."""
    routes = list(iter_api_routes())
    assert len(routes) >= _MIN_API_ROUTES, (
        f"route walk found only {len(routes)} API routes; the lazy include "
        "wrappers were probably not expanded"
    )
    claims_only = {
        (method, path)
        for path, route in routes
        if require_clerk_claims in dependency_calls(route.dependant)
        for method in route.methods
    }
    assert claims_only == _EXPECTED_CLAIMS_ONLY
