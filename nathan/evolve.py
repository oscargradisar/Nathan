"""Evolve: Nathan improves its own code, from a request or on its own initiative.

How one improvement goes:
  1. branch    a git worktree (a second checkout of Nathan's source) on branch evolve/<id>,
               so your own working copy is untouched while the work happens
  2. build     Claude Code (the overlord; Codex steps in when Claude isn't signed in), on
               your own plan, edits that copy, following CLAUDE.md, adding tests
  3. verify    the linter and the full test suite run on the copy
  4. you       read the diff, then Apply (merged into your source, committed) or Discard.
               Autopilot applies changes that pass on its own, round after round.
  5. reload    "Rebuild & restart" builds the desktop app from the new source, installs it
               over this one and reopens it.

Ideas: "Ask Claude for ideas" lets Claude read the source (read-only) and propose a ranked
list of improvements. You can build any of them with one click, and Autopilot works down
the list before it falls back to choosing on its own.

Why a worktree? Agents make mistakes. A separate checkout plus "tests must pass" means a
bad idea costs nothing: discarding it deletes the branch and the folder.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path

from nathan import managers
from nathan.jsonutil import extract_json

NO_WINDOW = managers.NO_WINDOW
# The linter takes seconds and the tests take minutes: lint first, so a lint error fails fast.
TEST_CMDS = [["uv", "run", "ruff", "check", "."], ["uv", "run", "pytest", "-q", "-x", "-p", "no:cacheprovider"]]
VERIFY_TIMEOUT_S = 15 * 60
AGENT_TIMEOUT_S = 40 * 60
SUGGEST_TIMEOUT_S = 10 * 60
MAX_DIFF = 200_000
LIVE = ("starting", "working", "testing")
KINDS = ("bug", "speed", "capability", "ui")
SIZES = ("small", "medium", "large")
CLAUDE_TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash(uv run pytest:*)", "Bash(uv run ruff:*)",
                "Bash(uv sync:*)", "Bash(git diff:*)", "Bash(git status:*)", "Bash(git log:*)",
                "Bash(node --test:*)"]

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
Run `git log --oneline -30` first and avoid anything already done there or in the roadmap. {avoid}"""

SUGGEST = """This folder is Nathan's source code. Read CLAUDE.md first: every idea must respect its
hard rules (free official APIs only, no paid services, no heavy wrapper frameworks, model names
only in providers.yaml, tests never call real APIs). Then look at docs/ROADMAP.md, the code in
nathan/, tests/ and the web UI in nathan/web/static. Only read: change nothing.

Propose 6 to 8 improvements, most valuable first: real bugs, slow paths, risky code without tests,
unfinished roadmap items, features that make Nathan more capable or more autonomous, rough edges
in the UI. Each must be one finished change that fits a single reviewable job.
Skip anything already done or in progress. Recent commits:
{log}
{avoid}

Reply with JSON only:
{{"ideas": [{{"title": "short imperative title", "why": "one or two sentences: what is wrong or missing, and why it matters", "kind": "bug|speed|capability|ui", "size": "small|medium|large"}}]}}"""


@dataclass
class Job:
    id: str
    goal: str
    manager: str = ""
    status: str = "starting"  # starting | working | testing | ready | failed | conflict | applied | discarded
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
    # Runtime only (never saved): the background task, and whether Discard asked it to stop.
    task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    discarding: bool = field(default=False, repr=False, compare=False)

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


SAVED = [f.name for f in fields(Job) if f.name not in ("task", "discarding")]


def _git(cwd: Path | str, *args: str, check: bool = True) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace", creationflags=NO_WINDOW, check=False)
    if check and out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(out.stderr or out.stdout).strip()[-300:]}")
    return out.stdout


def _have_uv() -> bool:
    return shutil.which("uv") is not None


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


def pick_builder(team: list[managers.Manager]) -> managers.Manager | None:
    """Claude is the overlord: it builds whenever it's signed in. Otherwise the lead steps in."""
    claude = next((m for m in team if m.id == "claude"), None)
    return claude or next(iter(managers.lead_order(team)), None)


