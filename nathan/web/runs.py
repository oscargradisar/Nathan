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
CHUNK_CHARS = 200  # streamed text is sent in pieces of about this size...
CHUNK_S = 0.05  # ...or this often, whichever comes first
HEARTBEAT_S = 15.0  # an idle SSE stream sends a comment this often so it doesn't look dead


class Chunker:
    """Groups streamed tokens into fewer, bigger events.

    Why: one event per token means tens of thousands of stored dicts on a swarm run and a
    browser repaint per token. ~50 ms batches look just as live to a person.
    """

    def __init__(self, emit, event: Event) -> None:
        self.emit, self.event = emit, event
        self.parts: list[str] = []
        self.size, self.last = 0, time.monotonic()

    def add(self, piece: str) -> None:
        self.parts.append(piece)
        self.size += len(piece)
        if self.size >= CHUNK_CHARS or time.monotonic() - self.last >= CHUNK_S:
            self.flush()

    def flush(self) -> None:
        if self.parts:
            self.emit({**self.event, "text": "".join(self.parts)})
        self.parts, self.size, self.last = [], 0, time.monotonic()


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
    job: dict | None = None  # the Jobs-page job this run belongs to (keeps its memory apart)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    _task: asyncio.Task | None = None

    def emit(self, event: Event) -> None:
        self.events.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    async def subscribe(self, since: int = 0, heartbeat: float | None = None):
        """Replay events from index `since`, then follow live ones until "done".

        With `heartbeat`, yields None after that many idle seconds (the caller sends a ping)."""
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events[max(0, since):]:
            q.put_nowait(e)
        self._subscribers.append(q)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), heartbeat)
                except TimeoutError:
                    yield None
                    continue
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

    def stop(self, run_id: str) -> bool:
        """Stop a run in progress. Returns False if it already finished."""
        run = self.runs.get(run_id)
        if run is None or run.finished or run._task is None:
            return False
        run._task.cancel()
        return True

    def start(self, prompt: str, mode: str = "vote", models: list[str] | None = None,
              n: int = 5, images: list | None = None, job: dict | None = None) -> Run:
        run = Run(uuid.uuid4().hex[:10], prompt, mode)
        run.job = job
        self.runs[run.id] = run
        if len(self.runs) > self.keep:  # forget the oldest finished run; never a live one
            old = next((k for k, r in self.runs.items() if r.finished), None)
            if old:
                self.runs.pop(old)
        run._task = asyncio.create_task(self._execute(run, models, n, images or [], job))
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

    async def _execute(self, run: Run, models: list[str] | None, n: int, images: list | None = None,
                       job: dict | None = None) -> None:
        from nathan import jobs

        jobs.current.set(job)  # this task and everything it starts now belong to the job
        if job:
            run.prompt = f"{run.prompt}\n\n({jobs.brief(job)})"
        try:
            if images:
                await self._see(run, images)
            mode = run.mode
            if mode == "auto":
                from nathan.router import route, run_mode

                r = await route(self.engine, run.prompt)
                mode = await run_mode(r)
                run.emit({"type": "routed", "pattern": mode, "task_type": r.task_type,
                          "reason": r.reason, "by": r.by})
            if mode == "swarm":  # everyone who can answer, at once
                n = len({ref.split("/")[0] for ref in self.engine.roster.candidates(None)})
                await self._vote(run, models, max(3, min(n * 2, SWARM_MAX)), distinct=False)
            elif mode == "vote":
                await self._vote(run, models, n)
            elif mode in ("workflow", "overlord"):
                from nathan.workflow import run_workflow

                run.workflow = await run_workflow(self.engine, run.prompt, run.emit,
                                                  overlord=mode == "overlord")
                run.final = run.workflow.final
                self.engine.memory.log_exchange(run.prompt, run.final, {"mode": mode})
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
        except asyncio.CancelledError:  # Stop pressed: lanes awaiting under this task are cancelled too
            run.emit({"type": "stopped"})
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
                out = Chunker(run.emit, {"type": "delta", "lane": i})
                try:
                    async for piece in self.engine.client.stream(current, messages):
                        parts.append(piece)
                        out.add(piece)
                    out.flush()
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
            out = Chunker(run.emit, {"type": "judge_delta"})
            try:
                async for piece in self.engine.client.stream(ref, messages):
                    parts.append(piece)
                    out.add(piece)
                out.flush()
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

    def answer_prayer(self, run: Run, reply: str) -> int:
        """Start revising with Claude's reply. Returns the event index to follow from."""
        from nathan.workflow import answer_prayer

        async def work() -> None:
            run.final = await answer_prayer(self.engine, run.workflow, reply, run.emit)

        return self._follow_up(run, work())

    def rejudge(self, run: Run) -> int:
        """Start judging again (e.g. after a pasted answer). Returns the index to follow from."""
        return self._follow_up(run, self.judge(run))

    def _follow_up(self, run: Run, work) -> int:
        """More work on a finished run. It becomes run._task, so Stop cancels it and the
        task has a reference (asyncio may garbage-collect unreferenced tasks mid-run)."""
        since = len(run.events)
        run.finished = False

        async def go() -> None:
            try:
                await work
            except asyncio.CancelledError:
                run.emit({"type": "stopped"})
            except NathanError as e:
                run.emit({"type": "error", "message": str(e)})
            except Exception as e:  # noqa: BLE001 - as in _execute: show it, don't end silently
                run.emit({"type": "error", "message": f"internal error: {type(e).__name__}: {e}"})
            finally:
                run.finished = True
                run.emit({"type": "done"})

        run._task = asyncio.create_task(go())
        return since
