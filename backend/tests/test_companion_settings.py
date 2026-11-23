"""The dashboard's settings surface over controls the façade already enforces (TOOL-02, TOOL-04).

Every route this suite covers is a NEW AUTHENTICATED WRITE onto a safety control, and specifically
one where a person WEAKENS it -- raising a blast-radius cap, turning destroy tools on, setting a
tool to run without asking. Nothing here adds a gate; everything here is a way to move one.

**The thing most likely to go wrong is not a broken query.** It is a surface that reports a mode the
server would not honour. ``evaluate_policy`` refuses a ``destroy`` verb for a companion whose switch
is off FIRST and unconditionally, before it ever looks at the tenant's explicit row -- so a tenant
who stored ``allow`` on such a verb has stored a mode the next call will refuse. A catalog that
showed only the stored value would display a tool as running freely at the exact moment the server
stops allowing it. That is why the stored mode and the resolved mode are separate fields here, and
why the assertions below insist on both.

**The second thing most likely to go wrong is a second implementation of agent visibility.** The
catalog deliberately reports MORE than ``tools/list`` does -- it shows destroy verbs while they are
hidden from the agent, which is what makes the switch explicable at all. The exposure FLAG it
carries must nonetheless agree exactly with what the agent sees, so
``test_the_catalog_agent_visibility_equals_the_agent_tool_list`` compares the two name sets
directly rather than trusting two implementations to agree on paper.

Tenant isolation is asserted in both halves throughout, for the reason
``test_companion_provenance.py`` records: a query broken badly enough to return nothing satisfies
"no foreign rows" perfectly, so the caller's own rows must be asserted present in the same test.
"""

from __future__ import annotations

import inspect
import uuid as uuid_module
from datetime import UTC, datetime, timedelta
from typing import Any, get_args

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

import app.routes.companions as companions_routes
from app.core.auth import AuthContext
from app.core.companion_caps import DEFAULT_CAPS, REASON_PER_CALL_CAP_EXCEEDED
from app.core.companion_policy import (
    CLASS_DEFAULT_MODES,
    POLICY_MODES,
    REASON_CLASS_DEFAULT,
    REASON_DESTROY_DISABLED,
    REASON_POLICY_ROW,
)
from app.core.companion_registry import COMPANION_VERBS
from app.models.audit import ControlPlaneAuditEvent
from app.models.companion_session_budget import (
    SESSION_BUDGET_WINDOW_SECONDS,
    CompanionCapPolicy,
    CompanionSessionBudget,
)
from app.models.companion_tool_policy import CompanionDestroyEnablement, CompanionToolPolicy
from app.models.project import PROJECT_KIND_PERSONAL, Project
from app.models.user import User
from app.schemas.companions import CompanionCapVectorRequest, CompanionPolicyMode
from app.services import companion_facade
from app.services.companion_facade import (
    budget_window,
    call_companion_tool,
    list_companion_tools,
    reset_companion_tools_cache,
)
from tests.hosted_runtime_fixtures import deploy_companion_state

CATALOG_PATH = "/v1/companions/tools"
CAPS_PATH = "/v1/companions/caps"
CONSUMPTION_PATH = "/v1/companions/consumption"

DESTROY_TOOL = "filebrowser__delete_path"
READ_TOOL = "filebrowser__list_dir"

CAP_FIELDS = (
    "per_call_calls",
    "per_call_records",
    "per_call_bytes",
    "per_session_calls",
    "per_session_records",
    "per_session_bytes",
)

# The six paths this plan adds, as the request each needs to answer 200. Spelled once so the alias
# assertion and the prefix assertion cover the SAME set -- two hand-maintained lists is how one of
# them ends up short by the route that mattered.
_NEW_SETTINGS_REQUESTS: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("GET", "/companions/tools", None),
    ("PUT", f"/companions/tools/{READ_TOOL}/policy", {"mode": "ask"}),
    ("PUT", "/companions/destroy-enablement/filebrowser", {"enabled": False}),
    ("GET", "/companions/caps", None),
    ("PUT", "/companions/caps/filebrowser", dict.fromkeys(CAP_FIELDS, 1)),
    ("GET", "/companions/consumption", None),
)

_NEW_SETTINGS_PATH_TEMPLATES = (
    "/v1/companions/tools",
    "/v1/companions/tools/{tool}/policy",
    "/v1/companions/destroy-enablement/{companion_key}",
    "/v1/companions/caps",
    "/v1/companions/caps/{companion_key}",
    "/v1/companions/consumption",
)


def _policy_path(tool: str) -> str:
    return f"/v1/companions/tools/{tool}/policy"


def _destroy_path(companion_key: str) -> str:
    return f"/v1/companions/destroy-enablement/{companion_key}"


def _caps_path(companion_key: str) -> str:
    return f"{CAPS_PATH}/{companion_key}"


def _default_payload() -> dict[str, int]:
    """The shipped defaults as a request body -- built FROM ``DEFAULT_CAPS``, never restated.

    A hand-copied literal is how "saving the shipped numbers verbatim" stops being what the test
    thinks it is the first time one of the six is retuned.
    """
    return {field: getattr(DEFAULT_CAPS, field) for field in CAP_FIELDS}


def _cap_values(item: dict[str, Any]) -> dict[str, int]:
    return {field: item[field] for field in CAP_FIELDS}


def _cap_item(body: dict[str, Any], companion_key: str) -> dict[str, Any]:
    matches = [row for row in body["items"] if row["companion_key"] == companion_key]
    assert matches, f"{companion_key} is absent from the cap listing: {body['items']}"
    return matches[0]


async def _budget_row(
    db_session,
    user: User,
    *,
    environment_id,
    calls: int = 0,
    records: int = 0,
    consumed_bytes: int = 0,
    window_started_at: datetime | None = None,
    session_id: str | None = None,
) -> None:
    """One budget row in the window the read-out will name, unless told otherwise.

    ``session_id=None`` is the WINDOW ledger's row; a string makes it a SESSION ledger row, whose
    ``window_started_at`` is when that budget opened rather than which hour it belongs to.
    """
    db_session.add(
        CompanionSessionBudget(
            user_id=user.id,
            environment_id=environment_id,
            companion_key="filebrowser",
            session_id=session_id,
            window_started_at=window_started_at or budget_window(datetime.now(UTC)),
            calls_consumed=calls,
            records_consumed=records,
            bytes_consumed=consumed_bytes,
        )
    )
    await db_session.commit()


