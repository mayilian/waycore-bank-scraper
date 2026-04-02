# WayCore Bank Scraper

Durable browser automation that logs into bank portals, completes OTP challenges, and extracts accounts, transactions, and balances into PostgreSQL. Survives crashes mid-sync, handles OTP pause/resume, writes idempotent data.

**Demo:** 3 accounts, 130 transactions, 3 balance snapshots from [Heritage Trust Bank](https://demo-bank-2.vercel.app) in ~60 seconds.

---

## Local Setup

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

Start services and run a sync:
```bash
docker compose up -d                # postgres + restate + worker + api
uv run alembic upgrade head         # run migrations
uv run waycore sync \
  --bank-url https://demo-bank-2.vercel.app \
  --username user --password pass --otp 123456
```

Expected output:
```
✓ Job created: 507abaf8-...
  Bank: heritage_bank  URL: https://demo-bank-2.vercel.app

Live step trace (polling every 2s):

  ✓ login                                     19.5s
  ✓ extract_583761204                         39.7s
  ✓ extract_583769842                         39.7s
  ✓ extract_739220031                         39.7s
  ✓ extract_all                               39.7s
  ✓ finalise                                  0.0s

✓ Sync complete. Accounts: 3  Transactions: 130
```

### Inspect results (CLI)

```bash
uv run waycore accounts       # list synced accounts
uv run waycore transactions   # list transactions
uv run waycore jobs           # list sync jobs
```

### Inspect results (API)

The API runs at `http://localhost:8000`. Generate a key, then query:

```bash
uv run waycore create-api-key --name test
# ✓ API key created: wc_DP3o_twmKxjj5MD9i4tUaakJEEhbbhOVDCfVnAK5ZbQ

export KEY=wc_DP3o_...  # your key from above

curl http://localhost:8000/v1/accounts      -H "Authorization: Bearer $KEY"
curl http://localhost:8000/v1/transactions   -H "Authorization: Bearer $KEY"
curl http://localhost:8000/v1/jobs           -H "Authorization: Bearer $KEY"
```

The CLI talks directly to Postgres (no auth, hardcoded dev user). The API is what real clients call — it requires a Bearer API key and scopes all data by tenant. Both read/write the same database.

API docs at `http://localhost:8000/docs` (Swagger UI).

### Stop

```bash
docker compose down
```

---

## AWS Setup (CDK)

One CDK stack deploys everything: VPC, RDS, ECS Fargate (API + Worker + Restate), ALB, S3, Secrets Manager.

**Prerequisites:** [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Node.js](https://nodejs.org/) (for CDK CLI), Docker, Python 3.12+

```bash
# Install CDK CLI (once)
npm install -g aws-cdk

# Configure AWS credentials
aws configure

# Bootstrap CDK (once per account/region)
cd deploy/cdk
pip install -r requirements.txt
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# Deploy
cdk deploy
```

This creates:

| Component | Details |
|---|---|
| **API** | ECS Fargate, ARM64, 0.25 vCPU / 512 MB, behind ALB |
| **Worker** | ECS Fargate Spot + on-demand base, ARM64, 1 vCPU / 2 GB |
| **Restate** | ECS Fargate, ARM64, 0.5 vCPU / 1 GB |
| **Database** | RDS PostgreSQL 16, db.t4g.micro, encrypted |
| **Screenshots** | S3, 30-day lifecycle |
| **Service discovery** | Cloud Map (`*.waycore.local`) |

### Post-deploy

Stack outputs print the ALB URL, ECR repo URIs, and secrets ARN.

```bash
# 1. Fill in secrets
aws secretsmanager put-secret-value --secret-id waycore/secrets \
  --secret-string '{"ENCRYPTION_KEY":"your-fernet-key","ANTHROPIC_API_KEY":"sk-ant-..."}'

# 2. Build and push Docker images to ECR
aws ecr get-login-password | docker login --username AWS --password-stdin YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com
docker build --platform linux/arm64 -t YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/waycore-api:latest .
docker push YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/waycore-api:latest
docker build --platform linux/arm64 -t YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/waycore-worker:latest .
docker push YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/waycore-worker:latest

# 3. Register worker with Restate (printed in stack outputs)
curl -X POST http://restate.waycore.local:9070/deployments \
  -H 'content-type: application/json' \
  -d '{"uri": "http://worker.waycore.local:9000"}'

# 4. Run migrations
aws ecs run-task --cluster waycore --task-definition WayCoreStack-WorkerTaskDef... \
  --overrides '{"containerOverrides":[{"name":"worker","command":["uv","run","alembic","upgrade","head"]}]}'
```

For Bedrock (no API key needed):
```bash
# Set LLM_PROVIDER=bedrock in the CDK stack environment — uses AWS IAM credentials automatically
```

### Teardown

Removes **everything** — no lingering resources, no surprise bills:

```bash
cd deploy/cdk
cdk destroy
```

All resources have `DESTROY` removal policies. For production, change to `RETAIN`/`SNAPSHOT` in `waycore_stack.py`.

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

### LLM Providers

| Provider | Config | Notes |
|---|---|---|
| Anthropic (default) | `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` | Direct API |
| Amazon Bedrock | `LLM_PROVIDER=bedrock` + `AWS_REGION` | Uses IAM credentials, no API key |
| OpenAI | `LLM_PROVIDER=openai` + `OPENAI_API_KEY` | GPT-4o |

### Data Model

- `NUMERIC(20,4)` + `Decimal` for all money
- Transactions: `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING`
- Balances: append-only (never UPDATE)
- `account_sync_results`: per-account outcome tracking (partial success)
- `sync_steps`: audit trail with screenshots on failure
- Credentials: MultiFernet-encrypted at rest, decrypted only in worker memory. Key rotation via `ENCRYPTION_KEY_PREVIOUS`.

### Multi-tenant Auth

API keys are SHA-256 hashed before storage. Lookup by hash + `hmac.compare_digest` for timing safety. All data queries scoped by `user_id` through `src/db/queries.py` — no raw `select(Model)` in route handlers.

---

## Configuration

All via environment variables:

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...localhost...` | Postgres connection |
| `DB_HOST` / `DB_PASSWORD` / ... | `""` | Individual fields (overrides DATABASE_URL when DB_HOST is set) |
| `ENCRYPTION_KEY` | (required) | Fernet key for credentials |
| `ENCRYPTION_KEY_PREVIOUS` | `""` | Old key during rotation |
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `bedrock`, or `openai` |
| `ANTHROPIC_API_KEY` | `""` | Required if LLM fallback triggers (anthropic provider) |
| `AWS_REGION` | `us-east-1` | AWS region for Bedrock |
| `RESTATE_INGRESS_URL` | `http://localhost:8080` | Restate endpoint |
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
cli.py                  CLI: sync, otp, jobs, transactions, accounts, create-api-key
src/
  api/                  FastAPI app: connections, syncs, accounts, transactions
    auth.py             API key auth → TenantContext (SHA-256 + hmac)
    schemas.py          Pydantic request/response models
    routes/             Thin route handlers → operations.py + queries.py
  adapters/             BankAdapter ABC + per-bank implementations
    heritage_bank_adapter.py   Demo bank: deterministic selectors + LLM fallback
    heritage_parsers.py        Pure parsing functions (no browser)
    generic_bank_adapter.py    LLM-driven adapter for unknown banks
  agent/
    llm.py              LLMClient protocol + providers (Anthropic, Bedrock, OpenAI)
    extractor.py        Per-goal LLM extraction with task-specific DOM observers
  core/
    config.py           pydantic-settings (all env vars)
    crypto.py           MultiFernet encrypt/decrypt with key rotation
    logging.py          structlog JSON + context binding
    metrics.py          CloudWatch EMF metric emitter
    operations.py       Shared business logic (CLI + API both call this)
    stealth.py          Playwright launch + stealth + bezier mouse
    urls.py             URL normalization
  db/
    models.py           SQLAlchemy models (Organization → User → ApiKey → BankConnection → ...)
    queries.py          Tenant-scoped query helpers
    session.py          Async session factory (RDS Proxy aware)
  worker/
    app.py              Restate ASGI app
    workflow.py         Durable workflow: login → extract_all → finalise
    steps.py            Step functions with batched DB writes
    concurrency.py      Per-bank semaphore
deploy/cdk/             CDK stack (VPC, RDS, ECS, ALB, S3)
alembic/                Database migrations
tests/                  Unit tests
```
