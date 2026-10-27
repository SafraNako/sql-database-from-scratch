"""The storage layer on its own (no SQL): pager, free list, journal and B-trees, with real SQLite as the judge. Every file written here is opened by SQLite, which runs its
own integrity check on it (page structure, key order, free list, index against table); and files made by SQLite are read back with this code."""
import os
import random
import shutil
import sqlite3
import struct

import pytest

from kendi_db.btree import IndexTree, TableTree
from kendi_db.errors import SimulatedCrash
from kendi_db.format import decode_record, encode_record
from kendi_db.pager import Pager
from storage_util import Store, compare_prefix, sql_order_key, sqlite_check, sqlite_query


def random_value(rng, i):
    kind = rng.random()
    if kind < 0.25:
        return rng.randint(-10**12, 10**12)
    if kind < 0.45:
        return "s" * rng.randint(0, 60)
    if kind < 0.55:
        return "long" * rng.randint(100, 2500)                          # several overflow pages on small page sizes
    if kind < 0.65:
        return None
    if kind < 0.75:
        return rng.random() * 1000
    if kind < 0.85:
        return bytes(rng.randint(0, 255) for _ in range(rng.randint(0, 400)))
    return rng.choice(["", "x", 0, 1, -1, 2**40, "héllo wörld ∑"])


@pytest.mark.parametrize("page_size, seed", [(4096, 1), (4096, 2), (1024, 3), (512, 4), (512, 5), (65536, 6)])
def test_table_tree_random_operations_agree_with_a_dict_and_with_sqlite(tmp_path, page_size, seed):
    rng = random.Random(seed)
    path = str(tmp_path / "t.db")
    store = Store(path, page_size)
    tree = store.add_table("t", ["a", "b"])
    expected = {}
    for step in range(1, 2500):
        op = rng.random()
        if op < 0.6 or not expected:
            rid = rng.randint(1, 4000) if rng.random() < 0.7 else (max(expected, default=0) + 1)
            row = [rid, random_value(rng, rid)]
            tree.insert(rid, encode_record(row))
            expected[rid] = row
        else:
            rid = rng.choice(sorted(expected)) if rng.random() < 0.9 else rng.randint(1, 4000)
            assert tree.delete(rid) == (rid in expected)
            expected.pop(rid, None)
        if step % 500 == 0:                                                  # commit, close, open again: what is on disk is what is expected
            store.commit()
            store.close()
            store = Store.__new__(Store)
            store.pager = Pager(path)
            store.pager.begin()
            store.master = TableTree(store.pager, 1)
            tree = TableTree(store.pager, store.master.get(1) and decode_record(store.master.get(1))[3])
            assert [(k, decode_record(v)) for k, v in tree.scan()] == [(k, expected[k]) for k in sorted(expected)]
    keys = sorted(expected)
    assert [k for k, _ in tree.scan()] == keys
    assert [k for k, _ in tree.scan_reverse()] == keys[::-1]
    for _ in range(20):
        lo, hi = sorted((rng.randint(0, 4100), rng.randint(0, 4100)))
        assert [k for k, _ in tree.scan(lo, hi)] == [k for k in keys if lo <= k <= hi]
        assert [k for k, _ in tree.scan_reverse(lo, hi)] == [k for k in keys if lo <= k <= hi][::-1]
        assert [k for k, _ in tree.scan(lo, None)] == [k for k in keys if k >= lo]
        assert [k for k, _ in tree.scan_reverse(None, hi)] == [k for k in keys if k <= hi][::-1]
    assert tree.count() == len(keys) and tree.max_rowid() == (keys[-1] if keys else 0)
    for rid in rng.sample(keys, min(30, len(keys))):
        assert decode_record(tree.get(rid)) == expected[rid]
    assert tree.get(10**9) is None
    store.commit()
    store.close()
    assert sqlite_check(path) == ["ok"]
    rows = sqlite_query(path, "SELECT rowid, a, b FROM t ORDER BY rowid")
    assert [list(r) for r in rows] == [[k, k, expected[k][1]] for k in keys]


