"""First-touch provisioning coverage (06.3 D-01, D-02, ACCESS-02).

Clerk alone decides who signs up. A verified Clerk session with no `users`
row is provisioned with a Personal project on first touch, with no invite
step: `lazy_create_user_with_personal_project` is back in upstream's shape.
The billing gate still refuses that unpaid user on every gated route.

Exempt populations stay exempt (ROADMAP SC-5, RESEARCH A2): partner-tenant
rows are stamped billing-exempt at creation with one
`billing.exempt_provisioned` audit row. A Clerk user is never stamped at
creation, whichever path creates the row; an operator exempts one explicitly
through `PUT /v1/admin/auth/billing-exemptions` (WR-02, see
`test_admin_billing_exemption.py`).

The /v2 subscription read is stood in for by the counting stub from
`test_billing_gate.py`, injected through `billing_gate._transport_override`.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, settings
from app.models.audit import ControlPlaneAuditEvent
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.routes.admin import _resolve_or_create_user
from app.services import billing_gate
from app.services.billing_gate import ensure_unlocked
from app.services.managed_ai_provider import (
    MANAGED_AI_PROVIDER_PROVENANCE_CAPABILITY,
    V2_DEPLOYMENT_MANAGED_AI_PROVIDER_PREFIX,
    V2_MANAGED_AI_PROVIDER_ID,
)
from app.services.user_provisioning import (
    lazy_create_partner_user_with_personal_project,
    lazy_create_user_with_personal_project,
)
from tests.doc_contract import REPO_ROOT
from tests.test_billing_gate import (
    DEPLOY_API_KEY,
    DEPLOY_API_URL,
    SubscriptionStub,
    _install_stub,
    _real_auth_client,
    _seed_api_key,
    _subscription_response,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def billing_gate_on(monkeypatch: pytest.MonkeyPatch):
    """Billing gate on against the fake /v2 origin, with a clean TTL cache."""
    monkeypatch.setattr(settings, "billing_gate_enabled", True)
    monkeypatch.setattr(settings, "deploy_api_url", DEPLOY_API_URL)
    monkeypatch.setattr(settings, "deploy_api_key", DEPLOY_API_KEY)
    billing_gate.reset_billing_gate_cache()
    yield
    billing_gate.reset_billing_gate_cache()


async def _personal_project_count(db: AsyncSession, user_id) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(Project)
            .where(Project.user_id == user_id, Project.kind == PROJECT_KIND_PERSONAL)
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# Shape: no invite parameters, no invite setting
# ---------------------------------------------------------------------------


def test_helper_signature_is_upstream_shaped() -> None:
    """D-02: the provisioning helper takes no invite code and no invite
    exemption keyword; Clerk is the only admission signal."""
    params = inspect.signature(lazy_create_user_with_personal_project).parameters
    invite_params = sorted(name for name in params if "invite" in name)
    assert invite_params == []


def test_settings_have_no_invite_gate() -> None:
    """D-02: the invite flag is removed outright (no alias). The billing gate
    flag stays, defaulting to False so self-hosted deployments are unaffected."""
    invite_fields = sorted(name for name in Settings.model_fields if "invite" in name)
    assert invite_fields == []
    assert Settings.model_fields["billing_gate_enabled"].default is False


# ---------------------------------------------------------------------------
# First touch
# ---------------------------------------------------------------------------


async def test_first_verified_session_provisions_without_an_invite(
    db_session: AsyncSession,
    billing_gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """D-02: with the billing gate on, a fresh Clerk identity's first verified
    session is provisioned by GET /v1/auth/me (user row plus one Personal
    project, no invite.* audit row), and the same unpaid user then gets 403
    billing_required from a gated route."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    clerk_id = f"first_touch_{test_identity}"
    token = f"dev-first-touch-{test_identity}"
    monkeypatch.setattr(settings, "dev_auth_bypass", True)
    monkeypatch.setattr(settings, "dev_auth_token", token)
    monkeypatch.setattr(settings, "dev_auth_clerk_id", clerk_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with _real_auth_client(db_session) as client:
        me = await client.get("/v1/auth/me", headers=headers)
        assert me.status_code == 200, me.text

        rows = list(
            (await db_session.execute(select(User).where(User.clerk_id == clerk_id))).scalars()
        )
        assert len(rows) == 1
        user = rows[0]
        assert me.json()["id"] == str(user.id)
        assert await _personal_project_count(db_session, user.id) == 1
        invite_audit_rows = (
            await db_session.execute(
                select(func.count())
                .select_from(ControlPlaneAuditEvent)
                .where(
                    ControlPlaneAuditEvent.target_user_id == user.id,
                    ControlPlaneAuditEvent.action.startswith("invite."),
                )
            )
        ).scalar_one()
        assert invite_audit_rows == 0

        refused = await client.get("/v1/agents", headers=headers)
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == {"code": "billing_required"}


async def test_first_touch_with_no_email_or_name(
    db_session: AsyncSession, test_identity: str
) -> None:
    """ACCESS-02 (edge: empty): a first touch carrying no email and no name
    claims still provisions, with both columns NULL."""
    clerk_id = f"first_touch_empty_{test_identity}"

    user = await lazy_create_user_with_personal_project(
        db_session,
        clerk_id=clerk_id,
        email=None,
        name=None,
        race_loser_status=401,
    )
    await db_session.commit()

    row = (await db_session.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one()
    assert row.id == user.id
    assert row.email is None
    assert row.name is None
    assert await _personal_project_count(db_session, row.id) == 1


@pytest.mark.committed_db
async def test_concurrent_first_touch_adopts_the_winner(engine, test_identity: str) -> None:
    """ACCESS-02 (edge: adjacency): two concurrent first touches for one Clerk
    subject, on independent committed sessions, end with exactly one users row
    and one Personal project. The loser adopts the winner's row; nobody gets a
    409 or any other refusal."""
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    clerk_id = f"race_first_touch_{test_identity}"

    async def _first_touch():
        async with sessionmaker() as session:
            user = await lazy_create_user_with_personal_project(
                session,
                clerk_id=clerk_id,
                email=None,
                name=None,
                race_loser_status=401,
            )
            await session.commit()
            return user.id

    try:
        results = await asyncio.gather(_first_touch(), _first_touch(), return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        assert results[0] == results[1]

        async with sessionmaker() as check:
            rows = list(
                (await check.execute(select(User).where(User.clerk_id == clerk_id))).scalars()
            )
            assert len(rows) == 1
            assert rows[0].id == results[0]
            assert await _personal_project_count(check, rows[0].id) == 1
    finally:
        async with sessionmaker() as cleanup:
            user_ids = select(User.id).where(User.clerk_id == clerk_id)
            await cleanup.execute(
                delete(ControlPlaneAuditEvent).where(
                    ControlPlaneAuditEvent.target_user_id.in_(user_ids)
                )
            )
            await cleanup.execute(delete(User).where(User.clerk_id == clerk_id))
            await cleanup.commit()


# ---------------------------------------------------------------------------
# Existing rows
# ---------------------------------------------------------------------------


async def test_grandfathered_row_is_unaffected(
    db_session: AsyncSession,
    seed_user: User,
    billing_gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-01: a grandfathered row (invite_exempted_at and billing_unlocked_at
    set by the c7e2a9f4d5b8 backfill) reaches gated routes with the gate on,
    with zero /v2 calls."""
    now = datetime.now(UTC)
    seed_user.invite_exempted_at = now
    seed_user.billing_unlocked_at = now
    await db_session.commit()
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    raw_key = await _seed_api_key(db_session, seed_user)

    async with _real_auth_client(db_session) as client:
        resp = await client.get("/v1/agents", headers={"Authorization": f"Bearer {raw_key}"})
    assert resp.status_code == 200, resp.text
    assert stub.calls == 0


# ---------------------------------------------------------------------------
# Exempt populations (D-01, ROADMAP SC-5, RESEARCH A2)
# ---------------------------------------------------------------------------

EXEMPT_ACTION = "billing.exempt_provisioned"


async def _exempt_audit_details(db: AsyncSession, user_id) -> list[object]:
    rows = (
        await db.execute(
            select(ControlPlaneAuditEvent).where(
                ControlPlaneAuditEvent.action == EXEMPT_ACTION,
                ControlPlaneAuditEvent.target_user_id == user_id,
            )
        )
    ).scalars()
    return [row.details for row in rows]


async def _billing_exempt_audit_count(db: AsyncSession, user_id) -> int:
    """Audit rows of any billing.exempt_* action for the user."""
    return (
        await db.execute(
            select(func.count())
            .select_from(ControlPlaneAuditEvent)
            .where(
                ControlPlaneAuditEvent.target_user_id == user_id,
                ControlPlaneAuditEvent.action.startswith("billing.exempt_"),
            )
        )
    ).scalar_one()


async def test_admin_lazy_created_row_is_not_billing_exempt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_identity: str
) -> None:
    """WR-02: an admin route that first-touches a Clerk id (for example hosted
    deploy orchestration) creates the row without a billing exemption and
    writes no billing.exempt_provisioned audit row."""
    monkeypatch.setattr(settings, "clerk_jwt_issuer", "https://clerk.admin-exempt.example")
    clerk_id = f"admin_lazy_{test_identity}"

    user = await _resolve_or_create_user(db_session, clerk_id)
    await db_session.commit()

    assert user.clerk_id == clerk_id
    assert user.invite_exempted_at is None
    assert await _exempt_audit_details(db_session, user.id) == []


# ---------------------------------------------------------------------------
# No admin call that creates a Clerk account exempts it (WR-02, D-02)
#
# The exemption pins cover Clerk-principal writers. The operator route
# `PUT /auth/billing-exemptions` is the only one. Partner-tenant provisioning
# writes the column by design (see test_billing_exemption_writer_inventory).
# ---------------------------------------------------------------------------

_ADMIN_KEY = "first-touch-exemption-admin-test"
_ADMIN_AUTH = {"X-Admin-Key": _ADMIN_KEY}
_ADMIN_CLERK_ISSUER = "https://first-touch-routes.clerk.example.test"
_ADMIN_SOURCE = _BACKEND_ROOT / "app" / "routes" / "admin.py"
# Names whose reach marks an admin route as able to first-touch a Clerk id.
CLERK_FIRST_TOUCH_ENTRYPOINTS = frozenset(
    {"_resolve_or_create_user", "lazy_create_user_with_personal_project"}
)
# Reaches _resolve_or_create_user but is the operator exemption itself, pinned
# by test_admin_billing_exemption.py rather than by the never-exempts table.
OPERATOR_EXEMPTION_ROUTE = ("PUT", "/auth/billing-exemptions")


@pytest.fixture
def admin_api(anon_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    """`X-Admin-Key` client on the test session, with a configured Clerk
    issuer so admin first touch can resolve the owner's issuer."""
    monkeypatch.setattr(settings, "admin_api_key", _ADMIN_KEY)
    monkeypatch.setattr(settings, "clerk_jwt_issuer", _ADMIN_CLERK_ISSUER)
    return anon_client


async def _mint_api_key(client: httpx.AsyncClient, clerk_id: str) -> httpx.Response:
    return await client.post(
        "/v1/admin/auth/keys",
        headers=_ADMIN_AUTH,
        json={"target_clerk_id": clerk_id, "label": "first-touch"},
    )


async def _register_agent(client: httpx.AsyncClient, clerk_id: str) -> httpx.Response:
    return await client.post(
        "/v1/admin/agents",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": clerk_id,
            "agent_id": str(uuid4()),
            "machine_id": "first-touch-agent",
            "machine_name": "first-touch-agent-pod",
            "agent_type": "openclaw",
        },
    )


async def _register_environment(client: httpx.AsyncClient, clerk_id: str) -> httpx.Response:
    return await client.post(
        "/v1/admin/environments",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": clerk_id,
            "machine_id": "first-touch-environment",
            "machine_name": "first-touch-environment-pod",
            "agent_type": "openclaw",
        },
    )


async def _create_private_channel(client: httpx.AsyncClient, clerk_id: str) -> httpx.Response:
    return await client.post(
        "/v1/admin/channels",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": clerk_id,
            "provider": "telegram",
            "name": f"first-touch-{uuid4().hex}",
            "visibility": "private",
        },
    )


async def _upsert_fixed_managed_provider(
    client: httpx.AsyncClient, clerk_id: str
) -> httpx.Response:
    return await client.put(
        f"/v1/admin/ai-providers/{V2_MANAGED_AI_PROVIDER_ID}",
        headers=_ADMIN_AUTH,
        json={
            "target_clerk_id": clerk_id,
            "base_url": "https://ai-gateway.example.test/v1",
            "api_key": "sk-first-touch-fixed",
        },
    )


async def _upsert_deployment_managed_provider(
    client: httpx.AsyncClient, clerk_id: str
) -> httpx.Response:
    return await client.put(
        # Deployment ids are numeric.
        f"/v1/admin/ai-providers/{V2_DEPLOYMENT_MANAGED_AI_PROVIDER_PREFIX}4201",
        headers=_ADMIN_AUTH,
        json={
            "owner": {"kind": "clerk", "ref": clerk_id},
            "base_url": "https://ai-gateway.example.test/v1",
            "api_key": "sk-first-touch-deployment",
            "models": [{"id": "gpt-5.5"}],
            "capabilities": {
                "chat": True,
                MANAGED_AI_PROVIDER_PROVENANCE_CAPABILITY: "first-touch-intent",
            },
        },
    )


@dataclass(frozen=True)
class FirstTouchRoute:
    """One admin call that can create a Clerk user row. `path` is relative to
    the admin router, as written in its decorator."""

    id: str
    method: str
    path: str
    send: Callable[[httpx.AsyncClient, str], Awaitable[httpx.Response]]


# Every admin route that can first-touch a Clerk id, called for real by
# test_first_touch_admin_route_never_exempts. test_first_touch_admin_routes_are_all_pinned
# fails when admin.py grows a first-touch route that is not listed here.
FIRST_TOUCH_ADMIN_ROUTES = (
    FirstTouchRoute("auth-keys", "POST", "/auth/keys", _mint_api_key),
    FirstTouchRoute("agents", "POST", "/agents", _register_agent),
    FirstTouchRoute("environments", "POST", "/environments", _register_environment),
    FirstTouchRoute("channels", "POST", "/channels", _create_private_channel),
    FirstTouchRoute(
        "ai-provider-fixed",
        "PUT",
        "/ai-providers/{provider_id}",
        _upsert_fixed_managed_provider,
    ),
    FirstTouchRoute(
        "ai-provider-deployment-clerk-owner",
        "PUT",
        "/ai-providers/{provider_id}",
        _upsert_deployment_managed_provider,
    ),
)


@pytest.mark.parametrize("route", FIRST_TOUCH_ADMIN_ROUTES, ids=lambda route: route.id)
async def test_first_touch_admin_route_never_exempts(
    admin_api: httpx.AsyncClient,
    db_session: AsyncSession,
    test_identity: str,
    route: FirstTouchRoute,
) -> None:
    """WR-02 (regression class: an exemption written anywhere on an admin
    first-touch path, in admin.py or in any module it calls, by assignment,
    keyword, setattr or SQL). Called with a never-seen Clerk id, the route
    creates the user row gated, with no billing.exempt_* audit row."""
    clerk_id = f"first_touch_route_{test_identity}"
    assert (
        await db_session.execute(select(User).where(User.clerk_id == clerk_id))
    ).scalar_one_or_none() is None

    response = await route.send(admin_api, clerk_id)
    assert 200 <= response.status_code < 300, response.text

    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one()
    assert row.principal_kind == "clerk"
    assert row.invite_exempted_at is None
    assert await _exempt_audit_details(db_session, row.id) == []
    assert await _billing_exempt_audit_count(db_session, row.id) == 0


def _admin_routes_reaching(targets: frozenset[str]) -> set[tuple[str, str]]:
    """Router-decorated routes in admin.py whose call graph reaches any name
    in `targets`. A function reaches a name when its body references it, or
    references a module-level function that does; references count whether
    they are calls or values (`Depends(helper)`)."""
    tree = ast.parse(_ADMIN_SOURCE.read_text())
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    references = {
        name: {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}
        for name, node in functions.items()
    }

    reaching = set(targets)
    grew = True
    while grew:
        grew = False
        for name in functions:
            if name not in reaching and references[name] & reaching:
                reaching.add(name)
                grew = True

    routes: set[tuple[str, str]] = set()
    for name in reaching & functions.keys():
        for decorator in functions[name].decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and isinstance(decorator.func.value, ast.Name)
                and decorator.func.value.id.endswith("router")
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.args[0].value, str)
            ):
                routes.add((decorator.func.attr.upper(), decorator.args[0].value))
    return routes


