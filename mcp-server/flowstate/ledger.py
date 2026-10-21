"""The run ledger (2.4.0): per automatic session, what it read and wrote. Record only;
there is no tool for it (2.5's run undo will build on it).

A run = one automatic session (one connection's work on one project): its start and end
are the session's own (auto_sessions), its app / machine are in session_runs (migration
015), its writes in run_writes (016, receipts.py), its reads here in run_views (017). Stored
lean: record ids and hashes only, never the text, so the ledger holds nothing the records
themselves don't (and nothing secret).
"""

import hashlib
import json
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import activity


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='run_views'").fetchone() is not None


def answer_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def record_read(db_path: Path, tool: str, action: Optional[str], meta: dict, answer: str,
                conn_key: Optional[str], ident) -> None:
    """One row for a successful read of a project inside a session."""
    pid, sid = meta.get("project_id"), meta.get("session_id")
    if pid is None or sid is None:
        return
    who = ident.as_dict() if ident is not None else {}
    shown = meta.get("shown")
    conn = _conn(db_path)
    try:
        if not available(conn):
            return
        conn.execute(
            """INSERT INTO run_views (session_id, conn_key, project_id, app, machine, tool,
               action, at, answer_hash, records) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (sid, conn_key, pid, who.get("app"), who.get("machine"), tool[:60],
             (action or None) and str(action)[:40], _now(), answer_hash(answer),
             json.dumps(shown[:200]) if shown is not None else None))
        if random.random() < 0.01:   # prune now and then, like the activity log
            cutoff = (datetime.now(timezone.utc) - timedelta(days=activity.keep_days())).strftime(
                "%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM run_views WHERE at < ?", (cutoff,))
            conn.execute("DELETE FROM run_writes WHERE finished_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


def run(db_path: Path, session_id: int) -> Optional[dict]:
    """Everything the ledger has on one session (for tests, the review page and 2.5)."""
    conn = _conn(db_path)
    try:
        if not available(conn):
            return None
        head = conn.execute(
            """SELECT a.session_id, a.project_id, a.client, a.opened_at, a.closed_at,
                      r.conn_key, r.app, r.model, r.machine, r.identity_source
               FROM auto_sessions a LEFT JOIN session_runs r USING (session_id)
               WHERE a.session_id = ?""", (session_id,)).fetchone()
        if head is None:
            return None
        reads = [dict(r) for r in conn.execute(
            "SELECT tool, action, at, answer_hash, records FROM run_views WHERE session_id = ? "
            "ORDER BY id", (session_id,))]
        for r in reads:
            r["records"] = json.loads(r["records"]) if r["records"] else None
        writes = [dict(r) for r in conn.execute(
            "SELECT change_id, tool, action, status, records, started_at, finished_at "
            "FROM run_writes WHERE session_id = ? ORDER BY id", (session_id,))]
        for w in writes:
            w["records"] = json.loads(w["records"] or "[]")
        return {**dict(head), "reads": reads, "writes": writes}
    finally:
        conn.close()
