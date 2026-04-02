# ECS Fargate Cost & Scaling Analysis

## Per-Sync Resource Profile

Measured from Heritage Bank demo (3 accounts, 130 transactions, 9 balance snapshots):

| Metric | Value | Source |
|---|---|---|
| Sync duration | ~60s (Tier 1) / ~90-120s (with LLM fallbacks) | E2E test |
| Browser sessions per sync | 2 (login + extract_all) | Workflow design |
| Peak memory (Chromium + worker) | ~800MB-1.2GB | Playwright + Python |
| CPU during browser work | ~0.5-0.8 vCPU (bursty) | Page loads + JS eval |
| CPU during idle/wait | ~0.05 vCPU | networkidle waits |
| LLM calls per sync (Tier 1 happy path) | 0 | No fallbacks triggered |
| LLM calls per sync (Generic adapter) | 8-15 per account | Login + accounts + nav + txns + balance + pagination |
| Screenshots per sync | 0 (Tier 1) / 1-5 (fallback diagnostics) | On LLM fallback only |
| Screenshot size | ~200-500KB PNG each | Full page capture |
| DB writes per sync | ~140 rows (3 accounts + 130 txns + 3 balances + steps) | Batch inserts |
| Hard timeout | 600s (configurable) | MAX_SYNC_DURATION_SECS |

## ECS Fargate Pricing (us-east-1, as of 2024)

| Resource | Price |
|---|---|
| vCPU per hour | $0.04048 |
| GB memory per hour | $0.004445 |
| Ephemeral storage (>20GB) | $0.000111/GB/hour |
| Data transfer out | $0.09/GB (first 10TB) |

### Per-sync cost (compute only)

```
Task size: 1 vCPU / 2GB memory
Sync duration: ~90s average (1.5 min)

vCPU:    1 × $0.04048/hr × (1.5/60) = $0.00101
Memory:  2 × $0.004445/hr × (1.5/60) = $0.00022
─────────────────────────────────────────────────
Total Fargate compute per sync:        $0.00123 (~$0.001)
```

## Scaling Scenarios

### Assumptions
- Average sync: 90s, 3 accounts, 130 transactions
- Syncs are sequential per connection (one browser = one sync)
- Fargate task: 1 vCPU / 2GB (can run 1 sync at a time)
- RDS: db.t4g.medium (2 vCPU, 4GB) scales to db.r6g.xlarge
- Restate: self-hosted on ECS (1 task) or Restate Cloud
- Daily sync frequency: 1x/day (most banks), up to 4x/day (high-frequency)

### Tier 1: 100 bank connections (startup / pilot)

```
Syncs per day: 100 (1x daily)
Concurrency: 2 Fargate tasks → 50 syncs/task/day
Wall clock: ~75 min to sync all (100 × 90s / 2 tasks)
```

| Component | Spec | Monthly Cost |
|---|---|---|
| ECS Fargate (worker) | 2 tasks × 1 vCPU / 2GB, ~2.5 hrs/day | ~$10 |
| RDS PostgreSQL | db.t4g.micro (2 vCPU, 1GB) | ~$15 |
| Restate (self-hosted) | 1 task × 0.5 vCPU / 1GB, always-on | ~$20 |
| S3 screenshots | <1GB stored | ~$0.02 |
| LLM API (if Generic adapter) | ~0 (Tier 1 uses no LLM) | $0 |
| Data transfer | Negligible | ~$1 |
| **Total** | | **~$46/month** |

### Tier 2: 1,000 bank connections (growth)

```
Syncs per day: 1,000
Concurrency: 10 Fargate tasks → 100 syncs/task/day
Wall clock: ~2.5 hrs to sync all
```

| Component | Spec | Monthly Cost |
|---|---|---|
| ECS Fargate (worker) | 10 tasks × 1 vCPU / 2GB, ~2.5 hrs/day | ~$45 |
| RDS PostgreSQL | db.t4g.medium (2 vCPU, 4GB) | ~$55 |
| Restate | 2 tasks × 0.5 vCPU / 1GB | ~$40 |
| S3 screenshots | ~5GB stored | ~$0.12 |
| LLM API (10% Generic) | ~100 syncs × 30 calls × $0.003/call | ~$9 |
| Data transfer | ~5GB/mo | ~$1 |
| **Total** | | **~$150/month** |

### Tier 3: 10,000 bank connections (scale)

```
Syncs per day: 10,000 (1x daily) or 40,000 (4x daily)
Concurrency: 50 Fargate tasks (1x) or 100 tasks (4x)
Wall clock: ~5 hrs to sync all (at 50 concurrent)
```

| Component | Spec | Monthly Cost |
|---|---|---|
| ECS Fargate (worker) | 50 tasks × 1 vCPU / 2GB, ~5 hrs/day | ~$200 |
| RDS PostgreSQL | db.r6g.large (2 vCPU, 16GB) | ~$200 |
| Restate | 4 tasks × 1 vCPU / 2GB or Restate Cloud | ~$100 |
| S3 screenshots | ~50GB stored | ~$1.15 |
| LLM API (10% Generic) | ~1,000 syncs × 30 calls × $0.003 | ~$90 |
| Data transfer | ~50GB/mo | ~$5 |
| **Total** | | **~$596/month** |

