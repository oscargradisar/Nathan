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

import asyncio
import re
import time
from typing import Any, Literal

import httpx2 as httpx
from pydantic import BaseModel, Field

from nathan.errors import NoModelAvailable
from nathan.jsonutil import race_json

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
    """Place name -> coordinates plus city/state. Nominatim first, Photon as backup."""
    try:
        rows = await _get(f"geo:{place.lower()}", 86400, "https://nominatim.openstreetmap.org/search",
                          params={"q": place, "format": "json", "limit": 1, "addressdetails": 1})
    except httpx.HTTPError:
        rows = []
    if rows:
        r, a = rows[0], rows[0].get("address", {})
        return {"lat": float(r["lat"]), "lon": float(r["lon"]), "label": r.get("display_name", place)[:80],
                "city": a.get("city") or a.get("town") or a.get("village") or "", "state": a.get("state", "")}
    _cache.pop(f"geo:{place.lower()}", None)  # don't remember a miss for a day
    try:  # Photon (komoot) is also free, keyless and more forgiving of loose text
        j = await _get(f"photon:{place.lower()}", 86400, "https://photon.komoot.io/api/",
                       params={"q": place, "limit": 1})
    except httpx.HTTPError:
        return None
    if not j.get("features"):
        return None
    f = j["features"][0]
    lon, lat = f["geometry"]["coordinates"]
    pr = f.get("properties", {})
    city = pr.get("city") or (pr.get("name", "") if pr.get("type") == "city" else "")
    label = ", ".join(x for x in (pr.get("name"), pr.get("state"), pr.get("country")) if x) or place
    return {"lat": float(lat), "lon": float(lon), "label": label[:80], "city": city, "state": pr.get("state", "")}


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
    layers = [x for x in layers if x in ("planes", "quakes", "iss", "shops", "media")] or ["planes"]
    focus.pop("media", None)
    focus.pop("cctv", None)
    focus.pop("replaceLayers", None)
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


def parse_search(prompt: str) -> tuple[str, str, list[str]]:
    """Extract a layer and place before handing only the place to the geocoder."""
    prompt = prompt.strip().strip("?.!")
    if not prompt or len(prompt) > 500:
        raise ValueError("Describe what you want to see (up to 500 characters).")
    patterns = {
        "cameras": r"(?:public\s+|live\s+|traffic\s+)?(?:cameras?|webcams?|cctv)",
        "planes": r"aircraft|airplanes?|aeroplanes?|planes?|flights?",
        "quakes": r"earthquakes?|quakes?|tremors?",
        "iss": r"iss|international space station",
        "shops": r"shops?|places",
    }
    matches = [(match, layer) for layer, pattern in patterns.items()
               for match in re.finditer(r"\b(?:" + pattern + r")\b", prompt, re.IGNORECASE)]
    if not matches:
        place = re.sub(r"^(?:please\s+)?(?:fly|go|take me)\s+to\s+", "", prompt, flags=re.IGNORECASE)
        return "layers", place, ["planes"]
    matches.sort(key=lambda pair: pair[0].start())
    layers = list(dict.fromkeys(layer for _, layer in matches if layer != "cameras"))
    kind = "cameras" if any(layer == "cameras" for _, layer in matches) else "layers"
    place = prompt[matches[-1][0].end():].strip()
    place = re.sub(r"^(?:\s*(?:in|near|over|around|at|of|for|from|somewhere in)\b)+", "", place, flags=re.IGNORECASE).strip()
    place = re.sub(r"\b(?:somewhere|randomly|random|anywhere|please|today|right now)\b", "", place, flags=re.IGNORECASE).strip(" \"'")
    if not place:
        prefix = prompt[:matches[0][0].start()].strip()
        prefix = re.sub(r"^(?:(?:please|show|me|find|open|display|the|live|latest|recent|all|public|traffic|see)\b\s*)+", "", prefix, flags=re.IGNORECASE)
        place = prefix.strip()
    if place.lower() in ("worldwide", "globally", "around the world", "everywhere", "the world"):
        place = ""
    if kind == "cameras" and not place:
        raise ValueError("Where would you like to see cameras? Try 'public cameras in Ljubljana'.")
    return kind, place, layers


class WorldIntent(BaseModel):
    kind: Literal["cameras", "layers"]
    place: str = Field(max_length=300)
    near: str = Field("", max_length=200)
    prefer_video: bool = True
    layers: list[Literal["planes", "quakes", "iss", "shops"]] = Field(default_factory=list)


