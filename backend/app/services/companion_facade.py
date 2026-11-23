"""The companion façade: the ONE place an agent's tool call becomes a companion request.

``list_companion_tools`` answers "what can this tenant's agent see", ``call_companion_tool`` runs
the fixed pipeline for a call. Both are consumed by ``app/routes/mcp_bridge.py``; neither is
reachable any other way, which is what makes "an agent reaches a companion only through a
Clawdi-owned façade" (TOOL-01) structural rather than a convention.

**The pipeline, in order.** Resolve the verb row -- an unknown name is an ``isError`` refusal and
is NEVER forwarded to the Composio connector fallthrough (T-5-05). Evaluate the class against the
set this façade can execute, failing closed on anything outside it. Re-derive the tenant from
``AuthContext``; no tenant identifier is ever read from an argument (T-5-08). Load that tenant's
destroy switch and its explicit row for this exact verb, and decide with
``app/core/companion_policy.evaluate_policy``. Resolve which of its environments has the companion
deployed. Mint the signed pre-authorized call, enqueue it, commit so the runtime's independent
connection can see it, then wait under a 25-second internal deadline inside the CLI proxy's
30-second budget. Sanitize and wrap whatever comes back. Mark the row terminal either way.

**The policy gate is UPSTREAM of the mint, and that placement is the whole point (D-10).** A call
that fails policy never becomes a signed pre-authorized call, so the runtime executor is never
handed an instruction it would have to be trusted to disobey. ``evaluate_policy`` takes no argument
payload at all, which is the structural reason TOOL-03 holds when the agent is told to ignore the
policy: there is no channel through which the telling could reach the decision (T-5-17).

**The destroy double-gate (D-06).** A ``destroy``-class verb is filtered out of the tool list for a
tenant without an enablement row, AND refused by name at call time for that same tenant. Hiding a
tool is not the enforcement -- the call-time half is what covers a hallucinated or
prompt-injected name, and it is what makes the 60-second list cache a staleness bound rather than an
exploit window (T-5-18, T-5-19).

**The group block is the same double gate, over a whole companion (APP-05, D-29/D-30).** A tenant
that blocked a companion's tool group sees none of its verbs in ``tools/list``, AND a call to any of
them by name is refused directly after the scope check, recorded as ``refused_group_disabled`` with
the ``[companion_group_disabled]`` token. The block row is read at call time, never from the cached
list, so an agent holding a list fetched before the block still cannot invoke the group. Blocking
is a security control rather than a preference, which is why its refusal has its own decision
instead of being folded into ``refused_policy``.

**The caps and the rate bucket are a SECOND gate, not a branch of the first (TOOL-04, TOOL-09).**
Policy says whether a call is permitted at all; the caps say how much it may touch and how fast
calls may arrive. They are wired as separate, independently reachable steps on purpose: folding cap
enforcement into the policy branch would make one of them unreachable without the other, and an
unreachable gate is an untestable one. The order is per-call cap, then rate-limit acquisition, then
-- inside the SAME transaction as ``mint_dispatch_call`` and ``enqueue_call`` -- the session debit.
The debit is a conditional ``UPDATE`` that fails when the budget is already spent, so two concurrent
callers cannot both spend one remainder (T-5-25), and a refused debit rolls the mint back with it.

**The money ceiling and the repeated-call breaker come BEFORE the caps (OPS-06, D-20, D-22).**
After the policy gate the façade checks the tenant's monthly spend ceiling, then the repeated-call
breaker (``companion_ratelimit.check_repeated_call``), and only then the caps and the mint. The
ceiling is the slow half of OPS-06, lagging behind metered cost; the breaker is the fast half, which
stops the same call repeating with the same arguments before any cost is recorded. Both refuse the
call and touch nothing else.

**No cap value reaches this module from an argument.** No kind's argument contract
(``app/services/companion_arguments.py``) declares a cap-shaped property, and every one sets
``additionalProperties: False``, so an argument named ``max_records``
or ``limit_override`` is rejected by validation before any cap is consulted; the value used always
comes from the tenant's ``companion_cap_policies`` row, which the agent has no write path to
(T-5-23). Those are two independent protections and
``backend/tests/test_companion_blast_radius.py`` asserts both, because either alone would leave the
other path open.

**The dry-run branch replaces the MINT, not the gates (TOOL-05, D-11/D-12).** A ``dry_run`` true
argument -- injected into the input schema of ``create``, ``mutate`` and ``destroy`` verbs and no
others -- runs the class, scope, argument, policy, cap and rate gates exactly as a real call does,
then routes to ``_run_dry_run`` instead of minting a mutating signed call. What gets minted is the
verb's declared READ PROBE, so no mutating pre-authorized envelope exists for an executor to find,
and the preview is built from what the companion actually reported rather than from the request.
A preview debits one ``calls`` unit and leaves ``records`` and ``bytes`` untouched.

**What is deliberately NOT here.** No approval continuation -- that is plan 05-06. An ``ask``
decision returns an ``isError`` naming the approval requirement until 05-06 replaces that branch
with the signed continuation, which is the safe direction to be incomplete in.

**No list-changed notification.** D-03 asked for one. Research verified there is no
server-to-client channel in either direction to carry it, and ``mcp_bridge`` declares
``{"tools": {"listChanged": False}}`` accordingly. The 60-second TTL below IS the staleness bound,
and uninstall is enforced at call time instead. Writing an emitter for a notification nothing can
receive would be a comment that compiles.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as uuid_module
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from pydantic import JsonValue
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext, require_auth_scopes, require_clerk_id
from app.core.companion_caps import (
    CAP_UNIT_BYTES,
    CAP_UNIT_CALLS,
    CAP_UNIT_RECORDS,
    DEFAULT_CAPS,
    REASON_SESSION_BUDGET_UNRESOLVED,
    REASON_WITHIN_CAP,
    CapDecision,
    CapVector,
    check_per_call_cap,
    debit_session_budget,
)
from app.core.companion_policy import (
    POLICY_MODE_ALLOW,
    POLICY_MODE_ASK,
    REASON_DESTROY_DISABLED,
    PolicyDecision,
    evaluate_policy,
)
from app.core.companion_registry import (
    COMPANION_TOOL_NAME_PATTERN,
    COMPANION_VERBS,
    CompanionVerbEntry,
    companion_verb,
    derive_annotations,
)
from app.models.companion_approval import (
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_ISSUED,
    STATUS_PREPARED,
    CompanionApproval,
)
from app.models.companion_installation import (
    COMPANION_INSTALLATION_STATUS_QUEUED,
    COMPANION_INSTALLATION_STATUS_RUNNING,
    CompanionInstallation,
    serving_installation_filter,
)
from app.models.companion_session_budget import (
    SESSION_BUDGET_LIFETIME_SECONDS,
    SESSION_BUDGET_WINDOW_SECONDS,
    CompanionCapPolicy,
    CompanionSessionBudget,
)
from app.models.companion_tool_call import CompanionToolCall
from app.models.companion_tool_policy import (
    CompanionDestroyEnablement,
    CompanionGroupBlock,
    CompanionToolPolicy,
)
from app.models.hosted_runtime import HostedRuntimeState
from app.models.session import AgentEnvironment
from app.schemas.companions import DRY_RUN_OUTPUT_SCHEMA, validate_dry_run_output
from app.services.companion_arguments import (
    _COMPANION_INPUT_SCHEMA as _COMPANION_INPUT_SCHEMA,
)
from app.services.companion_arguments import (
    _dispatch_arguments,
    argument_contract_for,
    argument_contract_kinds,
)
from app.services.companion_arguments import (
    _input_schema_for as _input_schema_for,
)
from app.services.companion_arguments import (
    _single_path_target_count as _single_path_target_count,
)
from app.services.companion_budget import check_spend_ceiling, spend_ceiling_refusal_message
from app.services.companion_dispatch import (
    APPROVAL_CONTINUATION_PREFIX,
    REASON_ACTION_HASH_MISMATCH,
    REASON_APPROVAL_BUDGET_SPENT,
    REASON_APPROVAL_DENIED,
    REASON_APPROVAL_EXPIRED,
    REASON_APPROVAL_NOT_FOUND,
    REASON_APPROVAL_READY,
    REASON_APPROVAL_REQUIRED,
    REASON_CONTINUATION_INVALID,
    REASON_TARGET_COUNT_MISMATCH,
    CompanionDispatchArgumentError,
    CompanionDispatchClaimError,
    approval_status_at,
    await_call_result,
    canonical_action_hash,
    consume_approval,
    enqueue_call,
    mint_continuation,
    mint_dispatch_call,
    record_refusal,
    redeem_continuation,
    upsert_approval,
)
from app.services.companion_dryrun import (
    MUTATING_TOOL_CLASSES,
    ProbeResponse,
    compute_dry_run,
    previewed_target_count,
    read_probe_tool_name,
    record_preview,
)
from app.services.companion_provenance import (
    CallDecision,
    prompting_content_ref,
    record_companion_tool_call,
    record_returned_content,
)
from app.services.companion_ratelimit import (
    SLOT_WAIT,
    RateLimitSlot,
    acquire_companion_slot,
    check_repeated_call,
)
from app.services.companion_sanitize import UNTRUSTED_BODY_LIMIT, wrap_untrusted

logger = logging.getLogger(__name__)

type JsonObject = dict[str, JsonValue]

# The companion scopes mirror the `connectors:read` / `connectors:invoke` precedent (D-09):
# `companions:read` gates the tool LIST, `companions:invoke` gates every CALL. Both ride in
# `RUNTIME_MCP_SCOPES`, so the key the platform mints for an Agent carries them.
#
# Every class maps to `companions:invoke` on purpose. The class-specific protection lives in the
# gates AFTER this one, and none of them moved: `create` and `mutate` still pass the policy gate
# and its approval path, and `destroy` stays hidden and refused until the tenant's destroy
# enablement row exists, then policy-gated on top. A per-class scope would only have required a
# vocabulary no mint path grants (research F5). Still a NEW vocabulary beside
# `_NativeToolSpec.scopes`, never a reuse of it: a native tool's scopes describe Clawdi resources,
# and a companion tool's describe what the agent may do to someone else's filesystem.
COMPANION_LIST_SCOPE = "companions:read"
COMPANION_TOOL_CLASS_SCOPES: dict[str, str] = {
    "read": "companions:invoke",
    "create": "companions:invoke",
    "mutate": "companions:invoke",
    "destroy": "companions:invoke",
}

# The classes the façade can actually execute. `tools/list` is filtered by this, so an agent is
# never shown a tool it would be refused for -- which is D-03's own reason for making the list
# dynamic. Plan 05-03 widened it to the registry's full vocabulary, because the thing that now
# decides whether a write may run is the POLICY below, not this set. `destroy` is in the set and is
# additionally gated on the tenant's enablement row, in both the list and the call (D-06).
#
# This set remains the fail-closed floor for a class the registry's vocabulary gains later: a fifth
# class added to `_TOOL_CLASSES` with no façade support must refuse rather than fall through to a
# policy default written for four.
_EXECUTABLE_TOOL_CLASSES = frozenset({"read", "create", "mutate", "destroy"})

# Which plan enabled each class. Named in the refusal below so a class this façade does NOT execute
# refuses with something a reader can act on rather than a flat "not supported", and kept accurate
# so the record stays trustworthy -- `create` read "05-02" until 05-03 corrected it, and a pointer
# that has been wrong once is a pointer nobody checks again.
# `backend/tests/test_companion_tools_list.py` asserts this covers the whole class vocabulary.
_CLASS_ENABLING_PLAN: dict[str, str] = {
    "read": "05-01",
    "create": "05-03",
    "mutate": "05-03",
    "destroy": "05-03",
}

# Same shape and same TTL as `_connector_tools_cache` in `app/routes/mcp_bridge.py`. Keyed by
# clerk id because that is the tenant identity the MCP endpoint already resolves.
COMPANION_TOOLS_CACHE_TTL_SECONDS = 60.0
_companion_tools_cache: dict[str, tuple[float, list[JsonObject]]] = {}

# The façade's own deadline, inside the CLI proxy's `MCP_FORWARD_TIMEOUT_MS = 30_000`. A call
# that outlives the proxy is a call whose answer is thrown away, and the agent sees a transport
# error instead of a refusal it could act on.
DISPATCH_DEADLINE_SECONDS = 25.0
DISPATCH_POLL_INTERVAL_SECONDS = 0.05

# The machine reason a call to a BLOCKED companion group is refused with (plan 06-24, D-30). It is
# in the agent-facing text and in the provenance row's `error_text`, for the reason
# `_policy_refusal_message` gives: the prose will be rewritten, the token is what a test, a log row
# and a dashboard correspond on. Re-exported by `companion_settings`, which owns the switch.
REASON_COMPANION_GROUP_DISABLED = "companion_group_disabled"


def _utc_now() -> datetime:
    """The wall clock the repeated-call breaker's window is measured on. Injectable for tests."""
    return datetime.now(UTC)


def reset_companion_tools_cache() -> None:
    """Drop every cached toolset. Tests only -- production lets the TTL do the work."""
    _companion_tools_cache.clear()


