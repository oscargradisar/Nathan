"""Tier 2: track how much of each free tier we've used, in SQLite.

Why track it ourselves? Free tiers punish you for hitting the wall (some ban the key
for minutes after a 429 burst). Counting our own requests lets Nathan skip a model
*before* it would fail, and pick one with room left instead.

Three sources of truth, strictest wins:
1. Published limits from providers.yaml (rpm / rpd / tpd per model, plus account limits
   such as OpenRouter's 50 requests/day for the whole account).
2. Live limits the provider reports in `x-ratelimit-*` response headers.
3. Blocks we set after a 429 ("don't touch this model for N seconds").

Windows are rolling: "per day" means "in the last 24 hours". Providers reset at
different wall-clock times, and a rolling window is the safe approximation.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from nathan.config import Limits, Provider, data_dir
from nathan.errors import QuotaExceeded

MINUTE = 60.0
DAY = 86400.0
KEEP_CALLS_S = 2 * DAY  # older call rows can't affect any window, so they're deleted
ACCOUNT = "*"  # model name used for account-wide rows
AUTH_BLOCK_S = 3600.0  # a rejected key (401/403) benches its whole provider this long

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (provider TEXT, model TEXT, ts REAL, tokens INTEGER);
CREATE INDEX IF NOT EXISTS calls_idx ON calls (provider, model, ts);
CREATE INDEX IF NOT EXISTS calls_ts ON calls (ts);
CREATE TABLE IF NOT EXISTS blocks (provider TEXT, model TEXT, until REAL, reason TEXT,
                                   PRIMARY KEY (provider, model));
CREATE TABLE IF NOT EXISTS live (provider TEXT, model TEXT, name TEXT, value REAL, updated REAL,
                                 PRIMARY KEY (provider, model, name));
CREATE TABLE IF NOT EXISTS gone (provider TEXT, model TEXT, ts REAL, PRIMARY KEY (provider, model));
"""

_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?"
                       r"(?:(\d+(?:\.\d+)?)ms)?$")


def parse_duration(value: str) -> float | None:
    """Parse reset headers: "12", "1.5", "7.66s", "2m59.56s", "1h2m3s", "250ms"."""
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    m = _DURATION.match(value)
    if not m or not any(m.groups()):
        return None
    h, mins, s, ms = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mins * 60 + s + ms / 1000


