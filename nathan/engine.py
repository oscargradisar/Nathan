"""The engine wires everything together: providers → quota → client → roster → patterns.

CLI commands and the web UI both talk to an Engine, so they behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nathan.client import Client, Message
from nathan.config import Provider, data_dir, load_providers
from nathan.errors import NathanError
from nathan.patterns import Candidate, fallback, race, vote
from nathan.quota import QuotaManager
from nathan.roster import Roster

MODES = ("auto", "single", "race", "vote", "critique", "plan")


@dataclass
class Answer:
    text: str
    mode: str
    models: list[str] = field(default_factory=list)   # models that produced the answer
    candidates: list[Candidate] = field(default_factory=list)
    judge: str | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class Engine:
    def __init__(self, providers: dict[str, Provider] | None = None, *,
                 client: Client | None = None, quota: QuotaManager | None = None,
                 data: Path | None = None) -> None:
        if client is not None:
            providers = client.providers
        self.providers = providers if providers is not None else load_providers()
        self.data = data or data_dir()
        self.quota = quota if quota is not None else QuotaManager(
            self.providers, self.data / "quota.sqlite")
        if client is None:
            client = Client(self.providers, quota=self.quota)
        elif client.quota is None:
            client.quota = self.quota
        self.client = client
        self.roster = Roster(self.providers, self.quota)

    async def close(self) -> None:
        await self.client.close()

    # ---- helpers -------------------------------------------------------------------------

    def chain(self, tags: list[str] | None = None, first: list[str] | None = None) -> list[str]:
        """A fallback chain: the given models first, then every usable model by tag fit."""
        out = list(first or [])
        for ref in self.roster.candidates(tags) + self.roster.candidates(None):
            if ref not in out:
                out.append(ref)
        return out

    def judges(self, exclude: list[str] | None = None) -> list[str]:
        j = [r for r in self.roster.candidates(["judge"]) if r not in (exclude or [])]
        return j + [r for r in self.roster.candidates(["judge"]) if r not in j]

    # ---- one question in, one answer out ----------------------------------------------------

    async def ask(self, question: str | list[Message], *, mode: str = "auto",
                  models: list[str] | None = None, n: int = 4, tags: list[str] | None = None,
                  extra: list[Candidate] | None = None, **kwargs: Any) -> Answer:
        messages = ([{"role": "user", "content": question}] if isinstance(question, str)
                    else question)
        on_event = kwargs.pop("on_event", None)  # progress callback (web UI), not for the API
        if mode == "single":
            res = await fallback(self.client, self.chain(tags, models), messages, **kwargs)
            return Answer(res.text, mode, [res.ref])
        if mode == "race":
            refs = models or self.roster.pick(tags or ["fast"], min(n, 3))
            res = await race(self.client, refs, messages, **kwargs)
            return Answer(res.text, mode, [res.ref])
        if mode == "vote":
            refs = models or self.roster.pick(tags or ["writing", "reasoning"], n)
            spares = [r for r in self.roster.candidates(tags or ["writing", "reasoning"])
                      if r not in refs]
            result = await vote(self.client, refs, messages, judges=self.judges(),
                                spares=spares, extra=extra, **kwargs)
            used = [c.ref for c in result.candidates if c.ok]
            return Answer(result.answer, mode, used, result.candidates, result.judge, result.note)
        if mode == "critique":
            from nathan.patterns.critique import critique

            drafters = self.chain(tags or ["writing"], models)
            critics = self.chain(["judge", "reasoning"])
            c = await critique(self.client, messages, drafters=drafters, critics=critics)
            return Answer(c.final, mode, [c.drafter, c.critic, c.reviser],
                          extra={"draft": c.draft, "critique": c.critique})
        if mode == "plan":
            from nathan.executor import execute
            from nathan.planner import plan

            goal = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "user")
            graph, planner_ref = await plan(self, goal)
            result = await execute(self, graph, on_event=on_event)
            used = sorted({r.model for r in result.results.values() if r.model})
            return Answer(result.answer, mode, used, judge=result.synthesizer,
                          extra={"planner": planner_ref, "graph": graph.model_dump(),
                                 "results": {k: v.__dict__ for k, v in result.results.items()},
                                 "max_parallel": result.max_parallel})
        if mode == "auto":
            from nathan.router import route

            question_text = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "user")
            r = await route(self, question_text)
            pattern = r.pattern
            if pattern == "agent" and not hasattr(self, "agent"):
                pattern = "single"
            ans = await self.ask(messages, mode=pattern, models=models, n=n,
                                 tags=tags or r.tags or None, extra=extra, on_event=on_event,
                                 **kwargs)
            ans.note = (ans.note + f" routed: {r.task_type} → {pattern} by {r.by}").strip()
            ans.extra["route"] = r.model_dump()
            return ans
        raise NathanError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
