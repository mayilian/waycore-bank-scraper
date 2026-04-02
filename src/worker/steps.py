"""Restate workflow step implementations.

Step boundaries are aligned with browser session economics:
  - step_login: Opens browser #1 — login, OTP, capture session cookies.
  - step_extract_all: Opens browser #2 — restores session, discovers accounts,
    extracts transactions + balance for ALL accounts in one browser session.
  - step_finalise: No browser — marks job complete.

This design gives 2 browser launches per sync (not N+2), while keeping
login separate for OTP webhook pause/resume.

IMPORTANT: These functions must not call back into the Restate context.
They are plain async functions — Restate calls them as durable side effects.
"""

import traceback
import uuid
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import Page, StorageState
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from src.adapters import get_adapter
from src.adapters.base import AccountData, BalanceData, TransactionData
from src.core.crypto import decrypt
from src.core.logging import get_logger
from src.core.screenshots import get_screenshot_store
from src.core.stealth import stealth_browser
from src.db.models import (
    Account,
    AccountSyncResult,
    Balance,
    BankConnection,
    SyncJob,
    SyncStep,
    Transaction,
)
from src.db.session import get_session

log = get_logger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────


async def _write_step(
    job_id: str,
    name: str,
    status: str,
    output: dict[str, Any] | None = None,
    screenshot_path: str | None = None,
    started_at: datetime | None = None,
) -> None:
    async with get_session() as db:
        step = SyncStep(
            id=str(uuid.uuid4()),
            job_id=job_id,
            name=name,
            status=status,
            output=output,
            screenshot_path=screenshot_path,
            started_at=started_at or datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )
        db.add(step)


async def _capture_failure(
    page: Page, job_id: str, step_name: str, exc: Exception, started_at: datetime
) -> None:
    screenshot_path: str | None = None
    try:
        png = await page.screenshot(type="png", full_page=True)
        store = get_screenshot_store()
        screenshot_path = await store.save(job_id, step_name, png)
    except Exception:
        log.warning("step.screenshot_failed", job_id=job_id, step=step_name)

    await _write_step(
        job_id=job_id,
        name=step_name,
        status="failed",
        output={"error": str(exc), "traceback": traceback.format_exc()},
        screenshot_path=screenshot_path,
        started_at=started_at,
    )


# ── Step: login ────────────────────────────────────────────────────────────────


async def step_login(connection_id: str, job_id: str, otp: str | None) -> dict[str, Any]:
    """Navigate to the bank, log in, handle OTP if provided.
    Returns {storage_state, post_login_url, bank_slug} for subsequent steps.
    """
    started_at = datetime.now(UTC)
    log.info("step.login.start", job_id=job_id)

    async with get_session() as db:
        conn = await db.get(BankConnection, connection_id)
        if not conn:
            raise ValueError(f"BankConnection {connection_id} not found")
        login_url = conn.login_url
        bank_slug = conn.bank_slug
        username = decrypt(conn.username_enc)
        password = decrypt(conn.password_enc)
        if otp is None and conn.otp_mode in ("static", "totp") and conn.otp_value_enc:
            otp = decrypt(conn.otp_value_enc)

    adapter = get_adapter(bank_slug)
    adapter.job_id = job_id

    async with stealth_browser(policy=adapter.browser_policy) as (_browser, page):
        try:
            await page.goto(login_url, wait_until="networkidle", timeout=20_000)

            await adapter.navigate_to_login(page)
            await adapter.fill_and_submit_credentials(page, username, password)

            if await adapter.is_otp_required(page):
                if not otp:
                    raise ValueError("OTP required but not provided")
                await adapter.submit_otp(page, otp)

            state: StorageState = await page.context.storage_state()
            post_login_url = page.url
        except Exception as exc:
            await _capture_failure(page, job_id, "login", exc, started_at)
            raise

    await _write_step(
        job_id,
        "login",
        "success",
        output={"cookies": len(state.get("cookies", []))},
        started_at=started_at,
    )
    log.info("step.login.success", job_id=job_id)
    return {"storage_state": state, "post_login_url": post_login_url, "bank_slug": bank_slug}


# ── Step: extract_all ─────────────────────────────────────────────────────────
# One browser session for the entire extraction phase:
#   restore session → discover accounts → extract each account sequentially.
# This eliminates N-1 browser launches and session restores.


