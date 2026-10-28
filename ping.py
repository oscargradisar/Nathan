"""Tier 0 gate: check every provider key in parallel.

For each provider in providers.yaml:
  1. is a key set in .env?
  2. does the provider answer a tiny chat request with the first configured model?
  3. which configured models are missing from the provider's live /models list?

Run:  uv run python ping.py
Exit code 0 when at least MIN_PASSING providers answer.
"""

from __future__ import annotations

import asyncio
import sys
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from rich.console import Console
from rich.table import Table

from nathan.config import Provider, load_providers, mask

MIN_PASSING = 4
console = Console()


async def check(p: Provider) -> dict:
    result = {"provider": p.name, "key": mask(p.api_key), "status": "", "model": "", "ms": "", "note": ""}
    if not p.api_key:
        result["status"] = "[yellow]no key[/]"
        result["note"] = f"get one: {p.signup}" if p.signup else ""
        return result

    client = AsyncOpenAI(base_url=p.base_url, api_key=p.api_key, timeout=30, max_retries=1)
    model = next(iter(p.models))
    result["model"] = model

    # Live model list (not every provider supports it; failure here is only a warning)
    try:
        live = {m.id.removeprefix("models/") async for m in client.models.list()}
        missing = [m for m in p.models if m not in live]
        if missing:
            result["note"] = "not in live list: " + ", ".join(missing)
    except Exception as e:  # noqa: BLE001 - informational only, the chat test below decides pass/fail
        result["note"] = f"model list unavailable ({type(e).__name__})"

    start = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly the word: ok"}],
            max_tokens=300,  # reasoning models spend tokens thinking first
        )
        text = (resp.choices[0].message.content or "").strip()
        result["ms"] = f"{(time.perf_counter() - start) * 1000:.0f}"
        result["status"] = "[green]pass[/]" if text else "[yellow]empty reply[/]"
    except APIStatusError as e:
        kind = "rate limited" if e.status_code == 429 else f"HTTP {e.status_code}"
        result["status"] = f"[red]{kind}[/]"
        result["note"] = (result["note"] + " " + str(e.message)[:120]).strip()
    except (APIConnectionError, APITimeoutError) as e:
        result["status"] = "[red]unreachable[/]"
        result["note"] = type(e).__name__
    finally:
        await client.close()
    return result


async def main() -> int:
    providers = load_providers()
    results = await asyncio.gather(*(check(p) for p in providers.values()))

    table = Table(title="Nathan provider check")
    for col in ("provider", "key", "status", "model", "ms", "note"):
        table.add_column(col, overflow="fold")
    for r in results:
        table.add_row(*(r[c] for c in ("provider", "key", "status", "model", "ms", "note")))
    console.print(table)

    passing = sum("pass" in r["status"] for r in results)
    ok = passing >= MIN_PASSING
    console.print(
        f"{passing}/{len(results)} providers passing — "
        + ("[green]Tier 0 gate passed[/]" if ok else f"[red]need at least {MIN_PASSING}[/]")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
