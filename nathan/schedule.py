"""Nightly self-check: an OS scheduled task that runs `Nathan --check` at 03:30.

Windows uses Task Scheduler (schtasks, per user, no admin), macOS a launchd agent in
~/Library/LaunchAgents. The check itself (desktop.run_check) asks every provider which
models it still offers and pings each model once, then writes last_check.json.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK = "Nathan nightly check"
LABEL = "com.nathan.nightlycheck"
AT = (3, 30)


def command() -> list[str]:
    if getattr(sys, "frozen", False):  # the installed app
        return [sys.executable, "--check"]
    exe = Path(sys.executable)
    quiet = exe.with_name("pythonw.exe")  # no console window popping up at night
    return [str(quiet if quiet.exists() else exe), "-m", "nathan.desktop", "--check"]


def supported() -> bool:
    return sys.platform in ("win32", "darwin")


def _plist() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_on() -> bool:
    if sys.platform == "win32":
        return subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True,
                              check=False).returncode == 0
    if sys.platform == "darwin":
        return _plist().exists()
    return False


def turn_on() -> None:
    cmd = command()
    if sys.platform == "win32":
        tr = " ".join(f'"{c}"' if " " in c else c for c in cmd)
        subprocess.run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", f"{AT[0]:02d}:{AT[1]:02d}",
                        "/TN", TASK, "/TR", tr], capture_output=True, check=True)
    elif sys.platform == "darwin":
        args = "".join(f"<string>{c}</string>" for c in cmd)
        _plist().parent.mkdir(parents=True, exist_ok=True)
        _plist().write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>{LABEL}</string>
<key>ProgramArguments</key><array>{args}</array>
<key>StartCalendarInterval</key><dict><key>Hour</key><integer>{AT[0]}</integer><key>Minute</key><integer>{AT[1]}</integer></dict>
</dict></plist>
""")
        subprocess.run(["launchctl", "load", str(_plist())], capture_output=True, check=False)
    else:
        raise RuntimeError("nightly checks need Windows or macOS")


def turn_off() -> None:
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK], capture_output=True, check=False)
    elif sys.platform == "darwin" and _plist().exists():
        subprocess.run(["launchctl", "unload", str(_plist())], capture_output=True, check=False)
        _plist().unlink()
