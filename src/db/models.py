"""SQLAlchemy ORM models.

Schema: organizations → users → bank_connections → accounts → transactions / balances.

The multi-tenant hierarchy (Organization, User) exists in the schema but the
application is currently single-tenant: CLI hardcodes a default org/user.
Multi-tenant enforcement (RLS, per-session org_id) is not yet implemented.

Invariants:
  - All money columns: NUMERIC(20,4) — never Float.
  - Balances: append-only (never updated).
  - Transactions: idempotent via UNIQUE(account_id, external_id).
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Organization(Base):
    """Tenant root. Billing attaches here."""

    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    plan: Mapped[str] = mapped_column(String(32), nullable=False, server_default="starter")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    users: Mapped[list["User"]] = relationship("User", back_populates="organization")


class User(Base):
    """Staff member within an org. Billing unit."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    org_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("organizations.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    organization: Mapped[Organization] = relationship("Organization", back_populates="users")
    bank_connections: Mapped[list["BankConnection"]] = relationship(
        "BankConnection", back_populates="user"
    )


class BankConnection(Base):
    """One user's credentials at one bank. Unit of work for a sync job."""

    __tablename__ = "bank_connections"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("users.id"), nullable=False, index=True
    )
    bank_slug: Mapped[str] = mapped_column(String(64), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(Text)
    login_url: Mapped[str] = mapped_column(Text, nullable=False)
    username_enc: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted
    password_enc: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet-encrypted
    otp_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="static"
    )  # static | totp | webhook
    otp_value_enc: Mapped[str | None] = mapped_column(Text)  # Fernet-encrypted
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user: Mapped[User] = relationship("User", back_populates="bank_connections")
    accounts: Mapped[list["Account"]] = relationship("Account", back_populates="connection")
    sync_jobs: Mapped[list["SyncJob"]] = relationship("SyncJob", back_populates="connection")


class Account(Base):
    """A bank account discovered within a connection."""

    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("connection_id", "external_id"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    connection_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bank_connections.id"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # bank's own account ID
    name: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str | None] = mapped_column(String(32))  # checking | savings | credit
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="USD")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    connection: Mapped[BankConnection] = relationship("BankConnection", back_populates="accounts")
    balances: Mapped[list["Balance"]] = relationship("Balance", back_populates="account")
    transactions: Mapped[list["Transaction"]] = relationship(
        "Transaction", back_populates="account"
    )


class Balance(Base):
    """Point-in-time balance snapshot. Append-only — never UPDATE rows.
    Enables balance history charts with no extra work.
    """

    __tablename__ = "balances"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id"), nullable=False, index=True
    )
    available: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    current: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    account: Mapped[Account] = relationship("Account", back_populates="balances")


class Transaction(Base):
    """Immutable transaction ledger entry.
    UNIQUE(account_id, external_id) makes all writes idempotent.
    """

    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("account_id", "external_id"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id"), nullable=False, index=True
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # bank's own transaction ID
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 4), nullable=False)  # negative = debit
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    running_balance: Mapped[Decimal | None] = mapped_column(Numeric(20, 4))
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # original scraped payload
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    account: Mapped[Account] = relationship("Account", back_populates="transactions")


class SyncJob(Base):
    """One sync execution attempt for a bank connection."""

    __tablename__ = "sync_jobs"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    restate_id: Mapped[str | None] = mapped_column(Text, unique=True)  # Restate workflow ID
    connection_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("bank_connections.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="pending"
    )  # pending|running|awaiting_otp|success|failed
    failure_reason: Mapped[str | None] = mapped_column(Text)
    transactions_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    accounts_synced: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    connection: Mapped[BankConnection] = relationship("BankConnection", back_populates="sync_jobs")
    steps: Mapped[list["SyncStep"]] = relationship(
        "SyncStep", back_populates="job", order_by="SyncStep.created_at"
    )
    account_results: Mapped[list["AccountSyncResult"]] = relationship(
        "AccountSyncResult", back_populates="job", order_by="AccountSyncResult.created_at"
    )


class AccountSyncResult(Base):
    """Per-account outcome within a sync job.

    First-class entity for partial success — no longer reconstructed from step rows.
    One row per (job, account) pair. Tracks what was extracted and any error.
    """

    __tablename__ = "account_sync_results"
    __table_args__ = (UniqueConstraint("job_id", "account_id"),)

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("sync_jobs.id"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("accounts.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # success | failed | partial
    transactions_found: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    transactions_inserted: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    balance_captured: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped["SyncJob"] = relationship("SyncJob", back_populates="account_results")
    account: Mapped["Account"] = relationship("Account")


class SyncStep(Base):
    """Step-level audit trail. Human debugging surface.
    One row per step attempt. Screenshots stored on failure.
    """

    __tablename__ = "sync_steps"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(
        UUID(as_uuid=False), ForeignKey("sync_jobs.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # running|success|failed|skipped
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # result data or {error, traceback}
    screenshot_path: Mapped[str | None] = mapped_column(Text)  # set on failure
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job: Mapped[SyncJob] = relationship("SyncJob", back_populates="steps")
