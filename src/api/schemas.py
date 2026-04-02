"""API request/response models. Separate from DB models."""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

OtpMode = Literal["static", "totp", "webhook"]
JobStatus = Literal["pending", "running", "awaiting_otp", "success", "failed", "partial_success"]
StepStatus = Literal["success", "failed", "partial"]


class CreateConnectionRequest(BaseModel):
    bank_url: str
    username: str
    password: str
    otp_mode: OtpMode = "static"
    otp: str | None = None


class ConnectionResponse(BaseModel):
    id: str
    bank_slug: str
    bank_name: str | None
    login_url: str
    otp_mode: OtpMode
    last_synced_at: datetime | None
    created_at: datetime


class TriggerSyncRequest(BaseModel):
    otp_mode: OtpMode = "static"
    otp: str | None = None


class TriggerSyncResponse(BaseModel):
    job_id: str


class OtpRequest(BaseModel):
    code: str


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    accounts_synced: int
    transactions_synced: int
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class StepResponse(BaseModel):
    name: str
    status: StepStatus
    started_at: datetime | None
    completed_at: datetime | None


class JobDetailResponse(JobResponse):
    steps: list[StepResponse]


class AccountResponse(BaseModel):
    id: str
    external_id: str
    name: str | None
    account_type: str | None
    currency: str


class BalanceResponse(BaseModel):
    id: str
    available: Decimal | None
    current: Decimal
    currency: str
    captured_at: datetime


class TransactionResponse(BaseModel):
    id: str
    external_id: str
    posted_at: datetime | None
    description: str | None
    amount: Decimal
    currency: str
    running_balance: Decimal | None
