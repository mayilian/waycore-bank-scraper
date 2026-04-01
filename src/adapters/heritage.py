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

from playwright.async_api import Page

from src.adapters.base import AccountData, BalanceData, BankAdapter, TransactionData
from src.agent import extractor
from src.core.logging import get_logger
from src.core.stealth import human_fill, human_move_and_click

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
            await human_fill(page, _SEL_USERNAME, username)
            await human_fill(page, _SEL_PASSWORD, password)
            await asyncio.sleep(0.4)
            await human_move_and_click(page, _SEL_SUBMIT)
        except Exception:
            log.warning("heritage.selector_miss_credentials", fallback="llm")
            fields = await extractor.find_login_fields(page)
            await human_fill(page, fields["username_selector"], username)
            await human_fill(page, fields["password_selector"], password)
            await human_move_and_click(page, fields["submit_selector"])

        await page.wait_for_load_state("networkidle", timeout=15_000)

    async def is_otp_required(self, page: Page) -> bool:
        state = await extractor.detect_post_login_state(page)
        return state == "otp_required"

    async def submit_otp(self, page: Page, otp: str) -> None:
        try:
            await human_fill(page, _SEL_OTP_INPUT, otp)
            await asyncio.sleep(0.3)
            await human_move_and_click(page, _SEL_OTP_SUBMIT)
        except Exception:
            log.warning("heritage.selector_miss_otp", fallback="llm")
            sel = await extractor.find_otp_field(page)
            await human_fill(page, sel, otp)
            await human_move_and_click(page, _SEL_OTP_SUBMIT)

        await page.wait_for_load_state("networkidle", timeout=15_000)

    async def get_accounts(self, page: Page) -> list[AccountData]:
        # LLM extracts accounts — structured parsing of account lists is fragile
        # across different bank UI frameworks.
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

    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        all_transactions: list[TransactionData] = []
        page_num = 0

        while True:
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

            await human_move_and_click(page, next_action.selector)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await asyncio.sleep(0.5)

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
            current=float(raw.get("current", 0)),
            available=float(raw["available"]) if raw.get("available") is not None else None,
            currency=raw.get("currency") or account.currency,
            captured_at=datetime.now(UTC),
        )

    def _parse_transaction(self, raw: dict) -> TransactionData | None:
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
            amount = float(str(amount_raw).replace(",", "").replace("$", ""))

            return TransactionData(
                external_id=external_id,
                posted_at=posted_at,
                description=raw.get("description"),
                amount=amount,
                currency=raw.get("currency") or "USD",
                running_balance=float(raw["running_balance"]) if raw.get("running_balance") else None,
                raw=raw,
            )
        except (ValueError, TypeError):
            log.warning("heritage.txn_parse_error", raw=raw)
            return None
