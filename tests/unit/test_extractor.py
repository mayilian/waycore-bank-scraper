"""Tests for extractor module — unit tests that don't require LLM calls."""

from src.agent.extractor import ActionType, LLMAction


def test_action_type_values() -> None:
    assert ActionType.CLICK.value == "click"
    assert ActionType.DONE.value == "done"


def test_llm_action_defaults() -> None:
    action = LLMAction(action=ActionType.DONE)
    assert action.selector is None


def test_llm_action_with_selector() -> None:
    action = LLMAction(action=ActionType.CLICK, selector="#next-page")
    assert action.action == "click"
    assert action.selector == "#next-page"
