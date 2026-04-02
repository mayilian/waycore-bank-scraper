"""Tests for Pydantic data models."""

from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.base import AccountData, BalanceData, TransactionData


def test_account_data_defaults() -> None:
    acc = AccountData(external_id="ACC001")
    assert acc.currency == "USD"
    assert acc.name is None
    assert acc.account_type is None


def test_transaction_data_decimal_precision() -> None:
    txn = TransactionData(
        external_id="TXN001",
        amount=Decimal("1234.5678"),
        currency="USD",
    )
    assert txn.amount == Decimal("1234.5678")
    assert isinstance(txn.amount, Decimal)


def test_balance_data_decimal() -> None:
    bal = BalanceData(
        account_external_id="ACC001",
        current=Decimal("10000.50"),
        available=Decimal("9500.25"),
        captured_at=datetime.now(UTC),
    )
    assert isinstance(bal.current, Decimal)
    assert isinstance(bal.available, Decimal)


def test_transaction_data_optional_fields() -> None:
    txn = TransactionData(
        external_id="TXN002",
        amount=Decimal("-50.00"),
    )
    assert txn.posted_at is None
    assert txn.description is None
    assert txn.running_balance is None
    assert txn.raw is None
    assert txn.currency == "USD"
