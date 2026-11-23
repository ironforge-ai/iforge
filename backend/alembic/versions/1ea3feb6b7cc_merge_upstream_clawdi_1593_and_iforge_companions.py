"""merge upstream clawdi 1593 and iforge companions

Revision ID: 1ea3feb6b7cc
Revises: fca5e43eb29d, 8bd33e1707fa
Create Date: 2026-09-30 12:04:44.147184

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1ea3feb6b7cc"
down_revision: str | Sequence[str] | None = ("fca5e43eb29d", "8bd33e1707fa")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
