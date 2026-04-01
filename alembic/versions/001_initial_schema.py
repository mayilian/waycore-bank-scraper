"""Initial schema

Revision ID: 001
Revises:
Create Date: 2026-04-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False, server_default="starter"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("org_id", UUID(as_uuid=False), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("email", sa.Text(), unique=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    op.create_table(
        "bank_connections",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=False), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("bank_slug", sa.String(64), nullable=False),
        sa.Column("bank_name", sa.Text()),
        sa.Column("login_url", sa.Text(), nullable=False),
        sa.Column("username_enc", sa.Text(), nullable=False),
        sa.Column("password_enc", sa.Text(), nullable=False),
        sa.Column("otp_mode", sa.String(16), nullable=False, server_default="static"),
        sa.Column("otp_value_enc", sa.Text()),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_bank_connections_user_id", "bank_connections", ["user_id"])

    op.create_table(
        "accounts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "connection_id",
            UUID(as_uuid=False),
            sa.ForeignKey("bank_connections.id"),
            nullable=False,
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text()),
        sa.Column("account_type", sa.String(32)),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("connection_id", "external_id", name="uq_accounts_connection_external"),
    )
    op.create_index("ix_accounts_connection_id", "accounts", ["connection_id"])

    op.create_table(
        "balances",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("account_id", UUID(as_uuid=False), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("available", sa.Numeric(20, 4)),
        sa.Column("current", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_balances_account_id", "balances", ["account_id"])

    op.create_table(
        "transactions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "account_id", UUID(as_uuid=False), sa.ForeignKey("accounts.id"), nullable=False
        ),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("description", sa.Text()),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("running_balance", sa.Numeric(20, 4)),
        sa.Column("raw", sa.JSON()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "account_id", "external_id", name="uq_transactions_account_external"
        ),
    )
    op.create_index("ix_transactions_account_id", "transactions", ["account_id"])
    op.create_index("ix_transactions_posted_at", "transactions", ["posted_at"])

    op.create_table(
        "sync_jobs",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("restate_id", sa.Text(), unique=True),
        sa.Column(
            "connection_id",
            UUID(as_uuid=False),
            sa.ForeignKey("bank_connections.id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("failure_reason", sa.Text()),
        sa.Column("transactions_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accounts_synced", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_sync_jobs_connection_id", "sync_jobs", ["connection_id"])

    op.create_table(
        "sync_steps",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("job_id", UUID(as_uuid=False), sa.ForeignKey("sync_jobs.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("output", sa.JSON()),
        sa.Column("screenshot_path", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_sync_steps_job_id", "sync_steps", ["job_id"])


def downgrade() -> None:
    op.drop_table("sync_steps")
    op.drop_table("sync_jobs")
    op.drop_table("transactions")
    op.drop_table("balances")
    op.drop_table("accounts")
    op.drop_table("bank_connections")
    op.drop_table("users")
    op.drop_table("organizations")
