"""The new-user billing path end to end, in process (06.3 SC-5, D-04, ACCESS-01..03).

A brand-new Clerk identity goes from its first verified session to a paid plan to an unlocked
account, with no deploy and no invite:

1. GET /v1/access/status reads ``billing_required`` before any ``users`` row exists.
2. GET /v1/auth/me provisions the user and a Personal project on first touch (Clerk alone
   decides who signs up, D-02).
3. A gated route (GET /v1/agents) refuses the unpaid user with 403 ``billing_required``.
4. A plan-only wallet checkout on the /v2 reference, posted with the caller's bearer, activates a
   subscription with no deployment.
5. Once the 3 s fresh interval has passed, the access-status poll reads ``provisioned``, with
   ``users.billing_unlocked_at`` set and exactly one ``billing.unlocked`` audit row.
6. The gated route answers 200.

The backend's billing gate reads the in-process reference (``scripts/mock_deploy_api.py``) over
``httpx.ASGITransport``, so the subscription the checkout files is the one the gate reads: the
mock derives a dashboard checkout's owner from the bearer's ``sub`` (D-04).
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.services import billing_gate
from scripts import mock_deploy_api
from tests.test_billing_gate import _real_auth_client

DEPLOY_API_URL = "http://deploy-api.test"
DEPLOY_API_KEY = "test-deploy-key"
FRESH_INTERVAL_SECONDS = 3.0


class _FakeClock:
    """Settable stand-in for `billing_gate.monotonic`."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _bearer_for(subject: str) -> str:
    """A JWT-shaped dashboard bearer whose payload names `subject`. The reference decodes the
    payload without verifying it; the hosted service derives the owner from a verified token."""

    def segment(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment({'sub': subject})}.unsigned"


@pytest.fixture
def gate_on_reference(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """The billing gate on, reading the in-process /v2 reference, with isolated mock stores and a
    settable clock for the fresh interval."""
    monkeypatch.setattr(settings, "billing_gate_enabled", True)
    monkeypatch.setattr(settings, "deploy_api_url", DEPLOY_API_URL)
    monkeypatch.setattr(settings, "deploy_api_key", DEPLOY_API_KEY)
    monkeypatch.setattr(billing_gate, "_transport_override", ASGITransport(app=mock_deploy_api.app))
    monkeypatch.setattr(mock_deploy_api, "SUBSCRIPTIONS", {})
    monkeypatch.setattr(mock_deploy_api, "CHECKOUT_IDEMPOTENCY", {})
    monkeypatch.delenv("MOCK_DASHBOARD_OWNER_USER_ID", raising=False)
    clock = _FakeClock()
    monkeypatch.setattr(billing_gate, "monotonic", clock)
    billing_gate.reset_billing_gate_cache()
    yield clock
    billing_gate.reset_billing_gate_cache()


async def _unlock_audit_count(db: AsyncSession, user: User) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ControlPlaneAuditEvent)
            .where(
                ControlPlaneAuditEvent.action == "billing.unlocked",
                ControlPlaneAuditEvent.target_user_id == user.id,
            )
        )
    ).scalar_one()


async def test_new_user_signs_up_pays_and_unlocks(
    db_session: AsyncSession,
    gate_on_reference: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    clerk_id = f"new_user_path_{test_identity}"
    token = f"dev-new-user-path-{test_identity}"
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "dev_auth_token", token)
    monkeypatch.setattr(settings, "dev_auth_clerk_id", clerk_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with _real_auth_client(db_session) as client:
        # 1. A fresh identity with no row is locked before it is provisioned.
        status_before = await client.get("/v1/access/status", headers=headers)
        assert status_before.status_code == 200, status_before.text
        assert status_before.json() == {"state": "billing_required"}
        assert (
            await db_session.execute(select(User).where(User.clerk_id == clerk_id))
        ).scalar_one_or_none() is None

        # 2. The first verified session provisions the user and a Personal project.
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text
        user = (
            await db_session.execute(select(User).where(User.clerk_id == clerk_id))
        ).scalar_one()
        assert me.json()["id"] == str(user.id)
        personal_projects = (
            await db_session.execute(
                select(func.count())
                .select_from(Project)
                .where(Project.user_id == user.id, Project.kind == PROJECT_KIND_PERSONAL)
            )
        ).scalar_one()
        assert personal_projects == 1

        # 3. The unpaid user is refused by a gated route.
        refused = await client.get("/v1/agents", headers=headers)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == {"code": "billing_required"}

        # 4. A plan-only wallet checkout with the caller's bearer activates a subscription.
        async with httpx.AsyncClient(
            transport=ASGITransport(app=mock_deploy_api.app), base_url=DEPLOY_API_URL
        ) as reference:
            checkout = await reference.post(
                "/v2/subscription/checkout",
                headers={
                    "Authorization": f"Bearer {_bearer_for(clerk_id)}",
                    "Idempotency-Key": f"new-user-path-{uuid.uuid4().hex}",
                },
                json={
                    "plan_slug": "compute_basic",
                    "billing_term_months": 1,
                    "funding_source": "wallet",
                    "subscription_selection": {"mode": "new"},
                },
            )
        assert checkout.status_code == 200, checkout.text
        assert checkout.json()["flow_type"] == "subscription_activation"
        assert checkout.json()["deployment_id"] is None

        # 5. One poll after the fresh interval unlocks the account, stickily and audited once.
        gate_on_reference.advance(FRESH_INTERVAL_SECONDS)
        status_after = await client.get("/v1/access/status", headers=headers)
        assert status_after.status_code == 200, status_after.text
        assert status_after.json() == {"state": "provisioned"}
        await db_session.refresh(user)
        assert user.billing_unlocked_at is not None
        assert await _unlock_audit_count(db_session, user) == 1

        # 6. The gated route now answers.
        allowed = await client.get("/v1/agents", headers=headers)
    assert allowed.status_code == 200, allowed.text
