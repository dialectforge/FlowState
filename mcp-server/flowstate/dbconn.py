"""One commit per save (2.4.0). Standard library only.

A save (one write call: the record, its edit history, its receipt and run-ledger row, the
session it belongs to and its activity-log row) used to commit five or six times, once per
module, and each commit waits for the disk. Now `save(db_path)` makes them one transaction:
every FlowState connection to that database opened in this thread while the save runs
(`connect()`, which every module uses instead of sqlite3.connect) is the save's own
connection, and the save commits once at the end, or not at all.

Each module still sees a connection of its own: what it does between opening it and its
commit() is a savepoint of the save. Its rollback(), or close() without commit(), undoes
only its own part, as before. Its explicit BEGIN / COMMIT / ROLLBACK become that savepoint's
start / release / rollback. Its commit() only releases the savepoint: nothing is on disk
until the save commits.

The lock: nothing is locked until the save's first write, which takes the write lock
(BEGIN IMMEDIATE, so two saves never deadlock); reads before it run as before. The lock is
released at the save's commit or rollback. Readers elsewhere keep reading the last commit.
Nothing slow (network, git, waiting for the user, safety copies) may run inside a save; the
caller keeps those out (server.py: _one_commit).

Journal mode and synchronous settings are untouched (data-git commits the .db file alone).
"""

import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional

TIMEOUT = 10.0     # seconds a save waits for another writer's lock (the slowest module's wait)

_local = threading.local()


class SaveBroken(sqlite3.OperationalError):
    """The save can no longer be committed as one piece (SQLite ended its transaction, or a
    module's part was already merged or undone); it is rolled back as a whole."""


def _key(db_path) -> str:
    return os.path.normcase(os.path.abspath(str(db_path)))


def current() -> Optional["Save"]:
    return getattr(_local, "save", None)


def connect(db_path, timeout: float = 5.0, isolation_level: Optional[str] = "",
            **kw) -> sqlite3.Connection:
    """sqlite3.connect for FlowState's database. Inside a save on the same file (this
    thread): a view of the save's connection (see the module docstring)."""
    s = current()
    if s is not None and s.key == _key(db_path) and not kw:
        return s.join(isolation_level is None)
    return sqlite3.connect(str(db_path), timeout=timeout, isolation_level=isolation_level, **kw)


def after(db_path, done) -> None:
    """Call done(committed) when this thread's save on db_path ends (True: committed),
    or at once with True outside a save: for what a module keeps in memory about a write,
    which must not outlive a save that was rolled back."""
    s = current()
    if s is not None and s.key == _key(db_path):
        s.after.append(done)
    else:
        done(True)


@contextmanager
def save(db_path):
    """Run the block as one save: committed once at the end, rolled back whole if the
    block raises. A save inside a save of the same database joins the outer one."""
    outer = current()
    if outer is not None and outer.key == _key(db_path):
        yield outer
        return
    s = Save(db_path)
    _local.save = s
    committed = False
    try:
        yield s
        s.commit()
        committed = True
    except BaseException:
        s.rollback()
        raise
    finally:
        _local.save = outer
        s.close()
        for done in s.after:
            done(committed)


# ------------------------------------------------------------------ statements
_COMMENT = re.compile(r"^\s*(?:(?:--[^\n]*\n)|(?:/\*.*?\*/))*\s*", re.S)
_WRITES_IN_WITH = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_READS = ("SELECT", "PRAGMA", "EXPLAIN", "VALUES")


def _words(sql: str) -> list:
    return _COMMENT.sub("", sql, count=1).split(None, 4)


def _is_write(words: list, sql: str) -> bool:
    first = words[0].upper() if words else ""
    if first == "WITH":
        return bool(_WRITES_IN_WITH.search(sql))
    return first not in _READS and first != ""


def _control(words: list) -> Optional[str]:
    """'begin', 'commit' or 'rollback' for a whole-transaction statement, else None."""
    up = [w.upper().rstrip(";") for w in words[:3]]
    if not up:
        return None
    if up[0] == "BEGIN":
        return "begin"
    if up[0] in ("COMMIT", "END"):
        return "commit"
    if up[0] == "ROLLBACK" and "TO" not in up[1:3]:
        return "rollback"
    return None


