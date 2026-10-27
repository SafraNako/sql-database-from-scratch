"""SELECT: from the syntax tree to something that runs.

A query is compiled once into closures and plan objects; running it walks the FROM clause as nested loops (an index or the rowid is used for a table whenever the WHERE or ON
conditions pin down a column to a value or a range), filters, groups and aggregates, projects, removes duplicates, sorts and cuts. All the conditions are still applied to
every row that an index lookup returns, so what the planner chooses can only make a query faster, never different.
"""
from __future__ import annotations

import itertools
import math

from . import functions as F
from . import sqlast as A
from . import values as V
from .errors import OperationalError
from .exprs import AggRef, ExprCompiler, Scope, Source, table_collations, truth, walk
from .tables import row_from_record

OPPOSITE = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "="}


class CompiledSelect:
    def __init__(self, names, affinities, correlated, run, plan, collations=None):
        self.names = names
        self.ncolumns = len(names)
        self.affinities = affinities
        self.collations = collations if collations is not None else [None] * len(names)
        self.correlated = correlated
        self._run = run
        self.plan = plan                                      # lines of text describing the access paths (EXPLAIN QUERY PLAN)

    def run(self, outer_row=None, limit=None):
        rows = self._run()
        return itertools.islice(rows, limit) if limit is not None else rows


# --- helpers on expressions -----------------------------------------------------------------------------------------------------------------------
def split_and(e):
    if isinstance(e, A.Binary) and e.op == "AND":
        return split_and(e.left) + split_and(e.right)
    return [e]


def transform(e, fn):
    """A copy of the expression with ``fn`` applied bottom-up (``fn`` returns a replacement node, or ``None`` to keep it)."""
    t = type(e)
    if t is A.Unary:
        n = A.Unary(e.op, transform(e.expr, fn))
    elif t is A.Binary:
        n = A.Binary(e.op, transform(e.left, fn), transform(e.right, fn))
    elif t is A.Between:
        n = A.Between(transform(e.expr, fn), transform(e.low, fn), transform(e.high, fn), e.negated)
    elif t is A.InList:
        n = A.InList(transform(e.expr, fn), [transform(i, fn) for i in e.items], e.negated)
    elif t is A.InSelect:
        n = A.InSelect(transform(e.expr, fn), e.select, e.negated)
    elif t is A.Like:
        n = A.Like(e.op, transform(e.expr, fn), transform(e.pattern, fn), transform(e.escape, fn) if e.escape is not None else None, e.negated)
    elif t is A.IsNull:
        n = A.IsNull(transform(e.expr, fn), e.negated)
    elif t is A.Case:
        n = A.Case(transform(e.base, fn) if e.base is not None else None, [(transform(c, fn), transform(r, fn)) for c, r in e.whens],
                   transform(e.other, fn) if e.other is not None else None)
    elif t is A.Cast:
        n = A.Cast(transform(e.expr, fn), e.typename)
    elif t is A.Collate:
        n = A.Collate(transform(e.expr, fn), e.collation)
    elif t is A.Func:
        n = A.Func(e.name, [transform(a, fn) for a in e.args], e.distinct, e.star)
    else:
        n = e
    r = fn(n)
    return n if r is None else r


def contains_subquery(e):
    return any(type(n) in (A.Subquery, A.Exists, A.InSelect) for n in walk(e))


# --- access paths ----------------------------------------------------------------------------------------------------------------------------------
class FullScan:
    def __init__(self, ctx, table):
        self.ctx, self.table = ctx, table

    def describe(self):
        return "SCAN %s" % self.table.name

    def rows(self, prefix):
        table = self.table
        for rowid, rec in self.ctx.db.table_tree(table).scan():
            yield tuple(row_from_record(table, rowid, rec))


