"""The tokens of SQL: identifiers (bare, "quoted", [bracketed], `backticked`), numbers, 'strings', x'blobs', operators and parameters."""
from __future__ import annotations

from .errors import SQLSyntaxError

ID, QID, INT, FLOAT, STR, BLOB, OP, PARAM, EOF = "id", "qid", "int", "float", "str", "blob", "op", "param", "eof"
TWO_CHAR = {"||", "<=", ">=", "==", "!=", "<>", "<<", ">>"}
ONE_CHAR = set("+-*/%<>=(),;.&|~")


class Token:
    __slots__ = ("kind", "value", "pos", "end")

    def __init__(self, kind, value, pos, end):
        self.kind = kind
        self.value = value                    # the text of an identifier or string, the number, the operator
        self.pos = pos
        self.end = end

    @property
    def upper(self):
        return self.value.upper() if self.kind == ID else None

    def __repr__(self):
        return "Token(%s, %r)" % (self.kind, self.value)


def tokenize(sql):
    out = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\n\r\f\v":
            i += 1
        elif c == "-" and sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
        elif c == "/" and sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c.isalpha() or c == "_" or ord(c) > 127:
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_" or sql[j] == "$" or ord(sql[j]) > 127):
                j += 1
            word = sql[i:j]
            if word in ("x", "X") and j < n and sql[j] == "'":
                k = sql.find("'", j + 1)
                if k < 0:
                    raise SQLSyntaxError("unrecognized token: %s" % sql[i:])
                digits = sql[j + 1:k]
                try:
                    out.append(Token(BLOB, bytes.fromhex(digits), i, k + 1))
                except ValueError:
                    raise SQLSyntaxError("unrecognized token: %s" % sql[i:k + 1]) from None
                i = k + 1
            else:
                out.append(Token(ID, word, i, j))
                i = j
        elif c.isdigit() or (c == "." and i + 1 < n and sql[i + 1].isdigit()):
            i = _number(sql, i, out)
        elif c == "'":
            j = i + 1
            parts = []
            while True:
                k = sql.find("'", j)
                if k < 0:
                    raise SQLSyntaxError("unrecognized token: %s" % sql[i:])
                parts.append(sql[j:k])
                if sql.startswith("''", k):
                    parts.append("'")
                    j = k + 2
                else:
                    j = k + 1
                    break
            out.append(Token(STR, "".join(parts), i, j))
            i = j
        elif c in '"`[':
            close = {'"': '"', "`": "`", "[": "]"}[c]
            j = i + 1
            parts = []
            while True:
                k = sql.find(close, j)
                if k < 0:
                    raise SQLSyntaxError("unrecognized token: %s" % sql[i:])
                parts.append(sql[j:k])
                if close != "]" and sql.startswith(close * 2, k):
                    parts.append(close)
                    j = k + 2
                else:
                    j = k + 1
                    break
            out.append(Token(QID, "".join(parts), i, j))
            i = j
        elif c == "?":
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            out.append(Token(PARAM, int(sql[i + 1:j]) if j > i + 1 else None, i, j))
            i = j
        elif c in ":@$":
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j == i + 1:
                raise SQLSyntaxError('unrecognized token: "%s"' % c)
            out.append(Token(PARAM, sql[i + 1:j], i, j))
            i = j
        elif sql[i:i + 2] in TWO_CHAR:
            out.append(Token(OP, sql[i:i + 2], i, i + 2))
            i += 2
        elif c in ONE_CHAR:
            out.append(Token(OP, c, i, i + 1))
            i += 1
        else:
            raise SQLSyntaxError('unrecognized token: "%s"' % c)
    out.append(Token(EOF, None, n, n))
    return out


def _number(sql, i, out):
    n = len(sql)
    j = i
    if sql.startswith(("0x", "0X"), i):
        j = i + 2
        while j < n and sql[j] in "0123456789abcdefABCDEF":
            j += 1
        if j == i + 2:
            raise SQLSyntaxError("unrecognized token: %s" % sql[i:j])
        v = int(sql[i + 2:j], 16)
        v = v - (1 << 64) if v >= (1 << 63) else v
        out.append(Token(INT, v, i, j))
        return j
    while j < n and sql[j].isdigit():
        j += 1
    is_float = False
    if j < n and sql[j] == ".":
        is_float = True
        j += 1
        while j < n and sql[j].isdigit():
            j += 1
    if j < n and sql[j] in "eE":
        k = j + 1
        if k < n and sql[k] in "+-":
            k += 1
        if k < n and sql[k].isdigit():
            while k < n and sql[k].isdigit():
                k += 1
            is_float, j = True, k
    if j < n and (sql[j].isalpha() or sql[j] == "_"):
        raise SQLSyntaxError("unrecognized token: %s" % sql[i:j + 1])
    text = sql[i:j]
    if is_float:
        out.append(Token(FLOAT, float(text), i, j))
    else:
        v = int(text)
        out.append(Token(INT if v <= (1 << 63) else FLOAT, v if v <= (1 << 63) else float(text), i, j))    # 2**63 is kept: it may be negated to the smallest integer
    return j
