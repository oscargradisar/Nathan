"""Draft → critique → revise, with a different model (ideally provider) for each role.

Why different models? A model is bad at spotting its own blind spots. A second model
reviewing the draft catches errors the first one is confident about.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from nathan.client import Client, Message
from nathan.patterns.fallback import fallback

CRITIC_SYSTEM = ("You are a strict reviewer. List the concrete problems in the draft: factual "
                 "errors, missing points, unclear parts, bugs. Be specific and brief. If it is "
                 "already excellent, say 'No changes needed.'")
REVISE_SYSTEM = ("Revise the draft using the review. Fix every valid point, ignore invalid "
                 "ones, and output only the improved final version.")


@dataclass
class CritiqueResult:
    draft: str
    critique: str
    final: str
    drafter: str
    critic: str
    reviser: str


def _others(chain: list[str], avoid: str) -> list[str]:
    """Same chain, but models from the avoided model's provider go last."""
    prov = avoid.split("/", 1)[0]
    return [r for r in chain if not r.startswith(prov + "/")] + [r for r in chain if r.startswith(prov + "/")]


async def critique(client: Client, messages: list[Message], *, drafters: list[str],
                   critics: list[str], revisers: list[str] | None = None,
                   on_event: Callable[[dict[str, Any]], Any] | None = None) -> CritiqueResult:
    """on_event gets {"type": "step", "id", "role", "status", "model", "text"} per role."""

    async def step(sid: str, role: str, refs: list[str], msgs: list[Message]):
        await _emit(on_event, {"type": "step", "id": sid, "role": role, "status": "working",
                               "model": refs[0] if refs else ""})
        res = await fallback(client, refs, msgs)
        await _emit(on_event, {"type": "step", "id": sid, "role": role, "status": "done",
                               "model": res.ref, "text": res.text,
                               "seconds": round(res.latency_ms / 1000, 1)})
        return res

    task = "\n\n".join(str(m["content"]) for m in messages if m["role"] in ("system", "user"))
    draft = await step("draft", "drafter", drafters, messages)
    review = await step("critique", "critic", _others(critics, draft.ref), [
        {"role": "system", "content": CRITIC_SYSTEM},
        {"role": "user", "content": f"TASK:\n{task}\n\nDRAFT:\n{draft.text}"},
    ])
    if "no changes needed" in review.text.lower():
        return CritiqueResult(draft.text, review.text, draft.text, draft.ref, review.ref, draft.ref)
    revised = await step("revise", "reviser", revisers or drafters, [
        {"role": "system", "content": REVISE_SYSTEM},
        {"role": "user", "content": f"TASK:\n{task}\n\nDRAFT:\n{draft.text}\n\nREVIEW:\n{review.text}"},
    ])
    return CritiqueResult(draft.text, review.text, revised.text, draft.ref, review.ref, revised.ref)


async def _emit(on_event, event: dict[str, Any]) -> None:
    if on_event is not None:
        out = on_event(event)
        if asyncio.iscoroutine(out):
            await out