async def _count(db_session, model, user: User) -> int:
    return int(
        await db_session.scalar(
            select(func.count()).select_from(model).where(model.user_id == user.id)
        )
    )


async def _audit_events(db_session, user: User) -> list[ControlPlaneAuditEvent]:
    """This tenant's control-plane audit trail, as rows rather than as a count.

    The rows rather than the count because "exactly one event per write" is only half the claim --
    the other half is that the event names the tenant, the surface and the resource that moved.
    """
    result = await db_session.execute(
        select(ControlPlaneAuditEvent).where(ControlPlaneAuditEvent.target_user_id == user.id)
    )
    return list(result.scalars().all())


async def _other_tenant(db_session) -> User:
    """A second tenant with the minimum graph a deployment needs.

    Copied from ``test_companion_provenance.py`` rather than imported: that module builds it for
    its own fixtures, and a cross-module import of a private helper would tie two suites' setup
    together for no gain.
    """
    other = User(
        clerk_id=f"test_other_{uuid_module.uuid4().hex[:12]}",
        email=f"other-{uuid_module.uuid4().hex[:12]}@clawdi.local",
        name="Other Tenant",
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(
        Project(user_id=other.id, name="Personal", slug="personal", kind=PROJECT_KIND_PERSONAL)
    )
    await db_session.commit()
    return other


async def _policy_row(db_session, user: User, *, verb: str, mode: str) -> CompanionToolPolicy:
    row = CompanionToolPolicy(
        user_id=user.id,
        companion_key="filebrowser",
        verb=verb,
        mode=mode,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _enable_destroy(db_session, user: User, *, companion_key: str = "filebrowser") -> None:
    db_session.add(
        CompanionDestroyEnablement(
            user_id=user.id,
            companion_key=companion_key,
            enabled_at=datetime.now(UTC),
        )
    )
    await db_session.commit()


def _tool(body: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [row for row in body["tools"] if row["tool"] == name]
    assert matches, f"{name} is absent from the catalog: {[row['tool'] for row in body['tools']]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Task 1 -- the catalog.
# ---------------------------------------------------------------------------


async def test_the_catalog_covers_every_verb_of_a_deployed_companion(
    db_session, seed_user, client
) -> None:
    """Including destroy verbs while destroy is disabled.

    The dashboard sees what is hidden from the agent, and that asymmetry is the whole reason the
    switch is explicable: a surface that showed only what the agent can already see could never
    offer the control that changes it.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="catalog")

    response = await client.get(CATALOG_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    expected = {name for name in COMPANION_VERBS if name.startswith("filebrowser__")}
    assert {row["tool"] for row in body["tools"]} == expected
    destroy = _tool(body, DESTROY_TOOL)
    assert destroy["tool_class"] == "destroy"
    assert destroy["destroy_enabled"] is False
    assert destroy["exposed_to_agent"] is False


async def test_the_catalog_reports_class_cap_unit_and_dry_run_availability(
    db_session, seed_user, client
) -> None:
    """Every row carries what the registry says about the verb, not a second copy of it."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="facets")

    body = (await client.get(CATALOG_PATH)).json()

    for row in body["tools"]:
        entry = COMPANION_VERBS[row["tool"]]
        assert row["tool_class"] == entry.tool_class
        assert row["cap_unit"] == entry.cap_unit
        assert row["dry_run_available"] is (entry.tool_class in {"create", "mutate", "destroy"})
        assert row["companion_key"] == row["tool"].split("__", 1)[0]
        assert row["verb"] == row["tool"].split("__", 1)[1]


async def test_the_catalog_agent_visibility_equals_the_agent_tool_list(
    db_session, seed_user, client
) -> None:
    """The most important assertion in this file: one comparison, not two agreeing implementations.

    The catalog computes exposure through ``is_exposed_to_agent`` -- the façade's own predicate --
    precisely so this equality is structural. It is asserted anyway, because a second caller of
    the same predicate can still pass it the wrong state.
    """
    reset_companion_tools_cache()
    await deploy_companion_state(db_session, seed_user, machine_prefix="parity")
    await _enable_destroy(db_session, seed_user)
    await _policy_row(db_session, seed_user, verb="list_dir", mode="deny")

    agent_names = {
        tool["name"] for tool in await list_companion_tools(AuthContext(user=seed_user), db_session)
    }
    body = (await client.get(CATALOG_PATH)).json()
    catalog_names = {row["tool"] for row in body["tools"] if row["exposed_to_agent"]}

    assert agent_names, "the agent saw no companion tools at all; the comparison would be vacuous"
    assert catalog_names == agent_names


async def test_a_tenant_with_nothing_deployed_reads_empty_lists_and_not_a_not_found(
    seed_user, client
) -> None:
    """Their catalog exists and is empty. A 404 would say the surface does not."""
    response = await client.get(CATALOG_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tools"] == []
    assert body["companions"] == []


async def test_a_verb_with_no_policy_row_carries_a_null_stored_mode_and_its_class_default(
    db_session, seed_user, client
) -> None:
    """Null is not a mode and not a refusal -- it means "fall through to the class default".

    This phase has collapsed null into a neighbouring value three times already. The stored mode is
    the place it would happen here.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="default")

    body = (await client.get(CATALOG_PATH)).json()

    for row in body["tools"]:
        if row["tool"] == DESTROY_TOOL:
            continue  # Destroy-disabled is refused ahead of the class default; asserted below.
        assert row["stored_mode"] is None
        assert row["resolved_mode"] == CLASS_DEFAULT_MODES[row["tool_class"]]
        assert row["resolved_reason"] == REASON_CLASS_DEFAULT


async def test_an_allow_row_on_a_destroy_verb_with_destroy_off_resolves_to_deny(
    db_session, seed_user, client
) -> None:
    """Stored ``allow``, resolved ``deny``, reason ``refused_destroy_disabled`` -- all three.

    ``evaluate_policy`` checks destroy enablement first and unconditionally, so this stored value is
    one the server will not honour. Reporting only the stored half would display a guarantee that
    does not exist; rewriting the stored half to match would discard a preference the tenant
    expressed and will want when they turn the switch on.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="overridden")
    await _policy_row(db_session, seed_user, verb="delete_path", mode="allow")

    row = _tool((await client.get(CATALOG_PATH)).json(), DESTROY_TOOL)

    assert row["stored_mode"] == "allow"
    assert row["resolved_mode"] == "deny"
    assert row["resolved_reason"] == REASON_DESTROY_DISABLED


async def test_an_explicit_deny_row_on_a_read_verb_reads_back_as_an_explicit_policy_row(
    db_session, seed_user, client
) -> None:
    """The ordinary case, and the one that proves the reason token is not a constant."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="explicit")
    await _policy_row(db_session, seed_user, verb="list_dir", mode="deny")

    row = _tool((await client.get(CATALOG_PATH)).json(), READ_TOOL)

    assert row["stored_mode"] == "deny"
    assert row["resolved_mode"] == "deny"
    assert row["resolved_reason"] == REASON_POLICY_ROW


async def test_the_companion_summary_separates_having_destroy_verbs_from_having_them_enabled(
    db_session, seed_user, client
) -> None:
    """Two different situations a renderer must be able to tell apart.

    A companion with no destroy verb at all and a companion whose destroy group is merely off both
    show zero usable destroy tools. Offering a switch for the first would be offering a control
    that governs nothing.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="summary")

    body = (await client.get(CATALOG_PATH)).json()
    summary = next(row for row in body["companions"] if row["companion_key"] == "filebrowser")

    assert summary["has_destroy_verbs"] is True
    assert summary["destroy_enabled"] is False
    assert summary["environment_ids"], "a deployed companion must name where it is deployed"
    visible = [row for row in body["tools"] if row["exposed_to_agent"]]
    assert summary["visible_verb_count"] == len(visible)
    assert summary["visible_verb_count"] < len(body["tools"])


async def test_the_summary_reports_destroy_enabled_once_the_switch_is_on(
    db_session, seed_user, client
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="switched")
    await _enable_destroy(db_session, seed_user)

    body = (await client.get(CATALOG_PATH)).json()
    summary = next(row for row in body["companions"] if row["companion_key"] == "filebrowser")

    assert summary["has_destroy_verbs"] is True
    assert summary["destroy_enabled"] is True
    assert _tool(body, DESTROY_TOOL)["exposed_to_agent"] is True


async def test_tenant_isolation(db_session, seed_user, client) -> None:
    """The foreign tenant's state is absent AND the caller's own is present and non-zero.

    Both halves, for the reason ``test_companion_provenance.py`` records: a query broken badly
    enough to return nothing satisfies the first half perfectly.
    """
    other = await _other_tenant(db_session)
    foreign_env, _ = await deploy_companion_state(db_session, other, machine_prefix="foreign")
    await _policy_row(db_session, other, verb="list_dir", mode="deny")
    await _enable_destroy(db_session, other)
    await deploy_companion_state(db_session, seed_user, machine_prefix="mine")

    body = (await client.get(CATALOG_PATH)).json()

    assert body["tools"], "the caller's own catalog was empty; the isolation claim would be vacuous"
    assert _tool(body, READ_TOOL)["stored_mode"] is None
    assert _tool(body, DESTROY_TOOL)["destroy_enabled"] is False
    summary = next(row for row in body["companions"] if row["companion_key"] == "filebrowser")
    assert str(foreign_env) not in {str(value) for value in summary["environment_ids"]}


def test_no_companion_settings_route_accepts_a_tenant_identifier_parameter() -> None:
    """The structural half of the scoping claim: there is no parameter to get wrong.

    Asserted over the whole router rather than over one endpoint, so a route added later to this
    file is covered the moment it exists rather than when somebody remembers to extend the list.
    """
    forbidden = {"user_id", "tenant_id", "target_user_id", "owner_user_id", "clerk_id"}
    for route in companions_routes.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        names = set(inspect.signature(endpoint).parameters)
        assert not (names & forbidden), f"{endpoint.__name__} accepts {names & forbidden}"


async def test_the_catalog_rejects_a_cli_credential(db_session, seed_user, cli_client) -> None:
    """An agent authenticates with a CLI key, and D-09 puts this surface outside its reach.

    The catalog reports MORE than ``tools/list`` does -- destroy verbs while they are hidden, and
    the stored policy behind every row. A CLI key reaching it would be a second, wider path into
    the gates' own configuration, held by the party the gates exist to bound.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="cliauth")

    response = await cli_client.get(CATALOG_PATH)

    assert response.status_code == 403


async def test_the_catalog_has_no_hidden_api_alias(db_session, seed_user, client) -> None:
    """Canonical-only, asked as a live request rather than read out of the mount table.

    A hidden alias is ``include_in_schema=False`` by definition, so it is absent from the OpenAPI
    schema whether or not it is mounted; only a request can tell the two apart.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="alias")

    canonical = await client.get(CATALOG_PATH)
    aliased = await client.get("/api/companions/tools")

    assert canonical.status_code == 200
    assert aliased.status_code == 404


def test_the_mode_literal_mirrors_the_policy_vocabulary() -> None:
    """One vocabulary, two spellings -- so the mirror cannot drift unnoticed.

    ``POLICY_MODES`` is a frozenset rather than an enum, so the schema mirrors it as a ``Literal``
    (the ``CompanionApprovalStatus`` precedent) instead of importing it the way the activity log
    imports ``CallDecision``. A mirror nothing asserts is a copy waiting to disagree.
    """
    assert set(get_args(CompanionPolicyMode)) == set(POLICY_MODES)


def test_the_promoted_facade_helpers_have_exactly_one_live_name() -> None:
    """Three predicates, three names -- and none of the former private names still resolves.

    The settings service computes agent visibility through the façade's OWN predicate, because a
    second implementation is a second thing that can drift and the drift would show a tenant a
    destroy verb as hidden while the agent could still see it. Leaving the private aliases in place
    would reintroduce the same hazard one level down: two names for one predicate is one name that
    can be changed without the other.
    """
    for name in (
        "is_exposed_to_agent",
        "deployed_companion_kinds",
        "destroy_enabled_companion_keys",
    ):
        assert hasattr(companion_facade, name), f"{name} is not public on the façade"
    for name in ("_is_exposed", "_deployed_companion_kinds", "_destroy_enabled_companion_keys"):
        assert not hasattr(companion_facade, name), (
            f"{name} still resolves alongside its public name"
        )


async def test_the_catalog_is_ordered_and_free_of_duplicates(db_session, seed_user, client) -> None:
    """Sorted by tool name, so two reads of unchanged state render identically."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="ordered")

    names = [row["tool"] for row in (await client.get(CATALOG_PATH)).json()["tools"]]

    assert names == sorted(names)
    assert len(names) == len(set(names))


@pytest.mark.parametrize("mode", sorted(POLICY_MODES))
async def test_every_mode_in_the_vocabulary_round_trips_through_the_catalog(
    db_session, seed_user, client, mode: str
) -> None:
    """Generated from the vocabulary, so a fourth mode arrives already covered."""
    await deploy_companion_state(db_session, seed_user, machine_prefix=f"mode{mode}")
    await _policy_row(db_session, seed_user, verb="list_dir", mode=mode)

    row = _tool((await client.get(CATALOG_PATH)).json(), READ_TOOL)

    assert row["stored_mode"] == mode
    assert row["resolved_reason"] == REASON_POLICY_ROW
    assert row["resolved_mode"] == mode


async def test_the_catalog_reads_only_active_policy_rows(db_session, seed_user, client) -> None:
    """A revoked row is history, not policy.

    The unique partial index is over live rows only, which is what lets a revoked row sit beside
    the row that replaced it -- and what makes "read the active one" a claim worth asserting rather
    than a property the schema hands over for free.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="revoked")
    revoked = await _policy_row(db_session, seed_user, verb="list_dir", mode="deny")
    revoked.revoked_at = datetime.now(UTC)
    db_session.add(revoked)
    await db_session.commit()

    row = _tool((await client.get(CATALOG_PATH)).json(), READ_TOOL)

    assert row["stored_mode"] is None
    assert row["resolved_reason"] == REASON_CLASS_DEFAULT
    live = await db_session.execute(
        select(CompanionToolPolicy).where(CompanionToolPolicy.user_id == seed_user.id)
    )
    assert len(live.scalars().all()) == 1


