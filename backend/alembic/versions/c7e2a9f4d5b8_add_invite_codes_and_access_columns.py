"""add invite_codes and users access columns, grandfather existing users

Revision ID: c7e2a9f4d5b8
Revises: b3d7f1a2c845
Create Date: 2026-08-22 12:10:00.000000

D-01/D-04: `invite_codes` — single-use shareable codes with batch metadata,
expiry, revocation and redemption binding.

D-07: every User row that exists at migration time is grandfathered —
`invite_exempted_at` and `billing_unlocked_at` are backfilled keyed on row
existence ONLY (no email/name predicate; admin-created rows have email=NULL),
with one `invite.grandfathered` control-plane audit event per row.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7e2a9f4d5b8'
down_revision: Union[str, Sequence[str], None] = 'b3d7f1a2c845'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'invite_codes',
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('batch_id', sa.UUID(), nullable=False),
        sa.Column('batch_label', sa.String(length=200), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('redeemed_by', sa.UUID(), nullable=True),
        sa.Column('redeemed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            'created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['redeemed_by'], ['users.id'], ondelete='SET NULL'),
    )
    op.create_index(
        op.f('ix_invite_codes_code'), 'invite_codes', ['code'], unique=True
    )
    op.create_index(
        op.f('ix_invite_codes_batch_id'), 'invite_codes', ['batch_id'], unique=False
    )
    op.create_index(
        op.f('ix_invite_codes_redeemed_by'), 'invite_codes', ['redeemed_by'], unique=False
    )

    op.add_column('users', sa.Column('invite_exempted_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('users', sa.Column('billing_unlocked_at', sa.DateTime(timezone=True), nullable=True))

    # D-07 grandfathering backfill: keyed on row existence ONLY — no
    # email/name predicate (admin-created rows carry email=NULL, Pitfall 3).
    op.execute(
        "UPDATE users SET invite_exempted_at = now(), billing_unlocked_at = now() "
        "WHERE invite_exempted_at IS NULL"
    )
    # One invite.grandfathered audit event per grandfathered row. PostgreSQL
    # 13+ ships gen_random_uuid() built in.
    op.execute(
        """
        INSERT INTO control_plane_audit_events
            (id, actor_type, actor_user_id, target_user_id, source, action,
             resource_type, resource_id, environment_id, channel_account_id,
             channel_agent_link_id, details, created_at, updated_at)
        SELECT
            gen_random_uuid(), 'system', NULL, u.id, 'migration',
            'invite.grandfathered', 'user', u.id::text, NULL, NULL, NULL,
            jsonb_build_object('reason', 'pre_gate_user'), now(), now()
        FROM users u
        """
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "DELETE FROM control_plane_audit_events "
        "WHERE action = 'invite.grandfathered' AND source = 'migration'"
    )
    op.drop_column('users', 'billing_unlocked_at')
    op.drop_column('users', 'invite_exempted_at')
    op.drop_index(op.f('ix_invite_codes_redeemed_by'), table_name='invite_codes')
    op.drop_index(op.f('ix_invite_codes_batch_id'), table_name='invite_codes')
    op.drop_index(op.f('ix_invite_codes_code'), table_name='invite_codes')
    op.drop_table('invite_codes')
