"""Integration tests for the API layer.

These tests exercise the full FastAPI app with a real database.
Requires: docker compose up -d (postgres) + alembic upgrade head.

Run: uv run pytest tests/integration/ -v
"""

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.core.config import settings
from src.db import session as session_mod
from src.db.models import ApiKey, Organization, User

pytestmark = pytest.mark.integration


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@pytest.fixture(autouse=True)
async def _reset_db_engine() -> None:
    """Reset the global engine so each test gets a fresh connection on the current loop."""
    session_mod._engine = None
    session_mod._session_factory = None


@pytest.fixture()
async def tenant() -> dict[str, str]:
    """Create a fresh org + user + API key for test isolation."""
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    raw_key = f"wc_test_{uuid.uuid4().hex[:16]}"
    key_hash = _hash_key(raw_key)

    engine = create_async_engine(settings.database_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    async with factory() as db:
        db.add(Organization(id=org_id, name="Test Org"))
        db.add(User(id=user_id, org_id=org_id, email=f"test-{user_id[:8]}@test.com"))
        db.add(
            ApiKey(
                org_id=org_id,
                user_id=user_id,
                key_hash=key_hash,
                key_prefix=raw_key[:12],
                name="integration-test",
            )
        )
        await db.commit()
    await engine.dispose()

    return {"org_id": org_id, "user_id": user_id, "api_key": raw_key}


class TestAuthBoundaries:
    """Verify tenant isolation — user A cannot see or modify user B's data."""

    async def test_invalid_api_key_returns_401(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/accounts", headers={"Authorization": "Bearer wc_invalid_key"}
            )
            assert resp.status_code == 401

    async def test_missing_auth_header_returns_401(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/v1/accounts")
            assert resp.status_code == 401

    async def test_tenant_isolation_accounts(self, tenant: dict[str, str]) -> None:
        """User A cannot see user B's accounts."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.get("/v1/accounts", headers=headers)
            assert resp.status_code == 200
            assert resp.json() == []  # fresh tenant, no data

    async def test_tenant_isolation_connections(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.get("/v1/connections", headers=headers)
            assert resp.status_code == 200
            assert resp.json() == []

    async def test_cannot_sync_other_tenants_connection(
        self, tenant: dict[str, str]
    ) -> None:
        """Trigger sync on a non-existent connection → 404, not 500."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.post(
                f"/v1/connections/{uuid.uuid4()}/sync",
                headers=headers,
                json={"otp_mode": "static", "otp": "123456"},
            )
            assert resp.status_code == 404


class TestConnectionLifecycle:
    """Test create connection + verify data isolation."""

    async def test_create_connection(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.post(
                "/v1/connections",
                headers=headers,
                json={
                    "bank_url": "https://demo-bank-2.vercel.app",
                    "username": "testuser",
                    "password": "testpass",
                    "otp_mode": "static",
                    "otp": "123456",
                },
            )
            assert resp.status_code == 201
            data = resp.json()
            assert data["bank_slug"] == "heritage_bank"
            assert "username" not in data  # credentials never in response
            assert "password" not in data
            assert "otp" not in data

    async def test_invalid_otp_mode_rejected(self, tenant: dict[str, str]) -> None:
        """Literal-typed otp_mode rejects invalid values at the API boundary."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.post(
                "/v1/connections",
                headers=headers,
                json={
                    "bank_url": "https://example.com",
                    "username": "u",
                    "password": "p",
                    "otp_mode": "invalid_mode",
                },
            )
            assert resp.status_code == 422  # Pydantic validation error


class TestHealthEndpoint:
    """Health endpoint is unauthenticated and always returns ok."""

    async def test_healthz(self) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}
