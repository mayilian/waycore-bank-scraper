"""LLM-powered DOM extraction via focused per-goal calls.

Each goal is a separate Claude call with a task-specific system prompt.
The DOM summary sent to the LLM is trimmed to ~4k tokens — not raw HTML.
Claude returns structured JSON; we validate with Pydantic.

This module is the intelligence layer. The execution layer (Playwright)
lives in stealth.py and the adapters. This module never touches the browser
directly — it receives a page, takes observations, and returns actions or data.
"""

import base64
import json
import re
from enum import StrEnum
from typing import Any

from anthropic import AsyncAnthropic
from playwright.async_api import Page
from pydantic import BaseModel

from src.core.logging import get_logger

log = get_logger(__name__)
_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        from src.core.config import settings

        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


_MODEL = "claude-sonnet-4-6"
_MAX_DOM_CHARS = 12_000  # ~4k tokens — allows larger transaction tables


class ActionType(StrEnum):
    CLICK = "click"
    DONE = "done"


class LLMAction(BaseModel):
    action: ActionType
    selector: str | None = None


# ── DOM helpers ───────────────────────────────────────────────────────────────


async def _dom_summary(page: Page) -> str:
    """Extract a compact, token-efficient representation of the visible DOM."""
    raw: Any = await page.evaluate("""() => {
        const els = document.querySelectorAll(
            'input, button, a, select, textarea, [role="button"], table, th, td, h1, h2, h3, label, form'
        );
        return Array.from(els).slice(0, 250).map(el => {
            const attrs = {};
            for (const a of el.attributes) attrs[a.name] = a.value;
            return {
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || '').trim().slice(0, 80),
                attrs,
            };
        });
    }""")
    # page.evaluate returns a list of dicts; serialize to JSON for the LLM
    return json.dumps(raw)[:_MAX_DOM_CHARS]


async def _screenshot_b64(page: Page) -> str:
    png = await page.screenshot(type="png")
    return base64.standard_b64encode(png).decode()


# ── Core inference ────────────────────────────────────────────────────────────


async def _ask(
    system: str, user_text: str, screenshot_b64: str | None, max_tokens: int = 1024
) -> str:
    content: list[Any] = []
    if screenshot_b64:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
            }
        )
    content.append({"type": "text", "text": user_text})

    client = _get_client()
    msg = await client.messages.create(
        model=_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    if not msg.content or not hasattr(msg.content[0], "text"):
        raise ValueError("LLM returned empty or non-text response")
    return msg.content[0].text


# ── Per-goal extraction functions ─────────────────────────────────────────────


async def find_login_fields(page: Page) -> dict[str, str]:
    """Return {username_selector, password_selector, submit_selector}."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a web automation assistant. Given a banking login page, "
        "identify CSS selectors for the username field, password field, and submit button. "
        "Return ONLY valid JSON with keys: username_selector, password_selector, submit_selector. "
        "Prefer id-based selectors (e.g. #username) over generic ones."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot)

    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        # Extract JSON from markdown code block if present
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())  # type: ignore[no-any-return]
        raise ValueError(f"Could not parse login fields from LLM response: {raw}")


async def detect_post_login_state(page: Page) -> str:
    """Return one of: 'logged_in', 'otp_required', 'login_failed'."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a web automation assistant analyzing a banking portal page. "
        "Determine the current state after a login attempt. "
        "Return ONLY one of these exact strings: logged_in, otp_required, login_failed"
    )
    user = f"DOM summary:\n{dom}\n\nCurrent URL: {page.url}"
    result = await _ask(system, user, screenshot)
    state = result.strip().lower()

    if state not in ("logged_in", "otp_required", "login_failed"):
        log.warning("llm.unexpected_state", raw=result)
        if any(w in result.lower() for w in ("otp", "code", "verification", "one-time")):
            return "otp_required"
        # Fail explicitly rather than optimistically assuming success —
        # scraping an unauthenticated page produces garbage data.
        return "login_failed"
    return state


