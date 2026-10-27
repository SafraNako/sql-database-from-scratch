"""The schema: tables, columns and indexes, as the CREATE statements stored in the schema table (``sqlite_master``, the table tree at page 1) describe them.

Each row of ``sqlite_master`` is ``(type, name, tbl_name, rootpage, sql)``. A table is rebuilt from the text of its CREATE TABLE; the indexes that SQLite creates by itself for
UNIQUE and PRIMARY KEY constraints (named ``sqlite_autoindex_<table>_<n>``, stored without SQL text) are matched to the constraints in the order they are written.
"""
from __future__ import annotations

from . import sqlast as A
from .btree import TableTree
from .errors import CorruptDatabase, OperationalError
from .format import decode_record
from .parser import parse_one
from .values import affinity_of


class Column:
    __slots__ = ("name", "typename", "affinity", "not_null", "default", "collation", "primary_key", "check")

    def __init__(self, d):
        self.name = d.name
        self.typename = d.typename or ""
        self.affinity = affinity_of(d.typename)
        self.not_null = d.not_null
        self.default = d.default
        self.collation = d.collation
        self.primary_key = d.primary_key
        self.check = d.check


class Index:
    """``columns`` is a list of ``(column number, collation, descending)``."""
    __slots__ = ("name", "table", "root", "columns", "unique", "auto", "sql", "where")

    def __init__(self, name, table, root, columns, unique, auto, sql, where=None):
        self.name = name
        self.table = table
        self.root = root
        self.columns = columns
        self.unique = unique
        self.auto = auto
        self.sql = sql
        self.where = where

    @property
    def collations(self):
        return [c[1] for c in self.columns] + [None]               # the rowid at the end of every key is compared as it is


class Table:
    def __init__(self, name, columns, root, sql, ipk, checks, autoincrement=False):
        self.name = name
        self.columns = columns
        self.root = root
        self.sql = sql
        self.ipk = ipk                                              # the column that is the rowid under another name, or None
        self.checks = checks                                        # [(expression, text)]
        self.autoincrement = autoincrement
        self.indexes = []
        self.colmap = {c.name.lower(): i for i, c in enumerate(columns)}

    def column_index(self, name):
        return self.colmap.get(name.lower())

    @property
    def names(self):
        return [c.name for c in self.columns]


class Schema:
    def __init__(self):
        self.tables = {}                                            # lower-case name -> Table
        self.indexes = {}                                           # lower-case name -> Index
        self.views = {}                                             # lower-case name -> (name, the SELECT as a tree, column names or None)
        self.triggers = {}                                          # lower-case table name -> [trigger names]
        self.unsupported = {}                                       # lower-case table name -> why this engine cannot use it

    def table(self, name):
        t = self.tables.get(name.lower())
        if t is None:
            why = self.unsupported.get(name.lower())
            raise OperationalError(why if why else "no such table: %s" % name)
        return t


def _is_integer_pk(coldef):
    return coldef.primary_key and (coldef.typename or "").strip().upper() == "INTEGER" and not coldef.desc


def unique_groups(create):
    """The column-name lists of the UNIQUE and PRIMARY KEY constraints of a CREATE TABLE that need an index, in the order SQLite creates them."""
    ipk_name = None
    pk_cols = [c for c in create.columns if c.primary_key]
    table_pks = [t for t in create.constraints if t.kind == "PRIMARY KEY"]
    if len(pk_cols) == 1 and not table_pks and _is_integer_pk(pk_cols[0]):
        ipk_name = pk_cols[0].name
    elif len(table_pks) == 1 and not pk_cols and len(table_pks[0].columns) == 1:
        c = next((c for c in create.columns if c.name.lower() == str(table_pks[0].columns[0][0]).lower()), None)
        if c is not None and (c.typename or "").strip().upper() == "INTEGER" and not table_pks[0].columns[0][2]:
            ipk_name = c.name
    groups = []

    def add(names, collations):
        key = ([n.lower() for n in names], tuple(collations))
        if key not in [(g[0], g[1]) for g in groups]:
            groups.append((key[0], key[1], names))

    for c in create.columns:
        if c.primary_key and c.name != ipk_name:
            add([c.name], [c.collation])
        if c.unique:
            add([c.name], [c.collation])
    for t in create.constraints:
        if t.kind in ("PRIMARY KEY", "UNIQUE"):
            if t.kind == "PRIMARY KEY" and ipk_name is not None:
                continue
            names = [str(col[0]) for col in t.columns]
            add(names, [col[1] for col in t.columns])
    return ipk_name, [(g[2], g[1]) for g in groups]


