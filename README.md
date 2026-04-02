# WayCore Bank Scraper

Durable browser automation that logs into bank portals, completes OTP challenges, and extracts accounts, transactions, and balances into PostgreSQL. Survives crashes mid-sync, handles OTP pause/resume, writes idempotent data.

**Demo:** 3 accounts, 130 transactions, 3 balance snapshots from [Heritage Trust Bank](https://demo-bank-2.vercel.app) in ~60 seconds.

---

## Quick Start

**Prerequisites:** Docker, Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
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
EOF
```

Start everything:
```bash
docker compose up -d                # postgres + restate + worker + api + register
uv run alembic upgrade head         # run migrations
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456
```

Expected output:
```
✓ Job created: 690b58ab-...
  Bank: heritage_bank  URL: https://demo-bank-2.vercel.app

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

---

## API

The API runs alongside the worker. With `docker compose up -d`, it's at `http://localhost:8000`.

```bash
# Create a connection and trigger sync
curl -X POST http://localhost:8000/v1/connections \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"bank_url":"https://demo-bank-2.vercel.app","username":"user","password":"pass","otp_mode":"static","otp":"123456"}'

# Trigger a sync
curl -X POST http://localhost:8000/v1/connections/{connection_id}/sync \
  -H "Authorization: Bearer YOUR_API_KEY"

# Check job status
curl http://localhost:8000/v1/jobs/{job_id} \
  -H "Authorization: Bearer YOUR_API_KEY"

# List accounts and transactions
curl http://localhost:8000/v1/accounts -H "Authorization: Bearer YOUR_API_KEY"
curl http://localhost:8000/v1/transactions -H "Authorization: Bearer YOUR_API_KEY"
```

API docs at `http://localhost:8000/docs` (auto-generated Swagger UI).

---

## Architecture

```
API (FastAPI) ──→ Restate (durable workflow) ──→ Worker (Playwright + LLM) ──→ PostgreSQL
     ↑                                                                              ↑
  Tenant auth                                                              Idempotent writes
  (API key)                                                               (ON CONFLICT DO NOTHING)
```

**Two services, one image:**
- **API** (port 8000): Creates connections, triggers syncs, returns data. No browser. 256MB RAM.
- **Worker** (port 9000): Runs Playwright, drives browsers, extracts data. 2GB RAM.

### Workflow

```
Browser #1 (login)       → login, OTP, capture session cookies
Browser #2 (extract_all) → restore session, discover accounts,
                           extract txns + balance for ALL accounts
(no browser) finalise    → mark job complete
```

**2 browser launches per sync** regardless of account count. Restate journals each step — if the worker crashes, replay resumes from the last checkpoint.

### Tiered Extraction

```
Tier 1 — Deterministic DOM    Known selectors. Zero LLM cost. Sub-second.
         ↓ (selector miss)
Tier 2 — LLM text fallback    DOM summary → LLM → JSON. ~2K tokens.
         ↓ (ambiguous DOM)
Tier 3 — LLM vision           DOM + screenshot → LLM. Handles anything.
```

Heritage adapter runs Tier 1 in the happy path. LLM is never instantiated unless a fallback triggers.

### Data Model

- `NUMERIC(20,4)` + `Decimal` for all money
- Transactions: `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING`
- Balances: append-only (never UPDATE)
- `account_sync_results`: per-account outcome tracking (partial success)
- `sync_steps`: audit trail with screenshots on failure
- Credentials: Fernet-encrypted at rest, decrypted only in worker memory

### Operational Caps

| Cap | Default | What it does |
|---|---|---|
| `MAX_SYNC_DURATION_SECS` | 600 | Hard timeout per sync |
| `MAX_PAGES_PER_ACCOUNT` | 50 | Pagination limit |
| `MAX_LLM_CALLS_PER_SYNC` | 100 | LLM API call budget |
| `MAX_CONCURRENT_PER_BANK` | 3 | Simultaneous syncs per bank |

---

## Deploy to AWS (CDK)

**Prerequisites:** [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Node.js](https://nodejs.org/) (for CDK CLI), Python 3.12+

```bash
# Install CDK CLI (once)
npm install -g aws-cdk

# Configure AWS credentials
aws configure --profile personal

# Deploy everything (VPC, RDS, ECS, ALB — one command)
cd deploy/cdk
pip install -r requirements.txt
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1
cdk deploy --profile personal
```

This creates:

| Component | Service | Size |
|---|---|---|
| API | ECS Fargate (ARM64) | 0.25 vCPU / 512 MB |
| Worker | ECS Fargate Spot (ARM64) | 1 vCPU / 2 GB |
| Restate | ECS Fargate (ARM64) | 0.5 vCPU / 1 GB |
| Database | RDS PostgreSQL 16 | db.t4g.micro |
| Screenshots | S3 (30-day lifecycle) | — |

After deploy, fill in secrets and push Docker images:
```bash
# Update secrets (printed in stack outputs)
aws secretsmanager put-secret-value --secret-id waycore/secrets \
  --secret-string '{"ENCRYPTION_KEY":"your-fernet-key","ANTHROPIC_API_KEY":"sk-ant-..."}'

# Build and push to ECR (repo URIs in stack outputs)
docker buildx build --platform linux/arm64 -t $API_REPO_URI:latest --push .
docker buildx build --platform linux/arm64 -t $WORKER_REPO_URI:latest --push .
```

### Teardown

Removes **everything** — no lingering resources, no surprise bills:

```bash
cd deploy/cdk
cdk destroy --profile personal
```

RDS, S3, ECR all have `DESTROY` removal policies — `cdk destroy` deletes them completely. For production, change these to `RETAIN`/`SNAPSHOT` in `waycore_stack.py`.

**Cost at scale:** ~$0.001/sync compute. 10K connections = ~$370/mo total. See [docs/ecs-cost-analysis.md](docs/ecs-cost-analysis.md).

---

## Configuration

All via environment variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://waycore:waycore@localhost:5432/waycore` | Postgres |
| `ENCRYPTION_KEY` | (required) | Fernet key for credentials |
| `ENCRYPTION_KEY_PREVIOUS` | `""` | Old key during rotation |
| `LLM_PROVIDER` | `anthropic` | `anthropic` or `openai` |
| `ANTHROPIC_API_KEY` | `""` | Required if LLM fallback triggers |
| `RESTATE_INGRESS_URL` | `http://localhost:8080` | Restate endpoint |
| `USE_RDS_PROXY` | `false` | Use NullPool when behind RDS Proxy |
| `SCREENSHOT_BACKEND` | `local` | `local` or `s3` |
| `MAX_SYNC_DURATION_SECS` | `600` | Sync timeout |
| `MAX_CONCURRENT_PER_BANK` | `3` | Per-bank concurrency limit |

---

## Adding a New Bank

1. Create `src/adapters/<slug>_adapter.py` implementing `BankAdapter`
2. Register in `ADAPTER_REGISTRY` in `src/adapters/__init__.py`
3. Unknown URLs automatically use `GenericBankAdapter` (full LLM)

---

## Project Structure

```
cli.py                  CLI (typer): sync, otp, jobs, transactions, accounts
src/
  api/                  FastAPI app: connections, syncs, accounts, transactions
    auth.py             API key auth → TenantContext
    schemas.py          Request/response models
    routes/             Route handlers (thin — call operations.py + queries.py)
  adapters/             BankAdapter ABC + per-bank implementations
  agent/                LLM client + per-goal extraction functions
  core/
    config.py           pydantic-settings
    crypto.py           Fernet encrypt/decrypt (MultiFernet for rotation)
    logging.py          structlog JSON + context binding
    metrics.py          CloudWatch EMF metric emitter
    operations.py       Shared business logic (CLI + API)
    stealth.py          Playwright launch + stealth
    urls.py             URL normalization
  db/
    models.py           SQLAlchemy models (Organization → User → ApiKey → BankConnection → ...)
    queries.py          Tenant-scoped query helpers
    session.py          Async session factory (RDS Proxy aware)
  worker/
    app.py              Restate ASGI app
    workflow.py          Durable workflow: login → extract_all → finalise
    steps.py            Step functions with batched DB writes
    concurrency.py      Per-bank semaphore
deploy/                 ECS task definitions, service configs
alembic/                Database migrations
tests/                  Unit tests
```
