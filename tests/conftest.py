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