async def find_otp_field(page: Page) -> str:
    """Return CSS selector for the OTP input field."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a web automation assistant. Find the OTP / verification code input field. "
        "Return ONLY a JSON object with key: selector (CSS selector string)."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot)

    try:
        return json.loads(raw)["selector"]  # type: ignore[no-any-return]
    except (json.JSONDecodeError, KeyError):
        return "input[type='text'], input[type='number'], input[name*='otp'], input[name*='code']"


async def extract_accounts(page: Page) -> list[dict[str, Any]]:
    """Return list of {external_id, name, account_type, currency} dicts."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a financial data extraction assistant. "
        "Extract all bank accounts visible on this page. "
        "Return ONLY a JSON array of objects, each with: "
        "external_id (account number or unique ID), name, account_type "
        "(checking/savings/credit/other), currency (3-letter code, default USD). "
        "If you cannot determine a field, use null."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot)

    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("accounts", [])
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            return json.loads(match.group())  # type: ignore[no-any-return]
        return []


async def extract_transactions_from_page(page: Page) -> list[dict[str, Any]]:
    """Extract all transactions visible in the current page/view."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a financial data extraction assistant. "
        "Extract all transactions visible on this banking page. "
        "Return ONLY a JSON array of objects, each with: "
        "external_id (transaction ID or a hash of date+desc+amount), "
        "posted_at (ISO 8601 datetime or null), "
        "description (merchant/payee name), "
        "amount (float, negative for debits/withdrawals), "
        "currency (3-letter code, default USD), "
        "running_balance (float or null). "
        "Include ALL rows in the table, do not truncate."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot, max_tokens=8192)

    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("transactions", [])
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            return json.loads(match.group())  # type: ignore[no-any-return]
        return []


async def find_account_link(page: Page, account_external_id: str) -> LLMAction:
    """Return a click action targeting the link/button for a specific account."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a web automation assistant on a banking dashboard. "
        "Find the clickable link, row, or button that navigates to the detail page "
        f"for account '{account_external_id}'. "
        "Return ONLY JSON with keys: "
        "action ('click' if found, 'done' if the page already shows this account's details), "
        "selector (CSS selector for the element, or null)."
    )
    user = f"DOM summary:\n{dom}\n\nTarget account: {account_external_id}"
    raw = await _ask(system, user, screenshot)

    try:
        data = json.loads(raw)
        return LLMAction(
            action=ActionType(data.get("action", "done")),
            selector=data.get("selector"),
        )
    except (json.JSONDecodeError, ValueError):
        return LLMAction(action=ActionType.DONE)


async def check_has_next_page(page: Page) -> LLMAction:
    """Return a click action for the next page button, or DONE if no more pages."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a web automation assistant on a bank transaction history page. "
        "Determine if there is a 'next page', 'load more', or pagination control. "
        "Return ONLY JSON with keys: "
        "action ('click' if next page exists, 'done' if no more pages), "
        "selector (CSS selector for the next-page control, or null)."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot)

    try:
        data = json.loads(raw)
        return LLMAction(
            action=ActionType(data.get("action", "done")),
            selector=data.get("selector"),
        )
    except (json.JSONDecodeError, ValueError):
        return LLMAction(action=ActionType.DONE)


async def extract_balance(page: Page) -> dict[str, Any]:
    """Return {current, available, currency} for the current account view."""
    dom = await _dom_summary(page)
    screenshot = await _screenshot_b64(page)

    system = (
        "You are a financial data extraction assistant. "
        "Extract the account balance from this banking page. "
        "Return ONLY JSON with keys: current (float), available (float or null), "
        "currency (3-letter code, default USD)."
    )
    user = f"DOM summary:\n{dom}"
    raw = await _ask(system, user, screenshot)

    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())  # type: ignore[no-any-return]
        return {"current": 0.0, "available": None, "currency": "USD"}
