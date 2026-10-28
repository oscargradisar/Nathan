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

import re
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

from nathan.config import Limits, Provider, data_dir
from nathan.errors import QuotaExceeded

MINUTE = 60.0
DAY = 86400.0
ACCOUNT = "*"  # model name used for account-wide rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (provider TEXT, model TEXT, ts REAL, tokens INTEGER);
CREATE INDEX IF NOT EXISTS calls_idx ON calls (provider, model, ts);
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


class QuotaManager:
    def __init__(self, providers: dict[str, Provider], db_path: Path | str | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.providers = providers
        self.clock = clock
        path = db_path if db_path is not None else data_dir() / "quota.sqlite"
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.executescript(SCHEMA)

    # ---- limits ------------------------------------------------------------------------

    def _limits(self, provider: str, model: str) -> tuple[Limits, Limits]:
        p = self.providers.get(provider)
        if p is None:
            return Limits(), Limits()
        spec = p.models.get(model)
        return (spec.limits if spec else Limits()), p.account_limits

    def _usage(self, provider: str, model: str | None, window: float) -> tuple[int, int]:
        since = self.clock() - window
        if model is None:
            row = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM calls WHERE provider=? AND ts>?",
                (provider, since)).fetchone()
        else:
            row = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens),0) FROM calls "
                "WHERE provider=? AND model=? AND ts>?", (provider, model, since)).fetchone()
        return int(row[0]), int(row[1])

    def why_not(self, provider: str, model: str) -> str | None:
        """Return a reason this model can't be used right now, or None if it can."""
        now = self.clock()
        if self.is_gone(provider, model):
            return "model gone"
        for m in (model, ACCOUNT):
            row = self.db.execute("SELECT until, reason FROM blocks WHERE provider=? AND model=?",
                                  (provider, m)).fetchone()
            if row and row[0] > now:
                return f"blocked {row[0] - now:.0f}s more ({row[1]})"
        model_lim, acct_lim = self._limits(provider, model)
        for lim, m in ((model_lim, model), (acct_lim, None)):
            scope = "account" if m is None else "model"
            if lim.rpm is not None and self._usage(provider, m, MINUTE)[0] >= lim.rpm:
                return f"{scope} requests/min used up"
            if lim.rpd is not None or lim.tpd is not None:
                reqs, toks = self._usage(provider, m, DAY)
                if lim.rpd is not None and reqs >= lim.rpd:
                    return f"{scope} requests/day used up"
                if lim.tpd is not None and toks >= lim.tpd:
                    return f"{scope} tokens/day used up"
        return None

    def available(self, provider: str, model: str) -> bool:
        return self.why_not(provider, model) is None

    def check(self, provider: str, model: str) -> None:
        reason = self.why_not(provider, model)
        if reason:
            raise QuotaExceeded(f"{provider}/{model}: {reason}", provider=provider, model=model,
                                retry_after=self._block_left(provider, model))

    def _block_left(self, provider: str, model: str) -> float | None:
        row = self.db.execute(
            "SELECT MAX(until) FROM blocks WHERE provider=? AND model IN (?, ?)",
            (provider, model, ACCOUNT)).fetchone()
        if row and row[0]:
            left = row[0] - self.clock()
            return left if left > 0 else None
        return None

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

    def remaining_today(self, provider: str, model: str) -> int | None:
        """Best guess of requests left in the next 24h for this model (None = unknown/unlimited)."""
        model_lim, acct_lim = self._limits(provider, model)
        guesses: list[float] = []
        if model_lim.rpd is not None:
            guesses.append(model_lim.rpd - self._usage(provider, model, DAY)[0])
        if acct_lim.rpd is not None:
            guesses.append(acct_lim.rpd - self._usage(provider, None, DAY)[0])
        for name in ("remaining-requests-day", "remaining-requests"):
            v = self.live_value(provider, model, name)
            if v is not None:
                guesses.append(v)
                break
        return max(0, int(min(guesses))) if guesses else None

    def report(self) -> list[dict]:
        """One row per configured model: used today, remaining, status. For the dashboard."""
        rows = []
        for p in self.providers.values():
            for model, spec in p.models.items():
                reqs, toks = self._usage(p.name, model, DAY)
                rows.append({
                    "provider": p.name, "model": model, "ref": p.ref(model),
                    "has_key": bool(p.api_key),
                    "used_requests": reqs, "used_tokens": toks,
                    "limit_rpd": spec.limits.rpd or p.account_limits.rpd,
                    "remaining": self.remaining_today(p.name, model),
                    "status": self.why_not(p.name, model) or "ok",
                })
        return rows
