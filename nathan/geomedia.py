"""Validated media catalogue and agent actions; media itself loads in the browser."""
from __future__ import annotations

import asyncio
import json
import os
import random
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit

import httpx2 as httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from nathan import globe

FIXTURE = Path(__file__).parent / "web/static/data/geomedia.json"
_cameras: list[dict] = []


async def findPublicCameras(place: str, near: str = "", prefer_video: bool = True) -> dict:
    """Load a bounded public search into the native globe and select a camera: random, or the
    working one closest to `near` (a landmark, business or address inside `place`)."""
    global _cameras
    from nathan.opencctv import cameras

    place = place.strip()
    if not place or len(place) > 300:
        raise ValueError("Enter a location (up to 300 characters).")
    location = await globe.geocode(place)
    if location is None:
        raise ValueError(f"Couldn't locate {place!r}.")
    target = None
    if near.strip():
        target = await globe.geocode(f"{near.strip()}, {place}") or await globe.geocode(near.strip())
        if target is None:
            raise ValueError(f"Found {location['label']}, but couldn't find {near.strip()!r} there.")
    url = "https://opencctv.org/?" + urlencode({
        "lat": location["lat"], "lng": location["lon"], "zoom": 12,
    })
    rows = await cameras(location)
    nodes = []
    types = {"image": "image-poll", "m3u8": "hls", "mp4": "mp4", "iframe": "iframe"}
    for row in rows:
        if row.get("feed_type") not in types:
            continue
        try:
            nodes.append(GeoMediaNode.model_validate({
                "id": row["id"], "title": row["name"], "category": row.get("category") or "camera",
                "coordinates": [row["lng"], row["lat"]], "country": row.get("country"),
                "region": row.get("state"), "streamUrl": row["feed_url"],
                "streamType": types[row["feed_type"]],
                "refreshIntervalMs": max(1000, int(row.get("update_rate") or 30000)),
                "attributionUrl": f"https://opencctv.org/cam/{row['camera_code']}",
            }).model_dump(mode="json", exclude_none=True))
        except (ValueError, KeyError, TypeError):
            continue
    if not nodes:
        raise ValueError(f"No supported public cameras found for {place!r}. Try a nearby city.")
    # Random camera each time: live video first, then refreshing images; embeds can't be
    # checked ahead (they're often black), so they come last.
    random.shuffle(nodes)
    order = {"hls": 0, "mp4": 0, "image-poll": 1, "iframe": 2}
    nodes.sort(key=lambda n: order[n["streamType"]])
    if target:  # "closest to X": nearest first, so the feed check covers the right cameras
        nodes.sort(key=lambda n: _km(target, n))
    # Camera servers go down (half of Colorado's were unreachable): open one that answers.
    alive = await _alive(nodes[:16])
    def rank(n: dict) -> tuple:
        video = order[n["streamType"]] if prefer_video else 0
        return (n["id"] not in alive, video, _km(target, n)) if target else (n["id"] not in alive, video)
    nodes.sort(key=rank)
    _cameras = nodes
    result = {"url": url, "label": location["label"], "count": len(nodes), "camera": nodes[0]["title"]}
    if target:
        result["near"] = target["label"]
        result["distance_km"] = round(_km(target, nodes[0]), 2)
        location = {**location, "lat": target["lat"], "lon": target["lon"]}
    globe.focus.pop("cctv", None)
    globe.focus.update(lat=location["lat"], lon=location["lon"], alt=0.15,
                       label=location["label"], layers=["media"], replaceLayers=True,
                       media={"visible": True, "id": nodes[0]["id"], "nodes": nodes},
                       v=globe.focus["v"] + 1)
    return result


def _km(point: dict, node: dict) -> float:
    from nathan.opencctv import _km as km

    return km(point["lat"], point["lon"], node["coordinates"][1], node["coordinates"][0])


async def _alive(nodes: list[dict]) -> set[str]:
    """IDs of feeds that answer within a few seconds. Iframe embeds can't be checked."""
    async def ok(http: httpx.AsyncClient, node: dict) -> bool:
        if node["streamType"] == "iframe":
            return False
        try:
            async with http.stream("GET", node["streamUrl"]) as r:
                return r.status_code == 200
        except httpx.HTTPError:
            return False

    async with httpx.AsyncClient(timeout=5, headers=globe.UA, follow_redirects=True) as http:
        results = await asyncio.gather(*(ok(http, n) for n in nodes))
    return {n["id"] for n, good in zip(nodes, results, strict=True) if good}


