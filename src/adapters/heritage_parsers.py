"""Heritage Bank parsing helpers — pure data transformation, no browser interaction.

Extracted from HeritageBankAdapter to separate execution policy (adapter) from
data parsing (this module). These functions take raw DOM data and return
typed domain objects.
"""

import hashlib
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from src.adapters.base import AccountData, BalanceData, TransactionData
from src.core.logging import get_logger

log = get_logger(__name__)


def parse_money(text: str) -> Decimal | None:
    """Parse a money string like '-$1,250.00' or '$516,303.00' into Decimal."""
    try:
        cleaned = re.sub(r"[,$\s+]", "", text)
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


def parse_date(text: str) -> datetime | None:
    """Parse date strings like '3/30/2026, 7:28:39 PM'."""
    try:
        return datetime.strptime(text, "%m/%d/%Y, %I:%M:%S %p").replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_accounts_from_rows(rows: list[dict[str, str]]) -> list[AccountData]:
    """Parse account table rows (from DOM evaluate) into AccountData list."""
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


def parse_transaction_row(
    row: dict[str, str], page_num: int = 0
) -> TransactionData | None:
    """Parse a single DOM table row into a TransactionData."""
    try:
        date_str = row.get("date", "")
        description = row.get("description", "")
        amount_str = row.get("amount", "")
        balance_str = row.get("balance", "")

        if not amount_str:
            return None

        amount = parse_money(amount_str)
        if amount is None:
            return None

        posted_at = parse_date(date_str) if date_str else None

        # Generate stable external_id from date + description + amount + row index + page
        # Page number is included to avoid collisions across paginated results
        row_index = row.get("_row_index", "")
        balance_str_for_id = balance_str or ""
        id_source = f"{date_str}|{description}|{amount_str}|{row_index}|{balance_str_for_id}|{page_num}"
        external_id = hashlib.sha256(id_source.encode()).hexdigest()[:16]

        running_balance = parse_money(balance_str) if balance_str else None

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


def parse_llm_transaction(raw: dict[str, Any]) -> TransactionData | None:
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


def parse_balance_text(
    balance_text: str, account_external_id: str
) -> BalanceData | None:
    """Parse a balance string extracted from the DOM."""
    current = parse_money(balance_text)
    if current is None:
        return None

    return BalanceData(
        account_external_id=account_external_id,
        current=current,
        available=None,
        currency="USD",
        captured_at=datetime.now(UTC),
    )
