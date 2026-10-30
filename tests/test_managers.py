"""Managers: sign-in checks are remembered, and a cancelled or timed-out call stops the CLI.
No real Claude/ChatGPT CLI runs here: sign-in checks are faked, and the process tests run
small Python scripts instead."""

import asyncio
import os
import subprocess
import sys
import time

import pytest

from nathan import managers
from nathan.managers import connected as real_connected  # conftest replaces managers.connected


@pytest.fixture(autouse=True)
def fresh_cache():
    managers.forget()
    yield
    managers.forget()


@pytest.fixture
def checks(monkeypatch):
    calls = []

    def signed_in(m):
        calls.append(m.id)
        return m.id == "claude"

    monkeypatch.setattr(managers, "signed_in", signed_in)
    return calls


async def test_connected_remembers_sign_in_for_a_while(checks):
    assert await real_connected() == [managers.CLAUDE]
    assert await real_connected() == [managers.CLAUDE]
    assert sorted(checks) == ["chatgpt", "claude"]  # checked once each, not on every run


async def test_signing_in_or_choosing_a_lead_checks_again(checks, monkeypatch):
    await real_connected()
    managers.choose("chatgpt")
    await real_connected()
    assert len(checks) == 4
    monkeypatch.setattr(managers, "SEEN_TTL_S", 0)  # an old answer is checked again too
    await real_connected()
    assert len(checks) == 6


def test_accounts_page_always_checks_for_real(checks, monkeypatch):
    monkeypatch.setattr(managers, "find_cli", lambda mid: "cli")
    managers.state(managers.CLAUDE)
    managers.state(managers.CLAUDE)
    assert checks == ["claude", "claude"]


def test_reading_a_folder_can_never_change_it():
    args = managers._args(managers.CLAUDE, "claude", False, read=True)
    allowed = args[args.index("--allowedTools") + 1:]
    assert allowed == ["Read", "Glob", "Grep"]
    assert "--disallowedTools" in args and "Edit" in args and "Bash" in args
    assert "--disallowedTools" not in managers._args(managers.CLAUDE, "claude", False)


async def test_ask_reads_the_cli_answer(monkeypatch):
    monkeypatch.setattr(managers, "find_cli", lambda mid: "cli")
    script = "import sys; sys.stdin.read(); print('{\"result\": \" hi \"}')"
    monkeypatch.setattr(managers, "_args", lambda *a, **k: [sys.executable, "-c", script])
    assert await managers.ask(managers.CLAUDE, "question") == "hi"


def _alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True, check=False).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _parent_with_child(tmp_path) -> tuple[list[str], object]:
    """A process that starts a child (like claude.cmd starting node), then both just wait."""
    pidfile = tmp_path / "child.pid"
    code = ("import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
            "time.sleep(60)\n")
    return [sys.executable, "-c", code], pidfile


def _gone(pidfile) -> bool:
    for _ in range(50):
        if pidfile.exists() and pidfile.read_text() and not _alive(int(pidfile.read_text())):
            return True
        time.sleep(0.1)
    return False


async def _child_started(pidfile) -> None:
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text():
            return
        await asyncio.sleep(0.05)


async def test_timeout_kills_the_whole_process_tree(tmp_path):
    args, pidfile = _parent_with_child(tmp_path)
    with pytest.raises(TimeoutError):
        await managers.run_process(args, None, timeout=3)
    assert _gone(pidfile)


async def test_cancelling_kills_the_whole_process_tree(tmp_path):
    args, pidfile = _parent_with_child(tmp_path)
    task = asyncio.create_task(managers.run_process(args, None, timeout=60))
    await _child_started(pidfile)
    task.cancel()  # e.g. the Stop button
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _gone(pidfile)


async def test_ask_turns_a_timeout_into_a_plain_reason(monkeypatch):
    monkeypatch.setattr(managers, "find_cli", lambda mid: "cli")
    monkeypatch.setattr(managers, "_args", lambda *a, **k: [sys.executable, "-c", "import time; time.sleep(30)"])
    with pytest.raises(RuntimeError, match="didn't answer within 1s"):
        await managers.ask(managers.CHATGPT, "q", timeout=1)
