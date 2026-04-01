# WayCore Bank Scraper

Extracts balances and full transaction history from bank web portals via authenticated browser automation. Built for reliability at scale: each sync is a durable, checkpointed workflow that survives worker crashes, handles OTP mid-flight, and writes idempotent data.

---

## Quick Start

### Prerequisites
- Docker + Docker Compose
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An [Anthropic API key](https://console.anthropic.com/)

### 1. Configure

```bash
cp .env.example .env
# Edit .env and set:
#   ANTHROPIC_API_KEY=sk-ant-...
#   ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
```

### 2. Start infrastructure

```bash
docker compose up -d
```

This starts PostgreSQL, Restate, the sync worker, and auto-registers the worker with Restate.

### 3. Run migrations

```bash
uv run alembic upgrade head
```

### 4. Sync the demo bank

```bash
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user \
  --password pass \
  --otp 123456
```

Live step trace prints to the terminal as the workflow progresses:

```
✓ Job created: a3f1bc2d-...
  Bank: heritage_bank  URL: https://demo-bank-2.vercel.app

Live step trace (polling every 2s):

  ✓ login                                    4.2s
  ✓ get_accounts                             2.8s
  ✓ transactions_ACC001                     18.3s
  ✓ balance_ACC001                           1.1s
  ✓ finalise                                 0.2s

✓ Sync complete. Accounts: 1  Transactions: 847
```

### 5. Inspect results

```bash
uv run waycore accounts
uv run waycore transactions
uv run waycore jobs
```

---

## OTP Modes

| Mode | When to use | How to provide |
|---|---|---|
| `static` | Demo banks, fixed OTP | `--otp 123456` on the `sync` command |
| `totp` | TOTP authenticator app | `--otp $(totp-cli generate ...)` |
| `webhook` | OTP arrives via SMS/email | Omit `--otp`; run `waycore otp` when it arrives |

For webhook mode the workflow suspends (zero resources held) until you send the OTP:

```bash
uv run waycore otp --job-id <id> --code 123456
```

---

## Architecture

### What type of workload is this?

A **durable, stateful, I/O-bound automation workflow**. Each sync is a sequential chain of steps (login → OTP → extract accounts → extract transactions → persist) with these properties:

- Steps must execute in order and be checkpointed *individually* — a crash mid-extraction should resume from the last completed step, not re-login from scratch
- External dependencies (bank websites) are unreliable — per-step retry, not whole-job retry
- Human input may arrive mid-flight (OTP codes)
- Re-running a sync must never produce duplicate data

### What's the best way to execute this workload?

**Durable execution via [Restate](https://restate.dev/).** A job queue knows if a job succeeded or failed. Restate knows *which step* succeeded — replay starts from the right place. The OTP pause (`ctx.promise().value()`) suspends the workflow with zero resources held until the signal arrives. Each `ctx.run("step_name", fn)` is automatically journaled and retried on failure.

### Could it scale horizontally?

Yes. Workers are stateless HTTP services. Restate routes each step invocation to any live instance. To handle more concurrent syncs:

```bash
docker compose up -d --scale worker=5
```

No other changes. The constraint is browser memory (~400 MB per Chromium instance), not DB throughput. For production at true scale: deploy workers as containers on ECS or Kubernetes, point `RESTATE_ENDPOINT` at Restate Cloud, and `DATABASE_URL` at a managed Postgres (Neon, RDS). No code changes required.

### Three execution layers

```
Layer 1 — Playwright + stealth      Always runs. Drives the browser.
                                    Bezier mouse, human-paced typing,
                                    anti-detection patches. No opinion
                                    about what to do.
       ↓
Layer 2 — LLM (Claude claude-sonnet-4-6)    Always runs. Multiple focused calls
                                    per sync phase. Each call receives a
                                    trimmed DOM summary + screenshot and
                                    returns structured JSON.
       ↓
Layer 3 — Bank adapter              Sequences goals. HeritageBankAdapter
                                    passes known selector hints (fast path).
                                    GenericBankAdapter lets the LLM discover
                                    everything (any unknown bank URL).
```

### Data model highlights

- `Decimal` in Python + `NUMERIC(20,4)` in Postgres for all money columns — no float precision loss
- `balances` is append-only (never updated) — enables balance history
- `transactions` uses `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING` — all syncs are idempotent
- Multi-tenant schema (`organizations → users → bank_connections → accounts`) — ready for RLS enforcement in production
- `sync_steps` is the audit trail: each completed step writes a record; failures include screenshot path and traceback

---

## Project Structure

```
src/
  adapters/       BankAdapter ABC, HeritageBankAdapter, GenericBankAdapter
  agent/          LLM extraction (per-goal focused calls)
  core/           Config, Fernet crypto, structlog, Playwright stealth, screenshots
  db/             SQLAlchemy models, async session factory
  worker/         Restate workflow, step implementations, ASGI app
cli.py            Typer CLI
alembic/          DB migrations
docker-compose.yml
Dockerfile
```

## Adding a New Bank

1. Create `src/adapters/<slug>.py` implementing `BankAdapter`
2. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`
3. The CLI auto-detects the slug from the URL domain

Unknown banks automatically use `GenericBankAdapter` — the LLM discovers the login form, OTP flow, accounts, and transactions from scratch.

## Cloud Extension

| Component | Local | Cloud |
|---|---|---|
| Database | Docker `postgres:16` | Set `DATABASE_URL` → Neon / RDS |
| Workflow engine | Docker `restatedev/restate` | Set `RESTATE_ENDPOINT` → Restate Cloud |
| Screenshots | Local volume | Set `SCREENSHOT_BACKEND=s3` + Cloudflare R2 credentials |
| Compute | `docker compose up` | `fly deploy` |

No code changes required for any of these migrations.
