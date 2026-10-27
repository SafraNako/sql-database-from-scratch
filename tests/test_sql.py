"""The SQL layer, statement by statement, with the answers written out by hand; and after every scenario SQLite is asked whether the file that was written is sound."""
import os
import sqlite3

import pytest

import kendi_db
from kendi_db import DatabaseError, IntegrityError, OperationalError, SQLSyntaxError


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "t.db")


@pytest.fixture
def con(path):
    c = kendi_db.connect(path)
    yield c
    c.close()
    if os.path.exists(path):
        assert sqlite3.connect(path).execute("PRAGMA integrity_check").fetchall() == [("ok",)]


def rows(con, sql, params=()):
    return con.execute(sql, params).fetchall()


# --- tables, rows, queries -------------------------------------------------------------------------------------------------------------------------
def test_create_insert_select(con):
    con.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, age INTEGER, email TEXT UNIQUE)")
    con.execute("INSERT INTO people (name, age, email) VALUES ('Ada', 36, 'ada@x.org'), ('Alan', 41, NULL), ('Grace', 85, 'grace@x.org')")
    assert rows(con, "SELECT id, name, age FROM people ORDER BY id") == [(1, "Ada", 36), (2, "Alan", 41), (3, "Grace", 85)]
    assert rows(con, "SELECT name FROM people WHERE age > 40 ORDER BY age DESC") == [("Grace",), ("Alan",)]
    assert rows(con, "SELECT count(*), avg(age), min(name), max(age) FROM people") == [(3, 54.0, "Ada", 85)]
    assert rows(con, "SELECT name FROM people WHERE email IS NULL") == [("Alan",)]
    assert rows(con, "SELECT upper(name) || '!' FROM people WHERE id = 2") == [("ALAN!",)]


def test_cursor_attributes_and_iteration(con):
    con.execute("CREATE TABLE t (a, b)")
    cur = con.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')")
    assert cur.rowcount == 2 and cur.lastrowid == 2
    cur = con.execute("SELECT a AS first, b FROM t ORDER BY a")
    assert [d[0] for d in cur.description] == ["first", "b"]
    assert cur.fetchone() == (1, "x") and cur.fetchmany(5) == [(2, "y")] and cur.fetchone() is None
    assert list(con.execute("SELECT a FROM t ORDER BY a DESC")) == [(2,), (1,)]
    assert con.execute("UPDATE t SET a = a + 10").rowcount == 2
    assert con.execute("DELETE FROM t WHERE a > 11").rowcount == 1
    assert con.total_changes == 2 + 2 + 1


def test_parameters_positional_numbered_and_named(con):
    con.execute("CREATE TABLE t (a, b)")
    con.execute("INSERT INTO t VALUES (?, ?)", (1, "one"))
    con.execute("INSERT INTO t VALUES (?2, ?1)", ("two", 2))
    con.execute("INSERT INTO t VALUES (:n, :s)", {"s": "three", "n": 3})
    con.executemany("INSERT INTO t VALUES (?, ?)", [(4, "four"), (5, "five")])
    assert rows(con, "SELECT * FROM t ORDER BY a") == [(1, "one"), (2, "two"), (3, "three"), (4, "four"), (5, "five")]
    assert rows(con, "SELECT b FROM t WHERE a = ? OR b = ?", (3, "five")) == [("three",), ("five",)]
    with pytest.raises(DatabaseError):
        con.execute("SELECT ? + ?", (1,))
    with pytest.raises(DatabaseError):
        con.execute("SELECT :x", {"y": 1})


def test_all_kinds_of_values_survive_a_round_trip(con):
    con.execute("CREATE TABLE t (a, b, c, d, e)")
    values = (None, -2**63, 2**63 - 1, 3.5)
    con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?)", values + ("héllo wörld ∑",))
    con.execute("INSERT INTO t VALUES (?, ?, ?, ?, ?)", (b"\x00\x01\xff", 0, 1, -0.0, "x" * 10000))
    assert rows(con, "SELECT * FROM t") == [values + ("héllo wörld ∑",), (b"\x00\x01\xff", 0, 1, -0.0, "x" * 10000)]
    assert rows(con, "SELECT typeof(a), typeof(b), typeof(d), typeof(e) FROM t") == [("null", "integer", "real", "text"), ("blob", "integer", "real", "text")]


