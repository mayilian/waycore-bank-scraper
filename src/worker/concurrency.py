"""Concurrency limiters for browser sessions.

Two layers:
  1. Global limit — caps total simultaneous browser sessions per worker process.
     Prevents OOM when many syncs arrive at once (each Chromium ~200MB).
  2. Per-bank limit — caps concurrent syncs against a single bank_slug.
     Banks rate-limit or block IPs on parallel logins.

Usage in workflow:
    async with acquire_sync_slot(bank_slug):
        # login + extract — slot released on exit (success or failure)
"""

import asyncio
from collections import defaultdict

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)

# Global limit: prevents OOM from too many concurrent browsers
_global_semaphore = asyncio.Semaphore(settings.max_concurrent_syncs)

# Per-bank limit: prevents IP bans from parallel logins to one bank
_bank_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(settings.max_concurrent_per_bank)
)


class _SyncSlot:
    """Async context manager that acquires both global and per-bank slots."""

    def __init__(self, bank_slug: str) -> None:
        self._slug = bank_slug
        self._bank_sem = _bank_semaphores[bank_slug]

    async def __aenter__(self) -> None:
        log.debug("sync_slot.acquiring_global", bank_slug=self._slug)
        await _global_semaphore.acquire()
        try:
            log.debug("sync_slot.acquiring_bank", bank_slug=self._slug)
            await self._bank_sem.acquire()
        except BaseException:
            _global_semaphore.release()
            raise
        log.debug("sync_slot.acquired", bank_slug=self._slug)

    async def __aexit__(self, *exc: object) -> None:
        self._bank_sem.release()
        _global_semaphore.release()
        log.debug("sync_slot.released", bank_slug=self._slug)


def acquire_sync_slot(bank_slug: str) -> _SyncSlot:
    """Return a context manager that limits both global and per-bank concurrency."""
    return _SyncSlot(bank_slug)


# Keep backward compat alias
acquire_bank_slot = acquire_sync_slot