# ---------------------------------------------------------------------------
# Task 2 -- the two independent switches.
# ---------------------------------------------------------------------------


async def test_setting_a_tool_policy_reads_back_through_the_catalog(
    db_session, seed_user, client
) -> None:
    """The write's own response is the RE-RESOLVED row, not an echo of the request.

    The client then renders from the server's own resolution, which is what keeps a safety control
    from flipping optimistically to a state the server does not hold.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="setpolicy")

    written = await client.put(_policy_path(READ_TOOL), json={"mode": "deny"})

    assert written.status_code == 200, written.text
    body = written.json()
    assert body["tool"] == READ_TOOL
    assert body["stored_mode"] == "deny"
    assert body["resolved_mode"] == "deny"
    assert body["resolved_reason"] == REASON_POLICY_ROW
    assert body == _tool((await client.get(CATALOG_PATH)).json(), READ_TOOL)


async def test_setting_a_policy_twice_revokes_rather_than_updating_in_place(
    db_session, seed_user, client
) -> None:
    """One active row and one revoked row -- the loosening-and-tightening history survives.

    An in-place ``UPDATE`` of ``mode`` satisfies ``uq_active_companion_tool_policy`` exactly as
    well and passes every test that only reads the current mode. It would also silently discard the
    history ``revoked_at`` exists for, which the model's own docstring names as what an approval
    decision most needs to be auditable against.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="history")

    first = await client.put(_policy_path(READ_TOOL), json={"mode": "deny"})
    second = await client.put(_policy_path(READ_TOOL), json={"mode": "allow"})

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    rows = (
        (
            await db_session.execute(
                select(CompanionToolPolicy).where(
                    CompanionToolPolicy.user_id == seed_user.id,
                    CompanionToolPolicy.verb == "list_dir",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    active = [row for row in rows if row.revoked_at is None]
    revoked = [row for row in rows if row.revoked_at is not None]
    assert len(active) == 1
    assert active[0].mode == "allow"
    assert len(revoked) == 1
    assert revoked[0].mode == "deny"


async def test_storing_allow_on_a_destroy_verb_with_destroy_off_is_accepted_and_resolves_deny(
    db_session, seed_user, client
) -> None:
    """Accepted, and honest about being currently overridden.

    Refusing the write would force the tenant to discover the ordering constraint by trial;
    rewriting the stored value to match the resolution would discard the preference they expressed
    and will want the moment they flip the switch. Both facts come back in ONE response.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="stored")

    body = (await client.put(_policy_path(DESTROY_TOOL), json={"mode": "allow"})).json()

    assert body["stored_mode"] == "allow"
    assert body["resolved_mode"] == "deny"
    assert body["resolved_reason"] == REASON_DESTROY_DISABLED
    assert body["exposed_to_agent"] is False


async def test_the_two_writes_touch_disjoint_tables(db_session, seed_user, client) -> None:
    """Asserted in BOTH directions by row count, because one direction is half a claim.

    "Policy and caps are independent gates" is only true if neither write can move the other's
    state, and the destroy switch is the gate a policy write is most tempting to fold into.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="disjoint")

    destroy_before = await _count(db_session, CompanionDestroyEnablement, seed_user)
    await client.put(_policy_path(READ_TOOL), json={"mode": "deny"})
    destroy_after = await _count(db_session, CompanionDestroyEnablement, seed_user)

    policy_before = await _count(db_session, CompanionToolPolicy, seed_user)
    await client.put(_destroy_path("filebrowser"), json={"enabled": True})
    policy_after = await _count(db_session, CompanionToolPolicy, seed_user)

    assert destroy_after == destroy_before
    assert policy_after == policy_before


async def test_a_policy_body_carrying_a_cap_field_is_refused_at_the_boundary(
    db_session, seed_user, client
) -> None:
    """``extra="forbid"`` plus disjoint field sets: neither gate can be expressed in the other.

    The second direction is asserted against the cap request model directly here, because the cap
    ROUTE does not exist until Task 3; ``test_a_cap_body_carrying_a_mode_field_is_refused`` below
    repeats it as a live request once it does.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="forbid")

    response = await client.put(_policy_path(READ_TOOL), json={"mode": "deny", "per_call_calls": 5})

    assert response.status_code == 422
    with pytest.raises(ValidationError):
        CompanionCapVectorRequest(
            per_call_calls=1,
            per_call_records=1,
            per_call_bytes=1,
            per_session_calls=1,
            per_session_records=1,
            per_session_bytes=1,
            mode="allow",  # pyright: ignore[reportCallIssue]
        )


async def test_an_unregistered_tool_name_is_a_not_found_and_a_malformed_one_is_unprocessable(
    db_session, seed_user, client
) -> None:
    """Malformed is refused at the path-parameter boundary and never reaches a query.

    A well-formed name the registry does not own is a 404 -- ``companion_verb`` raises rather than
    returning a default, for the same reason the façade refuses it: a default here would be a
    silent decision about whether an unregistered tool is configurable.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="unknown")

    unregistered = await client.put(
        _policy_path("filebrowser__no_such_verb"), json={"mode": "deny"}
    )
    foreign_kind = await client.put(_policy_path("nosuchkind__list_dir"), json={"mode": "deny"})
    malformed = await client.put(_policy_path("NotAToolName"), json={"mode": "deny"})

    assert unregistered.status_code == 404
    assert foreign_kind.status_code == 404
    assert malformed.status_code == 422


async def test_a_tool_whose_companion_is_not_deployed_is_a_not_found_never_a_forbidden(
    db_session, seed_user, client
) -> None:
    """404 rather than 403, because a 403 confirms existence and is a membership oracle.

    The foreign tenant here HAS the companion deployed, so a surface that leaked existence would
    answer differently for this caller than for one whose tenant simply has nothing -- and both
    answers must be the same 404.
    """
    other = await _other_tenant(db_session)
    await deploy_companion_state(db_session, other, machine_prefix="theirs")
    await _policy_row(db_session, other, verb="list_dir", mode="allow")

    policy = await client.put(_policy_path(READ_TOOL), json={"mode": "deny"})
    destroy = await client.put(_destroy_path("filebrowser"), json={"enabled": True})

    assert policy.status_code == 404
    assert destroy.status_code == 404
    assert await _count(db_session, CompanionToolPolicy, other) == 1
    assert await _count(db_session, CompanionToolPolicy, seed_user) == 0


async def test_the_destroy_switch_is_idempotent_in_both_positions(
    db_session, seed_user, client
) -> None:
    """A switch that errors when set to the position it already holds is a switch no UI can drive.

    Not merely an inconvenience: a UI that does not first read the switch would turn a double-click
    into an error dialog, and one that does read it first races with a second browser tab.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="idempotent")

    first_on = await client.put(_destroy_path("filebrowser"), json={"enabled": True})
    second_on = await client.put(_destroy_path("filebrowser"), json={"enabled": True})
    first_off = await client.put(_destroy_path("filebrowser"), json={"enabled": False})
    second_off = await client.put(_destroy_path("filebrowser"), json={"enabled": False})

    assert [response.status_code for response in (first_on, second_on, first_off, second_off)] == [
        200,
        200,
        200,
        200,
    ]
    assert second_on.json()["destroy_enabled"] is True
    assert second_off.json()["destroy_enabled"] is False
    assert await _count(db_session, CompanionDestroyEnablement, seed_user) == 0


async def test_the_destroy_switch_reports_the_companion_summary_it_moved(
    db_session, seed_user, client
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="switchsummary")

    body = (await client.put(_destroy_path("filebrowser"), json={"enabled": True})).json()

    assert body["companion_key"] == "filebrowser"
    assert body["has_destroy_verbs"] is True
    assert body["destroy_enabled"] is True
    catalog = (await client.get(CATALOG_PATH)).json()
    assert body == next(
        row for row in catalog["companions"] if row["companion_key"] == "filebrowser"
    )


async def test_disabling_destroy_refuses_the_very_next_call_without_waiting_for_the_list_ttl(
    db_session, seed_user, client
) -> None:
    """The asymmetry the settings surface deliberately does NOT paper over.

    ``_destroy_enabled`` is re-read at CALL time, so a revocation takes effect immediately even
    while a stale ``tools/list`` snapshot still names the verb -- that is exactly what makes the
    60-second TTL a staleness bound rather than an exploit window. Enabling is the only direction
    the cache delays, it delays it by at most a minute in the harmless direction, and
    ``reset_companion_tools_cache`` clears one PROCESS -- in a multi-worker deployment that would
    be a fix in one worker that reads like a fix everywhere.

    Proven by calling the façade rather than by re-reading the catalog: the catalog and the call
    path could agree with each other and both be wrong about the cache.
    """
    reset_companion_tools_cache()
    await deploy_companion_state(db_session, seed_user, machine_prefix="revoke")
    await client.put(_destroy_path("filebrowser"), json={"enabled": True})
    await client.put(_policy_path(DESTROY_TOOL), json={"mode": "allow"})

    auth = AuthContext(user=seed_user)
    warmed = {tool["name"] for tool in await list_companion_tools(auth, db_session)}
    assert DESTROY_TOOL in warmed, "the cache was never warmed; the staleness claim would be moot"

    await client.put(_destroy_path("filebrowser"), json={"enabled": False})
    still_listed = {tool["name"] for tool in await list_companion_tools(auth, db_session)}
    result = await call_companion_tool(
        DESTROY_TOOL, {"path": "/doomed"}, auth=auth, db=db_session, deadline_seconds=0
    )

    assert DESTROY_TOOL in still_listed, "the settings write reached into the per-process cache"
    assert result["isError"] is True
    assert REASON_DESTROY_DISABLED in result["content"][0]["text"]


async def test_each_settings_write_records_exactly_one_control_plane_audit_event(
    db_session, seed_user, client
) -> None:
    """A change to a safety control is not a change nobody can attribute.

    Recorded through the shipped writer, which adds WITHOUT committing, so the event and the row
    land in one transaction: a settings change that committed without its audit event would be
    unattributable, and one that rolled back leaving the event would attribute a change that never
    happened.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="audited")

    before = await _audit_events(db_session, seed_user)
    await client.put(_policy_path(READ_TOOL), json={"mode": "deny"})
    after_policy = await _audit_events(db_session, seed_user)
    await client.put(_destroy_path("filebrowser"), json={"enabled": True})
    after_destroy = await _audit_events(db_session, seed_user)

    assert len(after_policy) == len(before) + 1
    assert len(after_destroy) == len(after_policy) + 1
    assert {event.actor_type for event in after_destroy} == {"user"}
    assert {event.source for event in after_destroy} == {"api.companions"}
    assert {event.actor_user_id for event in after_destroy} == {seed_user.id}
    assert {event.target_user_id for event in after_destroy} == {seed_user.id}
    assert {event.resource_id for event in after_destroy} == {READ_TOOL, "filebrowser"}


async def test_both_writes_reject_a_cli_credential(db_session, seed_user, cli_client) -> None:
    """A CLI key is what an AGENT holds, and these are the controls that bound the agent.

    A CLI key reaching either write would make every gate in this phase self-granted by the party
    the gates exist to bound (T-5-64).
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="cliwrite")

    policy = await cli_client.put(_policy_path(READ_TOOL), json={"mode": "allow"})
    destroy = await cli_client.put(_destroy_path("filebrowser"), json={"enabled": True})

    assert policy.status_code == 403
    assert destroy.status_code == 403
    assert await _count(db_session, CompanionToolPolicy, seed_user) == 0
    assert await _count(db_session, CompanionDestroyEnablement, seed_user) == 0


async def test_an_unknown_mode_is_refused_at_the_boundary(db_session, seed_user, client) -> None:
    """The mode ``Literal`` is the gate, so a fourth value never reaches the CHECK constraint."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="badmode")

    response = await client.put(_policy_path(READ_TOOL), json={"mode": "maybe"})

    assert response.status_code == 422
    assert await _count(db_session, CompanionToolPolicy, seed_user) == 0


# ---------------------------------------------------------------------------
# Task 3 -- the cap vector, the live window read-out, and the canonical-only proof.
# ---------------------------------------------------------------------------


async def test_a_tenant_with_no_cap_row_reads_the_shipped_defaults_marked_as_defaults(
    db_session, seed_user, client
) -> None:
    """The absent row IS the answer, and the answer says so.

    ``DEFAULT_CAPS`` is the fail-safe direction -- a failure to insert leaves the shipped ceiling
    in force rather than removing the ceiling -- so an empty table must read as the defaults rather
    than as "no limit" or as an error.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capdefault")

    response = await client.get(CAPS_PATH)

    assert response.status_code == 200, response.text
    item = _cap_item(response.json(), "filebrowser")
    assert _cap_values(item) == _default_payload()
    assert item["is_default"] is True
    assert item["updated_at"] is None


async def test_saving_the_shipped_numbers_verbatim_clears_the_default_marker(
    db_session, seed_user, client
) -> None:
    """``is_default`` is ROW EXISTENCE, never a value comparison.

    A tenant who read the shipped numbers, agreed with them and saved them made a decision.
    Reporting it as an absence would show their choice as something nobody ever made -- and would
    make this field's answer change the next time ``DEFAULT_CAPS`` was retuned.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capsame")

    written = await client.put(_caps_path("filebrowser"), json=_default_payload())

    assert written.status_code == 200, written.text
    assert _cap_values(written.json()) == _default_payload()
    assert written.json()["is_default"] is False
    assert written.json()["updated_at"] is not None
    listed = _cap_item((await client.get(CAPS_PATH)).json(), "filebrowser")
    assert listed == written.json()


async def test_a_cap_of_zero_round_trips_as_zero_and_is_not_an_unset_scope(
    db_session, seed_user, client
) -> None:
    """Zero is a VALUE meaning "refuse everything", and the two answers must differ.

    ``app/core/companion_caps.py`` names reading zero as falsy and substituting a default as the
    single most likely way this guarantee is lost, because it turns the tightest possible policy
    into no policy at all, silently.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capzero")
    unset = _cap_item((await client.get(CAPS_PATH)).json(), "filebrowser")

    payload = {**_default_payload(), "per_call_calls": 0}
    saved = (await client.put(_caps_path("filebrowser"), json=payload)).json()

    assert saved["per_call_calls"] == 0
    assert unset["per_call_calls"] == DEFAULT_CAPS.per_call_calls
    assert saved["per_call_calls"] != unset["per_call_calls"]
    assert saved["is_default"] is False
    assert unset["is_default"] is True


async def test_a_partial_or_negative_cap_body_is_refused_at_the_boundary(
    db_session, seed_user, client
) -> None:
    """Caps save as ONE unit, and a negative never reaches the table's check constraint.

    An optional field falling back to a default lets a client that forgot one field silently widen
    a ceiling the tenant never touched, which is why all six are required with no defaults.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="cappartial")
    partial = {key: value for key, value in _default_payload().items() if key != "per_call_bytes"}

    missing = await client.put(_caps_path("filebrowser"), json=partial)
    negative = await client.put(
        _caps_path("filebrowser"), json={**_default_payload(), "per_session_records": -1}
    )

    assert missing.status_code == 422
    assert negative.status_code == 422
    assert await _count(db_session, CompanionCapPolicy, seed_user) == 0


