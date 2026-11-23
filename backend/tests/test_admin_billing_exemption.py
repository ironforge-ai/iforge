"""Explicit operator billing exemption (06.3 D-01, ROADMAP SC-5, WR-02).

`PUT /v1/admin/auth/billing-exemptions` is the only way to exempt a Clerk user
from the billing gate. Admin calls that merely create the user row (hosted
deploy orchestration) leave it gated.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.user import User
from app.schemas.admin_tenant_lifecycle import AdminBillingExemptionUpdate
from app.services import billing_gate
from app.services.billing_gate import ensure_unlocked
from tests.test_billing_gate import (
    DEPLOY_API_KEY,
    DEPLOY_API_URL,
    SubscriptionStub,
    _install_stub,
    _subscription_response,
)

pytestmark = pytest.mark.asyncio

_ISSUER = "https://billing-exemption.clerk.example.test"
_ADMIN_KEY = "billing-exemption-admin-test"
_AUTH = {"X-Admin-Key": _ADMIN_KEY}


@pytest.fixture(autouse=True)
def _exemption_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "clerk_jwt_issuer", _ISSUER)
    monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)
    monkeypatch.setattr(settings, "billing_gate_enabled", True)
    monkeypatch.setattr(settings, "deploy_api_url", DEPLOY_API_URL)
    monkeypatch.setattr(settings, "deploy_api_key", DEPLOY_API_KEY)
    billing_gate.reset_billing_gate_cache()
    yield
    billing_gate.reset_billing_gate_cache()


async def _set_exemption(client, *, subject: str, exempt: bool, reason: str = "operator test"):
    return await client.put(
        "/v1/admin/auth/billing-exemptions",
        headers=_AUTH,
        json={"target_clerk_id": subject, "exempt": exempt, "reason": reason},
    )


async def _load_user(db: AsyncSession, clerk_id: str) -> User:
    db.expire_all()
    return (await db.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one()


# WR-01 (06.3 review-fix iteration 3): no ORDER BY. `created_at` is
# `server_default=now()`, which is the transaction start time, and every test
# runs inside one outer transaction (conftest `db_session`). So all audit rows
# a test writes share one `created_at`, and any order over it is arbitrary.
# These helpers are order-free: a sorted multiset of (action, changed) and a
# per-action bucket of details.
async def _audit_rows(db: AsyncSession, user_id) -> list[ControlPlaneAuditEvent]:
    return list(
        (
            await db.execute(
                select(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.target_user_id == user_id,
                    ControlPlaneAuditEvent.action.like("billing.exempt_%"),
                )
            )
        ).scalars()
    )


async def _audit_actions(db: AsyncSession, user_id) -> list[tuple[str, object]]:
    rows = await _audit_rows(db, user_id)
    return sorted((row.action, row.details["changed"]) for row in rows)


async def _audit_details(db: AsyncSession, user_id) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for row in await _audit_rows(db, user_id):
        out.setdefault(row.action, []).append(dict(row.details))
    return out


_PAID_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


async def _seed_unlock(db: AsyncSession, clerk_id: str) -> User:
    """Stamp a sticky unlock on an existing row, as a past /v2 read would."""
    user = await _load_user(db, clerk_id)
    user.billing_unlocked_at = _PAID_AT
    await db.commit()
    return await _load_user(db, clerk_id)


async def _create_gated_user(client, db: AsyncSession, clerk_id: str) -> User:
    """Create a non-exempt row the way hosted orchestration does."""
    created = await client.post(
        "/v1/admin/auth/keys",
        headers=_AUTH,
        json={"target_clerk_id": clerk_id, "label": "deploy"},
    )
    assert created.status_code in (200, 201), created.text
    user = await _load_user(db, clerk_id)
    assert user.invite_exempted_at is None
    return user


async def _gate_refusal(db: AsyncSession, user: User) -> object:
    billing_gate.reset_billing_gate_cache()
    try:
        await ensure_unlocked(db, user)
    except HTTPException as exc:
        return exc.detail
    return None


async def test_requires_the_admin_key(anon_client, test_identity: str) -> None:
    resp = await anon_client.put(
        "/v1/admin/auth/billing-exemptions",
        json={"target_clerk_id": f"user_{test_identity}", "exempt": True, "reason": "x"},
    )
    assert resp.status_code in (401, 403)


async def test_grant_creates_an_exempt_user_and_revoke_regates(
    anon_client,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """An operator can exempt a Clerk id before its first sign-in; the gate
    then skips /v2. Revoking puts the user back behind the /v2 read."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"user_exempt_{test_identity}"

    granted = await _set_exemption(anon_client, subject=clerk_id, exempt=True)
    assert granted.status_code == 200, granted.text
    body = granted.json()
    assert body["exempt"] is True
    assert body["changed"] is True
    assert body["exempted_at"] is not None

    user = await _load_user(db_session, clerk_id)
    assert str(user.id) == body["user_id"]
    assert user.invite_exempted_at is not None
    assert await _gate_refusal(db_session, user) is None
    assert stub.calls == 0

    lookup = await anon_client.get("/v1/admin/users", headers=_AUTH, params={"q": clerk_id})
    assert lookup.status_code == 200, lookup.text
    assert [item["billing_exempt"] for item in lookup.json()["items"]] == [True]

    again = await _set_exemption(anon_client, subject=clerk_id, exempt=True)
    assert again.status_code == 200, again.text
    assert again.json()["changed"] is False

    revoked = await _set_exemption(anon_client, subject=clerk_id, exempt=False)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json() | {"user_id": None} == {
        "target_clerk_id": clerk_id,
        "user_id": None,
        "exempt": False,
        "exempted_at": None,
        "changed": True,
    }

    user = await _load_user(db_session, clerk_id)
    assert user.invite_exempted_at is None
    lookup = await anon_client.get("/v1/admin/users", headers=_AUTH, params={"q": clerk_id})
    assert [item["billing_exempt"] for item in lookup.json()["items"]] == [False]
    assert await _gate_refusal(db_session, user) is not None
    assert stub.calls == 1

    assert await _audit_actions(db_session, user.id) == sorted(
        [
            ("billing.exempt_granted", True),
            ("billing.exempt_granted", False),
            ("billing.exempt_revoked", True),
        ]
    )


