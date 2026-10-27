# sql-database-from-scratch

A SQL database engine written from scratch in pure Python, whose files are **real SQLite database files**: a pager with a rollback journal, B-trees for tables and indexes, a SQL parser, a query planner and an executor. Nothing in it uses the `sqlite3` module; SQLite is the **oracle**: it opens every file this engine writes and runs its own integrity check, it answers the same random SQL for comparison, and it rolls back this engine's interrupted commits (and the other way round).

```python
import kendi_db

con = kendi_db.connect("people.db")
con.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT NOT NULL, born INTEGER)")
con.executemany("INSERT INTO people (name, born) VALUES (?, ?)", [("Ada", 1815), ("Alan", 1912), ("Grace", 1906)])
print(con.execute("SELECT name FROM people WHERE born > ? ORDER BY born", (1900,)).fetchall())    # [('Grace',), ('Alan',)]
con.close()

import sqlite3                                       # the real thing reads the same file
print(sqlite3.connect("people.db").execute("PRAGMA integrity_check").fetchall())                 # [('ok',)]
```

```
$ pip install -e .
$ ksql people.db "SELECT count(*) FROM people"
3
$ ksql people.db
ksql> .tables
people
ksql> EXPLAIN QUERY PLAN SELECT * FROM people WHERE id = 2;
2|0|0|SEARCH people USING INTEGER PRIMARY KEY (rowid=?)
```

Python 3.9+, no dependencies. The interface follows `sqlite3`: `connect`, `Connection.execute / executemany / executescript / commit / rollback`, `Cursor.fetchone / fetchmany / fetchall / description / rowcount / lastrowid`, `?`, `?NNN`, `:name` parameters, and `ksql` is a small `sqlite3`-style shell (`.tables .schema .indexes .dump .headers .mode .integrity .read`).

## What it does

**Storage (SQLite file format 3, rollback-journal mode)**

