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

    server, url = desktop.start_server(engine)
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
