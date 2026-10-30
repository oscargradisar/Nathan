"""Hot-path speed: model picking must stay a handful of SQLite queries, and the
SQLite files must use the fast WAL journal. Scripted models only, no network."""

from nathan.cache import ResponseCache
from nathan.quota import QuotaManager
from nathan.roster import Roster
from nathan.staff import Staff
from tests.fakes import fake_providers

MANY = {f"prov{i}": {f"m{j}": ["fast", "writing"] if j % 2 else ["reasoning"] for j in range(6)}
        for i in range(6)}  # 36 models, about the size of the real roster


def count_queries(db):
    seen = []
    db.set_trace_callback(seen.append)
    return seen


def test_candidates_uses_a_few_queries(tmp_path):
    providers = fake_providers(MANY)
    quota = QuotaManager(providers, tmp_path / "q.sqlite")
    for p in providers.values():
        for m in p.models:
            quota.record(p.name, m, 10, {})
    roster = Roster(providers, quota)
    seen = count_queries(quota.db)
    refs = roster.candidates(["fast"])
    assert len(refs) == 18
    assert len(seen) <= 6, seen  # was ~250 (several per model)


def test_engine_chain_scans_once_and_keeps_order(make_engine, tmp_path):
    engine = make_engine(lambda r, m, **k: "ok")
    seen = count_queries(engine.quota.db)
    chain = engine.chain(["code"], first=["gamma/g-big"])
    assert len(seen) <= 6
    # given models first, then tag fit, then everything else usable
    assert chain[0] == "gamma/g-big" and chain[1] == "beta/b-big"
    assert sorted(chain) == sorted(engine.roster.candidates(None))


def test_engine_fastest_and_judges(make_engine):
    engine = make_engine(lambda r, m, **k: "ok")
    assert set(engine.fastest(["cheap"])) == {"alpha/a-small"}
    assert set(engine.fastest(["no-such-tag"])) == set(engine.roster.candidates(None))
    judges = engine.judges(exclude=["alpha/a-big"])
    assert judges[-1] == "alpha/a-big" and set(judges) == {"alpha/a-big", "gamma/g-big"}


def test_staff_reads_are_cached_until_a_write():
    s = Staff()
    s.record("a/m", ok=True, ms=2000)
    assert s.seconds("a/m") == 2.0
    seen = count_queries(s.db)
    for _ in range(50):
        s.priority("a/m")
        s.seconds("b/m")
    assert len(seen) <= 1
    s.record("a/m", ok=True, ms=4000)
    assert s.seconds("a/m") == 3.0  # our own write is seen at once


def test_sqlite_files_use_wal(tmp_path):
    for db in (QuotaManager(fake_providers(), tmp_path / "q.sqlite").db,
               Staff(tmp_path / "s.sqlite").db, ResponseCache(tmp_path / "c.sqlite").db):
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
