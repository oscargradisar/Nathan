"""Smoke test: the desktop app's server starts and stops headless (no window, no network)."""

import httpx2 as httpx

from nathan import desktop
from nathan.config import DEFAULT_CONFIG, load_providers
from nathan.engine import Engine


def test_server_starts_and_stops(tmp_path, monkeypatch):
    providers = load_providers(DEFAULT_CONFIG, env_file=None)
    for p in providers.values():
        monkeypatch.delenv(p.key_env, raising=False)
    engine = Engine(providers, data=tmp_path)
    assert desktop.first_page(engine) == "/connect?first=1"

    server, url = desktop.start_server(engine, self_check=False)  # no network in tests
    try:
        app = server.config.app
        app.state.last_seen = 0  # pretend the window went quiet long ago
        assert httpx.get(url + "/connect").status_code == 200
        assert app.state.last_seen > 0  # a request from the window keeps the app alive
        assert httpx.get(url + "/api/connect").json()["providers"]
    finally:
        server.should_exit = True


def test_app_folders(tmp_path, monkeypatch):
    import os

    env = {}  # throwaway environment so nothing leaks into other tests
    monkeypatch.setattr(os, "environ", env)
    monkeypatch.setattr(desktop, "user_data_dir", lambda *a, **k: str(tmp_path))
    desktop.use_app_folders()
    assert env["NATHAN_KEYCHAIN_ONLY"] == "1"
    assert env["NATHAN_DATA"].startswith(str(tmp_path))


def test_starts_without_a_console(tmp_path, monkeypatch):
    """pythonw and --windowed builds have sys.stdout = None; uvicorn used to crash on that."""
    import sys

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(desktop, "user_data_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setenv("NATHAN_KEYCHAIN_ONLY", "1")  # so monkeypatch restores it afterwards
    monkeypatch.setenv("NATHAN_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("NATHAN_WORKSPACE", str(tmp_path / "ws"))
    opened = []
    monkeypatch.setattr(desktop, "open_window", lambda url, home, app: opened.append(url))

    async def no_roll_call(engine, http=None):  # the startup check would hit the network
        return {"retired": [], "reinstated": [], "unchecked": []}

    monkeypatch.setattr("nathan.health.roll_call", no_roll_call)
    desktop.main()
    sys.stdout.close()
    assert opened and opened[0].startswith("http://127.0.0.1:")
    assert (tmp_path / "nathan.log").exists()
