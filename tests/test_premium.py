"""Research, Programming and manager reviews — sources and managers all faked (no network)."""

import json

import httpx2 as httpx

from nathan import managers
from nathan.code import files_of, run_code
from nathan.research import gather_evidence, run_research
from nathan.review import packet, run_review


def fake_sources():
    def handler(req):
        host = req.url.host
        if "wikipedia" in host:
            return httpx.Response(200, json={"query": {"pages": {"1": {
                "index": 1, "title": "Ljubljana", "fullurl": "https://en.wikipedia.org/wiki/Ljubljana",
                "extract": "Ljubljana is the capital of Slovenia, population about 295,000."}}}})
        if "duckduckgo" in host:
            return httpx.Response(200, json={"AbstractText": "Capital city of Slovenia.",
                                             "AbstractURL": "https://ddg.example/lj", "Heading": "Ljubljana"})
        if "openalex" in host:
            return httpx.Response(200, json={"results": [{"title": "Urban Ljubljana", "publication_year": 2020,
                                                          "doi": "https://doi.org/10/x",
                                                          "abstract_inverted_index": {"A": [0], "study": [1]}}]})
        if "arxiv" in host:
            return httpx.Response(200, text="<feed xmlns='http://www.w3.org/2005/Atom'></feed>")
        if "algolia" in host:
            return httpx.Response(200, json={"hits": []})
        if "stackexchange" in host:
            return httpx.Response(500)  # a failing source must not sink the research
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_gather_evidence_mixes_sources_and_survives_failures():
    events = []
    ev = await gather_evidence(["ljubljana"], events.append, fake_sources())
    sources = {e.source for e in ev}
    assert {"Wikipedia", "DuckDuckGo", "OpenAlex"} <= sources and [e.n for e in ev] == list(range(1, len(ev) + 1))
    assert any(e["type"] == "task_failed" and e["id"] == "src:Stack Overflow" for e in events)


async def test_research_with_a_premium_manager_verifying(make_engine):
    def responder(ref, messages, **kw):
        if "plan web research" in str(messages[0]["content"]):
            return json.dumps({"queries": ["ljubljana population"]})
        return "Ljubljana has about 295,000 people [1]."

    engine = make_engine(responder)
    asked = []

    async def ask(m, prompt, web=False):
        asked.append((m.id, web))
        return "Verified: about 295,000 [1]. Confidence: high" if "verifier" in prompt else "Gemini: 295k (web)"

    events = []
    r = await run_research(engine, "What is the population of Ljubljana?", events.append,
                           http=fake_sources(), team=[managers.CLAUDE, managers.GEMINI], ask=ask)
    assert ("gemini", True) in asked and ("claude", True) in asked  # both searched the web
    assert ("claude", False) in asked  # and the boss verified
    final = next(e for e in events if e["type"] == "final")
    assert final["writer"] == "Claude" and "Sources" in final["text"] and "wikipedia.org" in final["text"]
    assert r["evidence"]


async def test_code_mode_chatgpt_manages_and_files_are_saved(make_engine, tmp_path):
    engine = make_engine(lambda ref, m, **k: "```python\nprint('draft')\n```")
    engine.workspace = tmp_path
    spec = {"project": "Hello Tool", "language": "python",
            "files": [{"path": "hello.py", "purpose": "print hello"}, {"path": "../escape.py", "purpose": "x"}]}
    review = "### hello.py\n```python\nprint('hello')\n```\n\n## Review\nFixed the greeting."
    calls = []

    async def ask(m, prompt, web=False):
        calls.append(m.id)
        return json.dumps(spec) if "Plan the smallest" in prompt else review

    events = []
    r = await run_code(engine, "a hello world script", events.append,
                       team=[managers.CLAUDE, managers.CHATGPT], ask=ask)
    assert calls == ["chatgpt", "chatgpt"]  # ChatGPT runs Programming even with Claude signed in
    assert (tmp_path / "code" / "hello-tool" / "hello.py").read_text() == "print('hello')\n"
    assert not (tmp_path / "code" / "escape.py").exists()
    assert any(e["type"] == "saved" for e in events) and r["manager"] == "ChatGPT"


def test_files_of_parses_review_blocks():
    text = "### a.py\n```python\nx = 1\n```\n### `pkg/b.js`\n```js\nlet y\n```"
    assert files_of(text) == {"a.py": "x = 1\n", "pkg/b.js": "let y\n"}


async def test_review_assistant_drafts_and_boss_decides(make_engine):
    engine = make_engine(lambda *a, **k: "x")
    for i, ref in enumerate(("alpha/a-big", "beta/b-big", "gamma/g-big")):
        engine.staff.log_work(ref, "worker", "task", f"output number {i}")
    _, letters = packet(engine.staff.samples())
    a, b, c = list(letters)
    order = []

    async def ask(m, prompt, web=False):
        order.append(m.id)
        assert "WORKER" in prompt and "alpha/a-big" not in prompt  # blind review
        if m.id == "chatgpt":
            return json.dumps({"summary": "draft", "ranking": [{"worker": a, "score": 9, "note": "great"},
                                                               {"worker": b, "score": 5}, {"worker": c, "score": 4}]})
        assert "assistant manager already drafted" in prompt
        return json.dumps({"summary": "final", "ranking": [{"worker": b, "score": 9, "note": "underrated"},
                                                           {"worker": a, "score": 7}, {"worker": c, "score": 2}]})

    r = await run_review(engine, ask=ask, connected=[managers.CLAUDE, managers.CHATGPT])
    assert order == ["chatgpt", "claude"] and r["by"] == "Claude"
    assert r["changes"][letters[b]] > 0 > r["changes"][letters[c]]
    assert engine.staff.verdicts()[letters[b]]["note"] == "underrated"


async def test_review_needs_a_manager(make_engine):
    import pytest

    engine = make_engine(lambda *a, **k: "x")
    with pytest.raises(RuntimeError, match="No manager connected"):
        await run_review(engine, connected=[])
