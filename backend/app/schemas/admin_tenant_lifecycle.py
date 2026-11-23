"""Admin tenant deletion / purge lifecycle schemas (ACCESS-04, D-14/D-15).

Interface contract established for 03-09 (round-trip) and 03-10 (admin UI):

    POST /admin/tenants/{user_id}/delete       -> AdminTenantDeleteResponse
    POST /admin/tenants/{user_id}/purge/cancel -> AdminPurgeCancelResponse
    GET  /admin/tenants/{user_id}/lifecycle    -> AdminTenantLifecycleResponse
    PUT  /admin/auth/billing-exemptions        -> AdminBillingExemptionResponse
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.admin import AdminClerkId

TenantPurgeStatus = Literal["pending", "purging", "complete", "failed"]


class AdminTenantDeleteResponse(BaseModel):
    """Fence + identity delete done, purge scheduled."""

    lifecycle_id: UUID
    purge_after: datetime


class AdminPurgeCancelResponse(BaseModel):
    canceled: bool


class AdminTenantPurgeInfo(BaseModel):
    """What the purge is scheduled to do, and what it has already done.

    `error` / `attempt_count` / `claimed_at` are read straight off the
    `TenantPurge` row, which has always carried them. Without them the 03-10
    deletion panel labelled a FAILED purge "Purge scheduled" against a date
    already in the past, because `status` + `purge_after` + `completed_at`
    was everything the API would tell it — "what already ran" was answerable
    only by reading the audit log (W-01 / T-03-56).
    """

    status: TenantPurgeStatus
    purge_after: datetime
    completed_at: datetime | None = None
    # Last recorded sweep failure; None until an attempt fails.
    error: str | None = None
    # 0 until the worker claims the row for the first time.
    attempt_count: int = 0
    # When the most recent attempt was claimed; None for a never-claimed row.
    claimed_at: datetime | None = None


class AdminTenantLifecycleResponse(BaseModel):
    """Status view for the 03-10 deletion panel."""

    fenced: bool
    purge: AdminTenantPurgeInfo | None = None
    # The LIVE `settings.tenant_purge_grace_days`, not a constant. The window
    # is a server setting the client cannot know, and it is rendered inside
    # the typed-confirmation dialog — the last text an operator reads before
    # authorising an irreversible action — so the console must never assert
    # its own number (CR-02 / WR-13).
    grace_days: int


class AdminBillingExemptionUpdate(BaseModel):
    """Grant or revoke a Clerk user's billing exemption (06.3 WR-02)."""

    model_config = ConfigDict(extra="forbid")

    target_clerk_id: AdminClerkId
    exempt: bool
    reason: str = Field(min_length=1, max_length=191)

    @field_validator("reason")
    @classmethod
    def _validate_reason(cls, value: str) -> str:
        if value != value.strip() or not value.isprintable():
            raise ValueError("reason must be a printable canonical value")
        return value


class AdminBillingExemptionResponse(BaseModel):
    target_clerk_id: str
    user_id: UUID
    exempt: bool
    exempted_at: datetime | None
    changed: bool
