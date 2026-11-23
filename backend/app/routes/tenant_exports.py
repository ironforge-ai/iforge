"""The tenant's own exports (plan 06-19, D-31, APP-07).

The same durable job as the operator's export (`routes/admin.py`, ACCESS-04), started by the tenant
from the dashboard -- the uninstall dialog's "Export data first" offer starts exactly this job.
When the tenant has a live companion installation, the job also promises that companion's database
and media (`services/tenant_export.py`), so it waits for the runtime's upload and fails by name
rather than completing without them.

**Scoping (T-06-19-03).** Every route is scoped to the caller: a job is loaded by its id AND the
caller's user id in one query, so another tenant's job is the same 404 as one that does not exist.

**No vault values, ever.** The create body has no field at all: the self-service archive is never
a credentials bundle (D-10). Only the operator's audited path can opt in to vault values.

**Why ``require_web_auth``.** Starting an export is a human act in the browser; an agent's API key
must not be able to mint an archive of its owner's data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import AuthContext, require_web_auth
from app.core.database import get_session
from app.models.tenant_export import (
    TENANT_EXPORT_STATUS_COMPLETE,
    TENANT_EXPORT_STATUS_QUEUED,
    TenantExportJob,
)
from app.schemas.admin_export import TenantExportStatus
from app.schemas.tenant_exports import (
    TenantExportCreatedResponse,
    TenantExportDownloadResponse,
    TenantExportListItem,
    TenantExportListResponse,
)
from app.services.audit import record_control_plane_audit
from app.services.file_store import get_file_store
from app.services.tenant_export import (
    EXPORT_PRESIGNED_TTL_SECONDS,
    companion_components,
    create_tenant_export_job,
)

router = APIRouter(prefix="/me/exports", tags=["me"])


@router.post(
    "",
    response_model=TenantExportCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_tenant_export(
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> TenantExportCreatedResponse:
    """Queue an export of the caller's own data, companions included (D-31)."""
    job = await create_tenant_export_job(db, user_id=auth.user_id, include_vault_values=False)
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="tenant.export_requested",
        resource_type="tenant_export_job",
        resource_id=str(job.id),
        source="api.me",
        details={
            "job_id": str(job.id),
            "include_vault_values": False,
            "companion_installations": [
                str(component.get("installation_id"))
                for component in companion_components(job.manifest)
            ],
        },
    )
    await db.commit()
    return TenantExportCreatedResponse(job_id=job.id, status=TENANT_EXPORT_STATUS_QUEUED)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def _downloadable(job: TenantExportJob) -> bool:
    """The one predicate behind the list's flag and the download route, as in the admin API."""
    return job.status == TENANT_EXPORT_STATUS_COMPLETE and job.artifact_key is not None


@router.get("", response_model=TenantExportListResponse)
async def list_tenant_exports(
    response: Response,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> TenantExportListResponse:
    """The caller's own export jobs, newest first. A pure read: it presigns nothing."""
    _no_store(response)
    jobs = (
        await db.scalars(
            select(TenantExportJob)
            .where(TenantExportJob.user_id == auth.user_id)
            .order_by(TenantExportJob.created_at.desc(), TenantExportJob.id.desc())
        )
    ).all()
    return TenantExportListResponse(
        items=[
            TenantExportListItem(
                job_id=job.id,
                status=cast(TenantExportStatus, job.status),
                created_at=job.created_at,
                completed_at=job.completed_at,
                downloadable=_downloadable(job),
                error=job.error,
            )
            for job in jobs
        ]
    )


@router.get("/{job_id}/download", response_model=TenantExportDownloadResponse)
async def download_tenant_export(
    job_id: UUID,
    response: Response,
    auth: AuthContext = Depends(require_web_auth),
    db: AsyncSession = Depends(get_session),
) -> TenantExportDownloadResponse:
    """Mint one presigned link to one of the caller's own completed archives (T-06-19-03).

    The job is loaded by its id AND the caller's user id in one query, so another tenant's job is
    the same 404 as a missing one. A job that is not complete, or has no artifact, is refused with
    409 rather than answered with a null link. Each call writes one ``tenant.export_link_issued``
    audit row carrying the key and TTL, never the URL; the response is ``no-store`` because the
    link is bearer-equivalent for its whole life.
    """
    _no_store(response)
    job = (
        await db.scalars(
            select(TenantExportJob).where(
                TenantExportJob.id == job_id, TenantExportJob.user_id == auth.user_id
            )
        )
    ).one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export job not found")
    if not _downloadable(job):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"code": "export_not_downloadable", "status": job.status},
        )
    assert job.artifact_key is not None  # narrowed by _downloadable
    issued_at = datetime.now(UTC)
    url = await get_file_store().presigned_get_url(job.artifact_key, EXPORT_PRESIGNED_TTL_SECONDS)
    record_control_plane_audit(
        db,
        actor_type="user",
        actor_user_id=auth.user_id,
        target_user_id=auth.user_id,
        action="tenant.export_link_issued",
        resource_type="tenant_export_job",
        resource_id=str(job.id),
        source="api.me",
        details={
            "job_id": str(job.id),
            "artifact_key": job.artifact_key,
            "ttl_seconds": EXPORT_PRESIGNED_TTL_SECONDS,
        },
    )
    await db.commit()
    return TenantExportDownloadResponse(
        url=url, expires_at=issued_at + timedelta(seconds=EXPORT_PRESIGNED_TTL_SECONDS)
    )
