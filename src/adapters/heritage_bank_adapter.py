"""Heritage Bank adapter — deterministic DOM parsing with LLM fallback.

Tier 1: Direct selector/DOM parsing — fast, free, deterministic.
Tier 2: LLM text-only fallback — when selectors break after UI updates.
Tier 3: LLM vision — last resort (handled by GenericAdapter).

Execution policy lives here. Parsing logic lives in heritage_parsers.py.

Demo credentials: user / pass / OTP 123456
URL: https://demo-bank-2.vercel.app/
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from src.adapters.base import AccountData, BalanceData, BankAdapter, BrowserPolicy, TransactionData
from src.adapters.heritage_parsers import (
    parse_accounts_from_rows,
    parse_balance_text,
    parse_llm_transaction,
    parse_transaction_row,
)
from src.agent import extractor
from src.browser.screenshots import get_screenshot_store
from src.core.logging import get_logger

log = get_logger(__name__)


async def _save_fallback_screenshot(page: Page, job_id: str | None, label: str) -> None:
    """Capture a diagnostic screenshot when Tier 1 selectors fail and LLM fallback kicks in."""
    if not job_id:
        return
    try:
        png = await page.screenshot(type="png", full_page=True)
        store = get_screenshot_store()
        path = await store.save(job_id, f"fallback_{label}", png)
        log.info("heritage.fallback_screenshot", path=path, label=label)
    except Exception:
        log.warning("heritage.fallback_screenshot_failed", label=label)


# ── Known selectors ──────────────────────────────────────────────────────────

_SEL_USERNAME = "input[name='username'], input[id*='user'], input[placeholder*='sername']"
_SEL_PASSWORD = "input[type='password']"
_SEL_SUBMIT = "button[type='submit'], button:has-text('Login'), button:has-text('Sign in')"
_SEL_OTP_INPUT = "input[name='otp'], input[placeholder*='OTP'], input[placeholder*='code']"
_SEL_OTP_SUBMIT = "button[type='submit'], button:has-text('Verify'), button:has-text('Submit')"

_SEL_ACCOUNT_TABLE = "table[aria-label='Accounts'], table.legacy-table"
_SEL_TXN_TABLE = "table[aria-label='Account transactions'], table.legacy-table"
_SEL_NEXT_PAGE = "button:has-text('Next'), a:has-text('Next'), [aria-label='Next page']"


async def _wait_for_spa(page: Page, selector: str, wait_ms: int = 15_000) -> bool:
    """Wait for SPA content to render. Returns True if found."""
    try:
        await page.wait_for_selector(selector, timeout=wait_ms)
        # Wait until the DOM has stabilised (no new elements appearing)
        await page.wait_for_function(
            """(sel) => {
                const count = document.querySelectorAll(sel).length;
                return count > 0;
            }""",
            arg=selector,
            timeout=wait_ms,
        )
        # Brief settle for any trailing renders (replaces fixed 2s sleep)
        await asyncio.sleep(0.3)
        return True
    except Exception:
        return False


class HeritageBankAdapter(BankAdapter):
    bank_slug = "heritage_bank"
    browser_policy = BrowserPolicy(
        locale="en-US",
        timezone_id="America/New_York",
    )

    # ── Login flow (already deterministic) ────────────────────────────────────

    async def navigate_to_login(self, page: Page) -> None:
        await page.wait_for_load_state("networkidle", timeout=15_000)

    async def fill_and_submit_credentials(self, page: Page, username: str, password: str) -> None:
        try:
            await page.fill(_SEL_USERNAME, username)
            await page.fill(_SEL_PASSWORD, password)
            await asyncio.sleep(0.3)
            await page.click(_SEL_SUBMIT)
        except PlaywrightError:
            log.warning("heritage.selector_miss_credentials", fallback="llm")
            await _save_fallback_screenshot(page, self.job_id, "credentials")
            fields = await extractor.find_login_fields(page)
            await page.fill(fields["username_selector"], username)
            await page.fill(fields["password_selector"], password)
            await page.click(fields["submit_selector"])

        # Wait for SPA transition — OTP form or dashboard
        try:
            await page.wait_for_selector(
                "#otp, input[name='otp'], nav, [class*='dashboard']",
                timeout=15_000,
            )
        except Exception:
            log.warning("heritage.post_login_transition_timeout")
        await asyncio.sleep(0.3)

    async def is_otp_required(self, page: Page) -> bool:
        # Tier 1: check for known OTP selector
        otp_field = await page.query_selector(_SEL_OTP_INPUT)
        if otp_field:
            return True
        # Check if we're already on dashboard
        nav = await page.query_selector("nav, [class*='dashboard']")
        if nav:
            return False
        # Tier 2: LLM fallback
        state = await extractor.detect_post_login_state(page)
        return state == "otp_required"

    async def submit_otp(self, page: Page, otp: str) -> None:
        try:
            await page.fill(_SEL_OTP_INPUT, otp)
            await asyncio.sleep(0.3)
            await page.click(_SEL_OTP_SUBMIT)
        except PlaywrightError:
            log.warning("heritage.selector_miss_otp", fallback="llm")
            await _save_fallback_screenshot(page, self.job_id, "otp")
            sel = await extractor.find_otp_field(page)
            await page.fill(sel, otp)
            await page.click(_SEL_OTP_SUBMIT)

        # Wait for dashboard to render with content
        try:
            await page.wait_for_selector(
                "nav, table, h2:has-text('Account')",
                timeout=20_000,
            )
            # Wait for table rows to actually populate
            await page.wait_for_function(
                "() => document.querySelectorAll('table td').length > 0",
                timeout=10_000,
            )
        except Exception:
            log.warning("heritage.post_otp_transition_timeout")
        await asyncio.sleep(0.3)

    # ── Account extraction (Tier 1: DOM parsing) ─────────────────────────────

    async def get_accounts(self, page: Page) -> list[AccountData]:
        if not await _wait_for_spa(page, "table td"):
            log.warning("heritage.dashboard_table_not_found")

        # Tier 1: parse account table rows directly
        accounts = await self._parse_accounts_from_dom(page)
        if accounts:
            log.info("heritage.accounts_found", count=len(accounts), tier=1)
            return accounts

        # Tier 2: LLM fallback
        log.warning("heritage.accounts_dom_parse_failed", fallback="llm")
        await _save_fallback_screenshot(page, self.job_id, "accounts")
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
        log.info("heritage.accounts_found", count=len(accounts), tier=2)
        return accounts

    async def _parse_accounts_from_dom(self, page: Page) -> list[AccountData]:
        """Parse account table rows directly from the DOM."""
        rows: list[dict[str, str]] = await page.evaluate("""() => {
            const table = document.querySelector(
                "table[aria-label='Accounts'], table.legacy-table"
            );
            if (!table) return [];
            const headers = Array.from(table.querySelectorAll('th'))
                .map(th => th.innerText.trim().toLowerCase());
            return Array.from(table.querySelectorAll('tbody tr, tr:not(:first-child)'))
                .filter(tr => tr.querySelector('td'))
                .map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td'))
                        .map(td => td.innerText.trim());
                    const row = {};
                    headers.forEach((h, i) => { if (cells[i]) row[h] = cells[i]; });
                    return row;
                });
        }""")

        return parse_accounts_from_rows(rows)

    # ── Account navigation (Tier 1: deterministic locator) ───────────────────

    async def navigate_to_account(self, page: Page, account: AccountData) -> None:
        if not await _wait_for_spa(page, "table td"):
            log.warning("heritage.table_not_ready", account=account.external_id)

        # Tier 1: find "Open Details" link in the row with this account number
        row_link = page.locator(
            f"tr:has(td:text-is('{account.external_id}')) a:has-text('Open Details'), "
            f"tr:has(td:text-is('{account.external_id}')) a:has-text('Details')"
        )
        if await row_link.count() > 0:
            await row_link.first.click()
        else:
            # Tier 2: LLM fallback
            log.warning("heritage.nav_selector_miss", account=account.external_id, fallback="llm")
            await _save_fallback_screenshot(page, self.job_id, f"nav_{account.external_id}")
            nav = await extractor.find_account_link(page, account.external_id)
            if nav.action == "click" and nav.selector:
                await page.click(nav.selector)
            else:
                raise RuntimeError(f"Could not navigate to account {account.external_id}")

        await page.wait_for_load_state("networkidle", timeout=15_000)
        if not await _wait_for_spa(page, "table td, [class*='transaction']"):
            log.warning("heritage.account_detail_slow", account=account.external_id)
        log.debug("heritage.navigated_to_account", account=account.external_id)

    async def navigate_to_dashboard(self, page: Page) -> None:
        """Return to the accounts dashboard. Heritage Bank has a nav link."""
        dashboard_link = page.locator(
            "a:has-text('Dashboard'), a:has-text('Accounts'), a[href='/'], nav a:first-child"
        )
        if await dashboard_link.count() > 0:
            await dashboard_link.first.click()
        else:
            await page.go_back(wait_until="networkidle", timeout=15_000)

        await page.wait_for_load_state("networkidle", timeout=15_000)
        await _wait_for_spa(page, "table td")

    # ── Transaction extraction (Tier 1: DOM parsing) ─────────────────────────

    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        from src.core.config import settings

        all_transactions: list[TransactionData] = []
        page_num = 0
        max_pages = settings.max_pages_per_account

        while page_num < max_pages:
            page_num += 1
            log.debug("heritage.extracting_txn_page", account=account.external_id, page=page_num)

            # Tier 1: parse transaction table from DOM
            txns = await self._parse_transactions_from_dom(page, page_num)
            if txns:
                all_transactions.extend(txns)
            else:
                # Tier 2: LLM fallback
                log.warning(
                    "heritage.txn_dom_parse_failed",
                    account=account.external_id,
                    page=page_num,
                    fallback="llm",
                )
                await _save_fallback_screenshot(
                    page, self.job_id, f"txn_{account.external_id}_p{page_num}"
                )
                raw_txns = await extractor.extract_transactions_from_page(page)
                for raw in raw_txns:
                    txn = parse_llm_transaction(raw)
                    if txn:
                        all_transactions.append(txn)

            # Check for next page — deterministic first
            has_next = await self._has_next_page(page)
            if not has_next:
                break

            await page.click(_SEL_NEXT_PAGE)
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

    async def _parse_transactions_from_dom(
        self, page: Page, page_num: int = 1
    ) -> list[TransactionData]:
        """Parse transaction rows directly from the HTML table."""
        rows: list[dict[str, str]] = await page.evaluate("""() => {
            const table = document.querySelector(
                "table[aria-label='Account transactions'], table.legacy-table"
            );
            if (!table) return [];
            const headers = Array.from(table.querySelectorAll('th'))
                .map(th => th.innerText.trim().toLowerCase());
            return Array.from(table.querySelectorAll('tbody tr, tr:not(:first-child)'))
                .filter(tr => tr.querySelector('td'))
                .map((tr, idx) => {
                    const cells = Array.from(tr.querySelectorAll('td'))
                        .map(td => td.innerText.trim());
                    const row = {};
                    headers.forEach((h, i) => { if (cells[i]) row[h] = cells[i]; });
                    row['_row_index'] = String(idx);
                    return row;
                });
        }""")

        transactions = []
        for row in rows:
            txn = parse_transaction_row(row, page_num=page_num)
            if txn:
                transactions.append(txn)
        return transactions

    async def _has_next_page(self, page: Page) -> bool:
        """Check for a next-page button deterministically."""
        next_btn = await page.query_selector(_SEL_NEXT_PAGE)
        if next_btn:
            disabled = await next_btn.get_attribute("disabled")
            return disabled is None
        return False

    # ── Balance extraction (Tier 1: DOM parsing) ─────────────────────────────

    async def get_balance(self, page: Page, account: AccountData) -> BalanceData:
        # Tier 1: parse balance from known DOM structure
        balance = await self._parse_balance_from_dom(page, account)
        if balance:
            log.debug("heritage.balance_extracted", tier=1, account=account.external_id)
            return balance

        # Tier 2: LLM fallback
        log.warning("heritage.balance_dom_parse_failed", fallback="llm")
        await _save_fallback_screenshot(page, self.job_id, f"balance_{account.external_id}")
        raw = await extractor.extract_balance(page)
        return BalanceData(
            account_external_id=account.external_id,
            current=Decimal(str(raw.get("current", 0))),
            available=Decimal(str(raw["available"])) if raw.get("available") is not None else None,
            currency=raw.get("currency") or account.currency,
            captured_at=datetime.now(UTC),
        )

    async def _parse_balance_from_dom(self, page: Page, account: AccountData) -> BalanceData | None:
        """Extract balance from the account detail page DOM."""
        balance_text = await page.evaluate("""() => {
            const text = document.body.innerText;
            const match = text.match(/CURRENT BALANCE[\\s\\S]*?(\\$[\\d,.]+)/i);
            return match ? match[1] : null;
        }""")

        if not balance_text:
            return None

        return parse_balance_text(balance_text, account.external_id)
