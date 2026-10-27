"""The SQL parser: text to the trees of ``sqlast``, by recursive descent, with a precedence climbing parser for expressions (the precedences are SQLite's: ``||`` binds
tighter than ``*``, comparisons are below ``&``/``|``/shifts and above ``AND``, ``NOT`` is just above ``AND``)."""
from __future__ import annotations

from . import sqlast as A
from .errors import SQLSyntaxError
from .format import INT_MIN
from .lexer import BLOB, EOF, FLOAT, ID, INT, OP, PARAM, QID, STR, tokenize

# words that cannot be used as a bare alias or name where one is optional
RESERVED = {"SELECT", "FROM", "WHERE", "GROUP", "ORDER", "HAVING", "LIMIT", "OFFSET", "UNION", "INTERSECT", "EXCEPT", "JOIN", "ON", "USING", "LEFT", "INNER", "CROSS", "NATURAL",
            "OUTER", "RIGHT", "FULL", "AS", "AND", "OR", "NOT", "IN", "IS", "LIKE", "GLOB", "BETWEEN", "CASE", "WHEN", "THEN", "ELSE", "END", "NULL", "SET", "VALUES", "INTO",
            "DISTINCT", "ALL", "ISNULL", "NOTNULL", "ESCAPE", "COLLATE", "CAST", "EXISTS", "DEFAULT", "PRIMARY", "UNIQUE", "CHECK", "REFERENCES", "CONSTRAINT", "WITH", "RETURNING"}

CONSTRAINT_WORDS = {"PRIMARY", "NOT", "NULL", "UNIQUE", "CHECK", "DEFAULT", "COLLATE", "REFERENCES", "CONSTRAINT", "GENERATED", "AS"}
EQUALITY = {"=": "=", "==": "=", "!=": "!=", "<>": "!="}
RELATIONAL = {"<", "<=", ">", ">="}
BITWISE = {"&", "|", "<<", ">>"}


class ParsedStatement:
    __slots__ = ("ast", "nparams", "names", "text")

    def __init__(self, ast, nparams, names, text):
        self.ast, self.nparams, self.names, self.text = ast, nparams, names, text


