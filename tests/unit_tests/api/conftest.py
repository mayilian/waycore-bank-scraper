"""Integration test fixtures.

Tests marked with @pytest.mark.integration require a running PostgreSQL.
Tests in this directory without the mark run regardless.
"""

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.core.config import settings


def _db_is_reachable() -> bool:
    async def _check() -> bool:
        try:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            await engine.dispose()
            return True
        except Exception:
            return False

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_check())
    finally:
        loop.close()


_HAS_DB = _db_is_reachable()


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-skip tests marked 'integration' when DB is unreachable."""
    if _HAS_DB:
        return
    skip_marker = pytest.mark.skip(reason="PostgreSQL not reachable")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_marker)
