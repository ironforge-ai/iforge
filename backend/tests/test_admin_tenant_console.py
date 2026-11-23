"""Operator-console API regressions (ACCESS-01, ACCESS-04).

Covers the backend half of the 03-REVIEW operator-console findings:

- **W-05 / IN-04** — `GET /v1/admin/users` matched on `User.email` only, and
  `email` is nullable: admin-provisioned rows carry `email=NULL` until first
  direct sign-in and partner-tenant rows never have one, so those tenants were
  unreachable in BOTH the export and the deletion sections. The identifier an
  operator actually holds after an incident is a `user_id` (the backend logs
  `invite_redeemed clerk_id=… user_id=…` and every admin tenant route is keyed
  by it), which the picker could not accept at all.
- **W-01 backend half / T-03-56** — `AdminTenantPurgeInfo` exposed only
  `status`, `purge_after` and `completed_at`, so the deletion panel had nothing
  true to say about a FAILED purge. `error`, `attempt_count` and `claimed_at`
  already sit on the row.
- **CR-02 / WR-13** — the grace window was hardcoded in the web bundle and
  rendered inside the typed-confirmation dialog. `grace_days` puts the server's
  real window on the wire.
- **WR-09 route half / T-03-79** — `admin_delete_tenant` resolved the identity
  provider on the line AFTER `await db.commit()`, so a refused `user_prov`
  fenced the tenant, durably scheduled the irreversible purge, and handed the
  operator a 500 that reads as "nothing happened".

`test_tenant_export.py` (plan 03-18) and `test_tenant_delete_trigger.py`
(plan 03-15) own their own files; these assertions live here so the three
plans do not collide.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.main import app
from app.models.audit import ControlPlaneAuditEvent
from app.models.principal_lifecycle import PrincipalLifecycle
from app.models.tenant_purge import TENANT_PURGE_STATUS_FAILED, TenantPurge
from app.models.user import PRINCIPAL_KIND_PARTNER_TENANT, User
from app.routes.admin import admin_delete_tenant

pytestmark = pytest.mark.asyncio

_ADMIN_KEY = "tenant-console-admin-test"
_AUTH = {"X-Admin-Key": _ADMIN_KEY}
_ISSUER = "https://tenant-console.clerk.example.test"


@pytest_asyncio.fixture
async def console_admin_client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    """Client that does NOT inject auth — the admin surface tests the
    `X-Admin-Key` header gate directly (same pattern as
    `test_tenant_export.py`)."""

    async def _override_get_session():
        yield db_session

    original_admin_key = settings.admin_api_key
    settings.admin_api_key = _ADMIN_KEY
    app.dependency_overrides[get_session] = _override_get_session
    try:
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac
    finally:
        app.dependency_overrides.clear()
        settings.admin_api_key = original_admin_key


async def _add_clerk_user(
    db: AsyncSession,
    *,
    nonce: str,
    email: str | None,
    name: str | None = None,
) -> User:
    user = User(clerk_id=f"user_{nonce}", clerk_issuer=_ISSUER, email=email, name=name)
    db.add(user)
    await db.flush()
    return user


# --------------------------------------------------------------------------
# W-05 / IN-04 — the tenant picker must accept the identifier operators hold
# --------------------------------------------------------------------------


async def test_users_lookup_by_email_behaves_exactly_as_before(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The `?email=` contract is untouched: escaped case-insensitive
    substring match, capped, no 404 for a miss."""
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(
        db_session, nonce=nonce, email=f"Mixed-Case-{nonce}@clawdi.local", name="Email Tenant"
    )
    await db_session.commit()

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"email": f"mixed-case-{nonce}"}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(user.id)]
    assert items[0]["email"] == user.email
    assert items[0]["name"] == "Email Tenant"

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"email": f"absent-{uuid.uuid4().hex}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_users_lookup_by_q_matches_email_like_the_email_param(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=f"q-email-{nonce}@clawdi.local")
    await db_session.commit()

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": f"q-email-{nonce}"}
    )
    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()["items"]] == [str(user.id)]


async def test_users_lookup_by_q_finds_an_email_less_tenant_by_user_id(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """W-05: an admin-provisioned row has `email=NULL` until first direct
    sign-in, and the `user_id` is the identifier an operator holds after an
    incident. Both data-rights capabilities were unreachable for this tenant."""
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=None)
    await db_session.commit()

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(user.id)}
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["id"] for item in items] == [str(user.id)]
    assert items[0]["email"] is None


