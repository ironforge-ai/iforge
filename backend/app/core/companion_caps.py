"""The blast-radius cap vector, as pure arithmetic over tenant configuration (TOOL-04).

A cap NUMBER does not bound a companion. ``05-RESEARCH.md`` verified that filebrowser's
``DELETE /api/resources`` is bulk-capable and that ``PATCH /api/resources`` moves, copies and
renames through one endpoint, so a single permitted call can be arbitrarily destructive and a
calls-only cap bounds nothing anybody cares about. What is enforced instead is a cap VECTOR over
three independent units -- ``calls``, ``records`` and ``bytes`` -- and ANY ONE of them can refuse a
call on its own. ``CompanionVerbEntry.cap_unit`` is which unit a verb debits.

**Caps are a SECOND limit, separate from policy.** ``app/core/companion_policy.py`` decides whether
a call is permitted at all; this module decides how much it may touch. They are deliberately not
folded together: a policy that said "allow" and thereby skipped the cap, or a cap that could only
be reached through a policy branch, would each make one of the two gates unreachable and therefore
untestable. ``backend/tests/test_companion_blast_radius.py`` reaches this module with an explicit
``allow`` row in place, precisely so a refusal there is the cap and never the policy.

**Zero is a VALUE, not an absence.** A cap of zero means "refuse everything". There is no sentinel
for "unlimited" and none should be added: the single most likely way this guarantee is lost is an
implementation reading ``0`` as falsy and substituting a default, which turns the tightest possible
policy into no policy at all, silently.

**Truncation goes one way only.** A ``read`` whose response exceeds the per-call ``bytes`` cap is
CLIPPED at the cap and the envelope says so -- the agent gets the first ``cap`` bytes plus a
disclosure, which is strictly more useful than a refusal and leaks nothing the cap was protecting.
Every other class is REFUSED, because truncating a write means writing half a file and reporting
success, and truncating against a SESSION budget would make one request return different amounts at
different points in a session with nothing in the request explaining why.

**The shipped defaults are tunable configuration, not code.** ``DEFAULT_CAPS`` carries six values --
per call: one ``call``, fifty ``records`` and ten mebibytes of ``bytes``; per session: two hundred
``calls``, five hundred ``records`` and two hundred mebibytes of ``bytes``. They are research
assumption A5 in ``05-RESEARCH.md``, NOT a measured figure: too tight breaks real tasks and too
loose defeats TOOL-04, and the honest position is that nobody has yet watched a real agent work
against them. They are the FALLBACK used when a tenant has no ``companion_cap_policies`` row; the
per-tenant row is the authority, which is what makes the assumption cheap to correct without a
release. Revisit them against real usage before treating any of the six as settled.

**Integer arithmetic with ceiling rounding, everywhere.** ``retry_after_seconds_from_ms`` rounds UP,
matching ``TokenBucket.try_consume``'s ``ceil((deficit / refill_rate) * 1000)`` in
``app/services/rate_limiter.py``. Two modules that disagree about rounding produce a wait one
millisecond short of the refill, a retry that fails for that reason, and a bug nobody can reproduce.

Standard library only. No SQLAlchemy session type, no HTTP object, and -- as in
``companion_policy`` -- no arguments payload in any signature: agent-supplied text has no channel
through which it could reach a cap value. That absence is the structural reason "an agent cannot
raise the cap" holds.

**That sentence is about the cap's VALUE, and the guarantee also depends on the cap's KEY.** It is
not enough that no agent-supplied string can change a number; no agent-supplied string may select a
DIFFERENT counter either, because a counter the agent can pick afresh is a ceiling it never reaches.
The ceiling that always applies is therefore addressed by ``user_id``, ``environment_id``,
``companion_key`` and ``window_started_at`` -- the tenant, the Agent, the companion and the hour.
The control plane issues the first two, the server truncates the fourth from its own clock, and the
third is a registry key; the agent supplies none of the four. An identifier the agent DOES choose --
an ``Mcp-Session-Id`` off the wire -- selects an ADDITIONAL row that is debited on top, so it can
only ever tighten the answer. See ``companion_facade.apply_session_debit`` for the two ledgers and
for what went wrong the one time a chosen key was the whole key (GC-01).

**The two ceilings also have two PERIODS, and the period is a property of the ROW rather than of
this vector.** ``per_session(unit)`` is the ceiling VALUE for both ledgers -- one number, read
twice -- but what it is a ceiling PER differs. The window ledger's period is
``SESSION_BUDGET_WINDOW_SECONDS``, one hour, and it is part of that row's key, so a new hour is a
new row. The session ledger's period is ``SESSION_BUDGET_LIFETIME_SECONDS``, and it is NOT part of
any key: it is measured from the row's own ``window_started_at``, and a debit that finds the row
older than that resets it in place. So the enforced statement is "at most ``per_session(unit)`` per
hour per tenant per Agent per companion, AND at most ``per_session(unit)`` per conversation per
``SESSION_BUDGET_LIFETIME_SECONDS``". Nothing in this pure module mentions a row, which is exactly
why the sentence has to be here: a reader who only reads this file would otherwise take
``per_session`` to name a period as well as a value, and it names neither ledger's.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The unit vocabulary, mirrored by `_CAP_UNITS` in `app/core/companion_registry.py`. A verb row
# declares exactly one of these as its `capUnit`, and the parser refuses anything outside the set.
CAP_UNIT_CALLS = "calls"
CAP_UNIT_RECORDS = "records"
CAP_UNIT_BYTES = "bytes"
CAP_UNITS = frozenset({CAP_UNIT_CALLS, CAP_UNIT_RECORDS, CAP_UNIT_BYTES})

# The machine reason vocabulary, in the same style as `companion_policy.DECISION_REASONS`: a stable
# token separate from the human prose, because the prose is a surface that will be rewritten and the
# token is what a test, a dispatch row and a future caps UI all correspond on.
REASON_WITHIN_CAP = "within_cap"
REASON_PER_CALL_CAP_EXCEEDED = "per_call_cap_exceeded"
REASON_TRUNCATED_AT_PER_CALL_CAP = "truncated_at_per_call_cap"
REASON_SESSION_BUDGET_EXHAUSTED = "session_budget_exhausted"
# The durable half could not ADDRESS the row it needed -- which is a different event from a budget
# that was spent, and must not be reported as one. `apply_session_debit`'s conditional UPDATE
# matches zero rows for two unrelated causes, and only one of them is an empty budget; the other is
# a selector that reached nothing, which cannot be judged as arithmetic because there is no
# remainder to judge. A tenant reading the activity log must not be told their budget ran out when
# it did not, and the call must not proceed on a debit nothing recorded.
REASON_SESSION_BUDGET_UNRESOLVED = "session_budget_unresolved"
CAP_REASONS = frozenset(
    {
        REASON_WITHIN_CAP,
        REASON_PER_CALL_CAP_EXCEEDED,
        REASON_TRUNCATED_AT_PER_CALL_CAP,
        REASON_SESSION_BUDGET_EXHAUSTED,
        REASON_SESSION_BUDGET_UNRESOLVED,
    }
)

# The one class whose per-call `bytes` overrun is clipped rather than refused. See the module
# docstring: truncation is safe for a read and actively wrong for anything that writes.
_TRUNCATABLE_CLASSES = frozenset({"read"})

_MEBIBYTE = 1024 * 1024


@dataclass(frozen=True)
class CapVector:
    """One tenant's ceiling for one companion, over both scopes and all three units.

    Frozen and comparable for the same reason ``PolicyDecision`` is: "the cap this call was judged
    against" must be a value a test can hold and compare, and a mutable vector could be edited
    between the check and the debit -- which is the one window the checks exist to close.

    Every field is a plain non-negative integer. There is no ``None`` meaning "unlimited"; see the
    module docstring on why a sentinel would be a liability rather than a convenience.
    """

    per_call_calls: int
    per_call_records: int
    per_call_bytes: int
    per_session_calls: int
    per_session_records: int
    per_session_bytes: int

    def per_call(self, unit: str) -> int:
        """This vector's per-call ceiling for ``unit``, or RAISE on an unknown unit.

        Raises rather than returning a default, for the same reason ``companion_verb`` does: a
        default here would be a silent decision about whether an unrecognised unit is capped, and
        the answer this module has to give is that it is not callable at all.
        """
        return self._value("per_call", unit)

    def per_session(self, unit: str) -> int:
        """This vector's per-session budget for ``unit``, or RAISE on an unknown unit."""
        return self._value("per_session", unit)

    def _value(self, scope: str, unit: str) -> int:
        if unit not in CAP_UNITS:
            raise ValueError(f"unknown cap unit: {unit!r}")
        return int(getattr(self, f"{scope}_{unit}"))