def reset_companion_tools_cache_for(clerk_id: str) -> None:
    """Drop ONE tenant's cached toolset, in this process.

    The lifecycle worker calls this the moment an install completes (APP-04, "in the same
    action"). It is an in-process reset: another process's cache for this tenant is not touched,
    which is why ``list_companion_tools`` also declines to cache a tenant's list while one of its
    installations is in flight -- the list after completion is read fresh in every process.
    """
    _companion_tools_cache.pop(clerk_id, None)


def is_companion_tool_name(name: str) -> bool:
    """Whether ``name`` is shaped like a companion tool name (D-04).

    Shape only. A well-shaped name that owns no registry row is still refused by
    ``call_companion_tool`` -- but it is refused BY THE FAÇADE rather than falling through to the
    Composio connector, which is the distinction T-5-05 turns on.
    """
    import re

    return re.fullmatch(COMPANION_TOOL_NAME_PATTERN, name) is not None


async def list_companion_tools(
    auth: AuthContext,
    db: AsyncSession,
    *,
    now: Callable[[], float] = time.monotonic,
) -> list[JsonObject]:
    """The companion tools THIS tenant's agent may see, behind a 60-second TTL.

    Dynamic per tenant (D-03): only a deployed companion's verbs appear, so an agent never sees a
    tool it cannot use. Failures are NOT cached -- a transient resolution error must not blank the
    toolset for a whole minute.

    The scope check comes FIRST, before the cache is consulted: the cache is keyed by tenant, not
    by credential, so a list warmed by an owner credential must never be served to a scoped key
    that lacks ``companions:read``. A refusal is an empty list, exactly as for connectors --
    `tools/list` never fails the MCP session over a scope.
    """
    try:
        require_auth_scopes(auth, COMPANION_LIST_SCOPE)
    except HTTPException:
        return []
    clerk_id = _cache_key(auth)
    moment = now()
    if clerk_id is not None:
        cached = _companion_tools_cache.get(clerk_id)
        if cached and cached[0] > moment:
            return cached[1]
    try:
        kinds = await deployed_companion_kinds(auth, db)
        # Resolved INSIDE the same per-tenant block as the deployed kinds, so the existing cache key
        # already covers it and no second cache is needed. A separate destroy cache would be a
        # second staleness bound over the same tenant, expiring at a different moment.
        destroy_enabled_keys = await destroy_enabled_companion_keys(db, user_id=auth.user_id)
        # The group block (plan 06-24), in the same block for the same reason: one cache key, one
        # staleness bound. `set_group_block` resets this tenant's entry when it commits, and the
        # call-time refusal is what holds in every other process until their entry expires.
        blocked_keys = await blocked_companion_keys(db, user_id=auth.user_id)
        # A tenant whose install is still queued or running is about to gain tools. The worker's
        # reset at completion only reaches ITS process, so the list is not cached until nothing
        # is in flight: whichever process serves the next tools/list reads it fresh (APP-04).
        in_flight = await _installation_in_flight(db, user_id=auth.user_id)
    except Exception:
        # Not cached, and not raised: `tools/list` degrading to the native set is recoverable,
        # while a 500 from the list call takes the whole MCP session down.
        logger.info("Companion tools unavailable for tenant")
        return []
    tools = [
        _tool_definition(name, entry)
        for name, entry in sorted(COMPANION_VERBS.items())
        if is_exposed_to_agent(
            name,
            entry,
            kinds=kinds,
            destroy_enabled_keys=destroy_enabled_keys,
            blocked_keys=blocked_keys,
        )
    ]
    if clerk_id is not None and not in_flight:
        _companion_tools_cache[clerk_id] = (
            moment + COMPANION_TOOLS_CACHE_TTL_SECONDS,
            tools,
        )
    return tools


async def _installation_in_flight(db: AsyncSession, *, user_id: UUID) -> bool:
    """Whether this tenant has a live installation that is queued or running."""
    found = await db.scalar(
        select(CompanionInstallation.id)
        .where(
            CompanionInstallation.user_id == user_id,
            CompanionInstallation.removed_at.is_(None),
            CompanionInstallation.status.in_(
                (COMPANION_INSTALLATION_STATUS_QUEUED, COMPANION_INSTALLATION_STATUS_RUNNING)
            ),
        )
        .limit(1)
    )
    return found is not None