async def test_a_cap_body_carrying_a_mode_field_is_refused(db_session, seed_user, client) -> None:
    """The second half of the gate-independence claim, as a live request this time.

    Policy and caps have disjoint bodies under ``extra="forbid"``, so neither gate can be expressed
    in terms of the other -- and a request that tried cannot be parsed at all.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="capmode")

    response = await client.put(
        _caps_path("filebrowser"), json={**_default_payload(), "mode": "allow"}
    )

    assert response.status_code == 422


async def test_a_saved_per_call_ceiling_of_zero_refuses_the_next_call_by_the_cap(
    db_session, seed_user, client
) -> None:
    """The number the dashboard shows is the number the next call is judged against.

    An explicit ``allow`` policy row is in place so the refusal cannot be the policy -- the same
    shape ``test_companion_blast_radius.py`` uses, and the reason the two gates are separately
    reachable at all.
    """
    reset_companion_tools_cache()
    await deploy_companion_state(db_session, seed_user, machine_prefix="capbite")
    await client.put(_policy_path(READ_TOOL), json={"mode": "allow"})
    await client.put(_caps_path("filebrowser"), json={**_default_payload(), "per_call_calls": 0})

    result = await call_companion_tool(
        READ_TOOL,
        {"path": "/"},
        auth=AuthContext(user=seed_user),
        db=db_session,
        deadline_seconds=0,
    )

    assert result["isError"] is True
    assert REASON_PER_CALL_CAP_EXCEEDED in result["content"][0]["text"]
    assert REASON_CLASS_DEFAULT not in result["content"][0]["text"]


async def test_the_consumption_read_out_names_its_window_even_when_nothing_was_spent(
    db_session, seed_user, client
) -> None:
    """An empty list means "nothing spent since this time", not "unknown".

    The UI spec calls this a live SESSION read-out. The protocol as this backend speaks it delivers
    no session identifier to the façade, so what exists is a rolling one-hour window keyed by Agent
    and companion. Reporting the window is what keeps the read-out from naming a scope the server
    does not meter.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="nowindow")

    response = await client.get(CONSUMPTION_PATH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] == []
    assert body["window_seconds"] == SESSION_BUDGET_WINDOW_SECONDS
    assert datetime.fromisoformat(body["window_started_at"]) == budget_window(datetime.now(UTC))


