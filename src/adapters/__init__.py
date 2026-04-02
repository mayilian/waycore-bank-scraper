"""Adapter registry — maps bank_slug to BankAdapter class.

To add a new bank:
  1. Create src/adapters/<slug>.py implementing BankAdapter
  2. Add an entry here

Unknown slugs fall back to GenericBankAdapter (LLM-driven).
"""

from src.adapters.base import BankAdapter
from src.adapters.generic_bank_adapter import GenericBankAdapter
from src.adapters.heritage_bank_adapter import HeritageBankAdapter

ADAPTER_REGISTRY: dict[str, type[BankAdapter]] = {
    "heritage_bank": HeritageBankAdapter,
}


def get_adapter(bank_slug: str) -> BankAdapter:
    cls = ADAPTER_REGISTRY.get(bank_slug, GenericBankAdapter)
    return cls()