# ------------------------------------------------------------------ the save
class Save:
    def __init__(self, db_path):
        self.path, self.key = str(db_path), _key(db_path)
        self.conn: Optional[sqlite3.Connection] = None
        self.begun = False
        self.stack: list = []          # open savepoints, outermost first
        self.ended: dict = {}          # savepoint -> "merged" | "undone" once gone
        self.n = 0
        self.after: list = []          # after(): called once the save has ended

    def join(self, autocommit: bool) -> "Joined":
        if self.conn is None:
            c = sqlite3.connect(self.path, timeout=TIMEOUT, isolation_level=None)
            c.execute(f"PRAGMA busy_timeout = {int(TIMEOUT * 1000)}")
            c.execute("PRAGMA foreign_keys = ON")
            self.conn = c
        return Joined(self, autocommit)

    def ensure_begun(self) -> None:
        if not self.begun:
            self.conn.execute("BEGIN IMMEDIATE")
            self.begun = True
        elif not self.conn.in_transaction:
            raise SaveBroken("the save's transaction was ended by SQLite; nothing was saved")

    def open_savepoint(self) -> str:
        self.ensure_begun()
        self.n += 1
        name = f"fs_part_{self.n}"
        self.conn.execute(f"SAVEPOINT {name}")
        self.stack.append(name)
        return name

    def _end(self, name: str, how: str) -> None:
        if name not in self.stack:
            if self.ended.get(name) == how:
                return            # an outer part already did the same to it
            raise SaveBroken(f"part of this save was already {self.ended.get(name, 'ended')}")
        if not self.conn.in_transaction:
            raise SaveBroken("the save's transaction was ended by SQLite; nothing was saved")
        i = self.stack.index(name)
        if how == "merged":
            self.conn.execute(f"RELEASE {name}")
        else:
            self.conn.execute(f"ROLLBACK TO {name}")
            self.conn.execute(f"RELEASE {name}")
        for inner in self.stack[i:]:
            self.ended[inner] = how
        del self.stack[i:]

    def release(self, name: str) -> None:
        self._end(name, "merged")

    def undo(self, name: str) -> None:
        self._end(name, "undone")

    def commit(self) -> None:
        if self.conn is None or not self.begun:
            return
        if self.stack:   # a part neither committed nor closed: sqlite3 would drop it
            raise SaveBroken("part of this save was left open; nothing was saved")
        if not self.conn.in_transaction:
            raise SaveBroken("the save's transaction was ended by SQLite; nothing was saved")
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if self.conn is not None and self.conn.in_transaction:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


class Joined:
    """What a module gets from connect() inside a save: behaves like its own connection."""

    def __init__(self, save: Save, autocommit: bool):
        self._save, self._autocommit = save, autocommit
        self._sp: Optional[str] = None     # this module's open part (savepoint)
        self._closed = False
        self.row_factory = None

    # -- statements
    def _run(self, method: str, sql: str, params):
        if self._closed:
            raise sqlite3.ProgrammingError("Cannot operate on a closed database.")
        words = _words(sql)
        ctl = _control(words)
        if ctl == "begin":
            if self._sp is None:
                self._sp = self._save.open_savepoint()
            return self._cursor()
        if ctl == "commit":
            self.commit()
            return self._cursor()
        if ctl == "rollback":
            self.rollback()
            return self._cursor()
        if _is_write(words, sql):
            if self._sp is None and not self._autocommit:
                self._sp = self._save.open_savepoint()   # what an implicit BEGIN was
            else:
                self._save.ensure_begun()
        cur = self._cursor()
        return getattr(cur, method)(sql, params)

    def _cursor(self) -> sqlite3.Cursor:
        cur = self._save.conn.cursor()
        cur.row_factory = self.row_factory
        return cur

    def execute(self, sql: str, params=()):
        return self._run("execute", sql, params)

    def executemany(self, sql: str, seq):
        return self._run("executemany", sql, seq)

    def executescript(self, script: str):
        buf = ""
        for line in script.splitlines(keepends=True):
            buf += line
            if sqlite3.complete_statement(buf):
                self.execute(buf.strip())
                buf = ""
        if buf.strip():
            self.execute(buf.strip())
        return self._cursor()

    def cursor(self) -> sqlite3.Cursor:
        raise NotImplementedError("use execute() on a FlowState connection inside a save")

    # -- transactions
    @property
    def in_transaction(self) -> bool:
        return self._sp is not None

    def commit(self) -> None:
        if self._sp is not None:
            sp, self._sp = self._sp, None
            self._save.release(sp)

    def rollback(self) -> None:
        if self._sp is not None:
            sp, self._sp = self._sp, None
            self._save.undo(sp)

    def close(self) -> None:
        if not self._closed:
            try:
                self.rollback()   # like sqlite3: closing without commit() drops the changes
            finally:
                self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    @property
    def total_changes(self) -> int:
        return self._save.conn.total_changes

    def __getattr__(self, name):   # anything else (enable_load_extension, ...) as is
        return getattr(self._save.conn, name)


def is_joined(conn) -> bool:
    return isinstance(conn, Joined)


__all__ = ["connect", "save", "after", "current", "SaveBroken", "is_joined"]
