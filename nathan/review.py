"""Performance review: your managers judge the staff on what they actually delivered.

1. Nathan takes recent delivered work from the work log, a couple of samples per model,
   and hides who wrote what (workers become A, B, C...), so reputation can't sway anyone.
2. The assistant manager (ChatGPT) scores everyone first.
3. The manager (Claude) sees the same work plus that draft and has the final word.
4. Final scores move Elo at double weight, and each model keeps the manager's note.
If only one manager is signed in, that one does the whole review.
"""

from __future__ import annotations

import asyncio
import string
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nathan import managers
from nathan.jsonutil import extract_json
from nathan.staff import K

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
MANAGER_K = 2 * K  # a manager's verdict counts double

REVIEW_BRIEF = """You are {title} of Nathan, a team of free AI models. Below are samples of
real work by each worker, anonymised as letters. Judge each worker on accuracy, usefulness,
following the task, and clarity. Be fair and specific; one sentence of feedback each.
{draft}
Reply with ONLY JSON:
{{"summary": "2-3 sentences about the team",
  "ranking": [{{"worker": "A", "score": 8, "note": "one sentence"}}]}}
Scores are 1-10. Rank best first. Include every worker.
Note: each TASK is shown as a short excerpt, so evidence the workers were given may be cut off.
Don't penalise citations to sources you can't see; judge the answer itself.

WORK SAMPLES:
{samples}"""


class Entry(BaseModel):
    worker: str
    score: float
    note: str = ""


class Verdict(BaseModel):
    summary: str = ""
    ranking: list[Entry] = Field(default_factory=list)


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


def packet(samples: dict[str, list[dict]]) -> tuple[str, dict[str, str]]:
    """Anonymised work samples and the letter → model mapping."""
    letters = dict(zip(string.ascii_uppercase, samples, strict=False))
    parts = []
    for letter, ref in letters.items():
        for i, s in enumerate(samples[ref], 1):
            parts.append(f"=== WORKER {letter} · sample {i} ({s['role']}) ===\nTASK: {s['task'][:700]}"
                         f"\nOUTPUT: {s['output'][:1800]}")
    return "\n\n".join(parts), letters


async def _judge(m: managers.Manager, samples_text: str, draft: str, ask=managers.ask) -> Verdict:
    note = (f"\nYour assistant manager already drafted this review. Agree or overrule it:\n{draft}\n"
            if draft else "")
    text = await ask(m, REVIEW_BRIEF.format(title=m.title.lower(), draft=note, samples=samples_text))
    try:
        return Verdict.model_validate(extract_json(text))
    except (ValueError, ValidationError) as e:
        raise RuntimeError(f"{m.name} didn't return a readable review") from e


async def run_review(engine, emit: Emit | None = None, ask=managers.ask,
                     connected: list[managers.Manager] | None = None) -> dict:
    team = connected if connected is not None else await managers.connected()
    if not team:
        raise RuntimeError("No manager connected. Connect Claude, ChatGPT or Gemini under Accounts first.")
    samples = engine.staff.samples()
    if len(samples) < 2:
        raise RuntimeError("Not enough work to review yet: run a few workflows or research questions first.")
    text, letters = packet(samples)
    await _emit(emit, {"type": "phase", "phase": "reviewing"})

    draft, verdict, by = "", None, ""
    for m in reversed(managers.lead_order(team)):  # others draft first, the lead decides last
        unit = f"mgr:{m.id}"
        await _emit(emit, {"type": "working", "id": unit, "worker": m.name, "role": m.title.lower()})
        try:
            verdict = await _judge(m, text, draft, ask)
        except RuntimeError as e:
            await _emit(emit, {"type": "task_failed", "id": unit, "role": m.title.lower(), "reason": str(e),
                               "worker": m.name})
            continue
        by = m.name
        draft = verdict.model_dump_json(indent=1)
        await _emit(emit, {"type": "delivered", "id": unit, "worker": m.name, "role": m.title.lower(),
                           "text": verdict.summary, "seconds": None})
    if verdict is None:
        raise RuntimeError("No manager could finish the review.")

    scores: dict[str, float] = {}
    rows = []
    for e in verdict.ranking:
        ref = letters.get(e.worker.strip().upper()[:1])
        if not ref or ref in scores:
            continue
        score = max(1.0, min(10.0, e.score))
        scores[ref] = score
        engine.staff.set_verdict(ref, score, e.note, by)
        rows.append((ref, score, e.note))
    changes = engine.staff.rate(scores, k=MANAGER_K) if len(scores) > 1 else {}
    report = [f"**Performance review by {by}**", "", verdict.summary, "",
              "| # | Model | Score | Elo | Note |", "|---|---|---|---|---|"]
    for i, (ref, score, note) in enumerate(rows, 1):
        d = changes.get(ref, 0)
        report.append(f"| {i} | {ref} | {score:g}/10 | {'+' if d >= 0 else ''}{d:.0f} | {note} |")
    final = "\n".join(report)
    await _emit(emit, {"type": "ratings", "changes": {r: round(d, 1) for r, d in changes.items()}})
    await _emit(emit, {"type": "final", "text": final, "writer": by, "mode": "review"})
    return {"by": by, "scores": scores, "changes": changes, "summary": verdict.summary, "report": final}
