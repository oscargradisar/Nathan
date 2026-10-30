import pytest
from fastapi.testclient import TestClient

from nathan import desktop, geomedia, globe
from nathan.web.app import create_app


@pytest.mark.parametrize(("prompt", "kind", "place", "layers"), [
    ("show aircraft over Paris", "layers", "Paris", ["planes"]),
    ("earthquakes near Japan", "layers", "Japan", ["quakes"]),
    ("public cameras in Ljubljana", "cameras", "Ljubljana", []),
    ("show me live cameras near New York", "cameras", "New York", []),
    ("Paris", "layers", "Paris", ["planes"]),
    ("fly to London", "layers", "London", ["planes"]),
    ("show earthquakes worldwide", "layers", "", ["quakes"]),
    ("show me aircraft and earthquakes near Tokyo", "layers", "Tokyo", ["planes", "quakes"]),
    ("Thessaloniki cameras", "cameras", "Thessaloniki", []),
])
def test_parse_search(prompt, kind, place, layers):
    assert globe.parse_search(prompt) == (kind, place, layers)


async def test_search_geocodes_only_place_and_replaces_layers(monkeypatch):
    calls = []

    async def geocode(place):
        calls.append(place)
        return {"lat": 48.8, "lon": 2.3, "label": place}

    monkeypatch.setattr(globe, "geocode", geocode)
    monkeypatch.setattr(globe, "focus", {"layers": ["iss"], "v": 0, "cctv": {"url": "old"}})
    result = await globe.search("show aircraft over Paris")
    assert calls == ["Paris"]
    assert result["focus"]["layers"] == ["planes"]
    assert result["focus"]["replaceLayers"] is True
    assert "cctv" not in result["focus"]


async def test_search_camera_dispatch_and_missing_place(monkeypatch):
    calls = []

    async def cameras(place, near="", prefer_video=True):
        calls.append(place)
        return {"url": "https://opencctv.org/?lat=1&lng=2&zoom=12", "label": place, "count": 1}

    monkeypatch.setattr(geomedia, "findPublicCameras", cameras)
    result = await globe.search("public cameras in Ljubljana")
    assert result["kind"] == "cameras" and calls == ["Ljubljana"]
    with pytest.raises(ValueError, match="Where"):
        await globe.search("show me cameras")
    with pytest.raises(ValueError):
        await globe.search(" ")


def test_routes_and_local_security(make_engine, monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "open_camera_window", calls.append)

    async def geocode(place):
        return {"lat": 48.8, "lon": 2.3, "label": place}

    monkeypatch.setattr(globe, "geocode", geocode)
    monkeypatch.setattr(globe, "focus", {"layers": [], "v": 0})
    client = TestClient(create_app(make_engine(lambda *args, **kwargs: "ok")))
    assert client.post("/api/globe/search", json={"place": "Paris"}).status_code == 403
    response = client.post("/api/globe/search", json={"place": "aircraft over Paris"},
                           headers={"x-nathan": "1"})
    assert response.status_code == 200 and response.json()["kind"] == "layers"
    assert client.post("/api/globe/search", json={"place": "cameras"},
                       headers={"x-nathan": "1"}).status_code == 400
    response = client.post("/api/geomedia/cameras/open", json={"url": "https://opencctv.org/"},
                           headers={"x-nathan": "1"})
    assert response.json() == {"opened": True} and calls == ["https://opencctv.org/"]
async def test_fast_worker_normalizes_conversational_camera_request(monkeypatch):
    from types import SimpleNamespace

    from nathan import globe

    asked = []

    async def answer(client, refs, messages, schema, **kwargs):
        asked.append((refs, messages[-1]["content"]))
        return schema(kind="cameras", place="Colorado, United States", layers=[]), refs[0]

    monkeypatch.setattr(globe, "race_json", answer)
    engine = SimpleNamespace(fastest=lambda tags: ["fastest/model", "other/model"], client=object())
    prompt = "Open a camera of colorado somewhere random"
    intent = await globe.interpret(engine, prompt)
    assert (intent.kind, intent.place, intent.near) == ("cameras", "Colorado, United States", "")
    assert asked == [(["fastest/model", "other/model"], prompt)]


async def test_worker_timeout_falls_back_without_filler(monkeypatch):
    from types import SimpleNamespace

    from nathan import globe

    async def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr(globe, "race_json", timeout)
    engine = SimpleNamespace(fastest=lambda tags: ["fast/model"], client=object())
    intent = await globe.interpret(engine, "Open a camera of colorado somewhere random")
    assert (intent.kind, intent.place, intent.layers) == ("cameras", "colorado", [])
    # rules alone still split the landmark off the place
    intent = await globe.interpret(engine, "video camera in pueblo colorado closest to Mr Tandoori's restaurant")
    assert (intent.place, intent.near) == ("pueblo colorado", "Mr Tandoori")
