"""ping.py logic with a fake provider — no network."""

from types import SimpleNamespace

import ping
from nathan.config import Provider


class FakeModels:
    def list(self):
        async def gen():
            yield SimpleNamespace(id="good-model")
        return gen()


class FakeCompletions:
    async def create(self, **kwargs):
        msg = SimpleNamespace(content="ok")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class FakeClient:
    def __init__(self, **kwargs):
        self.models = FakeModels()
        self.chat = SimpleNamespace(completions=FakeCompletions())

    async def close(self):
        pass


def make_provider(monkeypatch, key: str | None) -> Provider:
    if key:
        monkeypatch.setenv("FAKE_API_KEY", key)
    else:
        monkeypatch.delenv("FAKE_API_KEY", raising=False)
    return Provider(
        name="fake", base_url="https://fake/v1", key_env="FAKE_API_KEY",
        models={"good-model": {}, "gone-model": {}},
    )


async def test_pass_and_missing_model_warning(monkeypatch):
    monkeypatch.setattr(ping, "AsyncOpenAI", FakeClient)
    r = await ping.check(make_provider(monkeypatch, "abcd1234"))
    assert "pass" in r["status"]
    assert "gone-model" in r["note"]
    assert r["key"] == "abcd…"


async def test_no_key(monkeypatch):
    r = await ping.check(make_provider(monkeypatch, None))
    assert "no key" in r["status"]