def test_first_touch_admin_routes_are_all_pinned() -> None:
    """WR-02 (regression class: a new admin route that creates Clerk user
    rows without a behavioral pin). This is a completeness trigger for
    FIRST_TOUCH_ADMIN_ROUTES, not a writer pin: every route whose call graph
    reaches the Clerk lazy-create, other than the operator exemption route,
    must have a case in the table."""
    reaching = _admin_routes_reaching(CLERK_FIRST_TOUCH_ENTRYPOINTS)
    assert len(reaching) >= 5, f"the admin.py call-graph walk found only {sorted(reaching)}"
    assert OPERATOR_EXEMPTION_ROUTE in reaching
    pinned = {(route.method, route.path) for route in FIRST_TOUCH_ADMIN_ROUTES}
    assert reaching - {OPERATOR_EXEMPTION_ROUTE} == pinned


_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "clerk-sign-up-access.md"
_RUNBOOK_EXEMPTION_HEADING = "Exempting a Clerk account from billing"


def _runbook_exemption_section() -> str:
    lines = _RUNBOOK.read_text().splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.lstrip("#").strip() == _RUNBOOK_EXEMPTION_HEADING and line.startswith("#")
    )
    level = len(lines[start]) - len(lines[start].lstrip("#"))
    section = []
    for line in lines[start + 1 :]:
        heading = re.match(r"^(#+) ", line)
        if heading and len(heading.group(1)) <= level:
            break
        section.append(line)
    return "\n".join(section)


