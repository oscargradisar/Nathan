"""Workflow: a manager splits your prompt into parts, named workers do the parts in
parallel, a writer turns their work into one answer, and a reviewer compares what you
asked for with what you got.

Every step is announced as an event, so the Workflow page can show who's working on
what. The reviewer's scores feed the staff Elo ladder (staff.py). If the team is stuck,
the reviewer can write a prayer to Claude: you approve it, paste it into claude.ai
yourself (there is no free Claude API), and paste the reply back for a revised answer.

Overlord mode (run_workflow(..., overlord=True)): Claude, through its official CLI on your
own plan, is the boss. It sees the whole staff with Elo and speed, plans the parts and
hand-picks a worker for each, writes the final answer from their work and reviews it.
ChatGPT steps in if Claude isn't signed in, and the free planner, writer and judges take
over any step a manager can't do, so a job never stalls on the boss.

Parts tagged "tools" run through the agent loop (web pages, Python, workspace files); their
tool calls show as "step" events with ids like "t1:tool1-call_0".

Events: phase, plan, working, backup, reassigned, delivered, task_failed, step, final,
review, redo, ratings, prayer, blessed, note.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nathan import managers
from nathan.errors import RECOVERABLE, NoModelAvailable
from nathan.jsonutil import extract_json, race_json
from nathan.patterns import fallback
from nathan.planner import MAX_TASKS, PLANNER_SYSTEM, Subtask, TaskGraph, plan
from nathan.staff import START

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
MAX_TRIES = 5  # workers tried per task before giving up on it
HEDGE_AFTER_S = 20.0  # no delivery after this long: a backup worker starts on the same part
BENCH_BELOW = 150  # pick score this far under a new model's: benched (slow or unreliable)
REDO_BELOW = 5  # parts the reviewer scores below this get redone once by someone else
SLOW_SCORE = 2.0  # what a worker scores when a backup had to finish its part
AGENT_STEPS = 6  # tool calls a "tools" part may make before it must answer
TOOL_TRIES = 12  # models a "tools" part may hand over to before answering without tools

WORKER_SYSTEM = ("You are one worker on a team of AI models. Do ONLY the part you are given, "
                 "thoroughly and factually. Say plainly if you are unsure of a fact.")
TOOL_WORKER_SYSTEM = (WORKER_SYSTEM + " You have tools: read live web pages, run Python for exact "
                      "answers and read the workspace files instead of guessing. When your part is "
                      "done, reply with the result itself, not a description of what you did.")
WRITER_SYSTEM = ("You are the lead writer. Turn the team's work into one coherent answer to "
                 "the original request. Resolve contradictions, cut repetition, keep it "
                 "well structured. Answer the person directly; don't mention the team.")
REVIEWER_SYSTEM = """You are the reviewer. Compare what the person ASKED FOR with what the
team DELIVERED, in plain words a busy person reads in one go.
Score each worker's part from 0 (useless or missing) to 10 (excellent).
Only if the team is genuinely unsure or missing something a stronger model could settle,
write "prayer": a short, humble, specific question to Claude. Otherwise leave it "".
Reply with ONLY JSON:
{"summary": "asked vs got, 2-4 sentences", "delivered": ["..."], "missing": ["..."],
 "scores": {"t1": 8}, "prayer": ""}"""


OVERLORD_SYSTEM = """You are the OVERLORD of Nathan: you command a staff of free AI models
that run in parallel, and you decide who does what. The staff available right now
(model · strengths · Elo rating · average seconds per job):
{staff}

For each subtask you may set "worker" to one of the models above (exact name) to
hand-pick it: hard reasoning to high-Elo models, quick lookups to fast ones, and parallel
parts spread across different providers. Leave "worker" as "" to let Nathan pick.
Tag a part "tools" when it must act rather than recall: read live web pages, run Python or
read the person's workspace files. Its worker then gets those tools."""


class Review(BaseModel):
    summary: str
    delivered: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    prayer: str = ""


