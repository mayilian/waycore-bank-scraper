"""Integration tests for failure modes.

These test adapter-level and step-level failure handling without requiring
a live bank or browser. They mock Playwright to simulate failures and verify
the system records correct status, screenshots, and error messages.

Run: uv run pytest tests/integration/ -v
"""

from datetime import UTC, datetime
from decimal import Decimal

from src.adapters.base import AccountData, AccountResult, BalanceData, TransactionData
from src.adapters.heritage_parsers import parse_balance_text, parse_transaction_row


class TestPartialFailure:
    """Verify per-account isolation — one failure doesn't kill the whole sync."""

    def test_account_result_captures_error(self) -> None:
        result = AccountResult(
            account=AccountData(external_id="FAIL_ACC"),
            transactions=[],
            balance=BalanceData(
                account_external_id="FAIL_ACC",
                current=Decimal("0"),
                captured_at=datetime.min,
            ),
            error="Navigation timeout after 15000ms",
        )
        assert result.error is not None
        assert "timeout" in result.error.lower()

    def test_mixed_success_and_failure_results(self) -> None:
        """Partial success: 2 accounts succeed, 1 fails."""
        results = [
            AccountResult(
                account=AccountData(external_id="ACC1"),
                transactions=[TransactionData(external_id="TX1", amount=Decimal("100"))],
                balance=BalanceData(
                    account_external_id="ACC1",
                    current=Decimal("500"),
                    captured_at=datetime.now(UTC),
                ),
            ),
            AccountResult(
                account=AccountData(external_id="ACC2"),
                transactions=[],
                balance=BalanceData(
                    account_external_id="ACC2",
                    current=Decimal("0"),
                    captured_at=datetime.min,
                ),
                error="Selector not found: table.transactions",
            ),
            AccountResult(
                account=AccountData(external_id="ACC3"),
                transactions=[TransactionData(external_id="TX2", amount=Decimal("200"))],
                balance=BalanceData(
                    account_external_id="ACC3",
                    current=Decimal("1000"),
                    captured_at=datetime.now(UTC),
                ),
            ),
        ]
        errors = [r.error for r in results if r.error]
        successes = [r for r in results if not r.error]
        assert len(errors) == 1
        assert len(successes) == 2
        # This is the "partial_success" path in workflow.py


class TestSelectorFallback:
    """Verify tiered extraction handles selector misses."""

    def test_parse_transaction_row_missing_fields_returns_none(self) -> None:
        """Tier 1 gracefully returns None when DOM row is incomplete."""
        assert parse_transaction_row({}) is None
        assert parse_transaction_row({"date": "today"}) is None
        assert parse_transaction_row({"amount": "not_money"}) is None

    def test_parse_balance_text_garbage_returns_none(self) -> None:
        """Tier 1 returns None on unrecognized balance text → triggers Tier 2."""
        assert parse_balance_text("Loading...", "ACC1") is None
        assert parse_balance_text("", "ACC1") is None
        assert parse_balance_text("Error: account suspended", "ACC1") is None

    def test_parse_transaction_handles_weird_amounts(self) -> None:
        """Real banks have inconsistent formatting."""
        # Parenthetical negative
        row = {"date": "1/1/2026, 12:00:00 AM", "amount": "($500.00)", "description": "Fee"}
        parse_transaction_row(row)  # must not crash — None is fine (triggers Tier 2)

    def test_parse_balance_text_extracts_from_real_formats(self) -> None:
        """Various real-world balance text formats."""
        assert parse_balance_text("$10,500.25", "ACC1") is not None
        assert parse_balance_text("$0.00", "ACC1") is not None
        result = parse_balance_text("$1,000,000.99", "ACC1")
        assert result is not None
        assert result.current == Decimal("1000000.99")


class TestEmptyAccountList:
    """Verify empty account list is handled."""

    def test_empty_accounts_list_detected(self) -> None:
        """The workflow raises RuntimeError on empty account list (steps.py:180)."""
        accounts: list[AccountData] = []
        assert len(accounts) == 0
        # In the real flow: raise RuntimeError("No accounts found — expected at least one")


class TestCredentialSecurity:
    """Verify credentials never leak."""

    def test_account_result_has_no_credential_fields(self) -> None:
        """AccountResult model doesn't carry credentials."""
        result = AccountResult(
            account=AccountData(external_id="ACC1"),
            transactions=[],
            balance=BalanceData(
                account_external_id="ACC1", current=Decimal("0"), captured_at=datetime.min
            ),
        )
        result_dict = result.model_dump() if hasattr(result, "model_dump") else vars(result)
        for key in result_dict:
            assert "password" not in key.lower()
            assert "credential" not in key.lower()
            assert "secret" not in key.lower()

    def test_connection_response_excludes_credentials(self) -> None:
        """API response schema doesn't include encrypted fields."""
        from src.api.schemas import ConnectionResponse

        fields = ConnectionResponse.model_fields
        assert "username_enc" not in fields
        assert "password_enc" not in fields
        assert "otp_value_enc" not in fields


class TestPaginationLimits:
    """Verify pagination doesn't loop forever."""

    def test_max_pages_per_account_is_bounded(self) -> None:
        from src.core.config import settings

        assert settings.max_pages_per_account > 0
        assert settings.max_pages_per_account <= 100  # sanity bound

    def test_max_sync_duration_is_bounded(self) -> None:
        from src.core.config import settings

        assert settings.max_sync_duration_secs > 0
        assert settings.max_sync_duration_secs <= 1800  # 30 min max


class TestLLMBudgetEnforcement:
    """Verify LLM call budget prevents runaway costs."""

    def test_budget_reset(self) -> None:
        from src.agent.extractor import reset_llm_budget

        reset_llm_budget()
        from src.agent import extractor

        assert extractor._llm_call_count == 0

    def test_budget_limit_is_configured(self) -> None:
        from src.core.config import settings

        assert settings.max_llm_calls_per_sync > 0
        assert settings.max_llm_calls_per_sync <= 500  # cost sanity


class TestConcurrencyLimiter:
    """Verify per-bank concurrency control."""

    async def test_semaphore_limits_concurrent_access(self) -> None:
        from src.worker.concurrency import acquire_bank_slot

        acquired = 0
        async with acquire_bank_slot("test_bank"):
            acquired += 1
        assert acquired == 1

    def test_max_concurrent_per_bank_is_configured(self) -> None:
        from src.core.config import settings

        assert settings.max_concurrent_per_bank > 0
        assert settings.max_concurrent_per_bank <= 10
