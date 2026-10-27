"""Compiling SQL expressions into Python closures.

``ExprCompiler(ctx, scope).compile(expr)`` gives a function ``f(row)``. A *row* is the tuple of all the columns of all the tables of the FROM clause, each table
contributing its columns followed by its rowid; the scope says where each column is in that tuple. Subqueries that refer to columns of the query around them
(correlated subqueries) read the current row of the outer query from a cell that the outer query keeps up to date.
"""
from __future__ import annotations

from . import functions as F
from . import sqlast as A
from . import values as V
from .errors import OperationalError

ROWID_NAMES = ("rowid", "_rowid_", "oid")
NUMERIC_AFFINITIES = (V.INTEGER, V.REAL, V.NUMERIC)


class AggRef:
    """Stands for the result of the i-th aggregate of a query, in the expressions that are evaluated after grouping."""
    __slots__ = ("index",)

    def __init__(self, index):
        self.index = index


class Source:
    """One table (or derived table) of a FROM clause, as the scope sees it."""

    def __init__(self, alias, names, affinities, collations, table=None, offset=0, rowid=True):
        self.alias = alias
        self.names = names
        self.affinities = affinities
        self.collations = collations
        self.table = table
        self.offset = offset
        self.rowid = rowid
        self.colmap = {n.lower(): i for i, n in enumerate(names)}
        self.hidden = set()                                  # columns that a USING / NATURAL join hides from "*"

    @property
    def width(self):
        return len(self.names) + 1                           # the columns and the rowid

    @property
    def rowid_index(self):
        return self.offset + len(self.names)


class Scope:
    def __init__(self, parent=None):
        self.sources = []
        self.parent = parent
        self.depth = 0
        self.cell = [None]                                   # the current row, for subqueries of this query
        self.referenced = False                              # does a subquery read the cell?
        self.using = {}                                      # lower-case column name -> the source that wins for unqualified use

    @property
    def width(self):
        return sum(s.width for s in self.sources)

    def add(self, source):
        source.offset = self.width
        self.sources.append(source)

    def find(self, qualifier, name):
        """``(source, column index or None for the rowid)`` or ``None``; raises for an ambiguous name."""
        lname = name.lower()
        found = []
        for s in self.sources:
            if qualifier is not None and qualifier.lower() != s.alias.lower():
                continue
            i = s.colmap.get(lname)
            if i is not None:
                found.append((s, i))
        if len(found) > 1 and qualifier is None and lname in self.using:
            return self.using[lname], self.using[lname].colmap[lname]
        if len(found) > 1:
            raise OperationalError("ambiguous column name: %s" % (name if qualifier is None else "%s.%s" % (qualifier, name)))
        if found:
            return found[0]
        if lname in ROWID_NAMES:
            cands = [s for s in self.sources if s.rowid and (qualifier is None or qualifier.lower() == s.alias.lower())]
            if len(cands) > 1 and qualifier is None:
                raise OperationalError("ambiguous column name: %s" % name)
            if cands:
                return cands[0], None
        return None


class Context:
    """What the closures share while a statement runs: the bound parameters and the connection's counters."""

    def __init__(self, db):
        self.db = db
        self.params = []

    def param(self, i):
        try:
            return self.params[i - 1]
        except IndexError:
            raise OperationalError("incomplete input: no value for parameter %d" % i) from None


def truth(v):
    """None for NULL, else whether the number the value stands for is not zero."""
    if v is None:
        return None
    t = type(v)
    if t is int:
        return v != 0
    if t is float:
        return v != 0.0
    return V.to_number(v) != 0


