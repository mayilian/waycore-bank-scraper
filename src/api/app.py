"""FastAPI application factory.

Entry point: ``uvicorn src.api.app:app --host 0.0.0.0 --port 8000``
"""

from fastapi import FastAPI

from src.api.routes import accounts, connections, health, syncs, transactions
from src.core.logging import configure_logging


def create_app() -> FastAPI:
    configure_logging("api")

    application = FastAPI(
        title="WayCore",
        version="0.1.0",
        description="Bank data extraction API",
    )

    application.include_router(health.router)
    application.include_router(connections.router, prefix="/v1")
    application.include_router(syncs.router, prefix="/v1")
    application.include_router(accounts.router, prefix="/v1")
    application.include_router(transactions.router, prefix="/v1")

    return application


app = create_app()
