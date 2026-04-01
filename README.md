# WayCore Bank Scraper

Durable browser automation that logs into bank portals, completes OTP challenges, and extracts accounts, transactions, and balances into PostgreSQL. Survives crashes mid-sync, handles OTP pause/resume, writes idempotent data.

**Demo result:** 3 accounts, 130 transactions, 3 balance snapshots from [Heritage Trust Bank](https://demo-bank-2.vercel.app) in ~2 minutes.

---

## Local Setup (copy-paste)

### Prerequisites
- Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/)
- PostgreSQL 16 (`brew install postgresql@16 && brew services start postgresql@16`)
- [Restate](https://restate.dev) (`brew install restatedev/tap/restate-server`)
- [Anthropic API key](https://console.anthropic.com/) (only needed for LLM fallback)

### 1. Clone and install

```bash
git clone https://github.com/mayilian/waycore-bank-scraper.git
cd waycore-bank-scraper
uv sync
uv run playwright install chromium
```

### 2. Create database

```bash
createdb waycore
psql -d waycore -c "CREATE USER waycore WITH PASSWORD 'waycore'; GRANT ALL ON DATABASE waycore TO waycore;"
psql -d waycore -c "GRANT ALL ON SCHEMA public TO waycore;"
uv run alembic upgrade head
```

### 3. Configure

```bash
cat > .env << 'EOF'
ANTHROPIC_API_KEY=sk-ant-...your-key...
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
DATABASE_URL=postgresql+asyncpg://waycore:waycore@localhost:5432/waycore
RESTATE_INGRESS_URL=http://localhost:8080
PLAYWRIGHT_HEADFUL=0
EOF
```

### 4. Start services

```bash
# Terminal 1: Restate server
restate-server --listen-mode tcp

# Terminal 2: Worker
uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"

# Terminal 3: Register worker (once)
curl -X POST http://localhost:9070/deployments \
  -H "Content-Type: application/json" \
  -d '{"uri": "http://localhost:9000"}'
```

### 5. Run sync

```bash
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456
```

```
✓ Job created: b9103d52-...
  Bank: heritage_bank  URL: https://demo-bank-2.vercel.app

Live step trace (polling every 2s):

  ✓ login                                     20.2s
  ✓ get_accounts                              9.4s
  ✓ transactions_583761204                    17.2s
  ✓ balance_583761204                         17.6s
  ✓ transactions_583769842                    16.7s
  ✓ balance_583769842                         16.5s
  ✓ transactions_739220031                    16.7s
  ✓ balance_739220031                         17.1s
  ✓ finalise                                  0.0s

✓ Sync complete. Accounts: 3  Transactions: 130
```

### 6. Inspect results

```bash
uv run waycore accounts       # list synced accounts
uv run waycore transactions   # list transactions
uv run waycore jobs           # list sync jobs
```

---

## Architecture

```
CLI (typer) → Restate (durable workflow) → Worker (Playwright + LLM) → PostgreSQL
```

### Why Restate?

Each sync is a chain of steps: login → OTP → accounts → per-account transactions → per-account balance. Restate journals each step — if the worker crashes mid-extraction, replay resumes from the last completed step. No re-login, no duplicate data. OTP pause (`ctx.promise`) suspends with zero resources until the code arrives.

### Tiered extraction (the key design)

```
Tier 1 — Deterministic DOM parsing    Known selectors, direct table reads.
                                      Zero LLM cost. Sub-second extraction.
                                      Built per bank during onboarding.
         ↓ (selector miss)
Tier 2 — LLM text-only               DOM summary → Claude → structured JSON.
                                      ~2K tokens, handles UI changes
                                      automatically.
         ↓ (ambiguous DOM)
Tier 3 — LLM vision                  DOM + screenshot → Claude. Expensive
                                      but handles anything. Used by
                                      GenericAdapter for unknown banks.
```

The Heritage adapter (demo bank) runs entirely on Tier 1 — zero API calls in the happy path.

### Data model

- `NUMERIC(20,4)` + Python `Decimal` for all money — no float precision loss
- Transactions: `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING` — idempotent syncs
- Balances: append-only (never UPDATE) — full balance history
- `sync_steps`: audit trail with screenshot paths and tracebacks on failure
- Multi-tenant: organizations → users → bank_connections → accounts → transactions/balances

---

## OTP Modes

| Mode | Use case | How |
|---|---|---|
| `static` | Demo banks, fixed OTP | `--otp 123456` |
| `webhook` | OTP via SMS/email | Omit `--otp`, then run `waycore otp --job-id <id> --code <code>` when it arrives |

---

## Adding a New Bank

1. Create `src/adapters/<slug>.py` implementing `BankAdapter`
2. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`
3. CLI auto-detects the slug from the URL domain

Unknown bank URLs automatically use `GenericBankAdapter` (Tier 3 — full LLM).

## Project Structure

```
cli.py              CLI entry point: sync, otp, jobs, transactions, accounts
src/
  adapters/         BankAdapter ABC + per-bank implementations
  agent/            LLM extraction (per-goal focused calls, vision fallback)
  core/             Config, Fernet crypto, structlog, Playwright stealth
  db/               SQLAlchemy models, async session factory
  worker/           Restate workflow, step implementations, ASGI app
tests/              Unit tests (crypto, models, adapters, extractor)
alembic/            Database migrations
```

## Production Deployment

| Component | Local | Production |
|---|---|---|
| Database | Local PostgreSQL | Neon / RDS |
| Workflow engine | Local Restate | Restate Cloud |
| Screenshots | Local filesystem | S3 / Cloudflare R2 |
| Compute | `hypercorn` | ECS / Kubernetes |

No code changes required — configuration only via environment variables.
