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
    first_t2 = {}
    base = responder_for({})

    def responder(ref, messages, **kw):
        text = str(messages[-1]["content"])
        if "YOUR PART: find comfort" in text:
            first_t2.setdefault("ref", ref)
        if "reviewer" in str(messages[0]["content"]):
            # t2 is weak while it's still the first worker's work, fine once someone redid it
            t2 = text.split("--- t2", 1)[1].split("---", 2)[1]
            score = 3 if f"work by {first_t2['ref']}" in t2 else 8
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
    assert [e["type"] for e in events].count("final") == 2  # rewritten after the redo


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


async def test_a_part_starts_when_its_own_dependencies_are_done(make_engine):
    """No wave barrier: t3 needs only t1, so it mustn't wait for a slow, unrelated t2."""
    import asyncio

    plan = {**PLAN, "tasks": [PLAN["tasks"][0], PLAN["tasks"][1],
                              {"id": "t3", "instruction": "check budget", "depends_on": ["t1"]}]}
    base = responder_for({"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}})

    async def responder(ref, messages, **kw):
        if "planner" in str(messages[0]["content"]):
            return json.dumps(plan)
        if "YOUR PART: find comfort" in str(messages[-1]["content"]):
            await asyncio.sleep(0.3)
        return base(ref, messages, **kw)

    events = []
    wf = await run_workflow(make_engine(responder), "Plan a trip", events.append)
    order = [(e["type"], e.get("id")) for e in events]
    assert order.index(("delivered", "t3")) < order.index(("delivered", "t2"))
    assert set(wf.results) == {"t1", "t2", "t3"}


async def test_a_part_still_runs_when_its_dependency_failed(make_engine):
    base = responder_for({"summary": "ok", "scores": {"t1": 0, "t2": 8, "t3": 8}})
    seen = []

    def responder(ref, messages, **kw):
        text = str(messages[-1]["content"])
        if "YOUR PART: find budget" in text:
            return ProviderDown("down")  # every worker fails t1
        if "YOUR PART: compare" in text:
            seen.append(text)
        return base(ref, messages, **kw)

    events = []
    wf = await run_workflow(make_engine(responder), "Plan a trip", events.append)
    assert any(e["type"] == "task_failed" and e["id"] == "t1" for e in events)
    assert "t3" in wf.results and "WORK FROM t2" in seen[0] and "WORK FROM t1" not in seen[0]


async def test_redo_worker_sees_the_reviewers_feedback(make_engine):
    first_t2, prompts = {}, []
    base = responder_for({})

    def responder(ref, messages, **kw):
        text = str(messages[-1]["content"])
        if "YOUR PART: find comfort" in text:
            first_t2.setdefault("ref", ref)
            prompts.append(text)
        if "reviewer" in str(messages[0]["content"]):
            t2 = text.split("--- t2", 1)[1].split("---", 2)[1]
            score = 3 if f"work by {first_t2['ref']}" in t2 else 8
            return json.dumps({"summary": "Comfort part is thin.", "missing": ["hotel prices"],
                               "scores": {"t1": 9, "t2": score, "t3": 8}})
        return base(ref, messages, **kw)

    await run_workflow(make_engine(responder), "Plan a trip", [].append)
    assert len(prompts) == 2 and "REDONE" not in prompts[0]
    redo = prompts[1]
    assert "scored the previous attempt 3/10" in redo and "hotel prices" in redo
    assert "Comfort part is thin." in redo and f"work by {first_t2['ref']}" in redo


async def test_signed_in_claude_answers_the_prayer_itself(make_engine, monkeypatch):
    from nathan import managers

    engine = make_engine(responder_for({}))
    asked = []

    async def connected():
        return [managers.CLAUDE]

    async def ask(m, prompt, **kw):
        if "reviewer" in prompt:
            return json.dumps({"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8},
                               "prayer": "Is the old town walkable in winter?"})
        asked.append((prompt, kw))
        return "Yes, very."

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(managers, "ask", ask)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)  # plain workflow: no boss
    types = [e["type"] for e in events]
    assert "prayer" not in types and "blessed" in types  # nothing for the owner to carry
    assert "walkable" in asked[0][0] and asked[0][1].get("web") is True
    assert any(e["type"] == "delivered" and e["id"] == "prayer" for e in events)
    assert wf.final == "FINAL: revised"


async def test_prayer_falls_back_to_the_owner_when_claude_cant_answer(make_engine, monkeypatch):
    from nathan import managers

    engine = make_engine(responder_for({}))

    async def connected():
        return [managers.CLAUDE]

    async def ask(m, prompt, **kw):
        if "reviewer" in prompt:
            return json.dumps({"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8},
                               "prayer": "Is the old town walkable in winter?"})
        raise RuntimeError("usage limit reached")

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(managers, "ask", ask)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append)
    assert any(e["type"] == "task_failed" and e["id"] == "prayer" for e in events)
    assert events[-2]["type"] == "prayer" and wf.final == "FINAL: first"


async def test_overlord_plans_picks_workers_and_writes(make_engine, monkeypatch):
    from nathan import managers

    review = {"summary": "Overlord approves.", "scores": {"t1": 9, "t2": 8, "t3": 8}, "prayer": "?"}
    engine = make_engine(responder_for({}))  # free staff: workers only
    asked = []

    async def connected():
        return [managers.CHATGPT, managers.CLAUDE]

    async def ask(m, prompt, **kw):
        asked.append(m.id)
        if "staff available right now" in prompt:
            assert "beta/b-big" in prompt and '"worker"' in prompt  # sees the staff sheet
            return json.dumps({**PLAN, "tasks": [{**t, "worker": "beta/b-big"} if t["id"] == "t1" else t
                                                 for t in PLAN["tasks"]]})
        if "reviewer" in prompt:
            return json.dumps(review)
        return "OVERLORD FINAL"

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(managers, "ask", ask)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append, overlord=True)
    assert set(asked) == {"claude"}  # Claude outranks ChatGPT and runs the whole job
    assert wf.assigned["t1"] == "beta/b-big" and wf.worker["t1"] == "beta/b-big"
    assert wf.final == "OVERLORD FINAL" and wf.writer == "Claude"
    assert next(e for e in events if e["type"] == "plan")["planner"] == "Claude"
    assert "prayer" not in [e["type"] for e in events]  # no praying to the boss


async def test_overlord_falls_back_to_free_staff(make_engine, monkeypatch):
    from nathan import managers

    engine = make_engine(responder_for({"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}}))

    async def connected():
        return [managers.CLAUDE]

    async def ask(m, prompt, **kw):
        raise RuntimeError("usage limit reached")

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(managers, "ask", ask)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append, overlord=True)
    assert wf.final == "FINAL: first" and wf.writer != "Claude"
    assert sum(e["type"] == "task_failed" and "take over" in e.get("reason", "") for e in events) >= 2