def test_runbook_names_only_existing_pins() -> None:
    """WR-02: the runbook's exemption guarantee cites tests by name. Every
    backticked test name in that section must be a test defined in this
    file, so retiring or renaming a pin cannot leave the runbook pointing at
    a test that no longer runs."""
    cited = sorted(set(re.findall(r"`(test_\w+)`", _runbook_exemption_section())))
    assert cited, "the runbook's exemption section cites no pin"
    module = globals()
    missing = [
        name
        for name in cited
        if not (inspect.isfunction(module.get(name)) and module[name].__module__ == __name__)
    ]
    assert missing == []


async def test_partner_tenant_row_is_billing_exempt_with_audit(
    db_session: AsyncSession, test_identity: str
) -> None:
    """SC-5: partner tenants have no hosted owner and cannot pay, so their
    row is stamped billing-exempt unconditionally, with one audit row."""
    ref = f"partner-exempt-{test_identity}"

    user = await lazy_create_partner_user_with_personal_project(
        db_session,
        partner_tenant_ref=ref,
        race_loser_status=500,
    )
    await db_session.commit()

    assert user.partner_tenant_ref == ref
    assert user.invite_exempted_at is not None
    assert await _exempt_audit_details(db_session, user.id) == [{"reason": "partner_provisioned"}]


