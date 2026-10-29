"""Database tool: SQL over databases and spreadsheets in the workspace folder.

- A .db / .sqlite file is opened directly. Reads and writes both work (it's your data,
  inside the sandbox), but only one statement runs per call.
- A .csv file is loaded into a temporary in-memory table named after the file, so the
  agent can ask "SELECT region, SUM(sales) FROM sales GROUP BY region" of a spreadsheet.

Paths go through the same workspace lock as the file tools.
"""

from __future__ import annotations

import csv
import re
import sqlite3
from pathlib import Path

from nathan.config import workspace_dir
from nathan.tools.base import Tool, params
from nathan.tools.files import safe_path

MAX_ROWS = 200


def _table_name(path: Path) -> str:
    return re.sub(r"\W+", "_", path.stem).strip("_") or "data"


def _csv_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise ValueError(f"{path.name} is empty")
    cols = [re.sub(r"\W+", "_", c).strip("_") or f"col{i}" for i, c in enumerate(rows[0])]
    table = _table_name(path)
    db.execute(f'CREATE TABLE "{table}" ({", ".join(f"{c!r}" for c in cols)})')
    db.executemany(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(cols))})',
                   [r + [""] * (len(cols) - len(r)) for r in rows[1:]])
    return db


def _format(cur: sqlite3.Cursor) -> str:
    if cur.description is None:
        return f"ok, {cur.rowcount} row(s) changed"
    cols = [d[0] for d in cur.description]
    rows = cur.fetchmany(MAX_ROWS + 1)
    lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r)
                                  for r in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"…more than {MAX_ROWS} rows; add LIMIT or aggregate")
    return "\n".join(lines) if rows else " | ".join(cols) + "\n(no rows)"


def make_tool(workspace: Path | None = None) -> Tool:
    root = (workspace or workspace_dir()).resolve()

    def query_data(path: str, sql: str = "") -> str:
        p = safe_path(root, path)
        if p.suffix.lower() == ".csv":
            if not p.is_file():
                return f"ERROR: no file {path}"
            db = _csv_db(p)
        elif p.suffix.lower() in (".db", ".sqlite", ".sqlite3"):
            p.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(p)
        else:
            return "ERROR: use a .csv, .db or .sqlite file in the workspace"
        try:
            if not sql.strip():  # no query: describe what's there
                tables = db.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
                return "\n\n".join(f"{name}:\n{ddl}" for name, ddl in tables) or "(no tables yet)"
            cur = db.execute(sql)  # execute() refuses multiple statements
            out = _format(cur)
            db.commit()
            return out
        except sqlite3.Error as e:
            return f"SQL error: {e}"
        finally:
            db.close()

    return Tool(
        "query_data",
        "Run SQL on a .db/.sqlite file or a .csv spreadsheet in the workspace (a CSV becomes a "
        "table named after the file). Leave sql empty to see the tables and columns first.",
        params(["path"], path=("string", "file inside the workspace, e.g. sales.csv"),
               sql=("string", "one SQL statement; empty to describe the tables")),
        query_data)
