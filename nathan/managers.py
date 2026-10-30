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
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# Background checks must not flash console windows on Windows
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

TIMEOUT_S = 300.0
SEEN_TTL_S = 60.0  # how long a sign-in check is trusted (each check starts a Node process)


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


# ---- running the CLIs ------------------------------------------------------------------
# On Windows the CLIs are usually npm shims (claude.cmd -> cmd.exe -> node). Killing the
# process we started would only end cmd.exe and leave node working (and spending your plan),
# so we always kill the whole process tree.

def kill_tree(pid: int) -> None:
    """Stop a process and everything it started."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=NO_WINDOW, check=False)
    else:
        try:  # started with start_new_session=True, so its group id is its pid
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        kill_tree(proc.pid)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _spawn(args: list[str], cwd: Path | str) -> subprocess.Popen:
    """Start a process (quick: waiting for it is what's slow, and that happens in a thread).
    On POSIX it gets its own process group, so kill_tree can stop its children too."""
    return subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                            cwd=cwd, creationflags=NO_WINDOW, start_new_session=sys.platform != "win32")


async def run_process(args: list[str], stdin: str | None = None, timeout: float = 30,
                      cwd: Path | str | None = None) -> subprocess.CompletedProcess:
    """Run a command without blocking the event loop, and make sure it dies with us.

    Why not asyncio.to_thread(subprocess.run)? A thread can't be cancelled: when the Stop
    button (or a timeout) cancels the caller, the CLI would keep running for minutes. Here a
    timeout raises TimeoutError and a cancel raises CancelledError, and both kill the process
    tree first. Without cwd it runs in an empty temp folder, so no project files leak in.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:
        proc = _spawn(args, cwd or empty)
        try:
            out, err = await asyncio.wait_for(asyncio.to_thread(proc.communicate, stdin), timeout)
        except BaseException:  # TimeoutError or CancelledError: stop the CLI, then pass it on
            _stop(proc)
            raise
        return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _run(args: list[str], stdin: str | None = None, timeout: float = 30) -> subprocess.CompletedProcess:
    """Quick blocking check (sign-in status), in an empty folder."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:
        return subprocess.run(args, input=stdin, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout, cwd=empty, check=False,
                              creationflags=NO_WINDOW)


# ---- who is signed in --------------------------------------------------------------------

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


# Every workflow, research, code and Evolve run asks who is signed in. Each answer costs a
# Node process start (~0.5-1s), so remember it for a minute. Signing in, installing or
# changing the lead forgets it, so those changes show up right away.
_seen: dict[str, tuple[float, bool]] = {}


def forget() -> None:
    """Drop the remembered sign-in answers."""
    _seen.clear()


def is_signed_in(m: Manager, fresh: bool = False) -> bool:
    hit = _seen.get(m.id)
    if hit and not fresh and time.monotonic() - hit[0] < SEEN_TTL_S:
        return hit[1]
    ok = signed_in(m)
    _seen[m.id] = (time.monotonic(), ok)
    return ok


def state(m: Manager) -> dict:
    # The Accounts page always checks for real (it polls while you sign in) and refreshes
    # the remembered answer for everyone else.
    cli = m.cli()
    return {"id": m.id, "name": m.name, "title": m.title, "plan": m.plan, "installed": bool(cli),
            "signed_in": bool(cli) and is_signed_in(m, fresh=True), "package": m.package}


def open_sign_in(m: Manager) -> None:
    """Start the CLI's own sign-in, which opens your browser. On Windows it gets its own
    console window so its instructions are visible."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name}'s helper isn't installed yet: press Install first")
    forget()
    args = {"claude": [cli, "auth", "login"], "chatgpt": [cli, "login"]}[m.id]
    flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
    subprocess.Popen(args, creationflags=flags)


def install(m: Manager) -> str:
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        raise RuntimeError("Installing needs Node.js first (free, nodejs.org).")
    forget()
    out = subprocess.run([npm, "install", "-g", m.package], capture_output=True, text=True,
                         timeout=900, check=False, creationflags=NO_WINDOW)
    if out.returncode != 0 or not m.cli():
        raise RuntimeError(out.stderr.strip()[-400:] or f"couldn't install {m.package}")
    return m.cli() or ""