async def test_partner_tenant_passes_the_billing_gate_without_a_v2_call(
    db_session: AsyncSession,
    billing_gate_on: None,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
) -> None:
    """RESEARCH Pitfall 11: with the billing gate on, a freshly provisioned
    partner tenant passes ensure_unlocked and /v2 is never called."""
    stub = SubscriptionStub(_subscription_response(None))
    _install_stub(monkeypatch, stub)
    user = await lazy_create_partner_user_with_personal_project(
        db_session,
        partner_tenant_ref=f"partner-gate-{test_identity}",
        race_loser_status=500,
    )
    await db_session.commit()

    refusal: object = None
    try:
        await ensure_unlocked(db_session, user)
    except HTTPException as exc:
        refusal = exc.detail
    assert refusal is None
    assert stub.calls == 0


@pytest.mark.parametrize("gate_enabled", [False, True])
async def test_self_signed_up_user_is_never_stamped(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_identity: str,
    gate_enabled: bool,
) -> None:
    """T-06.3-10-01: a self-signed-up Clerk user (the first-touch helper
    called with no exemption reason) is never billing-exempt, whatever the
    gate setting, and gets no billing.exempt_provisioned audit row."""
    monkeypatch.setattr(settings, "billing_gate_enabled", gate_enabled)
    clerk_id = f"self_signup_{test_identity}"

    user = await lazy_create_user_with_personal_project(
        db_session,
        clerk_id=clerk_id,
        email=f"{clerk_id}@example.com",
        name="Self Signup",
        race_loser_status=401,
    )
    await db_session.commit()

    assert user.invite_exempted_at is None
    assert await _exempt_audit_details(db_session, user.id) == []