class GeoMediaNode(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: str = Field(min_length=1)
    coordinates: tuple[float, float]
    region: str | None = None
    country: str | None = None
    streamUrl: str
    streamType: Literal["hls", "image-poll", "mp4", "iframe"]
    refreshIntervalMs: int | None = Field(default=None, ge=1000)
    attributionUrl: str | None = None

    @field_validator("coordinates", mode="before")
    @classmethod
    def coordinates_valid(cls, value):
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise ValueError("coordinates must be [longitude, latitude]")
        if any(type(v) not in (float, int) for v in value):
            raise ValueError("coordinates must be numbers")
        if not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90:
            raise ValueError("coordinates outside Earth")
        return tuple(value)

    @field_validator("streamUrl", "attributionUrl")
    @classmethod
    def safe_url(cls, value):
        if value is None:
            return value
        parsed = urlsplit(value)
        if (parsed.scheme in ("http", "https") and parsed.hostname
                and not parsed.username and not parsed.password):
            return value
        if value.startswith("/static/") and not parsed.netloc and "\\" not in value:
            return value
        raise ValueError("expected HTTP(S) or /static/ URL")


def parse_nodes(payload) -> list[dict]:
    if isinstance(payload, dict) and payload.get("type") == "FeatureCollection":
        rows = []
        for feature in payload["features"]:
            geometry = feature.get("geometry") or {}
            if geometry.get("type") != "Point":
                raise ValueError("GeoMedia GeoJSON requires Point features")
            rows.append({**feature.get("properties", {}), "coordinates": geometry["coordinates"]})
    else:
        rows = payload.get("items", payload.get("nodes")) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise TypeError("expected node array, items/nodes array, or GeoJSON FeatureCollection")
    nodes = [GeoMediaNode.model_validate(row).model_dump(mode="json", exclude_none=True) for row in rows]
    if len({n["id"] for n in nodes}) != len(nodes):
        raise ValueError("duplicate media IDs")
    return nodes


async def catalogue() -> dict:
    if _cameras:
        return {"items": _cameras, "source": "opencctv"}
    endpoint = os.getenv("NEXT_PUBLIC_GEOMEDIA_ENDPOINT", "").strip()
    if endpoint:
        try:
            if urlsplit(endpoint).scheme not in ("http", "https"):
                raise ValueError("endpoint must use HTTP(S)")
            raw = await globe._get("geomedia:" + endpoint, 300, endpoint)
            return {"items": parse_nodes(raw), "source": "remote"}
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            warning = "Media endpoint unavailable or invalid; showing demo fixtures."
    else:
        warning = "Demo media: coordinates are illustrative, not live camera locations."
    return {"items": parse_nodes(json.loads(FIXTURE.read_text(encoding="utf-8"))),
            "source": "fixture", "warning": warning}


async def filterMediaNodes(region: str = "", category: str = "", country: str = "",
                           visible: bool = True) -> str:
    result = await catalogue()
    filters = {"region": region, "category": category, "country": country}
    items = [n for n in result["items"] if all(
        not value or n.get(key, "").casefold() == value.casefold() for key, value in filters.items())]
    globe.focus.update(media={**filters, "visible": visible, "id": None}, v=globe.focus["v"] + 1)
    return json.dumps({"count": len(items), "items": [{"id": n["id"], "title": n["title"]}
                                                     for n in items[:30]],
                       "warning": result.get("warning")})


async def flyToMediaNode(id: str) -> str:
    node = next((n for n in (await catalogue())["items"] if n["id"] == id), None)
    if node is None:
        return f"No media node with ID {id!r}; use filterMediaNodes to find IDs."
    globe.focus.update(lat=node["coordinates"][1], lon=node["coordinates"][0], alt=0.15,
                       label=node["title"], media={"visible": True, "id": id,
                                                  "region": "", "category": "", "country": ""},
                       v=globe.focus["v"] + 1)
    return f"World view: opening {node['title']}."
