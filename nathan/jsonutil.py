"""Getting reliable JSON out of free models.

Small models often wrap JSON in ```json fences, add a sentence before it, or leave a
trailing comma. `extract_json` digs the object out; `ask_json` validates it with a
Pydantic model and, if that fails, tells the model what was wrong and asks once more
before moving on to the next model.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from nathan.client import Client, Message
from nathan.errors import RECOVERABLE, NoModelAvailable

T = TypeVar("T", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _balanced(text: str, start: int) -> str | None:
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def extract_json(text: str) -> Any:
    """Return the first JSON object/array found in text. Raises ValueError if none."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for chunk in candidates:
        for i, ch in enumerate(chunk):
            if ch not in "{[":
                continue
            block = _balanced(chunk, i)
            if block is None:
                continue
            for attempt in (block, re.sub(r",\s*([}\]])", r"\1", block)):
                try:
                    return json.loads(attempt)
                except json.JSONDecodeError:
                    pass
    raise ValueError("no JSON found in model output")


async def ask_json[T: BaseModel](client: Client, refs: list[str], messages: list[Message], schema: type[T],
                   **kwargs: Any) -> tuple[T, str]:
    """Ask models in order until one returns JSON matching `schema`. Returns (obj, ref)."""
    errors: list[str] = []
    for ref in refs:
        convo = list(messages)
        for _attempt in range(2):
            try:
                res = await client.chat(ref, convo, **kwargs)
            except RECOVERABLE as e:
                errors.append(f"{ref}: {type(e).__name__}")
                break
            try:
                return schema.model_validate(extract_json(res.text)), ref
            except (ValueError, ValidationError) as e:
                errors.append(f"{ref}: bad JSON")
                convo = convo + [
                    {"role": "assistant", "content": res.text},
                    {"role": "user", "content": f"That was not valid. Error: {str(e)[:500]}\n"
                                                "Reply with only the corrected JSON."},
                ]
    raise NoModelAvailable("no model returned valid JSON — " + "; ".join(errors))


async def race_json[T: BaseModel](client: Client, refs: list[str], messages: list[Message],
                                  schema: type[T], lanes: int = 2, **kwargs: Any) -> tuple[T, str]:
    """Like ask_json, but `lanes` independent chains run at once (refs dealt round-robin) and
    the first valid answer wins. One slow model can no longer stall the whole job."""
    chains = [refs[i::lanes] for i in range(lanes) if refs[i::lanes]]
    tasks = [asyncio.create_task(ask_json(client, c, messages, schema, **kwargs)) for c in chains]
    errors: list[str] = []
    try:
        for next_done in asyncio.as_completed(tasks):
            try:
                return await next_done
            except NoModelAvailable as e:
                errors.append(str(e))
    finally:
        for t in tasks:
            t.cancel()
    raise NoModelAvailable("no model returned valid JSON — " + "; ".join(errors))
