"""companion_session_budgets: scope the session-keyed unique index by tenant

Revision ID: b8d5f0a31c72
Revises: a3e7d9c04b18
Create Date: 2026-09-17

`uq_companion_session_budget` shipped as `(session_id, companion_key)` with no tenant column, while
`companion_facade._budget_selector` always reads with `user_id`. An MCP session id is an opaque
CLIENT-SUPPLIED protocol value, so two tenants can present the same string for the same companion
and collide on one row -- and because the selector names the tenant and the index did not, the
second tenant's upsert was a no-op against a row it could not then address. Every debit of theirs
fell through to the unaddressable branch, which granted the call without incrementing anything: an
unbounded session budget (05-REVIEW.md WR-02, threat T-05-11-03).

Plan 05-10 is what made this reachable rather than latent. `session_id` was NULL on every row until
it landed the `Mcp-Session-Id` handshake; the session-keyed branch is exercised now.

**Why the widening is safe on existing data.** Adding a column to a unique index can only ever
ADMIT rows the narrower index rejected -- any pair distinct under `(session_id, companion_key)` is
still distinct under `(user_id, session_id, companion_key)`. No existing row can violate the new
constraint, so no data migration and no deduplication pass is needed.

**The rollback window is not unconditional, and that is accepted rather than unnoticed**
(T-05-11-06). `downgrade` restores the narrow index, which would fail if two tenants had by then
each written a row under one session id -- exactly the collision this revision exists to permit
apart. On a database where that has not happened the round trip is clean, and that is the state
the migration is verified in.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d5f0a31c72"
down_revision: str | Sequence[str] | None = "a3e7d9c04b18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_companion_session_budget", table_name="companion_session_budgets")
    op.create_index(
        "uq_companion_session_budget",
        "companion_session_budgets",
        ["user_id", "session_id", "companion_key"],
        unique=True,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_companion_session_budget", table_name="companion_session_budgets")
    op.create_index(
        "uq_companion_session_budget",
        "companion_session_budgets",
        ["session_id", "companion_key"],
        unique=True,
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )
