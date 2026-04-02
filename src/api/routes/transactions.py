from fastapi import APIRouter, Depends, Query

from src.api.auth import TenantContext, get_tenant
from src.api.schemas import TransactionResponse
from src.db import queries
from src.db.session import get_session

router = APIRouter(tags=["transactions"])

MAX_LIMIT = 200


@router.get("/transactions", response_model=list[TransactionResponse])
async def list_transactions(
    account_id: str | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    tenant: TenantContext = Depends(get_tenant),
) -> list[TransactionResponse]:
    async with get_session() as db:
        txn_list = await queries.list_transactions(db, tenant.user_id, account_id, limit, offset)
    return [
        TransactionResponse(
            id=t.id,
            external_id=t.external_id,
            posted_at=t.posted_at,
            description=t.description,
            amount=t.amount,
            currency=t.currency,
            running_balance=t.running_balance,
        )
        for t in txn_list
    ]
