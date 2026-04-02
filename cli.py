"""WayCore CLI — trigger syncs, inspect results, provide OTPs.

Usage:
  uv run waycore sync --bank-url URL --username U --password P --otp CODE
  uv run waycore otp  --job-id ID --code CODE
  uv run waycore jobs
  uv run waycore transactions --account-id ID
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from src.core.config import settings
from src.core.crypto import encrypt
from src.core.logging import configure_logging
from src.db.models import (
    Account,
    BankConnection,
    Organization,
    SyncJob,
    SyncStep,
    Transaction,
    User,
)
from src.db.session import get_session

configure_logging()
app = typer.Typer(help="WayCore bank scraper CLI", add_completion=False)
console = Console()

# Default org/user created on first run — single-tenant for the demo.
_DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
_DEFAULT_USER_ID = "00000000-0000-0000-0000-000000000002"


async def _ensure_default_tenant() -> None:
    """Create a default org and user if they don't exist."""
    async with get_session() as db:
        org = await db.get(Organization, _DEFAULT_ORG_ID)
        if not org:
            db.add(Organization(id=_DEFAULT_ORG_ID, name="Default Org", plan="starter"))

        user = await db.get(User, _DEFAULT_USER_ID)
        if not user:
            db.add(User(id=_DEFAULT_USER_ID, org_id=_DEFAULT_ORG_ID, email="admin@waycore.local"))


def _bank_slug_from_url(url: str) -> str:
    """Derive a stable slug from a bank URL for adapter lookup."""
    host = urlparse(url).netloc.lower()
    if "heritage" in host or "demo-bank" in host:
        return "heritage_bank"
    slug = host.replace("www.", "").replace(".", "_").replace("-", "_")
    return slug[:64]


@app.command()
def sync(
    bank_url: Annotated[str, typer.Option("--bank-url", help="Bank login URL")],
    username: Annotated[str, typer.Option("--username", "-u")],
    password: Annotated[str, typer.Option("--password", "-p")],
    otp: Annotated[
        str | None, typer.Option("--otp", help="OTP code (omit for webhook mode)")
    ] = None,
    otp_mode: Annotated[str, typer.Option("--otp-mode")] = "static",
) -> None:
    """Trigger a bank sync and stream live step trace to the terminal."""
    asyncio.run(_sync(bank_url, username, password, otp, otp_mode))


async def _sync(
    bank_url: str, username: str, password: str, otp: str | None, otp_mode: str
) -> None:
    await _ensure_default_tenant()

    bank_slug = _bank_slug_from_url(bank_url)
    job_id = str(uuid.uuid4())

    # Reuse existing connection for the same user+bank+URL, or create a new one.
    # This prevents duplicate accounts/transactions on re-runs.
    async with get_session() as db:
        result = await db.execute(
            select(BankConnection).where(
                BankConnection.user_id == _DEFAULT_USER_ID,
                BankConnection.bank_slug == bank_slug,
                BankConnection.login_url == bank_url,
            )
        )
        existing_conn = result.scalars().first()

        if existing_conn:
            connection_id = existing_conn.id
            # Update credentials in case they changed
            existing_conn.username_enc = encrypt(username)
            existing_conn.password_enc = encrypt(password)
            existing_conn.otp_mode = otp_mode
            existing_conn.otp_value_enc = encrypt(otp) if otp else None
        else:
            connection_id = str(uuid.uuid4())
            db.add(
                BankConnection(
                    id=connection_id,
                    user_id=_DEFAULT_USER_ID,
                    bank_slug=bank_slug,
                    bank_name=bank_slug.replace("_", " ").title(),
                    login_url=bank_url,
                    username_enc=encrypt(username),
                    password_enc=encrypt(password),
                    otp_mode=otp_mode,
                    otp_value_enc=encrypt(otp) if otp else None,
                )
            )

        db.add(
            SyncJob(
                id=job_id,
                restate_id=job_id,
                connection_id=connection_id,
                status="pending",
                started_at=datetime.now(UTC),
            )
        )

    console.print(f"[bold green]✓[/] Job created: [cyan]{job_id}[/]")
    console.print(f"  Bank: [cyan]{bank_slug}[/]  URL: {bank_url}\n")

    # Trigger the Restate workflow.
    # Credentials are NOT included — step_login fetches them from DB.
    payload: dict[str, Any] = {
        "job_id": job_id,
        "connection_id": connection_id,
        "otp_mode": otp_mode,
    }
    # Use /send to trigger asynchronously — returns immediately.
    # The CLI polls the DB for step progress instead of waiting for completion.
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{settings.restate_ingress_url}/SyncBankWorkflow/{job_id}/run/send",
            json=payload,
        )
        if resp.status_code not in (200, 202):
            console.print(f"[red]Failed to trigger workflow: {resp.status_code} {resp.text}[/]")
            raise typer.Exit(1)

    console.print("[bold]Live step trace[/] (polling every 2s):\n")
    await _poll_job(job_id)