async def test_race_loser_adopting_an_existing_row_writes_no_audit(
    db_session: AsyncSession, test_identity: str
) -> None:
    """T-06.3-10-04: a second partner provisioning call for a ref that
    already has a row adopts that row and writes no second audit row."""
    ref = f"partner-race-{test_identity}"

    first = await lazy_create_partner_user_with_personal_project(
        db_session, partner_tenant_ref=ref, race_loser_status=500
    )
    await db_session.commit()
    first_id = first.id

    second = await lazy_create_partner_user_with_personal_project(
        db_session, partner_tenant_ref=ref, race_loser_status=500
    )
    await db_session.commit()

    assert second.id == first_id
    assert await _exempt_audit_details(db_session, first_id) == [{"reason": "partner_provisioned"}]


# ---------------------------------------------------------------------------
# The Clerk lazy-create path can never exempt (WR-02, D-02)
# ---------------------------------------------------------------------------

EXEMPTION_COLUMN = "invite_exempted_at"
_SQL_WRITE_WORD = re.compile(r"\b(?:update|set)\b", re.IGNORECASE)

# Calls whose arguments are column -> value mappings for a write: SQLAlchemy's
# `.values()` / `.ordered_values()`, `on_conflict_do_update(set_=...)`, the
# `update()` / `insert()` constructs, a session's `execute()` with bulk
# parameter rows, and the legacy bulk mapping helpers (IN-04).
_WRITE_MAPPING_CALLS = frozenset(
    {
        "values",
        "ordered_values",
        "on_conflict_do_update",
        "update",
        "insert",
        "execute",
        "executemany",
        "bulk_update_mappings",
        "bulk_insert_mappings",
    }
)

