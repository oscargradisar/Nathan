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
from nathan.errors import NathanError

app = typer.Typer(help="Nathan: many free AI models in parallel, one answer out.",
                  no_args_is_help=True, add_completion=False)
console = Console()


def build_client() -> Client:
    """Create the client used by CLI commands. Tests replace this with a fake."""
    return Client()


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


@app.command()
def ask(
    question: Annotated[str, typer.Argument(help="What you want answered")],
    model: Annotated[list[str] | None, typer.Option(
        "--model", "-m", help="provider/model; repeat to ask several models at once")] = None,
) -> None:
    """Ask one or more models a question."""
    client = build_client()

    async def run() -> int:
        try:
            models = model or [_default_model(client)]
            return await _ask_models(client, question, models)
        finally:
            await client.close()

    try:
        raise typer.Exit(asyncio.run(run()))
    except NathanError as e:
        console.print(f"[red]{type(e).__name__}[/]: {e}")
        raise typer.Exit(1) from e


def _default_model(client: Client) -> str:
    """First model of the first provider that has a key."""
    for p in client.providers.values():
        if p.api_key and p.models:
            return p.ref(next(iter(p.models)))
    raise NathanError("no provider has an API key yet — add one to .env (see README)")


@app.command()
def models() -> None:
    """List every configured model and whether its provider has a key."""
    client = build_client()
    for p in client.providers.values():
        status = "[green]key set[/]" if p.api_key else "[yellow]no key[/]"
        console.print(f"[bold]{p.name}[/] ({status})")
        for name, spec in p.models.items():
            console.print(f"  {p.ref(name)}  [dim]{', '.join(spec.tags)}[/]")


if __name__ == "__main__":
    app()
