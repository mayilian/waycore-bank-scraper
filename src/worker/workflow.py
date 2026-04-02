"""Restate durable workflow for bank synchronisation.

Step boundaries are aligned with browser session economics:
  1. login — separate step for OTP webhook pause/resume
  2. extract_all — single browser session for ALL accounts
  3. finalise — mark job complete

Each ctx.run() call is checkpointed in the Restate journal:
  - If the worker crashes mid-sync, replay resumes from the last
    completed step — no duplicate data.
  - OTP for 'webhook' mode: the workflow suspends via ctx.promise()
    (zero resources held) until the CLI sends the signal.
"""

import asyncio
import functools
from datetime import UTC, datetime
from typing import Any

import restate
from restate import WorkflowContext, WorkflowSharedContext
from restate.exceptions import TerminalError

from src.agent.extractor import reset_llm_budget
from src.core import metrics
from src.core.config import settings
from src.core.logging import bind_job_context, clear_job_context, get_logger
from src.db.models import SyncJob
from src.db.session import get_session
from src.worker import steps
from src.worker.concurrency import acquire_bank_slot

log = get_logger(__name__)

sync_workflow = restate.Workflow("SyncBankWorkflow")


async def _set_job_status(job_id: str, status: str) -> None:
    async with get_session() as db:
        job = await db.get(SyncJob, job_id)
        if not job:
            raise ValueError(f"SyncJob {job_id} not found — cannot set status to '{status}'")
        job.status = status
        if status == "running" and not job.started_at:
            job.started_at = datetime.now(UTC)


@sync_workflow.main()
async def run(ctx: WorkflowContext, req: dict[str, Any]) -> dict[str, Any]:
    """Main workflow handler. req keys:
    - job_id: str
    - connection_id: str
    - otp_mode: str  ("static" | "totp" | "webhook")
    """
    job_id: str = req["job_id"]
    connection_id: str = req["connection_id"]
    otp_mode: str = req.get("otp_mode", "static")

    log.info("workflow.start", job_id=job_id, connection_id=connection_id)

    await ctx.run("mark_running", functools.partial(_set_job_status, job_id, "running"))

    try:
        return await asyncio.wait_for(
            _run_sync(ctx, job_id, connection_id, otp_mode),
            timeout=settings.max_sync_duration_secs,
        )
    except TimeoutError as exc:
        timeout_msg = f"Sync exceeded {settings.max_sync_duration_secs}s time limit"
        log.error("workflow.timeout", job_id=job_id, limit=settings.max_sync_duration_secs)
        await ctx.run(
            "mark_failed",
            functools.partial(_mark_job_failed, job_id, timeout_msg),
        )
        raise TerminalError(timeout_msg) from exc
    except Exception as exc:
        await ctx.run(
            "mark_failed",
            functools.partial(_mark_job_failed, job_id, str(exc)),
        )
        raise TerminalError(str(exc)) from exc


async def _mark_job_failed(job_id: str, reason: str) -> None:
    async with get_session() as db:
        job = await db.get(SyncJob, job_id)
        if job:
            job.status = "failed"
            job.failure_reason = reason
            job.completed_at = datetime.now(UTC)


async def _run_sync(
    ctx: WorkflowContext, job_id: str, connection_id: str, otp_mode: str
) -> dict[str, Any]:
    reset_llm_budget()
    sync_start = datetime.now(UTC)

    # For webhook OTP: suspend before the browser opens (zero resources held).
    webhook_otp: str | None = None
    if otp_mode == "webhook":
        await ctx.run(
            "mark_awaiting_otp",
            functools.partial(_set_job_status, job_id, "awaiting_otp"),
        )
        log.info("workflow.awaiting_otp", job_id=job_id)
        webhook_otp = await ctx.promise("otp", type_hint=str).value()
        await ctx.run(
            "mark_running_after_otp",
            functools.partial(_set_job_status, job_id, "running"),
        )

    # ── Step 1: Login (browser #1) ────────────────────────────────────────────
    login_result: dict[str, Any] = await ctx.run(
        "login",
        functools.partial(steps.step_login, connection_id, job_id, webhook_otp),
    )
    session_state: Any = login_result["storage_state"]
    post_login_url: str = login_result["post_login_url"]
    bank_slug: str = login_result["bank_slug"]

    bind_job_context(job_id, connection_id, bank_slug)

    # ── Step 2: Extract all accounts (browser #2) ─────────────────────────────
    # Per-bank concurrency limiter prevents hammering one bank with too many
    # simultaneous browser sessions. Banks rate-limit or block IPs on parallel logins.
    async with acquire_bank_slot(bank_slug):
        extract_result: dict[str, Any] = await ctx.run(
            "extract_all",
            functools.partial(
                steps.step_extract_all,
                connection_id,
                job_id,
                session_state,
                post_login_url,
                bank_slug,
            ),
        )

    account_errors: list[str] = extract_result.get("errors", [])
    accounts_found: int = extract_result.get("accounts_found", 0)

    # ── Step 3: Finalise ──────────────────────────────────────────────────────
    if account_errors and len(account_errors) >= accounts_found:
        raise RuntimeError(f"All account extractions failed: {'; '.join(account_errors)}")

    final_status = "partial_success" if account_errors else "success"
    await ctx.run(
        "finalise",
        functools.partial(steps.step_finalise, job_id, final_status),
    )

    duration = (datetime.now(UTC) - sync_start).total_seconds()
    metrics.sync_completed(bank_slug, duration, final_status)
    clear_job_context()

    log.info("workflow.complete", job_id=job_id, status=final_status, duration_secs=duration)
    return {
        "status": final_status,
        "accounts_found": accounts_found,
        "accounts_extracted": extract_result.get("accounts_extracted", 0),
        "errors": account_errors,
    }


@sync_workflow.handler()
async def provide_otp(ctx: WorkflowSharedContext, otp: str) -> None:
    """Resolve the OTP promise for a paused webhook-mode workflow."""
    await ctx.promise("otp").resolve(otp)  # type: ignore[arg-type]
    log.info("workflow.otp_provided")
