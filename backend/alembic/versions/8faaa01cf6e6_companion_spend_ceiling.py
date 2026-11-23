"""companion spend ceiling: the tenant's monthly money ceiling and its own refusal decision

Revision ID: 8faaa01cf6e6
Revises: 66d5894e72d4
Create Date: 2026-09-24 06:00:00.000000

Phase 06 plan 22 (OPS-06, D-20, D-21). Two changes that have to land together:

* ``companion_spend_ceilings``: one row per tenant holding a monthly ceiling on recorded spend, in
  integer micro-dollars. This is a ledger of its own. Phase 5's caps count calls, records and
  bytes, and none of them is money.
* ``ck_companion_tool_calls_decision`` is re-created with ``refused_budget_ceiling``, the decision
  the façade records when that ceiling refuses an agent's call. It gets its own value so the
  activity log says which ceiling fired.

The decision list is spelled as a literal, as ``f1c8b3a7e2d9`` spells it, and not imported from
the model. A migration has to keep describing the schema it created after the model moves on.
``backend/tests/test_companion_provenance.py`` reads the live constraint back out of PostgreSQL and
compares it with ``CallDecision``, so drift between this literal and the enum fails a test.

``downgrade()`` is LOSSY for one value. The old CHECK cannot hold ``refused_budget_ceiling``, so
any such provenance row is rewritten to ``refused_policy`` before the old constraint is restored.
The row's ``error_text`` still carries the ``[spend_ceiling_reached]`` token, so the reason is not
lost, only the bucket. The ceiling table is dropped, and every tenant's ceiling with it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "8faaa01cf6e6"
down_revision: str | Sequence[str] | None = "66d5894e72d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_companion_tool_calls_decision"
_TABLE = "companion_tool_calls"

# The vocabulary BEFORE this revision, exactly as `f1c8b3a7e2d9` created it.
_DECISIONS_BEFORE = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "pending_approval",
    "refused_destroy_disabled",
    "refused_ratelimit",
    "refused_unknown_tool",
    "refused_approval",
    "runtime_unreachable",
    "dry_run_completed",
)
# Mirrored from `app.models.companion_tool_call.CallDecision`, in its declaration order.
_DECISIONS_AFTER = (
    "allowed",
    "refused_policy",
    "refused_cap",
    "refused_budget_ceiling",
    "pending_approval",
    "refused_destroy_disabled",
    "refused_ratelimit",
    "refused_unknown_tool",
    "refused_approval",
    "runtime_unreachable",
    "dry_run_completed",
)


def _decision_check(values: tuple[str, ...]) -> str:
    return "decision IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "companion_spend_ceilings",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ceiling_usd_micros", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.CheckConstraint(
            "ceiling_usd_micros >= 0",
            name="ck_companion_spend_ceilings_non_negative",
        ),
    )

    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_AFTER))


def downgrade() -> None:
    # Lossy by design (see the module docstring): the old CHECK has no bucket for this value.
    op.execute(
        sa.text(
            "UPDATE companion_tool_calls SET decision = 'refused_policy' "
            "WHERE decision = 'refused_budget_ceiling'"
        )
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _decision_check(_DECISIONS_BEFORE))

    op.drop_table("companion_spend_ceilings")
