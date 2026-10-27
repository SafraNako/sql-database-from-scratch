"""A seeded generator of random schemas, data and SQL statements, for differential testing against SQLite. It writes SQL text that both engines can run; the statements are
random enough that many are nonsense (adding text to numbers, comparing blobs with integers): the point is that both engines must agree on what nonsense means."""
import random

TYPES = ["INTEGER", "INT", "TEXT", "REAL", "NUMERIC", "BLOB", "", "VARCHAR(10)", "DOUBLE", "BOOLEAN", "FLOAT", "CHAR(3)"]
INT_LITERALS = ["0", "1", "2", "3", "5", "-1", "-2", "10", "100", "255", "2147483647", "-2147483648", "1099511627776"]
REAL_LITERALS = ["0.5", "-1.5", "2.0", "3.14159", "1e10", "-0.25", "1.0", "100.5", "1e-3", "2.5e3"]
TEXT_LITERALS = ["'a'", "'b'", "'abc'", "'ABC'", "''", "'12'", "'3.5'", "' 7 '", "'x y'", "'Hello World'", "'é'", "'abc%'", "'%b%'", "'-5'", "'1e2'", "'0x10'", "'true'"]
BLOB_LITERALS = ["x'00ff'", "x''", "x'414243'"]


class Gen:
    def __init__(self, seed):
        self.seed = seed
        self.rng = random.Random(seed)
        self.tables = {}                          # name -> list of (column, declared type)
        self.unique_counter = 0

    # --- schema and data --------------------------------------------------------------------------------------------------------------------------------
    def setup(self):
        rng = self.rng
        stmts = []
        for ti in range(rng.randint(1, 3)):
            name = "t%d" % (ti + 1)
            cols = []
            defs = []
            ncols = rng.randint(2, 4)
            for ci in range(ncols):
                cname = "c%d" % (ci + 1)
                typ = rng.choice(TYPES)
                cols.append((cname, typ))
                d = "%s %s%s" % (cname, typ, self.collation_suffix(typ))
                r = rng.random()
                if r < 0.1:
                    d += " NOT NULL DEFAULT 0"
                elif r < 0.2:
                    d += " DEFAULT 'x'"
                elif r < 0.27:
                    d += " UNIQUE"
                elif r < 0.32 and ci > 0:
                    d += " DEFAULT 1.5"
                defs.append(d)
            if rng.random() < 0.4:
                cols.insert(0, ("id", "INTEGER"))
                defs.insert(0, "id INTEGER PRIMARY KEY")
            self.tables[name] = cols
            stmts.append("CREATE TABLE %s (%s)" % (name, ", ".join(defs)))
            for k in range(rng.randint(0, 2)):
                ic = rng.sample([c[0] for c in cols], rng.randint(1, min(2, len(cols))))
                stmts.append("CREATE %sINDEX %s_i%d ON %s (%s)" % ("UNIQUE " if rng.random() < 0.15 else "", name, k, name, ", ".join(ic)))
            for _ in range(rng.randint(0, 25)):
                stmts.append(self.insert(name))
        return stmts

    def collation_suffix(self, typ):
        return ""

    def literal(self):
        rng = self.rng
        r = rng.random()
        if r < 0.3:
            return rng.choice(INT_LITERALS)
        if r < 0.45:
            return rng.choice(REAL_LITERALS)
        if r < 0.75:
            return rng.choice(TEXT_LITERALS)
        if r < 0.88:
            return "NULL"
        return rng.choice(BLOB_LITERALS)

    def insert(self, name):
        rng = self.rng
        cols = self.tables[name]
        chosen = [c for c in cols if rng.random() < 0.85] or cols[:1]
        action = rng.choice(["", "", "", "OR IGNORE ", "OR REPLACE "])
        vals = [self.literal() if not (c[0] == "id") else rng.choice(["NULL", str(rng.randint(1, 40))]) for c in chosen]
        return "INSERT %sINTO %s (%s) VALUES (%s)" % (action, name, ", ".join(c[0] for c in chosen), ", ".join(vals))

    # --- expressions -----------------------------------------------------------------------------------------------------------------------------------
    def column(self, scope):
        t, c = self.rng.choice(scope)
        return "%s.%s" % (t, c) if t else c

    def expr(self, scope, depth=0):
        rng = self.rng
        r = rng.random()
        if depth >= 3 or r < 0.22:
            return self.column(scope) if scope and rng.random() < 0.65 else self.literal()
        if r < 0.50:
            op = rng.choice(["+", "-", "*", "/", "%", "||", "&", "|", "<<", ">>", "=", "!=", "<", "<=", ">", ">=", "IS", "IS NOT", "AND", "OR"])
            return "(%s %s %s)" % (self.expr(scope, depth + 1), op, self.expr(scope, depth + 1))
        if r < 0.56:
            return "(%s%s)" % (rng.choice(["-", "NOT ", "+", "~"]), self.expr(scope, depth + 1))
        if r < 0.62:
            return "(%s %sBETWEEN %s AND %s)" % (self.expr(scope, depth + 1), rng.choice(["", "NOT "]), self.expr(scope, depth + 1), self.expr(scope, depth + 1))
        if r < 0.68:
            items = ", ".join(self.expr(scope, depth + 2) for _ in range(rng.randint(1, 4)))
            return "(%s %sIN (%s))" % (self.expr(scope, depth + 1), rng.choice(["", "NOT "]), items)
        if r < 0.73:
            return "(%s %s%s %s)" % (self.expr(scope, depth + 1), rng.choice(["", "NOT "]), rng.choice(["LIKE", "GLOB"]), rng.choice(["'a%'", "'%b%'", "'_b_'", "'A*'", "'[a-c]*'", "'%'", "''", self.column(scope) if scope else "'x'"]))
        if r < 0.77:
            return "(%s IS %sNULL)" % (self.expr(scope, depth + 1), rng.choice(["", "NOT "]))
        if r < 0.82:
            base = self.expr(scope, depth + 1) + " " if rng.random() < 0.3 else ""
            whens = " ".join("WHEN %s THEN %s" % (self.expr(scope, depth + 1), self.expr(scope, depth + 1)) for _ in range(rng.randint(1, 3)))
            other = " ELSE %s" % self.expr(scope, depth + 1) if rng.random() < 0.7 else ""
            return "(CASE %s%s%s END)" % (base, whens, other)
        if r < 0.86:
            return "CAST(%s AS %s)" % (self.expr(scope, depth + 1), rng.choice(["INTEGER", "REAL", "TEXT", "BLOB", "NUMERIC"]))
        if r < 0.97:
            fn = rng.choice(["abs", "length", "lower", "upper", "substr", "substr", "coalesce", "ifnull", "nullif", "typeof", "round", "trim", "ltrim", "rtrim", "replace", "instr", "hex",
                             "min", "max", "sign", "unicode"])
            n = {"substr": rng.choice([2, 3]), "coalesce": rng.randint(2, 3), "ifnull": 2, "nullif": 2, "round": rng.choice([1, 2]), "replace": 3, "instr": 2, "min": 2, "max": 2,
                 "trim": rng.choice([1, 2]), "ltrim": 1, "rtrim": 1}.get(fn, 1)
            args = [self.expr(scope, depth + 2) for _ in range(n)]
            if fn == "round" and n == 2:
                args[1] = str(rng.randint(0, 3))
            if fn == "quote":
                args = [self.column(scope) if scope else "1"]
            return "%s(%s)" % (fn, ", ".join(args))
        return self.subquery_expr(scope, depth)

    def subquery_expr(self, scope, depth):
        rng = self.rng
        if not self.tables:
            return "NULL"
        t = rng.choice(sorted(self.tables))
        inner_scope = [(t, c[0]) for c in self.tables[t]]
        both = inner_scope + scope
        cond = self.expr(both, depth + 2)
        kind = rng.choice(["scalar", "in", "exists"])
        col = rng.choice(inner_scope)[1]
        if kind == "scalar":
            return "(SELECT %s(%s.%s) FROM %s WHERE %s)" % (rng.choice(["max", "min", "count", "sum", "avg"]), t, col, t, cond)
        if kind == "in":
            return "(%s %sIN (SELECT %s.%s FROM %s WHERE %s))" % (self.column(scope) if scope else "1", rng.choice(["", "NOT "]), t, col, t, cond)
        return "(%sEXISTS (SELECT 1 FROM %s WHERE %s))" % (rng.choice(["", "NOT "]), t, cond)

    # --- queries -----------------------------------------------------------------------------------------------------------------------------------------
    def select(self):
        rng = self.rng
        t = rng.choice(sorted(self.tables))
        r = rng.random()
        if r < 0.42:
            return self.simple_select(t)
        if r < 0.58:
            return self.aggregate_select(t)
        if r < 0.74:
            return self.join_select()
        if r < 0.84:
            return self.compound_select()
        if r < 0.92:
            return self.derived_select(t)
        return self.simple_select(t, distinct=True)

    def tail(self, ncols, allow_order=True):
        rng = self.rng
        s = ""
        if allow_order and rng.random() < 0.5:
            keys = [str(rng.randint(1, ncols)) + rng.choice(["", " DESC", " ASC"]) for _ in range(rng.randint(1, 2))]
            s += " ORDER BY " + ", ".join(keys + [str(i) for i in range(1, ncols + 1)])
            if rng.random() < 0.4:
                s += " LIMIT %d" % rng.randint(0, 6)
                if rng.random() < 0.4:
                    s += " OFFSET %d" % rng.randint(0, 4)
        return s

    def simple_select(self, t, distinct=False):
        rng = self.rng
        scope = [(None, c[0]) for c in self.tables[t]]
        n = rng.randint(1, 3)
        cols = [self.expr(scope) for _ in range(n)]
        s = "SELECT %s%s FROM %s" % ("DISTINCT " if distinct else "", ", ".join(cols), t)
        if rng.random() < 0.8:
            s += " WHERE " + self.condition(scope)
        return s + self.tail(n)

    def condition(self, scope):
        rng = self.rng
        # often a simple indexable shape, so that the planner's paths are used
        if rng.random() < 0.45 and scope:
            c = self.column(scope)
            op = rng.choice(["=", "=", "<", ">", "<=", ">=", "BETWEEN"])
            if op == "BETWEEN":
                return "%s BETWEEN %s AND %s" % (c, self.literal(), self.literal())
            cond = "%s %s %s" % (c, op, self.literal())
            if rng.random() < 0.4:
                cond += " AND %s" % self.expr(scope, 2)
            return cond
        return self.expr(scope)

    def aggregate_select(self, t):
        rng = self.rng
        scope = [(None, c[0]) for c in self.tables[t]]
        group = rng.sample([c[0] for c in self.tables[t]], rng.randint(0, min(2, len(scope))))
        aggs = []
        for _ in range(rng.randint(1, 3)):
            fn = rng.choice(["count", "sum", "avg", "min", "max", "total"])        # (group_concat joins in an order that is up to the engine)
            if fn == "count" and rng.random() < 0.4:
                aggs.append("count(*)")
            elif fn == "group_concat":
                aggs.append("group_concat(%s)" % self.column(scope))
            else:
                aggs.append("%s(%s%s)" % (fn, "DISTINCT " if rng.random() < 0.2 else "", self.expr(scope, 2) if rng.random() < 0.4 else self.column(scope)))
        cols = group + aggs
        s = "SELECT %s FROM %s" % (", ".join(cols), t)
        if rng.random() < 0.5:
            s += " WHERE " + self.condition(scope)
        if group:
            s += " GROUP BY " + ", ".join(group)
            if rng.random() < 0.4:
                s += " HAVING %s %s %s" % (rng.choice(["count(*)", "max(%s)" % group[0], "sum(%s)" % self.column(scope)]), rng.choice([">", ">=", "<", "="]), self.literal())
        return s + self.tail(len(cols))

    def join_select(self):
        rng = self.rng
        names = sorted(self.tables)
        a = rng.choice(names)
        b = rng.choice(names)
        scope = [("x", c[0]) for c in self.tables[a]] + [("y", c[0]) for c in self.tables[b]]
        kind = rng.choice(["JOIN", "LEFT JOIN", "INNER JOIN", "CROSS JOIN"])
        on = "x.%s = y.%s" % (rng.choice(self.tables[a])[0], rng.choice(self.tables[b])[0])
        if rng.random() < 0.3:
            on += " AND " + self.expr(scope, 2)
        n = rng.randint(1, 3)
        cols = [self.expr(scope, 1) for _ in range(n)]
        s = "SELECT %s FROM %s AS x %s %s AS y" % (", ".join(cols), a, kind, b)
        if kind != "CROSS JOIN":
            s += " ON " + on
        if rng.random() < 0.5:
            s += " WHERE " + self.expr(scope, 1)
        return s + self.tail(n)

    def compound_select(self):
        rng = self.rng
        n = rng.randint(1, 2)
        parts = []
        for _ in range(rng.randint(2, 3)):
            t = rng.choice(sorted(self.tables))
            scope = [(None, c[0]) for c in self.tables[t]]
            parts.append("SELECT %s FROM %s" % (", ".join(self.expr(scope, 2) for _ in range(n)), t) + (" WHERE " + self.condition(scope) if rng.random() < 0.5 else ""))
        ops = [rng.choice(["UNION", "UNION ALL", "INTERSECT", "EXCEPT"]) for _ in parts[1:]]
        s = parts[0]
        for op, p in zip(ops, parts[1:]):
            s += " %s %s" % (op, p)
        return s + self.tail(n)

    def derived_select(self, t):
        rng = self.rng
        scope = [(None, c[0]) for c in self.tables[t]]
        inner = "SELECT %s FROM %s" % (", ".join("%s AS d%d" % (self.expr(scope, 2), i) for i in range(2)), t)
        if rng.random() < 0.5:
            inner += " WHERE " + self.condition(scope)
        dscope = [("d", "d0"), ("d", "d1")]
        s = "SELECT %s FROM (%s) AS d" % (self.expr(dscope, 2) + ", d.d1", inner)
        if rng.random() < 0.5:
            s += " WHERE " + self.expr(dscope, 2)
        return s + self.tail(2)

    # --- changes -----------------------------------------------------------------------------------------------------------------------------------------
    def change(self):
        rng = self.rng
        t = rng.choice(sorted(self.tables))
        scope = [(None, c[0]) for c in self.tables[t]]
        r = rng.random()
        if r < 0.4:
            return self.insert(t)
        if r < 0.75:
            col = rng.choice([c[0] for c in self.tables[t] if c[0] != "id"] or ["c1"])
            return "UPDATE %s SET %s = %s WHERE %s" % (t, col, self.expr(scope, 2), self.condition(scope))        # (with OR IGNORE, which rows win a conflict depends on the scan order)
        if r < 0.92:
            return "DELETE FROM %s WHERE %s" % (t, self.condition(scope))
        other = rng.choice(sorted(self.tables))
        cols = [c[0] for c in self.tables[t] if c[0] != "id"][:2] or ["c1"]
        ocols = [c[0] for c in self.tables[other] if c[0] != "id"]
        if len(ocols) < len(cols):
            return self.insert(t)
        return "INSERT OR IGNORE INTO %s (%s) SELECT %s FROM %s WHERE %s ORDER BY rowid" % (t, ", ".join(cols), ", ".join(ocols[:len(cols)]), other,
                                                                                            self.condition([(None, c[0]) for c in self.tables[other]]))


class CollationGen(Gen):
    """The same, with columns that declare a collation (NOCASE, RTRIM) and expressions that name one (COLLATE), which changes how =, <, IN, BETWEEN, ORDER BY, GROUP BY,
    DISTINCT, min() / max(), compound selects and index look-ups compare text. A separate random stream, so that the other generator's seeds mean what they always meant."""

    def __init__(self, seed):
        super().__init__(seed)
        self.crng = random.Random(seed * 7919 + 13)

    def collation_suffix(self, typ):
        if typ in ("TEXT", "VARCHAR(10)", "CHAR(3)", "") and self.crng.random() < 0.55:
            return " COLLATE " + self.crng.choice(["NOCASE", "NOCASE", "RTRIM", "BINARY"])
        return ""

    def column(self, scope):
        c = super().column(scope)
        r = self.crng.random()
        if r < 0.08:
            return "%s COLLATE NOCASE" % c
        if r < 0.12:
            return "%s COLLATE BINARY" % c
        return c
