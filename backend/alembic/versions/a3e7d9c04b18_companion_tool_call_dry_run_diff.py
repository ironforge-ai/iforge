"""companion_tool_call: persist the dry-run diff

Revision ID: a3e7d9c04b18
Revises: f1c8b3a7e2d9
Create Date: 2026-09-15

05-07 recorded `dry_run` as a bare boolean, so the structured payload `compute_dry_run`
produced reached the agent and was discarded. Plan 05-09 renders that payload and had nowhere
to read it from, and recomputing at render time would re-read the target as it is now -- showing
a reviewer a diff that never existed.

Nullable with no server default ON PURPOSE. Null means the attempt was not a completed preview;
an empty diff means the preview ran and nothing would change. Backfilling either onto the rows
that already exist would assert something about them that was never observed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a3e7d9c04b18"
down_revision: str | Sequence[str] | None = "f1c8b3a7e2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "companion_tool_calls",
        sa.Column("dry_run_diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("companion_tool_calls", "dry_run_diff")