def test_rowid_and_integer_primary_key(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v)")
    con.execute("INSERT INTO t (v) VALUES ('a')")
    con.execute("INSERT INTO t VALUES (10, 'b')")
    con.execute("INSERT INTO t (v) VALUES ('c')")
    assert rows(con, "SELECT rowid, id, v FROM t") == [(1, 1, "a"), (10, 10, "b"), (11, 11, "c")]
    assert rows(con, "SELECT v FROM t WHERE rowid = 10") == [("b",)] and rows(con, "SELECT v FROM t WHERE id BETWEEN 2 AND 10") == [("b",)]
    assert con.execute("INSERT INTO t (v) VALUES ('d')").lastrowid == 12
    con.execute("CREATE TABLE u (a)")
    con.execute("INSERT INTO u VALUES ('x'), ('y')")
    con.execute("DELETE FROM u WHERE a = 'y'")
    con.execute("INSERT INTO u VALUES ('z')")
    assert rows(con, "SELECT rowid, a FROM u ORDER BY rowid") == [(1, "x"), (2, "z")]           # the rowid of the deleted last row is used again
    with pytest.raises(IntegrityError):
        con.execute("INSERT INTO t VALUES (10, 'dup')")
    with pytest.raises(IntegrityError, match="datatype mismatch"):
        con.execute("INSERT INTO t VALUES ('abc', 'x')")


def test_autoincrement_never_reuses_a_rowid(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v)")
    con.execute("INSERT INTO t (v) VALUES ('a'), ('b'), ('c')")
    con.execute("DELETE FROM t WHERE id = 3")
    con.execute("INSERT INTO t (v) VALUES ('d')")
    assert rows(con, "SELECT id FROM t ORDER BY id") == [(1,), (2,), (4,)]
    assert rows(con, "SELECT * FROM sqlite_sequence") == [("t", 4)]
    con.execute("DROP TABLE t")
    assert rows(con, "SELECT * FROM sqlite_sequence") == []


# --- constraints and conflicts ------------------------------------------------------------------------------------------------------------------------
def test_constraints(con):
    con.execute("CREATE TABLE t (a INTEGER NOT NULL, b TEXT UNIQUE, c INTEGER CHECK (c > 0), d DEFAULT 'dd', UNIQUE (a, c))")
    con.execute("INSERT INTO t (a, b, c) VALUES (1, 'x', 5)")
    assert rows(con, "SELECT d FROM t") == [("dd",)]
    for sql, kind in [("INSERT INTO t (a, b, c) VALUES (NULL, 'y', 1)", "NOT NULL"), ("INSERT INTO t (a, b, c) VALUES (2, 'x', 1)", "UNIQUE"), ("INSERT INTO t (a, b, c) VALUES (2, 'y', 0)", "CHECK"),
                      ("INSERT INTO t (a, b, c) VALUES (1, 'z', 5)", "UNIQUE")]:
        with pytest.raises(IntegrityError, match=kind):
            con.execute(sql)
    con.execute("INSERT INTO t (a, b, c) VALUES (3, NULL, NULL)")                     # NULLs never collide in a UNIQUE
    con.execute("INSERT INTO t (a, b, c) VALUES (4, NULL, NULL)")
    assert rows(con, "SELECT count(*) FROM t") == [(3,)]


def test_insert_or_ignore_and_or_replace(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, k TEXT UNIQUE, v)")
    con.execute("INSERT INTO t VALUES (1, 'a', 'first'), (2, 'b', 'second')")
    assert con.execute("INSERT OR IGNORE INTO t VALUES (3, 'a', 'ignored')").rowcount == 0
    con.execute("INSERT OR REPLACE INTO t VALUES (3, 'a', 'replaced')")
    assert rows(con, "SELECT * FROM t ORDER BY id") == [(2, "b", "second"), (3, "a", "replaced")]
    con.execute("REPLACE INTO t VALUES (2, 'c', 'again')")
    assert rows(con, "SELECT * FROM t ORDER BY id") == [(2, "c", "again"), (3, "a", "replaced")]


