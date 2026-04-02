# WayCore — Claude Code Context

> This file is tool context for Claude Code. It is not canonical documentation.
> For architecture and operations, see README.md.

## What This Is

Bank data extraction platform: browser automation → bank portal login → extract accounts,
transactions, balances → PostgreSQL. Durable workflow (Restate) handles crash recovery and
OTP pause/resume. Tiered extraction: deterministic DOM selectors first, LLM fallback only
when selectors miss.

Two services (one codebase): API (FastAPI, port 8000) and Worker (Restate + Playwright, port 9000).
CLI (`cli.py`) for local dev. Both API and CLI call `src/services/operations.py`.

## Coding Conventions

1. Every bank is a `BankAdapter` subclass in `src/adapters/`. Bank-specific logic stays there.
2. LLM calls are per-goal with task-specific system prompts. No mega-prompts.
3. API routes are thin — call `operations.py` + `queries.py`. No business logic in handlers.
4. All data queries scoped by `user_id` via `src/db/queries.py`.
5. Credentials: MultiFernet-encrypted before DB write. Decrypt only in worker memory. Never log.
6. Transactions: `ON CONFLICT (account_id, external_id) DO NOTHING`. Balances: append-only.
7. Use `structlog` for logging (never `print()` or stdlib `logging` directly). Bind `job_id` in workflow steps.
8. Type hints on all function signatures. Constrained values use `Literal` types.
9. `asyncio.sleep` not `time.sleep` in async code. f-strings not `.format()`.
10. LLM provider is configurable (`LLM_PROVIDER=anthropic|bedrock|openai`). Never hardcode a provider.

## Concurrency Model

- Two-layer limiter: global max browsers per worker (`max_concurrent_syncs=5`) + per-bank max (`max_concurrent_per_bank=3`).
- Both login and extract are gated by the limiter — not just extract.
- Semaphores are process-local. For multi-worker deployments, distributed locking (Redis/DynamoDB) would be needed.

## Constraints (not enforced by code, but should be)

- Sync steps should write `sync_steps` records for auditing, but this is convention, not a runtime contract.
- On failure: save screenshot, write error to step output, set step+job status. Some error paths may not fully comply yet.

## Development Workflow

After every meaningful chunk of code:
- `/review` — principal engineer review
- `/style` — style consistency check
- `make check` — runs lint + tests

## Key Files

```
src/services/operations.py Shared business logic (CLI + API both call this)
src/worker/workflow.py     Durable workflow: login → extract_all → finalise
src/worker/steps.py        Step functions with batched DB writes
src/worker/concurrency.py  Global + per-bank concurrency limiter
src/adapters/base.py       BankAdapter ABC + data models
src/db/models.py           SQLAlchemy models (multi-tenant)
src/db/queries.py          Tenant-scoped query helpers
src/api/schemas.py         API request/response models (Literal-typed)
src/api/auth.py            API key auth → TenantContext
src/core/config.py         pydantic-settings (Literal-typed providers)
src/core/metrics.py        CloudWatch EMF metric emitter
deploy/cdk/stacks/         Two-stack CDK (foundation_stack.py + app_stack.py)
tests/unit_tests/          Mirrors src/ layout (adapters/, api/, core/, db/, worker/)
```
