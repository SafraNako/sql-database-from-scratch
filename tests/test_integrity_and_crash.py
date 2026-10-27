"""Two ways of asking whether a database is sound: ``PRAGMA integrity_check`` on files that were damaged on purpose, and what is left on disk when a SQL statement's commit is
interrupted by a crash (both this engine and SQLite then have to come back to the old data)."""
import os
import sqlite3

import pytest

import kendi_db
from kendi_db.errors import SimulatedCrash
from kendi_db.format import decode_record, encode_record
from kendi_db.pager import OFF_FREELIST_COUNT


def build(path, page_size=512, rows=300):
    con = kendi_db.connect(path, page_size=page_size)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)")
    con.execute("CREATE INDEX t_a ON t (a)")
    con.execute("CREATE UNIQUE INDEX t_b ON t (b)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i % 13, "b%05d" % i + "x" * (i % 70)) for i in range(1, rows + 1)])
    return con


def damage(con, change):
    """Run ``change(db, pager)`` inside a transaction on the engine's internals and commit it, bypassing every SQL-level check."""
    db = con._db
    db.load()
    db.pager.begin()
    change(db, db.pager)
    db.pager.commit()


def index_named(db, name):
    return next(i for t in db.schema.tables.values() for i in t.indexes if i.name == name)


def check(con):
    return [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]


def test_a_sound_database_is_ok(tmp_path):
    con = build(str(tmp_path / "ok.db"))
    assert check(con) == ["ok"]
    con.execute("DELETE FROM t WHERE id % 2 = 0")
    con.execute("UPDATE t SET a = a + 1")
    assert check(con) == ["ok"]
    con.execute("DROP INDEX t_a")
    assert check(con) == ["ok"]
    con.close()


def test_a_row_missing_from_an_index_is_found(tmp_path):
    con = build(str(tmp_path / "c.db"))
    damage(con, lambda db, pager: db.index_tree(index_named(db, "t_a")).delete([5 % 13, 5]))
    problems = check(con)
    assert problems != ["ok"] and any("missing from index t_a" in p for p in problems)
    assert not any("t_b" in p for p in problems)


def test_an_index_entry_without_a_row_is_found(tmp_path):
    con = build(str(tmp_path / "c.db"))
    damage(con, lambda db, pager: db.table_tree(db.schema.table("t")).delete(77))
    problems = check(con)
    assert any("wrong # of entries in index" in p for p in problems), problems
    ref = sqlite3.connect(str(tmp_path / "c.db"))
    assert ref.execute("PRAGMA integrity_check").fetchall() != [("ok",)]                  # SQLite agrees that this is damaged
    ref.close()


def test_a_leaked_page_and_a_wrong_free_list_count_are_found(tmp_path):
    path = str(tmp_path / "c.db")
    con = build(path)
    damage(con, lambda db, pager: pager.allocate())                                          # a page that nothing points to
    assert any("never used" in p for p in check(con))
    con.close()
    con = build(str(tmp_path / "d.db"))
    con.execute("DELETE FROM t WHERE id > 100")
    assert check(con) == ["ok"] and con.execute("PRAGMA freelist_count").fetchall()[0][0] > 0
    damage(con, lambda db, pager: pager.set_u32(OFF_FREELIST_COUNT, 1))
    assert any("free list" in p for p in check(con))
    con.close()


def test_a_page_that_is_used_twice_is_found(tmp_path):
    path = str(tmp_path / "c.db")
    con = build(path)
    master = con.execute("SELECT name, rootpage FROM sqlite_master WHERE type = 'index' ORDER BY name").fetchall()
    assert len(master) == 2

    def alias(db, pager):                                                                      # t_b's root page number now points at t_a's root
        raw = db.table_tree(db.master)
        for rowid, rec in list(raw.scan()):
            vals = decode_record(rec)
            if vals[1] == "t_b":
                vals[3] = master[0][1]
                raw.delete(rowid)
                raw.insert(rowid, encode_record(vals))
    damage(con, alias)
    con.close()
    con = kendi_db.connect(path)
    assert any("used twice" in p or "never used" in p for p in check(con))


def test_a_database_that_is_cut_short_is_refused_or_reported(tmp_path):
    path = str(tmp_path / "c.db")
    build(path).close()
    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.truncate(size - 512 * 3)
    try:
        con = kendi_db.connect(path)
        problems = check(con)
        assert problems != ["ok"]
    except kendi_db.DatabaseError:
        pass


# --- crashes through SQL ---------------------------------------------------------------------------------------------------------------------------
def crash_in(path, sql, point, params=()):
    con = kendi_db.connect(path)
    con._db.pager.crash_at = point
    with pytest.raises(SimulatedCrash):
        con.execute(sql, params)
    con._db.pager.file.close()                                                                   # the process is gone: no cleanup, no journal removal


BIG = ["journal_written", "after_page_1", "after_page_3", "database_synced"]
SMALL = ["journal_written", "after_page_1", "database_synced"]                                  # a statement that only touches two pages never gets to "after_page_3"
CASES = [(sql, point) for sql in ["DELETE FROM t WHERE id % 3 = 0", "UPDATE t SET b = b || 'zzzz'", "INSERT INTO t SELECT id + 1000, a, 'copy' || id FROM t",
                                  "CREATE TABLE extra AS SELECT * FROM t"] for point in BIG] + [("DROP INDEX t_a", point) for point in SMALL]


@pytest.mark.parametrize("sql, point", CASES)
@pytest.mark.parametrize("recover_with", ["this engine", "sqlite"])
def test_a_crash_while_committing_a_statement_leaves_the_old_data(tmp_path, sql, point, recover_with):
    path = str(tmp_path / "c.db")
    build(path).close()
    before = sqlite3.connect(path).execute("SELECT id, a, b FROM t ORDER BY id").fetchall()
    tables_before = sqlite3.connect(path).execute("SELECT type, name FROM sqlite_master ORDER BY name").fetchall()
    crash_in(path, sql, point)
    assert os.path.exists(path + "-journal")
    if recover_with == "this engine":
        con = kendi_db.connect(path)
        assert con.execute("SELECT id, a, b FROM t ORDER BY id").fetchall() == before
        assert check(con) == ["ok"]
        con.close()
    else:
        ref = sqlite3.connect(path)
        assert ref.execute("SELECT id, a, b FROM t ORDER BY id").fetchall() == before
        assert ref.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        ref.close()
    assert not os.path.exists(path + "-journal")
    assert sqlite3.connect(path).execute("SELECT type, name FROM sqlite_master ORDER BY name").fetchall() == tables_before
    con = kendi_db.connect(path)                                                                 # and the database is usable afterwards
    con.execute("INSERT INTO t VALUES (99999, 1, 'after the crash')")
    con.close()
    assert sqlite3.connect(path).execute("SELECT b FROM t WHERE id = 99999").fetchall() == [("after the crash",)]


def test_a_crash_inside_an_explicit_transaction_loses_all_of_it(tmp_path):
    path = str(tmp_path / "c.db")
    build(path).close()
    before = sqlite3.connect(path).execute("SELECT count(*), sum(a) FROM t").fetchall()
    con = kendi_db.connect(path)
    con.execute("BEGIN")
    con.execute("DELETE FROM t WHERE id < 100")
    con.execute("INSERT INTO t VALUES (5000, 1, 'x')")
    con.execute("CREATE TABLE side (x)")
    con._db.pager.crash_at = "after_page_2"
    with pytest.raises(SimulatedCrash):
        con.execute("COMMIT")
    con._db.pager.file.close()
    assert sqlite3.connect(path).execute("SELECT count(*), sum(a) FROM t").fetchall() == before
    assert sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE name = 'side'").fetchall() == []


def test_an_uncommitted_transaction_that_is_simply_abandoned_changes_nothing(tmp_path):
    path = str(tmp_path / "c.db")
    build(path).close()
    before = sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchall()
    con = kendi_db.connect(path)
    con.execute("BEGIN")
    con.execute("DELETE FROM t")
    con.close()                                                                                  # closing rolls back
    assert sqlite3.connect(path).execute("SELECT count(*) FROM t").fetchall() == before
    assert not os.path.exists(path + "-journal")
