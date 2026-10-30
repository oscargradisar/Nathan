"""Tier 5 web app: runs, SSE lanes, manual lane, quota, connect wizard — scripted models."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from nathan.errors import QuotaExceeded
from nathan.web.app import create_app
from nathan.web.manual import manual_prompt
from tests.fakes import fake_providers, last_user

H = {"x-nathan": "1"}


def five_providers():
    return fake_providers({
        "alpha": {"a-big": ["writing", "judge"]}, "beta": {"b-big": ["writing"]},
        "gamma": {"g-big": ["writing", "judge"]}, "delta": {"d-big": ["reasoning"]},
        "eps": {"e-big": ["writing"]}, "zeta": {"z-spare": ["writing"]},
    })


@pytest.fixture
def web(make_engine):
    def factory(responder, providers=None):
        engine = make_engine(responder, providers or five_providers())
        return TestClient(create_app(engine)), engine
    return factory


def read_events(client, run_id, since=0):
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events?since={since}") as r:
        for line in r.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
                if events[-1]["type"] == "done":
                    break
    return events


def responder(ref, messages, **kw):
    if "CANDIDATE" in last_user(messages):
        return "merged final answer"
    return f"lane answer from {ref} with several words"


def test_gate_vote_five_lanes_stream_and_merge(web):
    client, _ = web(responder)
    with client:
        r = client.post("/api/runs", json={"prompt": "Why is the sky blue?", "mode": "vote", "n": 5},
                        headers=H)
        assert r.status_code == 200
        run_id = r.json()["id"]
        assert "Why is the sky blue?" in r.json()["manual_prompt"]
        events = read_events(client, run_id)
    lanes = next(e for e in events if e["type"] == "lanes")["lanes"]
    assert len(lanes) == 5 and len({lane["model"].split("/")[0] for lane in lanes}) == 5
    for lane in lanes:
        assert any(e["type"] == "delta" and e["lane"] == lane["id"] for e in events)
        assert any(e["type"] == "lane_done" and e["lane"] == lane["id"] and e["ok"] for e in events)
    final = next(e for e in events if e["type"] == "final")
    assert final["text"] == "merged final answer" and final["judge"]
    assert events[-1]["type"] == "done"


def test_failed_lane_switches_to_spare(web):
    def r(ref, messages, **kw):
        if ref == "beta/b-big":
            return QuotaExceeded("out")
        return responder(ref, messages)

    client, _ = web(r)
    with client:
        run_id = client.post("/api/runs", json={"prompt": "q", "mode": "vote", "n": 5}, headers=H).json()["id"]
        events = read_events(client, run_id)
    switch = next(e for e in events if e["type"] == "lane_switch")
    assert switch["reason"] == "QuotaExceeded" and switch["model"] == "zeta/z-spare"


def test_manual_lane_adds_candidate_and_rejudges(web):
    seen = {}

    def r(ref, messages, **kw):
        if "CANDIDATE" in last_user(messages):
            seen["judge_prompt"] = last_user(messages)
        return responder(ref, messages)

    client, _ = web(r)
    with client:
        run_id = client.post("/api/runs", json={"prompt": "q", "mode": "vote", "n": 2}, headers=H).json()["id"]
        read_events(client, run_id)
        assert client.post(f"/api/runs/{run_id}/manual",
                           json={"source": "claude.ai", "text": "PASTED FROM CLAUDE"}, headers=H).json()["ok"]
        since = client.post(f"/api/runs/{run_id}/rejudge", headers=H).json()["since"]
        events = read_events(client, run_id, since - 1)
    assert events[0]["type"] == "manual_added"
    assert any(e["type"] == "final" for e in events)
    assert "PASTED FROM CLAUDE" in seen["judge_prompt"]


def test_other_modes_run_through_engine(web):
    def r(ref, messages, **kw):
        return f"single from {ref}"

    client, _ = web(r)
    with client:
        run_id = client.post("/api/runs", json={"prompt": "hello", "mode": "single"}, headers=H).json()["id"]
        events = read_events(client, run_id)
    final = next(e for e in events if e["type"] == "final")
    assert final["text"].startswith("single from")


def test_post_without_header_or_from_foreign_host_is_refused(web):
    client, _ = web(responder)
    assert client.post("/api/runs", json={"prompt": "q"}).status_code == 403
    r = client.get("/api/quota", headers={"host": "evil.example"})
    assert r.status_code == 403
    assert client.get("/api/quota", headers={"host": "127.0.0.1:8765"}).status_code == 200


def test_bad_mode_and_empty_prompt(web):
    client, _ = web(responder)
    assert client.post("/api/runs", json={"prompt": "q", "mode": "magic"}, headers=H).status_code == 400
    assert client.post("/api/runs", json={"prompt": "  "}, headers=H).status_code == 400


def test_quota_endpoint(web):
    client, engine = web(responder)
    engine.quota.record("alpha", "a-big", 10, {"x-ratelimit-remaining-requests-day": "99"})
    data = client.get("/api/quota").json()
    alpha = next(p for p in data["providers"] if p["provider"] == "alpha")
    assert alpha["used"] == 1 and alpha["remaining"] == 99 and alpha["has_key"]


def test_pages_and_static(web):
    client, _ = web(responder)
    assert "Nathan" in client.get("/").text
    for old in ("/connect", "/workflow", "/leaderboard"):  # old addresses open the console
        assert "view-accounts" in client.get(old).text
    for js in ("main", "core", "console", "views", "sound", "voice", "quips", "fx", "morphy", "world", "boot"):
        assert client.get(f"/static/js/{js}.js").status_code == 200
    assert "swarm" in client.get("/api/models").json()["modes"]
    for img in ("morphy-portrait", "morphy-daguerreotype", "morphy-1859", "morphy-blindfold"):
        assert client.get(f"/static/img/{img}.jpg").status_code == 200
    assert "vote" in client.get("/api/models").json()["modes"]


def test_connect_wizard_endpoints(web, monkeypatch):
    from nathan.config import DEFAULT_CONFIG, load_providers

    providers = load_providers(DEFAULT_CONFIG, env_file=None)
    for p in providers.values():
        monkeypatch.delenv(p.key_env, raising=False)
        for var in p.url_vars:  # e.g. CLOUDFLARE_ACCOUNT_ID, which another test may load from .env
            monkeypatch.delenv(var, raising=False)
    client, _ = web(responder, providers)

    async def ok(p, key):
        return True, "ok"

    client.app.state.hub.checker = ok
    client.app.state.hub.saver = lambda p, key: "keychain"
    listing = client.get("/api/connect").json()["providers"]
    assert [p["provider"] for p in listing][:4] == ["openrouter", "groq", "gemini", "cerebras"]
    cf = next(p for p in listing if p["provider"] == "cloudflare")
    assert "CLOUDFLARE_ACCOUNT_ID" in cf["missing_env"]

    opened: list[str] = []
    client.app.state.open_browser = opened.append
    client.app.state.connect_kw = {"use_clipboard": False}
    with client:
        r = client.post("/api/connect/groq", headers=H).json()
        assert r["method"] == "extension" and "#nathan=" in r["url"]
        r = client.post("/api/connect/openrouter", headers=H).json()
        assert r["url"].startswith("https://openrouter.ai/auth?")
        r = client.post("/api/connect/nvidia", headers=H).json()
        assert r["method"] == "manual" and r["url"].startswith("https://")
    assert len(opened) == 3  # the server opened each key page in the real browser

    bad = client.post("/api/keys/manual", json={"provider": "nvidia", "key": "not a key"}, headers=H)
    assert bad.status_code == 422
    # A key in a format providers.yaml doesn't know yet still works if the provider accepts it
    odd = client.post("/api/keys/manual", json={"provider": "gemini", "key": "AQ.new-format-key-1234"},
                      headers=H)
    assert odd.status_code == 200

    async def no(p, key):
        return False, "invalid key"

    client.app.state.hub.checker = no
    odd = client.post("/api/keys/manual", json={"provider": "gemini", "key": "AQ.new-format-key-1234"},
                      headers=H)
    assert odd.status_code == 400 and "doesn't look like" in odd.json()["detail"]
    client.app.state.hub.checker = ok
    good = client.post("/api/keys/manual", json={"provider": "nvidia", "key": "nvapi-" + "x" * 64},
                       headers=H)
    assert good.status_code == 200


def test_manual_prompt_text():
    p = manual_prompt("  Explain X  ")
    assert p.endswith("TASK:\nExplain X\n")


def test_save_and_exit_flags_the_app(web):
    client, _ = web(lambda *a, **k: "ok")
    with client:
        assert client.app.state.quit is False
        assert client.post("/api/quit", json={}, headers=H).json() == {"quitting": True}
        assert client.app.state.quit is True


def test_stop_ends_a_run_in_progress(web):
    import asyncio

    async def thinking():
        await asyncio.sleep(30)  # a model that's still thinking
        return "late answer"

    def slow(*a, **k):
        return thinking()

    client, _ = web(slow)
    with client:
        run_id = client.post("/api/runs", json={"prompt": "q", "mode": "single"}, headers=H).json()["id"]
        assert client.post(f"/api/runs/{run_id}/stop", headers=H).json() == {"stopped": True}
        for _ in range(100):
            if client.app.state.runs.get(run_id).finished:
                break
            time.sleep(0.02)
        types = [e["type"] for e in client.app.state.runs.get(run_id).events]
        assert types[-2:] == ["stopped", "done"]
        assert client.post(f"/api/runs/{run_id}/stop", headers=H).json() == {"stopped": False}
        assert client.post("/api/runs/nope/stop", headers=H).status_code == 404
