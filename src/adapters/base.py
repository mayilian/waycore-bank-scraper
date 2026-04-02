"""BankAdapter abstract base class, shared data models, and browser policy.

Every bank is a subclass of BankAdapter registered in ADAPTER_REGISTRY.
The adapter receives a live Playwright page and returns Pydantic models.
All browser interaction details (stealth, retries) belong here, not in
the workflow layer.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from playwright.async_api import Page
from pydantic import BaseModel

# ── Data models ───────────────────────────────────────────────────────────────


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


@dataclass
class AccountResult:
    """Result of extracting one account — returned by extract_all."""

    account: AccountData
    transactions: list[TransactionData]
    balance: BalanceData
    error: str | None = None


# ── Browser policy ────────────────────────────────────────────────────────────


@dataclass
class BrowserPolicy:
    """Per-bank browser configuration. Adapters override for locale/timezone
    matching, custom viewport, or bank-specific stealth requirements.
    """

    viewport_width: int = 1366
    viewport_height: int = 768
    locale: str = "en-US"
    timezone_id: str = "America/New_York"
    user_agent: str | None = None  # None = use global default from settings
    extra_args: list[str] = field(default_factory=list)


# ── Adapter ABC ───────────────────────────────────────────────────────────────


class BankAdapter(ABC):
    """Interface every bank must implement.

    Two levels of abstraction:
      - Low-level page methods (navigate_to_login, get_accounts, etc.)
        for granular control.
      - extract_all() — the workflow-level unit of work. One browser session,
        discovers accounts, extracts transactions + balance for each.
        Default implementation orchestrates the low-level methods.
        Adapters can override for optimized paths (CSV export, API, etc.).

    Each method receives a live Playwright Page (with stealth already applied
    and cookies already restored). Methods should not manage the browser
    lifecycle — only interact with the page.
    """

    bank_slug: str  # e.g. "heritage_bank" — must match ADAPTER_REGISTRY key
    job_id: str | None = None  # set by step functions for fallback screenshot capture
    browser_policy: BrowserPolicy = BrowserPolicy()

    # ── Login flow ────────────────────────────────────────────────────────────

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

    # ── Account-level methods ─────────────────────────────────────────────────

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

    async def navigate_to_dashboard(self, page: Page) -> None:
        """Return to the account list / dashboard from an account detail page.

        Default: browser back + wait. Adapters should override with a
        deterministic navigation (e.g. click a known dashboard link).
        """
        await page.go_back(wait_until="networkidle", timeout=15_000)

    # ── Workflow-level extraction ─────────────────────────────────────────────

    async def extract_account(
        self, page: Page, account: AccountData
    ) -> tuple[list[TransactionData], BalanceData]:
        """Navigate to an account and extract both transactions and balance in one pass.

        Default implementation calls navigate_to_account → get_transactions → get_balance.
        """
        await self.navigate_to_account(page, account)
        transactions = await self.get_transactions(page, account)
        balance = await self.get_balance(page, account)
        return transactions, balance

    async def extract_all(
        self, page: Page, dashboard_url: str
    ) -> tuple[list[AccountData], list[AccountResult]]:
        """Discover accounts and extract everything in one browser session.

        This is the workflow-level unit of work. One browser, one login session,
        all accounts extracted sequentially. Returns (accounts, results).

        Adapters can override for bank-specific optimizations (e.g. CSV download,
        API endpoints, multi-tab parallel extraction).

        Default implementation: get_accounts → for each account: extract_account,
        navigating back to dashboard between accounts.
        """
        accounts = await self.get_accounts(page)
        results: list[AccountResult] = []

        for i, account in enumerate(accounts):
            try:
                transactions, balance = await self.extract_account(page, account)
                results.append(
                    AccountResult(account=account, transactions=transactions, balance=balance)
                )
            except Exception as exc:
                results.append(
                    AccountResult(
                        account=account,
                        transactions=[],
                        balance=BalanceData(
                            account_external_id=account.external_id,
                            current=Decimal(0),
                            currency=account.currency,
                            captured_at=datetime.now(UTC),
                        ),
                        error=str(exc),
                    )
                )

            # Navigate back to dashboard for the next account (skip after last)
            if i < len(accounts) - 1:
                await self.navigate_to_dashboard(page)

        return accounts, results
