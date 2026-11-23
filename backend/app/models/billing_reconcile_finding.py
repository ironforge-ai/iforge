"""What the companion billing reconciliation found wrong between local and hosted billing (INT-4).

``CompanionBillingReconcileWorker`` (``app/services/companion_billing_reconcile.py``) compares
each hosted companion line with the local installation that records it and its accrued
``usage_events``. A disagreement is one row here per ``(line_item_id, kind)``: a re-run that
reproduces it moves ``last_seen`` and refreshes both values, and a run that no longer reproduces
it sets ``resolved_at``. A row that reappears after it was resolved is reopened, keeping its
``first_seen``.

``local_value`` and ``hosted_value`` are the two sides as short text (a window, a price, an hour
count, an amount in micro-dollars), so one table holds every kind. A hosted line with no local
installation behind it is logged and counted, never stored: there is no installation to hang the
row on.

The row belongs to its installation (``CASCADE``), so it goes wherever the installation goes,
the tenant purge included.
"""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.companion_installation import CompanionInstallation

FINDING_WINDOW_MISMATCH = "window_mismatch"
FINDING_PRICE_MISMATCH = "price_mismatch"
FINDING_ACCRUAL_GAP = "accrual_gap"
FINDING_AMOUNT_DRIFT = "amount_drift"
FINDING_KINDS = (
    FINDING_WINDOW_MISMATCH,
    FINDING_PRICE_MISMATCH,
    FINDING_ACCRUAL_GAP,
    FINDING_AMOUNT_DRIFT,
)


class BillingReconcileFinding(Base):
    """One open or resolved disagreement between a hosted companion line and local billing."""

    __tablename__ = "billing_reconcile_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    installation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(CompanionInstallation.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    local_value: Mapped[str | None] = mapped_column(String(200))
    hosted_value: Mapped[str | None] = mapped_column(String(200))
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("line_item_id", "kind", name="uq_billing_reconcile_findings_line_kind"),
        CheckConstraint(
            "kind IN ('window_mismatch', 'price_mismatch', 'accrual_gap', 'amount_drift')",
            name="ck_billing_reconcile_findings_kind",
        ),
        # The admin read: open findings, most recently seen first.
        Index(
            "ix_billing_reconcile_findings_open",
            "last_seen",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )
