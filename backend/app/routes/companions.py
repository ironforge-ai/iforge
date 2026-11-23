"""The companion surfaces a person reads and decides on: approvals, and the activity log (D-09).

Two things live here because they are one surface from the reader's side -- the log is where a
refusal is explained, and the approvals list is where the one kind of refusal a person can answer
is answered. Plan 05-07 added the activity log at the bottom of this file; the approvals routes
below came with 05-06 and are unchanged.

Modelled on ``app/routes/audit.py``, and the three things copied from it are copied deliberately:

* **The tenant predicate is the FIRST entry in every filter list, and it is derived from the auth
  context.** No route here accepts a tenant, user or environment identifier as a parameter, so the
  scope is not something a handler could get wrong under a supplied value -- there is no value. A
  listing whose scope came from a query parameter is a cross-tenant read one typo away (T-5-42).
  The one exception is the companion-installation routes (plan 06-09), which name the Agent to
  install on or filter by. That identifier only ever NARROWS the auth-derived scope: it is first
  proven to be one of the caller's own active agents, and a foreign or archived one is a 404.
* **The listing is bounded by the same ``Query(default=50, ge=1, le=200)``**, and ordered by
  creation time descending with the row identifier as the tiebreaker. Creation time alone is not a
  total order -- two approvals minted in the same millisecond tie -- and paging over a partial
  order repeats or skips rows.
* **Responses are constructed field by field from named columns**, never by serializing the model.
  The continuation token is never persisted, so there is no column for a serializer to leak; the
  action digest IS stored and is left out on purpose (T-5-44).

**Why ``require_web_auth`` and not ``require_user_auth``.** The audit route uses the latter, which
admits a wide-access CLI key. An agent authenticates with a CLI key. If that key could approve, the
gate would be a suggestion the agent can grant itself, and D-09's whole reason for putting the
decision in the dashboard would be gone. ``require_web_auth`` is Clerk-JWT-only and refuses both
API keys and first-party OAuth CLI tokens -- the same dependency the device-authorization approval
flow uses, for the same "a leaked key must not become an approval" reason.

**Why the decision is a conditional UPDATE rather than a read followed by a write.** Two browser
tabs are the ordinary case, not the adversarial one. ``WHERE status = 'issued'`` matches for
exactly one of them; the loser gets a 409 and no second approval exists to be spent (T-5-43).

There is deliberately NO agent-reachable approval endpoint here, and no channel delivery.
``05-CONTEXT.md`` D-09 records channel-delivered approvals as considered and deferred.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext, require_web_auth
from app.core.companion_catalog import CATALOG_TIERS, COMPANION_CATALOG, CompanionImagePin
from app.core.companion_registry import (
    COMPANION_KEY_PATTERN,
    COMPANION_TOOL_NAME_PATTERN,
    companion_verb,
)
from app.core.database import get_session
from app.models.companion_approval import (
    STATUS_DENIED,
    STATUS_ISSUED,
    STATUS_PREPARED,
    CompanionApproval,
)
from app.models.companion_dispatch import CompanionDispatchCall
from app.models.companion_installation import (
    CompanionInstallation,
    CompanionInstallationSecret,
    installation_is_serving,
)
from app.models.companion_tool_call import CallDecision, CompanionToolCall
from app.schemas.companions import (
    COMPANION_SPEND_MONTH_PATTERN,
    DEFAULT_INSTALL_LOG_PAGE,
    MAX_INSTALL_LOG_PAGE,
    CompanionApprovalListResponse,
    CompanionApprovalResponse,
    CompanionApprovalStatus,
    CompanionCapListResponse,
    CompanionCapVectorRequest,
    CompanionCapVectorResponse,
    CompanionCatalogImageResponse,
    CompanionCatalogImagesResponse,
    CompanionCatalogItemResponse,
    CompanionCatalogResponse,
    CompanionCatalogTierResponse,
    CompanionConsumptionResponse,
    CompanionDestroyEnablementRequest,
    CompanionGroupBlockRequest,
    CompanionInstallationListResponse,
    CompanionInstallationReachability,
    CompanionInstallationResponse,
    CompanionInstallationUpdate,
    CompanionInstallLogItemResponse,
    CompanionInstallLogResponse,
    CompanionInstallRequest,
    CompanionLoginLinkResponse,
    CompanionQuoteRequest,
    CompanionQuoteResponse,
    CompanionReachabilityState,
    CompanionSpendCeilingRequest,
    CompanionSpendCeilingResponse,
    CompanionSpendResponse,
    CompanionSummaryResponse,
    CompanionToolCallDetailResponse,
    CompanionToolCallListResponse,
    CompanionToolCallResponse,
    CompanionToolCatalogResponse,
    CompanionToolPolicyRequest,
    CompanionToolSettingsResponse,
    CompanionUninstallRequest,
    CompanionUpdateDecisionRequest,
)
from app.services.audit import record_control_plane_audit
from app.services.companion_billing import lowest_offered_monthly_price
from app.services.companion_budget import (
    SpendCeilingDecision,
    set_spend_ceiling,
    spend_ceiling_standing,
)
from app.services.companion_dispatch import approval_status_at
from app.services.companion_edge import NOT_ANSWERING, REACHABLE, TLS_PENDING
from app.services.companion_install import (
    create_installation,
    decide_companion_update,
    get_installation,
    list_install_log,
    list_installations,
    quote_companion,
    record_update_offers,
    request_companion_uninstall,
    update_view,
)
from app.services.companion_metering import companion_spend_for, month_start_of, parse_month
from app.services.companion_provenance import SOURCE_CONTENT_REF_DISCLOSURE
from app.services.companion_sanitize import UNTRUSTED_BODY_LIMIT, wrap_untrusted
from app.services.companion_settings import (
    resolve_cap_vector_settings,
    resolve_consumption,
    resolve_tool_catalog,
    set_cap_vector,
    set_destroy_enablement,
    set_group_block,
    set_tool_policy,
)
from app.services.hosted_owner import hosted_owner_id
from app.services.vault_crypto import decrypt_with_key_version

router = APIRouter(prefix="/companions", tags=["companions"])


@router.get("/approvals")
async def list_companion_approvals(
    status: CompanionApprovalStatus | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionApprovalListResponse:
    """This tenant's pending approvals, or one named state of them.

    The default is what a person came here to answer: undecided and not yet expired. Listing an
    expired request as pending would offer a decision the server then refuses with a conflict --
    a mode displayed that the server does not actually gate on.
    """
    moment = datetime.now(UTC)
    filters = [CompanionApproval.user_id == auth.user_id]
    if status is None:
        filters.append(CompanionApproval.status == STATUS_ISSUED)
        filters.append(CompanionApproval.expires_at > moment)
    else:
        filters.append(CompanionApproval.status == status)
    result = await db.execute(
        select(CompanionApproval)
        .where(*filters)
        .order_by(CompanionApproval.created_at.desc(), CompanionApproval.id.desc())
        .limit(limit)
    )
    return CompanionApprovalListResponse(
        items=[_approval_response(row, moment) for row in result.scalars().all()]
    )


@router.post("/approvals/{approval_id}/approve")
async def approve_companion_approval(
    approval_id: UUID,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionApprovalResponse:
    """Authorize one canonical action once.

    An expired request cannot be approved: the clock already answered it, and approving one would
    mint an authorization the redemption path refuses anyway.
    """
    return await _decide(db, auth=auth, approval_id=approval_id, decision=STATUS_PREPARED)


@router.post("/approvals/{approval_id}/deny")
async def deny_companion_approval(
    approval_id: UUID,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionApprovalResponse:
    """Decline one canonical action, terminally.

    Unlike approval this does not require the request to be unexpired. Declining something that
    has already lapsed is harmless and clears the list, and refusing the click would be a confusing
    answer to an unambiguous intent.
    """
    return await _decide(db, auth=auth, approval_id=approval_id, decision=STATUS_DENIED)


async def _decide(
    db: AsyncSession,
    *,
    auth: AuthContext,
    approval_id: UUID,
    decision: str,
) -> CompanionApprovalResponse:
    """Move one ``issued`` approval to a terminal decision, or explain why it did not move.

    The tenant predicate is first here too, and it is what makes a cross-tenant identifier a 404
    rather than a 403: a 403 would confirm the row exists, which is a membership oracle over
    somebody else's pending writes.

    404 and 409 are distinguished by a second read rather than collapsed into one status, because
    "there is no such approval" and "somebody already answered this in another tab" are different
    things for the person clicking, and a single answer would make one of them unactionable.
    """
    moment = datetime.now(UTC)
    owned = [
        CompanionApproval.user_id == auth.user_id,
        CompanionApproval.id == approval_id,
    ]
    conditions = [*owned, CompanionApproval.status == STATUS_ISSUED]
    if decision == STATUS_PREPARED:
        conditions.append(CompanionApproval.expires_at > moment)

    moved = (
        await db.execute(
            update(CompanionApproval)
            .where(*conditions)
            .values(status=decision, decided_at=moment, decided_by_user_id=auth.user_id)
            .returning(CompanionApproval.id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()

    if moved is None:
        existing = (
            await db.execute(
                select(CompanionApproval).where(*owned).execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise HTTPException(http_status.HTTP_404_NOT_FOUND, "no such companion approval")
        raise HTTPException(
            http_status.HTTP_409_CONFLICT,
            f"this approval is already {approval_status_at(existing, moment)}",
        )

    await db.commit()
    row = (
        await db.execute(
            select(CompanionApproval).where(*owned).execution_options(populate_existing=True)
        )
    ).scalar_one()
    return _approval_response(row, moment)


def _approval_response(row: CompanionApproval, moment: datetime) -> CompanionApprovalResponse:
    """One row, field by field. Deliberately not a model serialization.

    Every field here was chosen: the tool and its class so a person knows what kind of thing this
    is, the arguments preview so they know what it would touch, the resolved target count so they
    know how much, the budget and the expiry so they know what approving would grant and for how
    long. ``canonical_action_sha256`` is not among them, and neither is anything a holder could
    redeem.
    """
    return CompanionApprovalResponse(
        id=row.id,
        tool=f"{row.companion_key}__{row.verb}",
        tool_class=_tool_class(row),
        companion_key=row.companion_key,
        verb=row.verb,
        arguments_preview=dict(row.arguments or {}),
        resolved_target_count=row.resolved_target_count,
        remaining_budget=row.remaining_budget,
        status=approval_status_at(row, moment),  # pyright: ignore[reportArgumentType]
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        created_at=row.created_at,
    )


def _tool_class(row: CompanionApproval) -> str:
    """This verb's class from the REGISTRY, never from a column on the row.

    Same reasoning as ``companion_dispatch.dispatch_call_token`` reading ``method`` and
    ``path_template`` from the registry: the contract file is the single authority for what a verb
    is, and a copy stored on a row is a copy that can drift from it. ``unknown`` rather than a
    raise for a verb the registry no longer owns -- an approval for a withdrawn verb must still be
    visible enough to deny.
    """
    try:
        return companion_verb(f"{row.companion_key}__{row.verb}").tool_class
    except KeyError:
        return "unknown"


# ---------------------------------------------------------------------------
# The activity log (TOOL-07), added by plan 05-07.
#
# Copied from ``app/routes/audit.py`` in exactly the three respects the approvals routes above
# copied it: the tenant predicate is FIRST and comes from the auth context, the page is bounded by
# the same ``Query(default=50, ge=1, le=200)``, and the ordering is two-key. Every response is
# constructed field by field from named columns.
#
# **Why ``require_web_auth`` here too, where the audit route uses ``require_user_auth``.** The
# latter admits a wide-access CLI key, and a CLI key is what an AGENT authenticates with. The
# activity log is where a tenant reads back every refusal their agent collected, including the
# reason each gate gave -- handing that to the agent would be a new read path into the gates'
# own reasoning, reachable by injected content, for no consumer that exists: plans 05-08 and
# 05-09 both build on the dashboard. D-09 put this surface in the browser; this keeps it there.
# ---------------------------------------------------------------------------


@router.get("/activity")
async def list_companion_tool_calls(
    session_id: str | None = Query(default=None, min_length=1, max_length=128),
    tool_class: str | None = Query(default=None, min_length=1, max_length=16),
    decision: CallDecision | None = Query(default=None),
    companion_key: str | None = Query(default=None, min_length=1, max_length=31),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionToolCallListResponse:
    """This tenant's tool-call attempts, newest first -- refused ones included.

    **There is no tenant, owner or Agent parameter, and that absence is the scoping mechanism.**
    A listing whose scope came from a supplied value is one typo away from a cross-tenant read of
    the most sensitive kind (T-5-45); a value that does not exist cannot be preferred by mistake.
    The four filters NARROW within the tenant predicate and can never widen past it, because they
    are appended after it.

    **``decision`` is typed as the enum itself rather than as a mirrored literal.** The vocabulary
    has one source -- the model's CHECK constraint is generated from it too -- so an unknown value
    is a 422 at the boundary and the OpenAPI schema the dashboard generates against carries the
    facet list without anybody maintaining a second copy of it.

    **Ordering is two-key and paging is an explicit offset.** Creation time alone is not a total
    order: two attempts recorded in the same millisecond are ordinary at an agent's call rate, and
    over a partial order a page boundary between them repeats or skips a row. A tenant with no
    calls gets an empty list and a 200, never a 404 -- their log exists and nothing has happened
    in it yet.
    """
    filters = [CompanionToolCall.user_id == auth.user_id]
    if session_id is not None:
        filters.append(CompanionToolCall.session_id == session_id)
    if tool_class is not None:
        filters.append(CompanionToolCall.tool_class == tool_class)
    if decision is not None:
        filters.append(CompanionToolCall.decision == decision.value)
    if companion_key is not None:
        filters.append(CompanionToolCall.companion_key == companion_key)
    result = await db.execute(
        select(CompanionToolCall)
        .where(*filters)
        .order_by(CompanionToolCall.created_at.desc(), CompanionToolCall.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return CompanionToolCallListResponse(
        items=[_tool_call_response(row) for row in result.scalars().all()]
    )


@router.get("/activity/{call_id}")
async def get_companion_tool_call(
    call_id: UUID,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionToolCallDetailResponse:
    """One attempt and the whole chain behind it, read top-down as one path.

    A row belonging to somebody else is a 404 rather than a 403, for the reason the approvals
    decision route already records: a 403 would confirm the row exists, which is a membership
    oracle over a stranger's activity.

    The companion's own bytes come back inside the SPOTLIGHTING ENVELOPE the agent received them
    in. A reviewer deciding whether to approve a write has to see what the agent saw, and this
    route would otherwise be the one place in the system where companion-controlled text arrives
    unframed -- in front of the person least equipped to notice.
    """
    owned = [
        CompanionToolCall.user_id == auth.user_id,
        CompanionToolCall.id == call_id,
    ]
    row = (await db.execute(select(CompanionToolCall).where(*owned))).scalar_one_or_none()
    if row is None:
        raise HTTPException(http_status.HTTP_404_NOT_FOUND, "no such companion tool call")
    base = _tool_call_response(row)
    return CompanionToolCallDetailResponse(
        **base.model_dump(),
        arguments_digest=row.arguments_digest,
        arguments_preview=dict(row.arguments or {}),
        call_jti=row.call_jti,
        approval_id=row.approval_id,
        approved_by_user_id=row.approved_by_user_id,
        # Present exactly when the reference is. A disclosure each renderer had to remember for
        # itself is a disclosure one of them will eventually forget.
        source_content_disclosure=(
            None if row.source_content_ref is None else SOURCE_CONTENT_REF_DISCLOSURE
        ),
        dry_run_diff=row.dry_run_diff,
        companion_content=await _companion_content(db, row=row),
        repeat_count=row.repeat_count,
    )


def _tool_call_response(row: CompanionToolCall) -> CompanionToolCallResponse:
    """One row, field by field. Deliberately not a model serialization.

    Same reasoning as ``_approval_response``: a column added later must reach the dashboard
    because somebody chose to send it, not because nobody remembered to exclude it.
    """
    return CompanionToolCallResponse(
        id=row.id,
        created_at=row.created_at,
        session_id=row.session_id,
        environment_id=row.environment_id,
        tool=f"{row.companion_key}__{row.verb}",
        tool_class=row.tool_class,
        companion_key=row.companion_key,
        verb=row.verb,
        decision=CallDecision(row.decision),
        dry_run=row.dry_run,
        cap_unit=row.cap_unit,
        cap_consumed=row.cap_consumed,
        cap_remaining_after=row.cap_remaining_after,
        error_text=row.error_text,
        parent_call_id=row.parent_call_id,
        source_content_ref=row.source_content_ref,
    )


async def _companion_content(db: AsyncSession, *, row: CompanionToolCall) -> str | None:
    """The companion bytes this attempt returned, re-wrapped, or ``None``.

    Re-wrapped rather than stored wrapped: the envelope's mark is generated fresh per response on
    purpose (a fixed or reused mark is a boundary string injected content could learn once and
    forge afterwards), so the envelope is built at each egress rather than persisted.

    The tenant predicate is first here too, and it is not redundant with the caller's row having
    already been scoped: ``call_jti`` is the join key, and a join that could reach across tenants
    would be a cross-tenant read arriving through the back of a route that scoped its front.
    """
    if row.call_jti is None:
        return None
    dispatch = (
        await db.execute(
            select(CompanionDispatchCall).where(
                CompanionDispatchCall.user_id == row.user_id,
                CompanionDispatchCall.call_jti == row.call_jti,
            )
        )
    ).scalar_one_or_none()
    if dispatch is None:
        return None
    body = (dispatch.result or {}).get("body")
    if not isinstance(body, str) or not body:
        return None
    return wrap_untrusted(
        body,
        content_id=dispatch.call_jti,
        source=f"{row.companion_key}:{row.verb}",
        limit=UNTRUSTED_BODY_LIMIT,
    )


# ---------------------------------------------------------------------------
# The settings surface (TOOL-02, TOOL-04), added by plan 05-08a.
#
# The three properties the approvals and activity routes copied from ``app/routes/audit.py`` hold
# here too, and the first of them matters more here than anywhere else in this file: **the tenant
# predicate is derived from the auth context and there is no parameter through which another one
# could arrive.** These routes WRITE safety controls. A scope taken from a supplied value would be
# a cross-tenant write to somebody else's gates rather than a cross-tenant read of their rows.
#
# **Why ``require_web_auth`` here too.** Same reason, one notch stronger. An agent authenticates
# with a CLI key. Every route below either reports the gates' own configuration or MOVES one of
# them -- raising a blast-radius ceiling, turning destroy tools on, setting a tool to run without
# asking. A CLI key reaching any of them would make every gate in this phase self-granted by the
# party the gates exist to bound, and D-09's reason for putting the decision surface in the browser
# would be gone. The catalog is not a re-export of ``tools/list`` either: it deliberately reports
# MORE, including destroy verbs while they are hidden from the agent, which is exactly why it must
# stay on the dashboard-only dependency.
#
# **Every first segment after ``/companions`` is a literal.** No path parameter occupies that
# position anywhere in this file, so a companion key can never shadow a route name -- the collision
# is unrepresentable rather than merely unlikely.
#
# Canonical-only, inherited rather than re-declared: ``app/main.py`` registers this whole router in
# ``_CANONICAL_ONLY_ROUTERS``, and ``tests/test_api_version_alias.py`` carries ``/v1/companions/``
# as a PREFIX. ``tests/test_companion_settings.py`` asserts the six new paths genuinely 404 under
# ``/api`` rather than trusting the inheritance.
# ---------------------------------------------------------------------------


@router.get("/tools")
async def list_companion_tool_settings(
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionToolCatalogResponse:
    """Every verb of every companion this tenant has deployed, and what the server would decide.

    Each row carries the mode the tenant STORED and the mode the server would RESOLVE right now,
    plus the machine reason for the second. They are different facts: ``evaluate_policy`` refuses a
    destroy verb for a companion whose switch is off before it ever reads the explicit row, so a
    stored ``allow`` on such a verb is a mode the next call will not honour. One field would force
    this surface to display a guarantee that does not exist.

    A tenant with nothing deployed gets empty lists and a 200, never a 404 -- their catalog exists
    and nothing is in it, which is the same answer the activity log gives an empty log.
    """
    return await resolve_tool_catalog(db, auth=auth)


@router.put("/tools/{tool}/policy")
async def set_companion_tool_policy(
    body: CompanionToolPolicyRequest,
    tool: str = Path(pattern=COMPANION_TOOL_NAME_PATTERN),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionToolSettingsResponse:
    """Set one verb's approval mode, and answer with the row the catalog would return.

    ``{tool}`` is the composite D-04 name (``filebrowser__list_dir``), not a
    ``{companion_key}/{verb}`` pair. The registry's lookup key IS the composite --
    ``companion_verb`` takes it whole, and every other companion response already carries ``tool``
    in that form -- so a two-segment path could express a pair the registry never had and would
    have to be re-joined before anything could validate it.

    Bounded by ``COMPANION_TOOL_NAME_PATTERN`` at the path-parameter level, so a malformed
    identifier is a 422 at the boundary and never reaches a query. A well-formed name the registry
    does not own, and a well-formed name for a companion this tenant has not deployed, are both
    404s -- never 403s, because a 403 confirms existence.
    """
    return await set_tool_policy(db, auth=auth, tool=tool, mode=body.mode)


@router.put("/destroy-enablement/{companion_key}")
async def set_companion_destroy_enablement(
    body: CompanionDestroyEnablementRequest,
    companion_key: str = Path(pattern=COMPANION_KEY_PATTERN),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionSummaryResponse:
    """Turn this companion's whole destroy group on or off for this tenant (D-06).

    Per-companion and never per-verb: ``uq_companion_destroy_enablement`` makes a mixed
    enabled/disabled destroy group unrepresentable, which is a state no honest UI could render.

    Setting the switch to the position it already holds succeeds. It also does not touch any policy
    row -- these are independent gates, and the request bodies are disjoint under ``extra="forbid"``
    so neither can be expressed in terms of the other.
    """
    return await set_destroy_enablement(
        db, auth=auth, companion_key=companion_key, enabled=body.enabled
    )


@router.put("/group-block/{companion_key}")
async def set_companion_group_block(
    body: CompanionGroupBlockRequest,
    companion_key: str = Path(pattern=COMPANION_KEY_PATTERN),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionSummaryResponse:
    """Block or allow this companion's whole tool group for this tenant (APP-05, D-29/D-30).

    A blocked group is hidden from the agent's ``tools/list`` and refused by name at call time,
    recorded as ``refused_group_disabled``. Web auth only: an agent's CLI key can never reach this,
    so an agent cannot unblock a group it was blocked from. ``{companion_key}`` is bounded by
    ``COMPANION_KEY_PATTERN`` at the boundary, and a companion this tenant has not deployed is a
    404, never a 403, as for the destroy switch.
    """
    return await set_group_block(db, auth=auth, companion_key=companion_key, blocked=body.blocked)


@router.get("/caps")
async def list_companion_caps(
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionCapListResponse:
    """This tenant's blast-radius ceiling for each deployed companion (TOOL-04).

    ``is_default`` says whether the tenant ever CHOSE these numbers, and it is derived from row
    existence rather than by comparing values to the shipped defaults -- somebody who read the
    defaults, agreed and saved them made a decision, and reporting it as an absence would erase it.
    """
    return await resolve_cap_vector_settings(db, auth=auth)


@router.put("/caps/{companion_key}")
async def set_companion_caps(
    body: CompanionCapVectorRequest,
    companion_key: str = Path(pattern=COMPANION_KEY_PATTERN),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionCapVectorResponse:
    """Save all six ceilings for one companion, as one unit.

    Caps are per COMPANION -- ``uq_companion_cap_policy`` is over ``(user_id, companion_key)`` --
    so the companion is named in the path rather than assumed. All six values are required with no
    defaults: an optional field falling back to a default lets a client that forgot one silently
    widen a ceiling the tenant never touched. A negative value is a 422 at the boundary, upstream
    of the table's own non-negative check constraint, and a zero is a VALUE meaning "refuse
    everything" that round-trips as zero.
    """
    return await set_cap_vector(db, auth=auth, companion_key=companion_key, values=body)


@router.get("/consumption")
async def get_companion_consumption(
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionConsumptionResponse:
    """What this tenant has spent in the window that is actually being metered.

    The UI spec calls this a live SESSION read-out. The protocol as this backend speaks it delivers
    no session identifier to the façade, so what exists is a rolling one-hour window keyed by Agent
    and companion. This response names the window it covers, its width, and an explicit null
    session identifier on every item -- because a read-out labelled "this session" would name a
    scope the server does not meter, which is the same failure as displaying a mode the server does
    not gate on. Copy rendered above it should read "used since {time}", not "used this session".

    The window is reported whether or not any budget row exists, so an empty list means "nothing
    spent since this time" rather than "unknown".
    """
    return await resolve_consumption(db, auth=auth, moment=datetime.now(UTC))


@router.get("/spend")
async def get_companion_spend(
    month: str | None = Query(default=None, pattern=COMPANION_SPEND_MONTH_PATTERN),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionSpendResponse:
    """Your month-to-date companion spend by compute, storage and AI usage (OPS-05, D-23).

    ``month`` is ``YYYY-MM`` and defaults to the current UTC month. Every amount is summed from
    recorded ledger rows for your own installations and sessions; an amount nothing was recorded
    for is null, never zero, and companion-originated AI usage is not yet attributed (PLAT-03).
    """
    month_start = parse_month(month) if month else month_start_of(datetime.now(UTC))
    return await companion_spend_for(db, user_id=auth.user_id, month_start=month_start)


# The tenant's monthly money ceiling (OPS-06, D-20, plan 06-22). Dashboard-only
# (``require_web_auth``) for the reason every settings route above is: the ceiling bounds what an
# AGENT may spend, so an agent credential must not be able to read-modify-write it. The body can
# name no tenant, so the ceiling written is always the caller's.


def _spend_ceiling_response(standing: SpendCeilingDecision) -> CompanionSpendCeilingResponse:
    return CompanionSpendCeilingResponse(
        ceiling_usd_micros=standing.ceiling.ceiling_usd_micros,
        source=standing.ceiling.source,
        spent_usd_micros=standing.spent_usd_micros,
        reached=standing.reached,
        month=standing.month,
    )


@router.get("/spend-ceiling")
async def get_companion_spend_ceiling(
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionSpendCeilingResponse:
    """Your monthly companion spend ceiling, where it came from, and this month's recorded spend.

    ``source`` is ``tenant`` when you set the ceiling, ``operator_default`` when an operator
    default applies, and ``none`` when no ceiling applies (``ceiling_usd_micros`` is then null).
    At the ceiling (``reached``), agent tool calls are refused and your companions keep running.
    """
    return _spend_ceiling_response(await spend_ceiling_standing(db, user_id=auth.user_id))


@router.put("/spend-ceiling")
async def set_companion_spend_ceiling(
    body: CompanionSpendCeilingRequest,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionSpendCeilingResponse:
    """Set your monthly companion spend ceiling, in integer micro-dollars.

    A negative or non-integer amount is a 422. Zero is a value: agent tool calls are refused until
    it is raised. Only this dashboard route writes the ceiling; no tool argument can raise it.
    """
    await set_spend_ceiling(
        db,
        user_id=auth.user_id,
        ceiling_usd_micros=body.ceiling_usd_micros,
        updated_by_user_id=auth.user_id,
    )
    return _spend_ceiling_response(await spend_ceiling_standing(db, user_id=auth.user_id))


# ---------------------------------------------------------------------------
# Tenant-installed companions (phase 06, plan 09: APP-01, APP-03).
#
# Dashboard-only (``require_web_auth``) for the reason every settings route above is: installing a
# companion starts billable work on the tenant's agent, and an agent's own CLI key must not be able
# to grant itself new applications. Thin routes -- ``app/services/companion_install.py`` does the
# work, including the 404-never-403 ownership check.
#
# No response here can carry a credential: ``CompanionInstallationResponse.credential`` is the
# literal ``stored_in_vault``, and the platform-generated values live only in the iforge-owned
# encrypted store (CK-D).
# ---------------------------------------------------------------------------


_NO_HOSTNAME_YET = CompanionInstallationReachability(state="no_hostname_yet", checked_at=None)
_MEASURED_STATES: dict[str, CompanionReachabilityState] = {
    REACHABLE: "reachable",
    TLS_PENDING: "tls_pending",
    NOT_ANSWERING: "not_answering",
}


def _reachability(row: CompanionInstallation) -> CompanionInstallationReachability:
    """The probe's recorded answer, or ``no_hostname_yet`` -- never an inferred state (APP-02).

    Read from ``external_resources["edge"]["reachability"]``, where plan 06-14's lifecycle worker
    writes ``{state, checked_at}``. Only a complete installation reports a measured state: before
    completion the hostname is not routed yet, so a value left over from an earlier probe would
    describe an address the installation is not serving. A record with an unknown state or an
    unreadable time is treated as no measurement rather than guessed at. An installation in an
    update (plan 06-18) is still serving its hostname, so it keeps reporting what was measured.
    """
    if not installation_is_serving(row) or row.removed_at is not None:
        return _NO_HOSTNAME_YET
    edge = (row.external_resources or {}).get("edge")
    recorded = edge.get("reachability") if isinstance(edge, dict) else None
    if not isinstance(recorded, dict):
        return _NO_HOSTNAME_YET
    state = _MEASURED_STATES.get(str(recorded.get("state")))
    checked_at = recorded.get("checked_at")
    if state is None or not isinstance(checked_at, str):
        return _NO_HOSTNAME_YET
    try:
        moment = datetime.fromisoformat(checked_at)
    except ValueError:
        return _NO_HOSTNAME_YET
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return CompanionInstallationReachability(state=state, checked_at=moment)


def _update(row: CompanionInstallation) -> CompanionInstallationUpdate:
    offer = update_view(row)
    return CompanionInstallationUpdate(
        state=offer.state,  # pyright: ignore[reportArgumentType]
        offered_version=offer.offered_version,
        offered_digest=offer.offered_digest,
        deferred_digest=offer.deferred_digest,
        last_backup_at=offer.last_backup_at,
        error_code=offer.error_code,  # pyright: ignore[reportArgumentType]
    )


def _installation_response(row: CompanionInstallation) -> CompanionInstallationResponse:
    # Field by field from named columns, never by serializing the model: the secrets are another
    # table, but a column added later must not reach this surface by accident either.
    # The three vocabulary columns are plain strings to the type checker; the table's CHECK
    # constraints guarantee they are members of their literals, and pydantic re-checks at runtime.
    return CompanionInstallationResponse(
        id=row.id,
        agent_id=row.environment_id,
        companion_key=row.companion_key,
        isolation_tier=row.isolation_tier,  # pyright: ignore[reportArgumentType]
        operation=row.operation,  # pyright: ignore[reportArgumentType]
        status=row.status,  # pyright: ignore[reportArgumentType]
        step=row.step,
        error=row.error,
        version=row.version,
        pinned_digest=row.pinned_digest,
        hostname=row.hostname,
        owner_email=row.owner_email,
        reachability=_reachability(row),
        update=_update(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


@router.get("/catalog")
async def get_companion_catalog(
    auth: AuthContext = Depends(require_web_auth),
) -> CompanionCatalogResponse:
    """What this build can install, the images each runs, the tiers it offers, and from what price.

    Every image is a pinned sha256 index digest (D-28); an installation copies the app digest at
    request time. All three tiers the contract admits are listed, with only the offered ones
    marked so, because a tier missing from the list would read as a tier that does not exist.
    ``from_monthly_price_usd_micros`` is the lowest /v2 price of an offered tier for the caller
    (plan 06-17), or null when /v2 prices none or cannot be reached: a price is never invented.
    The caller is named to /v2 by Clerk id (``hosted_owner_id``, 06.2 D-09); a caller with none (a
    partner tenant) sees every price null and /v2 is not asked.
    The exact price for the chosen tier is the quote (``POST /v1/companions/quotes``).
    """

    def image(pin: CompanionImagePin) -> CompanionCatalogImageResponse:
        return CompanionCatalogImageResponse(image=pin.image, tag=pin.tag, sha256=pin.sha256)

    owner_id = hosted_owner_id(auth.user)
    from_prices = {
        entry.key: (
            None
            if owner_id is None
            else await lowest_offered_monthly_price(
                owner_user_id=owner_id,
                companion_key=entry.key,
                offered_tiers=entry.offered_tiers,
            )
        )
        for entry in COMPANION_CATALOG.values()
    }
    return CompanionCatalogResponse(
        items=[
            CompanionCatalogItemResponse(
                companion_key=entry.key,
                display_name=entry.display_name,
                version=entry.version,
                images=CompanionCatalogImagesResponse(
                    app=image(entry.app),
                    postgres=image(entry.postgres),
                    redis=image(entry.redis),
                    cloudflared=image(entry.cloudflared),
                ),
                tiers=[
                    CompanionCatalogTierResponse(
                        tier=tier,  # pyright: ignore[reportArgumentType]
                        offered=entry.tier_offered(tier),
                    )
                    for tier in CATALOG_TIERS
                ],
                from_monthly_price_usd_micros=from_prices[entry.key],
            )
            for entry in COMPANION_CATALOG.values()
        ]
    )


@router.post("/installations", status_code=http_status.HTTP_202_ACCEPTED)
async def request_companion_installation(
    body: CompanionInstallRequest,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallationResponse:
    """Ask for a companion on one of your own agents; the answer is a queued install job (D-26).

    Every credential the companion needs is generated here, server-side, and stored encrypted where
    no tenant or agent API can read it back (D-27, CK-D). The request carries none, and the
    response returns none.
    """
    row = await create_installation(
        db,
        auth=auth,
        agent_id=body.agent_id,
        companion_key=body.companion_key,
        isolation_tier=body.isolation_tier,
        quote_id=body.quote_id,
    )
    return _installation_response(row)


@router.post("/quotes")
async def create_companion_quote(
    body: CompanionQuoteRequest,
    auth: AuthContext = Depends(require_web_auth),
) -> CompanionQuoteResponse:
    """The price of one companion at one tier, shown before anything is provisioned (APP-08).

    The install request must carry this quote's id while it is current. Prices come from the
    hosted control plane's /v2 (ADR-0005); with /v2 unconfigured or unreachable this is 503
    ``companion_billing_unavailable`` and nothing can be installed.
    """
    quote = await quote_companion(
        auth=auth, companion_key=body.companion_key, isolation_tier=body.isolation_tier
    )
    return CompanionQuoteResponse(
        quote_id=quote.quote_id,
        companion_key=quote.companion_key,
        isolation_tier=quote.isolation_tier,  # pyright: ignore[reportArgumentType]
        currency=quote.currency,  # pyright: ignore[reportArgumentType]
        monthly_price_usd_micros=quote.monthly_price_usd_micros,
        storage_price_usd_micros_per_gib_month=quote.storage_price_usd_micros_per_gib_month,
        expires_at=quote.expires_at,
    )


@router.get("/installations")
async def list_companion_installations(
    agent_id: UUID | None = Query(default=None),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallationListResponse:
    """Your live installations, optionally for one of your own agents (404 for any other)."""
    rows = await list_installations(db, auth=auth, agent_id=agent_id)
    await record_update_offers(db, rows)
    return CompanionInstallationListResponse(items=[_installation_response(row) for row in rows])


@router.get("/installations/{installation_id}")
async def get_companion_installation(
    installation_id: UUID,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallationResponse:
    """One of your installations. Another tenant's installation is 404, never 403.

    ``update`` says whether a newer release is on offer (plan 06-18). The offer is computed on
    read, from the catalog, and stored; nothing about it changes what the installation runs.
    """
    row = await get_installation(db, auth=auth, installation_id=installation_id)
    await record_update_offers(db, [row])
    return _installation_response(row)


@router.post("/installations/{installation_id}/update")
async def decide_companion_installation_update(
    installation_id: UUID,
    body: CompanionUpdateDecisionRequest,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallationResponse:
    """Accept or defer the ONE release on offer for your installation (D-28, APP-06).

    Nothing updates without this. ``accept`` names the offered digest: the pin moves and an
    ``update`` operation starts, in which the runtime takes a backup first, cancels the update if
    the backup fails, and rolls back if Postiz fails its data assertion afterwards -- every failure
    returns the pin to the previous digest (OPS-02). ``defer`` sets that release aside; a later one
    is offered again. Any other digest is 409 ``companion_update_not_offered``; a decision while
    another operation is in flight is 409 ``companion_operation_in_progress``; accepting the digest
    already running is a no-op. Another tenant's installation is 404, never 403.
    """
    row = await decide_companion_update(
        db,
        auth=auth,
        installation_id=installation_id,
        decision=body.decision,
        digest=body.digest,
    )
    return _installation_response(row)


@router.post(
    "/installations/{installation_id}/uninstall", status_code=http_status.HTTP_202_ACCEPTED
)
async def uninstall_companion_installation(
    installation_id: UUID,
    body: CompanionUninstallRequest,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallationResponse:
    """Uninstall your installation, confirmed by typing the companion's name (APP-07, APP-08).

    ``confirm`` must be the companion's name exactly (case-sensitive, surrounding whitespace
    ignored), or this is 422 ``companion_uninstall_confirmation_mismatch``. Accepted, the answer is
    a running ``uninstall`` operation: billing stops first, then the runtime is told to remove the
    companion and its data, and once it reports that it did -- or after 30 minutes without that
    report -- the hostname, media bucket, workflow namespace and backup key are torn down. Existing
    backups are kept under their normal retention. Another operation in flight (another uninstall
    included) is 409 ``companion_operation_in_progress``; another tenant's installation is 404.
    """
    row = await request_companion_uninstall(
        db, auth=auth, installation_id=installation_id, confirm=body.confirm
    )
    return _installation_response(row)


@router.get("/installations/{installation_id}/logs")
async def get_companion_installation_logs(
    installation_id: UUID,
    after_seq: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_INSTALL_LOG_PAGE, ge=1, le=MAX_INSTALL_LOG_PAGE),
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionInstallLogResponse:
    """Your installation's log after ``after_seq``, as plain text lines with their stream.

    Every line was redacted against the installation's own credentials when it was stored, so no
    line here can carry one. The text is returned exactly as stored -- no HTML, no markdown, no
    other transformation -- and must be rendered as text. Another tenant's installation is 404.
    """
    rows = await list_install_log(
        db, auth=auth, installation_id=installation_id, after_seq=after_seq, limit=limit
    )
    return CompanionInstallLogResponse(
        items=[
            CompanionInstallLogItemResponse(
                seq=row.seq,
                stream=row.stream,  # pyright: ignore[reportArgumentType]
                line=row.line,
                created_at=row.created_at,
            )
            for row in rows
        ],
        next_after_seq=rows[-1].seq if rows else after_seq,
    )


# ---------------------------------------------------------------------------
# The owner's own Postiz sign-in (phase 06, plan 34: APP-03, CK-D).
#
# The human never sees the admin password the platform generated. Instead the control plane mints
# Postiz's own set-password link, ``https://<hostname>/auth/forgot/<jwt>`` with
# ``jwt = HS256({"id": <admin user id>, "expires": "YYYY-MM-DD HH:mm:ss"}, JWT_SECRET)`` (research
# Pattern 5, verified against Postiz v2.24.0 ``auth.service.ts``). It is signed with the
# installation's OWN jwt-secret atom, so only that installation accepts it. The atom is decrypted
# only to sign, and neither it, the token nor the URL is ever logged.
# ---------------------------------------------------------------------------

LOGIN_LINK_TTL = timedelta(minutes=20)
# Postiz parses ``expires`` with dayjs and compares it to the current time. The container runs in
# UTC, so the value is the UTC wall clock in dayjs' default ``YYYY-MM-DD HH:mm:ss`` form.
POSTIZ_EXPIRES_FORMAT = "%Y-%m-%d %H:%M:%S"
POSTIZ_SET_PASSWORD_PATH = "/auth/forgot/"
JWT_SECRET_ATOM = "jwt-secret"
LOGIN_LINK_MINTED = "companion.login_link.minted"
AUDIT_SOURCE = "api.companions"


def _companion_not_ready() -> HTTPException:
    return HTTPException(
        http_status.HTTP_409_CONFLICT,
        detail={
            "code": "companion_not_ready",
            "message": "This companion has not finished installing, so it has no sign-in yet.",
        },
    )


def _credentials_unreadable() -> HTTPException:
    return HTTPException(
        http_status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "code": "companion_credentials_unreadable",
            "message": "This installation's credentials cannot be read, so no link was minted.",
        },
    )


async def _installation_jwt_secret(db: AsyncSession, installation_id: UUID) -> str:
    """The installation's jwt-secret atom, decrypted under the key version recorded on its row.

    Fails closed: a missing atom, an unknown key version or a decrypt failure is a 503, and the
    reason never includes the value.
    """
    row = (
        await db.execute(
            select(
                CompanionInstallationSecret.encrypted_value,
                CompanionInstallationSecret.nonce,
                CompanionInstallationSecret.key_version,
            ).where(
                CompanionInstallationSecret.installation_id == installation_id,
                CompanionInstallationSecret.secret_name == JWT_SECRET_ATOM,
            )
        )
    ).one_or_none()
    if row is None:
        raise _credentials_unreadable()
    try:
        return decrypt_with_key_version(row.encrypted_value, row.nonce, row.key_version)
    except Exception:  # noqa: BLE001 - any decrypt failure means no link can be signed.
        raise _credentials_unreadable() from None


@router.post("/installations/{installation_id}/login-link")
async def mint_companion_login_link(
    installation_id: UUID,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> CompanionLoginLinkResponse:
    """A 20-minute link at which you set your own Postiz password (CK-D).

    Only for a COMPLETE installation. Completion follows the data-asserted ready report, which is
    what recorded the admin user id the link names, so a link minted earlier could claim the owner
    account before bootstrap finished (409 ``companion_not_ready``). Another tenant's installation
    is 404, never 403. The response names the sign-in email and nothing the platform generated.
    """
    installation = await get_installation(db, auth=auth, installation_id=installation_id)
    admin_user_id = (installation.external_resources or {}).get("admin_user_id")
    if (
        not installation_is_serving(installation)
        or installation.removed_at is not None
        or not isinstance(admin_user_id, str)
        or not admin_user_id
    ):
        raise _companion_not_ready()

    secret = await _installation_jwt_secret(db, installation.id)
    expires_at = (datetime.now(UTC) + LOGIN_LINK_TTL).replace(microsecond=0)
    token = jwt.encode(
        {"id": admin_user_id, "expires": expires_at.strftime(POSTIZ_EXPIRES_FORMAT)},
        secret,
        algorithm="HS256",
    )
    # One event per mint, committed BEFORE the link is returned: a link that reached a browser
    # without its event would be a credential nobody can attribute (T-06-34-04). The event names
    # the installation and its kind only -- never the URL, the token or any atom.
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action=LOGIN_LINK_MINTED,
        resource_type="companion_installation",
        resource_id=str(installation.id),
        environment_id=installation.environment_id,
        source=AUDIT_SOURCE,
        details={"companion_key": installation.companion_key},
    )
    await db.commit()
    return CompanionLoginLinkResponse(
        url=f"https://{installation.hostname}{POSTIZ_SET_PASSWORD_PATH}{token}",
        expires_at=expires_at,
        sign_in_email=installation.owner_email,
    )
