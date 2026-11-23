"""Add the companion provenance log: one append-only row per tool-call attempt.

Revision ID: f1c8b3a7e2d9
Revises: d4e2f8a6c1b3
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f1c8b3a7e2d9"
# Read from `alembic heads` at execution time, which reported exactly one head, rather than copied
# from the plan.
down_revision: str | Sequence[str] | None = "d4e2f8a6c1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Mirrored from `app.models.companion_tool_call.CallDecision`. Spelled as a literal here rather
# than imported, because a migration has to keep describing the schema it created even after the
# model moves on -- that is what makes `downgrade` replayable against an old checkout. The model
# generates its own CHECK from the enum, and a test in
# `backend/tests/test_companion_provenance.py` reads the constraint back out of PostgreSQL and
# compares the two -- so a drift between this literal and the enum is a red test rather than a
# silent divergence.
_DECISIONS = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "pending_approval",
    "refused_destroy_disabled",
    "refused_ratelimit",
    "refused_unknown_tool",
    "refused_approval",
    "runtime_unreachable",
    "dry_run_completed",
)


def upgrade() -> None:
    op.create_table(
        "companion_tool_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable with SET NULL, unlike `companion_dispatch_calls.environment_id`. An attempt
        # refused because no runtime of this tenant's has the companion deployed has no environment
        # to name, and that attempt is precisely one the activity log must still show.
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Nullable because `tools/call` carries only the tool name and its arguments; see the
        # column comment in the model for why a fabricated value would be worse than a null.
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("verb", sa.String(length=61), nullable=False),
        sa.Column("tool_class", sa.String(length=16), nullable=False),
        sa.Column("arguments_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "arguments",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("decision", sa.String(length=40), nullable=False),
        sa.Column("cap_unit", sa.String(length=16), nullable=True),
        sa.Column("cap_consumed", sa.Integer(), nullable=True),
        sa.Column("cap_remaining_after", sa.Integer(), nullable=True),
        sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # A best-effort correlation to the companion response that preceded this call in the same
        # session. A heuristic, not proof -- the model carries the long-form comment and the API
        # ships a disclosure sentence beside the value.
        sa.Column("source_content_ref", sa.String(length=64), nullable=True),
        sa.Column("call_jti", sa.String(length=64), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_call_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "decision IN (" + ", ".join(f"'{value}'" for value in _DECISIONS) + ")",
            name="ck_companion_tool_calls_decision",
        ),
        sa.CheckConstraint(
            "cap_consumed IS NULL OR cap_consumed >= 0",
            name="ck_companion_tool_calls_cap_consumed",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["agent_environments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approval_id"], ["companion_approvals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"], ondelete="SET NULL"),
        # Self-referencing: a call retried after approval points back at the pending-approval row
        # it followed. SET NULL rather than CASCADE -- losing the parent must not delete the child,
        # because the child is the row that says what actually happened.
        sa.ForeignKeyConstraint(
            ["parent_call_id"], ["companion_tool_calls.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_tool_calls_user_id", "companion_tool_calls", ["user_id"])
    op.create_index(
        "ix_companion_tool_calls_environment_id", "companion_tool_calls", ["environment_id"]
    )
    op.create_index("ix_companion_tool_calls_session_id", "companion_tool_calls", ["session_id"])
    op.create_index("ix_companion_tool_calls_decision", "companion_tool_calls", ["decision"])
    op.create_index("ix_companion_tool_calls_call_jti", "companion_tool_calls", ["call_jti"])
    op.create_index("ix_companion_tool_calls_approval_id", "companion_tool_calls", ["approval_id"])
    op.create_index(
        "ix_companion_tool_calls_approved_by_user_id",
        "companion_tool_calls",
        ["approved_by_user_id"],
    )
    op.create_index(
        "ix_companion_tool_calls_parent_call_id", "companion_tool_calls", ["parent_call_id"]
    )
    # The listing's own access path: this tenant's rows, newest first. The route orders by
    # `created_at DESC, id DESC` so paging has a total order at an identical timestamp.
    op.create_index(
        "ix_companion_tool_calls_user_created_at",
        "companion_tool_calls",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_companion_tool_calls_user_created_at", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_parent_call_id", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_approved_by_user_id", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_approval_id", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_call_jti", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_decision", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_session_id", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_environment_id", table_name="companion_tool_calls")
    op.drop_index("ix_companion_tool_calls_user_id", table_name="companion_tool_calls")
    op.drop_table("companion_tool_calls")
