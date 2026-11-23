"""Operator-triggered tenant deletion (ACCESS-04 deletion half, D-14..D-16).

Task 1: the operator delete drives the SAME lifecycle engine as the Clerk
webhook — a shared fence core (`fence_principal_deleted`) extracted from
`fence_clerk_user_deleted` — deletes the provider identity through the
D-02 seam, and schedules a cancellable grace-window purge. The operator
path writes NO ClerkWebhookEventReceipt: receipts stay genuine webhook
evidence (review finding HIGH #2); the operator evidence trail is
ControlPlaneAuditEvent.

Both trigger orders (operator-first with a later webhook echo, and
webhook-first with a later operator delete) must converge on exactly one
lifecycle row and one purge row (Pitfall 2).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.api_key import ApiKey
from app.models.audit import ControlPlaneAuditEvent
from app.models.channel import ChannelAccount, ChannelSecret
from app.models.memory import Memory
from app.models.principal_lifecycle import ClerkWebhookEventReceipt, PrincipalLifecycle
from app.models.session import Session
from app.models.skill import Skill
from app.models.tenant_purge import (
    TENANT_PURGE_STATUS_FAILED,
    TENANT_PURGE_STATUS_PURGING,
    TenantPurge,
)
from app.models.usage_event import UsageEvent
from app.models.user import User
from app.models.vault import Vault, VaultItem
from app.routes.admin import admin_cancel_tenant_purge
from app.services import vault_crypto
from app.services.file_store import LocalFileStore
from app.services.principal_lifecycle import (
    PrincipalTerminatedError,
    assert_clerk_principal_active,
    fence_principal_deleted,
)
from app.services.tenant_purge import PURGE_COVERAGE, execute_tenant_purge
from app.services.tenant_purge_worker import (
    ClaimedTenantPurge,
    TenantPurgeClaimLostError,
    TenantPurgeWorker,
    claim_tenant_purge,
    complete_tenant_purge,
    record_tenant_purge_failure,
)
from app.services.user_prov import (
    get_user_provisioning_provider as _real_user_prov_factory,
)
from tests.conftest import create_env_with_project, create_test_hosted_runtime_state

pytestmark = pytest.mark.asyncio

_ISSUER = "https://tenant-delete.clerk.example.test"
_SIGNING_SECRET = "whsec_dGVzdF9jbGVya19zaWduaW5nX3NlY3JldA=="
_ADMIN_KEY = "tenant-delete-admin-test"
_AUTH = {"X-Admin-Key": _ADMIN_KEY}


# --------------------------------------------------------------------------
# Helpers + fixtures
# --------------------------------------------------------------------------


def _webhook_payload(subject: str) -> bytes:
    return json.dumps(
        {
            "data": {"deleted": True, "id": subject, "object": "user"},
            "object": "event",
            "timestamp": int(time.time() * 1000),
            "type": "user.deleted",
        },
        separators=(",", ":"),
    ).encode()


def _signed_headers(payload: bytes, *, message_id: str) -> dict[str, str]:
    timestamp = str(int(time.time()))
    secret = base64.b64decode(_SIGNING_SECRET.removeprefix("whsec_"))
    signature = base64.b64encode(
        hmac.new(
            secret,
            f"{message_id}.{timestamp}.".encode() + payload,
            hashlib.sha256,
        ).digest()
    ).decode()
    return {
        "svix-id": message_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature}",
    }


async def _post_webhook_delete(anon_client, subject: str) -> object:
    payload = _webhook_payload(subject)
    return await anon_client.post(
        "/v1/webhooks/clerk",
        content=payload,
        headers=_signed_headers(payload, message_id=f"msg_{uuid.uuid4().hex}"),
    )


@pytest.fixture(autouse=True)
def _delete_trigger_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "clerk_jwt_issuer", _ISSUER)
    monkeypatch.setattr(
        settings,
        "clerk_webhook_signing_secret",
        SecretStr(_SIGNING_SECRET),
    )
    monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)


class _RecordingProvider:
    """Fake UserProvisioningProvider recording delete_identity calls."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_identity(self, subject: str) -> None:
        self.deleted.append(subject)


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
    from app.routes import admin as admin_routes

    provider = _RecordingProvider()
    monkeypatch.setattr(admin_routes, "get_user_provisioning_provider", lambda: provider)
    return provider


async def _lifecycle_count(db: AsyncSession, subject: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(PrincipalLifecycle)
            .where(
                PrincipalLifecycle.issuer == _ISSUER,
                PrincipalLifecycle.subject == subject,
            )
        )
    ).scalar_one()


async def _purge_rows(db: AsyncSession, user_id) -> list[TenantPurge]:
    return list(
        (
            await db.scalars(
                select(TenantPurge)
                .where(TenantPurge.user_id == user_id)
                .order_by(TenantPurge.created_at)
            )
        ).all()
    )


async def _cleanup_delete_route_rows(session_factory, *, user_id) -> None:
    """Drop the rows a committed-lane route call leaves behind.

    The suite shares a database and `seed_user`'s teardown deletes the user —
    a leaked principal_lifecycles row would block that FK, and a leaked
    tenant_purges row would make a sibling claim test flaky. The user row
    itself stays for the fixture to remove.
    """
    from sqlalchemy import delete as sa_delete

    async with session_factory() as db:
        await db.execute(sa_delete(TenantPurge).where(TenantPurge.user_id == user_id))
        await db.execute(
            sa_delete(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.target_user_id == user_id
            )
        )
        await db.execute(sa_delete(PrincipalLifecycle).where(PrincipalLifecycle.user_id == user_id))
        await db.commit()


def _receipts_for(subject: str):
    """Receipts belonging to ONE principal, via the lifecycle they hang off.

    `clerk_webhook_event_receipts` has no direct principal column — the
    ownership path is `lifecycle_id` -> `principal_lifecycles.(issuer,
    subject)`, which is the pair this suite owns per test.
    """
    return (
        select(ClerkWebhookEventReceipt)
        .join(
            PrincipalLifecycle,
            ClerkWebhookEventReceipt.lifecycle_id == PrincipalLifecycle.id,
        )
        .where(
            PrincipalLifecycle.issuer == _ISSUER,
            PrincipalLifecycle.subject == subject,
        )
    )


async def _receipt_count(db: AsyncSession, subject: str) -> int:
    """WR-12: scoped to the principal under test, not the whole table.

    The unscoped table-wide count this replaces was flaky under xdist and
    could be satisfied by a receipt belonging to anyone — while the claim
    it encodes ("the operator path fabricates no webhook receipt") is a
    claim about THIS principal.
    """
    return (
        await db.execute(
            select(func.count())
            .select_from(ClerkWebhookEventReceipt)
            .join(
                PrincipalLifecycle,
                ClerkWebhookEventReceipt.lifecycle_id == PrincipalLifecycle.id,
            )
            .where(
                PrincipalLifecycle.issuer == _ISSUER,
                PrincipalLifecycle.subject == subject,
            )
        )
    ).scalar_one()


# --------------------------------------------------------------------------
# Operator delete: fence + purge schedule + identity deletion (D-14, D-15)
# --------------------------------------------------------------------------


