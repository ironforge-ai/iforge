"""add tenant_purges

Revision ID: e5a9c3d7b2f4
Revises: d4f8b2c6a1e3
Create Date: 2026-08-22 13:30:00.000000

ACCESS-04 / D-15/D-16: grace-window purge row scheduled by the operator
delete trigger; claim-id fenced like the lifecycle cleanup and tenant
export jobs; at most one open purge per lifecycle (partial unique).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5a9c3d7b2f4"
down_revision: str | Sequence[str] | None = "d4f8b2c6a1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "tenant_purges",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("lifecycle_id", sa.UUID(), nullable=False),
        # Plain UUID by design: the users row dies in the purge, this value
        # must survive as the purge's audit anchor.
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("subject", sa.String(length=200), nullable=False),
        sa.Column("issuer", sa.String(length=255), nullable=False),
        sa.Column("purge_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("claim_id", sa.UUID(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["lifecycle_id"], ["principal_lifecycles.id"], ondelete="RESTRICT"),
        sa.CheckConstraint(
            "status IN ('pending', 'purging', 'complete', 'failed')",
            name="ck_tenant_purges_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_tenant_purges_attempt_count"),
    )
    op.create_index(
        op.f("ix_tenant_purges_lifecycle_id"), "tenant_purges", ["lifecycle_id"], unique=False
    )
    op.create_index(op.f("ix_tenant_purges_user_id"), "tenant_purges", ["user_id"], unique=False)
    op.create_index(
        "uq_tenant_purges_lifecycle_open",
        "tenant_purges",
        ["lifecycle_id"],
        unique=True,
        postgresql_where=sa.text("status != 'complete'"),
    )
    op.create_index(
        "ix_tenant_purges_due",
        "tenant_purges",
        ["status", "purge_after", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_tenant_purges_due", table_name="tenant_purges")
    op.drop_index("uq_tenant_purges_lifecycle_open", table_name="tenant_purges")
    op.drop_index(op.f("ix_tenant_purges_user_id"), table_name="tenant_purges")
    op.drop_index(op.f("ix_tenant_purges_lifecycle_id"), table_name="tenant_purges")
    op.drop_table("tenant_purges")
