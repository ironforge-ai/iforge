"""Durable rows for the blast-radius cap vector (TOOL-04): the ceiling, and what has been spent.

Two tables, one file, because they are one contract: ``CompanionCapPolicy`` is the number an agent
must not be able to raise, and ``CompanionSessionBudget`` is what has been spent against it. Column
style, CHECK-constraint discriminators and unique-index conventions are carried across verbatim from
``app/models/companion_tool_policy.py``, which took them from ``app/models/session_permission.py``.

**Why the ceiling is a ROW rather than a constant.** ``app/core/companion_caps.DEFAULT_CAPS`` is
research assumption A5, and the honest thing to do with an unmeasured number is to make it cheap to
correct. A tenant row can be edited without a release; a constant cannot. It is also what makes "an
agent cannot raise the cap" structural rather than hopeful: the cap lives in a table the agent has
no write path to, and the value is read at CALL time from that table, upstream of the mint.

**Why every counter is a ``BigInteger``.** The ``bytes`` unit counts bytes, and a session budget of
two hundred mebibytes is already 209,715,200 -- comfortably inside a 32-bit column, but the whole
point of a per-tenant configuration row is that somebody will raise it. An overflow in the column
that BOUNDS an agent would not fail closed; it would wrap, and a wrapped counter reads as a fresh
budget. The CHECK constraints below keep every value non-negative for the same reason: a negative
``consumed`` is a budget with more room than the tenant granted.

**``session_id`` is NULLABLE, and that is a deliberate choice rather than an oversight.** When this
table shipped, the MCP protocol as this backend spoke it delivered no session identifier to the
façade at all. Rather than invent one, or block the cap vector on a protocol change, a null session
falls back to a ROLLING ONE-HOUR WINDOW keyed by ``environment_id``: the counters accumulate per
Agent per companion within the hour that ``window_started_at`` names, and a new hour starts a new
row. That is a weaker bound than a true per-session budget -- an agent that works for three hours
gets three budgets.

**Plan 05-10 filled the column in, and the two addressing modes are now BOTH used on one call.**
``call_companion_tool`` takes a keyword-only ``session_id`` threaded down from the
``Mcp-Session-Id`` header that ``app/routes/mcp_bridge.py`` mints at ``initialize``. That value is
right for ATTRIBUTION and cannot be trusted as a cap key, because the party the cap exists to bound
is the party that chooses it. So ``apply_session_debit`` spends the WINDOW row on EVERY call and
the SESSION row additionally on calls that carry a session: the two partial indexes below are not
an either/or any more, they are the two addressing modes of one contract, and the one the agent
cannot address is the one that always applies. The column stays NULLABLE and the window row stays
the fallback identity as well, because a third-party client that does not echo the header, and
every row written before 05-10, still have no session to key on.

**Why the session row keeps no window term while the window row has one.** They measure different
things, and collapsing them would lose one of the two. A session is the lifetime of a conversation:
bounding it per hour would hand a three-hour conversation three budgets, which is the weaker bound
this table shipped with. A window is an hour across ALL of that tenant's conversations on one Agent
and companion: that is what makes N invented session ids cost one budget rather than N. Putting the
hour into the session key would therefore not have closed GC-01 -- N ids inside one window are
still N rows, each created zeroed by the upsert.

**Plan 05-16 bounded the session row in TIME without putting a window into its KEY, and that
distinction is the design.** "The lifetime of a conversation" was true without qualification when
this table shipped and is not any more: a conversation running longer than
``SESSION_BUDGET_LIFETIME_SECONDS`` gets a fresh budget, because a debit that finds the row stamped
strictly earlier than ``moment - SESSION_BUDGET_LIFETIME_SECONDS`` resets it in place rather than
refusing against it. Without that bound the session ledger never reset, never expired and was
deleted only by tenant purge, so a long-lived ``iforge mcp`` proxy -- one process, one id, held for
hours to days -- was refused permanently after ``per_session_calls`` calls with no in-product
remedy (GC2-03). The KEY is unchanged: still ``(user_id, session_id, companion_key)``, still no
window term, so everything the paragraph above says about GC-01 still holds and the window ledger
is still what makes N invented ids cost one budget rather than N. The time bound lives in the
upsert's ``ON CONFLICT DO UPDATE`` predicate and deliberately NOT in ``_budget_selector``: a
freshness term in the selector would leave a stale row still colliding with a conflict target that
has no time term while being unaddressable by the conditional ``UPDATE`` that follows, which turns a
long legitimate conversation into a ``session_budget_unresolved`` refusal (WR-02's mismatch class)
and would additionally strand a ``bytes`` refund on a row that aged between its debit and its
settlement.

**Retention, decided rather than observed (plan 05-16, WINDOWS #196).** The lifetime bounds a row's
COUNTERS. It does not bound the row's EXISTENCE. A returning session id reuses its row rather than
creating a second one -- that is what "reset in place" means -- so growth is one row per distinct
session id per companion per tenant, and the RATE of new rows is bounded by the window ledger at
``per_session_calls`` an hour per Agent and companion. That rate bound is what keeps an
agent-chosen key from being an unbounded write amplifier: the content of a session id is
attacker-influenced, the number of them that can be written in an hour is not. The only deletion
path today is ``app/services/tenant_purge.py``, deliberately. EVICTION of rows whose lifetime has
long passed is DEFERRED to Phase 6, and the shape is recorded here so it is not re-derived: a
PERIODIC SWEEP over ``window_started_at`` older than some multiple of the lifetime, not a ``DELETE``
in the debit path. A per-call delete buys nothing the reset does not already buy, and it would put
a second lock-taking statement into the hot path of a gate whose whole value is that it is one
conditional ``UPDATE``.

**Two unique indexes, not one, because of that nullability.** PostgreSQL treats NULLs as distinct in
a unique index, so ``(session_id, companion_key)`` alone would let unlimited null-session rows
accumulate and every debit would create a fresh budget -- a cap that resets on every call, which is
no cap. The pair of PARTIAL unique indexes below splits the two addressing modes cleanly: one over
the session-keyed rows, one over the window-keyed ones.

**Why the session-keyed one NAMES THE TENANT, and the window-keyed one does not.** An MCP session
id is an opaque client-supplied protocol value: the server mints one at ``initialize``, but nothing
stops two tenants presenting the same string, and the column is not a key the control plane issued
under its own authority. ``uq_companion_session_budget`` shipped as ``(session_id, companion_key)``
with no tenant column, so two tenants presenting one id for one companion collided on a SINGLE row
-- and because ``_budget_selector`` always requires ``user_id``, the second tenant's upsert was a
no-op against a row its own selector could not reach, and every debit of theirs fell through to the
unaddressable branch. That branch used to grant the call without recording it: an unbounded session
budget, reachable by guessing or replaying somebody else's session id (WR-02, T-05-11-03). The
index carries ``user_id`` now, so the conflict target and the selector agree about scope.
``uq_companion_window_budget`` needs no such column: ``environment_id`` is an identifier the
control plane issued and an Agent belongs to exactly one tenant, so that index is already
tenant-scoped through it.
"""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.companion_caps import DEFAULT_CAPS, CapVector
from app.models.base import Base, TimestampMixin

