"""SQL values and the rules of SQLite for them: storage classes and their order, column affinity, comparison, arithmetic and casts.

A value is ``None`` (NULL), ``int`` (64-bit), ``float``, ``str`` or ``bytes``. Values of different storage classes compare in a fixed order: NULL, then numbers, then text, then
blobs. A column has an *affinity*, decided by the text of its declared type, that says which storage class a value put into it is converted to; and when two things are
compared, the affinity of a column involved is applied to the other side first, which is why ``WHERE int_column = '5'`` finds the row with the integer 5.
"""
from __future__ import annotations

import math

from .format import INT_MAX, INT_MIN

INTEGER, TEXT, BLOB, REAL, NUMERIC = "INTEGER", "TEXT", "BLOB", "REAL", "NUMERIC"
_SPACE = " \t\n\v\f\r"


def type_name(v):
    if v is None:
        return "null"
    t = type(v)
    if t is int:
        return "integer"
    if t is float:
        return "real"
    if t is str:
        return "text"
    return "blob"


def storage_class(v):
    """0 NULL, 1 number, 2 text, 3 blob: the order in which values of different kinds sort."""
    if v is None:
        return 0
    t = type(v)
    if t is int or t is float:
        return 1
    if t is str:
        return 2
    return 3


def affinity_of(declared):
    """The affinity of a column from its declared type: the rules of SQLite, in order."""
    t = (declared or "").upper()
    if "INT" in t:
        return INTEGER
    if "CHAR" in t or "CLOB" in t or "TEXT" in t:
        return TEXT
    if "BLOB" in t or not t:
        return BLOB
    if "REAL" in t or "FLOA" in t or "DOUB" in t:
        return REAL
    return NUMERIC


# --- numbers in text --------------------------------------------------------------------------------------------------------------------------
def _scan_number(s, start=0):
    """The longest numeric prefix of ``s`` from ``start``: ``(end, is_integer)`` or ``None`` if there is no digit."""
    i, n = start, len(s)
    if i < n and s[i] in "+-":
        i += 1
    digits = 0
    while i < n and "0" <= s[i] <= "9":
        i += 1
        digits += 1
    is_int = True
    if i < n and s[i] == ".":
        j = i + 1
        frac = 0
        while j < n and "0" <= s[j] <= "9":
            j += 1
            frac += 1
        if digits or frac:
            i, digits, is_int = j, digits + frac, False
    if not digits:
        return None
    if i < n and s[i] in "eE":
        j = i + 1
        if j < n and s[j] in "+-":
            j += 1
        k = j
        while k < n and "0" <= s[k] <= "9":
            k += 1
        if k > j:
            i, is_int = k, False
    return i, is_int


def text_to_number(s):
    """Is the whole text (outside leading and trailing blanks) a number? Then that number (int if it is written as one and fits, else float), else ``None``."""
    t = s.strip(_SPACE)
    found = _scan_number(t)
    if found is None or found[0] != len(t):
        return None
    if found[1]:
        v = int(t)
        return v if INT_MIN <= v <= INT_MAX else float(t)
    return float(t)


def numeric_prefix(s):
    """The number at the start of a text, ignoring leading blanks and whatever follows it; 0 if there is none (what arithmetic on a string does)."""
    t = s.lstrip(_SPACE)
    found = _scan_number(t)
    if found is None:
        return 0
    text = t[:found[0]]
    if found[1]:
        v = int(text)
        return v if INT_MIN <= v <= INT_MAX else float(text)
    return float(text)


def real_to_text(f):
    """A float as text the way SQLite writes it: 15 significant digits, and always something that reads back as a real (``1.0``, ``1.0e+20``)."""
    if f != f:
        return "NaN"
    if f in (math.inf, -math.inf):
        return "Inf" if f > 0 else "-Inf"
    if f == 0:
        return "0.0"                                                       # (negative zero too: SQLite prints no sign)
    s = "%.15g" % f
    if "e" in s:
        mant, exp = s.split("e")
        if "." not in mant:
            mant += ".0"
        return "%se%s%02d" % (mant, exp[0], int(exp[1:]))
    if "." not in s:
        s += ".0"
    return s


def to_text(v):
    """A value converted to text (NULL stays NULL)."""
    if v is None or type(v) is str:
        return v
    if type(v) is int:
        return str(v)
    if type(v) is float:
        return real_to_text(v)
    return bytes(v).decode("utf-8", "surrogateescape")


def to_number(v):
    """A value as a number for arithmetic: NULL stays NULL, text and blobs use their numeric prefix."""
    t = type(v)
    if t is int or t is float or v is None:
        return v
    if t is str:
        return numeric_prefix(v)
    return numeric_prefix(bytes(v).decode("utf-8", "surrogateescape"))


