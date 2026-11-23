"""billing reconcile findings

Revision ID: 5b8e2d4f9a17
Revises: 3c4208b81c09
Create Date: 2026-10-22 20:00:00.000000

INT-4: the companion billing reconciliation records each disagreement between a hosted companion
line and local billing as one row per (line, kind). The table is new and starts empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b8e2d4f9a17"
down_revision: str | Sequence[str] | None = "3c4208b81c09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "billing_reconcile_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_item_id", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("local_value", sa.String(length=200), nullable=True),
        sa.Column("hosted_value", sa.String(length=200), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('window_mismatch', 'price_mismatch', 'accrual_gap', 'amount_drift')",
            name="ck_billing_reconcile_findings_kind",
        ),
        sa.ForeignKeyConstraint(
            ["installation_id"], ["companion_installations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "line_item_id", "kind", name="uq_billing_reconcile_findings_line_kind"
        ),
    )
    op.create_index(
        "ix_billing_reconcile_findings_installation_id",
        "billing_reconcile_findings",
        ["installation_id"],
    )
    op.create_index(
        "ix_billing_reconcile_findings_open",
        "billing_reconcile_findings",
        ["last_seen"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_billing_reconcile_findings_open", table_name="billing_reconcile_findings")
    op.drop_index(
        "ix_billing_reconcile_findings_installation_id", table_name="billing_reconcile_findings"
    )
    op.drop_table("billing_reconcile_findings")
