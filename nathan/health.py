"""Self-maintenance: keep the staff list honest without anyone editing providers.yaml.

Two checks:
- roll_call(): asks each provider which models it offers today. Listing models is free
  (no quota), so Nathan does this at every start. Models that vanished are retired;
  models that came back are reinstated.
- diagnostics(): a tiny "reply OK" to every model, timed. Costs one request each, so it
  only runs when you ask (or from the nightly schedule).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx2 as httpx

from nathan.errors import RECOVERABLE

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


async def live_models(provider, http: httpx.AsyncClient) -> set[str] | None:
    """The model ids a provider offers right now, or None if it wouldn't say."""
    url = provider.resolve(provider.models_url) if provider.models_url else (
        provider.resolved_base_url.rstrip("/") + "/models")
    headers = {} if not provider.key_env else {"Authorization": f"Bearer {provider.api_key}"}
    try:
        resp = await http.get(url, headers=headers)
        body = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    items = body.get("data") or body.get("result") or []
    # OpenAI-style lists use "id"; Cloudflare puts the model name in "name" (its id is a UUID)
    ids = {str(m.get(k) or "") for m in items if isinstance(m, dict) for k in ("id", "name")}
    return {i.removeprefix("models/") for i in ids if i} or None


async def roll_call(engine, http: httpx.AsyncClient | None = None) -> dict[str, list[str]]:
    """Retire models a provider no longer lists; reinstate ones it lists again.
    Returns {"retired": [...], "reinstated": [...], "unchecked": [providers]}."""
    own = http is None
    http = http or httpx.AsyncClient(timeout=20)
    out: dict[str, list[str]] = {"retired": [], "reinstated": [], "unchecked": []}
    try:
        providers = [p for p in engine.providers.values() if p.api_key]
        listed = await asyncio.gather(*(live_models(p, http) for p in providers))
        for p, ids in zip(providers, listed, strict=True):
            missing = [m for m in p.models if m not in ids] if ids else list(p.models)
            if ids is None or len(missing) > len(p.models) / 2:
                # A list missing most of our models is more likely wrong (paging, a changed
                # format) than half the staff quitting at once: don't act on it.
                out["unchecked"].append(p.name)
                continue
            for model in p.models:
                gone = engine.quota.is_gone(p.name, model)
                if model in ids and gone:
                    engine.quota.unmark_gone(p.name, model)
                    out["reinstated"].append(p.ref(model))
                elif model not in ids and not gone and not model.endswith("/free"):
                    engine.quota.mark_gone(p.name, model)
                    out["retired"].append(p.ref(model))
    finally:
        if own:
            await http.aclose()
    return out


async def diagnostics(engine, emit: Emit | None = None, timeout: float = 45) -> list[dict]:
    """Ping every configured model with a key. Each result: ref, ok, seconds, reason."""
    refs = [p.ref(m) for p in engine.providers.values() if p.api_key for m in p.models]
    results: list[dict] = []

    async def ping(ref: str) -> None:
        await _emit(emit, {"type": "working", "id": ref, "worker": ref, "role": "patient"})
        start = time.perf_counter()
        try:
            res = await asyncio.wait_for(engine.client.chat(
                ref, [{"role": "user", "content": "Reply with just: OK"}], max_tokens=300), timeout)
            ok, reason = bool(res.text), "" if res.text else "empty answer"
        except RECOVERABLE as e:
            ok, reason = False, type(e).__name__
        except TimeoutError:
            ok, reason = False, "no answer"
        r = {"ref": ref, "ok": ok, "seconds": round(time.perf_counter() - start, 1), "reason": reason}
        results.append(r)
        await _emit(emit, {"type": "delivered" if ok else "task_failed", "id": ref, "worker": ref,
                           "role": "patient", "seconds": r["seconds"], "reason": reason,
                           "text": "" if ok else reason})

    await asyncio.gather(*(ping(r) for r in refs))
    return sorted(results, key=lambda r: (not r["ok"], r["seconds"]))
