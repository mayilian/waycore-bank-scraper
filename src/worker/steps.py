"""Restate workflow step implementations.

Each function is a self-contained unit of work:
  - Opens a fresh browser and restores session cookies
  - Performs one logical phase of the sync
  - Writes sync_steps records (running → success/failed)
  - Saves a screenshot on failure
  - Returns JSON-serializable data for Restate to journal

IMPORTANT: These functions must not call back into the Restate context.
They are plain async functions — Restate calls them as durable side effects.
"""

import traceback
import uuid
from datetime import UTC, datetime

from playwright.async_api import Page
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.adapters import get_adapter
from src.adapters.base import AccountData
from src.core.crypto import decrypt
from src.core.logging import get_logger
from src.core.screenshots import get_screenshot_store
from src.core.stealth import stealth_browser
from src.db.models import Account, Balance, BankConnection, SyncJob, SyncStep, Transaction
from src.db.session import get_session

log = get_logger(__name__)


# ── Shared helpers ─────────────────────────────────────────────────────────────


async def _write_step(
    job_id: str,
    name: str,
    status: str,
    output: dict | None = None,
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
    async with get_session() as db:
        job = await db.get(SyncJob, job_id)
        if job:
            job.status = "failed"
            job.failure_reason = str(exc)
            job.completed_at = datetime.now(UTC)


# ── Step: login ────────────────────────────────────────────────────────────────


async def step_login(connection_id: str, job_id: str, otp: str | None) -> dict:
    """Navigate to the bank, log in, handle OTP if provided.
    Returns browser storage_state dict (cookies + localStorage) for subsequent steps.
    The OTP is decrypted from DB for static mode; passed directly for webhook mode.
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
        # For static/totp modes, use the OTP stored in DB (never passed through Restate payload)
        if otp is None and conn.otp_mode in ("static", "totp") and conn.otp_value_enc:
            otp = decrypt(conn.otp_value_enc)

    async with stealth_browser() as (_browser, page):
        try:
            await page.goto(login_url, wait_until="networkidle", timeout=20_000)
            adapter = get_adapter(bank_slug)

            await adapter.navigate_to_login(page)
            await adapter.fill_and_submit_credentials(page, username, password)

            if await adapter.is_otp_required(page):
                if not otp:
                    raise ValueError("OTP required but not provided")
                await adapter.submit_otp(page, otp)

            state: dict = await page.context.storage_state()
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
    return state


# ── Step: get_accounts ─────────────────────────────────────────────────────────


async def step_get_accounts(connection_id: str, job_id: str, session_state: dict) -> list[dict]:
    """Navigate to the dashboard and extract all accounts.
    Returns list of AccountData dicts (serializable for Restate journal).
    Persists accounts to DB.
    """
    started_at = datetime.now(UTC)
    log.info("step.get_accounts.start", job_id=job_id)

    async with get_session() as db:
        conn = await db.get(BankConnection, connection_id)
        if not conn:
            raise ValueError(f"BankConnection {connection_id} not found")
        login_url = conn.login_url
        bank_slug = conn.bank_slug

    async with stealth_browser(storage_state=session_state) as (_browser, page):
        try:
            await page.goto(login_url, wait_until="networkidle", timeout=20_000)
            adapter = get_adapter(bank_slug)
            accounts = await adapter.get_accounts(page)
        except Exception as exc:
            await _capture_failure(page, job_id, "get_accounts", exc, started_at)
            raise

    # Persist to DB — batch fetch existing, insert new
    account_dicts = []
    async with get_session() as db:
        existing_result = await db.execute(
            select(Account).where(Account.connection_id == connection_id)
        )
        existing_by_ext_id = {a.external_id: a.id for a in existing_result.scalars().all()}

        for acc_data in accounts:
            if acc_data.external_id in existing_by_ext_id:
                account_db_id = existing_by_ext_id[acc_data.external_id]
            else:
                new_account = Account(
                    id=str(uuid.uuid4()),
                    connection_id=connection_id,
                    external_id=acc_data.external_id,
                    name=acc_data.name,
                    account_type=acc_data.account_type,
                    currency=acc_data.currency,
                )
                db.add(new_account)
                account_db_id = new_account.id

            account_dicts.append({**acc_data.model_dump(), "db_id": account_db_id})

        job = await db.get(SyncJob, job_id)
        if job:
            job.accounts_synced = len(accounts)

    await _write_step(
        job_id,
        "get_accounts",
        "success",
        output={"count": len(accounts), "account_ids": [a["db_id"] for a in account_dicts]},
        started_at=started_at,
    )
    log.info("step.get_accounts.success", job_id=job_id, count=len(accounts))
    return account_dicts


# ── Step: get_transactions ─────────────────────────────────────────────────────


async def step_get_transactions(
    connection_id: str, job_id: str, session_state: dict, account_dict: dict
) -> int:
    """Extract all transactions for one account and persist to DB.
    Returns count of transactions stored.
    """
    account = AccountData(
        external_id=account_dict["external_id"],
        name=account_dict.get("name"),
        account_type=account_dict.get("account_type"),
        currency=account_dict.get("currency", "USD"),
    )
    account_db_id: str = account_dict["db_id"]
    step_name = f"transactions_{account.external_id}"
    started_at = datetime.now(UTC)
    log.info("step.transactions.start", job_id=job_id, account=account.external_id)

    async with get_session() as db:
        conn = await db.get(BankConnection, connection_id)
        if not conn:
            raise ValueError(f"BankConnection {connection_id} not found")
        login_url = conn.login_url
        bank_slug = conn.bank_slug

    async with stealth_browser(storage_state=session_state) as (_browser, page):
        try:
            await page.goto(login_url, wait_until="networkidle", timeout=20_000)
            adapter = get_adapter(bank_slug)
            transactions = await adapter.get_transactions(page, account)
        except Exception as exc:
            await _capture_failure(page, job_id, step_name, exc, started_at)
            raise

    # Persist — ON CONFLICT DO NOTHING (idempotent re-runs)
    inserted = 0
    async with get_session() as db:
        for txn in transactions:
            stmt = (
                pg_insert(Transaction)
                .values(
                    id=str(uuid.uuid4()),
                    account_id=account_db_id,
                    external_id=txn.external_id,
                    posted_at=txn.posted_at,
                    description=txn.description,
                    amount=txn.amount,
                    currency=txn.currency,
                    running_balance=txn.running_balance,
                    raw=txn.raw,
                )
                .on_conflict_do_nothing(index_elements=["account_id", "external_id"])
            )
            result = await db.execute(stmt)
            inserted += result.rowcount

        job = await db.get(SyncJob, job_id)
        if job:
            job.transactions_synced = (job.transactions_synced or 0) + inserted

    await _write_step(
        job_id,
        step_name,
        "success",
        output={"total": len(transactions), "inserted": inserted},
        started_at=started_at,
    )
    log.info(
        "step.transactions.success", job_id=job_id, account=account.external_id, inserted=inserted
    )
    return inserted


# ── Step: get_balance ──────────────────────────────────────────────────────────


async def step_get_balance(
    connection_id: str, job_id: str, session_state: dict, account_dict: dict
) -> dict:
    """Extract and persist the current balance for one account."""
    account = AccountData(
        external_id=account_dict["external_id"],
        name=account_dict.get("name"),
        account_type=account_dict.get("account_type"),
        currency=account_dict.get("currency", "USD"),
    )
    account_db_id: str = account_dict["db_id"]
    step_name = f"balance_{account.external_id}"
    started_at = datetime.now(UTC)

    async with get_session() as db:
        conn = await db.get(BankConnection, connection_id)
        if not conn:
            raise ValueError(f"BankConnection {connection_id} not found")
        login_url = conn.login_url
        bank_slug = conn.bank_slug

    async with stealth_browser(storage_state=session_state) as (_browser, page):
        try:
            await page.goto(login_url, wait_until="networkidle", timeout=20_000)
            adapter = get_adapter(bank_slug)
            balance = await adapter.get_balance(page, account)
        except Exception as exc:
            await _capture_failure(page, job_id, step_name, exc, started_at)
            raise

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

    await _write_step(
        job_id,
        step_name,
        "success",
        output={"current": balance.current, "currency": balance.currency},
        started_at=started_at,
    )
    return {"current": balance.current, "currency": balance.currency}


# ── Step: finalise ─────────────────────────────────────────────────────────────


async def step_finalise(job_id: str) -> None:
    started_at = datetime.now(UTC)
    async with get_session() as db:
        job = await db.get(SyncJob, job_id)
        if job:
            job.status = "success"
            job.completed_at = datetime.now(UTC)
    await _write_step(job_id, "finalise", "success", started_at=started_at)
    log.info("step.finalise.success", job_id=job_id)
