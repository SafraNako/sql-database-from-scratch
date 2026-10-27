"""The syntax tree of SQL statements and expressions."""
from __future__ import annotations


def _node(name, fields):
    fields = tuple(fields.split())

    def __init__(self, *args, **kwargs):
        for f, v in zip(fields, args):
            setattr(self, f, v)
        for f in fields[len(args):]:
            setattr(self, f, kwargs.get(f))

    def __repr__(self):
        return "%s(%s)" % (name, ", ".join("%s=%r" % (f, getattr(self, f)) for f in fields))
    return type(name, (), {"__slots__": fields, "__init__": __init__, "__repr__": __repr__, "fields": fields})


# --- expressions -------------------------------------------------------------------------------------------------------------------------------
Lit = _node("Lit", "value")
Col = _node("Col", "table name")                           # table may be None
Param = _node("Param", "index name")
Unary = _node("Unary", "op expr")                          # - + ~ NOT
Binary = _node("Binary", "op left right")                  # arithmetic, comparison, AND, OR, ||, bit operations, IS, IS NOT
Between = _node("Between", "expr low high negated")
InList = _node("InList", "expr items negated")
InSelect = _node("InSelect", "expr select negated")
Like = _node("Like", "op expr pattern escape negated")     # op: LIKE or GLOB
IsNull = _node("IsNull", "expr negated")
Case = _node("Case", "base whens other")                   # whens: [(condition, result)]
Cast = _node("Cast", "expr typename")
Func = _node("Func", "name args distinct star")
Exists = _node("Exists", "select negated")
Subquery = _node("Subquery", "select")
Collate = _node("Collate", "expr collation")

# --- select ------------------------------------------------------------------------------------------------------------------------------------
Select = _node("Select", "cores ops order limit offset ctes")      # cores: [SelectCore]; ops: ["UNION", ...] between them; order: [(expr, desc, nulls)]
SelectCore = _node("SelectCore", "distinct columns sources where group having values")   # sources: a From tree or None; values: [[expr]] for VALUES
ResultCol = _node("ResultCol", "expr alias star_table star text")   # star: True for * (star_table for t.*); text: the expression as written
TableRef = _node("TableRef", "name alias")
SubqueryRef = _node("SubqueryRef", "select alias")
Join = _node("Join", "left right kind on using natural")           # kind: INNER, LEFT, CROSS

# --- statements --------------------------------------------------------------------------------------------------------------------------------
ColumnDef = _node("ColumnDef", "name typename primary_key autoincrement not_null unique default collation check desc text default_text")
TableConstraint = _node("TableConstraint", "kind columns expr")    # kind: PRIMARY KEY, UNIQUE, CHECK
CreateTable = _node("CreateTable", "name columns constraints if_not_exists sql select")
CreateIndex = _node("CreateIndex", "name table columns unique if_not_exists sql where")   # columns: [(expr_or_name, collation, desc)]
DropTable = _node("DropTable", "name if_exists")
DropIndex = _node("DropIndex", "name if_exists")
Insert = _node("Insert", "table columns rows select default_values action returning")
Update = _node("Update", "table assignments where action")        # assignments: [(column, expr)]
Delete = _node("Delete", "table where")
Begin = _node("Begin", "mode")
Commit = _node("Commit", "")
Rollback = _node("Rollback", "")
Pragma = _node("Pragma", "name arg")
Explain = _node("Explain", "stmt plan")
AlterTable = _node("AlterTable", "table action arg")
Vacuum = _node("Vacuum", "")
CreateView = _node("CreateView", "name columns select if_not_exists sql")
DropView = _node("DropView", "name if_exists")