# Every code path under backend/app that writes `users.invite_exempted_at`, as
# (path relative to backend/, innermost enclosing function) -> the number of
# writes in it. Adding a writer, or a second write inside a listed one, fails
# test_billing_exemption_writer_inventory until it is a deliberate policy
# change recorded here.
BILLING_EXEMPTION_WRITE_COUNTS: dict[tuple[str, str], int] = {
    # The operator route: the only writer for a Clerk account.
    ("app/routes/admin.py", "admin_set_billing_exemption"): 1,
    # Partner tenants have no hosted owner and cannot pay, so their rows
    # are exempt by design when they are created (SC-5).
    ("app/services/user_provisioning.py", "lazy_create_partner_user_with_personal_project"): 1,
    # Copies the authoritative row's value onto a detached snapshot. It
    # reads an exemption that already exists and grants nothing.
    ("app/services/billing_gate.py", "ensure_unlocked"): 1,
}
BILLING_EXEMPTION_WRITERS = frozenset(BILLING_EXEMPTION_WRITE_COUNTS)


def test_clerk_lazy_create_has_no_exemption_parameter() -> None:
    """WR-02 (regression class: an exemption keyword re-added to the Clerk
    lazy-create service). No caller can ask the service to exempt the row,
    whatever the parameter is called."""
    params = inspect.signature(lazy_create_user_with_personal_project).parameters
    offending = sorted(name for name in params if "exempt" in name or "invite" in name)
    assert offending == []