async def test_operator_delete_fences_schedules_purge_and_deletes_identity(
    anon_client,
    db_session: AsyncSession,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    """POST delete fences the tenant IMMEDIATELY, schedules a purge at
    now()+tenant_purge_grace_days, deletes the provider identity, audits
    tenant.delete_requested — and writes NO webhook receipt."""
    before = datetime.now(UTC)
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Fenced at once — the fail-closed lifecycle check rejects the subject.
    with pytest.raises(PrincipalTerminatedError):
        await assert_clerk_principal_active(db_session, subject=seed_user.clerk_id, issuer=_ISSUER)

    (purge,) = await _purge_rows(db_session, seed_user.id)
    assert str(purge.lifecycle_id) == body["lifecycle_id"]
    assert purge.status == "pending"
    # Subject + issuer captured at schedule time for the post-user-row calls.
    assert purge.subject == seed_user.clerk_id
    assert purge.issuer == _ISSUER
    expected_after = before + timedelta(days=settings.tenant_purge_grace_days)
    assert abs((purge.purge_after - expected_after).total_seconds()) < 300
    assert body["purge_after"] is not None

    # Provider identity deleted through the D-02 seam.
    assert fake_provider.deleted == [seed_user.clerk_id]

    # Operator evidence trail is the audit event...
    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.delete_requested",
                ControlPlaneAuditEvent.target_user_id == seed_user.id,
            )
        )
    ).scalar_one()
    assert audit.details["lifecycle_id"] == body["lifecycle_id"]

    # ...never a fabricated webhook receipt (review finding HIGH #2).
    assert await _receipt_count(db_session, seed_user.clerk_id) == 0