class ExprCompiler:
    def __init__(self, ctx, scope, select_compiler=None):
        self.ctx = ctx
        self.scope = scope
        self.select_compiler = select_compiler               # callable(select, scope) -> CompiledSelect, for subqueries
        self.targets = getattr(select_compiler, "__self__", None) and select_compiler.__self__.ref_targets

    # --- static properties of an expression -----------------------------------------------------------------------------------------------------
    def affinity(self, e):
        t = type(e)
        if t is A.Col:
            r = self.resolve(e)
            return r[2] if r else None
        if t is A.Cast:
            return V.affinity_of(e.typename)
        if t is A.Collate:
            return self.affinity(e.expr)
        return None                                                       # (a unary + strips the affinity, which is what it is used for)

    def collation(self, e):
        """The collating sequence of an expression, found the way SQLite finds it: a column has its own; it passes through a CAST and a unary +; and an explicit COLLATE
        anywhere below makes the expression use it (the first one met, going through the operands, the arguments of a function, the branches of a CASE). Anything else,
        ``a || b`` or ``upper(a)`` or ``coalesce(a, b)``, has none, whatever its operands have."""
        while True:
            t = type(e)
            if t is A.Col:
                r = self.resolve(e)
                return r[3] if r else None
            if t is A.Collate:
                return e.collation
            if t is A.Cast or (t is A.Unary and e.op == "+"):
                e = e.expr
                continue
            for child in operands(e):
                if has_collate(child):
                    e = child
                    break
            else:
                return None

    def comparison_collation(self, lexpr, rexpr):
        """An explicit COLLATE on either side wins (the left one first); else the collation of the left operand if it has one (every column has: BINARY if none was
        declared, so a column on the left beats a collated column on the right); else the right operand's."""
        if has_collate(lexpr):
            return self.collation(lexpr)
        if has_collate(rexpr):
            return self.collation(rexpr)
        return self.collation(lexpr) or self.collation(rexpr)

    def resolve(self, col):
        """``(scope level up, index into the row, affinity, collation)`` of a column reference, or ``None`` if there is no such column."""
        scope = self.scope
        level = 0
        while scope is not None:
            f = scope.find(col.table, col.name)
            if f is not None:
                source, i = f
                if i is None:
                    return level, source.rowid_index, V.INTEGER, None, scope
                return level, source.offset + i, source.affinities[i], source.collations[i], scope
            scope = scope.parent
            level += 1
        return None

    # --- compiling ---------------------------------------------------------------------------------------------------------------------------------
    def compile(self, e):
        return getattr(self, "e_" + type(e).__name__)(e)

    def e_Lit(self, e):
        v = e.value
        return lambda row: v

    def e_Param(self, e):
        ctx, i = self.ctx, e.index
        return lambda row: ctx.param(i)

    def e_Col(self, e):
        r = self.resolve(e)
        if r is None:
            raise OperationalError("no such column: %s" % (e.name if e.table is None else "%s.%s" % (e.table, e.name)))
        level, idx, _, _, scope = r
        if level == 0:
            return lambda row: row[idx]
        scope.referenced = True                              # an outer query: its current row is in its cell
        if self.targets is not None:
            self.targets.add(scope)
        cell = scope.cell
        return lambda row: cell[0][idx]

    def e_AggRef(self, e):
        idx = self.scope.width + e.index
        return lambda row: row[idx]

    def e_Collate(self, e):
        return self.compile(e.expr)

    def e_Unary(self, e):
        f = self.compile(e.expr)
        op = e.op
        if op == "-":
            return lambda row: V.neg(f(row))
        if op == "+":
            return f
        if op == "~":
            def bitnot(row):
                v = f(row)
                return None if v is None else ~V.to_integer(v)
            return bitnot

        def not_(row):
            t = truth(f(row))
            return None if t is None else (0 if t else 1)
        return not_

    ARITH = {"+": V.add, "-": V.sub, "*": V.mul, "/": V.div, "%": V.mod, "||": V.concat, "&": V.bit_and, "|": V.bit_or}

    def e_Binary(self, e):
        op = e.op
        if op == "AND":
            lf, rf = self.compile(e.left), self.compile(e.right)

            def and_(row):
                a = truth(lf(row))
                if a is False:
                    return 0
                b = truth(rf(row))
                if b is False:
                    return 0
                return None if (a is None or b is None) else 1
            return and_
        if op == "OR":
            lf, rf = self.compile(e.left), self.compile(e.right)

            def or_(row):
                a = truth(lf(row))
                if a is True:
                    return 1
                b = truth(rf(row))
                if b is True:
                    return 1
                return None if (a is None or b is None) else 0
            return or_
        if op in self.ARITH:
            fn = self.ARITH[op]
            lf, rf = self.compile(e.left), self.compile(e.right)
            return lambda row: fn(lf(row), rf(row))
        if op == "<<" or op == ">>":
            lf, rf = self.compile(e.left), self.compile(e.right)
            left = op == "<<"
            return lambda row: V.shift(lf(row), rf(row), left)
        return self.comparison(op, e.left, e.right)

    # --- comparisons, with the affinity rules ------------------------------------------------------------------------------------------------------
    @staticmethod
    def compare_affinity(la, ra):
        """The affinity applied to both sides of a comparison (SQLite's rule): when both are columns, NUMERIC if either is numeric, else none; when only one is a
        column (``None`` means "not a column"), that column's affinity."""
        if la is not None and ra is not None:
            return V.NUMERIC if (la in NUMERIC_AFFINITIES or ra in NUMERIC_AFFINITIES) else None
        aff = la if la is not None else ra
        if aff in NUMERIC_AFFINITIES:
            return V.NUMERIC
        return V.TEXT if aff == V.TEXT else None

    def coercions(self, lexpr, rexpr):
        """The affinity (or ``None``) to apply to both operands of a comparison of these two expressions."""
        return self.compare_affinity(self.affinity(lexpr), self.affinity(rexpr))

    def comparison(self, op, lexpr, rexpr):
        lf, rf = self.compile(lexpr), self.compile(rexpr)
        ca = cb = self.coercions(lexpr, rexpr)
        coll = self.comparison_collation(lexpr, rexpr)
        compare = V.compare
        apply = V.apply_affinity
        if op in ("IS", "IS NOT"):
            negate = op == "IS NOT"

            def is_(row):
                a, b = lf(row), rf(row)
                if ca:
                    a = apply(a, ca)
                if cb:
                    b = apply(b, cb)
                if a is None or b is None:
                    same = a is None and b is None
                else:
                    same = compare(a, b, coll) == 0
                return int(same != negate)
            return is_
        test = {"=": lambda r: r == 0, "!=": lambda r: r != 0, "<": lambda r: r < 0, "<=": lambda r: r <= 0, ">": lambda r: r > 0, ">=": lambda r: r >= 0}.get(op)
        if test is None:
            raise OperationalError("unknown operator %s" % op)

        def cmp_(row):
            a, b = lf(row), rf(row)
            if a is None or b is None:
                return None
            if ca:
                a = apply(a, ca)
            if cb:
                b = apply(b, cb)
            return 1 if test(compare(a, b, coll)) else 0
        return cmp_

    def e_Between(self, e):
        ge = self.comparison(">=", e.expr, e.low)
        le = self.comparison("<=", e.expr, e.high)
        negated = e.negated

        def between(row):
            a = truth(ge(row))
            if a is False:
                r = 0
            else:
                b = truth(le(row))
                if b is False:
                    r = 0
                elif a is None or b is None:
                    return None
                else:
                    r = 1
            return 1 - r if negated else r
        return between

    def e_InList(self, e):
        xf = self.compile(e.expr)
        items = [self.compile(i) for i in e.items]
        la = self.affinity(e.expr)
        # for a list, only the affinity of the left side counts (and it is applied to both sides): a literal on the left converts nothing
        conv = V.NUMERIC if la in NUMERIC_AFFINITIES else (V.TEXT if la == V.TEXT else None)
        coll = self.collation(e.expr)
        negated = e.negated
        compare, apply = V.compare, V.apply_affinity
        if not items:
            return lambda row: 1 if negated else 0

        def in_(row):
            x = xf(row)
            if x is None:
                return None
            if conv:
                x = apply(x, conv)
            saw_null = False
            for f in items:
                y = f(row)
                if y is None:
                    saw_null = True
                    continue
                if conv:
                    y = apply(y, conv)
                if compare(x, y, coll) == 0:
                    return 0 if negated else 1
            if saw_null:
                return None
            return 1 if negated else 0
        return in_

    def e_InSelect(self, e):
        xf = self.compile(e.expr)
        sub = self.subselect(e.select)
        if sub.ncolumns != 1:
            raise OperationalError("sub-select returns %d columns - expected 1" % sub.ncolumns)
        conv = self.compare_affinity(self.affinity(e.expr), sub.affinities[0])
        conv_x = conv_y = conv
        coll = self.collation(e.expr)
        negated = e.negated
        cache = {}

        def in_select(row):
            x = xf(row)
            if "set" not in cache or sub.correlated:
                values = [r[0] for r in sub.run(row)]
                if conv_y:
                    values = [V.apply_affinity(v, conv_y) for v in values]
                nonnull = [v for v in values if v is not None]
                cache["set"] = ({V.sort_key(v, coll) for v in nonnull}, len(values) != len(nonnull), bool(values))
            keys, has_null, nonempty = cache["set"]
            if x is None:
                return None if nonempty else (1 if negated else 0)
            if conv_x:
                x = V.apply_affinity(x, conv_x)
            if V.sort_key(x, coll) in keys:
                return 0 if negated else 1
            if has_null:
                return None
            return 1 if negated else 0
        return in_select

    def e_Like(self, e):
        xf, pf = self.compile(e.expr), self.compile(e.pattern)
        ef = self.compile(e.escape) if e.escape is not None else None
        op, negated = e.op, e.negated

        def like(row):
            x, p = xf(row), pf(row)
            if x is None or p is None:
                return None
            if op == "GLOB":
                r = F.f_glob(p, x)
            else:
                esc = ef(row) if ef else None
                r = F.f_like(p, x, esc)
            r = bool(r)
            return int(r != negated)
        return like

    def e_IsNull(self, e):
        f = self.compile(e.expr)
        negated = e.negated
        return lambda row: int((f(row) is None) != negated)

    def e_Case(self, e):
        other = self.compile(e.other) if e.other is not None else None
        if e.base is None:
            whens = [(self.compile(cond), self.compile(res)) for cond, res in e.whens]

            def case(row):
                for c, r in whens:
                    if truth(c(row)):
                        return r(row)
                return other(row) if other is not None else None
            return case
        # CASE x WHEN a ...: x is evaluated once per row and each WHEN compares it, like x = a, through a one-slot holder
        base_f = self.compile(e.base)
        holder = [None]
        whens = [(self.compare_with_holder(e.base, cond, holder), self.compile(res)) for cond, res in e.whens]

        def case_base(row):
            holder[0] = base_f(row)
            for c, r in whens:
                if c(row) == 1:
                    return r(row)
            return other(row) if other is not None else None
        return case_base

    def compare_with_holder(self, base_expr, cond_expr, holder):
        cf = self.compile(cond_expr)
        ca = cb = self.coercions(base_expr, cond_expr)
        coll = self.comparison_collation(base_expr, cond_expr)
        compare, apply = V.compare, V.apply_affinity

        def eq(row):
            a, b = holder[0], cf(row)
            if a is None or b is None:
                return None
            if ca:
                a = apply(a, ca)
            if cb:
                b = apply(b, cb)
            return 1 if compare(a, b, coll) == 0 else 0
        return eq

    def e_Cast(self, e):
        f = self.compile(e.expr)
        t = e.typename
        return lambda row: V.cast(f(row), t)

    def e_Func(self, e):
        name = e.name
        if F.is_aggregate(name, len(e.args)) or (e.star and name == "count"):
            raise OperationalError("misuse of aggregate function %s()" % name)
        special = self.ctx.db.special_function(name, len(e.args)) if hasattr(self.ctx.db, "special_function") else None
        if special is not None:
            return special
        if name in F.SCALAR_MIN_MAX:
            F.check_scalar(name, len(e.args))
            fn = F.SCALAR_MIN_MAX[name]
        else:
            F.check_scalar(name, len(e.args))
            fn = F.SCALARS[name][2]
        args = [self.compile(a) for a in e.args]
        if len(args) == 1:
            a0 = args[0]
            return lambda row: fn(a0(row))
        if len(args) == 2:
            a0, a1 = args
            return lambda row: fn(a0(row), a1(row))
        return lambda row: fn(*[a(row) for a in args])

    def subselect(self, select):
        if self.select_compiler is None:
            raise OperationalError("subqueries are not allowed here")
        return self.select_compiler(select, self.scope)

    def e_Subquery(self, e):
        sub = self.subselect(e.select)
        if sub.ncolumns != 1:
            raise OperationalError("sub-select returns %d columns - expected 1" % sub.ncolumns)
        cache = []

        def scalar(row):
            if cache and not sub.correlated:
                return cache[0]
            r = None
            for r in sub.run(row, limit=1):
                break
            v = r[0] if r else None
            if not sub.correlated:
                cache[:] = [v]
            return v
        return scalar

    def e_Exists(self, e):
        sub = self.subselect(e.select)
        negated = e.negated
        cache = []

        def exists(row):
            if cache and not sub.correlated:
                return cache[0]
            found = 0
            for _ in sub.run(row, limit=1):
                found = 1
                break
            v = (1 - found) if negated else found
            if not sub.correlated:
                cache[:] = [v]
            return v
        return exists