async def connected() -> list[Manager]:
    """Signed-in managers. The checks run side by side, and answers are reused for a minute."""
    flags = await asyncio.gather(*(asyncio.to_thread(is_signed_in, m) for m in MANAGERS.values()))
    return [m for m, ok in zip(MANAGERS.values(), flags, strict=True) if ok]


# ---- asking -------------------------------------------------------------------------------

READ_TOOLS = ["Read", "Glob", "Grep"]
NO_CHANGES = ["Edit", "Write", "NotebookEdit", "Bash"]


def _args(m: Manager, cli: str, web: bool, images: list[Path] = (), read: bool = False) -> list[str]:
    if m.id == "claude":
        args = [cli, "-p", "--output-format", "json", "--no-session-persistence"]
        tools = (["WebSearch", "WebFetch"] if web else []) + (READ_TOOLS if read
                                                              else ["Read"] if images else [])
        args += [a for d in {str(i.parent) for i in images} for a in ("--add-dir", d)]
        if read:  # reading a real folder: belt and braces, it may look but never change
            args += ["--disallowedTools", *NO_CHANGES]
        return args + (["--allowedTools", *tools] if tools else [])
    return [cli, *(["--search"] if web else []), "exec", "--skip-git-repo-check", "--ephemeral",
            "-s", "read-only", *[a for i in images for a in ("-i", str(i))], "-"]


async def ask(m: Manager, prompt: str, *, web: bool = False, timeout: float = TIMEOUT_S,
              images: list[Path] = (), cwd: Path | None = None) -> str:
    """One question to this manager, answered with your own plan. web=True lets it search
    the live web itself; images are attached (ChatGPT) or read from disk (Claude); cwd lets
    it read (never change) the files in that folder. Raises RuntimeError with a plain reason.
    Cancelling the call stops the CLI too."""
    cli = m.cli()
    if not cli:
        raise RuntimeError(f"{m.name} isn't connected: install and sign in under Accounts")
    if images and m.id == "claude":
        prompt += "\n\nThe image(s): " + ", ".join(str(i) for i in images) + " (open them with Read)."
    try:
        out = await run_process(_args(m, cli, web, images, read=cwd is not None), prompt, timeout, cwd)
    except TimeoutError as e:
        raise RuntimeError(f"{m.name} didn't answer within {timeout:.0f}s") from e
    except OSError as e:
        forget()  # the CLI vanished (uninstalled?): check again next time
        raise RuntimeError(f"{m.name} couldn't start: {e}") from e
    if m.id == "claude":
        try:
            data = json.loads(out.stdout[out.stdout.find("{"):] if "{" in out.stdout else out.stdout)
        except ValueError as e:
            forget()  # often "not signed in any more"
            raise RuntimeError(f"{m.name}: {(out.stderr or out.stdout).strip()[-300:] or 'no answer'}") from e
        if data.get("is_error"):
            raise RuntimeError(f"{m.name}: {data.get('result') or 'error'}")
        return str(data.get("result") or "").strip()
    if out.returncode != 0 and not out.stdout.strip():
        forget()
        raise RuntimeError(f"{m.name}: {out.stderr.strip()[-300:] or 'error'}")
    return out.stdout.strip()


# ---- who leads -----------------------------------------------------------------------
# You pick the lead manager (Claude by default: the sharpest, and the Overlord; ChatGPT:
# lighter on your plan). The lead reviews workflows, verifies research, has the final word
# in performance reviews and runs Programming. If it isn't signed in, the other steps in.

def _settings_file() -> Path:
    from nathan.config import data_dir

    return data_dir() / "settings.json"


def chosen() -> str:
    try:
        mid = json.loads(_settings_file().read_text(encoding="utf-8")).get("manager", "claude")
    except (OSError, ValueError):
        mid = "claude"
    return mid if mid in ("chatgpt", "claude") else "claude"


def choose(mid: str) -> str:
    if mid not in ("chatgpt", "claude"):
        raise ValueError("the lead manager is ChatGPT or Claude")
    forget()  # the owner is looking at the team again: re-check who is really there
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