async def test_operator_delete_receipt_claim_ignores_another_principals_receipt(
    anon_client,
    db_session: AsyncSession,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    """WR-12, second instance: "the operator path wrote no receipt" is a
    claim about THIS principal.

    A genuine receipt belonging to an unrelated principal — a sibling xdist
    worker's webhook test, or any other row already in a shared database —
    is committed first. The assertion above must be unmoved by it; a
    table-wide count is not.
    """
    foreign_lifecycle = PrincipalLifecycle(
        issuer=_ISSUER,
        subject=f"user_foreign_{uuid.uuid4().hex[:12]}",
        terminated_at=datetime.now(UTC),
        next_cleanup_attempt_at=datetime.now(UTC),
    )
    db_session.add(foreign_lifecycle)
    await db_session.flush()
    db_session.add(
        ClerkWebhookEventReceipt(
            message_id=f"msg_foreign_{uuid.uuid4().hex}",
            lifecycle_id=foreign_lifecycle.id,
            receipt_source="clerk",
            event_type="user.deleted",
            payload_sha256="f" * 64,
            event_occurred_at=datetime.now(UTC),
        )
    )
    await db_session.commit()

    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text

    assert await _receipt_count(db_session, seed_user.clerk_id) == 0
    # The interference is real: unscoped, the same question answers 1.
    assert await _receipt_count(db_session, foreign_lifecycle.subject) == 1


@pytest.mark.committed_db
@pytest.mark.parametrize("grace_days", [7, 30])
async def test_configured_grace_window_gates_the_claim_end_to_end(
    anon_client,
    engine,
    seed_user: User,
    fake_provider: _RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
    grace_days: int,
) -> None:
    """CR-02's other half: the bound proven in test_config.py makes 0
    unreachable; this proves a LEGAL value is actually honoured.

    Every prior purge test drove `purge_after` directly, so no test had ever
    exercised `settings.tenant_purge_grace_days` through the route that
    consumes it — which is exactly why a zero window could have shipped
    without a single failing assertion. Drive the setting, not the column:
    `admin_delete_tenant` reads the module-level `settings` object, so
    constructing a fresh `Settings` here would change nothing.
    """
    monkeypatch.setattr(settings, "tenant_purge_grace_days", grace_days)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    before = datetime.now(UTC)
    try:
        resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        after = datetime.now(UTC)

        # The route schedules BY THE SETTING, and stores it in both columns
        # the claim predicate reads.
        expected = before + timedelta(days=grace_days)
        returned = datetime.fromisoformat(resp.json()["purge_after"])
        assert expected <= returned <= after + timedelta(days=grace_days)

        async with session_factory() as db:
            purge = (
                await db.scalars(select(TenantPurge).where(TenantPurge.user_id == seed_user.id))
            ).one()
            purge_id = purge.id
            assert purge.purge_after == returned
            assert purge.next_attempt_at == returned
            assert purge.status == "pending"

        # ...and the window genuinely HOLDS THE SWEEP SHUT, rather than
        # merely being recorded on the row.
        async with session_factory() as db:
            claimed = await claim_tenant_purge(db)
            await db.rollback()
        assert claimed is None or claimed.purge_id != purge_id, (
            "a not-yet-due purge was claimed — the grace window is not gating the sweep"
        )

        # Negative control: the assertion above is not passing vacuously
        # because nothing was claimable. Move the row into the past and it IS
        # claimed by the same predicate.
        long_ago = datetime.now(UTC) - timedelta(days=400)
        async with session_factory() as db:
            await db.execute(
                sa_update(TenantPurge)
                .where(TenantPurge.id == purge_id)
                .values(purge_after=long_ago, next_attempt_at=long_ago)
            )
            await db.commit()
        async with session_factory() as db:
            claimed = await claim_tenant_purge(db)
            await db.rollback()
        assert claimed is not None and claimed.purge_id == purge_id
    finally:
        await _cleanup_delete_route_rows(session_factory, user_id=seed_user.id)


async def test_operator_delete_unknown_user_404(anon_client, fake_provider) -> None:
    resp = await anon_client.post(f"/v1/admin/tenants/{uuid.uuid4()}/delete", headers=_AUTH)
    assert resp.status_code == 404, resp.text
    assert fake_provider.deleted == []


async def test_tenant_delete_endpoints_require_admin_key(anon_client, seed_user: User) -> None:
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete")
    assert resp.status_code == 401, resp.text
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/purge/cancel")
    assert resp.status_code == 401, resp.text
    resp = await anon_client.get(f"/v1/admin/tenants/{seed_user.id}/lifecycle")
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------
# Both trigger orders converge — one engine, two triggers (Pitfall 2)
# --------------------------------------------------------------------------


async def test_operator_first_then_webhook_echo_converges(
    anon_client,
    db_session: AsyncSession,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    """The Clerk echo after an operator delete finds the existing lifecycle
    (fence_created=false), writes its own genuine receipt, and creates no
    duplicate lifecycle or purge row."""
    subject = seed_user.clerk_id
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert await _receipt_count(db_session, subject) == 0

    echo = await _post_webhook_delete(anon_client, subject)
    assert echo.status_code == 200, echo.text

    # Exactly one lifecycle row for the principal, one purge row, and the
    # echo's own genuine receipt.
    assert await _lifecycle_count(db_session, subject) == 1
    assert len(await _purge_rows(db_session, seed_user.id)) == 1
    assert await _receipt_count(db_session, subject) == 1
    receipt = (await db_session.execute(_receipts_for(subject))).scalar_one()
    assert receipt.receipt_source == "clerk"
    assert receipt.payload_sha256 is not None


async def test_webhook_first_then_operator_delete_converges(
    anon_client,
    db_session: AsyncSession,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    """Webhook-first order: the fence already exists when the operator
    delete lands — no error, no second lifecycle, purge row created once."""
    subject = seed_user.clerk_id
    echo = await _post_webhook_delete(anon_client, subject)
    assert echo.status_code == 200, echo.text
    assert await _lifecycle_count(db_session, subject) == 1

    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text

    assert await _lifecycle_count(db_session, subject) == 1
    assert len(await _purge_rows(db_session, seed_user.id)) == 1
    # The webhook's receipt is the only one — the operator path added none.
    assert await _receipt_count(db_session, subject) == 1

    # Repeating the operator delete stays idempotent: still one purge row.
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert len(await _purge_rows(db_session, seed_user.id)) == 1


# --------------------------------------------------------------------------
# Cancel (D-15: cancellable pending purge, tenant stays fenced)
# --------------------------------------------------------------------------


async def test_cancel_pending_purge_deletes_row_audits_and_keeps_fence(
    anon_client,
    db_session: AsyncSession,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text

    cancel = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/purge/cancel", headers=_AUTH)
    assert cancel.status_code == 200, cancel.text
    assert cancel.json() == {"canceled": True}

    assert await _purge_rows(db_session, seed_user.id) == []
    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.purge_canceled",
                ControlPlaneAuditEvent.target_user_id == seed_user.id,
            )
        )
    ).scalar_one()
    assert audit is not None

    # The tenant stays fenced after cancel — cancel undoes the purge, not
    # the deletion (D-15).
    with pytest.raises(PrincipalTerminatedError):
        await assert_clerk_principal_active(db_session, subject=seed_user.clerk_id, issuer=_ISSUER)

    # No pending purge left → 404.
    again = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/purge/cancel", headers=_AUTH)
    assert again.status_code == 404, again.text


async def test_cancel_without_any_purge_404(anon_client, seed_user: User) -> None:
    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/purge/cancel", headers=_AUTH)
    assert resp.status_code == 404, resp.text


@pytest.mark.committed_db
async def test_cancel_racing_a_claimed_purge_conflicts_instead_of_reporting_success(
    engine, tmp_path
) -> None:
    """The CR-01 race, through real row locks on independent connections.

    The worker claims with `FOR UPDATE SKIP LOCKED` and COMMITS the claim
    before executing in a fresh session, so an unlocked cancel could read
    `pending`, lose the race, and still delete the row by primary key --
    answering `{"canceled": true}` while the irreversible sweep tore down
    deployments, S3 prefixes and rows. Cancel must instead block on the claim
    and then observe `purging`, which is the existing 409.
    """
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    try:
        async with session_factory() as worker_session:
            claim = await claim_tenant_purge(worker_session)
            # Guard against a stray due row from another test making this pass
            # vacuously: the claim must be OUR row.
            assert claim is not None
            assert claim.purge_id == purge.id
            # NOT committed -- the worker transaction still holds the row lock,
            # exactly as it does in the instant before it commits its claim.

            async def _cancel() -> object:
                async with session_factory() as cancel_session:
                    try:
                        return await admin_cancel_tenant_purge(user.id, None, db=cancel_session)
                    except HTTPException as exc:
                        await cancel_session.rollback()
                        return exc

            task = asyncio.create_task(_cancel())
            # Let the cancel reach the row and block on the lock.
            await asyncio.sleep(0.2)
            assert not task.done(), (
                "cancel returned without blocking on the claimed row -- "
                "the select is not taking FOR UPDATE"
            )
            await worker_session.commit()
            outcome = await asyncio.wait_for(task, timeout=10)

        assert isinstance(outcome, HTTPException), outcome
        assert outcome.status_code == 409

        # The row survives, still claimed, so the sweep the worker is about to
        # run is not operating on a row someone already reported as canceled.
        async with session_factory() as check:
            row = await check.get(TenantPurge, purge.id)
            assert row is not None
            assert row.status == TENANT_PURGE_STATUS_PURGING
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_worker_abandons_execution_when_claim_id_no_longer_matches(
    engine, tmp_path, monkeypatch: pytest.MonkeyPatch, purge_provider: _RecordingProvider
) -> None:
    """Defense in depth: the claim is committed before the sweep runs in a
    fresh session, so a stale-lease takeover could otherwise do irreversible
    work twice. `complete_tenant_purge` already fences on the claim id at the
    END -- by then the sweep has happened."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    try:
        real_claim = claim_tenant_purge

        async def _claim_with_stale_id(db, **kwargs):
            claimed = await real_claim(db, **kwargs)
            if claimed is None:
                return None
            # The row is really claimed; the worker just believes it holds a
            # claim that has since been superseded.
            return ClaimedTenantPurge(
                purge_id=claimed.purge_id,
                claim_id=uuid.uuid4(),
                attempt_count=claimed.attempt_count,
            )

        calls: list[object] = []

        async def _spy_execute(db, store, row):
            calls.append(row.id)
            raise AssertionError("execute_tenant_purge must not run on a lost claim")

        monkeypatch.setattr(
            "app.services.tenant_purge_worker.claim_tenant_purge", _claim_with_stale_id
        )
        monkeypatch.setattr("app.services.tenant_purge_worker.execute_tenant_purge", _spy_execute)

        worker = TenantPurgeWorker(session_factory, file_store=LocalFileStore(str(tmp_path / "s")))
        await worker.run_once()

        assert calls == []
        async with session_factory() as check:
            row = await check.get(TenantPurge, purge.id)
            assert row is not None
            assert row.status == TENANT_PURGE_STATUS_PURGING
            # Still there for the real claim holder to finish.
            assert await check.get(User, user.id) is not None
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


async def test_cancel_of_failed_purge_carries_the_sweep_evidence_into_the_audit(
    db_session: AsyncSession,
) -> None:
    """WR-02: a `failed` purge means a sweep already ran and partially deleted.
    Cancel still deletes the row -- that is what stops `claim_tenant_purge`
    re-claiming it every elapsed backoff -- so the attempt count and last error
    must survive in the audit event rather than dying with the row."""
    nonce = uuid.uuid4().hex
    now = datetime.now(UTC)
    user = User(
        clerk_id=f"cancel-failed-{nonce}",
        email=f"cancel-failed-{nonce}@clawdi.local",
        name="Cancel Failed User",
    )
    db_session.add(user)
    await db_session.flush()
    lifecycle = PrincipalLifecycle(
        issuer=_ISSUER,
        subject=user.clerk_id,
        user_id=user.id,
        terminated_at=now,
        next_cleanup_attempt_at=now,
    )
    db_session.add(lifecycle)
    await db_session.flush()
    purge = TenantPurge(
        lifecycle_id=lifecycle.id,
        user_id=user.id,
        subject=user.clerk_id,
        issuer=_ISSUER,
        purge_after=now - timedelta(days=1),
        next_attempt_at=now - timedelta(seconds=1),
        status=TENANT_PURGE_STATUS_FAILED,
        attempt_count=3,
        error="deployment teardown returned 502",
    )
    db_session.add(purge)
    await db_session.commit()

    result = await admin_cancel_tenant_purge(user.id, None, db=db_session)
    assert result.canceled is True

    # The row is gone, so the claim loop has nothing left to retry.
    assert await db_session.get(TenantPurge, purge.id) is None
    remaining = (
        (await db_session.execute(select(TenantPurge).where(TenantPurge.user_id == user.id)))
        .scalars()
        .all()
    )
    assert list(remaining) == []

    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.purge_canceled",
                ControlPlaneAuditEvent.target_user_id == user.id,
            )
        )
    ).scalar_one()
    assert audit.details["status_at_cancel"] == TENANT_PURGE_STATUS_FAILED
    assert audit.details["attempt_count"] == 3
    assert audit.details["error"] == "deployment teardown returned 502"


# --------------------------------------------------------------------------
# Lifecycle status view (03-10 deletion panel)
# --------------------------------------------------------------------------


async def test_lifecycle_status_endpoint_reports_fence_and_purge(
    anon_client,
    seed_user: User,
    fake_provider: _RecordingProvider,
) -> None:
    before = await anon_client.get(f"/v1/admin/tenants/{seed_user.id}/lifecycle", headers=_AUTH)
    assert before.status_code == 200, before.text
    # `grace_days` is the live setting the 03-10 confirmation dialog renders
    # (WR-13, plan 03-16); the exact-body pin keeps stating the whole shape.
    assert before.json() == {
        "fenced": False,
        "purge": None,
        "grace_days": settings.tenant_purge_grace_days,
    }

    resp = await anon_client.post(f"/v1/admin/tenants/{seed_user.id}/delete", headers=_AUTH)
    assert resp.status_code == 200, resp.text

    after = await anon_client.get(f"/v1/admin/tenants/{seed_user.id}/lifecycle", headers=_AUTH)
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["fenced"] is True
    assert body["purge"]["status"] == "pending"
    assert body["purge"]["purge_after"] is not None
    assert body["purge"]["completed_at"] is None


# ==========================================================================
# Task 2: Purge sweep + worker — the one-way stage (D-16, T-03-21..24)
# ==========================================================================


@pytest.fixture
def file_store(tmp_path) -> LocalFileStore:
    return LocalFileStore(str(tmp_path / "purge-files"))


@pytest.fixture
def purge_provider(monkeypatch: pytest.MonkeyPatch) -> _RecordingProvider:
    """Recording fake provider at the purge service's D-02 seam."""
    from app.services import tenant_purge as tenant_purge_service

    provider = _RecordingProvider()
    monkeypatch.setattr(tenant_purge_service, "get_user_provisioning_provider", lambda: provider)
    return provider


@pytest.fixture
def deploy_api(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """MockTransport standing in for /v2 (03-04/03-06 injection pattern)."""
    from app.services import tenant_purge as tenant_purge_service

    requests_log: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_log.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/v2/deployments/"):
            dep = path.rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                json={
                    "resource": {
                        "id": dep,
                        "metadata": {"resourceVersion": f"rv_{dep}"},
                    }
                },
            )
        if request.method == "DELETE" and path.startswith("/v2/deployments/"):
            return httpx.Response(200, json={"done": True})
        if request.method == "POST" and path == "/v2/subscription/cancel":
            return httpx.Response(200, json={"status": "canceled"})
        return httpx.Response(404)

    monkeypatch.setattr(settings, "deploy_api_url", "http://deploy.example.test")
    monkeypatch.setattr(settings, "deploy_api_key", "test-deploy-key")
    monkeypatch.setattr(tenant_purge_service, "_transport_override", httpx.MockTransport(handler))
    return requests_log


@pytest.fixture
def hosted_teardown_opted_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """A self-hosted deployment with no /v2 service, opted out DELIBERATELY.

    WR-04: an unconfigured hosted boundary is no longer a silent skip that
    still reports `complete` — it is either configured, or explicitly opted
    out here and recorded as such in the purge result and the audit stream.
    """
    monkeypatch.setattr(settings, "deploy_api_url", "")
    monkeypatch.setattr(settings, "tenant_purge_skip_hosted_teardown", True)


async def _seed_purgeable_tenant(
    db_session: AsyncSession,
    file_store: LocalFileStore,
    user: User,
    *,
    marker: str,
) -> dict[str, object]:
    """Seed data across the purge-covered set, including S3 objects."""
    env = await create_env_with_project(
        db_session,
        user_id=user.id,
        machine_id=f"purge-env-{uuid.uuid4().hex[:8]}",
        machine_name="Purge Test Agent",
        agent_type="hermes",
    )
    runtime_state = await create_test_hosted_runtime_state(db_session, env, runtime_name="hermes")

    local_session_id = f"session-{uuid.uuid4().hex[:12]}"
    session_file_key = f"sessions/{user.id}/{local_session_id}.json"
    await file_store.put(session_file_key, f"body {marker}".encode())
    session = Session(
        user_id=user.id,
        environment_id=env.id,
        local_session_id=local_session_id,
        started_at=datetime.now(UTC),
        file_key=session_file_key,
        content_hash="a" * 64,
    )
    db_session.add(session)
    await db_session.flush()
    db_session.add(
        UsageEvent(
            session_id=session.id,
            ts=datetime.now(UTC),
            provider="test-provider",
            model="test-model",
            in_tok=1,
            out_tok=2,
            cost_usd_micros=3,
        )
    )

    skill_key = f"skill-{uuid.uuid4().hex[:8]}"
    skill_file_key = f"skills/{user.id}/{env.default_project_id}/{skill_key}/{'b' * 64}.tar.gz"
    await file_store.put(skill_file_key, f"skill {marker}".encode())
    db_session.add(
        Skill(
            user_id=user.id,
            project_id=env.default_project_id,
            skill_key=skill_key,
            name="Purge Skill",
            content_hash="b" * 64,
            file_key=skill_file_key,
        )
    )

    db_session.add(Memory(user_id=user.id, content=f"memory {marker}", category="fact"))

    vault = Vault(user_id=user.id, slug=f"vault-{uuid.uuid4().hex[:8]}", name="Purge Vault")
    db_session.add(vault)
    await db_session.flush()
    ciphertext, nonce = vault_crypto.encrypt(f"plain-{marker}")
    db_session.add(
        VaultItem(
            vault_id=vault.id,
            item_name="item",
            section="prod",
            encrypted_value=ciphertext,
            nonce=nonce,
        )
    )

    account = ChannelAccount(
        user_id=user.id,
        provider="telegram",
        name=f"bot-{uuid.uuid4().hex[:8]}",
        webhook_secret_hash="0" * 64,
        config={"bot_username": "purge_test_bot"},
    )
    db_session.add(account)
    await db_session.flush()
    secret_cipher, secret_nonce = vault_crypto.encrypt(f"chan-{marker}")
    db_session.add(
        ChannelSecret(
            account_id=account.id,
            user_id=user.id,
            name="bot_token",
            encrypted_value=secret_cipher,
            value_nonce=secret_nonce,
        )
    )

    db_session.add(
        ApiKey(
            user_id=user.id,
            key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            key_prefix="clawdi_pg",
            label="purge-test-key",
        )
    )

    export_key = f"exports/{user.id}/{uuid.uuid4()}.tar.gz"
    await file_store.put(export_key, f"export {marker}".encode())
    await db_session.commit()

    return {
        "env": env,
        "deployment_id": runtime_state.deployment_id,
        "session_file_key": session_file_key,
        "skill_file_key": skill_file_key,
        "export_key": export_key,
    }


async def _fence_and_schedule_due_purge(
    db_session: AsyncSession, user: User
) -> tuple[PrincipalLifecycle, TenantPurge]:
    now = datetime.now(UTC)
    lifecycle, _ = await fence_principal_deleted(
        db_session,
        issuer=_ISSUER,
        subject=user.clerk_id,
        occurred_at=now,
        now=now,
    )
    purge = TenantPurge(
        lifecycle_id=lifecycle.id,
        user_id=user.id,
        subject=user.clerk_id,
        issuer=_ISSUER,
        purge_after=now - timedelta(hours=1),
        next_attempt_at=now - timedelta(hours=1),
    )
    db_session.add(purge)
    await db_session.commit()
    return lifecycle, purge


async def _user_scoped_count(db: AsyncSession, model, user_id) -> int:
    return (
        await db.execute(select(func.count()).select_from(model).where(model.user_id == user_id))
    ).scalar_one()


async def test_purge_sweep_erases_covered_set_and_keeps_tombstone(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    deploy_api: list[httpx.Request],
    purge_provider: _RecordingProvider,
) -> None:
    """The sweep erases tenant rows across the PURGE_COVERAGE 'purge'
    tables (embeddings die with memories), the three S3 prefixes, issues
    DELETE /v2/deployments/{id} and POST /v2/subscription/cancel, re-issues
    the provider identity delete 404-tolerantly — and RETAINS the
    principal_lifecycles tombstone plus SET-NULL audit rows (Pitfall 5)."""
    subject = seed_user.clerk_id
    seeded = await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="mine")
    lifecycle, purge = await _fence_and_schedule_due_purge(db_session, seed_user)
    purge_subject = purge.subject

    # An audit row referencing the tenant must SURVIVE the purge with its
    # user reference nulled (SET NULL), not disappear.
    from app.services.audit import record_control_plane_audit

    survivor = record_control_plane_audit(
        db_session,
        actor_type="admin",
        action="test.purge_survivor",
        resource_type="user",
        resource_id=str(seed_user.id),
        source="test",
        target_user_id=seed_user.id,
        details={},
    )
    await db_session.commit()
    survivor_id = survivor.id

    await execute_tenant_purge(db_session, file_store, purge)
    await db_session.commit()

    # Tenant rows across the covered set are gone (memories rows take
    # their embeddings with them — embedding is a column of memories).
    # Count queries, not session.get: expire_on_commit=False would hand
    # back cached instances for rows Core DELETE removed.
    user_rows = (
        await db_session.execute(
            select(func.count()).select_from(User).where(User.id == seed_user.id)
        )
    ).scalar_one()
    assert user_rows == 0
    for model in (Memory, Skill, Session, Vault, ChannelAccount, ApiKey):
        assert await _user_scoped_count(db_session, model, seed_user.id) == 0, model

    # S3 prefixes empty on both backends' list semantics.
    assert await file_store.list_keys(f"sessions/{seed_user.id}/") == []
    assert await file_store.list_keys(f"skills/{seed_user.id}/") == []
    assert await file_store.list_keys(f"exports/{seed_user.id}/") == []

    # Deployment teardown + subscription cancel went out over /v2.
    deleted_paths = [r.url.path for r in deploy_api if r.method == "DELETE"]
    assert f"/v2/deployments/{seeded['deployment_id']}" in deleted_paths
    cancel_requests = [
        r for r in deploy_api if r.method == "POST" and r.url.path == "/v2/subscription/cancel"
    ]
    assert len(cancel_requests) == 1
    # 06.2 D-09: the cancel names the hosted owner by the subject captured at
    # schedule time (the Clerk user id), never the internal users.id UUID.
    assert purge_subject == subject
    assert json.loads(cancel_requests[0].content)["owner_user_id"] == purge_subject

    # Identity delete re-issued through the D-02 seam (webhook-first order
    # cover; 404 from the provider is already-gone success).
    assert purge_provider.deleted == [subject]

    # The fence tombstone SURVIVES — the identity stays terminated.
    assert await db_session.get(PrincipalLifecycle, lifecycle.id) is not None
    with pytest.raises(PrincipalTerminatedError):
        await assert_clerk_principal_active(db_session, subject=subject, issuer=_ISSUER)

    # The purge row itself is a retained tombstone, marked by the caller.
    assert await db_session.get(TenantPurge, purge.id) is not None

    # Audit rows survive with SET-NULL user refs (anonymize disposition).
    # populate_existing forces a DB re-read past the cached instance.
    surviving_audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent)
            .where(ControlPlaneAuditEvent.id == survivor_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert surviving_audit.target_user_id is None


async def test_purge_sweep_scoped_to_target_tenant(
    db_session: AsyncSession,
    seed_user: User,
    test_identity: str,
    file_store: LocalFileStore,
    deploy_api: list[httpx.Request],
    purge_provider: _RecordingProvider,
) -> None:
    """A control tenant's rows, prefixes and deployments are untouched."""
    from app.models.project import PROJECT_KIND_PERSONAL, Project

    control_user = User(
        clerk_id=f"control_{test_identity}",
        email=f"control-{test_identity}@clawdi.local",
        name="Control User",
    )
    db_session.add(control_user)
    await db_session.flush()
    db_session.add(
        Project(
            user_id=control_user.id,
            name="Personal",
            slug="personal",
            kind=PROJECT_KIND_PERSONAL,
        )
    )
    await db_session.commit()

    await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="target")
    control_seed = await _seed_purgeable_tenant(
        db_session, file_store, control_user, marker="control"
    )
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    await execute_tenant_purge(db_session, file_store, purge)
    await db_session.commit()

    # Control tenant untouched: rows, prefixes and deployment survive.
    assert await db_session.get(User, control_user.id) is not None
    assert await _user_scoped_count(db_session, Memory, control_user.id) == 1
    assert await _user_scoped_count(db_session, Skill, control_user.id) == 1
    assert await file_store.list_keys(f"sessions/{control_user.id}/") != []
    assert await file_store.list_keys(f"skills/{control_user.id}/") != []
    deleted_paths = [r.url.path for r in deploy_api if r.method == "DELETE"]
    assert f"/v2/deployments/{control_seed['deployment_id']}" not in deleted_paths


