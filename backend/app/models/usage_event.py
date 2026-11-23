import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# The ledger's dimensions (D-23). ``llm`` is a recorded model call inside a session -- every row
# written before Phase 6. ``compute`` and ``storage`` are a companion installation's accrued cost
# for one period; they belong to a tenant and an installation, never to a session.
USAGE_DIMENSION_LLM = "llm"
USAGE_DIMENSION_COMPUTE = "compute"
USAGE_DIMENSION_STORAGE = "storage"
USAGE_DIMENSIONS = (USAGE_DIMENSION_LLM, USAGE_DIMENSION_COMPUTE, USAGE_DIMENSION_STORAGE)


class UsageEvent(Base, TimestampMixin):
    """One recorded model call inside a session, at per-model grain.

    IronHermes is the only runtime that records per-call usage — upstream
    Hermes has no such table at all. This is Clawdi's copy of that table, and
    the child of `sessions` that makes "what did this session cost?"
    answerable without flattening a multi-model session into one number.

    Why the cost lives HERE and not on `sessions` (D-15): 38 live sessions are
    genuinely multi-model. A session-level cost scalar would have to pick one
    model's pricing or silently sum across grains, and either way the number
    stops meaning anything you can attribute. The one recorded value that
    genuinely has no per-event analogue — `tool_call_count` — sits on the
    session row instead (D-16); everything else stays here.

    Cost representation (D-17): `cost_usd_micros` is a plain integer, stored
    verbatim, with no decimal conversion at any boundary. This is deliberate
    and runs against the usual "money belongs in NUMERIC" advice. IronHermes
    computed the figure at request time under the pricing then in effect; a
    later recompute cannot reproduce it, so the recorded integer IS the
    record, not a rounding of one. This is the repository's first
    money-storage column — there is no NUMERIC, `_cents` or `_micros` column
    anywhere else in `app/models/`, because billing lives entirely in
    Stripe — and Phase 6's per-tenant cost attribution inherits this
    representation.

    The per-tenant cost ledger (D-23, plan 06-21). The same table carries a companion
    installation's accrued compute and storage cost, so there is one ledger to reconcile against
    billing rather than two. Research F8: an llm row reaches its tenant through its session, and a
    compute or storage row has no session at all -- so ``dimension`` says which shape a row has,
    and ``ck_usage_events_dimension_shape`` holds the two apart: an llm row keeps its session; a
    compute or storage row carries NO session and MUST carry its tenant, its installation and its
    period. The missing session is also what keeps the replace-by-session ingest from ever
    deleting one (research Pitfall 8), and ``uq_usage_events_dimension_period`` is its own
    idempotency key: accruing a period twice records it once.
    """

    __tablename__ = "usage_events"

    # A fresh UUID, NOT the source table's `INTEGER PRIMARY KEY AUTOINCREMENT`.
    # That id is a *local* autoincrement: a store rebuild or vacuum can
    # renumber it, and keying dedupe on a renumberable id would silently
    # double-count recorded spend (D-22).
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # The source table declares NO foreign key, and real orphan rows exist in
    # live stores. Clawdi's copy has a real one; orphans are filtered on the
    # client (plan 02-05 emits events only for sessions it is actually
    # emitting), which is what lets this constraint stay real.
    #
    # Nullable since plan 06-21 only for the compute and storage dimensions; the shape CHECK below
    # still requires it on every llm row.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    dimension: Mapped[str] = mapped_column(
        String(16), nullable=False, default=USAGE_DIMENSION_LLM, server_default=USAGE_DIMENSION_LLM
    )
    # The tenant a compute or storage row is billed to. An llm row leaves it NULL and reaches its
    # tenant through its session, so the ingest route's per-row parameters stay what they were.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environments.id", ondelete="SET NULL"),
        nullable=True,
    )
    # CASCADE, not SET NULL: an installation row survives its uninstall (``removed_at``), so only
    # its agent's hard delete or the tenant purge removes it -- and the shape CHECK requires the
    # installation on every compute and storage row, so SET NULL would make that delete fail.
    installation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("companion_installations.id", ondelete="CASCADE"),
        nullable=True,
    )
    companion_key: Mapped[str | None] = mapped_column(String(31), nullable=True)
    # The start of the accrued period: a whole UTC hour for compute, a UTC midnight for storage.
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hours for a compute row (always 1), measured bytes for a storage row.
    quantity: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # The source column is epoch MILLISECONDS while every other timestamp in
    # that schema is float epoch seconds. The conversion happens on the
    # client; the server stores a proper timestamp.
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Nullable and blank-tolerant on purpose (D-18). The source column is
    # `provider TEXT NOT NULL`, yet 56 live rows hold the empty string — so
    # NOT NULL buys nothing here, and rejecting blanks would drop calls that
    # burned real tokens and real money. Fail open on this field.
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)

    in_tok: Mapped[int] = mapped_column(BigInteger, server_default="0")
    out_tok: Mapped[int] = mapped_column(BigInteger, server_default="0")
    cache_read: Mapped[int] = mapped_column(BigInteger, server_default="0")
    cache_create: Mapped[int] = mapped_column(BigInteger, server_default="0")

    # BigInteger, not Integer: int32 tops out around $2,147 expressed in
    # micros, which a single heavy account would pass.
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger, server_default="0")

    # Free text, deliberately. The repo convention for an enum-shaped column
    # is `String` plus a CHECK constraint (see `session_permission.py`), and
    # its absence here is a decision, not an oversight: the recorded design
    # named 4 error kinds and the live store already carries 9. The
    # vocabulary is owned by IronHermes and can grow at any time, so a CHECK
    # — or a PostgreSQL ENUM, or a typing-level enumeration — would reject
    # rows describing real usage. A failed call still consumed tokens;
    # excluding it makes the totals wrong.
    error_kind: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # The dedupe key for replace-by-session ingest (D-22): a hash of the whole
    # event set the client sent for this session. Every row in one session's
    # set carries the same value, so the ingest route reads any one row to
    # decide "unchanged → touch nothing". Nullable so a row written by a
    # future non-hashing producer is still storable rather than rejected.
    event_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # The aggregate query pattern is "every event for this session"; the
        # column-level index above covers it. This one supports the
        # time-ordered read the dashboard will want in Phase 6.
        Index("ix_usage_events_ts", "ts"),
        CheckConstraint(
            "dimension IN ('llm', 'compute', 'storage')", name="ck_usage_events_dimension"
        ),
        CheckConstraint(
            "(dimension = 'llm' AND session_id IS NOT NULL) OR "
            "(dimension <> 'llm' AND session_id IS NULL AND user_id IS NOT NULL "
            "AND installation_id IS NOT NULL AND period_start IS NOT NULL)",
            name="ck_usage_events_dimension_shape",
        ),
        Index(
            "uq_usage_events_dimension_period",
            "installation_id",
            "dimension",
            "period_start",
            unique=True,
            postgresql_where=text("dimension <> 'llm'"),
        ),
    )
