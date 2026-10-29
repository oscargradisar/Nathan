"""Programming: ChatGPT manages a team of free coding models.

  1. spec      the manager (ChatGPT; else Claude or Gemini; else a free planner) decides
               the files and what each must do
  2. coders    free code-tagged models write the files in parallel (with backups)
  3. review    the manager reviews every file and returns the corrected final versions
  4. save      files land in workspace/code/<project>/ (paths locked to that folder)

Events: phase, working, delivered, task_failed (roles: manager, coder, reviewer), saved, final.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from nathan.errors import NoModelAvailable
from nathan.jsonutil import extract_json, race_json
from nathan.tools.files import safe_path

Emit = Callable[[dict[str, Any]], Awaitable[None] | None]
MAX_FILES = 6

SPEC_BRIEF = """You are the engineering manager. Plan the smallest set of files (1-{n}) that fully
solves the request. For each file say exactly what it must contain, including the functions
or classes other files rely on, so separate programmers can write them in parallel.
Prefer one file for small tasks. Include a short README.md only for multi-file projects.
Reply with ONLY JSON:
{{"project": "short-kebab-name", "language": "python",
  "files": [{{"path": "main.py", "purpose": "what this file must do, with signatures"}}],
  "notes": "how the pieces fit together, how to run it"}}

REQUEST:
{task}"""

CODER_SYSTEM = """You are a senior programmer on a team. Write ONLY the file you are given,
complete and runnable, matching the spec and the interfaces of the other files. Reply with
just the file contents in one fenced code block. No explanations."""

REVIEW_BRIEF = """You are the engineering manager reviewing your team's code for this request.
Fix bugs, missing pieces, mismatched interfaces and security problems. Then output EVERY final
file, each as:

### path/to/file
```language
...full contents...
```

Finish with a section "## Review" listing what you changed and how to run it.

REQUEST:
{task}

PLAN:
{plan}

