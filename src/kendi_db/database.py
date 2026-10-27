"""The database engine: statements go in, rows come out, and everything is kept in a file that SQLite can read.

``Database.execute(parsed statement, parameters)`` runs one statement inside a transaction of its own (or inside the one the user opened with BEGIN, where a failing
statement is undone by itself and the transaction goes on). SELECTs only read, under a shared lock; everything else takes the write lock, changes pages in memory and
commits them through the rollback journal.
"""
from __future__ import annotations

import random

from . import sqlast as A
from . import values as V
from .btree import IndexTree, TableTree
from .errors import DatabaseError, IntegrityError, OperationalError
from .exprs import Context, ExprCompiler, Scope, Source, table_collations, truth
from .format import INT_MAX, decode_record, encode_record
from .pager import OFF_SCHEMA_COOKIE, Pager
from .schema import Column, Schema, Table, build_table, load_schema
from .select import SelectCompiler
from .tables import check_not_null, default_value, index_key, record_for_row, row_from_record, unique_message
from .values import record_compare

MASTER_COLUMNS = ["type", "name", "tbl_name", "rootpage", "sql"]


class Result:
    def __init__(self, names=None, rows=None, rowcount=-1, lastrowid=0):
        self.names = names or []
        self.rows = rows or []
        self.rowcount = rowcount
        self.lastrowid = lastrowid


def master_table():
    cols = [A.ColumnDef(n, None, False, False, False, False, None, None, None, False) for n in MASTER_COLUMNS]
    cols[3].typename = "integer"
    cols[4].typename = "text"
    t = Table("sqlite_master", [Column(c) for c in cols], 1, None, None, [])
    return t


