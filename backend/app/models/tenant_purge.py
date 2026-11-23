"""Durable grace-window tenant purge row (ACCESS-04, D-15/D-16).

One row per operator-triggered deletion. Scheduled at trigger time for
``now() + tenant_purge_grace_days``; cancellable while pending; claimed
with the same claim-id fencing shape as the principal-lifecycle cleanup
and the tenant export job so an interrupted purge remains durable retry
work.

``subject`` / ``issuer`` are captured at schedule time because the
``users`` row itself dies in the purge — the provider identity delete
and deployment teardown calls must survive that. ``user_id`` is a plain
UUID (no FK) for the same reason: the value must outlive the user row
as the purge's audit anchor. The row itself is a retained tombstone
(PURGE_COVERAGE disposition "tombstone").
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

TENANT_PURGE_STATUS_PENDING = "pending"
TENANT_PURGE_STATUS_PURGING = "purging"
TENANT_PURGE_STATUS_COMPLETE = "complete"
TENANT_PURGE_STATUS_FAILED = "failed"
TENANT_PURGE_STATUSES = (
    TENANT_PURGE_STATUS_PENDING,
    TENANT_PURGE_STATUS_PURGING,
    TENANT_PURGE_STATUS_COMPLETE,
    TENANT_PURGE_STATUS_FAILED,
)


class TenantPurge(Base, TimestampMixin):
    __tablename__ = "tenant_purges"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'purging', 'complete', 'failed')",
            name="ck_tenant_purges_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_tenant_purges_attempt_count",
        ),
        # At most one open (non-complete) purge per lifecycle — the DB
        # backstop for the endpoint's application-level idempotency check.
        Index(
            "uq_tenant_purges_lifecycle_open",
            "lifecycle_id",
            unique=True,
            postgresql_where=text("status != 'complete'"),
        ),
        # Claim scan support: due pending/failed rows plus stale claims.
        Index(
            "ix_tenant_purges_due",
            "status",
            "purge_after",
            "next_attempt_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    lifecycle_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("principal_lifecycles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    # Plain UUID by design — see module docstring.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    purge_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TENANT_PURGE_STATUS_PENDING,
        server_default=TENANT_PURGE_STATUS_PENDING,
    )
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    error: Mapped[str | None] = mapped_column(String(1000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
