"""Files that SQLite made are read and changed by this engine, and files that this engine made are read and changed by SQLite: the two share one file format, and each is the other's oracle."""
import os
import random
import sqlite3

import pytest

import kendi_db
from kendi_db import DatabaseError


def sqlite_file(path, script, page_size=4096):
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size = %d" % page_size)
    con.executescript(script)
    con.commit()
    con.close()


def both(path, sql, params=()):
    """The answer of this engine and the answer of SQLite to one query, on the same file."""
    ours = kendi_db.connect(path)
    try:
        a = ours.execute(sql, params).fetchall()
    finally:
        ours.close()
    ref = sqlite3.connect(path)
    ref.text_factory = lambda b: b.decode("utf-8", "surrogateescape")
    b = ref.execute(sql, params).fetchall()
    ref.close()
    return a, b


def integrity_ok(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        con.close()


@pytest.mark.parametrize("page_size", [512, 1024, 4096, 65536])
def test_reads_a_database_sqlite_built(tmp_path, page_size):
    path = str(tmp_path / "s.db")
    rnd = random.Random(page_size)
    stmts = ["CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, born INTEGER, bio TEXT, photo BLOB, score REAL)", "CREATE INDEX people_born ON people (born)",
             "CREATE UNIQUE INDEX people_name ON people (name)", "CREATE TABLE notes (a, b, c, UNIQUE (a, b))"]
    script = ";\n".join(stmts) + ";\n"
    con = sqlite3.connect(":memory:")
    con.executescript(script)
    rows = []
    for i in range(1, 1500):
        bio = "".join(rnd.choice("abcdefgh ") for _ in range(rnd.choice([0, 5, 80, 700, 5000])))
        rows.append((i, "name%05d" % rnd.randrange(10**5), rnd.choice([None, 1900 + i % 120]), bio, bytes(rnd.randrange(256) for _ in range(rnd.choice([0, 3, 300]))), rnd.random() * 100))
    con.close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA page_size = %d" % page_size)
    db.executescript(script)
    seen = set()
    for r in rows:
        if r[1] in seen:
            continue
        seen.add(r[1])
        db.execute("INSERT INTO people VALUES (?, ?, ?, ?, ?, ?)", r)
    db.executemany("INSERT INTO notes VALUES (?, ?, ?)", [(i % 40, i % 7, "n" * (i % 50)) for i in range(300) if (i % 40, i % 7) not in {(j % 40, j % 7) for j in range(i)}])
    db.execute("DELETE FROM people WHERE id % 5 = 0")
    db.commit()
    db.close()
    for sql in ["SELECT count(*), sum(born), min(name), max(length(bio)), sum(length(photo)), round(sum(score), 6) FROM people", "SELECT * FROM people ORDER BY id",
                "SELECT id FROM people WHERE born = 1950 ORDER BY id", "SELECT name FROM people WHERE name > 'name50000' ORDER BY name", "SELECT count(*) FROM notes WHERE a = 3 AND b = 3",
                "SELECT born, count(*) FROM people GROUP BY born ORDER BY born", "SELECT * FROM notes ORDER BY a, b", "SELECT name, type, tbl_name FROM sqlite_master ORDER BY name"]:
        a, b = both(path, sql)
        assert a == b, sql
    con = kendi_db.connect(path)
    assert con.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    assert con.execute("PRAGMA page_size").fetchall() == [(page_size,)]
    con.close()


def test_sqlite_features_that_this_engine_reads(tmp_path):
    path = str(tmp_path / "s.db")
    sqlite_file(path, """
        CREATE TABLE seq (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT);
        INSERT INTO seq (v) VALUES ('a'), ('b'), ('c');
        DELETE FROM seq WHERE id = 3;
        CREATE TABLE parent (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE child (id INTEGER PRIMARY KEY, parent INTEGER REFERENCES parent (id), x DEFAULT (1 + 2), y TEXT CHECK (y <> 'bad'));
        INSERT INTO parent VALUES (1, 'p');
        INSERT INTO child (parent, y) VALUES (1, 'ok');
        CREATE VIEW kids AS SELECT p.name AS parent, c.y AS y, c.x AS x FROM child c JOIN parent p ON p.id = c.parent;
        CREATE TABLE "weird name" ("col one" INTEGER, [two] TEXT, `three` REAL);
        INSERT INTO "weird name" VALUES (1, 'x', 2.5);
        CREATE INDEX "weird idx" ON "weird name" ("col one" DESC, two COLLATE NOCASE);
        CREATE TABLE gen (a INTEGER, b INTEGER);
        INSERT INTO gen SELECT value, value * value FROM (WITH RECURSIVE c(value) AS (SELECT 1 UNION ALL SELECT value + 1 FROM c WHERE value < 50) SELECT value FROM c);
        PRAGMA user_version = 42;
    """)
    ours = kendi_db.connect(path)
    assert ours.execute("PRAGMA user_version").fetchall() == [(42,)]
    assert ours.execute("SELECT * FROM kids").fetchall() == [("p", "ok", 3)]
    assert ours.execute('SELECT "col one", two, three FROM "weird name"').fetchall() == [(1, "x", 2.5)]
    assert ours.execute("SELECT sum(b) FROM gen").fetchall() == [(42925,)]
    assert ours.execute("SELECT * FROM sqlite_sequence").fetchall() == [("seq", 3)]
    ours.execute("INSERT INTO seq (v) VALUES ('d')")                                      # AUTOINCREMENT continues after the largest id ever used, 3, not 2
    assert ours.execute("SELECT id FROM seq ORDER BY id").fetchall() == [(1,), (2,), (4,)]
    ours.close()
    assert integrity_ok(path)


def test_triggers_and_without_rowid_tables_are_refused_rather_than_mishandled(tmp_path):
    path = str(tmp_path / "s.db")
    sqlite_file(path, """
        CREATE TABLE log (msg TEXT);
        CREATE TABLE t (a);
        CREATE TRIGGER t_ins AFTER INSERT ON t BEGIN INSERT INTO log VALUES ('inserted ' || new.a); END;
        CREATE TABLE wr (k TEXT PRIMARY KEY, v) WITHOUT ROWID;
        INSERT INTO wr VALUES ('a', 1);
        INSERT INTO t VALUES (1);
    """)
    con = kendi_db.connect(path)
    assert con.execute("SELECT msg FROM log").fetchall() == [("inserted 1",)]
    assert con.execute("SELECT a FROM t").fetchall() == [(1,)]                          # reading a table that has a trigger is fine
    for sql in ["INSERT INTO t VALUES (2)", "UPDATE t SET a = 5", "DELETE FROM t", "SELECT * FROM wr", "INSERT INTO wr VALUES ('b', 2)"]:
        with pytest.raises(DatabaseError):
            con.execute(sql)
    assert con.execute("SELECT a FROM t").fetchall() == [(1,)] and con.execute("SELECT count(*) FROM log").fetchall() == [(1,)]       # nothing was changed behind the trigger's back
    con.execute("INSERT INTO log VALUES ('direct')")                                    # tables without a trigger on them work as always
    con.close()
    assert integrity_ok(path)
    assert sqlite3.connect(path).execute("SELECT msg FROM log ORDER BY rowid").fetchall() == [("inserted 1",), ("direct",)]


def test_sqlite_changes_a_database_this_engine_wrote(tmp_path):
    path = str(tmp_path / "o.db")
    con = kendi_db.connect(path, page_size=1024)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)")
    con.execute("CREATE INDEX t_a ON t (a)")
    con.execute("CREATE UNIQUE INDEX t_b ON t (b)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i % 17, "b%d" % i + "-" * (i % 300)) for i in range(1, 1001)])
    con.close()
    assert integrity_ok(path)
    ref = sqlite3.connect(path)
    ref.execute("DELETE FROM t WHERE id % 3 = 0")
    ref.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i % 17, "new%d" % i) for i in range(2000, 2400)])
    ref.execute("UPDATE t SET a = a + 100 WHERE id % 2 = 0")
    ref.commit()
    ref.execute("VACUUM")                                                                 # (rewrites the whole file: every page of it is new)
    ref.close()
    a, b = both(path, "SELECT count(*), sum(a), sum(length(b)), min(id), max(id) FROM t")
    assert a == b
    ours = kendi_db.connect(path)
    assert ours.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    ours.execute("DELETE FROM t WHERE a > 100")
    ours.execute("INSERT INTO t VALUES (5000, 1, 'last')")
    ours.close()
    assert integrity_ok(path)
    a, b = both(path, "SELECT id, a, length(b) FROM t ORDER BY id")
    assert a == b


