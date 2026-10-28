"""Tier 4: memory, tools, agent loop — scripted models, temporary folders, no network."""

import json
from pathlib import Path

import httpx2 as httpx
import pytest

from nathan.agent import parse_text_call, run_agent
from nathan.client import ChatResult
from nathan.errors import BadRequest, ProviderDown
from nathan.memory import Memory, fts_query
from nathan.tools import default_toolbox
from nathan.tools.fetch import fetch_url, html_to_text
from nathan.tools.files import safe_path
from nathan.tools.python_exec import run_python
from tests.fakes import ScriptedClient, last_user, system_text

# ---- memory ---------------------------------------------------------------------------


def test_fts_query_sanitizes():
    assert fts_query('What is my cat\'s "name"?') == '"cat\'s" OR "name"'
    assert fts_query("the of a") == ""


def test_memory_recall_across_sessions(tmp_path):
    db = tmp_path / "m.sqlite"
    first = Memory(db, session="one")
    first.remember("My cat is called Miso and she is 4 years old.")
    first.log_exchange("Where do I work?", "You work at the Ljubljana bakery.")
    second = Memory(db, session="two")  # a new session, same file
    hits = second.search("what is my cat called")
    assert hits and "Miso" in hits[0].content
    assert "bakery" in second.context_for("where do I work")
    assert second.context_for("quantum chromodynamics") == ""


def test_memory_search_handles_odd_input(tmp_path):
    m = Memory(tmp_path / "m.sqlite")
    m.remember("NEAR AND OR are FTS keywords")
    m.search('NEAR( "unbalanced')  # must not raise
    assert m.search("") == []


# ---- tools ----------------------------------------------------------------------------


