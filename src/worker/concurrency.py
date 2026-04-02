"""Per-bank concurrency limiter.

Prevents hammering a single bank with too many simultaneous browser sessions.
Banks rate-limit or block IPs when they see parallel logins — this keeps
concurrent syncs per bank_slug within a safe limit.

Usage in workflow:
    async with acquire_bank_slot(bank_slug):
        # run sync — slot released on exit (success or failure)
"""

import asyncio
from collections import defaultdict

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)

_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(settings.max_concurrent_per_bank)
)


class _BankSlot:
    """Async context manager that acquires a per-bank semaphore slot."""

    def __init__(self, bank_slug: str) -> None:
        self._slug = bank_slug
        self._sem = _semaphores[bank_slug]

    async def __aenter__(self) -> None:
        log.debug("bank_slot.acquiring", bank_slug=self._slug)
        await self._sem.acquire()
        log.debug("bank_slot.acquired", bank_slug=self._slug)

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()
        log.debug("bank_slot.released", bank_slug=self._slug)


def acquire_bank_slot(bank_slug: str) -> _BankSlot:
    """Return a context manager that limits concurrency per bank_slug."""
    return _BankSlot(bank_slug)