async def test_purge_records_that_hosted_teardown_executed(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    deploy_api: list[httpx.Request],
    purge_provider: _RecordingProvider,
) -> None:
    """WR-04 branch 1 (the production shape): with /v2 configured the sweep
    performs both D-16 external calls AND says so in its result, so a reader
    can tell an executed teardown from a skipped one."""
    await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="executed")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    result = await execute_tenant_purge(db_session, file_store, purge)
    await db_session.commit()

    assert result["hosted_teardown"] == "executed"
    assert result["deployments"] != []
    assert result["deployments_not_found"] == []
    assert any(r.method == "POST" and r.url.path == "/v2/subscription/cancel" for r in deploy_api)


async def test_purge_records_the_deployments_v2_answered_404_for(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    deploy_api: list[httpx.Request],
    purge_provider: _RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 is taken as already gone, so the sweep names each such id: if the billing service
    was wrong (a restored database with no compute history), the record is how it is found."""
    from app.services import tenant_purge as tenant_purge_service

    def not_found(request: httpx.Request) -> httpx.Response:
        deploy_api.append(request)
        if request.method == "POST" and request.url.path == "/v2/subscription/cancel":
            return httpx.Response(200, json={"status": "canceled"})
        return httpx.Response(404)

    monkeypatch.setattr(tenant_purge_service, "_transport_override", httpx.MockTransport(not_found))
    seeded = await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="gone")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)
    assert seeded["deployment_id"]

    result = await execute_tenant_purge(db_session, file_store, purge)
    await db_session.commit()

    assert result["deployments"] == []
    assert result["deployments_not_found"] == [seeded["deployment_id"]]
    assert not any(r.method == "DELETE" for r in deploy_api)


async def test_purge_refuses_to_complete_on_an_unconfigured_hosted_boundary(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    purge_provider: _RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WR-04 branch 2 (today's silent-skip shape, and the shipped default):
    with `deploy_api_url` empty and no opt-out, the sweep RAISES before any
    destructive step instead of skipping both D-16 external calls and still
    reporting `complete`. Nothing is deleted on that attempt."""
    monkeypatch.setattr(settings, "deploy_api_url", "")
    monkeypatch.setattr(settings, "tenant_purge_skip_hosted_teardown", False)
    seeded = await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="unconfigured")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    with pytest.raises(RuntimeError, match="deploy_api_url"):
        await execute_tenant_purge(db_session, file_store, purge)
    await db_session.rollback()

    # The raise landed BEFORE the S3 deletes and the row deletes, so a
    # misconfigured deployment cannot half-destroy a tenant on every retry.
    assert await db_session.get(User, seed_user.id) is not None
    assert await _user_scoped_count(db_session, Memory, seed_user.id) == 1
    assert await file_store.list_keys(f"sessions/{seed_user.id}/") == [seeded["session_file_key"]]
    assert await file_store.list_keys(f"exports/{seed_user.id}/") == [seeded["export_key"]]
    # And no identity delete was re-issued either — the refusal is step 0.
    assert purge_provider.deleted == []


