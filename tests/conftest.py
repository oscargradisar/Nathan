import sys
from pathlib import Path

import pytest

# Let tests import ping.py from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """Every test gets its own data folder so SQLite files never leak between tests."""
    monkeypatch.setenv("NATHAN_DATA", str(tmp_path / "data"))


@pytest.fixture
def make_engine(tmp_path):
    """make_engine(responder, providers=None) → Engine around a ScriptedClient."""
    from nathan.engine import Engine
    from tests.fakes import ScriptedClient

    def factory(responder, providers=None, **kw):
        client = ScriptedClient(responder, providers)
        return Engine(client=client, data=tmp_path, **kw)

    return factory


class MemoryKeyring:
    """In-memory keyring backend: tests never touch the real OS keychain."""

    priority = 1

    def __init__(self):
        self.store = {}

    def get_password(self, service, user):
        return self.store.get((service, user))

    def set_password(self, service, user, password):
        self.store[(service, user)] = password

    def delete_password(self, service, user):
        self.store.pop((service, user), None)


@pytest.fixture(autouse=True)
def memory_keyring(monkeypatch):
    import keyring
    from keyring.backend import KeyringBackend

    from nathan import keys

    class Backend(MemoryKeyring, KeyringBackend):
        pass

    backend = Backend()
    old = keyring.get_keyring()
    keyring.set_keyring(backend)
    keys.clear_cache()
    monkeypatch.setattr(keys, "ENV_FILE", Path("/nonexistent/.env"))
    yield backend
    keyring.set_keyring(old)
    keys.clear_cache()


@pytest.fixture(autouse=True)
def no_real_managers(monkeypatch, tmp_path):
    """Claude/ChatGPT/Gemini CLIs may be signed in on this machine: tests must never call them."""
    async def nobody():
        return []

    monkeypatch.setattr("nathan.managers.connected", nobody)
    monkeypatch.setattr("nathan.managers._settings_file", lambda: tmp_path / "settings.json")
