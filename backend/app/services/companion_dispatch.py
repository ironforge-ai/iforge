"""The signed pre-authorized companion call, and the queue that delivers it (D-02).

Two primitives, one file, because they are one contract:

**The signed call.** A compact JWT carrying a CALL DESCRIPTOR and nothing else -- companion key,
verb, method, path template, canonical arguments, ``jti``, ``aud``, ``exp``. It carries no
credential material of any kind. The runtime that receives it mints its own short-lived companion
assertion from host-resident manifest material; the control plane never sends one and never sees
one (T-5-01). ``backend/tests/test_companion_facade_credentials.py`` asserts that the sentinel
secret is absent from this payload, so the claim is a test that can fail rather than a comment.

Modelled on ``create_mcp_bridge_token`` / ``verify_mcp_bridge_token`` in
``app/services/composio.py``: one module-level signing-key accessor that RAISES when unconfigured,
and a verifier whose ``algorithms`` list is a hard-pinned literal. Algorithm confusion -- handing
the decoder the token's own ``alg`` header -- is the footgun PyJWT deliberately will not close for
you, and a dispatch envelope is an instruction channel into a tenant's network (T-5-04).

**The queue.** ``companion_dispatch_calls`` rows, claimed with ``with_for_update(skip_locked=True)``
under a ``poll_claim_id`` fence, exactly as ``AiProviderOAuthAttempt``'s device-poll claim works.
``skip_locked`` is what makes two concurrent long-polls hand one call to exactly ONE executor: the
loser comes back empty rather than blocking or, far worse, executing the same call twice.

**The approval continuation (D-08), added by plan 05-06.** A third primitive lives here beside the
other two because it is the same class of object and must not become a second signing story: a
compact JWT carrying an action digest and no credential material, decoded with a hard-pinned
``algorithms`` literal. It is minted for a DIFFERENT audience than the dispatch envelope, so a
continuation the agent holds can never be presented as a pre-authorized call to a runtime.

**Where the continuation DOES appear, and where it does not.** It appears in exactly one place: the
refusal text returned to the agent, on the machine-readable trailer line that
``APPROVAL_CONTINUATION_PREFIX`` introduces. No column holds it -- specifically NOT
``companion_tool_calls.error_text`` and NOT ``companion_dispatch_calls.error_text``, which
``GET /v1/companions/activity`` and ``/activity/{id}`` return verbatim to the dashboard and to
every export. Both of those once did: the approval gate handed ``_refuse`` one string and it went
to the agent and to both columns alike, while four docstrings including this one asserted it went
nowhere (WR-01). ``companion_facade._refuse`` now takes a ``logged_message`` and the gate builds
the recorded refusal with ``continuation=None``, so the two strings differ by exactly the token.
The durable half is the approval ROW, and the token is only a pointer to it that the holder cannot
forge.

Every function here takes the caller's ``AsyncSession`` and does ``db.add`` / ``db.flush`` with NO
commit. The caller's transaction owns the write, so a dispatch row and whatever else the request
wrote either both land or neither does.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import jwt
from pydantic import JsonValue
from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.companion_registry import companion_verb
from app.core.config import settings
from app.models.companion_approval import (
    DEFAULT_EXECUTION_BUDGET,
    STATUS_COMMITTED,
    STATUS_DENIED,
    STATUS_EXPIRED,
    STATUS_ISSUED,
    STATUS_PREPARED,
    CompanionApproval,
)
from app.models.companion_dispatch import CompanionDispatchCall
from app.services.audit import _sanitize_audit_details

# The audience every dispatch envelope is minted for and verified against. A decoder that ignores
# `aud` accepts any token the control plane ever signed for any purpose -- including the MCP
# bridge tokens minted with the same key in `app/services/composio.py`.
COMPANION_DISPATCH_AUDIENCE = "clawdi-runtime-companion-dispatch"

# Hard-pinned as a LITERAL list, never derived from the token header. See the module docstring.
_DISPATCH_ALGORITHMS = ["HS256"]

# The call's own deadline. Deliberately shorter than the façade's 25-second internal deadline,
# which is itself inside the CLI proxy's `MCP_FORWARD_TIMEOUT_MS = 30_000` budget: an envelope
# that outlives the request waiting on it is a call nobody is listening for.
DISPATCH_CALL_TTL_SECONDS = 30

# How long a claimed row may sit before another poller may take it. An executor that claimed a
# call and died must not park it until `expires_at`; the whole budget is 30 seconds.
CLAIM_STALE_AFTER_SECONDS = 10


class CompanionDispatchError(Exception):
    """Base class for every dispatch failure this module raises."""


class CompanionDispatchTokenError(CompanionDispatchError):
    """A dispatch envelope failed signature, audience, expiry or claim-shape verification."""


class CompanionDispatchClaimError(CompanionDispatchError):
    """A completion was presented by something that does not own the row's claim."""


