"""Restate durable workflow for bank synchronisation.

Each ctx.run() call is checkpointed in the Restate journal:
  - If the worker crashes mid-sync, replay resumes from the last
    completed step — no re-login, no duplicate data.
  - OTP for 'webhook' mode: the workflow suspends via ctx.promise()
    (zero resources held) until the CLI sends the signal.

Browser session state (cookies) is returned from step_login and passed
into subsequent steps, so each step can restore the authenticated session
without re-logging in from scratch. See steps.py for the browser lifecycle.
"""

import restate
from restate import WorkflowContext, WorkflowSharedContext

from src.core.logging import get_logger
from src.worker import steps

log = get_logger(__name__)

sync_workflow = restate.Workflow("SyncBankWorkflow")


@sync_workflow.main()
async def run(ctx: WorkflowContext, req: dict) -> dict:
    """Main workflow handler. req keys:
      - job_id: str
      - connection_id: str
      - otp_mode: str  ("static" | "totp" | "webhook")
      - otp: str | None  (provided for static/totp modes)
    """
    job_id: str = req["job_id"]
    connection_id: str = req["connection_id"]
    otp_mode: str = req.get("otp_mode", "static")
    # NOTE: plaintext credentials are never included in the Restate payload.
    # Static/TOTP OTPs are retrieved from DB (encrypted) inside step_login.
    # Webhook OTPs arrive via the provide_otp handler below.

    log.info("workflow.start", job_id=job_id, connection_id=connection_id)

    # For webhook OTP: suspend before the browser opens (zero resources held).
    # Resolved by: uv run waycore otp --job-id <id> --code <code>
    webhook_otp: str | None = None
    if otp_mode == "webhook":
        log.info("workflow.awaiting_otp", job_id=job_id)
        webhook_otp = await ctx.promise("otp", type_hint=str).value()

    # ── Step 1: Login ──────────────────────────────────────────────────────────
    # otp=None for static/totp — step_login fetches from DB.
    # otp=webhook_otp for webhook mode — arrived via promise.
    session_state: dict = await ctx.run(
        "login",
        lambda: steps.step_login(connection_id, job_id, webhook_otp),
    )

    # ── Step 2: Discover accounts ──────────────────────────────────────────────
    account_dicts: list[dict] = await ctx.run(
        "get_accounts",
        lambda: steps.step_get_accounts(connection_id, job_id, session_state),
    )

    # ── Steps 3+: Per-account transactions and balance ─────────────────────────
    for acc in account_dicts:
        ext_id = acc["external_id"]

        await ctx.run(
            f"transactions_{ext_id}",
            lambda a=acc: steps.step_get_transactions(connection_id, job_id, session_state, a),
        )
        await ctx.run(
            f"balance_{ext_id}",
            lambda a=acc: steps.step_get_balance(connection_id, job_id, session_state, a),
        )

    # ── Finalise ───────────────────────────────────────────────────────────────
    await ctx.run("finalise", lambda: steps.step_finalise(job_id))

    log.info("workflow.complete", job_id=job_id)
    return {"status": "success", "accounts_synced": len(account_dicts)}


@sync_workflow.handler()
async def provide_otp(ctx: WorkflowSharedContext, otp: str) -> None:
    """Resolve the OTP promise for a paused webhook-mode workflow.
    Called by: uv run waycore otp --job-id <id> --code <otp>
    """
    await ctx.promise("otp").resolve(otp)
    log.info("workflow.otp_provided")