def auth_reason(api_key: str | None) -> str:
    """Block reason for a rejected key. It carries a short fingerprint of that key (never
    the key itself), so the block stops applying as soon as the owner saves a new key."""
    digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:8]
    return f"auth {digest}"


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite file tuned for many small writes from async code.

    WAL (write-ahead log) lets the web app read while another write is in progress, and
    synchronous=NORMAL skips the disk flush on every commit. Each write drops from ~5 ms
    to well under 1 ms, which matters because these writes run on the event loop. The
    worst a power cut can do is lose the last few counters, never corrupt the file.
    """
    db = sqlite3.connect(str(path), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    return db


@dataclass
class Snapshot:
    """Everything why_not/remaining_today need, read in four queries.

    Picking models checks ~30 of them at once; asking SQLite per model meant ~250 queries.
    One snapshot answers all of them from memory.
    """

    now: float
    blocks: dict[tuple[str, str], tuple[float, str]] = field(default_factory=dict)
    gone: set[tuple[str, str]] = field(default_factory=set)
    live: dict[tuple[str, str, str], float] = field(default_factory=dict)
    # (provider, model) -> (requests last minute, requests last day, tokens last day)
    usage: dict[tuple[str, str], tuple[int, int, int]] = field(default_factory=dict)

    def used(self, provider: str, model: str | None) -> tuple[int, int, int]:
        """(requests/min, requests/day, tokens/day) for one model, or the whole provider."""
        if model is not None:
            return self.usage.get((provider, model), (0, 0, 0))
        rows = [u for (p, _), u in self.usage.items() if p == provider]
        return (sum(r[0] for r in rows), sum(r[1] for r in rows), sum(r[2] for r in rows))


class QuotaManager:
    def __init__(self, providers: dict[str, Provider], db_path: Path | str | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.providers = providers
        self.clock = clock
        path = db_path if db_path is not None else data_dir() / "quota.sqlite"
        self.db = open_db(path)
        self.db.executescript(SCHEMA)
        # One row per request piles up forever otherwise, and every query gets slower
        self.db.execute("DELETE FROM calls WHERE ts < ?", (self.clock() - KEEP_CALLS_S,))
        self.db.commit()

    # ---- limits ------------------------------------------------------------------------

    def _limits(self, provider: str, model: str) -> tuple[Limits, Limits]:
        p = self.providers.get(provider)
        if p is None:
            return Limits(), Limits()
        spec = p.models.get(model)
        return (spec.limits if spec else Limits()), p.account_limits

    def snapshot(self, provider: str | None = None) -> Snapshot:
        """Load blocks, gone models, live header values and usage counts in one go.
        With `provider`, only that provider's rows are read (cheaper for a single check)."""
        now = self.clock()
        only, args = (" AND provider = ?", (provider,)) if provider else ("", ())
        snap = Snapshot(now)
        for p, m, until, reason in self.db.execute(
                "SELECT provider, model, until, reason FROM blocks WHERE until > ?" + only,
                (now, *args)):
            snap.blocks[(p, m)] = (until, reason)
        snap.gone = {(p, m) for p, m in self.db.execute(
            "SELECT provider, model FROM gone WHERE 1" + only, args)}
        for p, m, name, value in self.db.execute(
                "SELECT provider, model, name, value FROM live WHERE 1" + only, args):
            snap.live[(p, m, name)] = value
        for p, m, day_reqs, day_toks, minute_reqs in self.db.execute(
                "SELECT provider, model, COUNT(*), COALESCE(SUM(tokens), 0), SUM(ts > ?) "
                "FROM calls WHERE ts > ?" + only + " GROUP BY provider, model",
                (now - MINUTE, now - DAY, *args)):
            snap.usage[(p, m)] = (int(minute_reqs or 0), int(day_reqs), int(day_toks))
        return snap

    def _active_block(self, snap: Snapshot, provider: str, model: str) -> tuple[float, str] | None:
        block = snap.blocks.get((provider, model))
        if block is None:
            return None
        reason = block[1]
        p = self.providers.get(provider)
        if reason.startswith("auth ") and p is not None and reason != auth_reason(p.api_key):
            return None  # the key that was rejected has since been replaced
        return block

    def why_not(self, provider: str, model: str, snap: Snapshot | None = None) -> str | None:
        """Return a reason this model can't be used right now, or None if it can."""
        snap = snap or self.snapshot(provider)
        if (provider, model) in snap.gone:
            return "model gone"
        for m in (model, ACCOUNT):
            block = self._active_block(snap, provider, m)
            if block:
                return f"blocked {block[0] - snap.now:.0f}s more ({block[1]})"
        model_lim, acct_lim = self._limits(provider, model)
        for lim, m in ((model_lim, model), (acct_lim, None)):
            scope = "account" if m is None else "model"
            per_min, reqs, toks = snap.used(provider, m)
            if lim.rpm is not None and per_min >= lim.rpm:
                return f"{scope} requests/min used up"
            if lim.rpd is not None and reqs >= lim.rpd:
                return f"{scope} requests/day used up"
            if lim.tpd is not None and toks >= lim.tpd:
                return f"{scope} tokens/day used up"
        return None

    def available(self, provider: str, model: str, snap: Snapshot | None = None) -> bool:
        return self.why_not(provider, model, snap) is None

    def check(self, provider: str, model: str) -> None:
        snap = self.snapshot(provider)
        reason = self.why_not(provider, model, snap)
        if reason:
            raise QuotaExceeded(f"{provider}/{model}: {reason}", provider=provider, model=model,
                                retry_after=self._block_left(provider, model, snap))

    def _block_left(self, provider: str, model: str, snap: Snapshot) -> float | None:
        untils = [b[0] for m in (model, ACCOUNT) if (b := self._active_block(snap, provider, m))]
        return max(untils) - snap.now if untils else None

    # ---- recording ---------------------------------------------------------------------

    def record(self, provider: str, model: str, tokens: int, headers: dict[str, str]) -> None:
        now = self.clock()
        self.db.execute("INSERT INTO calls VALUES (?,?,?,?)", (provider, model, now, tokens))
        self._read_headers(provider, model, headers, now)
        self.db.commit()

    def _read_headers(self, provider: str, model: str, headers: dict[str, str], now: float) -> None:
        """Store x-ratelimit-remaining-* values; block the model if one hits zero."""
        for name, value in headers.items():
            name = name.lower()
            if not name.startswith("x-ratelimit-remaining-"):
                continue
            suffix = name.removeprefix("x-ratelimit-remaining-")  # e.g. "requests", "tokens-minute"
            try:
                remaining = float(value)
            except ValueError:
                continue
            self.db.execute("INSERT OR REPLACE INTO live VALUES (?,?,?,?,?)",
                            (provider, model, f"remaining-{suffix}", remaining, now))
            limit = headers.get(f"x-ratelimit-limit-{suffix}")
            if limit is not None:
                try:
                    self.db.execute("INSERT OR REPLACE INTO live VALUES (?,?,?,?,?)",
                                    (provider, model, f"limit-{suffix}", float(limit), now))
                except ValueError:
                    pass
            if remaining <= 0:
                reset = parse_duration(headers.get(f"x-ratelimit-reset-{suffix}", "")) or MINUTE
                self.block(provider, model, reset, f"header {suffix} = 0", commit=False)

    def block(self, provider: str, model: str, seconds: float, reason: str,
              commit: bool = True) -> None:
        self.db.execute("INSERT OR REPLACE INTO blocks VALUES (?,?,?,?)",
                        (provider, model, self.clock() + seconds, reason))
        if commit:
            self.db.commit()

    def mark_gone(self, provider: str, model: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO gone VALUES (?,?,?)", (provider, model, self.clock()))
        self.db.commit()

    def unmark_gone(self, provider: str, model: str) -> None:
        self.db.execute("DELETE FROM gone WHERE provider=? AND model=?", (provider, model))
        self.db.commit()

    def is_gone(self, provider: str, model: str) -> bool:
        return self.db.execute("SELECT 1 FROM gone WHERE provider=? AND model=?",
                               (provider, model)).fetchone() is not None

    # ---- reporting ---------------------------------------------------------------------

    def live_value(self, provider: str, model: str, name: str) -> float | None:
        row = self.db.execute("SELECT value FROM live WHERE provider=? AND model=? AND name=?",
                              (provider, model, name)).fetchone()
        return row[0] if row else None

    def remaining_today(self, provider: str, model: str,
                        snap: Snapshot | None = None) -> int | None:
        """Best guess of requests left in the next 24h for this model (None = unknown/unlimited)."""
        snap = snap or self.snapshot(provider)
        model_lim, acct_lim = self._limits(provider, model)
        guesses: list[float] = []
        if model_lim.rpd is not None:
            guesses.append(model_lim.rpd - snap.used(provider, model)[1])
        if acct_lim.rpd is not None:
            guesses.append(acct_lim.rpd - snap.used(provider, None)[1])
        for name in ("remaining-requests-day", "remaining-requests"):
            v = snap.live.get((provider, model, name))
            if v is not None:
                guesses.append(v)
                break
        return max(0, int(min(guesses))) if guesses else None

    def report(self) -> list[dict]:
        """One row per configured model: used today, remaining, status. For the dashboard."""
        snap = self.snapshot()
        rows = []
        for p in self.providers.values():
            for model, spec in p.models.items():
                _, reqs, toks = snap.used(p.name, model)
                rows.append({
                    "provider": p.name, "model": model, "ref": p.ref(model),
                    "has_key": bool(p.api_key),
                    "used_requests": reqs, "used_tokens": toks,
                    "limit_rpd": spec.limits.rpd or p.account_limits.rpd,
                    "remaining": self.remaining_today(p.name, model, snap),
                    "status": self.why_not(p.name, model, snap) or "ok",
                })
        return rows
