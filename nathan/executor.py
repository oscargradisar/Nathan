"""Tier 3 executor: run a task graph wave by wave, independent subtasks in parallel.

Each subtask gets its own fallback chain, rotated so that parallel subtasks start on
*different* providers (spreading load across free tiers). Results flow into the
subtasks that depend on them, then a synthesizer writes the final answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from nathan.errors import NoModelAvailable
from nathan.patterns import fallback
from nathan.planner import Subtask, TaskGraph

Event = dict[str, Any]
OnEvent = Callable[[Event], Awaitable[None] | None]

WORKER_SYSTEM = ("You are one worker in a team of AI models. Do ONLY the subtask you are "
                 "given, thoroughly and factually. Say so plainly if you are unsure of a fact.")
SYNTH_SYSTEM = ("You are the lead writer. Combine your team's results into the final answer "
                "for the goal. Resolve contradictions, remove repetition, keep it well "
                "structured. Answer directly; don't mention the team or subtasks.")


@dataclass
class SubtaskResult:
    id: str
    text: str = ""
    model: str = ""
    error: str = ""


@dataclass
class ExecResult:
    answer: str
    graph: TaskGraph
    results: dict[str, SubtaskResult] = field(default_factory=dict)
    synthesizer: str = ""
    max_parallel: int = 0  # widest wave actually run


async def _emit(on_event: OnEvent | None, event: Event) -> None:
    if on_event is not None:
        out = on_event(event)
        if asyncio.iscoroutine(out):
            await out


def _worker_messages(graph: TaskGraph, task: Subtask, results: dict[str, SubtaskResult]) -> list:
    parts = [f"OVERALL GOAL: {graph.goal}", f"YOUR SUBTASK: {task.instruction}"]
    for dep in task.depends_on:
        r = results.get(dep)
        if r and r.text:
            parts.append(f"--- RESULT OF {dep} ---\n{r.text}")
    return [{"role": "system", "content": WORKER_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


async def execute(engine, graph: TaskGraph, *, on_event: OnEvent | None = None,
                  concurrency: int = 6) -> ExecResult:
    results: dict[str, SubtaskResult] = {}
    sem = asyncio.Semaphore(concurrency)
    counter = 0
    widest = 0

    async def run_one(task: Subtask, slot: int) -> None:
        chain = engine.chain(task.tags or None)
        if chain:
            k = slot % len(chain)
            chain = chain[k:] + chain[:k]  # rotate: parallel tasks start on different models
        await _emit(on_event, {"type": "subtask_start", "id": task.id, "instruction": task.instruction})
        async with sem:
            try:
                res = await fallback(engine.client, chain, _worker_messages(graph, task, results))
                results[task.id] = SubtaskResult(task.id, res.text, res.ref)
            except NoModelAvailable as e:
                results[task.id] = SubtaskResult(task.id, error=str(e))
        r = results[task.id]
        await _emit(on_event, {"type": "subtask_done", "id": task.id, "model": r.model,
                               "text": r.text, "error": r.error})

    for wave in graph.waves():
        widest = max(widest, len(wave))
        slots = []
        for t in wave:
            slots.append(run_one(t, counter))
            counter += 1
        await asyncio.gather(*slots)

    done = [r for r in results.values() if r.text]
    if not done:
        raise NoModelAvailable("every subtask failed")
    body = "\n\n".join(f"--- {r.id}: {next(t.instruction for t in graph.tasks if t.id == r.id)} ---\n{r.text}"
                       for r in done)
    messages = [{"role": "system", "content": SYNTH_SYSTEM},
                {"role": "user", "content": f"GOAL: {graph.goal}\n\nHOW TO COMBINE: {graph.synthesis}"
                                            f"\n\nTEAM RESULTS:\n{body}"}]
    await _emit(on_event, {"type": "synthesis_start"})
    final = await fallback(engine.client, engine.chain(["writing", "long_context"]), messages)
    await _emit(on_event, {"type": "synthesis_done", "model": final.ref})
    return ExecResult(final.text, graph, results, final.ref, widest)