# Research assumption A5. Tunable configuration, overridden per tenant by a
# `companion_cap_policies` row; see the module docstring before changing any of the six.
DEFAULT_CAPS = CapVector(
    per_call_calls=1,
    per_call_records=50,
    per_call_bytes=10 * _MEBIBYTE,
    per_session_calls=200,
    per_session_records=500,
    per_session_bytes=200 * _MEBIBYTE,
)


@dataclass(frozen=True)
class CapDecision:
    """One cap decision, carrying enough to act on rather than only a boolean.

    ``unit`` says WHICH of the three ceilings was involved, ``requested`` says how much the call
    wanted, and ``remaining`` says how much was available. A refusal that said only "over cap" would
    satisfy the mechanical half of TOOL-04 and fail its user-facing half: those three numbers are
    what a person needs to choose between raising the cap, splitting the task, and stopping.

    ``truncate_to`` is set only on the one allowed-but-clipped outcome, and is ``None`` on every
    refusal -- a refusal carrying a truncation target would read as a partial success.
    """

    allowed: bool
    unit: str
    requested: int
    remaining: int
    reason: str
    truncate_to: int | None = None

    def describe(self) -> str:
        """The refusal text an agent reads, carrying the machine reason verbatim.

        Same shape as ``companion_facade._policy_refusal_message``: the token is in the text on
        purpose, because a refusal whose cause can only be inferred from wording is a refusal
        nothing can assert against.
        """
        return (
            f"[{self.reason}] unit={self.unit} requested={self.requested} "
            f"remaining={self.remaining}"
        )


