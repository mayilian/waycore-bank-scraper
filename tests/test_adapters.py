"""Tests for adapter registry and transaction parsing."""

from decimal import Decimal

from src.adapters import get_adapter
from src.adapters.generic_bank_adapter import GenericBankAdapter
from src.adapters.heritage_bank_adapter import HeritageBankAdapter


def test_heritage_adapter_registered() -> None:
    adapter = get_adapter("heritage_bank")
    assert isinstance(adapter, HeritageBankAdapter)


def test_unknown_slug_falls_back_to_generic() -> None:
    adapter = get_adapter("some_unknown_bank")
    assert isinstance(adapter, GenericBankAdapter)


def test_heritage_parse_transaction_valid() -> None:
    adapter = HeritageBankAdapter()
    raw = {
        "external_id": "TXN001",
        "posted_at": "2026-01-15T00:00:00",
        "description": "Coffee Shop",
        "amount": "-4.50",
        "currency": "USD",
        "running_balance": "995.50",
    }
    txn = adapter._parse_transaction(raw)
    assert txn is not None
    assert txn.external_id == "TXN001"
    assert txn.amount == Decimal("-4.50")
    assert txn.running_balance == Decimal("995.50")
    assert txn.description == "Coffee Shop"


def test_heritage_parse_transaction_missing_external_id() -> None:
    adapter = HeritageBankAdapter()
    txn = adapter._parse_transaction({"amount": "10.00"})
    assert txn is None


def test_heritage_parse_transaction_malformed_amount() -> None:
    adapter = HeritageBankAdapter()
    txn = adapter._parse_transaction({"external_id": "TXN", "amount": "not_a_number"})
    assert txn is None


def test_heritage_parse_transaction_dollar_sign_and_commas() -> None:
    adapter = HeritageBankAdapter()
    raw = {"external_id": "TXN", "amount": "$1,234.56"}
    txn = adapter._parse_transaction(raw)
    assert txn is not None
    assert txn.amount == Decimal("1234.56")


def test_heritage_parse_transaction_zero_running_balance() -> None:
    """Verify running_balance=0 is preserved (not treated as None via truthiness)."""
    adapter = HeritageBankAdapter()
    raw = {"external_id": "TXN", "amount": "100", "running_balance": "0"}
    txn = adapter._parse_transaction(raw)
    assert txn is not None
    assert txn.running_balance == Decimal("0")