def test_update_and_delete_with_indexes_keep_them_consistent(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b TEXT)")
    con.execute("CREATE INDEX t_a ON t (a)")
    con.execute("CREATE UNIQUE INDEX t_b ON t (b)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, i % 10, "k%d" % i) for i in range(1, 201)])
    con.execute("UPDATE t SET a = a + 100 WHERE id % 2 = 0")
    con.execute("UPDATE t SET b = 'z' || b WHERE a >= 100 AND id < 60")
    con.execute("DELETE FROM t WHERE a = 5")
    assert rows(con, "SELECT count(*) FROM t WHERE a = 5") == [(0,)]
    assert rows(con, "SELECT count(*) FROM t WHERE a = 100") == [(20,)]
    assert rows(con, "SELECT id FROM t WHERE b = 'zk2'") == [(2,)]
    with pytest.raises(IntegrityError):
        con.execute("UPDATE t SET b = 'k1' WHERE id = 3")
    assert rows(con, "SELECT b FROM t WHERE id = 3") == [("k3",)]                       # the failed update changed nothing
    assert rows(con, "PRAGMA integrity_check") == [("ok",)]


# --- queries --------------------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def shop(con):
    con.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT, city TEXT);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer INTEGER, amount REAL, status TEXT);
        INSERT INTO customers VALUES (1, 'Ada', 'London'), (2, 'Alan', 'London'), (3, 'Grace', 'NYC'), (4, 'Linus', 'Helsinki');
        INSERT INTO orders VALUES (1, 1, 10.5, 'paid'), (2, 1, 20.0, 'open'), (3, 2, 5.25, 'paid'), (4, 3, 100.0, 'paid'), (5, 3, 1.0, 'open'), (6, 9, 7.0, 'paid');
    """)
    return con


def test_joins(shop):
    q = "SELECT c.name, o.amount FROM customers c JOIN orders o ON o.customer = c.id ORDER BY o.id"
    assert rows(shop, q) == [("Ada", 10.5), ("Ada", 20.0), ("Alan", 5.25), ("Grace", 100.0), ("Grace", 1.0)]
    assert rows(shop, "SELECT c.name, o.id FROM customers c LEFT JOIN orders o ON o.customer = c.id WHERE o.id IS NULL") == [("Linus", None)]
    assert rows(shop, "SELECT count(*) FROM customers, orders") == [(24,)]
    assert rows(shop, "SELECT c.name FROM customers c INNER JOIN orders o USING (id) ORDER BY c.id") == [("Ada",), ("Alan",), ("Grace",), ("Linus",)]
    assert rows(shop, "SELECT o.id FROM orders o LEFT JOIN customers c ON c.id = o.customer WHERE c.id IS NULL") == [(6,)]


def test_group_by_having_distinct_order_limit(shop):
    assert rows(shop, "SELECT customer, count(*), sum(amount) FROM orders GROUP BY customer HAVING count(*) > 1 ORDER BY customer") == [(1, 2, 30.5), (3, 2, 101.0)]
    assert rows(shop, "SELECT status, count(*) FROM orders GROUP BY status ORDER BY 2 DESC, 1") == [("paid", 4), ("open", 2)]
    assert rows(shop, "SELECT DISTINCT city FROM customers ORDER BY city") == [("Helsinki",), ("London",), ("NYC",)]
    assert rows(shop, "SELECT id FROM orders ORDER BY amount DESC LIMIT 2 OFFSET 1") == [(2,), (1,)]
    assert rows(shop, "SELECT count(DISTINCT customer), total(amount), group_concat(status, '+') FROM (SELECT * FROM orders ORDER BY id)") == [(4, 143.75, "paid+open+paid+paid+open+paid")]
    assert rows(shop, "SELECT sum(amount) FROM orders WHERE 0") == [(None,)]
    assert rows(shop, "SELECT customer FROM orders GROUP BY customer ORDER BY max(amount) DESC") == [(3,), (1,), (9,), (2,)]


def test_subqueries(shop):
    assert rows(shop, "SELECT name FROM customers WHERE id IN (SELECT customer FROM orders WHERE amount > 15) ORDER BY id") == [("Ada",), ("Grace",)]
    assert rows(shop, "SELECT name FROM customers c WHERE EXISTS (SELECT 1 FROM orders o WHERE o.customer = c.id AND o.status = 'open') ORDER BY id") == [("Ada",), ("Grace",)]
    assert rows(shop, "SELECT name, (SELECT count(*) FROM orders o WHERE o.customer = c.id) FROM customers c ORDER BY id") == [("Ada", 2), ("Alan", 1), ("Grace", 2), ("Linus", 0)]
    assert rows(shop, "SELECT name FROM customers WHERE id NOT IN (SELECT customer FROM orders) ORDER BY id") == [("Linus",)]
    assert rows(shop, "SELECT m FROM (SELECT max(amount) AS m FROM orders)") == [(100.0,)]
    assert rows(shop, "SELECT (SELECT amount FROM orders WHERE id = 99)") == [(None,)]
    assert rows(shop, "WITH big AS (SELECT * FROM orders WHERE amount > 9) SELECT count(*) FROM big") == [(3,)]


def test_compound_selects(shop):
    assert rows(shop, "SELECT city FROM customers UNION SELECT 'Paris' ORDER BY 1") == [("Helsinki",), ("London",), ("NYC",), ("Paris",)]
    assert rows(shop, "SELECT id FROM customers INTERSECT SELECT customer FROM orders ORDER BY 1") == [(1,), (2,), (3,)]
    assert rows(shop, "SELECT customer FROM orders EXCEPT SELECT id FROM customers ORDER BY 1") == [(9,)]
    assert rows(shop, "SELECT 1 UNION ALL SELECT 1 UNION ALL SELECT 2") == [(1,), (1,), (2,)]
    assert rows(shop, "VALUES (1, 'a'), (2, 'b')") == [(1, "a"), (2, "b")]


def test_views(shop):
    shop.execute("CREATE VIEW paid AS SELECT customer, amount FROM orders WHERE status = 'paid'")
    assert rows(shop, "SELECT sum(amount) FROM paid") == [(122.75,)]
    assert rows(shop, "SELECT c.name, p.amount FROM paid p JOIN customers c ON c.id = p.customer ORDER BY p.amount") == [("Alan", 5.25), ("Ada", 10.5), ("Grace", 100.0)]
    with pytest.raises(OperationalError, match="view"):
        shop.execute("INSERT INTO paid VALUES (1, 1)")
    shop.execute("DROP VIEW paid")
    with pytest.raises(OperationalError, match="no such table"):
        shop.execute("SELECT * FROM paid")


def test_expressions_and_functions(con):
    q = lambda sql: con.execute(sql).fetchall()[0][0]                                           # noqa: E731
    assert q("SELECT 7 / 2") == 3 and q("SELECT 7 / 2.0") == 3.5 and q("SELECT -7 % 3") == -1 and q("SELECT 1 / 0") is None
    assert q("SELECT '5' + 3") == 8 and q("SELECT '5' || 3") == "53" and q("SELECT 'abc' + 1") == 1
    assert q("SELECT 1 < 2.5") == 1 and q("SELECT 'a' < 'b'") == 1 and q("SELECT 1 < 'a'") == 1 and q("SELECT NULL = NULL") is None and q("SELECT NULL IS NULL") == 1
    assert q("SELECT 5 BETWEEN 1 AND 10") == 1 and q("SELECT 3 IN (1, 2, 3)") == 1 and q("SELECT 3 NOT IN (1, NULL)") is None
    assert q("SELECT CASE WHEN 1 > 2 THEN 'a' WHEN 2 > 1 THEN 'b' END") == "b" and q("SELECT CASE 3 WHEN 1 THEN 'x' WHEN 3 THEN 'y' ELSE 'z' END") == "y"
    assert q("SELECT 'Hello' LIKE 'h%'") == 1 and q("SELECT 'Hello' GLOB 'h*'") == 0 and q("SELECT 'a_c' LIKE 'a\\_c' ESCAPE '\\'") == 1
    assert q("SELECT abs(-3)") == 3 and q("SELECT length('héllo')") == 5 and q("SELECT substr('hello', 2, 3)") == "ell" and q("SELECT substr('hello', -3)") == "llo"
    assert q("SELECT upper('abc') || lower('DEF')") == "ABCdef" and q("SELECT trim('  x  ')") == "x" and q("SELECT replace('banana', 'an', 'AN')") == "bANANa"
    assert q("SELECT instr('hello', 'll')") == 3 and q("SELECT hex('A')") == "41" and q("SELECT typeof(1.0)") == "real" and q("SELECT coalesce(NULL, NULL, 3)") == 3
    assert q("SELECT round(2.5)") == 3.0 and q("SELECT round(3.14159, 2)") == 3.14 and q("SELECT nullif(1, 1)") is None and q("SELECT ifnull(NULL, 'x')") == "x"
    assert q("SELECT CAST('12abc' AS INTEGER)") == 12 and q("SELECT CAST(3.9 AS INTEGER)") == 3 and q("SELECT CAST(7 AS TEXT)") == "7" and q("SELECT CAST('1e2' AS REAL)") == 100.0
    assert q("SELECT min(3, 1, 2)") == 1 and q("SELECT max(3, 1, 2)") == 3 and q("SELECT quote('it''s')") == "'it''s'" and q("SELECT quote(x'ff')") == "X'FF'"
    assert q("SELECT 9223372036854775807 + 1") == 9.223372036854776e18 and q("SELECT 1 << 3") == 8 and q("SELECT ~5") == -6 and q("SELECT 6 & 3") == 2
    assert q("SELECT printf('%05.1f|%d|%s', 3.14159, 42, 'x')") == "003.1|42|x"


def test_type_affinity_decides_what_is_stored_and_how_comparisons_go(con):
    con.execute("CREATE TABLE t (i INTEGER, r REAL, t TEXT, n NUMERIC, b BLOB)")
    con.execute("INSERT INTO t VALUES ('42', '1.5', 12, '3.0', '7')")
    assert rows(con, "SELECT typeof(i), typeof(r), typeof(t), typeof(n), typeof(b) FROM t") == [("integer", "real", "text", "integer", "text")]
    assert rows(con, "SELECT i = '42', t = 12, t = '12', n = 3, b = 7, b = '7' FROM t") == [(1, 1, 1, 1, 0, 1)]
    con.execute("INSERT INTO t (i) VALUES ('abc')")
    assert rows(con, "SELECT typeof(i) FROM t ORDER BY rowid") == [("integer",), ("text",)]


def test_null_ordering_and_collations(con):
    con.execute("CREATE TABLE t (a, n TEXT COLLATE NOCASE)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [(3, "b"), (None, "B"), (1, "a"), (None, "A"), ("x", "c")])
    assert rows(con, "SELECT a FROM t ORDER BY a") == [(None,), (None,), (1,), (3,), ("x",)]
    assert rows(con, "SELECT a FROM t ORDER BY a DESC") == [("x",), (3,), (1,), (None,), (None,)]
    assert rows(con, "SELECT a FROM t ORDER BY a NULLS LAST LIMIT 2") == [(1,), (3,)]
    assert rows(con, "SELECT count(*) FROM t WHERE n = 'A'") == [(2,)] and rows(con, "SELECT count(*) FROM t WHERE n = 'A' COLLATE BINARY") == [(1,)]
    assert rows(con, "SELECT n FROM t ORDER BY n, a") == [("A",), ("a",), ("B",), ("b",), ("c",)]              # ties under NOCASE are broken by a, NULL first


def test_collation_in_comparisons_distinct_aggregates_and_compounds(con):
    con.execute("CREATE TABLE t (n TEXT COLLATE NOCASE, r TEXT)")
    con.executemany("INSERT INTO t VALUES (?, ?)", [("b", "b"), ("B", "B"), ("a", "a"), ("A", "A"), ("c", "c")])
    assert rows(con, "SELECT count(*) FROM t WHERE n = 'A'") == [(2,)]
    assert rows(con, "SELECT count(*) FROM t WHERE n = 'A' COLLATE BINARY") == [(1,)]                      # an explicit COLLATE beats the column's, on either side
    assert rows(con, "SELECT count(*) FROM t WHERE 'A' COLLATE BINARY = n") == [(1,)]
    assert rows(con, "SELECT n FROM t GROUP BY n ORDER BY n") == [("a",), ("b",), ("c",)]
    assert rows(con, "SELECT DISTINCT n FROM t") == [("b",), ("a",), ("c",)]                               # of equal rows the first is kept
    assert rows(con, "SELECT count(DISTINCT n), count(DISTINCT r) FROM t") == [(3, 5)]
    assert rows(con, "SELECT min(n), max(n), min(r), max(r) FROM t") == [("a", "c", "A", "c")]
    assert rows(con, "SELECT n FROM t UNION SELECT 'A'") == [("A",), ("B",), ("c",)]                       # a compound select keeps the last of equal rows
    assert rows(con, "SELECT n FROM t EXCEPT SELECT 'B'") == [("A",), ("c",)]
    assert rows(con, "SELECT n FROM t INTERSECT SELECT 'B'") == [("B",)]
    assert rows(con, "SELECT n FROM t UNION ALL SELECT 'A' ORDER BY 1") == [("a",), ("A",), ("A",), ("b",), ("B",), ("c",)]
    assert rows(con, "SELECT 0 UNION SELECT 0.0") == [(0.0,)] and rows(con, "SELECT 0.0 UNION SELECT 0") == [(0,)]
    assert rows(con, "VALUES (1) UNION SELECT 1") == [(1,)]


# --- the planner ---------------------------------------------------------------------------------------------------------------------------------
def plan(con, sql):
    return [r[3] for r in con.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]


def test_the_planner_uses_the_rowid_and_indexes(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, c TEXT)")
    con.execute("CREATE INDEX t_ab ON t (a, b)")
    con.execute("CREATE INDEX t_c ON t (c)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?)", [(i, i % 20, i % 7, "c%d" % (i % 30)) for i in range(1, 501)])
    assert plan(con, "SELECT * FROM t WHERE id = 5") == ["SEARCH t USING INTEGER PRIMARY KEY (rowid=?)"]
    assert plan(con, "SELECT * FROM t WHERE id > 5 AND id < 9") == ["SEARCH t USING INTEGER PRIMARY KEY (rowid>?)"]
    assert "t_ab" in plan(con, "SELECT * FROM t WHERE a = 3")[0] and "(a=? AND b=?)" in plan(con, "SELECT * FROM t WHERE a = 3 AND b = 2")[0]
    assert "(a=? AND b>?)" in plan(con, "SELECT * FROM t WHERE a = 3 AND b > 2")[0]
    assert "t_c" in plan(con, "SELECT * FROM t WHERE c = 'c3'")[0]
    assert plan(con, "SELECT * FROM t WHERE b = 2") == ["SCAN t"] and plan(con, "SELECT * FROM t WHERE +a = 3") == ["SCAN t"]
    assert plan(con, "SELECT * FROM t WHERE c = 5")[0].startswith("SEARCH") and plan(con, "SELECT * FROM t WHERE a = '3'")[0].startswith("SEARCH")
    inner = plan(con, "SELECT x.id FROM t x JOIN t y ON y.id = x.a")
    assert inner == ["SCAN t", "SEARCH t USING INTEGER PRIMARY KEY (rowid=?)"]
    # and the answers are the ones a plain scan gives
    assert rows(con, "SELECT count(*) FROM t WHERE a = 3 AND b > 2") == rows(con, "SELECT count(*) FROM t WHERE +a = 3 AND +b > 2")
    assert rows(con, "SELECT id FROM t WHERE a = '3' AND b = 3") == rows(con, "SELECT id FROM t WHERE +a = 3 AND +b = 3")


# --- schema changes -----------------------------------------------------------------------------------------------------------------------------------
def test_create_and_drop(con):
    con.execute("CREATE TABLE t (a, b)")
    con.execute("CREATE INDEX t_a ON t (a)")
    with pytest.raises(OperationalError, match="already exists"):
        con.execute("CREATE TABLE t (x)")
    con.execute("CREATE TABLE IF NOT EXISTS t (x)")
    with pytest.raises(OperationalError, match="no such table"):
        con.execute("SELECT * FROM nope")
    with pytest.raises(OperationalError, match="no such column"):
        con.execute("SELECT zzz FROM t")
    con.execute("DROP INDEX t_a")
    con.execute("DROP TABLE t")
    con.execute("DROP TABLE IF EXISTS t")
    assert rows(con, "SELECT name FROM sqlite_master") == []
    con.execute("CREATE TABLE copy AS SELECT 1 AS one, 'x' AS two")
    assert rows(con, "SELECT * FROM copy") == [(1, "x")]


def test_alter_table(con):
    con.execute("CREATE TABLE t (a, b)")
    con.execute("CREATE INDEX t_a ON t (a)")
    con.execute("INSERT INTO t VALUES (1, 2), (3, 4)")
    con.execute("ALTER TABLE t ADD COLUMN c TEXT DEFAULT 'new'")
    assert rows(con, "SELECT * FROM t ORDER BY a") == [(1, 2, "new"), (3, 4, "new")]
    con.execute("INSERT INTO t VALUES (5, 6, 'old')")
    con.execute("ALTER TABLE t RENAME TO renamed")
    assert rows(con, "SELECT c FROM renamed ORDER BY a") == [("new",), ("new",), ("old",)]
    assert rows(con, "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'") == [("t_a", "renamed")]
    with pytest.raises(OperationalError):
        con.execute("SELECT * FROM t")
    with pytest.raises(OperationalError, match="NOT NULL"):
        con.execute("ALTER TABLE renamed ADD COLUMN d NOT NULL")


def test_pragmas(con):
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT 'x', v REAL)")
    assert rows(con, "PRAGMA table_info(t)") == [(0, "id", "INTEGER", 0, None, 1), (1, "name", "TEXT", 1, "'x'", 0), (2, "v", "REAL", 0, None, 0)]
    con.execute("CREATE UNIQUE INDEX t_name ON t (name)")
    assert rows(con, "PRAGMA index_list(t)") == [(0, "t_name", 1, "c", 0)] and rows(con, "PRAGMA index_info(t_name)") == [(0, 1, "name")]
    con.execute("PRAGMA user_version = 7")
    assert rows(con, "PRAGMA user_version") == [(7,)] and rows(con, "PRAGMA page_size") == [(4096,)] and rows(con, "PRAGMA integrity_check") == [("ok",)]
    assert rows(con, "PRAGMA freelist_count") == [(0,)] and rows(con, "PRAGMA encoding") == [("UTF-8",)]


# --- transactions and persistence ------------------------------------------------------------------------------------------------------------------------
def test_commit_and_rollback(path):
    con = kendi_db.connect(path)
    con.execute("CREATE TABLE t (a)")
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (1)")
    con.execute("INSERT INTO t VALUES (2)")
    assert con.in_transaction
    con.execute("ROLLBACK")
    assert rows(con, "SELECT count(*) FROM t") == [(0,)]
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (3)")
    con.execute("COMMIT")
    con.execute("BEGIN")
    con.execute("CREATE TABLE u (x)")                                                       # schema changes are transactional too
    con.execute("DROP TABLE t")
    con.execute("ROLLBACK")
    assert rows(con, "SELECT a FROM t") == [(3,)]
    with pytest.raises(OperationalError):
        con.execute("SELECT * FROM u")
    con.close()
    assert sqlite3.connect(path).execute("SELECT a FROM t").fetchall() == [(3,)]


def test_a_failing_statement_inside_a_transaction_is_undone_alone(path):
    con = kendi_db.connect(path)
    con.execute("CREATE TABLE t (a INTEGER PRIMARY KEY, b UNIQUE)")
    con.execute("BEGIN")
    con.execute("INSERT INTO t VALUES (1, 'x')")
    with pytest.raises(IntegrityError):
        con.execute("INSERT INTO t VALUES (2, 'y'), (3, 'x')")                              # (2, 'y') goes in, (3, 'x') fails: both are undone
    assert rows(con, "SELECT * FROM t") == [(1, "x")]
    with pytest.raises(OperationalError):
        con.execute("UPDATE t SET b = nosuchcolumn")
    con.execute("INSERT INTO t VALUES (4, 'z')")
    con.execute("COMMIT")
    assert rows(con, "SELECT * FROM t ORDER BY a") == [(1, "x"), (4, "z")]
    con.close()
    assert sqlite3.connect(path).execute("SELECT * FROM t ORDER BY a").fetchall() == [(1, "x"), (4, "z")]


def test_a_failing_statement_in_autocommit_mode_changes_nothing(con):
    con.execute("CREATE TABLE t (a UNIQUE)")
    con.execute("INSERT INTO t VALUES (1), (2)")
    with pytest.raises(IntegrityError):
        con.execute("UPDATE t SET a = 5")                                                  # the first row becomes 5, the second then collides
    assert rows(con, "SELECT a FROM t ORDER BY a") == [(1,), (2,)]
    with pytest.raises(IntegrityError):
        con.execute("INSERT INTO t SELECT 3 UNION ALL SELECT 1")
    assert rows(con, "SELECT count(*) FROM t") == [(2,)]


def test_data_persists_across_connections_and_sqlite_reads_it(path):
    con = kendi_db.connect(path)
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
    con.execute("CREATE INDEX t_body ON t (body)")
    con.executemany("INSERT INTO t (body) VALUES (?)", [("row %d " % i + "x" * (i % 40) * 20,) for i in range(300)])
    con.close()
    again = kendi_db.connect(path)
    assert rows(again, "SELECT count(*), max(length(body)) FROM t") == [(300, 788)]
    again.execute("DELETE FROM t WHERE id % 3 = 0")
    again.close()
    ref = sqlite3.connect(path)
    assert ref.execute("SELECT count(*) FROM t").fetchone() == (200,) and ref.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    ref.execute("INSERT INTO t (body) VALUES ('from sqlite')")
    ref.commit()
    ref.close()
    assert rows(kendi_db.connect(path), "SELECT body FROM t WHERE body = 'from sqlite'") == [("from sqlite",)]


def test_two_connections_see_each_others_committed_changes(path):
    a, b = kendi_db.connect(path), kendi_db.connect(path)
    a.execute("CREATE TABLE t (x)")
    a.execute("INSERT INTO t VALUES (1)")
    assert rows(b, "SELECT x FROM t") == [(1,)]
    a.execute("INSERT INTO t VALUES (2)")
    a.execute("CREATE TABLE u (y)")
    assert rows(b, "SELECT count(*) FROM t") == [(2,)] and rows(b, "SELECT count(*) FROM u") == [(0,)]
    a.execute("BEGIN")
    a.execute("INSERT INTO t VALUES (3)")
    with pytest.raises(OperationalError, match="locked"):
        b.execute("INSERT INTO t VALUES (9)")
    with pytest.raises(OperationalError, match="locked"):
        b.execute("SELECT * FROM t")
    a.execute("COMMIT")
    b.execute("INSERT INTO t VALUES (4)")
    assert rows(a, "SELECT x FROM t ORDER BY x") == [(1,), (2,), (3,), (4,)]
    a.close()
    b.close()


# --- mistakes ---------------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sql", ["SELEC 1", "SELECT FROM", "SELECT 1 +", "SELECT (1", "INSERT INTO", "CREATE TABLE t (", "SELECT 1 FROM", "SELECT 'abc", "SELECT 1; SELECT 2",
                                 "UPDATE t SET", "SELECT * FROM t WHERE", "SELECT 1 2", "DROP", "SELECT @"])
def test_syntax_errors(con, sql):
    con.execute("CREATE TABLE t (a)")
    with pytest.raises(SQLSyntaxError):
        con.execute(sql)


def test_semantic_errors(con):
    con.execute("CREATE TABLE t (a, b)")
    con.execute("CREATE TABLE u (a, c)")
    for sql, message in [("SELECT a FROM t, u", "ambiguous"), ("SELECT nosuch FROM t", "no such column"), ("SELECT * FROM nosuch", "no such table"), ("SELECT abs()", "wrong number"),
                         ("SELECT nosuchfn(1)", "no such function"), ("SELECT sum(sum(a)) FROM t", "misuse of aggregate"), ("SELECT * FROM t WHERE sum(a) > 1", "misuse of aggregate"),
                         ("INSERT INTO t VALUES (1)", "values"), ("INSERT INTO t (zzz) VALUES (1)", "no column named"), ("SELECT 1 FROM t ORDER BY 3", "ORDER BY term out of range"),
                         ("SELECT (SELECT a, b FROM t)", "sub-select returns 2 columns"), ("SELECT 1 UNION SELECT 1, 2", "do not have the same number"),
                         ("CREATE TABLE x (a, a)", "duplicate column"), ("DROP TABLE nosuch", "no such table"), ("CREATE INDEX i ON t (zzz)", "no column"),
                         ("UPDATE nosuch SET a = 1", "no such table"), ("DELETE FROM nosuch", "no such table"), ("SELECT 5 LIMIT 'abc'", "datatype mismatch")]:
        with pytest.raises(DatabaseError, match=message):
            con.execute(sql)
    assert rows(con, "SELECT 1 FROM t, u WHERE t.a = u.a") == []
    with pytest.raises(OperationalError, match="may not be modified"):
        con.execute("DELETE FROM sqlite_master")


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    p = tmp_path / "junk.db"
    p.write_bytes(b"not a database" * 100)
    with pytest.raises(kendi_db.CorruptDatabase):
        kendi_db.connect(str(p))


def test_the_engine_does_not_use_the_sqlite3_module():
    """SQLite is the oracle of the tests; the engine itself is written from scratch."""
    import ast
    import pathlib
    package = pathlib.Path(kendi_db.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            assert not any(n and n.split(".")[0] == "sqlite3" for n in names), source.name
