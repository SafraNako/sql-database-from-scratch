"""ALTER TABLE: RENAME TO and ADD COLUMN, done the way SQLite does them, by rewriting the stored CREATE statement."""
from __future__ import annotations

from . import sqlast as A
from .database import Result, quote_ident
from .errors import OperationalError
from .lexer import ID, QID, tokenize


def _replace_table_name(sql, old, new, after_words):
    """The CREATE statement with the first identifier that follows one of ``after_words`` (and equals ``old``) replaced by ``new``."""
    toks = tokenize(sql)
    for i, t in enumerate(toks[:-1]):
        if t.kind == ID and t.value.upper() in after_words:
            nxt = toks[i + 1]
            if nxt.kind in (ID, QID) and nxt.value.lower() == old.lower():
                return sql[:nxt.pos] + quote_ident(new) + sql[nxt.end:]
    return sql


def run_alter(db, ast):
    table = db.schema.table(ast.table)
    if ast.action == "rename":
        new = ast.arg
        if new.lower() in db.schema.tables or new.lower() in db.schema.indexes:
            raise OperationalError("there is already another table or index with this name: %s" % new)
        if new.lower().startswith("sqlite_"):
            raise OperationalError("object name reserved for internal use: %s" % new)
        old = table.name
        tree = db.master_tree()
        from .format import decode_record, encode_record
        for rowid, rec in list(tree.scan()):
            v = decode_record(rec)
            kind, name, tbl, root, sql = v
            if tbl != old:
                continue
            if kind == "table":
                v = [kind, new, new, root, _replace_table_name(sql, old, new, ("TABLE", "EXISTS"))]
            elif sql is None:
                v = [kind, name.replace("sqlite_autoindex_%s_" % old, "sqlite_autoindex_%s_" % new, 1), new, root, None]
            else:
                v = [kind, name, new, root, _replace_table_name(sql, old, new, ("ON",))]
            tree.insert(rowid, encode_record(v))
        db.bump_schema()
        return Result()
    if ast.action == "add_column":
        col = ast.arg
        if col.primary_key or col.unique:
            raise OperationalError("Cannot add a %s column" % ("PRIMARY KEY" if col.primary_key else "UNIQUE"))
        if col.not_null and (col.default is None or (isinstance(col.default, A.Lit) and col.default.value is None)):
            raise OperationalError("Cannot add a NOT NULL column with default value NULL")
        if col.default is not None and not isinstance(col.default, A.Lit):
            raise OperationalError("Cannot add a column with non-constant default")
        if table.column_index(col.name) is not None:
            raise OperationalError("duplicate column name: %s" % col.name)
        tree = db.master_tree()
        from .format import decode_record, encode_record
        for rowid, rec in list(tree.scan()):
            kind, name, tbl, root, sql = decode_record(rec)
            if kind == "table" and name == table.name:
                body = sql.rstrip()
                if not body.endswith(")"):
                    raise OperationalError("cannot add a column to this table")
                sql = body[:-1].rstrip() + ", " + col.text + ")"
                tree.insert(rowid, encode_record([kind, name, tbl, root, sql]))
        db.bump_schema()
        return Result()
    raise OperationalError("this ALTER TABLE is not supported")
