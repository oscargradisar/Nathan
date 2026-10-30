"""Quota manager tests with a fake clock — no network."""

import pytest

from nathan.errors import QuotaExceeded
from nathan.quota import QuotaManager, parse_duration
from tests.fakes import fake_providers


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def make(tmp_path, spec=None, account=None):
    providers = fake_providers(spec or {"p": {"m": []}})
    if account:
        providers["p"].account_limits = account
    clock = Clock()
    return QuotaManager(providers, tmp_path / "q.sqlite", clock=clock), providers, clock


@pytest.mark.parametrize("text,seconds", [
    ("12", 12), ("1.5", 1.5), ("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m3s", 3723),
    ("250ms", 0.25), ("6m0s", 360),
])
def test_parse_duration(text, seconds):
    assert parse_duration(text) == pytest.approx(seconds)


def test_parse_duration_garbage():
    assert parse_duration("soon") is None


def test_rpm_limit_blocks_then_frees(tmp_path):
    q, providers, clock = make(tmp_path)
    providers["p"].models["m"].limits.rpm = 2
    q.record("p", "m", 10, {})
    q.record("p", "m", 10, {})
    with pytest.raises(QuotaExceeded):
        q.check("p", "m")
    clock.t += 61
    q.check("p", "m")  # a minute later it's fine


def test_rpd_and_tpd(tmp_path):
    q, providers, clock = make(tmp_path)
    lim = providers["p"].models["m"].limits
    lim.rpd, lim.tpd = 100, 50
    q.record("p", "m", 60, {})
    assert "tokens/day" in q.why_not("p", "m")
    clock.t += 86401
    assert q.available("p", "m")


def test_account_limit_shared_across_models(tmp_path):
    from nathan.config import Limits
    q, _, _ = make(tmp_path, {"p": {"m1": [], "m2": []}}, account=Limits(rpd=2))
    q.record("p", "m1", 1, {})
    q.record("p", "m2", 1, {})
    assert not q.available("p", "m1")
    assert "account" in q.why_not("p", "m2")
    assert q.remaining_today("p", "m1") == 0


def test_headers_zero_remaining_blocks_until_reset(tmp_path):
    q, _, clock = make(tmp_path)
    q.record("p", "m", 5, {"x-ratelimit-remaining-requests": "0",
                           "x-ratelimit-reset-requests": "2m0s",
                           "x-ratelimit-limit-requests": "1000"})
    assert "blocked" in q.why_not("p", "m")
    clock.t += 121
    assert q.available("p", "m")
    assert q.live_value("p", "m", "limit-requests") == 1000


def test_headers_remaining_feed_report(tmp_path):
    q, _, _ = make(tmp_path)
    q.record("p", "m", 5, {"x-ratelimit-remaining-requests-day": "42"})
    assert q.remaining_today("p", "m") == 42
    row = q.report()[0]
    assert row["remaining"] == 42 and row["used_requests"] == 1 and row["status"] == "ok"


def test_block_and_gone(tmp_path):
    q, _, clock = make(tmp_path)
    q.block("p", "m", 30, "429")
    with pytest.raises(QuotaExceeded) as ei:
        q.check("p", "m")
    assert ei.value.retry_after == pytest.approx(30)
    clock.t += 31
    q.mark_gone("p", "m")
    assert q.why_not("p", "m") == "model gone"
    q.unmark_gone("p", "m")
    assert q.available("p", "m")


def test_persists_across_instances(tmp_path):
    q, providers, clock = make(tmp_path)
    providers["p"].models["m"].limits.rpd = 1
    q.record("p", "m", 1, {})
    q2 = QuotaManager(providers, tmp_path / "q.sqlite", clock=clock)
    assert not q2.available("p", "m")


def test_uses_wal_journal(tmp_path):
    q, _, _ = make(tmp_path)
    assert q.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_old_calls_are_pruned_on_startup(tmp_path):
    q, providers, clock = make(tmp_path)
    q.record("p", "m", 1, {})
    clock.t += 3 * 86400
    q.record("p", "m", 1, {})
    q2 = QuotaManager(providers, tmp_path / "q.sqlite", clock=clock)
    assert q2.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 1


def test_snapshot_matches_single_checks(tmp_path):
    from nathan.config import Limits
    q, providers, _ = make(tmp_path, {"p": {"m1": [], "m2": [], "m3": []}},
                               account=Limits(rpd=5))
    providers["p"].models["m1"].limits.rpm = 1
    q.record("p", "m1", 3, {})
    q.record("p", "m2", 4, {"x-ratelimit-remaining-requests": "9"})
    q.block("p", "m3", 30, "429")
    snap = q.snapshot()
    for m in ("m1", "m2", "m3"):
        assert q.why_not("p", m, snap) == q.why_not("p", m)
        assert q.remaining_today("p", m, snap) == q.remaining_today("p", m)
    assert "requests/min" in q.why_not("p", "m1", snap)
    assert q.remaining_today("p", "m2", snap) == 3  # account: 5 a day, 2 used
    assert snap.used("p", None) == (2, 2, 7)


def test_auth_block_covers_provider_until_key_changes(tmp_path):
    from nathan.quota import ACCOUNT, auth_reason
    q, providers, _ = make(tmp_path, {"p": {"m1": [], "m2": []}})
    q.block("p", ACCOUNT, 3600, auth_reason(providers["p"].api_key))
    assert "auth" in q.why_not("p", "m2")
    with pytest.raises(QuotaExceeded) as ei:
        q.check("p", "m1")
    assert ei.value.retry_after == pytest.approx(3600)
    providers["p"].key_override = "a-brand-new-key"
    assert q.available("p", "m1") and q.available("p", "m2")
