"""companion repeat breaker: the repeated-call circuit breaker and its own refusal decision

Revision ID: fbc4a93bffc4
Revises: 8faaa01cf6e6
Create Date: 2026-09-24 06:40:00.000000

Phase 06 plan 23 (OPS-06, D-22). Three changes that have to land together:

* ``companion_repeat_breakers``: one row per (tenant, companion, verb, canonical action hash,
  dry-run flag), counting how often that exact call arrived in the current window and holding any
  cooldown. It is in PostgreSQL, not the process-wide rate limiter, so a loop spread across
  workers is counted once.
* ``companion_tool_calls.repeat_count``: how many times the call had repeated when the breaker
  stopped it. Nullable, and set only on breaker refusals.
* ``ck_companion_tool_calls_decision`` is re-created with ``refused_repeated_call``, the decision
  the façade records when the breaker stops a call.

The decision list is spelled as a literal, as ``f1c8b3a7e2d9`` and ``8faaa01cf6e6`` spell it, and
not imported from the model. A migration has to keep describing the schema it created after the
model moves on. ``backend/tests/test_companion_provenance.py`` reads the live constraint back out of
PostgreSQL and compares it with ``CallDecision``, so drift between this literal and the enum fails
a test.

``downgrade()`` is LOSSY for one value. The old CHECK cannot hold ``refused_repeated_call``, so any
such provenance row is rewritten to ``refused_policy`` before the old constraint is restored. The
row's ``error_text`` still carries the ``[repeated_call_breaker_tripped]`` token, so the reason is
not lost, only the bucket and the count. The column and the breaker table are dropped.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "fbc4a93bffc4"
down_revision: str | Sequence[str] | None = "8faaa01cf6e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_companion_tool_calls_decision"
_TABLE = "companion_tool_calls"

# The vocabulary BEFORE this revision, exactly as `8faaa01cf6e6` created it.
_DECISIONS_BEFORE = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "refused_budget_ceiling",
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
        "companion_repeat_breakers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("companion_key", sa.String(31), nullable=False),
        sa.Column("verb", sa.String(61), nullable=False),
        sa.Column("action_sha256", sa.String(64), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("tripped_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "user_id",
            "companion_key",
            "verb",
            "action_sha256",
            "dry_run",
            name="uq_companion_repeat_breakers_call",
        ),
        sa.CheckConstraint("count >= 0", name="ck_companion_repeat_breakers_count_non_negative"),
    )
    op.create_index(
        "ix_companion_repeat_breakers_user_id", "companion_repeat_breakers", ["user_id"]
    )

    op.add_column(_TABLE, sa.Column("repeat_count", sa.Integer(), nullable=True))

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_AFTER))


def downgrade() -> None:
    # Lossy by design (see the module docstring): the old CHECK has no bucket for this value.
    op.execute(
        sa.text(
            "UPDATE companion_tool_calls SET decision = 'refused_policy' "
            "WHERE decision = 'refused_repeated_call'"
        )
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_BEFORE))

    op.drop_column(_TABLE, "repeat_count")

    op.drop_index("ix_companion_repeat_breakers_user_id", table_name="companion_repeat_breakers")
    op.drop_table("companion_repeat_breakers")