def has_collate(e):
    """Does the expression carry an explicit COLLATE (SQLite's EP_Collate flag)? It passes up through the operands listed by ``operands``."""
    return type(e) is A.Collate or any(has_collate(x) for x in operands(e))


def table_collations(table):
    """The collation of every column of a table as the comparison rules see it: the declared one, BINARY if none was; none for the rowid alias."""
    return [None if i == table.ipk else (c.collation or "BINARY") for i, c in enumerate(table.columns)]


def operands(e):
    """The direct operands of an expression, in the order SQLite looks at them for an explicit COLLATE: the left one first, then the list (arguments, branches), then the right."""
    t = type(e)
    if t is A.Unary or t is A.Cast or t is A.Collate or t is A.IsNull or t is A.InSelect:
        return [e.expr]
    if t is A.Binary:
        return [e.left, e.right]
    if t is A.Between:
        return [e.expr]                                               # (a COLLATE in the bounds counts for the comparison with that bound only)
    if t is A.InList:
        return [e.expr] + list(e.items)
    if t is A.Like:
        return [x for x in (e.expr, e.pattern, e.escape) if x is not None]
    if t is A.Case:
        out = [e.base] if e.base is not None else []
        for c, r in e.whens:
            out += [c, r]
        return out + ([e.other] if e.other is not None else [])
    if t is A.Func:
        return list(e.args)
    return []


def walk(e):
    """Every expression node below ``e`` (``e`` included), not entering subqueries."""
    yield e
    t = type(e)
    if t is A.Unary or t is A.Cast or t is A.Collate or t is A.IsNull:
        yield from walk(e.expr)
    elif t is A.Binary:
        yield from walk(e.left)
        yield from walk(e.right)
    elif t is A.Between:
        for x in (e.expr, e.low, e.high):
            yield from walk(x)
    elif t is A.InList:
        yield from walk(e.expr)
        for x in e.items:
            yield from walk(x)
    elif t is A.InSelect:
        yield from walk(e.expr)
    elif t is A.Like:
        for x in (e.expr, e.pattern, e.escape):
            if x is not None:
                yield from walk(x)
    elif t is A.Case:
        if e.base is not None:
            yield from walk(e.base)
        for c, r in e.whens:
            yield from walk(c)
            yield from walk(r)
        if e.other is not None:
            yield from walk(e.other)
    elif t is A.Func:
        for x in e.args:
            yield from walk(x)
