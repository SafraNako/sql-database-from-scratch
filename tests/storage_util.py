"""Helpers for the storage tests: a database made directly with the pager and the B-trees (no SQL), registered in the schema table so that real SQLite can open it."""
import sqlite3

from kendi_db.btree import IndexTree, TableTree
from kendi_db.format import encode_record
from kendi_db.pager import Pager


def sql_order_key(v):
    """SQLite's order of values: NULL, then numbers, then text, then blobs."""
    if v is None:
        return (0, 0)
    if isinstance(v, (int, float)):
        return (1, v)
    if isinstance(v, str):
        return (2, v)
    return (3, bytes(v))


def compare_prefix(key, probe):
    for a, b in zip(key, probe):
        ka, kb = sql_order_key(a), sql_order_key(b)
        if ka != kb:
            return -1 if ka < kb else 1
    return 0


class Store:
    """A pager with a schema table at page 1; ``add_table`` and ``add_index`` create trees and record them there like SQLite does."""

    def __init__(self, path, page_size=4096):
        self.pager = Pager(path, page_size)
        self.pager.begin()
        self.master = TableTree(self.pager, 1)
        self.next_master_rowid = 1

    def add_table(self, name, columns):
        root = TableTree.create(self.pager)
        sql = "CREATE TABLE %s(%s)" % (name, ", ".join(columns))
        self.master.insert(self.next_master_rowid, encode_record(["table", name, name, root, sql]))
        self.next_master_rowid += 1
        self.pager.set_u32(40, self.pager.get_u32(40) + 1)
        return TableTree(self.pager, root)

    def add_index(self, name, table, columns):
        root = IndexTree.create(self.pager)
        sql = "CREATE INDEX %s ON %s(%s)" % (name, table, ", ".join(columns))
        self.master.insert(self.next_master_rowid, encode_record(["index", name, table, root, sql]))
        self.next_master_rowid += 1
        self.pager.set_u32(40, self.pager.get_u32(40) + 1)
        return IndexTree(self.pager, root, compare_prefix)

    def commit(self):
        self.pager.commit()

    def close(self):
        self.pager.close()


def sqlite_check(path):
    """What real SQLite says about the file: its integrity check, and a function to run queries on it."""
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return [r[0] for r in con.execute("PRAGMA integrity_check")]
    finally:
        con.close()


def sqlite_query(path, sql, params=()):
    con = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()
