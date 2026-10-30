"""Evolve: Nathan improves its own code, from a request or on its own initiative.

How one improvement goes:
  1. branch    a git worktree (a second checkout of Nathan's source) on branch evolve/<id>,
               so your own working copy is untouched while the work happens
  2. build     the lead manager (Claude Code or Codex, on your own plan) edits that copy,
               following CLAUDE.md, adding tests
  3. verify    the full test suite and the linter run on the copy
  4. you       read the diff, then Apply (merged into your source, committed) or Discard.
               Autopilot applies changes that pass on its own, round after round.
  5. reload    "Rebuild & restart" builds the desktop app from the new source, installs it
               over this one and reopens it.

Why a worktree? Agents make mistakes. A separate checkout plus "tests must pass" means a
bad idea costs nothing: discarding it deletes the branch and the folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from nathan import managers

NO_WINDOW = managers.NO_WINDOW
TEST_CMDS = [["uv", "run", "pytest", "-q", "-x", "-p", "no:cacheprovider"], ["uv", "run", "ruff", "check", "."]]
AGENT_TIMEOUT_S = 40 * 60
MAX_DIFF = 200_000
CLAUDE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(uv run pytest:*)", "Bash(uv run ruff:*)",
                "Bash(uv sync:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(node --test:*)"]

BRIEF = """You are improving Nathan, the app whose source code is this folder. Follow CLAUDE.md.
{task}

Rules:
- Make one coherent, finished change. Keep it small enough to review, and add or update tests.
- Tests must never call real APIs. Run `uv run pytest -q` and `uv run ruff check .` and fix
  anything you broke before you stop.
- Don't commit, push, touch .env, add paid services, or edit providers' keys.
- Finish with one line: SUMMARY: <what changed, in plain words for the owner>"""

AUTONOMOUS = """Choose the single most valuable improvement yourself. Look at docs/ROADMAP.md, the
code, the tests and the web UI (nathan/web/static). Good picks: a real bug, a rough edge in
the UI, something slow, a missing test for risky code, a small feature the roadmap asks for.
Avoid anything already listed as done. {avoid}"""


@dataclass
class Job:
    id: str
    goal: str
    manager: str = ""
    status: str = "starting"  # starting | working | testing | ready | failed | applied | discarded
    log: list[str] = field(default_factory=list)
    summary: str = ""
    diff: str = ""
    stat: str = ""
    tests: str = ""
    passed: bool = False
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    branch: str = ""
    path: str = ""

    def say(self, line: str) -> None:
        self.log.append(line.strip()[:400])
        del self.log[:-400]

    def public(self, full: bool = False) -> dict:
        d = {k: getattr(self, k) for k in ("id", "goal", "manager", "status", "summary", "stat",
                                            "passed", "started", "finished", "branch")}
        d["log"] = self.log[-60:]
        if full:
            d.update(diff=self.diff, tests=self.tests, log=self.log)
        return d


def _git(cwd: Path | str, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=NO_WINDOW, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(out.stderr or out.stdout).strip()[-300:]}")
    return out.stdout


def is_source(path: Path) -> bool:
    try:
        return (path / ".git").exists() and 'name = "nathan"' in (path / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False


def find_source() -> Path | None:
    """Nathan's source checkout: NATHAN_SOURCE, the folder this runs from (dev), or the usual spot."""
    here = Path(__file__).resolve().parents[1]
    for c in (os.getenv("NATHAN_SOURCE"), here, Path.home() / "Documents/Codex/Nathan"):
        if c and is_source(Path(c)):
            return Path(c)
    return None


