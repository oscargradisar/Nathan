"""Fallback chain: try models in order until one answers.

Used everywhere a single answer is needed. A model that is out of quota, down, gone or
rejects the request is skipped; any other exception is a bug and is raised.
"""

from __future__ import annotations

from typing import Any

from nathan.client import ChatResult, Client, Message
from nathan.errors import RECOVERABLE, NoModelAvailable


async def fallback(client: Client, refs: list[str], messages: list[Message],
                   **kwargs: Any) -> ChatResult:
    errors: list[str] = []
    for ref in refs:
        try:
            result = await client.chat(ref, messages, **kwargs)
        except RECOVERABLE as e:
            errors.append(f"{ref}: {type(e).__name__}")
            continue
        if result.text or result.tool_calls:
            return result
        errors.append(f"{ref}: empty reply")
    raise NoModelAvailable("no model could answer — " + ("; ".join(errors) or "no candidates"))
