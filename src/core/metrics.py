"""CloudWatch Embedded Metric Format (EMF) emitter.

Prints special JSON to stdout → ECS awslogs → CloudWatch auto-extracts metrics.
No sidecar, no agent, no SDK. Works anywhere logs go to CloudWatch.
"""

import json
import sys
import time


def _emit(
    metric_name: str,
    value: float,
    unit: str = "None",
    dimensions: dict[str, str] | None = None,
) -> None:
    dims = dimensions or {}
    doc = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": "WayCore",
                    "Dimensions": [list(dims.keys())] if dims else [],
                    "Metrics": [{"Name": metric_name, "Unit": unit}],
                }
            ],
        },
        metric_name: value,
        **dims,
    }
    print(json.dumps(doc), file=sys.stdout, flush=True)


def sync_completed(bank_slug: str, duration_secs: float, status: str) -> None:
    _emit("SyncDuration", duration_secs, "Seconds", {"BankSlug": bank_slug, "Status": status})


def sync_failed(bank_slug: str) -> None:
    _emit("SyncFailure", 1, "Count", {"BankSlug": bank_slug})


def llm_fallback(bank_slug: str, step: str) -> None:
    _emit("LLMFallback", 1, "Count", {"BankSlug": bank_slug, "Step": step})


def transactions_synced(bank_slug: str, count: int) -> None:
    _emit("TransactionsSynced", count, "Count", {"BankSlug": bank_slug})
