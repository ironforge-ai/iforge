"""Schemas for admin tenant export endpoints (ACCESS-04, D-13).

`POST/GET /v1/admin/tenants/{user_id}/exports` and the
`GET /v1/admin/users` tenant picker run behind the `X-Admin-Key`
header gate (require_admin_api_key). Kept in a separate file so they
don't pollute user-facing schemas (same split as its sibling
`app/schemas/admin_tenant_lifecycle.py`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

TenantExportStatus = Literal["queued", "running", "complete", "failed"]


class AdminTenantExportCreate(BaseModel):
    """Body for `POST /v1/admin/tenants/{user_id}/exports`."""

    model_config = ConfigDict(extra="forbid")

    # D-10: vault secret values are opt-in only — the routine export
    # artifact is never a credentials bundle.
    include_vault_values: bool = False


class AdminTenantExportCreatedResponse(BaseModel):
    job_id: UUID
    status: TenantExportStatus


class AdminTenantExportListItem(BaseModel):
    job_id: UUID
    status: TenantExportStatus
    created_at: datetime
    completed_at: datetime | None
    include_vault_values: bool
    # ALWAYS null. Retained only so the field does not vanish from a
    # response shape hand-mirrored by the console; minting moved to
    # `GET /v1/admin/tenants/{user_id}/exports/{job_id}/download`
    # (W-03 / WR-02). The list used to presign on read, so the console's
    # 4-second poll manufactured a fresh live credential — and a
    # `tenant.export_link_issued` audit row — for every complete job
    # nobody had asked to download, including `include_vault_values`
    # archives (T-03-64 / T-03-65).
    download_url: None = None
    # True exactly when this job is complete AND carries an artifact key,
    # i.e. when the download endpoint will answer with a URL rather than
    # refuse. A statement about the job, not an instruction to the UI.
    downloadable: bool
    error: str | None


class AdminTenantExportListResponse(BaseModel):
    items: list[AdminTenantExportListItem]


class AdminTenantExportDownloadResponse(BaseModel):
    """Body of `GET /v1/admin/tenants/{user_id}/exports/{job_id}/download`.

    `expires_in_seconds` is the TTL the URL was actually presigned with
    (`EXPORT_PRESIGNED_TTL_SECONDS`), returned so the console can render an
    accurate expiry without mirroring the constant.
    """

    job_id: UUID
    download_url: str
    expires_in_seconds: int


class AdminUserListItem(BaseModel):
    id: UUID
    email: str | None
    # The lookup already matched on `User.clerk_id == term` but never
    # emitted it, leaving the third identifier it accepts unobtainable from
    # the console (G-03-62). ALWAYS PRESENT, sometimes null: the column is
    # `Mapped[str | None]` and the table's check constraint FORCES it NULL
    # for `principal_kind='partner_tenant'` (models/user.py:23-32), so a
    # required `str` would raise a ValidationError for exactly the
    # population W-05/IN-04 reported as unreachable.
    clerk_id: str | None
    name: str | None
    created_at: datetime
    # True when the billing gate lets this user through without a /v2 read:
    # operator-exempted Clerk users, partner tenants and grandfathered rows
    # (`users.invite_exempted_at` set). Drives the admin page's toggle.
    billing_exempt: bool


class AdminUserListResponse(BaseModel):
    items: list[AdminUserListItem]
