"""File tools, locked to the workspace/ folder.

Every path is resolved and checked to be *inside* the workspace, so "../../.env" or an
absolute path can't escape it. Symlinks are resolved first, so they can't escape either.
"""

from __future__ import annotations

from pathlib import Path

from nathan.config import workspace_dir
from nathan.tools.base import Tool, params

MAX_READ = 20000


def safe_path(root: Path, rel: str) -> Path:
    root = root.resolve()
    target = (root / rel).resolve()
    if target != root and not target.is_relative_to(root):
        raise PermissionError(f"{rel!r} is outside the workspace")
    return target


def make_tools(workspace: Path | None = None) -> list[Tool]:
    root = (workspace or workspace_dir()).resolve()

    def list_files(path: str = ".") -> str:
        base = safe_path(root, path)
        if not base.is_dir():
            return f"ERROR: {path} is not a folder"
        items = []
        for p in sorted(base.rglob("*")):
            if p.name.startswith(".") or not p.is_file():
                continue
            items.append(f"{p.relative_to(root)} ({p.stat().st_size} bytes)")
            if len(items) >= 200:
                items.append("…more files not shown")
                break
        return "\n".join(items) or "(empty)"

    def read_file(path: str, start: int = 0) -> str:
        p = safe_path(root, path)
        if not p.is_file():
            return f"ERROR: no file {path}"
        text = p.read_text(errors="replace")
        chunk = text[start:start + MAX_READ]
        if start + MAX_READ < len(text):
            chunk += f"\n…[more: call read_file with start={start + MAX_READ}]"
        return chunk

    def write_file(path: str, content: str) -> str:
        p = safe_path(root, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {p.relative_to(root)}"

    def search_files(query: str) -> str:
        q = query.lower()
        hits = []
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name.startswith("."):
                continue
            try:
                for n, line in enumerate(p.read_text(errors="ignore").splitlines(), 1):
                    if q in line.lower():
                        hits.append(f"{p.relative_to(root)}:{n}: {line.strip()[:200]}")
            except OSError:
                continue
            if len(hits) >= 50:
                break
        return "\n".join(hits) or "no matches"

    return [
        Tool("list_files", "List files in the workspace (or a subfolder).",
             params([], path=("string", "folder inside the workspace, default '.'")), list_files),
        Tool("read_file", "Read a text file from the workspace.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "description": "file path inside the workspace"},
                 "start": {"type": "integer", "description": "character offset, default 0"}},
              "required": ["path"]}, read_file),
        Tool("write_file", "Create or overwrite a text file in the workspace.",
             params(path=("string", "file path"), content=("string", "full file content")),
             write_file),
        Tool("search_files", "Case-insensitive text search across workspace files.",
             params(query=("string", "text to find")), search_files),
    ]
