"""Workflow: manager → named workers → writer → reviewer → Elo → optional prayer."""

import json

from nathan.errors import ProviderDown
from nathan.workflow import answer_prayer, run_workflow

PLAN = {"goal": "trip", "synthesis": "combine",
        "tasks": [{"id": "t1", "instruction": "find budget options", "depends_on": []},
                  {"id": "t2", "instruction": "find comfort options", "depends_on": []},
                  {"id": "t3", "instruction": "compare them", "depends_on": ["t1", "t2"]}]}


def responder_for(review: dict, flaky: set[str] | None = None):
    def responder(ref, messages, **kw):
        system = str(messages[0]["content"])
        if "planner" in system:
            return json.dumps(PLAN)
        if "reviewer" in system:
            return json.dumps(review)
        if "lead writer" in system:
            return "FINAL: " + ("revised" if "CLAUDE REPLIED" in str(messages[-1]["content"]) else "first")
        if flaky and ref in flaky:
            return ProviderDown("timed out")
        return f"work by {ref}"
    return responder


async def test_workflow_assigns_reviews_and_rates(make_engine):
    review = {"summary": "You asked for a trip plan and got one.", "delivered": ["budget", "comfort"],
              "missing": [], "scores": {"t1": 9, "t2": 6, "t3": 7}, "prayer": ""}
    engine = make_engine(responder_for(review))
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)
    types = [e["type"] for e in events]

    plan = next(e for e in events if e["type"] == "plan")
    first_wave = [t["worker"] for t in plan["tasks"] if not t["depends_on"]]
    assert len(set(first_wave)) == 2  # parallel parts go to different workers
    workers_done = [e for e in events if e["type"] == "delivered" and e["role"] == "worker"]
    assert len(workers_done) == 3 and "prayer" not in types and "redo" not in types
    assert wf.final == "FINAL: first" and wf.review.summary.startswith("You asked")
    ratings = next(e for e in events if e["type"] == "ratings")["changes"]
    assert ratings[wf.worker["t1"]] > ratings[wf.worker["t2"]]
    assert engine.staff.board()[0]["ref"] == wf.worker["t1"]


async def test_no_show_is_reassigned_and_loses_elo(make_engine):
    review = {"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}, "prayer": ""}
    engine = make_engine(lambda *a, **k: None)
    events = []

    # Find who t1 will be assigned to, then make that worker time out
    from nathan.planner import TaskGraph
    from nathan.workflow import assign

    first = assign(engine, TaskGraph.model_validate(PLAN))["t1"]
    engine.client.responder = responder_for(review, flaky={first})
    wf = await run_workflow(engine, "Plan a trip", events.append)
    moved = next(e for e in events if e["type"] == "reassigned" and e["id"] == "t1")
    assert moved["from"] == first and wf.worker["t1"] != first
    no_show = next(r for r in engine.staff.board() if r["ref"] == first)
    assert no_show["no_shows"] >= 1 and no_show["elo"] < 1000


async def test_prayer_round_trip(make_engine):
    review = {"summary": "ok", "scores": {"t1": 7, "t2": 7, "t3": 7},
              "prayer": "Is the old town walkable in winter?"}
    engine = make_engine(responder_for(review))
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)
    prayer = next(e for e in events if e["type"] == "prayer")
    assert "walkable" in prayer["message"] and "Plan a trip" in prayer["message"]
    assert await answer_prayer(engine, wf, "Yes, very.", events.append) == "FINAL: revised"
    assert events[-1]["type"] == "blessed"


async def test_quota_limit_is_not_a_no_show(make_engine):
    from nathan.errors import QuotaExceeded
    from nathan.planner import TaskGraph
    from nathan.workflow import assign

    review = {"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}, "prayer": ""}
    engine = make_engine(lambda *a, **k: None)
    first = assign(engine, TaskGraph.model_validate(PLAN))["t1"]
    base = responder_for(review)
    engine.client.responder = lambda ref, m, **k: QuotaExceeded("429") if ref == first else base(ref, m, **k)
    wf = await run_workflow(engine, "Plan a trip")
    assert wf.no_shows["t1"] == [] and all(r["no_shows"] == 0 for r in engine.staff.board())


async def test_weak_part_is_redone_by_someone_else(make_engine):
    calls = {"review": 0}
    base = responder_for({})

    def responder(ref, messages, **kw):
        if "reviewer" in str(messages[0]["content"]):
            calls["review"] += 1
            score = 3 if calls["review"] == 1 else 8  # weak first time, fixed after the redo
            return json.dumps({"summary": "ok", "scores": {"t1": 9, "t2": score, "t3": 8}})
        return base(ref, messages, **kw)

    engine = make_engine(responder)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)
    redo = next(e for e in events if e["type"] == "redo")
    assert redo["ids"] == ["t2"]
    first = next(e["worker"] for e in events if e["type"] == "delivered" and e["id"] == "t2")
    assert wf.worker["t2"] != first  # someone else redid it
    assert (first, 3) in wf.round_one  # and the first worker keeps its low score for Elo
    assert calls["review"] == 2 and [e["type"] for e in events].count("final") == 2


async def test_slow_worker_gets_a_backup(make_engine, monkeypatch):
    import asyncio

    import nathan.workflow as w

    monkeypatch.setattr(w, "HEDGE_AFTER_S", 0.05)
    base = responder_for({"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}})
    engine = make_engine(lambda *a, **k: None)
    slow = w.assign(engine, w.TaskGraph.model_validate(PLAN))["t1"]

    async def responder(ref, messages, **kw):
        if ref == slow and "YOUR PART: find budget" in str(messages[-1]["content"]):
            await asyncio.sleep(5)  # would hold the whole job up without a backup
        return base(ref, messages, **kw)

    engine.client.responder = responder
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)
    backup = next(e for e in events if e["type"] == "backup" and e["id"] == "t1")
    assert backup["for"] == slow and wf.worker["t1"] == backup["worker"]
    assert all(r["no_shows"] == 0 for r in engine.staff.board())  # slow isn't absent