async def test_purge_records_a_deliberately_skipped_hosted_teardown(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    purge_provider: _RecordingProvider,
    hosted_teardown_opted_out: None,
) -> None:
    """WR-04 branch 3: with the opt-out on, the sweep proceeds and records the
    skip and its reason explicitly — distinguishable from a tenant that simply
    had no deployments (which carries the `executed` marker)."""
    await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="optout")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    result = await execute_tenant_purge(db_session, file_store, purge)
    await db_session.commit()

    assert result["hosted_teardown"].startswith("skipped:")
    assert "unconfigured" in result["hosted_teardown"]
    assert result["deployments"] == []
    # The tenant is still genuinely purged — the skip is the /v2 half only.
    # Count query, not session.get: expire_on_commit=False hands back cached
    # instances for rows Core DELETE removed.
    assert (
        await db_session.execute(
            select(func.count()).select_from(User).where(User.id == seed_user.id)
        )
    ).scalar_one() == 0
    assert await file_store.list_keys(f"sessions/{seed_user.id}/") == []


async def test_purge_sweep_scopes_and_erases_the_int3_foreign_key_only_tables(
    db_session: AsyncSession, seed_user: User, file_store: LocalFileStore, test_identity: str
) -> None:
    """INT-3: the 06.1 tables reach their tenant only through foreign keys. Their explicit
    via-parent dispositions give the sweep a real scope for each -- non-empty before, empty after
    -- and that scope never reaches a second tenant's rows."""
    from app.models.base import Base
    from app.services.tenant_purge import _delete_covered_rows, _tenant_scope_condition
    from tests.test_tenant_export import _seed_tenant

    control = User(clerk_id=f"int3-control-{test_identity}", email=f"int3-{test_identity}@x.test")
    db_session.add(control)
    await db_session.flush()
    await _seed_tenant(db_session, file_store, seed_user, marker="int3-purge")
    await _seed_tenant(db_session, file_store, control, marker="int3-control")

    names = ("agent_plugin_installations", "agent_skill_references", "vault_secret_requests")

    async def counts(user_id: uuid.UUID) -> dict[str, int]:
        result: dict[str, int] = {}
        for name in names:
            table = Base.metadata.tables[name]
            condition = _tenant_scope_condition(table, user_id)
            assert condition is not None, name
            result[name] = (
                await db_session.execute(select(func.count()).select_from(table).where(condition))
            ).scalar_one()
        return result

    assert await counts(seed_user.id) == dict.fromkeys(names, 1)
    assert await counts(control.id) == dict.fromkeys(names, 1)

    deleted = await _delete_covered_rows(db_session, seed_user.id)
    for name in names:
        assert deleted.get(name) == 1, (name, deleted)
    assert await counts(control.id) == dict.fromkeys(names, 1)


