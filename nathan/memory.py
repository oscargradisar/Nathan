"""Tier 4 memory: every question, answer and saved fact goes into SQLite with FTS5.

FTS5 is SQLite's built-in full-text search engine: it builds an index of words so
"what's my cat called?" finds the old note "my cat is called Miso" in milliseconds,
ranked by relevance (BM25). No vector database or embedding model needed, so it costs
no quota and works offline.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from nathan.config import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY, ts REAL, session TEXT, kind TEXT, content TEXT, meta TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5(
    content, content='entries', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS entries_ai AFTER INSERT ON entries BEGIN
    INSERT INTO entries_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS entries_ad AFTER DELETE ON entries BEGIN
    INSERT INTO entries_fts(entries_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;
"""

# Words too common to help a search
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "but",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "i",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "me",
    "my",
    "of",
    "on",
    "or",
    "our",
    "so",
    "than",
    "that",
    "the",
    "their",
    "them",
    "then",
    "there",
    "these",
    "they",
    "this",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "you",
    "your",
}


@dataclass
class Entry:
    id: int
    ts: float
    session: str
    kind: str  # question | answer | fact | note
    content: str
    meta: dict


def fts_query(text: str) -> str:
    """Turn free text into a safe FTS5 query: quoted words joined by OR."""
    words = [w for w in re.findall(r"[\w']+", text.lower()) if w not in STOPWORDS and len(w) > 1]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class Memory:
    def __init__(self, path: Path | str | None = None, session: str | None = None) -> None:
        self.db = sqlite3.connect(
            str(path or data_dir() / "memory.sqlite"), check_same_thread=False
        )
        self.db.executescript(SCHEMA)
        self.session = session or uuid.uuid4().hex[:8]

    def add(self, kind: str, content: str, meta: dict | None = None) -> int:
        from nathan.jobs import current

        if (job := current.get()) and "job" not in (meta or {}):
            meta = {**(meta or {}), "job": job["id"]}  # tagged, so jobs never recall each other
        cur = self.db.execute(
            "INSERT INTO entries (ts, session, kind, content, meta) VALUES (?,?,?,?,?)",
            (time.time(), self.session, kind, content, json.dumps(meta or {})),
        )
        self.db.commit()
        return int(cur.lastrowid)

    def remember(self, fact: str) -> int:
        return self.add("fact", fact)

    def log_exchange(self, question: str, answer: str, meta: dict | None = None) -> None:
        self.add("question", question, meta)
        self.add("answer", f"Q: {question}\nA: {answer}", meta)

    def _rows(self, sql: str, args: tuple) -> list[Entry]:
        return [
            Entry(r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or "{}"))
            for r in self.db.execute(sql, args)
        ]

    def search(
        self,
        text: str,
        k: int = 5,
        kinds: tuple[str, ...] = ("fact", "answer", "note"),
        exclude_session: str | None = None,
    ) -> list[Entry]:
        q = fts_query(text)
        if not q:
            return []
        from nathan.jobs import current

        job = current.get()
        marks = ",".join("?" * len(kinds))
        # Inside a job: only that job's entries. Outside: only entries from no job.
        scope = "AND json_extract(e.meta, '$.job') = ? " if job else "AND json_extract(e.meta, '$.job') IS NULL "
        sql = (
            f"SELECT e.id, e.ts, e.session, e.kind, e.content, e.meta FROM entries_fts f "
            f"JOIN entries e ON e.id = f.rowid WHERE entries_fts MATCH ? AND e.kind IN ({marks}) "
            + scope
            + ("AND e.session != ? " if exclude_session else "")
            + "ORDER BY bm25(entries_fts) LIMIT ?"
        )
        args: tuple = ((q, *kinds) + ((job["id"],) if job else ())
                       + ((exclude_session,) if exclude_session else ()) + (k,))
        return self._rows(sql, args)

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]

    def recent(self, n: int = 10) -> list[Entry]:
        return self._rows(
            "SELECT id, ts, session, kind, content, meta FROM entries ORDER BY id DESC LIMIT ?",
            (n,),
        )

    def context_for(self, question: str, k: int = 4, max_chars: int = 3000) -> str:
        """A system-message block of relevant past notes, or "" if nothing matches."""
        hits = self.search(question, k)
        if not hits:
            return ""
        lines, used = [], 0
        for e in hits:
            snippet = e.content[:800]
            if used + len(snippet) > max_chars:
                break
            day = time.strftime("%Y-%m-%d", time.localtime(e.ts))
            lines.append(f"- ({e.kind}, {day}) {snippet}")
            used += len(snippet)
        return (
            "Things the user told you or asked in earlier sessions (use them if relevant, "
            "ignore them otherwise):\n" + "\n".join(lines)
        )
