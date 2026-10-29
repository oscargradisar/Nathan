"""Read the public camera search UI in a short-lived, invisible browser session."""
from __future__ import annotations

import asyncio
import time

from playwright.async_api import async_playwright

_lock = asyncio.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}


async def cameras(place: str) -> list[dict]:
    query = place.split(",")[0].strip()
    async with _lock:
        hit = _cache.get(query.casefold())
        if hit and time.monotonic() - hit[0] < 300:
            return hit[1]
        async with async_playwright() as driver:
            # Uses installed Edge; no downloaded browser, credentials, or copied API tokens.
            browser = await driver.chromium.launch(channel="msedge", headless=True)
            try:
                page = await browser.new_page()
                async def lightweight(route):
                    request = route.request
                    if request.resource_type in ("image", "media", "font") or any(
                        path in request.url for path in (
                            "/api/cameras/markers", "/api/cameras/marker-names",
                            "/api/cameras/feed-pool", "/api/cameras/batch", "/api/feed/")):
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/*", lightweight)
                await page.goto("https://opencctv.org/", wait_until="domcontentloaded", timeout=30000)
                await page.get_by_role("button", name="List", exact=True).click()
                await page.get_by_placeholder("Search cameras...").fill(query)
                async with page.expect_response(
                    lambda r: "/api/cameras/list?" in r.url and "&q=" in r.url,
                    timeout=20000,
                ) as response:
                    await page.get_by_role("button", name="Search", exact=True).click()
                result = await (await response.value).json()
                rows = result.get("cameras", [])[:50]
            finally:
                await browser.close()
        if len(_cache) >= 4:
            _cache.pop(next(iter(_cache)))
        _cache[query.casefold()] = (time.monotonic(), rows)
        return rows
