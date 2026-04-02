"""FastAPI application — the public API layer.

Thin: creates DB records, triggers Restate workflows, returns results.
No browser, no LLM, no Playwright. Runs as a separate ECS service from the worker.

  uvicorn src.api.app:app --host 0.0.0.0 --port 8000
"""

from fastapi import FastAPI

from src.api.routes import accounts, connections, health, syncs, transactions
from src.core.logging import configure_logging

configure_logging("api")

app = FastAPI(
    title="WayCore",
    version="0.1.0",
    description="Bank data extraction API",
)

app.include_router(health.router)
app.include_router(connections.router, prefix="/v1")
app.include_router(syncs.router, prefix="/v1")
app.include_router(accounts.router, prefix="/v1")
app.include_router(transactions.router, prefix="/v1")