@dataclass
class Workflow:
    prompt: str
    graph: TaskGraph | None = None
    assigned: dict[str, str] = field(default_factory=dict)    # task id → first worker
    worker: dict[str, str] = field(default_factory=dict)      # task id → who delivered
    no_shows: dict[str, list[str]] = field(default_factory=dict)
    results: dict[str, str] = field(default_factory=dict)
    final: str = ""
    writer: str = ""
    review: Review | None = None
    round_one: list[tuple[str, float]] = field(default_factory=list)  # (worker, score) of redone parts
    team: list = field(default_factory=list)  # signed-in premium managers
    slow: list[str] = field(default_factory=list)  # workers that needed a backup to cover them


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


def assign(engine, graph: TaskGraph) -> dict[str, str]:
    """Give each task a worker: the best-rated model that fits its tags, spread so parallel
    tasks in a wave go to different models (and different providers when possible)."""
    out: dict[str, str] = {}
    # The overlord's pick must be a real, usable model from providers.yaml: a made-up name
    # would waste a request on a 404 and leave a ghost row on the Elo ladder.
    staff = set(engine.roster.candidates(None))
    for wave in graph.waves():
        busy: set[str] = set()
        for task in wave:
            if task.worker and task.worker not in busy and task.worker in staff:
                out[task.id] = task.worker  # the overlord's pick
                busy.add(task.worker)
                continue
            fits = engine.roster.candidates(task.tags or None)[:12] or engine.roster.candidates(None)
            starters = [r for r in fits if engine.staff.priority(r) >= START - BENCH_BELOW]
            fits = starters or fits  # the bench only plays when nobody better is free
            if not fits:
                raise NoModelAvailable("no workers available: connect a provider first")
            fits.sort(key=lambda r: -engine.staff.priority(r))
            free = [r for r in fits if r not in busy]
            other_provider = [r for r in free if r.split("/")[0] not in {b.split("/")[0] for b in busy}]
            out[task.id] = (other_provider or free or fits)[0]
            busy.add(out[task.id])
    return out


