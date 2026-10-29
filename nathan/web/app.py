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
import webbrowser
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
RUN_MODES = (*MODES, "swarm", "diagnostics")
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


def create_app(engine, hub: Hub | None = None, self_check: bool = False) -> FastAPI:
    """self_check: at startup, ask every provider which models it still offers (free)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if self_check:
            from nathan.health import roll_call

            async def check() -> None:
                try:
                    app.state.roll_call = await roll_call(engine)
                except Exception as e:  # noqa: BLE001 - a failed check must never stop the app
                    app.state.roll_call = {"error": str(e)}

            app.state.roll_call_task = asyncio.create_task(check())
        yield

    app = FastAPI(title="Nathan", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.roll_call = None
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

    @app.get("/workflow")
    async def workflow_page() -> FileResponse:
        return FileResponse(STATIC / "workflow.html")

    @app.get("/leaderboard")
    async def leaderboard_page() -> FileResponse:
        return FileResponse(STATIC / "leaderboard.html")

    @app.get("/api/status")
    async def status() -> dict:
        from importlib.metadata import PackageNotFoundError, version

        try:
            ver = version("nathan")
        except PackageNotFoundError:
            ver = "dev"
        keyed = [p for p in engine.providers.values() if p.key_env and p.api_key]
        free = [p for p in engine.providers.values() if not p.key_env]
        staff = engine.roster.candidates(None)
        total = sum(len(p.models) for p in engine.providers.values() if p.api_key)
        return {"version": ver, "accounts": [p.name for p in keyed],
                "gateways": [p.name for p in free], "staff_ready": len(staff),
                "staff_total": total, "active_runs": sum(not r.finished for r in runs.runs.values()),
                "roll_call": app.state.roll_call, "memories": engine.memory.count()}

    @app.get("/api/history")
    async def history(limit: int = 20) -> dict:
        items = []
        for e in engine.memory.recent(limit * 3):
            if e.kind == "question":
                items.append({"id": e.id, "ts": e.ts, "question": e.content,
                              "mode": e.meta.get("mode", "")})
            if len(items) >= limit:
                break
        return {"items": items}

    @app.get("/api/staff")
    async def staff_board() -> dict:
        latest: dict[tuple[str, str], dict] = {}  # (run, task) → that task's latest event
        for r in runs.runs.values():
            if not r.finished:
                for e in r.events:
                    if e["type"] in ("working", "delivered", "reassigned", "task_failed"):
                        latest[(r.id, e["id"])] = e
        busy = {e["worker"] for e in latest.values() if e["type"] == "working"}
        rows = engine.staff.board()
        known = {r["ref"] for r in rows}
        rows += [{"place": None, "ref": ref, "elo": 1000, "rank": "Silver", "jobs": 0, "done": 0,
                  "no_shows": 0, "wins": 0, "losses": 0, "avg_score": None, "avg_seconds": None}
                 for ref in engine.roster.candidates(None) if ref not in known]
        for r in rows:
            r["working"] = r["ref"] in busy
        return {"staff": rows}

    @app.post("/api/runs/{run_id}/prayer")
    async def prayer_answer(run_id: str, body: ManualIn) -> dict:
        run = runs.get(run_id)
        if run is None or run.workflow is None:
            raise HTTPException(404, "no workflow run with that id")
        if not run.finished:
            raise HTTPException(409, "the team is still working")
        if not body.text.strip():
            raise HTTPException(400, "paste Claude's reply first")
        since = len(run.events)
        asyncio.create_task(runs.answer_prayer(run, body.text[:50_000]))
        return {"ok": True, "since": since}

    # ---- runs ---------------------------------------------------------------------------

    @app.post("/api/runs")
    async def start_run(body: RunIn) -> dict:
        if body.mode not in RUN_MODES:
            raise HTTPException(400, f"mode must be one of {RUN_MODES}")
        if not body.prompt.strip():
            raise HTTPException(400, "empty prompt")
        run = runs.start(body.prompt, body.mode, body.models, max(1, min(body.n, 16)))
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
                "modes": list(RUN_MODES)}

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
                "provider": name, "method": "none" if not p.key_env else (ob.method if ob else "manual"),
                "key_page": (ob.key_page if ob else "") or p.signup or "",
                "note": ob.note if ob else "", "missing_env": p.missing_env,
                "state": "connected" if p.api_key else (st.state if st else "not connected"),
                "detail": st.detail if st else "",
                "models": len(p.models),
                "daily_requests": p.account_limits.rpd or daily or None,
            })
        return {"providers": out}

    # The server opens key pages in the real browser (a desktop window can't open tabs) and
    # keeps listening in the background: Helper extension, clipboard copy or OAuth callback.
    app.state.open_browser = webbrowser.open
    app.state.connect_kw = {}
    connecting: set[asyncio.Task] = set()  # references so running tasks aren't garbage-collected

    @app.post("/api/connect/{provider}")
    async def connect_start(provider: str) -> dict:
        from nathan.onboarding.connect import connect

        p = engine.providers.get(provider)
        if p is None:
            raise HTTPException(404, "unknown provider")
        opened: asyncio.Future = asyncio.get_running_loop().create_future()

        def open_browser(url: str) -> None:
            if not opened.done():
                opened.set_result(url)
            app.state.open_browser(url)

        task = asyncio.create_task(connect(hub, provider, open_browser=open_browser,
                                           **app.state.connect_kw))
        connecting.add(task)
        task.add_done_callback(connecting.discard)
        await asyncio.wait({opened, task}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        url = opened.result() if opened.done() else ""
        return {"method": p.onboarding.method if p.onboarding else "manual", "url": url,
                "note": p.onboarding.note if p.onboarding else ""}

    @app.post("/api/key-page/{provider}")
    async def open_key_page(provider: str) -> dict:
        p = engine.providers.get(provider)
        if p is None:
            raise HTTPException(404, "unknown provider")
        page = (p.onboarding.key_page if p.onboarding else "") or p.signup
        if page:
            app.state.open_browser(page)
        return {"ok": bool(page)}

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
        if not p.key_env:
            raise HTTPException(400, f"{provider} needs no key, so there is nothing to remove")
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
    app = create_app(engine, hub, self_check=True)  # hub.loop None: all on uvicorn's loop
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}{path}")).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