async def test_clerk_lazy_create_never_exempts(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_identity: str
) -> None:
    """WR-02 (regression class: the Clerk lazy-create service stamping the
    exemption itself, whichever caller reaches it). A fresh Clerk id is
    created gated, with no billing.exempt_* audit row."""
    issuer = "https://clerk.lazy-create-exempt.example"
    monkeypatch.setattr(settings, "clerk_jwt_issuer", issuer)
    clerk_id = f"lazy_never_exempt_{test_identity}"

    user = await lazy_create_user_with_personal_project(
        db_session,
        clerk_id=clerk_id,
        clerk_issuer=issuer,
        email=f"{clerk_id}@example.com",
        name="Lazy Create",
        race_loser_status=500,
    )
    await db_session.commit()

    db_session.expire_all()
    row = (await db_session.execute(select(User).where(User.clerk_id == clerk_id))).scalar_one()
    assert row.id == user.id
    assert row.invite_exempted_at is None
    assert await _exempt_audit_details(db_session, row.id) == []
    assert await _billing_exempt_audit_count(db_session, row.id) == 0


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the string constants that are module, class or function
    docstrings. Prose that mentions the column is not a write."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _write_mapping_dicts(tree: ast.AST) -> set[int]:
    """ids of the dict literals passed, at any depth, as an argument of a
    write-mapping call (`_WRITE_MAPPING_CALLS`): `.values({...})`,
    `on_conflict_do_update(set_={...})`, `execute(stmt, [{...}])`."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) not in _WRITE_MAPPING_CALLS:
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            found.update(id(child) for child in ast.walk(argument) if isinstance(child, ast.Dict))
    return found


def _reads_exemption_column(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Attribute) and child.attr == EXEMPTION_COLUMN
        for child in ast.walk(node)
    )


def _writes_exemption_column(
    node: ast.AST, docstrings: set[int], write_dicts: set[int] | None = None
) -> bool:
    # Dict keys (IN-04). A column attribute as a key -- `.values({User.col: v})`
    # -- is a write mapping wherever it appears. A string key is a write inside a
    # write-mapping call (`.values({"col": v})`, `set_={"col": v}`), and anywhere
    # else unless its value just reads the same column back: that shape is a
    # projection (`{"col": _iso(row.col)}` in an export or a response), while
    # `payload = {"col": now}` handed to `.values(payload)` later is caught.
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Attribute) and key.attr == EXEMPTION_COLUMN:
                return True
            if isinstance(key, ast.Constant) and key.value == EXEMPTION_COLUMN:
                if id(node) in (write_dicts or set()) or not _reads_exemption_column(value):
                    return True
        return False
    # Attribute assignment in any store position: `x.col = v`, `x.col += v`,
    # `x.col: T = v`, tuple unpacking, for/with targets.
    if (
        isinstance(node, ast.Attribute)
        and node.attr == EXEMPTION_COLUMN
        and isinstance(node.ctx, ast.Store)
    ):
        return True
    # Keyword argument: `User(col=v)`, `.values(col=v)`.
    if isinstance(node, ast.keyword) and node.arg == EXEMPTION_COLUMN:
        return True
    # `setattr(x, "col", v)`, also reached as an attribute (`builtins.setattr`).
    if isinstance(node, ast.Call):
        if (
            _call_name(node) == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == EXEMPTION_COLUMN
        ):
            return True
    # Raw SQL: a string naming the column together with UPDATE or SET.
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and EXEMPTION_COLUMN in node.value
        and _SQL_WRITE_WORD.search(node.value) is not None
    )


def _exemption_writes_in_source(source: str, relative: str) -> dict[tuple[str, str], int]:
    """(relative, innermost enclosing function) -> the number of writes of
    `invite_exempted_at` in one module's source."""
    tree = ast.parse(source, filename=relative)
    docstrings = _docstring_nodes(tree)
    write_dicts = _write_mapping_dicts(tree)
    writes: dict[tuple[str, str], int] = {}

    def visit(node: ast.AST, scope: str) -> None:
        if _writes_exemption_column(node, docstrings, write_dicts):
            writes[(relative, scope)] = writes.get((relative, scope), 0) + 1
        for child in ast.iter_child_nodes(node):
            child_scope = (
                child.name if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else scope
            )
            visit(child, child_scope)

    visit(tree, "<module>")
    return writes


