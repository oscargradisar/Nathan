"""Rebuild Nathan from source and install it over the running copy, then reopen it.

Started by the Evolve page ("Rebuild & restart"):
  uv run python packaging/update_install.py --pid <app pid> --install <app folder>

Waits for the app to exit (its files are locked while it runs), builds with build.py,
copies dist/Nathan over the install folder and launches the new Nathan.exe. If the build
fails, the old install is left exactly as it was and reopened.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def running(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False)
        return str(pid) in out.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pid", type=int, required=True)
    ap.add_argument("--install", type=Path, required=True)
    a = ap.parse_args()
    print(time.strftime("%Y-%m-%d %H:%M:%S"), "update: waiting for Nathan to close", flush=True)
    for _ in range(120):
        if not running(a.pid):
            break
        time.sleep(1)
    built = subprocess.run([sys.executable, str(ROOT / "packaging" / "build.py")], cwd=ROOT, check=False).returncode == 0
    exe = a.install / ("Nathan.exe" if sys.platform == "win32" else "Nathan")
    if built:
        shutil.rmtree(a.install / "_internal", ignore_errors=True)
        shutil.copytree(ROOT / "dist" / "Nathan", a.install, dirs_exist_ok=True)
        print("update: installed the new build", flush=True)
    else:
        print("update: build failed; reopening the old version", flush=True)
    subprocess.Popen([str(exe)], cwd=a.install, close_fds=True)


if __name__ == "__main__":
    main()
