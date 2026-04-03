"""Tests for failure handling, parser robustness, and safety bounds.

These verify data models, parser edge cases, config guards, and API-level
failure contracts. They do NOT exercise the Restate workflow or browser —
those paths are verified by end-to-end sync tests against the demo bank.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from src.bank_adapters.base import AccountData, AccountResult, BalanceData, TransactionData
from src.bank_adapters.heritage_bank.parsers import parse_balance_text, parse_transaction_row

# ── Partial failure model ────────────────────────────────────────────────────


class TestPartialFailureModel:
    """AccountResult captures per-account errors so partial success works."""

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


# ── Parser robustness (Tier 1 edge cases) ────────────────────────────────────


class TestParserRobustness:
    """Tier 1 parsers return None on bad input instead of crashing."""

    def test_parse_transaction_row_missing_fields_returns_none(self) -> None:
        assert parse_transaction_row({}) is None
        assert parse_transaction_row({"date": "today"}) is None
        assert parse_transaction_row({"amount": "not_money"}) is None

    def test_parse_balance_text_garbage_returns_none(self) -> None:
        assert parse_balance_text("Loading...", "ACC1") is None
        assert parse_balance_text("", "ACC1") is None
        assert parse_balance_text("Error: account suspended", "ACC1") is None

    def test_parse_transaction_handles_weird_amounts(self) -> None:
        row = {"date": "1/1/2026, 12:00:00 AM", "amount": "($500.00)", "description": "Fee"}
        parse_transaction_row(row)  # must not crash

    def test_parse_balance_text_extracts_from_real_formats(self) -> None:
        assert parse_balance_text("$10,500.25", "ACC1") is not None
        assert parse_balance_text("$0.00", "ACC1") is not None
        result = parse_balance_text("$1,000,000.99", "ACC1")
        assert result is not None
        assert result.current == Decimal("1000000.99")


# ── Credential safety ────────────────────────────────────────────────────────


class TestCredentialSafety:
    """Credentials never appear in data models or API responses."""

    def test_account_result_has_no_credential_fields(self) -> None:
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
        from src.api.schemas import ConnectionResponse

        fields = ConnectionResponse.model_fields
        assert "username_enc" not in fields
        assert "password_enc" not in fields
        assert "otp_value_enc" not in fields


# ── Config safety bounds ─────────────────────────────────────────────────────


class TestConfigBounds:
    """Config values are bounded to prevent runaway syncs and costs."""

    def test_max_pages_per_account_is_bounded(self) -> None:
        from src.core.config import settings

        assert 0 < settings.max_pages_per_account <= 100

    def test_max_sync_duration_is_bounded(self) -> None:
        from src.core.config import settings

        assert 0 < settings.max_sync_duration_secs <= 1800

    def test_max_llm_calls_is_bounded(self) -> None:
        from src.core.config import settings

        assert 0 < settings.max_llm_calls_per_sync <= 500

    def test_max_concurrent_per_bank_is_bounded(self) -> None:
        from src.core.config import settings

        assert 0 < settings.max_concurrent_per_bank <= 10


# ── LLM budget ───────────────────────────────────────────────────────────────


class TestLLMBudget:
    """LLM call budget resets per sync and has a configured limit."""

    def test_budget_reset(self) -> None:
        from src.agent import extractor
        from src.agent.extractor import reset_llm_budget

        reset_llm_budget()
        assert extractor._llm_call_count.get() == 0


# ── Concurrency limiter ──────────────────────────────────────────────────────


class TestConcurrencyLimiter:
    """Process-local semaphore acquires and releases correctly."""

    async def test_acquire_and_release(self) -> None:
        from src.worker.concurrency import acquire_sync_slot

        async with acquire_sync_slot("test_bank"):
            pass  # should not hang or raise

    async def test_per_bank_isolation(self) -> None:
        """Different banks get independent semaphores."""
        from src.worker.concurrency import _bank_semaphores

        _ = _bank_semaphores["bank_a"]
        _ = _bank_semaphores["bank_b"]
        assert _bank_semaphores["bank_a"] is not _bank_semaphores["bank_b"]


# ── Trigger sync failure ─────────────────────────────────────────────────────


class TestTriggerSyncFailure:
    """When Restate HTTP trigger fails, the job is marked failed (not orphaned)."""

    async def test_trigger_failure_marks_job_failed(self) -> None:
        """Mock the Restate HTTP call to fail, verify job gets marked failed."""
        from src.services.operations import trigger_sync

        mock_response = AsyncMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("src.services.operations.httpx.AsyncClient", return_value=mock_client):
            with patch("src.services.operations.get_session") as mock_session:
                # First call: create job. Second call: mark failed.
                mock_db = AsyncMock()
                mock_job = AsyncMock()
                mock_job.status = "pending"
                mock_db.get = AsyncMock(return_value=mock_job)
                mock_db.__aenter__ = AsyncMock(return_value=mock_db)
                mock_db.__aexit__ = AsyncMock(return_value=False)
                mock_session.return_value = mock_db

                try:
                    await trigger_sync("fake-conn-id", "static")
                    raise AssertionError("Should have raised")
                except RuntimeError:
                    pass

                # Verify the job was marked failed
                assert mock_job.status == "failed"
                assert mock_job.failure_reason == "Failed to trigger workflow"
