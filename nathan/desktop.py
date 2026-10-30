"""Nathan desktop app: the control room in its own window. No terminal, no .env.

Starts the Tier 5 web app on a free 127.0.0.1 port in a background thread, then opens it
in its own window (an Edge app window on Windows, a pywebview WebKit window on
macOS). Closing the window stops the server and quits.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from platformdirs import user_data_dir

MIN_CONNECTED = 2  # first run stays on the Connect page until this many accounts work
# Hidden pages may only run timers once a minute, so allow longer than that before quitting.
IDLE_QUIT_S = 90
_camera_process = None


def close_edge_profile(profile: Path) -> None:
    """Close only the Edge window running on one of Nathan's private profiles (Windows)."""
    # Edge can relaunch or forward to an existing process; identify our private profile.
    env = {**os.environ, "NATHAN_CAMERA_PROFILE": str(profile)}
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    ("Get-CimInstance Win32_Process -Filter \"name = 'msedge.exe'\" | "
                    "Where-Object { $_.CommandLine -and "
                    "$_.CommandLine.Contains($env:NATHAN_CAMERA_PROFILE) -and "
                    "$_.CommandLine -notlike '*--type=*' } | "
                    "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }")],
                   env=env, creationflags=0x08000000, capture_output=True, check=False)


def close_camera_window() -> None:
    """Close only the dedicated camera browser process owned by Nathan."""
    global _camera_process
    if sys.platform == "win32":
        close_edge_profile(Path(user_data_dir("Nathan", appauthor=False)) / "camera-viewer")
    elif _camera_process is not None and _camera_process.poll() is None:
        _camera_process.terminate()
    _camera_process = None


def use_app_folders() -> Path:
    """Keys only in the keychain; data in the OS app-data folder, never the install folder."""
    home = Path(user_data_dir("Nathan", appauthor=False))
    os.environ["NATHAN_KEYCHAIN_ONLY"] = "1"
    os.environ.setdefault("NATHAN_DATA", str(home / "data"))
    os.environ.setdefault("NATHAN_WORKSPACE", str(home / "workspace"))
    return home


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(engine=None, port: int | None = None, self_check: bool = True):
    """Run the web app in a daemon thread. Returns (server, url) once it is accepting."""
    import uvicorn

    from nathan.engine import Engine
    from nathan.onboarding.server import Hub
    from nathan.web.app import create_app

    port = port or free_port()
    engine = engine or Engine()
    app = create_app(engine, Hub(engine.providers, port=port), self_check=self_check)
    app.state.last_seen = time.monotonic()

    @app.middleware("http")
    async def note_activity(request, call_next):
        app.state.last_seen = time.monotonic()  # the open window polls every few seconds
        return await call_next(request)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("Nathan's server didn't start")
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"


def first_page(engine) -> str:
    connected = sum(1 for p in engine.providers.values() if p.key_env and p.api_key)  # gateways need no account
    return "/#console" if connected >= MIN_CONNECTED else "/#accounts"


def edge_path() -> str | None:
    for base in (os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles")):
        exe = Path(base or "") / "Microsoft/Edge/Application/msedge.exe"
        if base and exe.exists():
            return str(exe)
    return shutil.which("msedge")


def open_camera_window(url: str) -> None:
    """Use a standalone app window: OpenCCTV disallows embedding in our page."""
    from urllib.parse import urlsplit

    global _camera_process
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "opencctv.org" or parsed.path != "/":
        raise ValueError("Only OpenCCTV location URLs are supported")
    if sys.platform == "win32" and (edge := edge_path()):
        home = Path(user_data_dir("Nathan", appauthor=False))
        close_camera_window()
        # A separate profile prevents inheriting the main window's fullscreen state.
        _camera_process = subprocess.Popen([
            edge, f"--app={url}", f"--user-data-dir={home / 'camera-viewer'}",
            "--no-first-run", "--edge-skip-compat-layer-relaunch",
            "--window-size=1100,760", "--window-position=120,80"])
    else:
        import webbrowser

        webbrowser.open(url)


def open_window(url: str, home: Path, app) -> None:
    """Show the app and block until its window is closed."""
    if sys.platform == "win32" and (edge := edge_path()):
        # Edge ships with Windows; --app gives a bare window with no tabs or address bar.
        # Edge may hand the window to another of its processes, so instead of waiting on the
        # process we treat "the page stopped polling" as "the window was closed".
        subprocess.Popen([edge, f"--app={url}", f"--user-data-dir={home / 'window'}",
                          "--no-first-run", "--window-size=1280,860"])
        while not app.state.quit and time.monotonic() - app.state.last_seen < IDLE_QUIT_S:
            time.sleep(0.5)
        if app.state.quit:  # Save & exit: the page asked, so close its window for it
            close_edge_profile(home / "window")
        return
    import webview  # macOS: native WebKit window

    window = webview.create_window("Nathan", url, width=1280, height=860, min_size=(800, 600))

    def watch_quit() -> None:  # Save & exit from the page closes the native window too
        while not app.state.quit:
            time.sleep(0.5)
        window.destroy()

    webview.start(watch_quit)


def run_check(engine) -> dict:
    """The nightly check: roll call (free) + one ping per model. Saved to last_check.json."""
    import asyncio
    import json

    from nathan.health import diagnostics, roll_call

    async def go() -> dict:
        listed = await roll_call(engine)
        results = await diagnostics(engine)
        await engine.close()
        return {"at": time.time(), "roll_call": listed, "results": results}

    report = asyncio.run(go())
    (engine.data / "last_check.json").write_text(json.dumps(report, indent=1))
    return report


def main() -> None:
    home = use_app_folders()
    if sys.stdout is None or sys.stderr is None:
        # No console (pythonw, or a --windowed build): print() and uvicorn would crash on
        # None, and errors would vanish. Write them to a log file instead.
        home.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(home / "nathan.log", "a", buffering=1, encoding="utf-8")  # noqa: SIM115
    from nathan.engine import Engine

    engine = Engine()
    if "--check" in sys.argv:  # nightly scheduled task: no window
        report = run_check(engine)
        ok = sum(r["ok"] for r in report["results"])
        print(f"nightly check: {ok}/{len(report['results'])} models answered", flush=True)
        return
    server, url = start_server(engine)
    open_window(url + first_page(engine), home, server.config.app)
    close_camera_window()
    server.should_exit = True


if __name__ == "__main__":
    main()
