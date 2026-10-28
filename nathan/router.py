"""Tier 3 router: a small, fast model labels the task and picks a pattern.

Routing costs one cheap request, and saves expensive ones: a greeting doesn't need a
5-model vote, and a research brief shouldn't be answered in one shot. If the router
model is unavailable or talks nonsense, a keyword heuristic decides instead.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from nathan.errors import NoModelAvailable
from nathan.jsonutil import ask_json

TaskType = Literal["chat", "factual", "code", "math", "writing", "research", "analysis"]
Pattern = Literal["single", "race", "vote", "critique", "plan", "agent"]

# Which roster tags suit each task type (tags themselves live in providers.yaml)
TAGS_FOR: dict[str, list[str]] = {
    "chat": ["fast"], "factual": ["reasoning"], "code": ["code", "reasoning"],
    "math": ["reasoning"], "writing": ["writing"], "research": ["writing", "reasoning"],
    "analysis": ["reasoning", "long_context"],
}

ROUTER_SYSTEM = """You route tasks for a team of AI models. Reply with ONLY a JSON object:
{"task_type": one of ["chat","factual","code","math","writing","research","analysis"],
 "pattern": one of ["single","race","vote","critique","plan","agent"],
 "reason": "<10 words"}
Patterns:
- single: quick, simple or conversational requests.
- race: short factual lookups where speed matters.
- vote: questions with one right answer where mistakes are likely (math, tricky facts, code bugs).
- critique: a piece of writing or code that benefits from a review-and-revise pass.
- plan: big multi-part jobs (research + write a brief/report, compare many things).
- agent: needs files in the workspace, running Python, or fetching a web page."""


class Route(BaseModel):
    task_type: TaskType = "chat"
    pattern: Pattern = "single"
    reason: str = ""
    tags: list[str] = Field(default_factory=list)
    by: str = "heuristic"  # which model routed, or "heuristic"


def heuristic_route(question: str) -> Route:
    q = question.lower()
    words = len(q.split())

    def has(*keys: str) -> bool:
        return any(re.search(rf"\b{k}", q) for k in keys)

    if has("file", "folder", "workspace", "run python", "execute", "fetch", "http", "url"):
        r = Route(task_type="analysis", pattern="agent")
    elif has("research", "brief", "report", "compare", "overview", "survey") and words > 5:
        r = Route(task_type="research", pattern="plan")
    elif has("code", "function", "bug", "python", "javascript", "sql", "regex"):
        r = Route(task_type="code", pattern="vote")
    elif has("calculate", "solve", "how many", "probability", "equation") or re.search(r"\d+\s*[-+*/^]\s*\d+", q):
        r = Route(task_type="math", pattern="vote")
    elif has("write", "draft", "essay", "email", "letter", "poem", "story", "rewrite", "edit"):
        r = Route(task_type="writing", pattern="critique")
    elif words <= 6:
        r = Route(task_type="chat", pattern="single")
    else:
        r = Route(task_type="factual", pattern="single")
    r.reason = "keyword heuristic"
    r.tags = TAGS_FOR[r.task_type]
    return r


async def route(engine, question: str) -> Route:  # engine: nathan.engine.Engine
    refs = engine.roster.pick(["cheap", "fast"], 2)
    if not refs:
        return heuristic_route(question)
    messages = [{"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": f"TASK:\n{question[:4000]}"}]
    try:
        r, ref = await ask_json(engine.client, refs, messages, Route, max_tokens=200, temperature=0)
    except NoModelAvailable:
        return heuristic_route(question)
    r.by = ref
    r.tags = TAGS_FOR.get(r.task_type, [])
    return r
