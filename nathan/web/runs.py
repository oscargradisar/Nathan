"""A "run" is one task executing in the background, broadcasting events to the browser.

Why server-sent events (SSE)? The browser opens one long HTTP response and the server
writes `data: {...}` lines into it as things happen. It's simpler than WebSockets and
perfect for one-way "here's the next piece of text" streaming.

Events (JSON):
  {"type": "lanes", "lanes": [{"id": 0, "model": "groq/..."}]}
  {"type": "delta", "lane": 0, "text": "..."}                 streamed text
  {"type": "lane_switch", "lane": 0, "model": "...", "reason": "QuotaExceeded"}
  {"type": "lane_done", "lane": 0, "ok": true, "ms": 812, "error": ""}
  {"type": "judge_start", "model": "..."}  {"type": "judge_delta", "text": "..."}
  {"type": "subtask_start"/"subtask_done", ...}                plan mode
  {"type": "final", "text": "...", "judge": "...", "note": ""}
  {"type": "error", "message": "..."}   {"type": "done"}
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from nathan.client import strip_thinking
from nathan.errors import RECOVERABLE, NathanError, NoModelAvailable
from nathan.patterns.vote import Candidate, judge_messages

Event = dict[str, Any]
SWARM_MAX = 12  # swarm mode: at most this many models answer at once


def diagnostics_report(results: list[dict], listed: dict[str, list[str]]) -> str:
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    lines = [f"{len(ok)} of {len(results)} models answered."]
    if ok:
        fast = ok[0]
        lines.append(f"Fastest: {fast['ref']} in {fast['seconds']}s.")
    if bad:
        lines.append("Didn't answer: " + ", ".join(f"{r['ref']} ({r['reason']})" for r in bad) + ".")
    if listed.get("retired"):
        lines.append("Retired (no longer offered): " + ", ".join(listed["retired"]) + ".")
    if listed.get("reinstated"):
        lines.append("Back on staff: " + ", ".join(listed["reinstated"]) + ".")
    return "\n".join(lines)


@dataclass
class Run:
    id: str
    prompt: str
    mode: str
    events: list[Event] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    final: str = ""
    finished: bool = False
    workflow: Any = None  # nathan.workflow.Workflow, kept so a prayer can be answered later
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _task: asyncio.Task | None = None

    def emit(self, event: Event) -> None:
        self.events.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    async def subscribe(self, since: int = 0):
        """Replay events from index `since`, then follow live ones until "done"."""
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events[since:]:
            q.put_nowait(e)
        self._subscribers.append(q)
        try:
            while True:
                event = await q.get()
                yield event
                if event["type"] == "done" and q.empty():
                    break
        finally:
            self._subscribers.remove(q)


class RunManager:
    def __init__(self, engine, keep: int = 50) -> None:
        self.engine = engine
        self.runs: dict[str, Run] = {}
        self.keep = keep

    def get(self, run_id: str) -> Run | None:
        return self.runs.get(run_id)

    def start(self, prompt: str, mode: str = "vote", models: list[str] | None = None,
              n: int = 5, images: list | None = None) -> Run:
        run = Run(uuid.uuid4().hex[:10], prompt, mode)
        self.runs[run.id] = run
        if len(self.runs) > self.keep:
            self.runs.pop(next(iter(self.runs)))
        run._task = asyncio.create_task(self._execute(run, models, n, images or []))
        return run

    async def _see(self, run: Run, images: list) -> None:
        """Dropped images: one model with eyes describes each (in parallel), then the task
        runs on the request plus those descriptions."""
        from nathan import vision

        async def one(i: int, path) -> tuple[str, str]:
            uid = f"eyes{i}"
            run.emit({"type": "working", "id": uid, "worker": "looking", "role": f"eyes · image {i}"})
            text, who = await vision.look(self.engine, path, run.prompt)
            run.emit({"type": "delivered", "id": uid, "worker": who, "role": "eyes", "text": text})
            return text, who

        try:
            seen = await asyncio.gather(*(one(i, p) for i, p in enumerate(images, 1)))
        except RuntimeError as e:
            raise NoModelAvailable(str(e)) from e
        run.prompt = vision.brief(run.prompt, list(seen))

    async def _execute(self, run: Run, models: list[str] | None, n: int, images: list | None = None) -> None:
        try:
            if images:
                await self._see(run, images)
            mode = run.mode
            if mode == "auto":
                from nathan.router import route

                r = await route(self.engine, run.prompt)
                mode = {"plan": "workflow", "agent": "autopilot"}.get(r.pattern, r.pattern)
                run.emit({"type": "routed", "pattern": mode, "task_type": r.task_type,
                          "reason": r.reason, "by": r.by})
            if mode == "swarm":  # everyone who can answer, at once
                n = len({ref.split("/")[0] for ref in self.engine.roster.candidates(None)})
                await self._vote(run, models, max(3, min(n * 2, SWARM_MAX)), distinct=False)
            elif mode == "vote":
                await self._vote(run, models, n)
            elif mode == "workflow":
                from nathan.workflow import run_workflow

                run.workflow = await run_workflow(self.engine, run.prompt, run.emit)
                run.final = run.workflow.final
                self.engine.memory.log_exchange(run.prompt, run.final, {"mode": "workflow"})
            elif mode == "review":
                from nathan.review import run_review

                result = await run_review(self.engine, run.emit)
                run.final = result["report"]
            elif mode == "diagnostics":
                from nathan.health import diagnostics, roll_call

                listed = await roll_call(self.engine)
                results = await diagnostics(self.engine, run.emit)
                run.final = diagnostics_report(results, listed)
                run.emit({"type": "final", "text": run.final, "mode": "diagnostics",
                          "results": results, "roll_call": listed})
            else:
                ans = await self.engine.ask(run.prompt, mode=mode, models=models, n=n,
                                            on_event=run.emit)
                run.final = ans.text
                run.emit({"type": "final", "text": ans.text, "judge": ans.judge,
                          "models": ans.models, "note": ans.note, "mode": ans.mode})
        except NathanError as e:
            run.emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        except Exception as e:  # noqa: BLE001 - surface unexpected errors in the UI too
            run.emit({"type": "error", "message": f"internal error: {type(e).__name__}: {e}"})
        finally:
            run.finished = True
            run.emit({"type": "done"})

    # ---- streaming vote -----------------------------------------------------------------

    async def _vote(self, run: Run, models: list[str] | None, n: int, distinct: bool = True) -> None:
        roster = self.engine.roster
        tags = ["writing", "reasoning"]
        refs = models or (roster.pick(tags, n) if distinct else
                          [r for r in roster.candidates(tags) + roster.candidates(None)
                           if r][: n * 2])
        refs = list(dict.fromkeys(refs))[:n]
        if not refs:
            raise NoModelAvailable("no models available — connect a provider first")
        spares = [r for r in roster.candidates(tags) + roster.candidates(None) if r not in refs]
        messages = [{"role": "user", "content": run.prompt}]
        context = self.engine.memory.context_for(run.prompt)
        if context:
            messages.insert(0, {"role": "system", "content": context})
        run.emit({"type": "lanes", "lanes": [{"id": i, "model": r} for i, r in enumerate(refs)]})

        async def lane(i: int, ref: str) -> Candidate:
            current: str | None = ref
            while current is not None:
                start = time.perf_counter()
                parts: list[str] = []
                try:
                    async for piece in self.engine.client.stream(current, messages):
                        parts.append(piece)
                        run.emit({"type": "delta", "lane": i, "text": piece})
                    text = strip_thinking("".join(parts))
                    if text:
                        ms = (time.perf_counter() - start) * 1000
                        run.emit({"type": "lane_done", "lane": i, "ok": True, "ms": round(ms),
                                  "model": current})
                        return Candidate(current, text, latency_ms=ms)
                    reason = "empty answer"
                except RECOVERABLE as e:
                    reason = type(e).__name__
                current = spares.pop(0) if spares else None
                if current:
                    run.emit({"type": "lane_switch", "lane": i, "model": current, "reason": reason})
                else:
                    run.emit({"type": "lane_done", "lane": i, "ok": False, "error": reason})
            return Candidate(ref, error="no model could answer")

        run.candidates = list(await asyncio.gather(*(lane(i, r) for i, r in enumerate(refs))))
        await self.judge(run)
        self.engine.memory.log_exchange(run.prompt, run.final, {"mode": "vote"})

    async def judge(self, run: Run) -> None:
        good = [c for c in run.candidates if c.ok]
        if not good:
            raise NoModelAvailable("every lane failed")
        if len(good) == 1:
            run.final = good[0].text
            run.emit({"type": "final", "text": run.final, "judge": None,
                      "note": "only one answer, no judging needed"})
            return
        messages = judge_messages(run.prompt, good)
        used = {c.ref for c in good}
        judges = self.engine.judges()
        judges = [j for j in judges if j not in used] + [j for j in judges if j in used]
        for ref in judges:
            run.emit({"type": "judge_start", "model": ref})
            parts: list[str] = []
            try:
                async for piece in self.engine.client.stream(ref, messages):
                    parts.append(piece)
                    run.emit({"type": "judge_delta", "text": piece})
            except RECOVERABLE as e:
                run.emit({"type": "judge_failed", "model": ref, "reason": type(e).__name__})
                continue
            text = "".join(parts).strip()
            if text:
                run.final = text
                run.emit({"type": "final", "text": text, "judge": ref, "note": ""})
                return
        best = max(good, key=lambda c: len(c.text))
        run.final = best.text
        run.emit({"type": "final", "text": best.text, "judge": None,
                  "note": "no judge available; showing the longest answer"})

    async def add_manual(self, run: Run, source: str, text: str) -> None:
        cand = Candidate(f"manual/{source}", text.strip())
        run.candidates.append(cand)
        run.emit({"type": "manual_added", "model": cand.ref, "text": cand.text})

    async def answer_prayer(self, run: Run, reply: str) -> None:
        from nathan.workflow import answer_prayer

        run.finished = False
        try:
            run.final = await answer_prayer(self.engine, run.workflow, reply, run.emit)
        except NathanError as e:
            run.emit({"type": "error", "message": str(e)})
        finally:
            run.finished = True
            run.emit({"type": "done"})

    async def rejudge(self, run: Run) -> None:
        run.finished = False
        try:
            await self.judge(run)
        except NathanError as e:
            run.emit({"type": "error", "message": str(e)})
        finally:
            run.finished = True
            run.emit({"type": "done"})
