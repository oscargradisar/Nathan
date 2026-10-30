"""Jobs: separate pieces of work that never mix.

A job (say "CHEM 1111 lab reports" or "Trip to Lisbon") has its own name, notes, history,
memory and files folder. While a run belongs to a job:
  - memory only recalls things said inside that job (and saves new things tagged with it),
  - the agent's file tools are locked to workspace/jobs/<job>/,
  - the job's notes ride along as context, so Nathan knows what this job is about.
Runs outside any job see only un-jobbed memory, so nothing leaks either way.

Why a ContextVar? It's a per-task global: set it once when a run starts and every await
underneath (memory, tools, sub-tasks) sees the same job, while other runs at the same time
see theirs. No need to thread a job argument through every function.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from contextvars import ContextVar
from pathlib import Path

current: ContextVar[dict | None] = ContextVar("nathan_job", default=None)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "job"


def workspace(base: Path) -> Path:
    """The file tools' root: the job's own folder while a job is active."""
    job = current.get()
    if not job:
        return base
    path = base / "jobs" / f"{slug(job['name'])}-{job['id'][:4]}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def brief(job: dict) -> str:
    notes = job.get("notes", "").strip()
    return f"This request is part of the job \"{job['name']}\"." + (f" Job notes: {notes}" if notes else "")


class JobStore:
    def __init__(self, data: Path) -> None:
        self.file = data / "jobs.json"

    def _load(self) -> list[dict]:
        try:
            return json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _save(self, jobs: list[dict]) -> None:
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(jobs, indent=1), encoding="utf-8")
        tmp.replace(self.file)  # never a half-written file

    def list(self, archived: bool = False) -> list[dict]:
        return [j for j in self._load() if bool(j.get("archived")) == archived]

    def get(self, job_id: str) -> dict:
        for j in self._load():
            if j["id"] == job_id:
                return j
        raise KeyError(job_id)

    def create(self, name: str, notes: str = "") -> dict:
        name = name.strip()[:80]
        if not name:
            raise ValueError("give the job a name")
        job = {"id": uuid.uuid4().hex[:10], "name": name, "notes": notes.strip()[:4000],
               "created": time.time(), "archived": False}
        self._save(self._load() + [job])
        return job

    def update(self, job_id: str, **changes) -> dict:
        jobs = self._load()
        for j in jobs:
            if j["id"] == job_id:
                for k in ("name", "notes", "archived"):
                    if changes.get(k) is not None:
                        j[k] = changes[k].strip()[:4000] if isinstance(changes[k], str) else changes[k]
                if not j["name"]:
                    raise ValueError("give the job a name")
                self._save(jobs)
                return j
        raise KeyError(job_id)
