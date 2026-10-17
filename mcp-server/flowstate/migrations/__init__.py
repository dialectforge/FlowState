"""Numbered, additive schema migrations. Standard library only (setup_flowstate.py
imports this before any package is installed).

Each `NNN_name.sql` file in this folder runs once, inside one transaction, and is
recorded in `schema_migrations`. Rules for new files: add tables, indexes or rows only;
never DROP or rewrite existing tables (the v1 server shares the database). The one ALTER
allowed is `ADD COLUMN ... DEFAULT NULL` on FlowState's own v2 tables (012), which leaves
every existing row and every older reader as it was.
"""

import re
import sqlite3
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
_NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")

VERSION_DDL = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)"""


def available() -> list[tuple[int, str, Path]]:
    out = []
    for p in sorted(HERE.glob("*.sql")):
        m = _NAME.match(p.name)
        if m:
            out.append((int(m.group(1)), m.group(2), p))
    return out


def latest_version() -> int:
    return max((v for v, _n, _p in available()), default=0)


def _statements(sql: str) -> list[str]:
    """Split a script into complete statements (handles triggers and quoted ';')."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt.strip(";").strip() and not all(
                    l.strip().startswith("--") or not l.strip() for l in stmt.splitlines()):
                out.append(stmt)
            buf = ""
    if buf.strip() and not all(l.strip().startswith("--") or not l.strip()
                               for l in buf.splitlines()):
        raise ValueError("migration ends with an incomplete statement")
    return out


_ADD_COLUMN = re.compile(r"^\s*ALTER\s+TABLE\s+\"?(\w+)\"?\s+ADD\s+COLUMN\s+\"?(\w+)\"?", re.I)


def _column_exists(conn, stmt: str) -> bool:
    """True for an `ALTER TABLE t ADD COLUMN c` whose column is already there, so such a
    migration can run again (like CREATE ... IF NOT EXISTS) on a database that lost its
    schema_migrations rows."""
    m = _ADD_COLUMN.match("\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")))
    if not m:
        return False
    return m.group(2) in {r[1] for r in conn.execute(f'PRAGMA table_info("{m.group(1)}")')}


def applied(db_path: Path) -> set[int]:
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='schema_migrations'").fetchone():
            return set()
        return {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    finally:
        conn.close()


def pending(db_path: Path) -> list[tuple[int, str, Path]]:
    done = applied(db_path)
    return [m for m in available() if m[0] not in done]


def _has_data(db_path: Path) -> bool:
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='projects'").fetchone():
            return False
        return conn.execute("SELECT 1 FROM projects LIMIT 1").fetchone() is not None
    finally:
        conn.close()


def migrate(db_path: Path, backup_dir: Optional[Path] = None, backup_first: bool = True,
            log=None) -> dict:
    """Apply pending migrations. Returns {"applied": [...], "backup": path|None}.

    When the database already holds data, an integrity-checked online backup is taken
    first; if that backup fails, nothing is migrated and the error is raised."""
    db_path = Path(db_path)
    todo = pending(db_path)
    result = {"applied": [], "backup": None}
    if not todo:
        return result
    if backup_first and _has_data(db_path):
        from ..backup import backup_now  # stdlib only as well
        b = backup_now(db_path, backup_dir, prune=False, label="pre-migration")
        result["backup"] = str(b["path"])
        if log:
            log(f"[migrate] backup before migrating: {b['path']}")
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(VERSION_DDL)
        for version, name, path in todo:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another process may have applied it while we waited for the lock.
                if conn.execute("SELECT 1 FROM schema_migrations WHERE version = ?",
                                (version,)).fetchone():
                    conn.execute("ROLLBACK")
                    continue
                for stmt in _statements(path.read_text(encoding="utf-8")):
                    if not _column_exists(conn, stmt):
                        conn.execute(stmt)
                conn.execute("INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                             (version, name))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            result["applied"].append(f"{version:03d}_{name}")
            if log:
                log(f"[migrate] applied {version:03d}_{name}")
    finally:
        conn.close()
    return result
