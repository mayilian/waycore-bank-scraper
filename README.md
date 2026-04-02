# WayCore Bank Scraper

Durable browser automation that logs into bank portals, completes OTP challenges, and extracts accounts, transactions, and balances into PostgreSQL. Survives crashes mid-sync, handles OTP pause/resume, writes idempotent data.

**Demo:** 3 accounts, 130 transactions, 3 balance snapshots from [Heritage Trust Bank](https://demo-bank-2.vercel.app) in ~60 seconds.

## Table of Contents

- [Architecture](#architecture)
- [Design Decisions & Tradeoffs](#design-decisions--tradeoffs)
- [AWS Deployment (CDK)](#aws-deployment-cdk)
- [Local Setup](#local-setup)
- [LLM Providers](#llm-providers)
- [Configuration](#configuration)
- [Adding a New Bank](#adding-a-new-bank)
- [Project Structure](#project-structure)
- [Stress Test Results](#stress-test-results)

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

---

## Design Decisions & Tradeoffs

### Problems solved

| Problem | Solution | Why it matters |
|---|---|---|
| **Browser launches are expensive** (2-4s cold start, 200MB+ each) | 2 browsers per sync regardless of account count. Login gets its own browser (OTP pause/resume requires it). All account extraction shares one browser session. | A naive "1 browser per account" design would cost 10s+ and 2GB+ for a 10-account bank. This keeps it flat at ~400MB and ~20s for the browser phase. |
| **LLM calls are slow and expensive** | Tiered extraction: deterministic DOM selectors first, LLM only as fallback. Per-goal focused prompts (not one mega-prompt). Task-specific DOM observers strip irrelevant HTML before sending to LLM. | Heritage Bank demo runs at zero LLM cost. When LLM does trigger, each call sees only the relevant DOM slice (~2K tokens vs ~50K for full page). |
| **Bank scraping is inherently flaky** | Restate durable workflows journal every step. Crash mid-extraction → restart from last checkpoint, not from login. Per-account `AccountSyncResult` tracking enables partial success. | A 30-minute sync that crashes at account #9 of 10 doesn't lose the first 8. Failed accounts are retried independently. |
| **OTP requires human-in-the-loop** | Restate `ctx.promise()` suspends the workflow with zero resources held. No browser open, no memory consumed while waiting. CLI or API sends the OTP code, workflow resumes. | Webhook OTP can wait minutes/hours. Holding a browser open during that time is wasteful and fragile. |
| **Money precision** | `NUMERIC(20,4)` + Python `Decimal` everywhere. No floats. | IEEE 754 floating point loses precision on currency. $0.10 + $0.20 ≠ $0.30 in float math. |
| **Credential security** | MultiFernet encryption at rest. Decrypt only in worker memory. Key rotation via `ENCRYPTION_KEY_PREVIOUS` — no downtime, no re-encryption migration needed. Never logged, never in API responses. | Credentials in plaintext in a database is a compliance and security failure. MultiFernet makes rotation zero-downtime. |
| **Duplicate data on re-sync** | `ON CONFLICT (account_id, external_id) DO NOTHING` on all transaction inserts. Balances are append-only (never UPDATE). | Re-running a sync is safe. No duplicate transactions, no overwritten balance history. |
| **Multi-tenant data isolation** | All queries scoped by `user_id` via `src/db/queries.py`. No raw `select(Model)` in API routes. API keys SHA-256 hashed + `hmac.compare_digest` for timing safety. | App-level tenant isolation prevents data leakage. Timing-safe comparison prevents key enumeration. |
| **Per-bank rate limiting** | `asyncio.Semaphore` per bank slug (`MAX_CONCURRENT_PER_BANK=3`). | Banks rate-limit or block IPs on parallel logins. Without this, 10 concurrent syncs to the same bank would get IP-banned. |

### Tradeoffs accepted

| Tradeoff | Chose | Over | Rationale |
|---|---|---|---|
| **Extraction strategy** | Deterministic selectors with LLM fallback | Pure LLM for everything | 10x faster, zero cost for known banks. LLM still handles unknown banks via `GenericBankAdapter`. |
| **Browser sessions** | 2 browsers per sync (login + extract_all) | 1 browser for everything | Login needs its own session for OTP pause/resume. Could be 1 if OTP weren't a requirement, but it is. |
| **Restate deployment** | Single self-hosted instance | Restate Cloud or distributed | Simpler, cheaper, sufficient for current scale. Restate single-node handles thousands of concurrent workflows. |
| **Worker scaling** | Fargate Spot (80%) + on-demand base (20%) | All on-demand | 60-70% cost savings. Spot interruptions are fine — Restate replays from the last checkpoint. |
| **CDK split** | Two stacks (Foundation + App) | Single stack | Solves ECR chicken-and-egg: Foundation creates repos, images get pushed, then App creates services that pull them. |
| **DB connection pooling** | SQLAlchemy async pool (configurable) + RDS Proxy support | PgBouncer sidecar | Fewer moving parts. RDS Proxy handles connection multiplexing in production. Toggle with `USE_RDS_PROXY=true`. |
| **Screenshot storage** | Local filesystem (dev) / S3 (prod) | Always S3 | Local is simpler for dev. S3 with 30-day lifecycle for prod — failure screenshots auto-expire. |

---

## AWS Deployment (CDK)

Two CDK stacks: **Foundation** (VPC, RDS, ECR, S3, Secrets, Restate) and **App** (API + Worker services). Deploy Foundation first, push Docker images to ECR, then deploy App.

**Prerequisites:** [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Node.js](https://nodejs.org/) (for CDK CLI), Docker, Python 3.12+, [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) (for ECS exec)

| Component | Details |
|---|---|
| **API** | ECS Fargate, ARM64, 0.25 vCPU / 512 MB, behind ALB |
| **Worker** | ECS Fargate Spot + on-demand base, ARM64, 1 vCPU / 2 GB |
| **Restate** | ECS Fargate, ARM64, 0.5 vCPU / 1 GB |
| **Database** | RDS PostgreSQL 16, db.t4g.micro, encrypted, 7-day backups |
| **Screenshots** | S3, 30-day lifecycle, encryption at rest |
| **Service discovery** | Cloud Map (`*.waycore.local`) — private DNS for inter-service communication |

### Step-by-step deployment

```bash
# 0. One-time setup
npm install -g aws-cdk
aws configure                          # set your AWS credentials
cd deploy/cdk
pip install -r requirements.txt
cdk bootstrap aws://YOUR_ACCOUNT_ID/us-east-1

# 1. Deploy Foundation (creates VPC, RDS, ECR repos, S3, Restate)
cdk deploy WayCoreFoundation -c account=YOUR_ACCOUNT_ID -c region=us-east-1

# 2. Fill secrets
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
FERNET_KEY=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
aws secretsmanager put-secret-value --secret-id waycore/secrets --region $REGION \
  --secret-string "{\"ENCRYPTION_KEY\":\"$FERNET_KEY\",\"ANTHROPIC_API_KEY\":\"sk-ant-...your-key...\"}"

# 3. Build and push Docker images (same image for both services)
aws ecr get-login-password --region $REGION | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com
docker build --platform linux/arm64 -t waycore .
docker tag waycore $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-api:latest
docker tag waycore $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-worker:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-api:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-worker:latest

# 4. Deploy App (creates API + Worker ECS services — images must exist in ECR)
cdk deploy WayCoreApp -c account=YOUR_ACCOUNT_ID -c region=us-east-1

# 5. Register worker with Restate (via ECS exec — Restate admin port is VPC-only)
RESTATE_SVC=$(aws ecs list-services --cluster waycore --region $REGION \
  --query 'serviceArns[?contains(@,`Restate`)]|[0]' --output text | rev | cut -d/ -f1 | rev)
TASK_ID=$(aws ecs list-tasks --cluster waycore --service-name $RESTATE_SVC --region $REGION \
  --query 'taskArns[0]' --output text | rev | cut -d/ -f1 | rev)
aws ecs execute-command --cluster waycore --task $TASK_ID --container restate --interactive \
  --region $REGION \
  --command 'curl -XPOST -H "Content-Type: application/json" -d "{\"uri\":\"http://worker.waycore.local:9000\"}" http://localhost:9070/deployments'

# 6. Run database migrations
WORKER_SVC=$(aws ecs list-services --cluster waycore --region $REGION \
  --query 'serviceArns[?contains(@,`Worker`)]|[0]' --output text | rev | cut -d/ -f1 | rev)
WORKER_TASK_DEF=$(aws ecs describe-services --cluster waycore --services $WORKER_SVC \
  --region $REGION --query 'services[0].taskDefinition' --output text)
NET_CONFIG=$(aws ecs describe-services --cluster waycore --services $WORKER_SVC \
  --region $REGION --query 'services[0].networkConfiguration' --output json)
aws ecs run-task --cluster waycore --task-definition $WORKER_TASK_DEF --launch-type FARGATE \
  --region $REGION --network-configuration "$NET_CONFIG" \
  --overrides '{"containerOverrides":[{"name":"worker","command":["uv","run","alembic","upgrade","head"]}]}'

# 7. Verify — ALB URL is in the stack outputs
ALB_URL=$(aws cloudformation describe-stacks --stack-name WayCoreApp --region $REGION \
  --query 'Stacks[0].Outputs[?OutputKey==`AlbUrl`].OutputValue' --output text)
curl http://$ALB_URL/healthz
# → {"status":"ok"}
```

For Bedrock (no Anthropic API key needed):
```bash
# Set LLM_PROVIDER=bedrock in the CDK worker environment — uses IAM credentials automatically
```

### Updating code

After code changes, rebuild and push the image, then restart ECS services:

```bash
docker build --platform linux/arm64 -t waycore .
docker tag waycore $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-api:latest
docker tag waycore $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-worker:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-api:latest
docker push $ACCOUNT.dkr.ecr.$REGION.amazonaws.com/waycore-worker:latest
aws ecs update-service --cluster waycore --service $API_SVC --force-new-deployment --region $REGION
aws ecs update-service --cluster waycore --service $WORKER_SVC --force-new-deployment --region $REGION
```

No CDK redeploy needed for code-only changes.

### Teardown

Removes **everything** — no lingering resources, no surprise bills:

```bash
cd deploy/cdk
cdk destroy WayCoreApp -c account=YOUR_ACCOUNT_ID -c region=us-east-1
cdk destroy WayCoreFoundation -c account=YOUR_ACCOUNT_ID -c region=us-east-1
```

Destroy App first (depends on Foundation). All resources have `DESTROY` removal policies. For production, change to `RETAIN`/`SNAPSHOT` in `waycore_stack.py`.

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

## LLM Providers

| Provider | Config | Notes |
|---|---|---|
| Anthropic (default) | `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` | Direct API |
| Amazon Bedrock | `LLM_PROVIDER=bedrock` + `AWS_REGION` | Uses IAM credentials, no API key |
| OpenAI | `LLM_PROVIDER=openai` + `OPENAI_API_KEY` | GPT-4o |

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
    concurrency.py      Global + per-bank concurrency limiter
deploy/cdk/             Two-stack CDK (Foundation + App)
alembic/                Database migrations
tests/
  unit/                 Fast tests — no external dependencies
  integration/          API + failure mode tests — requires PostgreSQL
```

---

## Stress Test Results

Single worker (Docker, MacBook), Heritage Bank demo (3 accounts, ~130 txns per sync):

| Parallel syncs | All succeeded | Wall time | Avg per sync | Throughput |
|---|---|---|---|---|
| 1 | 1/1 | 60s | 60s | 0.02/s |
| 3 | 3/3 | 60s | 58s | 0.05/s |
| 5 | 5/5 | 115s | 81s | 0.04/s |
| 10 | 10/10 | 208s | 126s | 0.05/s |

**Key observations:**
- **Zero failures at 10x concurrency.** The two-layer concurrency limiter (global max 5 browser sessions + per-bank max 3) queues excess syncs cleanly.
- **3 syncs fit in a single wave** (~60s) because `MAX_CONCURRENT_PER_BANK=3`. 5 syncs take 2 waves (~115s). 10 syncs take 4 waves (~208s).
- **Worker memory stayed under 300MB** even at peak. Without the limiter, 20 concurrent browsers caused the worker to stall at 289MB+ with no progress.
- **Horizontal scaling is linear.** With N Fargate workers, throughput scales to ~N × 0.05 syncs/s. Restate distributes workflows across registered workers automatically.