def _billing_exemption_writes() -> dict[tuple[str, str], int]:
    """(path relative to backend/, innermost enclosing function) -> write count
    for every write of `invite_exempted_at` in any module under backend/app."""
    writes: dict[tuple[str, str], int] = {}
    for path in sorted((_BACKEND_ROOT / "app").rglob("*.py")):
        relative = path.relative_to(_BACKEND_ROOT).as_posix()
        writes.update(_exemption_writes_in_source(path.read_text(), relative))
    return writes


def test_billing_exemption_writer_inventory() -> None:
    """WR-02 (regression class: an exemption writer in any module, written as
    an attribute assignment, a keyword argument, a setattr call, raw SQL or a
    dict-key column mapping). The app-wide set of code paths writing the
    column is exactly the three recorded in BILLING_EXEMPTION_WRITE_COUNTS,
    each with its recorded number of writes, so a second write inside a
    listed function fails too (IN-04). Unlike a walk over admin.py alone,
    this sees cross-module writers such as the provisioning services."""
    writes = _billing_exemption_writes()
    assert writes, "found no writer of invite_exempted_at under backend/app; the scan is broken"
    assert set(writes) == BILLING_EXEMPTION_WRITERS
    assert writes == BILLING_EXEMPTION_WRITE_COUNTS


@pytest.mark.parametrize(
    "snippet",
    [
        # IN-04: dict-key column writes.
        'def f():\n    update(User).values({"invite_exempted_at": now})\n',
        "def f():\n    update(User).values({User.invite_exempted_at: now})\n",
        "def f():\n    insert(User).on_conflict_do_update(\n"
        '        index_elements=["id"], set_={"invite_exempted_at": now}\n    )\n',
        'def f():\n    db.execute(update(User), [{"id": i, "invite_exempted_at": now}])\n',
        'def f():\n    payload = {"invite_exempted_at": now}\n    stmt.values(payload)\n',
        # A copy of the column handed to a write call is still a write.
        'def f():\n    stmt.values({"invite_exempted_at": other.invite_exempted_at})\n',
        # The shapes the scanner already caught.
        "def f():\n    user.invite_exempted_at = now\n",
        "def f():\n    User(invite_exempted_at=now)\n",
        'def f():\n    setattr(user, "invite_exempted_at", now)\n',
        'def f():\n    text("UPDATE users SET invite_exempted_at = now()")\n',
    ],
)
def test_exemption_scanner_detects_each_write_shape(snippet: str) -> None:
    """Self-test: each write shape is reported once, in its enclosing function."""
    assert _exemption_writes_in_source(snippet, "probe.py") == {("probe.py", "f"): 1}


@pytest.mark.parametrize(
    "snippet",
    [
        # A projection reads the column into a same-named key; it writes nothing.
        'def f():\n    return {"invite_exempted_at": _iso(row.invite_exempted_at)}\n',
        "def f():\n    return user.invite_exempted_at is not None\n",
        'def f():\n    """Sets invite_exempted_at via UPDATE ... SET."""\n',
    ],
)
def test_exemption_scanner_ignores_reads(snippet: str) -> None:
    assert _exemption_writes_in_source(snippet, "probe.py") == {}


def test_exemption_scanner_counts_a_second_write_in_one_function() -> None:
    """IN-04: a whitelisted function gaining a second write changes its count."""
    snippet = (
        "def ensure_unlocked():\n"
        "    user.invite_exempted_at = db_user.invite_exempted_at\n"
        '    stmt.values({"invite_exempted_at": now})\n'
    )
    assert _exemption_writes_in_source(snippet, "probe.py") == {("probe.py", "ensure_unlocked"): 2}
