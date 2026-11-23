"""Durable DB-backed tenant export job (ACCESS-04, D-12).

One row per operator-triggered export. Claim columns mirror the
`principal_lifecycles` cleanup shape (claim id fencing + lease-based
stale-claim recovery + failure backoff) so an interrupted or failed
export remains durable retry work rather than being lost.
"""

import uuid
from datetime import datetime

from pydantic import JsonValue
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

TENANT_EXPORT_STATUS_QUEUED = "queued"
TENANT_EXPORT_STATUS_RUNNING = "running"
TENANT_EXPORT_STATUS_COMPLETE = "complete"
TENANT_EXPORT_STATUS_FAILED = "failed"
TENANT_EXPORT_STATUSES = (
    TENANT_EXPORT_STATUS_QUEUED,
    TENANT_EXPORT_STATUS_RUNNING,
    TENANT_EXPORT_STATUS_COMPLETE,
    TENANT_EXPORT_STATUS_FAILED,
)


class TenantExportJob(Base, TimestampMixin):
    __tablename__ = "tenant_export_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'complete', 'failed')",
            name="ck_tenant_export_jobs_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_tenant_export_jobs_attempt_count",
        ),
        # Claim scan support: due queued/failed rows plus stale running claims.
        Index(
            "ix_tenant_export_jobs_due",
            "status",
            "next_attempt_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    include_vault_values: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=TENANT_EXPORT_STATUS_QUEUED,
        server_default=TENANT_EXPORT_STATUS_QUEUED,
    )
    artifact_key: Mapped[str | None] = mapped_column(String(800))
    manifest: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB(none_as_null=True))
    error: Mapped[str | None] = mapped_column(String(1000))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