async def test_users_lookup_by_q_finds_an_email_less_tenant_by_clerk_id(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """`invite_redeemed clerk_id=… user_id=…` is the log line an operator
    reads; both halves of it must select the tenant."""
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=None)
    await db_session.commit()

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": f"user_{nonce}"}
    )
    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()["items"]] == [str(user.id)]


async def test_users_lookup_by_q_finds_a_partner_tenant(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Partner tenants have no clerk_id and never an email — the picker
    deliberately does NOT filter by principal kind, because they are exactly
    one of the populations W-05 reported as unreachable."""
    nonce = uuid.uuid4().hex
    partner = User(
        clerk_id=None,
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        partner_tenant_ref=f"partner-{nonce}",
        email=None,
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(partner.id)}
    )
    assert resp.status_code == 200, resp.text
    assert [item["id"] for item in resp.json()["items"]] == [str(partner.id)]


async def test_users_lookup_by_q_with_an_unmatched_uuid_is_an_empty_list(
    console_admin_client: httpx.AsyncClient,
) -> None:
    """A well-formed id that matches nothing is an empty list — not a 404 and
    not a 500 from an unguarded UUID parse."""
    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(uuid.uuid4())}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_users_lookup_with_an_unparseable_term_does_not_raise(
    console_admin_client: httpx.AsyncClient,
) -> None:
    """The UUID branch must simply not contribute for a non-UUID term."""
    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": "not-a-uuid-at-all"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_users_lookup_without_a_term_is_422(
    console_admin_client: httpx.AsyncClient,
) -> None:
    """The endpoint has no unfiltered mode — listing every tenant is not a
    capability this adds."""
    resp = await console_admin_client.get("/v1/admin/users", headers=_AUTH)
    assert resp.status_code == 422, resp.text


async def test_users_lookup_escapes_like_wildcards_in_q(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """T-03-57: `%` and `_` stay literal on the widened predicate, so `q=%`
    cannot enumerate the tenant table."""
    nonce = uuid.uuid4().hex
    await _add_clerk_user(db_session, nonce=nonce, email=f"wildcard-{nonce}@clawdi.local")
    await db_session.commit()

    resp = await console_admin_client.get("/v1/admin/users", headers=_AUTH, params={"q": "%"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


async def test_users_lookup_caps_results_at_twenty(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The 20-row cap applies on every branch."""
    nonce = uuid.uuid4().hex
    for index in range(21):
        await _add_clerk_user(
            db_session, nonce=f"{nonce}-{index}", email=f"capped-{nonce}-{index}@clawdi.local"
        )
    await db_session.commit()

    for params in ({"q": f"capped-{nonce}"}, {"email": f"capped-{nonce}"}):
        resp = await console_admin_client.get("/v1/admin/users", headers=_AUTH, params=params)
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["items"]) == 20, params


async def test_users_lookup_requires_admin_key(
    console_admin_client: httpx.AsyncClient,
) -> None:
    resp = await console_admin_client.get("/v1/admin/users", params={"q": str(uuid.uuid4())})
    assert resp.status_code == 401, resp.text


# --------------------------------------------------------------------------
# G-03-62 / RESEARCH Pitfall 8 — the console must EMIT a tenant identifier,
# not merely accept one, and an operator holding only a Clerk user id must
# reach the tenant without the removed invite table (D-01)
# --------------------------------------------------------------------------


async def _add_email_less_user(db: AsyncSession, *, nonce: str) -> User:
    """A Clerk tenant with no email: admin-provisioned rows carry
    `email=NULL` until first direct sign-in, so an email search cannot
    reach them and the operator needs another identifier."""
    user = User(clerk_id=f"user_{nonce}", clerk_issuer=_ISSUER, email=None)
    db.add(user)
    await db.flush()
    return user


