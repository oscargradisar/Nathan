"""Canvas: assignments are found through the API and the right one opens (no network)."""

import pytest

from nathan import router
from nathan.tools import canvas


@pytest.fixture
def school(monkeypatch):
    opened = []
    monkeypatch.setattr(canvas, "settings", lambda: ("https://school.instructure.com", "tok"))
    monkeypatch.setattr(canvas.webbrowser, "open", opened.append)

    async def get(http, url, **params):
        if url.endswith("/courses"):
            return [{"id": 1, "name": "General Chemistry I", "course_code": "CHEM 1111"},
                    {"id": 2, "name": "Physics", "course_code": "PHYS 2010"}]
        if "/courses/1/" in url:
            return [{"name": "Pre-Lab 4: Titration", "due_at": "2099-01-01T05:00:00Z", "html_url": "https://school/a/14"},
                    {"name": "Pre-Lab 3: Density", "due_at": "2020-01-01T05:00:00Z", "html_url": "https://school/a/13"},
                    {"name": "Exam 1", "due_at": None, "html_url": "https://school/a/1"}]
        return [{"name": "Lab report", "due_at": None, "html_url": "https://school/b/1"}]

    monkeypatch.setattr(canvas, "_get", get)
    return opened


async def test_finds_the_upcoming_prelab_and_opens_it(school):
    text = await canvas.canvas_assignment("pre-lab")
    assert school == ["https://school/a/14"]  # upcoming beats past, "pre-lab" matches "Pre-Lab"
    assert text.startswith("Opened Pre-Lab 4: Titration (General Chemistry I")
    assert "Pre-Lab 3" in text and "Exam 1" not in text


async def test_course_filter_and_not_connected(school, monkeypatch):
    hits = await canvas.find("lab", course="physics")
    assert [h["name"] for h in hits] == ["Lab report"]
    monkeypatch.setattr(canvas, "settings", lambda: None)
    assert "Accounts → Canvas" in await canvas.canvas_assignment("pre-lab")


def test_school_address_is_cleaned():
    assert canvas.normalize_url("myschool.instructure.com/courses/5") == "https://myschool.instructure.com"
    with pytest.raises(ValueError):
        canvas.normalize_url("http://localhost")


async def test_canvas_requests_skip_routing_and_go_to_the_agent():
    r = await router.route(None, "pull up my canvas assignment for the pre-lab")
    assert (r.pattern, r.by) == ("agent", "shortcut")
