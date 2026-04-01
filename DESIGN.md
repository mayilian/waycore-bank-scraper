# WayCore Bank Scraper — Final Design

> **Status**: Final — approved, ready to build
> **Date**: 2026-04-01

---

## Business Problem

Enterprise finance teams manage 100s of company bank accounts across banks that have no API or open-banking integration. Staff connect accounts once; the system automatically extracts transactions and balances on a schedule. Revenue model: monthly subscription per connected user.

For the challenge: a local-runnable implementation against the Heritage Bank demo that demonstrates this architecture.

---

## Workload Analysis

**Type**: Durable, stateful, I/O-bound automation workflow.

Each sync is a sequential chain of steps (login → OTP → navigate → extract → persist) where:
- Steps must execute in order and be checkpointed individually — a crash mid-extraction should resume from the last completed step, not re-login
- External dependencies (bank websites) are unreliable — per-step retry, not whole-job retry
- Human input may be required mid-flight (OTP)
- Re-runs of the same sync must never corrupt data (idempotent writes)

**Best execution model**: Durable execution via Restate. A job queue knows if a job failed. Restate knows *which step* failed — replay starts from the right place. The OTP pause (`ctx.promise().value()`) suspends the workflow with zero resources held until the signal arrives.

**Horizontal scaling**: Workers are stateless HTTP services. Restate routes each step invocation to any live instance. Scale = add worker containers. Per-bank concurrency is enforced at the workflow level — Bank A's rate limit never affects Bank B. The bottleneck is always browser memory (~400MB per Chromium instance), not the database.

---

## Architecture

```
CLI
 └─→ Restate server (local Docker)
       └─→ Worker service (Playwright + LLM + Restate SDK/hypercorn)
             └─→ PostgreSQL (local Docker)
```

No FastAPI. No separate API service. The CLI triggers workflows directly via the Restate SDK client. The worker exposes HTTP endpoints only for Restate to call (internal, handled by the SDK).

---

## Three Execution Layers

Always all three, always in this order:

**1. Execution** — Playwright + stealth
Always runs. Launches Chromium, drives the browser, handles bot evasion (stealth patches, bezier mouse, human-paced typing). Has no opinion about what to do — only executes.

**2. Intelligence** — LLM (series of focused calls)
Always runs. For each goal, observes the current page (trimmed DOM + screenshot) and returns a structured JSON action. Multiple targeted calls per sync — not one big prompt. Each call has a task-specific system prompt.

| Goal | What the LLM resolves |
|---|---|
| `find_login_fields` | `{username_sel, password_sel, submit_sel}` |
| `detect_post_login` | `{state: logged_in \| otp_required \| error}` |
| `find_accounts` | `[{id, name, balance, currency}]` |
| `find_txn_nav` | selector or URL to transaction history |
| `extract_txn_page` | `[{date, desc, amount, balance}]` |
| `has_next_page` | `{has_next: bool, selector?}` |

**3. Orchestration** — Bank adapter
Sequences goals for a specific bank. `HeritageBankAdapter` passes known selector hints to LLM calls (faster, fewer tokens). `GenericAdapter` passes no hints — LLM discovers everything from scratch. Both sit on the same execution and intelligence layers.

---

## Workflow (Restate)

```python
@workflow.main()
async def sync_bank(ctx: WorkflowContext, req: SyncRequest) -> SyncResult:
    await ctx.run("login",        step_login,     req)
    await ctx.run("otp_check",    step_otp_check)

    if await ctx.run("needs_otp", step_needs_otp):
        otp = await ctx.promise("otp", type_hint=str).value()  # suspends
        await ctx.run("submit_otp", step_submit_otp, otp)

    accounts = await ctx.run("get_accounts", step_get_accounts)

    for acc in accounts:
        await ctx.run(f"txns_{acc.id}",    step_get_transactions, acc)
        await ctx.run(f"balance_{acc.id}", step_get_balance,       acc)

    await ctx.run("persist", step_persist)
    await ctx.run("close",   step_close_browser)

@workflow.handler()
async def provide_otp(ctx: WorkflowSharedContext, otp: str) -> None:
    await ctx.promise("otp").resolve(otp)
```

