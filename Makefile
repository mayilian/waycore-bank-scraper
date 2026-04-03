.PHONY: install test lint format dead-code check run-worker register sync accounts transactions jobs deploy-foundation push-image deploy-app destroy

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
	uv run ruff check src/ cli.py tests/ scripts/

format:  ## Auto-format code
	uv run ruff format src/ cli.py tests/ scripts/

dead-code:  ## Detect unused code (vulture)
	uv run vulture src/ cli.py vulture_whitelist.py --min-confidence 80

check: lint dead-code test  ## Run lint + dead-code + tests

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

# ── AWS Deployment ──────────────────────────────────────────────────────────
# Usage: make deploy-foundation ACCOUNT_ID=123456789012 AWS_REGION=us-east-1

deploy-foundation:  ## Deploy foundation stack (VPC, RDS, ECR, Restate)
	cd deploy/cdk && uv pip install -r requirements.txt && \
		cdk deploy WayCoreFoundation -c account=$(ACCOUNT_ID) -c region=$(AWS_REGION)

push-image:  ## Build and push Docker image to ECR
	$(eval IMAGE_TAG := $(shell git rev-parse --short HEAD))
	$(eval ECR := $(ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com)
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(ECR)
	docker build --platform linux/arm64 -t waycore:$(IMAGE_TAG) .
	@for repo in waycore-api waycore-worker; do \
		docker tag waycore:$(IMAGE_TAG) $(ECR)/$$repo:$(IMAGE_TAG); \
		docker push $(ECR)/$$repo:$(IMAGE_TAG); \
		docker tag waycore:$(IMAGE_TAG) $(ECR)/$$repo:latest; \
		docker push $(ECR)/$$repo:latest; \
	done

deploy-app:  ## Deploy app stack (API + Worker on Fargate)
	cd deploy/cdk && cdk deploy WayCoreApp -c account=$(ACCOUNT_ID) -c region=$(AWS_REGION)

destroy:  ## Tear down all stacks (app first, then foundation)
	cd deploy/cdk && \
		cdk destroy WayCoreApp -c account=$(ACCOUNT_ID) -c region=$(AWS_REGION) && \
		cdk destroy WayCoreFoundation -c account=$(ACCOUNT_ID) -c region=$(AWS_REGION)

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

.DEFAULT_GOAL := help
