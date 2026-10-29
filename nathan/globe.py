"""World view: open, public data for the globe. Free, keyless, cached briefly.

  planes   OpenSky Network live aircraft (anonymous API, rate-limited)
  quakes   USGS earthquakes in the last day
  iss      where the International Space Station is right now
  shops    OpenStreetMap shops and places near a location
  geocode  OpenStreetMap Nominatim: place name -> coordinates

`focus` is shared state: Nathan's agent (or you) points the globe somewhere and the World
view flies there.
"""

from __future__ import annotations

import time
from typing import Any

import httpx2 as httpx

UA = {"user-agent": "Nathan/1.0 (https://github.com/oscargradisar/Nathan) httpx2"}
_cache: dict[str, tuple[float, Any]] = {}
focus: dict[str, Any] = {"lat": 46.05, "lon": 14.51, "alt": 2.2, "label": "", "layers": ["quakes", "iss"], "v": 0}


async def _get(key: str, ttl: float, url: str, **kw) -> Any:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    async with httpx.AsyncClient(timeout=35, headers=UA, follow_redirects=True) as http:
        r = await http.get(url, **kw)
        r.raise_for_status()
        data = r.json()
    _cache[key] = (time.time(), data)
    return data


async def geocode(place: str) -> dict | None:
    rows = await _get(f"geo:{place.lower()}", 86400, "https://nominatim.openstreetmap.org/search",
                      params={"q": place, "format": "json", "limit": 1})
    if not rows:
        return None
    r = rows[0]
    return {"lat": float(r["lat"]), "lon": float(r["lon"]), "label": r.get("display_name", place)[:80]}


async def planes(lat: float, lon: float, span: float = 4.0) -> list[dict]:
    j = await _get(f"planes:{round(lat)}:{round(lon)}:{span}", 30, "https://opensky-network.org/api/states/all",
                   params={"lamin": lat - span, "lamax": lat + span, "lomin": lon - span * 1.5, "lomax": lon + span * 1.5})
    out = []
    for s in (j.get("states") or [])[:400]:
        if s[5] is not None and s[6] is not None:
            out.append({"lat": s[6], "lon": s[5], "label": (s[1] or "").strip() or s[0], "country": s[2],
                        "alt_m": s[7], "speed_ms": s[9], "heading": s[10]})
    return out


async def quakes() -> list[dict]:
    j = await _get("quakes", 300, "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson")
    return [{"lat": f["geometry"]["coordinates"][1], "lon": f["geometry"]["coordinates"][0],
             "mag": f["properties"]["mag"] or 0, "label": f["properties"]["place"]} for f in j.get("features", [])]


async def iss() -> dict:
    j = await _get("iss", 5, "https://api.wheretheiss.at/v1/satellites/25544")
    return {"lat": j["latitude"], "lon": j["longitude"], "alt_km": round(j["altitude"]), "label": "ISS"}


async def shops(lat: float, lon: float, kind: str = "", radius_m: int = 1500) -> list[dict]:
    tag = f'["shop"="{kind}"]' if kind else '["shop"]'
    q = f"[out:json][timeout:20];node{tag}(around:{radius_m},{lat},{lon});out 150;"
    j = await _get(f"shops:{lat:.3f}:{lon:.3f}:{kind}:{radius_m}", 3600, "https://overpass-api.de/api/interpreter",
                   params={"data": q})
    return [{"lat": e["lat"], "lon": e["lon"], "label": e.get("tags", {}).get("name") or e.get("tags", {}).get("shop", "shop"),
             "kind": e.get("tags", {}).get("shop", "")} for e in j.get("elements", [])]


async def point(place: str, layers: list[str]) -> str:
    """Fly the globe to a place with some layers on; returns a one-line summary for the agent."""
    g = await geocode(place)
    if g is None:
        return f"Couldn't find {place!r} on the map."
    layers = [x for x in layers if x in ("planes", "quakes", "iss", "shops")] or ["planes"]
    focus.update(lat=g["lat"], lon=g["lon"], alt=0.6 if "shops" in layers else 1.2, label=g["label"],
                 layers=layers, v=focus["v"] + 1)
    parts = []
    try:
        if "planes" in layers:
            parts.append(f"{len(await planes(g['lat'], g['lon']))} aircraft overhead")
        if "shops" in layers:
            parts.append(f"{len(await shops(g['lat'], g['lon']))} shops nearby")
        if "quakes" in layers:
            parts.append(f"{len(await quakes())} earthquakes worldwide today")
    except httpx.HTTPError as e:
        parts.append(f"some data didn't load ({type(e).__name__})")
    return f"World view: flying to {g['label']}" + (": " + ", ".join(parts) if parts else "") + "."