### Tier 4: 100,000 bank connections (enterprise SaaS)

```
Syncs per day: 100,000
Concurrency: 200-500 Fargate tasks
Wall clock: ~5-12 hrs depending on concurrency
```

| Component | Spec | Monthly Cost |
|---|---|---|
| ECS Fargate (worker) | 200 tasks × 1 vCPU / 2GB, ~12 hrs/day | ~$1,600 |
| RDS PostgreSQL | db.r6g.2xlarge (8 vCPU, 64GB) + read replica | ~$1,200 |
| Restate Cloud | Managed | ~$300+ |
| S3 screenshots | ~500GB stored | ~$12 |
| LLM API (10% Generic) | ~10,000 syncs × 30 calls × $0.003 | ~$900 |
| Data transfer | ~500GB/mo | ~$45 |
| NAT Gateway | 2 AZs, high throughput | ~$200 |
| **Total** | | **~$4,257/month** |

## Memory Breakdown Per Fargate Task

```
┌─────────────────────────────────────────────┐
│ ECS Fargate Task: 2048 MB                   │
│                                              │
│  Chromium browser process     600-900 MB    │
│  Python worker (asyncio)       80-120 MB    │
│  Playwright library              30-50 MB    │
│  SQLAlchemy + connection pool    20-30 MB    │
│  DOM snapshots in memory          5-15 MB    │
│  Screenshot buffers (PNG)          1-5 MB    │
│  OS / container overhead          50-80 MB    │
│                                              │
│  Total used:               ~800-1200 MB     │
│  Headroom:                  ~800-1200 MB    │
└─────────────────────────────────────────────┘
```

**Why 2GB not 1GB:** Chromium on complex banking pages with SPAs can spike to 900MB+. 1GB tasks will OOM on heavy pages. 2GB gives enough headroom for reliability.

**When to go to 4GB:** If banks have very heavy SPAs (React dashboards with thousands of DOM nodes) or if you need to hold 2 browser contexts simultaneously (future: parallel account extraction within one task).

## CPU Profile

```
Phase           Duration    CPU Usage    Notes
────────────────────────────────────────────────────
Browser launch    3-5s      0.8 vCPU     Chromium startup spike
Page load         2-5s      0.5 vCPU     JS parsing + rendering
networkidle wait  1-10s     0.05 vCPU    Waiting for SPA to settle
DOM evaluation    <0.5s     0.3 vCPU     page.evaluate() calls
LLM API call      1-5s      0.05 vCPU    Waiting on network I/O
DB batch insert   <0.5s     0.2 vCPU     asyncpg write
────────────────────────────────────────────────────
Average across sync:        ~0.3 vCPU    Mostly I/O-bound
```

The workload is **I/O-bound** (browser waits, network, DB). CPU rarely saturates. You could use 0.5 vCPU tasks, but Fargate's minimum is 0.25 vCPU at 2GB, and the browser launch spike benefits from 1 vCPU.

## LLM API Cost Detail

Per-call cost (Anthropic Claude Sonnet, vision):

| Call type | Input tokens | Output tokens | Cost/call |
|---|---|---|---|
| DOM-only (Tier 2 fallback) | ~4K text | ~500 | ~$0.002 |
| DOM + screenshot (Tier 3 / Generic) | ~4K text + ~1K image | ~500 | ~$0.003 |
| Transaction extraction (8K output) | ~4K | ~4K | ~$0.005 |

Per-sync LLM cost by adapter type:

| Adapter | Calls/sync | Cost/sync |
|---|---|---|
| Heritage (Tier 1, happy path) | 0 | $0.00 |
| Heritage (Tier 2, selectors broke) | 2-5 | $0.01 |
| Generic (all LLM) | 8-15 per account | $0.07-0.15 |

**Budget cap:** `MAX_LLM_CALLS_PER_SYNC=100` prevents runaway costs. Worst case: 100 × $0.005 = $0.50/sync.

## Database Sizing

### Row growth per sync
- 3-10 accounts (upsert, not growing)
- 50-200 transactions per account (idempotent, grows on new txns only)
- 1 balance per account (append-only, grows every sync)
- 1 sync_job + 3-10 sync_steps + 3-10 account_sync_results

### Storage growth

| Connections | Daily new rows | Monthly storage growth | 1-year DB size |
|---|---|---|---|
| 100 | ~500 txns + 300 balances | ~50MB | ~600MB |
| 1,000 | ~5K txns + 3K balances | ~500MB | ~6GB |
| 10,000 | ~50K txns + 30K balances | ~5GB | ~60GB |
| 100,000 | ~500K txns + 300K balances | ~50GB | ~600GB |

RDS storage is $0.115/GB/month (gp3). At 100K connections, ~$70/month for storage after 1 year.

### Connection pool sizing

