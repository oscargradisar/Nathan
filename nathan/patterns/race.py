"""Race: ask several models at once, keep the first good answer, cancel the rest.

Why? Free endpoints vary wildly in speed from minute to minute. Racing three of them
gives you the speed of whichever is fastest right now. It costs one request on each
model that started, so use it for short, latency-sensitive calls.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from nathan.client import ChatResult, Client, Message
from nathan.errors import RECOVERABLE, NoModelAvailable


def _non_empty(result: ChatResult) -> bool:
    return bool(result.text.strip())


async def race(client: Client, refs: list[str], messages: list[Message], *,
               valid: Callable[[ChatResult], bool] = _non_empty, **kwargs: Any) -> ChatResult:
    if not refs:
        raise NoModelAvailable("race needs at least one model")
    tasks = {asyncio.create_task(client.chat(ref, messages, **kwargs)): ref for ref in refs}
    errors: list[str] = []
    try:
        for next_done in asyncio.as_completed(tasks):
            try:
                result = await next_done
            except RECOVERABLE as e:
                errors.append(f"{type(e).__name__}: {e}")
                continue
            if valid(result):
                return result
            errors.append(f"{result.ref}: invalid answer")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    raise NoModelAvailable("race: no valid answer — " + "; ".join(errors))
