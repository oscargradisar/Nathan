"""The staff ledger: how much work each model has done and how good it was, as an Elo rating.

Elo is the rating system from chess and ranked video games. Everyone starts at 1000.
After each workflow the reviewer scores every worker's part out of 10, and each pair of
workers in that job plays a "match": the higher score wins. Beating a higher-rated
model earns more points than beating a lower-rated one. A worker that didn't show up
(timed out, quota gone) scores 0 and loses its matches.
"""

from __future__ import annotations

import itertools
import time
from pathlib import Path

from nathan.quota import open_db

START = 1000.0
K = 24.0  # how far one match moves a rating
SLOW_AFTER_S = 10.0  # average answer times beyond this cost priority...
SLOW_COST = 3.0      # ...this many Elo points per extra second
GHOST_COST = 400.0   # models that don't deliver go to the back of the line
ROWS_TTL_S = 2.0     # how long one read of the staff table is reused (see Staff._row)
RANKS = [(1400, "Champion"), (1300, "Diamond"), (1200, "Platinum"), (1100, "Gold"),
         (1000, "Silver"), (900, "Bronze"), (float("-inf"), "Iron")]


def rank_name(elo: float) -> str:
    return next(name for floor, name in RANKS if elo >= floor)


def expected(a: float, b: float) -> float:
    """Chance that a rating of `a` beats `b`."""
    return 1 / (1 + 10 ** ((b - a) / 400))


