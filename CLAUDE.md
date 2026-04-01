# WayCore Bank Scraper — Claude Code Context

## What This Is
Hosted SaaS bank scraper. Operator deploys once to Fly.io. Reviewers/users open a web UI,
paste a bank URL + credentials, watch the LLM-driven scraper run live. No local setup for users.

## Architecture (short version)
```
Web UI (HTMX) → FastAPI → Restate Cloud → Worker (Playwright + Claude vision) → Neon Postgres
```

## Three Layers (Always All Three)
- **Layer 1 — Execution**: Playwright + stealth always runs. Drives the browser. Has no opinion.
- **Layer 2 — Intelligence**: LLM (multiple focused calls per goal). Observes DOM/screenshot, returns JSON action. Provider: Anthropic API, abstracted behind `LLMClient`.
- **Layer 3 — Orchestration**: Adapter sequences goals. `HeritageBankAdapter` passes selector hints. `GenericAdapter` lets LLM discover everything. Both use same execution + intelligence layers.

## Key Design Rules
1. Every bank is a `BankAdapter` subclass in `src/adapters/`. Never put bank logic elsewhere.
2. LLM calls are per-goal and focused (not one big prompt). Each call has a task-specific system prompt and returns structured JSON.
3. Sync jobs must write `sync_job_steps` records for every step — this is the audit trail and human debugging surface.
4. On any failure: save screenshot, write error to step output, set step+job status. Never swallow errors silently.
5. Credentials: always encrypt (Fernet) before DB write. Decrypt only inside worker memory. Never log.
6. Transactions: `ON CONFLICT (account_id, bank_txn_id) DO NOTHING` — all writes idempotent.
7. Balances: append-only. Never UPDATE balance rows.
8. API keys required on all endpoints except `/health`. Rate limit per key AND globally (browser semaphore).

## Stack
- Python 3.12, `uv`
- Restate Cloud (`restate-sdk`) — durable workflows, OTP human-in-loop via `ctx.promise`
- Playwright async + `playwright-stealth` — stealth browser, bezier mouse movement
- FastAPI + Jinja2 + HTMX — API + web UI, SSE for live step trace
- SQLAlchemy 2.x async + Alembic — ORM + migrations
- Neon (serverless Postgres) — cloud DB, no Docker
- Claude claude-sonnet-4-6 vision — LLM computer-use loop in `GenericAdapter`
- structlog — JSON logs, always bind job_id
- Fly.io — compute + TLS + Tigris (S3) for screenshots

## No Docker for DB or Redis
Neon is the DB (cloud). Restate Cloud handles queue + state. No Redis. No local Postgres.
Docker is only used for Fly.io deployment.

## Adding a New Bank (structured adapter)
1. `src/adapters/<bank_slug>.py` implementing `BankAdapter`
2. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`
3. Optionally: insert row in `banks` table with `adapter_cls` set

## Development Workflow

After every meaningful chunk of code written, run:
- `/review` — principal engineer review: bugs, wrong assumptions, dead code, CLAUDE.md violations
- `/style` — style consistency check: type hints, logging, naming, imports

Run both before considering any feature done. Fix MUST FIX items immediately.
To run on a loop during a session: `/loop 20m /review`

## File Layout
```
src/
  adapters/base.py        BankAdapter ABC + Pydantic data models
  adapters/generic.py     LLM computer-use adapter (default for unknown banks)
  adapters/heritage.py    Demo bank structured adapter
  agent/computer_use.py   Claude vision loop: screenshot → action → verify
  api/main.py             FastAPI app + middleware
  api/routes/             sync.py, data.py, admin.py
  api/templates/          Jinja2 + HTMX templates
  core/config.py          pydantic-settings
  core/crypto.py          Fernet encrypt/decrypt
  core/stealth.py         Playwright launch + bezier mouse + human typing
  db/models.py            All SQLAlchemy models
  db/session.py           Async session factory
  worker/app.py           Restate app registration
  worker/workflow.py      @workflow.main() sync_bank + provide_otp handler
  worker/steps.py         Individual step functions
manage.py                 Operator CLI: create-key, list-keys, revoke-key
```
