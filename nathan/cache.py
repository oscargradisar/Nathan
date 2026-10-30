"""Response cache: the same question to the same model within a day costs no quota.

Keyed on model + messages + settings (a hash, so prompts aren't stored as keys). Entries
expire after TTL_S. Tool calls are never cached: the client skips the cache when tools
are offered, because a tool's result can change between calls.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from nathan.client import ChatResult
from nathan.quota import open_db

TTL_S = 24 * 3600


def make_key(ref: str, params: dict[str, Any]) -> str:
    blob = json.dumps({"ref": ref, **params}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache:
    def __init__(self, path: Path | str = ":memory:", ttl: float = TTL_S, clock=time.time) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = open_db(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, ts REAL, result TEXT)")
        self.ttl, self.clock = ttl, clock
        self.hits = 0

    def get(self, key: str) -> ChatResult | None:
        row = self.db.execute("SELECT ts, result FROM cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        if self.clock() - row[0] > self.ttl:
            self.db.execute("DELETE FROM cache WHERE key = ?", (key,))
            self.db.commit()
            return None
        self.hits += 1
        return ChatResult(**{**json.loads(row[1]), "cached": True, "latency_ms": 0.0})

    def put(self, key: str, result: ChatResult) -> None:
        data = {k: v for k, v in dataclasses.asdict(result).items() if k != "cached"}
        self.db.execute("INSERT OR REPLACE INTO cache VALUES (?, ?, ?)",
                        (key, self.clock(), json.dumps(data)))
        self.db.commit()

    def prune(self) -> int:
        cur = self.db.execute("DELETE FROM cache WHERE ts < ?", (self.clock() - self.ttl,))
        self.db.commit()
        return cur.rowcount
