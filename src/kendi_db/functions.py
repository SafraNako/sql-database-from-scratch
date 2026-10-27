"""The built-in functions of SQL: scalar functions and aggregates, with SQLite's behaviour for NULLs, types and edge cases."""
from __future__ import annotations

import math
import os
import struct
from decimal import ROUND_HALF_UP, Decimal

from . import values as V
from .errors import DatabaseError, OperationalError
from .format import INT_MAX, INT_MIN


_MISSING = object()                                                   # an optional argument that was left out (as opposed to given as NULL)


def _text(v):
    return V.to_text(v)


def f_abs(x):
    if x is None:
        return None
    if type(x) is int:
        if x == INT_MIN:
            raise DatabaseError("integer overflow")
        return abs(x)
    if type(x) is float:
        return abs(x)
    n = V.to_number(x)
    return abs(float(n))


def f_length(x):
    if x is None:
        return None
    if type(x) is bytes:
        return len(x)
    t = _text(x)
    nul = t.find("\x00")
    return len(t if nul < 0 else t[:nul])


def f_lower(x):
    return None if x is None else _text(x).translate({i: i + 32 for i in range(65, 91)})


def f_upper(x):
    return None if x is None else _text(x).translate({i: i - 32 for i in range(97, 123)})


def f_substr(x, start, length=_MISSING):
    if x is None or start is None or length is None:
        return None
    is_blob = type(x) is bytes
    if is_blob and not x:
        return None                                                    # SQLite finds no pointer to an empty blob and gives NULL
    s = x if is_blob else _cstr(x)                                    # (text is walked as a C string: it ends at a NUL)
    n = len(s)
    p1 = V.to_integer(start)
    neg = False
    if length is _MISSING:
        p2 = 1_000_000_000                                              # SQLITE_MAX_LENGTH
    else:
        p2 = V.to_integer(length)
        if p2 < 0:
            p2, neg = -p2, True
    if p1 < 0:
        p1 += n
        if p1 < 0:
            p2 += p1
            if p2 < 0:
                p2 = 0
            p1 = 0
    elif p1 > 0:
        p1 -= 1
    elif p2 > 0:
        p2 -= 1
    if neg:
        p1 -= p2
        if p1 < 0:
            p2 += p1
            p1 = 0
    return s[p1:p1 + p2]


def _trim(x, chars, left, right):
    if x is None:
        return None
    s = _text(x)
    if chars is _MISSING:
        cs = " "
    elif chars is None:
        return None
    else:
        cs = _cstr(chars)                                              # (the set of characters ends at a NUL)
        if cs == "":
            return s
    if left:
        s = s.lstrip(cs)
    if right:
        s = s.rstrip(cs)
    return s


def f_replace(x, y, z):
    if x is None or y is None:
        return None
    y = _cstr(y)
    if y == "":
        return _text(x)                                                # nothing to replace: the value comes back (as text) before the replacement is even looked at
    if z is None:
        return None
    return _text(x).replace(y, _text(z))


def f_instr(x, y):
    if x is None or y is None:
        return None
    if type(x) is bytes and type(y) is bytes:
        return x.find(y) + 1
    return _text(x).find(_text(y)) + 1


def f_hex(x):
    if x is None:
        return ""
    b = x if type(x) is bytes else _text(x).encode("utf-8", "surrogateescape")
    return b.hex().upper()


def f_round(x, digits=_MISSING):
    if x is None or digits is None:
        return None
    n = 0 if digits is _MISSING else V.to_integer(digits)
    n = max(0, min(30, n or 0))
    f = float(V.to_number(x))
    if f != f or f in (math.inf, -math.inf):
        return f
    if n == 0:
        if abs(f) < 4503599627370496.0:
            return float(int(f + (0.5 if f >= 0 else -0.5)))
        return f
    if abs(f) >= 1e15:
        return f
    return float(Decimal(f).quantize(Decimal(1).scaleb(-n), rounding=ROUND_HALF_UP))


def f_sign(x):
    if x is None or type(x) is bytes:
        return None
    if type(x) is str:
        x = V.text_to_number(x)                                        # text counts only if it is a number
        if x is None:
            return None
    return (x > 0) - (x < 0)                                           # always an integer


def f_nullif(a, b):
    if a is None or b is None:
        return a
    return None if V.compare(a, b) == 0 else a


def f_coalesce(*args):
    for a in args:
        if a is not None:
            return a
    return None


def f_scalar_min(*args):
    if any(a is None for a in args):
        return None
    best = args[0]
    for a in args[1:]:
        if V.compare(a, best) <= 0:                                    # of equal values the last one is the answer (and for max the first)
            best = a
    return best


def f_scalar_max(*args):
    if any(a is None for a in args):
        return None
    best = args[0]
    for a in args[1:]:
        if V.compare(a, best) > 0:
            best = a
    return best