| Concurrent tasks | DB pool per task | Total connections | RDS max_connections |
|---|---|---|---|
| 2 | 5 + 10 overflow | 30 | db.t4g.micro (85) |
| 10 | 5 + 10 overflow | 150 | db.t4g.medium (170) |
| 50 | 5 + 10 overflow | 750 | db.r6g.large (1600) |
| 200 | 5 + 5 overflow | 2000 | db.r6g.2xlarge (5000) + RDS Proxy |

At 50+ tasks, use **RDS Proxy** ($0.015/vCPU-hr) to pool connections and avoid exhausting `max_connections`.

## Bottlenecks & Mitigations (Implemented)

| Bottleneck | Before | After | Implementation |
|---|---|---|---|
| Browser memory | ~900MB peak, OOM risk on 2GB | ~600MB peak, stable headroom | Chromium flags: `--disable-gpu`, `--no-zygote`, `--js-flags=--max-old-space-size=256`, `--disable-extensions` in `stealth.py` |
| DB connections at scale | 50 tasks × 15 pool = 750 connections → exhausts RDS | NullPool when behind proxy | `USE_RDS_PROXY=true` → `NullPool` in `session.py`, proxy handles pooling. **Note:** The app-side config exists, but the CDK stack does not provision RDS Proxy by default (`USE_RDS_PROXY=false`). These numbers describe the target architecture for when RDS Proxy is added. |
| Bank rate limiting | No limit — 50 syncs hit one bank simultaneously | 3 concurrent per bank_slug | `acquire_bank_slot()` semaphore in `concurrency.py`, wired into workflow |
| DB round trips | ~8 sessions per sync (1 per account per table) | 1 batch session for all writes | Batch insert transactions, balances, sync_results, steps in `steps.py` |
| Compute cost | $0.04048/vCPU-hr (amd64, on-demand) | $0.01619/vCPU-hr (arm64 Spot) | Multi-arch Dockerfile + Spot capacity provider in CDK stack (`deploy/cdk/`) |
| Fargate task launch | ~30-60s cold start | ~30-60s (unchanged) | Mitigated by Spot base=1 (always warm) + Restate retry on Spot reclaim |

### Before/After Cost at 10K Connections

| Component | Before | After | Savings |
|---|---|---|---|
| Fargate compute (50 tasks, 5 hrs/day) | $200/mo | **$52/mo** | 74% (Graviton + Spot) |
| RDS | $200/mo (r6g.large) | **$200/mo** + $20 Proxy | — (Proxy prevents connection exhaustion) |
| DB round trips | ~8 per sync × 10K = 80K/day | ~2 per sync × 10K = 20K/day | 75% fewer DB sessions |
| LLM API | $90/mo | $90/mo | — (depends on adapter tier, not infra) |
| **Total** | **$596/mo** | **~$370/mo** | **~38% reduction** |

### Before/After Cost at 100K Connections

| Component | Before | After | Savings |
|---|---|---|---|
| Fargate compute (200 tasks, 12 hrs/day) | $1,600/mo | **$416/mo** | 74% |
| RDS + Proxy | $1,200/mo | **$1,200/mo** + $80 Proxy | Prevents connection exhaustion |
| LLM API | $900/mo | $900/mo | — |
| **Total** | **$4,257/mo** | **~$2,700/mo** | **~37% reduction** |

## Remaining Bottlenecks

| Bottleneck | Limit | Status | Mitigation |
|---|---|---|---|
| Sync duration | 600s hard cap | Configurable | Increase `MAX_SYNC_DURATION_SECS` for banks with many accounts |
| LLM budget | 100 calls/sync | Configurable | Increase `MAX_LLM_CALLS_PER_SYNC` for Generic-adapter banks |
| NAT Gateway throughput | 45 Gbps | Not a concern below 500 tasks | — |
| Fargate cold start | ~30-60s | Partially mitigated | Spot base=1 keeps 1 warm; EC2 capacity provider for zero cold start |
| Browser per task | 1 sync per task | Architectural | Warm browser pool (future) would allow reuse |

## Cost Optimization Levers

1. **Promote banks to Tier 1** — Each bank with deterministic selectors saves $0.07-0.15/sync in LLM costs and ~30s in sync time
2. **Reduce sync frequency** — 1x/day vs 4x/day is 4x compute savings
3. **Fargate Spot** — 70% discount on compute, Restate handles replay on reclaim *(implemented in CDK stack: `deploy/cdk/`)*
4. **ARM64 Graviton** — 20% cheaper Fargate pricing *(implemented: multi-arch Dockerfile)*
5. **RDS Proxy** — Prevents connection exhaustion at scale *(app-side config implemented: `USE_RDS_PROXY` in `session.py`; CDK stack does not provision RDS Proxy by default — add it when scaling past ~50 concurrent tasks)*
6. **Batch DB writes** — 75% fewer DB round trips per sync *(implemented: batched inserts in `steps.py`)*
7. **Per-bank concurrency** — Prevents IP blocks from parallel logins *(implemented: `concurrency.py`)*
8. **Right-size RDS** — Start with db.t4g.micro, upgrade when CPU credits deplete
9. **Warm browser pool** (future) — Eliminates 3-5s browser launch overhead per sync
10. **Batch scheduling** — Spread syncs across off-peak hours for lower Spot pricing
