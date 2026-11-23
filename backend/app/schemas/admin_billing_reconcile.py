"""Schemas for the admin companion billing reconciliation endpoints (INT-4).

`POST /v1/admin/companions/billing-reconcile` runs one reconciliation pass on demand and
`GET /v1/admin/companions/billing-reconcile/findings` reads the open findings, both behind the
`X-Admin-Key` gate (require_admin_api_key).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

BillingReconcileFindingKind = Literal[
    "window_mismatch", "price_mismatch", "accrual_gap", "amount_drift"
]


class AdminBillingReconcileRunResponse(BaseModel):
    """One pass's counts; `skipped` when `/v2` billing is not configured."""

    skipped: bool
    owners_scanned: int
    owners_failed: int
    lines_seen: int
    mismatches: int
    corrections: int
    findings_resolved: int


class AdminBillingReconcileFinding(BaseModel):
    finding_id: UUID
    user_id: UUID
    installation_id: UUID
    line_item_id: str
    kind: BillingReconcileFindingKind
    local_value: str | None
    hosted_value: str | None
    first_seen: datetime
    last_seen: datetime


class AdminBillingReconcileFindingList(BaseModel):
    items: list[AdminBillingReconcileFinding]
