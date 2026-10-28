"""Run model-written Python in a separate process with a timeout.

This is a *convenience* sandbox, not a security boundary: the code runs as you, inside
the workspace folder, with a clean environment (no API keys) and a hard time limit.
Only use the agent on tasks you'd be happy to run yourself.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from nathan.config import workspace_dir
from nathan.tools.base import Tool, params

TIMEOUT_S = 15
MAX_OUTPUT = 6000


async def run_python(code: str, workspace: Path, timeout: float = TIMEOUT_S) -> str:
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}  # no keys leak in
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c", code, cwd=str(workspace), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return f"ERROR: timed out after {timeout:.0f}s"
    text = out.decode(errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[:MAX_OUTPUT] + "\n…[output truncated]"
    return f"exit code {proc.returncode}\n{text}".strip()


def make_tool(workspace: Path | None = None, timeout: float = TIMEOUT_S) -> Tool:
    root = workspace or workspace_dir()
    return Tool(
        "run_python",
        f"Run a Python 3 script (stdlib only) in the workspace folder; returns its printed "
        f"output. Time limit {timeout:.0f}s. Use print() to see results.",
        params(code=("string", "complete Python source code")),
        lambda code: run_python(code, root, timeout),
    )
