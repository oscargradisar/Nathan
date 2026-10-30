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


async def test_keyless_gateway_sends_no_key():
    """Free gateways (kilo, llm7) reject any Authorization header, even a placeholder."""
    from nathan.client import Client
    from nathan.config import Provider

    seen = {}

    def handler(req):
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0,
                                         "model": "m", "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": "OK"}}]})

    p = Provider(name="gw", base_url="https://gw.example/v1", models={"m": {}})
    client = Client({"gw": p}, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    res = await client.chat("gw/m", [{"role": "user", "content": "hi"}])
    assert res.text == "OK" and seen["auth"] is None


def test_strip_thinking():
    from nathan.client import strip_thinking

    assert strip_thinking("<thought>hmm</thought>\nOK") == "OK"
    assert strip_thinking("<think>a\nb</think>Answer") == "Answer"
    assert strip_thinking("<thought>never closed") == ""  # all reasoning, no answer: empty
    assert strip_thinking("plain") == "plain"


def test_strip_thinking_keeps_answers_that_mention_tags():
    from nathan.client import strip_thinking

    text = "Wrap the reasoning in <thinking> tags, then answer after </thinking>."
    assert strip_thinking(text) == text
    assert strip_thinking("<think>plan</think>Use <thinking> here.") == "Use <thinking> here."


# ---- quota bookkeeping -----------------------------------------------------------------

def quota_client(tmp_path, handler, providers=None, sleep=no_sleep, **kw):
    from nathan.quota import QuotaManager

    providers = providers or fake_providers()
    quota = QuotaManager(providers, tmp_path / "q.sqlite")
    return Client(providers, quota=quota, http_client=mock_http(handler), sleep=sleep, **kw), quota


def calls_for(quota, provider="alpha", model="a-big") -> int:
    return quota.db.execute("SELECT COUNT(*) FROM calls WHERE provider=? AND model=?",
                            (provider, model)).fetchone()[0]


async def test_every_attempt_is_recorded(tmp_path):
    def handler(req):
        return httpx.Response(503, json={"error": {"message": "down"}})

    c, quota = quota_client(tmp_path, handler, max_retries=2)
    with pytest.raises(ProviderDown):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert calls_for(quota) == 3  # the provider counted all three, so do we


async def test_cancelled_request_is_recorded(tmp_path):
    import asyncio

    started = asyncio.Event()

    async def slow(req):
        started.set()
        await asyncio.sleep(10)
        return httpx.Response(200, json=completion_json("late"))

    from nathan.quota import QuotaManager

    providers = fake_providers()
    quota = QuotaManager(providers, tmp_path / "q.sqlite")
    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    c = Client(providers, quota=quota, http_client=http, sleep=no_sleep)
    task = asyncio.create_task(c.chat("alpha/a-big", [{"role": "user", "content": "x"}]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls_for(quota) == 1  # a race loser still used up a request


async def test_429_blocks_model_before_sleeping(tmp_path):
    seen = {}
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": {"message": "slow"}},
                                  headers={"retry-after": "2"})
        return httpx.Response(200, json=completion_json("ok"))

    async def sleep(seconds):
        seen["slept"] = seconds
        seen["blocked"] = quota.why_not("alpha", "a-big")

    c, quota = quota_client(tmp_path, handler, sleep=sleep)
    res = await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert res.text == "ok" and seen["slept"] == 2
    assert "429" in seen["blocked"]  # other lanes were steered away while we waited


async def test_429_long_wait_gives_up_without_sleeping(tmp_path):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    def handler(req):
        return httpx.Response(429, json={"error": {"message": "x"}}, headers={"retry-after": "10"})

    c, quota = quota_client(tmp_path, handler, sleep=sleep)
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert slept == []  # 10s is too long to wait when other models are free
    assert "429" in quota.why_not("alpha", "a-big")


async def test_429_retry_after_zero_is_not_a_minute_block(tmp_path):
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "x"}}, headers={"retry-after": "0"})

    c, quota = quota_client(tmp_path, handler)
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert quota.available("alpha", "a-big")


async def test_429_rate_limit_headers_are_read(tmp_path):
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "x"}}, headers={
            "retry-after": "900", "x-ratelimit-remaining-requests-day": "0",
            "x-ratelimit-reset-requests-day": "2h"})

    c, quota = quota_client(tmp_path, handler)
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert quota.live_value("alpha", "a-big", "remaining-requests-day") == 0
    assert quota.remaining_today("alpha", "a-big") == 0


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_benches_provider_until_key_changes(tmp_path, status):
    seen = []

    def handler(req):
        seen.append(req.headers["authorization"])
        if req.headers["authorization"] == "Bearer alpha-test-key":
            return httpx.Response(status, json={"error": {"message": "bad key"}})
        return httpx.Response(200, json=completion_json("ok"))

    providers = fake_providers()
    c, quota = quota_client(tmp_path, handler, providers=providers)
    with pytest.raises(AuthError):
        await c.chat("alpha/a-big", [{"role": "user", "content": "x"}])
    assert "auth" in quota.why_not("alpha", "a-small")  # every model of that provider
    with pytest.raises(QuotaExceeded):
        await c.chat("alpha/a-small", [{"role": "user", "content": "x"}])
    assert len(seen) == 1  # no second doomed request

    providers["alpha"].key_override = "alpha-new-key"  # the owner saves a fresh key
    assert quota.available("alpha", "a-small")
    res = await c.chat("alpha/a-small", [{"role": "user", "content": "x"}])
    assert res.text == "ok" and seen[-1] == "Bearer alpha-new-key"  # client rebuilt


async def test_stream_429_blocks_and_records(tmp_path):
    def handler(req):
        return httpx.Response(429, json={"error": {"message": "x"}}, headers={"retry-after": "120"})

    c, quota = quota_client(tmp_path, handler)
    with pytest.raises(QuotaExceeded):
        async for _ in c.stream("alpha/a-big", [{"role": "user", "content": "x"}]):
            pass
    assert "429" in quota.why_not("alpha", "a-big")
    assert calls_for(quota) == 1


async def test_stream_timeout_benches_model(tmp_path):
    def handler(req):
        raise httpx.ReadTimeout("slow")

    c, quota = quota_client(tmp_path, handler)
    with pytest.raises(ProviderDown):
        async for _ in c.stream("alpha/a-big", [{"role": "user", "content": "x"}]):
            pass
    assert "timeout" in quota.why_not("alpha", "a-big")
    assert calls_for(quota) == 1
