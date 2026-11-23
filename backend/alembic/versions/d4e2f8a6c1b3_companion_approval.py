"""Add durable companion approval state for the non-blocking approval gate.

Revision ID: d4e2f8a6c1b3
Revises: c9d3b6e1f472
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4e2f8a6c1b3"
# Read from `alembic heads` at execution time, which reported exactly one head, rather than copied
# from the plan.
down_revision: str | Sequence[str] | None = "c9d3b6e1f472"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Nullable with SET NULL: an approval outlives the Agent it was requested through, and a
        # person deciding about a call still needs to see it after that Agent is archived.
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("verb", sa.String(length=61), nullable=False),
        # The durable unit of authorization. UNIQUE, and the uniqueness is the mechanism rather
        # than a hygiene constraint: it is what makes a repeat request for the same action reuse
        # the existing row instead of minting a second authorization (05-RESEARCH.md Pitfall 2).
        sa.Column("canonical_action_sha256", sa.String(length=64), nullable=False),
        sa.Column("resolved_target_count", sa.Integer(), nullable=True),
        # The column the OAuth-attempt analog this table is modelled on does not have. A single-use
        # token does not stop semantic replay; a remaining execution budget does.
        sa.Column("remaining_budget", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="issued"),
        sa.Column(
            "arguments",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('issued', 'prepared', 'committed', 'denied', 'expired')",
            name="ck_companion_approvals_status",
        ),
        # A negative budget is a budget with more executions than the tenant granted, which is the
        # same failure mode a negative cap counter would be.
        sa.CheckConstraint(
            "remaining_budget >= 0",
            name="ck_companion_approvals_budget",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["agent_environments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_companion_approvals_user_id", "companion_approvals", ["user_id"])
    op.create_index(
        "ix_companion_approvals_environment_id", "companion_approvals", ["environment_id"]
    )
    # Not partial. Unlike a policy row there is nothing here to supersede: an action hash addresses
    # one authorization for all time, and an expired one is RE-ISSUED on the same row rather than
    # replaced by a second one.
    op.create_index(
        "uq_companion_approval_action",
        "companion_approvals",
        ["canonical_action_sha256"],
        unique=True,
    )
    # The listing's own access path: this tenant's rows, newest first. The route orders by
    # `created_at DESC, id DESC` so paging has a total order.
    op.create_index(
        "ix_companion_approvals_user_created_at",
        "companion_approvals",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_companion_approvals_user_created_at", table_name="companion_approvals")
    op.drop_index("uq_companion_approval_action", table_name="companion_approvals")
    op.drop_index("ix_companion_approvals_environment_id", table_name="companion_approvals")
    op.drop_index("ix_companion_approvals_user_id", table_name="companion_approvals")
    op.drop_table("companion_approvals")