async def call_companion_tool(
    name: str,
    arguments: JsonObject,
    *,
    auth: AuthContext,
    db: AsyncSession,
    deadline_seconds: float = DISPATCH_DEADLINE_SECONDS,
    poll_interval_seconds: float = DISPATCH_POLL_INTERVAL_SECONDS,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    session_id: str | None = None,
    count_targets: Callable[[CompanionVerbEntry, JsonObject], int] | None = None,
    utc_now: Callable[[], datetime] = _utc_now,
) -> JsonObject:
    """Run one companion tool call end to end, or return an ``isError`` refusal.

    ``now`` and ``sleep`` are injectable so the deadline can be asserted with a clock instead of
    by waiting 25 real seconds. A test that proves a timeout by taking the timeout is a test
    nobody runs twice, and a timeout nobody tests is a hang in production.

    ``session_id`` is the MCP Streamable HTTP session this call belongs to: the value
    ``app/routes/mcp_bridge.read_mcp_session_id`` reads off the ``Mcp-Session-Id`` request header,
    minted by that module at ``initialize`` and echoed by the client thereafter. Plan 05-10's
    Task 4 is where the shipped ``iforge mcp`` proxy acquires and echoes it, so the value is live
    on the canonical agent path rather than only in tests that set the header themselves.

    It is ``None`` when the client supplied no usable one -- a client that ran no ``initialize``,
    a client whose handshake produced nothing, a value outside the transport's bound, or a request
    from a surface that deliberately carries no session identity such as ``doctor.ts``'s
    ``checkMcp`` health probe. A null session falls back to a rolling one-hour budget window; see
    ``app/models/companion_session_budget.py`` for why that fallback is deliberate. It records no
    prompting-content reference at all -- see ``app/services/companion_provenance._ledger_key``,
    which has no representable shared bucket.

    ``count_targets`` resolves how many ``records`` a bulk-capable verb will touch. It is injectable
    because the real resolver is the READ PROBE, and the read probe is plan 05-05's work: the
    default below counts a single path as one record, which is correct for every argument shape the
    narrow input schema admits today. What matters now is that the debit is whatever this returns
    rather than a flat one call -- a thousand-target delete debits a thousand records (T-5-24), so
    when 05-05 wires the probe in, the accounting is already right.

    ``utc_now`` is the wall clock the repeated-call breaker measures its window on. It is separate
    from ``now`` because ``now`` is monotonic and the breaker's window is a timestamp held in
    PostgreSQL, shared by every worker.
    """
    resolve_targets = count_targets or _previewed_target_count_resolver(auth.user_id, name)
    companion_key, _, verb = name.partition("__")
    try:
        entry = companion_verb(name)
    except KeyError:
        # Recorded BEFORE anything knows what the verb was, because there is no registry row to
        # read a class from -- `unknown` is the honest value. Leaving this branch out of the log
        # would hide exactly the attempt a hallucinated or injected tool name produces (T-5-05),
        # which is one of the attempts a tenant owner most needs to see.
        unknown = f"{name} is not a companion tool this façade exposes"
        await _record_call(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            tool_class="unknown",
            arguments=arguments,
            decision=CallDecision.REFUSED_UNKNOWN_TOOL,
            session_id=session_id,
            error_text=unknown,
        )
        return _error(unknown)

    if entry.tool_class not in _EXECUTABLE_TOOL_CLASSES:
        plan = _CLASS_ENABLING_PLAN.get(entry.tool_class, "a later plan")
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=(
                f"{name} is class {entry.tool_class!r}, which this deployment does not enable "
                f"yet (plan {plan})"
            ),
            decision=CallDecision.REFUSED_POLICY,
            session_id=session_id,
        )

    scope = COMPANION_TOOL_CLASS_SCOPES[entry.tool_class]
    try:
        require_auth_scopes(auth, scope)
    except HTTPException as exc:
        denied = f"{name} requires the {scope} scope: {exc.detail}"
        await _record_call(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            arguments=arguments,
            decision=CallDecision.REFUSED_POLICY,
            session_id=session_id,
            error_text=denied,
        )
        return _error(denied)

    # The group block (plan 06-24, D-29/D-30), directly after the scope check and before anything
    # reads the arguments. Read from the table on EVERY call, never from the cached tool list: an
    # agent holding a list fetched before the block, or guessing a name, is refused here. Exact key
    # equality, so blocking one companion never blocks another whose key shares a prefix.
    if await _group_blocked(db, user_id=auth.user_id, companion_key=companion_key):
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=group_block_refusal_message(name),
            decision=CallDecision.REFUSED_GROUP_DISABLED,
            session_id=session_id,
        )

    # The kind's own argument contract (06-07). A kind with verb rows but no contract is refused
    # rather than validated against another kind's shape: every kind with rows owns a contract, and
    # `backend/tests/test_postiz_verbs.py` holds the two sets equal.
    contract = (
        argument_contract_for(companion_key) if companion_key in argument_contract_kinds() else None
    )
    invalid = (
        f"the {companion_key} companion declares no argument contract"
        if contract is None
        else contract.invalid(verb, entry, arguments)
    )
    if contract is None or invalid is not None:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments={},
            message=f"{name} rejected its arguments: {invalid}",
            decision=CallDecision.REFUSED_POLICY,
            session_id=session_id,
        )

    # The policy gate. Everything it reads is tenant state loaded from `auth.user_id`; nothing it
    # reads came from `arguments`, and `evaluate_policy` has no parameter that could carry it
    # (T-5-17, T-5-22). Placed BEFORE the mint below, which is D-10: a refused call never becomes a
    # signed pre-authorized call, so the runtime has nothing to be talked out of refusing.
    decision = evaluate_policy(
        entry.tool_class,
        policy_mode=await _policy_mode(
            db, user_id=auth.user_id, companion_key=companion_key, verb=verb
        ),
        destroy_enabled=await _destroy_enabled(
            db, user_id=auth.user_id, companion_key=companion_key
        ),
    )
    # `deny` -- including the destroy switch, which resolves to `deny` -- stops here, exactly as it
    # did before plan 05-06. `ask` does NOT: it now continues through the cap and rate gates to the
    # approval branch below, which is the seam 05-03 left for this plan. What has not moved is the
    # MINT: an `ask` call that presents no valid continuation never reaches it (D-10).
    dry_run = bool(arguments.get("dry_run"))
    if decision.mode != POLICY_MODE_ALLOW and decision.mode != POLICY_MODE_ASK:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_policy_refusal_message(name, decision),
            # The destroy switch resolves to `deny` with its own reason, and the log keeps the two
            # apart: "the account owner disabled destroy tools for this companion" and "the account
            # owner's policy for this tool is deny" send a reader to different settings.
            decision=(
                CallDecision.REFUSED_DESTROY_DISABLED
                if decision.reason == REASON_DESTROY_DISABLED
                else CallDecision.REFUSED_POLICY
            ),
            session_id=session_id,
        )

    # The tenant's monthly MONEY ceiling (OPS-06, D-20). A ledger of its own, never a fourth cap
    # unit, checked here: after the policy gate, so a deny keeps its more specific answer, and
    # before the caps and the mint, so a call refused at the ceiling is never signed (D-10). It
    # refuses the agent's CALL and nothing else. No unit, installation or line item is touched
    # (D-21). Everything it reads is tenant state by `auth.user_id`; `arguments` is not passed.
    spend = await check_spend_ceiling(db, user_id=auth.user_id)
    if not spend.allowed:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=spend_ceiling_refusal_message(name),
            decision=CallDecision.REFUSED_BUDGET_CEILING,
            session_id=session_id,
            dry_run=dry_run,
        )

    # Which of the tenant's Agents this call routes to. Resolved HERE, once, because the
    # repeated-call breaker below keys on the same canonical action hash approvals bind to, and
    # that hash names the Agent. `None` (nothing deployed) is still a valid key: a loop against a
    # companion that is not deployed is a loop, and it is refused as unreachable further down.
    environment_id = await _environment_with_companion(auth, db, companion_key=companion_key)

    # The repeated-call breaker (OPS-06, D-22): the SAME call, by canonical action hash and dry-run
    # flag, arriving more often than the breaker allows inside its window. It is checked after the
    # ceiling and before the caps and the mint, so a stopped loop is never signed (D-10), and it
    # needs no cost data, so it stops a runaway before the lagging ceiling can see it. The count is
    # committed at once, so it holds across workers and survives whatever this call does next.
    repeated = await check_repeated_call(
        db,
        user_id=auth.user_id,
        companion_key=companion_key,
        verb=verb,
        arguments=arguments,
        dry_run=dry_run,
        agent_id=environment_id,
        now=utc_now(),
    )
    await db.commit()
    if not repeated.allowed:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=repeated.describe(name),
            decision=CallDecision.REFUSED_REPEATED_CALL,
            session_id=session_id,
            environment_id=environment_id,
            dry_run=dry_run,
            repeat_count=repeated.count,
        )

    # The blast-radius gate, step one: how much may THIS call touch (TOOL-04). A SECOND limit, run
    # after the policy allowed the call and never inside its branch -- policy decides whether, caps
    # decide how much, and each must be able to refuse on its own.
    caps = await resolve_cap_vector(db, user_id=auth.user_id, companion_key=companion_key)
    # A dry run costs ONE `calls` unit and nothing else. The read probe is a call, so a call is
    # charged; nothing is written, so no `records` or `bytes` are. Charging a preview the unit of
    # the mutation it previews would make previewing a cost the agent learns to skip, which is the
    # opposite of what TOOL-05 is for.
    cap_unit = CAP_UNIT_CALLS if dry_run else entry.cap_unit
    requested = (
        1
        if dry_run
        else resolve_requested_units(entry, arguments, caps=caps, count_targets=resolve_targets)
    )
    per_call = check_per_call_cap(
        unit=cap_unit, requested=requested, tool_class=entry.tool_class, caps=caps
    )
    if not per_call.allowed:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_cap_refusal_message(name, per_call),
            decision=CallDecision.REFUSED_CAP,
            session_id=session_id,
            cap_unit=cap_unit,
            cap_consumed=0,
            cap_remaining_after=per_call.remaining,
            dry_run=dry_run,
        )

    # Step two: how FAST may calls arrive (TOOL-09). A wait inside the safe budget slows the
    # agent's task; only a wait that would outlive the proxy budget refuses, and it refuses with an
    # explicit retry-after so the agent self-corrects rather than seeing a transport timeout.
    slot = acquire_companion_slot(user_id=auth.user_id, companion_key=companion_key)
    if slot.action == SLOT_WAIT:
        # Logged so a slowed call is distinguishable from a hung one. That distinction is what
        # keeps "hitting the limit slows the task" from reading as "stopped responding".
        logger.info("Companion call waiting on the rate limit for %d ms", slot.wait_ms)
        await sleep(slot.wait_ms / 1000)
        slot = acquire_companion_slot(user_id=auth.user_id, companion_key=companion_key)
    if not slot.proceed:
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_rate_limit_refusal_message(name, slot),
            decision=CallDecision.REFUSED_RATELIMIT,
            session_id=session_id,
            dry_run=dry_run,
        )

    if environment_id is None:
        # The row the DISPATCH queue cannot hold. `record_refusal` returns `None` here because
        # `companion_dispatch_calls.environment_id` is NOT NULL and there is no environment to
        # name, so an activity log sourced from that table would show nothing at all for an
        # attempt the tenant most needs explained. The provenance row's environment is nullable
        # precisely so this attempt is recorded rather than inferred from an absence.
        unreachable = f"no runtime of yours has a {companion_key} companion deployed"
        await _record_call(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            arguments=arguments,
            decision=CallDecision.RUNTIME_UNREACHABLE,
            session_id=session_id,
            dry_run=dry_run,
            error_text=unreachable,
        )
        return _error(unreachable)

    # The dry-run branch replaces the MINT, and that placement is the point: a previewed call never
    # becomes a signed pre-authorized MUTATING call, so there is no envelope for an executor to run
    # if it were restarted between the preview and the approval. What is minted instead is the read
    # probe, which is a `read`-class verb of the same kind by the parser's own rule (D-13).
    #
    # A preview runs under an `ask` decision, and that is deliberate rather than an oversight. The
    # human this gate asks has to be able to SEE what they are approving, and the count the preview
    # resolves is the blast radius the approval binds -- refusing to preview until the call is
    # approved would make both undecidable. What a preview must NOT become is a way around the READ
    # PROBE's own policy, so the probe verb's policy is evaluated below on its own terms.
    if dry_run:
        probe_refusal = await _probe_policy_refusal(
            db, auth=auth, name=name, entry=entry, companion_key=companion_key
        )
        if probe_refusal is not None:
            return await _refuse(
                db,
                auth=auth,
                companion_key=companion_key,
                verb=verb,
                entry=entry,
                arguments=arguments,
                message=probe_refusal,
                decision=CallDecision.REFUSED_POLICY,
                session_id=session_id,
                environment_id=environment_id,
                dry_run=True,
            )
        return await _run_dry_run(
            db,
            auth=auth,
            name=name,
            entry=entry,
            companion_key=companion_key,
            arguments=arguments,
            environment_id=environment_id,
            caps=caps,
            session_id=session_id,
            deadline_seconds=deadline_seconds,
            poll_interval_seconds=poll_interval_seconds,
            now=now,
            sleep=sleep,
        )

    # The approval gate (D-08). Like the policy gate it sits UPSTREAM of the mint, and for a
    # sharper version of the same reason: a call awaiting a human's answer must not be holding a
    # signed pre-authorized call while it waits. `approved` is the row whose budget this call is
    # about to spend, or `None` when the decision was `allow` and no approval is involved at all.
    approved: CompanionApproval | None = None
    if decision.mode == POLICY_MODE_ASK:
        outcome = await _resolve_approval(
            db,
            auth=auth,
            name=name,
            entry=entry,
            companion_key=companion_key,
            verb=verb,
            arguments=arguments,
            environment_id=environment_id,
            decision=decision,
            target_count=_approved_target_count(
                name, entry, arguments, count_targets=resolve_targets
            ),
            session_id=session_id,
        )
        if isinstance(outcome, dict):
            return outcome
        approved = outcome

    # The refusal this call is the continuation of, so the chain survives the round trip through a
    # human. Without it the activity log shows two unrelated rows and "what happened to the call I
    # approved" has no answer in it.
    parent_call_id = (
        None
        if approved is None
        else await _pending_approval_call_id(db, user_id=auth.user_id, approval_id=approved.id)
    )
    # Captured as plain values HERE, before the commit below. A committed session may expire every
    # instance the caller is holding, and reading an attribute off `approved` after that point
    # lazy-loads outside the greenlet -- the same hazard the SAVEPOINT comment below records,
    # arriving through the recording path instead of the refusal one.
    approval_id = None if approved is None else approved.id
    approved_by_user_id = None if approved is None else approved.decided_by_user_id

    minted = mint_dispatch_call(
        companion_key=companion_key,
        verb=verb,
        method=entry.method,
        path_template=entry.path_template,
        # Control parameters stripped: the continuation is a bearer token and `dry_run` is an
        # instruction to this façade. Neither is a companion request parameter, and the envelope
        # is persisted on the dispatch row.
        arguments=contract.dispatch_arguments(arguments),
    )
    # Step three, and the placement is the whole point: the session debit runs in the SAME
    # transaction that mints and enqueues the signed call. A refused debit therefore unwinds the
    # enqueue with it, so no pre-authorized call can outlive the budget decision that was supposed
    # to stop it (T-5-25).
    #
    # A SAVEPOINT rather than a whole-session rollback. Rolling the outer transaction back would
    # also EXPIRE every instance the caller is holding -- including the `User` behind
    # `auth.user_id`, which `_refuse` needs on the very next line. Narrowing the unwind to the two
    # statements that need unwinding is both more precise and the only version that does not make
    # the refusal path depend on a lazy reload succeeding.
    window = budget_window(datetime.now(UTC))
    nested = await db.begin_nested()
    try:
        await enqueue_call(
            db,
            user_id=auth.user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            minted=minted,
        )
    except CompanionDispatchArgumentError as exc:
        # A REFUSAL, not a crash. The queued row is re-minted into the executor's envelope at
        # claim time, so arguments that do not survive storage unchanged would dispatch a call
        # the approval digest never covered (CR-01). Letting this propagate would answer the
        # agent with a 500 and -- worse here -- leave no provenance row, breaking the property
        # 05-01 established and 05-07 renders: every ATTEMPT writes exactly one row, refusals
        # included. The log exists to answer "why not".
        await nested.rollback()
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=f"{name} was refused: {exc}",
            decision=CallDecision.REFUSED_POLICY,
            session_id=session_id,
            environment_id=environment_id,
            approval_id=approval_id,
            parent_call_id=parent_call_id,
        )
    debit = await apply_session_debit(
        db,
        user_id=auth.user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        session_id=session_id,
        unit=entry.cap_unit,
        requested=requested,
        caps=caps,
        window=window,
    )
    if not debit.allowed:
        await nested.rollback()
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_cap_refusal_message(name, debit),
            decision=CallDecision.REFUSED_CAP,
            session_id=session_id,
            environment_id=environment_id,
            cap_unit=entry.cap_unit,
            cap_consumed=0,
            cap_remaining_after=debit.remaining,
            approval_id=approval_id,
            parent_call_id=parent_call_id,
        )
    if approved is not None and not await consume_approval(db, approval_id=approved.id):
        # The approval's remaining budget is spent inside THIS transaction -- the same boundary
        # plan 05-04 chose for the session debit, for the same double-spend reason. Two concurrent
        # redemptions both read a budget of one and both reached here; the conditional UPDATE
        # inside `consume_approval` matches for exactly one of them, and the loser unwinds the mint
        # with it rather than proceeding on an authorization it did not obtain.
        await nested.rollback()
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_approval_refusal_message(name, REASON_APPROVAL_BUDGET_SPENT, decision),
            decision=CallDecision.REFUSED_APPROVAL,
            session_id=session_id,
            environment_id=environment_id,
            approval_id=approval_id,
            parent_call_id=parent_call_id,
        )
    await nested.commit()
    # Committed BEFORE the wait, deliberately: the executor polls on an independent connection
    # and cannot see an uncommitted row. Without this the wait would always time out.
    await db.commit()

    row = await _await_terminal(
        db,
        call_jti=minted.jti,
        deadline_seconds=deadline_seconds,
        poll_interval_seconds=poll_interval_seconds,
        now=now,
        sleep=sleep,
    )
    settle = _bytes_settlement(
        db,
        user_id=auth.user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        session_id=session_id,
        entry=entry,
        reserved=requested,
        window=window,
    )

    # Everything below this line records the SAME attempt, so the helper is bound once with the
    # facts that do not change and each terminal branch supplies only its own outcome.
    async def record_outcome(
        outcome: CallDecision, *, consumed: int, remaining: int | None, error: str | None
    ) -> None:
        await _record_call(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            arguments=arguments,
            decision=outcome,
            environment_id=environment_id,
            session_id=session_id,
            cap_unit=entry.cap_unit,
            cap_consumed=consumed,
            cap_remaining_after=remaining,
            call_jti=minted.jti,
            approval_id=approval_id,
            approved_by_user_id=approved_by_user_id,
            parent_call_id=parent_call_id,
            error_text=error,
        )

    if row is None:
        await _expire(db, call_jti=minted.jti)
        await settle(0)
        unreachable = (
            f"the runtime hosting your {companion_key} companion is not reachable; "
            "no executor claimed the call before the deadline"
        )
        await record_outcome(
            CallDecision.RUNTIME_UNREACHABLE,
            consumed=0,
            remaining=debit.remaining,
            error=unreachable,
        )
        return _error(unreachable)
    if row.status == "failed":
        await settle(0)
        failure = f"{name} failed: {row.error_text or 'no detail reported'}"
        # `allowed`, not a refusal. The façade let this call through and the runtime ran it; what
        # came back is a fact about the companion, and it is in `error_text`. Recording it as a
        # refusal would make "we stopped this" and "they stopped this" the same value.
        await record_outcome(
            CallDecision.ALLOWED, consumed=0, remaining=debit.remaining, error=failure
        )
        return _error(failure)

    result = row.result or {}
    status = result.get("status")
    body = result.get("body")
    if isinstance(status, int) and status >= 400:
        await settle(0)
        refused_by_companion = f"{name} returned HTTP {status} from the companion"
        await record_outcome(
            CallDecision.ALLOWED,
            consumed=0,
            remaining=debit.remaining,
            error=refused_by_companion,
        )
        return _error(refused_by_companion)

    body_text = body if isinstance(body, str) else ""
    truncation: CapDecision | None = None
    # What the call ACTUALLY spent. `requested` is a reservation for the `bytes` unit, and the
    # unused remainder is released below -- so recording the reservation would tell a reader this
    # call cost the whole per-call ceiling when it cost a few hundred bytes.
    consumed = requested
    if entry.cap_unit == CAP_UNIT_BYTES:
        actual = len(body_text.encode("utf-8"))
        clipped = check_per_call_cap(
            unit=CAP_UNIT_BYTES, requested=actual, tool_class=entry.tool_class, caps=caps
        )
        if clipped.truncate_to is not None:
            body_text = _truncate_utf8(body_text, clipped.truncate_to)
            truncation = clipped
        consumed = min(actual, caps.per_call_bytes)
        await settle(consumed)

    wrapped = wrap_untrusted(
        body_text,
        content_id=row.call_jti,
        source=f"{companion_key}:{verb}",
        # The TIGHTER of the envelope's own context bound and this tenant's per-call bytes ceiling.
        # The envelope discloses whichever one it applied, which is what keeps the notice a
        # statement about what the agent received rather than about a constant in a module.
        limit=min(UNTRUSTED_BODY_LIMIT, caps.per_call_bytes),
    )
    content: list[JsonValue] = []
    if truncation is not None:
        # BEFORE the untrusted envelope, never after it. A disclosure placed after companion bytes
        # is a disclosure companion bytes could forge; placed before, it is unambiguously the
        # façade speaking. Silent truncation would be a lie about what the agent received.
        content.append(
            {
                "type": "text",
                "text": (
                    f"Notice: this response was truncated at your per-call {CAP_UNIT_BYTES} cap. "
                    f"{truncation.describe()}"
                ),
            }
        )
    content.append({"type": "text", "text": wrapped})
    await record_outcome(
        CallDecision.ALLOWED, consumed=consumed, remaining=debit.remaining, error=None
    )
    # The identifier the agent just saw in the envelope becomes the prompting-content reference a
    # LATER call in this session points at. Recorded AFTER this call's own row, so a call is never
    # attributed to its own output -- which would be a correlation that is not merely best-effort
    # but impossible.
    record_returned_content(user_id=auth.user_id, session_id=session_id, content_id=row.call_jti)
    return {"content": content}


# ---------------------------------------------------------------------------
# The blast-radius cap vector's durable half (TOOL-04).
#
# These live HERE rather than in `app/core/companion_caps.py` because that module is pure by
# contract -- it holds the arithmetic and knows nothing about a session, a table or a transaction.
# They are public rather than underscored because the double-spend they defend against is only
# provable from two INDEPENDENT connections, which means a test has to be able to call them
# directly; a private helper reachable only through `call_companion_tool` would make the concurrency
# case unwritable without also standing up two executors.
# ---------------------------------------------------------------------------


def budget_window(moment: datetime) -> datetime:
    """The rolling window a null-session budget belongs to, truncated down.

    Truncated rather than "an hour from first use" so two concurrent first calls land on the same
    row: a window anchored to whenever a caller happened to arrive would give each racing caller
    its own budget, which is the cap resetting under exactly the load it exists to bound.
    """
    window = SESSION_BUDGET_WINDOW_SECONDS
    seconds = int(moment.timestamp()) // window * window
    return datetime.fromtimestamp(seconds, tz=UTC)


def session_budget_cutoff(moment: datetime) -> datetime:
    """The oldest ``window_started_at`` a SESSION budget row can carry and still be LIVE.

    A row stamped at or after this instant is live, and its counters are judged as SPENT. A row
    stamped strictly before it is STALE: the next debit's upsert zeroes its three counters and
    re-stamps it rather than refusing against it, so a conversation longer than the lifetime gets a
    fresh budget instead of a permanent wall (GC2-03).

    The comparison is STRICT so the boundary has exactly one meaning, and both sides of it are
    asserted in ``backend/tests/test_companion_blast_radius.py``. Public for the same reason
    ``budget_window`` is: a test has to be able to name the boundary without reaching into a
    private helper, and a boundary nameable only from the code that implements it is a boundary
    asserted against itself.

    Only the SESSION ledger has a use for this. A window row's key already contains
    ``window_started_at``, so a new hour is a new row and a stale window row is unreachable by
    construction.
    """
    return moment - timedelta(seconds=SESSION_BUDGET_LIFETIME_SECONDS)


