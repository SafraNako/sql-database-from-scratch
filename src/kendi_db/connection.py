"""The Python interface, shaped like the standard ``sqlite3`` module: ``connect``, ``Connection.execute``, ``Cursor.fetchall`` ..."""
from __future__ import annotations

from .database import Database
from .parser import parse_script, parse_statement


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1
        self._rows = []
        self._pos = 0

    def execute(self, sql, params=()):
        parsed = parse_statement(sql)
        self._finish(self.connection._db.execute(parsed, params))
        return self

    def executemany(self, sql, seq):
        parsed = parse_statement(sql)
        total = 0
        db = self.connection._db
        for params in seq:
            r = db.execute(parsed, params)
            total += max(r.rowcount, 0)
        self._rows, self._pos = [], 0
        self.description = None
        self.rowcount = total
        return self

    def executescript(self, script):
        for parsed in parse_script(script):
            self._finish(self.connection._db.execute(parsed, ()))
        return self

    def _finish(self, result):
        self._rows, self._pos = result.rows, 0
        self.description = [(n, None, None, None, None, None, None) for n in result.names] if result.names else None
        self.rowcount = result.rowcount
        self.lastrowid = result.lastrowid

    def fetchone(self):
        if self._pos < len(self._rows):
            row = self._rows[self._pos]
            self._pos += 1
            return row
        return None

    def fetchmany(self, size=None):
        size = size or self.arraysize
        out = self._rows[self._pos:self._pos + size]
        self._pos += len(out)
        return out

    def fetchall(self):
        out = self._rows[self._pos:]
        self._pos = len(self._rows)
        return out

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row

    def close(self):
        pass


class Connection:
    def __init__(self, path, page_size=4096):
        self._db = Database(path, page_size)

    def cursor(self):
        return Cursor(self)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def commit(self):
        if self._db.in_user_txn:
            self._db.pager.commit()
            self._db.in_user_txn = False

    def rollback(self):
        if self._db.in_user_txn:
            self._db.pager.rollback()
            self._db.in_user_txn = False
            self._db.schema = None

    @property
    def in_transaction(self):
        return self._db.in_user_txn

    @property
    def total_changes(self):
        return self._db.total_changes

    def close(self):
        if self._db.in_user_txn:
            self.rollback()
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


def connect(path, page_size=4096):
    return Connection(path, page_size)