def parse_ideas(text: str) -> list[dict]:
    """Ideas from the manager's reply (JSON, maybe wrapped in prose). Bad entries are skipped."""
    try:
        raw = extract_json(text)
    except ValueError:
        return []
    items = raw.get("ideas", []) if isinstance(raw, dict) else raw
    ideas = []
    for i in items if isinstance(items, list) else []:
        if not isinstance(i, dict) or not str(i.get("title") or "").strip():
            continue
        kind, size = str(i.get("kind", "")).lower(), str(i.get("size", "")).lower()
        ideas.append({"id": uuid.uuid4().hex[:8], "title": str(i["title"]).strip()[:120],
                      "why": str(i.get("why") or "").strip()[:500],
                      "kind": kind if kind in KINDS else "capability",
                      "size": size if size in SIZES else "medium"})
    return ideas[:8]


def idea_goal(idea: dict) -> str:
    return f"{idea['title']}. {idea['why']}" if idea.get("why") else idea["title"]


class Evolver:
    def __init__(self, data: Path, source: Path | None = None) -> None:
        self.data = data
        self.source = source if source is not None else find_source()
        self.jobs: dict[str, Job] = {}
        self.busy = asyncio.Lock()
        self.autopilot: dict = {"on": False, "round": 0, "rounds": 0, "applied": 0, "error": ""}
        self.pending_restart = False
        self.suggesting = False
        self.suggest_error = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._swept = False
        self._save_lock = threading.Lock()  # apply/discard save from worker threads
        self._load_jobs()
        self.ideas: list[dict] = [i for i in self._load("ideas.json") if isinstance(i, dict) and i.get("title")]

    # ---- state ---------------------------------------------------------------------------

    def running(self) -> bool:
        # Read on the event loop, where start() also runs, so check-then-start can't race.
        return any(j.status in LIVE for j in self.jobs.values())

    def status(self) -> dict:
        return {"source": str(self.source or ""), "ready": bool(self.source), "running": self.running(),
                "frozen": bool(getattr(sys, "frozen", False)), "autopilot": self.autopilot,
                "pending_restart": self.pending_restart, "ideas": self.ideas,
                "suggesting": self.suggesting, "suggest_error": self.suggest_error,
                "jobs": [j.public() for j in sorted(self.jobs.values(), key=lambda j: -j.started)][:20]}

    def get(self, job_id: str) -> Job:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    # ---- saved between restarts (Apply usually ends in a restart) ------------------------

    def _load(self, name: str) -> list:
        try:
            data = json.loads((self.data / "evolve" / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _save(self, name: str, items: list) -> None:
        folder = self.data / "evolve"
        with self._save_lock:
            try:
                folder.mkdir(parents=True, exist_ok=True)
                tmp = folder / f"{name}.tmp"
                tmp.write_text(json.dumps(items), encoding="utf-8")
                tmp.replace(folder / name)  # all or nothing: a crash never leaves half a file
            except OSError:
                pass

    def _load_jobs(self) -> None:
        for d in self._load("jobs.json"):
            try:
                job = Job(**{k: v for k, v in d.items() if k in SAVED})
            except (TypeError, AttributeError):
                continue
            if job.status in LIVE:
                job.status = "failed"
                job.say("Interrupted by a restart.")
            elif job.status == "ready" and not Path(job.path).is_dir():
                job.status = "failed"
                job.say("Its working copy is gone.")
            self.jobs[job.id] = job

    def _save_jobs(self) -> None:
        keep = sorted(self.jobs.values(), key=lambda j: -j.started)[:30]
        rows = []
        for j in keep:
            row = {k: getattr(j, k) for k in SAVED}
            row["log"] = j.log[-100:]
            if j.status != "ready":
                row["diff"] = ""  # only a change you can still apply needs its full diff
            rows.append(row)
        self._save("jobs.json", rows)

    def _save_ideas(self) -> None:
        self._save("ideas.json", self.ideas)

    # ---- one improvement -------------------------------------------------------------------

    def start(self, goal: str, idea: str = "", *, auto: bool = False) -> Job:
        """Start building `goal`. `idea` is the id of a suggested idea it came from (it leaves
        the list). Runs on the event loop: the checks and the new job happen in one step."""
        if not self.source:
            raise RuntimeError("Nathan's source code wasn't found. Set NATHAN_SOURCE to the folder with its git checkout.")
        if not _have_uv():
            raise RuntimeError("Evolve needs uv on PATH to test changes (free: https://astral.sh/uv).")
        if self.running():
            raise RuntimeError("An improvement is already running.")
        if self.autopilot["on"] and not auto:
            raise RuntimeError("Autopilot is running. Stop it first.")
        if idea:
            self.ideas = [i for i in self.ideas if i.get("id") != idea]
            self._save_ideas()
        self._loop = asyncio.get_running_loop()
        job = Job(uuid.uuid4().hex[:8], goal.strip())
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._guarded(job))
        return job

    async def _guarded(self, job: Job) -> None:
        try:
            async with self.busy:
                await self.run(job)
        except Exception as e:  # noqa: BLE001 - the page shows why instead of a silent stop
            job.status = "failed"
            job.say(f"stopped: {e}")
        finally:
            job.finished = time.time()
            if job.discarding:  # Discard while it ran: the agent has exited now, so tidy up
                job.status = "discarded"
                await asyncio.to_thread(self._cleanup, job)
            self._save_jobs()

    async def run(self, job: Job) -> None:
        if not self._swept:
            self._swept = True
            await asyncio.to_thread(self._sweep)
        m = pick_builder(await managers.connected())
        if m is None:
            raise RuntimeError("Evolve needs Claude or ChatGPT signed in under Accounts.")
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
        job.say("Running the linter and the tests on the copy…")
        job.passed, job.tests = await self._verify(work)
        job.status = "ready" if job.passed else "failed"
        job.say("Tests pass. Review the diff, then Apply or Discard." if job.passed
                else "Tests or lint failed: see the report. You can still Discard it.")

    def _recent(self) -> str:
        """What's already done or under way, so the next pick (or idea list) doesn't repeat it."""
        done = [j.summary or j.goal for j in sorted(self.jobs.values(), key=lambda j: j.started)
                if (j.summary or j.goal) and j.status in ("ready", "applied", *LIVE)]
        return ("Already done or in progress, pick something else: "
                + "; ".join(d[:160] for d in done[-8:])) if done else ""

    async def _agent(self, m: managers.Manager, prompt: str, cwd: Path, job: Job) -> None:
        cli = m.cli()
        if m.id == "claude":
            args = [cli, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                    "--permission-mode", "acceptEdits", "--allowedTools", *CLAUDE_TOOLS]
        else:
            args = [cli, "exec", "--skip-git-repo-check", "-s", "workspace-write", "-C", str(cwd), "-"]
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=cwd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, creationflags=NO_WINDOW, limit=4 * 1024 * 1024,
            start_new_session=sys.platform != "win32")

        async def read() -> None:
            async for raw in proc.stdout:
                for line in narrate(raw.decode("utf-8", "replace"), m.id):
                    if line.startswith("SUMMARY:"):
                        job.summary = line[8:].strip()
                    job.say(line)

        try:
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                await asyncio.wait_for(read(), AGENT_TIMEOUT_S)
                await proc.wait()
            except TimeoutError:
                job.say(f"{m.name} ran out of time ({AGENT_TIMEOUT_S // 60} min); keeping what it finished.")
        finally:
            # Timeout, Stop & discard or a crash: the agent must not keep editing (or using
            # your plan). Kill the whole tree: on Windows the CLI is a .cmd shim around node.
            if proc.returncode is None:
                managers.kill_tree(proc.pid)
            await proc.wait()

    async def _verify(self, cwd: Path) -> tuple[bool, str]:
        report = []
        for cmd in TEST_CMDS:
            line = " ".join(cmd)
            try:  # run_process kills the whole tree on timeout or Stop (uv -> python -> pytest)
                out = await managers.run_process(cmd, None, VERIFY_TIMEOUT_S, cwd)
            except TimeoutError:
                report.append(f"$ {line}\ntimed out after {VERIFY_TIMEOUT_S // 60} min")
                return False, "\n\n".join(report)
            report.append(f"$ {line}  (exit {out.returncode})\n{(out.stdout + out.stderr).strip()[-3000:]}")
            if out.returncode != 0:
                return False, "\n\n".join(report)
        return True, "\n\n".join(report)

    # ---- your decision (these run in a worker thread: git can take a while) -----------------

    def apply(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status != "ready":
            raise RuntimeError("Only a finished change whose tests pass can be applied.")
        if _git(self.source, "status", "--porcelain", "--untracked-files=no").strip():
            raise RuntimeError("Your source folder has uncommitted changes; commit or stash them first.")
        title = (job.summary or job.goal or "self-improvement")[:68]
        if _git(job.path, "status", "--porcelain").strip():  # already committed by an earlier try?
            _git(job.path, "add", "-A")
            _git(job.path, "-c", "user.name=Nathan", "-c", "user.email=nathan@localhost", "commit", "-qm",
                 f"Evolve: {title}\n\n{job.summary or job.goal}\n\nMade by {job.manager} in Nathan's Evolve page; tests passed.")
        try:
            _git(self.source, "merge", "--no-edit", job.branch)
        except RuntimeError as e:
            # Never leave your source half-merged with conflict markers in it.
            _git(self.source, "merge", "--abort", check=False)
            job.status = "conflict"
            job.say("Couldn't apply: your source changed in the same places since this copy was made.")
            self._save_jobs()
            raise RuntimeError("This change conflicts with edits made to your source since it was built, "
                               "so nothing was merged and your source is as it was. Discard it and run "
                               "it again.") from e
        self._cleanup(job)
        job.status = "applied"
        job.say("Applied to your source. Rebuild & restart to run it.")
        self.pending_restart = True
        self._save_jobs()
        return job

    def discard(self, job_id: str) -> Job:
        job = self.get(job_id)
        job.discarding = True
        task = job.task
        if task is not None and not task.done() and self._loop is not None:
            # This usually runs in a worker thread, and an asyncio task may only be cancelled
            # from its own loop. Its finally block cleans up once the agent has really exited.
            self._loop.call_soon_threadsafe(task.cancel)
            job.say("Stopping…")
        else:
            self._cleanup(job)
        job.status = "discarded"
        self._save_jobs()
        return job

    def _cleanup(self, job: Job) -> None:
        if job.path:
            for _ in range(3):
                _git(self.source, "worktree", "remove", "--force", job.path, check=False)
                if not Path(job.path).exists():
                    break
                time.sleep(1)  # Windows: a process that just died can hold a file for a moment
            shutil.rmtree(job.path, ignore_errors=True)
            _git(self.source, "worktree", "prune", check=False)  # forget a folder we deleted by hand
        if job.branch:
            _git(self.source, "branch", "-D", job.branch, check=False)

    def _sweep(self) -> None:
        """Once per session: remove copies and branches that earlier sessions left behind
        (a restart forgets running jobs; a locked file can make a removal fail)."""
        keep = {j.id for j in self.jobs.values() if j.status == "ready" or j.status in LIVE}
        root = self.data / "evolve"
        _git(self.source, "worktree", "prune", check=False)
        if root.is_dir():
            for d in root.iterdir():
                if d.is_dir() and d.name not in keep:
                    _git(self.source, "worktree", "remove", "--force", str(d), check=False)
                    shutil.rmtree(d, ignore_errors=True)
        _git(self.source, "worktree", "prune", check=False)
        for j in self.jobs.values():  # finished jobs we know about: their branches can go
            if j.branch and j.id not in keep:
                _git(self.source, "branch", "-D", j.branch, check=False)
        # Unknown evolve/* branches: delete only the merged ones; an unmerged one may hold work.
        merged = _git(self.source, "branch", "--merged", "HEAD", "--format=%(refname:short)",
                      "--list", "evolve/*", check=False).split()
        live = {j.branch for j in self.jobs.values() if j.id in keep}
        for b in merged:
            if b not in live:
                _git(self.source, "branch", "-d", b, check=False)

    # ---- ideas: Claude reads the code and proposes what to build ---------------------------

    def start_suggest(self) -> None:
        if not self.source:
            raise RuntimeError("Nathan's source code wasn't found. Set NATHAN_SOURCE to the folder with its git checkout.")
        if self.suggesting:
            raise RuntimeError("Already reading the code for ideas.")
        self.suggesting, self.suggest_error = True, ""  # set now, so a double click can't start two
        self._loop = asyncio.get_running_loop()
        self._suggest_task = asyncio.create_task(self._suggest())

    async def _suggest(self) -> None:
        try:
            m = pick_builder(await managers.connected())
            if m is None:
                raise RuntimeError("Ideas need Claude (or ChatGPT) signed in under Accounts.")
            log = await asyncio.to_thread(_git, self.source, "log", "--oneline", "-30", check=False)
            prompt = SUGGEST.format(log=log.strip() or "(none)", avoid=self._recent())
            # cwd: it reads the real source, read-only (Claude: Read/Glob/Grep; Codex: read-only sandbox)
            text = await managers.ask(m, prompt, cwd=self.source, timeout=SUGGEST_TIMEOUT_S)
            ideas = parse_ideas(text)
            if not ideas:
                raise RuntimeError(f"{m.name} didn't return any ideas. Try again.")
            self.ideas = ideas
            self._save_ideas()
        except Exception as e:  # noqa: BLE001 - shown on the page
            self.suggest_error = str(e) or type(e).__name__
        finally:
            self.suggesting = False

    # ---- autopilot: Nathan keeps improving itself ----------------------------------------------

    def start_autopilot(self, rounds: int, goal: str = "") -> None:
        if not self.source:
            raise RuntimeError("Nathan's source code wasn't found. Set NATHAN_SOURCE to the folder with its git checkout.")
        if self.autopilot["on"] or self.running():
            raise RuntimeError("Already running.")
        self.autopilot = {"on": True, "round": 0, "rounds": max(1, min(rounds, 10)), "applied": 0,
                          "goal": goal, "error": ""}
        self._auto_task = asyncio.create_task(self._autopilot())

    def stop_autopilot(self) -> None:
        self.autopilot["on"] = False

    async def _autopilot(self) -> None:
        try:
            while self.autopilot["on"] and self.autopilot["round"] < self.autopilot["rounds"]:
                self.autopilot["round"] += 1
                goal = self.autopilot.get("goal", "") if self.autopilot["round"] == 1 else ""
                idea = ""
                if not goal and self.ideas:  # work down Claude's ranked ideas, then choose freely
                    idea, goal = self.ideas[0]["id"], idea_goal(self.ideas[0])
                try:
                    job = self.start(goal, idea, auto=True)
                except RuntimeError as e:  # e.g. uv missing: say why on the page
                    self.autopilot["error"] = str(e)
                    break
                await asyncio.wait({job.task})  # never raises, even if you discarded the job
                if job.status == "ready":
                    try:
                        await asyncio.to_thread(self.apply, job.id)
                    except RuntimeError as e:  # e.g. your source has uncommitted edits: leave it for you
                        job.say(f"Autopilot stopped before applying: {e}")
                        self.autopilot["error"] = str(e)
                        break
                    self.autopilot["applied"] += 1
                elif job.status != "discarded":
                    await asyncio.to_thread(self.discard, job.id)
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
