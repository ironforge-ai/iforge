"""Add usage_events and sessions.tool_call_count.

One revision carries both schema changes because they are one decision split
across two grains: cost and cache-creation live on the event row at per-model
grain (D-15), and `tool_call_count` — the one recorded value with no per-event
analogue — lives on the session row (D-16).

`sessions` is a large shipped table, so the new column takes a server-side
default rather than a Python backfill. PostgreSQL has stored non-volatile
column defaults without a table rewrite since 11, so this is a metadata-only
change and existing rows read 0 without a migration scan.

Revision ID: b3d7f1a2c845
Revises: a4f9c2e7d1b6
Create Date: 2026-08-18 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b3d7f1a2c845"
down_revision: str | Sequence[str] | None = "a4f9c2e7d1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("in_tok", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("out_tok", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_read", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cache_create", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("cost_usd_micros", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("error_kind", sa.String(length=200), nullable=True),
        sa.Column("event_set_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_events_session_id", "usage_events", ["session_id"], unique=False)
    op.create_index("ix_usage_events_ts", "usage_events", ["ts"], unique=False)

    op.add_column(
        "sessions",
        sa.Column("tool_call_count", sa.BigInteger(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("sessions", "tool_call_count")
    op.drop_index("ix_usage_events_ts", table_name="usage_events")
    op.drop_index("ix_usage_events_session_id", table_name="usage_events")
    op.drop_table("usage_events")
