"""Fallback when the Helper extension isn't installed: watch the clipboard.

You click "Create key" and "Copy" on the provider's page yourself; Nathan notices a
string that looks like that provider's key appear on the clipboard, takes it, and then
clears the clipboard so the key doesn't linger there.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable

WATCH_S = 180.0


def _default_paste() -> str:
    import pyperclip

    return pyperclip.paste() or ""


def _default_copy(text: str) -> None:
    import pyperclip

    pyperclip.copy(text)


def clipboard_available() -> bool:
    try:
        _default_paste()
        return True
    except Exception:  # noqa: BLE001 - no clipboard tool installed (e.g. headless Linux)
        return False


async def watch_clipboard(pattern: str, *, timeout: float = WATCH_S, interval: float = 0.5,
                          paste: Callable[[], str] = _default_paste,
                          copy: Callable[[str], None] = _default_copy) -> str | None:
    """Return the first new clipboard text matching `pattern`, or None after timeout."""
    regex = re.compile(pattern)
    try:
        before = paste()
    except Exception:  # noqa: BLE001
        return None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        await asyncio.sleep(interval)
        try:
            text = paste().strip()
        except Exception:  # noqa: BLE001
            return None
        if text and text != before:
            if regex.fullmatch(text):
                with contextlib.suppress(Exception):
                    copy("")
                return text
            before = text
    return None
