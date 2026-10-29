"""Command line: `nathan ask "question" --model groq/openai/gpt-oss-120b`.

Run `uv run nathan --help` to see every command.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from nathan.client import ChatResult, Client
from nathan.engine import Answer, Engine
from nathan.errors import NathanError

app = typer.Typer(help="Nathan: many free AI models in parallel, one answer out.",
                  no_args_is_help=True, add_completion=False)
console = Console()


def build_engine() -> Engine:
    """Create the engine used by CLI commands. Tests replace this with a fake."""
    return Engine()


def show_result(res: ChatResult, title: str | None = None) -> None:
    subtitle = f"{res.latency_ms:.0f} ms · {res.total_tokens} tokens" + (" · cached" if res.cached else "")
    console.print(Panel(Markdown(res.text or "_(empty)_"), title=title or res.ref,
                        subtitle=subtitle, title_align="left"))


async def _ask_models(client: Client, question: str, models: list[str]) -> int:
    messages = [{"role": "user", "content": question}]
    results = await asyncio.gather(*(client.chat(m, messages) for m in models),
                                   return_exceptions=True)
    failures = 0
    for model, res in zip(models, results, strict=True):
        if isinstance(res, BaseException):
            failures += 1
            console.print(Panel(f"[red]{type(res).__name__}[/]: {res}", title=model,
                                title_align="left"))
        else:
            show_result(res)
    return 1 if failures == len(models) else 0


def show_answer(ans: Answer, verbose: bool) -> None:
    if ans.mode == "vote" and verbose:
        for c in ans.candidates:
            if c.ok:
                console.print(Panel(Markdown(c.text), title=f"raw · {c.ref}", title_align="left",
                                    border_style="dim", subtitle=f"{c.latency_ms:.0f} ms"))
            else:
                console.print(Panel(f"[red]{c.error}[/]", title=f"raw · {c.ref}",
                                    title_align="left", border_style="red"))
    title = f"{ans.mode} · " + (f"judge {ans.judge}" if ans.judge else ", ".join(ans.models))
    console.print(Panel(Markdown(ans.text or "_(empty)_"), title=title, title_align="left",
                        border_style="green"))
    if ans.mode == "plan" and verbose:
        graph = ans.extra.get("graph", {})
        console.print(f"[dim]planner {ans.extra.get('planner')} · {len(graph.get('tasks', []))} "
                      f"subtasks · up to {ans.extra.get('max_parallel')} in parallel[/]")
        for tid, r in ans.extra.get("results", {}).items():
            console.print(f"[dim]  {tid}: {r['model'] or 'FAILED ' + r['error'][:80]}[/]")
    if ans.mode == "agent" and verbose:
        for call in ans.extra.get("tool_calls", []):
            console.print(f"[dim]  tool: {call[:120]}[/]")
    if ans.mode == "critique" and verbose:
        console.print(Panel(ans.extra.get("critique", ""), title="critique", border_style="dim",
                            title_align="left"))
    if ans.note:
        console.print(f"[dim]{ans.note}[/]")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What you want answered")],
    model: Annotated[list[str] | None, typer.Option(
        "--model", "-m", help="provider/model; repeat to ask several models at once")] = None,
    mode: Annotated[str | None, typer.Option(
        "--mode", help="auto | single | race | vote | critique | plan | agent | each "
                         "(default: auto, or each with several -m)")
    ] = None,
    n: Annotated[int, typer.Option("--n", help="how many models to fan out to")] = 4,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="only the final answer")] = False,
    no_memory: Annotated[bool, typer.Option(
        "--no-memory", help="don't use or save conversation memory")] = False,
    max_steps: Annotated[int, typer.Option("--max-steps", help="agent tool-call limit")] = 8,
) -> None:
    """Ask one or more models a question."""
    engine = build_engine()
    chosen = mode or ("each" if model and len(model) > 1 else ("single" if model else "auto"))

    async def run() -> int:
        try:
            if chosen == "each":
                return await _ask_models(engine.client, question, model or engine.roster.pick(None, n))
            ans = await engine.ask(question, mode=chosen, models=model, n=n,
                                   use_memory=not no_memory, max_steps=max_steps)
            show_answer(ans, verbose=not quiet)
            return 0
        finally:
            await engine.close()

    try:
        code = asyncio.run(run())
    except NathanError as e:
        console.print(f"[red]{type(e).__name__}[/]: {e}")
        raise typer.Exit(1) from e
    raise typer.Exit(code)


@app.command()
def models() -> None:
    """List every configured model and whether its provider has a key."""
    engine = build_engine()
    for p in engine.providers.values():
        status = "[green]key set[/]" if p.api_key else "[yellow]no key[/]"
        console.print(f"[bold]{p.name}[/] ({status})")
        for name, spec in p.models.items():
            console.print(f"  {p.ref(name)}  [dim]{', '.join(spec.tags)}[/]")



@app.command()
def quota() -> None:
    """Show how much of each free tier is used in the last 24 hours."""
    from rich.table import Table

    engine = build_engine()
    table = Table(title="Free-tier usage (last 24h)")
    for col in ("model", "key", "used", "remaining", "status"):
        table.add_column(col)
    for row in engine.quota.report():
        remaining = "?" if row["remaining"] is None else str(row["remaining"])
        status = row["status"] if row["status"] == "ok" else f"[yellow]{row['status']}[/]"
        table.add_row(row["ref"], "yes" if row["has_key"] else "[dim]no[/]",
                      str(row["used_requests"]), remaining, status)
    console.print(table)



@app.command()
def health(
    ping: Annotated[bool, typer.Option("--ping/--no-ping",
                                       help="also send one tiny request to every model")] = True,
) -> None:
    """Self-check: retire models providers stopped offering, then ping every model."""
    from rich.table import Table

    from nathan.health import diagnostics, roll_call

    engine = build_engine()

    async def run():
        listed = await roll_call(engine)
        results = await diagnostics(engine) if ping else []
        await engine.close()
        return listed, results

    listed, results = asyncio.run(run())
    for key, label in (("retired", "retired"), ("reinstated", "back on staff"), ("unchecked", "couldn't list")):
        if listed[key]:
            console.print(f"[bold]{label}:[/] {', '.join(listed[key])}")
    if results:
        table = Table(title="Model check")
        for col in ("model", "ok", "seconds", "reason"):
            table.add_column(col)
        for r in results:
            table.add_row(r["ref"], "[green]yes[/]" if r["ok"] else "[red]no[/]", str(r["seconds"]), r["reason"])
        console.print(table)
        console.print(f"{sum(r['ok'] for r in results)}/{len(results)} models answered")


@app.command()
def remember(fact: Annotated[str, typer.Argument(help="Something Nathan should remember")]) -> None:
    """Save a fact to long-term memory."""
    engine = build_engine()
    engine.memory.remember(fact)
    console.print("[green]saved[/]")


@app.command()
def recall(query: Annotated[str, typer.Argument(help="What to search for")],
           k: Annotated[int, typer.Option("--k")] = 5) -> None:
    """Search memory (facts and earlier answers)."""
    engine = build_engine()
    hits = engine.memory.search(query, k)
    if not hits:
        console.print("[dim]nothing found[/]")
    for e in hits:
        console.print(f"[bold]{e.kind}[/] [dim]#{e.id}[/] {e.content[:300]}")



@app.command()
def connect(
    provider: Annotated[str, typer.Argument(help="provider name, or 'all'")] = "all",
    timeout: Annotated[int, typer.Option(help="seconds to wait per provider")] = 300,
    skip_connected: Annotated[bool, typer.Option(help="skip providers that already have a key")] = True,
) -> None:
    """Connect AI accounts: opens each key page and captures the key automatically."""
    from nathan.onboarding.connect import connect as do_connect
    from nathan.onboarding.connect import connect_order
    from nathan.onboarding.server import BackgroundServer, Hub

    engine = build_engine()
    names = connect_order(engine.providers) if provider == "all" else [provider]
    for name in names:
        if name not in engine.providers:
            console.print(f"[red]unknown provider {name}[/]")
            raise typer.Exit(1)

    def ask_paste(name: str) -> str | None:
        console.print(f"[yellow]{name}:[/] no key captured automatically.")
        typed = typer.prompt("Paste the key (hidden, Enter to skip)", default="",
                             hide_input=True, show_default=False)
        return typed.strip() or None

    async def run() -> int:
        hub = Hub(engine.providers)
        hub.loop = asyncio.get_running_loop()
        connected = 0
        with BackgroundServer(hub):
            for name in names:
                p = engine.providers[name]
                if skip_connected and p.api_key:
                    console.print(f"[green]✓ {name}[/] already connected")
                    connected += 1
                    continue
                how = p.onboarding.method if p.onboarding else "manual"
                console.print(f"[bold]→ {name}[/] ({how}): a browser tab is opening…")
                ok, msg = await do_connect(hub, name, timeout=timeout, ask_paste=ask_paste)
                console.print(f"  {'[green]✓' if ok else '[red]✗'} {name}[/]: {msg}")
                connected += ok
        console.print(f"{connected}/{len(names)} providers connected")
        return 0 if connected else 1

    raise typer.Exit(asyncio.run(run()))


@app.command()
def keys() -> None:
    """Show which providers have a key and where it is stored (masked)."""
    from nathan.keys import keychain_available, mask, where

    engine = build_engine()
    console.print(f"OS keychain: {'available' if keychain_available() else '[yellow]not available[/] (using .env)'}")
    for p in engine.providers.values():
        console.print(f"  {p.name:<12} {where(p.name, p.key_env):<9} {mask(p.api_key)}")


@app.command()
def disconnect(provider: Annotated[str, typer.Argument(help="provider name")]) -> None:
    """Delete a provider's key from this computer (revoke it on the provider's site too)."""
    from nathan.keys import delete_key

    engine = build_engine()
    p = engine.providers.get(provider)
    if p is None:
        console.print(f"[red]unknown provider {provider}[/]")
        raise typer.Exit(1)
    delete_key(p.name, p.key_env)
    console.print(f"removed {provider} key. Revoke it too: {p.onboarding.key_page if p.onboarding else p.signup}")



@app.command()
def web(port: Annotated[int, typer.Option(help="local port")] = 8765,
        no_browser: Annotated[bool, typer.Option("--no-browser")] = False) -> None:
    """Open the control room in your browser (live lanes, quota, manual lane, connect)."""
    from nathan.web.app import serve

    console.print(f"Nathan control room: http://127.0.0.1:{port}  (Ctrl+C to stop)")
    serve(build_engine(), port=port, open_browser=not no_browser)


if __name__ == "__main__":
    app()
