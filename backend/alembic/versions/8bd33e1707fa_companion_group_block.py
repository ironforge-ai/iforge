"""companion group block: block a companion's tool group and its own refusal decision

Revision ID: 8bd33e1707fa
Revises: fbc4a93bffc4
Create Date: 2026-09-24 07:00:00.000000

Phase 06 plan 24 (APP-05, D-29/D-30). Two changes that have to land together:

* ``companion_group_blocks``: one row per (tenant, companion) whose tool group the tenant blocked.
  A row means blocked and allowing deletes it, the destroy-enablement shape with the polarity
  inverted. The façade hides a blocked group from ``tools/list`` and refuses a call to it by name.
* ``ck_companion_tool_calls_decision`` is re-created with ``refused_group_disabled``, the decision
  the façade records when it refuses a call to a blocked group.

The decision list is spelled as a literal, as ``f1c8b3a7e2d9``, ``8faaa01cf6e6`` and
``fbc4a93bffc4`` spell it, and not imported from the model. A migration has to keep describing the
schema it created after the model moves on. ``backend/tests/test_companion_provenance.py`` reads the
live constraint back out of PostgreSQL and compares it with ``CallDecision``, so drift between this
literal and the enum fails a test.

``downgrade()`` is LOSSY for one value. The old CHECK cannot hold ``refused_group_disabled``, so any
such provenance row is rewritten to ``refused_policy`` before the old constraint is restored. The
row's ``error_text`` still carries the ``[companion_group_disabled]`` token, so the reason is not
lost, only the bucket. The block table is dropped, which unblocks every group.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8bd33e1707fa"
down_revision: str | Sequence[str] | None = "fbc4a93bffc4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_companion_tool_calls_decision"
_TABLE = "companion_tool_calls"

# The vocabulary BEFORE this revision, exactly as `fbc4a93bffc4` created it.
_DECISIONS_BEFORE = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "refused_budget_ceiling",
    "refused_repeated_call",
    "pending_approval",
    "refused_destroy_disabled",
    "refused_ratelimit",
    "refused_unknown_tool",
    "refused_approval",
    "runtime_unreachable",
    "dry_run_completed",
)
# Mirrored from `app.models.companion_tool_call.CallDecision`, in its declaration order.
_DECISIONS_AFTER = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "refused_budget_ceiling",
    "refused_repeated_call",
    "pending_approval",
    "refused_destroy_disabled",
    "refused_group_disabled",
    "refused_ratelimit",
    "refused_unknown_tool",
    "refused_approval",
    "runtime_unreachable",
    "dry_run_completed",
)


def _decision_check(values: tuple[str, ...]) -> str:
    return "decision IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "companion_group_blocks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["blocked_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_group_blocks_user_id",
        "companion_group_blocks",
        ["user_id"],
    )
    # Per tenant, per companion -- never per verb. A per-verb block would admit a partially
    # blocked group, and per-verb control already exists as the approval policy.
    op.create_index(
        "uq_companion_group_block",
        "companion_group_blocks",
        ["user_id", "companion_key"],
        unique=True,
    )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_AFTER))


def downgrade() -> None:
    # Lossy by design (see the module docstring): the old CHECK has no bucket for this value.
    op.execute(
        sa.text(
            "UPDATE companion_tool_calls SET decision = 'refused_policy' "
            "WHERE decision = 'refused_group_disabled'"
        )
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_BEFORE))

    op.drop_index("uq_companion_group_block", table_name="companion_group_blocks")
    op.drop_index("ix_companion_group_blocks_user_id", table_name="companion_group_blocks")
    op.drop_table("companion_group_blocks")