async def test_operator_holding_a_clerk_id_resolves_the_tenant(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """Pitfall 8 capability journey: the invite table used to be the one
    surface that named an email-less tenant's `users.id`. With it removed
    (D-01), the identifier an operator holds is the Clerk user id copied
    from the Clerk Dashboard, and that alone must lead to the tenant's
    lifecycle.

    THE FIXTURE'S OWN users.id MUST NOT BE FED TO A REQUEST. The only
    out-of-band value is the Clerk id; the tenant id used for the lifecycle
    read is taken from the lookup response. A version of this test that
    reads the lifecycle by `user.id` proves nothing about discovery.
    """
    nonce = uuid.uuid4().hex
    user = await _add_email_less_user(db_session, nonce=nonce)
    await db_session.commit()
    clerk_id_from_dashboard = user.clerk_id
    assert clerk_id_from_dashboard is not None

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": clerk_id_from_dashboard}
    )
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["email"] is None
    assert item["clerk_id"] == clerk_id_from_dashboard
    obtained_id = item["id"]

    resp = await console_admin_client.get(
        f"/v1/admin/tenants/{obtained_id}/lifecycle", headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    assert obtained_id == str(user.id)


async def test_tenant_lookup_names_every_identifier_it_accepts(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """G-03-62 missing item 9: `admin_lookup_users` already matched
    `User.clerk_id == term` but never emitted it, so the third accepted
    identifier was unobtainable. The lookup response must name it, and the
    emitted value must resolve the same tenant."""
    nonce = uuid.uuid4().hex
    user = await _add_email_less_user(db_session, nonce=nonce)
    await db_session.commit()

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(user.id)}
    )
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    obtained_clerk_id = item["clerk_id"]
    assert obtained_clerk_id == f"user_{nonce}"

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": obtained_clerk_id}
    )
    assert resp.status_code == 200, resp.text
    assert [row["id"] for row in resp.json()["items"]] == [item["id"]]


async def test_tenant_lookup_names_a_partner_tenant_without_a_clerk_id(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """`User.clerk_id` is `Mapped[str | None]` and the table's own check
    constraint FORCES it NULL for `principal_kind='partner_tenant'`
    (models/user.py). Emitting it as a required `str` would make the lookup
    raise a Pydantic ValidationError for exactly the population W-05/IN-04
    named as unreachable, so the emitted field is nullable and a partner
    tenant resolves with `clerk_id: null` rather than a 500."""
    partner = User(
        clerk_id=None,
        principal_kind=PRINCIPAL_KIND_PARTNER_TENANT,
        partner_tenant_ref=f"partner-{uuid.uuid4().hex}",
        email=None,
    )
    db_session.add(partner)
    await db_session.commit()
    await db_session.refresh(partner)

    resp = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(partner.id)}
    )
    assert resp.status_code == 200, resp.text
    (item,) = resp.json()["items"]
    assert item["id"] == str(partner.id)
    assert item["clerk_id"] is None


async def test_tenant_directory_endpoints_forbid_caching(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """CR-02: three admin reads emit tenant-identifying bodies and must be
    marked uncacheable, like the other `_no_store` responses in admin.py.

    `SecurityHeadersMiddleware` sets nosniff/frame/referrer only, and the
    `X-Admin-Key` scheme puts these outside RFC 9111 section 3.5's
    shared-cache protection for `Authorization`, so a shared cache may
    store the response keyed on URL alone and replay it to a requester
    holding no admin key.

    `/v1/admin/users` is the most sensitive of the three: it emits
    `clerk_id` alongside `email`, `name` and `id`. The lifecycle and export
    reads name the same tenant and are covered with it.
    """
    user = await _add_email_less_user(db_session, nonce=uuid.uuid4().hex[:12])
    await db_session.commit()

    looked_up = await console_admin_client.get(
        "/v1/admin/users", headers=_AUTH, params={"q": str(user.id)}
    )
    assert looked_up.status_code == 200, looked_up.text
    assert looked_up.json()["items"], "fixture must match, or the header assertion proves nothing"
    assert looked_up.headers["cache-control"] == "no-store, private"
    assert looked_up.headers["pragma"] == "no-cache"

    lifecycle = await console_admin_client.get(
        f"/v1/admin/tenants/{user.id}/lifecycle", headers=_AUTH
    )
    assert lifecycle.status_code == 200, lifecycle.text
    assert lifecycle.headers["cache-control"] == "no-store, private"
    assert lifecycle.headers["pragma"] == "no-cache"

    exports = await console_admin_client.get(f"/v1/admin/tenants/{user.id}/exports", headers=_AUTH)
    assert exports.status_code == 200, exports.text
    assert exports.headers["cache-control"] == "no-store, private"
    assert exports.headers["pragma"] == "no-cache"


# --------------------------------------------------------------------------
# D-01 — the operator invite back office is gone
# --------------------------------------------------------------------------


async def test_admin_invite_routes_are_gone(
    console_admin_client: httpx.AsyncClient,
) -> None:
    """Clerk is the only sign-up authority (D-02), so no admin handler
    issues, lists or revokes invite codes (D-01). With a valid key all three
    former paths answer 404; no alias or redirect stands in for them."""
    resp = await console_admin_client.get("/v1/admin/invites", headers=_AUTH)
    assert resp.status_code == 404, resp.text

    resp = await console_admin_client.post(
        "/v1/admin/invites/batches", headers=_AUTH, json={"count": 1}
    )
    assert resp.status_code == 404, resp.text

    resp = await console_admin_client.post(
        f"/v1/admin/invites/{uuid.uuid4()}/revoke", headers=_AUTH
    )
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------
# W-01 backend half / WR-13 — the lifecycle view must report what ran
# --------------------------------------------------------------------------


async def _add_fenced_tenant_with_purge(
    db: AsyncSession,
    *,
    nonce: str,
    status: str | None = None,
    error: str | None = None,
    attempt_count: int = 0,
    claimed_at: datetime | None = None,
) -> tuple[User, TenantPurge]:
    """A fenced tenant plus one purge row in whatever state the test needs."""
    now = datetime.now(UTC)
    user = await _add_clerk_user(db, nonce=nonce, email=f"lifecycle-{nonce}@clawdi.local")
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
        subject=user.clerk_id or "",
        issuer=_ISSUER,
        purge_after=now + timedelta(days=30),
        next_attempt_at=now + timedelta(days=30),
        error=error,
        attempt_count=attempt_count,
        claimed_at=claimed_at,
        **({"status": status} if status is not None else {}),
    )
    db.add(purge)
    await db.commit()
    await db.refresh(purge)
    return user, purge


