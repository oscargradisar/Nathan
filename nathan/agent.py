"""Tier 4 agent: a bounded tool-calling loop that works on any provider.

The loop: ask the model → if it requests a tool, run it and show the result → repeat,
at most `max_steps` times, then demand a final answer. The step limit is what keeps a
confused model from burning a whole day's free quota in a loop.

Two ways to call tools, chosen per model:
- native: the provider's function-calling API (`tools=[...]`, `tool_calls` in the reply);
- text:   for models without it, the tools are described in the prompt and the model
          replies with a ```tool {"name": ..., "arguments": {...}}``` block.
The history is stored in a neutral form (`Step`s), so if one model fails mid-task the
next model in the chain continues the same conversation, in whichever mode it supports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from nathan.client import Message
from nathan.errors import RECOVERABLE, BadRequest, NoModelAvailable
from nathan.jsonutil import extract_json
from nathan.tools import Toolbox

AGENT_SYSTEM = ("You are Nathan, a capable assistant that can use tools. Use tools when they "
                "help (e.g. read files before answering questions about them; run Python for "
                "exact calculations). When you have enough information, answer the user "
                "directly and completely.")

TEXT_PROTOCOL = """You can use these tools:
{tools}

To call a tool, reply with ONLY a block like this and nothing else:
```tool
{{"name": "read_file", "arguments": {{"path": "notes.txt"}}}}
```
You will get the result back, then you can call another tool or give your final answer.
When you give the final answer, do not include a ```tool block."""

_TOOL_BLOCK = re.compile(r"```tool\s*(.*?)```", re.DOTALL)


@dataclass
class Step:
    kind: str  # "call" | "result" | "text"
    name: str = ""
    arguments: str = "{}"
    content: str = ""
    call_id: str = ""
    model: str = ""


@dataclass
class AgentResult:
    answer: str
    steps: list[Step] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    hit_limit: bool = False

    @property
    def tool_calls(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "call"]


def _native_messages(base: list[Message], steps: list[Step]) -> list[Message]:
    msgs = list(base)
    i = 0
    while i < len(steps):
        s = steps[i]
        if s.kind == "call":
            calls = []
            while i < len(steps) and steps[i].kind == "call":
                c = steps[i]
                calls.append({"id": c.call_id, "type": "function",
                              "function": {"name": c.name, "arguments": c.arguments}})
                i += 1
            msgs.append({"role": "assistant", "content": None, "tool_calls": calls})
            continue
        if s.kind == "result":
            msgs.append({"role": "tool", "tool_call_id": s.call_id, "content": s.content})
        elif s.kind == "text":
            msgs.append({"role": "assistant", "content": s.content})
        i += 1
    return msgs


def _text_messages(base: list[Message], steps: list[Step], toolbox: Toolbox) -> list[Message]:
    protocol = TEXT_PROTOCOL.format(tools=toolbox.describe())
    msgs: list[Message] = []
    for m in base:
        if m["role"] == "system":
            msgs.append({"role": "system", "content": f"{m['content']}\n\n{protocol}"})
        else:
            msgs.append(m)
    if not any(m["role"] == "system" for m in base):
        msgs.insert(0, {"role": "system", "content": protocol})
    for s in steps:
        if s.kind == "call":
            block = json.dumps({"name": s.name, "arguments": json.loads(s.arguments or "{}")})
            msgs.append({"role": "assistant", "content": f"```tool\n{block}\n```"})
        elif s.kind == "result":
            msgs.append({"role": "user", "content": f"TOOL RESULT ({s.name}):\n{s.content}"})
        elif s.kind == "text":
            msgs.append({"role": "assistant", "content": s.content})
    return msgs


def parse_text_call(text: str) -> tuple[str, dict[str, Any]] | None:
    m = _TOOL_BLOCK.search(text)
    if not m:
        return None
    try:
        obj = extract_json(m.group(1))
    except ValueError:
        return None
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments") or {}
    return str(obj["name"]), args if isinstance(args, dict) else {}


async def run_agent(client, chain: list[str], task: str | list[Message], toolbox: Toolbox, *,
                    max_steps: int = 8, system: str = AGENT_SYSTEM,
                    text_mode_models: set[str] | None = None) -> AgentResult:
    """Run the loop. `chain` = models to try in order; a failing model hands over to the next."""
    base: list[Message] = ([{"role": "system", "content": system}, {"role": "user", "content": task}]
                           if isinstance(task, str) else list(task))
    steps: list[Step] = []
    text_mode = set(text_mode_models or ())
    models_used: list[str] = []
    chain = list(chain)
    if not chain:
        raise NoModelAvailable("agent: no models available")
    idx = 0
    errors: list[str] = []
    call_counter = 0

    for step_no in range(max_steps + 1):
        final_turn = step_no == max_steps
        while True:
            if idx >= len(chain):
                raise NoModelAvailable("agent: every model failed — " + "; ".join(errors))
            ref = chain[idx]
            use_text = ref in text_mode
            try:
                if use_text:
                    msgs = _text_messages(base, steps, toolbox)
                    if final_turn:
                        msgs.append({"role": "user", "content": "Step limit reached. Give your "
                                     "final answer now, without any tool block."})
                    res = await client.chat(ref, msgs)
                else:
                    msgs = _native_messages(base, steps)
                    if final_turn:
                        msgs.append({"role": "user", "content": "Step limit reached. Give your "
                                     "final answer now."})
                    res = await client.chat(ref, msgs, tools=None if final_turn else toolbox.schemas())
                break
            except BadRequest as e:
                if not use_text:  # maybe this model can't do native tools: retry in text mode
                    text_mode.add(ref)
                    continue
                errors.append(f"{ref}: {e}")
                idx += 1
            except RECOVERABLE as e:
                errors.append(f"{ref}: {type(e).__name__}")
                idx += 1

        if ref not in models_used:
            models_used.append(ref)

        calls: list[tuple[str, str, str]] = []  # (id, name, arguments json)
        if not use_text:
            for tc in res.tool_calls:
                calls.append((tc["id"] or f"call_{call_counter}", tc["name"], tc["arguments"]))
                call_counter += 1
        elif not final_turn:
            parsed = parse_text_call(res.text)
            if parsed:
                calls.append((f"call_{call_counter}", parsed[0], json.dumps(parsed[1])))
                call_counter += 1

        if not calls or final_turn:
            answer = _TOOL_BLOCK.sub("", res.text).strip() if use_text else res.text
            return AgentResult(answer, steps, models_used, hit_limit=final_turn)

        for call_id, name, args in calls:
            steps.append(Step("call", name, args or "{}", call_id=call_id, model=ref))
        for call_id, name, args in calls:
            output = await toolbox.call(name, args or "{}")
            steps.append(Step("result", name, content=output, call_id=call_id, model=ref))

    raise AssertionError("unreachable")
