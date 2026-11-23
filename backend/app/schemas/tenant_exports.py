"""Schemas for the tenant's own exports and the runtime's companion upload (plan 06-19, D-31).

`/v1/me/exports` is the tenant's self-service half of the Phase 3 export (ACCESS-04): the same
job, the same archive, the same states. It never carries vault values -- there is no field that
could ask for them -- because a self-service archive must never become a credentials bundle.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from app.schemas.admin_export import TenantExportStatus


class TenantExportCreatedResponse(BaseModel):
    """Body of `POST /v1/me/exports`: the queued job the tenant polls."""

    job_id: UUID
    status: TenantExportStatus


class TenantExportListItem(BaseModel):
    """One of the caller's own export jobs: the admin list's shape, less the vault flag."""

    job_id: UUID
    status: TenantExportStatus
    created_at: datetime
    completed_at: datetime | None
    # True exactly when the download route will answer with a link rather than refuse.
    downloadable: bool
    # The named reason a failed job gave (e.g. `companion_export_component_timeout: ...`).
    error: str | None


class TenantExportListResponse(BaseModel):
    items: list[TenantExportListItem]


class TenantExportDownloadResponse(BaseModel):
    """Body of `GET /v1/me/exports/{job_id}/download`: one presigned link and when it expires."""

    url: str
    expires_at: datetime


class CompanionExportUploadResponse(BaseModel):
    """Body of `PUT /v1/runtime/companion-installations/{id}/exports/{job_id}`."""

    installation_id: UUID
    job_id: UUID
    state: Literal["uploaded"]
    bytes: int
