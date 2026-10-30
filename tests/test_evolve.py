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
    monkeypatch.setattr(evolve, "_have_uv", lambda: True)
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


async def test_merge_conflict_is_aborted_and_reported(evolver, source):
    job = evolver.start("print v2")
    await job.task
    (source / "app.py").write_text("print('owner edit')\n")  # the owner moved on meanwhile
    git(source, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "owner")
    with pytest.raises(RuntimeError, match="conflicts"):
        evolver.apply(job.id)
    assert git(source, "status", "--porcelain") == ""  # not left half-merged
    assert not (source / ".git" / "MERGE_HEAD").exists()
    assert (source / "app.py").read_text() == "print('owner edit')\n" and job.status == "conflict"


async def test_discard_from_a_worker_thread_stops_the_agent_then_cleans_up(evolver, source, monkeypatch):
    import asyncio

    started, stopped = asyncio.Event(), []

    async def stuck_agent(self, m, prompt, cwd, job):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            stopped.append(cwd.exists())  # still there: cleanup waits until the agent has stopped
            raise

    monkeypatch.setattr(evolve.Evolver, "_agent", stuck_agent)
    job = evolver.start("print v2")
    await started.wait()
    await asyncio.to_thread(evolver.discard, job.id)  # the route runs it in a thread
    await asyncio.wait({job.task}, timeout=10)
    assert stopped == [True] and job.status == "discarded"
    assert job.branch not in git(source, "branch") and "evolve" not in git(source, "worktree", "list")


async def test_claude_builds_even_when_chatgpt_leads(evolver, monkeypatch):
    managers.choose("chatgpt")

    async def both():
        return [managers.CHATGPT, managers.CLAUDE]

    monkeypatch.setattr(managers, "connected", both)
    job = evolver.start("print v2")
    await job.task
    assert job.manager == "Claude"


async def test_one_job_at_a_time_and_none_beside_autopilot(evolver):
    job = evolver.start("print v2")
    with pytest.raises(RuntimeError, match="already running"):
        evolver.start("again")  # checked at once, not when the lock is finally taken
    await job.task
    evolver.autopilot["on"] = True
    with pytest.raises(RuntimeError, match="Autopilot"):
        evolver.start("manual")
    evolver.autopilot["on"] = False


async def test_missing_uv_is_explained(evolver, monkeypatch):
    monkeypatch.setattr(evolve, "_have_uv", lambda: False)
    with pytest.raises(RuntimeError, match="needs uv"):
        evolver.start("x")


async def test_autopilot_survives_start_failing(evolver, monkeypatch):
    monkeypatch.setattr(evolve, "_have_uv", lambda: False)
    evolver.start_autopilot(2)
    await evolver._auto_task  # no unretrieved exception: the reason is on the page instead
    assert not evolver.autopilot["on"] and "uv" in evolver.autopilot["error"]


async def test_jobs_survive_a_restart_and_leftovers_are_swept(evolver, tmp_path, source):
    leftover = tmp_path / "data" / "evolve" / "deadbeef"
    (leftover / "sub").mkdir(parents=True)
    job = evolver.start("print v2")
    await job.task
    assert not leftover.exists()  # an earlier session's copy is removed before the first job
    again = evolve.Evolver(tmp_path / "data", source)
    assert again.get(job.id).status == "ready" and "+print('v2')" in again.get(job.id).diff
    again.apply(job.id)  # a ready change can still be applied after a restart
    assert (source / "app.py").read_text() == "print('v2')\n"


def test_live_jobs_are_marked_interrupted_after_a_restart(tmp_path, source):
    folder = tmp_path / "data" / "evolve"
    folder.mkdir(parents=True)
    (folder / "jobs.json").write_text('[{"id": "abc", "goal": "x", "status": "working"}]')
    ev = evolve.Evolver(tmp_path / "data", source)
    assert ev.get("abc").status == "failed" and "restart" in ev.get("abc").log[-1]
    assert not ev.running()


# ---- ideas: Claude reads the code and suggests what to build ----------------------------

