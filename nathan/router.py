"""Tier 3 router: a small, fast model labels the task and picks a pattern.

Routing costs one cheap request, and saves expensive ones: a greeting doesn't need a
5-model vote, and a research brief shouldn't be answered in one shot. If the router
model is unavailable or talks nonsense, a keyword heuristic decides instead.
"""

from __future__ import annotations

import asyncio
import re
from typing import Literal

from pydantic import BaseModel, Field

from nathan.errors import NoModelAvailable
from nathan.jsonutil import race_json

TaskType = Literal["chat", "factual", "code", "math", "writing", "research", "analysis"]
Pattern = Literal["single", "race", "vote", "critique", "plan", "agent", "research", "code"]

ROUTE_TIMEOUT_S = 5.0
ROUTER_LANES = 3  # routers asked at once; the first valid answer wins

# Things only the agent can do on this computer. Deliberately narrow: words like "tell",
# "show me" or "run" start ordinary questions too ("tell me about the French revolution").
_AGENT_VERBS = re.compile(r"^\s*(please\s+|can you\s+|could you\s+)?"
                          r"(open|launch|play|message|text|dm|send|email|fly to|zoom)\b")
# School work lives behind the Canvas tool. Plain "assignment" or "quiz" is not enough:
# "variable assignment in Python" or "write a quiz about WW2" are ordinary questions.
_SCHOOL = re.compile(r"\b(canvas|homework|pre-?lab|syllabus)\b"
                     r"|\bmy\s+(\w+\s+)?(assignments?|quiz(zes)?)\b", re.IGNORECASE)

# Which roster tags suit each task type (tags themselves live in providers.yaml)
TAGS_FOR: dict[str, list[str]] = {
    "chat": ["fast"], "factual": ["reasoning"], "code": ["code", "reasoning"],
    "math": ["reasoning"], "writing": ["writing"], "research": ["writing", "reasoning"],
    "analysis": ["reasoning", "long_context"],
}

ROUTER_SYSTEM = """You route tasks for a team of AI models. Reply with ONLY a JSON object:
{"task_type": one of ["chat","factual","code","math","writing","research","analysis"],
 "pattern": one of ["single","race","vote","critique","plan","agent","research","code"],
 "reason": "<10 words"}
Patterns:
- single: quick, simple or conversational requests.
- race: short factual lookups where speed matters.
- vote: questions with one right answer where mistakes are likely (math, tricky facts, code bugs).
- critique: a piece of writing or code that benefits from a review-and-revise pass.
- plan: big multi-part jobs (research + write a brief/report, compare many things).
- agent: needs files in the workspace, running Python, fetching a web page, or doing something on
  this computer: opening/launching a game, app or website ("open Rhythia", "play Terraria"), or
  messaging someone on Discord or by email ("message Sam on discord saying ..."), or school
  work on Canvas ("pull up my pre-lab assignment").
- research: factual questions that need real sources: niche facts, specifics, history, science,
  "who/what/when/how many", anything a model might misremember. Prefer this for factual questions.
- code: write a program, script, app or tool (not a quick snippet or a bug question)."""


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

    if _AGENT_VERBS.match(q) or has(
            "file", "folder", "workspace", "run python", "execute", "fetch", "http", "url"):
        r = Route(task_type="analysis", pattern="agent")
    elif has("research", "brief", "report", "compare", "overview", "survey") and words > 5:
        r = Route(task_type="research", pattern="plan")
    elif has("write a program", "build an app", "write a script", "make a tool", "create an app"):
        r = Route(task_type="code", pattern="code")
    elif has("code", "function", "bug", "python", "javascript", "sql", "regex"):
        r = Route(task_type="code", pattern="vote")
    elif has("calculate", "solve", "how many", "probability", "equation") or re.search(r"\d+\s*[-+*/^]\s*\d+", q):
        r = Route(task_type="math", pattern="vote")
    elif has("write", "draft", "essay", "email", "letter", "poem", "story", "rewrite", "edit"):
        r = Route(task_type="writing", pattern="critique")
    elif words <= 6:
        r = Route(task_type="chat", pattern="single")
    else:
        r = Route(task_type="factual", pattern="research")
    r.reason = "keyword heuristic"
    r.tags = TAGS_FOR[r.task_type]
    return r


async def run_mode(r: Route) -> str:
    """The run mode for a route. Big multi-part jobs go to the Overlord when a manager is
    signed in to lead them; otherwise to the free staff's own workflow."""
    from nathan import managers

    if r.pattern == "plan":
        return "overlord" if await managers.connected() else "workflow"
    return {"agent": "autopilot"}.get(r.pattern, r.pattern)


async def route(engine, question: str) -> Route:  # engine: nathan.engine.Engine
    # Routing is pure overhead to the person waiting, so the quickest models do it, and if
    # they dawdle the keyword rules decide instantly instead.
    if _SCHOOL.search(question):
        # School work lives behind the Canvas tool: go straight to the agent, no routing hop
        return Route(task_type="analysis", pattern="agent", reason="Canvas request", by="shortcut")
    refs = engine.fastest(["cheap", "fast"])[:ROUTER_LANES]
    if not refs:
        return heuristic_route(question)
    messages = [{"role": "system", "content": ROUTER_SYSTEM},
                {"role": "user", "content": f"TASK:\n{question[:4000]}"}]
    try:
        # All routers start at once, so one slow model can't eat the whole routing budget.
        # temperature=0 keeps the answer stable, so the response cache can reuse it.
        r, ref = await asyncio.wait_for(
            race_json(engine.client, refs, messages, Route, lanes=len(refs),
                      max_tokens=300, temperature=0), ROUTE_TIMEOUT_S)
    except (NoModelAvailable, TimeoutError):
        return heuristic_route(question)
    r.by = ref
    r.tags = TAGS_FOR.get(r.task_type, [])
    return r
