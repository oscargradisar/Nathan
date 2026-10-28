"""The localhost onboarding server: where keys arrive.

Endpoints (127.0.0.1 only):
  GET  /onboarding.json     page hints for the Helper extension (from providers.yaml)
  POST /keys                {provider, key, token} from the extension → test → save
  GET  /callback            OpenRouter OAuth redirect lands here with ?code=…&state=…
  GET  /connect/status      which providers are connected (for the wizard)

The `Hub` holds the state and logic so the CLI, the web UI (Tier 5) and tests share it.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Self

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from nathan.config import Provider
from nathan.keys import mask, set_key
from nathan.onboarding.keytest import check_key
from nathan.onboarding.tokens import TokenStore

Checker = Callable[[Provider, str], Awaitable[tuple[bool, str]]]
Saver = Callable[[Provider, str], str]


def default_saver(p: Provider, key: str) -> str:
    return set_key(p.name, p.key_env, key)


@dataclass
class Status:
    state: str = "not connected"  # not connected | waiting | connected | failed
    detail: str = ""


@dataclass
class Hub:
    providers: dict[str, Provider]
    tokens: TokenStore = field(default_factory=TokenStore)
    checker: Checker = check_key
    saver: Saver = default_saver
    port: int = 0
    status: dict[str, Status] = field(default_factory=dict)
    _waiters: dict[str, asyncio.Future] = field(default_factory=dict)
    _oauth: dict[str, asyncio.Future] = field(default_factory=dict)  # state → future(code)
    loop: asyncio.AbstractEventLoop | None = None

    def __post_init__(self) -> None:
        for name, p in self.providers.items():
            self.status.setdefault(name, Status("connected" if p.api_key else "not connected"))

    # ---- extension / clipboard / manual path -------------------------------------------

    def start(self, provider: str) -> str:
        """Begin a connection: issue a token and return the URL to open."""
        p = self.providers[provider]
        token = self.tokens.issue(provider)
        self.status[provider] = Status("waiting", "waiting for you in the browser")
        loop = self.loop or asyncio.get_running_loop()
        old = self._waiters.get(provider)
        if old is None or old.done():
            self._waiters[provider] = loop.create_future()
        page = p.onboarding.key_page if p.onboarding else (p.signup or "")
        return f"{page}#nathan={token}&port={self.port}&provider={provider}"

    async def submit(self, provider: str, key: str, token: str | None,
                     require_token: bool = True) -> tuple[int, str]:
        """Validate and save a key. Returns (http_status, message)."""
        p = self.providers.get(provider)
        if p is None:
            return 404, "unknown provider"
        if require_token and not self.tokens.valid(token, provider):
            return 403, "missing, wrong or expired token — press Connect again in Nathan"
        key = key.strip()
        if p.onboarding and p.onboarding.key_pattern and not p.onboarding.matches(key):
            return 422, f"that doesn't look like a {provider} key"
        ok, msg = await self.checker(p, key)
        if not ok:
            self.status[provider] = Status("failed", msg)
            return 400, msg
        if token:
            self.tokens.consume(token)
        where = self.saver(p, key)
        self.status[provider] = Status("connected", f"{mask(key)} saved to {where}")
        fut = self._waiters.get(provider)
        if fut is not None and not fut.done():
            fut.set_result(key)
        return 200, f"connected ({where})"

    async def wait_for(self, provider: str, timeout: float) -> str | None:
        fut = self._waiters.get(provider)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except TimeoutError:
            return None

    # ---- OAuth path --------------------------------------------------------------------

    def expect_oauth(self, state: str) -> asyncio.Future:
        loop = self.loop or asyncio.get_running_loop()
        fut = loop.create_future()
        self._oauth[state] = fut
        return fut

    def oauth_callback(self, state: str, code: str) -> bool:
        fut = self._oauth.pop(state, None)
        if fut is None or fut.done():
            return False
        if self.loop is not None and self.loop.is_running():
            self.loop.call_soon_threadsafe(fut.set_result, code)
        else:
            fut.set_result(code)
        return True

    def onboarding_json(self) -> dict:
        return {name: p.onboarding.model_dump() for name, p in self.providers.items()
                if p.onboarding and p.onboarding.method == "extension"}


class KeyIn(BaseModel):
    provider: str
    key: str
    token: str


def make_router(hub: Hub) -> APIRouter:
    router = APIRouter()

    @router.get("/onboarding.json")
    async def onboarding_json() -> dict:
        return hub.onboarding_json()

    @router.post("/keys")
    async def post_key(body: KeyIn) -> dict:
        code, msg = await _on_hub_loop(hub, hub.submit(body.provider, body.key, body.token))
        if code != 200:
            raise HTTPException(code, msg)
        return {"ok": True, "close": True, "message": msg}

    @router.get("/callback", response_class=HTMLResponse)
    async def callback(request: Request) -> str:
        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        if not code or not hub.oauth_callback(state, code):
            return _page("Nathan", "This sign-in link is invalid or expired. Press Connect again.")
        return _page("Nathan", "Connected. You can close this tab and go back to Nathan.")

    @router.get("/connect/status")
    async def status() -> dict:
        return {name: {"state": s.state, "detail": s.detail} for name, s in hub.status.items()}

    return router


async def _on_hub_loop(hub: Hub, coro):
    """Run on the hub's loop when the server lives in another thread (CLI mode)."""
    current = asyncio.get_running_loop()
    if hub.loop is None or hub.loop is current:
        return await coro
    return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coro, hub.loop))


def _page(title: str, text: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<body style='font-family:system-ui;padding:3rem'><h2>{title}</h2><p>{text}</p>")


def create_app(hub: Hub) -> FastAPI:
    app = FastAPI(title="Nathan onboarding", docs_url=None, redoc_url=None)
    # The extension calls from its background worker (chrome-extension://…). Only allow that.
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^chrome-extension://.*$",
                       allow_methods=["GET", "POST"], allow_headers=["content-type"])
    app.include_router(make_router(hub))
    return app


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class BackgroundServer:
    """Run the onboarding app with uvicorn in a thread (used by `nathan connect`)."""

    def __init__(self, hub: Hub, port: int | None = None) -> None:
        import uvicorn

        self.hub = hub
        self.port = port or free_port()
        hub.port = self.port
        config = uvicorn.Config(create_app(hub), host="127.0.0.1", port=self.port,
                                log_level="warning")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            threading.Event().wait(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        with contextlib.suppress(RuntimeError):
            self.thread.join(timeout=5)
