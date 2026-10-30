"""Evolve: a change is built on its own branch, verified, then applied or discarded.
Real git in a temp repo; the agent and the test run are fakes (no AI, no network)."""

import subprocess

import pytest

from nathan import evolve, managers


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "src"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "nathan"\n')
    (repo / "app.py").write_text("print('v1')\n")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "v1")
    return repo


@pytest.fixture
def evolver(tmp_path, source, monkeypatch):
    ev = evolve.Evolver(tmp_path / "data", source)
    boss = managers.Manager("claude", "Claude", "Manager", 0, "pkg", "plan")

    async def connected():
        return [boss]

    async def agent(self, m, prompt, cwd, job):
        assert "Follow CLAUDE.md" in prompt and cwd != source
        (cwd / "app.py").write_text("print('v2')\n")
        job.summary = "prints v2"

    passed = {"ok": True}

    async def verify(self, cwd):
        return passed["ok"], "fake tests"

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(evolve.Evolver, "_agent", agent)
    monkeypatch.setattr(evolve.Evolver, "_verify", verify)
    ev.passed = passed
    return ev


async def test_change_is_built_apart_then_applied(evolver, source):
    job = evolver.start("print v2")
    await job.task
    assert job.status == "ready" and "app.py" in job.stat and "+print('v2')" in job.diff
    assert (source / "app.py").read_text() == "print('v1')\n"  # your copy untouched until Apply
    evolver.apply(job.id)
    assert (source / "app.py").read_text() == "print('v2')\n"
    assert "Evolve: prints v2" in git(source, "log", "-1", "--format=%B")
    assert job.branch not in git(source, "branch") and evolver.pending_restart


async def test_failing_tests_cannot_be_applied_and_discard_cleans_up(evolver, source):
    evolver.passed["ok"] = False
    job = evolver.start("")
    await job.task
    assert job.status == "failed"
    with pytest.raises(RuntimeError, match="tests pass"):
        evolver.apply(job.id)
    evolver.discard(job.id)
    assert job.branch not in git(source, "branch")
    assert "evolve" not in git(source, "worktree", "list")


async def test_autopilot_applies_passing_rounds(evolver, source):
    evolver.start_autopilot(1)
    await evolver._auto_task
    assert evolver.autopilot["applied"] == 1 and not evolver.autopilot["on"]
    assert (source / "app.py").read_text() == "print('v2')\n"


def test_narrate_claude_stream():
    line = ('{"type":"assistant","message":{"content":[{"type":"text","text":"Fixing the bug"},'
            '{"type":"tool_use","name":"Edit","input":{"file_path":"C:/x/nathan/globe.py"}}]}}')
    assert evolve.narrate(line, "claude") == ["Fixing the bug", "→ Edit globe.py"]
    assert evolve.narrate('{"type":"result","result":"done\\nSUMMARY: faster"}', "claude") == ["SUMMARY: faster"]


def test_page_can_start_and_discard_through_the_api(evolver, make_engine, monkeypatch):
    # The page talks to the server: the background job must start on the server's loop.
    import time

    from fastapi.testclient import TestClient

    from nathan.web.app import create_app

    client = TestClient(create_app(make_engine(lambda *a, **k: "ok")))
    with client:
        client.app.state.evolver.__dict__.update(source=evolver.source, data=evolver.data)
        job = client.post("/api/evolve", json={"goal": "print v2"}, headers={"x-nathan": "1"}).json()
        for _ in range(100):
            if client.get(f"/api/evolve/{job['id']}").json()["status"] in ("ready", "failed"):
                break
            time.sleep(0.05)
        assert client.get(f"/api/evolve/{job['id']}").json()["status"] == "ready"
        r = client.post(f"/api/evolve/{job['id']}/discard", headers={"x-nathan": "1"})
        assert r.json()["status"] == "discarded"
