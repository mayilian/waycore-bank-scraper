"""LLM-powered DOM extraction via focused per-goal calls.

Each goal is a separate Claude call with a task-specific system prompt.
The DOM summary sent to the LLM is trimmed to ~2k tokens — not raw HTML.
Claude returns structured JSON; we validate with Pydantic.

This module is the intelligence layer. The execution layer (Playwright)
lives in stealth.py and the adapters. This module never touches the browser
directly — it receives a page, takes observations, and returns actions or data.
"""

import base64
import json
import re
from enum import StrEnum

from anthropic import AsyncAnthropic
from playwright.async_api import Page
from pydantic import BaseModel

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)
_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

_MODEL = "claude-sonnet-4-6"
_MAX_DOM_CHARS = 6_000  # ~2k tokens


class ActionType(StrEnum):
    CLICK = "click"
    FILL = "fill"
    NAVIGATE = "navigate"
    DONE = "done"
    FAILED = "failed"


class LLMAction(BaseModel):
    action: ActionType
    selector: str | None = None  # CSS selector for click/fill
    value: str | None = None  # text for fill, URL for navigate
    reason: str | None = None  # LLM's brief explanation


# ── DOM helpers ───────────────────────────────────────────────────────────────


async def _dom_summary(page: Page) -> str:
    """Extract a compact, token-efficient representation of the visible DOM."""
    raw: str = await page.evaluate("""() => {
        const els = document.querySelectorAll(
            'input, button, a, select, textarea, [role="button"], table, th, td, h1, h2, h3, label, form'
        );
        return Array.from(els).slice(0, 120).map(el => {
            const attrs = {};
            for (const a of el.attributes) attrs[a.name] = a.value;
            return {
                tag: el.tagName.toLowerCase(),
                text: (el.innerText || el.value || '').trim().slice(0, 80),
                attrs,
            };
        });
    }""")
    return raw[:_MAX_DOM_CHARS]


async def _screenshot_b64(page: Page) -> str:
    png = await page.screenshot(type="png")
    return base64.standard_b64encode(png).decode()


# ── Core inference ────────────────────────────────────────────────────────────


async def _ask(system: str, user_text: str, screenshot_b64: str | None) -> str:
    content: list = []
    if screenshot_b64:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": screenshot_b64},
            }
        )
    content.append({"type": "text", "text": user_text})

    msg = await _client.messages.create(
        model=_MODEL,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    return msg.content[0].text  # type: ignore[union-attr]


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
        return json.loads(raw)
    except json.JSONDecodeError:
        # Extract JSON from markdown code block if present
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
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
        # Default: if we see an OTP-related word, treat as otp_required
        if any(w in result.lower() for w in ("otp", "code", "verification", "one-time")):
            return "otp_required"
        return "logged_in"  # Optimistic default
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
        return json.loads(raw)["selector"]
    except (json.JSONDecodeError, KeyError):
        return "input[type='text'], input[type='number'], input[name*='otp'], input[name*='code']"


async def extract_accounts(page: Page) -> list[dict]:
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
            return json.loads(match.group())
        return []


async def extract_transactions_from_page(page: Page) -> list[dict]:
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
    raw = await _ask(system, user, screenshot)

    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else data.get("transactions", [])
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return []


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


async def extract_balance(page: Page) -> dict:
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
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"current": 0.0, "available": None, "currency": "USD"}
