"""Run the same statements through this engine and through SQLite and compare what they say."""
import os
import sqlite3

import kendi_db

MODERN_SQLITE = (3, 50, 0)                   # the SQLite this engine was written against: older ones differ in corners that the fuzzer reaches with its odd arguments:
OLD_SQLITE_DIFFERS = ("substr(", "replace(")  # substr() with huge or fractional start and length, and replace() with an empty pattern (older ones return the value itself, not text)


def normalize(v):
    return v


def same_value(a, b, loose=False, fold=False):
    """``loose``: an integer and a real that are equal are the same; ``fold``: so are two texts that differ only in case (rows that a NOCASE collation calls equal are
    interchangeable, and which of them a DISTINCT or a compound select hands back is up to the implementation)."""
    if loose and fold and type(a) is str and type(b) is str:
        return a.lower() == b.lower()
    if loose and type(a) in (int, float) and type(b) in (int, float):
        return a == b or (type(a) is float and type(b) is float and abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-300))
    if type(a) is float and type(b) is float:
        if a != a and b != b:
            return True
        return a == b or abs(a - b) <= 1e-9 * max(abs(a), abs(b), 1e-300)
    if type(a) != type(b):
        return False
    return a == b


def same_rows(a, b, ordered, loose=False, fold=False):
    if len(a) != len(b):
        return False
    if not ordered:
        key = lambda r: tuple((type(v).__name__ if not isinstance(v, (int, float)) else "n", (0 if v is None else v if not isinstance(v, bytes) else v.hex()),   # noqa: E731
                              "" if loose else type(v).__name__) for v in (r if not fold else [x.lower() if type(x) is str else x for x in r]))
        try:
            a, b = sorted(a, key=key), sorted(b, key=key)
        except TypeError:
            a, b = sorted(a, key=repr), sorted(b, key=repr)
    for ra, rb in zip(a, b):
        if len(ra) != len(rb) or not all(same_value(x, y, loose, fold) for x, y in zip(ra, rb)):
            return False
    return True


def multiset_difference(a, b):
    """The first few rows that are in ``a`` but not in ``b`` and the other way round (for a readable report)."""
    def key(r):
        return repr([(type(v).__name__, v if not isinstance(v, float) else round(v, 9)) for v in r])
    ca, cb = {}, {}
    for r in a:
        ca.setdefault(key(r), []).append(r)
    for r in b:
        cb.setdefault(key(r), []).append(r)
    only_a = [r for k, rs in ca.items() for r in rs[len(cb.get(k, [])):]]
    only_b = [r for k, rs in cb.items() for r in rs[len(ca.get(k, [])):]]
    return only_a[:3], only_b[:3]


class Pair:
    """A database of this engine and one of SQLite, in a directory."""

    def __init__(self, directory, page_size=4096, fold_case=False):
        self.fold_case = fold_case
        self.mine_path = os.path.join(directory, "mine.db")
        self.ref_path = os.path.join(directory, "ref.db")
        self.mine = kendi_db.connect(self.mine_path, page_size)
        self.ref = sqlite3.connect(self.ref_path, isolation_level=None)
        self.ref.text_factory = lambda b: b.decode("utf-8", "surrogateescape")          # text made from blobs need not be valid UTF-8
        self.ref.execute("PRAGMA page_size=%d" % page_size)

    def run(self, sql):
        """``(ok, mine, ref)``: whether both engines accepted it and agree, plus what each said (rows, or the exception)."""
        try:
            mine = self.mine.execute(sql).fetchall()
            mine_err = None
        except kendi_db.DatabaseError as exc:
            mine, mine_err = None, exc
        except Exception as exc:                                              # a bug of ours, not a SQL error: keep the type visible
            mine, mine_err = None, exc
        try:
            ref = self.ref.execute(sql).fetchall()
            ref_err = None
        except (sqlite3.Error, OverflowError) as exc:
            ref, ref_err = None, exc
        return mine, mine_err, ref, ref_err

    def compare(self, sql, ordered=None, strict=False):
        """``None`` if they agree, else a description of the difference (``strict``: an integer and a real that are equal are still different)."""
        if sqlite3.sqlite_version_info < MODERN_SQLITE and any(f in sql.lower() for f in OLD_SQLITE_DIFFERS):
            return None
        mine, mine_err, ref, ref_err = self.run(sql)
        if mine_err is not None and not isinstance(mine_err, kendi_db.DatabaseError):
            return "CRASH %s: %r" % (type(mine_err).__name__, mine_err)
        if (mine_err is None) != (ref_err is None):
            return "one failed: mine %r, sqlite %r" % (mine_err, ref_err)
        if mine_err is not None:
            return None
        if ordered is None:
            ordered = " ORDER BY " in sql.upper()
        upper = sql.upper()
        loose = not strict and (ordered or any(w in upper for w in ("UNION", "INTERSECT", "EXCEPT", "DISTINCT", "GROUP BY")))     # 0 and 0.0 tie in a sort, and one of them survives a removal of duplicates
        fold = self.fold_case and loose
        if not same_rows(mine, ref, ordered, loose, fold):
            if ordered and len(mine) == len(ref):
                i = next((k for k, (x, y) in enumerate(zip(mine, ref)) if not (len(x) == len(y) and all(same_value(p, q, loose, fold) for p, q in zip(x, y)))), 0)
                return "rows differ at row %d of %d:\n    mine:   %r\n    sqlite: %r" % (i, len(mine), mine[i], ref[i])
            return "rows differ (%d vs %d rows):\n    mine:   %r\n    sqlite: %r%s" % (len(mine), len(ref), mine[:4], ref[:4], "" if ordered else "\n    only in mine: %r\n    only in sqlite: %r" % tuple(multiset_difference(mine, ref)))
        return None

    def close(self):
        self.mine.close()
        self.ref.close()
