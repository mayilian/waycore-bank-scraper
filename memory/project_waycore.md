---
name: WayCore Bank Scraper Project
description: Design and build a banking data extraction system for enterprise CFOs
type: project
---

Enterprise banking scraper challenge. Design doc written at DESIGN.md, awaiting user approval before implementation.

**Why:** Challenge for a role — quality matters, targeting principal/staff engineer bar.

**Scope approved**: Design doc only (as of 2026-04-01). Next step: get approval then build in this order:
1. DB models + Alembic migrations
2. BankAdapter ABC + data models
3. HeritageBankAdapter (demo bank)
4. Celery sync worker + step tracking
5. FastAPI API
6. Docker Compose

**Key constraints:**
- Demo bank: https://demo-bank-2.vercel.app/ (user/pass/123456 OTP)
- App is Next.js SPA with bot detection overlay and deliberate delays
- Must use Python + uv
- Must be reproducible on any machine (Docker Compose)
- GitHub repo as deliverable

**How to apply:** When building, follow the adapter pattern strictly. Every bank = one file.