async def test_lifecycle_reports_the_evidence_of_a_failed_purge(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """W-01 / T-03-56: the deletion panel labelled a FAILED purge "Purge
    scheduled" against a date already in the past because the API gave it
    nothing else. `error`, `attempt_count` and `claimed_at` are already on the
    row; this is what makes "what already ran" answerable without reading the
    audit log."""
    nonce = uuid.uuid4().hex
    claimed_at = datetime.now(UTC) - timedelta(minutes=5)
    user, _purge = await _add_fenced_tenant_with_purge(
        db_session,
        nonce=nonce,
        status=TENANT_PURGE_STATUS_FAILED,
        error="synthetic S3 outage",
        attempt_count=3,
        claimed_at=claimed_at,
    )

    resp = await console_admin_client.get(f"/v1/admin/tenants/{user.id}/lifecycle", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["fenced"] is True
    purge_info = body["purge"]
    assert purge_info["status"] == "failed"
    assert purge_info["error"] == "synthetic S3 outage"
    assert purge_info["attempt_count"] == 3
    assert purge_info["claimed_at"] is not None
    assert datetime.fromisoformat(purge_info["claimed_at"]) == claimed_at


async def test_lifecycle_reports_a_never_claimed_purge_as_such(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """The negative control for the fields above: a pending purge that has
    never been claimed must not look like a failed one."""
    nonce = uuid.uuid4().hex
    user, _purge = await _add_fenced_tenant_with_purge(db_session, nonce=nonce)

    resp = await console_admin_client.get(f"/v1/admin/tenants/{user.id}/lifecycle", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    purge_info = resp.json()["purge"]
    assert purge_info["status"] == "pending"
    assert purge_info["error"] is None
    assert purge_info["attempt_count"] == 0
    assert purge_info["claimed_at"] is None


async def test_lifecycle_carries_the_servers_real_grace_window(
    console_admin_client: httpx.AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02 / WR-13: the console hardcoded TENANT_PURGE_GRACE_DAYS = 30 and
    rendered it inside the typed-confirmation dialog — the last text an
    operator reads before authorising an irreversible action. The window is a
    setting the client cannot know, so the server states it."""
    monkeypatch.setattr(settings, "tenant_purge_grace_days", 7)
    nonce = uuid.uuid4().hex
    user, _purge = await _add_fenced_tenant_with_purge(db_session, nonce=nonce)

    resp = await console_admin_client.get(f"/v1/admin/tenants/{user.id}/lifecycle", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["grace_days"] == 7


async def test_lifecycle_reports_the_grace_window_for_an_unfenced_tenant_too(
    console_admin_client: httpx.AsyncClient, db_session: AsyncSession
) -> None:
    """`grace_days` is a property of the server, not of a purge — the dialog
    needs it before anything has been scheduled."""
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=f"unfenced-{nonce}@clawdi.local")
    await db_session.commit()

    resp = await console_admin_client.get(f"/v1/admin/tenants/{user.id}/lifecycle", headers=_AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "fenced": False,
        "purge": None,
        "grace_days": settings.tenant_purge_grace_days,
    }


# --------------------------------------------------------------------------
# The delete-request audit records the window that produced its purge_after
# --------------------------------------------------------------------------


class _NoopProvider:
    """Records delete_identity calls; the D-02 seam is not under test here."""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def delete_identity(self, subject: str) -> None:
        self.deleted.append(subject)


@pytest.fixture
def delete_route_provider(monkeypatch: pytest.MonkeyPatch) -> _NoopProvider:
    from app.routes import admin as admin_routes

    provider = _NoopProvider()
    monkeypatch.setattr(admin_routes, "get_user_provisioning_provider", lambda: provider)
    return provider


async def test_delete_requested_audit_records_the_grace_window_it_applied(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    delete_route_provider: _NoopProvider,
) -> None:
    """The schedule an operator authorised must be auditable, not inferred
    from a config file read at a later date: the recorded window and the
    recorded `purge_after` are provably the same decision."""
    monkeypatch.setattr(settings, "tenant_purge_grace_days", 7)
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=f"audit-{nonce}@clawdi.local")
    await db_session.commit()

    response = await admin_delete_tenant(user.id, None, db=db_session)
    assert delete_route_provider.deleted == [f"user_{nonce}"]

    audit = (
        await db_session.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == "tenant.delete_requested",
                ControlPlaneAuditEvent.target_user_id == user.id,
            )
        )
    ).scalar_one()
    assert audit.details["grace_days"] == 7
    assert audit.details["purge_after"] == response.purge_after.isoformat()

    purge = (
        await db_session.scalars(select(TenantPurge).where(TenantPurge.user_id == user.id))
    ).one()
    assert purge.purge_after - purge.created_at < timedelta(days=7, seconds=5)
    assert purge.purge_after - purge.created_at > timedelta(days=6, hours=23)


# --------------------------------------------------------------------------
# WR-09 / T-03-79 — an unusable identity seam refuses BEFORE the fence commit
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_user_prov", ["supa", "vault"])
async def test_refused_identity_provider_commits_nothing_at_all(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    bad_user_prov: str,
) -> None:
    """At HEAD `get_user_provisioning_provider()` was called on the line AFTER
    `await db.commit()`, so a config value the factory refuses left the tenant
    irreversibly fenced, the one-way purge durably scheduled, the Clerk
    identity still alive, and the operator holding a 500 that reads as
    "nothing happened" — the worst direction for a confirmation error on a
    destructive action.

    Assert the ABSENCE of all three rows, not the status code: the call
    already failed before this change, and the durable side effects are the
    defect (WR-09 route half; plan 03-15 closed the settings-load half)."""
    monkeypatch.setattr(settings, "user_prov", bad_user_prov)
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=f"wr09-{nonce}@clawdi.local")
    await db_session.commit()

    with pytest.raises((NotImplementedError, ValueError)):
        await admin_delete_tenant(user.id, None, db=db_session)

    fences = (
        await db_session.execute(
            select(func.count())
            .select_from(PrincipalLifecycle)
            .where(PrincipalLifecycle.user_id == user.id)
        )
    ).scalar_one()
    assert fences == 0, "the tenant was fenced by a delete that could not complete"

    purges = (
        await db_session.execute(
            select(func.count()).select_from(TenantPurge).where(TenantPurge.user_id == user.id)
        )
    ).scalar_one()
    assert purges == 0, "an irreversible purge was scheduled by a delete that could not complete"

    audits = (
        await db_session.execute(
            select(func.count())
            .select_from(ControlPlaneAuditEvent)
            .where(
                ControlPlaneAuditEvent.action == "tenant.delete_requested",
                ControlPlaneAuditEvent.target_user_id == user.id,
            )
        )
    ).scalar_one()
    assert audits == 0, "a delete that never happened was recorded as requested"


async def test_default_provider_delete_still_fences_schedules_and_deletes_identity(
    db_session: AsyncSession,
    delete_route_provider: _NoopProvider,
) -> None:
    """The happy path is byte-identical after the hoist: fence, schedule,
    commit, THEN delete the identity."""
    nonce = uuid.uuid4().hex
    user = await _add_clerk_user(db_session, nonce=nonce, email=f"happy-{nonce}@clawdi.local")
    await db_session.commit()

    response = await admin_delete_tenant(user.id, None, db=db_session)
    assert response.purge_after is not None
    assert delete_route_provider.deleted == [f"user_{nonce}"]

    fences = (
        await db_session.execute(
            select(func.count())
            .select_from(PrincipalLifecycle)
            .where(PrincipalLifecycle.user_id == user.id)
        )
    ).scalar_one()
    assert fences == 1
    purge = (
        await db_session.scalars(select(TenantPurge).where(TenantPurge.user_id == user.id))
    ).one()
    assert purge.purge_after == response.purge_after