async def test_each_consumption_item_carries_a_null_session_and_the_ceilings_it_is_read_against(
    db_session, seed_user, client
) -> None:
    """One response, one moment. Two requests divided by each other are two moments.

    ``session_id`` is rendered as an explicit null rather than omitted, so a read-out can say "not
    recorded" instead of inferring a scope from a missing key.
    """
    environment_id, _ = await deploy_companion_state(db_session, seed_user, machine_prefix="spent")
    await _budget_row(db_session, seed_user, environment_id=environment_id, calls=7, records=3)

    body = (await client.get(CONSUMPTION_PATH)).json()

    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["companion_key"] == "filebrowser"
    assert item["session_id"] is None
    assert item["calls_consumed"] == 7
    assert item["records_consumed"] == 3
    assert item["bytes_consumed"] == 0
    assert item["per_session_calls"] == DEFAULT_CAPS.per_session_calls
    assert item["per_session_records"] == DEFAULT_CAPS.per_session_records
    assert item["per_session_bytes"] == DEFAULT_CAPS.per_session_bytes


async def test_the_consumption_read_out_reports_the_saved_ceilings_not_the_shipped_ones(
    db_session, seed_user, client
) -> None:
    """Resolved through the façade's own ``resolve_cap_vector``, so the fraction has one numerator
    and one denominator that the next call will also use."""
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="ceiling"
    )
    await _budget_row(db_session, seed_user, environment_id=environment_id, calls=2)
    await client.put(_caps_path("filebrowser"), json={**_default_payload(), "per_session_calls": 9})

    item = (await client.get(CONSUMPTION_PATH)).json()["items"][0]

    assert item["per_session_calls"] == 9


