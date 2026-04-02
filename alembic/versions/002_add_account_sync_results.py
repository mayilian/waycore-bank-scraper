"""Add account_sync_results table

Revision ID: 002
Revises: 001
Create Date: 2026-04-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_sync_results",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=False), sa.ForeignKey("sync_jobs.id"), nullable=False),
        sa.Column("account_id", UUID(as_uuid=False), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("transactions_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("transactions_inserted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("balance_captured", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", "account_id", name="uq_account_sync_results_job_account"),
    )
    op.create_index("ix_account_sync_results_job_id", "account_sync_results", ["job_id"])
    op.create_index("ix_account_sync_results_account_id", "account_sync_results", ["account_id"])


def downgrade() -> None:
    op.drop_table("account_sync_results")
