"""The DASHBOARD's view of the companion gates, and the writes that move them (TOOL-02/04, APP-05).

**The boundary this file draws is the point of it existing.** ``companion_facade.py`` is what the
AGENT reaches: ``tools/list`` and ``tools/call``, behind a CLI key. This module is what the
DASHBOARD reaches: the catalog a person reads and the controls they set, behind ``require_web_auth``
and nothing else. D-09 put the decision surface deliberately outside any channel the agent can
reach, and keeping the two in separate modules is what makes that visible to the next reader rather
than something to be re-derived from a dependency list. Nothing here is imported by
``app/routes/mcp_bridge.py``, and nothing here should ever be.

**Everything resolved here is resolved through the façade's OWN predicates.** Agent visibility comes
from ``is_exposed_to_agent``, the policy answer from ``evaluate_policy``, the cap vector from
``resolve_cap_vector``, the consumption window from ``budget_window``. A second implementation of
any of them would be a second thing that can drift, and each drift has a specific shape: a catalog
that showed a destroy verb as hidden while the agent could still see it, a mode the next call will
not honour, or a ceiling the dashboard displays and the debit does not use.

**The stored mode and the resolved mode are separate facts, and the surface carries both.**
``evaluate_policy`` checks destroy enablement FIRST and UNCONDITIONALLY, before it reads the
tenant's explicit row. So a tenant who stored ``allow`` on a destroy verb for a companion whose
switch is off has stored a mode the server will refuse. Reporting only the stored value would show
the tool as running freely at the exact moment it stops; rewriting the stored value to match the
resolution would discard a preference the tenant expressed and will want the moment they flip the
switch. Both travel, with the machine reason that produced the second.

**This module creates no table and changes no call-path behaviour.** All three tables predate it --
``companion_tool_policies``, ``companion_destroy_enablements`` and ``companion_cap_policies`` -- and
the façade has read all three on every call since plans 05-03 and 05-04. What was missing was only
a way to WRITE them without a database client.

**The group block (plan 06-24, APP-05) is the fourth control moved from here.**
``companion_group_blocks`` is read by the façade in both of its halves -- the list filter and the
call-time refusal -- through ``blocked_companion_keys`` and its call-time twin, which live in the
façade beside ``destroy_enabled_companion_keys`` and are re-exported here with the reason token.
``set_group_block`` is the only writer, and like every write in this module it is reachable from the
dashboard alone.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext, require_clerk_id
from app.core.companion_policy import evaluate_policy
from app.core.companion_registry import COMPANION_VERBS, CompanionVerbEntry, companion_verb
from app.models.companion_session_budget import (
    SESSION_BUDGET_WINDOW_SECONDS,
    CompanionCapPolicy,
    CompanionSessionBudget,
)
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.schemas.companions import (
    CompanionCapListResponse,
    CompanionCapVectorRequest,
    CompanionCapVectorResponse,
    CompanionConsumptionItemResponse,
    CompanionConsumptionResponse,
    CompanionSummaryResponse,
    CompanionToolCatalogResponse,
    CompanionToolSettingsResponse,
)
from app.services.audit import record_control_plane_audit
from app.services.companion_dryrun import MUTATING_TOOL_CLASSES
from app.services.companion_facade import (
    REASON_COMPANION_GROUP_DISABLED,
    blocked_companion_keys,
    budget_window,
    deployed_companion_environments,
    deployed_companion_kinds,
    destroy_enabled_companion_keys,
    is_exposed_to_agent,
    reset_companion_tools_cache_for,
    resolve_cap_vector,
    session_budget_cutoff,
)

__all__ = [
    "AUDIT_SOURCE",
    "REASON_COMPANION_GROUP_DISABLED",
    "blocked_companion_keys",
    "resolve_cap_vector_settings",
    "resolve_consumption",
    "resolve_tool_catalog",
    "set_cap_vector",
    "set_destroy_enablement",
    "set_group_block",
    "set_tool_policy",
]

# The audit `source` every write in this module records under. One value rather than one per route,
# matching `api.channels` in `app/routes/channel_routers/public.py`: the source names the SURFACE a
# change arrived through, and "which control moved" is what `action` already carries.
AUDIT_SOURCE = "api.companions"


async def resolve_tool_catalog(
    db: AsyncSession, *, auth: AuthContext
) -> CompanionToolCatalogResponse:
    """Every verb of every companion this tenant has deployed, with what the server would decide.

    Deployment-scoped, not registry-scoped: a verb whose companion the tenant has not deployed is
    absent rather than listed as unavailable, because a control over a companion that is not there
    is a control that governs nothing.

    A tenant with nothing deployed gets two empty lists and a 200. Their catalog exists; nothing is
    in it. A 404 would say the surface does not exist, which is a different and untrue answer.

    **Ordered by ``(companion_key, verb)``, the key the groups are ordered by (APP-05).** Not by
    the composite tool name: a key may contain ``-``, which sorts before the ``__`` separator, so
    ``filebrowser-x__list_dir`` would sort ahead of every ``filebrowser__`` verb while the groups
    list ``filebrowser`` first. Two reads of unchanged state render identically either way; only
    this order also keeps each group's tools together in the order the groups appear.
    """
    kinds = await deployed_companion_kinds(auth, db)
    destroy_keys = await destroy_enabled_companion_keys(db, user_id=auth.user_id)
    blocked_keys = await blocked_companion_keys(db, user_id=auth.user_id)
    stored = await _active_policy_modes(db, user_id=auth.user_id)
    environments = await _deployment_environments(auth, db, kinds=kinds)

    tools = [
        _tool_settings(
            name,
            entry,
            stored_mode=stored.get(_split(name)),
            kinds=kinds,
            destroy_keys=destroy_keys,
            blocked_keys=blocked_keys,
        )
        for name, entry in sorted(COMPANION_VERBS.items(), key=lambda item: _split(item[0]))
        if _split(name)[0] in kinds
    ]
    companions = [
        _companion_summary(
            key,
            environment_ids=environments.get(key, []),
            destroy_keys=destroy_keys,
            blocked_keys=blocked_keys,
            tools=tools,
        )
        for key in sorted(kinds)
    ]
    return CompanionToolCatalogResponse(companions=companions, tools=tools)


async def set_tool_policy(
    db: AsyncSession, *, auth: AuthContext, tool: str, mode: str
) -> CompanionToolSettingsResponse:
    """Store this tenant's approval mode for one verb, and answer with the RE-RESOLVED row.

    **Revoke-then-insert, not an in-place update, and the alternative looks correct.** Setting
    ``mode`` on the existing row satisfies ``uq_active_companion_tool_policy`` exactly as well and
    passes every test that only reads the current mode -- and silently discards the history
    ``revoked_at`` exists for, which ``app/models/companion_tool_policy.py`` names as what an
    approval decision most needs to be auditable against. The unique partial index covers LIVE rows
    only, which is what makes revoking first legal rather than a constraint violation.

    **The answer is the catalog's own row, not an echo of the request.** The client renders from the
    server's resolution, which is what keeps a safety control from flipping optimistically to a
    state the server does not hold. The case that proves it is a stored ``allow`` on a destroy verb
    whose switch is off: the write is ACCEPTED and comes back resolved ``deny``. Refusing it would
    force the tenant to discover the ordering constraint by trial, and rewriting the stored value
    to match the resolution would discard the preference they expressed for the moment they flip
    the switch. Two fields exist precisely so both facts can be told.
    """
    entry, companion_key, verb = await _owned_verb(db, auth=auth, tool=tool)
    moment = datetime.now(UTC)

    await db.execute(
        update(CompanionToolPolicy)
        .where(
            CompanionToolPolicy.user_id == auth.user_id,
            CompanionToolPolicy.companion_key == companion_key,
            CompanionToolPolicy.verb == verb,
            CompanionToolPolicy.revoked_at.is_(None),
        )
        .values(revoked_at=moment)
        .execution_options(synchronize_session=False)
    )
    db.add(
        CompanionToolPolicy(
            user_id=auth.user_id,
            companion_key=companion_key,
            verb=verb,
            mode=mode,
        )
    )
    # In the SAME transaction as the row, through the shipped writer, which adds without
    # committing. A settings change that committed without its audit event would be a change to a
    # safety control nobody can attribute; one that rolled back leaving the event would attribute a
    # change that never happened.
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="companion.tool_policy.set",
        resource_type="companion_tool_policy",
        resource_id=tool,
        source=AUDIT_SOURCE,
        details={"tool": tool, "companion_key": companion_key, "verb": verb, "mode": mode},
    )
    await db.commit()

    destroy_keys = await destroy_enabled_companion_keys(db, user_id=auth.user_id)
    blocked_keys = await blocked_companion_keys(db, user_id=auth.user_id)
    kinds = await deployed_companion_kinds(auth, db)
    stored = await _active_policy_modes(db, user_id=auth.user_id)
    return _tool_settings(
        tool,
        entry,
        stored_mode=stored.get((companion_key, verb)),
        kinds=kinds,
        destroy_keys=destroy_keys,
        blocked_keys=blocked_keys,
    )


async def set_destroy_enablement(
    db: AsyncSession, *, auth: AuthContext, companion_key: str, enabled: bool
) -> CompanionSummaryResponse:
    """Hold this companion's destroy switch in the named position, and answer with its summary.

    Row present means enabled and row absent means disabled -- there is no boolean column, because
    one would admit a third state (present, ``enabled=false``) meaning the same thing as absence.
    So enabling inserts if missing and disabling deletes if present, and BOTH are idempotent. A
    switch that errors when set to the position it already holds cannot be driven by a UI that does
    not first read it, and a UI that reads it first races with a second browser tab.

    **This does NOT reach into the façade's 60-second tool-list cache, and the asymmetry is
    deliberate.** Disabling is already safe without it: ``_destroy_enabled`` is re-read at CALL
    time, so a revocation takes effect on the very next call even while a stale snapshot still
    names the verb -- that is exactly what makes the TTL a staleness bound rather than an exploit
    window. Enabling is the only direction the cache delays, it delays it by at most a minute in
    the harmless direction, and ``reset_companion_tools_cache`` clears every tenant's entry in ONE
    process: in a multi-worker deployment that is a fix in one worker that reads like a fix
    everywhere, which is worse than the honest delay.

    This write touches NO policy row. Policy and the destroy switch are independent gates, and
    setting one in terms of the other is how a gate is made vacuous by its neighbour.
    """
    await _owned_companion(db, auth=auth, companion_key=companion_key)
    existing = await db.scalar(
        select(CompanionDestroyEnablement.id).where(
            CompanionDestroyEnablement.user_id == auth.user_id,
            CompanionDestroyEnablement.companion_key == companion_key,
        )
    )
    if enabled and existing is None:
        db.add(
            CompanionDestroyEnablement(
                user_id=auth.user_id,
                companion_key=companion_key,
                enabled_at=datetime.now(UTC),
                enabled_by_user_id=auth.user_id,
            )
        )
    elif not enabled and existing is not None:
        await db.execute(
            delete(CompanionDestroyEnablement)
            .where(
                CompanionDestroyEnablement.user_id == auth.user_id,
                CompanionDestroyEnablement.companion_key == companion_key,
            )
            .execution_options(synchronize_session=False)
        )
    # Recorded even when the switch was already in the requested position. The event records that
    # somebody ASSERTED a position at a moment, which is the question an audit answers; suppressing
    # the no-op would make the trail depend on prior state a reader cannot see.
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="companion.destroy_enablement.set",
        resource_type="companion_destroy_enablement",
        resource_id=companion_key,
        source=AUDIT_SOURCE,
        details={"companion_key": companion_key, "enabled": enabled},
    )
    await db.commit()

    return await _companion_summary_for(db, auth=auth, companion_key=companion_key)


async def set_group_block(
    db: AsyncSession, *, auth: AuthContext, companion_key: str, blocked: bool
) -> CompanionSummaryResponse:
    """Block or allow this companion's whole tool group, and answer with its summary (D-29/D-30).

    Row present means blocked, the destroy switch's shape with the polarity inverted: blocking
    inserts if missing, allowing deletes if present, and both are idempotent for the reason
    ``set_destroy_enablement`` gives. The audit event is recorded in the same transaction, even for
    a no-op, because it records that somebody asserted a position at a moment.

    **Unlike the destroy switch, this DOES reset the tenant's tool-list cache, after the commit.**
    The block's list half should take effect for the very next ``tools/list``, in both directions:
    a blocked group must stop being advertised, and an allowed one must come back without a
    minute's wait. The reset is in-process, so another worker's cached list may still name the
    group until its 60-second entry expires -- which is safe, because the call-time half re-reads
    the block row on every call and refuses the group by name in every process. The reset runs
    after the commit so a list read concurrently cannot re-cache the state from before the change.

    A call already minted before the change commits completes: the block gates the decision to
    mint, and a signed call that already passed every gate is not revoked mid-flight.

    This write touches no policy row and no destroy switch. The three are independent gates.
    """
    await _owned_companion(db, auth=auth, companion_key=companion_key)
    # Read before the commit: an attribute read on the auth user after it can lazy-load.
    try:
        clerk_id: str | None = require_clerk_id(auth)
    except HTTPException:
        clerk_id = None
    existing = await db.scalar(
        select(CompanionGroupBlock.id).where(
            CompanionGroupBlock.user_id == auth.user_id,
            CompanionGroupBlock.companion_key == companion_key,
        )
    )
    if blocked and existing is None:
        db.add(
            CompanionGroupBlock(
                user_id=auth.user_id,
                companion_key=companion_key,
                blocked_at=datetime.now(UTC),
                blocked_by_user_id=auth.user_id,
            )
        )
    elif not blocked and existing is not None:
        await db.execute(
            delete(CompanionGroupBlock)
            .where(
                CompanionGroupBlock.user_id == auth.user_id,
                CompanionGroupBlock.companion_key == companion_key,
            )
            .execution_options(synchronize_session=False)
        )
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="companion.group_block.set",
        resource_type="companion_group_block",
        resource_id=companion_key,
        source=AUDIT_SOURCE,
        details={"companion_key": companion_key, "blocked": blocked},
    )
    await db.commit()
    if clerk_id is not None:
        reset_companion_tools_cache_for(clerk_id)

    return await _companion_summary_for(db, auth=auth, companion_key=companion_key)


async def resolve_cap_vector_settings(
    db: AsyncSession, *, auth: AuthContext
) -> CompanionCapListResponse:
    """Every deployed companion's blast-radius ceiling, and whether the tenant ever chose it.

    **One entry per COMPANION, because ``uq_companion_cap_policy`` is over
    ``(user_id, companion_key)``.** There is one companion kind in the registry today, so the
    dashboard will render one card -- but shaping the API around that would make a second companion
    an API change rather than a rendering change, and would misdescribe a table that has always
    been per-companion.
    """
    kinds = await deployed_companion_kinds(auth, db)
    return CompanionCapListResponse(
        items=[
            await _cap_response(db, user_id=auth.user_id, companion_key=key)
            for key in sorted(kinds)
        ]
    )


async def set_cap_vector(
    db: AsyncSession, *, auth: AuthContext, companion_key: str, values: CompanionCapVectorRequest
) -> CompanionCapVectorResponse:
    """Save all six ceilings for one companion as ONE unit, and read the saved vector back.

    Never partially: the request model requires all six with no defaults, so there is no state in
    which some ceilings are the tenant's and the rest are the shipped ones with nothing saying
    which is which. Zero survives the whole round trip as zero -- ``companion_caps`` names reading
    zero as falsy and substituting a default as the single most likely way this guarantee is lost,
    because it turns the tightest possible policy into no policy at all.

    Caps have NO history column, so the control-plane audit event is the only record that a ceiling
    moved and who moved it. It is recorded in the same transaction as the row, for the reason the
    policy write records.
    """
    await _owned_companion(db, auth=auth, companion_key=companion_key)
    saved = values.model_dump()
    row = (
        await db.execute(
            select(CompanionCapPolicy).where(
                CompanionCapPolicy.user_id == auth.user_id,
                CompanionCapPolicy.companion_key == companion_key,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        db.add(CompanionCapPolicy(user_id=auth.user_id, companion_key=companion_key, **saved))
    else:
        for field, value in saved.items():
            setattr(row, field, value)
        db.add(row)
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="companion.cap_policy.set",
        resource_type="companion_cap_policy",
        resource_id=companion_key,
        source=AUDIT_SOURCE,
        details={"companion_key": companion_key, **saved},
    )
    await db.commit()

    return await _cap_response(db, user_id=auth.user_id, companion_key=companion_key)


async def resolve_consumption(
    db: AsyncSession, *, auth: AuthContext, moment: datetime
) -> CompanionConsumptionResponse:
    """What this tenant has spent against the ceilings that will judge the next call.

    **The honest shape matters more than the requested name.** ``05-UI-SPEC.md`` calls this a live
    SESSION read-out. What the server actually meters is two ledgers, and this read-out shows both,
    each by its OWN period: exactly one WINDOW item per companion and Agent, whose ``session_id`` is
    null and which is the ceiling enforced on every call, plus every SESSION item whose budget is
    still live. A read-out labelled "this session" alone would name a scope the server does not
    meter, which is the same failure as displaying a mode the server does not gate on -- and that
    reasoning is what makes the null-session item worth LABELLING rather than hiding, since it is
    the one a refusal will most often cite.

    **Each ledger is filtered by the period the gate itself uses (GC2-02, WINDOWS #188 -- closed by
    plan 06-29).** The WINDOW ledger keeps its equality to the current hour: that row's key contains
    the hour, so a new hour is a new row and an earlier one is history. The SESSION ledger is
    selected by ``window_started_at`` at or after ``companion_facade.session_budget_cutoff`` of the
    hour the debit would stamp -- the exact expression ``apply_session_debit`` passes as its
    ``reset_if_opened_before``. A session row's stamp is when that budget OPENED, not which hour it
    belongs to, so a conversation three hours into a four-hour lifetime is read out here with the
    counter that will refuse it; a row older than the cutoff is one the next debit RESETS rather
    than refuses against, so it is not shown. The read-out and the gate agree by construction
    rather than by coincidence, including at the boundary: the debit's reset is strict ("opened
    before"), so this filter is inclusive.

    **The order is total (GC2-01, WINDOWS #187 -- closed by plan 06-29, together with #188 as the
    ledger requires).** Companion key, Agent, then ``session_id`` with nulls FIRST, then the stamp.
    Ordering on companion key and Agent alone left one companion's rows as a tie PostgreSQL broke
    however the plan fell, and widening the session filter without this would have put two tied
    rows in front of a consumer. The window row now leads its companion on every read, and the web
    caps card selects it by its null ``session_id`` -- the discriminator this response carries --
    rather than by position.

    (Plan 05-10 made an older wording here false: ``call_companion_tool`` does receive a
    conversation id now, threaded down from the ``Mcp-Session-Id`` header. It is still not the only
    key the budget is addressed by, and after GC-01 it is deliberately not the one that bounds the
    hour -- see ``companion_facade.apply_session_debit``.)

    So the window is reported whether or not a budget row exists, and the session identifier is
    carried as an explicit null on the window item. An empty list then reads as "nothing spent since
    this time" rather than as "unknown" -- this phase has collapsed null into a neighbouring value
    three times already, and an unlabelled empty answer is how it happens a fourth.
    """
    window = budget_window(moment)
    # `apply_session_debit` stamps and judges with the hour-truncated window as its moment, so this
    # is `reset_if_opened_before` exactly -- not a cutoff taken from the unrounded clock.
    session_cutoff = session_budget_cutoff(window)
    rows = (
        (
            await db.execute(
                select(CompanionSessionBudget)
                .where(
                    CompanionSessionBudget.user_id == auth.user_id,
                    or_(
                        and_(
                            CompanionSessionBudget.session_id.is_(None),
                            CompanionSessionBudget.window_started_at == window,
                        ),
                        and_(
                            CompanionSessionBudget.session_id.is_not(None),
                            CompanionSessionBudget.window_started_at >= session_cutoff,
                        ),
                    ),
                )
                .order_by(
                    CompanionSessionBudget.companion_key,
                    CompanionSessionBudget.environment_id,
                    CompanionSessionBudget.session_id.asc().nulls_first(),
                    CompanionSessionBudget.window_started_at,
                )
            )
        )
        .scalars()
        .all()
    )
    items = []
    for row in rows:
        # Through the façade's own resolver, so the ceiling this read-out renders a fraction
        # against is the same number the next call is judged by. Carried ON the item rather than
        # fetched by a second request: two responses divided by each other are two moments in time
        # presented as one.
        caps = await resolve_cap_vector(db, user_id=auth.user_id, companion_key=row.companion_key)
        items.append(
            CompanionConsumptionItemResponse(
                companion_key=row.companion_key,
                environment_id=row.environment_id,
                session_id=row.session_id,
                calls_consumed=row.calls_consumed,
                records_consumed=row.records_consumed,
                bytes_consumed=row.bytes_consumed,
                per_session_calls=caps.per_session_calls,
                per_session_records=caps.per_session_records,
                per_session_bytes=caps.per_session_bytes,
            )
        )
    return CompanionConsumptionResponse(
        window_started_at=window,
        window_seconds=SESSION_BUDGET_WINDOW_SECONDS,
        items=items,
    )


async def _cap_response(
    db: AsyncSession, *, user_id: UUID, companion_key: str
) -> CompanionCapVectorResponse:
    """One companion's cap vector, with ``is_default`` derived from ROW EXISTENCE.

    Never by comparing the six values to ``DEFAULT_CAPS``. A tenant who read the shipped numbers,
    agreed with them and saved them made a decision; reporting that decision as an absence would
    show their choice as something nobody ever made, and would additionally make this field's
    answer change whenever the shipped defaults were retuned. The existence query below reads only
    ``updated_at`` -- it is the EXISTENCE test, and the timestamp is the second fact that has to
    travel with it.

    The VALUES come from ``resolve_cap_vector``, the façade's own public resolver, rather than from
    a second read of the same table: the number the dashboard shows and the number the next call is
    judged against cannot be allowed to disagree.
    """
    updated_at = await db.scalar(
        select(CompanionCapPolicy.updated_at).where(
            CompanionCapPolicy.user_id == user_id,
            CompanionCapPolicy.companion_key == companion_key,
        )
    )
    caps = await resolve_cap_vector(db, user_id=user_id, companion_key=companion_key)
    return CompanionCapVectorResponse(
        companion_key=companion_key,
        per_call_calls=caps.per_call_calls,
        per_call_records=caps.per_call_records,
        per_call_bytes=caps.per_call_bytes,
        per_session_calls=caps.per_session_calls,
        per_session_records=caps.per_session_records,
        per_session_bytes=caps.per_session_bytes,
        is_default=updated_at is None,
        updated_at=updated_at,
    )


async def _owned_verb(
    db: AsyncSession, *, auth: AuthContext, tool: str
) -> tuple[CompanionVerbEntry, str, str]:
    """Resolve a composite tool name to a verb this tenant can actually configure, or 404.

    404 rather than 403 in BOTH failure modes, for the reason ``routes/companions.py::_decide``
    records: a 403 confirms the thing exists, which is a membership oracle -- here over which
    companions somebody else has deployed.

    A companion this tenant has not deployed is a 404 too, and not merely for symmetry: the catalog
    is deployment-scoped, so a write with no corresponding read would leave an orphan row no
    surface will ever show and no reader could ever undo.
    """
    try:
        entry = companion_verb(tool)
    except KeyError:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "no such companion tool") from None
    companion_key, verb = _split(tool)
    await _owned_companion(db, auth=auth, companion_key=companion_key)
    return entry, companion_key, verb


async def _owned_companion(db: AsyncSession, *, auth: AuthContext, companion_key: str) -> None:
    """Assert this tenant has ``companion_key`` deployed, or 404.

    Resolved through ``deployed_companion_kinds`` -- the façade's own helper -- so "deployed" means
    the same thing here as it does to the agent's tool list. A second membership rule would make
    the settings surface configurable for a companion the agent cannot reach, or vice versa.
    """
    kinds = await deployed_companion_kinds(auth, db)
    if companion_key not in kinds:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "no such companion tool")


async def _companion_summary_for(
    db: AsyncSession, *, auth: AuthContext, companion_key: str
) -> CompanionSummaryResponse:
    """The summary the CATALOG would return for one companion, re-resolved after a write.

    Re-resolved through ``resolve_tool_catalog`` rather than assembled from the request, for the
    same reason the policy write returns the catalog's own row: the two must be the same answer,
    and the cheapest way to guarantee that is for there to be only one of them.
    """
    catalog = await resolve_tool_catalog(db, auth=auth)
    for summary in catalog.companions:
        if summary.companion_key == companion_key:
            return summary
    raise HTTPException(http_status.HTTP_404_NOT_FOUND, "no such companion tool")


def _tool_settings(
    name: str,
    entry: CompanionVerbEntry,
    *,
    stored_mode: str | None,
    kinds: frozenset[str],
    destroy_keys: frozenset[str],
    blocked_keys: frozenset[str],
) -> CompanionToolSettingsResponse:
    """One catalog row, field by field from the registry and the tenant's own state.

    Field by field rather than by serializing anything, for the reason ``_approval_response``
    records: a field added later must reach the dashboard because somebody chose to send it.

    The decision comes from ``evaluate_policy`` -- the same function ``call_companion_tool`` uses,
    given the same two inputs. Recomputing the resolution here with a second rule set is how a
    surface ends up displaying a mode the server does not gate on, which is this plan's one
    standing prohibition.
    """
    companion_key, verb = _split(name)
    destroy_enabled = companion_key in destroy_keys
    decision = evaluate_policy(
        entry.tool_class, policy_mode=stored_mode, destroy_enabled=destroy_enabled
    )
    return CompanionToolSettingsResponse(
        tool=name,
        companion_key=companion_key,
        verb=verb,
        tool_class=entry.tool_class,
        cap_unit=entry.cap_unit,
        dry_run_available=entry.tool_class in MUTATING_TOOL_CLASSES,
        stored_mode=stored_mode,
        resolved_mode=decision.mode,  # pyright: ignore[reportArgumentType]
        resolved_reason=decision.reason,
        destroy_enabled=destroy_enabled,
        exposed_to_agent=is_exposed_to_agent(
            name,
            entry,
            kinds=kinds,
            destroy_enabled_keys=destroy_keys,
            blocked_keys=blocked_keys,
        ),
    )


def _companion_summary(
    companion_key: str,
    *,
    environment_ids: list[UUID],
    destroy_keys: frozenset[str],
    blocked_keys: frozenset[str],
    tools: list[CompanionToolSettingsResponse],
) -> CompanionSummaryResponse:
    """One deployed companion: the two destroy facts kept apart, and the group block beside them.

    ``has_destroy_verbs`` is a property of the REGISTRY and ``destroy_enabled`` is a property of the
    TENANT. Collapsing them would make "this companion has nothing to destroy" and "this companion's
    destroy group is off" the same answer, and only one of them has a switch worth rendering.
    ``blocked`` is the tenant's group block (plan 06-24); ``visible_verb_count`` follows it through
    ``is_exposed_to_agent``, so a blocked group reads zero visible verbs.
    """
    return CompanionSummaryResponse(
        companion_key=companion_key,
        environment_ids=environment_ids,
        has_destroy_verbs=any(
            entry.tool_class == "destroy"
            for name, entry in COMPANION_VERBS.items()
            if _split(name)[0] == companion_key
        ),
        destroy_enabled=companion_key in destroy_keys,
        blocked=companion_key in blocked_keys,
        visible_verb_count=sum(
            1 for row in tools if row.companion_key == companion_key and row.exposed_to_agent
        ),
    )


async def _active_policy_modes(db: AsyncSession, *, user_id: UUID) -> dict[tuple[str, str], str]:
    """Every ACTIVE policy row this tenant holds, in one query, keyed by ``(companion_key, verb)``.

    One query rather than one per verb: the catalog walks the whole registry, and a lookup per row
    would make the response cost proportional to the vocabulary rather than to the tenant's state.

    ``revoked_at IS NULL`` is the same predicate ``companion_facade._policy_mode`` reads under, and
    the unique partial index makes at most one live row per key possible -- so this is a mapping
    rather than a resolution between competing rows.
    """
    rows = await db.execute(
        select(
            CompanionToolPolicy.companion_key,
            CompanionToolPolicy.verb,
            CompanionToolPolicy.mode,
        ).where(
            CompanionToolPolicy.user_id == user_id,
            CompanionToolPolicy.revoked_at.is_(None),
        )
    )
    return {(companion_key, verb): mode for companion_key, verb, mode in rows}


async def _deployment_environments(
    auth: AuthContext, db: AsyncSession, *, kinds: frozenset[str]
) -> dict[str, list[UUID]]:
    """Which of this tenant's Agents each companion is deployed on.

    Read from ``deployed_companion_environments`` -- the helper ``deployed_companion_kinds`` is
    built on -- so a kind deployed through a completed installation (plan 06-11) is placed on its
    installation's Agent exactly as a provisioner-declared kind is. ``kinds`` is still resolved
    through the kinds helper rather than derived here: the catalog's membership test and the
    agent's must be the same answer, and this exists only to say WHERE, never WHETHER.

    The tenant predicate comes from the auth context, as it is in every companion route -- there
    is no parameter here through which another tenant's Agents could arrive.
    """
    placements = await deployed_companion_environments(auth, db)
    return {key: agents for key, agents in placements.items() if key in kinds}


def _split(name: str) -> tuple[str, str]:
    """A composite D-04 tool name as its ``(companion_key, verb)`` pair.

    The registry's lookup key is the composite and stays the composite -- this split is for
    addressing rows in two tables that store the halves separately, never for reconstructing a name
    to validate.
    """
    companion_key, _, verb = name.partition("__")
    return companion_key, verb