def test_safe_path_blocks_escape(tmp_path):
    with pytest.raises(PermissionError):
        safe_path(tmp_path, "../secret")
    with pytest.raises(PermissionError):
        safe_path(tmp_path, "/etc/passwd")
    assert safe_path(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()


async def test_file_tools_roundtrip(tmp_path):
    box = default_toolbox(workspace=tmp_path)
    assert "wrote" in await box.call("write_file", {"path": "notes/a.txt", "content": "hello\nworld"})
    assert "notes/a.txt" in await box.call("list_files", {})
    assert await box.call("read_file", '{"path": "notes/a.txt"}') == "hello\nworld"
    assert "notes/a.txt:2" in await box.call("search_files", {"query": "WORLD"})
    assert "outside the workspace" in await box.call("read_file", {"path": "../../.env"})
    assert "unknown tool" in await box.call("rm_rf", {})
    assert "bad arguments" in await box.call("read_file", "{not json")


async def test_run_python_output_and_timeout(tmp_path):
    out = await run_python("print(6*7)", tmp_path)
    assert "exit code 0" in out and "42" in out
    assert "timed out" in await run_python("while True: pass", tmp_path, timeout=0.5)


async def test_run_python_has_no_api_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
    out = await run_python("import os; print(os.environ.get('GROQ_API_KEY'))", tmp_path)
    assert "gsk_secret" not in out


async def test_fetch_url_with_mock_transport():
    def handler(req):
        return httpx.Response(200, text="<html><script>x()</script><h1>Title</h1><p>Body &amp; more</p></html>",
                              headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await fetch_url("https://example.test/page", client)
    assert "Title" in out and "Body & more" in out and "x()" not in out
    assert "only http" in await fetch_url("file:///etc/passwd")


def test_html_to_text():
    assert html_to_text("<p>a</p><p>b</p>").split() == ["a", "b"]


# ---- agent ----------------------------------------------------------------------------


def test_parse_text_call():
    assert parse_text_call('ok\n```tool\n{"name": "read_file", "arguments": {"path": "x"}}\n```') == (
        "read_file", {"path": "x"})
    assert parse_text_call("just an answer") is None


async def test_agent_native_tools(tmp_path):
    (tmp_path / "report.txt").write_text("Revenue in 2025 was 1.2 million euros.")
    box = default_toolbox(workspace=tmp_path)

    def responder(ref, messages, **kw):
        assert kw.get("tools") or messages[-1]["role"] == "user"
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        if not tool_msgs:
            return ChatResult("", "alpha", "a-big",
                              tool_calls=[{"id": "c1", "name": "read_file",
                                           "arguments": json.dumps({"path": "report.txt"})}])
        assert "1.2 million" in tool_msgs[-1]["content"]
        return "Revenue was 1.2 million euros."

    c = ScriptedClient(responder)
    r = await run_agent(c, ["alpha/a-big"], "What was revenue in 2025?", box)
    assert r.answer == "Revenue was 1.2 million euros."
    assert [s.name for s in r.tool_calls] == ["read_file"]


async def test_agent_switches_to_text_mode_on_bad_request(tmp_path):
    (tmp_path / "a.txt").write_text("the password hint is 'blue'")
    box = default_toolbox(workspace=tmp_path)

    def responder(ref, messages, **kw):
        if kw.get("tools"):
            return BadRequest("tools not supported")
        assert "```tool" in system_text(messages)
        if "TOOL RESULT" in last_user(messages):
            return "The hint is blue."
        return '```tool\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```'

    r = await run_agent(ScriptedClient(responder), ["alpha/a-big"], "what's the hint?", box)
    assert r.answer == "The hint is blue." and len(r.tool_calls) == 1


async def test_agent_hands_over_to_next_model_mid_task(tmp_path):
    (tmp_path / "a.txt").write_text("42")
    box = default_toolbox(workspace=tmp_path)

    def responder(ref, messages, **kw):
        if ref == "alpha/a-big":
            if any(m["role"] == "tool" for m in messages):
                return ProviderDown("died after first step")
            return ChatResult("", "alpha", "a-big", tool_calls=[
                {"id": "c1", "name": "read_file", "arguments": '{"path": "a.txt"}'}])
        assert any(m["role"] == "tool" and m["content"] == "42" for m in messages)
        return "The file says 42."

    r = await run_agent(ScriptedClient(responder), ["alpha/a-big", "beta/b-big"], "read a.txt", box)
    assert r.answer == "The file says 42." and r.models == ["alpha/a-big", "beta/b-big"]


async def test_agent_step_limit(tmp_path):
    box = default_toolbox(workspace=tmp_path)

    def responder(ref, messages, **kw):
        if not kw.get("tools"):
            return "best effort answer"
        return ChatResult("", "alpha", "a-big", tool_calls=[
            {"id": "", "name": "list_files", "arguments": "{}"}])

    c = ScriptedClient(responder)
    r = await run_agent(c, ["alpha/a-big"], "loop forever", box, max_steps=3)
    assert r.hit_limit and r.answer == "best effort answer"
    assert len(r.tool_calls) == 3 and len(c.calls) == 4


# ---- gate simulation ---------------------------------------------------------------------


async def test_gate_folder_questions_and_cross_session_recall(tmp_path, make_engine):
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True)
    (ws / "docs/meeting.md").write_text("Decision: launch date is 14 March.")
    (ws / "docs/budget.md").write_text("Budget: 3000 EUR.")

    def responder(ref, messages, **kw):
        sys = system_text(messages)
        if "route tasks" in sys:
            return '{"task_type": "analysis", "pattern": "agent"}'
        tool_results = [m["content"] for m in messages if m["role"] == "tool"]
        if kw.get("tools") and not tool_results:
            return ChatResult("", *ref.split("/", 1), tool_calls=[
                {"id": "1", "name": "search_files", "arguments": '{"query": "launch"}'}])
        if tool_results:
            return "The launch date is 14 March (docs/meeting.md)."
        # plain question: answer from memory context if present
        if "Miso" in sys:
            return "Your cat is Miso."
        return "I don't know."

    engine1 = make_engine(responder)
    engine1.workspace = ws
    ans = await engine1.ask("When is the launch according to the files in my workspace?")
    assert ans.mode == "agent" and "14 March" in ans.text
    engine1.memory.remember("My cat is called Miso.")

    # "Next session": a brand-new engine over the same data folder
    engine2 = make_engine(responder)
    engine2.memory = Memory(Path(engine1.data) / "memory.sqlite", session="later")
    ans2 = await engine2.ask("What is my cat called?", mode="single")
    assert ans2.text == "Your cat is Miso."
