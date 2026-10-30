"""Images dropped into the console: validated, described by a model with eyes, then worked on."""

import base64
import time

import pytest
from fastapi.testclient import TestClient

from nathan import vision
from nathan.web.app import create_app
from tests.fakes import fake_providers

H = {"x-nathan": "1"}
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


def test_save_validates_type_count_and_size(tmp_path):
    [path] = vision.save([PNG], tmp_path)
    assert path.suffix == ".png" and path.read_bytes().startswith(b"\x89PNG")
    with pytest.raises(ValueError, match="PNG, JPEG"):
        vision.save(["data:text/html;base64,PGI+"], tmp_path)
    with pytest.raises(ValueError, match="up to"):
        vision.save([PNG] * 5, tmp_path)


def test_brief_puts_descriptions_under_the_request():
    text = vision.brief("what does this error mean?", [("TypeError: x is None", "gemini/flash")])
    assert text.startswith("what does this error mean?")
    assert "[Attached image 1, as described by gemini/flash]\nTypeError: x is None" in text


def test_dropped_image_is_seen_then_answered(make_engine, monkeypatch):
    providers = fake_providers({"eyes": {"see": ["vision"]},
                                "alpha": {"a-big": ["reasoning", "writing", "judge"]}})
    seen_by, asked = [], []

    def responder(ref, messages, **kw):
        content = messages[-1]["content"]
        if isinstance(content, list):  # the picture goes only to the model with eyes
            seen_by.append(ref)
            assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
            return "A red error dialog: 'Disk full'."
        asked.append(content)
        return "Free up space on the disk."

    engine = make_engine(responder, providers)
    client = TestClient(create_app(engine))
    with client:
        run = client.post("/api/runs", json={"prompt": "fix this", "mode": "single", "images": [PNG]},
                          headers=H).json()
        for _ in range(100):
            if client.app.state.runs.get(run["id"]).finished:
                break
            time.sleep(0.05)
        r = client.app.state.runs.get(run["id"])
    assert seen_by == ["eyes/see"]
    assert any("Disk full" in a and a.startswith("fix this") for a in asked)
    assert r.final == "Free up space on the disk."
    bad = client.post("/api/runs", json={"prompt": "x", "images": ["data:image/svg+xml;base64,PHN2Zz4="]}, headers=H)
    assert bad.status_code == 400
