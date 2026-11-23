"""usage_events dimensions: the per-tenant cost ledger across compute, storage and llm

Revision ID: 66d5894e72d4
Revises: e74e5772a551
Create Date: 2026-09-24 05:30:00.000000

Phase 06 plan 21 (OPS-05, D-23). ``usage_events`` becomes the per-tenant cost ledger instead of a
second metering path standing up beside it. Until now every row was one recorded model call inside
a session: ``session_id`` was NOT NULL and there was no tenant column (research F8). This revision
adds a ``dimension`` (``llm`` | ``compute`` | ``storage``) and the per-tenant keys a companion's
compute and storage rows need, and holds the two row shapes apart with one CHECK:

* an ``llm`` row is still session-bound -- every existing row, which the ``llm`` server default
  classifies without a backfill;
* a ``compute`` or ``storage`` row carries NO session, and must carry its tenant, its installation
  and its period.

A compute or storage row with no session is what keeps the replace-by-session ingest from ever
deleting one (research Pitfall 8): that delete is ``WHERE session_id = :session``, which a NULL
never matches. Its own idempotency key is the partial unique index on
``(installation_id, dimension, period_start)``, so accruing a period twice records it once.

``installation_id`` cascades with its installation. An installation row survives an uninstall
(``removed_at``), so the only deletes that reach it are its agent's hard delete and the tenant
purge -- and ``SET NULL`` there could never succeed: the shape CHECK requires the installation on
every non-llm row, so the parent delete would fail instead of detaching the row.

``downgrade()`` deletes every non-llm row (they cannot exist in the old shape), drops the
additions, and restores NOT NULL on ``session_id``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "66d5894e72d4"
down_revision: str | Sequence[str] | None = "e74e5772a551"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIMENSION_CHECK = "dimension IN ('llm', 'compute', 'storage')"
_SHAPE_CHECK = (
    "(dimension = 'llm' AND session_id IS NOT NULL) OR "
    "(dimension <> 'llm' AND session_id IS NULL AND user_id IS NOT NULL "
    "AND installation_id IS NOT NULL AND period_start IS NOT NULL)"
)


def upgrade() -> None:
    op.alter_column(
        "usage_events",
        "session_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=True,
    )
    op.add_column(
        "usage_events",
        sa.Column("dimension", sa.String(length=16), nullable=False, server_default="llm"),
    )
    op.add_column(
        "usage_events",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column(
        "usage_events",
        sa.Column(
            "environment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_environments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "usage_events",
        sa.Column(
            "installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companion_installations.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("usage_events", sa.Column("companion_key", sa.String(length=31), nullable=True))
    op.add_column(
        "usage_events", sa.Column("period_start", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("usage_events", sa.Column("quantity", sa.BigInteger(), nullable=True))
    op.create_check_constraint("ck_usage_events_dimension", "usage_events", _DIMENSION_CHECK)
    op.create_check_constraint("ck_usage_events_dimension_shape", "usage_events", _SHAPE_CHECK)
    op.create_index("ix_usage_events_user_id", "usage_events", ["user_id"])
    op.create_index(
        "uq_usage_events_dimension_period",
        "usage_events",
        ["installation_id", "dimension", "period_start"],
        unique=True,
        postgresql_where=sa.text("dimension <> 'llm'"),
    )


def downgrade() -> None:
    # A compute or storage row has no session, and the old shape requires one.
    op.execute("DELETE FROM usage_events WHERE dimension <> 'llm'")
    op.drop_index("uq_usage_events_dimension_period", table_name="usage_events")
    op.drop_index("ix_usage_events_user_id", table_name="usage_events")
    op.drop_constraint("ck_usage_events_dimension_shape", "usage_events", type_="check")
    op.drop_constraint("ck_usage_events_dimension", "usage_events", type_="check")
    op.drop_column("usage_events", "quantity")
    op.drop_column("usage_events", "period_start")
    op.drop_column("usage_events", "companion_key")
    op.drop_column("usage_events", "installation_id")
    op.drop_column("usage_events", "environment_id")
    op.drop_column("usage_events", "user_id")
    op.drop_column("usage_events", "dimension")
    op.alter_column(
        "usage_events",
        "session_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