async def resolve_cap_vector(db: AsyncSession, *, user_id: UUID, companion_key: str) -> CapVector:
    """This tenant's cap vector for one companion, or the shipped defaults.

    Absent means ``DEFAULT_CAPS``, which is the fail-SAFE direction: a failure to insert leaves the
    shipped ceiling in force rather than removing the ceiling. Read at CALL time from a table the
    agent has no write path to, which is the structural half of "an agent cannot raise the cap".
    """
    row = (
        await db.execute(
            select(CompanionCapPolicy)
            .where(
                CompanionCapPolicy.user_id == user_id,
                CompanionCapPolicy.companion_key == companion_key,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    return DEFAULT_CAPS if row is None else row.cap_vector()


def resolve_requested_units(
    entry: CompanionVerbEntry,
    arguments: JsonObject,
    *,
    caps: CapVector,
    count_targets: Callable[[CompanionVerbEntry, JsonObject], int],
) -> int:
    """How much of its declared unit this call will consume.

    ``calls`` is always one. ``records`` is the RESOLVED TARGET COUNT, because
    ``DELETE /api/resources`` is bulk-capable and debiting one call for a thousand deletions would
    make the session budget a count of requests -- which bounds nothing an agent cares about
    (T-5-24).

    ``bytes`` is a RESERVATION of the whole per-call ceiling, because the size of a response is not
    knowable before the response exists. The unused remainder is released after the call settles
    (see ``_bytes_settlement``); reserving optimistically instead would let two concurrent reads
    each believe the budget had room for them. A zero ceiling reserves one byte rather than zero, so
    "a cap of zero refuses everything" stays true for this unit too -- a zero-byte request against a
    zero-byte ceiling would otherwise compare as within cap.
    """
    if entry.cap_unit == CAP_UNIT_CALLS:
        return 1
    if entry.cap_unit == CAP_UNIT_RECORDS:
        return max(0, int(count_targets(entry, arguments)))
    return caps.per_call_bytes or 1


async def apply_session_debit(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID,
    companion_key: str,
    session_id: str | None,
    unit: str,
    requested: int,
    caps: CapVector,
    window: datetime | None = None,
) -> CapDecision:
    """Spend ``requested`` of this tenant's budget for ``unit``, on TWO ledgers, or refuse.

    **There are two rows, and only one of them is addressable by the agent.**

    ======== ====================================================================== ===============
    Ledger   Key                                                                    Debited by
    ======== ====================================================================== ===============
    Window   ``(user_id, environment_id, companion_key, window_started_at)``,        every call
             ``session_id IS NULL`` -- resets hourly
    Session  ``(user_id, session_id, companion_key)`` -- resets when the            calls carrying
             budget it opened is older than the session lifetime                    a session
    ======== ====================================================================== ===============

    Both ceilings are ``caps.per_session(unit)``, and they have DIFFERENT PERIODS. The window's is
    one hour, and it is part of that row's key. The session's is
    ``SESSION_BUDGET_LIFETIME_SECONDS``, and it is NOT part of that row's key -- it is measured
    from the row's own ``window_started_at``, so at most ``caps.per_session(unit)`` is spendable by
    one conversation within any one lifetime. They stay genuinely independent rather than one
    shadowing the other: the session gate fires on a runaway conversation and the window gate fires
    on N invented ones.

    **Why the session ledger has a period at all.** Without one it never reset, never expired and
    was deleted only by tenant purge, while carrying the same ceiling as the hourly window. A
    long-lived ``iforge mcp`` proxy is one process holding one minted id for hours to days, so a
    legitimate agent working steadily was refused on its ``per_session_calls + 1``-th call and on
    every call after it for the life of that process, with no in-product remedy: waiting did not
    help, because the row that refused it carried no time term at all (GC2-03). A blast-radius cap
    exists to bound a runaway, not to end a working day.

    **The bound lives in the UPSERT, not in the selector.** ``_debit_ledger`` takes
    ``reset_if_opened_before`` and turns the session ``ON CONFLICT DO NOTHING`` into a
    ``DO UPDATE`` guarded on the target row's own stamp. Putting a freshness term in
    ``_budget_selector`` instead would NOT work: that conflict target has no time term, so a stale
    row still collides and is left where it is, the filtered selector then addresses nothing, the
    conditional ``UPDATE`` matches no row and the ``consumed is None`` branch fires -- the same
    wall, now reported as ``session_budget_unresolved``, which is the word for "this gate is
    broken". That is WR-02's conflict-target/selector mismatch class. It would also break refunds:
    ``release_session_reservation`` builds its session selector from the same helper, so a
    freshness term there would strand a ``bytes`` refund on a row that aged between debit and
    settlement.

    **A reset cannot be a cap bypass.** Within one window a session's counter can never exceed the
    window's counter for the same unit, because every session debit is preceded by a window debit
    for the same amount and a window refusal short-circuits before the session ledger is touched.
    Even a doubly-reset session row therefore cannot let a tenant spend more in an hour than the
    window ledger allows, and the window ledger is untouched by this mechanism in key, ceiling,
    conflict target and upsert mode.

    **An agent CAN wait out the lifetime.** It could already wait out the hour. Time-bounded is
    what this cap has always been; the second ledger is now honest about it rather than pretending
    a conversation is bounded forever. What no agent can do is SHORTEN either period: both are read
    from the server clock and from a server-written column.

    **The one interaction, stated as what it does rather than as harmless.** A ``bytes``
    reservation whose settlement arrives after its row was reset is subtracted from a FRESH
    counter, not from the one it was reserved against. ``GREATEST(..., 0)`` in ``_release_ledger``
    clamps only the LOWER bound -- it stops a counter going negative, it does not make the refund
    correct -- so a stale settlement can return up to its own size of a fresh session budget,
    erasing that much genuinely-new-period session consumption. Two real bounds sit beside it. It
    is not a cap bypass: the window ledger is untouched by both the reset and this refund, so the
    hour is still bounded at ``caps.per_session(unit)`` whatever the session counter reads. And it
    is bounded in frequency by what reaching it takes -- a settlement landing a whole lifetime
    after its own debit means a response that never returned for four windows -- so it is a rare
    tail rather than a path an agent can drive.

    **The invariant: a key the agent chooses can only ADD a ceiling, never remove one.**
    ``environment_id`` is resolved from the ``AuthContext`` by ``_environment_with_companion`` and
    never from arguments; ``window_started_at`` is ``budget_window(datetime.now(UTC))``, truncated
    server-side. The agent supplies neither, so the window ledger is unaddressable by anything it
    can put on the wire. A call that also carries a session id spends its session budget IN
    ADDITION, which can only make the answer stricter.

    **Why that is written down.** A client-supplied ``Mcp-Session-Id`` was briefly the WHOLE key of
    the only ledger this function debited (plan 05-10 threaded the header down; the session branch
    of ``_budget_selector`` is a bare string equality with no window term). Since the upsert creates
    a zeroed row for any unseen triple, a fresh header value was a fresh budget -- an agent raised
    its own per-session cap by changing a string it chose, and because the value was non-null the
    rolling-hour ceiling that had actually been enforcing TOOL-04 was unreachable for every client
    that sent the header (GC-01).

    **Verifying the header would not have closed that.** Nothing meters ``initialize``, so an agent
    wanting a fresh budget calls it again and receives a genuinely minted, genuinely signed id: the
    cost of a reset goes from "change a string" to "one extra POST", which is not a cost. Nor would
    putting the window into the session key have helped -- N invented ids inside one window are
    still N rows, each created zeroed by the upsert. The only thing that closes it is a ceiling
    addressed by columns the agent does not supply, which is what the window ledger is.

    **Lock order is window, then session, everywhere.** ``release_session_reservation`` takes the
    same pair in the same order. Two code paths locking one pair in opposite orders deadlock under
    concurrency, and these two are the only places in the codebase that touch both rows.

    The spend is a CONDITIONAL ``UPDATE`` -- ``SET consumed = consumed + n WHERE consumed + n <=
    budget`` -- and never a read followed by a write. Under ``READ COMMITTED`` PostgreSQL re-checks
    that ``WHERE`` clause after it acquires the row lock, so a second caller that queued behind the
    first one's uncommitted spend sees the NEW remainder and matches zero rows. A read-then-write
    would re-use the remainder it read before the lock and let both callers through, which is the
    classic double-spend and is exactly what the ``committed_db`` case in
    ``backend/tests/test_companion_blast_radius.py`` exists to catch.

    No commit. The caller's transaction owns the write, which is what makes the debit and
    ``mint_dispatch_call`` land together or not at all.

    **A zero-row UPDATE has two causes and they are NOT interchangeable.** "The budget is spent"
    leaves a row behind whose counter can be re-read and judged; "the selector addressed no row"
    leaves nothing to judge. The second happens when the upsert's ON CONFLICT target and
    ``_budget_selector``'s predicate disagree -- the insert is a no-op against somebody else's row
    and the update then matches nothing of this tenant's. Coercing that absent counter to ``0``
    treated an unaddressable row as an EMPTY one and returned allowed, which is a fail-open default
    in the one gate whose entire purpose is to fail closed (WR-02, T-05-11-02). It refuses with
    ``REASON_SESSION_BUDGET_UNRESOLVED`` now, and the activity log records the two events apart.

    Plan 05-10 made this live rather than latent: ``session_id`` is a real, client-supplied MCP
    session id now, so the session-keyed branch is exercised and its conflict target is reachable
    by a second tenant presenting the same opaque string. The tenant column added to
    ``uq_companion_session_budget`` closes that particular disagreement; this branch is what makes
    any remaining one a refusal rather than a free call. The discrimination holds on BOTH ledgers
    now -- ``_debit_ledger`` is the extracted body and is called twice, unchanged.
    """
    moment = window or budget_window(datetime.now(UTC))
    budget = caps.per_session(unit)
    column = getattr(CompanionSessionBudget, f"{unit}_consumed")

    def _row_values(row_session: str | None) -> dict[str, Any]:
        return {
            "id": uuid_module.uuid4(),
            "user_id": user_id,
            "environment_id": environment_id,
            "companion_key": companion_key,
            "session_id": row_session,
            "window_started_at": moment,
            "calls_consumed": 0,
            "records_consumed": 0,
            "bytes_consumed": 0,
        }

    # The WINDOW ledger, unconditionally and FIRST. Its three conflict columns are
    # `uq_companion_window_budget` exactly, and none of them is a value the agent supplies.
    window_selector = _budget_selector(
        user_id=user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        session_id=None,
        window=moment,
    )
    window_decision = await _debit_ledger(
        db,
        values=_row_values(None),
        index_elements=["environment_id", "companion_key", "window_started_at"],
        index_where="session_id IS NULL",
        selector=window_selector,
        column=column,
        requested=requested,
        budget=budget,
        unit=unit,
        caps=caps,
    )
    if not window_decision.allowed or session_id is None:
        # Nothing else has been written, so a refusal here needs no compensation.
        return window_decision

    # The SESSION ledger, second, and only for a call that carries one. These three columns are
    # `uq_companion_session_budget` exactly. A conflict target that does not name an existing index
    # is a runtime error, and one that names a NARROWER index than the selector reads with is worse
    # than an error: it silently upserts nothing and sends every debit through the unaddressable
    # branch (WR-02). No window term here, deliberately -- see the table above.
    session_selector = _budget_selector(
        user_id=user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        session_id=session_id,
        window=moment,
    )
    session_decision = await _debit_ledger(
        db,
        values=_row_values(session_id),
        index_elements=["user_id", "session_id", "companion_key"],
        index_where="session_id IS NOT NULL",
        selector=session_selector,
        column=column,
        requested=requested,
        budget=budget,
        unit=unit,
        caps=caps,
        # The ONE place the session ledger's time bound lives. Nothing on the window call above:
        # that row's key already contains the hour, so a reset there would be code that can never
        # run.
        reset_if_opened_before=session_budget_cutoff(moment),
    )
    if not session_decision.allowed:
        # Reachable: a conversation crossing an hour boundary can have a spent session budget while
        # the fresh window still has room. The window increment above must not survive a call that
        # was ultimately refused, or a refusal would silently cost the tenant a unit of the hour.
        await _release_ledger(db, selector=window_selector, column=column, refund=requested)
        return session_decision
    return CapDecision(
        allowed=True,
        unit=unit,
        requested=requested,
        # The SMALLER remainder is the honest one: it is how much is actually left before the next
        # refusal, and it is what `_cap_refusal_message` and `cap_remaining_after` report.
        remaining=min(window_decision.remaining, session_decision.remaining),
        reason=REASON_WITHIN_CAP,
    )


async def release_session_reservation(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID,
    companion_key: str,
    session_id: str | None,
    unit: str,
    refund: int,
    window: datetime,
) -> None:
    """Give back the part of a reservation the call did not use.

    Only the ``bytes`` unit reserves, and only because a response's size is unknowable before it
    arrives. Without this release, a one-byte read would cost a whole per-call ceiling and a session
    budget of two hundred mebibytes would be twenty reads -- a limit tight enough to break real
    tasks for a reason no user could discover. ``GREATEST(..., 0)`` keeps the counter non-negative
    even if a refund is ever computed wrongly, because a negative counter is a budget with more room
    than the tenant granted.

    **Both ledgers, in the debit's lock order: window, then session.** ``apply_session_debit``
    reserves from the window row on every call and additionally from the session row when the call
    carries a session, so the refund has to reach both. Releasing only one would make a one-byte
    read cost a whole per-call ceiling on the other -- the exact failure this release exists to
    prevent, reintroduced on the ledger nobody looked at. Taking the pair in the SAME order the
    debit took it is what keeps the two paths from deadlocking against each other.
    """
    if refund <= 0:
        return
    column = getattr(CompanionSessionBudget, f"{unit}_consumed")
    await _release_ledger(
        db,
        selector=_budget_selector(
            user_id=user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            session_id=None,
            window=window,
        ),
        column=column,
        refund=refund,
    )
    if session_id is not None:
        await _release_ledger(
            db,
            selector=_budget_selector(
                user_id=user_id,
                environment_id=environment_id,
                companion_key=companion_key,
                session_id=session_id,
                window=window,
            ),
            column=column,
            refund=refund,
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


async def _debit_ledger(
    db: AsyncSession,
    *,
    values: dict[str, Any],
    index_elements: list[str],
    index_where: str,
    selector: list[Any],
    column: Any,
    requested: int,
    budget: int,
    unit: str,
    caps: CapVector,
    reset_if_opened_before: datetime | None = None,
) -> CapDecision:
    """One ledger's conditional spend: upsert a zeroed row, then debit it or explain why not.

    This is ``apply_session_debit``'s former body, moved verbatim so that BOTH ledgers get the same
    arithmetic and -- more importantly -- the same three-way discrimination. WR-02's whole point is
    that "the budget is spent" and "the selector addressed no row" are different events; an
    extraction that blurred them on the second ledger would reintroduce the fail-open on the half
    of the mechanism nobody had looked at yet.

    ``index_where`` is the partial index's predicate as text rather than a clause object, because
    the two callers name two different partial indexes and the string is what makes the pairing of
    target and predicate readable at the call site next to the ``index_elements`` it belongs with.

    **``reset_if_opened_before`` turns the upsert into a RESET, and never into an increment.**
    Absent -- the window ledger's case -- the upsert is ``ON CONFLICT DO NOTHING`` with exactly the
    conflict target it has always had, so that ledger's behaviour is byte-for-byte what it was.
    Present, it becomes ``ON CONFLICT DO UPDATE`` on the SAME ``index_elements`` and the SAME
    ``index_where``, zeroing the three counters and re-stamping ``window_started_at`` to the moment
    this call already carries in ``values``, guarded by a ``WHERE`` on the TARGET row that is true
    only when that row's stamp is strictly earlier than the argument. It happens BEFORE this call's
    own debit, so a stale row is zeroed and then spent by one: the counter afterwards reads this
    call's request, never the aged value plus it. A datetime rather than text, deliberately -- the
    parameter beside it is a raw-SQL string and this one must not join it.

    **The concurrency guarantee it rests on.** Under ``READ COMMITTED`` a second caller conflicting
    on a row the first has already updated blocks on that row's lock, and PostgreSQL re-evaluates
    the ``DO UPDATE`` predicate against the LATEST version of the row once the lock is released.
    The first caller's reset refreshed ``window_started_at``, so the second caller's staleness test
    is false and no second reset lands -- one stale row, one reset, whatever the arrival order.
    ``backend/tests/test_companion_blast_radius.py`` asserts that on two independent connections
    rather than trusting this paragraph.

    ``updated_at`` is named explicitly in the ``SET`` because SQLAlchemy does not apply a column's
    Python-side ``onupdate`` inside an ``ON CONFLICT DO UPDATE``; without it a reset would be
    invisible in the row it happened to.
    """
    insert = pg_insert(CompanionSessionBudget).values(**values)
    if reset_if_opened_before is None:
        upsert = insert.on_conflict_do_nothing(
            index_elements=index_elements, index_where=text(index_where)
        )
    else:
        upsert = insert.on_conflict_do_update(
            index_elements=index_elements,
            index_where=text(index_where),
            set_={
                "calls_consumed": 0,
                "records_consumed": 0,
                "bytes_consumed": 0,
                "window_started_at": values["window_started_at"],
                "updated_at": func.now(),
            },
            where=CompanionSessionBudget.window_started_at < reset_if_opened_before,
        )
    await db.execute(upsert)
    spent = (
        await db.execute(
            update(CompanionSessionBudget)
            .where(*selector, column + requested <= budget)
            .values({column: column + requested})
            .returning(column)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if spent is not None:
        return CapDecision(
            allowed=True,
            unit=unit,
            requested=requested,
            remaining=max(0, budget - int(spent)),
            reason=REASON_WITHIN_CAP,
        )
    # Re-read under a fresh statement snapshot. The update matched nothing for one of THREE
    # reasons, and the branch below is what keeps the third from being judged as the first two.
    consumed = await db.scalar(select(column).where(*selector))
    if consumed is None:
        # No row at all. After the `pg_insert` above, that can only mean the insert's conflict
        # target and the selector disagree -- a row exists that satisfies the ON CONFLICT predicate
        # but not the WHERE this tenant reads with -- so the durable half has nothing to increment.
        # `int(None or 0)` used to make this a budget of zero consumed, which `debit_session_budget`
        # answers ALLOWED for any request inside the ceiling, and the caller went on to mint and
        # dispatch with no counter touched: an unbounded session budget for as long as the
        # condition held. Refuse instead, under a reason of its own.
        return CapDecision(
            allowed=False,
            unit=unit,
            requested=requested,
            remaining=0,
            reason=REASON_SESSION_BUDGET_UNRESOLVED,
        )
    # A real integer: the budget is spent, or this one call is larger than the whole budget, and
    # the pure function is what turns that remainder into the refusal an agent reads.
    return debit_session_budget(unit=unit, requested=requested, consumed=int(consumed), caps=caps)


async def _release_ledger(
    db: AsyncSession, *, selector: list[Any], column: Any, refund: int
) -> None:
    """Give ``refund`` back to ONE ledger row. No commit: the caller owns the transaction.

    Used for two different purposes with identical mechanics -- settling an unused ``bytes``
    reservation, and compensating the window increment when the session ledger then refuses -- and
    ``GREATEST(..., 0)`` keeps the counter non-negative for both, because a negative counter is a
    budget with more room than the tenant granted.
    """
    await db.execute(
        update(CompanionSessionBudget)
        .where(*selector)
        .values({column: func.greatest(column - refund, 0)})
        .execution_options(synchronize_session=False)
    )


def _budget_selector(
    *,
    user_id: UUID,
    environment_id: UUID,
    companion_key: str,
    session_id: str | None,
    window: datetime,
):
    """The WHERE terms addressing exactly one budget row, in whichever of the two modes applies.

    ``user_id`` is in both, though neither unique index needs it: a selector that matched a row
    belonging to somebody else would debit the wrong tenant's budget, and the cost of the extra
    equality is nothing next to that.
    """
    terms = [
        CompanionSessionBudget.user_id == user_id,
        CompanionSessionBudget.companion_key == companion_key,
    ]
    if session_id is None:
        terms.append(CompanionSessionBudget.session_id.is_(None))
        terms.append(CompanionSessionBudget.environment_id == environment_id)
        terms.append(CompanionSessionBudget.window_started_at == window)
    else:
        terms.append(CompanionSessionBudget.session_id == session_id)
    return terms


def _previewed_target_count_resolver(
    user_id: UUID, name: str
) -> Callable[[CompanionVerbEntry, JsonObject], int]:
    """The default ``records`` resolver: what a dry run COUNTED, or the single-path floor.

    A dry run is the only thing in this system that has actually read the target and counted what
    a call would touch, so its count is the one worth carrying into the real call's cap check --
    which is what stops an approval granted against a two-entry preview being redeemed for a
    two-thousand-entry delete debited as one record (T-5-34).

    **Composed with ``max``, never as a replacement.** The preview ledger is per-process and
    TTL-bounded, so a miss is ordinary: a second worker, an expired entry, a different path. On a
    miss this returns exactly what it returned before plan 05-05, so the ledger can only ever RAISE
    a debit. A resolver that replaced the count would turn every cache miss into a budget bypass,
    which is precisely the failure the cap exists to prevent.

    What remains open, and is recorded rather than implied: a bulk call made with NO preceding dry
    run still debits a single record. Closing that needs the probe on every mutating call's own
    path, at the cost of an extra runtime round trip per call.
    """

    companion_key, _, verb = name.partition("__")

    def resolve(entry: CompanionVerbEntry, arguments: JsonObject) -> int:
        contract = argument_contract_for(companion_key)
        counted = contract.floor_target_count(verb, entry, arguments)
        target = contract.target_key(verb, arguments)
        if target is None:
            return counted
        return max(
            counted,
            previewed_target_count(user_id=user_id, tool_name=name, target_key=target),
        )

    return resolve


def _approved_target_count(
    name: str,
    entry: CompanionVerbEntry,
    arguments: JsonObject,
    *,
    count_targets: Callable[[CompanionVerbEntry, JsonObject], int],
) -> int | None:
    """How many targets an approval for this call binds, or ``None`` when nobody has COUNTED.

    Only a ``records`` verb has a count worth binding. ``calls`` is always one by definition and
    ``bytes`` is a RESERVATION of the per-call ceiling rather than a measurement of the target, so
    binding either would bind a constant and the re-check at redemption would be decoration.

    **``None`` when the resolver did not get past the single-path floor, and that is the important
    half.** The resolver is the one the cap check uses: 05-05's preview ledger composed with
    ``_single_path_target_count`` by ``max``. On a ledger miss it returns the floor -- one -- which
    is an honest DEBIT but a dishonest CLAIM. ``filebrowser__delete_path`` on a directory is
    bulk-capable, so binding "1" to an approval for it would put the number 1 in front of a person
    about to authorize an unbounded delete, and would then "match" at redemption because the
    redemption resolves the same floor. A count nobody counted is not a bound, and the plan's own
    prohibition is that an approval must not display a mode the server does not actually gate on:
    the truthful answer is that this call's blast radius is undetermined.

    The consequence is visible rather than silent. ``resolved_target_count`` is ``null`` on the
    dashboard for an un-previewed bulk call, and the drift check is live exactly when a dry run
    actually resolved something -- which is the case T-5-39 names.
    """
    if entry.cap_unit != CAP_UNIT_RECORDS:
        return None
    resolved = max(0, int(count_targets(entry, arguments)))
    companion_key, _, verb = name.partition("__")
    floor = argument_contract_for(companion_key).floor_target_count(verb, entry, arguments)
    if resolved <= floor:
        return None
    return resolved


async def _probe_policy_refusal(
    db: AsyncSession,
    *,
    auth: AuthContext,
    name: str,
    entry: CompanionVerbEntry,
    companion_key: str,
) -> str | None:
    """Refuse a preview whose READ PROBE the tenant's own policy would refuse, or ``None``.

    A dry run mints the probe and reads the target with it. Without this, a tenant who put their
    ``read`` verb behind ``ask`` or ``deny`` would find that a preview of a WRITE verb read the
    same directory anyway -- so the preview would be a way around a policy decision the tenant had
    already made. The probe is a real read and its own policy governs it.

    A no-op for every tenant who has written no row against the probe verb, which is all of them
    today: ``read`` defaults to ``allow``.
    """
    probe_tool = read_probe_tool_name(name, entry)
    if probe_tool is None:
        return None
    try:
        probe_entry = companion_verb(probe_tool)
    except KeyError:
        return None
    probe_verb = probe_tool.partition("__")[2]
    decision = evaluate_policy(
        probe_entry.tool_class,
        policy_mode=await _policy_mode(
            db, user_id=auth.user_id, companion_key=companion_key, verb=probe_verb
        ),
        # The probe is a `read`-class verb by the parser's own rule, so the destroy branch cannot
        # fire; passing `False` states that rather than loading a switch that cannot apply.
        destroy_enabled=False,
    )
    if decision.mode == POLICY_MODE_ALLOW:
        return None
    return (
        f"{name} could not be previewed, because previewing it reads the target through "
        f"{probe_tool} and {_policy_refusal_message(probe_tool, decision)}"
    )


async def _resolve_approval(
    db: AsyncSession,
    *,
    auth: AuthContext,
    name: str,
    entry: CompanionVerbEntry,
    companion_key: str,
    verb: str,
    arguments: JsonObject,
    environment_id: UUID,
    decision: PolicyDecision,
    target_count: int | None,
    session_id: str | None = None,
) -> CompanionApproval | JsonObject:
    """The approval gate: the row this call may spend, or the refusal to return instead.

    Two shapes come back and the caller branches on the type, because they are genuinely different
    outcomes rather than two values of one: a ``CompanionApproval`` means "continue to the mint and
    spend this", and a ``JsonObject`` is already the MCP result to hand the agent.

    **A continuation presented** is redeemed against the arguments it arrives with. Every refusal
    carries its own machine reason from ``companion_dispatch``'s vocabulary.

    **No continuation presented** is the ask: upsert the row for this canonical action, and answer
    according to the state that row is actually in.

    * undecided -> ``approval_required``, carrying a freshly minted continuation.
    * approved and unspent -> ``approval_ready``, carrying one too. There is no channel from the
      control plane to a running agent (``05-RESEARCH.md`` Pitfall 3 verified there is none in
      either direction), so asking again is the only way an agent learns it was approved, and the
      two reasons have to be distinguishable or it cannot tell.
    * spent, denied or otherwise terminal -> the reason, and NO continuation. Handing over a token
      that cannot be redeemed would invite a retry loop against a wall.
    """
    presented = arguments.get("approval")
    action_sha256 = canonical_action_hash(
        companion_key=companion_key,
        verb=verb,
        arguments=arguments,
        tenant_id=auth.user_id,
        agent_id=environment_id,
    )

    if isinstance(presented, str) and presented:
        redemption = await redeem_continuation(
            db,
            token=presented,
            companion_key=companion_key,
            verb=verb,
            arguments=arguments,
            tenant_id=auth.user_id,
            agent_id=environment_id,
            target_count=target_count,
        )
        if redemption.allowed and redemption.approval is not None:
            return redemption.approval
        return await _refuse(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            entry=entry,
            arguments=arguments,
            message=_approval_refusal_message(name, redemption.reason, decision),
            decision=CallDecision.REFUSED_APPROVAL,
            session_id=session_id,
            environment_id=environment_id,
        )

    approval = await upsert_approval(
        db,
        user_id=auth.user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        verb=verb,
        action_sha256=action_sha256,
        arguments=arguments,
        target_count=target_count,
    )
    await db.commit()
    status = approval_status_at(approval, datetime.now(UTC))
    if status == STATUS_PREPARED and approval.remaining_budget > 0:
        reason, continuation = (
            REASON_APPROVAL_READY,
            mint_continuation(action_sha256=action_sha256, approval_id=approval.id),
        )
    elif status == STATUS_ISSUED:
        reason, continuation = (
            REASON_APPROVAL_REQUIRED,
            mint_continuation(action_sha256=action_sha256, approval_id=approval.id),
        )
    elif status == STATUS_DENIED:
        reason, continuation = REASON_APPROVAL_DENIED, None
    elif status == STATUS_EXPIRED:
        reason, continuation = REASON_APPROVAL_EXPIRED, None
    else:
        reason, continuation = REASON_APPROVAL_BUDGET_SPENT, None
    # Two strings from ONE helper, and the difference between them is the bearer token. The agent
    # needs the continuation to retry; `error_text` is a queryable column served by
    # `GET /v1/companions/activity`, so the recorded half is built with `continuation=None` rather
    # than parsed back out of the returned half (WR-01, T-05-11-01). Both carry the same two
    # machine reasons, so nothing a reader of the activity log acts on is lost.
    return await _refuse(
        db,
        auth=auth,
        companion_key=companion_key,
        verb=verb,
        entry=entry,
        arguments=arguments,
        message=_approval_refusal_message(
            name,
            reason,
            decision,
            continuation=None if continuation is None else continuation.token,
        ),
        logged_message=_approval_refusal_message(name, reason, decision),
        # A call HELD for a human is not the same event as one refused outright, and the activity
        # log has to distinguish them: one is waiting for the reader, the other is finished.
        decision=(
            CallDecision.PENDING_APPROVAL
            if reason in {REASON_APPROVAL_REQUIRED, REASON_APPROVAL_READY}
            else CallDecision.REFUSED_APPROVAL
        ),
        session_id=session_id,
        environment_id=environment_id,
        # Carried on the row so the retry that follows a decision can find this one and link back
        # to it. Without the approval identifier here the chain has no join to make.
        approval_id=approval.id,
    )


def _approval_refusal_message(
    name: str,
    reason: str,
    decision: PolicyDecision,
    *,
    continuation: str | None = None,
) -> str:
    """The refusal an agent reads when the approval gate stopped the call.

    Both reason tokens are in the text, and that is deliberate rather than redundant. The POLICY
    reason says why approval was needed at all -- ``class_default`` ("nobody has decided about this
    tool") and ``explicit_policy_row`` ("you chose ask") are different situations for a tenant
    reading the activity log. The APPROVAL reason says what happened to the request. Collapsing
    them into one would lose whichever half the writer happened to care about that day.

    The continuation rides a machine-readable trailer line rather than being embedded in prose. An
    agent must be able to find it without parsing a sentence, and the sentences here are a human
    surface that will be rewritten.
    """
    detail = _APPROVAL_REFUSAL_DETAIL.get(reason, "this call is not authorized")
    lines = [f"{name} was not run [{decision.reason}] [{reason}]: {detail}"]
    if continuation is not None:
        lines.append(
            "Retry this exact call with the value below as the 'approval' argument once it has "
            "been approved. It authorizes this action only."
        )
        lines.append(f"{APPROVAL_CONTINUATION_PREFIX}{continuation}")
    return "\n".join(lines)


# One sentence per machine reason. Written for a MODEL to act on: each says what happened and what
# the next useful move is, and none of them names a way to self-approve -- the decision surface is
# deliberately outside every channel an agent can reach (D-09), so describing it would only
# encourage a retry loop against a wall.
_APPROVAL_REFUSAL_DETAIL: dict[str, str] = {
    REASON_APPROVAL_REQUIRED: (
        "the account owner has not decided about this call yet. Nothing was run and nothing was "
        "sent to the companion. Their answer arrives out of band; do not retry in a loop"
    ),
    REASON_APPROVAL_READY: (
        "this call has been approved and is waiting to be redeemed. Retry it with the "
        "continuation below"
    ),
    REASON_APPROVAL_DENIED: (
        "the account owner declined this call. Asking again for the same action returns this same "
        "answer; treat it as final rather than as a transient failure"
    ),
    REASON_APPROVAL_EXPIRED: (
        "the approval for this call expired before it was redeemed. Ask again -- retrying with "
        "the old continuation cannot succeed"
    ),
    REASON_APPROVAL_BUDGET_SPENT: (
        "this approval has already been used. One approval authorizes one execution, and asking "
        "again for the identical action returns this same spent approval rather than a new one"
    ),
    REASON_APPROVAL_NOT_FOUND: (
        "no approval exists for this call. Ask for one by retrying without the 'approval' argument"
    ),
    REASON_ACTION_HASH_MISMATCH: (
        "the approval you presented was granted for a different call. An approval covers the exact "
        "arguments it was granted against; ask again for the call you are actually making"
    ),
    REASON_TARGET_COUNT_MISMATCH: (
        "this call would touch a different number of targets than the approval was granted "
        "against. Preview it again and ask for a fresh approval against what it now resolves"
    ),
    REASON_CONTINUATION_INVALID: (
        "the 'approval' value is not a continuation this control plane issued, or it has expired. "
        "Ask again by retrying without it"
    ),
}


async def _run_dry_run(
    db: AsyncSession,
    *,
    auth: AuthContext,
    name: str,
    entry: CompanionVerbEntry,
    companion_key: str,
    arguments: JsonObject,
    environment_id: UUID,
    caps: CapVector,
    session_id: str | None,
    deadline_seconds: float,
    poll_interval_seconds: float,
    now: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
) -> JsonObject:
    """Preview one mutating call by reading its target, and return the diff, or an error.

    The probe is dispatched through the SAME queue, the same signed envelope and the same executor
    an ordinary read uses. Nothing here is a special read path, which is what makes "the preview
    reads the target" a property of the shipped dispatch channel rather than of a shortcut only
    previews take.
    """
    verb = name.partition("__")[2]

    async def record_preview_outcome(
        outcome: CallDecision,
        *,
        consumed: int | None,
        remaining: int | None,
        error: str | None,
        diff: JsonObject | None = None,
    ) -> None:
        """One row for the PREVIEW attempt, bound once with the facts that do not change.

        A preview is an attempt like any other and gets exactly one row, `dry_run` true. What it
        must NOT get is the decision of the mutation it previewed: nothing was written, and a log
        that recorded a preview as an allowed delete would be worse than one that recorded nothing.
        """
        await _record_call(
            db,
            auth=auth,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            arguments=arguments,
            decision=outcome,
            environment_id=environment_id,
            session_id=session_id,
            # A preview costs one `calls` unit whatever the previewed verb's own unit is -- the
            # read probe is a call, and nothing is written.
            cap_unit=CAP_UNIT_CALLS,
            cap_consumed=consumed,
            cap_remaining_after=remaining,
            dry_run=True,
            # Only a COMPLETED preview carries one. A preview that errored has no diff to show and
            # must not record an empty one: null is "nobody looked", `[]` is "nothing would change".
            dry_run_diff=diff,
            error_text=error,
        )

    probe_tool = read_probe_tool_name(name, entry)
    if probe_tool is None:
        no_probe = f"{name} declares no read probe, so there is nothing to preview it with"
        await record_preview_outcome(
            CallDecision.REFUSED_POLICY, consumed=0, remaining=None, error=no_probe
        )
        return _error(no_probe)

    # What the probe's own outcome was, so the preview's row can say WHY it did not complete.
    # A dict rather than a `nonlocal`, because the closure below is defined before the value
    # exists and rebinding a name across that boundary is the shape that silently reads a stale
    # one.
    probe_state: dict[str, object] = {}

    async def execute_probe(tool: str, probe_arguments: JsonObject) -> ProbeResponse:
        probe_entry = companion_verb(tool)
        probe_verb = tool.partition("__")[2]
        minted = mint_dispatch_call(
            companion_key=companion_key,
            verb=probe_verb,
            method=probe_entry.method,
            path_template=probe_entry.path_template,
            arguments=probe_arguments,
        )
        # Same invariant as the real path, applied to the probe: the debit lands in the SAME
        # transaction that mints and enqueues, so a refused debit unwinds the enqueue with it and
        # no pre-authorized call outlives the budget decision meant to stop it (T-5-25).
        window = budget_window(datetime.now(UTC))
        nested = await db.begin_nested()
        await enqueue_call(
            db,
            user_id=auth.user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            verb=probe_verb,
            tool_class=probe_entry.tool_class,
            minted=minted,
        )
        debit = await apply_session_debit(
            db,
            user_id=auth.user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            session_id=session_id,
            unit=CAP_UNIT_CALLS,
            requested=1,
            caps=caps,
            window=window,
        )
        if not debit.allowed:
            await nested.rollback()
            probe_state["decision"] = CallDecision.REFUSED_CAP
            probe_state["remaining"] = debit.remaining
            return ProbeResponse(status=None, body="", error=_cap_refusal_message(name, debit))
        probe_state["remaining"] = debit.remaining
        await nested.commit()
        await db.commit()

        row = await _await_terminal(
            db,
            call_jti=minted.jti,
            deadline_seconds=deadline_seconds,
            poll_interval_seconds=poll_interval_seconds,
            now=now,
            sleep=sleep,
        )
        if row is None:
            await _expire(db, call_jti=minted.jti)
            probe_state["decision"] = CallDecision.RUNTIME_UNREACHABLE
            return ProbeResponse(
                status=None,
                body="",
                error=(
                    f"the runtime hosting your {companion_key} companion is not reachable; no "
                    "executor claimed the read probe before the deadline"
                ),
            )
        if row.status == "failed":
            probe_state["decision"] = CallDecision.RUNTIME_UNREACHABLE
            return ProbeResponse(status=None, body="", error=row.error_text or "no detail reported")
        result = row.result or {}
        status = result.get("status")
        body = result.get("body")
        return ProbeResponse(
            status=status if isinstance(status, int) else None,
            body=body if isinstance(body, str) else "",
        )

    probe_remaining = probe_state.get("remaining")
    probe_debit = 0 if probe_state.get("decision") is CallDecision.REFUSED_CAP else 1
    outcome = await compute_dry_run(name, entry, arguments, execute_probe=execute_probe)
    if outcome.is_error:
        not_previewed = f"{name} could not be previewed: {outcome.message}"
        # `ProbeResponse.error` means the probe never produced an HTTP answer at all: a gate
        # refused it, or the runtime never answered. `refused_policy` is the fallback for the
        # remaining case -- the façade declining to preview under its own rules -- and the reason
        # is verbatim in `error_text` either way.
        await record_preview_outcome(
            probe_state.get("decision", CallDecision.REFUSED_POLICY),  # pyright: ignore[reportArgumentType]
            consumed=probe_debit,
            remaining=probe_remaining,  # pyright: ignore[reportArgumentType]
            error=not_previewed,
        )
        return _error(not_previewed)

    structured = outcome.structured()
    problem = validate_dry_run_output(structured)
    if problem is not None:
        # An invalid diff is an ERROR, never a value returned anyway. A preview an agent reads as
        # truth has to be a shape the declared schema vouches for, or the schema is decoration.
        logger.warning("A dry-run diff failed its own declared output schema: %s", problem)
        invalid_diff = f"{name} produced a preview that failed its own output schema: {problem}"
        await record_preview_outcome(
            CallDecision.REFUSED_POLICY,
            consumed=probe_debit,
            remaining=probe_remaining,  # pyright: ignore[reportArgumentType]
            error=invalid_diff,
        )
        return _error(invalid_diff)

    # Keyed on the kind's canonical target (06-07): the path for filebrowser, exactly as before,
    # and the UTC day plus the sorted channel ids for postiz__create_post.
    preview_key = argument_contract_for(companion_key).target_key(verb, arguments)
    if preview_key is not None:
        record_preview(
            user_id=auth.user_id,
            tool_name=name,
            target_key=preview_key,
            target_count=outcome.target_count,
        )
    await record_preview_outcome(
        CallDecision.DRY_RUN_COMPLETED,
        consumed=probe_debit,
        remaining=probe_remaining,  # pyright: ignore[reportArgumentType]
        error=None,
        # The SAME object returned below, after it passed `validate_dry_run_output`. Persisting the
        # validated payload rather than re-deriving one is what makes the stored row and the agent's
        # copy the same artifact -- 05-09 renders this, and a diff that disagreed with what the
        # agent acted on would be worse than no diff at all.
        diff=structured,
    )
    # The serialized diff rides the text channel beside the structured one, which is what the MCP
    # specification asks of a tool declaring an `outputSchema`: a client that does not read
    # structured content still sees the preview rather than an empty result.
    return {
        "content": [{"type": "text", "text": json.dumps(structured, indent=2, sort_keys=True)}],
        "structuredContent": structured,
    }


def _bytes_settlement(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID,
    companion_key: str,
    session_id: str | None,
    entry: CompanionVerbEntry,
    reserved: int,
    window: datetime,
) -> Callable[[int], Awaitable[None]]:
    """A closure that releases the unused part of a ``bytes`` reservation, or does nothing.

    Only a ``read`` settles. For a write, the bytes consumed are the REQUEST payload rather than
    the response, and this façade cannot see one yet -- the input schema admits no content
    argument. Refunding a write down to its response size would therefore hand back budget the call
    actually spent, so the reservation stands until a later plan gives a write a size to measure.
    """

    async def settle(actual: int) -> None:
        if entry.cap_unit != CAP_UNIT_BYTES or entry.tool_class != "read":
            return
        await release_session_reservation(
            db,
            user_id=user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            session_id=session_id,
            unit=CAP_UNIT_BYTES,
            refund=reserved - max(0, actual),
            window=window,
        )

    return settle


def _truncate_utf8(text_value: str, limit_bytes: int) -> str:
    """Clip ``text_value`` to at most ``limit_bytes`` UTF-8 bytes, never mid-character.

    Decoded with ``errors='ignore'`` so a multi-byte character straddling the boundary is dropped
    whole rather than emitted as a replacement glyph the agent would read as content.
    """
    encoded = text_value.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text_value
    return encoded[:limit_bytes].decode("utf-8", errors="ignore")


def _cap_refusal_message(name: str, decision: CapDecision) -> str:
    """The refusal an agent reads when a cap stopped the call, carrying the numbers verbatim.

    Same shape and same reasoning as ``_policy_refusal_message``: the machine reason token, the
    unit, the requested amount and the remaining amount are all in the text, because a refusal that
    said only "over cap" would satisfy TOOL-04's mechanical half and fail its user-facing one. A
    stopped call has to be VISIBLE, not silently absorbed.
    """
    return (
        f"{name} was refused by a blast-radius cap {decision.describe()}: the ceiling is a setting "
        "on your account, not a tool argument -- this call cannot raise it, and nothing you send "
        "as an argument can"
    )


def _rate_limit_refusal_message(name: str, slot: RateLimitSlot) -> str:
    """The refusal an agent reads when the wait would have outlived the request's own budget."""
    return (
        f"{name} was refused {slot.describe()}: the wait needed is longer than this request can "
        "hold open, so retry rather than treating this as a failure"
    )


def _cache_key(auth: AuthContext) -> str | None:
    try:
        return require_clerk_id(auth)
    except HTTPException:
        return None


def is_exposed_to_agent(
    name: str,
    entry: CompanionVerbEntry,
    *,
    kinds: frozenset[str],
    destroy_enabled_keys: frozenset[str],
    blocked_keys: frozenset[str],
) -> bool:
    """Whether this tenant's agent may SEE this verb.

    Three conditions, and the third is the first half of D-06: the companion is deployed, the class
    is one this façade executes, and a ``destroy``-class verb additionally requires the tenant's
    enablement row for that companion. Since 06-07 the kind must also own an argument contract,
    because a verb with no input schema cannot be shown. Since 06-24 the tenant must not have
    blocked the companion's group -- the list half of D-29, whose call half is the refusal in
    ``call_companion_tool``. ``blocked_keys`` is required rather than defaulted, so no caller can
    compute visibility while forgetting the block. The complement is asserted by name in
    ``backend/tests/test_companion_tools_list.py`` so a verb that vanishes for an unrelated reason
    cannot hide behind a documented filter.
    """
    companion_key = name.split("__", 1)[0]
    if companion_key not in kinds:
        return False
    # Set membership is exact equality: blocking `postiz` leaves a kind named `postizx` visible.
    if companion_key in blocked_keys:
        return False
    # A kind with no argument contract has no input schema to show, and every call to it is
    # refused, so it is not listed either (06-07).
    if companion_key not in argument_contract_kinds():
        return False
    if entry.tool_class not in _EXECUTABLE_TOOL_CLASSES:
        return False
    return entry.tool_class != "destroy" or companion_key in destroy_enabled_keys


def _policy_refusal_message(name: str, decision: PolicyDecision) -> str:
    """The refusal an agent reads, carrying the machine reason verbatim.

    The reason token is in the text on purpose. The prose around it is a human surface that will be
    rewritten; the token is what a test, a dispatch row and a future approvals UI correspond on, and
    a refusal whose cause can only be inferred from wording is a refusal nothing can assert against.
    """
    if decision.reason == REASON_DESTROY_DISABLED:
        detail = (
            "destroy tools are disabled for this companion until the account owner enables them. "
            "Enabling is a per-companion switch outside any channel you can reach -- it is not a "
            "tool argument and not something this call can request"
        )
    elif decision.mode == "ask":
        # Reachable only through `_probe_policy_refusal` now. Plan 05-06 moved the ask decision for
        # the CALLED verb to `_resolve_approval`, which returns a continuation instead of this;
        # what is left here is a verb the tenant put behind approval being reached indirectly, as a
        # dry run's read probe, where there is nothing to hand a continuation for.
        detail = (
            "the account owner's policy for this tool is 'ask', and this call reached it "
            "indirectly rather than as the tool the agent named, so there is no approval to "
            "request for it"
        )
    else:
        detail = "the account owner's policy for this tool is 'deny'"
    return f"{name} was refused by policy [{decision.reason}]: {detail}"


def group_block_refusal_message(name: str) -> str:
    """The refusal an agent reads when it calls a verb of a BLOCKED companion group (D-30).

    It says the block is an account setting, because the next thing an agent told "no" tends to
    try is asking again with different arguments. Nothing in ``arguments`` reaches the block check.
    """
    return (
        f"{name} was refused [{REASON_COMPANION_GROUP_DISABLED}]: the account owner blocked this "
        "companion's tools. Blocking is an account setting and cannot be changed from a tool "
        "argument."
    )


def _tool_definition(name: str, entry: CompanionVerbEntry) -> JsonObject:
    companion_key, _, verb = name.partition("__")
    definition: JsonObject = {
        "name": name,
        "description": (
            f"{verb.replace('_', ' ')} on your {companion_key} companion, through the Clawdi "
            f"façade. Class: {entry.tool_class}. The companion's credential never leaves the "
            "host it runs on."
        ),
        "inputSchema": argument_contract_for(companion_key).input_schema(verb, entry),
        # Advisory client-side UX derived mechanically from the class (D-07). NEVER the gate:
        # the class check above is.
        "annotations": derive_annotations(entry.tool_class),
    }
    if entry.tool_class in MUTATING_TOOL_CLASSES:
        # Declared on the same verbs that accept `dry_run`, and on no others. Attaching it is what
        # makes preview accuracy a schema-checkable property rather than a claim: the façade
        # validates the diff against this before returning it.
        definition["outputSchema"] = DRY_RUN_OUTPUT_SCHEMA
    return definition


async def deployed_companion_environments(
    auth: AuthContext, db: AsyncSession, *, environment_id: UUID | None = None
) -> dict[str, list[UUID]]:
    """Each companion kind this tenant has deployed, and the live Agents it is deployed on.

    Two sources, merged: kinds the provisioner declared in an Agent's hosted runtime state, and
    kinds the tenant installed and whose install COMPLETED (plan 06-11, APP-04). An installation
    that is still queued or running is not deployed yet -- its companion is only callable once the
    runtime has reported it ready and the job completed -- and a removed one no longer is. An
    installation in an UPDATE stays deployed throughout (plan 06-18): its install completed, and
    its previous digest serves until the swap and again after a failed update is rolled back.

    Agents are ordered oldest first, so a caller that picks the first Agent routes two calls in
    one session to the same runtime rather than alternating. ``environment_id`` narrows the answer
    to one Agent (an environment-bound key's own). The tenant predicate comes from the auth
    context and is first in both queries; there is no parameter through which another tenant's
    Agents could arrive.
    """
    hosted = (
        select(AgentEnvironment.id, AgentEnvironment.created_at, HostedRuntimeState.companions)
        .join(HostedRuntimeState, HostedRuntimeState.environment_id == AgentEnvironment.id)
        .where(
            AgentEnvironment.user_id == auth.user_id,
            AgentEnvironment.archived_at.is_(None),
        )
    )
    installed = (
        select(
            AgentEnvironment.id, AgentEnvironment.created_at, CompanionInstallation.companion_key
        )
        .join(AgentEnvironment, AgentEnvironment.id == CompanionInstallation.environment_id)
        .where(
            CompanionInstallation.user_id == auth.user_id,
            AgentEnvironment.user_id == auth.user_id,
            AgentEnvironment.archived_at.is_(None),
            serving_installation_filter(),
            CompanionInstallation.removed_at.is_(None),
        )
    )
    if environment_id is not None:
        hosted = hosted.where(AgentEnvironment.id == environment_id)
        installed = installed.where(AgentEnvironment.id == environment_id)

    placements: dict[str, set[tuple[datetime, UUID]]] = {}
    for agent_id, created_at, companions in await db.execute(hosted):
        if isinstance(companions, dict):
            for key in companions:
                if isinstance(key, str):
                    placements.setdefault(key, set()).add((created_at, agent_id))
    for agent_id, created_at, companion_key in await db.execute(installed):
        placements.setdefault(companion_key, set()).add((created_at, agent_id))
    return {key: [agent_id for _, agent_id in sorted(agents)] for key, agents in placements.items()}


async def deployed_companion_kinds(auth: AuthContext, db: AsyncSession) -> frozenset[str]:
    """The companion kinds this tenant actually has deployed, across its live Agents."""
    return frozenset(await deployed_companion_environments(auth, db))


async def destroy_enabled_companion_keys(db: AsyncSession, *, user_id: UUID) -> frozenset[str]:
    """Every companion key this tenant has turned destroy tools on for.

    Row present = enabled. Scoped by ``user_id`` alone: destroy enablement is a tenant-level switch
    covering the whole ``destroy`` class for one companion, never a per-Agent or per-tool one.
    """
    rows = await db.execute(
        select(CompanionDestroyEnablement.companion_key).where(
            CompanionDestroyEnablement.user_id == user_id
        )
    )
    return frozenset(key for (key,) in rows)


async def blocked_companion_keys(db: AsyncSession, *, user_id: UUID) -> frozenset[str]:
    """Every companion key whose tool group this tenant has blocked (plan 06-24, APP-05).

    Row present = blocked. Scoped by ``user_id`` alone, like destroy enablement: a block covers the
    companion's whole group for the tenant, never one Agent or one verb. Defined HERE, beside the
    predicate that consumes it, and re-exported by ``companion_settings`` -- that module imports the
    façade, so the reverse import would be circular, and "blocked" must mean one thing to both.

    A key with no registered verbs may be blocked; it hides nothing and suppresses no other kind.
    """
    rows = await db.execute(
        select(CompanionGroupBlock.companion_key).where(CompanionGroupBlock.user_id == user_id)
    )
    return frozenset(key for (key,) in rows)


async def _group_blocked(db: AsyncSession, *, user_id: UUID, companion_key: str) -> bool:
    """Whether this tenant has blocked this companion's tool group, read at CALL time (D-29).

    Re-read on every call rather than taken from the cached tool list, for the reason
    ``_destroy_enabled`` gives: the list's TTL must stay a staleness bound, not an exploit window.
    """
    found = await db.scalar(
        select(CompanionGroupBlock.id).where(
            CompanionGroupBlock.user_id == user_id,
            CompanionGroupBlock.companion_key == companion_key,
        )
    )
    return found is not None


async def _destroy_enabled(db: AsyncSession, *, user_id: UUID, companion_key: str) -> bool:
    """Whether this tenant has enabled destroy tools for this companion, read at CALL time.

    Re-read on every call rather than taken from the cached tool list. That is what makes the
    60-second list TTL a staleness bound rather than an exploit window: a tenant who revokes
    enablement is protected on the very next call, even while a stale snapshot still names the verb
    (T-5-19).
    """
    found = await db.scalar(
        select(CompanionDestroyEnablement.id).where(
            CompanionDestroyEnablement.user_id == user_id,
            CompanionDestroyEnablement.companion_key == companion_key,
        )
    )
    return found is not None


async def _policy_mode(
    db: AsyncSession, *, user_id: UUID, companion_key: str, verb: str
) -> str | None:
    """This tenant's active policy mode for one verb, or ``None`` when no row exists.

    ``None`` is NOT "allow" and not an error -- it means "fall through to the class default", which
    is ``ask`` for everything that can change something. The unique partial index makes at most one
    active row possible, so this is a lookup rather than a resolution between competing rows.
    """
    return await db.scalar(
        select(CompanionToolPolicy.mode).where(
            CompanionToolPolicy.user_id == user_id,
            CompanionToolPolicy.companion_key == companion_key,
            CompanionToolPolicy.verb == verb,
            CompanionToolPolicy.revoked_at.is_(None),
        )
    )


async def _environment_with_companion(
    auth: AuthContext, db: AsyncSession, *, companion_key: str
) -> UUID | None:
    """The tenant's environment that has ``companion_key`` deployed, resolved from AuthContext.

    The tenant is ALWAYS re-derived here and never read from an argument (T-5-08). When the API
    key is bound to one environment, that binding wins; otherwise the oldest matching Agent is
    chosen, so two calls in one session route to the same runtime rather than alternating. An
    installed kind resolves to its installation's Agent (plan 06-11), through the same helper the
    tool list uses, so "listed" and "routable" cannot disagree.
    """
    bound = auth.api_key.environment_id if auth.api_key is not None else None
    placements = await deployed_companion_environments(auth, db, environment_id=bound)
    agents = placements.get(companion_key)
    return agents[0] if agents else None


async def _await_terminal(
    db: AsyncSession,
    *,
    call_jti: str,
    deadline_seconds: float,
    poll_interval_seconds: float,
    now: Callable[[], float],
    sleep: Callable[[float], Awaitable[None]],
):
    """Poll the dispatch row until it goes terminal, or return ``None`` at the deadline.

    Polling rather than listening because there is nothing to listen to: the executor writes its
    result through an ordinary HTTP request on its own connection. Under PostgreSQL's default
    READ COMMITTED each statement takes a fresh snapshot, so a re-read inside this transaction
    does see the committed result -- but the ORM would keep handing back its identity-mapped
    instance, which is why ``await_call_result`` forces ``populate_existing``.
    """
    started = now()
    while True:
        row = await await_call_result(db, call_jti=call_jti)
        if row is not None and row.status in {"completed", "failed"}:
            return row
        if now() - started >= deadline_seconds:
            return None
        await sleep(poll_interval_seconds)
        # End the read transaction between polls so a session configured for a stricter
        # isolation level than READ COMMITTED still observes the executor's commit.
        await db.rollback()


async def _expire(db: AsyncSession, *, call_jti: str) -> None:
    row = await await_call_result(db, call_jti=call_jti)
    if row is not None and row.status not in {"completed", "failed"}:
        row.status = "expired"
        row.error_text = "no executor claimed the call before the deadline"
        db.add(row)
        await db.commit()


async def _refuse(
    db: AsyncSession,
    *,
    auth: AuthContext,
    companion_key: str,
    verb: str,
    entry: CompanionVerbEntry,
    arguments: JsonObject,
    message: str,
    logged_message: str | None = None,
    decision: CallDecision,
    session_id: str | None = None,
    environment_id: UUID | None = None,
    cap_unit: str | None = None,
    cap_consumed: int | None = None,
    cap_remaining_after: int | None = None,
    dry_run: bool = False,
    approval_id: UUID | None = None,
    parent_call_id: UUID | None = None,
    repeat_count: int | None = None,
) -> JsonObject:
    """Return an ``isError`` refusal AND leave both records behind.

    "Every tool-call attempt writes exactly one record, including attempts that are refused before
    a companion is reached" -- a refusal is precisely the attempt an activity log most needs to
    carry, and one that only recorded successes would be a record of the wrong thing (T-5-10).

    **The PROVENANCE row is written and committed first, and that ordering is deliberate.** The
    dispatch record is a queue artifact and cannot exist without an environment; the provenance row
    is what a person reads. Writing the queue record first would mean a refusal with no resolvable
    environment -- or any failure journaling it -- took the row a tenant actually needs down with
    it. Two commits rather than one for the same reason: neither write may be able to lose the
    other.

    **``logged_message`` is what the RECORDS keep, when that must differ from what the agent
    reads.** It exists for exactly one caller: the approval gate, whose refusal carries a
    continuation. That continuation is a BEARER value -- whoever holds it can redeem the action
    once the owner approves -- while ``error_text`` is a queryable column on both
    ``companion_tool_calls`` and ``companion_dispatch_calls``, returned verbatim by
    ``GET /v1/companions/activity`` and ``/activity/{id}`` and carried out by every export and
    every database snapshot taken inside its TTL. The two must not be the same string, so the
    caller builds both and this function keeps them apart. Left ``None`` -- which is every other
    call site -- the records keep exactly what the agent read, and that identity is asserted
    rather than assumed (``test_a_refusal_with_no_continuation_logs_exactly_what_it_returned``).
    Stripping the trailer here instead was considered and rejected: a strip is a parser over prose
    the agent surface owns and will rewrite, while the caller already holds both strings in
    structured form.
    """
    recorded_message = message if logged_message is None else logged_message
    resolved_environment_id = environment_id
    if resolved_environment_id is None:
        try:
            resolved_environment_id = await _environment_with_companion(
                auth, db, companion_key=companion_key
            )
        except (CompanionDispatchClaimError, ValueError):
            resolved_environment_id = None
    await _record_call(
        db,
        auth=auth,
        companion_key=companion_key,
        verb=verb,
        tool_class=entry.tool_class,
        arguments=arguments,
        decision=decision,
        environment_id=resolved_environment_id,
        session_id=session_id,
        cap_unit=cap_unit,
        cap_consumed=cap_consumed,
        cap_remaining_after=cap_remaining_after,
        dry_run=dry_run,
        approval_id=approval_id,
        parent_call_id=parent_call_id,
        error_text=recorded_message,
        repeat_count=repeat_count,
    )
    try:
        await record_refusal(
            db,
            user_id=auth.user_id,
            environment_id=resolved_environment_id,
            companion_key=companion_key,
            verb=verb,
            tool_class=entry.tool_class,
            # Stripped HERE rather than at each call site, because there are seven of them and the
            # eighth is where a bearer continuation would end up persisted in the row the activity
            # log renders.
            arguments=_dispatch_arguments(arguments),
            error_text=recorded_message,
        )
        await db.commit()
    except (CompanionDispatchClaimError, ValueError):
        # The refusal itself is the answer the agent needs; failing to journal it must not turn
        # a clean refusal into a 500.
        logger.info("Companion refusal record could not be written")
    return _error(message)


async def _record_call(
    db: AsyncSession,
    *,
    auth: AuthContext,
    companion_key: str,
    verb: str,
    tool_class: str,
    arguments: JsonObject,
    decision: CallDecision,
    environment_id: UUID | None = None,
    session_id: str | None = None,
    cap_unit: str | None = None,
    cap_consumed: int | None = None,
    cap_remaining_after: int | None = None,
    dry_run: bool = False,
    dry_run_diff: JsonObject | None = None,
    call_jti: str | None = None,
    approval_id: UUID | None = None,
    approved_by_user_id: UUID | None = None,
    parent_call_id: UUID | None = None,
    error_text: str | None = None,
    repeat_count: int | None = None,
) -> None:
    """One provenance row for one attempt, committed, and never allowed to become a 500.

    The arguments are stripped of the façade's control parameters on the way in, exactly as the
    dispatch row and the approval row are: ``approval`` is a bearer continuation and ``dry_run`` is
    an instruction to this module, and neither belongs in a row the dashboard renders (T-5-44).
    The preview flag survives as the ``dry_run`` COLUMN, which is where a reader can act on it.

    A journalling failure is logged and swallowed. The alternative is a 500 on a call that already
    reached its answer, which would replace a recorded refusal with an unrecorded crash.
    """
    try:
        await record_companion_tool_call(
            db,
            user_id=auth.user_id,
            environment_id=environment_id,
            session_id=session_id,
            companion_key=companion_key,
            verb=verb,
            tool_class=tool_class,
            arguments=_dispatch_arguments(arguments),
            decision=decision,
            cap_unit=cap_unit,
            cap_consumed=cap_consumed,
            cap_remaining_after=cap_remaining_after,
            dry_run=dry_run,
            dry_run_diff=dry_run_diff,
            source_content_ref=prompting_content_ref(user_id=auth.user_id, session_id=session_id),
            call_jti=call_jti,
            approval_id=approval_id,
            approved_by_user_id=approved_by_user_id,
            parent_call_id=parent_call_id,
            error_text=error_text,
            repeat_count=repeat_count,
        )
        await db.commit()
    except Exception:
        logger.warning("Companion provenance row could not be written")
        await db.rollback()


async def _pending_approval_call_id(
    db: AsyncSession, *, user_id: UUID, approval_id: UUID
) -> UUID | None:
    """The newest ``pending_approval`` row for this approval, or ``None``.

    Scoped by tenant as well as by approval identifier. The approval's own uniqueness already
    makes that redundant, and it is here for the same reason ``_budget_selector`` carries a
    ``user_id`` the unique index does not need: a parent link resolved across a tenant boundary
    would be the single worst row this log could produce.
    """
    return (
        await db.execute(
            select(CompanionToolCall.id)
            .where(
                CompanionToolCall.user_id == user_id,
                CompanionToolCall.approval_id == approval_id,
                CompanionToolCall.decision == CallDecision.PENDING_APPROVAL.value,
            )
            .order_by(CompanionToolCall.created_at.desc(), CompanionToolCall.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _error(message: str) -> JsonObject:
    return {"content": [{"type": "text", "text": f"Error: {message}"}], "isError": True}
