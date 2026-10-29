"""The staff ledger: how much work each model has done and how good it was, as an Elo rating.

Elo is the rating system from chess and ranked video games. Everyone starts at 1000.
After each workflow the reviewer scores every worker's part out of 10, and each pair of
workers in that job plays a "match": the higher score wins. Beating a higher-rated
model earns more points than beating a lower-rated one. A worker that didn't show up
(timed out, quota gone) scores 0 and loses its matches.
"""

from __future__ import annotations

import itertools
import sqlite3
from pathlib import Path

START = 1000.0
K = 24.0  # how far one match moves a rating
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
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("""CREATE TABLE IF NOT EXISTS staff (
            ref TEXT PRIMARY KEY, elo REAL NOT NULL, jobs INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0, no_shows INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0,
            rated INTEGER NOT NULL DEFAULT 0, score_sum REAL NOT NULL DEFAULT 0,
            ms_sum REAL NOT NULL DEFAULT 0)""")
        self.db.commit()

    def _ensure(self, ref: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO staff (ref, elo) VALUES (?, ?)", (ref, START))

    def elo(self, ref: str) -> float:
        row = self.db.execute("SELECT elo FROM staff WHERE ref = ?", (ref,)).fetchone()
        return row[0] if row else START

    def record(self, ref: str, *, ok: bool, ms: float = 0.0) -> None:
        """One assignment: the worker either delivered or was a no-show."""
        self._ensure(ref)
        self.db.execute("UPDATE staff SET jobs = jobs + 1, done = done + ?, no_shows = no_shows + ?,"
                        " ms_sum = ms_sum + ? WHERE ref = ?", (int(ok), int(not ok), ms, ref))
        self.db.commit()

    def rate(self, scores: dict[str, float]) -> dict[str, float]:
        """Apply one job's review scores (0-10 per worker). Returns each worker's Elo change."""
        for ref in scores:
            self._ensure(ref)
        before = {ref: self.elo(ref) for ref in scores}
        delta = dict.fromkeys(scores, 0.0)
        for a, b in itertools.combinations(scores, 2):
            if scores[a] == scores[b]:
                result = 0.5
            else:
                result = 1.0 if scores[a] > scores[b] else 0.0
            change = K * (result - expected(before[a], before[b]))
            delta[a] += change
            delta[b] -= change
        for ref, score in scores.items():
            won = sum(score > scores[o] for o in scores if o != ref)
            lost = sum(score < scores[o] for o in scores if o != ref)
            self.db.execute("UPDATE staff SET elo = elo + ?, wins = wins + ?, losses = losses + ?,"
                            " rated = rated + 1, score_sum = score_sum + ? WHERE ref = ?",
                            (delta[ref], won, lost, score, ref))
        self.db.commit()
        return delta

    def board(self) -> list[dict]:
        rows = self.db.execute("SELECT ref, elo, jobs, done, no_shows, wins, losses, rated, score_sum,"
                               " ms_sum FROM staff ORDER BY elo DESC, done DESC").fetchall()
        out = []
        for i, (ref, elo, jobs, done, no_shows, wins, losses, rated, score_sum, ms_sum) in enumerate(rows):
            out.append({"place": i + 1, "ref": ref, "elo": round(elo), "rank": rank_name(elo),
                        "jobs": jobs, "done": done, "no_shows": no_shows, "wins": wins,
                        "losses": losses,
                        "avg_score": round(score_sum / rated, 1) if rated else None,
                        "avg_seconds": round(ms_sum / done / 1000, 1) if done else None})
        return out
