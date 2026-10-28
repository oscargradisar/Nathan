"""Tier 4.5 onboarding: keychain, tokens, PKCE, /keys endpoint, connect flows.

No real providers, no real keychain (in-memory backend from conftest), no browser.
"""

import asyncio
import base64
import hashlib
import json
import re

import httpx2 as httpx
import pytest
from fastapi.testclient import TestClient

from nathan import keys
from nathan.config import DEFAULT_CONFIG, load_providers
from nathan.onboarding import oauth_openrouter
from nathan.onboarding.clipboard import watch_clipboard
from nathan.onboarding.connect import connect, connect_order
from nathan.onboarding.keytest import check_key
from nathan.onboarding.server import Hub, create_app
from nathan.onboarding.tokens import TokenStore

GOOD = {
    "groq": "gsk_" + "A1b2" * 13,
    "gemini": "AIza" + "SyD-x_9" * 5,
    "cerebras": "csk-" + "a1b2c3d4e5" * 5,
    "openrouter": "sk-or-v1-" + "0123456789abcdef" * 4,
    "mistral": "Zz90" * 8,  # built, not literal: real-looking keys trip secret scanners
}
BAD = {
    "groq": ["gsk_short", "GSK_" + "A1b2" * 13, "xgsk_" + "A1b2" * 13, "sk-" + "a" * 48],
    "gemini": ["AIza123", "Aiza" + "SyD-x_9" * 5, "AIza" + "SyD-x_9" * 5 + "extra"],
    "cerebras": ["csk-SHORT", "csk_" + "a1b2c3d4e5" * 5],
    "openrouter": ["sk-or-v1-xyz", "sk-or-v2-" + "0123456789abcdef" * 4, "sk-" + "0" * 64],
    "mistral": ["tooshort", "Zz90" * 8 + "X", "Zz9-" + "Zz90" * 7],
}


@pytest.fixture
def providers():
    return load_providers(DEFAULT_CONFIG, env_file=None)


async def ok_checker(p, key):
    return True, "key works"


# ---- key patterns (from providers.yaml) ---------------------------------------------------

@pytest.mark.parametrize("name", list(GOOD))
def test_key_patterns_accept_real_shapes_and_reject_lookalikes(providers, name):
    ob = providers[name].onboarding
    assert ob.matches(GOOD[name])
    for bad in BAD[name]:
        assert not ob.matches(bad), bad


def test_every_extension_provider_has_page_hints(providers):
    for p in providers.values():
        assert p.onboarding, p.name
        assert p.onboarding.key_page.startswith("https://")
        if p.onboarding.method == "extension":
            assert p.onboarding.create_button and p.onboarding.key_pattern


# ---- keychain ---------------------------------------------------------------------------

def test_keychain_roundtrip_and_precedence(monkeypatch, tmp_path, memory_keyring):
    env = tmp_path / ".env"
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert keys.get_key("groq", "GROQ_API_KEY") == "from-env"
    assert keys.set_key("groq", "GROQ_API_KEY", GOOD["groq"], env) == "keychain"
    assert memory_keyring.store[("nathan", "groq")] == GOOD["groq"]
    assert keys.get_key("groq", "GROQ_API_KEY") == GOOD["groq"]
    assert keys.where("groq", "GROQ_API_KEY") == "keychain"
    keys.delete_key("groq", "GROQ_API_KEY", env)
    assert keys.get_key("groq", "GROQ_API_KEY") is None