def float_to_int(f):
    """A float as an integer, truncating toward zero and saturating at the ends of the 64-bit range (what CAST AS INTEGER does)."""
    if f != f:
        return 0
    if f >= 9.2233720368547758e18:
        return INT_MAX
    if f <= -9.2233720368547758e18:
        return INT_MIN
    return int(f)


def integer_prefix(s):
    """The integer at the start of a text (sign and digits only: ``'12.9'`` and ``'1e3'`` give 12 and 1), saturating at the ends of the 64-bit range; 0 if there is none."""
    t = s.lstrip(_SPACE)
    i = 0
    if t[:1] in ("+", "-"):
        i = 1
    j = i
    while j < len(t) and "0" <= t[j] <= "9":
        j += 1
    if j == i:
        return 0
    v = int(t[:j])
    return INT_MAX if v > INT_MAX else (INT_MIN if v < INT_MIN else v)


def to_integer(v):
    t = type(v)
    if v is None or t is int:
        return v
    if t is float:
        return float_to_int(v)
    return integer_prefix(v if t is str else bytes(v).decode("utf-8", "surrogateescape"))


# --- affinity -----------------------------------------------------------------------------------------------------------------------------------
def apply_affinity(v, aff):
    """What storing ``v`` in a column of this affinity (or comparing it with such a column) turns it into."""
    if v is None or aff == BLOB:
        return v
    t = type(v)
    if aff == TEXT:
        if t is int or t is float:
            return to_text(v)
        return v
    if aff == REAL:
        if t is int:
            return float(v)
        if t is str:
            n = text_to_number(v)
            return float(n) if n is not None else v
        return v
    # INTEGER and NUMERIC
    if t is str:
        n = text_to_number(v)
        if n is None:
            return v
        v, t = n, type(n)
    if t is float and v == v and -9.2233720368547758e18 <= v < 9.2233720368547758e18 and v == int(v):
        return int(v)                                                      # a real that is a whole number is stored as an integer
    return v


# --- comparison ---------------------------------------------------------------------------------------------------------------------------------
def collate_key(text, collation):
    if collation == "NOCASE":
        key = text.translate(_NOCASE)
        i = key.find("\0")
        if i >= 0:                                                     # SQLite's NOCASE stops at a NUL: what follows it is not compared, only the length of the whole is
            key = key[:i + 1] + chr(min(len(text.encode("utf-8", "surrogateescape")) + 1, 0x10FFFF))
        return key
    if collation == "RTRIM":
        return text.rstrip(" ")
    return text


_NOCASE = {i: i + 32 for i in range(ord("A"), ord("Z") + 1)}


def compare(a, b, collation=None):
    """-1, 0 or 1 for two values that are not NULL."""
    ca, cb = storage_class(a), storage_class(b)
    if ca != cb:
        return -1 if ca < cb else 1
    if ca == 2 and collation:
        a, b = collate_key(a, collation), collate_key(b, collation)
    elif ca == 3:
        a, b = bytes(a), bytes(b)
    return -1 if a < b else (1 if a > b else 0)


def sort_key(v, collation=None):
    """A key that Python sorts into SQLite's order (NULL first)."""
    if v is None:
        return (0, 0)
    t = type(v)
    if t is int or t is float:
        return (1, v)
    if t is str:
        return (2, collate_key(v, collation) if collation else v)
    return (3, bytes(v))


def record_compare(a, b, collations=None):
    """Compare two records (lists of values, NULL sorting first, equal NULLs equal) column by column, over the length of the shorter one."""
    for i in range(min(len(a), len(b))):
        x, y = a[i], b[i]
        if x is None or y is None:
            if x is None and y is None:
                continue
            return -1 if x is None else 1
        r = compare(x, y, collations[i] if collations and i < len(collations) else None)
        if r:
            return r
    return 0


# --- arithmetic ---------------------------------------------------------------------------------------------------------------------------------
def _fit(v):
    """An integer result outside the 64-bit range becomes a float."""
    if type(v) is int and not (INT_MIN <= v <= INT_MAX):
        return float(v)
    return v


def _nan_is_null(x):
    return None if x != x else x                                           # a NaN result is NULL


def add(a, b):
    if a is None or b is None:
        return None
    a, b = to_number(a), to_number(b)
    return _fit(a + b) if type(a) is int and type(b) is int else _nan_is_null(float(a) + float(b))


def sub(a, b):
    if a is None or b is None:
        return None
    a, b = to_number(a), to_number(b)
    return _fit(a - b) if type(a) is int and type(b) is int else _nan_is_null(float(a) - float(b))


def mul(a, b):
    if a is None or b is None:
        return None
    a, b = to_number(a), to_number(b)
    if type(a) is int and type(b) is int:
        return _fit(a * b)
    try:
        return _nan_is_null(float(a) * float(b))
    except OverflowError:
        return math.inf