class CompanionDispatchArgumentError(CompanionDispatchError):
    """The validated arguments would not survive storage unchanged, so the call is refused.

    The queued row is BOTH the audit record and the dispatch source of truth: `_claim_envelope`
    re-mints the executor's envelope from `row.arguments`, and the token minted at enqueue time is
    discarded. So any transform applied on the way into the row changes what the companion
    actually runs -- while `canonical_action_hash`, the digest the approver approved and the
    provenance row recorded, covers the arguments as VALIDATED.

    Two transforms can diverge: `_sanitize_audit_details` truncates strings at 500 characters, and
    it rewrites values under a `SECRETISH_KEY_PARTS` key to `[REDACTED]`. Either one dispatches a
    different call than the one authorized (CR-01).

    Refusing here rather than storing verbatim is deliberate: the redaction is load-bearing
    (T-5-10), so the answer is to reject a call that cannot be both safely stored AND faithfully
    dispatched, not to weaken the redactor.
    """


@dataclass(frozen=True)
class MintedDispatchCall:
    """A signed pre-authorized call, plus the fields the enqueue path needs alongside it."""

    token: str
    payload: dict[str, Any]
    jti: str
    expires_at: datetime


def _dispatch_signing_key() -> str:
    """The HS256 signing key, or a RAISE.

    Same accessor shape and same failure mode as ``composio._jwt_signing_key``: an unconfigured
    key is a configuration error surfaced loudly at first use, never a silently unsigned or
    weakly-signed envelope.
    """
    key = settings.encryption_key
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY is not configured. Generate a 32-byte hex value and "
            "set it in backend/.env; it must be distinct from VAULT_ENCRYPTION_KEY."
        )
    return key


def mint_dispatch_call(
    *,
    companion_key: str,
    verb: str,
    method: str,
    path_template: str,
    arguments: dict[str, JsonValue] | None = None,
    ttl_seconds: int = DISPATCH_CALL_TTL_SECONDS,
    now: datetime | None = None,
) -> MintedDispatchCall:
    """Mint one signed pre-authorized call.

    The payload's key set is CLOSED and is asserted to be exactly these eight claims. Adding a
    claim is a wire-contract change every deployed runtime parses, which is why the shape is
    pinned by a test rather than left to whatever a caller passes.

    Note what is NOT here: no secret, no bearer token, no companion credential, no tenant
    identifier the callee could act on. The runtime re-derives its own credential from the host
    manifest and the control plane re-derives the tenant from ``AuthContext`` on the way back in.
    """
    minted_at = now or datetime.now(UTC)
    expires_at = minted_at + timedelta(seconds=ttl_seconds)
    jti = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "companion_key": companion_key,
        "verb": verb,
        "method": method,
        "path_template": path_template,
        "arguments": dict(arguments or {}),
        "jti": jti,
        "aud": COMPANION_DISPATCH_AUDIENCE,
        "exp": expires_at,
    }
    token = jwt.encode(payload, _dispatch_signing_key(), algorithm=_DISPATCH_ALGORITHMS[0])
    return MintedDispatchCall(
        token=token,
        # `exp` is re-stated as the integer seconds PyJWT actually encoded, so the payload a
        # caller inspects is the payload that was signed rather than a datetime beside it.
        payload={**payload, "exp": int(expires_at.timestamp())},
        jti=jti,
        expires_at=expires_at,
    )


