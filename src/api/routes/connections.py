from fastapi import APIRouter, Depends, HTTPException

from src.api.auth import TenantContext, get_tenant
from src.api.schemas import ConnectionResponse, CreateConnectionRequest
from src.core.operations import find_or_create_connection
from src.db import queries
from src.db.session import get_session

router = APIRouter(tags=["connections"])


@router.post("/connections", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    req: CreateConnectionRequest, tenant: TenantContext = Depends(get_tenant)
) -> ConnectionResponse:
    connection_id, _ = await find_or_create_connection(
        tenant.user_id, req.bank_url, req.username, req.password, req.otp_mode, req.otp
    )
    async with get_session() as db:
        conn = await queries.get_connection(db, connection_id, tenant.user_id)
    if not conn:
        raise HTTPException(500, "Connection created but not found")
    return ConnectionResponse(
        id=conn.id,
        bank_slug=conn.bank_slug,
        bank_name=conn.bank_name,
        login_url=conn.login_url,
        otp_mode=conn.otp_mode,
        last_synced_at=conn.last_synced_at,
        created_at=conn.created_at,
    )


@router.get("/connections", response_model=list[ConnectionResponse])
async def list_connections(tenant: TenantContext = Depends(get_tenant)) -> list[ConnectionResponse]:
    async with get_session() as db:
        conns = await queries.list_connections(db, tenant.user_id)
    return [
        ConnectionResponse(
            id=c.id,
            bank_slug=c.bank_slug,
            bank_name=c.bank_name,
            login_url=c.login_url,
            otp_mode=c.otp_mode,
            last_synced_at=c.last_synced_at,
            created_at=c.created_at,
        )
        for c in conns
    ]
