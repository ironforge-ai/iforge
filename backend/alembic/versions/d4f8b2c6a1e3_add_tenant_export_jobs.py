"""add tenant_export_jobs

Revision ID: d4f8b2c6a1e3
Revises: c7e2a9f4d5b8
Create Date: 2026-08-22 13:00:00.000000

ACCESS-04 / D-12: durable DB-backed export job claimed with claim-id
fencing, failure recorded with backoff, retried — cloned from the
principal lifecycle cleanup shape.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f8b2c6a1e3"
down_revision: str | Sequence[str] | None = "c7e2a9f4d5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenant_export_jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("include_vault_values", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="queued", nullable=False),
        sa.Column("artifact_key", sa.String(length=800), nullable=True),
        sa.Column(
            "manifest", postgresql.JSONB(astext_type=sa.Text(), none_as_null=True), nullable=True
        ),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'complete', 'failed')",
            name="ck_tenant_export_jobs_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_tenant_export_jobs_attempt_count"),
    )
    op.create_index(
        op.f("ix_tenant_export_jobs_user_id"), "tenant_export_jobs", ["user_id"], unique=False
    )
    op.create_index(
        "ix_tenant_export_jobs_due",
        "tenant_export_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tenant_export_jobs_due", table_name="tenant_export_jobs")
    op.drop_index(op.f("ix_tenant_export_jobs_user_id"), table_name="tenant_export_jobs")
    op.drop_table("tenant_export_jobs")
