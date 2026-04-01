"""BankAdapter abstract base class and shared data models.

Every bank is a subclass of BankAdapter registered in ADAPTER_REGISTRY.
The adapter receives a live Playwright page and returns Pydantic models.
All browser interaction details (stealth, retries) belong here, not in
the workflow layer.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from playwright.async_api import Page
from pydantic import BaseModel


class AccountData(BaseModel):
    external_id: str
    name: str | None = None
    account_type: str | None = None  # checking | savings | credit
    currency: str = "USD"


class BalanceData(BaseModel):
    account_external_id: str
    current: Decimal
    available: Decimal | None = None
    currency: str = "USD"
    captured_at: datetime


class TransactionData(BaseModel):
    external_id: str
    posted_at: datetime | None = None
    description: str | None = None
    amount: Decimal  # negative = debit
    currency: str = "USD"
    running_balance: Decimal | None = None
    raw: dict[str, Any] | None = None


class BankAdapter(ABC):
    """Interface every bank must implement.

    Each method receives a live Playwright Page (with stealth already applied
    and cookies already restored). Methods should not manage the browser
    lifecycle — only interact with the page.
    """

    bank_slug: str  # e.g. "heritage_bank" — must match ADAPTER_REGISTRY key

    @abstractmethod
    async def navigate_to_login(self, page: Page) -> None:
        """Navigate to the login form. Page may already be on login URL."""

    @abstractmethod
    async def fill_and_submit_credentials(self, page: Page, username: str, password: str) -> None:
        """Fill and submit the login form."""

    @abstractmethod
    async def is_otp_required(self, page: Page) -> bool:
        """Return True if an OTP prompt is visible after login."""

    @abstractmethod
    async def submit_otp(self, page: Page, otp: str) -> None:
        """Enter and submit the OTP."""

    @abstractmethod
    async def get_accounts(self, page: Page) -> list[AccountData]:
        """Return all accounts visible in the authenticated session."""

    @abstractmethod
    async def navigate_to_account(self, page: Page, account: AccountData) -> None:
        """Navigate to a specific account's detail page before extraction."""

    @abstractmethod
    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        """Return the full transaction history for one account.
        Caller must call navigate_to_account() first.
        """

    @abstractmethod
    async def get_balance(self, page: Page, account: AccountData) -> BalanceData:
        """Return the current balance for one account.
        Caller must call navigate_to_account() first.
        """
