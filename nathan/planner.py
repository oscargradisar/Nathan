"""Tier 3 planner: turn a big task into a small graph of subtasks (JSON → Pydantic).

A task graph is a list of subtasks; each may depend on others. Subtasks with no
unfinished dependencies run in parallel ("waves"), so independent research questions
go to different providers at the same time.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

from nathan.jsonutil import ask_json

MAX_TASKS = 10

PLANNER_SYSTEM = f"""You are the planner for a team of AI workers that run in parallel.
Break the user's goal into 3-{MAX_TASKS} subtasks. Prefer INDEPENDENT subtasks (empty
depends_on) so they can run at the same time; only add a dependency when a subtask truly
needs another's output. Each instruction must be self-contained (the worker sees only
its instruction plus the outputs of its dependencies). Do not add a final "write the
answer" subtask: a separate writer combines everything using "synthesis".
Optional tags per subtask pick a kind of model: fast, reasoning, code, writing, long_context.
Reply with ONLY JSON:
{{"goal": "...", "tasks": [{{"id": "t1", "instruction": "...", "depends_on": [], "tags": []}}],
  "synthesis": "how the writer should combine the results into the final answer"}}"""


class Subtask(BaseModel):
    id: str
    instruction: str
    depends_on: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("empty id")
        return v


class TaskGraph(BaseModel):
    goal: str
    tasks: list[Subtask]
    synthesis: str = "Combine the results into one clear, complete answer to the goal."

    @model_validator(mode="after")
    def _valid_graph(self) -> TaskGraph:
        if not 1 <= len(self.tasks) <= MAX_TASKS:
            raise ValueError(f"need 1-{MAX_TASKS} tasks, got {len(self.tasks)}")
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task ids")
        for t in self.tasks:
            unknown = set(t.depends_on) - set(ids)
            if unknown:
                raise ValueError(f"{t.id} depends on unknown {sorted(unknown)}")
            if t.id in t.depends_on:
                raise ValueError(f"{t.id} depends on itself")
        self.waves()  # raises on cycles
        return self

    def waves(self) -> list[list[Subtask]]:
        """Group tasks into layers; every task's dependencies are in earlier layers."""
        done: set[str] = set()
        remaining = list(self.tasks)
        layers: list[list[Subtask]] = []
        while remaining:
            ready = [t for t in remaining if set(t.depends_on) <= done]
            if not ready:
                raise ValueError("dependency cycle in task graph")
            layers.append(ready)
            done |= {t.id for t in ready}
            remaining = [t for t in remaining if t.id not in done]
        return layers


async def plan(engine, goal: str) -> tuple[TaskGraph, str]:  # engine: nathan.engine.Engine
    refs = engine.chain(["planner", "reasoning", "long_context"])
    messages = [{"role": "system", "content": PLANNER_SYSTEM},
                {"role": "user", "content": f"GOAL:\n{goal}"}]
    return await ask_json(engine.client, refs[:4], messages, TaskGraph, temperature=0.2)
