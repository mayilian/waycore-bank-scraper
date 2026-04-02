.PHONY: install test lint format run-worker register sync accounts transactions jobs

# ── Setup ────────────────────────────────────────────────────────────────────

install:  ## Install dependencies + Playwright browser
	uv sync --extra all
	uv run playwright install chromium

migrate:  ## Run database migrations
	uv run alembic upgrade head

# ── Quality ──────────────────────────────────────────────────────────────────

test:  ## Run tests
	uv run pytest tests/ -v

lint:  ## Run linter
	uv run ruff check src/ cli.py tests/

format:  ## Auto-format code
	uv run ruff format src/ cli.py tests/

check: lint test  ## Run lint + tests

# ── Services ─────────────────────────────────────────────────────────────────

run-restate:  ## Start Restate server (foreground)
	restate-server --listen-mode tcp

run-api:  ## Start API server (foreground)
	uv run uvicorn src.api.app:app --host 0.0.0.0 --port 8000 --reload

run-worker:  ## Start worker (foreground)
	uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"

register:  ## Register worker with Restate (run once after worker starts)
	curl -sf -X POST http://localhost:9070/deployments \
		-H "Content-Type: application/json" \
		-d '{"uri": "http://localhost:9000", "force": true}'

# ── CLI shortcuts ────────────────────────────────────────────────────────────

sync:  ## Run demo bank sync
	uv run waycore sync \
		--bank-url https://demo-bank-2.vercel.app \
		--username user --password pass --otp 123456

accounts:  ## List synced accounts
	uv run waycore accounts

transactions:  ## List recent transactions
	uv run waycore transactions

jobs:  ## List sync jobs
	uv run waycore jobs

create-api-key:  ## Create an API key for the default tenant
	uv run waycore create-api-key

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