class Staff:
    def __init__(self, path: Path | str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = open_db(path)
        self._rows: dict[str, tuple] | None = None  # see _row()
        self._rows_at = 0.0
        self.db.execute("""CREATE TABLE IF NOT EXISTS staff (
            ref TEXT PRIMARY KEY, elo REAL NOT NULL, jobs INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0, no_shows INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
            rated INTEGER NOT NULL DEFAULT 0, score_sum REAL NOT NULL DEFAULT 0,
            ms_sum REAL NOT NULL DEFAULT 0)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS worklog (
            id INTEGER PRIMARY KEY, ts REAL, ref TEXT, role TEXT, task TEXT, output TEXT)""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS verdicts (
            ref TEXT PRIMARY KEY, ts REAL, score REAL, note TEXT, by TEXT)""")
        self.db.commit()

    def _ensure(self, ref: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO staff (ref, elo) VALUES (?, ?)", (ref, START))

    def elo(self, ref: str) -> float:
        row = self.db.execute("SELECT elo FROM staff WHERE ref = ?", (ref,)).fetchone()
        return row[0] if row else START

    def _row(self, ref: str) -> tuple | None:
        """(elo, done, no_shows, ms_sum) for one model, from a table read kept for a moment.
        Picking models asks priority() and seconds() for every model several times per
        request; one read of the small staff table answers them all. Our own writes reset
        it, and the short expiry picks up writes from other Nathan processes."""
        now = time.monotonic()
        if self._rows is None or now - self._rows_at > ROWS_TTL_S:
            self._rows = {r[0]: r[1:] for r in self.db.execute(
                "SELECT ref, elo, done, no_shows, ms_sum FROM staff")}
            self._rows_at = now
        return self._rows.get(ref)

    def _changed(self) -> None:
        self.db.commit()
        self._rows = None

    def priority(self, ref: str) -> float:
        """Who gets picked first: Elo, minus a cost for being slow or for not showing up.
        Quality alone made the slowest good model (36s a job) everyone's first pick."""
        row = self._row(ref)
        if row is None:
            return START
        elo, done, no_shows, ms_sum = row
        cost = 0.0
        if done:
            cost += SLOW_COST * max(0.0, ms_sum / done / 1000 - SLOW_AFTER_S)
        if no_shows and (done == 0 or (no_shows >= 3 and no_shows > done)):
            cost += GHOST_COST  # never or rarely delivers: last resort only
        return elo - cost

    def seconds(self, ref: str) -> float | None:
        """Average answer time in seconds, or None if this model hasn't delivered yet."""
        row = self._row(ref)
        return row[3] / row[1] / 1000 if row and row[1] else None

    def record(self, ref: str, *, ok: bool, ms: float = 0.0) -> None:
        """One assignment: the worker either delivered or was a no-show."""
        self._ensure(ref)
        self.db.execute("UPDATE staff SET jobs = jobs + 1, done = done + ?, no_shows = no_shows + ?,"
                        " ms_sum = ms_sum + ? WHERE ref = ?", (int(ok), int(not ok), ms, ref))
        self._changed()

    def log_work(self, ref: str, role: str, task: str, output: str) -> None:
        """Keep a sample of delivered work so managers can review real output later."""
        self.db.execute("INSERT INTO worklog (ts, ref, role, task, output) VALUES (?,?,?,?,?)",
                        (time.time(), ref, role, task[:1500], output[:3000]))
        self.db.execute("DELETE FROM worklog WHERE id <= (SELECT MAX(id) FROM worklog) - 400")
        self.db.commit()

    def samples(self, per_model: int = 2, models: int = 12) -> dict[str, list[dict]]:
        """Most recent work per model, most active models first."""
        out: dict[str, list[dict]] = {}
        for ref, role, task, output in self.db.execute(
                "SELECT ref, role, task, output FROM worklog ORDER BY id DESC"):
            if ref not in out and len(out) >= models:
                continue
            bucket = out.setdefault(ref, [])
            if len(bucket) < per_model:
                bucket.append({"role": role, "task": task, "output": output})
        return out

    def set_verdict(self, ref: str, score: float, note: str, by: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?)",
                        (ref, time.time(), score, note[:400], by))
        self.db.commit()

    def verdicts(self) -> dict[str, dict]:
        return {r[0]: {"ts": r[1], "score": r[2], "note": r[3], "by": r[4]}
                for r in self.db.execute("SELECT ref, ts, score, note, by FROM verdicts")}

    def rate(self, scores: dict[str, float], k: float = K) -> dict[str, float]:
        """Apply one job's review scores (0-10 per worker). Returns each worker's Elo change."""
        for ref in scores:
            self._ensure(ref)
        before = {ref: self.elo(ref) for ref in scores}
        delta = dict.fromkeys(scores, 0.0)
        # Big jobs mean many matches each: scale so a job moves ratings about as much as a
        # three-worker one, instead of swinging them by 100+ points
        k = k * 2 / max(2, len(scores) - 1)
        for a, b in itertools.combinations(scores, 2):
            if scores[a] == scores[b]:
                result = 0.5
            else:
                result = 1.0 if scores[a] > scores[b] else 0.0
            change = k * (result - expected(before[a], before[b]))
            delta[a] += change
            delta[b] -= change
        for ref, score in scores.items():
            won = sum(score > scores[o] for o in scores if o != ref)
            lost = sum(score < scores[o] for o in scores if o != ref)
            self.db.execute("UPDATE staff SET elo = elo + ?, wins = wins + ?, losses = losses + ?,"
                            " rated = rated + 1, score_sum = score_sum + ? WHERE ref = ?",
                            (delta[ref], won, lost, score, ref))
        self._changed()
        return delta

    def board(self) -> list[dict]:
        verdicts = self.verdicts()
        rows = self.db.execute("SELECT ref, elo, jobs, done, no_shows, wins, losses, rated, score_sum,"
                               " ms_sum FROM staff ORDER BY elo DESC, done DESC").fetchall()
        out = []
        for i, (ref, elo, jobs, done, no_shows, wins, losses, rated, score_sum, ms_sum) in enumerate(rows):
            out.append({"place": i + 1, "ref": ref, "elo": round(elo), "rank": rank_name(elo),
                        "jobs": jobs, "done": done, "no_shows": no_shows, "wins": wins,
                        "losses": losses,
                        "avg_score": round(score_sum / rated, 1) if rated else None,
                        "avg_seconds": round(ms_sum / done / 1000, 1) if done else None,
                        "priority": round(self.priority(ref)),
                        "verdict": verdicts.get(ref)})
        return out