class Evolver:
    def __init__(self, data: Path, source: Path | None = None) -> None:
        self.data = data
        self.source = source if source is not None else find_source()
        self.jobs: dict[str, Job] = {}
        self.busy = asyncio.Lock()
        self.autopilot: dict = {"on": False, "round": 0, "rounds": 0, "applied": 0}
        self.pending_restart = False

    # ---- state ---------------------------------------------------------------------------

    def status(self) -> dict:
        return {"source": str(self.source or ""), "ready": bool(self.source), "running": self.busy.locked(),
                "frozen": bool(getattr(sys, "frozen", False)), "autopilot": self.autopilot,
                "pending_restart": self.pending_restart,
                "jobs": [j.public() for j in sorted(self.jobs.values(), key=lambda j: -j.started)][:20]}

    def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    # ---- one improvement -------------------------------------------------------------------

    def start(self, goal: str) -> Job:
        if not self.source:
            raise RuntimeError("Nathan's source code wasn't found. Set NATHAN_SOURCE to the folder with its git checkout.")
        if self.busy.locked():
            raise RuntimeError("An improvement is already running.")
        job = Job(uuid.uuid4().hex[:8], goal.strip())
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._guarded(job))  # type: ignore[attr-defined]
        return job

    async def _guarded(self, job: Job) -> None:
        async with self.busy:
            try:
                await self.run(job)
            except Exception as e:  # noqa: BLE001 - the page shows why instead of a silent stop
                job.status = "failed"
                job.say(f"stopped: {e}")
            finally:
                job.finished = time.time()

    async def run(self, job: Job) -> None:
        team = managers.lead_order(await managers.connected())
        if not team:
            raise RuntimeError("Evolve needs Claude or ChatGPT signed in under Accounts.")
        m = team[0]
        job.manager = m.name
        job.branch = f"evolve/{time.strftime('%Y%m%d-%H%M')}-{job.id}"
        work = self.data / "evolve" / job.id
        work.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_git, self.source, "worktree", "add", "-b", job.branch, str(work), "HEAD")
        job.path = str(work)
        job.say(f"{m.name} is working on a separate copy (branch {job.branch}).")
        job.status = "working"

        avoid = self._recent()
        task = f"The owner asked: {job.goal}" if job.goal else AUTONOMOUS.format(avoid=avoid)
        await self._agent(m, BRIEF.format(task=task), work, job)

        job.status = "testing"
        await asyncio.to_thread(_git, work, "add", "-A")
        job.stat = (await asyncio.to_thread(_git, work, "diff", "--cached", "--stat")).strip()
        job.diff = (await asyncio.to_thread(_git, work, "diff", "--cached"))[:MAX_DIFF]
        if not job.diff.strip():
            job.status = "failed"
            job.say("No changes were made.")
            return
        job.say("Running the tests and the linter on the copy…")
        job.passed, job.tests = await self._verify(work)
        job.status = "ready" if job.passed else "failed"
        job.say("Tests pass. Review the diff, then Apply or Discard." if job.passed
                else "Tests or lint failed: see the report. You can still Discard it.")

    def _recent(self) -> str:
        done = [j.summary for j in self.jobs.values() if j.summary and j.status in ("ready", "applied")]
        return ("Already done this session, pick something else: " + "; ".join(done[-5:])) if done else ""

    async def _agent(self, m: managers.Manager, prompt: str, cwd: Path, job: Job) -> None:
        cli = m.cli()
        if m.id == "claude":
            args = [cli, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits",
                    "--allowedTools", *CLAUDE_TOOLS]
        else:
            args = [cli, "exec", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(cwd), "-"]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW, limit=4 * 1024 * 1024)
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

        async def read() -> None:
            async for raw in proc.stdout:
                for line in narrate(raw.decode("utf-8", "replace"), m.id):
                    if line.startswith("SUMMARY:"):
                        job.summary = line[8:].strip()
                    job.say(line)

        try:
            await asyncio.wait_for(read(), AGENT_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            job.say(f"{m.name} ran out of time ({AGENT_TIMEOUT_S // 60} min); keeping what it finished.")
        await proc.wait()

    async def _verify(self, cwd: Path) -> tuple[bool, str]:
        report = []
        for cmd in TEST_CMDS:
            out = await asyncio.to_thread(subprocess.run, cmd, cwd=cwd, capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
                                          timeout=900, check=False)
            report.append(f"$ {' '.join(cmd)}  (exit {out.returncode})\n{(out.stdout + out.stderr).strip()[-3000:]}")
            if out.returncode != 0:
                return False, "\n\n".join(report)
        return True, "\n\n".join(report)

    # ---- your decision ---------------------------------------------------------------------

    def apply(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status != "ready":
            raise RuntimeError("Only a finished change whose tests pass can be applied.")
        if _git(self.source, "status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError("Your source folder has uncommitted changes; commit or stash them first.")
        title = (job.summary or job.goal or "self-improvement")[:68]
        _git(job.path, "-c", "user.name=Nathan", "-c", "user.email=nathan@localhost", "commit", "-qm",
             f"Evolve: {title}\n\n{job.summary or job.goal}\n\nMade by {job.manager} in Nathan's Evolve page; tests passed.")
        _git(self.source, "merge", "--no-edit", job.branch)
        self._cleanup(job)
        job.status = "applied"
        job.say("Applied to your source. Rebuild & restart to run it.")
        self.pending_restart = True
        return job

    def discard(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in ("starting", "working", "testing"):
            task = getattr(job, "task", None)
            if task:
                task.cancel()
        self._cleanup(job)
        job.status = "discarded"
        return job

    def _cleanup(self, job: Job) -> None:
        if job.path:
            _git(self.source, "worktree", "remove", "--force", job.path, check=False)
        if job.branch:
            _git(self.source, "branch", "-D", job.branch, check=False)

    # ---- autopilot: Nathan keeps improving itself ----------------------------------------------

    def start_autopilot(self, rounds: int, goal: str = "") -> None:
        if self.autopilot["on"] or self.busy.locked():
            raise RuntimeError("Already running.")
        self.autopilot = {"on": True, "round": 0, "rounds": max(1, min(rounds, 10)), "applied": 0, "goal": goal}
        self._auto_task = asyncio.create_task(self._autopilot())

    def stop_autopilot(self) -> None:
        self.autopilot["on"] = False

    async def _autopilot(self) -> None:
        try:
            while self.autopilot["on"] and self.autopilot["round"] < self.autopilot["rounds"]:
                self.autopilot["round"] += 1
                job = self.start(self.autopilot.get("goal", "") if self.autopilot["round"] == 1 else "")
                await job.task  # type: ignore[attr-defined]
                if job.status == "ready":
                    try:
                        await asyncio.to_thread(self.apply, job.id)
                    except RuntimeError as e:  # e.g. your source has uncommitted edits: leave it for you
                        job.say(f"Autopilot stopped before applying: {e}")
                        break
                    self.autopilot["applied"] += 1
                else:
                    self.discard(job.id)
        finally:
            self.autopilot["on"] = False

    # ---- reload ------------------------------------------------------------------------------

    def restart(self, pid: int) -> None:
        """Hand off to packaging/update_install.py: it waits for this app to close, rebuilds,
        installs over this copy and reopens it."""
        if not getattr(sys, "frozen", False):
            raise RuntimeError("Running from source: just restart it to load the changes.")
        install = Path(sys.executable).parent
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW) if sys.platform == "win32" else 0
        log = open(self.data / "update.log", "a", encoding="utf-8")  # noqa: SIM115 - child keeps it
        subprocess.Popen(["uv", "run", "python", "packaging/update_install.py", "--pid", str(pid),
                          "--install", str(install)], cwd=self.source, stdout=log, stderr=log,
                         creationflags=flags, close_fds=True)


def narrate(raw: str, kind: str) -> list[str]:
    """Turn an agent's output line into short human lines for the page."""
    raw = raw.strip()
    if not raw:
        return []
    if kind != "claude":
        return [raw[:400]]
    try:
        ev = json.loads(raw)
    except ValueError:
        return [raw[:400]]
    out = []
    if ev.get("type") == "assistant":
        for part in ev.get("message", {}).get("content", []):
            if part.get("type") == "text" and part.get("text", "").strip():
                out += [ln for ln in part["text"].strip().splitlines() if ln.strip()][-6:]
            elif part.get("type") == "tool_use":
                inp = part.get("input", {})
                what = Path(inp["file_path"]).name if inp.get("file_path") else (inp.get("pattern") or inp.get("command") or "")
                out.append(f"→ {part.get('name')} {str(what)[:160]}")
    elif ev.get("type") == "result" and ev.get("result"):
        out += [ln for ln in str(ev["result"]).splitlines() if ln.startswith("SUMMARY:")]
    return out