def verify_dispatch_call(token: str) -> dict[str, Any]:
    """Decode and validate a dispatch envelope, or RAISE.

    ``algorithms`` is the hard-pinned literal; ``aud`` is asserted by the decoder AND re-asserted
    after decode; ``exp`` is enforced by PyJWT. Every failure becomes one exception type, because
    a caller that can tell "bad signature" from "wrong audience" has an oracle it does not need.
    """
    try:
        payload = jwt.decode(
            token,
            _dispatch_signing_key(),
            algorithms=_DISPATCH_ALGORITHMS,
            audience=COMPANION_DISPATCH_AUDIENCE,
            options={"require": ["exp", "aud", "jti"]},
        )
    except jwt.InvalidTokenError as exc:
        raise CompanionDispatchTokenError("invalid companion dispatch call") from exc
    if payload.get("aud") != COMPANION_DISPATCH_AUDIENCE:
        raise CompanionDispatchTokenError("companion dispatch call has the wrong audience")
    for claim in ("companion_key", "verb", "method", "path_template", "jti"):
        if not isinstance(payload.get(claim), str) or not payload[claim]:
            raise CompanionDispatchTokenError(f"companion dispatch call is missing {claim}")
    if not isinstance(payload.get("arguments"), dict):
        raise CompanionDispatchTokenError("companion dispatch call is missing arguments")
    return payload


def dispatch_call_token(row: CompanionDispatchCall) -> str:
    """Re-sign one queued row's descriptor as a dispatch envelope, at claim time.

    Re-minted rather than persisted. A signed call is a bearer instruction, and a bearer
    instruction sitting in a table is one SQL injection or one careless log line away from being
    replayable; re-deriving it from the row costs one HMAC. ``method`` and ``path_template`` are
    read from the registry rather than stored on the row, so the registry stays the single
    authority for what a verb does and a row cannot drift from it.

    The envelope's ``exp`` is the ROW's deadline, not a fresh TTL: claiming a call must not
    extend the window the façade is waiting inside.
    """
    entry = companion_verb(f"{row.companion_key}__{row.verb}")
    payload: dict[str, Any] = {
        "companion_key": row.companion_key,
        "verb": row.verb,
        "method": entry.method,
        "path_template": entry.path_template,
        "arguments": dict(row.arguments or {}),
        "jti": row.call_jti,
        "aud": COMPANION_DISPATCH_AUDIENCE,
        "exp": row.expires_at,
    }
    return jwt.encode(payload, _dispatch_signing_key(), algorithm=_DISPATCH_ALGORITHMS[0])


async def enqueue_call(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID,
    companion_key: str,
    verb: str,
    tool_class: str,
    minted: MintedDispatchCall,
) -> CompanionDispatchCall:
    """Write the queued row for a minted call. No commit -- the caller's transaction owns it.

    ``arguments`` goes through the SHIPPED redactor rather than a copy of it, so a value that
    trips ``SECRETISH_KEY_PARTS`` is redacted before it is persisted and every over-long string is
    truncated at the same 500 characters the audit log uses (T-5-10).

    That redaction is also why this function can REFUSE. The row it writes is re-minted into the
    executor's envelope at claim time, so a redacted or truncated value would be dispatched as the
    authorized argument while the approval digest covered the original -- the companion would run
    something the approver never saw (CR-01). Raising ``CompanionDispatchArgumentError`` when the
    round trip is not an identity keeps both properties: nothing unsafe is persisted, and nothing
    dispatched differs from what was approved.
    """
    stored_arguments = _sanitize_audit_details(minted.payload["arguments"])
    if stored_arguments != minted.payload["arguments"]:
        raise CompanionDispatchArgumentError(
            f"{companion_key}__{verb} arguments do not survive storage unchanged, so the call "
            "the executor would run is not the call that was authorized"
        )
    row = CompanionDispatchCall(
        user_id=user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        verb=verb,
        tool_class=tool_class,
        call_jti=minted.jti,
        arguments=stored_arguments,
        status="queued",
        expires_at=minted.expires_at,
    )
    db.add(row)
    await db.flush()
    return row


async def record_refusal(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID | None,
    companion_key: str,
    verb: str,
    tool_class: str,
    arguments: dict[str, JsonValue] | None,
    error_text: str,
) -> CompanionDispatchCall | None:
    """Write the terminal row for an attempt refused before any companion was reached.

    "Every tool-call attempt writes exactly one dispatch record, including attempts that are
    refused before a companion is reached" is a truth of this phase, and a refusal is precisely
    the attempt a repudiation record most needs to cover.

    Returns ``None`` when no environment could be resolved: ``environment_id`` is NOT NULL and
    there is no tenant-less row to write. That case is itself a refusal ("no runtime has this
    companion"), and inventing a placeholder environment to satisfy a column would make the
    activity log claim a call was routed somewhere it was not.
    """
    if environment_id is None:
        return None
    row = CompanionDispatchCall(
        user_id=user_id,
        environment_id=environment_id,
        companion_key=companion_key,
        verb=verb,
        tool_class=tool_class,
        call_jti=uuid.uuid4().hex,
        arguments=_sanitize_audit_details(arguments or {}),
        status="failed",
        expires_at=datetime.now(UTC),
        error_text=error_text[:1000],
    )
    db.add(row)
    await db.flush()
    return row


