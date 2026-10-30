"""Read the public camera search UI in a short-lived, invisible browser session."""
from __future__ import annotations

import asyncio
import math
import time

from playwright.async_api import async_playwright

_lock = asyncio.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}


def _km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    dy = math.radians(b_lat - a_lat)
    dx = math.radians(b_lon - a_lon) * math.cos(math.radians((a_lat + b_lat) / 2))
    return 6371 * math.hypot(dx, dy)


def nearby(rows: list[dict], location: dict) -> list[dict]:
    """Text search also matches street names ("Colorado St" in Texas): keep only cameras in the
    place's state or within 150 km of it, nearest first."""
    state = (location.get("state") or "").casefold()
    lat, lon = location["lat"], location["lon"]
    rows = [r for r in rows if isinstance(r.get("lat"), (int, float)) and isinstance(r.get("lng"), (int, float))
            and ((state and (r.get("state") or "").casefold() == state) or _km(lat, lon, r["lat"], r["lng"]) < 150)]
    return sorted(rows, key=lambda r: _km(lat, lon, r["lat"], r["lng"]))


async def cameras(location: dict) -> list[dict]:
    """Cameras near a geocoded place: search its city, then its state if the city has none."""
    terms = [t for t in (location.get("city"), location.get("state"),
                         location.get("label", "").split(",")[0].strip()) if t]
    for term in dict.fromkeys(terms):
        rows = nearby(await _search(term), location)
        if rows:
            return rows
    return []


async def _search(place: str) -> list[dict]:
    query = place.strip()
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
