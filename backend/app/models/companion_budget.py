"""The tenant's monthly companion spend ceiling (OPS-06, D-20).

**A ledger of its own, deliberately NOT a fourth cap unit.** Phase 5's blast-radius caps
(``app/core/companion_caps.CAP_UNITS``) count calls, records and bytes, and none of them is
denominated in money. Folding a money amount into that vector would make "how much may one call
touch" and "how much may this tenant spend this month" the same kind of number. They are not: the
first bounds what a single call can do, the second bounds a bill. So the ceiling lives here, one
row per tenant, and ``app/services/companion_budget.py`` compares it with the month's recorded
spend in ``usage_events``.

**One row per tenant, keyed by the tenant.** ``user_id`` is the primary key, so two ceilings for
one tenant cannot exist and the lookup is a primary-key read on every agent tool call. No row means
the tenant has not set a ceiling. That is a different statement from a ceiling of zero, which is a
VALUE meaning "refuse every agent call", exactly as a zero cap is in Phase 5.

**Integer micro-dollars**, the representation every recorded cost in ``usage_events`` already uses
(D-17), so the comparison is integer arithmetic and never a float.

``updated_by_user_id`` records who last set it. It is ``SET NULL`` rather than ``CASCADE``, so
the ceiling survives the removal of whoever last changed it.

**The repeated-call breaker's counters live here too (plan 06-23, D-22).** The ceiling above is the
SLOW half of OPS-06: it only knows what the metering worker has already recorded. The breaker is the
fast half. It counts how often the same call (Phase 5's canonical action hash plus the dry-run flag)
arrives inside a short window, and needs no cost data at all. It is held in PostgreSQL rather than
in the process-wide rate limiter because a loop spread across workers must not get one allowance
per worker. ``app/services/companion_ratelimit.check_repeated_call`` is the only writer.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.user import User


class CompanionSpendCeiling(Base, TimestampMixin):
    """One tenant's monthly ceiling on recorded companion spend, in integer micro-dollars."""

    __tablename__ = "companion_spend_ceilings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="CASCADE"),
        primary_key=True,
    )
    ceiling_usd_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        # Negative would be a ceiling already exceeded before anything was spent, which is not a
        # setting anybody means; the route refuses it with a 422 first, and this is the backstop.
        CheckConstraint(
            "ceiling_usd_micros >= 0",
            name="ck_companion_spend_ceilings_non_negative",
        ),
    )


class CompanionRepeatBreaker(Base, TimestampMixin):
    """How often one tenant's same call has arrived in the current window, and any cooldown.

    One row per (tenant, companion, verb, canonical action, dry-run flag). The unique constraint is
    the conflict target of the single ``INSERT ... ON CONFLICT DO UPDATE`` that counts a call, so
    two workers counting the same call at the same moment serialize on this row and neither
    increment is lost.

    ``action_sha256`` is ``companion_dispatch.canonical_action_hash`` of the call, the digest the
    approval gate binds to. The tenant and the agent are inside that digest as well as in
    ``user_id``: the column is what scopes the row, and the digest is what says "the same call".
    ``dry_run`` is kept apart from the digest because the digest deliberately strips it, and a
    preview and the real call it previews are two different things to count.

    ``tripped_until`` is null until the breaker trips. While it is in the future the count is not
    reset, so a loop that keeps calling through its cooldown keeps being stopped.
    """

    __tablename__ = "companion_repeat_breakers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(User.id, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The same widths `companion_tool_calls` uses for these two columns.
    companion_key: Mapped[str] = mapped_column(String(31), nullable=False)
    verb: Mapped[str] = mapped_column(String(61), nullable=False)
    action_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tripped_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "companion_key",
            "verb",
            "action_sha256",
            "dry_run",
            name="uq_companion_repeat_breakers_call",
        ),
        CheckConstraint("count >= 0", name="ck_companion_repeat_breakers_count_non_negative"),
    )


__all__ = ["CompanionRepeatBreaker", "CompanionSpendCeiling"]
