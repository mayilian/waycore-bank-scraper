"""Integration tests for the API layer.

These tests exercise the full FastAPI app with a real database.
Requires: docker compose up -d (postgres) + alembic upgrade head.

Run: uv run pytest tests/integration/ -v
"""

import hashlib
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.api.app import app
from src.core.config import settings
from src.core.crypto import encrypt
from src.db import session as session_mod
from src.db.models import (
    Account,
    ApiKey,
    Balance,
    BankConnection,
    Organization,
    SyncJob,
    SyncStep,
    Transaction,
    User,
)

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

    async def test_cannot_sync_other_tenants_connection(self, tenant: dict[str, str]) -> None:
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


@pytest.fixture()
async def tenant_with_data(tenant: dict[str, str]) -> dict[str, str]:
    """Extend tenant fixture with a connection, account, balance, transaction, and job."""
    user_id = tenant["user_id"]
    conn_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())

    engine = create_async_engine(settings.database_url)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    async with factory() as db:
        db.add(
            BankConnection(
                id=conn_id,
                user_id=user_id,
                bank_slug="heritage_bank",
                bank_name="Heritage Bank",
                login_url="https://demo-bank-2.vercel.app",
                login_url_normalized="https://demo-bank-2.vercel.app",
                username_enc=encrypt("testuser"),
                password_enc=encrypt("testpass"),
                otp_mode="static",
            )
        )
        db.add(
            Account(
                id=acct_id,
                connection_id=conn_id,
                external_id="123456789",
                name="Test Checking",
                account_type="checking",
                currency="USD",
            )
        )
        db.add(
            Balance(
                id=str(uuid.uuid4()),
                account_id=acct_id,
                current=Decimal("1000.00"),
                available=Decimal("950.00"),
                currency="USD",
                captured_at=datetime.now(UTC),
            )
        )
        db.add(
            Transaction(
                id=str(uuid.uuid4()),
                account_id=acct_id,
                external_id="txn_001",
                posted_at=datetime.now(UTC),
                description="Test transaction",
                amount=Decimal("-50.00"),
                currency="USD",
            )
        )
        db.add(
            SyncJob(
                id=job_id,
                connection_id=conn_id,
                status="success",
                accounts_synced=1,
                transactions_synced=1,
            )
        )
        db.add(
            SyncStep(
                id=str(uuid.uuid4()),
                job_id=job_id,
                name="login",
                status="success",
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()
    await engine.dispose()

    return {
        **tenant,
        "connection_id": conn_id,
        "account_id": acct_id,
        "job_id": job_id,
    }


class TestAccountEndpoints:
    """Test account detail and balance history endpoints."""

    async def test_get_account(self, tenant_with_data: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant_with_data['api_key']}"}
            resp = await client.get(
                f"/v1/accounts/{tenant_with_data['account_id']}", headers=headers
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["external_id"] == "123456789"
            assert data["name"] == "Test Checking"
            assert data["account_type"] == "checking"

    async def test_get_account_not_found(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.get(f"/v1/accounts/{uuid.uuid4()}", headers=headers)
            assert resp.status_code == 404

    async def test_list_balances(self, tenant_with_data: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant_with_data['api_key']}"}
            resp = await client.get(
                f"/v1/accounts/{tenant_with_data['account_id']}/balances", headers=headers
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["current"] == "1000.0000"
            assert data[0]["available"] == "950.0000"
            assert data[0]["currency"] == "USD"

    async def test_list_balances_not_found(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.get(f"/v1/accounts/{uuid.uuid4()}/balances", headers=headers)
            assert resp.status_code == 404

    async def test_transactions_filtered_by_account(self, tenant_with_data: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant_with_data['api_key']}"}
            resp = await client.get(
                f"/v1/transactions?account_id={tenant_with_data['account_id']}",
                headers=headers,
            )
            assert resp.status_code == 200
            data = resp.json()
            assert len(data) == 1
            assert data[0]["description"] == "Test transaction"


class TestConnectionDetailEndpoints:
    """Test connection detail, delete, and cascade behavior."""

    async def test_get_connection(self, tenant_with_data: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant_with_data['api_key']}"}
            resp = await client.get(
                f"/v1/connections/{tenant_with_data['connection_id']}", headers=headers
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["bank_slug"] == "heritage_bank"
            assert "username" not in data
            assert "password" not in data

    async def test_get_connection_not_found(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.get(f"/v1/connections/{uuid.uuid4()}", headers=headers)
            assert resp.status_code == 404

    async def test_delete_connection_cascades(self, tenant_with_data: dict[str, str]) -> None:
        """Deleting a connection removes accounts, transactions, balances, and jobs."""
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant_with_data['api_key']}"}

            # Verify data exists before delete
            resp = await client.get("/v1/accounts", headers=headers)
            assert len(resp.json()) == 1

            # Delete
            resp = await client.delete(
                f"/v1/connections/{tenant_with_data['connection_id']}", headers=headers
            )
            assert resp.status_code == 204

            # Verify cascade — all data gone
            resp = await client.get("/v1/accounts", headers=headers)
            assert resp.json() == []
            resp = await client.get("/v1/transactions", headers=headers)
            assert resp.json() == []
            resp = await client.get("/v1/jobs", headers=headers)
            assert resp.json() == []

    async def test_delete_connection_not_found(self, tenant: dict[str, str]) -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            headers = {"Authorization": f"Bearer {tenant['api_key']}"}
            resp = await client.delete(f"/v1/connections/{uuid.uuid4()}", headers=headers)
            assert resp.status_code == 404
