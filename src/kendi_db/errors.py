"""The exceptions of the database."""
from __future__ import annotations


class DatabaseError(Exception):
    """Anything that goes wrong inside the engine (SQL mistakes, constraint violations, a damaged file)."""


class SQLSyntaxError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    """No such table, no such column, a locked or unreadable file: things that are not a mistake in the SQL text itself."""


class IntegrityError(DatabaseError):
    """A constraint (NOT NULL, UNIQUE, PRIMARY KEY) would be violated."""


class CorruptDatabase(DatabaseError):
    """The file is not a database this engine can read, or it is damaged."""


class SimulatedCrash(BaseException):
    """Raised by the crash hooks of the tests to stop a commit half way, as a power failure would."""
