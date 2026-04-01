---
name: Build State
description: Current implementation status, known gaps, and what to do next
type: project
---

## Status: Initial implementation complete, pushed to GitHub

All code passes `ruff check`, `ruff format --check`, `mypy --strict`.

---

## What's Done

- [x] Git repo + GitHub: https://github.com/mayilian/waycore-bank-scraper
- [x] Pre-push hook: ruff + mypy gate on every push
- [x] `pyproject.toml` (uv), `Dockerfile`, `docker-compose.yml`, `.env.example`
- [x] DB models: full multi-tenant schema (orgs → users → connections → accounts → balances/transactions/sync_jobs/sync_steps)
- [x] Alembic migration: `alembic/versions/001_initial_schema.py`
- [x] Core utilities: `config.py`, `crypto.py` (Fernet), `logging.py` (structlog), `stealth.py` (Playwright + bezier), `screenshots.py` (local/S3)
- [x] `BankAdapter` ABC + data models (`AccountData`, `BalanceData`, `TransactionData`)
- [x] `HeritageBankAdapter` — demo bank, uses known selectors + LLM fallback
- [x] `GenericBankAdapter` — LLM-first, works on any URL
- [x] LLM extractor: `src/agent/extractor.py` — 6 focused per-goal functions
- [x] Restate workflow: `src/worker/workflow.py` — durable steps, OTP suspend/resume
- [x] Step implementations: `src/worker/steps.py` — login, get_accounts, get_transactions, get_balance, finalise
- [x] Restate ASGI app: `src/worker/app.py`
- [x] CLI: `cli.py` — sync, otp, jobs, transactions, accounts commands with live step trace
- [x] README with setup instructions + architecture section (answers the 3 rubric questions)
- [x] DESIGN.md — final architecture document
- [x] CLAUDE.md — project context for Claude Code sessions
- [x] `.claude/commands/review.md` — /review slash command
- [x] `.claude/commands/style.md` — /style slash command

---

## Known Gaps / Next Steps

### High priority (before submission)
- [ ] **End-to-end test**: Actually run against the demo bank. The code is written but untested end-to-end. Need to: `docker compose up`, run migrations, run `waycore sync`, verify data lands in DB.
- [ ] **Verify Restate registration**: The `register` service in docker-compose registers the worker via curl. Confirm this works on first boot and after worker restarts.
- [ ] **Verify bot detection bypass**: The demo bank has `/bot-detection-overlay.js`. Need to confirm `playwright-stealth` + headless mode gets past it. If not, may need `PLAYWRIGHT_HEADFUL=1` or additional patches.

### Medium priority
- [ ] **Heritage bank DOM navigation**: `HeritageBankAdapter.get_accounts()` and `get_transactions()` rely on LLM extraction from the dashboard. Need to verify the LLM correctly identifies the accounts and transaction table selectors for this specific bank UI.
- [ ] **Transaction pagination**: `check_has_next_page()` in extractor.py returns a click action for pagination. Need to verify it correctly handles the Heritage bank's pagination (if any).
- [ ] **uv.lock committed**: The lockfile should be committed so others get reproducible installs.

### Low priority / future
- [ ] Add `fly.toml` for Fly.io cloud deployment
- [ ] Add `--schedule` flag to CLI for recurring syncs
- [ ] Wire up PostgreSQL RLS for production multi-tenancy
- [ ] Add a second bank adapter to prove the pattern generalises

---

## Architecture Gotchas to Remember

1. **Browser session state**: Playwright's `context.storage_state()` returns a `StorageState` TypedDict (cookies + origins). This is passed between Restate steps — each step opens a fresh browser and restores the session. This avoids keeping a browser process alive between Restate checkpoints.

2. **OTP flow**: For `static` mode, the CLI stores the encrypted OTP in `bank_connections.otp_value_enc`. The Restate payload contains ONLY `job_id`, `connection_id`, `otp_mode` — no credentials. `step_login` decrypts from DB. For `webhook` mode, the workflow suspends via `ctx.promise("otp").value()` and resumes when `waycore otp --job-id X --code Y` is run.

3. **Idempotent inserts**: `pg_insert(Transaction).on_conflict_do_nothing(index_elements=["account_id", "external_id"])` — use `index_elements` with column names, NOT a constraint name string. Constraint names can be wrong; column names can't.

4. **docker-compose `register` service**: After the worker starts (healthcheck passes), a one-shot `curlimages/curl` container calls `POST http://restate:9070/deployments` to register the worker with Restate. This must succeed before any workflow can be triggered.

5. **Default tenant**: The CLI creates a hardcoded org/user (`_DEFAULT_ORG_ID`, `_DEFAULT_USER_ID`) on first run for the single-tenant demo. In a real multi-tenant deployment this would come from an auth layer.