---

## Data Model

```sql
-- Tenant root
organizations (
  id         UUID PRIMARY KEY,
  name       TEXT NOT NULL,
  plan       TEXT NOT NULL DEFAULT 'starter',
  created_at TIMESTAMPTZ DEFAULT now()
)

-- Staff members within an org (billing unit)
users (
  id         UUID PRIMARY KEY,
  org_id     UUID REFERENCES organizations NOT NULL,
  email      TEXT UNIQUE NOT NULL,
  created_at TIMESTAMPTZ DEFAULT now()
)

-- One user's credentials at one bank
bank_connections (
  id             UUID PRIMARY KEY,
  user_id        UUID REFERENCES users NOT NULL,
  bank_slug      TEXT NOT NULL,
  bank_name      TEXT,
  login_url      TEXT NOT NULL,
  username_enc   TEXT NOT NULL,          -- Fernet-encrypted
  password_enc   TEXT NOT NULL,
  otp_mode       TEXT NOT NULL,          -- static | totp | webhook
  otp_value_enc  TEXT,
  last_synced_at TIMESTAMPTZ,
  created_at     TIMESTAMPTZ DEFAULT now()
)

-- Accounts discovered within a connection
accounts (
  id            UUID PRIMARY KEY,
  connection_id UUID REFERENCES bank_connections NOT NULL,
  external_id   TEXT NOT NULL,
  name          TEXT,
  type          TEXT,
  currency      CHAR(3) NOT NULL DEFAULT 'USD',
  created_at    TIMESTAMPTZ DEFAULT now(),
  UNIQUE (connection_id, external_id)
)

-- Append-only balance snapshots — enables balance history
balances (
  id          UUID PRIMARY KEY,
  account_id  UUID REFERENCES accounts NOT NULL,
  available   NUMERIC(20,4),
  current     NUMERIC(20,4) NOT NULL,
  currency    CHAR(3) NOT NULL,
  captured_at TIMESTAMPTZ NOT NULL
)

-- Immutable transaction ledger — ON CONFLICT DO NOTHING = idempotent
transactions (
  id              UUID PRIMARY KEY,
  account_id      UUID REFERENCES accounts NOT NULL,
  external_id     TEXT NOT NULL,
  posted_at       TIMESTAMPTZ,
  description     TEXT,
  amount          NUMERIC(20,4) NOT NULL,   -- negative = debit
  currency        CHAR(3) NOT NULL,
  running_balance NUMERIC(20,4),
  raw             JSONB,
  created_at      TIMESTAMPTZ DEFAULT now(),
  UNIQUE (account_id, external_id)
)

-- Sync execution history
sync_jobs (
  id                  UUID PRIMARY KEY,
  restate_id          TEXT UNIQUE,
  connection_id       UUID REFERENCES bank_connections NOT NULL,
  status              TEXT NOT NULL,  -- pending|running|awaiting_otp|success|failed
  failure_reason      TEXT,
  transactions_synced INT DEFAULT 0,
  started_at          TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  created_at          TIMESTAMPTZ DEFAULT now()
)

-- Step-level audit trail and human debugging surface
sync_steps (
  id              UUID PRIMARY KEY,
  job_id          UUID REFERENCES sync_jobs NOT NULL,
  name            TEXT NOT NULL,
  status          TEXT NOT NULL,      -- running|success|failed|skipped
  attempt         INT NOT NULL DEFAULT 1,
  output          JSONB,              -- result data or {error, traceback}
  screenshot_path TEXT,               -- set on failure, or in debug mode
  started_at      TIMESTAMPTZ,
  completed_at    TIMESTAMPTZ
)
```

