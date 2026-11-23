"""billing lapse recheck

Revision ID: 3c4208b81c09
Revises: 107014e81cb5
Create Date: 2026-10-22 18:00:00.000000

INT-1 supersedes the sticky billing unlock of 06.3 D-03: an unlocked user's
subscription is re-read periodically, and the unlock is cleared once a lapse
outlasts a one-day grace. Two nullable columns carry that state; existing rows
start with both NULL, so every unlocked user is re-read on its next request
after one re-check interval from its unlock.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c4208b81c09"
down_revision: str | Sequence[str] | None = "107014e81cb5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("billing_checked_at", sa.DateTime(timezone=True)))
    op.add_column("users", sa.Column("billing_lapsed_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_column("users", "billing_lapsed_at")
    op.drop_column("users", "billing_checked_at")
