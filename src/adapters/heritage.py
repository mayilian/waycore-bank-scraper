"""Heritage Bank adapter — structured implementation for the demo bank.

Uses known CSS selectors as hints, falling back to LLM extraction when
a selector is not found. This is the fast path: zero LLM calls for
navigation, LLM only for data extraction where structured parsing
is fragile.

Demo credentials: user / pass / OTP 123456
URL: https://demo-bank-2.vercel.app/
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import Page

from src.adapters.base import AccountData, BalanceData, BankAdapter, TransactionData
from src.agent import extractor
from src.core.logging import get_logger

log = get_logger(__name__)

# Known selectors — update these if the demo bank UI changes.
# The adapter falls back to LLM extraction if any selector misses.
_SEL_USERNAME = "input[name='username'], input[id*='user'], input[placeholder*='sername']"
_SEL_PASSWORD = "input[type='password']"
_SEL_SUBMIT = "button[type='submit'], button:has-text('Login'), button:has-text('Sign in')"
_SEL_OTP_INPUT = "input[name='otp'], input[placeholder*='OTP'], input[placeholder*='code']"
_SEL_OTP_SUBMIT = "button[type='submit'], button:has-text('Verify'), button:has-text('Submit')"


class HeritageBankAdapter(BankAdapter):
    bank_slug = "heritage_bank"

    async def navigate_to_login(self, page: Page) -> None:
        # Page is already on login_url — just wait for the form to render.
        await page.wait_for_load_state("networkidle", timeout=15_000)
        log.debug("heritage.login_page_loaded", url=page.url)

    async def fill_and_submit_credentials(self, page: Page, username: str, password: str) -> None:
        try:
            await page.fill(_SEL_USERNAME, username)
            await page.fill(_SEL_PASSWORD, password)
            await asyncio.sleep(0.3)
            await page.click(_SEL_SUBMIT)
        except Exception:
            log.warning("heritage.selector_miss_credentials", fallback="llm")
            fields = await extractor.find_login_fields(page)
            await page.fill(fields["username_selector"], username)
            await page.fill(fields["password_selector"], password)
            await page.click(fields["submit_selector"])

        # Wait for the SPA to transition — either OTP form or dashboard
        try:
            await page.wait_for_selector(
                "#otp, input[name='otp'], nav, [class*='dashboard']",
                timeout=15_000,
            )
        except Exception:
            log.warning("heritage.post_login_transition_timeout")
        await asyncio.sleep(1)

    async def is_otp_required(self, page: Page) -> bool:
        state = await extractor.detect_post_login_state(page)
        return state == "otp_required"

    async def submit_otp(self, page: Page, otp: str) -> None:
        try:
            await page.fill(_SEL_OTP_INPUT, otp)
            await asyncio.sleep(0.3)
            await page.click(_SEL_OTP_SUBMIT)
        except Exception:
            log.warning("heritage.selector_miss_otp", fallback="llm")
            sel = await extractor.find_otp_field(page)
            await page.fill(sel, otp)
            await page.click(_SEL_OTP_SUBMIT)

        # Wait for the SPA to process OTP and transition to dashboard
        try:
            await page.wait_for_selector(
                "nav, [class*='dashboard'], table, h2:has-text('Account')",
                timeout=20_000,
            )
        except Exception:
            log.warning("heritage.post_otp_transition_timeout")
        await asyncio.sleep(3)

    async def get_accounts(self, page: Page) -> list[AccountData]:
        # SPA renders asynchronously from localStorage — wait for table content
        try:
            await page.wait_for_selector("table, td, [class*='account']", timeout=15_000)
            await asyncio.sleep(2)  # let remaining rows render
        except Exception:
            log.warning("heritage.dashboard_table_not_found")
        raw_accounts = await extractor.extract_accounts(page)
        accounts = []
        for raw in raw_accounts:
            if not raw.get("external_id"):
                continue
            accounts.append(
                AccountData(
                    external_id=str(raw["external_id"]),
                    name=raw.get("name"),
                    account_type=raw.get("account_type"),
                    currency=raw.get("currency") or "USD",
                )
            )
        log.info("heritage.accounts_found", count=len(accounts))
        return accounts

    async def navigate_to_account(self, page: Page, account: AccountData) -> None:
        # Wait for the account table to render (SPA loads from localStorage)
        try:
            await page.wait_for_selector("table td", timeout=15_000)
            await asyncio.sleep(2)
        except Exception:
            log.warning("heritage.table_not_ready", account=account.external_id)

        # Find the table row containing this account's external_id
        # and click the "Open Details" link in that row.
        row_link = page.locator(
            f"tr:has(td:text-is('{account.external_id}')) a:has-text('Open Details'), "
            f"tr:has(td:text-is('{account.external_id}')) a:has-text('Details')"
        )
        if await row_link.count() > 0:
            await row_link.first.click()
        else:
            # Fallback: use LLM to find the link
            nav = await extractor.find_account_link(page, account.external_id)
            if nav.action == "click" and nav.selector:
                await page.click(nav.selector)
            else:
                raise RuntimeError(
                    f"Could not navigate to account {account.external_id}"
                )

        await page.wait_for_load_state("networkidle", timeout=15_000)
        # Wait for transaction table or balance to render in the SPA
        try:
            await page.wait_for_selector("table, td, [class*='transaction']", timeout=15_000)
            await asyncio.sleep(3)
        except Exception:
            log.warning("heritage.account_detail_slow", account=account.external_id)
        log.debug("heritage.navigated_to_account", account=account.external_id)

    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        all_transactions: list[TransactionData] = []
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1
            log.debug("heritage.extracting_txn_page", account=account.external_id, page=page_num)

            raw_txns = await extractor.extract_transactions_from_page(page)
            for raw in raw_txns:
                txn = self._parse_transaction(raw)
                if txn:
                    all_transactions.append(txn)

            next_action = await extractor.check_has_next_page(page)
            if next_action.action != "click" or not next_action.selector:
                break

            await page.click(next_action.selector)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await asyncio.sleep(0.5)

        if page_num >= max_pages:
            log.warning("heritage.pagination_limit_reached", account=account.external_id)
        log.info(
            "heritage.transactions_extracted",
            account=account.external_id,
            count=len(all_transactions),
        )
        return all_transactions

    async def get_balance(self, page: Page, account: AccountData) -> BalanceData:
        raw = await extractor.extract_balance(page)
        return BalanceData(
            account_external_id=account.external_id,
            current=Decimal(str(raw.get("current", 0))),
            available=Decimal(str(raw["available"])) if raw.get("available") is not None else None,
            currency=raw.get("currency") or account.currency,
            captured_at=datetime.now(UTC),
        )

    def _parse_transaction(self, raw: dict[str, Any]) -> TransactionData | None:
        try:
            external_id = str(raw.get("external_id") or "")
            if not external_id:
                return None

            posted_at = None
            if raw.get("posted_at"):
                try:
                    posted_at = datetime.fromisoformat(str(raw["posted_at"]))
                except ValueError:
                    pass

            amount_raw = raw.get("amount", 0)
            amount = Decimal(str(amount_raw).replace(",", "").replace("$", ""))

            return TransactionData(
                external_id=external_id,
                posted_at=posted_at,
                description=raw.get("description"),
                amount=amount,
                currency=raw.get("currency") or "USD",
                running_balance=Decimal(str(raw["running_balance"]))
                if raw.get("running_balance") is not None
                else None,
                raw=raw,
            )
        except (ValueError, TypeError, InvalidOperation):
            log.warning("heritage.txn_parse_error", raw=raw)
            return None