def check_per_call_cap(
    *, unit: str, requested: int, tool_class: str, caps: CapVector
) -> CapDecision:
    """Judge ONE call against the per-call ceiling for ``unit``.

    ``remaining`` is the whole per-call ceiling, because a per-call cap resets on every call -- it
    is what was available to this call, which is the same question ``debit_session_budget`` answers
    against the session's remainder.

    A ``read`` over the ``bytes`` ceiling is allowed with ``truncate_to`` set; everything else over
    any ceiling is refused. A zero ceiling refuses even a read: clipping to zero bytes is not an
    answer, it is a refusal wearing a success envelope.
    """
    if requested < 0:
        raise ValueError(f"requested must be non-negative: {requested!r}")
    ceiling = caps.per_call(unit)
    if requested <= ceiling:
        return CapDecision(
            allowed=True,
            unit=unit,
            requested=requested,
            remaining=ceiling,
            reason=REASON_WITHIN_CAP,
        )
    if unit == CAP_UNIT_BYTES and tool_class in _TRUNCATABLE_CLASSES and ceiling > 0:
        return CapDecision(
            allowed=True,
            unit=unit,
            requested=requested,
            remaining=ceiling,
            reason=REASON_TRUNCATED_AT_PER_CALL_CAP,
            truncate_to=ceiling,
        )
    return CapDecision(
        allowed=False,
        unit=unit,
        requested=requested,
        remaining=ceiling,
        reason=REASON_PER_CALL_CAP_EXCEEDED,
    )


def debit_session_budget(
    *, unit: str, requested: int, consumed: int, caps: CapVector
) -> CapDecision:
    """Judge ``requested`` against what is LEFT of the session budget for ``unit``.

    Pure arithmetic over a remainder the caller loaded. The durable half -- reading the counter and
    incrementing it atomically -- is ``companion_facade.apply_session_debit``, which expresses the
    same rule as a conditional ``UPDATE`` so two concurrent callers cannot both spend one remainder.
    This function is what that update's condition means, and what a test can assert without a
    database.

    Never truncates. See the module docstring: shrinking an approved call because of unrelated
    earlier history in the same session is a behaviour no request explains.
    """
    if requested < 0:
        raise ValueError(f"requested must be non-negative: {requested!r}")
    if consumed < 0:
        raise ValueError(f"consumed must be non-negative: {consumed!r}")
    budget = caps.per_session(unit)
    remaining = max(0, budget - consumed)
    if requested <= remaining:
        return CapDecision(
            allowed=True,
            unit=unit,
            requested=requested,
            remaining=remaining - requested,
            reason=REASON_WITHIN_CAP,
        )
    return CapDecision(
        allowed=False,
        unit=unit,
        requested=requested,
        remaining=remaining,
        reason=REASON_SESSION_BUDGET_EXHAUSTED,
    )


def retry_after_seconds_from_ms(retry_after_ms: int) -> int:
    """Whole seconds a caller must wait, rounded UP from integer milliseconds.

    Rounding DOWN here is the classic off-by-one that turns one rate-limit refusal into two: a wait
    of 1500 ms reported as one second means the retry arrives 500 ms before the token exists. Up is
    also the direction ``TokenBucket.try_consume`` already rounds, so the control plane and the
    runtime never disagree about when a slot is available.
    """
    if retry_after_ms < 0:
        raise ValueError(f"retry_after_ms must be non-negative: {retry_after_ms!r}")
    return math.ceil(retry_after_ms / 1000)
