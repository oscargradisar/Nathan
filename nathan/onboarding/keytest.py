"""Check that a key works before saving it: GET {base_url}/models with the key.

Listing models is free on every provider (it doesn't use chat quota)."""

from __future__ import annotations

import httpx2 as httpx

from nathan.config import Provider


async def check_key(provider: Provider, key: str, client: httpx.AsyncClient | None = None) -> tuple[bool, str]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        resp = await client.get(provider.base_url.rstrip("/") + "/models",
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
