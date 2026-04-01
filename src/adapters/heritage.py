"""Heritage Bank adapter — deterministic DOM parsing with LLM fallback.

Tier 1: Direct selector/DOM parsing — fast, free, deterministic.
Tier 2: LLM text-only fallback — when selectors break after UI updates.
Tier 3: LLM vision — last resort (handled by GenericAdapter).

Demo credentials: user / pass / OTP 123456
URL: https://demo-bank-2.vercel.app/
"""

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from playwright.async_api import Page

from src.adapters.base import AccountData, BalanceData, BankAdapter, TransactionData
from src.agent import extractor
from src.core.logging import get_logger

log = get_logger(__name__)

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
        await asyncio.sleep(2)
        return True
    except Exception:
        return False


class HeritageBankAdapter(BankAdapter):
    bank_slug = "heritage_bank"

    # ── Login flow (already deterministic) ────────────────────────────────────

    async def navigate_to_login(self, page: Page) -> None:
        await page.wait_for_load_state("networkidle", timeout=15_000)

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

        # Wait for SPA transition — OTP form or dashboard
        try:
            await page.wait_for_selector(
                "#otp, input[name='otp'], nav, [class*='dashboard']",
                timeout=15_000,
            )
        except Exception:
            log.warning("heritage.post_login_transition_timeout")
        await asyncio.sleep(1)

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
        except Exception:
            log.warning("heritage.selector_miss_otp", fallback="llm")
            sel = await extractor.find_otp_field(page)
            await page.fill(sel, otp)
            await page.click(_SEL_OTP_SUBMIT)

        # Wait for dashboard to render
        try:
            await page.wait_for_selector(
                "nav, table, h2:has-text('Account')",
                timeout=20_000,
            )
        except Exception:
            log.warning("heritage.post_otp_transition_timeout")
        await asyncio.sleep(3)

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

        accounts = []
        for row in rows:
            ext_id = row.get("account number", "")
            if not ext_id:
                continue
            acct_type = (row.get("type", "") or "").lower()
            accounts.append(
                AccountData(
                    external_id=ext_id,
                    name=row.get("account name"),
                    account_type=acct_type if acct_type else None,
                    currency="USD",
                )
            )
        return accounts

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
            nav = await extractor.find_account_link(page, account.external_id)
            if nav.action == "click" and nav.selector:
                await page.click(nav.selector)
            else:
                raise RuntimeError(f"Could not navigate to account {account.external_id}")

        await page.wait_for_load_state("networkidle", timeout=15_000)
        if not await _wait_for_spa(page, "table td, [class*='transaction']"):
            log.warning("heritage.account_detail_slow", account=account.external_id)
        log.debug("heritage.navigated_to_account", account=account.external_id)

    # ── Transaction extraction (Tier 1: DOM parsing) ─────────────────────────

    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        all_transactions: list[TransactionData] = []
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1
            log.debug("heritage.extracting_txn_page", account=account.external_id, page=page_num)

            # Tier 1: parse transaction table from DOM
            txns = await self._parse_transactions_from_dom(page)
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
                raw_txns = await extractor.extract_transactions_from_page(page)
                for raw in raw_txns:
                    txn = self._parse_transaction(raw)
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

    async def _parse_transactions_from_dom(self, page: Page) -> list[TransactionData]:
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
                .map(tr => {
                    const cells = Array.from(tr.querySelectorAll('td'))
                        .map(td => td.innerText.trim());
                    const row = {};
                    headers.forEach((h, i) => { if (cells[i]) row[h] = cells[i]; });
                    return row;
                });
        }""")

        transactions = []
        for row in rows:
            txn = self._parse_dom_row(row)
            if txn:
                transactions.append(txn)
        return transactions

    def _parse_dom_row(self, row: dict[str, str]) -> TransactionData | None:
        """Parse a single DOM table row into a TransactionData."""
        try:
            date_str = row.get("date", "")
            description = row.get("description", "")
            amount_str = row.get("amount", "")
            balance_str = row.get("balance", "")

            if not amount_str:
                return None

            # Parse amount: "-$500.00" or "+$12,978.00"
            amount = self._parse_money(amount_str)
            if amount is None:
                return None

            # Parse date
            posted_at = self._parse_date(date_str) if date_str else None

            # Generate stable external_id from date + description + amount
            id_source = f"{date_str}|{description}|{amount_str}"
            external_id = hashlib.sha256(id_source.encode()).hexdigest()[:16]

            # Parse running balance
            running_balance = self._parse_money(balance_str) if balance_str else None

            return TransactionData(
                external_id=external_id,
                posted_at=posted_at,
                description=description or None,
                amount=amount,
                currency="USD",
                running_balance=running_balance,
                raw=row,
            )
        except (ValueError, TypeError, InvalidOperation):
            log.warning("heritage.dom_row_parse_error", row=row)
            return None

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
            // Search the full page text for "CURRENT BALANCE" followed by a dollar amount
            const text = document.body.innerText;
            const match = text.match(/CURRENT BALANCE[\\s\\S]*?(\\$[\\d,.]+)/i);
            return match ? match[1] : null;
        }""")

        if not balance_text:
            return None

        current = self._parse_money(balance_text)
        if current is None:
            return None

        return BalanceData(
            account_external_id=account.external_id,
            current=current,
            available=None,
            currency="USD",
            captured_at=datetime.now(UTC),
        )

    # ── Shared parsing helpers ───────────────────────────────────────────────

    @staticmethod
    def _parse_money(text: str) -> Decimal | None:
        """Parse a money string like '-$1,250.00' or '$516,303.00' into Decimal."""
        try:
            cleaned = re.sub(r"[,$\s+]", "", text)
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _parse_date(text: str) -> datetime | None:
        """Parse date strings like '3/30/2026, 7:28:39 PM'."""
        try:
            return datetime.strptime(text, "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=UTC)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _parse_transaction(self, raw: dict[str, Any]) -> TransactionData | None:
        """Parse an LLM-extracted transaction dict (Tier 2 fallback)."""
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
