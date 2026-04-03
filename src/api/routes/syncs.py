from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.auth import TenantContext, get_tenant
from src.api.schemas import (
    JobDetailResponse,
    JobResponse,
    OtpRequest,
    StepResponse,
    TriggerSyncRequest,
    TriggerSyncResponse,
)
from src.db import queries
from src.db.session import get_session
from src.services.operations import provide_otp, trigger_sync

router = APIRouter(tags=["syncs"])


@router.post(
    "/connections/{connection_id}/sync", response_model=TriggerSyncResponse, status_code=202
)
async def start_sync(
    connection_id: str,
    req: TriggerSyncRequest,
    tenant: TenantContext = Depends(get_tenant),
) -> TriggerSyncResponse:
    async with get_session() as db:
        conn = await queries.get_connection(db, connection_id, tenant.user_id)
    if not conn:
        raise HTTPException(404, "Connection not found")

    # Static/TOTP OTP can come from the request OR from stored credentials on the
    # connection (conn.otp_value_enc). Only reject if neither source has a value.
    if req.otp_mode == "static" and not req.otp and not conn.otp_value_enc:
        raise HTTPException(422, "OTP is required when otp_mode is 'static' and no OTP is stored on the connection")

    job_id = await trigger_sync(connection_id, req.otp_mode)
    return TriggerSyncResponse(job_id=job_id)


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: TenantContext = Depends(get_tenant),
) -> list[JobResponse]:
    async with get_session() as db:
        job_list = await queries.list_jobs(db, tenant.user_id, limit, offset)
    return [
        JobResponse(
            id=j.id,
            status=j.status,
            accounts_synced=j.accounts_synced,
            transactions_synced=j.transactions_synced,
            failure_reason=j.failure_reason,
            started_at=j.started_at,
            completed_at=j.completed_at,
            created_at=j.created_at,
        )
        for j in job_list
    ]


@router.get("/jobs/{job_id}", response_model=JobDetailResponse)
async def get_job(job_id: str, tenant: TenantContext = Depends(get_tenant)) -> JobDetailResponse:
    async with get_session() as db:
        job = await queries.get_job(db, job_id, tenant.user_id)
        if not job:
            raise HTTPException(404, "Job not found")
        steps = await queries.get_job_steps(db, job_id, tenant.user_id)

    return JobDetailResponse(
        id=job.id,
        status=job.status,
        accounts_synced=job.accounts_synced,
        transactions_synced=job.transactions_synced,
        failure_reason=job.failure_reason,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
        steps=[
            StepResponse(
                name=s.name,
                status=s.status,
                started_at=s.started_at,
                completed_at=s.completed_at,
            )
            for s in steps
        ],
    )


@router.post("/jobs/{job_id}/otp", status_code=202)
async def send_otp(
    job_id: str, req: OtpRequest, tenant: TenantContext = Depends(get_tenant)
) -> dict[str, str]:
    async with get_session() as db:
        job = await queries.get_job(db, job_id, tenant.user_id)
    if not job:
        raise HTTPException(404, "Job not found")
    await provide_otp(job_id, req.code)
    return {"status": "otp_sent"}