- The 100-byte header (page size 512 to 65536, change counter, free-list, schema cookie, user version, ...), the schema table on page 1, table B-trees (rowid keys) and index B-trees (record keys), cells with overflow chains for large rows (the exact local-payload formulas), the free-page list (trunk and leaf pages), the pending-byte page, `sqlite_sequence` for `AUTOINCREMENT`, the `sqlite_autoindex_*` indexes of `UNIQUE` / `PRIMARY KEY` constraints.
- **Atomic commit** the way SQLite does it: the original contents of the pages about to change go to `<file>-journal` (SQLite's journal format, with checksums) and are made durable, then the new pages are written and made durable, then the journal is deleted. A crash at any point leaves a "hot" journal that the next opener, **this engine or SQLite**, plays back.
- Changes of a transaction stay in memory until the commit, so a `ROLLBACK` is forgetting them; a statement that fails inside a transaction is undone on its own (a savepoint per statement) and the transaction goes on.
- Locks: shared while reading, exclusive while writing, held only for the duration of a transaction.

**SQL**

- `CREATE / DROP TABLE / INDEX / VIEW` (`IF [NOT] EXISTS`, `CREATE TABLE ... AS SELECT`, `UNIQUE`, partial indexes, `DESC`, per-column collations), `ALTER TABLE ... RENAME TO / ADD COLUMN`, `INSERT` (multi-row, `INSERT ... SELECT`, `DEFAULT VALUES`, `OR IGNORE / REPLACE / ABORT / ROLLBACK / FAIL`), `UPDATE`, `DELETE`, `BEGIN / COMMIT / ROLLBACK`, `EXPLAIN QUERY PLAN`, `ANALYZE` (accepted), and the `PRAGMA`s `table_info`, `index_list`, `index_info`, `integrity_check`, `page_size`, `user_version`, `freelist_count`, `encoding`, ...
- `SELECT` with joins (`INNER`, `LEFT`, `CROSS`, `NATURAL`, `USING`), `WHERE`, `GROUP BY` / `HAVING`, `DISTINCT`, `ORDER BY` (`NULLS FIRST / LAST`, collations), `LIMIT / OFFSET`, subqueries (scalar, `IN`, `EXISTS`, correlated, in `FROM`), `WITH` (non-recursive), `UNION [ALL] / INTERSECT / EXCEPT`, `VALUES`, views.
- The type system of SQLite: storage classes and their order, **column affinity** from the declared type, the comparison rules (which side's affinity is applied), integer overflow to real, `-0.0`, `NaN` becoming `NULL`, three-valued logic, **collations** (`BINARY`, `NOCASE`, `RTRIM`) with SQLite's propagation rules.
- Constraints: `NOT NULL`, `UNIQUE`, `PRIMARY KEY`, `CHECK`, `DEFAULT`, `INTEGER PRIMARY KEY` as the rowid alias, `AUTOINCREMENT`.
- About 40 functions: `abs`, `length`, `substr`, `instr`, `replace`, `trim`, `upper`, `lower`, `round`, `coalesce`, `nullif`, `iif`, `typeof`, `cast`, `hex`, `quote`, `printf`, `char`, `unicode`, `zeroblob`, `random`, `min / max` (both forms), `count`, `sum`, `total`, `avg`, `group_concat`, `LIKE` (with `ESCAPE`), `GLOB`, ...
- A planner that picks the rowid, a single-column or multi-column index (equality prefix plus a range), per table of a join, and says so in `EXPLAIN QUERY PLAN`.

## Layout

| Module | Role |
|---|---|
| `format.py`, `node.py` | The byte-level file format: varints, records (serial types), B-tree page encode/decode, cells |
| `pager.py` | Pages, cache, free list, transactions, statement savepoints, journal write and play-back, locks |
| `btree.py` | `TableTree` and `IndexTree`: search, insert with multi-way splits (the root keeps its page number), delete with merging, scans, overflow chains |
| `lexer.py`, `parser.py`, `sqlast.py` | SQL text to a syntax tree (precedence climbing for expressions) |
| `values.py`, `functions.py` | Storage classes, affinity, comparison, `CAST`, `LIKE`, the scalar and aggregate functions |
| `exprs.py`, `select.py` | Expressions compiled to closures; the query compiler: scopes, join order, access paths, aggregation, compound selects |
| `schema.py`, `tables.py`, `database.py` | The schema table, rows to records, constraint checks, `INSERT / UPDATE / DELETE`, DDL, transactions |
| `integrity.py`, `pragmas.py`, `alter.py` | `PRAGMA integrity_check` (every page accounted for, every index against its table), the other pragmas, `ALTER TABLE` |
| `connection.py`, `shell.py` | The `sqlite3`-shaped Python interface and `ksql` |

## How it was checked

There is no mock anywhere: the oracle is the real SQLite library, through Python's `sqlite3` module, used only by the tests and the benchmark.

| Check | What | Result |
|---|---|---|
| **SQLite reads our files** | After table/index workloads (random inserts, deletes, overflow records, 512 to 65 536 byte pages) the file is opened by SQLite, whose `PRAGMA integrity_check` verifies page types, key order, depths, the free list and every index against its table | `ok` in every test |
| **We read SQLite's files** | Databases built by SQLite (all page sizes, indexes, views, `AUTOINCREMENT`, quoted names, defaults, a `user_version`) are queried by both engines and the answers compared | identical |
| **Taking turns** | The two engines insert and delete in the same file alternately; each time the other's integrity check and the contents are compared | identical |
| **Crash recovery** | A commit is stopped (`SimulatedCrash`) after the journal, after page 1, 3, 5, 9 or after the database sync, for statements of every kind (`DELETE`, `UPDATE`, `INSERT ... SELECT`, `CREATE TABLE AS`, `DROP INDEX`) and for an explicit transaction; then **this engine** and, in another run, **SQLite** open the file | old data back, integrity `ok`, journal gone, both ways |
| **Differential fuzzing** | Seeded random schemas (all type names, `UNIQUE`, `NOT NULL`, `DEFAULT`, indexes), data (integers, reals, numeric-looking text, blobs, `NULL`) and statements (every operator and function, joins, aggregates, subqueries, compound selects, `INSERT / UPDATE / DELETE`); rows of every query and the state of every table after every change are compared with SQLite, then SQLite checks the file. A second generator adds columns with `COLLATE NOCASE / RTRIM / BINARY` and `COLLATE` inside expressions | 280 seeds in the test suite; about 400 000 comparisons over the development |
| **Hand-written expectations** | 44 tests that spell out the answers: DDL, constraints, conflict clauses, transactions, joins, subqueries, compound selects, views, functions and quirks, affinity, collations, parameters, the planner's choices (`EXPLAIN QUERY PLAN`), `ALTER`, pragmas, error messages, two connections | |
| **Corruption** | A row missing from an index, an index entry without a row, a leaked page, a wrong free-list count, a page used twice, a page overwritten, a truncated file are all reported by `PRAGMA integrity_check` (and SQLite agrees the file is damaged) | |
| **`ksql`** | The real command line as a subprocess: statements as arguments and on standard input, dot commands, `.dump` loaded into SQLite and back, exit status | |

182 tests (`pytest -q`, about 75 s on an old MacBook), run on Python 3.9 / SQLite 3.39 and on Python 3.13 / SQLite 3.50.

**What the oracle found in this engine** (each is now a test): a permanent file lock that made SQLite answer "database is locked"; a statement savepoint that lost pages reused from the free list; **a B-tree bug**: when deletions left a non-root interior page with a single child, it was replaced by that child, which puts the leaves of the tree at different depths (a bulk delete in descending order of a 300-row index at page size 512 corrupted it; found by the SQL tests, and the new bulk-delete storage test fails 15 of its 20 cases without the fix, the page is now merged with a neighbour); and then a long list of SQL semantics that the fuzzer, not the documentation, taught: which operand's affinity applies in a comparison and what an `IN` list does with it, a unary `+` that strips the affinity but not the collation, text-to-number conversion rules, `sum()` with Kahan summation and its overflow error, integer-versus-real tie rules of `min` / `max`, `substr` / `trim` / `replace` / `round` / `unicode` / `LIKE` on values with a NUL, and **collations**: an explicit `COLLATE` wins on either side of a comparison and travels up through functions and `CASE` (but not out of `BETWEEN` bounds), every column has one (`BINARY` if none was declared, so a plain column on the left beats a `NOCASE` column on the right), derived tables and views keep their columns' collations, `DISTINCT` / `GROUP BY` / `min` / `max` / `count(DISTINCT)` use them, a compound select uses those of its first select that has one, `NOCASE` stops comparing at a NUL.

**Known differences** (the comparison tolerates them, each is pinned or explained in `tests/`):

- Which of several rows that compare equal is returned by `DISTINCT`, a compound select or `GROUP BY` (`0` and `0.0`; `'abc'` and `'ABC'` under `NOCASE`), the order of ties in `ORDER BY`, and the order inside `group_concat` are implementation choices. This engine keeps the first for `DISTINCT` / `GROUP BY` and the last for compound selects, as SQLite does in the cases checked; with an `ORDER BY` on a compound select SQLite sometimes chooses otherwise.
- `quote()` of a real that needs more than 15 digits.
- `VALUES ... UNION ... ORDER BY` is accepted here, a syntax error in SQLite.
- Older SQLite libraries differ from 3.50 in `substr()` with huge or fractional arguments and in `replace()` with an empty pattern (they return the value itself rather than text); the differential tests skip those statements when the SQLite behind Python is older than 3.50.

## Numbers

Pure Python against the C library, one connection each, an old Intel MacBook, Python 3.13, 20 000 rows (`python benchmarks/bench.py`):

| workload | kendi_db | sqlite3 | ratio |
|---|---|---|---|
| insert 20 000 rows in one transaction (with an index) | 3 852 ms | 47 ms | 81× |
| 2 000 look-ups by primary key | 398 ms | 32 ms | 12× |
| 300 queries through the age index | 1 383 ms | 29 ms | 48× |
| full scan: count, avg, min, max | 208 ms | 2.5 ms | 82× |
| group by city, order by avg score | 148 ms | 8.6 ms | 17× |
| join with a small table, group by | 571 ms | 11 ms | 54× |
| sort all rows by an unindexed column, first 10 | 141 ms | 1.6 ms | 90× |
| update a third of the rows (indexed column) | 1 661 ms | 15 ms | 109× |
| delete half of the rows | 495 ms | 13 ms | 37× |
| 50 single-row inserts, each its own commit | 93 ms | 53 ms | 2× |

A Python engine is 10 to 100 times slower than the C one; the last row shows why that matters little for durable single-row writes, where both wait for the disk. The file it writes is about 9 % larger than SQLite's for the same data (1 000 KB against 920 KB), because splits are simpler.

## Limits (on purpose)

- Not supported, and reported as errors rather than mishandled: triggers (a table that has one can be read, any change to it is refused, so a trigger is never silently skipped), `WITHOUT ROWID` tables (reading one is an error), recursive CTEs, window functions, `RETURNING`, upserts (`ON CONFLICT DO UPDATE`), `ATTACH`, `VACUUM`, temporary tables, `RIGHT` / `FULL JOIN`, row values, expression indexes, generated columns, `ALTER TABLE ... DROP / RENAME COLUMN`, `SAVEPOINT`, `UPDATE ... FROM`, JSON and date functions, the math functions. Foreign keys are parsed and, as in SQLite's default, not enforced.
- UTF-8 only; the write-ahead log mode is not implemented (a file in WAL mode is refused).
- One writer at a time among processes that use this engine (advisory `flock`). SQLite takes its own, different kind of locks, so the two should not write to the same file at the same moment: take turns, as the tests do.
- The page cache is the working set of the transaction: a transaction that changes more pages than fit in memory is not handled.
- Slow compared with C, see above.

## Development

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
python benchmarks/bench.py [rows]
```

MIT licensed.