async def test_existing_gated_user_can_be_exempted(
    anon_client, db_session: AsyncSession, test_identity: str
) -> None:
    """A row an orchestration call created first (not exempt) can still be
    exempted later; the order of first touch no longer decides it."""
    clerk_id = f"user_orchestrated_{test_identity}"
    created = await anon_client.post(
        "/v1/admin/auth/keys",
        headers=_AUTH,
        json={"target_clerk_id": clerk_id, "label": "deploy"},
    )
    assert created.status_code in (200, 201), created.text
    user = await _load_user(db_session, clerk_id)
    assert user.invite_exempted_at is None

    granted = await _set_exemption(anon_client, subject=clerk_id, exempt=True)
    assert granted.status_code == 200, granted.text
    assert granted.json()["changed"] is True
    user = await _load_user(db_session, clerk_id)
    assert user.invite_exempted_at is not None


async def test_revoke_clears_the_sticky_unlock_and_regates(
    anon_client,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """G-06.3-R2-4 / WR-01 Option A: a revoke also clears the sticky unlock,
    so the next gated request re-reads /v2 instead of short-circuiting on an
    old `billing_unlocked_at`."""
    clerk_id = f"user_paid_exempt_{test_identity}"
    granted = await _set_exemption(anon_client, subject=clerk_id, exempt=True)
    assert granted.status_code == 200, granted.text
    await _seed_unlock(db_session, clerk_id)

    revoked = await _set_exemption(anon_client, subject=clerk_id, exempt=False)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["changed"] is True

    user = await _load_user(db_session, clerk_id)
    assert user.invite_exempted_at is None
    assert user.billing_unlocked_at is None

    details = await _audit_details(db_session, user.id)
    assert sorted(details) == ["billing.exempt_granted", "billing.exempt_revoked"]
    assert len(details["billing.exempt_revoked"]) == 1
    assert details["billing.exempt_revoked"][0].get("billing_unlock_cleared") is True
    # WR-03: the cleared value survives in the audit row for a manual restore.
    assert details["billing.exempt_revoked"][0].get("previous_billing_unlocked_at") == (
        _PAID_AT.isoformat()
    )
    assert details["billing.exempt_granted"][0].get("previous_billing_unlocked_at") is None

    no_subscription = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, no_subscription)
    assert await _gate_refusal(db_session, user) == {"code": "billing_required"}
    assert no_subscription.calls == 1

    active = SubscriptionStub(_subscription_response("active"))
    _install_stub(monkeypatch, active)
    user = await _load_user(db_session, clerk_id)
    assert await _gate_refusal(db_session, user) is None
    assert active.calls == 1
    user = await _load_user(db_session, clerk_id)
    assert user.billing_unlocked_at is not None