@pytest.mark.parametrize("page_size, seed", [(4096, 11), (1024, 12), (512, 13), (512, 14)])
def test_index_tree_random_operations_stay_consistent_with_the_table(tmp_path, page_size, seed):
    """Rows go in and out of a table and of an index on it; SQLite's integrity check compares the two (an entry for every row, in the right order)."""
    rng = random.Random(seed)
    path = str(tmp_path / "i.db")
    store = Store(path, page_size)
    table = store.add_table("t", ["a", "b"])
    index = store.add_index("i", "t", ["b"])
    rows = {}
    for step in range(1, 2500):
        if rng.random() < 0.62 or not rows:
            rid = rng.randint(1, 3000)
            if rid in rows:
                old = rows.pop(rid)
                table.delete(rid)
                assert index.delete([old[1], rid])
            value = rng.choice([rng.randint(0, 60), "k%d" % rng.randint(0, 60), None, "wide" * rng.randint(1, 900), rng.random(), b"\x01\x02" * rng.randint(0, 30)])
            rows[rid] = [rid, value]
            table.insert(rid, encode_record([rid, value]))
            assert index.insert([value, rid], encode_record([value, rid]))
        else:
            rid = rng.choice(sorted(rows))
            row = rows.pop(rid)
            assert table.delete(rid)
            assert index.delete([row[1], rid])
        if step % 400 == 0:
            store.commit()
            store.pager.begin()
    expected = sorted(([v, r] for r, (_, v) in rows.items()), key=lambda kv: (sql_order_key(kv[0]), kv[1]))
    assert list(index.scan()) == expected
    for probe in ([5], ["k7"], [None], [0.5], ["wide" * 3]):
        mine = [k for k in index.scan(probe, probe)]
        assert mine == [e for e in expected if compare_prefix(e, probe) == 0]
        assert index.contains(probe) == bool(mine)
    store.commit()
    store.close()
    assert sqlite_check(path) == ["ok"]
    assert sqlite_query(path, "SELECT b, rowid FROM t INDEXED BY i ORDER BY b, rowid") == [tuple(e) for e in expected]


@pytest.mark.parametrize("order", ["ascending", "descending", "random", "blocks", "all but a few"])
@pytest.mark.parametrize("page_size, seed", [(512, 21), (512, 22), (1024, 23), (4096, 24)])
def test_deleting_in_bulk_in_any_order_keeps_every_leaf_at_one_depth(tmp_path, page_size, seed, order):
    """Emptying leaves and interior pages in every order (a page that is left with one child must be merged with a neighbour, not replaced by the child, or the leaves of the
    tree end up at different depths): after each round of deletes SQLite checks the file, and the contents have to be exactly what is left."""
    rng = random.Random(seed)
    path = str(tmp_path / "bulk.db")
    store = Store(path, page_size)
    table = store.add_table("t", ["a", "b"])
    index = store.add_index("i", "t", ["b"])
    rows = {}
    for rid in range(1, 1401):
        value = "b%05d" % rid + "x" * rng.choice([0, 3, 20, 70])
        rows[rid] = value
        table.insert(rid, encode_record([rid, value]))
        assert index.insert([value, rid], encode_record([value, rid]))
    store.commit()
    victims = sorted(rows)
    if order == "descending":
        victims.reverse()
    elif order == "random":
        rng.shuffle(victims)
    elif order == "blocks":
        blocks = [victims[i:i + 40] for i in range(0, len(victims), 40)]
        rng.shuffle(blocks)
        victims = [v for b in blocks for v in b]
    elif order == "all but a few":
        rng.shuffle(victims)
        victims = victims[:-7]
    rounds = 6
    step = (len(victims) + rounds - 1) // rounds
    for start in range(0, len(victims), step):
        store.pager.begin()
        for rid in victims[start:start + step]:
            assert table.delete(rid)
            assert index.delete([rows.pop(rid), rid])
        store.commit()
        assert sqlite_check(path) == ["ok"], "after %d deletes" % (start + step)
        assert [r for r, _ in table.scan()] == sorted(rows)
        assert [tuple(k) for k in index.scan()] == sorted(((v, r) for r, v in rows.items()))
    store.close()


