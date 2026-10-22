"""Which app did what, and a pause switch per app (v2 Phase D, migration 006).

Every tool call adds one row: time, app, tool, action, project id, ok or the error code,
and (migration 012) the model, machine and identity source (identity.py) plus the type and
id of a record the call created. Never the arguments or any text, so nothing secret can
land here. Rows older than
FLOWSTATE_ACTIVITY_DAYS (default 90) are pruned now and then.

The pause switch belongs to the user: it is set from the command line or the review page,
never through an MCP tool. A paused app can still read; its writes get "paused by the user".
"""

import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
from . import identity


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='activity_log'").fetchone() is not None


def keep_days() -> int:
    try:
        return max(1, int(os.environ.get("FLOWSTATE_ACTIVITY_DAYS", "90")))
    except ValueError:
        return 90


def record(db_path: Path, app: str, tool: str, action: Optional[str], project_id: Optional[int],
           write: bool, ok: bool, error: Optional[str] = None, ms: Optional[int] = None,
           caller: Optional[str] = None, record: Optional[tuple] = None) -> None:
    """One row per call, with the caller's model / machine / identity source (migration
    012) and, for a write that created a record, its (record_type, id)."""
    conn = _conn(db_path)
    try:
        if not available(conn):
            return
        row = {"at": _now(), "app": (app or "unknown")[:80],
               "caller": (caller or "")[:80] or None, "tool": tool[:60],
               "action": (action or None) and str(action)[:40], "project_id": project_id,
               "write": int(bool(write)), "ok": int(bool(ok)),
               "error": (error or None) and str(error)[:60], "ms": ms}
        if record and identity.has_column(conn, "activity_log", "record_type"):
            row["record_type"], row["record_id"] = str(record[0])[:20], int(record[1])
        identity.insert(conn, "activity_log", row, app)
        if random.random() < 0.01:   # prune now and then
            cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days())).strftime(
                "%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM activity_log WHERE at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def is_paused(db_path: Path, app: str) -> bool:
    conn = _conn(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='app_controls'").fetchone():
            return False
        row = conn.execute("SELECT paused FROM app_controls WHERE app = ?",
                           ((app or "unknown")[:80],)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


def set_paused(db_path: Path, app: str, paused: bool, by: str) -> dict:
    app = (app or "").strip()
    if not app:
        raise ValueError("name the app (as shown by `flowstate apps list`)")
    conn = _conn(db_path)
    try:
        conn.execute("""INSERT INTO app_controls (app, paused, changed_at, changed_by)
                        VALUES (?, ?, ?, ?) ON CONFLICT(app) DO UPDATE SET paused = excluded.paused,
                        changed_at = excluded.changed_at, changed_by = excluded.changed_by""",
                     (app[:80], int(paused), _now(), by[:40]))
        conn.commit()
        return {"app": app, "paused": paused}
    finally:
        conn.close()


def apps(db_path: Path) -> list[dict]:
    """Every app seen (or paused), with last use and counts."""
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        rows = {r["app"]: dict(r) for r in conn.execute(
            """SELECT app, MAX(at) last_seen, COUNT(*) calls, SUM(write) writes,
                      SUM(CASE WHEN ok = 0 THEN 1 ELSE 0 END) errors
               FROM activity_log GROUP BY app""")}
        for r in conn.execute("SELECT app, paused, changed_at FROM app_controls"):
            rows.setdefault(r["app"], {"app": r["app"], "last_seen": None, "calls": 0,
                                       "writes": 0, "errors": 0})
            rows[r["app"]]["paused"] = bool(r["paused"])
            rows[r["app"]]["paused_changed_at"] = r["changed_at"]
        for v in rows.values():
            v.setdefault("paused", False)
        return sorted(rows.values(), key=lambda v: v["last_seen"] or "", reverse=True)
    finally:
        conn.close()


def recent(db_path: Path, app: Optional[str] = None, limit: int = 50) -> list[dict]:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        sql = """SELECT a.at, a.app, a.tool, a.action, a.project_id, p.name project, a.write, a.ok,
                        a.error FROM activity_log a LEFT JOIN projects p ON p.id = a.project_id"""
        params: list = []
        if app:
            sql += " WHERE a.app = ?"
            params.append(app)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