**Why NUMERIC(20,4)**: financial amounts require exact decimal representation. Floats are wrong for money. `NUMERIC` is unambiguous.
**Why append-only balances**: one extra row per sync per account enables balance history charts for free.
**Why raw JSONB on transactions**: every bank returns slightly different fields. Normalize the known fields; don't discard the rest.
**Why multi-tenant from day one**: the billing unit is `users`. All access is scoped through the FK chain `transactions → accounts → connections → users → organizations`.

---

## Tech Stack

| Layer | Choice | Why |
|---|---|---|
| Packages | `uv` | Fast, lockfile-based, modern |
| Workflow | Restate + local Docker | Durable execution, OTP suspend/resume, stateless workers |
| Browser | Playwright async + `playwright-stealth` | SPA support, bot evasion |
| LLM | Anthropic SDK (`claude-sonnet-4-6` vision) | Structured DOM extraction |
| DB | PostgreSQL 16 (Docker) | Financial precision, relational, JSONB, cloud-portable |
| ORM | SQLAlchemy 2 async + Alembic | Async-native, clean migration story |
| Logging | structlog | JSON, job_id bound through all layers |
| Secrets | `.env` + `python-dotenv` | Simple for local; Fly.io secrets for cloud |

---

## Cloud Extension Path

The local Docker Compose maps cleanly to cloud with three env var changes:

| Component | Local | Cloud |
|---|---|---|
| DB | Docker `postgres:16` | `DATABASE_URL` → Neon / RDS |
| Workflow | Docker `restatedev/restate` | `RESTATE_ENDPOINT` → Restate Cloud |
| Screenshots | Local volume | `SCREENSHOT_BACKEND=s3` + Cloudflare R2 credentials |
| Compute | `docker compose up` | `fly deploy` |

No code changes. No architecture changes.

---

## Repository Structure

```
waycore-bank-scraper/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── .env.example
├── alembic/
│   ├── env.py
│   └── versions/
├── src/
│   ├── adapters/
│   │   ├── __init__.py        # ADAPTER_REGISTRY
│   │   ├── base.py            # BankAdapter ABC + Pydantic data models
│   │   ├── heritage.py        # Heritage Bank (demo) — structured hints
│   │   └── generic.py         # Any bank — LLM discovers everything
│   ├── agent/
│   │   └── extractor.py       # LLM extraction loop (focused per-goal calls)
│   ├── core/
│   │   ├── config.py          # pydantic-settings
│   │   ├── crypto.py          # Fernet encrypt/decrypt
│   │   ├── stealth.py         # Browser launch + bezier mouse + human typing
│   │   └── logging.py         # structlog setup
│   ├── db/
│   │   ├── models.py          # All SQLAlchemy models
│   │   └── session.py         # Async session factory
│   └── worker/
│       ├── app.py             # Restate app + service registration
│       ├── workflow.py        # @workflow.main() sync_bank
│       └── steps.py           # Individual step implementations
└── cli.py                     # waycore sync / jobs / transactions / otp
```

---

## CLI Interface

```bash
# Start infrastructure
docker compose up -d

# Run a sync (Heritage Bank demo)
uv run python cli.py sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456

# Provide OTP for a paused job
uv run python cli.py otp --job-id <id> --code 123456

# Inspect results
uv run python cli.py jobs
uv run python cli.py transactions --account-id <id>
```

---

## Implementation Order

1. `docker-compose.yml` + `Dockerfile` + `.env.example`
2. DB models (`src/db/models.py`) + Alembic migration
3. `BankAdapter` ABC + Pydantic data models (`src/adapters/base.py`)
4. Stealth browser utilities (`src/core/stealth.py`)
5. LLM extraction loop (`src/agent/extractor.py`)
6. `HeritageBankAdapter` (`src/adapters/heritage.py`)
7. `GenericAdapter` (`src/adapters/generic.py`)
8. Restate workflow + steps (`src/worker/`)
9. CLI (`cli.py`)
10. README
