"""API request/response models. Separate from DB models."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class CreateConnectionRequest(BaseModel):
    bank_url: str
    username: str
    password: str
    otp_mode: str = "static"
    otp: str | None = None


class ConnectionResponse(BaseModel):
    id: str
    bank_slug: str
    bank_name: str | None
    login_url: str
    otp_mode: str
    last_synced_at: datetime | None
    created_at: datetime


class TriggerSyncRequest(BaseModel):
    otp_mode: str = "static"


class TriggerSyncResponse(BaseModel):
    job_id: str


class OtpRequest(BaseModel):
    code: str


class JobResponse(BaseModel):
    id: str
    status: str
    accounts_synced: int
    transactions_synced: int
    failure_reason: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class StepResponse(BaseModel):
    name: str
    status: str
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


class TransactionResponse(BaseModel):
    id: str
    external_id: str
    posted_at: datetime | None
    description: str | None
    amount: Decimal
    currency: str
    running_balance: Decimal | None
