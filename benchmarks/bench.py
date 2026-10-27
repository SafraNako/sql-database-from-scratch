"""kendi_db against SQLite (the C library, through the standard ``sqlite3`` module) on the same workload, on the same kind of file. Run: ``python benchmarks/bench.py [rows]``."""
import os
import random
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import kendi_db  # noqa: E402

ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000


def timed(fn):
    start = time.perf_counter()
    result = fn()
    return time.perf_counter() - start, result


def workload(con, path):
    rnd = random.Random(7)
    out = {}
    con.execute("CREATE TABLE person (id INTEGER PRIMARY KEY, name TEXT, age INTEGER, city TEXT, score REAL)")
    con.execute("CREATE INDEX person_age ON person (age)")
    con.execute("CREATE TABLE city (name TEXT PRIMARY KEY, country TEXT)")
    cities = ["city%02d" % i for i in range(50)]
    con.executemany("INSERT INTO city VALUES (?, ?)", [(c, "country%d" % (i % 7)) for i, c in enumerate(cities)])
    rows = [(i, "name%06d" % rnd.randrange(10**6), rnd.randrange(18, 90), rnd.choice(cities), rnd.random() * 100) for i in range(1, ROWS + 1)]

    def insert():
        con.execute("BEGIN")
        con.executemany("INSERT INTO person VALUES (?, ?, ?, ?, ?)", rows)
        con.execute("COMMIT")
    out["insert %d rows in one transaction" % ROWS] = timed(insert)[0]

    def point():
        for k in range(2000):
            con.execute("SELECT name FROM person WHERE id = ?", (rnd.randrange(1, ROWS + 1),)).fetchall()
    out["2000 look-ups by primary key"] = timed(point)[0]

    def by_index():
        for k in range(300):
            con.execute("SELECT count(*), avg(score) FROM person WHERE age = ?", (rnd.randrange(18, 90),)).fetchall()
    out["300 queries through the age index"] = timed(by_index)[0]
    out["full scan: count, avg, min, max"] = timed(lambda: con.execute("SELECT count(*), avg(score), min(name), max(age) FROM person").fetchall())[0]
    out["group by city, order by avg score"] = timed(lambda: con.execute("SELECT city, count(*), avg(score) FROM person GROUP BY city ORDER BY 3 DESC").fetchall())[0]
    out["join with city, group by country"] = timed(lambda: con.execute("SELECT c.country, count(*) FROM person p JOIN city c ON c.name = p.city GROUP BY c.country").fetchall())[0]
    out["sort all rows by name (no index), first 10"] = timed(lambda: con.execute("SELECT id FROM person ORDER BY name LIMIT 10").fetchall())[0]

    def updates():
        con.execute("BEGIN")
        con.execute("UPDATE person SET age = age + 1 WHERE id % 3 = 0")
        con.execute("COMMIT")
    out["update a third of the rows (indexed column)"] = timed(updates)[0]

    def deletes():
        con.execute("BEGIN")
        con.execute("DELETE FROM person WHERE id % 2 = 0")
        con.execute("COMMIT")
    out["delete half of the rows"] = timed(deletes)[0]

    def autocommit():
        for k in range(50):
            con.execute("INSERT INTO city VALUES (?, ?)", ("extra%d" % k, "x"))                     # (autocommit: a journal, an fsync and a delete each time)
    out["50 single-row inserts, each its own commit"] = timed(autocommit)[0]
    return out


def main():
    results = {}
    for name in ("kendi_db", "sqlite3"):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "bench.db")
        con = kendi_db.connect(path) if name == "kendi_db" else sqlite3.connect(path, isolation_level=None)
        results[name] = workload(con, path)
        con.close()
        check = sqlite3.connect(path)
        assert check.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        count = check.execute("SELECT count(*) FROM person").fetchone()[0]
        results[name]["_size_kb"] = os.path.getsize(path) / 1024
        results[name]["_rows"] = count
        check.close()
    assert results["kendi_db"]["_rows"] == results["sqlite3"]["_rows"]
    print("| workload | kendi_db | sqlite3 | ratio |")
    print("|---|---|---|---|")
    for key in results["kendi_db"]:
        if key.startswith("_"):
            continue
        a, b = results["kendi_db"][key], results["sqlite3"][key]
        print("| %s | %.0f ms | %.1f ms | %.0fx |" % (key, a * 1000, b * 1000, a / b if b else float("inf")))
    print()
    print("file size: kendi_db %.0f KB, sqlite3 %.0f KB (both files pass SQLite's integrity check)" % (results["kendi_db"]["_size_kb"], results["sqlite3"]["_size_kb"]))


if __name__ == "__main__":
    main()