def f_quote(x):
    if x is None:
        return "NULL"
    if type(x) is str:
        return "'" + x.replace("'", "''") + "'"
    if type(x) is bytes:
        return "X'" + x.hex().upper() + "'"
    if type(x) is float:
        return V.real_to_text(x)
    return str(x)


def f_unicode(x):
    if x is None:
        return None
    t = _cstr(x)
    return ord(t[0]) if t else None


def f_char(*args):
    return "".join(chr(V.to_integer(a)) for a in args if a is not None)


def f_zeroblob(n):
    return b"\x00" * max(0, V.to_integer(n))


def f_randomblob(n):
    return os.urandom(max(1, V.to_integer(n)))


def f_random():
    return struct.unpack(">q", os.urandom(8))[0]


def _cstr(v):
    """The text as a C string: SQLite's pattern matching stops at the first NUL."""
    t = _text(v)
    i = t.find("\x00")
    return t if i < 0 else t[:i]


def f_like(pattern, text, escape=None):
    if pattern is None or text is None:
        return None
    return 1 if V.like(_cstr(text), _cstr(pattern), _cstr(escape) if escape is not None else None) else 0


def f_glob(pattern, text):
    if pattern is None or text is None:
        return None
    return 1 if V.glob(_cstr(text), _cstr(pattern)) else 0


def f_iif(c, a, b):
    return a if (c is not None and V.to_number(c) not in (0, 0.0) and V.to_number(c) is not None) else b


def f_printf(fmt, *args):
    if fmt is None:
        return None
    out, i, a = [], 0, 0
    fmt = _text(fmt)
    while i < len(fmt):
        c = fmt[i]
        if c != "%":
            out.append(c)
            i += 1
            continue
        j = i + 1
        while j < len(fmt) and fmt[j] in "-+ 0#.0123456789":
            j += 1
        if j >= len(fmt):
            break
        flags, conv = fmt[i + 1:j], fmt[j]
        i = j + 1
        if conv == "%":
            out.append("%")
            continue
        arg = args[a] if a < len(args) else None
        a += 1
        if conv in "di":
            out.append(("%" + flags + "d") % (V.to_integer(arg) if arg is not None else 0))
        elif conv in "fFeEgG":
            out.append(("%" + flags + conv) % (float(V.to_number(arg)) if arg is not None else 0.0))
        elif conv == "s":
            out.append(("%" + flags + "s") % (_text(arg) if arg is not None else ""))
        elif conv in "xXo":
            out.append(("%" + flags + conv) % (V.to_integer(arg) if arg is not None else 0))
        elif conv == "c":
            out.append(chr(V.to_integer(arg)) if arg is not None else "")
        elif conv == "q":
            out.append((_text(arg) if arg is not None else "NULL").replace("'", "''"))
        elif conv == "Q":
            out.append("NULL" if arg is None else "'" + _text(arg).replace("'", "''") + "'")
        else:
            out.append("%" + flags + conv)
    return "".join(out)


def f_concat(*args):
    return "".join(_text(a) for a in args if a is not None)


def f_concat_ws(sep, *args):
    if sep is None:
        return None
    return _text(sep).join(_text(a) for a in args if a is not None)


def f_ifnull(a, b):
    return b if a is None else a


def f_typeof(x):
    return V.type_name(x)


def f_sqlite_version():
    return "3.45.0"


# name -> (minimum arguments, maximum arguments or None, function)
SCALARS = {
    "abs": (1, 1, f_abs), "length": (1, 1, f_length), "lower": (1, 1, f_lower), "upper": (1, 1, f_upper), "substr": (2, 3, f_substr), "substring": (2, 3, f_substr),
    "trim": (1, 2, lambda x, c=_MISSING: _trim(x, c, True, True)), "ltrim": (1, 2, lambda x, c=_MISSING: _trim(x, c, True, False)),
    "rtrim": (1, 2, lambda x, c=_MISSING: _trim(x, c, False, True)),
    "replace": (3, 3, f_replace), "instr": (2, 2, f_instr), "hex": (1, 1, f_hex), "typeof": (1, 1, f_typeof), "round": (1, 2, f_round), "sign": (1, 1, f_sign),
    "nullif": (2, 2, f_nullif), "coalesce": (2, None, f_coalesce), "ifnull": (2, 2, f_ifnull), "iif": (3, 3, f_iif), "quote": (1, 1, f_quote), "unicode": (1, 1, f_unicode),
    "char": (0, None, f_char), "zeroblob": (1, 1, f_zeroblob), "randomblob": (1, 1, f_randomblob), "random": (0, 0, f_random), "like": (2, 3, f_like), "glob": (2, 2, f_glob),
    "printf": (1, None, f_printf), "format": (1, None, f_printf), "concat": (1, None, f_concat), "concat_ws": (2, None, f_concat_ws), "sqlite_version": (0, 0, f_sqlite_version),
}
SCALAR_MIN_MAX = {"min": f_scalar_min, "max": f_scalar_max}


