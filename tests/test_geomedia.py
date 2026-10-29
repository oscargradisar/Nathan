import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from nathan import geomedia, globe
from nathan.tools import default_toolbox
from nathan.web.app import create_app


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(geomedia, "_cameras", [])
    monkeypatch.delenv("NEXT_PUBLIC_GEOMEDIA_ENDPOINT", raising=False)
    monkeypatch.setattr(globe, "focus", {"lat": 0, "lon": 0, "alt": 2, "layers": [], "v": 0})


def test_contract_and_geojson():
    node = geomedia.parse_nodes(json.loads(geomedia.FIXTURE.read_text()))[0]
    assert geomedia.parse_nodes({"type": "FeatureCollection", "features": [{
        "type": "Feature", "geometry": {"type": "Point", "coordinates": node["coordinates"]},
        "properties": {k: v for k, v in node.items() if k != "coordinates"},
    }]}) == [node]
    for change in ({"coordinates": [181, 0]}, {"coordinates": [False, 0]},
                   {"coordinates": [0, float("nan")]}, {"streamType": "script"},
                   {"streamUrl": "javascript:alert(1)"}, {"streamUrl": "//evil.test/"},
                   {"refreshIntervalMs": 0}, {"extra": 1}, {"id": ""}):
        with pytest.raises((ValueError, ValidationError)):
            geomedia.parse_nodes([{**node, **change}])
    with pytest.raises(ValueError, match="duplicate"):
        geomedia.parse_nodes([node, node])


async def test_endpoint_and_fallback(monkeypatch):
    fixture = json.loads(geomedia.FIXTURE.read_text())
    monkeypatch.setenv("NEXT_PUBLIC_GEOMEDIA_ENDPOINT", "https://example.test/media")

    async def get(*args):
        assert args[-1] == "https://example.test/media"
        return {"items": fixture}

    monkeypatch.setattr(globe, "_get", get)
    assert (await geomedia.catalogue())["source"] == "remote"

    async def bad(*args):
        return [{"id": "broken"}]

    monkeypatch.setattr(globe, "_get", bad)
    fallback = await geomedia.catalogue()
    assert fallback["source"] == "fixture" and len(fallback["items"]) == 8
    assert "invalid" in fallback["warning"]


async def test_agent_hooks(tmp_path):
    box = default_toolbox(workspace=tmp_path)
    result = json.loads(await box.call("filterMediaNodes", {"region": "europe", "category": "city"}))
    assert result["count"] == 4
    assert globe.focus["media"]["visible"]
    assert "opening" in await box.call("flyToMediaNode", {"id": "demo-paris"})
    assert globe.focus["media"]["id"] == "demo-paris"
    assert globe.focus["lat"] == 48.8566
    version = globe.focus["v"]
    assert "No media node" in await box.call("flyToMediaNode", {"id": "missing"})
    assert globe.focus["v"] == version
    await box.call("filterMediaNodes", {"visible": False})
    assert not globe.focus["media"]["visible"]


def test_catalogue_route(make_engine):
    client = TestClient(create_app(make_engine(lambda *args, **kwargs: "ok")))
    result = client.get("/api/geomedia/catalogue")
    assert result.status_code == 200
    assert result.json()["source"] == "fixture"
    assert {n["streamType"] for n in result.json()["items"]} == {"image-poll", "hls", "mp4", "iframe"}


async def test_opencctv_is_location_only_and_on_demand(monkeypatch, tmp_path):
    calls = []

    async def locate(place):
        calls.append(place)
        return {"lat": 0.0, "lon": 0.0, "label": "Test location"}

    monkeypatch.setattr(globe, "geocode", locate)
    async def lookup(place):
        return [{"id": "real-camera", "name": "Public camera", "lat": 0, "lng": 0,
                 "feed_url": "https://example.test/live.jpg", "feed_type": "image",
                 "camera_code": 123}]

    monkeypatch.setattr("nathan.opencctv.cameras", lookup)
    box = default_toolbox(workspace=tmp_path)
    await geomedia.catalogue()
    assert calls == []
    result = await geomedia.findPublicCameras(" Test location ")
    assert result["url"] == "https://opencctv.org/?lat=0.0&lng=0.0&zoom=12"
    assert globe.focus["media"]["id"] == "real-camera"
    assert globe.focus["media"]["nodes"][0]["coordinates"] == [0, 0]
    assert "cctv" not in globe.focus
    assert (await geomedia.catalogue())["source"] == "opencctv"
    assert "opencctv.org" in await box.call("findPublicCameras", {"place": "Test location"})
    with pytest.raises(ValueError):
        await geomedia.findPublicCameras(" ")
    assert len(calls) == 2


def test_camera_lookup_endpoint(make_engine, monkeypatch):
    async def locate(place):
        return None

    monkeypatch.setattr(globe, "geocode", locate)
    client = TestClient(create_app(make_engine(lambda *args, **kwargs: "ok")))
    assert client.post("/api/geomedia/cameras", json={"place": "Paris"}).status_code == 403
    assert client.post("/api/geomedia/cameras", json={"place": "Paris"},
                       headers={"x-nathan": "1"}).status_code == 400
