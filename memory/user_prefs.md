---
name: User Preferences
description: How this user likes to work and collaborate
type: user
---

## Working Style

- Thinks at principal/staff engineer level. Wants design-first, then iterate. Don't just start coding — propose design, get approval, build.
- Prefers decisive recommendations over option lists. "Use X because Y" beats "here are 3 options."
- Asks probing questions ("what about Dynamo?", "does this mean we should run locally?") to pressure-test decisions. Engage seriously with these — don't deflect.
- Wants to understand trade-offs, not just the answer. Explain the why.

## Communication

- Concise. Don't pad responses. Short answers with clear reasoning are better than long hedged ones.
- No emojis unless asked.
- When explaining architecture, use tables and code blocks — easier to scan than prose.

## Code Quality Standards

- No dead code. No useless boilerplate. If it's not needed for the current scope, don't add it.
- Review and style checks before every push. `/review` and `/style` slash commands exist for this.
- Type annotations everywhere (mypy strict). structlog for all logging (never print). Fernet encryption for credentials.
- Pre-push hook: ruff + mypy gate. Don't skip it.

## Decision Making

- Wants rationale captured in memory/decisions.md so a new Claude context can pick up without re-litigating everything.
- Build state tracked in memory/build_state.md.
- When context resets, read MEMORY.md → load relevant memory files → read CLAUDE.md → then proceed.

## Project Specifics (WayCore)

- Demo bank URL: https://demo-bank-2.vercel.app (user/pass/123456 OTP)
- GitHub: https://github.com/mayilian/waycore-bank-scraper
- Primary local dev: docker compose up + uv run waycore sync ...
- Cloud path: Fly.io + Neon + Restate Cloud (documented, not yet built)
