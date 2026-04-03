#!/usr/bin/env bash
set -e

# Run migrations if this is the worker container (has alembic config).
# Safe to run on every start — alembic skips already-applied migrations.
if [ -f alembic.ini ]; then
    echo "Running database migrations..."
    uv run alembic upgrade head
fi

exec "$@"
