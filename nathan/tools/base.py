"""A tool is a Python function plus a JSON schema describing its arguments.

The schema uses the OpenAI "function calling" format, which every provider we use
understands. Tools return plain text (errors included), so a failing tool never crashes
the agent: the model just sees the error and can try something else.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

MAX_RESULT_CHARS = 8000
# A tool that takes longer hands the model an error instead of stalling its whole job
# (seen live: research inside an agent ran for minutes while providers were rate-limited)
TOOL_TIMEOUT_S = 120.0


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., Any]

    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def params(required: list[str] | None = None, **props: tuple[str, str]) -> dict[str, Any]:
    """params(path=("string", "file path")) → JSON schema object."""
    return {"type": "object",
            "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            "required": required if required is not None else list(props)}


@dataclass
class Toolbox:
    tools: dict[str, Tool] = field(default_factory=dict)
    timeout: float = TOOL_TIMEOUT_S

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def describe(self) -> str:
        """Plain-text tool list, for models without native tool calling."""
        lines = []
        for t in self.tools.values():
            args = ", ".join(f"{k}: {v.get('type')}" for k, v in t.parameters["properties"].items())
            lines.append(f"- {t.name}({args}): {t.description}")
        return "\n".join(lines)

    async def call(self, name: str, arguments: str | dict[str, Any]) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return f"ERROR: unknown tool {name!r}. Available: {', '.join(self.tools)}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments)
            if not isinstance(args, dict):
                raise TypeError("arguments must be a JSON object")
        except (ValueError, TypeError) as e:
            return f"ERROR: bad arguments for {name}: {e}"
        async def run() -> Any:
            if inspect.iscoroutinefunction(tool.func):
                return await tool.func(**args)
            # Plain functions (Discord typing, Start-menu scans, file searches) can take
            # seconds; in a thread they don't freeze every other run and the live streams
            out = await asyncio.to_thread(tool.func, **args)
            if asyncio.iscoroutine(out):  # a lambda that returns a coroutine (run_python)
                out = await out
            return out

        try:
            out = await asyncio.wait_for(run(), self.timeout)
        except TimeoutError:
            return (f"ERROR: {name} took longer than {self.timeout:.0f}s and was stopped. "
                    "Carry on without it, or try something simpler.")
        except TypeError as e:
            return f"ERROR: wrong arguments for {name}: {e}"
        except Exception as e:  # noqa: BLE001 - report any tool failure back to the model
            return f"ERROR: {type(e).__name__}: {e}"
        text = str(out)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n…[truncated, {len(text)} chars total]"
        return text


def default_toolbox(memory=None, workspace=None, engine=None) -> Toolbox:
    from nathan.tools import canvas, database, desktop, fetch, files, python_exec

    box = Toolbox()
    for tool in files.make_tools(workspace):
        box.add(tool)
    box.add(python_exec.make_tool(workspace))
    box.add(fetch.make_tool())
    box.add(database.make_tool(workspace))
    for tool in desktop.make_tools() + canvas.make_tools():
        box.add(tool)

    async def world(place: str, layers: str = "") -> str:
        from nathan import globe

        if not layers:
            return json.dumps(await globe.search(place, engine))
        return await globe.point(place, [x.strip() for x in layers.split(",")])

    box.add(Tool("world_view", "Show a request in Nathan's World view, e.g. aircraft over Paris, "
                 "earthquakes near Japan or public cameras in Ljubljana. Or specify a place and layers: "
                 "planes, quakes, iss, shops, media (comma-separated). Returns what's there.",
                 params(["place"], place=("string", "a place name"), layers=("string", "e.g. 'planes,shops'")), world))
    from nathan.geomedia import filterMediaNodes, findPublicCameras, flyToMediaNode

    box.add(Tool("findPublicCameras", "Load real public cameras for a location from OpenCCTV into "
                 "Nathan's globe. Automatically opens a feed inside World view. "
                 "Use when asked for webcams/CCTV/cameras at a place; bounded to 50 results. "
                 "Give `near` to open the working camera closest to a business, landmark or address.",
                 params(["place"], place=("string", "the area: city, state/region, country"),
                        near=("string", "optional landmark/business/address to be closest to")),
                 findPublicCameras))

    box.add(Tool("filterMediaNodes", "Show/hide globe media and search nodes by region, category or country. "
                 "Returns IDs to use with flyToMediaNode. Empty filters reset the search.",
                 params([], region=("string", "exact region, case insensitive"),
                        category=("string", "category"), country=("string", "country"),
                        visible=("boolean", "whether the media layer is visible")), filterMediaNodes))
    box.add(Tool("flyToMediaNode", "Fly to a media node ID and open its media inspector in World view.",
                 params(id=("string", "media node ID from filterMediaNodes")), flyToMediaNode))
    if engine is not None:
        async def research(question: str) -> str:
            from nathan.research import run_research

            # Free pipeline only: an agent may research many times per job, and each premium
            # round would spend the owner's Claude/ChatGPT plan with nothing shown on screen
            return (await run_research(engine, question, team=[]))["answer"]

        box.add(Tool("research", "Research a question properly: searches the web and many sources, several "
                     "models cross-check, returns a cited answer. Use for facts you aren't sure of.",
                     params(question=("string", "the question")), research))
    if memory is not None:
        # Async on purpose: plain tools run in a thread, and memory's SQLite connection is
        # shared with the event loop, which can't be used from two threads at once
        async def remember(fact: str) -> str:
            return memory.remember(fact)

        async def recall(query: str) -> str:
            return "\n".join(f"- {e.content}" for e in memory.search(query, 5)) or "nothing found"

        box.add(Tool("remember", "Save a fact for future sessions.",
                     params(fact=("string", "the fact to remember")), remember))
        box.add(Tool("recall", "Search notes and answers from earlier sessions.",
                     params(query=("string", "what to look for")), recall))
    return box
