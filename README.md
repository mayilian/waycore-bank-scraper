# WayCore Bank Scraper

Durable browser automation that logs into bank portals, completes OTP challenges, and extracts accounts, transactions, and balances into PostgreSQL. Survives crashes mid-sync, handles OTP pause/resume, writes idempotent data.

**Demo result:** 3 accounts, 130 transactions, 3 balance snapshots from [Heritage Trust Bank](https://demo-bank-2.vercel.app) in ~60 seconds.

---

## Quick Start

### Prerequisites
- Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Docker & Docker Compose (recommended), **or** local PostgreSQL 16 + [Restate](https://restate.dev)

### Option A: Docker Compose (recommended)

```bash
git clone https://github.com/mayilian/waycore-bank-scraper.git
cd waycore-bank-scraper
uv sync --extra anthropic
uv run playwright install chromium
```

Create `.env`:
```bash
# Generate a Fernet encryption key
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > .env << EOF
ANTHROPIC_API_KEY=sk-ant-...your-key...
ENCRYPTION_KEY=$ENCRYPTION_KEY
EOF
```

Start services and run:
```bash
docker compose up -d                # postgres + restate + worker + register
uv run alembic upgrade head         # run migrations
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456
```

### Option B: Local services (no Docker)

```bash
# Install local services
brew install postgresql@16 && brew services start postgresql@16
brew install restatedev/tap/restate-server

# Create database
createdb waycore
psql -d waycore -c "CREATE USER waycore WITH PASSWORD 'waycore'; GRANT ALL ON DATABASE waycore TO waycore;"
psql -d waycore -c "GRANT ALL ON SCHEMA public TO waycore;"

# Clone and install
git clone https://github.com/mayilian/waycore-bank-scraper.git
cd waycore-bank-scraper
uv sync --extra anthropic
uv run playwright install chromium
```

Create `.env`:
```bash
ENCRYPTION_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

cat > .env << EOF
ANTHROPIC_API_KEY=sk-ant-...your-key...
ENCRYPTION_KEY=$ENCRYPTION_KEY
DATABASE_URL=postgresql+asyncpg://waycore:waycore@localhost:5432/waycore
RESTATE_INGRESS_URL=http://localhost:8080
EOF
```

Start services:
```bash
# Terminal 1: Restate server
restate-server --listen-mode tcp

# Terminal 2: Worker
uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"

# Terminal 3: Register worker (once), then run migrations + sync
curl -X POST http://localhost:9070/deployments \
  -H "Content-Type: application/json" \
  -d '{"uri": "http://localhost:9000"}'

uv run alembic upgrade head
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456
```

### Expected output

```
✓ Job created: 690b58ab-...
  Bank: heritage_bank  URL: https://demo-bank-2.vercel.app/

Live step trace (polling every 2s):

  ✓ login                                     18.9s
  ✓ extract_583761204                         40.0s
  ✓ extract_583769842                         40.0s
  ✓ extract_739220031                         40.0s
  ✓ extract_all                               40.0s
  ✓ finalise                                  0.0s

✓ Sync complete. Accounts: 3  Transactions: 130
```

### Inspect results

```bash
uv run waycore accounts       # list synced accounts
uv run waycore transactions   # list transactions
uv run waycore jobs           # list sync jobs
```

### Common commands (Makefile)

```bash
make install        # install deps + Playwright browser
make migrate        # run database migrations
make test           # run tests
make lint           # run ruff linter
make check          # lint + tests
make format         # auto-format code
make run-restate    # start Restate server (terminal 1)
make run-worker     # start worker (terminal 2)
make register       # register worker with Restate (once)
make sync           # run demo bank sync
make accounts       # list synced accounts
make transactions   # list recent transactions
make jobs           # list sync jobs
```

---

## Architecture

```
CLI (typer) → Restate (durable workflow) → Worker (Playwright + LLM) → PostgreSQL
```

### Workflow design

Step boundaries are aligned with browser session economics:

```
Browser #1 (login)       — login, OTP, capture session cookies
Browser #2 (extract_all) — restore session, discover accounts,
                           extract txns + balance for ALL accounts
                           in one browser session
(no browser) finalise    — mark job complete
```

This gives **2 browser launches per sync** regardless of account count. Restate journals each step — if the worker crashes, replay resumes from the last completed step. OTP pause (`ctx.promise`) suspends with zero resources until the code arrives.

### Tiered extraction

```
Tier 1 — Deterministic DOM parsing    Known selectors, direct table reads.
                                      Zero LLM cost. Sub-second extraction.
                                      Built per bank during onboarding.
         ↓ (selector miss)
Tier 2 — LLM text-only               Task-specific DOM summary → LLM → JSON.
                                      ~2K tokens, handles UI changes
                                      automatically.
         ↓ (ambiguous DOM)
Tier 3 — LLM vision                  DOM + screenshot → LLM. Expensive
                                      but handles anything. Used by
                                      GenericAdapter for unknown banks.
```

The Heritage adapter (demo bank) runs Tier 1 for all extraction in the happy path. LLM fallback is embedded at every stage (login, OTP, accounts, navigation, transactions, balance) and activates automatically when selectors break. A diagnostic screenshot is captured on each fallback for debugging.

**Note:** The LLM client is never instantiated unless a fallback is triggered. Pure Tier 1 runs make zero API calls.

### LLM provider

The LLM provider is pluggable via `LLM_PROVIDER` env var:

| Provider | Install | Config |
|---|---|---|
| Anthropic (default) | `uv sync --extra anthropic` | `ANTHROPIC_API_KEY`, `LLM_MODEL` (default: claude-sonnet-4-6) |
| OpenAI | `uv sync --extra openai` | `OPENAI_API_KEY`, `LLM_MODEL` (default: gpt-4o) |

### Browser policy

Each adapter declares a `BrowserPolicy` (viewport, locale, timezone, user agent) so stealth configuration matches the bank's expectations. The default policy uses common US desktop settings. Per-bank overrides are set in the adapter class.

### Performance

The real throughput constraint is browser lifecycle, not orchestration:
- Two browser launches per sync (login + extract_all) regardless of account count
- SPA navigation + rendering adds ~3-5s per account-detail page
- Stealth measures (bezier mouse, per-key typing) add latency on the generic path
- All accounts are extracted sequentially in one browser session

Restate scales orchestration horizontally, but actual throughput is limited by browser memory, anti-bot pacing, and LLM latency when fallbacks activate. A warm browser pool (borrowing pre-launched browsers instead of starting fresh) would reduce startup cost further — the adapter/step interfaces are structured to support this, but it is not yet implemented.

### Data model

- `NUMERIC(20,4)` + Python `Decimal` for all money — no float precision loss
- Transactions: `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING` — idempotent syncs
- Balances: append-only (never UPDATE) — full balance history
- `account_sync_results`: first-class per-account outcome tracking (partial success)
- `sync_steps`: audit trail with screenshot paths and tracebacks on failure
- `login_url_normalized`: stable connection identity — `normalize_url()` strips trailing slashes, `www.`, default ports, lowercases host. CLI matches on this column, preventing duplicate connections from cosmetic URL differences
- Composite indexes on `(account_id, captured_at)`, `(connection_id, status)`, `(account_id, sync_job_id)` for cloud-scale query performance
- Multi-tenant schema (organizations → users → bank_connections → accounts), currently single-tenant in application behavior (CLI hardcodes demo org/user)

### Operational caps

Hard limits prevent runaway syncs from burning resources:

| Cap | Default | Controls |
|---|---|---|
| `MAX_SYNC_DURATION_SECS` | `600` (10 min) | `asyncio.wait_for` timeout on the entire workflow |
| `MAX_PAGES_PER_ACCOUNT` | `50` | Pagination loop limit in both Heritage and Generic adapters |
| `MAX_LLM_CALLS_PER_SYNC` | `100` | Per-sync LLM API call budget — raises `RuntimeError` on exceed |

All three are configurable via environment variables.

---

## OTP Modes

| Mode | Use case | How |
|---|---|---|
| `static` | Demo banks, fixed OTP | `--otp 123456` |
| `webhook` | OTP via SMS/email | Omit `--otp`, then run `waycore otp --job-id <id> --code <code>` when it arrives |

---

## Configuration

All settings are environment variables (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://waycore:waycore@localhost:5432/waycore` | Postgres connection |
| `DB_POOL_SIZE` | `5` | Connection pool size |
| `DB_MAX_OVERFLOW` | `10` | Max overflow connections |
| `DB_POOL_RECYCLE` | `3600` | Recycle connections after N seconds |
| `ENCRYPTION_KEY` | (required) | Fernet key for credential encryption |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `LLM_MODEL` | provider default | Override model name |
| `ANTHROPIC_API_KEY` | `""` | Required if LLM_PROVIDER=anthropic and fallback triggers |
| `OPENAI_API_KEY` | `""` | Required if LLM_PROVIDER=openai |
| `RESTATE_INGRESS_URL` | `http://localhost:8080` | Restate ingress endpoint |
| `WORKER_PORT` | `9000` | Hypercorn bind port |
| `PLAYWRIGHT_HEADFUL` | `0` | Set to `1` for headed browser (debugging) |
| `BROWSER_USER_AGENT` | Chrome 131 UA | Browser user agent string |
| `BROWSER_LOCALE` | `en-US` | Browser locale |
| `BROWSER_TIMEZONE` | `America/New_York` | Browser timezone |
| `MAX_SYNC_DURATION_SECS` | `600` | Hard timeout (seconds) per sync job |
| `MAX_PAGES_PER_ACCOUNT` | `50` | Max pagination pages per account |
| `MAX_LLM_CALLS_PER_SYNC` | `100` | LLM API call budget per sync |
| `SCREENSHOT_BACKEND` | `local` | `local` or `s3` (requires `uv sync --extra s3`) |

Docker Compose ports are configurable via env vars: `DB_PORT`, `RESTATE_INGRESS_PORT`, `RESTATE_ADMIN_PORT`, `WORKER_PORT`.

---

## Adding a New Bank

1. Create `src/adapters/<slug>_adapter.py` implementing `BankAdapter`
2. Optionally set `browser_policy` for locale/timezone matching
3. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`
4. CLI auto-detects the slug from the URL domain

Unknown bank URLs automatically use `GenericBankAdapter` (Tier 3 — full LLM).

## Project Structure

```
cli.py              CLI entry point: sync, otp, jobs, transactions, accounts
src/
  adapters/         BankAdapter ABC + per-bank implementations + parsers
  agent/            LLM client abstraction + per-goal extraction functions
  core/             Config, Fernet crypto, structlog, Playwright stealth, URL normalization
  db/               SQLAlchemy models, async session factory
  worker/           Restate workflow, step implementations, ASGI app
tests/              Unit tests (crypto, models, adapters, extractor, URL normalization)
alembic/            Database migrations (URL normalization, indexes, data dedup)
```

## Roadmap

### Done (recent)

- [x] **URL normalization** — Stable connection identity via `normalize_url()`. Prevents duplicate connections from trailing slashes, `www.`, default ports.
- [x] **Cloud-ready indexes** — Composite indexes on high-query-volume columns (balances, sync_jobs, account_sync_results).
- [x] **Operational caps** — Hard limits on sync duration, pagination depth, and LLM call budget.
- [x] **Data deduplication** — Migration merges duplicate connections created by URL variants, re-parents accounts/transactions/balances.

### Must do

- [ ] **Infinite scroll handling** — Only "Next" button pagination works. Banks using infinite scroll need: scroll to bottom → wait for new rows → extract → repeat.
- [ ] **CSV/PDF export fast path** — `try_export()` on BankAdapter. Download + parse is faster and more reliable than DOM parsing for bulk history.

### Should do

- [ ] **Warm browser pool** — Pre-launch browsers, hand out fresh contexts per step. Reduces ~3-5s startup cost per browser launch.
- [ ] **GenericAdapter end-to-end testing** — All testing was against Heritage (demo bank). Generic adapter needs validation against a second bank.
- [ ] **Auto-promote LLM discoveries** — When LLM fallback finds working selectors, cache them per `bank_slug` so next sync uses Tier 1.
- [ ] **Parallel account extraction** — Extract transactions for multiple accounts concurrently (separate browser tabs within one context).
- [ ] **Date-range pagination** — For banks without Next buttons, iterate by date range (monthly chunks).

### Design scope (not handling yet)

- **CAPTCHA solving** — If a bank presents a CAPTCHA, the sync fails with a screenshot.
- **Multi-factor beyond OTP** — Push notifications, biometric, hardware keys not supported. Only numeric OTP codes.
- **Real-time / streaming sync** — Syncs are batch operations triggered by CLI.

---

## Production Deployment

| Component | Local | Production |
|---|---|---|
| Database | Local PostgreSQL | Neon / RDS |
| Workflow engine | Local Restate | Restate Cloud |
| Screenshots | Local filesystem | S3 / Cloudflare R2 (`uv sync --extra s3`) |
| Compute | `hypercorn` | ECS / Kubernetes |

Configuration is via environment variables. Cloud migration requires updating `DATABASE_URL`, `RESTATE_INGRESS_URL`, screenshot backend settings, and `BROWSER_*` settings to match the deployment region. All ports and pool settings are configurable.

**Demo shortcuts still in code:** Single-tenant org/user IDs in `cli.py` (hardcoded UUIDs for the demo — production would use real auth).