class Parser:
    def __init__(self, sql):
        self.sql = sql
        self.tokens = tokenize(sql)
        self.i = 0
        self.param_count = 0
        self.param_names = {}

    # --- tokens --------------------------------------------------------------------------------------------------------------------------------
    def peek(self, k=0):
        j = self.i + k
        return self.tokens[j] if j < len(self.tokens) else self.tokens[-1]

    def next(self):
        t = self.tokens[self.i]
        if t.kind != EOF:
            self.i += 1
        return t

    def error(self, tok=None):
        tok = tok or self.peek()
        if tok.kind == EOF:
            return SQLSyntaxError("incomplete input")
        return SQLSyntaxError('near "%s": syntax error' % self.sql[tok.pos:tok.end])

    def is_kw(self, *words, k=0):
        t = self.peek(k)
        return t.kind == ID and t.value.upper() in words

    def accept_kw(self, *words):
        if self.is_kw(*words):
            return self.next().value.upper()
        return None

    def expect_kw(self, *words):
        if not self.is_kw(*words):
            raise self.error()
        return self.next().value.upper()

    def is_op(self, *ops, k=0):
        t = self.peek(k)
        return t.kind == OP and t.value in ops

    def accept_op(self, *ops):
        if self.is_op(*ops):
            return self.next().value
        return None

    def expect_op(self, op):
        if not self.is_op(op):
            raise self.error()
        return self.next()

    def name(self, what="a name"):
        """An identifier: bare (not a reserved word), or quoted."""
        t = self.peek()
        if t.kind == QID or (t.kind == ID and t.value.upper() not in RESERVED):
            return self.next().value
        raise self.error()

    def any_name(self):
        t = self.peek()
        if t.kind in (ID, QID):
            return self.next().value
        raise self.error()

    # --- statements ----------------------------------------------------------------------------------------------------------------------------
    def parse_script(self):
        """All the statements of the text. Each is a ``ParsedStatement``: the tree, how many parameters it has and the names of the named ones."""
        out = []
        while True:
            while self.accept_op(";"):
                pass
            if self.peek().kind == EOF:
                return out
            self.param_count = 0
            self.param_names = {}
            start = self.peek().pos
            stmt = self.statement()
            if self.peek().kind != EOF and not self.is_op(";"):
                raise self.error()
            out.append(ParsedStatement(stmt, self.param_count, dict(self.param_names), self.sql[start:self.tokens[self.i - 1].end]))

    def parse_one(self):
        """One statement as a tree (a script of several is an error)."""
        return self.parse_statement().ast

    def parse_statement(self):
        out = self.parse_script()
        if len(out) != 1:
            raise SQLSyntaxError("exactly one SQL statement is expected" if out else "no SQL statement")
        return out[0]

    def statement(self):
        t = self.peek()
        if t.kind != ID:
            raise self.error()
        w = t.value.upper()
        if w == "EXPLAIN":
            self.next()
            plan = False
            if self.accept_kw("QUERY"):
                self.expect_kw("PLAN")
                plan = True
            return A.Explain(self.statement(), plan)
        if w in ("SELECT", "WITH", "VALUES"):
            return self.select()
        if w == "CREATE":
            return self.create()
        if w == "DROP":
            return self.drop()
        if w in ("INSERT", "REPLACE"):
            return self.insert()
        if w == "UPDATE":
            return self.update()
        if w == "DELETE":
            return self.delete()
        if w == "BEGIN":
            self.next()
            mode = self.accept_kw("DEFERRED", "IMMEDIATE", "EXCLUSIVE")
            self.accept_kw("TRANSACTION")
            return A.Begin(mode)
        if w in ("COMMIT", "END"):
            self.next()
            self.accept_kw("TRANSACTION")
            return A.Commit()
        if w == "ROLLBACK":
            self.next()
            self.accept_kw("TRANSACTION")
            return A.Rollback()
        if w == "PRAGMA":
            return self.pragma()
        if w == "ALTER":
            return self.alter()
        if w == "VACUUM":
            self.next()
            return A.Vacuum()
        if w == "ANALYZE":
            self.next()
            while self.peek().kind != EOF and not self.is_op(";"):
                self.next()
            return A.Pragma("analyze", None)
        raise self.error()

    def pragma(self):
        self.next()
        name = self.name().lower()
        if self.accept_op("."):
            name = self.name().lower()
        arg = None
        if self.accept_op("="):
            arg = self.pragma_value()
        elif self.accept_op("("):
            arg = self.pragma_value()
            self.expect_op(")")
        return A.Pragma(name, arg)

    def pragma_value(self):
        sign = 1
        if self.accept_op("-"):
            sign = -1
        elif self.accept_op("+"):
            pass
        t = self.next()
        if t.kind in (INT, FLOAT):
            return sign * t.value
        if t.kind in (STR, ID, QID):
            return t.value
        raise self.error(t)

    def alter(self):
        self.next()
        self.expect_kw("TABLE")
        table = self.name()
        if self.accept_kw("RENAME"):
            if self.accept_kw("TO"):
                return A.AlterTable(table, "rename", self.name())
            self.accept_kw("COLUMN")
            old = self.name()
            self.expect_kw("TO")
            return A.AlterTable(table, "rename_column", (old, self.name()))
        if self.accept_kw("ADD"):
            self.accept_kw("COLUMN")
            return A.AlterTable(table, "add_column", self.column_def())
        raise self.error()

    # --- CREATE and DROP -----------------------------------------------------------------------------------------------------------------------
    def create(self):
        self.next()
        unique = bool(self.accept_kw("UNIQUE"))
        if self.accept_kw("TEMP", "TEMPORARY"):
            raise SQLSyntaxError("temporary tables are not supported")
        if self.is_kw("INDEX"):
            return self.create_index(unique)
        if unique:
            raise self.error()
        if self.accept_kw("VIEW"):
            return self.create_view()
        if self.is_kw("TRIGGER"):
            raise SQLSyntaxError("triggers are not supported")
        self.expect_kw("TABLE")
        if_not_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            if_not_exists = True
        name_tok = self.peek()
        name = self.name()
        if self.accept_op("."):                                            # schema.table: only "main" makes sense
            name_tok = self.peek()
            name = self.name()
        if self.accept_kw("AS"):
            return A.CreateTable(name, None, None, if_not_exists, None, self.select())
        self.expect_op("(")
        columns, constraints = [], []
        while True:
            if self.is_kw("PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT") and not (self.peek(1).kind == OP and self.peek(1).value in (",", ")")):
                constraints.append(self.table_constraint())
            else:
                columns.append(self.column_def())
            if not self.accept_op(","):
                break
        self.expect_op(")")
        if self.accept_kw("WITHOUT"):
            raise SQLSyntaxError("WITHOUT ROWID tables are not supported")
        end = self.peek().pos
        sql = "CREATE TABLE " + self.sql[name_tok.pos:end].rstrip()
        return A.CreateTable(name, columns, constraints, if_not_exists, sql, None)

    def create_view(self):
        if_not_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            if_not_exists = True
        name_tok = self.peek()
        name = self.name()
        cols = None
        if self.accept_op("("):
            cols = []
            while True:
                cols.append(self.name())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
        self.expect_kw("AS")
        select = self.select()
        end = self.peek().pos
        sql = "CREATE VIEW " + self.sql[name_tok.pos:end].rstrip()
        return A.CreateView(name, cols, select, if_not_exists, sql)

    def type_name(self):
        words = []
        while self.peek().kind in (ID, QID) and not (self.peek().kind == ID and self.peek().value.upper() in CONSTRAINT_WORDS):
            words.append(self.next().value)
        if words and self.accept_op("("):
            args = []
            while not self.is_op(")"):
                t = self.next()
                if t.kind == EOF:
                    raise self.error()
                args.append(self.sql[t.pos:t.end])
            self.next()
            words[-1] += "(" + "".join(args) + ")"
        return " ".join(words) if words else None

    def column_def(self):
        start = self.peek().pos
        c = A.ColumnDef(self.name(), None, False, False, False, False, None, None, None, False)
        c.typename = self.type_name()
        while True:
            if self.accept_kw("CONSTRAINT"):
                self.name()
            if self.accept_kw("PRIMARY"):
                self.expect_kw("KEY")
                c.primary_key = True
                if self.accept_kw("DESC"):
                    c.desc = True
                else:
                    self.accept_kw("ASC")
                self.conflict_clause()
                if self.accept_kw("AUTOINCREMENT"):
                    c.autoincrement = True
            elif self.accept_kw("NOT"):
                self.expect_kw("NULL")
                c.not_null = True
                self.conflict_clause()
            elif self.accept_kw("NULL"):
                self.conflict_clause()
            elif self.accept_kw("UNIQUE"):
                c.unique = True
                self.conflict_clause()
            elif self.accept_kw("CHECK"):
                self.expect_op("(")
                c.check = self.expr()
                self.expect_op(")")
            elif self.accept_kw("DEFAULT"):
                dstart = self.peek().pos
                c.default = self.default_value()
                c.default_text = self.sql[dstart:self.tokens[self.i - 1].end]
            elif self.accept_kw("COLLATE"):
                c.collation = self.any_name().upper()
            elif self.accept_kw("REFERENCES"):
                self.skip_references()
            else:
                c.text = self.sql[start:self.tokens[self.i - 1].end]
                return c

    def conflict_clause(self):
        if self.accept_kw("ON"):
            self.expect_kw("CONFLICT")
            self.expect_kw("ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE")

    def skip_references(self):
        self.any_name()
        if self.accept_op("("):
            while not self.accept_op(")"):
                if self.next().kind == EOF:
                    raise self.error()
        while self.is_kw("ON", "MATCH", "NOT", "DEFERRABLE", "INITIALLY"):
            self.next()
            if self.peek().kind == ID and not self.is_kw("ON", "MATCH", "NOT", "DEFERRABLE", "INITIALLY"):
                self.next()
                if self.is_kw("ACTION", "NULL", "DEFAULT", "CASCADE", "RESTRICT"):
                    self.next()
            while self.peek().kind == ID and self.is_kw("DELETE", "UPDATE", "SET", "NO", "NULL", "DEFAULT", "CASCADE", "RESTRICT", "ACTION", "DEFERRED", "IMMEDIATE", "DEFERRABLE"):
                self.next()

    def default_value(self):
        if self.accept_op("("):
            e = self.expr()
            self.expect_op(")")
            return e
        sign = 1
        if self.accept_op("-"):
            sign = -1
        elif self.accept_op("+"):
            pass
        t = self.next()
        if t.kind in (INT, FLOAT):
            v = t.value
            if t.kind == INT and v == 1 << 63:
                v = float(v) if sign == 1 else INT_MIN
                return A.Lit(v)
            return A.Lit(sign * v)
        if t.kind == STR or t.kind == BLOB:
            return A.Lit(t.value)
        if t.kind == ID:
            w = t.value.upper()
            if w == "NULL":
                return A.Lit(None)
            if w in ("TRUE", "FALSE"):
                return A.Lit(1 if w == "TRUE" else 0)
            if w in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME"):
                return A.Func(w.lower(), [], False, False)
        raise self.error(t)

    def table_constraint(self):
        if self.accept_kw("CONSTRAINT"):
            self.name()
        if self.accept_kw("PRIMARY"):
            self.expect_kw("KEY")
            cols = self.indexed_columns()
            self.conflict_clause()
            return A.TableConstraint("PRIMARY KEY", cols, None)
        if self.accept_kw("UNIQUE"):
            cols = self.indexed_columns()
            self.conflict_clause()
            return A.TableConstraint("UNIQUE", cols, None)
        if self.accept_kw("CHECK"):
            self.expect_op("(")
            e = self.expr()
            self.expect_op(")")
            return A.TableConstraint("CHECK", None, e)
        if self.accept_kw("FOREIGN"):
            self.expect_kw("KEY")
            self.indexed_columns()
            self.expect_kw("REFERENCES")
            self.skip_references()
            return A.TableConstraint("FOREIGN KEY", None, None)
        raise self.error()

    def indexed_columns(self):
        """``(a, b COLLATE NOCASE DESC)`` as [(name or expression, collation, descending)]."""
        self.expect_op("(")
        cols = []
        while True:
            e = self.expr()
            coll, desc = None, False
            if isinstance(e, A.Collate):
                e, coll = e.expr, e.collation
            if self.accept_kw("COLLATE"):
                coll = self.any_name().upper()
            if self.accept_kw("DESC"):
                desc = True
            else:
                self.accept_kw("ASC")
            cols.append((e.name if isinstance(e, A.Col) and e.table is None else e, coll, desc))
            if not self.accept_op(","):
                break
        self.expect_op(")")
        return cols

    def create_index(self, unique):
        self.expect_kw("INDEX")
        if_not_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("NOT")
            self.expect_kw("EXISTS")
            if_not_exists = True
        name_tok = self.peek()
        name = self.name()
        self.expect_kw("ON")
        table = self.name()
        cols = self.indexed_columns()
        where = None
        if self.accept_kw("WHERE"):
            where = self.expr()
        end = self.peek().pos
        sql = "CREATE %sINDEX %s" % ("UNIQUE " if unique else "", self.sql[name_tok.pos:end].rstrip())
        return A.CreateIndex(name, table, cols, unique, if_not_exists, sql, where)

    def drop(self):
        self.next()
        kind = self.expect_kw("TABLE", "INDEX", "VIEW", "TRIGGER")
        if kind == "TRIGGER":
            raise SQLSyntaxError("triggers are not supported")
        if_exists = False
        if self.accept_kw("IF"):
            self.expect_kw("EXISTS")
            if_exists = True
        name = self.name()
        if self.accept_op("."):
            name = self.name()
        return {"TABLE": A.DropTable, "INDEX": A.DropIndex, "VIEW": A.DropView}[kind](name, if_exists)

    # --- DML -----------------------------------------------------------------------------------------------------------------------------------
    def insert(self):
        verb = self.next().value.upper()
        action = "REPLACE" if verb == "REPLACE" else None
        if verb == "INSERT" and self.accept_kw("OR"):
            action = self.expect_kw("ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE")
        self.expect_kw("INTO")
        table = self.name()
        if self.accept_op("."):
            table = self.name()
        columns = None
        if self.is_op("(") and not self.is_kw("SELECT", k=1):
            self.next()
            columns = []
            while True:
                columns.append(self.name())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
        if self.accept_kw("DEFAULT"):
            self.expect_kw("VALUES")
            return A.Insert(table, columns, None, None, True, action)
        if self.is_kw("SELECT", "WITH"):
            return A.Insert(table, columns, None, self.select(), False, action)
        self.expect_kw("VALUES")
        rows = []
        while True:
            self.expect_op("(")
            row = []
            while True:
                row.append(self.expr())
                if not self.accept_op(","):
                    break
            self.expect_op(")")
            rows.append(row)
            if not self.accept_op(","):
                break
        return A.Insert(table, columns, rows, None, False, action)

    def update(self):
        self.next()
        action = None
        if self.accept_kw("OR"):
            action = self.expect_kw("ROLLBACK", "ABORT", "FAIL", "IGNORE", "REPLACE")
        table = self.name()
        self.expect_kw("SET")
        assignments = []
        while True:
            assignments.append((self.name(), None))
            self.expect_op("=")
            assignments[-1] = (assignments[-1][0], self.expr())
            if not self.accept_op(","):
                break
        where = self.expr() if self.accept_kw("WHERE") else None
        return A.Update(table, assignments, where, action)

    def delete(self):
        self.next()
        self.expect_kw("FROM")
        table = self.name()
        where = self.expr() if self.accept_kw("WHERE") else None
        return A.Delete(table, where)

    # --- SELECT ------------------------------------------------------------------------------------------------------------------------------------
    def select(self):
        ctes = []
        if self.accept_kw("WITH"):
            if self.is_kw("RECURSIVE"):
                raise SQLSyntaxError("recursive common table expressions are not supported")
            while True:
                name = self.name()
                cols = None
                if self.accept_op("("):
                    cols = []
                    while True:
                        cols.append(self.name())
                        if not self.accept_op(","):
                            break
                    self.expect_op(")")
                self.expect_kw("AS")
                self.expect_op("(")
                ctes.append((name, cols, self.select()))
                self.expect_op(")")
                if not self.accept_op(","):
                    break
        cores, ops = [self.select_core()], []
        while self.is_kw("UNION", "INTERSECT", "EXCEPT"):
            op = self.next().value.upper()
            if op == "UNION" and self.accept_kw("ALL"):
                op = "UNION ALL"
            ops.append(op)
            cores.append(self.select_core())
        order = []
        if self.accept_kw("ORDER"):
            self.expect_kw("BY")
            while True:
                e = self.expr()
                desc = False
                if self.accept_kw("DESC"):
                    desc = True
                else:
                    self.accept_kw("ASC")
                nulls = None
                if self.accept_kw("NULLS"):
                    nulls = self.expect_kw("FIRST", "LAST")
                order.append((e, desc, nulls))
                if not self.accept_op(","):
                    break
        limit = offset = None
        if self.accept_kw("LIMIT"):
            limit = self.expr()
            if self.accept_kw("OFFSET"):
                offset = self.expr()
            elif self.accept_op(","):
                offset, limit = limit, self.expr()
        return A.Select(cores, ops, order, limit, offset, ctes)

    def select_core(self):
        if self.accept_kw("VALUES"):
            rows = []
            while True:
                self.expect_op("(")
                row = []
                while True:
                    row.append(self.expr())
                    if not self.accept_op(","):
                        break
                self.expect_op(")")
                rows.append(row)
                if not self.accept_op(","):
                    break
            return A.SelectCore(False, None, None, None, None, None, rows)
        self.expect_kw("SELECT")
        distinct = False
        if self.accept_kw("DISTINCT"):
            distinct = True
        else:
            self.accept_kw("ALL")
        columns = []
        while True:
            columns.append(self.result_column())
            if not self.accept_op(","):
                break
        sources = where = having = None
        group = []
        if self.accept_kw("FROM"):
            sources = self.from_clause()
        if self.accept_kw("WHERE"):
            where = self.expr()
        if self.accept_kw("GROUP"):
            self.expect_kw("BY")
            while True:
                group.append(self.expr())
                if not self.accept_op(","):
                    break
        if self.accept_kw("HAVING"):
            having = self.expr()
        return A.SelectCore(distinct, columns, sources, where, group, having, None)

    def result_column(self):
        if self.accept_op("*"):
            return A.ResultCol(None, None, None, True, '*')
        if self.peek().kind in (ID, QID) and self.is_op(".", k=1) and self.is_op("*", k=2):
            t = self.next().value
            self.next()
            self.next()
            return A.ResultCol(None, None, t, True, t + '.*')
        start = self.peek().pos
        e = self.expr()
        text = self.sql[start:self.tokens[self.i - 1].end]
        alias = None
        if self.accept_kw("AS"):
            alias = self.any_name()
        elif self.peek().kind == QID or (self.peek().kind == ID and self.peek().value.upper() not in RESERVED):
            alias = self.next().value
        return A.ResultCol(e, alias, None, False, text)

    def from_clause(self):
        left = self.table_or_subquery()
        while True:
            if self.accept_op(","):
                left = A.Join(left, self.table_or_subquery(), "CROSS", None, None, False)
                continue
            natural = bool(self.accept_kw("NATURAL"))
            kind = None
            if self.accept_kw("LEFT"):
                self.accept_kw("OUTER")
                kind = "LEFT"
            elif self.accept_kw("INNER"):
                kind = "INNER"
            elif self.accept_kw("CROSS"):
                kind = "CROSS"
            elif self.is_kw("RIGHT", "FULL"):
                raise SQLSyntaxError("RIGHT and FULL OUTER JOINs are not supported")
            if kind is None and not self.is_kw("JOIN"):
                if natural:
                    raise self.error()
                return left
            self.expect_kw("JOIN")
            right = self.table_or_subquery()
            on = using = None
            if self.accept_kw("ON"):
                on = self.expr()
            elif self.accept_kw("USING"):
                self.expect_op("(")
                using = []
                while True:
                    using.append(self.name())
                    if not self.accept_op(","):
                        break
                self.expect_op(")")
            left = A.Join(left, right, kind or "INNER", on, using, natural)

    def table_or_subquery(self):
        if self.accept_op("("):
            if self.is_kw("SELECT", "WITH", "VALUES"):
                sel = self.select()
                self.expect_op(")")
                return A.SubqueryRef(sel, self.alias())
            inner = self.from_clause()
            self.expect_op(")")
            return inner
        name = self.name()
        if self.accept_op("."):
            name = self.name()
        alias = self.alias()
        if self.accept_kw("INDEXED"):
            self.expect_kw("BY")
            self.name()
        elif self.accept_kw("NOT"):
            self.expect_kw("INDEXED")
        return A.TableRef(name, alias)

    def alias(self):
        if self.accept_kw("AS"):
            return self.any_name()
        t = self.peek()
        if t.kind == QID or (t.kind == ID and t.value.upper() not in RESERVED and t.value.upper() not in ("INDEXED",)):
            return self.next().value
        return None

    # --- expressions ---------------------------------------------------------------------------------------------------------------------------
    def expr(self, min_bp=0):
        left = self.unary()
        while True:
            t = self.peek()
            if t.kind == OP:
                op = t.value
                if op in EQUALITY:
                    bp, rbp = 4, 5
                elif op in RELATIONAL:
                    bp, rbp = 5, 6
                elif op in BITWISE:
                    bp, rbp = 6, 7
                elif op in ("+", "-"):
                    bp, rbp = 7, 8
                elif op in ("*", "/", "%"):
                    bp, rbp = 8, 9
                elif op == "||":
                    bp, rbp = 9, 10
                else:
                    return left
                if bp < min_bp:
                    return left
                self.next()
                right = self.expr(rbp)
                left = A.Binary(EQUALITY.get(op, op), left, right)
                continue
            if t.kind != ID:
                return left
            w = t.value.upper()
            if w == "OR":
                bp = 1
            elif w == "AND":
                bp = 2
            elif w in ("IS", "IN", "LIKE", "GLOB", "BETWEEN", "ISNULL", "NOTNULL", "NOT", "COLLATE", "MATCH", "REGEXP"):
                bp = 10 if w == "COLLATE" else 4
            else:
                return left
            if bp < min_bp:
                return left
            if w == "OR" or w == "AND":
                self.next()
                left = A.Binary(w, left, self.expr(bp + 1))
            elif w == "COLLATE":
                self.next()
                left = A.Collate(left, self.any_name().upper())
            elif w == "ISNULL":
                self.next()
                left = A.IsNull(left, False)
            elif w == "NOTNULL":
                self.next()
                left = A.IsNull(left, True)
            elif w == "IS":
                self.next()
                negated = bool(self.accept_kw("NOT"))
                if self.accept_kw("DISTINCT"):
                    self.expect_kw("FROM")
                    negated = not negated
                right = self.expr(5)
                left = A.Binary("IS NOT" if negated else "IS", left, right)
            else:
                negated = False
                if w == "NOT":
                    if not (self.is_kw("IN", "LIKE", "GLOB", "BETWEEN", "NULL", "MATCH", "REGEXP", k=1)):
                        return left
                    self.next()
                    negated = True
                    w = self.peek().value.upper()
                    if w == "NULL":
                        self.next()
                        left = A.IsNull(left, True)
                        continue
                self.next()
                if w == "BETWEEN":
                    low = self.expr(5)
                    self.expect_kw("AND")
                    left = A.Between(left, low, self.expr(5), negated)
                elif w == "IN":
                    left = self.in_rest(left, negated)
                elif w in ("LIKE", "GLOB"):
                    pattern = self.expr(5)
                    escape = self.expr(5) if self.accept_kw("ESCAPE") else None
                    left = A.Like(w, left, pattern, escape, negated)
                else:
                    raise SQLSyntaxError("%s is not supported" % w)

    def in_rest(self, left, negated):
        if self.accept_op("("):
            if self.is_kw("SELECT", "WITH", "VALUES"):
                sel = self.select()
                self.expect_op(")")
                return A.InSelect(left, sel, negated)
            items = []
            if not self.is_op(")"):
                while True:
                    items.append(self.expr())
                    if not self.accept_op(","):
                        break
            self.expect_op(")")
            return A.InList(left, items, negated)
        name = self.name()                                                  # x IN tablename
        return A.InSelect(left, A.Select([A.SelectCore(False, [A.ResultCol(None, None, None, True, '*')], A.TableRef(name, None), None, [], None, None)], [], [], None, None, []),
                          negated)

    def unary(self):
        t = self.peek()
        if t.kind == OP and t.value in ("-", "+", "~"):
            self.next()
            if t.value == "-" and self.peek().kind == INT and self.peek().value == 1 << 63:
                self.next()
                return A.Lit(INT_MIN)
            operand = self.expr(11)
            if t.value == "-" and isinstance(operand, A.Lit) and type(operand.value) in (int, float) and operand.value is not True:
                return A.Lit(0 - operand.value) if not (type(operand.value) is int and operand.value == INT_MIN) else A.Unary("-", operand)
            return A.Unary(t.value, operand)
        if t.kind == ID and t.value.upper() == "NOT":
            self.next()
            if self.is_kw("EXISTS"):
                return self.exists(True)
            return A.Unary("NOT", self.expr(3))
        return self.primary()

    def exists(self, negated):
        self.expect_kw("EXISTS")
        self.expect_op("(")
        sel = self.select()
        self.expect_op(")")
        return A.Exists(sel, negated)

    def primary(self):
        t = self.next()
        k = t.kind
        if k == INT:
            return A.Lit(float(t.value) if t.value == 1 << 63 else t.value)
        if k == FLOAT or k == STR or k == BLOB:
            return A.Lit(t.value)
        if k == PARAM:
            if t.value is None:                                            # a bare ?: the next number
                self.param_count += 1
                return A.Param(self.param_count, None)
            if isinstance(t.value, int):
                self.param_count = max(self.param_count, t.value)
                return A.Param(t.value, None)
            if t.value not in self.param_names:
                self.param_count += 1
                self.param_names[t.value] = self.param_count
            return A.Param(self.param_names[t.value], t.value)
        if k == OP:
            if t.value == "(":
                if self.is_kw("SELECT", "WITH", "VALUES"):
                    sel = self.select()
                    self.expect_op(")")
                    return A.Subquery(sel)
                e = self.expr()
                if self.is_op(","):
                    raise SQLSyntaxError("row values are not supported")
                self.expect_op(")")
                return e
            raise self.error(t)
        if k == QID or k == ID:
            w = t.value.upper() if k == ID else None
            if k == ID:
                if w == "NULL":
                    return A.Lit(None)
                if w == "TRUE":
                    return A.Lit(1)
                if w == "FALSE":
                    return A.Lit(0)
                if w == "CASE":
                    return self.case()
                if w == "CAST":
                    self.expect_op("(")
                    e = self.expr()
                    self.expect_kw("AS")
                    typename = self.type_name()
                    self.expect_op(")")
                    return A.Cast(e, typename or "")
                if w == "EXISTS":
                    self.i -= 1
                    return self.exists(False)
                if w in RESERVED and not self.is_op("(") and w not in ("CURRENT_TIMESTAMP",):
                    raise self.error(t)
                if w in ("CURRENT_TIMESTAMP", "CURRENT_DATE", "CURRENT_TIME") and not self.is_op("("):
                    return A.Func(w.lower(), [], False, False)
            if self.is_op("("):
                return self.function_call(t.value)
            if self.is_op(".") and self.peek(1).kind in (ID, QID):
                self.next()
                col = self.next().value
                if self.is_op(".") and self.peek(1).kind in (ID, QID):             # schema.table.column
                    self.next()
                    return A.Col(col, self.next().value)
                return A.Col(t.value, col)
            return A.Col(None, t.value)
        raise self.error(t)

    def function_call(self, name):
        self.expect_op("(")
        args, distinct, star = [], False, False
        if self.accept_op("*"):
            star = True
        elif not self.is_op(")"):
            if self.accept_kw("DISTINCT"):
                distinct = True
            while True:
                args.append(self.expr())
                if not self.accept_op(","):
                    break
        self.expect_op(")")
        if self.is_kw("OVER") or self.is_kw("FILTER"):
            raise SQLSyntaxError("window functions and FILTER are not supported")
        return A.Func(name.lower(), args, distinct, star)

    def case(self):
        base = None
        if not self.is_kw("WHEN"):
            base = self.expr()
        whens = []
        while self.accept_kw("WHEN"):
            cond = self.expr()
            self.expect_kw("THEN")
            whens.append((cond, self.expr()))
        if not whens:
            raise self.error()
        other = self.expr() if self.accept_kw("ELSE") else None
        self.expect_kw("END")
        return A.Case(base, whens, other)


def parse_script(sql):
    return Parser(sql).parse_script()


def parse_one(sql):
    return Parser(sql).parse_one()


def parse_statement(sql):
    return Parser(sql).parse_statement()