async def test_a_budget_row_from_a_previous_window_is_not_reported(
    db_session, seed_user, client
) -> None:
    """Only the current window. Earlier windows are history, and the activity log is where history
    lives -- a read-out that silently summed them would over-report what is currently spent."""
    environment_id, _ = await deploy_companion_state(db_session, seed_user, machine_prefix="stale")
    previous = budget_window(datetime.now(UTC)) - timedelta(seconds=SESSION_BUDGET_WINDOW_SECONDS)
    await _budget_row(
        db_session, seed_user, environment_id=environment_id, calls=99, window_started_at=previous
    )

    body = (await client.get(CONSUMPTION_PATH)).json()

    assert body["items"] == []
    assert datetime.fromisoformat(body["window_started_at"]) != previous


async def test_a_session_budget_from_an_earlier_hour_stays_visible_until_its_lifetime_ends(
    db_session, seed_user, client
) -> None:
    """GC2-02 (WINDOWS #188): the read-out shows the session counter that will refuse the call.

    A session row's ``window_started_at`` is when that budget OPENED, not which hour it belongs to,
    so the window equality that is right for the window ledger hid a live session budget from the
    hour after it opened onward. The read-out now selects session rows by the gate's own cutoff --
    ``session_budget_cutoff`` of the hour the debit would stamp -- so a row the debit would judge
    as spent is shown, and a row the debit would reset is not. Both sides of the boundary are here,
    and the boundary itself is inclusive exactly as the debit's strict "opened before" is.
    """
    environment_id, _ = await deploy_companion_state(
        db_session, seed_user, machine_prefix="sessionlife"
    )
    window = budget_window(datetime.now(UTC))
    cutoff = companion_facade.session_budget_cutoff(window)
    three_hours_ago = window - timedelta(seconds=3 * SESSION_BUDGET_WINDOW_SECONDS)
    assert three_hours_ago >= cutoff, "the lifetime must outlast three hours for this case"
    await _budget_row(
        db_session,
        seed_user,
        environment_id=environment_id,
        calls=190,
        window_started_at=three_hours_ago,
        session_id="long-conversation",
    )
    await _budget_row(
        db_session,
        seed_user,
        environment_id=environment_id,
        calls=40,
        window_started_at=cutoff,
        session_id="opened-exactly-at-the-cutoff",
    )
    await _budget_row(
        db_session,
        seed_user,
        environment_id=environment_id,
        calls=150,
        window_started_at=cutoff - timedelta(seconds=1),
        session_id="outlived-its-lifetime",
    )

    body = (await client.get(CONSUMPTION_PATH)).json()

    by_session = {item["session_id"]: item["calls_consumed"] for item in body["items"]}
    assert by_session.get("long-conversation") == 190
    assert by_session.get("opened-exactly-at-the-cutoff") == 40
    # The debit would RESET this row rather than refuse against it, so it is not a live ceiling.
    assert "outlived-its-lifetime" not in by_session
    assert None not in by_session, "no window row was written, so none is reported"


