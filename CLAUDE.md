# WayCore Bank Scraper — Claude Code Context

## What This Is
CLI-driven bank scraper. Logs into a bank portal via browser automation, extracts accounts,
transactions, and balances, stores them in Postgres. Durable workflow via Restate handles
crash recovery and OTP pause/resume. LLM (Claude vision) handles DOM understanding.

## Architecture (short version)
```
CLI (typer) → Restate (durable workflow) → Worker (Playwright + Claude vision) → PostgreSQL
```

## Browser Session Design
Step boundaries are aligned with browser economics, not Restate granularity:
- **Browser #1 (login)**: Separate step — required for OTP webhook pause/resume.
- **Browser #2 (extract_all)**: ONE browser session for discovering all accounts and
  extracting transactions + balance for each. Eliminates N-1 browser launches.
- Adapters declare a `BrowserPolicy` (viewport, locale, timezone, UA) so stealth
  config matches each bank's expectations.
- Warm browser pooling is a future optimization — the clean boundary is already in place.

## Three Layers (Always All Three)
- **Layer 1 — Execution**: Playwright + stealth always runs. Drives the browser. Has no opinion.
- **Layer 2 — Intelligence**: LLM (multiple focused calls per goal). Task-specific DOM observers
  (`_dom_forms`, `_dom_tables`, `_dom_navigation`, `_dom_balance`). Provider pluggable via
  `LLM_PROVIDER` config, lazy-initialized in `agent/llm.py`.
- **Layer 3 — Orchestration**: Adapter sequences goals. `HeritageBankAdapter` passes selector hints.
  `GenericAdapter` lets LLM discover everything. Both use same execution + intelligence layers.
  `extract_all()` is the workflow-level unit of work — one browser, all accounts.

## Key Design Rules
1. Every bank is a `BankAdapter` subclass in `src/adapters/`. Never put bank logic elsewhere.
2. LLM calls are per-goal and focused (not one big prompt). Each call has a task-specific system prompt and returns structured JSON.
3. Sync jobs must write `sync_steps` records for every step — this is the audit trail and human debugging surface.
4. On any failure: save screenshot, write error to step output, set step+job status. Never swallow errors silently.
5. Credentials: always encrypt (Fernet) before DB write. Decrypt only inside worker memory. Never log.
6. Transactions: `ON CONFLICT (account_id, external_id) DO NOTHING` — all writes idempotent. Batch inserts.
7. Balances: append-only. Never UPDATE balance rows.

## Stack
- Python 3.12, `uv`
- Restate (`restate-sdk`) — durable workflows, OTP human-in-loop via `ctx.promise`
- Playwright async + `playwright-stealth` — stealth browser, bezier mouse movement
- SQLAlchemy 2.x async + Alembic — ORM + migrations
- PostgreSQL 16 (Docker Compose for local dev)
- Pluggable LLM (Anthropic/OpenAI) — DOM extraction fallback in adapters, vision in `GenericAdapter`
- structlog — JSON logs, always bind job_id
- typer + rich — CLI with live step trace

## Local Dev
```bash
docker compose up -d        # postgres + restate + worker + register
uv run alembic upgrade head # run migrations
uv run waycore sync --bank-url URL --username U --password P --otp CODE
```

## Adding a New Bank (structured adapter)
1. `src/adapters/<bank_slug>.py` implementing `BankAdapter`
2. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`

## Development Workflow

After every meaningful chunk of code written, run:
- `/review` — principal engineer review: bugs, wrong assumptions, dead code, CLAUDE.md violations
- `/style` — style consistency check: type hints, logging, naming, imports

Run both before considering any feature done. Fix MUST FIX items immediately.
To run on a loop during a session: `/loop 20m /review`

## File Layout
```
cli.py                                CLI entry point (typer): sync, otp, jobs, transactions, accounts
src/
  adapters/base.py                    BankAdapter ABC + data models + BrowserPolicy + AccountResult
  adapters/generic_bank_adapter.py    LLM-driven adapter (default for unknown banks)
  adapters/heritage_bank_adapter.py   Demo bank: deterministic selectors + LLM fallback
  adapters/heritage_parsers.py        Pure parsing functions (no browser) for Heritage Bank
  adapters/__init__.py                ADAPTER_REGISTRY + get_adapter()
  agent/llm.py                        LLMClient protocol + provider implementations (Anthropic, OpenAI)
  agent/extractor.py                  Per-goal LLM extraction with task-specific DOM observers
  core/config.py                      pydantic-settings (DB pool, browser, LLM, screenshots)
  core/crypto.py                      Fernet encrypt/decrypt
  core/logging.py                     structlog JSON logging
  core/screenshots.py                 Screenshot storage (local/S3)
  core/stealth.py                     Playwright launch + BrowserPolicy + bezier mouse + human typing
  db/models.py                        All SQLAlchemy models (multi-tenant schema, single-tenant app)
  db/session.py                       Lazy async session factory (engine created on first use)
  worker/app.py                       Restate ASGI app registration
  worker/workflow.py                  Durable workflow: login → extract_all → finalise
  worker/steps.py                     Step functions: step_login, step_extract_all, step_finalise
```
