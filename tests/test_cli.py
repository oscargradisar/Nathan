"""CLI tests with a scripted client — no network."""

from typer.testing import CliRunner

from nathan import cli
from nathan.errors import QuotaExceeded
from tests.fakes import ScriptedClient, last_user

runner = CliRunner()


def _engine(responder):
    from nathan.engine import Engine
    return Engine(client=ScriptedClient(responder))


def test_ask_four_providers(monkeypatch):
    def responder(ref, messages, **kw):
        return f"answer from {ref}"

    monkeypatch.setattr(cli, "build_engine", lambda: _engine(responder))
    models = ["alpha/a-big", "beta/b-big", "gamma/g-big", "delta/d-big"]
    args = ["ask", "What is 2+2?"]
    for m in models:
        args += ["--model", m]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    for m in models:
        assert f"answer from {m}" in result.output


def test_ask_reports_failure_but_shows_others(monkeypatch):
    def responder(ref, messages, **kw):
        if ref.startswith("beta"):
            return QuotaExceeded("out", provider="beta")
        return "fine"

    monkeypatch.setattr(cli, "build_engine", lambda: _engine(responder))
    result = runner.invoke(cli.app, ["ask", "q", "-m", "alpha/a-big", "-m", "beta/b-big"])
    assert result.exit_code == 0
    assert "QuotaExceeded" in result.output and "fine" in result.output


def test_ask_default_model(monkeypatch):
    monkeypatch.setattr(cli, "build_engine", lambda: _engine(lambda r, m, **k: r))
    result = runner.invoke(cli.app, ["ask", "q"])
    assert result.exit_code == 0
    assert "alpha/a-big" in result.output


def test_ask_vote_shows_raw_and_merged(monkeypatch):
    def responder(ref, messages, **kw):
        if "CANDIDATE A" in last_user(messages):
            return "MERGED ANSWER"
        return f"raw from {ref}"

    monkeypatch.setattr(cli, "build_engine", lambda: _engine(responder))
    result = runner.invoke(cli.app, ["ask", "q", "--mode", "vote"])
    assert result.exit_code == 0, result.output
    assert "MERGED ANSWER" in result.output
    assert result.output.count("raw from") >= 3


def test_quota_command(monkeypatch):
    monkeypatch.setattr(cli, "build_engine", lambda: _engine(lambda r, m, **k: "x"))
    result = runner.invoke(cli.app, ["quota"])
    assert result.exit_code == 0
    assert "alpha/a-big" in result.output
