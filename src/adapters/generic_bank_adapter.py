"""Generic bank adapter — LLM-driven, works on any bank URL.

No hardcoded selectors. The LLM discovers everything from the page.
Use this for any bank not in ADAPTER_REGISTRY.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

from src.adapters.base import AccountData, BalanceData, BankAdapter, TransactionData
from src.agent import extractor
from src.core.logging import get_logger
from src.core.stealth import human_fill, human_move_and_click

log = get_logger(__name__)


class GenericBankAdapter(BankAdapter):
    bank_slug = "generic"

    async def navigate_to_login(self, page: Page) -> None:
        await page.wait_for_load_state("networkidle", timeout=20_000)
        log.debug("generic.login_page_loaded", url=page.url)

    async def fill_and_submit_credentials(self, page: Page, username: str, password: str) -> None:
        fields = await extractor.find_login_fields(page)
        await human_fill(page, fields["username_selector"], username)
        await human_fill(page, fields["password_selector"], password)
        await asyncio.sleep(0.3)
        await human_move_and_click(page, fields["submit_selector"])
        await page.wait_for_load_state("networkidle", timeout=20_000)

    async def is_otp_required(self, page: Page) -> bool:
        state = await extractor.detect_post_login_state(page)
        return state == "otp_required"

    async def submit_otp(self, page: Page, otp: str) -> None:
        sel = await extractor.find_otp_field(page)
        await human_fill(page, sel, otp)
        await asyncio.sleep(0.3)

        # Try common submit selectors; raise if none work
        submitted = False
        for submit_sel in (
            "button[type='submit']",
            "button:has-text('Submit')",
            "button:has-text('Verify')",
        ):
            try:
                await page.locator(submit_sel).first.click(timeout=2_000)
                submitted = True
                break
            except PlaywrightError:
                continue
        if not submitted:
            raise RuntimeError("Could not find OTP submit button — manual intervention required")

        await page.wait_for_load_state("networkidle", timeout=20_000)

    async def get_accounts(self, page: Page) -> list[AccountData]:
        raw_accounts = await extractor.extract_accounts(page)
        accounts = [
            AccountData(
                external_id=str(r["external_id"]),
                name=r.get("name"),
                account_type=r.get("account_type"),
                currency=r.get("currency") or "USD",
            )
            for r in raw_accounts
            if r.get("external_id")
        ]
        log.info("generic.accounts_found", count=len(accounts))
        return accounts

    async def navigate_to_account(self, page: Page, account: AccountData) -> None:
        nav = await extractor.find_account_link(page, account.external_id)
        if nav.action == "click" and nav.selector:
            await human_move_and_click(page, nav.selector)
            await page.wait_for_load_state("networkidle", timeout=15_000)
            log.debug("generic.navigated_to_account", account=account.external_id)
        elif nav.action == "done":
            log.debug("generic.already_on_account", account=account.external_id)
        else:
            raise RuntimeError(
                f"Could not navigate to account {account.external_id}: "
                f"LLM returned action={nav.action} selector={nav.selector}"
            )

    async def get_transactions(self, page: Page, account: AccountData) -> list[TransactionData]:
        all_transactions: list[TransactionData] = []
        page_num = 0
        max_pages = 50

        while page_num < max_pages:
            page_num += 1
            raw_txns = await extractor.extract_transactions_from_page(page)
            for raw in raw_txns:
                try:
                    external_id = str(raw.get("external_id") or "")
                    if not external_id:
                        continue
                    posted_at = None
                    if raw.get("posted_at"):
                        try:
                            posted_at = datetime.fromisoformat(str(raw["posted_at"]))
                        except ValueError:
                            pass
                    all_transactions.append(
                        TransactionData(
                            external_id=external_id,
                            posted_at=posted_at,
                            description=raw.get("description"),
                            amount=Decimal(
                                str(raw.get("amount", 0)).replace(",", "").replace("$", "")
                            ),
                            currency=raw.get("currency") or "USD",
                            running_balance=Decimal(str(raw["running_balance"]))
                            if raw.get("running_balance") is not None
                            else None,
                            raw=raw,
                        )
                    )
                except (ValueError, TypeError, InvalidOperation):
                    log.warning("generic.txn_parse_error", raw=raw)

            next_action = await extractor.check_has_next_page(page)
            if next_action.action != "click" or not next_action.selector:
                break

            await human_move_and_click(page, next_action.selector)
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await asyncio.sleep(0.5)

        if page_num >= max_pages:
            log.warning("generic.pagination_limit_reached", account=account.external_id)
        log.info(
            "generic.transactions_extracted",
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
