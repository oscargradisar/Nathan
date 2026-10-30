"""Eyes: images dropped into the console are looked at before the task runs.

Why describe first instead of sending the picture along? Most of Nathan's free staff read
text only. One model with eyes turns each image into a careful written account (every bit
of text in it, numbers, layout, what matters for your question), then the whole team works
on that like any other prompt.

Who looks: free vision models first (Gemini, tagged `vision` in providers.yaml), then the
lead manager (ChatGPT/Codex `-i`, or Claude reading the file) on your own plan.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import re
from pathlib import Path

from nathan.errors import RECOVERABLE

MAX_IMAGES = 4
MAX_BYTES = 8 * 1024 * 1024
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
_DATA_URL = re.compile(r"^data:(image/[a-z+]+);base64,([A-Za-z0-9+/=\s]+)$")

LOOK = """Look at this image carefully for someone who can't see it. They asked:
"{question}"
Describe exactly what is there: every piece of readable text verbatim, numbers, code, UI,
charts (values and axes), people/objects/places, and anything odd or important for their
question. Be precise and complete; don't answer the question itself, just describe."""


def save(data_urls: list[str], folder: Path) -> list[Path]:
    """Validate base64 data URLs from the browser and store them. Raises ValueError."""
    if len(data_urls) > MAX_IMAGES:
        raise ValueError(f"up to {MAX_IMAGES} images at a time")
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for url in data_urls:
        m = _DATA_URL.match(url.strip())
        if not m or m.group(1) not in TYPES:
            raise ValueError("images must be PNG, JPEG, WebP or GIF")
        raw = base64.b64decode(m.group(2), validate=False)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError("each image must be under 8 MB")
        path = folder / (hashlib.sha256(raw).hexdigest()[:16] + TYPES[m.group(1)])
        path.write_bytes(raw)
        paths.append(path)
    return paths


def _data_url(path: Path) -> str:
    mime = next(k for k, v in TYPES.items() if v == path.suffix)
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


async def look(engine, path: Path, question: str) -> tuple[str, str]:
    """One image -> (description, who looked). Raises RuntimeError if nobody could."""
    from nathan import managers

    prompt = LOOK.format(question=question[:500])
    message = [{"role": "user", "content": [{"type": "text", "text": prompt},
                                            {"type": "image_url", "image_url": {"url": _data_url(path)}}]}]
    eyes = set(engine.roster.candidates(["vision"]))
    # ranked with "reasoning" too (sharpest first); they race, so one stalled model can't hold it up
    refs = [r for r in engine.roster.candidates(["vision", "reasoning"]) if r in eyes][:3]

    async def one(ref: str) -> tuple[str, str]:
        res = await engine.client.chat(ref, message, max_tokens=1500, temperature=0.2, use_cache=False)
        if not res.text.strip():
            raise ValueError("empty answer")
        return res.text.strip(), ref

    tasks = [asyncio.create_task(one(r)) for r in refs]
    try:
        for done in asyncio.as_completed(tasks, timeout=60):
            try:
                return await done
            except (*RECOVERABLE, ValueError):
                continue
    except TimeoutError:
        pass
    finally:
        for t in tasks:
            t.cancel()
    for m in managers.lead_order(await managers.connected()):
        try:
            return await managers.ask(m, prompt, images=[path], timeout=180), m.name
        except RuntimeError:
            continue
    raise RuntimeError("no model with eyes is available: connect Gemini or a manager under Accounts")


def brief(question: str, seen: list[tuple[str, str]]) -> str:
    """The prompt the team works on: the request plus what each image shows."""
    parts = [question.strip() or "What is in these images? Explain anything important."]
    for i, (text, who) in enumerate(seen, 1):
        parts.append(f"\n[Attached image {i}, as described by {who}]\n{text}")
    return "\n".join(parts)
