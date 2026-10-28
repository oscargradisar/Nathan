"""Fetch a web page and return readable text (scripts, styles and tags stripped)."""

from __future__ import annotations

import html
import re

import httpx2 as httpx

from nathan.tools.base import Tool, params

MAX_BYTES = 2_000_000
MAX_CHARS = 12000


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


async def fetch_url(url: str, client: httpx.AsyncClient | None = None) -> str:
    if not re.match(r"^https?://", url):
        return "ERROR: only http(s) URLs are allowed"
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True,
                                         headers={"user-agent": "Nathan/0.1 (+personal research)"})
    try:
        resp = await client.get(url)
        body = resp.content[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
    except httpx.HTTPError as e:
        return f"ERROR: {type(e).__name__}: {e}"
    finally:
        if own:
            await client.aclose()
    ctype = resp.headers.get("content-type", "")
    text = html_to_text(body) if "html" in ctype or body.lstrip().startswith("<") else body
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…[truncated]"
    return f"HTTP {resp.status_code} {url}\n\n{text}"


def make_tool() -> Tool:
    return Tool("fetch_url", "Download a web page and return its readable text.",
                params(url=("string", "http(s) URL")), fetch_url)
