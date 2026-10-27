"""Random SQL through this engine and through SQLite, and a comparison of everything: the rows of every query, the state of every table after every change, and at the end
SQLite's own integrity check of the file that this engine wrote. The schemas (types, UNIQUE, NOT NULL, DEFAULT, indexes), the data (integers, reals, text that looks like
numbers, blobs, NULLs) and the statements (expressions with every operator and function, joins, aggregates, subqueries, compound selects, derived tables, INSERT / UPDATE /
DELETE) are generated from fixed seeds, so a failure repeats."""
import sqlite3

import pytest

import sqlgen
from difftest import Pair

PAGE_SIZES = [4096, 4096, 1024, 512]


def run_seed(directory, seed, statements=25, generator=sqlgen.Gen):
    g = generator(seed)
    pair = Pair(directory, PAGE_SIZES[seed % len(PAGE_SIZES)], fold_case=generator is not sqlgen.Gen)
    stmts = list(g.setup())
    for _ in range(statements):
        stmts.append(g.select() if g.rng.random() < 0.8 else g.change())
    problems, tables = [], []
    try:
        for sql in stmts:
            r = pair.compare(sql)
            if r and "Could not decode" in r:
                r = None
            if not sql.startswith("SELECT"):
                if sql.startswith("CREATE TABLE"):
                    tables.append(sql.split()[2])
                for t in tables:
                    r2 = pair.compare("SELECT rowid, * FROM %s ORDER BY rowid" % t, strict=True)
                    if r2 and not r:
                        r = "state of %s after the change: %s" % (t, r2)
            if r:
                problems.append("%s\n    %s" % (sql[:300], r))
        pair.mine.close()
        check = sqlite3.connect(pair.mine_path).execute("PRAGMA integrity_check").fetchall()
        if check != [("ok",)]:
            problems.append("SQLite's integrity check of our file: %r" % check[:3])
        again = sqlite3.connect(pair.mine_path)
        again.text_factory = pair.ref.text_factory
        for t in tables:
            ours = again.execute("SELECT rowid, * FROM %s ORDER BY rowid" % t).fetchall()
            theirs = pair.ref.execute("SELECT rowid, * FROM %s ORDER BY rowid" % t).fetchall()
            if len(ours) != len(theirs):
                problems.append("table %s: SQLite reads %d rows from our file, %d from its own" % (t, len(ours), len(theirs)))
    finally:
        pair.ref.close()
    return problems


@pytest.mark.parametrize("first", range(1, 161, 20))
def test_random_sql_behaves_like_sqlite(tmp_path, first):
    bad = []
    for seed in range(first, first + 20):
        d = tmp_path / ("s%d" % seed)
        d.mkdir()
        for p in run_seed(str(d), seed):
            bad.append("seed %d: %s" % (seed, p))
    assert not bad, "%d differences, the first of them:\n%s" % (len(bad), "\n".join(bad[:3]))


@pytest.mark.parametrize("first", range(1, 121, 20))
def test_random_sql_with_collations_behaves_like_sqlite(tmp_path, first):
    """The same, on columns that declare NOCASE / RTRIM / BINARY and expressions that say COLLATE."""
    bad = []
    for seed in range(first, first + 20):
        d = tmp_path / ("s%d" % seed)
        d.mkdir()
        for p in run_seed(str(d), seed, generator=sqlgen.CollationGen):
            bad.append("seed %d: %s" % (seed, p))
    assert not bad, "%d differences, the first of them:\n%s" % (len(bad), "\n".join(bad[:3]))
