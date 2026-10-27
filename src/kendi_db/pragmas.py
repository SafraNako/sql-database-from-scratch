"""PRAGMA statements."""
from __future__ import annotations

from .database import Result
from .errors import OperationalError
from .pager import OFF_APPLICATION_ID, OFF_FREELIST_COUNT, OFF_SCHEMA_COOKIE, OFF_USER_VERSION


def run_pragma(db, ast):
    name, arg = ast.name, ast.arg
    pager = db.pager
    if name == "table_info" or name == "table_xinfo":
        table = db.schema_table(str(arg))
        pk_order = {}
        from .parser import parse_one
        if table.sql:
            create = parse_one(table.sql)
            pks = [c.name for c in create.columns if c.primary_key]
            for t in create.constraints:
                if t.kind == "PRIMARY KEY":
                    pks = [str(col[0]) for col in t.columns]
            pk_order = {n.lower(): i + 1 for i, n in enumerate(pks)}
        rows = []
        for i, c in enumerate(table.columns):
            d = None
            if c.default is not None:
                d = _default_text(c)
            rows.append((i, c.name, c.typename, 1 if c.not_null else 0, d, pk_order.get(c.name.lower(), 0)))
        return Result(["cid", "name", "type", "notnull", "dflt_value", "pk"], rows)
    if name == "index_list":
        table = db.schema_table(str(arg))
        rows = []
        for seq, idx in enumerate(reversed(table.indexes)):
            origin = "pk" if idx.auto and _is_pk(table, idx) else ("u" if idx.auto else "c")
            rows.append((seq, idx.name, 1 if idx.unique else 0, origin, 1 if idx.where is not None else 0))
        return Result(["seq", "name", "unique", "origin", "partial"], rows)
    if name == "index_info":
        idx = db.schema.indexes.get(str(arg).lower())
        if idx is None:
            return Result(["seqno", "cid", "name"], [])
        return Result(["seqno", "cid", "name"], [(i, c, idx.table.columns[c].name) for i, (c, _, _) in enumerate(idx.columns)])
    if name == "user_version":
        if arg is not None:
            pager.set_u32(OFF_USER_VERSION, int(arg))
            return Result()
        return Result(["user_version"], [(_signed(pager.get_u32(OFF_USER_VERSION)),)])
    if name == "application_id":
        if arg is not None:
            pager.set_u32(OFF_APPLICATION_ID, int(arg))
            return Result()
        return Result(["application_id"], [(pager.get_u32(OFF_APPLICATION_ID),)])
    if name == "schema_version":
        if arg is not None:
            pager.set_u32(OFF_SCHEMA_COOKIE, int(arg))
            db.schema = None
            return Result()
        return Result(["schema_version"], [(pager.get_u32(OFF_SCHEMA_COOKIE),)])
    if name == "page_size":
        return Result(["page_size"], [(pager.page_size,)])
    if name == "page_count":
        return Result(["page_count"], [(pager.db_size,)])
    if name == "freelist_count":
        return Result(["freelist_count"], [(pager.get_u32(OFF_FREELIST_COUNT) if pager.db_size else 0,)])
    if name == "encoding":
        return Result(["encoding"], [("UTF-8",)])
    if name == "journal_mode":
        return Result(["journal_mode"], [("delete",)])
    if name in ("integrity_check", "quick_check"):
        from .integrity import check_integrity
        problems = check_integrity(db)
        return Result([name], [(p,) for p in (problems or ["ok"])])
    if name == "database_list":
        return Result(["seq", "name", "file"], [(0, "main", db.path)])
    if name in ("foreign_keys", "synchronous", "cache_size", "temp_store", "locking_mode", "analyze", "optimize", "busy_timeout", "recursive_triggers", "case_sensitive_like",
                "automatic_index", "mmap_size", "wal_autocheckpoint", "secure_delete", "foreign_key_check", "compile_options"):
        return Result()
    raise OperationalError("unsupported pragma: %s" % name)


def _signed(v):
    return v - (1 << 32) if v >= (1 << 31) else v


def _is_pk(table, idx):
    from .parser import parse_one
    if not table.sql:
        return False
    create = parse_one(table.sql)
    pk = [c.name.lower() for c in create.columns if c.primary_key]
    for t in create.constraints:
        if t.kind == "PRIMARY KEY":
            pk = [str(col[0]).lower() for col in t.columns]
    return [table.columns[c].name.lower() for c, _, _ in idx.columns] == pk and bool(pk)


def _default_text(c):
    from . import sqlast as A
    d = c.default
    if isinstance(d, A.Lit):
        from .functions import f_quote
        v = d.value
        return f_quote(v) if v is not None else "NULL"
    return "(expression)"
