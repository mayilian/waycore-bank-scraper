"""Screenshot storage abstraction.

Local backend: writes to SCREENSHOT_DIR on disk.
S3 backend: writes to any S3-compatible bucket (Cloudflare R2, AWS S3).

Switch via SCREENSHOT_BACKEND env var. The rest of the codebase calls
save() and url() — it never knows which backend is active.
"""

import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)


class ScreenshotStore(Protocol):
    async def save(self, job_id: str, step: str, png: bytes) -> str: ...
    async def url(self, path: str) -> str: ...


class LocalScreenshotStore:
    def __init__(self, base_dir: str | Path) -> None:
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)

    async def save(self, job_id: str, step: str, png: bytes) -> str:
        path = self._base / job_id / f"{step}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        log.debug("screenshot.saved", path=str(path))
        return str(path)

    async def url(self, path: str) -> str:
        # os.path is acceptable here — this is a sync call inside an async fn,
        # not an async file I/O operation. The ASYNC240 lint rule is overly broad.
        abs_path = os.path.abspath(path)  # noqa: ASYNC240
        return f"file://{abs_path}"


class S3ScreenshotStore:
    def __init__(self) -> None:
        try:
            import aioboto3

            self._aioboto3 = aioboto3
        except ImportError as e:
            raise RuntimeError("Install aioboto3 for S3 screenshot storage") from e

    def _client(self):  # type: ignore[no-untyped-def]
        session = self._aioboto3.Session()
        kwargs: dict[str, Any] = {
            "aws_access_key_id": settings.s3_access_key_id.get_secret_value()
            if settings.s3_access_key_id
            else None,
            "aws_secret_access_key": settings.s3_secret_access_key.get_secret_value()
            if settings.s3_secret_access_key
            else None,
        }
        if settings.s3_endpoint_url:
            kwargs["endpoint_url"] = settings.s3_endpoint_url
        return session.client("s3", **kwargs)

    async def save(self, job_id: str, step: str, png: bytes) -> str:
        key = f"{job_id}/{step}.png"
        async with self._client() as s3:  # type: ignore[no-untyped-call]
            await s3.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=png,
                ContentType="image/png",
            )
        log.debug("screenshot.saved", bucket=settings.s3_bucket, key=key)
        return key

    async def url(self, path: str) -> str:
        async with self._client() as s3:  # type: ignore[no-untyped-call]
            return await s3.generate_presigned_url(  # type: ignore[no-any-return]
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": path},
                ExpiresIn=int(timedelta(hours=1).total_seconds()),
            )


def get_screenshot_store() -> ScreenshotStore:
    if settings.screenshot_backend == "s3":
        return S3ScreenshotStore()
    return LocalScreenshotStore(settings.screenshot_dir)
