---
name: WayCore Bank Scraper Project
description: Enterprise bank data extraction system — challenge submission, fully built and pushed
type: project
---

B2B SaaS bank scraper challenge. Extract transactions + balances from any bank web portal via browser automation. Demo target: https://demo-bank-2.vercel.app (user/pass/123456).

**GitHub repo**: https://github.com/mayilian/waycore-bank-scraper (private)

**Current state (2026-04-01)**: Initial implementation complete and pushed. All code passes ruff + mypy strict. Pre-push hook enforces this on every push.

**What's built:**
- Full project scaffold: pyproject.toml (uv), Dockerfile, docker-compose.yml
- DB schema: multi-tenant (organizations → users → bank_connections → accounts → transactions/balances), Alembic migration
- Core utilities: Fernet crypto, structlog, Playwright stealth + bezier mouse, screenshot store (local/S3)
- Adapters: BankAdapter ABC, HeritageBankAdapter (demo bank), GenericBankAdapter (LLM-driven for any URL)
- LLM extractor: Claude vision, per-goal focused calls (find_login_fields, detect_post_login_state, extract_accounts, extract_transactions_from_page, etc.)
- Restate workflow: durable steps, OTP suspend/resume via ctx.promise()
- CLI: typer — sync, otp, jobs, transactions, accounts commands
- Pre-push hook: ruff check + ruff format --check + mypy strict

**How to run locally:**
```bash
cp .env.example .env  # set ANTHROPIC_API_KEY and ENCRYPTION_KEY
docker compose up -d
uv run alembic upgrade head
uv run waycore sync --bank-url https://demo-bank-2.vercel.app --username user --password pass --otp 123456
```

**Why:** Challenge for a role. Quality bar is principal/staff engineer. Submission = GitHub repo.

**How to apply:** When resuming work, read DESIGN.md for architecture, CLAUDE.md for code rules, decisions.md for why each choice was made.
