"""Restate ASGI application factory.

Entry point: ``uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"``

On startup the worker automatically registers itself with the Restate server
and reconciles any orphaned jobs from prior crashes.
"""

import asyncio
from typing import Any

import httpx
import restate

from src.core.config import settings
from src.core.logging import configure_logging, get_logger
from src.services.operations import reconcile_orphaned_jobs
from src.worker.workflow import sync_workflow

log = get_logger(__name__)

_background_tasks: set[asyncio.Task[None]] = set()
_startup_done = False


def _create_restate_app() -> Any:
    configure_logging("worker")
    return restate.app(services=[sync_workflow])


_restate_app = _create_restate_app()


async def _run_startup_tasks() -> None:
    """Run startup tasks once the event loop is running."""
    global _startup_done
    if _startup_done:
        return
    _startup_done = True

    for coro in (_reconcile_on_startup(), _register_with_restate()):
        task = asyncio.create_task(coro)
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI wrapper that fires startup tasks on first request, then delegates to Restate."""
    await _run_startup_tasks()
    await _restate_app(scope, receive, send)


async def _reconcile_on_startup() -> None:
    try:
        count = await reconcile_orphaned_jobs()
        if count:
            log.info("worker.reconciled_orphaned_jobs", count=count)
    except Exception:
        log.warning("worker.reconcile_failed", exc_info=True)


async def _register_with_restate(retries: int = 5, delay: float = 3.0) -> None:
    """Register this worker with the Restate server on startup.

    Retries a few times since Restate may not be ready immediately.
    """
    admin_url = settings.restate_admin_url
    worker_url = settings.restate_worker_url
    payload = {"uri": worker_url, "force": True}

    async with httpx.AsyncClient(timeout=5) as client:
        for attempt in range(1, retries + 1):
            try:
                await client.post(f"{admin_url}/deployments", json=payload)
                log.info(
                    "worker.registered_with_restate",
                    admin_url=admin_url,
                    worker_url=worker_url,
                )
                return
            except Exception:
                if attempt < retries:
                    log.debug("worker.restate_not_ready", attempt=attempt, retry_in=delay)
                    await asyncio.sleep(delay)
                else:
                    log.warning("worker.restate_registration_failed", attempts=retries)
