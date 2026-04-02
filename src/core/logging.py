"""Structured JSON logging via structlog.

Configures structlog with JSON output, stdlib bridge (captures warnings
and third-party library logs), and contextvars for job-scoped fields.

Usage:
    configure_logging("api")          # call once at entrypoint
    log = get_logger(__name__)
    log.info("event", key="value")

    bind_job_context(job_id, ...)     # inside workflow steps
    clear_job_context()               # after workflow completes
"""

import logging
import os
from typing import cast

import structlog


def configure_logging(service: str = "worker") -> None:
    """Configure structlog with JSON rendering and stdlib bridge.

    Args:
        service: Service name included in every log line ("api" or "worker").
    """
    log_level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)

    # Bridge stdlib logging → structlog so third-party libraries (sqlalchemy,
    # uvicorn, playwright) emit structured JSON instead of unformatted text.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

    # Bind service name globally so every log line identifies the source.
    structlog.contextvars.bind_contextvars(service=service)

    # Route stdlib logging through structlog for consistent JSON output.
    logging.basicConfig(format="%(message)s", level=log_level, handlers=[logging.StreamHandler()])
    for name in ("uvicorn", "uvicorn.access", "sqlalchemy.engine", "hypercorn"):
        logging.getLogger(name).setLevel(log_level)


def get_logger(name: str) -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger(name))


def bind_job_context(job_id: str, connection_id: str, bank_slug: str) -> None:
    """Bind job context — all downstream logs auto-include these fields."""
    structlog.contextvars.bind_contextvars(
        job_id=job_id, connection_id=connection_id, bank_slug=bank_slug
    )


def clear_job_context() -> None:
    structlog.contextvars.clear_contextvars()