async def test_window_row_orders_first_and_the_order_is_stable(
    db_session, seed_user, client
) -> None:
    """GC2-01 (WINDOWS #187): one companion's rows are never a tie the database breaks.

    With a window row and session rows for the same companion and Agent, the order was only
    ``(companion_key, environment_id)`` -- a tie PostgreSQL resolves however the plan falls. The
    window row (null session) is first on every read now, then the session rows by id, then stamp,
    so the caps card's "the window row" and the response's first row are the same row every time.
    """
    environment_id, _ = await deploy_companion_state(db_session, seed_user, machine_prefix="order")
    window = budget_window(datetime.now(UTC))
    await _budget_row(
        db_session,
        seed_user,
        environment_id=environment_id,
        calls=2,
        window_started_at=window,
        session_id="b-session",
    )
    await _budget_row(db_session, seed_user, environment_id=environment_id, calls=7)
    await _budget_row(
        db_session,
        seed_user,
        environment_id=environment_id,
        calls=5,
        window_started_at=window - timedelta(seconds=SESSION_BUDGET_WINDOW_SECONDS),
        session_id="a-session",
    )

    orders = []
    for _ in range(5):
        body = (await client.get(CONSUMPTION_PATH)).json()
        orders.append([(item["session_id"], item["calls_consumed"]) for item in body["items"]])

    assert orders[0] == [(None, 7), ("a-session", 5), ("b-session", 2)]
    assert all(order == orders[0] for order in orders)


