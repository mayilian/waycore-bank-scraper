from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.auth import TenantContext, get_tenant
from src.api.schemas import AccountResponse, BalanceResponse
from src.db import queries
from src.db.session import get_session

router = APIRouter(tags=["accounts"])

MAX_BALANCE_LIMIT = 100


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


@router.get("/accounts/{account_id}", response_model=AccountResponse)
async def get_account(
    account_id: str, tenant: TenantContext = Depends(get_tenant)
) -> AccountResponse:
    async with get_session() as db:
        account = await queries.get_account(db, account_id, tenant.user_id)
    if not account:
        raise HTTPException(404, "Account not found")
    return AccountResponse(
        id=account.id,
        external_id=account.external_id,
        name=account.name,
        account_type=account.account_type,
        currency=account.currency,
    )


@router.get("/accounts/{account_id}/balances", response_model=list[BalanceResponse])
async def list_balances(
    account_id: str,
    limit: int = Query(default=50, ge=1, le=MAX_BALANCE_LIMIT),
    offset: int = Query(default=0, ge=0),
    tenant: TenantContext = Depends(get_tenant),
) -> list[BalanceResponse]:
    async with get_session() as db:
        account = await queries.get_account(db, account_id, tenant.user_id)
        if not account:
            raise HTTPException(404, "Account not found")
        balance_list = await queries.list_balances(db, account_id, tenant.user_id, limit, offset)
    return [
        BalanceResponse(
            id=b.id,
            available=b.available,
            current=b.current,
            currency=b.currency,
            captured_at=b.captured_at,
        )
        for b in balance_list
    ]
