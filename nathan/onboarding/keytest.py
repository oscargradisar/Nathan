"""Check that a key works before saving it: GET {base_url}/models with the key.

Listing models is free on every provider (it doesn't use chat quota)."""

from __future__ import annotations

import os

import httpx2 as httpx

from nathan.config import Provider


async def check_key(provider: Provider, key: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        if provider.missing_env and provider.onboarding and provider.onboarding.account_id_url:
            await _find_account_id(provider, key, client)
        if provider.missing_env:
            return False, (f"{provider.name} also needs your account id "
                           f"({', '.join(provider.missing_env)}) and this key couldn't look it up")
        url = (provider.onboarding and provider.onboarding.check_url) or (
            provider.resolved_base_url.rstrip("/") + "/models")
        resp = await client.get(url,
                                headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as e:
        return False, f"could not reach {provider.name}: {type(e).__name__}"
    finally:
        if own:
            await client.aclose()
    if resp.status_code == 200:
        return True, "key works"
    if resp.status_code in (401, 403):
        return False, "the provider rejected this key"
    return False, f"unexpected HTTP {resp.status_code} from {provider.name}"


async def _find_account_id(provider: Provider, key: str, client: httpx.AsyncClient) -> None:
    """Ask the provider which account this key belongs to and fill the missing ${VAR}.

    Kept in the environment for now; the saver stores it with the key once the key works.
    Expects Cloudflare's reply shape: {"result": [{"id": "..."}]}."""
    resp = await client.get(provider.onboarding.account_id_url,
                            headers={"Authorization": f"Bearer {key}"})
    if resp.status_code != 200:
        return
    accounts = resp.json().get("result") or []
    if accounts:
        for var in provider.missing_env:
            os.environ[var] = accounts[0]["id"]