async def test_cap_and_consumption_reads_are_tenant_scoped_in_both_halves(
    db_session, seed_user, client
) -> None:
    """The foreign tenant's ceiling and counters are absent AND the caller's own are present."""
    other = await _other_tenant(db_session)
    foreign_env, _ = await deploy_companion_state(db_session, other, machine_prefix="theircaps")
    db_session.add(
        CompanionCapPolicy(
            user_id=other.id,
            companion_key="filebrowser",
            per_call_calls=1234,
            per_call_records=1,
            per_call_bytes=1,
            per_session_calls=4321,
            per_session_records=1,
            per_session_bytes=1,
        )
    )
    await db_session.commit()
    await _budget_row(db_session, other, environment_id=foreign_env, calls=77)
    mine_env, _ = await deploy_companion_state(db_session, seed_user, machine_prefix="mycaps")
    await _budget_row(db_session, seed_user, environment_id=mine_env, calls=5)

    caps = (await client.get(CAPS_PATH)).json()
    consumption = (await client.get(CONSUMPTION_PATH)).json()

    assert len(caps["items"]) == 1
    assert _cap_item(caps, "filebrowser")["per_call_calls"] == DEFAULT_CAPS.per_call_calls
    assert _cap_item(caps, "filebrowser")["is_default"] is True
    assert len(consumption["items"]) == 1
    assert consumption["items"][0]["calls_consumed"] == 5
    assert str(foreign_env) not in {item["environment_id"] for item in consumption["items"]}


async def test_all_three_cap_and_consumption_routes_reject_a_cli_credential(
    db_session, seed_user, cli_client
) -> None:
    await deploy_companion_state(db_session, seed_user, machine_prefix="clicaps")

    listed = await cli_client.get(CAPS_PATH)
    written = await cli_client.put(_caps_path("filebrowser"), json=_default_payload())
    consumed = await cli_client.get(CONSUMPTION_PATH)

    assert listed.status_code == 403
    assert written.status_code == 403
    assert consumed.status_code == 403
    assert await _count(db_session, CompanionCapPolicy, seed_user) == 0


async def test_a_cap_write_records_exactly_one_control_plane_audit_event(
    db_session, seed_user, client
) -> None:
    """Caps have no history column at all, so the audit event is the ONLY record that a ceiling
    moved and who moved it."""
    await deploy_companion_state(db_session, seed_user, machine_prefix="capaudit")

    before = await _audit_events(db_session, seed_user)
    await client.put(_caps_path("filebrowser"), json=_default_payload())
    after = await _audit_events(db_session, seed_user)

    assert len(after) == len(before) + 1
    event = after[-1]
    assert event.actor_type == "user"
    assert event.source == "api.companions"
    assert event.resource_type == "companion_cap_policy"
    assert event.resource_id == "filebrowser"


async def test_a_cap_write_for_an_undeployed_companion_is_a_not_found(
    db_session, seed_user, client
) -> None:
    other = await _other_tenant(db_session)
    await deploy_companion_state(db_session, other, machine_prefix="notmine")

    response = await client.put(_caps_path("filebrowser"), json=_default_payload())

    assert response.status_code == 404
    assert await _count(db_session, CompanionCapPolicy, seed_user) == 0


async def test_every_new_settings_path_answers_under_v1_and_404s_under_api(
    db_session, seed_user, client
) -> None:
    """All six, asked as LIVE requests rather than read out of the mount table.

    A hidden alias is ``include_in_schema=False`` by definition, so it is absent from the OpenAPI
    schema whether or not it is mounted -- a schema check would pass with the alias present.
    """
    await deploy_companion_state(db_session, seed_user, machine_prefix="sixalias")

    answered = {
        path: (
            await client.request(method, f"/v1{path}", json=body)
            if body is not None
            else await client.request(method, f"/v1{path}")
        ).status_code
        for method, path, body in _NEW_SETTINGS_REQUESTS
    }
    aliased = {
        path: (
            await client.request(method, f"/api{path}", json=body)
            if body is not None
            else await client.request(method, f"/api{path}")
        ).status_code
        for method, path, body in _NEW_SETTINGS_REQUESTS
    }

    assert set(answered.values()) == {200}, answered
    assert set(aliased.values()) == {404}, aliased


def test_every_new_settings_path_is_covered_by_the_canonical_only_prefixes() -> None:
    """Imported rather than restated, so a later route moved off this router cannot silently
    acquire an alias while this file still claims it is canonical-only.

    ``test_api_version_alias.py`` is deliberately NOT modified: the prefix ``/v1/companions/`` and
    the router's membership in ``_CANONICAL_ONLY_ROUTERS`` already cover everything hanging off
    this router, and adding per-path entries would turn an inherited decision into six copies.
    """
    from app.main import app
    from tests.test_api_version_alias import _CANONICAL_ONLY_V1_PREFIXES

    published = set(app.openapi()["paths"])
    for template in _NEW_SETTINGS_PATH_TEMPLATES:
        assert template in published, f"{template} is not published on the canonical surface"
        assert template.startswith(_CANONICAL_ONLY_V1_PREFIXES), template
