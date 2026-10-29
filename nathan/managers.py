"""Nathan's management: the premium AI plans you already pay for, lent through their
official command-line tools (each signs in with your own account; no API keys, no scraping):

  Claude   (Claude Code, `claude -p`)    the Manager: final word on reviews and research
  ChatGPT  (Codex CLI, `codex exec`)     Assistant manager, and manager of Programming

Gemini isn't a manager: Google retired plan sign-in for its CLI, and on the free API it's
just another worker (it stays on the free staff).

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
MANAGERS = {m.id: m for m in (CLAUDE, CHATGPT)}
_BIN = {"claude": "claude", "chatgpt": "codex"}


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


def _run(args: list[str], stdin: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as cwd:  # empty folder: no project files leak in
        return subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=cwd, check=False,
                              creationflags=NO_WINDOW)


def signed_in(m: Manager) -> bool:
    cli = m.cli()
    if not cli:
        return False
    try:
        if m.id == "claude":
            return bool(json.loads(_run([cli, "auth", "status", "--json"]).stdout or "{}").get("loggedIn"))
        out = _run([cli, "login", "status"])
        text = (out.stdout + out.stderr).lower()
        return "logged in" in text and "not logged in" not in text
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def state(m: Manager) -> dict:
    cli = m.cli()
    return {"id": m.id, "name": m.name, "title": m.title, "plan": m.plan, "installed": bool(cli),
            "signed_in": bool(cli) and signed_in(m), "package": m.package}


def open_sign_in(m: Manager) -> None:
    """Start the CLI's own sign-in, which opens your browser. On Windows it gets its own
    console window so its instructions are visible."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name}'s helper isn't installed yet: press Install first")
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
    return [cli, *(["--search"] if web else []), "exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only", "-"]


async def ask(m: Manager, prompt: str, *, web: bool = False, timeout: float = TIMEOUT_S) -> str:
    """One question to this manager, answered with your own plan. web=True lets it search
    the live web itself. Raises RuntimeError with a plain reason."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name} isn't connected: install and sign in under Accounts")
    try:
        out = await asyncio.wait_for(asyncio.to_thread(_run, _args(m, cli, web), prompt, timeout),
                                     timeout + 5)
    except (subprocess.TimeoutExpired, TimeoutError) as e:
        raise RuntimeError(f"{m.name} didn't answer within {timeout:.0f}s") from e
    if m.id == "claude":
        try:
            data = json.loads(out.stdout[out.stdout.find("{"):] if "{" in out.stdout else out.stdout)
        except ValueError as e:
            raise RuntimeError(f"{m.name}: {(out.stderr or out.stdout).strip()[-300:] or 'no answer'}") from e
        if data.get("is_error"):
            raise RuntimeError(f"{m.name}: {data.get('result') or 'error'}")
        return str(data.get("result") or "").strip()
    if out.returncode != 0 and not out.stdout.strip():
        raise RuntimeError(f"{m.name}: {out.stderr.strip()[-300:] or 'error'}")
    return out.stdout.strip()


async def connected() -> list[Manager]:
    flags = await asyncio.gather(*(asyncio.to_thread(signed_in, m) for m in MANAGERS.values()))
    return [m for m, ok in zip(MANAGERS.values(), flags, strict=True) if ok]


# ---- who leads -----------------------------------------------------------------------
# You pick the lead manager (ChatGPT by default: cheaper on your plan; Claude: sharper).
# The lead reviews workflows, verifies research, has the final word in performance
# reviews and runs Programming. If it isn't signed in, the other steps in.

def _settings_file() -> Path:
    from nathan.config import data_dir

    return data_dir() / "settings.json"


def chosen() -> str:
    try:
        mid = json.loads(_settings_file().read_text(encoding="utf-8")).get("manager", "chatgpt")
    except (OSError, ValueError):
        mid = "chatgpt"
    return mid if mid in ("chatgpt", "claude") else "chatgpt"


def choose(mid: str) -> str:
    if mid not in ("chatgpt", "claude"):
        raise ValueError("the lead manager is ChatGPT or Claude")
    f = _settings_file()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data["manager"] = mid
    f.write_text(json.dumps(data), encoding="utf-8")
    return mid


def lead_order(team: list[Manager]) -> list[Manager]:
    """Signed-in managers, the chosen lead first, then the other."""
    first = chosen()
    rank = {first: 0, ("claude" if first == "chatgpt" else "chatgpt"): 1}
    return sorted(team, key=lambda m: rank.get(m.id, 9))
