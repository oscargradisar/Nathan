"""Tier 5 web app: `uv run nathan web` → http://127.0.0.1:8765

Pages:   /            control room (live lanes, final answer, manual lane, quota)
         /connect     "Connect your AI accounts" wizard
API:     POST /api/runs, GET /api/runs/{id}/events (SSE), POST /api/runs/{id}/manual,
         POST /api/runs/{id}/rejudge, GET /api/quota, GET /api/models,
         POST /api/connect/{provider}, POST /api/keys/manual, POST /api/disconnect/{provider}
Plus the onboarding endpoints (/keys, /callback, /onboarding.json) on the same port.

Security for a local app: every page on the internet can try to send requests to
127.0.0.1. Two cheap defences stop that:
- Host check: only "127.0.0.1:<port>" / "localhost:<port>" are served (blocks DNS rebinding).
- POSTs under /api need an `X-Nathan: 1` header. Browsers won't let another site add a
  custom header without a CORS preflight, which this server never approves.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from nathan.engine import MODES
from nathan.onboarding.server import Hub, make_router
from nathan.web.manual import SITES, manual_prompt
from nathan.web.runs import RunManager

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class RunIn(BaseModel):
    prompt: str
    mode: str = "vote"
    models: list[str] | None = None
    n: int = 5


class ManualIn(BaseModel):
    source: str = "claude.ai"
    text: str


class KeyIn(BaseModel):
    provider: str
    key: str


def create_app(engine, hub: Hub | None = None) -> FastAPI:
    app = FastAPI(title="Nathan", docs_url=None, redoc_url=None)
    runs = RunManager(engine)
    hub = hub or Hub(engine.providers)
    app.state.engine, app.state.runs, app.state.hub = engine, runs, hub

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS and host != "testserver":
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        if (request.method == "POST" and request.url.path.startswith("/api/")
                and request.headers.get("x-nathan") != "1"):
            return JSONResponse({"detail": "missing X-Nathan header"}, status_code=403)
        return await call_next(request)

    # The Helper extension's background worker posts to /keys from chrome-extension://
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"^chrome-extension://.*$",
                       allow_methods=["GET", "POST"], allow_headers=["content-type"])
    app.include_router(make_router(hub))
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # ---- pages --------------------------------------------------------------------------

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/connect")
    async def connect_page() -> FileResponse:
        return FileResponse(STATIC / "connect.html")

    # ---- runs ---------------------------------------------------------------------------

    @app.post("/api/runs")
    async def start_run(body: RunIn) -> dict:
        if body.mode not in MODES:
            raise HTTPException(400, f"mode must be one of {MODES}")
        if not body.prompt.strip():
            raise HTTPException(400, "empty prompt")
        run = runs.start(body.prompt, body.mode, body.models, max(1, min(body.n, 8)))
        return {"id": run.id, "manual_prompt": manual_prompt(body.prompt), "sites": SITES}

    @app.get("/api/runs/{run_id}/events")
    async def events(run_id: str, since: int = 0) -> StreamingResponse:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")

        async def stream():
            async for event in run.subscribe(since):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    @app.get("/api/runs/{run_id}")
    async def run_state(run_id: str) -> dict:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        return {"id": run.id, "prompt": run.prompt, "mode": run.mode, "final": run.final,
                "finished": run.finished, "events": len(run.events),
                "candidates": [c.__dict__ for c in run.candidates]}

    @app.post("/api/runs/{run_id}/manual")
    async def add_manual(run_id: str, body: ManualIn) -> dict:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        if not body.text.strip():
            raise HTTPException(400, "empty answer")
        await runs.add_manual(run, body.source[:40], body.text[:50_000])
        return {"ok": True, "candidates": len(run.candidates)}

    @app.post("/api/runs/{run_id}/rejudge")
    async def rejudge(run_id: str) -> dict:
        run = runs.get(run_id)
        if run is None:
            raise HTTPException(404, "no such run")
        if not run.finished:
            raise HTTPException(409, "run still in progress")
        since = len(run.events)
        asyncio.create_task(runs.rejudge(run))
        return {"ok": True, "since": since}

    @app.post("/api/manual-prompt")
    async def make_manual_prompt(body: RunIn) -> dict:
        return {"prompt": manual_prompt(body.prompt), "sites": SITES}

    # ---- quota + models -----------------------------------------------------------------

    @app.get("/api/quota")
    async def quota() -> dict:
        rows = engine.quota.report()
        providers: dict[str, dict] = {}
        for r in rows:
            p = providers.setdefault(r["provider"], {"provider": r["provider"],
                                                     "has_key": r["has_key"], "used": 0,
                                                     "remaining": 0, "unknown": False,
                                                     "models": []})
            p["used"] += r["used_requests"]
            if r["remaining"] is None:
                p["unknown"] = True
            else:
                p["remaining"] += r["remaining"]
            p["models"].append(r)
        # Account-wide daily caps (e.g. OpenRouter 50/day) are shared, not per model
        for name, p in providers.items():
            acct = engine.providers[name].account_limits.rpd
            if acct is not None:
                p["remaining"] = min(p["remaining"], max(0, acct - p["used"]))
        return {"providers": list(providers.values())}

    @app.get("/api/models")
    async def models() -> dict:
        return {"models": [{"ref": p.ref(m), "provider": p.name, "tags": spec.tags,
                            "has_key": bool(p.api_key)}
                           for p in engine.providers.values() for m, spec in p.models.items()],
                "modes": list(MODES)}

    # ---- connect wizard -----------------------------------------------------------------

    @app.get("/api/connect")
    async def connect_list() -> dict:
        from nathan.onboarding.connect import connect_order

        out = []
        for name in connect_order(engine.providers):
            p = engine.providers[name]
            ob = p.onboarding
            st = hub.status.get(name)
            daily = sum((s.limits.rpd or 0) for s in p.models.values())
            out.append({
                "provider": name, "method": ob.method if ob else "manual",
                "key_page": (ob.key_page if ob else "") or p.signup or "",
                "note": ob.note if ob else "", "missing_env": p.missing_env,
                "state": "connected" if p.api_key else (st.state if st else "not connected"),
                "detail": st.detail if st else "",
                "models": len(p.models),
                "daily_requests": p.account_limits.rpd or daily or None,
            })
        return {"providers": out}

    @app.post("/api/connect/{provider}")
    async def connect_start(provider: str) -> dict:
        from nathan.onboarding.connect import connect_oauth

        p = engine.providers.get(provider)
        if p is None:
            raise HTTPException(404, "unknown provider")
        method = p.onboarding.method if p.onboarding else "manual"
        if method == "oauth_pkce":
            loop = asyncio.get_running_loop()
            url_future: asyncio.Future = loop.create_future()
            task = asyncio.create_task(connect_oauth(hub, provider, open_browser=url_future.set_result))
            app.state.oauth_task = task  # keep a reference so it isn't garbage-collected
            url = await asyncio.wait_for(url_future, 5)
            return {"method": method, "url": url}
        if method == "extension":
            return {"method": method, "url": hub.start(provider)}
        return {"method": "manual", "url": (p.onboarding.key_page if p.onboarding else "")
                or p.signup or "", "note": p.onboarding.note if p.onboarding else ""}

    @app.post("/api/keys/manual")
    async def manual_key(body: KeyIn) -> dict:
        code, msg = await hub.submit(body.provider, body.key, None, require_token=False)
        if code != 200:
            raise HTTPException(code, msg)
        return {"ok": True, "message": msg}

    @app.post("/api/disconnect/{provider}")
    async def disconnect(provider: str) -> dict:
        from nathan.keys import delete_key
        from nathan.onboarding.server import Status

        p = engine.providers.get(provider)
        if p is None:
            raise HTTPException(404, "unknown provider")
        delete_key(p.name, p.key_env)
        hub.status[provider] = Status("not connected", "key removed from this computer")
        return {"ok": True, "revoke_at": (p.onboarding.key_page if p.onboarding else p.signup)}

    return app


def serve(engine=None, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          path: str = "/") -> None:
    """Run the web app (blocking)."""
    import threading
    import webbrowser

    import uvicorn

    from nathan.engine import Engine

    engine = engine or Engine()
    hub = Hub(engine.providers, port=port)
    app = create_app(engine, hub)  # hub.loop stays None: everything runs on uvicorn's loop
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}{path}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