class Database:
    def __init__(self, path, page_size=4096):
        self.path = path
        self.pager = Pager(path, page_size)
        self.schema = None
        self.schema_cookie = None
        self.in_user_txn = False
        self.ctx = Context(self)
        self.changes = 0
        self.total_changes = 0
        self.last_insert_rowid = 0
        self.master = master_table()

    def close(self):
        self.pager.close()

    # --- access for the planner and the executors ---------------------------------------------------------------------------------------------------
    def table_tree(self, table):
        return TableTree(self.pager, table.root)

    def index_tree(self, index):
        collations = index.collations
        return IndexTree(self.pager, index.root, lambda key, probe: record_compare(key, probe, collations))

    def special_function(self, name, nargs):
        if name == "last_insert_rowid" and nargs == 0:
            return lambda row: self.last_insert_rowid
        if name == "changes" and nargs == 0:
            return lambda row: self.changes
        if name == "total_changes" and nargs == 0:
            return lambda row: self.total_changes
        return None

    def load(self):
        """Make sure the schema in memory is the schema in the file."""
        cookie = self.pager.get_u32(OFF_SCHEMA_COOKIE) if self.pager.db_size else 0
        if self.schema is None or cookie != self.schema_cookie:
            self.schema = load_schema(self.pager) if self.pager.db_size else Schema()
            self.schema_cookie = cookie

    def schema_table(self, name):
        if name.lower() in ("sqlite_master", "sqlite_schema"):
            return self.master
        self.load()
        return self.schema.table(name)

    # --- running a statement ------------------------------------------------------------------------------------------------------------------------
    def execute(self, parsed, params=()):
        ast = parsed.ast
        t = type(ast)
        if t is A.Begin:
            if self.in_user_txn:
                raise OperationalError("cannot start a transaction within a transaction")
            self.pager.begin()
            self.in_user_txn = True
            return Result()
        if t is A.Commit:
            if not self.in_user_txn:
                raise OperationalError("cannot commit - no transaction is active")
            self.pager.commit()
            self.in_user_txn = False
            return Result()
        if t is A.Rollback:
            if not self.in_user_txn:
                raise OperationalError("cannot rollback - no transaction is active")
            self.pager.rollback()
            self.in_user_txn = False
            self.schema = None
            return Result()
        self.ctx.params = bind_parameters(params, parsed)
        writes = self.writes(ast)
        if self.in_user_txn:
            if not writes:
                return self.run(ast)
            self.pager.savepoint()
            try:
                result = self.run(ast)
            except BaseException:
                self.pager.rollback_savepoint()
                self.schema = None
                raise
            self.pager.release_savepoint()
            return result
        if writes:
            self.pager.begin()
            try:
                result = self.run(ast)
                self.pager.commit()
            except BaseException:
                self.pager.rollback()
                self.schema = None
                raise
            return result
        self.pager.begin_read()
        try:
            return self.run(ast)
        finally:
            self.pager.end_read()

    def writes(self, ast):
        t = type(ast)
        if t in (A.Insert, A.Update, A.Delete, A.CreateTable, A.CreateIndex, A.DropTable, A.DropIndex, A.AlterTable, A.Vacuum, A.CreateView, A.DropView):
            return True
        if t is A.Pragma:
            return ast.arg is not None and ast.name in ("user_version", "schema_version", "application_id")
        if t is A.Explain:
            return False
        return False

    def run(self, ast):
        self.load()
        t = type(ast)
        if t is A.Select:
            return self.run_select(ast)
        if t is A.Insert:
            return self.run_insert(ast)
        if t is A.Update:
            return self.run_update(ast)
        if t is A.Delete:
            return self.run_delete(ast)
        if t is A.CreateTable:
            return self.run_create_table(ast)
        if t is A.CreateIndex:
            return self.run_create_index(ast)
        if t is A.DropTable:
            return self.run_drop_table(ast)
        if t is A.DropIndex:
            return self.run_drop_index(ast)
        if t is A.CreateView:
            return self.run_create_view(ast)
        if t is A.DropView:
            return self.run_drop_view(ast)
        if t is A.Pragma:
            from .pragmas import run_pragma
            return run_pragma(self, ast)
        if t is A.Explain:
            return self.run_explain(ast)
        if t is A.AlterTable:
            from .alter import run_alter
            return run_alter(self, ast)
        if t is A.Vacuum:
            raise OperationalError("VACUUM is not supported")
        raise OperationalError("unsupported statement")

    def compiler(self):
        return SelectCompiler(self.ctx)

    def run_select(self, ast):
        compiled = self.compiler().compile(ast)
        rows = list(compiled.run())
        return Result(compiled.names, rows)

    def run_explain(self, ast):
        inner = ast.stmt
        if not isinstance(inner, A.Select):
            return Result(["detail"], [("%s" % type(inner).__name__,)])
        compiled = self.compiler().compile(inner)
        if ast.plan:
            return Result(["id", "parent", "notused", "detail"], [(i + 2, 0, 0, line) for i, line in enumerate(compiled.plan)])
        return Result(["detail"], [(line,) for line in compiled.plan])

    # --- the schema table -----------------------------------------------------------------------------------------------------------------------------
    def master_tree(self):
        return TableTree(self.pager, 1)

    def add_master_row(self, kind, name, tbl_name, root, sql):
        tree = self.master_tree()
        tree.insert(tree.max_rowid() + 1, encode_record([kind, name, tbl_name, root, sql]))

    def delete_master_rows(self, predicate):
        tree = self.master_tree()
        victims = [rowid for rowid, rec in tree.scan() if predicate(decode_record(rec))]
        for rowid in victims:
            tree.delete(rowid)

    def bump_schema(self):
        self.pager.set_u32(OFF_SCHEMA_COOKIE, self.pager.get_u32(OFF_SCHEMA_COOKIE) + 1)
        self.schema = load_schema(self.pager)
        self.schema_cookie = self.pager.get_u32(OFF_SCHEMA_COOKIE)

    # --- CREATE and DROP --------------------------------------------------------------------------------------------------------------------------------
    def run_create_table(self, ast):
        if ast.name.lower() in ("sqlite_master", "sqlite_schema") or ast.name.lower().startswith("sqlite_"):
            raise OperationalError("object name reserved for internal use: %s" % ast.name)
        if ast.name.lower() in self.schema.tables:
            if ast.if_not_exists:
                return Result()
            raise OperationalError("table %s already exists" % ast.name)
        if ast.name.lower() in self.schema.indexes:
            raise OperationalError("there is already an index named %s" % ast.name)
        select = ast.select
        columns = ast.columns
        sql = ast.sql
        compiled = None
        if select is not None:
            compiled = self.compiler().compile(select)
            rows = list(compiled.run())
            defs = []
            for name, aff in zip(compiled.names, compiled.affinities):
                tn = {V.INTEGER: "INT", V.TEXT: "TEXT", V.REAL: "REAL", V.NUMERIC: "NUM", V.BLOB: ""}.get(aff, "")
                defs.append("%s %s" % (quote_ident(name), tn) if tn else quote_ident(name))
            sql = "CREATE TABLE %s(%s)" % (quote_ident(ast.name), ", ".join(defs))
            from .parser import parse_one
            ast = parse_one(sql)
            ast.name = ast.name
            columns = ast.columns
        names = [c.name.lower() for c in columns]
        if len(set(names)) != len(names):
            dup = next(n for n in names if names.count(n) > 1)
            raise OperationalError("duplicate column name: %s" % dup)
        for c in ast.constraints:
            if c.columns:
                for col in c.columns:
                    if str(col[0]).lower() not in names:
                        raise OperationalError("table %s has no column named %s" % (ast.name, col[0]))
        if sum(1 for c in columns if c.primary_key) + sum(1 for t in ast.constraints if t.kind == "PRIMARY KEY") > 1:
            raise OperationalError("table \"%s\" has more than one primary key" % ast.name)
        if any(c.autoincrement for c in columns) and "sqlite_sequence" not in self.schema.tables:
            seq_root = TableTree.create(self.pager)
            self.add_master_row("table", "sqlite_sequence", "sqlite_sequence", seq_root, "CREATE TABLE sqlite_sequence(name,seq)")
        root = TableTree.create(self.pager)
        table, groups = build_table(ast, root)
        self.add_master_row("table", ast.name, ast.name, root, ast.sql)
        for n, (cols, collations) in enumerate(groups, 1):
            iroot = IndexTree.create(self.pager)
            self.add_master_row("index", "sqlite_autoindex_%s_%d" % (ast.name, n), ast.name, iroot, None)
        self.bump_schema()
        if compiled is not None:
            t = self.schema.table(ast.name)
            n = 0
            for row in rows:
                self.insert_row(t, list(row), None, None)
                n += 1
            return Result(rowcount=n)
        return Result()

    def run_create_index(self, ast):
        table = self.schema.table(ast.table)
        if ast.name.lower() in self.schema.indexes or ast.name.lower() in self.schema.tables:
            if ast.if_not_exists:
                return Result()
            raise OperationalError("index %s already exists" % ast.name)
        if ast.name.lower().startswith("sqlite_"):
            raise OperationalError("object name reserved for internal use: %s" % ast.name)
        cols = []
        for expr, coll, desc in ast.columns:
            if not isinstance(expr, str):
                raise OperationalError("indexes on expressions are not supported")
            ci = table.column_index(expr)
            if ci is None:
                raise OperationalError("table %s has no column named %s" % (table.name, expr))
            cols.append((ci, coll or table.columns[ci].collation, desc))
        root = IndexTree.create(self.pager)
        self.add_master_row("index", ast.name, table.name, root, ast.sql)
        self.bump_schema()
        index = self.schema.indexes[ast.name.lower()]
        tree = self.index_tree(index)
        where_f = None
        if index.where is not None:
            where_f = self.table_expr(table, index.where)
        for rowid, rec in self.table_tree(table).scan():
            row = row_from_record(table, rowid, rec)
            if where_f is not None and truth(where_f(tuple(row))) is not True:
                continue
            key = index_key(index, row, rowid)
            if index.unique and all(k is not None for k in key[:-1]) and tree.contains(key[:-1]):
                raise IntegrityError(unique_message(table, index))
            tree.insert(key, encode_record(key))
        return Result()

    def table_expr(self, table, expr, alias=None):
        """An expression compiled over the rows of one table: ``f(row)`` with the row as the column values and the rowid."""
        scope = Scope()
        scope.add(Source(alias or table.name, table.names, [c.affinity for c in table.columns], table_collations(table), table))
        return ExprCompiler(self.ctx, scope, self.compiler().compile_subselect).compile(expr)

    def run_drop_table(self, ast):
        if ast.name.lower() in ("sqlite_master", "sqlite_schema") or ast.name.lower().startswith("sqlite_"):
            raise OperationalError("table %s may not be dropped" % ast.name)
        table = self.schema.tables.get(ast.name.lower())
        if table is None:
            if ast.if_exists:
                return Result()
            raise OperationalError("no such table: %s" % ast.name)
        for index in table.indexes:
            self.index_tree(index).drop()
        self.table_tree(table).drop()
        name = table.name
        self.delete_master_rows(lambda r: r[2] == name)
        seq = self.schema.tables.get("sqlite_sequence")
        if seq is not None and table.autoincrement:
            st = self.table_tree(seq)
            for rowid, rec in list(st.scan()):
                if decode_record(rec)[0] == name:
                    st.delete(rowid)
        self.bump_schema()
        return Result()

    def run_create_view(self, ast):
        name = ast.name
        if name.lower() in self.schema.tables or name.lower() in self.schema.views or name.lower() in self.schema.indexes:
            if ast.if_not_exists and name.lower() in self.schema.views:
                return Result()
            raise OperationalError("%s %s already exists" % ("view" if name.lower() in self.schema.views else "table", name))
        compiled = self.compiler().compile(ast.select)                    # (so that mistakes in the query are reported now)
        if ast.columns and len(ast.columns) != compiled.ncolumns:
            raise OperationalError("expected %d columns for '%s' but got %d" % (len(ast.columns), name, compiled.ncolumns))
        self.add_master_row("view", name, name, 0, ast.sql)
        self.bump_schema()
        return Result()

    def run_drop_view(self, ast):
        if ast.name.lower() not in self.schema.views:
            if ast.if_exists:
                return Result()
            raise OperationalError("no such view: %s" % ast.name)
        name = ast.name
        self.delete_master_rows(lambda r: r[0] == "view" and r[1].lower() == name.lower())
        self.bump_schema()
        return Result()

    def run_drop_index(self, ast):
        index = self.schema.indexes.get(ast.name.lower())
        if index is None:
            if ast.if_exists:
                return Result()
            raise OperationalError("no such index: %s" % ast.name)
        if index.auto:
            raise OperationalError("index associated with UNIQUE or PRIMARY KEY constraint cannot be dropped")
        self.index_tree(index).drop()
        name = index.name
        self.delete_master_rows(lambda r: r[0] == "index" and r[1] == name)
        self.bump_schema()
        return Result()

    # --- reading the rows a WHERE clause selects -----------------------------------------------------------------------------------------------------
    def matching_rows(self, table, where):
        """Every row of the table (as a tuple of the values and the rowid) for which ``where`` is true, found the way a SELECT would find them."""
        sel = A.Select([A.SelectCore(False, [A.ResultCol(None, None, None, True, "*")], A.TableRef(table.name, None), where, [], None, None)], [], [], None, None, [])
        compiler = self.compiler()
        # a SELECT of the table's own columns and its rowid in one go
        core = sel.cores[0]
        core.columns = [A.ResultCol(A.Col(table.name, c.name), None, None, False, c.name) for c in table.columns] + [A.ResultCol(A.Col(table.name, "rowid"), None, None, False, "rowid")]
        compiled = compiler.compile(sel)
        rows = [tuple(r) for r in compiled.run()]
        rows.sort(key=lambda r: r[-1])                                   # changes are made in rowid order, whichever way the rows were found
        return rows

    # --- INSERT -----------------------------------------------------------------------------------------------------------------------------------------
    def writable_table(self, name):
        if name.lower() in ("sqlite_master", "sqlite_schema"):
            raise OperationalError("table %s may not be modified" % name)
        if name.lower() in self.schema.views:
            raise OperationalError("cannot modify %s because it is a view" % name)
        if self.schema.triggers.get(name.lower()):
            raise OperationalError("table %s has triggers, which this engine does not run" % name)
        return self.schema.table(name)

    def run_insert(self, ast):
        table = self.writable_table(ast.table)
        if ast.columns:
            cols = []
            for name in ast.columns:
                ci = table.column_index(name)
                if ci is None:
                    if name.lower() in ("rowid", "oid", "_rowid_") and table.ipk is None:
                        cols.append(-1)
                        continue
                    raise OperationalError("table %s has no column named %s" % (table.name, name))
                cols.append(ci)
        else:
            cols = list(range(len(table.columns)))
        if ast.default_values:
            source_rows = [[]]
            cols = []
        elif ast.select is not None:
            compiled = self.compiler().compile(ast.select)
            if compiled.ncolumns != len(cols):
                raise OperationalError("table %s has %d columns but %d values were supplied" % (table.name, len(cols), compiled.ncolumns) if not ast.columns else
                                       "%d values for %d columns" % (compiled.ncolumns, len(cols)))
            source_rows = [list(r) for r in compiled.run()]
        else:
            scope = Scope()
            ec = ExprCompiler(self.ctx, scope, self.compiler().compile_subselect)
            fns = []
            for row in ast.rows:
                if len(row) != len(cols):
                    raise OperationalError("%d values for %d columns" % (len(row), len(cols)) if ast.columns else "table %s has %d columns but %d values were supplied" % (
                        table.name, len(cols), len(row)))
                fns.append([ec.compile(e) for e in row])
            source_rows = [[f(()) for f in row] for row in fns]
        defaults = self.default_functions(table)
        count = 0
        for src in source_rows:
            values = [None] * len(table.columns)
            given = set()
            rowid = None
            for ci, v in zip(cols, src):
                if ci == -1:
                    rowid = v
                else:
                    values[ci] = v
                    given.add(ci)
            for i in range(len(table.columns)):
                if i not in given:
                    values[i] = defaults[i]()
            if self.insert_row(table, values, rowid, ast.action):
                count += 1
        self.changes = count
        self.total_changes += count
        return Result(rowcount=count, lastrowid=self.last_insert_rowid)

    def default_functions(self, table):
        out = []
        for c in table.columns:
            d = c.default
            if d is None:
                out.append(lambda: None)
            elif isinstance(d, A.Lit):
                v = d.value
                out.append(lambda v=v: v)
            else:
                f = ExprCompiler(self.ctx, Scope(), None).compile(d)
                out.append(lambda f=f: f(()))
        return out

    def check_functions(self, table):
        cache = getattr(table, "_checks", None)
        if cache is None:
            cache = [(self.table_expr(table, e), e) for e, _ in table.checks]
            table._checks = cache
        return cache

    def insert_row(self, table, values, rowid, action):
        """Add one row, applying affinity and checking the constraints. ``action`` is the conflict clause (None for the default, which is to fail). Gives whether a row was added."""
        tree = self.table_tree(table)
        ipk = table.ipk
        if ipk is not None and rowid is None:
            v = values[ipk]
            if v is not None:
                v = V.apply_affinity(v, V.INTEGER)
                if type(v) is not int:
                    raise IntegrityError("datatype mismatch")
                rowid = v
        elif rowid is not None:
            rowid = V.apply_affinity(rowid, V.INTEGER)
            if type(rowid) is not int:
                raise IntegrityError("datatype mismatch")
        for i, c in enumerate(table.columns):
            if i != ipk:
                values[i] = V.apply_affinity(values[i], c.affinity)
                if type(values[i]) is float and values[i] != values[i]:
                    values[i] = None
        seq_info = self.sequence(table) if table.autoincrement else None
        if rowid is None:
            mx = tree.max_rowid()
            if seq_info is not None:
                mx = max(mx, seq_info[1])
            if mx >= INT_MAX and seq_info is not None:
                raise DatabaseError("database or disk is full")
            if mx >= INT_MAX:
                rowid = random.randint(1, INT_MAX)
                while tree.get(rowid) is not None:
                    rowid = random.randint(1, INT_MAX)
            else:
                rowid = mx + 1
        if ipk is not None:
            values[ipk] = rowid
        try:
            check_not_null(table, values)
        except IntegrityError:
            if action == "IGNORE":
                return False
            if action == "REPLACE":
                for i, c in enumerate(table.columns):
                    if c.not_null and values[i] is None and i != ipk:
                        d = default_value(c)
                        if d is None:
                            raise
                        values[i] = d
            else:
                raise
        full = tuple(values) + (rowid,)
        for f, e in self.check_functions(table):
            if f(full) is not None and truth(f(full)) is False:
                if action == "IGNORE":
                    return False
                raise IntegrityError("CHECK constraint failed: %s" % table.name)
        # conflicts with rows that are there already
        conflicts = []
        if tree.get(rowid) is not None:
            conflicts.append((rowid, "%s.%s" % (table.name, table.columns[ipk].name) if ipk is not None else "%s.rowid" % table.name))
        for index in table.indexes:
            if not index.unique:
                continue
            if index.where is not None:
                continue
            key = index_key(index, values, rowid)
            if any(k is None for k in key[:-1]):
                continue
            itree = self.index_tree(index)
            for found in itree.scan(key[:-1], key[:-1]):
                conflicts.append((found[-1], unique_message(table, index)[len("UNIQUE constraint failed: "):]))
                break
        if conflicts:
            if action == "IGNORE":
                return False
            if action == "REPLACE":
                for victim, _ in conflicts:
                    self.delete_row(table, victim)
            else:
                raise IntegrityError("UNIQUE constraint failed: " + conflicts[0][1])
        tree.insert(rowid, record_for_row(table, values))
        for index in table.indexes:
            if index.where is not None and truth(self.index_where(index)(full)) is not True:
                continue
            key = index_key(index, values, rowid)
            self.index_tree(index).insert(key, encode_record(key))
        if seq_info is not None and rowid > seq_info[1]:
            self.set_sequence(table, seq_info[0], rowid)
        self.last_insert_rowid = rowid
        return True

    def sequence(self, table):
        """``(rowid of the row in sqlite_sequence or None, the largest rowid this AUTOINCREMENT table has ever had)``."""
        seq = self.schema.tables.get("sqlite_sequence")
        if seq is None:
            return None, 0
        for rowid, rec in self.table_tree(seq).scan():
            v = decode_record(rec)
            if v[0] == table.name:
                return rowid, int(v[1] or 0)
        return None, 0

    def set_sequence(self, table, seq_rowid, value):
        tree = self.table_tree(self.schema.tables["sqlite_sequence"])
        if seq_rowid is None:
            seq_rowid = tree.max_rowid() + 1
        tree.insert(seq_rowid, encode_record([table.name, value]))

    def index_where(self, index):
        f = getattr(index, "_where_f", None)
        if f is None:
            f = self.table_expr(index.table, index.where)
            index._where_f = f
        return f

    def delete_row(self, table, rowid):
        tree = self.table_tree(table)
        rec = tree.get(rowid)
        if rec is None:
            return False
        row = row_from_record(table, rowid, rec)
        full = tuple(row)
        for index in table.indexes:
            if index.where is not None and truth(self.index_where(index)(full)) is not True:
                continue
            self.index_tree(index).delete(index_key(index, row, rowid))
        tree.delete(rowid)
        return True

    # --- UPDATE and DELETE ------------------------------------------------------------------------------------------------------------------------------
    def run_update(self, ast):
        table = self.writable_table(ast.table)
        ipk = table.ipk
        scope = Scope()
        scope.add(Source(table.name, table.names, [c.affinity for c in table.columns], table_collations(table), table))
        ec = ExprCompiler(self.ctx, scope, self.compiler().compile_subselect)
        sets = []
        for name, expr in ast.assignments:
            ci = table.column_index(name)
            if ci is None:
                if name.lower() in ("rowid", "oid", "_rowid_") and ipk is None:
                    ci = -1
                else:
                    raise OperationalError("no such column: %s" % name)
            sets.append((ci, ec.compile(expr)))
        rows = self.matching_rows(table, ast.where)
        count = 0
        n = len(table.columns)
        for row in rows:
            old_values, old_rowid = list(row[:n]), row[n]
            new_values = list(old_values)
            new_rowid = old_rowid
            scope.cell[0] = row
            for ci, f in sets:
                v = f(row)
                if ci == -1:
                    new_rowid = V.apply_affinity(v, V.INTEGER)
                    if type(new_rowid) is not int:
                        raise IntegrityError("datatype mismatch")
                else:
                    new_values[ci] = v
            for i, c in enumerate(table.columns):
                if i != ipk:
                    new_values[i] = V.apply_affinity(new_values[i], c.affinity)
            if ipk is not None:
                v = V.apply_affinity(new_values[ipk], V.INTEGER)
                if type(v) is not int:
                    raise IntegrityError("datatype mismatch")
                new_rowid = v
                new_values[ipk] = v
            if self.update_row(table, old_values, old_rowid, new_values, new_rowid, ast.action):
                count += 1
        self.changes = count
        self.total_changes += count
        return Result(rowcount=count)

    def update_row(self, table, old_values, old_rowid, new_values, new_rowid, action):
        try:
            check_not_null(table, new_values)
        except IntegrityError:
            if action == "IGNORE":
                return False
            raise
        full = tuple(new_values) + (new_rowid,)
        for f, e in self.check_functions(table):
            if f(full) is not None and truth(f(full)) is False:
                if action == "IGNORE":
                    return False
                raise IntegrityError("CHECK constraint failed: %s" % table.name)
        tree = self.table_tree(table)
        if new_rowid != old_rowid and tree.get(new_rowid) is not None:
            if action == "IGNORE":
                return False
            if action == "REPLACE":
                self.delete_row(table, new_rowid)
            else:
                raise IntegrityError("UNIQUE constraint failed: %s.%s" % (table.name, table.columns[table.ipk].name if table.ipk is not None else "rowid"))
        for index in table.indexes:
            if not index.unique or index.where is not None:
                continue
            new_key = index_key(index, new_values, new_rowid)
            if any(k is None for k in new_key[:-1]):
                continue
            if new_key[:-1] == index_key(index, old_values, old_rowid)[:-1] and new_rowid == old_rowid:
                continue
            for found in self.index_tree(index).scan(new_key[:-1], new_key[:-1]):
                if found[-1] != old_rowid:
                    if action == "IGNORE":
                        return False
                    if action == "REPLACE":
                        self.delete_row(table, found[-1])
                        break
                    raise IntegrityError(unique_message(table, index))
                break
        old_full = tuple(old_values) + (old_rowid,)
        for index in table.indexes:
            old_in = index.where is None or truth(self.index_where(index)(old_full)) is True
            new_in = index.where is None or truth(self.index_where(index)(full)) is True
            old_key, new_key = index_key(index, old_values, old_rowid), index_key(index, new_values, new_rowid)
            itree = self.index_tree(index)
            if old_in and (not new_in or old_key != new_key):
                itree.delete(old_key)
            if new_in and (not old_in or old_key != new_key):
                itree.insert(new_key, encode_record(new_key))
        if new_rowid != old_rowid:
            tree.delete(old_rowid)
        tree.insert(new_rowid, record_for_row(table, new_values))
        return True

    def run_delete(self, ast):
        table = self.writable_table(ast.table)
        n = len(table.columns)
        if ast.where is None:
            count = self.table_tree(table).count()
            for index in table.indexes:
                self.index_tree(index).clear()
            self.table_tree(table).clear()
        else:
            rows = self.matching_rows(table, ast.where)
            count = 0
            for row in rows:
                if self.delete_row(table, row[n]):
                    count += 1
        self.changes = count
        self.total_changes += count
        return Result(rowcount=count)


def quote_ident(name):
    if name and (name[0].isalpha() or name[0] == "_") and all(c.isalnum() or c == "_" for c in name):
        return name
    return '"' + name.replace('"', '""') + '"'


def bind_parameters(params, parsed):
    """The parameters of a statement as a list indexed by parameter number - 1."""
    n = parsed.nparams
    if isinstance(params, dict):
        out = [None] * n
        names = {i: name for name, i in parsed.names.items()}
        for i in range(1, n + 1):
            if i not in names:
                raise DatabaseError("you did not supply a value for binding parameter %d" % i)
            if names[i] not in params:
                raise DatabaseError("you did not supply a value for binding parameter :%s" % names[i])
            out[i - 1] = params[names[i]]
        return out
    params = list(params)
    if len(params) != n:
        raise DatabaseError("Incorrect number of bindings supplied. The current statement uses %d, and there are %d supplied." % (n, len(params)))
    return params
