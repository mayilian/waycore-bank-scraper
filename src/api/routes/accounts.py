from fastapi import APIRouter, Depends

from src.api.auth import TenantContext, get_tenant
from src.api.schemas import AccountResponse
from src.db import queries
from src.db.session import get_session

router = APIRouter(tags=["accounts"])


@router.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(tenant: TenantContext = Depends(get_tenant)) -> list[AccountResponse]:
    async with get_session() as db:
        account_list = await queries.list_accounts(db, tenant.user_id)
    return [
        AccountResponse(
            id=a.id,
            external_id=a.external_id,
            name=a.name,
            account_type=a.account_type,
            currency=a.currency,
        )
        for a in account_list
    ]