def test_the_two_engines_take_turns_on_one_file(tmp_path):
    path = str(tmp_path / "turns.db")
    ours = kendi_db.connect(path)
    ours.execute("CREATE TABLE t (n INTEGER PRIMARY KEY, who TEXT)")
    ours.close()
    for turn in range(6):
        if turn % 2 == 0:
            c = kendi_db.connect(path)
            c.executemany("INSERT INTO t (who) VALUES (?)", [("ours",)] * 40)
            c.execute("DELETE FROM t WHERE n % 7 = 0")
            c.close()
        else:
            c = sqlite3.connect(path)
            c.executemany("INSERT INTO t (who) VALUES (?)", [("sqlite",)] * 40)
            c.execute("DELETE FROM t WHERE n % 5 = 0")
            c.commit()
            c.close()
        assert integrity_ok(path)
        a, b = both(path, "SELECT who, count(*), sum(n) FROM t GROUP BY who ORDER BY who")
        assert a == b
    assert both(path, "SELECT count(*) FROM t")[0][0][0] > 100


def test_a_page_of_the_file_gone_bad_is_reported_not_trusted(tmp_path):
    path = str(tmp_path / "bad.db")
    con = kendi_db.connect(path, page_size=512)
    con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(i, "x" * 60) for i in range(1, 400)])
    con.close()
    with open(path, "r+b") as f:
        f.seek(512 * 3)
        f.write(b"\xff" * 512)                                                         # a page that is not a B-tree page at all
    con = kendi_db.connect(path)
    problems = con.execute("PRAGMA integrity_check").fetchall()
    assert problems != [("ok",)] and any("page" in p[0].lower() for p in problems)
    con.close()
    ref = sqlite3.connect(path)
    try:
        assert ref.execute("PRAGMA integrity_check").fetchall() != [("ok",)]                # SQLite finds the same file broken (or refuses to look at it)
    except sqlite3.DatabaseError:
        pass
    ref.close()


