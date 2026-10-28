"""Pattern tests: fallback, race, vote, roster — scripted models, no network."""

import asyncio

import pytest

from nathan.errors import NoModelAvailable, ProviderDown, QuotaExceeded
from nathan.patterns import Candidate, fallback, race, vote
from nathan.roster import Roster
from tests.fakes import ScriptedClient, fake_providers, last_user

MSG = [{"role": "user", "content": "q"}]


async def test_fallback_skips_failures():
    def responder(ref, messages, **kw):
        if ref == "alpha/a-big":
            return QuotaExceeded("out", provider="alpha")
        if ref == "beta/b-big":
            return ProviderDown("down", provider="beta")
        return f"answer {ref}"

    c = ScriptedClient(responder)
    res = await fallback(c, ["alpha/a-big", "beta/b-big", "gamma/g-big"], MSG)
    assert res.ref == "gamma/g-big"


async def test_fallback_all_fail():
    c = ScriptedClient(lambda r, m, **k: ProviderDown("x"))
    with pytest.raises(NoModelAvailable):
        await fallback(c, ["alpha/a-big"], MSG)


async def test_race_first_valid_wins_and_cancels_slow():
    cancelled = []

    async def responder(ref, messages, **kw):
        if ref == "alpha/a-big":
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(ref)
                raise
        if ref == "beta/b-big":
            return ""  # invalid (empty) — must not win
        await asyncio.sleep(0.01)
        return "fast answer"

    c = ScriptedClient(responder)
    res = await race(c, ["alpha/a-big", "beta/b-big", "gamma/g-big"], MSG)
    assert res.text == "fast answer" and res.ref == "gamma/g-big"
    assert cancelled == ["alpha/a-big"]


async def test_race_all_fail():
    c = ScriptedClient(lambda r, m, **k: QuotaExceeded("x"))
    with pytest.raises(NoModelAvailable):
        await race(c, ["alpha/a-big", "beta/b-big"], MSG)


async def test_vote_merges_with_judge_and_replaces_failed_lane():
    def responder(ref, messages, **kw):
        if "CANDIDATE" in last_user(messages):
            assert "CANDIDATE C" in last_user(messages)  # three answers reached the judge
            return "merged"
        if ref == "beta/b-big":
            return ProviderDown("down")
        return f"raw {ref}"

    c = ScriptedClient(responder)
    result = await vote(c, ["alpha/a-big", "beta/b-big", "gamma/g-big"], MSG,
                        judges=["alpha/a-big"], spares=["delta/d-big"])
    assert result.answer == "merged" and result.judge == "alpha/a-big"
    refs = [cand.ref for cand in result.candidates if cand.ok]
    assert sorted(refs) == ["alpha/a-big", "delta/d-big", "gamma/g-big"]


async def test_vote_judge_down_falls_back_to_longest():
    def responder(ref, messages, **kw):
        if "CANDIDATE" in last_user(messages):
            return QuotaExceeded("judge out")
        return "short" if ref == "alpha/a-big" else "a much longer answer"

    c = ScriptedClient(responder)
    result = await vote(c, ["alpha/a-big", "beta/b-big"], MSG, judges=["gamma/g-big"])
    assert result.answer == "a much longer answer" and result.judge is None
    assert "judge unavailable" in result.note


async def test_vote_includes_manual_candidate():
    seen = {}

    def responder(ref, messages, **kw):
        if "CANDIDATE" in last_user(messages):
            seen["prompt"] = last_user(messages)
            return "merged"
        return "auto"

    c = ScriptedClient(responder)
    await vote(c, ["alpha/a-big"], MSG, judges=["gamma/g-big"],
               extra=[Candidate("manual/claude.ai", "pasted answer")])
    assert "pasted answer" in seen["prompt"]


def test_roster_picks_distinct_providers_and_skips_missing_keys():
    providers = fake_providers()
    providers["beta"].key_override = None  # one provider's key removed
    roster = Roster(providers)
    picked = roster.pick(["writing"], 4)
    assert all(not r.startswith("beta/") for r in picked)
    assert len({r.split("/")[0] for r in picked}) == len(picked) == 3
    assert roster.pick_one(["judge"]) in ("alpha/a-big", "gamma/g-big")


async def test_engine_vote_works_with_one_key_removed(make_engine):
    providers = fake_providers()
    providers["gamma"].key_override = None

    def responder(ref, messages, **kw):
        assert not ref.startswith("gamma/")
        return "merged" if "CANDIDATE" in last_user(messages) else f"raw {ref}"

    engine = make_engine(responder, providers)
    ans = await engine.ask("q", mode="vote")
    assert ans.text == "merged"
    assert len(ans.candidates) >= 2 and all(c.ok for c in ans.candidates)


async def test_engine_single_falls_back_when_quota_tracker_says_no(make_engine):
    engine = make_engine(lambda r, m, **k: f"from {r}")
    engine.quota.block("alpha", "a-big", 600, "test")
    ans = await engine.ask("q", models=["alpha/a-big"])
    assert ans.models != ["alpha/a-big"] and ans.text.startswith("from ")


async def test_engine_race(make_engine):
    engine = make_engine(lambda r, m, **k: f"from {r}")
    ans = await engine.ask("q", mode="race")
    assert ans.text.startswith("from ")