# Mirrored from `companion_tool_policy.py` so one companion key has one width everywhere.
_COMPANION_KEY_LENGTH = 31

# The MCP session identifier the transport delivers (05-10). Bounded rather than `Text` because it
# is untrusted input arriving on the wire, and an unbounded key column is an unbounded index entry;
# `mcp_bridge.MCP_SESSION_ID_PATTERN` is the matching bound on the way in.
_SESSION_ID_LENGTH = 128

# The fallback window's width. One hour is short enough that a runaway agent hits a ceiling inside a
# working session, and long enough that an ordinary task does not straddle three budgets.
SESSION_BUDGET_WINDOW_SECONDS = 3600

# How long a SESSION-keyed budget lives before the next debit RESETS it in place. The number is a
# judgement rather than a measurement -- research assumption A5's family -- and it is a module
# constant precisely so that correcting it costs no migration. Four legs, in the order they bind:
#
# 1. It must be MORE than one window. Not because one window would make the session gate STRICTLY
#    redundant with the window gate: it would not. The lifetime is measured from the ROW's own
#    `window_started_at` with a strict comparison, and that stamp is not aligned to the hour buckets
#    the window ledger is keyed on. A conversation opened at 10:59 that spends its budget is still
#    inside a one-window lifetime at 11:01 -- its cutoff is 10:01 and its stamp is 10:59 -- while
#    the 11:00 window row is fresh at zero, so the session gate fires there and the window gate does
#    not. What a one-window lifetime actually does is make the session gate NEAR-redundant: it fires
#    only on the residue of that misalignment, which leaves the per-session half of TOOL-04 as a
#    ceiling set by where inside the hour a conversation happened to open rather than as a number
#    anybody chose. A bound nobody chose is the defect, and that is the leg.
# 2. It must be short enough that a wall clears inside a working day. GC2-03's harm was a refusal
#    with no in-product remedy; four hours makes the remedy "wait", which is the remedy the hourly
#    window already has and the only one a cap is entitled to impose.
# 3. It must be long enough that the dogfooding case never reaches it. Thirty `list_dir` calls an
#    hour across four hours is a hundred and twenty, comfortably under the two hundred
#    `per_session_calls` default, so an operator working steadily never meets the session gate.
# 4. It still bounds one conversation to a quarter of the aggregate hourly rate. A single
#    conversation sustaining more than fifty calls an hour on average is what the session gate now
#    fires on, which is the runaway-conversation case the gate exists for.
#
# NO TEST GUARDS THIS VALUE. The boundary cases in `backend/tests/test_companion_blast_radius.py`
# assert that the comparison is strict, and every one of them computes its own moments FROM the
# constant, so they stay green at any value of it -- including at one window. A retune is guarded by
# the four legs above and by WINDOWS #197, and by nothing else.
SESSION_BUDGET_LIFETIME_WINDOWS = 4
SESSION_BUDGET_LIFETIME_SECONDS = SESSION_BUDGET_LIFETIME_WINDOWS * SESSION_BUDGET_WINDOW_SECONDS


