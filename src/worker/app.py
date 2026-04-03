"""Restate ASGI application factory.

Entry point: ``uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"``

After starting, register with the Restate server::

    curl -X POST http://localhost:9070/deployments \
      -H 'Content-Type: application/json' \
      -d '{"uri": "http://localhost:9000"}'

In Docker Compose this registration is handled by the 'register' service.
"""

import asyncio

import restate

from src.core.logging import configure_logging, get_logger
from src.services.operations import reconcile_orphaned_jobs
from src.worker.workflow import sync_workflow

log = get_logger(__name__)


def create_app() -> object:
    """Build and return the Restate ASGI application."""
    configure_logging("worker")
    # Reconcile jobs orphaned by prior crashes (pending > 5 min with no workflow).
    asyncio.get_event_loop().create_task(_reconcile_on_startup())
    return restate.app(services=[sync_workflow])


async def _reconcile_on_startup() -> None:
    try:
        count = await reconcile_orphaned_jobs()
        if count:
            log.info("worker.reconciled_orphaned_jobs", count=count)
    except Exception:
        log.warning("worker.reconcile_failed", exc_info=True)


app = create_app()
