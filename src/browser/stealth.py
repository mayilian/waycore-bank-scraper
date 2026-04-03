"""Stealth Playwright browser utilities.

Launches Chromium with bot-detection evasion:
  - playwright-stealth patches (navigator.webdriver, plugins, Chrome runtime)
  - Bezier curve mouse movement (defeats behavioral mouse tracking)
  - Per-keystroke random delays (defeats typing cadence analysis)
  - Configurable viewport, locale, timezone via BrowserPolicy
"""

import asyncio
import contextlib
import math
import random
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from playwright.async_api import Browser, BrowserContext, Page, StorageState, async_playwright
from playwright_stealth import Stealth

from src.core.config import settings

if TYPE_CHECKING:
    from src.adapters.base import BrowserPolicy


@asynccontextmanager
async def stealth_browser(
    storage_state: StorageState | None = None,
    policy: "BrowserPolicy | None" = None,
) -> AsyncGenerator[tuple[Browser, Page], None]:
    """Yield a (browser, page) pair configured for stealth operation.

    If storage_state is provided (dict with 'cookies' and 'origins'),
    the browser context is initialized with those cookies — useful for
    restoring a session across Restate workflow steps.

    If policy is provided, viewport/locale/timezone/UA come from it.
    Otherwise falls back to global settings defaults.
    """
    async with async_playwright() as pw:
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            # Memory optimizations for containerized environments (Fargate/Fly)
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-default-apps",
            "--disable-translate",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-zygote",
            # Cap renderer memory to prevent OOM on heavy SPAs
            "--js-flags=--max-old-space-size=256",
        ]
        if policy and policy.extra_args:
            launch_args.extend(policy.extra_args)

        browser = await pw.chromium.launch(
            headless=not settings.playwright_headful,
            args=launch_args,
        )

        # Per-bank overrides from BrowserPolicy, falling back to global settings
        vp_width = policy.viewport_width if policy else 1366
        vp_height = policy.viewport_height if policy else 768
        locale = policy.locale if policy else settings.browser_locale
        timezone_id = policy.timezone_id if policy else settings.browser_timezone
        user_agent = (
            policy.user_agent if policy and policy.user_agent else None
        ) or settings.browser_user_agent

        ctx_kwargs: dict[str, Any] = {
            "viewport": {"width": vp_width, "height": vp_height},
            "user_agent": user_agent,
            "locale": locale,
            "timezone_id": timezone_id,
        }
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state

        stealth = Stealth()
        context: BrowserContext = await browser.new_context(**ctx_kwargs)
        await stealth.apply_stealth_async(context)
        page = await context.new_page()

        try:
            yield browser, page
        finally:
            with contextlib.suppress(Exception):
                await context.close()
            with contextlib.suppress(Exception):
                await browser.close()


async def human_move_and_click(page: Page, selector: str) -> None:
    """Move the mouse to an element along a cubic Bezier curve, then click."""
    element = page.locator(selector)
    await element.wait_for(state="visible", timeout=10_000)
    box = await element.bounding_box()
    if not box:
        await element.click()
        return

    target_x = box["x"] + box["width"] / 2 + random.uniform(-5, 5)
    target_y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)

    current = await page.evaluate("() => ({x: window.mouseX || 0, y: window.mouseY || 0})")
    start_x = float(current.get("x", 0))
    start_y = float(current.get("y", 0))

    points = _bezier_points(start_x, start_y, target_x, target_y, steps=22)
    for x, y in points:
        await page.mouse.move(x, y)
        await asyncio.sleep(random.uniform(0.006, 0.014))

    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.click(target_x, target_y)


async def human_fill(page: Page, selector: str, text: str) -> None:
    """Click a field and type text with per-character random delays."""
    await human_move_and_click(page, selector)
    await asyncio.sleep(random.uniform(0.1, 0.3))
    for char in text:
        await page.keyboard.type(char)
        await asyncio.sleep(random.uniform(0.04, 0.12))


def _bezier_points(
    x0: float, y0: float, x3: float, y3: float, steps: int
) -> list[tuple[float, float]]:
    """Return `steps` points along a cubic Bezier curve from (x0,y0) to (x3,y3).

    Control points are randomised to produce natural-looking mouse paths.
    """
    cp1_x = x0 + (x3 - x0) * random.uniform(0.2, 0.4) + random.uniform(-30, 30)
    cp1_y = y0 + (y3 - y0) * random.uniform(0.1, 0.3) + random.uniform(-30, 30)
    cp2_x = x0 + (x3 - x0) * random.uniform(0.6, 0.8) + random.uniform(-30, 30)
    cp2_y = y0 + (y3 - y0) * random.uniform(0.7, 0.9) + random.uniform(-30, 30)

    result: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        inv = 1 - t
        x = inv**3 * x0 + 3 * inv**2 * t * cp1_x + 3 * inv * t**2 * cp2_x + t**3 * x3
        y = inv**3 * y0 + 3 * inv**2 * t * cp1_y + 3 * inv * t**2 * cp2_y + t**3 * y3
        result.append((math.floor(x), math.floor(y)))
    return result
