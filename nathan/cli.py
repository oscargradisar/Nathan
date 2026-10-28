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
    if ans.note:
        console.print(f"[dim]{ans.note}[/]")


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What you want answered")],
    model: Annotated[list[str] | None, typer.Option(
        "--model", "-m", help="provider/model; repeat to ask several models at once")] = None,
    mode: Annotated[str | None, typer.Option(
        "--mode", help="single | race | vote | each (default: single, or each with several -m)")
    ] = None,
    n: Annotated[int, typer.Option("--n", help="how many models to fan out to")] = 4,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="only the final answer")] = False,
) -> None:
    """Ask one or more models a question."""
    engine = build_engine()
    chosen = mode or ("each" if model and len(model) > 1 else "single")

    async def run() -> int:
        try:
            if chosen == "each":
                return await _ask_models(engine.client, question, model or engine.roster.pick(None, n))
            ans = await engine.ask(question, mode=chosen, models=model, n=n)
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


if __name__ == "__main__":
    app()