REPLY = ('Here you go: {"ideas": [{"title": "Cache pings", "why": "Every run waits a second.", '
         '"kind": "speed", "size": "small"}, {"title": "Fix the globe", "why": "It spins.", '
         '"kind": "weird", "size": "huge"}, {"why": "no title: skipped"}]}')


@pytest.fixture
def asked(monkeypatch):
    calls = []

    async def ask(m, prompt, **kw):
        calls.append({"m": m.id, "prompt": prompt, **kw})
        return REPLY

    monkeypatch.setattr(managers, "ask", ask)
    return calls


async def test_suggest_reads_the_source_and_lists_ideas(evolver, source, asked):
    evolver.start_suggest()
    assert evolver.status()["suggesting"]
    with pytest.raises(RuntimeError, match="Already"):
        evolver.start_suggest()  # a double click can't start two
    await evolver._suggest_task
    s = evolver.status()
    assert asked[0]["m"] == "claude" and asked[0]["cwd"] == source  # read-only, in the source
    assert "CLAUDE.md" in asked[0]["prompt"] and "v1" in asked[0]["prompt"]  # rules + git log
    assert not s["suggesting"] and not s["suggest_error"]
    assert [(i["title"], i["kind"], i["size"]) for i in s["ideas"]] == [
        ("Cache pings", "speed", "small"), ("Fix the globe", "capability", "medium")]
    assert evolve.Evolver(evolver.data, source).ideas == s["ideas"]  # kept across a restart


async def test_building_an_idea_removes_it_and_autopilot_takes_the_top_one(evolver, asked, monkeypatch):
    evolver.start_suggest()
    await evolver._suggest_task
    _first, second = evolver.ideas
    job = evolver.start("Fix the globe. It spins.", second["id"])
    await job.task
    assert [i["title"] for i in evolver.ideas] == ["Cache pings"]

    goals = []

    async def agent(self, m, prompt, cwd, job):
        goals.append(job.goal)
        (cwd / "app.py").write_text(f"print({len(goals)})\n")

    monkeypatch.setattr(evolve.Evolver, "_agent", agent)
    evolver.discard(job.id)
    evolver.start_autopilot(2)
    await evolver._auto_task
    assert goals == ["Cache pings. Every run waits a second.", ""]  # then it chooses by itself
    assert evolver.ideas == [] and evolver.autopilot["applied"] == 2


async def test_suggest_without_a_manager_says_why(evolver, asked, monkeypatch):
    async def nobody():
        return []

    monkeypatch.setattr(managers, "connected", nobody)
    evolver.start_suggest()
    await evolver._suggest_task
    assert "signed in" in evolver.status()["suggest_error"] and not asked


def test_parse_ideas_tolerates_junk():
    assert evolve.parse_ideas("no json here") == []
    assert evolve.parse_ideas('[{"title": "A", "kind": "UI", "size": "Large"}]')[0]["kind"] == "ui"
    assert len(evolve.parse_ideas('{"ideas": [' + ",".join(['{"title": "x"}'] * 12) + "]}")) == 8


def test_page_can_ask_for_ideas_through_the_api(evolver, asked, make_engine):
    import time

    from fastapi.testclient import TestClient

    from nathan.web.app import create_app

    client = TestClient(create_app(make_engine(lambda *a, **k: "ok")))
    with client:
        ev = client.app.state.evolver
        ev.__dict__.update(source=evolver.source, data=evolver.data, ideas=[])
        assert client.post("/api/evolve-suggest", headers={"x-nathan": "1"}).json()["suggesting"]
        for _ in range(100):
            if not client.get("/api/evolve").json()["suggesting"]:
                break
            time.sleep(0.05)
        idea = client.get("/api/evolve").json()["ideas"][0]
        job = client.post("/api/evolve", json={"goal": "Cache pings", "idea": idea["id"]},
                          headers={"x-nathan": "1"}).json()
        assert job["goal"] == "Cache pings"
        assert idea["id"] not in [i["id"] for i in client.get("/api/evolve").json()["ideas"]]
