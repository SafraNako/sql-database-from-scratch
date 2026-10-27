"""``ksql``: a command line for the database, in the spirit of the ``sqlite3`` shell."""
from __future__ import annotations

import sys

from . import __version__
from .connection import connect
from .errors import DatabaseError
from .functions import f_quote
from .parser import parse_script

HELP = """.tables             list the tables
.schema [NAME]      show the CREATE statements (of one table)
.indexes [TABLE]    list the indexes
.dump               print the database as SQL
.headers on|off     show the names of the columns
.mode list|csv|line how rows are printed
.integrity          run the integrity check
.read FILE          run the SQL in a file
.quit               leave"""


def show(v, mode):
    if v is None:
        return "" if mode == "list" else ""
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    if isinstance(v, float):
        return repr(v)
    return str(v)


def csv_field(v):
    s = show(v, "csv")
    if any(c in s for c in ',"\n'):
        return '"' + s.replace('"', '""') + '"'
    return s


class Shell:
    def __init__(self, con, out=None):
        self.con = con
        self.out = out or sys.stdout
        self.headers = False
        self.mode = "list"

    def print_rows(self, cur):
        rows = cur.fetchall()
        names = [d[0] for d in cur.description] if cur.description else []
        if not names:
            return
        if self.mode == "line":
            for row in rows:
                width = max(len(n) for n in names)
                for n, v in zip(names, row):
                    self.out.write("%s = %s\n" % (n.rjust(width), show(v, "line")))
                self.out.write("\n")
            return
        if self.headers:
            self.out.write(("," if self.mode == "csv" else "|").join(names) + "\n")
        for row in rows:
            if self.mode == "csv":
                self.out.write(",".join(csv_field(v) for v in row) + "\n")
            else:
                self.out.write("|".join(show(v, "list") for v in row) + "\n")

    def run_sql(self, sql):
        try:
            for parsed in parse_script(sql):
                cur = self.con.cursor()
                cur._finish(self.con._db.execute(parsed, ()))
                self.print_rows(cur)
            return True
        except DatabaseError as exc:
            self.out.write("Error: %s\n" % exc)
            return False

    def dot(self, line):
        parts = line.split()
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else None
        if cmd in (".quit", ".exit"):
            return False
        if cmd == ".help":
            self.out.write(HELP + "\n")
        elif cmd == ".tables":
            rows = self.con.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
            self.out.write("  ".join(r[0] for r in rows) + "\n")
        elif cmd == ".schema":
            q = "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL" + (" AND tbl_name = ?" if arg else "") + " ORDER BY rowid"
            for r in self.con.execute(q, (arg,) if arg else ()).fetchall():
                self.out.write(r[0] + ";\n")
        elif cmd == ".indexes":
            q = "SELECT name FROM sqlite_master WHERE type = 'index'" + (" AND tbl_name = ?" if arg else "") + " ORDER BY name"
            self.out.write("  ".join(r[0] for r in self.con.execute(q, (arg,) if arg else ()).fetchall()) + "\n")
        elif cmd == ".headers":
            self.headers = (arg or "").lower() in ("on", "1", "yes")
        elif cmd == ".mode":
            if arg not in ("list", "csv", "line"):
                self.out.write("unknown mode: %s\n" % arg)
            else:
                self.mode = arg
        elif cmd == ".dump":
            self.dump()
        elif cmd == ".integrity":
            self.run_sql("PRAGMA integrity_check")
        elif cmd == ".read":
            try:
                with open(arg, encoding="utf-8") as f:
                    self.run_sql(f.read())
            except OSError as exc:
                self.out.write("cannot read %s: %s\n" % (arg, exc.strerror))
        else:
            self.out.write("unknown command: %s (try .help)\n" % cmd)
        return True

    def dump(self):
        self.out.write("BEGIN TRANSACTION;\n")
        masters = self.con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY rowid").fetchall()
        for kind, name, tbl, sql in masters:
            if kind != "table":
                continue
            self.out.write(sql + ";\n")
            cur = self.con.execute("SELECT * FROM %s" % name)
            for row in cur.fetchall():
                self.out.write("INSERT INTO %s VALUES(%s);\n" % (name, ",".join(f_quote(v) for v in row)))
        for kind, name, tbl, sql in masters:
            if kind == "index":
                self.out.write(sql + ";\n")
        self.out.write("COMMIT;\n")

    def loop(self, stdin, interactive):
        buffer = ""
        while True:
            if interactive:
                self.out.write("ksql> " if not buffer else "   ...> ")
                self.out.flush()
            line = stdin.readline()
            if not line:
                break
            stripped = line.strip()
            if not buffer and stripped.startswith("."):
                if not self.dot(stripped):
                    break
                continue
            buffer += line
            if self.complete(buffer):
                self.run_sql(buffer)
                buffer = ""
        if buffer.strip():
            self.run_sql(buffer)

    @staticmethod
    def complete(text):
        """Does the text end with a ``;`` that is not inside a string or a comment?"""
        from .lexer import tokenize
        from .errors import SQLSyntaxError
        try:
            toks = tokenize(text)
        except SQLSyntaxError:
            return False
        return len(toks) > 1 and toks[-2].kind == "op" and toks[-2].value == ";"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write("usage: ksql DATABASE [SQL]\n\n" + HELP + "\n")
        return 0
    if argv and argv[0] == "--version":
        sys.stdout.write("ksql %s\n" % __version__)
        return 0
    if not argv:
        sys.stderr.write("usage: ksql DATABASE [SQL]\n")
        return 2
    try:
        con = connect(argv[0])
    except DatabaseError as exc:
        sys.stderr.write("ksql: %s\n" % exc)
        return 1
    shell = Shell(con)
    try:
        if len(argv) > 1:
            ok = True
            for sql in argv[1:]:
                ok = shell.run_sql(sql) and ok
            return 0 if ok else 1
        interactive = sys.stdin.isatty()
        if interactive:
            sys.stdout.write("kendi_db %s - .help for help, .quit to leave\n" % __version__)
        shell.loop(sys.stdin, interactive)
        return 0
    finally:
        con.close()


if __name__ == "__main__":                                              # pragma: no cover
    sys.exit(main())
