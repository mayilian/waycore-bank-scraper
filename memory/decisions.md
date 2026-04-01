---
name: Architecture Decisions
description: Every key technical decision made for the WayCore bank scraper, with rationale
type: project
---

## Workflow Engine: Restate (not Celery, not Temporal)

**Decision**: Use Restate (`restate-sdk`) for durable workflow orchestration.

**Why**: Each bank sync is a multi-step sequential workflow that must checkpoint after each step (crash mid-extraction → resume from last step, not re-login). Restate's `ctx.run()` journals every step result. The `ctx.promise()` primitive is the exact right model for OTP: the workflow suspends with zero resources held until the user sends the code. Celery has no per-step checkpointing. Temporal is the right production answer but heavy to operate. Restate is the sweet spot.

**How to apply**: Never add Celery or Redis. If someone suggests a job queue, explain the checkpointing requirement.

---

## No FastAPI

**Decision**: CLI only. No FastAPI service.

**Why**: The challenge asks for "a process that logs in, extracts, and stores." An API is dead code for this scope. The Restate worker already exposes HTTP internally (for Restate to call it) via hypercorn — that's not FastAPI, it's the SDK's transport. FastAPI would be the right addition if this became a multi-client hosted service.

**How to apply**: Don't add a FastAPI app. If the scope changes to "hosted service with multiple clients," then add it.

---

## Database: PostgreSQL (not DynamoDB, not MongoDB)

**Decision**: PostgreSQL 16 via SQLAlchemy async.

**Why**: Financial data needs NUMERIC(20,4) — not float. Access patterns are relational (transactions → accounts → connections → users → orgs). Complex queries ("all transactions over $10k this month") are one SQL WHERE clause; in DynamoDB they require GSIs or full scans. UPSERT (`ON CONFLICT DO NOTHING`) for idempotent transaction writes is one line in Postgres. Multi-tenant RLS is a Postgres native feature.

**How to apply**: Never suggest changing to DynamoDB or MongoDB for this use case. The scale bottleneck is browser memory (400MB/Chromium), never DB throughput.

---

## Balances: Append-Only

**Decision**: Never UPDATE balance rows. Always INSERT.

**Why**: Append-only gives balance history for free. Every sync captures a snapshot. This enables "show me balance over time" without any extra work.

**How to apply**: If you see `UPDATE balances SET ...` anywhere, that's a bug.

---

## Transactions: Idempotent via UNIQUE constraint

**Decision**: `UNIQUE(account_id, external_id)` + `ON CONFLICT DO NOTHING` on every insert.

**Why**: Re-running a sync (crash recovery, manual retry) must never double-count transactions. The conflict is silently ignored — we count `rowcount` to know what was actually new.

**How to apply**: Always use `pg_insert(...).on_conflict_do_nothing(index_elements=["account_id", "external_id"])`. Never use `index_elements` with a constraint name string — use column names directly to avoid name mismatch bugs.

---

## LLM Strategy: Multiple Focused Calls (not one big prompt)

**Decision**: One LLM call per goal (`find_login_fields`, `detect_post_login_state`, `extract_accounts`, etc.).

**Why**: A single "do everything" prompt is unreliable and expensive. Focused calls with task-specific system prompts are more accurate, cheaper, and easier to debug. Each call gets a trimmed DOM summary (~6k chars) + screenshot.

**How to apply**: Never add a generic "figure out this page" prompt. Every new extraction goal = a new function in `src/agent/extractor.py` with its own system prompt.

---

## OTP Security: Never in Restate Payload

**Decision**: Credentials and OTPs are never included in the Restate workflow trigger payload.

**Why**: Restate journals the workflow input for durability. Putting a password or OTP in that payload means it's stored in Restate's state store in plaintext. Instead: static/TOTP OTPs are stored encrypted in DB and decrypted inside the worker step. Webhook OTPs arrive via Restate's durable promise (the signal itself is short-lived and never journaled as part of the original request).

**How to apply**: The `SyncRequest` payload sent to Restate contains only `job_id`, `connection_id`, `otp_mode`. Never add `username`, `password`, or `otp` to it.

---

## Multi-Tenancy: Schema Design

**Decision**: FK chain `organizations → users → bank_connections → accounts → transactions`.

**Why**: Billing unit is `users` (monthly per-user subscription). Every data query scopes through this chain. In production, PostgreSQL RLS policies on each table (filtering by `app.current_org_id` session variable) enforce tenant isolation at the DB layer with no application-level filtering needed.

**How to apply**: When adding new tables, always include a FK that traces back to `bank_connections` or `users`. Never add a table that floats outside the tenant hierarchy.

---

## Cloud Extension Path (documented, not built)

**Decision**: Local Docker Compose is primary. Cloud = change 3 env vars, no code changes.

| Component | Local | Cloud |
|---|---|---|
| DB | Docker postgres:16 | DATABASE_URL → Neon/RDS |
| Workflow | Docker restatedev/restate | RESTATE_ENDPOINT → Restate Cloud |
| Screenshots | Local volume | SCREENSHOT_BACKEND=s3 + Cloudflare R2 |
| Compute | docker compose up | fly deploy |

**Why**: Cloudflare R2 is the right screenshot store for the operator — they're on Cloudflare free tier, R2 is S3-compatible, 10GB free, no egress fees.

---

## Screenshot Storage: Abstracted Behind Protocol

**Decision**: `ScreenshotStore` protocol with `LocalScreenshotStore` and `S3ScreenshotStore` implementations.

**Why**: Screenshots are write-once debug artifacts. The rest of the codebase calls `store.save()` and `store.url()` — it never knows the backend. Switch = one env var. Retention: delete after 30 days (cron or R2 lifecycle rule).

---

## Bot Detection Evasion Stack

**Decision**: `playwright-stealth` + bezier curve mouse movement + per-keystroke random delays.

**Why**: The demo bank loads `/bot-detection-overlay.js` which fingerprints `navigator.webdriver`, plugins array, and Chrome runtime object. `playwright-stealth` patches all of these. Bezier curves defeat behavioral mouse tracking. Keystroke cadence randomisation defeats typing pattern analysis.

**Implementation**: `src/core/stealth.py` — `stealth_browser()` context manager, `human_move_and_click()`, `human_fill()`, `_bezier_points()`.

---

## Code Quality Enforcement

**Decision**: Pre-push git hook runs `ruff check` + `ruff format --check` + `mypy --strict` before every push.

**Why**: Catches type errors, style issues, and dead imports before they reach the repo. During development, run `/review` (principal engineer review) and `/style` (style check) as Claude Code slash commands after each feature.

**How to apply**: Hook is at `.githooks/pre-push`. Git is configured to use it via `git config core.hooksPath .githooks`. This is set up in the repo — new contributors need to run `git config core.hooksPath .githooks` once after cloning.
