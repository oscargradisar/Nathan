"""Fan-out and vote: ask several models, then a judge model merges the best answer.

Each "lane" asks one model. If a lane's model fails (quota, outage), the lane takes the
next spare model so you still get N opinions. Then a judge (a model tagged `judge`)
reads all answers and writes one merged answer, fixing mistakes it can spot.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from nathan.client import ChatResult, Client, Message
from nathan.errors import RECOVERABLE, NoModelAvailable
from nathan.patterns.fallback import fallback

JUDGE_SYSTEM = (
    "You are the judge in a panel of AI models. You receive a task and several candidate "
    "answers. Check them against each other, keep what is correct, drop what is wrong or "
    "unsupported, and write the single best final answer to the task. Answer the task "
    "directly: do not mention the candidates, the panel, or that you are a judge."
)


@dataclass
class Candidate:
    ref: str  # "provider/model", or "manual/claude.ai" for pasted answers
    text: str = ""
    error: str = ""
    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.error


@dataclass
class VoteResult:
    answer: str
    judge: str | None  # ref of the judge, None if no judging was needed or possible
    candidates: list[Candidate] = field(default_factory=list)
    note: str = ""


def judge_messages(task: str, candidates: list[Candidate]) -> list[Message]:
    parts = [f"TASK:\n{task}\n"]
    for i, c in enumerate(candidates):
        parts.append(f"--- CANDIDATE {chr(65 + i)} ---\n{c.text}\n")
    parts.append("Write the best final answer now.")
    return [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": "\n".join(parts)}]


def task_text(messages: list[Message]) -> str:
    return "\n\n".join(str(m["content"]) for m in messages if m["role"] in ("user", "system"))


async def run_lanes(client: Client, refs: list[str], messages: list[Message],
                    spares: list[str] | None = None, **kwargs: Any) -> list[Candidate]:
    pool = list(spares or [])

    async def lane(ref: str) -> Candidate:
        current: str | None = ref
        errors = []
        while current is not None:
            try:
                res: ChatResult = await client.chat(current, messages, **kwargs)
                if res.text:
                    return Candidate(current, res.text, latency_ms=res.latency_ms)
                errors.append(f"{current}: empty")
            except RECOVERABLE as e:
                errors.append(f"{current}: {type(e).__name__}")
            current = pool.pop(0) if pool else None  # take a spare model (shared pool)
        return Candidate(ref, error="; ".join(errors))

    return list(await asyncio.gather(*(lane(r) for r in refs)))


async def judge(client: Client, task: str, candidates: list[Candidate],
                judges: list[str]) -> VoteResult:
    good = [c for c in candidates if c.ok]
    if not good:
        raise NoModelAvailable("vote: every lane failed — " +
                               "; ".join(c.error for c in candidates))
    if len(good) == 1:
        return VoteResult(good[0].text, None, candidates, note="only one answer, no judging needed")
    try:
        res = await fallback(client, judges, judge_messages(task, good))
        return VoteResult(res.text, res.ref, candidates)
    except NoModelAvailable as e:
        best = max(good, key=lambda c: len(c.text))
        return VoteResult(best.text, None, candidates,
                          note=f"judge unavailable ({e}); showing the longest answer")


async def vote(client: Client, refs: list[str], messages: list[Message], *,
               judges: list[str], spares: list[str] | None = None,
               extra: list[Candidate] | None = None, **kwargs: Any) -> VoteResult:
    candidates = await run_lanes(client, refs, messages, spares, **kwargs)
    candidates += list(extra or [])
    return await judge(client, task_text(messages), candidates, judges)