async def test_unchanged_revoke_keeps_a_paying_users_unlock(
    anon_client, db_session: AsyncSession, test_identity: str
) -> None:
    """A revoke that changes nothing (the user was never exempt) must not
    reset a paying user's sticky unlock."""
    clerk_id = f"user_paying_{test_identity}"
    await _create_gated_user(anon_client, db_session, clerk_id)
    await _seed_unlock(db_session, clerk_id)

    revoked = await _set_exemption(anon_client, subject=clerk_id, exempt=False)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["changed"] is False

    user = await _load_user(db_session, clerk_id)
    assert user.billing_unlocked_at == _PAID_AT
    details = await _audit_details(db_session, user.id)
    assert sorted(details) == ["billing.exempt_revoked"]
    assert len(details["billing.exempt_revoked"]) == 1
    assert details["billing.exempt_revoked"][0].get("billing_unlock_cleared") is False
    assert details["billing.exempt_revoked"][0].get("previous_billing_unlocked_at") is None


async def test_grant_leaves_the_unlock_untouched(
    anon_client, db_session: AsyncSession, test_identity: str
) -> None:
    """A grant never touches the sticky unlock, and its audit row says so."""
    clerk_id = f"user_paying_grant_{test_identity}"
    await _create_gated_user(anon_client, db_session, clerk_id)
    await _seed_unlock(db_session, clerk_id)

    granted = await _set_exemption(anon_client, subject=clerk_id, exempt=True)
    assert granted.status_code == 200, granted.text
    assert granted.json()["changed"] is True

    user = await _load_user(db_session, clerk_id)
    assert user.invite_exempted_at is not None
    assert user.billing_unlocked_at == _PAID_AT
    details = await _audit_details(db_session, user.id)
    assert sorted(details) == ["billing.exempt_granted"]
    assert len(details["billing.exempt_granted"]) == 1
    assert details["billing.exempt_granted"][0].get("billing_unlock_cleared") is False
    assert details["billing.exempt_granted"][0].get("previous_billing_unlocked_at") is None


@pytest.mark.parametrize("reason", ["", " padded ", "line\nbreak"])
async def test_rejects_a_non_canonical_reason(anon_client, test_identity: str, reason: str) -> None:
    resp = await _set_exemption(
        anon_client, subject=f"user_reason_{test_identity}", exempt=True, reason=reason
    )
    assert resp.status_code == 422


# WR-05 (06.3-21): the shared reason fixture. The web test
# (apps/web/src/pages/dashboard/admin/billing-exemption-section.test.tsx)
# reads the same billing_exemption_reason_cases.json, so the admin panel and
# this schema are pinned to one set of cases.
_REASON_CASES_PATH = Path(__file__).parent / "fixtures" / "billing_exemption_reason_cases.json"
_REASON_MAX = 191


def _reason_cases() -> list[tuple[str, str, bool]]:
    data = json.loads(_REASON_CASES_PATH.read_text(encoding="utf-8"))
    cases: list[tuple[str, str, bool]] = []
    for case in data["cases"]:
        if "repeat" in case:
            reason = case["repeat"]["text"] * case["repeat"]["times"]
        else:
            reason = case["reason"]
        cases.append((case["id"], reason, case["valid"]))
    return cases


_REASON_CASES = _reason_cases()


async def test_reason_fixture_is_well_formed() -> None:
    assert _REASON_CASES, "billing_exemption_reason_cases.json has no cases"
    ids = [case_id for case_id, _, _ in _REASON_CASES]
    assert len(ids) == len(set(ids)), "billing_exemption_reason_cases.json repeats an id"
    assert any(not valid and not reason.isascii() for _, reason, valid in _REASON_CASES), (
        "the fixture needs at least one invalid non-ASCII case"
    )


@pytest.mark.parametrize(
    ("reason", "valid"),
    [(reason, valid) for _, reason, valid in _REASON_CASES],
    ids=[case_id for case_id, _, _ in _REASON_CASES],
)
async def test_reason_fixture_matches_pythons_rule(reason: str, valid: bool) -> None:
    """Guards the fixture itself: each expectation is Python's own rule."""
    assert valid == (
        reason == reason.strip() and reason.isprintable() and 1 <= len(reason) <= _REASON_MAX
    )


@pytest.mark.parametrize(
    ("reason", "valid"),
    [(reason, valid) for _, reason, valid in _REASON_CASES],
    ids=[case_id for case_id, _, _ in _REASON_CASES],
)
async def test_schema_agrees_with_the_reason_fixture(reason: str, valid: bool) -> None:
    payload = {"target_clerk_id": "user_2fExemptionFixture", "exempt": True, "reason": reason}
    try:
        AdminBillingExemptionUpdate.model_validate(payload)
    except ValidationError:
        accepted = False
    else:
        accepted = True
    assert accepted is valid


async def test_route_rejects_an_nbsp_reason(anon_client, test_identity: str) -> None:
    resp = await _set_exemption(
        anon_client,
        subject=f"user_nbsp_{test_identity}",
        exempt=True,
        reason="design\u00a0partner",
    )
    assert resp.status_code == 422
