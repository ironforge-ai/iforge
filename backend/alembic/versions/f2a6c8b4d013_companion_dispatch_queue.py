"""Add the companion dispatch queue.

Revision ID: f2a6c8b4d013
Revises: e5a9c3d7b2f4
Create Date: 2026-09-14 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f2a6c8b4d013"
down_revision: str | Sequence[str] | None = "e5a9c3d7b2f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_dispatch_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("verb", sa.String(length=61), nullable=False),
        sa.Column("tool_class", sa.String(length=16), nullable=False),
        sa.Column("call_jti", sa.String(length=64), nullable=False),
        sa.Column("arguments", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("poll_claim_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('queued', 'claimed', 'completed', 'failed', 'expired')",
            name="ck_companion_dispatch_calls_status",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND poll_claim_id IS NOT NULL) OR status <> 'claimed'",
            name="ck_companion_dispatch_calls_claim",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["agent_environments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("call_jti"),
    )
    op.create_index(
        "ix_companion_dispatch_calls_user_id",
        "companion_dispatch_calls",
        ["user_id"],
    )
    op.create_index(
        "ix_companion_dispatch_calls_environment_id",
        "companion_dispatch_calls",
        ["environment_id"],
    )
    # The dequeue predicate, in the order `claim_next_call` filters: one environment's queued
    # rows, oldest first. Without it every long-poll claim is a sequential scan of the whole
    # queue, on the hottest path this phase adds.
    op.create_index(
        "ix_companion_dispatch_calls_queue",
        "companion_dispatch_calls",
        ["environment_id", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_companion_dispatch_calls_queue", table_name="companion_dispatch_calls")
    op.drop_index(
        "ix_companion_dispatch_calls_environment_id", table_name="companion_dispatch_calls"
    )
    op.drop_index("ix_companion_dispatch_calls_user_id", table_name="companion_dispatch_calls")
    op.drop_table("companion_dispatch_calls")