class RowidScan:
    """A rowid (or INTEGER PRIMARY KEY) equal to a value, or between two bounds."""

    def __init__(self, ctx, table, eq, lo, hi, convert):
        self.ctx, self.table, self.eq, self.lo, self.hi, self.convert = ctx, table, eq, lo, hi, convert

    def describe(self):
        return "SEARCH %s USING INTEGER PRIMARY KEY (rowid%s?)" % (self.table.name, "=" if self.eq else ">" if self.lo else "<")

    def _value(self, probe, prefix):
        v = probe[0](prefix)
        if v is None:
            return None, True
        if probe[1]:
            v = V.apply_affinity(v, probe[1])
        return v, False

    def rows(self, prefix):
        table = self.table
        tree = self.ctx.db.table_tree(table)
        if self.eq is not None:
            v, null = self._value(self.eq, prefix)
            if null:
                return
            if type(v) is float and v == int(v) and abs(v) < 2 ** 63:
                v = int(v)
            if type(v) is not int:
                return                                              # not an integer: no rowid is equal to it
            rec = tree.get(v)
            if rec is not None:
                yield tuple(row_from_record(table, v, rec))
            return
        lo = hi = None
        if self.lo is not None:
            v, null = self._value(self.lo[0], prefix)
            if null:
                return
            lo = self._bound(v, self.lo[1], True)
            if lo is False:
                return
        if self.hi is not None:
            v, null = self._value(self.hi[0], prefix)
            if null:
                return
            hi = self._bound(v, self.hi[1], False)
            if hi is False:
                return
        if lo == "scan" or hi == "scan":                             # a bound that is not a number: let the filters decide
            lo = hi = None
        for rowid, rec in tree.scan(lo, hi):
            yield tuple(row_from_record(table, rowid, rec))

    @staticmethod
    def _bound(v, inclusive, lower):
        """The integer rowid bound for ``rowid >= v`` (lower) or ``rowid <= v``; ``False`` when nothing can match, ``"scan"`` for a value that is not a number."""
        if type(v) not in (int, float):
            return "scan"
        if type(v) is float:
            if v != v:
                return False
            if v >= 2.0 ** 63:
                return False if lower else None
            if v <= -2.0 ** 63:
                return None if lower else False
            n = math.ceil(v) if lower else math.floor(v)
            if n == v and not inclusive:
                n = n + 1 if lower else n - 1
            return n
        return v if inclusive else (v + 1 if lower else v - 1)


class IndexScan:
    def __init__(self, ctx, table, index, eqs, lo, hi):
        self.ctx, self.table, self.index = ctx, table, index
        self.eqs = eqs                                             # [(closure, coercion)] for the leading columns
        self.lo, self.hi = lo, hi                                  # (closure, coercion, inclusive) for the next column

    def describe(self):
        cols = [self.table.columns[c].name for c, _, _ in self.index.columns]
        parts = ["%s=?" % cols[i] for i in range(len(self.eqs))]
        if self.lo is not None:
            parts.append("%s>?" % cols[len(self.eqs)])
        if self.hi is not None:
            parts.append("%s<?" % cols[len(self.eqs)])
        return "SEARCH %s USING %sINDEX %s (%s)" % (self.table.name, "COVERING " if False else "", self.index.name, " AND ".join(parts))

    @staticmethod
    def _value(probe, prefix):
        v = probe[0](prefix)
        if v is not None and probe[1]:
            v = V.apply_affinity(v, probe[1])
        return v

    def rows(self, prefix):
        eq_vals = []
        for probe in self.eqs:
            v = self._value(probe, prefix)
            if v is None:
                return
            eq_vals.append(v)
        lo, lo_inc, hi, hi_inc = eq_vals, True, eq_vals, True
        if self.lo is not None:
            v = self._value(self.lo, prefix)
            if v is None:
                return
            lo, lo_inc = eq_vals + [v], self.lo[2]
        else:
            lo, lo_inc = (eq_vals + [None], False) if self.hi is not None else (eq_vals, True)      # skip the NULLs, which sort first
        if self.hi is not None:
            v = self._value(self.hi, prefix)
            if v is None:
                return
            hi, hi_inc = eq_vals + [v], self.hi[2]
        db = self.ctx.db
        table = self.table
        tree = db.index_tree(self.index)
        data = db.table_tree(table)
        for key in tree.scan(lo or None, hi or None, lo_inc, hi_inc):
            rowid = key[-1]
            rec = data.get(rowid)
            if rec is not None:
                yield tuple(row_from_record(table, rowid, rec))


class MaterializedScan:
    """The rows of a derived table (a subquery in FROM, or a common table expression): computed once per execution of the query that contains it."""

    def __init__(self, compiled, label):
        self.compiled, self.label = compiled, label
        self.cache = None

    def describe(self):
        return "SCAN %s" % self.label

    def rows(self, prefix):
        if self.cache is None or self.compiled.correlated:
            self.cache = [r + (i + 1,) for i, r in enumerate(self.compiled.run())]
        return iter(self.cache)

    def reset(self):
        self.cache = None