async def test_purge_coverage_disposition_pins() -> None:
    """Acceptance pins: principal_lifecycles is tombstone, usage_events is
    via-parent:sessions, and the hosted_runtime/channel children are named
    explicitly (review finding 6)."""
    assert PURGE_COVERAGE["principal_lifecycles"] == "tombstone"
    assert PURGE_COVERAGE["usage_events"] == "via-parent:sessions"
    assert PURGE_COVERAGE["hosted_runtime_states"] == "via-parent:agent_environments"
    assert PURGE_COVERAGE["hosted_runtime_secrets"] == "via-parent:hosted_runtime_states"
    assert (
        PURGE_COVERAGE["hosted_runtime_config_observations"] == "via-parent:hosted_runtime_states"
    )
    assert PURGE_COVERAGE["tenant_purges"] == "tombstone"
    # channel_accounts is the chain root ("purge"); every channel child
    # records its actual parent (review finding 6).
    assert PURGE_COVERAGE["channel_accounts"] == "purge"
    for name, disposition in PURGE_COVERAGE.items():
        if name.startswith("channel_") and name != "channel_accounts":
            assert disposition.startswith("via-parent:"), (name, disposition)


# --------------------------------------------------------------------------
# WR-09 purge half / UAT 64 — a REFUSED identity seam aborts before side effects
# --------------------------------------------------------------------------
#
# The route-side twin is `test_refused_identity_provider_commits_nothing_at_all`
# (test_admin_tenant_console.py): a config value the factory refuses must not
# leave a tenant fenced and a one-way purge scheduled. This is the service-side
# half, and it was the phase's last behavior-unverified truth — 03-15 claims the
# `get_user_provisioning_provider()` hoist to step 0 of `execute_tenant_purge`
# holds INDEPENDENTLY of the load-time validator that would normally make a bad
# `user_prov` unreachable, and every one of the sibling purge tests above
# monkeypatches a WORKING provider, so nothing exercised the refusing case.
#
# The distinguishing assertion is the /v2 request log. The neighbouring
# unconfigured-boundary test cannot make it — with `deploy_api_url` empty there
# is no transport to count against. Here /v2 IS configured and answering, the
# tenant HAS a deployment to tear down, and the log must still be empty: that is
# what pins the resolution ABOVE the httpx block rather than merely above the
# S3 and DB steps.


@pytest.fixture
def refusing_purge_seam() -> None:
    """Assert the purge seam is the REAL factory, not a fake.

    These tests deliberately do NOT request `purge_provider` — the refusal has
    to come from the real `get_user_provisioning_provider()` reading a real
    `settings.user_prov`, because a fake that raises would pin the test to a
    raise site this code does not have. The parametrized values below are the
    factory's own two refusal branches.

    The guard matters because the sibling tests in this module patch that exact
    name: if a future edit made the patch module-wide or autouse, these tests
    would silently stop reaching the refusal and assert nothing.
    """
    from app.services import tenant_purge as tenant_purge_service

    assert tenant_purge_service.get_user_provisioning_provider is _real_user_prov_factory, (
        "the purge seam is stubbed; the refusal under test would never be reached"
    )


@pytest.mark.parametrize(
    ("bad_user_prov", "expected_error"),
    [("supa", NotImplementedError), ("vault", ValueError)],
)
async def test_refused_identity_provider_aborts_purge_before_any_side_effect(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    deploy_api: list[httpx.Request],
    refusing_purge_seam: None,
    monkeypatch: pytest.MonkeyPatch,
    bad_user_prov: str,
    expected_error: type[Exception],
) -> None:
    """UAT 64 / 03-15 truth 6: with the identity seam refusing, the sweep
    raises at step 0 — before the /v2 enumeration, before the deployment
    teardown and subscription cancel, before the S3 prefix deletes and before
    the row deletes.

    Ordering, not merely outcome: a purge that tore down a tenant's
    deployments and cancelled its subscription and only THEN discovered it
    could not delete the identity would leave the tenant half-destroyed on
    every retry, with no way back."""
    monkeypatch.setattr(settings, "user_prov", bad_user_prov)
    # Bind the id before the sweep: the rollback below expires `seed_user`, and
    # a later `seed_user.id` would then emit IO from a sync attribute access.
    user_id = seed_user.id
    seeded = await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="refused")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    # Non-vacuity: "nothing was destroyed" is only a claim if there was
    # something to destroy. Every assertion below has a live precondition.
    assert seeded["deployment_id"], "no deployment to tear down — the /v2 claim would be vacuous"
    assert await file_store.list_keys(f"sessions/{user_id}/") == [seeded["session_file_key"]]
    assert await file_store.list_keys(f"skills/{user_id}/") == [seeded["skill_file_key"]]
    assert await file_store.list_keys(f"exports/{user_id}/") == [seeded["export_key"]]
    assert await _user_scoped_count(db_session, Memory, user_id) == 1

    with pytest.raises(expected_error):
        await execute_tenant_purge(db_session, file_store, purge)
    await db_session.rollback()

    # 1. Zero /v2 requests. The transport is live and `deploy_api_url` is set
    #    (the `deploy_api` fixture does both), so an empty log can only mean the
    #    raise preceded the enumeration — not that there was nothing to call.
    assert deploy_api == [], (
        "the identity seam was resolved AFTER the /v2 block: "
        f"{[(r.method, r.url.path) for r in deploy_api]}"
    )

    # 2. Zero S3 keys deleted, on all three prefixes.
    assert await file_store.list_keys(f"sessions/{user_id}/") == [seeded["session_file_key"]]
    assert await file_store.list_keys(f"skills/{user_id}/") == [seeded["skill_file_key"]]
    assert await file_store.list_keys(f"exports/{user_id}/") == [seeded["export_key"]]

    # 3. Zero PURGE_COVERAGE rows removed.
    assert (
        await db_session.execute(select(func.count()).select_from(User).where(User.id == user_id))
    ).scalar_one() == 1
    for model in (Memory, Skill, Session, Vault, ChannelAccount, ApiKey):
        assert await _user_scoped_count(db_session, model, user_id) == 1, model