def test_the_free_list_hands_pages_out_again(tmp_path):
    path = str(tmp_path / "f.db")
    store = Store(path, 1024)
    t = store.add_table("t", ["a"])
    for i in range(1, 3000):
        t.insert(i, encode_record(["x" * 50]))
    store.commit()
    pages_full = store.pager.db_size
    store.pager.begin()
    for i in range(1, 3000):
        t.delete(i)
    store.commit()
    assert store.pager.get_u32(36) > pages_full // 2                          # most pages are on the free list
    assert sqlite_check(path) == ["ok"]
    store.pager.begin()
    for i in range(1, 3000):
        t.insert(i, encode_record(["y" * 50]))
    store.commit()
    assert store.pager.db_size <= pages_full + 2                              # the same pages were used again, the file did not grow
    store.close()
    assert sqlite_check(path) == ["ok"]
    assert sqlite_query(path, "SELECT count(*) FROM t") == [(2999,)]


def test_dropping_a_tree_frees_every_page(tmp_path):
    path = str(tmp_path / "d.db")
    store = Store(path, 512)
    t = store.add_table("t", ["a"])
    for i in range(1, 500):
        t.insert(i, encode_record(["z" * (i * 3)]))                           # many overflow chains
    store.commit()
    store.pager.begin()
    t.drop()
    store.master.delete(1)                                                    # (forget the table in the schema too)
    store.commit()
    store.close()
    assert sqlite_check(path) == ["ok"]


# --- files made by SQLite --------------------------------------------------------------------------------------------------------------------------
def make_sqlite_file(path, page_size, seed):
    rng = random.Random(seed)
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=%d" % page_size)
    con.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, a, b TEXT, c REAL, d BLOB)")
    con.execute("CREATE INDEX ta ON t(a)")
    con.execute("CREATE TABLE u(x, y)")
    rows = []
    for i in range(1, 1500):
        rows.append((i, rng.choice([rng.randint(-1000, 1000), "t%d" % rng.randint(0, 50), None]), "w" * rng.choice([1, 20, 500, 5000]), rng.choice([1.5, 2.0, -3.25, 1e100]),
                     bytes(rng.randint(0, 255) for _ in range(rng.choice([0, 10, 2000]))) if rng.random() < 0.5 else None))
    con.executemany("INSERT INTO t VALUES (?,?,?,?,?)", rows)
    con.executemany("INSERT INTO u VALUES (?,?)", [(i, "y" * (i % 30)) for i in range(300)])
    con.execute("DELETE FROM t WHERE id % 3 = 0")                             # leaves free blocks inside pages and free pages
    con.execute("UPDATE t SET b = b || 'x' WHERE id % 5 = 0")
    con.commit()
    con.close()


@pytest.mark.parametrize("page_size", [512, 1024, 4096, 16384])
def test_files_made_by_sqlite_are_read_correctly(tmp_path, page_size):
    path = str(tmp_path / "s.db")
    make_sqlite_file(path, page_size, page_size)
    pager = Pager(path)
    assert pager.page_size == page_size
    master = TableTree(pager, 1)
    schema = {decode_record(rec)[1]: decode_record(rec) for _, rec in master.scan()}
    assert set(schema) == {"t", "ta", "u"}
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    for name, ncols in (("t", 5), ("u", 2)):
        tree = TableTree(pager, schema[name][3])
        mine = []
        for rid, rec in tree.scan():
            vals = decode_record(rec)
            if name == "t":
                vals[0] = rid if vals[0] is None else vals[0]                   # INTEGER PRIMARY KEY is stored as NULL in the record
            mine.append(vals + [None] * (ncols - len(vals)))
        theirs = [list(r) for r in con.execute("SELECT * FROM %s ORDER BY rowid" % name)]
        assert [[float(x) if isinstance(x, (int, float)) and name == "t" and i == 3 else x for i, x in enumerate(r)] for r in mine] == theirs
    index = IndexTree(pager, schema["ta"][3], compare_prefix)
    assert [k for k in index.scan()] == [list(r) for r in con.execute("SELECT a, rowid FROM t INDEXED BY ta ORDER BY a, rowid")]
    con.close()
    pager.close()


