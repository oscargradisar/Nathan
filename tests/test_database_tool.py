from nathan.tools import default_toolbox


async def test_query_csv_and_sqlite(tmp_path):
    (tmp_path / "sales.csv").write_text("Region,Amount\nnorth,10\nsouth,5\nnorth,7\n")
    box = default_toolbox(workspace=tmp_path)
    assert "Region" in await box.call("query_data", {"path": "sales.csv"})
    out = await box.call("query_data", {"path": "sales.csv",
                                        "sql": "SELECT Region, SUM(Amount) FROM sales GROUP BY Region"})
    assert "north | 17" in out and "south | 5" in out

    assert "changed" in await box.call("query_data", {"path": "notes.db",
                                                      "sql": "CREATE TABLE t (x INTEGER)"})
    await box.call("query_data", {"path": "notes.db", "sql": "INSERT INTO t VALUES (42)"})
    assert "42" in await box.call("query_data", {"path": "notes.db", "sql": "SELECT x FROM t"})
    assert "SQL error" in await box.call("query_data", {"path": "notes.db", "sql": "SELEC nope"})


async def test_database_stays_in_workspace(tmp_path):
    box = default_toolbox(workspace=tmp_path)
    out = await box.call("query_data", {"path": "../escape.db", "sql": "SELECT 1"})
    assert "outside" in out or "ERROR" in out
    assert not (tmp_path.parent / "escape.db").exists()
