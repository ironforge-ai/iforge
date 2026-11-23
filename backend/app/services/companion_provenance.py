"""The single writer for the companion provenance log (TOOL-07).

One function, ``record_companion_tool_call``, copied in shape from
``app/services/audit.record_control_plane_audit``: keyword-only arguments, sanitize on the way in
through the SHIPPED redactor, ``db.add`` with **no commit** so the caller's transaction owns the
write. That last part is what keeps a provenance row, the cap debit, the approval budget decrement
and the mint inside one atomic unit on the paths where they belong together.

**No second redactor, ever.** ``_sanitize_audit_details`` is imported from ``app.services.audit``
rather than reimplemented. Two redactors drift, and the first symptom of that drift is a key shape
one of them redacts and the other stores -- discovered, if at all, by reading a log that already
contains the thing it was supposed to keep out. The shipped one also already bounds string length
at 500 characters and sequences at 100 items, which is the second half of what this table needs.

**No second canonicalizer either.** The argument digest is produced by
``companion_dispatch.canonical_action_hash``, the same function ``companion_approvals`` is keyed
on, so a provenance row and the approval it belongs to carry the same value and join without a
second rule about how arguments serialize.

---

## Every terminal branch of ``call_companion_tool``, and what it records

``CallDecision`` records what the FAÇADE decided. The eleven values are a bucket vocabulary; the
REASON is ``error_text``, verbatim, which is why the buckets can be coarse without the log
becoming vague.

| Façade branch                                        | Decision                    |
|------------------------------------------------------|-----------------------------|
| the name owns no registry row                          | ``refused_unknown_tool``    |
| the class is one this deployment does not execute      | ``refused_policy``          |
| the caller's token lacks the class's scope             | ``refused_policy``          |
| the arguments failed the verb's input schema           | ``refused_policy``          |
| ``evaluate_policy`` returned ``deny``                  | ``refused_policy``          |
| ...with reason ``refused_destroy_disabled``            | ``refused_destroy_disabled``|
| the tenant's monthly spend ceiling is reached         | ``refused_budget_ceiling``  |
| the same call repeated past the repeated-call breaker  | ``refused_repeated_call``   |
| the per-call cap refused                               | ``refused_cap``             |
| the session debit refused (real call or read probe)    | ``refused_cap``             |
| the rate bucket refused                                | ``refused_ratelimit``       |
| no runtime of this tenant's has the companion deployed | ``runtime_unreachable``     |
| no executor claimed the call before the deadline       | ``runtime_unreachable``     |
| a preview's read probe was refused by its own policy   | ``refused_policy``          |
| the verb declares no read probe to preview it with     | ``refused_policy``          |
| a preview's diff failed its own declared output schema | ``refused_policy``          |
| a preview computed a diff                              | ``dry_run_completed``       |
| the approval gate asked, or reported ready             | ``pending_approval``        |
| the approval gate refused (denied, expired, spent, ... )| ``refused_approval``        |
| the call was dispatched                                | ``allowed``                 |

``refused_policy`` is deliberately the bucket for "the façade's own rules stopped this before it
was dispatched", which is wider than the tenant's policy row alone. A class this deployment does
not execute, a missing scope and a rejected argument payload are all the server refusing under
rules the agent cannot influence, and every one of them carries its specific reason in
``error_text``. Splitting them into their own decision values would grow the vocabulary past the
set the dashboard renders as filter facets without telling a reader anything ``error_text`` does
not already say.

A call that WAS dispatched and then met an HTTP 4xx from the companion, or an executor reporting a
failure, stays ``allowed``: the façade allowed it, and what the companion answered is a different
fact, recorded in ``error_text``. Folding those into a refusal would make "we stopped this" and
"they stopped this" the same value.

---

## The prompting-content ledger

``record_returned_content`` remembers the identifier of the last companion response the façade
handed back for a (tenant, session) pair, and ``prompting_content_ref`` reads it. That identifier
is the dispatch row's ``call_jti``, which is also the ``id`` the untrusted envelope embeds -- so the
value a reviewer sees on the provenance row is the value the agent saw in the content.

Per PROCESS and TTL-bounded, exactly like ``companion_dryrun``'s preview ledger and for the same
reasons. A miss is ordinary -- a second worker, an expired entry, a fresh session, a call that
carried no session at all -- and a miss records ``None``, which reads as "not recorded". A
correlation this ledger DOES produce is still only an ordering: the façade knows the response came
first and cannot know the call was caused by it. ``SOURCE_CONTENT_REF_DISCLOSURE`` is the sentence
the API hands the dashboard to say so.

**(tenant, session) is the WHOLE identity, and there is no shared bucket** -- see ``_ledger_key``
for why plan 05-10 removed the one that used to exist. A call with no observed session is
unattributable rather than pooled with every other sessionless call in its tenant, so the
disclosure's "in the same session" is now true of what the server observes: a reference is
produced only when a real observed session id matched on both the record and the read.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.companion_tool_call import CALL_DECISIONS, CallDecision, CompanionToolCall
from app.services.audit import _sanitize_audit_details
from app.services.companion_dispatch import canonical_action_hash

# The sentence the detail route hands the dashboard to render beside a prompting-content link.
# Spelled once, here, beside the ledger that produces the value -- so the disclosure travels WITH
# the correlation rather than living in a docstring no renderer reads. The plan's prohibition is
# explicit: a best-effort provenance correlation must not be presented as established fact.
SOURCE_CONTENT_REF_DISCLOSURE = (
    "Best-effort correlation: this call followed that companion response in the same session. "
    "The server observed the ordering only -- it is not evidence that the response caused the call."
)

# How long a returned response stays attributable to a following call. The same short bound the
# preview ledger uses, for the same reason: an attribution to something an agent saw fifteen
# minutes and forty tool calls ago is a claim about causation nobody should act on.
CONTENT_REF_TTL_SECONDS = 900.0

# Per PROCESS, deliberately not a table. See the module docstring.
_content_ledger: dict[tuple[str, str], tuple[float, str]] = {}

__all__ = [
    "CALL_DECISIONS",
    "CONTENT_REF_TTL_SECONDS",
    "SOURCE_CONTENT_REF_DISCLOSURE",
    "CallDecision",
    "CompanionToolCall",
    "prompting_content_ref",
    "record_companion_tool_call",
    "record_returned_content",
    "reset_content_ledger",
]


async def record_companion_tool_call(
    db: AsyncSession,
    *,
    user_id: UUID,
    companion_key: str,
    verb: str,
    tool_class: str,
    decision: CallDecision | str,
    environment_id: UUID | None = None,
    session_id: str | None = None,
    arguments: Mapping[str, object] | None = None,
    cap_unit: str | None = None,
    cap_consumed: int | None = None,
    cap_remaining_after: int | None = None,
    dry_run: bool = False,
    dry_run_diff: Mapping[str, object] | None = None,
    source_content_ref: str | None = None,
    call_jti: str | None = None,
    approval_id: UUID | None = None,
    approved_by_user_id: UUID | None = None,
    parent_call_id: UUID | None = None,
    error_text: str | None = None,
    repeat_count: int | None = None,
) -> CompanionToolCall:
    """Write one provenance row for one attempt. No commit -- the caller's transaction owns it.

    ``arguments`` goes through the shipped audit redactor on the way in, so a credential passed by
    mistake under a secret-ish key is stored ``[REDACTED]`` and never reaches the column verbatim.
    The digest is computed from the arguments AS GIVEN, before redaction, because its job is to
    correlate this attempt with the approval that authorized it and with a later repeat of the same
    action -- a digest over the redacted payload would collide every call that happened to carry a
    secret-ish key, which is the opposite of an identity.
    """
    value = decision.value if isinstance(decision, CallDecision) else str(decision)
    row = CompanionToolCall(
        user_id=user_id,
        environment_id=environment_id,
        session_id=session_id,
        companion_key=companion_key,
        verb=verb,
        tool_class=tool_class,
        arguments_digest=canonical_action_hash(
            companion_key=companion_key,
            verb=verb,
            arguments=_json_arguments(arguments),
            tenant_id=user_id,
            agent_id=environment_id,
        ),
        arguments=_sanitized_object(arguments),
        decision=value,
        cap_unit=cap_unit,
        cap_consumed=cap_consumed,
        cap_remaining_after=cap_remaining_after,
        dry_run=dry_run,
        # NOT passed through `_sanitized_object`. The diff is machine-generated by
        # `compute_dry_run` from the probe's own response and has already been validated against
        # `DRY_RUN_OUTPUT_SCHEMA`; it carries paths and counts, never a caller-supplied key that
        # the redactor exists to catch. Redacting it would also mean the persisted diff no longer
        # matched the one the agent was shown, which is the single property this column exists for.
        dry_run_diff=dict(dry_run_diff) if dry_run_diff is not None else None,
        source_content_ref=source_content_ref,
        call_jti=call_jti,
        approval_id=approval_id,
        approved_by_user_id=approved_by_user_id,
        parent_call_id=parent_call_id,
        # Bounded exactly as `record_refusal` bounds its own: the column is `Text`, but a refusal
        # message is a human surface and an unbounded one is an unbounded write.
        error_text=None if error_text is None else error_text[:1000],
        repeat_count=repeat_count,
    )
    db.add(row)
    await db.flush()
    return row


def _json_arguments(arguments: Mapping[str, object] | None) -> dict[str, JsonValue]:
    """The argument mapping as the digest function expects it, unredacted and uncopied in spirit."""
    return {str(key): value for key, value in (arguments or {}).items()}  # pyright: ignore[reportReturnType]


def _sanitized_object(arguments: Mapping[str, object] | None) -> dict[str, JsonValue]:
    """The redacted payload, guaranteed to be an object whatever the redactor hands back."""
    sanitized = _sanitize_audit_details(dict(arguments or {}))
    return sanitized if isinstance(sanitized, dict) else {}


# ---------------------------------------------------------------------------
# The prompting-content ledger.
# ---------------------------------------------------------------------------


def _ledger_key(user_id: object, session_id: str | None) -> tuple[str, str] | None:
    """The ledger's identity is (tenant, session), or ``None`` when there is no session.

    The tenant half is not optional: without it, two tenants presenting the same session id would
    share one bucket, and one tenant's provenance row would point at another tenant's content.
    That is a cross-tenant correlation, which is the most sensitive kind of leak this log could
    produce -- and session ids are CLIENT-SUPPLIED and unverified by design, so a tenant can
    present any string it likes, including one it observed elsewhere.

    **The session half is not optional either, and that is what changed in plan 05-10.** This
    function used to return ``(str(user_id), session_id or "")``. While nothing supplied a session
    id, that ``""`` was one bucket per tenant and looked harmless. Once
    ``app/routes/mcp_bridge.read_mcp_session_id`` made session ids real, it stopped being
    harmless: two concurrent agent conversations under one tenant are ordinary, and a shared
    bucket would let one read the other's "prompted by" reference while
    ``SOURCE_CONTENT_REF_DISCLOSURE`` asserts "in the same session" -- a scope the server cannot
    observe from inside a shared bucket.

    So an absent or blank session is UNATTRIBUTABLE rather than demoted to a shared bucket. The
    shared bucket is not kept as a fallback variant; it is made unrepresentable, because a bucket
    that cannot be attributed to a session cannot honestly answer a claim about a session.

    **The consequence, stated plainly rather than hidden.** A call with no session id produces no
    "prompted by" reference at all, and the dashboard renders "Not recorded". That is a true
    statement about a heuristic that did not fire. A cross-attributed reference would have been a
    false statement about what an agent read, presented under the word provenance.
    """
    if session_id is None or not session_id.strip():
        return None
    return (str(user_id), session_id)


def record_returned_content(
    *,
    user_id: object,
    session_id: str | None,
    content_id: str,
    now: Callable[[], float] = time.monotonic,
) -> None:
    """Remember the identifier of the companion response just handed to the agent.

    A call with no observed session writes NOTHING. There is no bucket to put it in that would
    not be a bucket some other call could read out of.
    """
    key = _ledger_key(user_id, session_id)
    if key is None:
        return
    _content_ledger[key] = (now() + CONTENT_REF_TTL_SECONDS, content_id)


def prompting_content_ref(
    *,
    user_id: object,
    session_id: str | None,
    now: Callable[[], float] = time.monotonic,
) -> str | None:
    """The most recent companion response in this session, or ``None``.

    ``None`` on a miss, never a guess. "Not recorded" is a true statement about a heuristic that
    did not fire; a fabricated reference would be a false statement about what an agent read.

    ``None`` for an absent session too, before the ledger is touched at all -- see ``_ledger_key``.
    An unobserved session has no reference to offer, and offering one from a shared bucket would
    be exactly the fabrication the paragraph above rules out.
    """
    key = _ledger_key(user_id, session_id)
    if key is None:
        return None
    entry = _content_ledger.get(key)
    if entry is None:
        return None
    expires_at, content_id = entry
    if expires_at <= now():
        return None
    return content_id


def reset_content_ledger() -> None:
    """Drop every remembered response. Tests only -- production lets the TTL do the work."""
    _content_ledger.clear()
