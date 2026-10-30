"""Overlord mode: Claude's plan is checked and repaired, its picks are real staff, and parts
tagged "tools" run through the tool-using agent loop. Managers are always faked here."""

import json

from nathan import managers
from nathan.client import ChatResult
from nathan.planner import MAX_TASKS
from nathan.workflow import run_workflow
from tests.test_workflow import PLAN, responder_for

REVIEW = {"summary": "ok", "scores": {"t1": 8, "t2": 8, "t3": 8}, "prayer": ""}


def claude_boss(monkeypatch, plans: list[str], asked: list[str] | None = None) -> None:
    """Claude is signed in; its plan replies come from `plans` in order."""
    async def connected():
        return [managers.CLAUDE]

    async def ask(m, prompt, **kw):
        if asked is not None:
            asked.append(prompt)
        if "staff available right now" in prompt:
            return plans.pop(0)
        if "reviewer" in prompt:
            return json.dumps(REVIEW)
        return "OVERLORD FINAL"

    monkeypatch.setattr(managers, "connected", connected)
    monkeypatch.setattr(managers, "ask", ask)


async def test_made_up_worker_falls_back_to_normal_assignment(make_engine, monkeypatch):
    engine = make_engine(responder_for({}))
    tasks = [{**t, "worker": "alpha/a-imaginary-70b"} if t["id"] == "t1" else t for t in PLAN["tasks"]]
    claude_boss(monkeypatch, [json.dumps({**PLAN, "tasks": tasks})])
    wf = await run_workflow(engine, "Plan a trip", overlord=True)
    assert wf.assigned["t1"] in engine.roster.candidates(None)
    assert all(r["ref"] != "alpha/a-imaginary-70b" for r in engine.staff.board())  # no ghost row


async def test_invalid_plan_gets_one_second_chance(make_engine, monkeypatch):
    engine = make_engine(responder_for({}))
    cyclic = {**PLAN, "tasks": [{"id": "t1", "instruction": "a", "depends_on": ["t2"]},
                                {"id": "t2", "instruction": "b", "depends_on": ["t1"]}]}
    asked: list[str] = []
    claude_boss(monkeypatch, [json.dumps(cyclic), json.dumps(PLAN)], asked)
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append, overlord=True)
    plan_asks = [p for p in asked if "staff available right now" in p]
    assert len(plan_asks) == 2 and "REJECTED" in plan_asks[1] and "cycle" in plan_asks[1]
    assert next(e for e in events if e["type"] == "plan")["planner"] == "Claude"
    assert [t.id for t in wf.graph.tasks] == ["t1", "t2", "t3"]


async def test_still_invalid_after_retry_falls_back_to_free_planner(make_engine, monkeypatch):
    engine = make_engine(responder_for(REVIEW))
    claude_boss(monkeypatch, ["not json at all", "still not json"])
    events = []
    await run_workflow(engine, "Plan a trip", events.append, overlord=True)
    assert next(e for e in events if e["type"] == "plan")["planner"] != "Claude"
    assert any(e["type"] == "task_failed" and e["id"] == "manager" for e in events)


async def test_oversized_plan_is_trimmed_not_rejected(make_engine, monkeypatch):
    engine = make_engine(responder_for({}))
    big = {"goal": "g", "synthesis": "s",
           "tasks": [{"id": f"t{i}", "instruction": f"part {i}",
                      "depends_on": ["t12"] if i == 1 else []} for i in range(1, 13)]}
    asked: list[str] = []
    claude_boss(monkeypatch, [json.dumps(big)], asked)
    wf = await run_workflow(engine, "Plan a trip", overlord=True)
    assert len(wf.graph.tasks) == MAX_TASKS and wf.graph.tasks[0].depends_on == []
    assert sum("staff available right now" in p for p in asked) == 1  # no retry needed