async def step_extract_all(
    connection_id: str,
    job_id: str,
    session_state: StorageState,
    post_login_url: str,
    bank_slug: str,
) -> dict[str, Any]:
    """Extract all accounts, transactions, and balances in a single browser session.

    Returns {accounts: [...], results: [...], errors: [...]}.
    Persists accounts, transactions, balances, and AccountSyncResult rows.
    """
    started_at = datetime.now(UTC)
    log.info("step.extract_all.start", job_id=job_id)

    adapter = get_adapter(bank_slug)
    adapter.job_id = job_id

    async with stealth_browser(storage_state=session_state, policy=adapter.browser_policy) as (
        _browser,
        page,
    ):
        try:
            await page.goto(post_login_url, wait_until="networkidle", timeout=20_000)
            accounts, results = await adapter.extract_all(page, post_login_url)
        except Exception as exc:
            await _capture_failure(page, job_id, "extract_all", exc, started_at)
            raise

    if not accounts:
        raise RuntimeError("No accounts found — expected at least one account after login")

    # Persist everything to DB
    account_dicts = await _persist_accounts(connection_id, job_id, accounts)
    account_errors: list[str] = []

    for result in results:
        db_id = account_dicts.get(result.account.external_id)
        if not db_id:
            continue

        if result.error:
            account_errors.append(f"{result.account.external_id}: {result.error}")
            await _write_account_result(
                job_id, db_id, "failed", error=result.error, started_at=started_at
            )
            await _write_step(
                job_id,
                f"extract_{result.account.external_id}",
                "failed",
                output={"error": result.error},
                started_at=started_at,
            )
            continue

        inserted = await _persist_transactions(job_id, db_id, result.transactions)
        await _persist_balance(db_id, result.balance)
        await _write_account_result(
            job_id,
            db_id,
            "success",
            transactions_found=len(result.transactions),
            transactions_inserted=inserted,
            balance_captured=True,
            started_at=started_at,
        )

        # Write per-account step for audit trail
        await _write_step(
            job_id,
            f"extract_{result.account.external_id}",
            "success",
            output={
                "transactions_total": len(result.transactions),
                "transactions_inserted": inserted,
                "balance_current": str(result.balance.current),
                "balance_currency": result.balance.currency,
            },
            started_at=started_at,
        )
        log.info(
            "step.extract_account.success",
            job_id=job_id,
            account=result.account.external_id,
            transactions_inserted=inserted,
            balance=str(result.balance.current),
        )

    # Write the extract_all step
    await _write_step(
        job_id,
        "extract_all",
        "success" if not account_errors else "partial",
        output={
            "accounts_found": len(accounts),
            "accounts_extracted": len(accounts) - len(account_errors),
            "errors": account_errors,
        },
        started_at=started_at,
    )
    log.info(
        "step.extract_all.success",
        job_id=job_id,
        accounts=len(accounts),
        errors=len(account_errors),
    )
    return {
        "accounts_found": len(accounts),
        "accounts_extracted": len(accounts) - len(account_errors),
        "errors": account_errors,
    }


# ── DB persistence helpers ────────────────────────────────────────────────────


async def _persist_accounts(
    connection_id: str, job_id: str, accounts: list[AccountData]
) -> dict[str, str]:
    """Persist discovered accounts and return {external_id: db_id} mapping.

    Uses ON CONFLICT DO UPDATE to handle concurrent syncs for the same connection.
    """
    account_map: dict[str, str] = {}

    async with get_session() as db:
        for acc_data in accounts:
            db_id = str(uuid.uuid4())
            stmt = (
                pg_insert(Account)
                .values(
                    id=db_id,
                    connection_id=connection_id,
                    external_id=acc_data.external_id,
                    name=acc_data.name,
                    account_type=acc_data.account_type,
                    currency=acc_data.currency,
                )
                .on_conflict_do_update(
                    index_elements=["connection_id", "external_id"],
                    set_={"name": acc_data.name, "account_type": acc_data.account_type},
                )
                .returning(Account.id)
            )
            result = await db.execute(stmt)
            account_map[acc_data.external_id] = result.scalar_one()

        job = await db.get(SyncJob, job_id)
        if job:
            job.accounts_synced = len(accounts)

    return account_map


async def _persist_transactions(
    job_id: str, account_db_id: str, transactions: list[TransactionData]
) -> int:
    """Batch-insert transactions with ON CONFLICT DO NOTHING. Returns inserted count."""
    if not transactions:
        return 0

    rows = [
        {
            "id": str(uuid.uuid4()),
            "account_id": account_db_id,
            "external_id": txn.external_id,
            "posted_at": txn.posted_at,
            "description": txn.description,
            "amount": txn.amount,
            "currency": txn.currency,
            "running_balance": txn.running_balance,
            "raw": txn.raw,
        }
        for txn in transactions
    ]
    async with get_session() as db:
        stmt = (
            pg_insert(Transaction)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["account_id", "external_id"])
        )
        cursor_result: CursorResult[Any] = await db.execute(stmt)  # type: ignore[assignment]
        inserted = cursor_result.rowcount

        job = await db.get(SyncJob, job_id)
        if job:
            job.transactions_synced = (job.transactions_synced or 0) + inserted

    return inserted


async def _persist_balance(account_db_id: str, balance: BalanceData) -> None:
    """Append a balance snapshot."""
    async with get_session() as db:
        db.add(
            Balance(
                id=str(uuid.uuid4()),
                account_id=account_db_id,
                available=balance.available,
                current=balance.current,
                currency=balance.currency,
                captured_at=balance.captured_at,
            )
        )


async def _write_account_result(
    job_id: str,
    account_id: str,
    status: str,
    *,
    transactions_found: int = 0,
    transactions_inserted: int = 0,
    balance_captured: bool = False,
    error: str | None = None,
    started_at: datetime | None = None,
) -> None:
    async with get_session() as db:
        db.add(
            AccountSyncResult(
                id=str(uuid.uuid4()),
                job_id=job_id,
                account_id=account_id,
                status=status,
                transactions_found=transactions_found,
                transactions_inserted=transactions_inserted,
                balance_captured=balance_captured,
                error=error,
                started_at=started_at or datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )


# ── Step: finalise ─────────────────────────────────────────────────────────────


async def step_finalise(job_id: str, status: str = "success") -> None:
    started_at = datetime.now(UTC)
    async with get_session() as db:
        job = await db.get(SyncJob, job_id)
        if job:
            job.status = status
            job.completed_at = datetime.now(UTC)
            conn = await db.get(BankConnection, job.connection_id)
            if conn:
                conn.last_synced_at = datetime.now(UTC)
    await _write_step(job_id, "finalise", "success", started_at=started_at)
    log.info("step.finalise.success", job_id=job_id, status=status)
