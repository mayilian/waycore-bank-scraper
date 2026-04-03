"""Shared business operations used by both CLI and API.

These are the core actions: create connections, trigger syncs, provide OTPs.
No UI concerns — just DB writes and Restate HTTP calls.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from src.core.config import settings
from src.core.crypto import encrypt
from src.core.urls import normalize_url
from src.db.models import BankConnection, SyncJob
from src.db.session import get_session


def bank_slug_from_url(url: str) -> str:
    """Derive a stable slug from a bank URL for adapter lookup."""
    host = urlparse(url).netloc.lower()
    if "heritage" in host or "demo-bank" in host:
        return "heritage_bank"
    slug = host.replace("www.", "").replace(".", "_").replace("-", "_")
    return slug[:64]


async def find_or_create_connection(
    user_id: str,
    bank_url: str,
    username: str,
    password: str,
    otp_mode: str = "static",
    otp: str | None = None,
) -> tuple[str, str]:
    """Find or create a bank connection. Returns (connection_id, bank_slug)."""
    bank_slug = bank_slug_from_url(bank_url)
    normalized_url = normalize_url(bank_url)

    async with get_session() as db:
        result = await db.execute(
            select(BankConnection).where(
                BankConnection.user_id == user_id,
                BankConnection.bank_slug == bank_slug,
                BankConnection.login_url_normalized == normalized_url,
            )
        )
        existing = result.scalars().first()

        if existing:
            existing.username_enc = encrypt(username)
            existing.password_enc = encrypt(password)
            existing.login_url = bank_url
            existing.otp_mode = otp_mode
            existing.otp_value_enc = encrypt(otp) if otp else None
            await db.flush()
            return existing.id, bank_slug

        connection_id = str(uuid.uuid4())
        db.add(
            BankConnection(
                id=connection_id,
                user_id=user_id,
                bank_slug=bank_slug,
                bank_name=bank_slug.replace("_", " ").title(),
                login_url=bank_url,
                login_url_normalized=normalized_url,
                username_enc=encrypt(username),
                password_enc=encrypt(password),
                otp_mode=otp_mode,
                otp_value_enc=encrypt(otp) if otp else None,
            )
        )
        return connection_id, bank_slug


async def trigger_sync(connection_id: str, otp_mode: str = "static") -> str:
    """Create a SyncJob and trigger the Restate workflow. Returns job_id."""
    job_id = str(uuid.uuid4())

    async with get_session() as db:
        db.add(
            SyncJob(
                id=job_id,
                restate_id=job_id,
                connection_id=connection_id,
                status="pending",
            )
        )

    payload: dict[str, Any] = {
        "job_id": job_id,
        "connection_id": connection_id,
        "otp_mode": otp_mode,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.restate_ingress_url}/SyncBankWorkflow/{job_id}/run/send",
                json=payload,
            )
            if resp.status_code not in (200, 202):
                raise RuntimeError(f"Restate returned {resp.status_code}: {resp.text}")
    except Exception:
        # Mark job as failed so it doesn't sit as orphaned "pending" forever.
        async with get_session() as db:
            job = await db.get(SyncJob, job_id)
            if job:
                job.status = "failed"
                job.failure_reason = "Failed to trigger workflow"
                job.completed_at = datetime.now(UTC)
        raise

    return job_id


async def provide_otp(job_id: str, code: str) -> None:
    """Send an OTP code to a paused webhook-mode workflow."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{settings.restate_ingress_url}/SyncBankWorkflow/{job_id}/provide_otp",
            json=code,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code not in (200, 202):
            raise RuntimeError(f"Failed to provide OTP: {resp.status_code} {resp.text}")
