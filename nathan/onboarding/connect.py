"""`nathan connect`: drive one provider (or all) through the best available method.

Order of attempts for extension providers: the Helper extension and the clipboard watcher
run at the same time (whichever gets the key first wins), then a hidden manual-paste
prompt as the last resort.
"""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Callable

from nathan.onboarding import oauth_openrouter
from nathan.onboarding.clipboard import clipboard_available, watch_clipboard
from nathan.onboarding.server import Hub

ORDER = ["openrouter", "groq", "gemini", "cerebras"]


async def connect_oauth(hub: Hub, provider: str, *, open_browser: Callable[[str], object],
                        timeout: float = 300, http=None) -> tuple[bool, str]:
    import secrets

    verifier, challenge = oauth_openrouter.make_pkce()
    state = secrets.token_urlsafe(16)
    callback = f"http://127.0.0.1:{hub.port}/callback?state={state}"
    fut = hub.expect_oauth(state)
    hub.status[provider].state = "waiting"
    open_browser(oauth_openrouter.auth_url(callback, challenge))
    try:
        code = await asyncio.wait_for(fut, timeout)
    except TimeoutError:
        return False, "timed out waiting for OpenRouter sign-in"
    try:
        key = await oauth_openrouter.exchange_code(code, verifier, http)
    except oauth_openrouter.ExchangeError as e:
        return False, str(e)
    status, msg = await hub.submit(provider, key, None, require_token=False)
    return status == 200, msg


async def connect_extension(hub: Hub, provider: str, *, open_browser: Callable[[str], object],
                            timeout: float = 300, use_clipboard: bool | None = None,
                            ask_paste: Callable[[str], str | None] | None = None,
                            clipboard_kwargs: dict | None = None) -> tuple[bool, str]:
    p = hub.providers[provider]
    url = hub.start(provider)
    open_browser(url)
    waiter = asyncio.create_task(hub.wait_for(provider, timeout))
    tasks = {waiter}
    if use_clipboard is None:
        use_clipboard = clipboard_available()
    clip = None
    if use_clipboard and p.onboarding and p.onboarding.key_pattern:
        clip = asyncio.create_task(watch_clipboard(p.onboarding.key_pattern, timeout=timeout,
                                                   **(clipboard_kwargs or {})))
        tasks.add(clip)
    key_from_clipboard = None
    while tasks:
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if waiter in done and waiter.result():
            break
        if clip is not None and clip in done and clip.result():
            key_from_clipboard = clip.result()
            break
    for t in tasks:
        t.cancel()
    if hub.status[provider].state == "connected":
        return True, hub.status[provider].detail
    if key_from_clipboard:
        status, msg = await hub.submit(provider, key_from_clipboard, None, require_token=False)
        return status == 200, msg
    if ask_paste is not None:
        typed = ask_paste(provider)
        if typed:
            status, msg = await hub.submit(provider, typed, None, require_token=False)
            return status == 200, msg
    return False, "no key received"


async def connect(hub: Hub, provider: str, *, open_browser: Callable[[str], object] = webbrowser.open,
                  timeout: float = 300, ask_paste: Callable[[str], str | None] | None = None,
                  **kw) -> tuple[bool, str]:
    p = hub.providers[provider]
    method = p.onboarding.method if p.onboarding else "manual"
    if method == "oauth_pkce":
        ok, msg = await connect_oauth(hub, provider, open_browser=open_browser, timeout=timeout,
                                      http=kw.get("http"))
        if ok or ask_paste is None:
            return ok, msg
        typed = ask_paste(provider)
        if typed:
            status, msg = await hub.submit(provider, typed, None, require_token=False)
            return status == 200, msg
        return False, msg
    if method == "extension":
        return await connect_extension(hub, provider, open_browser=open_browser, timeout=timeout,
                                       ask_paste=ask_paste, **{k: v for k, v in kw.items() if k != "http"})
    page = (p.onboarding.key_page if p.onboarding else "") or p.signup
    if page:
        open_browser(page)
    typed = ask_paste(provider) if ask_paste else None
    if not typed:
        return False, "no key entered"
    status, msg = await hub.submit(provider, typed, None, require_token=False)
    return status == 200, msg


def connect_order(providers: dict) -> list[str]:
    known = [p for p in ORDER if p in providers]
    return known + [p for p in providers if p not in known]
