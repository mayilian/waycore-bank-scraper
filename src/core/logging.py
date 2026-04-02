"""Structured JSON logging via structlog."""

import logging
from typing import cast

import structlog


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


def get_logger(name: str) -> structlog.BoundLogger:
    return cast(structlog.BoundLogger, structlog.get_logger(name))


def bind_job_context(job_id: str, connection_id: str, bank_slug: str) -> None:
    """Bind job context — all downstream logs auto-include these fields."""
    structlog.contextvars.bind_contextvars(
        job_id=job_id, connection_id=connection_id, bank_slug=bank_slug
    )


def clear_job_context() -> None:
    structlog.contextvars.clear_contextvars()
