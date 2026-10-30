"""Canvas: find your assignments and open them, through Canvas's official REST API.

Why the API and not the website? Nathan's browser isn't signed in to your school, so opening
canvas.instructure.com just lands on a login page. A personal access token (Canvas: Account →
Settings → + New Access Token, free, made by you, revocable any time) lets Nathan read your
courses and assignments in about a second. The assignment then opens in YOUR browser, where
you're already signed in. Token and school address live in the OS keychain.
"""

from __future__ import annotations

import asyncio
import re
import webbrowser
from datetime import datetime
from urllib.parse import urlsplit

import httpx2 as httpx

from nathan.keys import get_key, set_key
from nathan.tools.base import Tool, params

URL_KEY, TOKEN_KEY = ("canvas_url", "CANVAS_URL"), ("canvas", "CANVAS_TOKEN")
SETUP = ("Canvas isn't connected yet. In Nathan open Accounts → Canvas, enter your school's Canvas "
         "address (e.g. https://yourschool.instructure.com) and an access token from Canvas → "
         "Account → Settings → + New Access Token.")


def settings() -> tuple[str, str] | None:
    url, token = get_key(*URL_KEY), get_key(*TOKEN_KEY)
    return (url.rstrip("/"), token) if url and token else None


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or "." not in parts.hostname:
        raise ValueError("enter your school's Canvas address, like https://yourschool.instructure.com")
    return f"https://{parts.hostname}"


async def connect(url: str, token: str) -> str:
    """Check the address and token against Canvas, then save them. Returns your Canvas name."""
    url, token = normalize_url(url), token.strip()
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as http:
        r = await http.get(f"{url}/api/v1/users/self", headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise ValueError("Canvas rejected that token: make a new one under Account → Settings")
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        raise ValueError(f"that doesn't look like a Canvas site (HTTP {r.status_code})")
    set_key(*URL_KEY, url)
    set_key(*TOKEN_KEY, token)
    return r.json().get("name", "you")


async def _get(http: httpx.AsyncClient, url: str, **params) -> list[dict]:
    r = await http.get(url, params={"per_page": 100, **params})
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def score(query: str, name: str) -> float:
    """How well an assignment name matches the request (words in common, 'pre-lab' = 'prelab')."""
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower().replace("-", ""))
    q, n = set(norm(query).split()), norm(name)
    if not q:
        return 0.0
    return sum(1.0 for w in q if w in n) / len(q) + (1.0 if norm(query) in n else 0.0)


async def find(query: str, course: str = "") -> list[dict]:
    """Matching assignments across your active courses, best first, upcoming before past."""
    cfg = settings()
    if not cfg:
        raise RuntimeError(SETUP)
    base, token = cfg
    async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                 headers={"Authorization": f"Bearer {token}"}) as http:
        courses = await _get(http, f"{base}/api/v1/courses", enrollment_state="active")
        if course:
            courses = [c for c in courses if score(course, c.get("name", "") + " " + c.get("course_code", "")) > 0] or courses
        lists = await asyncio.gather(*(_get(http, f"{base}/api/v1/courses/{c['id']}/assignments",
                                            order_by="due_at") for c in courses if c.get("id")),
                                     return_exceptions=True)
    now = datetime.now().astimezone()
    found = []
    for c, items in zip([c for c in courses if c.get("id")], lists, strict=True):
        if isinstance(items, BaseException):
            continue
        for a in items:
            s = score(query, a.get("name", ""))
            if s <= 0:
                continue
            due = a.get("due_at")
            upcoming = bool(due) and datetime.fromisoformat(due) >= now
            found.append({"name": a.get("name"), "course": c.get("name"), "due": due, "url": a.get("html_url"),
                          "score": s + (0.5 if upcoming else 0)})
    return sorted(found, key=lambda a: -a["score"])[:10]


async def canvas_assignment(query: str, course: str = "", open_it: bool = True) -> str:
    try:
        hits = await find(query, course)
    except RuntimeError as e:
        return str(e)
    except httpx.HTTPError as e:
        return f"Canvas didn't answer ({type(e).__name__}); try again in a moment."
    if not hits:
        return f"No assignment matching {query!r} in your active Canvas courses."
    best = hits[0]
    if open_it and best["url"]:
        webbrowser.open(best["url"])
    others = "\n".join(f"- {h['name']} ({h['course']}, due {h['due'] or 'no date'}) {h['url']}" for h in hits[1:5])
    return (f"{'Opened' if open_it else 'Found'} {best['name']} ({best['course']}, due {best['due'] or 'no date'}): "
            f"{best['url']}" + (f"\nOther matches:\n{others}" if others else ""))


def make_tools() -> list[Tool]:
    return [Tool("canvas_assignment",
                 "Find a Canvas (school) assignment by words in its name, e.g. 'pre-lab', and open it in "
                 "the person's browser where they're signed in. Use this for anything about Canvas, "
                 "assignments, homework or courses; never open the Canvas login page.",
                 params(["query"], query=("string", "words from the assignment name, e.g. 'pre-lab 5'"),
                        course=("string", "optional course name or code to narrow it"),
                        open_it=("boolean", "open the best match in the browser (default true)")),
                 canvas_assignment)]