async def _poll_job(job_id: str) -> None:
    seen_steps: set[str] = set()

    while True:
        async with get_session() as db:
            job = await db.get(SyncJob, job_id)
            if not job:
                console.print("[red]Job not found[/]")
                raise typer.Exit(1)

            result = await db.execute(
                select(SyncStep).where(SyncStep.job_id == job_id).order_by(SyncStep.created_at)
            )
            steps_list = result.scalars().all()

        for step in steps_list:
            if step.id in seen_steps:
                continue
            seen_steps.add(step.id)
            icon = {"success": "[green]✓[/]", "failed": "[red]✗[/]", "running": "[yellow]→[/]"}.get(
                step.status, "·"
            )
            duration = ""
            if step.started_at and step.completed_at:
                secs = (step.completed_at - step.started_at).total_seconds()
                duration = f"  [dim]{secs:.1f}s[/]"
            console.print(f"  {icon} {step.name:<40}{duration}")
            if step.status == "failed" and step.output:
                console.print(f"    [red]{step.output.get('error', '')}[/]")
            if step.screenshot_path:
                console.print(f"    [dim]screenshot: {step.screenshot_path}[/]")

        if job.status == "success":
            console.print(
                f"\n[bold green]✓ Sync complete.[/] "
                f"Accounts: {job.accounts_synced}  Transactions: {job.transactions_synced}"
            )
            return

        if job.status == "partial_success":
            console.print(
                f"\n[bold yellow]⚠ Sync partially complete.[/] "
                f"Accounts: {job.accounts_synced}  Transactions: {job.transactions_synced}\n"
                f"Some accounts had errors — check sync steps for details."
            )
            return

        if job.status == "failed":
            console.print(f"\n[bold red]✗ Sync failed:[/] {job.failure_reason}")
            raise typer.Exit(1)

        if job.status == "awaiting_otp":
            console.print(
                "\n[yellow]⏸ Waiting for OTP...[/] Run: waycore otp --job-id <id> --code <code>"
            )

        await asyncio.sleep(2)


@app.command()
def otp(
    job_id: Annotated[str, typer.Option("--job-id")],
    code: Annotated[str, typer.Option("--code")],
) -> None:
    """Provide an OTP for a paused webhook-mode sync job."""
    asyncio.run(_provide_otp(job_id, code))


async def _provide_otp(job_id: str, code: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.restate_ingress_url}/SyncBankWorkflow/{job_id}/provide_otp",
            json=code,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code in (200, 202):
            console.print(f"[green]✓ OTP sent for job {job_id}[/]")
        else:
            console.print(f"[red]Failed: {resp.status_code} {resp.text}[/]")
            raise typer.Exit(1)


@app.command()
def jobs(limit: Annotated[int, typer.Option()] = 20) -> None:
    """List recent sync jobs."""
    asyncio.run(_list_jobs(limit))


async def _list_jobs(limit: int) -> None:
    async with get_session() as db:
        result = await db.execute(select(SyncJob).order_by(SyncJob.created_at.desc()).limit(limit))
        job_list = result.scalars().all()

    table = Table("Job ID", "Status", "Accounts", "Transactions", "Started", "Completed")
    for job in job_list:
        status_fmt = {
            "success": "[green]success[/]",
            "failed": "[red]failed[/]",
            "running": "[yellow]running[/]",
            "awaiting_otp": "[yellow]awaiting_otp[/]",
        }.get(job.status, job.status)
        table.add_row(
            job.id[:8] + "…",
            status_fmt,
            str(job.accounts_synced),
            str(job.transactions_synced),
            job.started_at.strftime("%Y-%m-%d %H:%M") if job.started_at else "—",
            job.completed_at.strftime("%Y-%m-%d %H:%M") if job.completed_at else "—",
        )
    console.print(table)


@app.command()
def transactions(
    account_id: Annotated[str | None, typer.Option("--account-id")] = None,
    limit: Annotated[int, typer.Option()] = 50,
) -> None:
    """Show transactions, optionally filtered by account DB ID."""
    asyncio.run(_list_transactions(account_id, limit))


async def _list_transactions(account_id: str | None, limit: int) -> None:
    async with get_session() as db:
        stmt = select(Transaction).order_by(Transaction.posted_at.desc()).limit(limit)
        if account_id:
            stmt = stmt.where(Transaction.account_id == account_id)
        result = await db.execute(stmt)
        txn_list = result.scalars().all()

    table = Table("Date", "Description", "Amount", "Currency", "Running Balance")
    for txn in txn_list:
        amount_fmt = (
            f"[red]{txn.amount:.2f}[/]" if txn.amount < 0 else f"[green]{txn.amount:.2f}[/]"
        )
        table.add_row(
            txn.posted_at.strftime("%Y-%m-%d") if txn.posted_at else "—",
            (txn.description or "")[:50],
            amount_fmt,
            txn.currency,
            f"{txn.running_balance:.2f}" if txn.running_balance is not None else "—",
        )
    console.print(table)


@app.command()
def accounts() -> None:
    """List all synced accounts."""
    asyncio.run(_list_accounts())


async def _list_accounts() -> None:
    async with get_session() as db:
        result = await db.execute(select(Account).order_by(Account.created_at))
        account_list = result.scalars().all()

    table = Table("DB ID", "External ID", "Name", "Type", "Currency")
    for acc in account_list:
        table.add_row(
            acc.id[:8] + "…",
            acc.external_id,
            acc.name or "—",
            acc.account_type or "—",
            acc.currency,
        )
    console.print(table)


if __name__ == "__main__":
    app()