def test_files_that_are_not_databases(tmp_path):
    for name, content in [("empty-ok", b""), ("text", b"hello world\n" * 500), ("short-header", b"SQLite format 3\x00" + b"\x00" * 20)]:
        path = str(tmp_path / name)
        with open(path, "wb") as f:
            f.write(content)
        if name == "empty-ok":
            con = kendi_db.connect(path)
            con.execute("CREATE TABLE t (a)")
            con.close()
            assert integrity_ok(path)
        else:
            with pytest.raises(kendi_db.CorruptDatabase):
                kendi_db.connect(path).execute("SELECT 1 FROM sqlite_master")
    assert not os.path.exists(str(tmp_path / "text") + "-journal")


def test_a_directory_or_a_missing_folder_is_an_error(tmp_path):
    with pytest.raises(DatabaseError):
        kendi_db.connect(str(tmp_path))
    with pytest.raises(DatabaseError):
        kendi_db.connect(str(tmp_path / "no" / "such" / "folder" / "x.db")).execute("CREATE TABLE t (a)")


def test_alter_and_pragmas_leave_sqlite_a_good_file(tmp_path):
    path = str(tmp_path / "alter.db")
    con = kendi_db.connect(path)
    con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT UNIQUE, c)")
    con.execute("CREATE INDEX t_c ON t (c)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, "b%d" % i, i * 2) for i in range(1, 50)])
    con.execute("ALTER TABLE t ADD COLUMN d TEXT DEFAULT 'dflt'")
    con.execute("ALTER TABLE t RENAME TO moved")
    con.execute("PRAGMA user_version = 3")
    con.close()
    assert integrity_ok(path)
    ref = sqlite3.connect(path)
    assert ref.execute("SELECT count(*), min(d), max(d) FROM moved").fetchall() == [(49, "dflt", "dflt")]
    assert ref.execute("SELECT name, tbl_name FROM sqlite_master ORDER BY name").fetchall() == [("moved", "moved"), ("sqlite_autoindex_moved_1", "moved"), ("t_c", "moved")]
    assert ref.execute("PRAGMA user_version").fetchall() == [(3,)]
    ref.execute("INSERT INTO moved (b, c) VALUES ('from sqlite', 1)")
    ref.commit()
    ref.close()
    assert kendi_db.connect(path).execute("SELECT d FROM moved WHERE b = 'from sqlite'").fetchall() == [("dflt",)]
