"""API key authentication.

Keys are SHA-256 hashed before storage. The raw key is only ever seen
by the client at creation time. Lookup is by hash, verified with hmac.compare_digest.
"""

import hashlib
import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request
from sqlalchemy import select

from src.db.models import ApiKey
from src.db.session import get_session


@dataclass
class TenantContext:
    org_id: str
    user_id: str


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def get_tenant(request: Request) -> TenantContext:
    """FastAPI dependency: validate API key, return tenant context."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or malformed Authorization header")

    key_hash = hash_key(auth.removeprefix("Bearer ").strip())

    async with get_session() as db:
        result = await db.execute(
            select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.is_active.is_(True))
        )
        api_key = result.scalars().first()

    if not api_key or not hmac.compare_digest(api_key.key_hash, key_hash):
        raise HTTPException(401, "Invalid API key")

    return TenantContext(org_id=api_key.org_id, user_id=api_key.user_id)
