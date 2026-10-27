"""Rows of tables: turning stored records into rows and back, the keys of indexes, and the constraint checks that go with them."""
from __future__ import annotations

from . import sqlast as A
from . import values as V
from .errors import IntegrityError
from .format import decode_record, encode_record


def default_value(column):
    """The value a column has when nothing was given: its DEFAULT if that is a constant, else NULL (expressions are evaluated by the insert itself)."""
    d = column.default
    if isinstance(d, A.Lit):
        return V.apply_affinity(d.value, column.affinity)
    return None


def row_from_record(table, rowid, rec):
    """The list of column values of a stored row, plus its rowid as the last element."""
    vals = decode_record(rec)
    n = len(table.columns)
    if len(vals) < n:                                                    # a column added after the row was written
        vals.extend(default_value(table.columns[i]) for i in range(len(vals), n))
    elif len(vals) > n:
        del vals[n:]
    if table.ipk is not None:
        vals[table.ipk] = rowid
    for i, c in enumerate(table.columns):
        if c.affinity == V.REAL and type(vals[i]) is int:
            vals[i] = float(vals[i])                                     # SQLite stores a whole-number real as an integer to save space
    vals.append(rowid)
    return vals


def record_for_row(table, values):
    """The record to store: the column values, with NULL in the place of an INTEGER PRIMARY KEY (it lives in the rowid)."""
    if table.ipk is not None:
        values = list(values)
        values[table.ipk] = None
    return encode_record(values)


def index_key(index, values, rowid):
    """The key of an index entry for a row: the indexed columns and the rowid."""
    return [values[c] for c, _, _ in index.columns] + [rowid]


def unique_message(table, index_or_cols):
    if hasattr(index_or_cols, "columns"):
        cols = [table.columns[c].name for c, _, _ in index_or_cols.columns]
    else:
        cols = index_or_cols
    return "UNIQUE constraint failed: " + ", ".join("%s.%s" % (table.name, c) for c in cols)


def check_not_null(table, values, skip_ipk=True):
    for i, c in enumerate(table.columns):
        if c.not_null and values[i] is None and not (skip_ipk and i == table.ipk):
            raise IntegrityError("NOT NULL constraint failed: %s.%s" % (table.name, c.name))