async def test_tools_part_runs_through_the_agent_loop(make_engine, monkeypatch, tmp_path):
    (tmp_path / "prices.txt").write_text("Hotel Lev: 120 EUR a night")
    plan = {**PLAN, "tasks": [{"id": "t1", "instruction": "read prices.txt", "depends_on": [],
                               "tags": ["tools"]},
                              {"id": "t2", "instruction": "find comfort options", "depends_on": []}]}
    base = responder_for({})

    def responder(ref, messages, **kw):
        if "You have tools" in str(messages[0]["content"]):
            results = [m for m in messages if m["role"] == "tool"]
            if not results:
                prov, _, model = ref.partition("/")
                return ChatResult("", prov, model, tool_calls=[
                    {"id": "c1", "name": "read_file", "arguments": json.dumps({"path": "prices.txt"})}])
            return f"From the file: {results[-1]['content']}"
        return base(ref, messages, **kw)

    engine = make_engine(responder)
    engine.workspace = tmp_path  # file tools stay inside the test's folder
    claude_boss(monkeypatch, [json.dumps(plan)])
    events = []
    wf = await run_workflow(engine, "Plan a trip", events.append, overlord=True)
    assert wf.results["t1"] == "From the file: Hotel Lev: 120 EUR a night"
    assert wf.results["t2"].startswith("work by")  # untagged parts stay plain chats
    steps = [e for e in events if e["type"] == "step"]
    assert steps and all(e["id"].startswith("t1:") for e in steps)  # tool steps under t1
    assert any(e["type"] == "delivered" and e["id"] == "t1" for e in events)
    assert next(r for r in engine.staff.board() if r["ref"] == wf.worker["t1"])["done"] >= 1


async def test_tools_part_answers_without_tools_when_every_agent_fails(make_engine, monkeypatch):
    """Seen live: parallel tool agents ran every model out of per-minute tokens. The part
    must still deliver, from a plain worker, instead of failing."""
    from nathan.errors import QuotaExceeded

    plan = {**PLAN, "tasks": [{"id": "t1", "instruction": "read the Notion pricing page",
                               "depends_on": [], "tags": ["tools"]}]}
    base = responder_for({})

    def responder(ref, messages, **kw):
        if "You have tools" in str(messages[0]["content"]):
            return QuotaExceeded("429 tokens per minute")
        return base(ref, messages, **kw)

    engine = make_engine(responder)
    claude_boss(monkeypatch, [json.dumps(plan)])
    events = []
    wf = await run_workflow(engine, "Compare note apps", events.append, overlord=True)
    assert wf.results["t1"].startswith("work by")
    assert any(e["type"] == "reassigned" and "without them" in e["reason"] for e in events)


# ---- managers as researchers -------------------------------------------------------------


async def test_research_doesnt_wait_out_a_slow_premium_researcher(make_engine, monkeypatch):
    """Once the free analysts answered, a premium researcher gets a short grace period,
    not its full timeout; what arrived in time is still used."""
    import asyncio

    from nathan import research
    from tests.test_premium import fake_sources

    monkeypatch.setattr(research, "PREMIUM_GRACE_S", 0.1)
    engine = make_engine(lambda ref, m, **k: json.dumps({"queries": ["Ljubljana population"]})
                         if "plan web research" in str(m[0]["content"]) else "about 295,000 [1]")

    async def ask(m, prompt, web=False, timeout=None):
        if m.id == "chatgpt" and web:
            await asyncio.sleep(30)  # would hold the answer for half a minute
        return "Verified: 295,000 [1]. Confidence: high" if "verifier" in prompt else "Claude: 295k"

    events = []
    r = await asyncio.wait_for(research.run_research(
        engine, "What is the population of Ljubljana?", events.append, http=fake_sources(),
        team=[managers.CLAUDE, managers.CHATGPT], ask=ask), 10)
    late = next(e for e in events if e["type"] == "task_failed" and e["id"] == "premium:chatgpt")
    assert "went ahead" in late["reason"]
    assert any(e["type"] == "delivered" and e["id"] == "premium:claude" for e in events)
    assert "Claude" in r["analysts"] and "ChatGPT" not in r["analysts"]
