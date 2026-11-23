"""Durable rows for the companion approval policy (TOOL-03) and the destroy switch (D-06).

Modelled directly on ``app/models/session_permission.py``, and the three conventions it records are
carried across verbatim rather than re-derived:

* **Explicit identifier columns, not one polymorphic ``principal_id``.** A foreign key on
  ``user_id`` enforces "this user actually exists", and equality on a real column is what lets the
  policy lookup be a ``WHERE`` clause rather than a dispatch.
* **CHECK-constraint discriminators, replaced rather than ``ENUM ALTER``-ed.** The project
  convention from ``Project.kind`` and ``ApiKey.kind``; adding a fourth mode means replacing
  ``ck_companion_tool_policies_mode``, which shows up in a migration diff as a decision.
* **Uniqueness expressed as a unique partial index** over the live rows only
  (``postgresql_where=text("revoked_at IS NULL")``), so a revoked row stays for audit without
  blocking the row that replaces it.

**The one thing INVERTED from that template, and the reason it matters.** ``SessionPermission``'s
docstring says an owner needs no row because access is implicit -- rows there exist to WIDEN access,
so an absent row means "no grant". Here the polarity is reversed: an absent row means ``ask`` for
every non-``read`` class (``app/core/companion_policy.CLASS_DEFAULT_MODES``). A failure to insert
therefore defaults SAFE rather than open. Reading this table with the ``SessionPermission`` mental
model -- "no row, no restriction" -- is the single most likely way to reintroduce a fail-open policy
engine, which is why it is stated here and not left to be inferred.

**Why destroy enablement is a separate table rather than a mode value.** It is a per-tenant,
per-companion switch covering the whole ``destroy`` class; there is no per-tool destroy enablement,
by design. Folding it into ``CompanionToolPolicy`` would make a mixed enabled/disabled destroy group
representable -- a state no honest UI can render and no user can reason about -- and the unique
index below is what makes that state unrepresentable rather than merely discouraged.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.companion_policy import POLICY_MODES
from app.models.base import Base, TimestampMixin

# Bounds mirrored from `companion_dispatch.py` so one companion key has one width everywhere:
# `COMPANION_KEY_PATTERN` caps a key at 31 characters and `_VERB_NAME_PATTERN` caps a verb at 61.
_COMPANION_KEY_LENGTH = 31
_VERB_LENGTH = 61

# Spelled from the policy module's own vocabulary so the CHECK and the decision cannot disagree.
# A mode the database accepts and `evaluate_policy` does not understand would resolve to `ask` --
# safe, but silently wrong, and the wrongness would live in a row nobody re-reads.
_MODE_CHECK = ", ".join(f"'{mode}'" for mode in sorted(POLICY_MODES))


class CompanionToolPolicy(Base, TimestampMixin):
    """One tenant's approval mode for one companion verb.

    At most one ACTIVE row per ``(user_id, companion_key, verb)``, enforced by the unique partial
    index below rather than by a check-then-insert in application code. Two active rows for one verb
    would make the decision depend on row order -- a nondeterministic policy engine, which is worse
    than a wrong one because it is not reproducible (T-5-20).
    """

    __tablename__ = "companion_tool_policies"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: a policy is a tenant-level decision by default. The column exists so a later plan
    # can scope one Agent's policy without a migration, and the resolver ignores it today rather
    # than pretending a narrowing exists that nothing writes.
    environment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_environments.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)
    verb: Mapped[str] = mapped_column(String(_VERB_LENGTH), nullable=False)

    # The discriminator. `allow` runs it, `ask` requires approval, `deny` refuses outright.
    mode: Mapped[str] = mapped_column(String(16), nullable=False)

    # Soft-delete. NULL = active. Kept so a tenant can see when they loosened or tightened a tool,
    # which is exactly the history an approval decision most needs to be auditable against.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            f"mode IN ({_MODE_CHECK})",
            name="ck_companion_tool_policies_mode",
        ),
        Index(
            "uq_active_companion_tool_policy",
            "user_id",
            "companion_key",
            "verb",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class CompanionDestroyEnablement(Base, TimestampMixin):
    """The tenant's per-companion destroy switch. Row present = enabled; absent = disabled.

    Enablement is recorded as the EXISTENCE of a row rather than as a boolean column, for the same
    reason ``SessionPermission``'s ``kind='link'`` row is: the row's existence IS the policy, and a
    boolean column admits a third state (row present, ``enabled=false``) that means the same thing
    as absence and would have to be handled everywhere forever.

    Revoking is deleting the row. There is no ``revoked_at`` here deliberately: the destroy group is
    either on or off for a companion, and enforcement happens at CALL time, so a revocation takes
    effect on the very next call regardless of what any cached tool list still says (T-5-19).
    """

    __tablename__ = "companion_destroy_enablements"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)

    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # SET NULL rather than CASCADE: removing the account of whoever flipped the switch must not
    # silently re-disable the companion's destroy group for the tenant that still owns it.
    enabled_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "uq_companion_destroy_enablement",
            "user_id",
            "companion_key",
            unique=True,
        ),
    )


class CompanionGroupBlock(Base, TimestampMixin):
    """The tenant's per-companion tool-group block (APP-05, D-29/D-30). Row present = BLOCKED.

    ``CompanionDestroyEnablement`` above, with the polarity inverted, and the inversion is the
    point. There the safe default is OFF, so a row widens access. Here the safe default for a
    companion the tenant installed is that its tools work, so a row NARROWS access: while it exists
    the companion's whole tool group is hidden from ``tools/list`` AND refused by name at call time.
    Allowing the group again is deleting the row. Existence rather than a boolean column for the
    reason the destroy switch gives: a boolean admits a third state (row present, ``blocked=false``)
    that means the same thing as absence and would have to be handled everywhere forever.

    Per tenant, per companion, never per verb and never per Agent: ``uq_companion_group_block``
    makes a partially blocked group unrepresentable. Per-verb control already exists as the
    approval policy (``CompanionToolPolicy``), and blocking is deliberately the coarser switch.

    The block is READ at call time, not taken from any cached tool list. That is what makes the
    list's 60-second TTL a staleness bound rather than an exploit window: an agent that fetched its
    list before the block, or that guesses a name, is refused on the very next call (D-29).
    """

    __tablename__ = "companion_group_blocks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Matched EXACTLY. Blocking `postiz` must never block a kind whose key merely begins with the
    # same letters, so every reader compares with equality and none with a prefix.
    companion_key: Mapped[str] = mapped_column(String(_COMPANION_KEY_LENGTH), nullable=False)

    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # SET NULL rather than CASCADE, as for `enabled_by_user_id`: removing the account of whoever
    # blocked the group must not silently UNBLOCK it for the tenant that still owns it.
    blocked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index(
            "uq_companion_group_block",
            "user_id",
            "companion_key",
            unique=True,
        ),
    )
