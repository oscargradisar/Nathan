"""Nathan's management: the premium AI plans you already pay for, lent through their
official command-line tools (each signs in with your own account; no API keys, no scraping):

  Claude   (Claude Code, `claude -p`)    the Manager: final word on reviews and research
  ChatGPT  (Codex CLI, `codex exec`)     Assistant manager, and manager of Programming
  Gemini   (Gemini CLI, `gemini -p`)     Research director: searches the web with Google

Google retired personal-account sign-in for the Gemini CLI (it now points Pro subscribers to
Antigravity), so the Gemini manager runs on your free AI Studio key from Free staff instead,
with its own settings folder so the retired login is never used.

Each request counts toward that plan's normal usage limits, so Nathan uses them for the
work that needs them (reviews, verification, research, code review), not for chores.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Background checks must not flash console windows on Windows
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

TIMEOUT_S = 300.0


@dataclass(frozen=True)
class Manager:
    id: str
    name: str
    title: str
    rank: int      # lower is more senior: the boss has the final word
    package: str   # npm package with the official CLI
    plan: str      # which subscription it uses

    def cli(self) -> str | None:
        return find_cli(self.id)


CLAUDE = Manager("claude", "Claude", "Manager", 0, "@anthropic-ai/claude-code", "Claude Pro/Max")
CHATGPT = Manager("chatgpt", "ChatGPT", "Assistant manager", 1, "@openai/codex", "ChatGPT Plus/Pro")
GEMINI = Manager("gemini", "Gemini", "Research director", 2, "@google/gemini-cli", "free AI Studio key")
GEMINI_MODEL = "gemini-flash-latest"  # the CLI's default Pro model has almost no free quota
GEMINI_TIMEOUT_S = 150.0
MANAGERS = {m.id: m for m in (CLAUDE, CHATGPT, GEMINI)}
_BIN = {"claude": "claude", "chatgpt": "codex", "gemini": "gemini"}


def find_cli(mid: str) -> str | None:
    home = Path.home()
    npm = Path(os.getenv("APPDATA", home / "AppData/Roaming")) / "npm"
    name = _BIN[mid]
    candidates = [shutil.which(name), npm / f"{name}.cmd", home / ".npm-global/bin" / name,
                  Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]
    if mid == "claude":
        candidates += [home / ".local/bin/claude.exe", home / ".local/bin/claude", home / ".claude/local/claude"]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def _run(args: list[str], stdin: str | None = None, timeout: float = 30,
         env: dict | None = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as cwd:  # empty folder: no project files leak in
        return subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd, check=False, env=env,
                              creationflags=NO_WINDOW)


def _gemini_key() -> str | None:
    from nathan.keys import get_key

    return get_key("gemini", "GEMINI_API_KEY")


def _gemini_env() -> dict:
    """Run the Gemini CLI on the AI Studio key, in a home folder of its own."""
    from nathan.config import data_dir

    home = data_dir() / "gemini-home"
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    (home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "gemini-api-key"}}}), encoding="utf-8")
    return {**os.environ, "GEMINI_API_KEY": _gemini_key() or "", "GEMINI_CLI_TRUST_WORKSPACE": "true",
            "HOME": str(home), "USERPROFILE": str(home)}


def signed_in(m: Manager) -> bool:
    cli = m.cli()
    if not cli:
        return False
    try:
        if m.id == "claude":
            return bool(json.loads(_run([cli, "auth", "status", "--json"]).stdout or "{}").get("loggedIn"))
        if m.id == "chatgpt":
            out = _run([cli, "login", "status"])
            text = (out.stdout + out.stderr).lower()
            return "logged in" in text and "not logged in" not in text
        return bool(_gemini_key())  # see the module note: Gemini runs on the AI Studio key
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def state(m: Manager) -> dict:
    cli = m.cli()
    return {"id": m.id, "name": m.name, "title": m.title, "plan": m.plan, "installed": bool(cli),
            "signed_in": bool(cli) and signed_in(m), "package": m.package}


def open_sign_in(m: Manager) -> None:
    """Start the CLI's own sign-in, which opens your browser. On Windows it gets its own
    console window so its instructions are visible (Gemini asks you to pick 'Login with Google')."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name}'s helper isn't installed yet: press Install first")
    if m.id == "gemini":
        raise RuntimeError("Google retired sign-in for this tool. Nathan uses your free Gemini key instead: "
                           "connect Gemini under Free staff.")
    args = {"claude": [cli, "auth", "login"], "chatgpt": [cli, "login"]}[m.id]
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen(args, creationflags=flags)


def install(m: Manager) -> str:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("Installing needs Node.js first (free, nodejs.org).")
    out = subprocess.run([npm, "install", "-g", m.package], capture_output=True, text=True,
                         timeout=900, check=False, creationflags=NO_WINDOW)
    if out.returncode != 0 or not m.cli():
        raise RuntimeError(out.stderr.strip()[-400:] or f"couldn't install {m.package}")
    return m.cli() or ""


def _args(m: Manager, cli: str, web: bool) -> list[str]:
    if m.id == "claude":
        args = [cli, "-p", "--output-format", "json", "--no-session-persistence"]
        return args + (["--allowedTools", "WebSearch", "WebFetch"] if web else [])
    if m.id == "chatgpt":
        return [cli, *(["--search"] if web else []), "exec", "--skip-git-repo-check", "--ephemeral",
                "-s", "read-only", "-"]
    return [cli, "-m", GEMINI_MODEL, "-p", " ", "-o", "json", "--approval-mode", "plan"]  # plan = read-only


async def ask(m: Manager, prompt: str, *, web: bool = False, timeout: float = TIMEOUT_S) -> str:
    """One question to this manager, answered with your own plan. web=True lets it search
    the live web itself. Raises RuntimeError with a plain reason."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name} isn't connected: install and sign in under Accounts")
    env = None
    if m.id == "gemini":
        if not _gemini_key():
            raise RuntimeError("Gemini needs your free Gemini key: connect Gemini under Free staff")
        env, timeout = _gemini_env(), min(timeout, GEMINI_TIMEOUT_S)
    try:
        out = await asyncio.wait_for(asyncio.to_thread(_run, _args(m, cli, web), prompt, timeout, env),
                                     timeout + 5)
    except (subprocess.TimeoutExpired, TimeoutError) as e:
        raise RuntimeError(f"{m.name} didn't answer within {timeout:.0f}s") from e
    if m.id in ("claude", "gemini"):
        if m.id == "gemini" and ("exhausted your daily quota" in out.stdout + out.stderr
                                 or '"code": 429' in out.stdout + out.stderr):
            raise RuntimeError("Gemini's free daily limit is used up; it's back tomorrow")
        try:
            data = json.loads(out.stdout[out.stdout.find("{"):] if "{" in out.stdout else out.stdout)
        except ValueError as e:
            raise RuntimeError(f"{m.name}: {(out.stderr or out.stdout).strip()[-300:] or 'no answer'}") from e
        if m.id == "claude":
            if data.get("is_error"):
                raise RuntimeError(f"{m.name}: {data.get('result') or 'error'}")
            return str(data.get("result") or "").strip()
        if data.get("error"):
            raise RuntimeError(f"{m.name}: {data['error'].get('message', data['error'])}")
        return str(data.get("response") or "").strip()
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(f"{m.name}: {out.stderr.strip()[-300:] or 'error'}")
    return out.stdout.strip()


async def connected() -> list[Manager]:
    flags = await asyncio.gather(*(asyncio.to_thread(signed_in, m) for m in MANAGERS.values()))
    return [m for m, ok in zip(MANAGERS.values(), flags, strict=True) if ok]
