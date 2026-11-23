"""drop invite codes

Revision ID: 107014e81cb5
Revises: 1ea3feb6b7cc
Create Date: 2026-10-05 21:38:34.695476

06.3 D-01 removes the invite-code system: Clerk's access mode now decides who
may sign up, and nothing reads or writes `invite_codes` any more (the admin
endpoints, the redeem route and the provisioning consume are gone).

- The table is a leaf: only its own `redeemed_by` column references `users`,
  and nothing references it, so it drops cleanly.
- Redemption history stays in `control_plane_audit_events`; this revision does
  not touch any audit row.
- `users.invite_exempted_at` and `users.billing_unlocked_at` are deliberately
  untouched: they carry the billing exemption and the sticky billing unlock.
- The drop is one-way for data. The downgrade recreates the empty table and
  its three indexes exactly as c7e2a9f4d5b8 defined them so the history stays
  walkable; it cannot restore rows.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "107014e81cb5"
down_revision: str | Sequence[str] | None = "1ea3feb6b7cc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the invite_codes indexes, then the table."""
    op.drop_index(op.f("ix_invite_codes_redeemed_by"), table_name="invite_codes")
    op.drop_index(op.f("ix_invite_codes_batch_id"), table_name="invite_codes")
    op.drop_index(op.f("ix_invite_codes_code"), table_name="invite_codes")
    op.drop_table("invite_codes")


def downgrade() -> None:
    """Recreate the empty invite_codes structure from c7e2a9f4d5b8 (no rows)."""
    op.create_table(
        "invite_codes",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("batch_label", sa.String(length=200), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redeemed_by", sa.UUID(), nullable=True),
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["redeemed_by"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_invite_codes_code"), "invite_codes", ["code"], unique=True)
    op.create_index(op.f("ix_invite_codes_batch_id"), "invite_codes", ["batch_id"], unique=False)
    op.create_index(
        op.f("ix_invite_codes_redeemed_by"), "invite_codes", ["redeemed_by"], unique=False
    )