class OneRow:
    def describe(self):
        return "SCAN CONSTANT ROW"

    def rows(self, prefix):
        yield ()


class Level:
    def __init__(self, source, kind, access):
        self.source, self.kind, self.access = source, kind, access
        self.on = None
        self.filters = []
        self.null_row = (None,) * source.width


class SelectCompiler:
    def __init__(self, ctx):
        self.ctx = ctx
        self.ctes = []                                                # stack of {lower name: (names, CompiledSelect)}
        self.ref_targets = set()

    # --- entry --------------------------------------------------------------------------------------------------------------------------------------
    def compile(self, select, parent_scope=None):
        saved_targets = self.ref_targets
        self.ref_targets = set()
        depth = (parent_scope.depth + 1) if parent_scope is not None else 0
        frame = {}
        for name, cols, sel in select.ctes or []:
            compiled = self.compile(sel, parent_scope)
            names = cols if cols else compiled.names
            frame[name.lower()] = (names, compiled)
        self.ctes.append(frame)
        try:
            plan = []
            if len(select.cores) == 1:
                core = select.cores[0]
                names, affs, produce, order_info = self.compile_core(core, select.order, parent_scope, depth, plan)
                run = self.finish_single(produce, order_info, select, names)
                colls = order_info[2] if order_info else None
            else:
                names, affs, run, colls = self.compile_compound(select, parent_scope, depth, plan)
            limit, offset = self.compile_limit(select, parent_scope)
            inner = run
            if limit is not None or offset is not None:
                def run():
                    rows = inner()
                    lim, off = limit() if limit else None, offset() if offset else 0
                    if lim is not None and lim < 0:
                        lim = None
                    start = max(off or 0, 0)
                    return itertools.islice(rows, start, None if lim is None else start + lim)
            targets = self.ref_targets
            correlated = any(t.depth < depth for t in targets)
            saved_targets |= {t for t in targets if t.depth < depth}
            return CompiledSelect(names, affs, correlated, run, plan, colls)
        finally:
            self.ctes.pop()
            self.ref_targets = saved_targets

    def compile_limit(self, select, parent_scope):
        scope = Scope(parent_scope)
        scope.depth = (parent_scope.depth + 1) if parent_scope is not None else 0
        ec = self.expr_compiler(scope)

        def make(e):
            if e is None:
                return None
            f = ec.compile(e)

            def value():
                v = f(())
                if v is None:
                    return None
                n = V.to_integer(v) if type(v) in (int, float, str) else None
                if n is None or (type(v) is float and v != int(v)) or (type(v) is str and V.text_to_number(v) is None):
                    raise OperationalError("datatype mismatch")
                return n
            return value
        return make(select.limit), make(select.offset)

    def expr_compiler(self, scope):
        ec = ExprCompiler(self.ctx, scope, self.compile_subselect)
        return ec

    def compile_subselect(self, select, scope):
        return self.compile(select, scope)

    # --- the FROM clause ------------------------------------------------------------------------------------------------------------------------
    def flatten(self, node, out, first=True):
        if isinstance(node, A.Join):
            self.flatten(node.left, out, first)
            if isinstance(node.right, A.Join):
                raise OperationalError("a parenthesised join on the right of a JOIN is not supported")
            out.append((node.right, node.kind, node.on, node.using, node.natural))
        else:
            out.append((node, None, None, None, False))
        return out

    def make_source(self, node, scope, parent_scope, plan):
        """``(Source, base access)`` for a table or a subquery of the FROM clause."""
        db = self.ctx.db
        if isinstance(node, A.TableRef):
            cte = next((f[node.name.lower()] for f in reversed(self.ctes) if node.name.lower() in f), None)
            if cte is not None:
                names, compiled = cte
                src = Source(node.alias or node.name, list(names), list(compiled.affinities), [c or "BINARY" for c in compiled.collations], None, rowid=False)
                return src, MaterializedScan(compiled, node.name)
            view = db.schema.views.get(node.name.lower()) if db.schema is not None else None
            if view is not None and node.name.lower() not in ("sqlite_master", "sqlite_schema"):
                vname, vselect, vcols = view
                compiled = self.compile(vselect, parent_scope)
                names = list(vcols) if vcols else list(compiled.names)
                src = Source(node.alias or vname, names, list(compiled.affinities), [c or "BINARY" for c in compiled.collations], None, rowid=False)
                return src, MaterializedScan(compiled, vname)
            table = db.schema_table(node.name)
            src = Source(node.alias or table.name, table.names, [c.affinity for c in table.columns], table_collations(table), table)
            return src, None
        compiled = self.compile(node.select, parent_scope)
        alias = node.alias or "(subquery)"
        src = Source(alias, list(compiled.names), list(compiled.affinities), [c or "BINARY" for c in compiled.collations], None, rowid=False)
        return src, MaterializedScan(compiled, alias)

    def build_levels(self, core, scope, parent_scope, where_conjuncts, plan):
        ec = self.expr_compiler(scope)
        items = []
        if core.sources is None:
            return [], ec
        self.flatten(core.sources, items)
        levels = []
        on_asts = []
        for node, kind, on, using, natural in items:
            src, access = self.make_source(node, scope, parent_scope, plan)
            if using or natural:
                if using is None:
                    left_names = {n.lower() for s in scope.sources for n in s.names}
                    using = [n for n in src.names if n.lower() in left_names]
                for name in using:
                    left = next((s for s in scope.sources if name.lower() in s.colmap), None)
                    if left is None or name.lower() not in src.colmap:
                        raise OperationalError("cannot join using column %s - column not present in both tables" % name)
                    scope.using[name.lower()] = left
                    src.hidden.add(name.lower())
                scope.add(src)
                conds = [A.Binary("=", A.Col(scope.using[n.lower()].alias, n), A.Col(src.alias, n)) for n in using]
                on = conds[0]
                for c in conds[1:]:
                    on = A.Binary("AND", on, c)
            else:
                scope.add(src)
            levels.append(Level(src, kind, access))
            on_asts.append(on)
        # place the WHERE conjuncts at the first level at which all the tables they use are there
        n = len(levels)
        placed = [[] for _ in range(n)]
        for c in where_conjuncts:
            refs, sub = self.sources_used(ec, c, scope)
            level = (n - 1) if (sub or not refs) else max(refs)
            if not refs and not sub:
                level = 0
            placed[level].append(c)
        for i, lv in enumerate(levels):
            usable = list(placed[i]) if lv.kind in (None, "INNER", "CROSS") else []
            on_conj = split_and(on_asts[i]) if on_asts[i] is not None else []
            lv.on = self.conjunction(ec, on_conj)
            lv.filters = [ec.compile(c) for c in placed[i]]
            if lv.access is None:
                lv.access = self.choose_access(ec, scope, i, lv.source, on_conj + usable)
            plan.append(lv.access.describe())
        return levels, ec

    @staticmethod
    def conjunction(ec, conjuncts):
        if not conjuncts:
            return None
        fns = [ec.compile(c) for c in conjuncts]
        if len(fns) == 1:
            return fns[0]

        def all_(row):
            for f in fns:
                if truth(f(row)) is not True:
                    return 0
            return 1
        return all_

    def sources_used(self, ec, e, scope):
        refs, sub = set(), False
        for n in walk(e):
            t = type(n)
            if t is A.Col:
                r = ec.resolve(n)
                if r is None:
                    continue
                level, idx, _, _, owner = r
                if level == 0:
                    for si, s in enumerate(scope.sources):
                        if s.offset <= idx < s.offset + s.width:
                            refs.add(si)
                else:
                    self.ref_targets.add(owner)
            elif t in (A.Subquery, A.Exists, A.InSelect):
                sub = True
        return refs, sub

    # --- choosing how to read a table -------------------------------------------------------------------------------------------------------------
    def constraints(self, ec, scope, level, source, conjuncts):
        out = []
        for c in conjuncts:
            pairs = []
            if isinstance(c, A.Binary) and c.op in OPPOSITE:
                pairs.append((c.left, c.op, c.right, (c.left, c.right)))
                pairs.append((c.right, OPPOSITE[c.op], c.left, (c.left, c.right)))          # (the collation is that of the comparison as written, whichever side the column is on)
            elif isinstance(c, A.Between) and not c.negated:
                pairs.append((c.expr, ">=", c.low, (c.expr, c.low)))
                pairs.append((c.expr, "<=", c.high, (c.expr, c.high)))
            for col_e, op, other, written in pairs:
                if not isinstance(col_e, A.Col):
                    continue
                r = ec.resolve(col_e)
                if r is None or r[0] != 0:
                    continue
                idx = r[1]
                if not (source.offset <= idx < source.offset + source.width):
                    continue
                refs, sub = self.sources_used(ec, other, scope)
                if sub or any(ref >= level for ref in refs):
                    continue
                conv = ec.coercions(col_e, other)
                col_aff = ec.affinity(col_e)
                col_class = V.NUMERIC if col_aff in (V.INTEGER, V.REAL, V.NUMERIC) else col_aff
                if conv is not None and conv != col_class:
                    continue                                          # the column's own values would have to be converted: an index cannot help
                cb = conv
                local = idx - source.offset
                col = -1 if local == len(source.names) else local
                coll = ec.comparison_collation(*written)
                out.append((col, op, ec.compile(other), cb, coll))
        return out

    def choose_access(self, ec, scope, level, source, conjuncts):
        table = source.table
        if table is None:
            return OneRow()
        cons = self.constraints(ec, scope, level, source, conjuncts)
        ctx = self.ctx
        rowid_cols = {-1} | ({table.ipk} if table.ipk is not None else set())
        eq = next((c for c in cons if c[0] in rowid_cols and c[1] == "="), None)
        if eq is not None:
            return RowidScan(ctx, table, (eq[2], eq[3]), None, None, None)
        lo = next(((c[2], c[3]), c[1] == ">=") for c in cons if c[0] in rowid_cols and c[1] in (">", ">=")) if any(c[0] in rowid_cols and c[1] in (">", ">=") for c in cons) else None
        hi = next(((c[2], c[3]), c[1] == "<=") for c in cons if c[0] in rowid_cols and c[1] in ("<", "<=")) if any(c[0] in rowid_cols and c[1] in ("<", "<=") for c in cons) else None
        if lo is not None or hi is not None:
            return RowidScan(ctx, table, None, lo, hi, None)
        best = None
        for index in table.indexes:
            if index.where is not None:
                continue
            eqs, used = [], 0
            for ci, coll, _ in index.columns:
                m = next((c for c in cons if c[0] == ci and c[1] == "=" and (c[4] or "BINARY") == (coll or "BINARY")), None)
                if m is None:
                    break
                eqs.append((m[2], m[3]))
                used += 1
            rng_lo = rng_hi = None
            if used < len(index.columns):
                ci, coll, _ = index.columns[used]
                for c in cons:
                    if c[0] == ci and (c[4] or "BINARY") == (coll or "BINARY"):
                        if c[1] in (">", ">=") and rng_lo is None:
                            rng_lo = (c[2], c[3], c[1] == ">=")
                        elif c[1] in ("<", "<=") and rng_hi is None:
                            rng_hi = (c[2], c[3], c[1] == "<=")
            score = (used, (rng_lo is not None) + (rng_hi is not None), index.unique and used == len(index.columns))
            if used == 0 and rng_lo is None and rng_hi is None:
                continue
            if best is None or score > best[0]:
                best = (score, IndexScan(ctx, table, index, eqs, rng_lo, rng_hi))
        if best is not None:
            return best[1]
        return FullScan(ctx, table)

    # --- running the nested loops ------------------------------------------------------------------------------------------------------------------
    @staticmethod
    def make_rows(levels, scope):
        """A generator function of the combined rows that pass every join condition and filter."""
        n = len(levels)
        referenced = scope

        def go(i, prefix):
            lv = levels[i]
            on, filters, kind = lv.on, lv.filters, lv.kind
            matched = False
            last = i == n - 1
            for r in lv.access.rows(prefix):
                row = prefix + r
                if referenced.referenced:
                    referenced.cell[0] = row
                if on is not None and truth(on(row)) is not True:
                    continue
                matched = True
                ok = True
                for f in filters:
                    if truth(f(row)) is not True:
                        ok = False
                        break
                if ok:
                    if last:
                        yield row
                    else:
                        yield from go(i + 1, row)
            if kind == "LEFT" and not matched:
                row = prefix + lv.null_row
                if referenced.referenced:
                    referenced.cell[0] = row
                if all(truth(f(row)) is True for f in filters):
                    if last:
                        yield row
                    else:
                        yield from go(i + 1, row)

        def rows():
            for lv in levels:
                if isinstance(lv.access, MaterializedScan):
                    lv.access.reset()
            if n == 0:
                yield ()
                return
            yield from go(0, ())
        return rows

    # --- one SELECT core ---------------------------------------------------------------------------------------------------------------------------
    def expand_columns(self, core, scope):
        """The result columns with ``*`` expanded: ``[(expression, name)]``."""
        out = []
        for rc in core.columns:
            if rc.star:
                found = False
                for s in scope.sources:
                    if rc.star_table is not None and rc.star_table.lower() != s.alias.lower():
                        continue
                    found = True
                    for i, name in enumerate(s.names):
                        if name.lower() in s.hidden:
                            continue
                        out.append((A.Col(s.alias, name), name))
                if not found:
                    raise OperationalError("no such table: %s" % rc.star_table if rc.star_table else "no tables specified")
            else:
                name = rc.alias or (rc.expr.name if isinstance(rc.expr, A.Col) else rc.text)
                out.append((rc.expr, name))
        return out

    def compile_core(self, core, order, parent_scope, depth, plan):
        scope = Scope(parent_scope)
        scope.depth = depth
        if core.values is not None:
            ec = self.expr_compiler(scope)
            rows = [[ec.compile(e) for e in row] for row in core.values]
            ncols = len(rows[0])
            if any(len(r) != ncols for r in rows):
                raise OperationalError("all VALUES must have the same number of terms")
            names = ["column%d" % (i + 1) for i in range(ncols)]
            affs = [None] * ncols

            def produce():
                for r in rows:
                    yield tuple(f(()) for f in r), ()
            return names, affs, produce, None
        where_conjuncts = split_and(core.where) if core.where is not None else []
        levels, ec = self.build_levels(core, scope, parent_scope, where_conjuncts, plan)
        result_cols = self.expand_columns(core, scope)
        names = [n for _, n in result_cols]
        affs = [ec.affinity(e) for e, _ in result_cols]
        aliases = {}
        for i, (e, n) in enumerate(result_cols):
            aliases.setdefault(n.lower(), i)
        # aggregates?
        group_exprs = []
        for g in core.group or []:
            group_exprs.append(self.resolve_group_term(g, result_cols))
        specs = []

        def find_aggs(e):
            def fn(node):
                if isinstance(node, A.Func) and (F.is_aggregate(node.name, len(node.args)) or (node.star and node.name == "count")):
                    for a in node.args:
                        if any(isinstance(x, AggRef) or (isinstance(x, A.Func) and F.is_aggregate(x.name, len(x.args))) for x in walk(a)):
                            raise OperationalError("misuse of aggregate function %s()" % node.name)
                    specs.append(node)
                    return AggRef(len(specs) - 1)
                return None
            return transform(e, fn)

        proj_exprs = [find_aggs(e) for e, _ in result_cols]
        having = find_aggs(core.having) if core.having is not None else None
        order_terms = []                                           # [(kind, payload, desc, nulls, collation)]
        for term, desc, nulls in order or []:
            order_terms.append(self.compile_order_term(term, desc, nulls, result_cols, aliases, ec, find_aggs))
        grouped = bool(group_exprs) or bool(specs)
        projection = [ec.compile(e) for e in proj_exprs]
        having_f = ec.compile(having) if having is not None else None
        filter_rows = self.make_rows(levels, scope)
        width = scope.width
        order_fns = []
        for kind, payload, desc, nulls, coll in order_terms:
            order_fns.append((kind, ec.compile(payload) if kind == "expr" else payload, desc, nulls, coll))

        if not grouped:
            def produce():
                for row in filter_rows():
                    if scope.referenced:
                        scope.cell[0] = row
                    out = tuple(f(row) for f in projection)
                    keys = tuple((out[p] if kind == "col" else p(row)) for kind, p, _, _, _ in order_fns)
                    yield out, keys
        else:
            group_fns = [ec.compile(g) for g in group_exprs]
            group_colls = [ec.collation(g) for g in group_exprs]
            agg_args = []
            for node in specs:
                agg_args.append(([ec.compile(a) for a in node.args], node, ec.collation(node.args[0]) if node.args else None))

            def produce():
                groups = {}
                order_seen = []
                for row in filter_rows():
                    if scope.referenced:
                        scope.cell[0] = row
                    key = tuple(V.sort_key(f(row), c) for f, c in zip(group_fns, group_colls))
                    g = groups.get(key)
                    if g is None:
                        g = groups[key] = [row, [F.make_aggregate(node.name, node.star, coll) for _, node, coll in agg_args], [set() if node.distinct else None for _, node, _ in agg_args]]
                        order_seen.append(key)
                    for (fns, node, coll), agg, seen in zip(agg_args, g[1], g[2]):
                        vals = [f(row) for f in fns]
                        if seen is not None:
                            k = tuple(V.sort_key(v, coll) for v in vals)
                            if k in seen or vals[0] is None:
                                continue
                            seen.add(k)
                        agg.step(vals)
                if not group_fns and not groups:
                    groups[()] = [(None,) * width, [F.make_aggregate(node.name, node.star, coll) for _, node, coll in agg_args], []]
                    order_seen.append(())
                for key in sorted(order_seen) if group_fns else order_seen:
                    g = groups[key]
                    out_row = g[0] + tuple(a.result() for a in g[1])
                    if scope.referenced:
                        scope.cell[0] = g[0]
                    if having_f is not None and truth(having_f(out_row)) is not True:
                        continue
                    out = tuple(f(out_row) for f in projection)
                    keys = tuple((out[p] if kind == "col" else p(out_row)) for kind, p, _, _, _ in order_fns)
                    yield out, keys
        return names, affs, produce, (order_fns, core.distinct, tuple(ec.collation(e) for e, _ in result_cols))

    def resolve_group_term(self, g, result_cols):
        if isinstance(g, A.Lit) and type(g.value) is int:
            if not 1 <= g.value <= len(result_cols):
                raise OperationalError("%s GROUP BY term out of range - should be between 1 and %d" % (ordinal(g.value), len(result_cols)))
            return result_cols[g.value - 1][0]
        if isinstance(g, A.Col) and g.table is None:
            for e, n in result_cols:
                if n.lower() == g.name.lower() and not isinstance(e, A.Col):
                    return e
        return g

    def compile_order_term(self, term, desc, nulls, result_cols, aliases, ec, find_aggs):
        coll = None
        if isinstance(term, A.Collate):
            coll, term = term.collation, term.expr
        if isinstance(term, A.Lit) and type(term.value) is int:
            n = term.value
            if not 1 <= n <= len(result_cols):
                raise OperationalError("%s ORDER BY term out of range - should be between 1 and %d" % (ordinal(n), len(result_cols)))
            return ("col", n - 1, desc, nulls, coll or ec.collation(result_cols[n - 1][0]))
        if isinstance(term, A.Col) and term.table is None and term.name.lower() in aliases and not ec.resolve(term) is not None:
            return ("col", aliases[term.name.lower()], desc, nulls, coll)
        if isinstance(term, A.Col) and term.table is None and term.name.lower() in aliases and any(
                isinstance(e, A.Col) is False and n.lower() == term.name.lower() for e, n in result_cols):
            return ("col", aliases[term.name.lower()], desc, nulls, coll)
        return ("expr", find_aggs(term), desc, nulls, coll or ec.collation(term))

    # --- sorting, distinct and cutting ----------------------------------------------------------------------------------------------------------------
    def finish_single(self, produce, order_info, select, names):
        if order_info is None:                                    # VALUES
            return self.apply_order(produce, [], False, select)
        order_fns, distinct, colls = order_info
        return self.apply_order(produce, order_fns, colls if distinct else False, select)

    @staticmethod
    def apply_order(produce, order_fns, distinct, select):
        def run():
            pairs = produce()
            if distinct:
                seen = set()
                pairs = [p for p in pairs if not (V_key(p[0], distinct) in seen or seen.add(V_key(p[0], distinct)))]
            if order_fns:
                pairs = list(pairs)
                for i in range(len(order_fns) - 1, -1, -1):
                    _, _, desc, nulls, coll = order_fns[i]
                    nulls_first = (not desc) if nulls is None else (nulls == "FIRST")
                    if nulls_first == (not desc):
                        pairs.sort(key=lambda p, i=i, coll=coll: V.sort_key(p[1][i], coll), reverse=desc)
                    else:                                              # NULLs where the direction would not put them
                        nn = [p for p in pairs if p[1][i] is not None]
                        nl = [p for p in pairs if p[1][i] is None]
                        nn.sort(key=lambda p, i=i, coll=coll: V.sort_key(p[1][i], coll), reverse=desc)
                        pairs = (nl + nn) if nulls_first else (nn + nl)
            return (p[0] for p in pairs)
        return run

    # --- UNION, INTERSECT, EXCEPT -----------------------------------------------------------------------------------------------------------------
    def compile_compound(self, select, parent_scope, depth, plan):
        parts = []
        names = affs = None
        for core in select.cores:
            n, a, produce, info = self.compile_core(core, [], parent_scope, depth, plan)
            parts.append((produce, info))
            if names is None:
                names, affs = n, a
            elif len(n) != len(names):
                raise OperationalError("SELECTs to the left and right of %s do not have the same number of result columns" % select.ops[len(parts) - 2])
        ops = select.ops
        order = select.order
        ncols = len(names)
        part_colls = [info[2] if info is not None else [None] * ncols for _, info in parts]
        # how rows of the compound compare: for each column the collation of the first SELECT, from the left, that has one (a column has: BINARY if none was declared; a
        # literal or an expression has none)
        colls = [next((pc[i] for pc in part_colls if pc[i]), None) for i in range(ncols)]
        order_cols = []
        for term, desc, nulls in order:
            coll = None
            if isinstance(term, A.Collate):
                coll, term = term.collation, term.expr
            if isinstance(term, A.Lit) and type(term.value) is int:
                if not 1 <= term.value <= len(names):
                    raise OperationalError("%s ORDER BY term out of range - should be between 1 and %d" % (ordinal(term.value), len(names)))
                idx = term.value - 1
            elif isinstance(term, A.Col) and term.table is None and term.name.lower() in [n.lower() for n in names]:
                idx = [n.lower() for n in names].index(term.name.lower())
            else:
                raise OperationalError("ORDER BY term does not match any column in the result set")
            order_cols.append((idx, desc, nulls, coll or (colls[idx] if colls else None)))

        def key(r):
            return V_key(r, colls)

        def run():
            result = [r for r, _ in self.run_part(parts[0])]
            for op, part in zip(ops, parts[1:]):
                right = [r for r, _ in self.run_part(part)]
                if op == "UNION ALL":
                    result = result + right
                elif op == "UNION":
                    result = dedupe(result + right, colls, True)
                elif op == "INTERSECT":
                    keys = {key(r) for r in right}
                    result = dedupe([r for r in result if key(r) in keys], colls, True)
                else:
                    keys = {key(r) for r in right}
                    result = dedupe([r for r in result if key(r) not in keys], colls, True)
                if op != "UNION ALL":
                    result.sort(key=key)                            # SQLite's compound selects come out in the order of the columns
            if order_cols:
                for idx, desc, nulls, coll in reversed(order_cols):
                    nulls_first = (not desc) if nulls is None else (nulls == "FIRST")
                    if nulls_first == (not desc):
                        result.sort(key=lambda r, idx=idx, coll=coll: V.sort_key(r[idx], coll), reverse=desc)
                    else:
                        nn = [r for r in result if r[idx] is not None]
                        nl = [r for r in result if r[idx] is None]
                        nn.sort(key=lambda r, idx=idx, coll=coll: V.sort_key(r[idx], coll), reverse=desc)
                        result = (nl + nn) if nulls_first else (nn + nl)
            return iter(result)
        return names, affs, run, part_colls[0]

    @staticmethod
    def run_part(part):
        produce, info = part
        return produce()


def V_key(row, collations=None):
    if collations:
        return tuple(V.sort_key(v, c) for v, c in zip(row, collations))
    return tuple(V.sort_key(v) for v in row)


def dedupe(rows, collations=None, keep_last=False):
    """The rows without duplicates; of rows that compare equal, SQLite's compound selects keep the last one, DISTINCT the first."""
    seen = {}
    for r in rows:
        k = V_key(r, collations)
        if keep_last or k not in seen:
            seen[k] = r
    return list(seen.values())


def ordinal(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))