INTENT_BRIEF = """You turn a request for Nathan's globe into JSON. Think about what the person
actually wants, then reply with ONLY this JSON:
{"kind": "cameras" or "layers",
 "place": "the area to search, as city, state/region, country (e.g. Pueblo, Colorado, United States)",
 "near": "a specific landmark, business, street or address the camera should be closest to, else empty",
 "prefer_video": true unless they only want the nearest camera whatever its type, or ask for photos/images,
 "layers": subset of ["planes","quakes","iss","shops"] actually requested (cameras: [])}
Rules: fix spelling and expand abbreviations; never put filler (random, somewhere, show me, video,
closest, please) in place or near; keep a business name exactly as said (e.g. Mr Tandoori's restaurant
-> near "Mr Tandoori"); a bare state or country is a valid place; empty place means worldwide (layers only).
Examples:
"video camera in pueblo colorado closest to Mr Tandoori's restaurant" ->
 {"kind":"cameras","place":"Pueblo, Colorado, United States","near":"Mr Tandoori","prefer_video":true,"layers":[]}
"random cam somewhere in colorado" -> {"kind":"cameras","place":"Colorado, United States","near":"","prefer_video":true,"layers":[]}
"planes and quakes over japan" -> {"kind":"layers","place":"Japan","near":"","prefer_video":true,"layers":["planes","quakes"]}"""

_NEAR = re.compile(r"\b(?:closest|nearest|close|next)\s+to\s+(?:the\s+)?(.+)$", re.IGNORECASE)


def fallback_intent(prompt: str) -> WorldIntent:
    """No model answered: rules only. 'closest to X' becomes the landmark."""
    near = ""
    if m := _NEAR.search(prompt.strip().strip("?.!")):
        near = re.sub(r"(?:'s)?\s+(?:restaurant|store|shop|place|building)$", "", m.group(1).strip(), flags=re.IGNORECASE)
        prompt = prompt[:m.start()]
    kind, place, layers = parse_search(prompt)
    return WorldIntent(kind=kind, place=place, near=near, layers=layers)


async def interpret(engine, prompt: str) -> WorldIntent:
    if not prompt.strip() or len(prompt) > 500:
        raise ValueError("Describe what you want to see (up to 500 characters).")
    # Capable models, three racing at once: the first sensible answer wins, a slow one can't stall it.
    refs = engine.fastest(["reasoning"])[:6] if engine else []
    if refs:
        messages = [{"role": "system", "content": INTENT_BRIEF}, {"role": "user", "content": prompt}]
        try:
            intent, _ = await asyncio.wait_for(race_json(
                engine.client, refs, messages, WorldIntent, lanes=3, max_tokens=300, temperature=0), 15)
            if intent.kind == "layers" and not intent.layers:
                intent.layers = ["planes"]
            intent.place, intent.near = intent.place.strip(), intent.near.strip()
            if intent.kind != "cameras" or intent.place:
                return intent
        except (NoModelAvailable, TimeoutError):
            pass
    return fallback_intent(prompt)


async def search(prompt: str, engine=None) -> dict:
    intent = await interpret(engine, prompt)
    kind, place, layers = intent.kind, intent.place, intent.layers
    if kind == "cameras":
        from nathan.geomedia import findPublicCameras

        cam = await findPublicCameras(place, intent.near, intent.prefer_video)
        summary = f"Loaded {cam['count']} public cameras near {cam['label']}."
        if cam.get("near"):
            summary = (f"Closest working camera to {cam['near']}: {cam['camera']} "
                       f"({cam['distance_km']} km away). {cam['count']} cameras loaded.")
        return {"kind": kind, "summary": summary, "focus": focus}
    location = await geocode(place) if place else None
    if place and location is None:
        raise ValueError(f"Couldn't find {place!r}. Try a city, country or address.")
    focus.pop("media", None)
    focus.pop("cctv", None)
    if location:
        focus.update(lat=location["lat"], lon=location["lon"], label=location["label"], alt=1.2)
    focus.update(layers=layers, replaceLayers=True, v=focus["v"] + 1)
    labels = {"planes": "aircraft", "quakes": "earthquakes", "iss": "the ISS", "shops": "places"}
    summary = "Showing " + " and ".join(labels[layer] for layer in layers)
    if location:
        summary += " near " + location["label"]
    return {"kind": kind, "summary": summary + ".", "focus": focus}
