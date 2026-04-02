"""Tests for adapter registry, parsing helpers, and adapter contract."""

from datetime import datetime
from decimal import Decimal

from src.adapters import get_adapter
from src.adapters.base import AccountData, AccountResult, BalanceData, BrowserPolicy
from src.adapters.generic_bank_adapter import GenericBankAdapter
from src.adapters.heritage_bank_adapter import HeritageBankAdapter
from src.adapters.heritage_parsers import (
    parse_balance_text,
    parse_llm_transaction,
    parse_money,
    parse_transaction_row,
)


# ── Registry tests ────────────────────────────────────────────────────────────


def test_heritage_adapter_registered() -> None:
    adapter = get_adapter("heritage_bank")
    assert isinstance(adapter, HeritageBankAdapter)


def test_unknown_slug_falls_back_to_generic() -> None:
    adapter = get_adapter("some_unknown_bank")
    assert isinstance(adapter, GenericBankAdapter)


# ── BrowserPolicy tests ──────────────────────────────────────────────────────


def test_heritage_has_browser_policy() -> None:
    adapter = HeritageBankAdapter()
    assert isinstance(adapter.browser_policy, BrowserPolicy)
    assert adapter.browser_policy.locale == "en-US"


def test_browser_policy_defaults() -> None:
    policy = BrowserPolicy()
    assert policy.viewport_width == 1366
    assert policy.viewport_height == 768
    assert policy.user_agent is None
    assert policy.extra_args == []


# ── Transaction parsing (heritage_parsers) ────────────────────────────────────


def test_parse_transaction_row_valid() -> None:
    row = {
        "date": "3/30/2026, 7:28:39 PM",
        "description": "Coffee Shop",
        "amount": "-$4.50",
        "balance": "$995.50",
        "_row_index": "0",
    }
    txn = parse_transaction_row(row)
    assert txn is not None
    assert txn.amount == Decimal("-4.50")
    assert txn.running_balance == Decimal("995.50")
    assert txn.description == "Coffee Shop"
    assert len(txn.external_id) == 16  # SHA256 hash prefix


def test_parse_transaction_row_missing_amount() -> None:
    txn = parse_transaction_row({"description": "Test"})
    assert txn is None


def test_parse_llm_transaction_valid() -> None:
    raw = {
        "external_id": "TXN001",
        "posted_at": "2026-01-15T00:00:00",
        "description": "Coffee Shop",
        "amount": "-4.50",
        "currency": "USD",
        "running_balance": "995.50",
    }
    txn = parse_llm_transaction(raw)
    assert txn is not None
    assert txn.external_id == "TXN001"
    assert txn.amount == Decimal("-4.50")
    assert txn.running_balance == Decimal("995.50")


def test_parse_llm_transaction_missing_external_id() -> None:
    txn = parse_llm_transaction({"amount": "10.00"})
    assert txn is None


def test_parse_llm_transaction_malformed_amount() -> None:
    txn = parse_llm_transaction({"external_id": "TXN", "amount": "not_a_number"})
    assert txn is None


def test_parse_llm_transaction_dollar_sign_and_commas() -> None:
    raw = {"external_id": "TXN", "amount": "$1,234.56"}
    txn = parse_llm_transaction(raw)
    assert txn is not None
    assert txn.amount == Decimal("1234.56")


def test_parse_llm_transaction_zero_running_balance() -> None:
    """Verify running_balance=0 is preserved (not treated as None via truthiness)."""
    raw = {"external_id": "TXN", "amount": "100", "running_balance": "0"}
    txn = parse_llm_transaction(raw)
    assert txn is not None
    assert txn.running_balance == Decimal("0")


# ── Money parsing ─────────────────────────────────────────────────────────────


def test_parse_money_standard() -> None:
    assert parse_money("$1,250.00") == Decimal("1250.00")


def test_parse_money_negative() -> None:
    assert parse_money("-$4.50") == Decimal("-4.50")


def test_parse_money_invalid() -> None:
    assert parse_money("not_money") is None


# ── Balance parsing ───────────────────────────────────────────────────────────


def test_parse_balance_text_valid() -> None:
    result = parse_balance_text("$10,500.25", "ACC001")
    assert result is not None
    assert result.current == Decimal("10500.25")
    assert result.account_external_id == "ACC001"


def test_parse_balance_text_invalid() -> None:
    assert parse_balance_text("no balance here", "ACC001") is None


# ── AccountResult model ───────────────────────────────────────────────────────


def test_account_result_success() -> None:
    result = AccountResult(
        account=AccountData(external_id="ACC1"),
        transactions=[],
        balance=BalanceData(
            account_external_id="ACC1",
            current=Decimal("100"),
            captured_at=datetime.now(),
        ),
    )
    assert result.error is None


def test_account_result_failure() -> None:
    result = AccountResult(
        account=AccountData(external_id="ACC1"),
        transactions=[],
        balance=BalanceData(
            account_external_id="ACC1",
            current=Decimal("0"),
            captured_at=datetime.min,
        ),
        error="Navigation timeout",
    )
    assert result.error == "Navigation timeout"