def test_sqlite_can_change_a_file_that_this_code_wrote_and_this_code_reads_the_change(tmp_path):
    path = str(tmp_path / "x.db")
    store = Store(path, 1024)
    t = store.add_table("t", ["a", "b"])
    for i in range(1, 800):
        t.insert(i, encode_record([i, "v%d" % i]))
    store.commit()
    store.close()
    con = sqlite3.connect(path)
    con.execute("DELETE FROM t WHERE a % 2 = 0")
    con.execute("INSERT INTO t VALUES (5000, 'new')")
    con.execute("UPDATE t SET b = 'changed' WHERE a = 7")
    con.commit()
    con.close()
    pager = Pager(path)
    tree = TableTree(pager, 2)
    rows = {decode_record(v)[0]: decode_record(v) for k, v in tree.scan()}                 # by column a
    assert 2 not in rows and rows[7][1] == "changed" and rows[5000] == [5000, "new"] and len(rows) == 400 + 1
    pager.begin()
    tree.insert(2, encode_record([2, "back"]))                                 # ... and we can write into what SQLite changed
    pager.commit()
    pager.close()
    assert sqlite_check(path) == ["ok"]
    assert sqlite_query(path, "SELECT b FROM t WHERE a = 2") == [("back",)]


# --- atomic commit: a crash while writing ----------------------------------------------------------------------------------------------------------
def build_committed_file(path):
    store = Store(path, 1024)
    t = store.add_table("t", ["a", "b"])
    for i in range(1, 400):
        t.insert(i, encode_record([i, "before%d" % i]))
    store.commit()
    store.close()
    return sqlite_query(path, "SELECT a, b FROM t ORDER BY a")


def crash_during_commit(path, point):
    pager = Pager(path)
    pager.begin()
    tree = TableTree(pager, 2)
    for i in range(1, 400, 2):
        tree.insert(i, encode_record([i, "AFTER%d" % i]))
    for i in range(400, 700):
        tree.insert(i, encode_record([i, "new%d" % i]))
    tree.delete(100)
    pager.crash_at = point
    with pytest.raises(SimulatedCrash):
        pager.commit()
    pager.file.close()                                                           # the process is gone; nothing is cleaned up


@pytest.mark.parametrize("point", ["journal_written", "after_page_1", "after_page_5", "after_page_9", "database_synced"])
@pytest.mark.parametrize("recover_with", ["this code", "sqlite"])
def test_a_commit_interrupted_at_any_point_leaves_the_old_database(tmp_path, point, recover_with):
    path = str(tmp_path / "c.db")
    before = build_committed_file(path)
    crash_during_commit(path, point)
    assert os.path.exists(path + "-journal")                                   # a "hot" journal is what the crash leaves behind
    if recover_with == "sqlite":
        con = sqlite3.connect(path)                                             # SQLite plays our journal back
        assert [list(r) for r in con.execute("PRAGMA integrity_check")] == [["ok"]]
        assert con.execute("SELECT a, b FROM t ORDER BY a").fetchall() == before
        con.close()
    else:
        pager = Pager(path)                                                     # we play it back
        rows = [(k, decode_record(v)) for k, v in TableTree(pager, 2).scan()]
        assert [(r[1][0], r[1][1]) for r in rows] == before
        pager.close()
        assert sqlite_check(path) == ["ok"]
    assert not os.path.exists(path + "-journal")
    assert sqlite_query(path, "SELECT a, b FROM t ORDER BY a") == before


