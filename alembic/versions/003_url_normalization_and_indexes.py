"""URL normalization column, cloud-ready indexes, backfill normalized URLs.

Revision ID: 003
Revises: 002
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _normalize_url(url: str) -> str:
    """Inline normalization — same logic as src/core/urls.py."""
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    netloc = f"{host}:{port}" if port else host
    path = parsed.path.rstrip("/") or ""
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def upgrade() -> None:
    # 1. Add login_url_normalized column
    op.add_column(
        "bank_connections",
        sa.Column("login_url_normalized", sa.Text(), nullable=True),
    )

    # 2. Backfill normalized URLs from existing login_url values
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, login_url FROM bank_connections WHERE login_url_normalized IS NULL")
    )
    for row in rows:
        normalized = _normalize_url(row.login_url)
        conn.execute(
            sa.text("UPDATE bank_connections SET login_url_normalized = :norm WHERE id = :id"),
            {"norm": normalized, "id": row.id},
        )

    # 3. Index on normalized URL for connection matching
    op.create_index(
        "ix_bank_connections_login_url_normalized",
        "bank_connections",
        ["login_url_normalized"],
    )

    # 4. Cloud-ready composite indexes for common dashboard queries

    # "Latest balance per account" — hot query for account overview
    op.create_index(
        "ix_balances_account_captured_desc",
        "balances",
        ["account_id", sa.text("captured_at DESC")],
    )

    # "Recent sync jobs for a connection" — sync history view
    op.create_index(
        "ix_sync_jobs_connection_created_desc",
        "sync_jobs",
        ["connection_id", sa.text("created_at DESC")],
    )

    # "Account sync results by account" — per-account history
    op.create_index(
        "ix_account_sync_results_account_created_desc",
        "account_sync_results",
        ["account_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_account_sync_results_account_created_desc", "account_sync_results")
    op.drop_index("ix_sync_jobs_connection_created_desc", "sync_jobs")
    op.drop_index("ix_balances_account_captured_desc", "balances")
    op.drop_index("ix_bank_connections_login_url_normalized", "bank_connections")
    op.drop_column("bank_connections", "login_url_normalized")