class CompanionCapPolicy(Base, TimestampMixin):
    """One tenant's cap vector for one companion. The ONLY place a cap value comes from.

    Absent means ``DEFAULT_CAPS``. That polarity is the same fail-safe direction as
    ``CompanionToolPolicy``'s: a failure to insert leaves the shipped ceiling in force rather than
    removing the ceiling, so the cap vector cannot be widened by failing to write a row.
    """

    __tablename__ = "companion_cap_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)

    per_call_calls: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_call_calls
    )
    per_call_records: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_call_records
    )
    per_call_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_call_bytes
    )
    per_session_calls: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_session_calls
    )
    per_session_records: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_session_records
    )
    per_session_bytes: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=DEFAULT_CAPS.per_session_bytes
    )

    __table_args__ = (
        CheckConstraint(
            "per_call_calls >= 0 AND per_call_records >= 0 AND per_call_bytes >= 0 "
            "AND per_session_calls >= 0 AND per_session_records >= 0 AND per_session_bytes >= 0",
            name="ck_companion_cap_policies_non_negative",
        ),
        Index(
            "uq_companion_cap_policy",
            "user_id",
            "companion_key",
            unique=True,
        ),
    )

    def cap_vector(self) -> CapVector:
        """This row as the value ``app/core/companion_caps`` judges against.

        The conversion lives here rather than in the pure module so that module keeps its single
        import and never learns what a table is.
        """
        return CapVector(
            per_call_calls=self.per_call_calls,
            per_call_records=self.per_call_records,
            per_call_bytes=self.per_call_bytes,
            per_session_calls=self.per_session_calls,
            per_session_records=self.per_session_records,
            per_session_bytes=self.per_session_bytes,
        )


class CompanionSessionBudget(Base, TimestampMixin):
    """What one session -- or one hour, when there is no session -- has spent on one companion.

    Three counters, because the cap vector has three units and any one of them can refuse a call on
    its own. They are incremented by a CONDITIONAL ``UPDATE`` in
    ``app/services/companion_facade.apply_session_debit``, inside the same transaction that mints
    and enqueues the signed call. A read-then-write against a stale remainder is the classic double
    spend, and it is the one thing ``backend/tests/test_companion_blast_radius.py``'s
    ``committed_db`` case exists to catch.

    See the module docstring for why ``session_id`` is nullable, and what a null session falls
    back to.
    """

    __tablename__ = "companion_session_budgets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # NOT NULL: the debit runs after the façade has resolved which of the tenant's Agents hosts the
    # companion, so there is always one, and it is what the null-session fallback keys on.
    environment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)

    session_id: Mapped[str | None] = mapped_column(String(_SESSION_ID_LENGTH), nullable=True)
    # The hour this row's counters belong to, truncated. For a NULL session it is part of the key:
    # a new hour is a new row, and a stale window row is unreachable by construction. For a
    # SESSION-keyed row it is load-bearing in a different way -- it records when that budget last
    # OPENED, and a row whose value is strictly older than `SESSION_BUDGET_LIFETIME_SECONDS` is
    # STALE rather than spent: the next debit's upsert resets it in place, zeroing the three
    # counters and re-stamping this column. See `companion_facade._debit_ledger`.
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    calls_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    records_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    bytes_consumed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        CheckConstraint(
            "calls_consumed >= 0 AND records_consumed >= 0 AND bytes_consumed >= 0",
            name="ck_companion_session_budgets_non_negative",
        ),
        Index(
            "uq_companion_session_budget",
            "user_id",
            "session_id",
            "companion_key",
            unique=True,
            postgresql_where=text("session_id IS NOT NULL"),
        ),
        Index(
            "uq_companion_window_budget",
            "environment_id",
            "companion_key",
            "window_started_at",
            unique=True,
            postgresql_where=text("session_id IS NULL"),
        ),
    )

    def consumed(self, unit: str) -> int:
        """This row's counter for one cap unit, or RAISE on an unknown unit."""
        from app.core.companion_caps import CAP_UNITS

        if unit not in CAP_UNITS:
            raise ValueError(f"unknown cap unit: {unit!r}")
        return int(getattr(self, f"{unit}_consumed"))