def test_env_fallback_when_no_keychain(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# keys\nGROQ_API_KEY=\nOTHER=1\n")
    monkeypatch.setattr(keys, "keychain_available", lambda: False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert keys.set_key("groq", "GROQ_API_KEY", "gsk_x", env) == ".env"
    assert env.read_text() == "# keys\nGROQ_API_KEY=gsk_x\nOTHER=1\n"
    assert keys.get_key("groq", "GROQ_API_KEY") == "gsk_x"


def test_keychain_only_ignores_env(monkeypatch):
    monkeypatch.setenv("NATHAN_KEYCHAIN_ONLY", "1")
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert keys.get_key("groq", "GROQ_API_KEY") is None


def test_mask():
    assert keys.mask("gsk_abcdef") == "gsk_…" and keys.mask("") == "—"


# ---- tokens ------------------------------------------------------------------------------

def test_tokens_expire_and_are_provider_bound():
    t = [0.0]
    store = TokenStore(ttl=300, clock=lambda: t[0])
    tok = store.issue("groq")
    assert store.valid(tok, "groq") and not store.valid(tok, "gemini")
    assert not store.valid(None, "groq") and not store.valid("forged", "groq")
    assert store.active_provider() == "groq"
    t[0] = 301
    assert not store.valid(tok, "groq")


# ---- PKCE ------------------------------------------------------------------------------

def test_pkce_challenge_matches_verifier():
    verifier, challenge = oauth_openrouter.make_pkce()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()
    url = oauth_openrouter.auth_url("http://127.0.0.1:5000/callback?state=s", challenge)
    assert url.startswith("https://openrouter.ai/auth?") and "code_challenge_method=S256" in url
    assert "callback_url=http%3A%2F%2F127.0.0.1%3A5000%2Fcallback%3Fstate%3Ds" in url


async def test_exchange_code_posts_verifier():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"key": GOOD["openrouter"]})

    key = await oauth_openrouter.exchange_code("the-code", "the-verifier",
                                               httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert key == GOOD["openrouter"]
    assert seen["url"] == oauth_openrouter.EXCHANGE_URL
    assert seen["body"] == {"code": "the-code", "code_verifier": "the-verifier",
                            "code_challenge_method": "S256"}


async def test_exchange_code_error():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(403, json={})))
    with pytest.raises(oauth_openrouter.ExchangeError):
        await oauth_openrouter.exchange_code("c", "v", client)


# ---- key test (GET /models) ---------------------------------------------------------------

