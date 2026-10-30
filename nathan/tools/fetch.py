"""Fetch a web page and return readable text (scripts, styles and tags stripped).

Many modern pages (leaderboards, dashboards, single-page apps) arrive as an empty shell
that JavaScript fills in later. When a page looks like that, Nathan renders it in a
hidden Edge or Chrome (already on the computer) and reads what a person would see.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx2 as httpx

from nathan.tools.base import Tool, params

MAX_BYTES = 2_000_000
MAX_CHARS = 12000
UA = {"user-agent": "Nathan/1.0 (https://github.com/oscargradisar/Nathan) httpx2"}
RENDER_WAIT_MS = 9000  # how long the hidden browser lets the page's scripts run
_JS_SHELL = re.compile(r"enable javascript|requires javascript|you need to enable|loading\.\.\.|__next|id=\"root\"|id=\"app\"", re.IGNORECASE)


class PrivateAddress(PermissionError):
    """The agent asked for an address on this computer or the local network."""


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


async def check_public(host: str) -> None:
    """Raise PrivateAddress unless `host` is on the public internet.

    Why: the agent reads pages written by strangers. A page can tell it "now fetch
    http://127.0.0.1:8765/..." (Nathan's own API) or a router's admin page on the LAN, so
    the fetch tool only goes where any website could send you anyway."""
    host = host.strip("[]").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise PrivateAddress(f"{host} is this computer")
    try:
        if _blocked(ipaddress.ip_address(host)):
            raise PrivateAddress(f"{host} is a private or local address")
        return
    except ValueError:
        pass  # a name, not an address: look it up
    if host.endswith((".test", ".example", ".invalid")):
        return  # reserved names that never resolve publicly (used by the tests)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None)
    except OSError:
        return  # doesn't resolve: the request itself will fail, nothing to protect
    for info in infos:
        if _blocked(ipaddress.ip_address(info[4][0].split("%")[0])):
            raise PrivateAddress(f"{host} points to a private or local address")


async def _guard(request: httpx.Request) -> None:  # every hop, redirects included
    await check_public(request.url.host)


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|h\d|li|tr)>", "\n", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)  # keep table cells apart: leaderboards are tables
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def browser() -> str | None:
    """A Chromium browser that can render headlessly: Edge ships with Windows."""
    pf = [os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles"), os.getenv("LOCALAPPDATA")]
    for base in filter(None, pf):
        for rel in ("Microsoft/Edge/Application/msedge.exe", "Google/Chrome/Application/chrome.exe"):
            if (Path(base) / rel).exists():
                return str(Path(base) / rel)
    for mac in ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        if Path(mac).exists():
            return mac
    return shutil.which("msedge") or shutil.which("google-chrome") or shutil.which("chromium")


def _render_sync(url: str) -> str:
    exe = browser()
    if not exe:
        return ""
    with tempfile.TemporaryDirectory() as profile:
        out = subprocess.run(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio",
             f"--user-data-dir={profile}", f"--virtual-time-budget={RENDER_WAIT_MS}", "--dump-dom", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45, check=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    return out.stdout


async def render(url: str) -> str:
    """The page's HTML after its JavaScript ran ("" if no browser or it failed)."""
    try:
        return await asyncio.to_thread(_render_sync, url)
    except (OSError, subprocess.TimeoutExpired):
        return ""


def needs_browser(raw_html: str, text: str) -> bool:
    return len(text) < 600 or (bool(_JS_SHELL.search(raw_html[:20000])) and len(text) < 3000)


def links(raw_html: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, link text) for every link on the page."""
    from urllib.parse import urljoin

    out = []
    for href, label in re.findall(r'(?is)<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', raw_html):
        out.append((urljoin(base, html.unescape(href)), html_to_text(label)[:80]))
    return out


async def read_page_html(url: str, http: httpx.AsyncClient | None = None) -> tuple[int, str, str]:
    """(status, readable text, final html) — the html is the rendered one when rendering helped."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA)
    try:
        # Streamed, so a huge file stops at MAX_BYTES instead of being downloaded in full
        async with http.stream("GET", url) as resp:
            data = bytearray()
            async for chunk in resp.aiter_bytes():
                data += chunk
                if len(data) >= MAX_BYTES:
                    break
            body = bytes(data[:MAX_BYTES]).decode(resp.encoding or "utf-8", errors="replace")
            status = resp.status_code
    except httpx.HTTPError:
        body, status = "", 0
    finally:
        if own:
            await http.aclose()
    is_html = body.lstrip()[:1] == "<" or not body
    text = html_to_text(body) if is_html else body
    if is_html and needs_browser(body, text):
        rendered = await render(url)
        if rendered and len(html_to_text(rendered)) > len(text):
            body, text, status = rendered, html_to_text(rendered), status or 200
    return status, text, body


async def read_page(url: str, http: httpx.AsyncClient | None = None) -> tuple[int, str]:
    """(status, readable text) of a page, rendering it in a hidden browser when needed."""
    status, text, _ = await read_page_html(url, http)
    return status, text


async def fetch_url(url: str, client: httpx.AsyncClient | None = None) -> str:
    if not re.match(r"^https?://", url):
        return "ERROR: only http(s) URLs are allowed"
    own = client is None
    client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers=UA)
    if _guard not in client.event_hooks["request"]:
        client.event_hooks["request"] = [*client.event_hooks["request"], _guard]
    try:
        # shortcut: the hidden-browser fallback (render) follows its own redirects unchecked;
        # route it through a proxy that applies check_public if pages start abusing that
        await check_public(httpx.URL(url).host)
        status, text = await read_page(url, client)
    except PrivateAddress as e:
        return f"ERROR: not allowed: {e}. The fetch tool only reads public web pages."
    finally:
        if own:
            await client.aclose()
    if not text:
        return f"ERROR: couldn't read {url}"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…[truncated]"
    return f"HTTP {status} {url}\n\n{text}"


def make_tool() -> Tool:
    return Tool("fetch_url", "Read a web page (including pages built by JavaScript, like live "
                "leaderboards) and return its text.", params(url=("string", "http(s) URL")), fetch_url)