async def claim_next_call(
    db: AsyncSession,
    *,
    environment_id: UUID,
    now: datetime | None = None,
    claim_id: str | None = None,
) -> CompanionDispatchCall | None:
    """Claim the oldest deliverable call for one environment, or return ``None``.

    ``skip_locked`` is load-bearing: a second concurrent poller must come back EMPTY rather than
    block on the first poller's row lock. Blocking would serialize every runtime's long poll
    behind one slow claim; not locking at all would hand the same pre-authorized call to two
    executors, which for a non-idempotent verb means the side effect happens twice.

    A ``claimed`` row whose claim has gone stale is re-offered. An executor that claimed a call
    and then died must not park it for the whole ``expires_at`` window -- the entire round-trip
    budget is 30 seconds, so a 10-second stale window is the difference between one retry and
    none.
    """
    moment = now or datetime.now(UTC)
    stale_before = moment - timedelta(seconds=CLAIM_STALE_AFTER_SECONDS)
    statement = (
        select(CompanionDispatchCall)
        .where(
            CompanionDispatchCall.environment_id == environment_id,
            CompanionDispatchCall.expires_at > moment,
            or_(
                CompanionDispatchCall.status == "queued",
                (CompanionDispatchCall.status == "claimed")
                & (CompanionDispatchCall.updated_at < stale_before),
            ),
        )
        .order_by(CompanionDispatchCall.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        return None
    row.status = "claimed"
    row.poll_claim_id = claim_id or secrets.token_hex(16)
    row.updated_at = moment
    db.add(row)
    await db.flush()
    return row


async def complete_call(
    db: AsyncSession,
    *,
    call_jti: str,
    poll_claim_id: str,
    result: dict[str, JsonValue] | None = None,
    error_text: str | None = None,
) -> CompanionDispatchCall:
    """Record an executor's outcome for a call it still owns, or RAISE.

    The fence is checked BEFORE anything is written. An executor whose claim went stale and was
    re-offered to another poller must not be able to overwrite the winner's result with its own
    late one -- that is the same lost-update the ``poll_claim_id`` exists for, arriving from the
    other direction.
    """
    row = (
        await db.execute(
            select(CompanionDispatchCall)
            .where(CompanionDispatchCall.call_jti == call_jti)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise CompanionDispatchClaimError(f"unknown companion dispatch call: {call_jti}")
    if row.status != "claimed" or row.poll_claim_id != poll_claim_id:
        raise CompanionDispatchClaimError(
            f"companion dispatch call {call_jti} is not held by this claim"
        )
    row.status = "failed" if error_text else "completed"
    row.result = _stored_result(result) if result is not None else None
    row.error_text = error_text[:1000] if error_text else None
    db.add(row)
    await db.flush()
    return row


def _stored_result(result: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The executor's result as the row stores it: redacted, but with the companion body whole.

    Every key except ``body`` still goes through the shipped audit redactor (T-5-10). ``body`` is
    stored unchanged because it is not audit detail. It is the companion's answer, and the
    façade reads it back from this row to answer the agent. Clipping it at the redactor's 500
    characters truncated every longer answer without telling anyone. That broke three things: the
    untrusted envelope's rule that content is never altered without disclosure (TOOL-06), the
    ``bytes`` cap, which measures this body (TOOL-04), and any dry run whose probe listing ran past
    500 characters, which then failed its JSON parse (06-07). The body stays bounded by
    ``MAX_DISPATCH_RESULT_BODY`` on the route that accepts it.
    """
    stored = _sanitize_audit_details({key: value for key, value in result.items() if key != "body"})
    assert isinstance(stored, dict)
    if "body" in result:
        stored["body"] = result["body"]
    return stored


async def await_call_result(
    db: AsyncSession,
    *,
    call_jti: str,
) -> CompanionDispatchCall | None:
    """Re-read one dispatch row from the database, bypassing the identity map.

    The façade polls this while an executor works on the call in a DIFFERENT session; without
    ``populate_existing`` the ORM would keep handing back the stale instance it already has and
    the façade would wait out its whole deadline on a call that completed in the first 50ms.
    """
    return (
        await db.execute(
            select(CompanionDispatchCall)
            .where(CompanionDispatchCall.call_jti == call_jti)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# The approval continuation (D-08, TOOL-03), added by plan 05-06.
#
# Everything below exists because of one finding. `05-RESEARCH.md` Pitfall 2: a perfect single-use
# approval token still permits three destructive writes, because the agent's move after a refusal
# is not to replay the spent token -- it is to ask again and receive a fresh, fully valid one.
# "The nonce protects the token, not the action." So the durable unit is the canonical ACTION,
# carried in `companion_approvals` with a remaining execution budget, and the token below is only
# an unforgeable pointer at that row.
# ---------------------------------------------------------------------------

# A DIFFERENT audience from `COMPANION_DISPATCH_AUDIENCE`. One decoder accepting the other's
# tokens would make a continuation -- which the agent holds -- presentable to a runtime as a
# pre-authorized call, which is the whole thing the dispatch envelope's audience exists to stop.
COMPANION_APPROVAL_AUDIENCE = "clawdi-agent-companion-approval"

# Hard-pinned as a LITERAL list, never derived from the token header. Same reasoning as
# `_DISPATCH_ALGORITHMS`: algorithm confusion is the footgun PyJWT deliberately will not close for
# you, and this token authorizes a write inside a tenant's filesystem (T-5-40).
_APPROVAL_ALGORITHMS = ["HS256"]

# How long an approval stays askable and redeemable. Matched to `companion_dryrun`'s preview TTL
# on purpose: an approval binds the target count a preview resolved, and an approval that outlived
# the preview it was granted against would be an authorization for a directory listing nobody
# still has.
APPROVAL_CONTINUATION_TTL_SECONDS = 900

# Bumped if the serialization below ever changes. It is IN the hashed payload, so a change is a
# new hash domain rather than a silent collision between two spellings of one action.
CANONICAL_ACTION_VERSION = 1

# Arguments that are façade CONTROL parameters rather than part of the action. They are excluded
# from the hash for different reasons that land in the same place: `dry_run` must not make a
# preview and its real call two different actions, and `approval` cannot be part of a digest the
# token carrying it has to be checked against.
CONTROL_ARGUMENT_KEYS = frozenset({"dry_run", "approval"})

# The machine-readable trailer an agent finds the continuation on. A CONTRACT, not prose: the
# refusal's sentences are a human surface that will be rewritten, and an agent that had to parse
# them would break on the rewrite.
APPROVAL_CONTINUATION_PREFIX = "approval_continuation="

# The reason vocabulary, in the same style as `companion_policy.DECISION_REASONS` and
# `companion_caps.CAP_REASONS`. Every refusal path below returns exactly one of these, because a
# single generic failure leaves 05-07's activity log unable to answer *why not* -- and "the
# directory grew" and "you changed the path" send an agent to fix different things.
REASON_APPROVAL_REQUIRED = "approval_required"
REASON_APPROVAL_READY = "approval_ready"
REASON_APPROVAL_DENIED = "approval_denied"
REASON_APPROVAL_EXPIRED = "approval_expired"
REASON_APPROVAL_BUDGET_SPENT = "approval_budget_spent"
REASON_APPROVAL_NOT_FOUND = "approval_not_found"
REASON_ACTION_HASH_MISMATCH = "action_hash_mismatch"
REASON_TARGET_COUNT_MISMATCH = "target_count_mismatch"
REASON_CONTINUATION_INVALID = "continuation_invalid"
REASON_APPROVAL_REDEEMABLE = "approval_redeemable"
APPROVAL_REASONS = frozenset(
    {
        REASON_APPROVAL_REQUIRED,
        REASON_APPROVAL_READY,
        REASON_APPROVAL_DENIED,
        REASON_APPROVAL_EXPIRED,
        REASON_APPROVAL_BUDGET_SPENT,
        REASON_APPROVAL_NOT_FOUND,
        REASON_ACTION_HASH_MISMATCH,
        REASON_TARGET_COUNT_MISMATCH,
        REASON_CONTINUATION_INVALID,
        REASON_APPROVAL_REDEEMABLE,
    }
)


@dataclass(frozen=True)
class MintedContinuation:
    """A signed approval continuation, plus what the caller needs to describe it."""

    token: str
    jti: str
    expires_at: datetime


@dataclass(frozen=True)
class ContinuationRedemption:
    """Whether a presented continuation authorizes this exact call, and the reason either way.

    Frozen and comparable for the same reason ``PolicyDecision`` and ``CapDecision`` are: "the
    answer this call was judged by" has to be a value a test can hold, and a mutable one could be
    edited between the check and the mint.
    """

    allowed: bool
    reason: str
    approval: CompanionApproval | None = None


def canonical_action_arguments(arguments: dict[str, JsonValue] | None) -> dict[str, JsonValue]:
    """The arguments that ARE the action, with the façade's control parameters removed.

    Removing them is what makes ``{"path": "/x", "dry_run": true}`` and ``{"path": "/x"}`` one
    action rather than two -- and two would mean a human approving the preview and being asked a
    second time about the call it previewed.
    """
    return {
        key: value for key, value in (arguments or {}).items() if key not in CONTROL_ARGUMENT_KEYS
    }


def canonical_action_hash(
    *,
    companion_key: str,
    verb: str,
    arguments: dict[str, JsonValue] | None,
    tenant_id: object,
    agent_id: object = None,
) -> str:
    """The durable identity of one companion action, as a SHA-256 hex digest.

    The serialization is genuinely canonical, and every part of that matters: ``sort_keys`` makes
    key order irrelevant at every nesting level, the tight ``separators`` remove insignificant
    whitespace, and ``ensure_ascii`` pins the byte sequence for a non-ASCII path regardless of how
    the caller's JSON arrived. Two semantically identical requests that hashed differently would
    silently mint two authorizations, and one human decision would then cover half of what the
    agent went on to do.

    The TENANT and the AGENT are in the payload, not only in the row's columns. Without the tenant
    the unique index over this digest would make one account's pending request collide with a
    stranger's -- so a stranger's decision would answer it, which is a confused deputy with the
    authorization already attached (T-5-42).
    """
    payload = {
        "version": CANONICAL_ACTION_VERSION,
        "companion_key": companion_key,
        "verb": verb,
        "arguments": canonical_action_arguments(arguments),
        "tenant": str(tenant_id),
        "agent": "" if agent_id is None else str(agent_id),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def mint_continuation(
    *,
    action_sha256: str,
    approval_id: UUID,
    ttl_seconds: int = APPROVAL_CONTINUATION_TTL_SECONDS,
    now: datetime | None = None,
) -> MintedContinuation:
    """Mint the continuation a refused call carries back to the agent.

    Five claims and no more: ``jti``, the action digest, the approval row's identifier, the
    audience and the expiry. NO credential material of any kind, and nothing the holder could act
    on without the row -- the token proves only that the control plane issued it, and the row is
    what says whether it may be spent.
    """
    minted_at = now or datetime.now(UTC)
    expires_at = minted_at + timedelta(seconds=ttl_seconds)
    jti = uuid.uuid4().hex
    token = jwt.encode(
        {
            "jti": jti,
            "action_sha256": action_sha256,
            "approval_id": str(approval_id),
            "aud": COMPANION_APPROVAL_AUDIENCE,
            "exp": expires_at,
        },
        _dispatch_signing_key(),
        algorithm=_APPROVAL_ALGORITHMS[0],
    )
    return MintedContinuation(token=token, jti=jti, expires_at=expires_at)


def verify_continuation(token: str) -> dict[str, Any]:
    """Decode and validate a continuation, or RAISE.

    ``algorithms`` is the hard-pinned literal; ``aud`` is asserted by the decoder AND re-asserted
    after decode, because a decoder that ignored it would accept every token this control plane
    has ever signed with the same key -- including a dispatch envelope and a legacy MCP bridge
    token. Every failure becomes ONE exception type: a caller that could tell "bad signature" from
    "wrong audience" has an oracle it does not need.
    """
    try:
        payload = jwt.decode(
            token,
            _dispatch_signing_key(),
            algorithms=_APPROVAL_ALGORITHMS,
            audience=COMPANION_APPROVAL_AUDIENCE,
            options={"require": ["exp", "aud", "jti"]},
        )
    except jwt.InvalidTokenError as exc:
        raise CompanionDispatchTokenError("invalid companion approval continuation") from exc
    if payload.get("aud") != COMPANION_APPROVAL_AUDIENCE:
        raise CompanionDispatchTokenError("companion approval continuation has the wrong audience")
    for claim in ("action_sha256", "approval_id", "jti"):
        if not isinstance(payload.get(claim), str) or not payload[claim]:
            raise CompanionDispatchTokenError(f"companion approval continuation lacks {claim}")
    return payload


def approval_status_at(approval: CompanionApproval, moment: datetime) -> str:
    """The status this row actually has at ``moment``, with expiry applied.

    Expiry is a fact about the CLOCK, not a value somebody has to remember to write. Deriving it
    here rather than sweeping the table means a row cannot sit in ``issued`` past its deadline and
    be approved by a dashboard that had not been told yet -- the decision route and the redemption
    path both ask this one function.
    """
    if approval.status in (STATUS_DENIED, STATUS_EXPIRED):
        return approval.status
    if approval.expires_at <= moment:
        return STATUS_EXPIRED
    return approval.status


async def upsert_approval(
    db: AsyncSession,
    *,
    user_id: UUID,
    environment_id: UUID | None,
    companion_key: str,
    verb: str,
    action_sha256: str,
    arguments: dict[str, JsonValue] | None,
    target_count: int | None,
    ttl_seconds: int = APPROVAL_CONTINUATION_TTL_SECONDS,
    now: datetime | None = None,
) -> CompanionApproval:
    """Get the authorization row for one canonical action, creating it only if there is none.

    ``ON CONFLICT DO NOTHING`` over the unique action digest, then a read. That ordering is the
    whole defence: a repeat request REUSES the existing row, so a spent budget stays spent and a
    second human decision is never requested for an action one decision already covered.

    **A spent budget is never silently reset.** The only state that is re-issued is an EXPIRED one,
    and it re-issues to ``issued`` -- never to ``prepared``. The agent therefore has to obtain a
    new human decision; replenishing a budget without one would be Pitfall 2's reissuance arriving
    through the clock instead of through the token.

    **A denied action is not re-askable.** Turning a "no" back into a prompt is how an agent grinds
    a person down, and the tenant already has a better lever: setting the tool's policy row to
    ``allow`` (``app/models/companion_tool_policy.py``) bypasses this gate entirely.

    ``resolved_target_count`` is written on insert and on re-issue, and left ALONE on an existing
    row. What a person is deciding about must not move underneath them between the request and the
    decision, and the agent controls how often it re-asks.

    No commit. The caller's transaction owns the write.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(seconds=ttl_seconds)
    canonical = canonical_action_arguments(arguments)
    statement = (
        pg_insert(CompanionApproval)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            environment_id=environment_id,
            companion_key=companion_key,
            verb=verb,
            canonical_action_sha256=action_sha256,
            resolved_target_count=target_count,
            remaining_budget=DEFAULT_EXECUTION_BUDGET,
            status=STATUS_ISSUED,
            # Through the SHIPPED redactor rather than a copy of it, so a value that trips
            # `SECRETISH_KEY_PARTS` is redacted before the dashboard ever renders it (T-5-44).
            arguments=_sanitize_audit_details(canonical),
            expires_at=expires_at,
        )
        .on_conflict_do_nothing(index_elements=["canonical_action_sha256"])
    )
    await db.execute(statement)
    row = (
        await db.execute(
            select(CompanionApproval)
            .where(CompanionApproval.canonical_action_sha256 == action_sha256)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if row.status != STATUS_DENIED and approval_status_at(row, moment) == STATUS_EXPIRED:
        row.status = STATUS_ISSUED
        row.remaining_budget = DEFAULT_EXECUTION_BUDGET
        row.expires_at = expires_at
        row.decided_at = None
        row.decided_by_user_id = None
        row.resolved_target_count = target_count
        row.environment_id = environment_id
        row.arguments = _sanitize_audit_details(canonical)
        db.add(row)
        await db.flush()
    return row


async def redeem_continuation(
    db: AsyncSession,
    *,
    token: str,
    companion_key: str,
    verb: str,
    arguments: dict[str, JsonValue] | None,
    tenant_id: UUID,
    agent_id: object = None,
    target_count: int | None = None,
    now: datetime | None = None,
) -> ContinuationRedemption:
    """Decide whether a presented continuation authorizes THIS call, with its own reason.

    The order is the design, and every step is a separate refusal rather than one generic failure.

    1. Decode with the pinned algorithms and audience.
    2. Recompute the action digest from the arguments the agent PRESENTED and compare it to the
       token's claim with ``hmac.compare_digest``. This is what makes an approval for
       ``delete /tmp/x`` unusable against ``delete /`` (T-5-38). Constant-time because the agent
       supplies one side of the comparison and ``==`` on a digest is a timing oracle.
    3. Find the row BY THAT DIGEST, scoped to this tenant. The row is never found by the token's
       identifier claim alone -- that claim is only cross-checked afterwards, so a token cannot
       address a row its own digest does not match.
    4. Terminal states first: denied, then expired, then still-undecided.
    5. The approved target count, re-checked. A three-file delete is not a three-thousand-file one
       (T-5-39).
    6. The remaining budget last, because it is the one this call is about to spend.

    This function only READS. The spend is ``consume_approval``, and it is a conditional UPDATE, so
    two redemptions that both got here cannot both succeed.
    """
    moment = now or datetime.now(UTC)
    try:
        claims = verify_continuation(token)
    except CompanionDispatchTokenError:
        return ContinuationRedemption(allowed=False, reason=REASON_CONTINUATION_INVALID)

    presented = canonical_action_hash(
        companion_key=companion_key,
        verb=verb,
        arguments=arguments,
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    if not hmac.compare_digest(str(claims["action_sha256"]), presented):
        return ContinuationRedemption(allowed=False, reason=REASON_ACTION_HASH_MISMATCH)

    approval = (
        await db.execute(
            select(CompanionApproval)
            .where(
                CompanionApproval.user_id == tenant_id,
                CompanionApproval.canonical_action_sha256 == presented,
            )
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if approval is None:
        return ContinuationRedemption(allowed=False, reason=REASON_APPROVAL_NOT_FOUND)
    if not hmac.compare_digest(str(claims["approval_id"]), str(approval.id)):
        # The digest matched a row this token does not name. Reported as an invalid continuation
        # rather than as a hash mismatch: the digests DID agree, so saying otherwise would send a
        # reader looking at the arguments.
        return ContinuationRedemption(allowed=False, reason=REASON_CONTINUATION_INVALID)

    status = approval_status_at(approval, moment)
    if status == STATUS_DENIED:
        return ContinuationRedemption(
            allowed=False, reason=REASON_APPROVAL_DENIED, approval=approval
        )
    if status == STATUS_EXPIRED:
        return ContinuationRedemption(
            allowed=False, reason=REASON_APPROVAL_EXPIRED, approval=approval
        )
    if status == STATUS_ISSUED:
        return ContinuationRedemption(
            allowed=False, reason=REASON_APPROVAL_REQUIRED, approval=approval
        )
    if approval.resolved_target_count != target_count:
        # Compared including the None-ness of both sides, deliberately. A stored count against a
        # presented ``None`` means the two calls resolved their blast radius differently, and
        # treating that as "no opinion" would be the fail-OPEN reading of a gate whose whole job is
        # to bound how much one approval covers.
        return ContinuationRedemption(
            allowed=False, reason=REASON_TARGET_COUNT_MISMATCH, approval=approval
        )
    if approval.remaining_budget <= 0:
        return ContinuationRedemption(
            allowed=False, reason=REASON_APPROVAL_BUDGET_SPENT, approval=approval
        )
    return ContinuationRedemption(
        allowed=True, reason=REASON_APPROVAL_REDEEMABLE, approval=approval
    )


async def consume_approval(db: AsyncSession, *, approval_id: UUID) -> bool:
    """Spend one execution of an approval, atomically, or report that there was none to spend.

    A CONDITIONAL ``UPDATE`` -- ``WHERE status = 'prepared' AND remaining_budget > 0`` -- and never
    a read followed by a write. Under ``READ COMMITTED`` PostgreSQL re-checks that ``WHERE`` after
    acquiring the row lock, so the second of two concurrent redemptions matches zero rows and comes
    back ``False``. The identical discipline, and the identical reason, as
    ``companion_facade.apply_session_debit``: a read-then-write against a remainder read before the
    lock is the classic double spend, and here the thing double-spent is a human's decision.

    No commit. The caller runs this inside the SAME transaction that mints and enqueues the signed
    call, so an approval can never be spent by a call that did not happen, nor a call happen on an
    approval that was not spent.
    """
    spent = (
        await db.execute(
            update(CompanionApproval)
            .where(
                CompanionApproval.id == approval_id,
                CompanionApproval.status == STATUS_PREPARED,
                CompanionApproval.remaining_budget > 0,
            )
            .values(
                remaining_budget=CompanionApproval.remaining_budget - 1,
                status=case(
                    (CompanionApproval.remaining_budget - 1 <= 0, STATUS_COMMITTED),
                    else_=STATUS_PREPARED,
                ),
            )
            .returning(CompanionApproval.remaining_budget)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    return spent is not None
