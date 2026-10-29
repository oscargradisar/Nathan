"""Workflow: a manager splits your prompt into parts, named workers do the parts in
parallel, a writer turns their work into one answer, and a reviewer compares what you
asked for with what you got.

Every step is announced as an event, so the Workflow page can show who's working on
what. The reviewer's scores feed the staff Elo ladder (staff.py). If the team is stuck,
the reviewer can write a prayer to Claude: you approve it, paste it into claude.ai
yourself (there is no free Claude API), and paste the reply back for a revised answer.

Events: phase, plan, assigned, working, reassigned, delivered, task_failed, final,
review, ratings, prayer, blessed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from nathan.errors import RECOVERABLE, NoModelAvailable
from nathan.jsonutil import ask_json
from nathan.patterns import fallback
from nathan.planner import Subtask, TaskGraph, plan

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
MAX_TRIES = 5  # workers tried per task before giving up on it

WORKER_SYSTEM = ("You are one worker on a team of AI models. Do ONLY the part you are given, "
                 "thoroughly and factually. Say plainly if you are unsure of a fact.")
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


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


def assign(engine, graph: TaskGraph) -> dict[str, str]:
    """Give each task a worker: the best-rated model that fits its tags, spread so parallel
    tasks in a wave go to different models (and different providers when possible)."""
    out: dict[str, str] = {}
    for wave in graph.waves():
        busy: set[str] = set()
        for task in wave:
            fits = engine.roster.candidates(task.tags or None)[:12] or engine.roster.candidates(None)
            if not fits:
                raise NoModelAvailable("no workers available: connect a provider first")
            fits.sort(key=lambda r: -engine.staff.elo(r))
            free = [r for r in fits if r not in busy]
            other_provider = [r for r in free if r.split("/")[0] not in {b.split("/")[0] for b in busy}]
            out[task.id] = (other_provider or free or fits)[0]
            busy.add(out[task.id])
    return out


def _worker_messages(wf: Workflow, task: Subtask) -> list:
    parts = [f"THE PERSON'S REQUEST: {wf.prompt}", f"YOUR PART: {task.instruction}"]
    for dep in task.depends_on:
        if wf.results.get(dep):
            parts.append(f"--- WORK FROM {dep} ---\n{wf.results[dep]}")
    return [{"role": "system", "content": WORKER_SYSTEM},
            {"role": "user", "content": "\n\n".join(parts)}]


async def _do_task(engine, wf: Workflow, task: Subtask, emit: Emit | None) -> None:
    first = wf.assigned[task.id]
    chain = [first] + [r for r in engine.chain(task.tags or None) if r != first]
    wf.no_shows[task.id] = []
    for i, ref in enumerate(chain[:MAX_TRIES]):
        await _emit(emit, {"type": "working", "id": task.id, "worker": ref})
        start = time.perf_counter()
        try:
            res = await engine.client.chat(ref, _worker_messages(wf, task))
            reason = "" if res.text else "empty answer"
        except RECOVERABLE as e:
            reason = type(e).__name__
        if not reason:
            ms = (time.perf_counter() - start) * 1000
            engine.staff.record(ref, ok=True, ms=ms)
            wf.worker[task.id], wf.results[task.id] = ref, res.text
            await _emit(emit, {"type": "delivered", "id": task.id, "worker": ref,
                               "text": res.text, "seconds": round(ms / 1000, 1)})
            return
        if reason != "QuotaExceeded":  # out of free quota isn't the worker's fault: no penalty
            engine.staff.record(ref, ok=False)
            wf.no_shows[task.id].append(ref)
        nxt = chain[i + 1] if i + 1 < min(len(chain), MAX_TRIES) else None
        if nxt:
            await _emit(emit, {"type": "reassigned", "id": task.id, "from": ref, "to": nxt,
                               "reason": reason})
    await _emit(emit, {"type": "task_failed", "id": task.id})


def _team_work(wf: Workflow) -> str:
    assert wf.graph is not None
    return "\n\n".join(f"--- {t.id} ({t.instruction}) ---\n{wf.results.get(t.id) or '(no result)'}"
                       for t in wf.graph.tasks)


async def run_workflow(engine, prompt: str, emit: Emit | None = None) -> Workflow:
    wf = Workflow(prompt)
    await _emit(emit, {"type": "phase", "phase": "planning"})
    wf.graph, planner = await plan(engine, prompt)
    wf.assigned = assign(engine, wf.graph)
    await _emit(emit, {"type": "plan", "planner": planner, "synthesis": wf.graph.synthesis,
                       "tasks": [{**t.model_dump(), "worker": wf.assigned[t.id]}
                                 for t in wf.graph.tasks]})

    await _emit(emit, {"type": "phase", "phase": "working"})
    for wave in wf.graph.waves():
        await asyncio.gather(*(_do_task(engine, wf, t, emit) for t in wave))
    if not wf.results:
        raise NoModelAvailable("no worker could do any part of the task")

    await _emit(emit, {"type": "phase", "phase": "writing"})
    writers = engine.chain(["writing", "long_context"])
    res = await fallback(engine.client, writers, [
        {"role": "system", "content": WRITER_SYSTEM},
        {"role": "user", "content": f"ORIGINAL REQUEST:\n{prompt}\n\nHOW TO COMBINE: "
                                    f"{wf.graph.synthesis}\n\nTEAM WORK:\n{_team_work(wf)}"}])
    wf.final, wf.writer = res.text, res.ref
    await _emit(emit, {"type": "final", "text": wf.final, "writer": wf.writer})

    await _emit(emit, {"type": "phase", "phase": "reviewing"})
    reviewers = [r for r in engine.judges() if r != wf.writer] or engine.chain(["reasoning"])
    try:
        wf.review, reviewer = await ask_json(engine.client, reviewers[:4], [
            {"role": "system", "content": REVIEWER_SYSTEM},
            {"role": "user", "content": f"ASKED FOR:\n{prompt}\n\nTEAM WORK:\n{_team_work(wf)}"
                                        f"\n\nFINAL ANSWER:\n{wf.final}"}], Review, temperature=0.2)
    except NoModelAvailable:
        await _emit(emit, {"type": "phase", "phase": "done"})
        return wf  # the answer stands; only the review (and this round's Elo) is skipped
    await _emit(emit, {"type": "review", "reviewer": reviewer, **wf.review.model_dump()})

    changes = engine.staff.rate(score_workers(wf))
    await _emit(emit, {"type": "ratings", "changes": {r: round(d, 1) for r, d in changes.items()}})
    if wf.review.prayer.strip():
        await _emit(emit, {"type": "prayer", "text": wf.review.prayer.strip(),
                           "message": prayer_message(wf)})
    await _emit(emit, {"type": "phase", "phase": "done"})
    return wf


def score_workers(wf: Workflow) -> dict[str, float]:
    """Review scores per worker (averaged if one worker did several parts); no-shows get 0."""
    got: dict[str, list[float]] = {}
    assert wf.review is not None
    for tid, ref in wf.worker.items():
        got.setdefault(ref, []).append(max(0.0, min(10.0, wf.review.scores.get(tid, 5.0))))
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
    await _emit(emit, {"type": "blessed", "text": wf.final, "writer": wf.writer})
    return wf.final
