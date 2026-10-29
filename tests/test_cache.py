from nathan.cache import ResponseCache, make_key
from nathan.client import Client
from tests.fakes import completion_json, fake_providers, mock_http


def test_cache_round_trip_and_expiry():
    now = [1000.0]
    cache = ResponseCache(clock=lambda: now[0], ttl=60)
    key = make_key("p/m", {"messages": [{"role": "user", "content": "hi"}]})
    assert cache.get(key) is None
    from nathan.client import ChatResult

    cache.put(key, ChatResult("hello", "p", "m", latency_ms=900))
    hit = cache.get(key)
    assert hit.text == "hello" and hit.cached and hit.latency_ms == 0
    now[0] += 61
    assert cache.get(key) is None  # expired


async def test_client_skips_network_on_repeat_and_never_caches_tools():
    import httpx2 as httpx

    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json=completion_json("answer"))

    client = Client(fake_providers(), http_client=mock_http(handler), cache=ResponseCache())
    msgs = [{"role": "user", "content": "same question"}]
    first = await client.chat("alpha/a-big", msgs)
    again = await client.chat("alpha/a-big", msgs)
    assert first.text == again.text == "answer" and again.cached and len(calls) == 1
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    await client.chat("alpha/a-big", msgs, tools=tools)
    await client.chat("alpha/a-big", msgs, tools=tools)
    assert len(calls) == 3  # both tool calls went to the provider
