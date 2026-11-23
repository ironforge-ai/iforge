"""Add companion tool policy rows and the per-companion destroy switch.

Revision ID: b7c4e1f9a2d6
Revises: f2a6c8b4d013
Create Date: 2026-09-15 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b7c4e1f9a2d6"
down_revision: str | Sequence[str] | None = "f2a6c8b4d013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "companion_tool_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("environment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("verb", sa.String(length=61), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        # The discriminator, as a CHECK rather than an ENUM: the project convention from
        # `Project.kind` and `ApiKey.kind` is that a new value replaces the constraint, which shows
        # up in a migration diff as a decision instead of as an in-place type alteration.
        sa.CheckConstraint(
            "mode IN ('allow', 'ask', 'deny')",
            name="ck_companion_tool_policies_mode",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["environment_id"], ["agent_environments.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_tool_policies_user_id",
        "companion_tool_policies",
        ["user_id"],
    )
    op.create_index(
        "ix_companion_tool_policies_environment_id",
        "companion_tool_policies",
        ["environment_id"],
    )
    # At most one ACTIVE policy per (tenant, companion, verb). PARTIAL, so a revoked row stays for
    # audit without blocking its replacement. Enforced here rather than by a check-then-insert in
    # application code: that is two statements with a race between them, and two active rows for
    # one verb make the decision depend on row order (T-5-20).
    op.create_index(
        "uq_active_companion_tool_policy",
        "companion_tool_policies",
        ["user_id", "companion_key", "verb"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.create_table(
        "companion_destroy_enablements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("companion_key", sa.String(length=31), nullable=False),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("enabled_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["enabled_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_destroy_enablements_user_id",
        "companion_destroy_enablements",
        ["user_id"],
    )
    # Per tenant, per companion -- never per tool. A per-tool destroy switch would admit a mixed
    # enabled/disabled destroy group, which is a state no UI can render honestly (D-06).
    op.create_index(
        "uq_companion_destroy_enablement",
        "companion_destroy_enablements",
        ["user_id", "companion_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_companion_destroy_enablement", table_name="companion_destroy_enablements"
    )
    op.drop_index(
        "ix_companion_destroy_enablements_user_id", table_name="companion_destroy_enablements"
    )
    op.drop_table("companion_destroy_enablements")
    op.drop_index("uq_active_companion_tool_policy", table_name="companion_tool_policies")
    op.drop_index(
        "ix_companion_tool_policies_environment_id", table_name="companion_tool_policies"
    )
    op.drop_index("ix_companion_tool_policies_user_id", table_name="companion_tool_policies")
    op.drop_table("companion_tool_policies")
