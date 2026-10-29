"""Tier 3: JSON extraction, router, planner, executor, critique — scripted models only."""

import asyncio
import json

import pytest
from pydantic import ValidationError

from nathan.errors import ProviderDown
from nathan.executor import execute
from nathan.jsonutil import ask_json, extract_json
from nathan.patterns.critique import critique
from nathan.planner import TaskGraph
from nathan.router import Route, heuristic_route, route
from tests.fakes import ScriptedClient, last_user, system_text

MSG = [{"role": "user", "content": "q"}]


# ---- jsonutil ------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Sure! Here you go:\n```json\n{"a": 1}\n```',
    'Result: {"a": 1,} done',
    '```\n{"a": 1}\n```',
    'noise {"a": 1} {"b": 2}',
])
def test_extract_json_variants(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_handles_braces_in_strings():
    assert extract_json('x {"s": "a } b", "n": [1]} y') == {"s": "a } b", "n": [1]}


def test_extract_json_none():
    with pytest.raises(ValueError):
        extract_json("no json here")


async def test_ask_json_repairs_then_moves_on():
    calls = []

    def responder(ref, messages, **kw):
        calls.append(ref)
        if ref == "alpha/a-big":
            return "not json at all"
        return '{"task_type": "code", "pattern": "vote"}'

    c = ScriptedClient(responder)
    r, ref = await ask_json(c, ["alpha/a-big", "beta/b-big"], MSG, Route)
    assert ref == "beta/b-big" and r.pattern == "vote"
    assert calls == ["alpha/a-big", "alpha/a-big", "beta/b-big"]  # one repair attempt each


# ---- router --------------------------------------------------------------------------

@pytest.mark.parametrize("q,pattern", [
    ("hi there", "single"),
    ("Research the history of solar power and write a one-page brief", "plan"),
    ("Fix the bug in this python function", "vote"),
    ("Write an email to my landlord about the heating", "critique"),
    ("Summarize the files in my workspace folder", "agent"),
    ("What is 17 * 23?", "vote"),
])
def test_heuristic_route(q, pattern):
    assert heuristic_route(q).pattern == pattern


async def test_route_uses_cheap_model(make_engine):
    def responder(ref, messages, **kw):
        assert "route tasks" in system_text(messages)
        return '{"task_type": "math", "pattern": "vote", "reason": "arithmetic"}'

    engine = make_engine(responder)
    r = await route(engine, "what is 2+2")
    assert r.pattern == "vote" and r.by == "alpha/a-small"  # the only cheap/fast-tagged model first
    assert r.tags == ["reasoning"]


async def test_route_falls_back_to_heuristic(make_engine):
    engine = make_engine(lambda r, m, **k: ProviderDown("down"))
    r = await route(engine, "Research X and write a one-page brief about it")
    assert r.pattern == "plan" and r.by == "heuristic"


# ---- planner -------------------------------------------------------------------------

def graph(tasks, **kw):
    return TaskGraph.model_validate({"goal": "g", "tasks": tasks, **kw})


def test_graph_waves():
    g = graph([
        {"id": "a", "instruction": "x"}, {"id": "b", "instruction": "x"},
        {"id": "c", "instruction": "x", "depends_on": ["a", "b"]},
        {"id": "d", "instruction": "x", "depends_on": ["c"]},
    ])
    assert [[t.id for t in w] for w in g.waves()] == [["a", "b"], ["c"], ["d"]]


@pytest.mark.parametrize("tasks", [
    [{"id": "a", "instruction": "x", "depends_on": ["zzz"]}],
    [{"id": "a", "instruction": "x", "depends_on": ["b"]}, {"id": "b", "instruction": "x", "depends_on": ["a"]}],
    [{"id": "a", "instruction": "x"}, {"id": "a", "instruction": "y"}],
    [],
])
def test_graph_rejects_bad(tasks):
    with pytest.raises(ValidationError):
        graph(tasks)


# ---- executor + gate simulation ---------------------------------------------------------

PLAN = {
    "goal": "Research solar power and write a one-page brief",
    "tasks": [
        {"id": "history", "instruction": "History of solar power"},
        {"id": "tech", "instruction": "How panels work"},
        {"id": "costs", "instruction": "Cost trends"},
        {"id": "policy", "instruction": "Policy landscape"},
        {"id": "outlook", "instruction": "Outlook", "depends_on": ["costs", "policy"]},
    ],
    "synthesis": "One-page brief with headings",
}


def make_gate_responder(state):
    async def responder(ref, messages, **kw):
        sys = system_text(messages)
        user = last_user(messages)
        if "planner" in sys:
            return json.dumps(PLAN)
        if "lead writer" in sys:
            assert all(f"--- {t['id']}" in user for t in PLAN["tasks"])
            return "# Solar brief\nA coherent one-page brief."
        if "reviewer" in sys:  # workflow's asked-vs-got review
            return json.dumps({"summary": "You asked for a brief and got one.", "scores": {}})
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.02)
        state["running"] -= 1
        if "Outlook" in user:
            for dep in ("costs", "policy"):  # plan says RESULT OF, workflow says WORK FROM
                assert f"RESULT OF {dep}" in user or f"WORK FROM {dep}" in user
        state["models"].add(ref)
        return f"findings for: {user.splitlines()[1]}"
    return responder


