"""kendi_db: a SQL database engine written from scratch, whose files are SQLite database files."""
from .connection import Connection, Cursor, connect
from .errors import CorruptDatabase, DatabaseError, IntegrityError, OperationalError, SQLSyntaxError

Error = DatabaseError
__version__ = "0.1.0"
__all__ = ["connect", "Connection", "Cursor", "Error", "DatabaseError", "IntegrityError", "OperationalError", "SQLSyntaxError", "CorruptDatabase"]