def _worker_messages(wf: Workflow, task: Subtask, feedback: str = "",
                     system: str = WORKER_SYSTEM) -> list:
    parts = [f"THE PERSON'S REQUEST: {wf.prompt}", f"YOUR PART: {task.instruction}"]
    for dep in task.depends_on:
        if wf.results.get(dep):
            parts.append(f"--- WORK FROM {dep} ---\n{wf.results[dep]}")
    if feedback:  # a redo: the new worker learns why the first attempt fell short
        parts.append(f"--- WHY THIS PART IS BEING REDONE ---\n{feedback}")
    return [{"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(parts)}]


def _redo_feedback(wf: Workflow, task: Subtask, score: float) -> str:
    """What the reviewer said about a weak part, for the worker who redoes it."""
    assert wf.review is not None
    lines = [f"A reviewer scored the previous attempt {score:g}/10."]
    if wf.review.summary:
        lines.append(f"Review: {wf.review.summary}")
    if wf.review.missing:
        lines.append("Missing from the answer: " + "; ".join(wf.review.missing))
    if wf.results.get(task.id):
        lines.append(f"Previous attempt (do better than this):\n{wf.results[task.id][:2000]}")
    return "\n".join(lines)


async def hedged(engine, unit: str, chain: list[str], messages: list, emit: Emit | None,
                 no_shows: list[str] | None = None, role: str = "worker",
                 hedge_after: float | None = None, slow: list[str] | None = None):
    """Run one job on the first worker in `chain`. If it hasn't delivered after HEDGE_AFTER_S,
    a backup starts on the same job and whoever finishes first wins (the slow one is cancelled,
    no penalty: slow isn't absent). Failures hand over to the next worker.
    Returns (ref, ChatResult, ms) or None if nobody delivered."""
    chain = chain[:MAX_TRIES]
    running: dict[asyncio.Task, tuple[str, float]] = {}
    nxt = 0

    async def launch(event: dict) -> bool:
        nonlocal nxt
        if nxt >= len(chain):
            return False
        ref = chain[nxt]
        nxt += 1
        running[asyncio.create_task(engine.client.chat(ref, messages))] = (ref, time.perf_counter())
        await _emit(emit, {**event, "id": unit, "worker": ref, "role": role})
        return True

    await launch({"type": "working"})
    try:
        while running:
            hedge = len(running) == 1 and nxt < len(chain)
            wait = hedge_after if hedge_after is not None else HEDGE_AFTER_S
            done, _ = await asyncio.wait(running, timeout=wait if hedge else None,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:  # slow, not dead: bring in a backup alongside, the quickest we have
                laggard = next(iter(running.values()))[0]
                if slow is not None:
                    slow.append(laggard)
                speed = engine.staff.seconds
                chain[nxt:] = sorted(chain[nxt:], key=lambda r: speed(r) or HEDGE_AFTER_S)
                await launch({"type": "backup", "for": laggard})
                continue
            for t in done:
                ref, start = running.pop(t)
                try:
                    res = t.result()
                    reason = "" if res.text else "empty answer"
                except RECOVERABLE as e:
                    reason = type(e).__name__
                if not reason:
                    ms = (time.perf_counter() - start) * 1000
                    if not res.cached:  # a cache hit says nothing about this worker's speed
                        engine.staff.record(ref, ok=True, ms=ms)
                        engine.staff.log_work(ref, role, str(messages[-1]["content"]), res.text)
                    await _emit(emit, {"type": "delivered", "id": unit, "worker": ref, "role": role,
                                       "text": res.text, "seconds": round(ms / 1000, 1)})
                    return ref, res, ms
                if reason != "QuotaExceeded":  # out of free quota isn't the worker's fault
                    engine.staff.record(ref, ok=False)
                    if no_shows is not None:
                        no_shows.append(ref)
                if not running and nxt < len(chain):
                    await launch({"type": "reassigned", "from": ref, "reason": reason})
                    # (the event's "worker" is the new one; the UI reads from → worker)
        await _emit(emit, {"type": "task_failed", "id": unit, "role": role})
        return None
    finally:
        for t in running:
            t.cancel()


async def _do_task(engine, wf: Workflow, task: Subtask, emit: Emit | None,
                   avoid: tuple[str, ...] = (), feedback: str = "") -> None:
    first = wf.assigned[task.id]
    chain = [first] + [r for r in engine.chain(task.tags or None) if r != first and r not in avoid]
    wf.no_shows.setdefault(task.id, [])
    if "tools" in task.tags:  # this part has to act (web pages, Python, files), not just recall
        await _do_tool_task(engine, wf, task, chain, emit, feedback)
        return
    got = await hedged(engine, task.id, chain, _worker_messages(wf, task, feedback), emit,
                       wf.no_shows[task.id], slow=wf.slow)
    if got:
        wf.worker[task.id], wf.results[task.id] = got[0], got[1].text


async def _do_tool_task(engine, wf: Workflow, task: Subtask, chain: list[str], emit: Emit | None,
                        feedback: str = "") -> None:
    """A part tagged "tools" runs through the agent loop (as agent mode does), so the worker
    can fetch live pages, run Python and read workspace files. No hedging here: two agents
    racing on the same part would do every action twice."""
    from nathan.agent import run_agent
    from nathan.jobs import workspace
    from nathan.tools import default_toolbox, desktop

    toolbox = default_toolbox(engine.memory, workspace(engine.workspace), engine)
    # Parts run in parallel, so tools that drive the person's own screen and keyboard
    # (apps, Discord, email) stay out: two agents typing at once would garble both.
    for t in desktop.make_tools():
        toolbox.tools.pop(t.name, None)

    async def on_event(event: dict) -> None:  # tool steps show under this part on the board
        await _emit(emit, {**event, "id": f"{task.id}:{event['id']}"})

    await _emit(emit, {"type": "working", "id": task.id, "worker": chain[0], "role": "worker"})
    start = time.perf_counter()
    try:
        # Fetched pages are big, so tool agents hit per-minute token limits quickly: a longer
        # chain than plain parts get, so one busy provider can't sink the part
        r = await run_agent(engine.client, chain[:TOOL_TRIES],
                            _worker_messages(wf, task, feedback, TOOL_WORKER_SYSTEM), toolbox,
                            max_steps=AGENT_STEPS, on_event=on_event)
        reason = "" if r.answer.strip() else "empty answer"
    except NoModelAvailable as e:
        reason = str(e)
    if reason:  # no tools worked out: a plain worker answers from what it knows
        await _emit(emit, {"type": "reassigned", "id": task.id, "role": "worker", "from": chain[0],
                           "worker": chain[0], "reason": f"tools failed ({reason[:80]}); answering "
                                                         "without them"})
        got = await hedged(engine, task.id, chain, _worker_messages(wf, task, feedback), emit,
                           wf.no_shows[task.id], slow=wf.slow)
        if got:
            wf.worker[task.id], wf.results[task.id] = got[0], got[1].text
        return
    ref, ms = r.models[-1], (time.perf_counter() - start) * 1000  # the model that answered
    engine.staff.record(ref, ok=True, ms=ms)
    engine.staff.log_work(ref, "worker", task.instruction, r.answer)
    wf.worker[task.id], wf.results[task.id] = ref, r.answer
    await _emit(emit, {"type": "delivered", "id": task.id, "worker": ref, "role": "worker",
                       "text": r.answer, "seconds": round(ms / 1000, 1)})


async def _run_tasks(engine, wf: Workflow, tasks: list[Subtask], emit: Emit | None,
                     avoid: dict[str, str] | None = None,
                     feedback: dict[str, str] | None = None) -> None:
    """Start every task as soon as ITS OWN dependencies are finished, not when a whole wave
    is: a part that needs t1 shouldn't wait for an unrelated t2 that is still hedging.

    Each task is an asyncio task; a dependent first awaits the tasks it depends on (only
    those in this batch: in a redo, the others already delivered). A dependency that failed
    still counts as finished: the dependent runs without that input, as before."""
    ids = {t.id for t in tasks}
    running: dict[str, asyncio.Task] = {}

    async def go(t: Subtask) -> None:
        # All tasks are created below before any of them runs, so every dependency is there
        deps = [running[d] for d in t.depends_on if d in ids]
        if deps:
            await asyncio.gather(*deps)
        await _do_task(engine, wf, t, emit, avoid=tuple(filter(None, [(avoid or {}).get(t.id)])),
                       feedback=(feedback or {}).get(t.id, ""))

    for t in tasks:
        running[t.id] = asyncio.create_task(go(t))
    try:
        await asyncio.gather(*running.values())
    finally:  # one part crashed (or the run was stopped): don't leave the others running
        for task in running.values():
            task.cancel()


async def _write(engine, wf: Workflow, emit: Emit | None, boss: managers.Manager | None = None) -> None:
    assert wf.graph is not None
    brief = (f"ORIGINAL REQUEST:\n{wf.prompt}\n\nHOW TO COMBINE: {wf.graph.synthesis}"
             f"\n\nTEAM WORK:\n{_team_work(wf)}")
    if boss is not None:
        await _emit(emit, {"type": "working", "id": "writer", "worker": boss.name, "role": "writer"})
        try:
            text = (await managers.ask(boss, WRITER_SYSTEM + "\n\n" + brief)).strip()
        except RuntimeError as e:
            text = ""
            await _emit(emit, {"type": "task_failed", "id": "writer", "role": "writer", "worker": boss.name,
                               "reason": f"{boss.name} couldn't write ({str(e)[:80]}); free writers take over"})
        if text:
            wf.final, wf.writer = text, boss.name
            await _emit(emit, {"type": "delivered", "id": "writer", "worker": boss.name, "role": "writer",
                               "text": text})
            await _emit(emit, {"type": "final", "text": wf.final, "writer": wf.writer})
            return
    # The write-up is long, so a slow model makes everyone wait: quickest of the good writers
    good = engine.roster.candidates(["writing"])[:10]
    writers = sorted(good, key=lambda r: engine.staff.seconds(r) or HEDGE_AFTER_S) + engine.chain(["writing"])
    got = await hedged(engine, "writer", list(dict.fromkeys(writers)), [
        {"role": "system", "content": WRITER_SYSTEM}, {"role": "user", "content": brief}],
        emit, role="writer")
    if got is None:
        raise NoModelAvailable("no writer could combine the team's work")
    wf.final, wf.writer = got[1].text, got[0]
    await _emit(emit, {"type": "final", "text": wf.final, "writer": wf.writer})


def _team_work(wf: Workflow) -> str:
    assert wf.graph is not None
    return "\n\n".join(f"--- {t.id} ({t.instruction}) ---\n{wf.results.get(t.id) or '(no result)'}"
                       for t in wf.graph.tasks)


def staff_sheet(engine, limit: int = 30) -> str:
    """The roster as the overlord sees it: one line per usable model."""
    lines = []
    for ref in engine.roster.candidates(None)[:limit]:
        prov, _, model = ref.partition("/")
        tags = ", ".join(engine.providers[prov].models[model].tags) or "general"
        secs = engine.staff.seconds(ref)
        lines.append(f"- {ref} · {tags} · Elo {engine.staff.priority(ref):.0f} · "
                     + (f"{secs:.0f}s" if secs else "speed unknown"))
    return "\n".join(lines) or "(no free workers connected)"


def _parse_plan(text: str) -> TaskGraph:
    """Claude's plan → TaskGraph, after fixing the two slips that are safe to fix here:
    too many parts (keep the first MAX_TASKS) and dependencies on parts that don't exist."""
    data = extract_json(text)
    if isinstance(data, dict) and isinstance(data.get("tasks"), list):
        tasks = [t for t in data["tasks"] if isinstance(t, dict)][:MAX_TASKS]
        ids = {str(t.get("id", "")).strip() for t in tasks}
        for t in tasks:
            if isinstance(t.get("depends_on"), list):
                t["depends_on"] = [d for d in t["depends_on"] if str(d).strip() in ids]
        data["tasks"] = tasks
    return TaskGraph.model_validate(data)


async def _overlord_plan(engine, boss: managers.Manager, prompt: str) -> TaskGraph:
    schema = PLANNER_SYSTEM.replace('"tags": []}', '"tags": [], "worker": ""}')
    brief = f"{schema}\n\n{OVERLORD_SYSTEM.format(staff=staff_sheet(engine))}\n\nGOAL:\n{prompt}"
    text = await managers.ask(boss, brief)
    try:
        return _parse_plan(text)
    except (ValueError, ValidationError) as e:
        # A plan with one mistake (a cycle, a duplicate id) is still a plan: one second
        # chance with the exact error beats throwing the whole Claude call away
        text = await managers.ask(boss, f"{brief}\n\nYOUR LAST REPLY WAS REJECTED: {str(e)[:500]}\n"
                                        f"Reply again with ONLY the corrected JSON.\n\n"
                                        f"YOUR LAST REPLY:\n{text[:4000]}")
        return _parse_plan(text)


async def run_workflow(engine, prompt: str, emit: Emit | None = None, *,
                       overlord: bool = False) -> Workflow:
    wf = Workflow(prompt)
    wf.team = await managers.connected()
    # The overlord is the most senior manager signed in: Claude, else ChatGPT
    boss = min(wf.team, key=lambda m: m.rank) if overlord and wf.team else None
    await _emit(emit, {"type": "phase", "phase": "planning"})
    if overlord and boss is None:
        await _emit(emit, {"type": "note", "text": "No manager signed in, so the free staff runs this "
                                                  "one. Sign in Claude under Accounts to put it in charge."})
    if boss is not None:
        await _emit(emit, {"type": "working", "id": "manager", "role": "overlord", "worker": boss.name})
        try:
            wf.graph, planner = await _overlord_plan(engine, boss, prompt), boss.name
        except (RuntimeError, ValueError, ValidationError) as e:
            await _emit(emit, {"type": "task_failed", "id": "manager", "role": "overlord", "worker": boss.name,
                               "reason": f"{boss.name} couldn't plan ({str(e)[:80]}); free planners take over"})
    if wf.graph is None:
        planners = engine.chain(["planner", "reasoning", "long_context"])[:2]
        await _emit(emit, {"type": "working", "id": "manager", "role": "manager",
                           "worker": " vs ".join(planners)})
        wf.graph, planner = await plan(engine, prompt)
    wf.assigned = assign(engine, wf.graph)
    await _emit(emit, {"type": "plan", "planner": planner, "synthesis": wf.graph.synthesis,
                       "tasks": [{**t.model_dump(), "worker": wf.assigned[t.id]}
                                 for t in wf.graph.tasks]})

    await _emit(emit, {"type": "phase", "phase": "working"})
    await _run_tasks(engine, wf, wf.graph.tasks, emit)
    if not wf.results:
        raise NoModelAvailable("no worker could do any part of the task")

    await _emit(emit, {"type": "phase", "phase": "writing"})
    await _write(engine, wf, emit, boss)

    await _emit(emit, {"type": "phase", "phase": "reviewing"})
    reviewer = await _review(engine, wf, emit, boss)
    if wf.review is None:
        await _emit(emit, {"type": "phase", "phase": "done"})
        return wf  # the answer stands; only the review (and this round's Elo) is skipped

    # Self-correction: weak parts are redone once by someone else, then the answer is rewritten
    weak = [t for t in wf.graph.tasks if wf.review.scores.get(t.id, 10) < REDO_BELOW]
    if weak:
        first_scores = dict(wf.review.scores)
        await _emit(emit, {"type": "phase", "phase": "fixing"})
        await _emit(emit, {"type": "redo", "ids": [t.id for t in weak],
                           "scores": {t.id: first_scores[t.id] for t in weak}})
        feedback = {t.id: _redo_feedback(wf, t, first_scores[t.id]) for t in weak}
        avoid: dict[str, str] = {}
        for t in weak:
            before = wf.worker.get(t.id)
            if before:  # the round-one worker keeps its low score for Elo
                wf.round_one.append((before, first_scores[t.id]))
                avoid[t.id] = before
            wf.assigned[t.id] = next((r for r in sorted(engine.roster.candidates(t.tags or None)[:12],
                                                        key=lambda r: -engine.staff.priority(r))
                                      if r != before), before or "")
        await _run_tasks(engine, wf, weak, emit, avoid=avoid, feedback=feedback)
        await _emit(emit, {"type": "phase", "phase": "writing"})
        await _write(engine, wf, emit, boss)
        await _emit(emit, {"type": "phase", "phase": "reviewing"})
        reviewer = await _review(engine, wf, emit, boss) or reviewer
        if wf.review is None:
            wf.review = Review(summary="", scores=first_scores)

    changes = engine.staff.rate(score_workers(wf))
    await _emit(emit, {"type": "ratings", "changes": {r: round(d, 1) for r, d in changes.items()}})
    if wf.review.prayer.strip() and not (boss and boss.id == "claude"):  # Claude already ran it
        await _pray(engine, wf, emit)
    await _emit(emit, {"type": "phase", "phase": "done"})
    return wf


async def _pray(engine, wf: Workflow, emit: Emit | None) -> None:
    """The team has a question for Claude. Signed in to Claude: Nathan asks it directly
    (with web search) and revises the answer. Otherwise you carry it to claude.ai."""
    assert wf.review is not None
    engine.memory.add("prayer", wf.review.prayer.strip(), {"prompt": wf.prompt[:500]})
    claude = next((m for m in wf.team if m.id == managers.CLAUDE.id), None)
    if claude is not None:
        await _emit(emit, {"type": "working", "id": "prayer", "worker": claude.name, "role": "prayer"})
        try:
            reply = (await managers.ask(claude, prayer_message(wf), web=True)).strip()
            if not reply:
                raise RuntimeError("empty answer")
            await _emit(emit, {"type": "delivered", "id": "prayer", "worker": claude.name,
                               "role": "prayer", "text": reply, "seconds": None})
            await answer_prayer(engine, wf, reply, emit)
            return
        except (RuntimeError, NoModelAvailable) as e:
            await _emit(emit, {"type": "task_failed", "id": "prayer", "role": "prayer",
                               "worker": claude.name,
                               "reason": f"couldn't answer the prayer ({str(e)[:80]}); carry it yourself"})
    await _emit(emit, {"type": "prayer", "text": wf.review.prayer.strip(),
                       "message": prayer_message(wf)})


async def _review(engine, wf: Workflow, emit: Emit | None,
                  boss: managers.Manager | None = None) -> str | None:
    """Asked-vs-got review. Sets wf.review (None if no reviewer answered); returns the reviewer.
    The overlord, else your lead manager (Claude or ChatGPT) when signed in; else free judges."""
    brief = f"ASKED FOR:\n{wf.prompt}\n\nTEAM WORK:\n{_team_work(wf)}\n\nFINAL ANSWER:\n{wf.final}"
    for m in [boss] if boss else managers.lead_order(wf.team)[:1]:
        await _emit(emit, {"type": "working", "id": "reviewer", "worker": m.name, "role": "reviewer"})
        try:
            text = await managers.ask(m, REVIEWER_SYSTEM + "\n\n" + brief)
            wf.review = Review.model_validate(extract_json(text))
            await _emit(emit, {"type": "review", "reviewer": m.name, **wf.review.model_dump()})
            return m.name
        except (RuntimeError, ValueError, ValidationError) as e:
            await _emit(emit, {"type": "task_failed", "id": "reviewer", "role": "reviewer", "worker": m.name,
                               "reason": f"{m.name} couldn't review ({str(e)[:80]}); free judges take over"})
    # judges first, then any reasoning model: a review shouldn't fail because judges are busy
    judges = [r for r in dict.fromkeys(engine.judges() + engine.chain(["reasoning"])) if r != wf.writer]
    # two reviewers race; within the good judges, measured-fast ones go first
    reviewers = sorted(judges[:8], key=lambda r: engine.staff.seconds(r) or HEDGE_AFTER_S) + judges[8:]
    await _emit(emit, {"type": "working", "id": "reviewer", "worker": reviewers[0] if reviewers else "",
                       "role": "reviewer"})
    try:
        wf.review, reviewer = await race_json(engine.client, reviewers[:6], [
            {"role": "system", "content": REVIEWER_SYSTEM},
            {"role": "user", "content": f"ASKED FOR:\n{wf.prompt}\n\nTEAM WORK:\n{_team_work(wf)}"
                                        f"\n\nFINAL ANSWER:\n{wf.final}"}], Review, temperature=0.2)
    except NoModelAvailable:
        wf.review = None
        await _emit(emit, {"type": "task_failed", "id": "reviewer", "role": "reviewer"})
        return None
    await _emit(emit, {"type": "review", "reviewer": reviewer, **wf.review.model_dump()})
    return reviewer


def score_workers(wf: Workflow) -> dict[str, float]:
    """Review scores per worker (averaged if one worker did several parts); no-shows get 0."""
    got: dict[str, list[float]] = {}
    assert wf.review is not None
    for tid, ref in wf.worker.items():
        got.setdefault(ref, []).append(max(0.0, min(10.0, wf.review.scores.get(tid, 5.0))))
    for ref, score in wf.round_one:
        got.setdefault(ref, []).append(score)
    for ref in wf.slow:  # too slow to finish before a backup did: punished like a poor part
        if ref not in wf.worker.values():
            got.setdefault(ref, []).append(SLOW_SCORE)
    for refs in wf.no_shows.values():
        for ref in refs:
            got.setdefault(ref, []).append(0.0)
    return {ref: sum(v) / len(v) for ref, v in got.items()}


def prayer_message(wf: Workflow) -> str:
    """What you paste into claude.ai."""
    assert wf.review is not None
    return ("I'm running a team of free AI models (an app called Nathan). They worked on the "
            "request below and have a question for you.\n\n"
            f"REQUEST:\n{wf.prompt}\n\nTHEIR ANSWER SO FAR:\n{wf.final[:4000]}\n\n"
            f"THEIR QUESTION FOR YOU:\n{wf.review.prayer.strip()}\n")


async def answer_prayer(engine, wf: Workflow, reply: str, emit: Emit | None = None) -> str:
    """Claude answered: the writer revises the final answer with that reply."""
    res = await fallback(engine.client, engine.chain(["writing", "long_context"]), [
        {"role": "system", "content": WRITER_SYSTEM},
        {"role": "user", "content": f"ORIGINAL REQUEST:\n{wf.prompt}\n\nTHE TEAM'S ANSWER:\n"
                                    f"{wf.final}\n\nTHE TEAM ASKED CLAUDE (a stronger model):\n"
                                    f"{wf.review.prayer if wf.review else ''}\n\nCLAUDE REPLIED:\n"
                                    f"{reply}\n\nRewrite the final answer, taking Claude's reply "
                                    "into account."}])
    wf.final, wf.writer = res.text, res.ref
    engine.memory.add("blessing", reply[:4000], {"prompt": wf.prompt[:500]})
    await _emit(emit, {"type": "blessed", "text": wf.final, "writer": wf.writer})
    return wf.final