async def test_gate_plan_runs_four_parallel_and_returns_brief(make_engine):
    state = {"running": 0, "peak": 0, "models": set()}
    engine = make_engine(make_gate_responder(state))
    ans = await engine.ask("Research solar power and write a one-page brief", mode="plan")
    assert ans.text.startswith("# Solar brief")
    assert state["peak"] >= 4 and ans.extra["max_parallel"] == 4
    assert len({m.split("/")[0] for m in state["models"]}) >= 3  # spread across providers


async def test_executor_survives_a_failing_subtask(make_engine):
    def responder(ref, messages, **kw):
        if "lead writer" in system_text(messages):
            return "final"
        if "bad" in last_user(messages):
            return ProviderDown("down")
        return "ok"

    engine = make_engine(responder)
    g = graph([{"id": "good", "instruction": "fine"}, {"id": "bad", "instruction": "bad one"}])
    events = []
    res = await execute(engine, g, on_event=events.append)
    assert res.answer == "final"
    assert res.results["bad"].error and res.results["good"].text == "ok"
    assert {e["type"] for e in events} >= {"subtask_start", "subtask_done", "synthesis_done"}


async def test_auto_mode_routes_to_plan(make_engine):
    state = {"running": 0, "peak": 0, "models": set()}
    inner = make_gate_responder(state)

    async def responder(ref, messages, **kw):
        if "route tasks" in system_text(messages):
            return '{"task_type": "research", "pattern": "plan"}'
        return await inner(ref, messages, **kw)

    engine = make_engine(responder)
    ans = await engine.ask("Research solar power and write a one-page brief")
    assert ans.mode == "workflow" and "routed: research → workflow" in ans.note


# ---- critique ------------------------------------------------------------------------

async def test_critique_uses_different_providers():
    def responder(ref, messages, **kw):
        sys = system_text(messages)
        if "strict reviewer" in sys:
            return "Point 2 is wrong."
        if "Revise the draft" in sys:
            return "revised text"
        return "draft text"

    c = ScriptedClient(responder)
    r = await critique(c, MSG, drafters=["alpha/a-big"], critics=["alpha/a-big", "gamma/g-big"])
    assert r.final == "revised text" and r.draft == "draft text"
    assert r.critic == "gamma/g-big"  # not the drafter's provider


async def test_critique_skips_revision_when_clean():
    def responder(ref, messages, **kw):
        return "No changes needed." if "strict reviewer" in system_text(messages) else "draft"

    c = ScriptedClient(responder)
    r = await critique(c, MSG, drafters=["alpha/a-big"], critics=["beta/b-big"])
    assert r.final == "draft"
    assert len(c.calls) == 2
