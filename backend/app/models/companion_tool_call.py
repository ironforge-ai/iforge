"""The provenance chain: one append-only row per companion tool-call ATTEMPT (TOOL-07).

``companion_dispatch_calls`` (05-01) is the QUEUE -- a row exists so an executor can claim it, and
its ``environment_id`` is NOT NULL because there is nothing to deliver a call to without one. This
table is the LOG, and the difference is load-bearing: an attempt refused because no runtime of the
tenant's has the companion deployed writes nothing to the queue, and that is exactly the attempt a
person opens an activity log to understand. The log has to answer **why not**, not only what.

``ControlPlaneAuditEvent`` in ``app/models/audit.py`` was the obvious place to extend and is not
usable: ``05-RESEARCH.md`` verified it has no session column, and TOOL-07 asks for a log queryable
per tenant AND per session. Its column STYLE is copied here in full -- ``ondelete="SET NULL"`` with
``index=True`` on every optional foreign key, a ``JSONB(none_as_null=True)`` payload written
through the shipped audit redactor, and ``TimestampMixin`` for ``created_at``.

**No column here holds credential material, and that is a design rule rather than an observation.**
The only free-form payload is ``arguments``, which ``app/services/companion_provenance.py`` passes
through the shipped ``_sanitize_audit_details`` -- so a value under a key containing a secret-ish
substring arrives ``[REDACTED]`` and can never be written verbatim. ``error_text`` is prose the
façade itself authored -- and for the approval gate it authors TWO versions, because the
continuation an approval refusal hands the agent is a bearer token. The agent's copy carries the
token on its trailer line; the copy written HERE is built without it, through
``companion_facade._refuse``'s ``logged_message``. That split is what makes the absence real
rather than asserted: this row's ``error_text`` is returned verbatim by
``GET /v1/companions/activity`` and ``/activity/{id}``, so a token in this column would be a
redeemable value on a read API, in every export and in every database snapshot taken inside its
fifteen-minute TTL. It was, until WR-01 (T-5-44).

**The decision vocabulary lives here, not in the service.** The CHECK constraint below, the writer
and the route's filter all read ``CallDecision``, so the three cannot disagree about what a decision
may be. Defining it beside the constraint rather than in ``companion_provenance`` is what keeps that
true without a circular import.
"""

from __future__ import annotations

import enum
import uuid

from pydantic import JsonValue
from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.companion_approval import CompanionApproval
from app.models.session import AgentEnvironment
from app.models.user import User


class CallDecision(enum.StrEnum):
    """What the FAÇADE decided about one attempt -- never what the companion then answered.

    Eleven of these are refusals or deferrals ``call_companion_tool`` reached on its own.
    ``ALLOWED`` and ``DRY_RUN_COMPLETED`` mean the façade let the attempt through; whatever
    happened afterwards -- an HTTP status the companion chose, an executor reporting a failure --
    is in ``error_text`` rather than in this column. Collapsing the two would make "the façade
    refused this" and "the companion refused this" the same value, and they send a reader to
    entirely different places.

    The mapping from every terminal branch of the façade to a value here is written out in
    ``app/services/companion_provenance.py``. ``backend/tests/test_companion_provenance.py``
    asserts its own literal branch table is the same size and the same set as this enum, so a
    branch added later without a provenance write cannot pass silently.
    """

    ALLOWED = "allowed"
    REFUSED_POLICY = "refused_policy"
    REFUSED_CAP = "refused_cap"
    # The tenant's monthly MONEY ceiling (plan 06-22, D-20/D-21), which is a different ledger from
    # the calls/records/bytes caps above. It has its own value so an activity row says which ceiling
    # fired; recording it as `refused_cap` would repeat the WINDOWS #186 ambiguity.
    REFUSED_BUDGET_CEILING = "refused_budget_ceiling"
    # The repeated-call breaker (plan 06-23, D-22): the SAME call, by canonical action hash, arrived
    # more often than the breaker allows inside its window. A loop, not a volume, which is why it
    # is neither `refused_cap` nor `refused_ratelimit`. The row's `repeat_count` says how often.
    REFUSED_REPEATED_CALL = "refused_repeated_call"
    PENDING_APPROVAL = "pending_approval"
    REFUSED_DESTROY_DISABLED = "refused_destroy_disabled"
    # The tenant blocked this companion's whole tool group (plan 06-24, D-29/D-30). Its own value
    # because blocking is a security control, not a preference: "the owner blocked this companion"
    # and "the owner's policy for this tool is deny" send a reader to different settings, and an
    # agent that called a blocked group by name is exactly the attempt the log must make visible.
    REFUSED_GROUP_DISABLED = "refused_group_disabled"
    REFUSED_RATELIMIT = "refused_ratelimit"
    REFUSED_UNKNOWN_TOOL = "refused_unknown_tool"
    REFUSED_APPROVAL = "refused_approval"
    RUNTIME_UNREACHABLE = "runtime_unreachable"
    DRY_RUN_COMPLETED = "dry_run_completed"


