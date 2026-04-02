"""Local stress test — fire N parallel syncs and measure throughput.

Usage: uv run python scripts/stress_test.py [N]
Default N=10. Runs against local docker compose stack.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from typing import Any

import httpx

API_BASE = "http://localhost:8000"
BANK_URL = "https://demo-bank-2.vercel.app"
USERNAME = "user"
PASSWORD = "pass"
OTP = "123456"


def _get_or_create_api_key() -> str:
    """Get an API key — create one via CLI if needed."""
    result = subprocess.run(
        ["uv", "run", "waycore", "create-api-key", "--name", "stress-test"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if "wc_" in line:
            for word in line.split():
                if word.startswith("wc_"):
                    return word.strip()
    raise RuntimeError(f"Failed to create API key: {result.stdout}\n{result.stderr}")


async def create_connection(client: httpx.AsyncClient, headers: dict[str, str]) -> str | None:
    """Create a bank connection, return connection ID."""
    resp = await client.post(
        f"{API_BASE}/v1/connections",
        headers=headers,
        json={
            "bank_url": BANK_URL,
            "username": USERNAME,
            "password": PASSWORD,
            "otp_mode": "static",
            "otp": OTP,
        },
    )
    if resp.status_code == 201:
        return str(resp.json()["id"])
    resp = await client.get(f"{API_BASE}/v1/connections", headers=headers)
    conns = resp.json()
    if conns:
        return str(conns[0]["id"])
    return None


async def trigger_sync(client: httpx.AsyncClient, headers: dict[str, str], conn_id: str) -> str:
    """Trigger a sync, return job ID."""
    resp = await client.post(
        f"{API_BASE}/v1/connections/{conn_id}/sync",
        headers=headers,
        json={"otp_mode": "static", "otp": OTP},
    )
    if resp.status_code in (200, 201, 202):
        return str(resp.json()["job_id"])
    raise RuntimeError(f"Sync trigger failed: {resp.status_code} {resp.text}")


async def wait_for_job(
    client: httpx.AsyncClient, headers: dict[str, str], job_id: str, max_wait: float = 300
) -> dict[str, Any]:
    """Poll until job completes or times out."""
    start = time.monotonic()
    while time.monotonic() - start < max_wait:
        resp = await client.get(f"{API_BASE}/v1/jobs/{job_id}", headers=headers)
        if resp.status_code != 200:
            await asyncio.sleep(2)
            continue
        data = resp.json()
        status = data.get("status", "")
        if status in ("success", "partial_success", "failed"):
            return {
                "job_id": job_id,
                "status": status,
                "duration": time.monotonic() - start,
            }
        await asyncio.sleep(3)
    return {"job_id": job_id, "status": "timeout", "duration": max_wait}


async def run_single_sync(
    client: httpx.AsyncClient, headers: dict[str, str], conn_id: str, idx: int
) -> dict[str, Any]:
    """Run one sync end-to-end and return timing."""
    t0 = time.monotonic()
    try:
        job_id = await trigger_sync(client, headers, conn_id)
        print(f"  [{idx}] triggered job {job_id[:8]}...")
        result = await wait_for_job(client, headers, job_id)
        result["index"] = idx
        result["wall_time"] = time.monotonic() - t0
        return result
    except Exception as e:
        return {
            "index": idx,
            "job_id": "N/A",
            "status": "error",
            "error": str(e),
            "wall_time": time.monotonic() - t0,
        }


async def run_batch(
    n: int, client: httpx.AsyncClient, headers: dict[str, str], conn_id: str
) -> dict[str, Any]:
    """Fire N syncs in parallel, wait for all, return stats."""
    print(f"\n{'=' * 60}")
    print(f"  BATCH: {n} parallel syncs")
    print(f"{'=' * 60}")

    t0 = time.monotonic()
    tasks = [run_single_sync(client, headers, conn_id, i) for i in range(n)]
    results = await asyncio.gather(*tasks)
    wall = time.monotonic() - t0

    successes = [r for r in results if r["status"] in ("success", "partial_success")]
    failures = [r for r in results if r["status"] not in ("success", "partial_success")]
    durations = [r["wall_time"] for r in results]

    print("\n  Results:")
    for r in sorted(results, key=lambda x: x["index"]):
        status_icon = "✓" if r["status"] in ("success", "partial_success") else "✗"
        extra = f" error={r.get('error', '')}" if r.get("error") else ""
        print(f"    {status_icon} [{r['index']}] {r['status']:16s} {r['wall_time']:.1f}s{extra}")

    stats: dict[str, Any] = {
        "batch_size": n,
        "wall_time": wall,
        "successes": len(successes),
        "failures": len(failures),
        "min_duration": min(durations) if durations else 0,
        "max_duration": max(durations) if durations else 0,
        "avg_duration": sum(durations) / len(durations) if durations else 0,
    }

    print(f"\n  Summary: {stats['successes']}/{n} succeeded, wall={wall:.1f}s, "
          f"min={stats['min_duration']:.1f}s, max={stats['max_duration']:.1f}s, "
          f"avg={stats['avg_duration']:.1f}s")

    return stats


async def main() -> None:
    max_n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    batch_sizes = [b for b in [1, 3, 5, 10] if b <= max_n]

    print("WayCore Stress Test")
    print(f"Target: {API_BASE}")
    print(f"Bank: {BANK_URL}")
    print(f"Batches: {batch_sizes}")

    api_key = _get_or_create_api_key()
    print(f"API key: {api_key[:12]}...")
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        resp = await client.get(f"{API_BASE}/healthz")
        assert resp.status_code == 200, f"Health check failed: {resp.status_code}"
        print("Health check: OK")

        conn_id = await create_connection(client, headers)
        if not conn_id:
            print("ERROR: Could not create connection")
            return
        print(f"Connection: {conn_id[:8]}...")

        all_stats: list[dict[str, Any]] = []
        for n in batch_sizes:
            stats = await run_batch(n, client, headers, conn_id)
            all_stats.append(stats)
            if n != batch_sizes[-1]:
                print("\n  (waiting 5s before next batch...)")
                await asyncio.sleep(5)

        print(f"\n{'=' * 60}")
        print("  FINAL REPORT")
        print(f"{'=' * 60}")
        print(f"  {'Batch':>6s}  {'Success':>8s}  {'Wall(s)':>8s}  {'Avg(s)':>8s}  {'Max(s)':>8s}  {'Throughput':>12s}")
        for s in all_stats:
            tp = s["successes"] / s["wall_time"] if s["wall_time"] > 0 else 0
            print(
                f"  {s['batch_size']:>6d}  {s['successes']:>8d}  "
                f"{s['wall_time']:>8.1f}  {s['avg_duration']:>8.1f}  "
                f"{s['max_duration']:>8.1f}  {tp:>10.2f}/s"
            )


if __name__ == "__main__":
    asyncio.run(main())
