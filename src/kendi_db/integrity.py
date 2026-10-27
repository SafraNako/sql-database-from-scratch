"""``PRAGMA integrity_check``: walk every B-tree of the file and see that it is what it should be.

Checked: the type of every page, that keys are in order and within the range the parent promises, that all the leaves of a tree are at the same depth, that overflow chains
have the right length, that no page is used twice, that the free list is intact and that every page of the file is either in a tree or on the free list; and for every index,
that it holds exactly one entry for each row of its table (the key of every row is found, and nothing else is there) and, if it is unique, that no key repeats.
"""
from __future__ import annotations

import struct

from . import node as N
from .btree import TableTree
from .errors import CorruptDatabase
from .pager import OFF_FREELIST_COUNT, PENDING_BYTE
from .tables import index_key, row_from_record
from .values import record_compare, sort_key


def check_integrity(db):
    """A list of problems found (empty if the database is fine)."""
    pager = db.pager
    db.load()
    problems = []
    used = {}

    def claim(pgno, what):
        if pgno < 1 or pgno > pager.db_size:
            problems.append("%s: page %d is outside the file (%d pages)" % (what, pgno, pager.db_size))
            return False
        if pgno in used:
            problems.append("Page %d is used twice: as %s and as %s" % (pgno, used[pgno], what))
            return False
        used[pgno] = what
        return True

    def check_overflow(cell, what):
        remaining = cell.total - len(cell.payload)
        pgno = cell.ovfl
        steps = 0
        while remaining > 0:
            if pgno == 0:
                problems.append("%s: an overflow chain ends too early" % what)
                return
            if not claim(pgno, "overflow page of " + what):
                return
            remaining -= pager.usable - 4
            pgno = struct.unpack_from(">I", pager.get_raw(pgno), 0)[0]
            steps += 1
        if pgno != 0:
            problems.append("%s: an overflow chain is longer than the record" % what)

    def walk_table(pgno, lo, hi, depth, leaf_depths, what):
        try:
            node = pager.get_node(pgno)
        except CorruptDatabase as exc:
            problems.append("%s: %s" % (what, exc))
            return
        if not claim(pgno, what):
            return
        if node.kind == N.TABLE_LEAF:
            leaf_depths.add(depth)
            prev = lo
            for c in node.cells:
                if (prev is not None and c.key <= prev) or (hi is not None and c.key > hi):
                    problems.append("Page %d of %s: rowid %d out of order or out of range" % (pgno, what, c.key))
                prev = c.key
                if c.ovfl:
                    check_overflow(c, "row %d of %s" % (c.key, what))
            return
        if node.kind != N.TABLE_INTERIOR:
            problems.append("Page %d of %s: unexpected page type %d" % (pgno, what, node.kind))
            return
        if not node.cells and node.right == 0:
            problems.append("Page %d of %s: an interior page without children" % (pgno, what))
        prev = lo
        for c in node.cells:
            if (prev is not None and c.key <= prev) or (hi is not None and c.key > hi):
                problems.append("Page %d of %s: separator %d out of order" % (pgno, what, c.key))
            walk_table(c.left, prev, c.key, depth + 1, leaf_depths, what)
            prev = c.key
        walk_table(node.right, prev, hi, depth + 1, leaf_depths, what)

    def walk_index(pgno, depth, leaf_depths, what, tree, keys):
        try:
            node = pager.get_node(pgno)
        except CorruptDatabase as exc:
            problems.append("%s: %s" % (what, exc))
            return
        if not claim(pgno, what):
            return
        if node.kind not in (N.INDEX_LEAF, N.INDEX_INTERIOR):
            problems.append("Page %d of %s: unexpected page type %d" % (pgno, what, node.kind))
            return
        leaf = node.kind == N.INDEX_LEAF
        if leaf:
            leaf_depths.add(depth)
        elif not node.cells and node.right == 0:
            problems.append("Page %d of %s: an interior page without children" % (pgno, what))
        for c in node.cells:
            if not leaf:
                walk_index(c.left, depth + 1, leaf_depths, what, tree, keys)
            if c.ovfl:
                check_overflow(c, "an entry of " + what)
            try:
                keys.append(tree.vals(c))
            except Exception as exc:
                problems.append("Page %d of %s: an entry cannot be read (%s)" % (pgno, what, exc))
        if not leaf:
            walk_index(node.right, depth + 1, leaf_depths, what, tree, keys)

    # the schema table and the tables
    depths = set()
    walk_table(1, None, None, 0, depths, "sqlite_master")
    if len(depths) > 1:
        problems.append("sqlite_master: leaves at different depths")
    rows_by_table = {}
    for table in db.schema.tables.values():
        depths = set()
        walk_table(table.root, None, None, 0, depths, "table " + table.name)
        if len(depths) > 1:
            problems.append("table %s: leaves at different depths" % table.name)
        rows_by_table[table.name] = []
    for table in db.schema.tables.values():
        tree = TableTree(pager, table.root)
        rows = []
        try:
            for rowid, rec in tree.scan():
                try:
                    rows.append(row_from_record(table, rowid, rec))
                except Exception as exc:
                    problems.append("row %d of %s cannot be read (%s)" % (rowid, table.name, exc))
        except CorruptDatabase:
            continue
        for row in rows:
            for i, c in enumerate(table.columns):
                if c.not_null and row[i] is None and i != table.ipk:
                    problems.append("NULL value in %s.%s (row %d)" % (table.name, c.name, row[-1]))
        rows_by_table[table.name] = rows
    # the indexes
    for index in (i for t in db.schema.tables.values() for i in t.indexes):
        tree = db.index_tree(index)
        depths, keys = set(), []
        what = "index " + index.name
        walk_index(index.root, 0, depths, what, tree, keys)
        if len(depths) > 1:
            problems.append("%s: leaves at different depths" % what)
        for a, b in zip(keys, keys[1:]):
            if record_compare(a, b, index.collations) >= 0:
                problems.append("%s: entries out of order" % what)
                break
        table = index.table
        expected = []
        for row in rows_by_table.get(table.name, []):
            if index.where is not None:
                continue
            expected.append(index_key(index, row, row[-1]))
        if index.where is None:
            have = {tuple(sort_key(v) for v in k) for k in keys}
            want = {tuple(sort_key(v) for v in k) for k in expected}
            for k in expected:
                if tuple(sort_key(v) for v in k) not in have:
                    problems.append("row %s missing from index %s" % (k[-1], index.name))
            if len(keys) != len(expected) and have >= want:
                problems.append("wrong # of entries in index %s (%d, should be %d)" % (index.name, len(keys), len(expected)))
        if index.unique:
            seen = set()
            for k in keys:
                if any(v is None for v in k[:-1]):
                    continue
                kk = tuple(sort_key(v) for v in k[:-1])
                if kk in seen:
                    problems.append("%s: duplicate key in a unique index" % what)
                    break
                seen.add(kk)
    # the free list
    try:
        free = pager.freelist_pages()
    except CorruptDatabase as exc:
        problems.append("free list: %s" % exc)
        free = []
    if len(free) != pager.get_u32(OFF_FREELIST_COUNT):
        problems.append("free list: %d pages found, the header says %d" % (len(free), pager.get_u32(OFF_FREELIST_COUNT)))
    for pgno in free:
        claim(pgno, "free-list page")
    pending = PENDING_BYTE // pager.page_size + 1
    if not db.schema.unsupported:                                       # (the pages of a table this engine cannot read cannot be accounted for)
        for pgno in range(1, pager.db_size + 1):
            if pgno not in used and pgno != pending:
                problems.append("Page %d is never used" % pgno)
    return problems
