"""Self-maintenance (roll call, diagnostics) and the console's run modes — all mocked."""

import json

import httpx2 as httpx
from fastapi.testclient import TestClient

from nathan.errors import ModelGone
from nathan.health import diagnostics, roll_call
from nathan.web.app import create_app
from tests.fakes import fake_providers
from tests.test_web import H, read_events


def lister(listing: dict[str, list[str]]):
    def handler(req):
        name = req.url.host.split(".")[0]
        return httpx.Response(200, json={"data": [{"id": i} for i in listing.get(name, [])]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_roll_call_retires_and_reinstates(make_engine):
    providers = fake_providers({"alpha": {"a1": [], "a2": [], "a3": []}, "beta": {"b1": []}})
    engine = make_engine(lambda *a, **k: "ok", providers)
    engine.quota.mark_gone("beta", "b1")
    out = await roll_call(engine, lister({"alpha": ["a1", "a2"], "beta": ["b1"]}))
    assert out["retired"] == ["alpha/a3"] and out["reinstated"] == ["beta/b1"]
    assert engine.quota.is_gone("alpha", "a3") and not engine.quota.is_gone("beta", "b1")


async def test_roll_call_ignores_a_list_missing_most_models(make_engine):
    providers = fake_providers({"alpha": {"a1": [], "a2": [], "a3": []}})
    engine = make_engine(lambda *a, **k: "ok", providers)
    out = await roll_call(engine, lister({"alpha": ["something-else"]}))
    assert out["unchecked"] == ["alpha"] and not out["retired"]


async def test_diagnostics_reports_each_model(make_engine):
    providers = fake_providers({"alpha": {"good": []}, "beta": {"dead": []}})
    engine = make_engine(lambda ref, m, **k: ModelGone("404") if "dead" in ref else "OK", providers)
    events = []
    results = await diagnostics(engine, events.append)
    assert [(r["ref"], r["ok"]) for r in results] == [("alpha/good", True), ("beta/dead", False)]
    assert {e["type"] for e in events} >= {"working", "delivered", "task_failed"}


def run(client, prompt, mode, **kw):
    r = client.post("/api/runs", json={"prompt": prompt, "mode": mode, **kw}, headers=H)
    assert r.status_code == 200, r.text
    return read_events(client, r.json()["id"])


def test_auto_routes_and_streams(make_engine):
    def responder(ref, messages, **kw):
        if "route tasks" in str(messages[0]["content"]):
            return json.dumps({"task_type": "writing", "pattern": "critique"})
        sys = str(messages[0]["content"])
        return "No changes needed." if "strict reviewer" in sys else "a draft"

    engine = make_engine(responder)
    with TestClient(create_app(engine)) as client:
        events = run(client, "Write a haiku about rain", "auto")
    routed = next(e for e in events if e["type"] == "routed")
    assert routed["pattern"] == "critique"
    steps = [(e["role"], e["status"]) for e in events if e["type"] == "step"]
    assert ("drafter", "working") in steps and ("critic", "done") in steps
    assert next(e for e in events if e["type"] == "final")["text"] == "a draft"


def test_swarm_uses_many_models(make_engine):
    providers = fake_providers({p: {f"{p}-m": ["writing", "judge"]} for p in "abcdef"})
    engine = make_engine(lambda ref, m, **k: "merged" if "CANDIDATE" in str(m[-1]["content"])
                         else f"answer from {ref} in several words", providers)
    with TestClient(create_app(engine)) as client:
        events = run(client, "Name a colour", "swarm")
    lanes = next(e for e in events if e["type"] == "lanes")["lanes"]
    assert len(lanes) == 6 and next(e for e in events if e["type"] == "final")["text"] == "merged"


def test_status_and_history(make_engine):
    engine = make_engine(lambda *a, **k: "an answer")
    engine.memory.log_exchange("What is 2+2?", "4", {"mode": "single"})
    with TestClient(create_app(engine)) as client:
        status = client.get("/api/status").json()
        history = client.get("/api/history").json()["items"]
    assert status["staff_ready"] > 0 and status["memories"] >= 2
    assert history[0]["question"] == "What is 2+2?" and history[0]["mode"] == "single"


async def test_slow_router_falls_back_to_keywords(make_engine, monkeypatch):
    import asyncio

    from nathan import router

    monkeypatch.setattr(router, "ROUTE_TIMEOUT_S", 0.05)

    async def slow(ref, messages, **kw):
        await asyncio.sleep(2)
        return "{}"

    engine = make_engine(slow)
    r = await router.route(engine, "Research and compare solar and wind power in a brief")
    assert r.by == "heuristic" and r.pattern == "plan"


async def test_quick_mode_prefers_the_fastest_model(make_engine):
    engine = make_engine(lambda ref, m, **k: f"answer from {ref}")
    slow, fast = engine.roster.candidates(["fast"])[:2]
    engine.staff.record(slow, ok=True, ms=30000)
    engine.staff.record(fast, ok=True, ms=500)
    assert engine.fastest(["fast"])[0] == fast
    ans = await engine.ask("hi", mode="single", use_memory=False)
    assert ans.models == [fast]