TEAM'S FILES:
{files}"""


class FileSpec(BaseModel):
    path: str
    purpose: str = ""


class Spec(BaseModel):
    project: str = "project"
    language: str = "python"
    files: list[FileSpec] = Field(default_factory=list)
    notes: str = ""


async def _emit(emit: Emit | None, event: dict[str, Any]) -> None:
    if emit is not None:
        out = emit(event)
        if asyncio.iscoroutine(out):
            await out


_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)
_FILE = re.compile(r"^###\s+`?([^\n`]+?)`?\s*\n+```[\w+-]*\n(.*?)```", re.DOTALL | re.MULTILINE)


def code_of(text: str) -> str:
    m = _FENCE.search(text)
    return (m.group(1) if m else text).rstrip() + "\n"


def files_of(text: str) -> dict[str, str]:
    """'### path' + fenced block pairs from a review."""
    return {p.strip(): body.rstrip() + "\n" for p, body in _FILE.findall(text)}


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")[:40] or "project"


def save(workspace, project: str, files: dict[str, str]) -> tuple[str, list[str]]:
    root = safe_path(workspace, f"code/{slug(project)}")
    written = []
    for rel, body in files.items():
        target = safe_path(root, rel.lstrip("/\\"))  # no ../ escapes out of the project
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        written.append(target.relative_to(root).as_posix())
    return str(root), written


async def _manager_ask(team, prompt: str, ask, emit: Emit | None, unit: str, role: str):
    for m in team:
        await _emit(emit, {"type": "working", "id": unit, "worker": m.name, "role": role})
        try:
            text = await ask(m, prompt)
            await _emit(emit, {"type": "delivered", "id": unit, "worker": m.name, "role": role,
                               "text": text[:1500], "seconds": None})
            return m.name, text
        except RuntimeError as e:
            await _emit(emit, {"type": "task_failed", "id": unit, "worker": m.name, "role": role,
                               "reason": str(e)})
    return None


async def run_code(engine, task: str, emit: Emit | None = None, team=None, ask=None) -> dict:
    from nathan import managers
    from nathan.workflow import hedged

    ask = ask or managers.ask
    connected = team if team is not None else await managers.connected()
    team = managers.lead_order(connected)  # the lead manager runs Programming

    # 1. spec
    await _emit(emit, {"type": "phase", "phase": "planning"})
    spec, manager = None, ""
    got = await _manager_ask(team, SPEC_BRIEF.format(n=MAX_FILES, task=task), ask, emit, "manager", "manager")
    if got:
        try:
            spec, manager = Spec.model_validate(extract_json(got[1])), got[0]
        except (ValueError, ValidationError):
            spec = None
    if spec is None:
        await _emit(emit, {"type": "working", "id": "manager", "worker": "free planner", "role": "manager"})
        spec, manager = await race_json(engine.client, engine.chain(["code", "reasoning"])[:6], [
            {"role": "user", "content": SPEC_BRIEF.format(n=MAX_FILES, task=task)}], Spec, temperature=0.2)
        await _emit(emit, {"type": "delivered", "id": "manager", "worker": manager, "role": "manager",
                           "text": "\n".join(f"{f.path}: {f.purpose}" for f in spec.files), "seconds": None})
    spec.files = spec.files[:MAX_FILES] or [FileSpec(path="main.py", purpose=task)]
    await _emit(emit, {"type": "plan", "planner": manager, "synthesis": spec.notes,
                       "tasks": [{"id": f.path, "instruction": f.purpose, "depends_on": [], "tags": ["code"],
                                  "worker": ""} for f in spec.files]})

    # 2. coders
    await _emit(emit, {"type": "phase", "phase": "working"})
    plan = "\n".join(f"- {f.path}: {f.purpose}" for f in spec.files) + f"\nNotes: {spec.notes}"
    coders = engine.chain(["code"])
    written: dict[str, str] = {}

    async def write(i: int, f: FileSpec) -> None:
        chain = coders[i % max(1, len(coders)):] + coders[:i % max(1, len(coders))]
        res = await hedged(engine, f.path, chain, [
            {"role": "system", "content": CODER_SYSTEM},
            {"role": "user", "content": f"REQUEST: {task}\n\nTEAM PLAN ({spec.language}):\n{plan}"
                                        f"\n\nYOUR FILE: {f.path}\nIT MUST: {f.purpose}"}], emit, role="coder")
        if res:
            written[f.path] = code_of(res[1].text)

    await asyncio.gather(*(write(i, f) for i, f in enumerate(spec.files)))
    if not written:
        raise NoModelAvailable("no coder could write the files")

    # 3. review: the manager fixes and returns final files; else a free reviewer
    await _emit(emit, {"type": "phase", "phase": "reviewing"})
    files_text = "\n\n".join(f"### {p}\n```\n{c}```" for p, c in written.items())
    brief = REVIEW_BRIEF.format(task=task, plan=plan, files=files_text)
    reviewed = await _manager_ask(team, brief, ask, emit, "reviewer", "reviewer")
    if reviewed is None:
        done = await hedged(engine, "reviewer", engine.judges() + engine.chain(["code"]),
                            [{"role": "user", "content": brief}], emit, role="reviewer")
        reviewed = (done[0], done[1].text) if done else ("", files_text + "\n\n## Review\nNot reviewed.")
    final_files = files_of(reviewed[1]) or written

    # 4. save
    folder, saved = save(engine.workspace, spec.project, final_files)
    await _emit(emit, {"type": "saved", "folder": folder, "files": saved})
    report = reviewed[1] if files_of(reviewed[1]) else files_text + "\n\n" + reviewed[1]
    report += f"\n\n**Saved to** `{folder}`"
    await _emit(emit, {"type": "final", "text": report, "writer": reviewed[0] or manager, "mode": "code"})
    return {"folder": folder, "files": saved, "manager": manager, "reviewer": reviewed[0]}