async def test_refused_identity_provider_is_reached_before_the_hosted_branch(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    refusing_purge_seam: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hoist sits above step 0b too, not just above step 1.

    With BOTH faults present — a refusing identity seam and an unconfigured
    hosted boundary — the identity refusal is what surfaces. Without this, a
    reordering that moved the resolution below the branch decision would still
    pass the test above (which raises either way) while silently changing which
    misconfiguration an operator is told about."""
    monkeypatch.setattr(settings, "user_prov", "supa")
    monkeypatch.setattr(settings, "deploy_api_url", "")
    monkeypatch.setattr(settings, "tenant_purge_skip_hosted_teardown", False)
    user_id = seed_user.id
    await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="refused-both")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    # NotImplementedError (step 0), not RuntimeError("deploy_api_url", step 0b).
    with pytest.raises(NotImplementedError):
        await execute_tenant_purge(db_session, file_store, purge)
    await db_session.rollback()

    assert (
        await db_session.execute(select(func.count()).select_from(User).where(User.id == user_id))
    ).scalar_one() == 1


# --------------------------------------------------------------------------
# UAT 61 — an unreachable /v2 fails the sweep BEFORE any destructive step
# --------------------------------------------------------------------------
#
# `scripts/uat-purge-failure.sh` induces a real failed purge so the operator
# console's failed-sweep panel can be walked, and its safety rests entirely on
# this ordering: with `deploy_api_url` pointed at a closed port the sweep dies
# inside the /v2 block, which runs before the identity delete, the S3 prefix
# deletes and the row deletes. The script re-checks that at runtime (it counts
# the tenant's rows either side and refuses to record anything if they moved),
# but a runtime check on a UAT host is a poor place to discover a regression.
# This pins it in CI instead.


async def test_unreachable_deploy_api_fails_purge_before_any_destructive_step(
    db_session: AsyncSession,
    seed_user: User,
    file_store: LocalFileStore,
    purge_provider: _RecordingProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A /v2 outage lands the purge FAILED with the tenant fully intact.

    This is the ordinary production shape of a failed sweep — the hosted API
    is down, the purge retries later — and the one the UAT script reproduces
    deliberately. Port 1 is privileged and unassigned, so the connect is
    refused rather than hanging for the sweep's 10s timeout."""
    monkeypatch.setattr(settings, "deploy_api_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "tenant_purge_skip_hosted_teardown", False)
    monkeypatch.setattr("app.services.tenant_purge._transport_override", None)
    user_id = seed_user.id
    seeded = await _seed_purgeable_tenant(db_session, file_store, seed_user, marker="outage")
    _, purge = await _fence_and_schedule_due_purge(db_session, seed_user)

    with pytest.raises(httpx.HTTPError):
        await execute_tenant_purge(db_session, file_store, purge)
    await db_session.rollback()

    # Nothing of the tenant is gone: no identity re-delete, no S3 key, no row.
    assert purge_provider.deleted == []
    assert await file_store.list_keys(f"sessions/{user_id}/") == [seeded["session_file_key"]]
    assert await file_store.list_keys(f"skills/{user_id}/") == [seeded["skill_file_key"]]
    assert await file_store.list_keys(f"exports/{user_id}/") == [seeded["export_key"]]
    assert (
        await db_session.execute(select(func.count()).select_from(User).where(User.id == user_id))
    ).scalar_one() == 1
    for model in (Memory, Skill, Session, Vault, ChannelAccount, ApiKey):
        assert await _user_scoped_count(db_session, model, user_id) == 1, model


# --------------------------------------------------------------------------
# Worker durability: claim gating, backoff retry, stale-claim fencing
# --------------------------------------------------------------------------


class _FailingDeletePrefixStore(LocalFileStore):
    """delete_prefix always raises — simulates S3 partial failure."""

    async def delete_prefix(self, prefix: str) -> int:
        raise RuntimeError("synthetic S3 outage")


async def _create_committed_purge_fixture(session_factory, *, nonce: str, due: bool):
    """Committed-lane tenant: user + lifecycle + purge row (+ one memory)."""
    now = datetime.now(UTC)
    offset = -timedelta(hours=1) if due else timedelta(days=30)
    async with session_factory() as db:
        user = User(
            clerk_id=f"purge-worker-{nonce}",
            email=f"purge-worker-{nonce}@clawdi.local",
            name="Purge Worker User",
        )
        db.add(user)
        await db.flush()
        lifecycle = PrincipalLifecycle(
            issuer=_ISSUER,
            subject=user.clerk_id,
            user_id=user.id,
            terminated_at=now,
            next_cleanup_attempt_at=now,
        )
        db.add(lifecycle)
        await db.flush()
        purge = TenantPurge(
            lifecycle_id=lifecycle.id,
            user_id=user.id,
            subject=user.clerk_id,
            issuer=_ISSUER,
            purge_after=now + offset,
            next_attempt_at=now + offset,
        )
        db.add(purge)
        db.add(Memory(user_id=user.id, content=f"memory {nonce}", category="fact"))
        await db.commit()
        await db.refresh(purge)
        await db.refresh(user)
        return user, lifecycle.id, purge


async def _cleanup_purge_rows(session_factory, *, user_id, lifecycle_id) -> None:
    from sqlalchemy import delete

    async with session_factory() as db:
        await db.execute(delete(Memory).where(Memory.user_id == user_id))
        await db.execute(delete(TenantPurge).where(TenantPurge.lifecycle_id == lifecycle_id))
        await db.execute(
            delete(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action.in_(("tenant.purged",))
            )
        )
        await db.execute(delete(PrincipalLifecycle).where(PrincipalLifecycle.id == lifecycle_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.mark.committed_db
async def test_recorded_purge_failure_renders_as_a_failed_sweep(
    engine, tmp_path, purge_provider: _RecordingProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row `scripts/uat-purge-failure.sh` leaves behind is the row UAT 61's
    console panel needs.

    That panel is driven by exactly three fields — `status`, `error` and
    `attempt_count` — plus a `purge_after` in the PAST (W-01: a failed row was
    only claimable once its grace window elapsed, so the old "Purge scheduled —
    {future date}" badge was false on both halves). Assert the shape the real
    claim/execute/record cycle produces, not a hand-built row.

    Committed lane with the global `claim_tenant_purge`, so it owns its rows
    and drops them in `finally`: a leaked due purge makes every sibling claim
    test claim the wrong tenant.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(settings, "deploy_api_url", "http://127.0.0.1:1")
    monkeypatch.setattr(settings, "tenant_purge_skip_hosted_teardown", False)
    monkeypatch.setattr("app.services.tenant_purge._transport_override", None)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    store = LocalFileStore(str(tmp_path / "uat61-files"))
    try:
        async with session_factory() as db:
            claim = await claim_tenant_purge(db)
            assert claim is not None and claim.purge_id == purge.id
            await db.commit()

        async with session_factory() as db:
            claimed = await db.get(TenantPurge, purge.id)
            assert claimed is not None
            with pytest.raises(httpx.HTTPError) as caught:
                await execute_tenant_purge(db, store, claimed)
            await db.rollback()
        error = str(caught.value) or caught.value.__class__.__name__

        async with session_factory() as db:
            assert await record_tenant_purge_failure(
                db, purge_id=claim.purge_id, expected_claim_id=claim.claim_id, error=error
            )
            await db.commit()

        async with session_factory() as db:
            recorded = await db.get(TenantPurge, purge.id)
            assert recorded is not None
            assert recorded.status == TENANT_PURGE_STATUS_FAILED
            # A null error renders "Attempt {n}" alone, not the failure summary.
            assert recorded.error
            assert recorded.attempt_count == 1
            # The badge's date half: a failed row's purge_after is in the past.
            assert recorded.purge_after < datetime.now(UTC)
            # Claim released and backoff set, so the retry can reclaim it.
            assert recorded.claim_id is None
            assert recorded.next_attempt_at is not None
            # And the tenant is intact — the sweep died before its first
            # destructive step, which is what makes the UAT script safe.
            assert await db.get(User, user.id) is not None
            assert purge_provider.deleted == []
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_purge_worker_run_once_completes_due_purge_and_audits(
    engine, tmp_path, purge_provider: _RecordingProvider, hosted_teardown_opted_out: None
) -> None:
    """run_once claims a due purge, sweeps, marks complete and audits
    tenant.purged with table/prefix/deployment counts.

    This deployment has no /v2 service, so it runs the WR-04 opt-out branch
    explicitly — and the audit row has to SAY the hosted teardown was skipped
    rather than leaving the completion claim to be read as a full D-16 sweep."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    store = LocalFileStore(str(tmp_path / "worker-files"))
    await store.put(f"sessions/{user.id}/s.json", b"body")
    try:
        worker = TenantPurgeWorker(session_factory, file_store=store)
        assert await worker.run_once() == purge.id

        async with session_factory() as db:
            refreshed = await db.get(TenantPurge, purge.id)
            assert refreshed is not None
            assert refreshed.status == "complete"
            assert refreshed.completed_at is not None
            assert refreshed.claim_id is None
            assert refreshed.error is None
            # Rows and prefix gone; tombstone survives.
            assert await db.get(User, user.id) is None
            assert await _user_scoped_count(db, Memory, user.id) == 0
            assert await db.get(PrincipalLifecycle, lifecycle_id) is not None
            assert await store.list_keys(f"sessions/{user.id}/") == []

            audit = (
                await db.execute(
                    select(ControlPlaneAuditEvent).where(
                        ControlPlaneAuditEvent.action == "tenant.purged",
                        ControlPlaneAuditEvent.resource_id == str(purge.id),
                    )
                )
            ).scalar_one()
            assert audit.details["user_id"] == str(user.id)
            assert audit.details["tables"]["memories"] == 1
            assert audit.details["hosted_teardown"].startswith("skipped:")
            assert "unconfigured" in audit.details["hosted_teardown"]
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_purge_audit_distinguishes_no_deployments_from_a_skipped_teardown(
    engine, tmp_path, purge_provider: _RecordingProvider, deploy_api: list[httpx.Request]
) -> None:
    """WR-04's distinguishability claim, end to end through the audit stream:
    a tenant that simply HAD no deployments audits `deployments: []` with the
    executed marker, which a reader can tell apart from the opt-out branch's
    skipped marker asserted in the test above."""
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    try:
        worker = TenantPurgeWorker(
            session_factory, file_store=LocalFileStore(str(tmp_path / "no-deployments"))
        )
        assert await worker.run_once() == purge.id

        async with session_factory() as db:
            refreshed = await db.get(TenantPurge, purge.id)
            assert refreshed is not None
            assert refreshed.status == "complete"
            audit = (
                await db.execute(
                    select(ControlPlaneAuditEvent).where(
                        ControlPlaneAuditEvent.action == "tenant.purged",
                        ControlPlaneAuditEvent.resource_id == str(purge.id),
                    )
                )
            ).scalar_one()
            assert audit.details["hosted_teardown"] == "executed"
            assert audit.details["deployments"] == []
            assert audit.details["deployments_not_found"] == []
        # The subscription cancel really went out — "no deployments" is not
        # the same as "no external calls".
        assert any(
            r.method == "POST" and r.url.path == "/v2/subscription/cancel" for r in deploy_api
        )
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_not_yet_due_purge_is_not_claimed(engine, purge_provider: _RecordingProvider) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=False
    )
    try:
        async with session_factory() as db:
            assert await claim_tenant_purge(db) is None
            await db.rollback()
        async with session_factory() as db:
            refreshed = await db.get(TenantPurge, purge.id)
            assert refreshed is not None
            assert refreshed.status == "pending"
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_purge_s3_failure_lands_failed_with_backoff_then_retry_converges(
    engine, tmp_path, purge_provider: _RecordingProvider, hosted_teardown_opted_out: None
) -> None:
    """delete_prefix raising lands the purge failed with backoff and an
    error; a later run_once retries the half-completed purge and converges
    (missing rows/objects are not errors — T-03-24)."""
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    try:
        failing_worker = TenantPurgeWorker(
            session_factory,
            file_store=_FailingDeletePrefixStore(str(tmp_path / "broken")),
        )
        assert await failing_worker.run_once() == purge.id

        async with session_factory() as db:
            failed = await db.get(TenantPurge, purge.id)
            assert failed is not None
            assert failed.status == "failed"
            assert failed.error is not None
            assert failed.attempt_count == 1
            assert failed.next_attempt_at is not None
            assert failed.next_attempt_at > datetime.now(UTC)
            assert failed.claim_id is None

        async with session_factory() as db:
            await db.execute(
                update(TenantPurge)
                .where(TenantPurge.id == purge.id)
                .values(next_attempt_at=datetime.now(UTC))
            )
            await db.commit()

        healthy_worker = TenantPurgeWorker(
            session_factory, file_store=LocalFileStore(str(tmp_path / "healthy"))
        )
        assert await healthy_worker.run_once() == purge.id

        async with session_factory() as db:
            done = await db.get(TenantPurge, purge.id)
            assert done is not None
            assert done.status == "complete"
            assert done.attempt_count == 2
            # The half-completed purge converged: user gone, tombstone kept.
            assert await db.get(User, user.id) is None
            assert await db.get(PrincipalLifecycle, lifecycle_id) is not None
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


@pytest.mark.committed_db
async def test_stale_claim_id_cannot_complete_purge(
    engine, purge_provider: _RecordingProvider
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    nonce = uuid.uuid4().hex
    user, lifecycle_id, purge = await _create_committed_purge_fixture(
        session_factory, nonce=nonce, due=True
    )
    try:
        async with session_factory() as db:
            claim = await claim_tenant_purge(db)
            assert claim is not None
            assert claim.purge_id == purge.id
            await db.commit()

        stale = uuid.uuid4()
        assert stale != claim.claim_id
        async with session_factory() as db:
            with pytest.raises(TenantPurgeClaimLostError):
                await complete_tenant_purge(db, purge_id=purge.id, expected_claim_id=stale)
            await db.rollback()
        async with session_factory() as db:
            assert (
                await record_tenant_purge_failure(
                    db, purge_id=purge.id, expected_claim_id=stale, error="stale"
                )
                is False
            )
            await db.rollback()

        async with session_factory() as db:
            await complete_tenant_purge(db, purge_id=purge.id, expected_claim_id=claim.claim_id)
            await db.commit()
        async with session_factory() as db:
            done = await db.get(TenantPurge, purge.id)
            assert done is not None
            assert done.status == "complete"
    finally:
        await _cleanup_purge_rows(session_factory, user_id=user.id, lifecycle_id=lifecycle_id)


async def test_tenant_purge_worker_registered_in_channel_worker_fleet() -> None:
    import inspect

    from app.workers import channels as channel_workers

    source = inspect.getsource(channel_workers.build_channel_workers)
    assert "TenantPurgeWorker" in source
    assert "PrincipalLifecycleCleanupWorker" in source
