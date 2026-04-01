"""Restate ASGI application.

Served by hypercorn:
  uv run hypercorn "src.worker.app:app" --bind "0.0.0.0:9000"

After starting, register with the Restate server:
  curl -X POST http://localhost:9070/deployments \\
    -H 'Content-Type: application/json' \\
    -d '{"uri": "http://localhost:9000"}'

In Docker Compose this registration is handled by the 'register' service.
"""

import restate

from src.core.logging import configure_logging
from src.worker.workflow import sync_workflow

configure_logging()

app = restate.app(services=[sync_workflow])