def div(a, b):
    if a is None or b is None:
        return None
    a, b = to_number(a), to_number(b)
    if type(a) is int and type(b) is int:
        if b == 0:
            return None
        q = abs(a) // abs(b)
        q = q if (a < 0) == (b < 0) else -q
        return _fit(q)
    b = float(b)
    if b == 0.0:
        return None
    return _nan_is_null(float(a) / b)


def mod(a, b):
    if a is None or b is None:
        return None
    real = type(to_number(a)) is float or type(to_number(b)) is float
    ia, ib = to_integer(a), to_integer(b)
    if ib == 0:
        return None
    r = abs(ia) % abs(ib)
    r = -r if ia < 0 else r
    return float(r) if real else r


def neg(a):
    """Unary minus is ``0 - a`` (so that ``-0.0`` is ``0.0``, as in SQLite)."""
    if a is None:
        return None
    a = to_number(a)
    if type(a) is int:
        return _fit(-a)
    return 0.0 - a


def concat(a, b):
    if a is None or b is None:
        return None
    return to_text(a) + to_text(b)


def bit_and(a, b):
    return None if a is None or b is None else to_integer(a) & to_integer(b)


def bit_or(a, b):
    return None if a is None or b is None else to_integer(a) | to_integer(b)


def shift(a, b, left):
    if a is None or b is None:
        return None
    a, b = to_integer(a), to_integer(b)
    if b < 0:
        a, b, left = a, -b, not left
    if b >= 64:
        return 0 if (left or a >= 0) else -1
    v = (a << b) if left else (a >> b)
    v &= 0xFFFFFFFFFFFFFFFF
    return v - (1 << 64) if v >= (1 << 63) else v


def cast(v, typename):
    """``CAST(v AS typename)``: the affinity rules of SQLite decide the target from the type name."""
    if v is None:
        return None
    aff = affinity_of(typename)
    if aff == TEXT:
        return to_text(v)
    if aff == BLOB:
        if type(v) is bytes:
            return v
        return to_text(v).encode("utf-8", "surrogateescape")
    if aff == INTEGER:
        return to_integer(v)
    if aff == REAL:
        n = to_number(v)
        return float(n)
    if type(v) is int or type(v) is float:                                 # NUMERIC leaves numbers alone; text becomes the number it starts with
        return v
    return apply_affinity(to_number(v), NUMERIC)


# --- LIKE and GLOB -----------------------------------------------------------------------------------------------------------------------------
def like(text, pattern, escape=None):
    """SQL LIKE: ``%`` any run of characters, ``_`` one character; letters match without regard to case (ASCII only)."""
    t = text.translate(_NOCASE)
    p = pattern.translate(_NOCASE)
    return _match(t, 0, p, 0, escape.translate(_NOCASE) if escape else None)


def _match(t, i, p, j, esc):
    n, m = len(t), len(p)
    while j < m:
        c = p[j]
        if esc is not None and c == esc and j + 1 < m:
            if i < n and t[i] == p[j + 1]:
                i += 1
                j += 2
                continue
            return False
        if c == "%":
            while j < m and p[j] == "%":
                j += 1
            if j == m:
                return True
            for k in range(i, n + 1):
                if _match(t, k, p, j, esc):
                    return True
            return False
        if c == "_":
            if i >= n:
                return False
            i += 1
            j += 1
            continue
        if i < n and t[i] == c:
            i += 1
            j += 1
            continue
        return False
    return i == n


def glob(text, pattern):
    """SQL GLOB: Unix-style, case sensitive: ``*``, ``?`` and ``[a-z]`` / ``[^a-z]``."""
    return _glob(text, 0, pattern, 0)


def _glob(t, i, p, j):
    n, m = len(t), len(p)
    while j < m:
        c = p[j]
        if c == "*":
            while j < m and p[j] == "*":
                j += 1
            if j == m:
                return True
            for k in range(i, n + 1):
                if _glob(t, k, p, j):
                    return True
            return False
        if c == "?":
            if i >= n:
                return False
            i += 1
            j += 1
        elif c == "[":
            if i >= n:
                return False
            k = j + 1
            negate = k < m and p[k] == "^"
            if negate:
                k += 1
            matched = False
            first = True
            while k < m and (p[k] != "]" or first):
                first = False
                if k + 2 < m and p[k + 1] == "-" and p[k + 2] != "]":
                    if p[k] <= t[i] <= p[k + 2]:
                        matched = True
                    k += 3
                else:
                    if p[k] == t[i]:
                        matched = True
                    k += 1
            if k >= m:                                                      # no closing bracket: the pattern cannot match
                return False
            if matched == negate:
                return False
            i += 1
            j = k + 1
        else:
            if i >= n or t[i] != c:
                return False
            i += 1
            j += 1
    return i == n
