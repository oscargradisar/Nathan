"""Client tests against a mocked HTTP transport — no network."""

import json

import httpx2 as httpx  # httpx2: the HTTP library the openai SDK is built on
import pytest

from nathan.client import ChatResult, Client, ModelRef
from nathan.errors import AuthError, BadRequest, ModelGone, ProviderDown, QuotaExceeded
from tests.fakes import completion_json, fake_providers, mock_http


async def no_sleep(_s: float) -> None:
    return None


def make_client(handler, **kw) -> Client:
    return Client(fake_providers(), http_client=mock_http(handler), sleep=no_sleep, **kw)


def test_model_ref_parse():
    r = ModelRef.parse("groq/openai/gpt-oss-120b")
    assert r.provider == "groq" and r.model == "openai/gpt-oss-120b"
    assert str(r) == "groq/openai/gpt-oss-120b"
    with pytest.raises(ValueError):
        ModelRef.parse("no-slash")


async def test_chat_success_routes_to_provider_and_keeps_headers():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["host"] = req.url.host
        seen["auth"] = req.headers["authorization"]
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=completion_json("hello", "b-big"),
                              headers={"x-ratelimit-remaining-requests": "99"})

    c = make_client(handler)
    res = await c.chat("beta/b-big", [{"role": "user", "content": "hi"}], max_tokens=50)
    assert isinstance(res, ChatResult)
    assert res.text == "hello" and res.ref == "beta/b-big"
    assert res.total_tokens == 15
    assert res.headers["x-ratelimit-remaining-requests"] == "99"
    assert seen["host"] == "beta.test"
    assert seen["auth"] == "Bearer beta-test-key"
    assert seen["body"]["max_tokens"] == 50


async def test_429_then_success_retries():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}},
                                  headers={"retry-after": "1"})
        return httpx.Response(200, json=completion_json("ok"))

    res = await make_client(handler).chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert res.text == "ok" and calls["n"] == 2


async def test_429_with_long_retry_after_is_quota_exceeded():
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "daily limit"}},
                              headers={"retry-after": "3600"})

    with pytest.raises(QuotaExceeded) as ei:
        await make_client(handler).chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert ei.value.retry_after == 3600
    assert ei.value.provider == "alpha"


async def test_5xx_exhausts_retries_then_provider_down():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503, json={"error": {"message": "down"}})

    with pytest.raises(ProviderDown):
        await make_client(handler, max_retries=2).chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert calls["n"] == 3


async def test_connection_error_is_provider_down():
    def handler(req):
        raise httpx.ConnectError("nope")

    with pytest.raises(ProviderDown):
        await make_client(handler).chat("alpha/a-big", [{"role": "user", "content": "x"}])


@pytest.mark.parametrize("status,exc", [(401, AuthError), (404, ModelGone), (400, BadRequest)])
async def test_client_errors_are_typed_and_not_retried(status, exc):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(status, json={"error": {"message": "bad"}})

    with pytest.raises(exc):
        await make_client(handler).chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert calls["n"] == 1


async def test_missing_key_is_auth_error():
    c = Client(fake_providers(keys=False), http_client=mock_http(lambda r: httpx.Response(200)))
    with pytest.raises(AuthError):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])


async def test_tool_calls_are_parsed():
    def handler(req):
        return httpx.Response(200, json=completion_json(
            "", tool_calls=[{"name": "read_file", "arguments": {"path": "a.txt"}}]))

    res = await make_client(handler).chat("alpha/a-big", [{"role": "user", "content": "x"}],
                                          tools=[{"type": "function", "function": {"name": "read_file"}}])
    assert res.tool_calls[0]["name"] == "read_file"
    assert json.loads(res.tool_calls[0]["arguments"]) == {"path": "a.txt"}


async def test_stream_yields_pieces():
    def handler(req):
        chunks = []
        for piece in ["Hel", "lo"]:
            chunks.append("data: " + json.dumps({
                "id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
            }) + "\n\n")
        chunks.append("data: [DONE]\n\n")
        return httpx.Response(200, content="".join(chunks).encode(),
                              headers={"content-type": "text/event-stream"})

    pieces = [p async for p in make_client(handler).stream("alpha/a-big", [{"role": "user", "content": "x"}])]
    assert "".join(pieces) == "Hello"


async def test_client_feeds_quota_manager(tmp_path):
    from nathan.quota import QuotaManager

    providers = fake_providers()
    quota = QuotaManager(providers, tmp_path / "q.sqlite")
    state = {"status": 200}

    def handler(req):
        if state["status"] == 429:
            return httpx.Response(429, json={"error": {"message": "x"}}, headers={"retry-after": "900"})
        return httpx.Response(200, json=completion_json("ok"),
                              headers={"x-ratelimit-remaining-requests": "7"})

    c = Client(providers, quota=quota, http_client=mock_http(handler), sleep=no_sleep)
    await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert quota.live_value("alpha", "a-big", "remaining-requests") == 7
    state["status"] = 429
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    # Now blocked locally: the next call fails without touching the network
    state["status"] = 200
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