# --- aggregates ---------------------------------------------------------------------------------------------------------------------------------
class Count:
    def __init__(self, star=False):
        self.n = 0
        self.star = star

    def step(self, args):
        if self.star or args[0] is not None:
            self.n += 1

    def result(self):
        return self.n


class FloatSum:
    """A running sum of floats that keeps the rounding errors (Kahan-Babuska summation, as SQLite does)."""
    __slots__ = ("s", "c")

    def __init__(self):
        self.s = 0.0
        self.c = 0.0

    def add(self, x):
        t = self.s + x
        if t != t or t in (math.inf, -math.inf):                       # no error term for infinities (it would turn them into NaN)
            self.s = t
            return
        if abs(self.s) >= abs(x):
            self.c += (self.s - t) + x
        else:
            self.c += (x - t) + self.s
        self.s = t

    @property
    def value(self):
        return self.s + self.c


class Sum:
    """sum() and total(): the integers are added exactly while that is possible; a real in the input (or an overflow, if a real follows) makes the result a float."""

    def __init__(self, total=False):
        self.total = total
        self.int_sum = 0
        self.float_sum = FloatSum()
        self.approx = total                                            # the result is a float
        self.overflow = False
        self.seen = False

    def step(self, args):
        v = args[0]
        if v is None:
            return
        self.seen = True
        if type(v) is str:
            n = V.text_to_number(v)                                    # a text that is a number: as an integer if it is written as one, else as a real
            v = n if n is not None else V.numeric_prefix(v) + 0.0
        elif type(v) is bytes:
            v = V.numeric_prefix(V.to_text(v)) + 0.0
        if type(v) is int:
            self.float_sum.add(float(v))
            if not (self.approx or self.overflow):
                self.int_sum += v
                if not (INT_MIN <= self.int_sum <= INT_MAX):
                    self.approx = self.overflow = True
        else:
            self.approx = True
            self.overflow = False
            self.float_sum.add(float(v))

    def result(self):
        if not self.seen:
            return 0.0 if self.total else None
        if self.approx:
            if self.overflow:
                raise DatabaseError("integer overflow")
            return self.float_sum.value
        return self.int_sum


class Avg:
    def __init__(self):
        self.total = FloatSum()
        self.n = 0

    def step(self, args):
        v = args[0]
        if v is None:
            return
        if type(v) is str:
            n = V.text_to_number(v)
            v = n if n is not None else V.numeric_prefix(v)
        elif type(v) is bytes:
            v = V.numeric_prefix(V.to_text(v))
        self.total.add(float(v))
        self.n += 1

    def result(self):
        return self.total.value / self.n if self.n else None


class MinMax:
    def __init__(self, want_max, collation=None):
        self.best = None
        self.want_max = want_max
        self.collation = collation

    def step(self, args):
        v = args[0]
        if v is None:
            return
        if self.best is None:
            self.best = v
        else:
            r = V.compare(v, self.best, self.collation)
            if (r > 0) if self.want_max else (r < 0):
                self.best = v

    def result(self):
        return self.best


class GroupConcat:
    def __init__(self):
        self.parts = []
        self.sep = ","

    def step(self, args):
        if args[0] is None:
            return
        if len(args) > 1 and args[1] is not None:
            self.sep = _text(args[1])
        self.parts.append(_text(args[0]))

    def result(self):
        return self.sep.join(self.parts) if self.parts else None


AGGREGATES = {
    "count": lambda star: Count(star), "sum": lambda star: Sum(), "total": lambda star: Sum(True), "avg": lambda star: Avg(), "group_concat": lambda star: GroupConcat(),
    "string_agg": lambda star: GroupConcat(),
}
AGGREGATE_ARGS = {"count": (0, 1), "sum": (1, 1), "total": (1, 1), "avg": (1, 1), "group_concat": (1, 2), "string_agg": (2, 2), "min": (1, 1), "max": (1, 1)}


def is_aggregate(name, nargs):
    if name in ("min", "max"):
        return nargs == 1
    return name in AGGREGATES


def make_aggregate(name, star, collation=None):
    if name == "min":
        return MinMax(False, collation)
    if name == "max":
        return MinMax(True, collation)
    return AGGREGATES[name](star)


def check_scalar(name, nargs):
    if name in SCALAR_MIN_MAX:
        if nargs < 2:
            raise OperationalError("wrong number of arguments to function %s()" % name)
        return
    spec = SCALARS.get(name)
    if spec is None:
        raise OperationalError("no such function: %s" % name)
    lo, hi, _ = spec
    if nargs < lo or (hi is not None and nargs > hi):
        raise OperationalError("wrong number of arguments to function %s()" % name)
