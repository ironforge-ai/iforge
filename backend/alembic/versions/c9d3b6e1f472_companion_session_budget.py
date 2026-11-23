"""Add the companion blast-radius cap policy and its per-session budget counters.

Revision ID: c9d3b6e1f472
Revises: b7c4e1f9a2d6
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9d3b6e1f472"
down_revision: str | Sequence[str] | None = "b7c4e1f9a2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_cap_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        # BigInteger throughout: the `bytes` units already run to hundreds of millions, and the
        # whole point of a per-tenant configuration row is that somebody will raise one. An
        # overflow in the column that BOUNDS an agent would not fail closed -- it would wrap, and a
        # wrapped counter reads as a fresh budget.
        sa.Column("per_call_calls", sa.BigInteger(), nullable=False),
        sa.Column("per_call_records", sa.BigInteger(), nullable=False),
        sa.Column("per_call_bytes", sa.BigInteger(), nullable=False),
        sa.Column("per_session_calls", sa.BigInteger(), nullable=False),
        sa.Column("per_session_records", sa.BigInteger(), nullable=False),
        sa.Column("per_session_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "per_call_calls >= 0 AND per_call_records >= 0 AND per_call_bytes >= 0 "
            "AND per_session_calls >= 0 AND per_session_records >= 0 AND per_session_bytes >= 0",
            name="ck_companion_cap_policies_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_cap_policies_user_id",
        "companion_cap_policies",
        ["user_id"],
    )
    # One ceiling per (tenant, companion). Not partial: unlike a policy row there is nothing to
    # revoke here -- a tenant edits their ceiling rather than superseding it.
    op.create_index(
        "uq_companion_cap_policy",
        "companion_cap_policies",
        ["user_id", "companion_key"],
        unique=True,
    )

    op.create_table(
        "companion_session_budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        # NULLABLE deliberately: the MCP protocol as this backend speaks it delivers no session id
        # to the façade today, and a null session falls back to the rolling window below rather
        # than to an invented identifier. See app/models/companion_session_budget.py.
        sa.Column("session_id", sa.String(length=128), nullable=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("calls_consumed", sa.BigInteger(), nullable=False),
        sa.Column("records_consumed", sa.BigInteger(), nullable=False),
        sa.Column("bytes_consumed", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "calls_consumed >= 0 AND records_consumed >= 0 AND bytes_consumed >= 0",
            name="ck_companion_session_budgets_non_negative",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["environment_id"], ["agent_environments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_session_budgets_user_id",
        "companion_session_budgets",
        ["user_id"],
    )
    op.create_index(
        "ix_companion_session_budgets_environment_id",
        "companion_session_budgets",
        ["environment_id"],
    )
    # Two PARTIAL unique indexes rather than one plain one, because PostgreSQL treats NULLs as
    # distinct: a single unique index over (session_id, companion_key) would admit unlimited
    # null-session rows, every debit would create a fresh budget, and the cap would reset on every
    # call -- which is no cap at all.
    op.create_index(
        "uq_companion_session_budget",
        "companion_session_budgets",
        ["session_id", "companion_key"],
        unique=True,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
    op.create_index(
        "uq_companion_window_budget",
        "companion_session_budgets",
        ["environment_id", "companion_key", "window_started_at"],
        unique=True,
        postgresql_where=sa.text("session_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_companion_window_budget", table_name="companion_session_budgets")
    op.drop_index("uq_companion_session_budget", table_name="companion_session_budgets")
    op.drop_index(
        "ix_companion_session_budgets_environment_id", table_name="companion_session_budgets"
    )
    op.drop_index("ix_companion_session_budgets_user_id", table_name="companion_session_budgets")
    op.drop_table("companion_session_budgets")
    op.drop_index("uq_companion_cap_policy", table_name="companion_cap_policies")
    op.drop_index("ix_companion_cap_policies_user_id", table_name="companion_cap_policies")
    op.drop_table("companion_cap_policies")
