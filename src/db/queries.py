"""Tenant-scoped query helpers.

Every data query goes through here. No raw select(Model) in route handlers.
All queries filter by user_id — accounts and transactions are scoped
transitively through BankConnection.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.db.models import Account, Balance, BankConnection, SyncJob, SyncStep, Transaction


async def list_connections(db: AsyncSession, user_id: str) -> list[BankConnection]:
    result = await db.execute(
        select(BankConnection)
        .where(BankConnection.user_id == user_id)
        .order_by(BankConnection.created_at.desc())
    )
    return list(result.scalars().all())


async def get_connection(
    db: AsyncSession, connection_id: str, user_id: str
) -> BankConnection | None:
    result = await db.execute(
        select(BankConnection).where(
            BankConnection.id == connection_id,
            BankConnection.user_id == user_id,
        )
    )
    return result.scalars().first()


async def list_jobs(
    db: AsyncSession, user_id: str, limit: int = 20, offset: int = 0
) -> list[SyncJob]:
    result = await db.execute(
        select(SyncJob)
        .join(BankConnection)
        .where(BankConnection.user_id == user_id)
        .order_by(SyncJob.created_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_job(db: AsyncSession, job_id: str, user_id: str) -> SyncJob | None:
    result = await db.execute(
        select(SyncJob)
        .join(BankConnection)
        .where(SyncJob.id == job_id, BankConnection.user_id == user_id)
    )
    return result.scalars().first()


async def get_job_steps(db: AsyncSession, job_id: str, user_id: str) -> list[SyncStep]:
    result = await db.execute(
        select(SyncStep)
        .join(SyncJob)
        .join(BankConnection)
        .where(SyncStep.job_id == job_id, BankConnection.user_id == user_id)
        .order_by(SyncStep.created_at)
    )
    return list(result.scalars().all())


async def list_accounts(db: AsyncSession, user_id: str) -> list[Account]:
    result = await db.execute(
        select(Account)
        .join(BankConnection)
        .where(BankConnection.user_id == user_id)
        .order_by(Account.created_at)
    )
    return list(result.scalars().all())


async def get_account(db: AsyncSession, account_id: str, user_id: str) -> Account | None:
    result = await db.execute(
        select(Account)
        .join(BankConnection)
        .where(Account.id == account_id, BankConnection.user_id == user_id)
    )
    return result.scalars().first()


async def list_balances(
    db: AsyncSession, account_id: str, user_id: str, limit: int = 50, offset: int = 0
) -> list[Balance]:
    result = await db.execute(
        select(Balance)
        .join(Account)
        .join(BankConnection)
        .where(Balance.account_id == account_id, BankConnection.user_id == user_id)
        .order_by(Balance.captured_at.desc())
        .offset(offset)
        .limit(limit)
    )
    return list(result.scalars().all())


async def delete_connection(db: AsyncSession, connection_id: str, user_id: str) -> bool:
    result = await db.execute(
        select(BankConnection)
        .where(BankConnection.id == connection_id, BankConnection.user_id == user_id)
        .options(
            selectinload(BankConnection.accounts).selectinload(Account.transactions),
            selectinload(BankConnection.accounts).selectinload(Account.balances),
            selectinload(BankConnection.sync_jobs).selectinload(SyncJob.steps),
            selectinload(BankConnection.sync_jobs).selectinload(SyncJob.account_results),
        )
    )
    conn = result.scalars().first()
    if not conn:
        return False
    await db.delete(conn)
    return True


async def list_transactions(
    db: AsyncSession,
    user_id: str,
    account_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    stmt = (
        select(Transaction)
        .join(Account)
        .join(BankConnection)
        .where(BankConnection.user_id == user_id)
        .order_by(Transaction.posted_at.desc())
        .offset(offset)
        .limit(limit)
    )
    if account_id:
        stmt = stmt.where(Transaction.account_id == account_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())