def decision_check_expression() -> str:
    """The CHECK body, generated from ``CallDecision`` so the two cannot drift.

    Generated rather than hand-written for the same reason ``companion_dispatch``'s status
    constraint is spelled beside its tuple: a literal list maintained by hand is a list that goes
    stale on the one edit nobody re-read. ``backend/tests/test_companion_provenance.py`` reads the
    constraint back out of PostgreSQL and compares it to this enum, because the constraint that
    actually refuses a bad value is the one the server is holding -- not the one the model would
    regenerate.
    """
    values = ", ".join(f"'{member.value}'" for member in CallDecision)
    return f"decision IN ({values})"


# Same width as `CompanionSessionBudget.session_id`. One spelling of "how long an MCP session
# identifier may be" across the two tables that carry one.
_SESSION_ID_LENGTH = 128


class CompanionToolCall(Base, TimestampMixin):
    __tablename__ = "companion_tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable with SET NULL, unlike the dispatch queue's NOT NULL environment. An attempt refused
    # because NO runtime of this tenant's has the companion deployed has no environment to name,
    # and that attempt is one of the most important rows in the log. Inventing a placeholder to
    # satisfy a column would make the log claim a call was routed somewhere it was not.
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(AgentEnvironment.id, ondelete="SET NULL"),
        index=True,
    )
    # NULLABLE, and this is the load-bearing comment the plan asks be written here rather than left
    # in prose somebody has to go find.
    #
    # The MCP `tools/call` request carries only the tool NAME and its ARGUMENTS. The protocol
    # delivers no session identifier to this layer, so today every row written by the façade has a
    # null here. A NOT NULL column would leave exactly two options and both are worse than a null:
    # block the phase on a protocol change nobody here controls, or FABRICATE a value -- and a
    # fabricated session identifier would make the activity log claim a grouping that does not
    # exist, which is a lie told confidently to the person least able to check it. A null says "not
    # recorded", which is true.
    #
    # A plain string rather than a foreign key, matching `CompanionSessionBudget.session_id`: an
    # MCP session identifier is an opaque protocol value, not a row in `sessions`.
    session_id: Mapped[str | None] = mapped_column(
        String(_SESSION_ID_LENGTH), nullable=True, index=True
    )
    companion_key: Mapped[str] = mapped_column(String(31), nullable=False)
    verb: Mapped[str] = mapped_column(String(61), nullable=False)
    tool_class: Mapped[str] = mapped_column(String(16), nullable=False)
    # The canonical action digest for this attempt, produced by the SHIPPED
    # `companion_dispatch.canonical_action_hash`. One digest function rather than a second
    # canonicalizer written here: two serializations that disagreed about key order or about
    # non-ASCII escaping would make the same call hash two ways, and this value is also what joins
    # a row to `companion_approvals.canonical_action_sha256`.
    arguments_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, JsonValue]] = mapped_column(
        JSONB(none_as_null=True),
        nullable=False,
        default=dict,
    )
    decision: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # What this attempt actually spent, in the unit its verb declares. Null on an attempt refused
    # before any cap was consulted -- which is a different statement from "it cost nothing", and
    # the log should not flatten the two.
    cap_unit: Mapped[str | None] = mapped_column(String(16))
    cap_consumed: Mapped[int | None] = mapped_column(Integer)
    cap_remaining_after: Mapped[int | None] = mapped_column(Integer)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # The diff this preview SHOWED, kept because it cannot be recovered later.
    #
    # Null on every attempt that was not a completed preview, and that is the load-bearing
    # distinction: an empty diff means "nothing would change", null means "nobody looked", and a
    # renderer that cannot tell them apart will show the first when it means the second.
    #
    # Stored rather than recomputed. Re-running the probe at render time re-reads the target as it
    # is NOW, so a reviewer opening this row after the write landed would be shown a diff that
    # never existed -- an audit row has to say what was seen, not what is. The payload has already
    # passed `validate_dry_run_output` against `DRY_RUN_OUTPUT_SCHEMA` before it reaches this
    # column, so what is persisted is the same artifact the agent was handed, byte for byte.
    dry_run_diff: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    # A BEST-EFFORT correlation, and the comment must say so because the dashboard renders a
    # disclosure line that depends on this being written down where the next reader will find it.
    #
    # When the façade returns companion content it assigns that response an identifier -- the
    # dispatch row's `call_jti`, which is the same value the untrusted envelope embeds as its
    # `id` -- and records it. A later call in the same session is then attributed to the most
    # recent companion response in that session. That is a HEURISTIC: the façade knows the response
    # came first, and it cannot know the call was caused by it. An agent may have decided on its
    # own, or acted on something a user typed. Presenting the correlation as proof would tell a
    # person deciding whether to approve a write that an injected file caused it, when all that was
    # observed is an ordering. `app/services/companion_provenance.SOURCE_CONTENT_REF_DISCLOSURE` is
    # the sentence the API hands the dashboard to render beside it.
    source_content_ref: Mapped[str | None] = mapped_column(String(64))
    # The dispatch row this attempt minted, when it minted one. Nullable because most refusals
    # never reach the mint -- which is D-10's whole point.
    call_jti: Mapped[str | None] = mapped_column(String(64), index=True)
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(CompanionApproval.id, ondelete="SET NULL"),
        index=True,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="SET NULL"),
        index=True,
    )
    # The refusal this attempt followed. Self-referencing, so a call retried after a human's
    # decision points back at the pending-approval row it is the continuation of -- without it the
    # log shows two unrelated rows and "what happened to the call I approved" has no answer in it.
    parent_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companion_tool_calls.id", ondelete="SET NULL"),
        index=True,
    )
    error_text: Mapped[str | None] = mapped_column(Text)
    # How many times this call had repeated when the breaker stopped it (plan 06-23). Set ONLY on
    # `refused_repeated_call` rows; null everywhere else, because "not a breaker stop" is not a
    # count of zero.
    repeat_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        CheckConstraint(
            decision_check_expression(),
            name="ck_companion_tool_calls_decision",
        ),
        # A negative debit is a debit that gave budget back, which is the same failure mode a
        # negative cap counter would be.
        CheckConstraint(
            "cap_consumed IS NULL OR cap_consumed >= 0",
            name="ck_companion_tool_calls_cap_consumed",
        ),
    )


# Declared for the migration and for anything that needs the vocabulary without importing the ORM
# class. Same tuple the CHECK is generated from.
CALL_DECISIONS: tuple[str, ...] = tuple(member.value for member in CallDecision)

__all__ = [
    "CALL_DECISIONS",
    "CallDecision",
    "CompanionToolCall",
    "decision_check_expression",
]