def build_table(create, root):
    columns = [Column(d) for d in create.columns]
    ipk_name, groups = unique_groups(create)
    ipk = None
    for i, c in enumerate(columns):
        if ipk_name is not None and c.name == ipk_name:
            ipk = i
    checks = [(c.check, "") for c in create.columns if c.check is not None] + [(t.expr, "") for t in create.constraints if t.kind == "CHECK"]
    table = Table(create.name, columns, root, create.sql, ipk, checks, any(c.autoincrement for c in create.columns))
    return table, groups


def load_schema(pager):
    """Read ``sqlite_master`` and build the schema."""
    schema = Schema()
    rows = []
    for rowid, rec in TableTree(pager, 1).scan():
        v = decode_record(rec)
        v += [None] * (5 - len(v))
        rows.append((rowid, v))
    pending_auto = {}
    for rowid, (kind, name, tbl, root, sql) in rows:
        if kind == "view":
            try:
                create = parse_one(sql)
                schema.views[name.lower()] = (name, create.select, create.columns)
            except Exception as exc:
                schema.unsupported[name.lower()] = "cannot use view %s: %s" % (name, exc)
        elif kind == "trigger":
            schema.triggers.setdefault(tbl.lower(), []).append(name)
        elif kind == "table":
            if sql is None:
                raise CorruptDatabase("table %s has no CREATE statement" % name)
            try:
                create = parse_one(sql)
                if not isinstance(create, A.CreateTable) or create.select is not None:
                    raise OperationalError("not a plain CREATE TABLE")
            except Exception as exc:
                schema.unsupported[name.lower()] = "cannot use table %s: %s" % (name, exc)
                continue
            create.name = name
            table, groups = build_table(create, root)
            schema.tables[name.lower()] = table
            pending_auto[name.lower()] = groups
    auto_seen = {}
    for rowid, (kind, name, tbl, root, sql) in rows:
        if kind != "index":
            continue
        table = schema.tables.get(tbl.lower())
        if table is None:
            if tbl.lower() in schema.unsupported:
                continue
            raise CorruptDatabase("index %s is on a missing table %s" % (name, tbl))
        if sql is None:
            n = auto_seen.get(tbl.lower(), 0)
            auto_seen[tbl.lower()] = n + 1
            groups = pending_auto[tbl.lower()]
            if n >= len(groups):
                raise CorruptDatabase("the automatic index %s matches no constraint" % name)
            names, collations = groups[n]
            cols = [(table.column_index(nm), collations[k] or table.columns[table.column_index(nm)].collation, False) for k, nm in enumerate(names)]
            idx = Index(name, table, root, cols, True, True, None)
        else:
            create = parse_one(sql)
            cols = []
            for expr, coll, desc in create.columns:
                if not isinstance(expr, str):
                    raise CorruptDatabase("indexes on expressions are not supported (index %s)" % name)
                ci = table.column_index(expr)
                if ci is None:
                    raise CorruptDatabase("index %s refers to a missing column %s" % (name, expr))
                cols.append((ci, coll or table.columns[ci].collation, desc))
            idx = Index(name, table, root, cols, create.unique, False, sql, create.where)
        table.indexes.append(idx)
        schema.indexes[name.lower()] = idx
    return schema
