"""OpenRouter's official OAuth PKCE flow: get a key without copy-paste.

PKCE ("pixie") in one paragraph: Nathan invents a random secret (the verifier) and sends
only its SHA-256 hash (the challenge) to OpenRouter in the browser. After you click
Authorize, OpenRouter redirects back to Nathan with a one-time code. Nathan sends the code
*plus the original verifier*; OpenRouter checks the hash matches and returns an API key.
Someone who intercepts the code can't use it without the verifier, which never left Nathan.
Docs: https://openrouter.ai/docs/use-cases/oauth-pkce
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import httpx2 as httpx

AUTH_URL = "https://openrouter.ai/auth"
EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"


def make_pkce() -> tuple[str, str]:
    """Return (verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(48)  # 64 URL-safe characters
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def auth_url(callback_url: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "callback_url": callback_url,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


class ExchangeError(Exception):
    pass


async def exchange_code(code: str, verifier: str,
                        client: httpx.AsyncClient | None = None) -> str:
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        resp = await client.post(EXCHANGE_URL, json={
            "code": code, "code_verifier": verifier, "code_challenge_method": "S256"})
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise ExchangeError(f"OpenRouter refused the code (HTTP {resp.status_code})")
    key = (resp.json() or {}).get("key")
    if not key:
        raise ExchangeError("OpenRouter's reply had no key")
    return key