async def test_check_key(providers):
    def handler(req):
        assert req.url.path.endswith("/models")
        ok = req.headers["authorization"] == "Bearer good"
        return httpx.Response(200 if ok else 401, json={"data": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert (await check_key(providers["groq"], "good", client))[0]
    ok, msg = await check_key(providers["groq"], "bad", client)
    assert not ok and "rejected" in msg


# ---- /keys endpoint ----------------------------------------------------------------------

def make_hub(providers, saved):
    def saver(p, key):
        saved[p.name] = key
        return "keychain"

    for p in providers.values():
        p.key_override = None
    return Hub(providers, checker=ok_checker, saver=saver)


def test_keys_endpoint_token_rules(providers, monkeypatch):
    for var in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    saved = {}
    hub = make_hub(providers, saved)
    client = TestClient(create_app(hub))
    hub.loop = None

    async def start():
        return hub.start("groq")

    url = asyncio.run(start())
    token = re.search(r"#nathan=([^&]+)", url).group(1)
    assert url.startswith("https://console.groq.com/keys#nathan=")

    body = {"provider": "groq", "key": GOOD["groq"]}
    assert client.post("/keys", json={**body, "token": ""}).status_code == 403
    assert client.post("/keys", json={**body, "token": "wrong"}).status_code == 403
    assert client.post("/keys", json={**body, "token": token, "provider": "gemini"}).status_code == 403
    assert client.post("/keys", json={**body, "key": "nope", "token": token}).status_code == 422
    r = client.post("/keys", json={**body, "token": token})
    assert r.status_code == 200 and r.json()["close"] is True
    assert saved == {"groq": GOOD["groq"]}
    assert client.post("/keys", json={**body, "token": token}).status_code == 403  # used once
    status = client.get("/connect/status").json()
    assert status["groq"]["state"] == "connected" and GOOD["groq"] not in json.dumps(status)


def test_keys_endpoint_expired_token(providers):
    hub = make_hub(providers, {})
    t = [0.0]
    hub.tokens = TokenStore(clock=lambda: t[0])
    token = hub.tokens.issue("groq")
    t[0] = 10_000
    client = TestClient(create_app(hub))
    r = client.post("/keys", json={"provider": "groq", "key": GOOD["groq"], "token": token})
    assert r.status_code == 403


def test_onboarding_json_lists_extension_providers_only(providers):
    data = TestClient(create_app(make_hub(providers, {}))).get("/onboarding.json").json()
    assert set(data) == {"groq", "gemini", "cerebras", "mistral"}
    assert data["groq"]["key_pattern"].startswith("gsk_")


def test_cors_only_for_extensions(providers):
    client = TestClient(create_app(make_hub(providers, {})))
    ok = client.options("/keys", headers={"origin": "chrome-extension://abc",
                                          "access-control-request-method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == "chrome-extension://abc"
    evil = client.options("/keys", headers={"origin": "https://evil.example",
                                            "access-control-request-method": "POST"})
    assert "access-control-allow-origin" not in evil.headers


# ---- connect flows ------------------------------------------------------------------------

async def test_connect_openrouter_oauth(providers):
    saved = {}
    hub = make_hub(providers, saved)
    hub.port = 5555
    opened = []

    def open_browser(url):
        opened.append(url)
        # Simulate the browser coming back to /callback with the state we sent
        state = re.search(r"state%3D([^&]+)", url).group(1)
        asyncio.get_running_loop().call_soon(hub.oauth_callback, state, "auth-code")

    def handler(req):
        assert json.loads(req.content)["code"] == "auth-code"
        return httpx.Response(200, json={"key": GOOD["openrouter"]})

    ok, msg = await connect(hub, "openrouter", open_browser=open_browser, timeout=5,
                            http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert ok, msg
    assert saved["openrouter"] == GOOD["openrouter"]
    assert opened[0].startswith("https://openrouter.ai/auth?")


def test_oauth_callback_rejects_unknown_state(providers):
    client = TestClient(create_app(make_hub(providers, {})))
    assert "invalid or expired" in client.get("/callback?code=x&state=nope").text


async def test_connect_extension_path(providers):
    saved = {}
    hub = make_hub(providers, saved)

    def open_browser(url):
        token = re.search(r"#nathan=([^&]+)", url).group(1)

        async def extension_posts():
            await asyncio.sleep(0.01)
            await hub.submit("groq", GOOD["groq"], token)

        asyncio.get_running_loop().create_task(extension_posts())

    ok, _msg = await connect(hub, "groq", open_browser=open_browser, timeout=5,
                            use_clipboard=False)
    assert ok and saved["groq"] == GOOD["groq"]


async def test_connect_clipboard_fallback(providers):
    saved = {}
    hub = make_hub(providers, saved)
    clip = {"text": "something old", "reads": 0}

    def paste():
        clip["reads"] += 1
        if clip["reads"] == 3:
            clip["text"] = GOOD["gemini"]  # user clicked Copy on the page
        return clip["text"]

    def copy(text):
        clip["text"] = text

    ok, _msg = await connect(hub, "gemini", open_browser=lambda u: None, timeout=5,
                            use_clipboard=True,
                            clipboard_kwargs={"paste": paste, "copy": copy, "interval": 0.001})
    assert ok and saved["gemini"] == GOOD["gemini"]
    assert clip["text"] == ""  # clipboard cleared afterwards


async def test_connect_manual_paste_fallback(providers):
    saved = {}
    hub = make_hub(providers, saved)
    ok, _ = await connect(hub, "mistral", open_browser=lambda u: None, timeout=0.05,
                          use_clipboard=False, ask_paste=lambda name: GOOD["mistral"])
    assert ok and saved["mistral"] == GOOD["mistral"]


async def test_watch_clipboard_ignores_non_keys():
    seq = iter(["old", "hello", "gsk_bad", "gsk_" + "A1b2" * 13])
    got = await watch_clipboard(r"gsk_[A-Za-z0-9]{40,64}", timeout=2, interval=0.001,
                                paste=lambda: next(seq), copy=lambda t: None)
    assert got == "gsk_" + "A1b2" * 13


def test_connect_order(providers):
    assert connect_order(providers)[:5] == ["openrouter", "groq", "gemini", "cerebras", "mistral"]
