"""``ksql``, the command line: run for real as a subprocess, with SQLite's own shell module (``sqlite3``) reading what it wrote."""
import io
import os
import sqlite3
import subprocess
import sys

import pytest

import kendi_db
from kendi_db.shell import Shell

ENV = dict(os.environ, PYTHONPATH=os.path.join(os.path.dirname(__file__), "..", "src"))


def ksql(*args, stdin=None):
    return subprocess.run([sys.executable, "-m", "kendi_db", *args], input=stdin, capture_output=True, text=True, env=ENV, timeout=60)


def test_a_statement_on_the_command_line(tmp_path):
    db = str(tmp_path / "x.db")
    r = ksql(db, "CREATE TABLE t (a, b); INSERT INTO t VALUES (1, 'one'), (2, 'two'); SELECT * FROM t ORDER BY a;")
    assert r.returncode == 0 and r.stdout == "1|one\n2|two\n" and r.stderr == ""
    assert sqlite3.connect(db).execute("SELECT * FROM t ORDER BY a").fetchall() == [(1, "one"), (2, "two")]


def test_an_error_gives_a_message_and_a_failing_exit_status(tmp_path):
    db = str(tmp_path / "x.db")
    r = ksql(db, "SELECT * FROM nothing")
    assert r.returncode == 1 and "Error: no such table: nothing" in r.stdout
    r = ksql(db, "SELEC 1")
    assert r.returncode == 1 and "Error:" in r.stdout


def test_statements_from_standard_input_and_the_dot_commands(tmp_path):
    db = str(tmp_path / "x.db")
    script = """
CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);
CREATE INDEX people_name ON people (name);
INSERT INTO people (name) VALUES ('Ada'),
  ('Grace');
.headers on
SELECT * FROM people;
.mode csv
SELECT name, 'a,b', 'say "hi"' FROM people WHERE id = 1;
.mode list
.tables
.indexes people
.schema people
.integrity
SELECT 'semicolon ; inside a string';
.quit
SELECT 'never runs';
"""
    r = ksql(db, stdin=script)
    assert r.returncode == 0
    out = r.stdout
    assert "id|name\n1|Ada\n2|Grace\n" in out
    assert 'Ada,"a,b","say ""hi"""\n' in out
    assert "people\n" in out and "people_name\n" in out
    assert "CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);\n" in out and "CREATE INDEX people_name ON people (name);\n" in out
    assert "ok\n" in out and "semicolon ; inside a string\n" in out and "never runs" not in out


def test_dump_can_be_loaded_into_sqlite_and_back(tmp_path):
    db = str(tmp_path / "x.db")
    ksql(db, "CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT, c REAL, d BLOB); CREATE INDEX t_b ON t (b);"
             "INSERT INTO t VALUES (1, 'it''s', 2.5, x'00ff'), (2, NULL, NULL, NULL), (3, 'line' || char(10) || 'two', -0.5, x'');")
    dump = ksql(db, stdin=".dump\n").stdout
    assert dump.startswith("BEGIN TRANSACTION;\n") and dump.endswith("COMMIT;\n")
    ref = sqlite3.connect(":memory:")
    ref.executescript(dump)
    assert ref.execute("SELECT * FROM t ORDER BY a").fetchall() == [(1, "it's", 2.5, b"\x00\xff"), (2, None, None, None), (3, "line\ntwo", -0.5, b"")]
    copy = str(tmp_path / "copy.db")
    ksql(copy, stdin=dump)
    assert sqlite3.connect(copy).execute("SELECT * FROM t ORDER BY a").fetchall() == ref.execute("SELECT * FROM t ORDER BY a").fetchall()


def test_read_runs_a_file_of_sql(tmp_path):
    script = tmp_path / "s.sql"
    script.write_text("CREATE TABLE t (a);\nINSERT INTO t VALUES (7);\nSELECT a * 6 FROM t;\n")
    r = ksql(str(tmp_path / "x.db"), stdin=".read %s\n" % script)
    assert r.stdout == "42\n"
    r = ksql(str(tmp_path / "x.db"), stdin=".read %s\n" % (tmp_path / "missing.sql"))
    assert "cannot read" in r.stdout


def test_line_mode_and_unknown_commands(tmp_path):
    r = ksql(str(tmp_path / "x.db"), stdin="CREATE TABLE t (alpha, b); INSERT INTO t VALUES (1, 'x');\n.mode line\nSELECT * FROM t;\n.mode weird\n.nosuch\n.help\n")
    assert "alpha = 1\n" in r.stdout and "    b = x\n" in r.stdout
    assert "unknown mode: weird" in r.stdout and "unknown command: .nosuch" in r.stdout and ".tables" in r.stdout


def test_usage_and_version():
    assert ksql("--version").stdout.startswith("ksql ")
    assert ksql("--help").stdout.startswith("usage: ksql")
    r = ksql()
    assert r.returncode == 2 and "usage" in r.stderr


def test_a_file_that_is_not_a_database(tmp_path):
    junk = tmp_path / "junk"
    junk.write_bytes(b"this is not a database" * 100)
    r = ksql(str(junk), "SELECT 1 FROM sqlite_master")
    assert r.returncode == 1


@pytest.mark.parametrize("sql", ["SELECT 1; SELECT 2;", "SELECT 'a;b';", "SELECT 1 -- comment ;\n;", "/* ; */ SELECT 3;"])
def test_complete_knows_where_a_statement_ends(sql):
    assert Shell.complete(sql)
    assert not Shell.complete("SELECT 1")
    assert not Shell.complete("SELECT 'open string;")


def test_the_shell_class_writes_to_any_stream(tmp_path):
    con = kendi_db.connect(str(tmp_path / "x.db"))
    out = io.StringIO()
    shell = Shell(con, out)
    shell.loop(io.StringIO("CREATE TABLE t (a, b);\nINSERT INTO t VALUES (1.5, NULL), (2, 'x');\nSELECT * FROM t;\n"), False)
    assert out.getvalue() == "1.5|\n2|x\n"
    con.close()