def test_a_commit_that_finished_keeps_the_new_database(tmp_path):
    path = str(tmp_path / "ok.db")
    build_committed_file(path)
    pager = Pager(path)
    pager.begin()
    tree = TableTree(pager, 2)
    tree.insert(1, encode_record([1, "AFTER"]))
    pager.commit()
    pager.close()
    assert not os.path.exists(path + "-journal")
    assert sqlite_query(path, "SELECT b FROM t WHERE a = 1") == [("AFTER",)]


def test_a_rollback_forgets_everything(tmp_path):
    path = str(tmp_path / "r.db")
    before = build_committed_file(path)
    size = os.path.getsize(path)
    pager = Pager(path)
    pager.begin()
    tree = TableTree(pager, 2)
    for i in range(1000, 3000):
        tree.insert(i, encode_record([i, "gone"]))
    pager.rollback()
    assert os.path.getsize(path) == size and not os.path.exists(path + "-journal")
    pager.begin()
    assert TableTree(pager, 2).count() == len(before)
    pager.rollback()
    pager.close()
    assert sqlite_query(path, "SELECT a, b FROM t ORDER BY a") == before


def test_a_torn_journal_record_is_not_played_back(tmp_path):
    """The journal is only trusted up to the first record whose checksum fails: a crash in the middle of writing the journal itself (before any database page was
    touched) must not damage the database."""
    path = str(tmp_path / "torn.db")
    before = build_committed_file(path)
    crash_during_commit(path, "journal_written")
    with open(path + "-journal", "r+b") as j:
        j.seek(512 + 4 + 1024 + 4 + 100)                                        # in the second record
        j.write(b"\xff" * 50)
    pager = Pager(path)
    pager.close()
    assert sqlite_query(path, "SELECT a, b FROM t ORDER BY a") == before


def test_header_fields_are_what_sqlite_expects(tmp_path):
    path = str(tmp_path / "h.db")
    store = Store(path, 2048)
    store.add_table("t", ["a"])
    store.commit()
    store.close()
    with open(path, "rb") as f:
        h = f.read(100)
    assert h[:16] == b"SQLite format 3\x00" and struct.unpack(">H", h[16:18])[0] == 2048 and h[18:20] == b"\x01\x01"
    assert struct.unpack(">I", h[28:32])[0] == os.path.getsize(path) // 2048
    assert struct.unpack(">I", h[24:28])[0] == struct.unpack(">I", h[92:96])[0] >= 1
    assert sqlite_query(path, "PRAGMA page_size") == [(2048,)] and sqlite_query(path, "PRAGMA encoding") == [("UTF-8",)]


def test_a_second_writer_is_refused_while_a_transaction_is_open_and_readers_see_only_committed_data(tmp_path):
    from kendi_db.errors import OperationalError
    path = str(tmp_path / "l.db")
    first = Pager(path)
    first.begin()
    TableTree(first, 1).insert(1, encode_record(["table", "t", "t", 2, "CREATE TABLE t(a)"]))
    second = Pager(path)
    with pytest.raises(OperationalError, match="locked"):
        second.begin()
    with pytest.raises(OperationalError, match="locked"):
        second.begin_read()
    first.commit()
    second.begin_read()                                                          # the file changed since the second connection looked: it sees the new schema
    assert [decode_record(v)[1] for _, v in TableTree(second, 1).scan()] == ["t"]
    second.end_read()
    first.close()
    second.close()


def test_a_file_that_is_not_a_database_is_refused(tmp_path):
    from kendi_db.errors import CorruptDatabase
    p = tmp_path / "junk.db"
    p.write_bytes(b"this is not a database file" * 50)
    with pytest.raises(CorruptDatabase):
        Pager(str(p))
    q = tmp_path / "ok.db"
    shutil.copy(str(p), str(q))
