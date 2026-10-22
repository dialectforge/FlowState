"""Implicit sessions: the server opens a FlowState session on a client's first call
for a project and closes it after an idle timeout, on shutdown, or when the agent ends
it explicitly. Works the same for stdio and HTTP; state lives in SQLite so a session
left behind by a crashed process is closed by the next reaper run of any process.

Nothing in here may break a tool call: callers wrap every method in try/except.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn

# Kept identical to the v2 block at the end of database/schema.sql (a test checks it).
AUTO_SESSIONS_DDL = """CREATE TABLE IF NOT EXISTS auto_sessions (
    session_id INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    client TEXT NOT NULL,
    server_instance TEXT NOT NULL,
    opened_at TEXT NOT NULL,      -- UTC, 'YYYY-MM-DD HH:MM:SS'
    last_activity TEXT NOT NULL,  -- UTC
    call_count INTEGER NOT NULL DEFAULT 0,
    events TEXT NOT NULL DEFAULT '[]', -- JSON list of short event labels (capped)
    closed_at TEXT,
    close_reason TEXT             -- idle | shutdown | stale | explicit
);
CREATE INDEX IF NOT EXISTS idx_auto_sessions_open ON auto_sessions(closed_at, client, project_id);"""

AUTO_TAG = "[auto-captured, needs review]"
OFF_TAG = "[auto summary, review off]"
MAX_EVENTS = 60
MAX_SUMMARY = 600
TS_FMT = "%Y-%m-%d %H:%M:%S"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts[:19].replace("T", " "), TS_FMT)


def idle_minutes_from_env() -> float:
    try:
        return float(os.environ.get("FLOWSTATE_IDLE_MINUTES", "120"))
    except ValueError:
        return 120.0


def ensure_schema(db_path: Path) -> None:
    """Additive migration: create auto_sessions if missing. Never alters existing data."""
    conn = dbconn.connect(str(db_path), timeout=5.0)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.executescript(AUTO_SESSIONS_DDL)
        conn.commit()
    finally:
        conn.close()


def _has_runs(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='session_runs'").fetchone() is not None


_OPEN = """SELECT a.* FROM auto_sessions a WHERE a.closed_at IS NULL AND a.client = ?
           AND a.project_id = ?"""
# 2.4.0 (migration 015): one session per connection; conn_key None = the old matching
_BY_CONN = " AND a.session_id IN (SELECT session_id FROM session_runs WHERE conn_key = ?)"
_LATEST = " ORDER BY a.session_id DESC LIMIT 1"


class AutoSessions:
    """Tracks implicit sessions keyed by (client, project, connection). The connection
    (`conn_key`: identity.Identity.session, i.e. the stdio server process or the HTTP MCP
    session) keeps two windows of one app apart; without one (or on a database before
    migration 015) sessions are keyed by (client, project) as before."""

    def __init__(self, db_path: Path, idle_minutes: Optional[float] = None,
                 instance: Optional[str] = None):
        self.db_path = Path(db_path)
        self.idle = timedelta(minutes=idle_minutes if idle_minutes is not None
                              else idle_minutes_from_env())
        self.instance = instance or uuid.uuid4().hex[:12]
        self._last: dict = {}   # conn_key -> the session it touched last (this process)

    def _conn(self) -> sqlite3.Connection:
        conn = dbconn.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---------------------------------------------------------------- touch
    def _find(self, conn, client: str, project_id: int, conn_key: Optional[str]):
        if conn_key and _has_runs(conn):
            return conn.execute(_OPEN + _BY_CONN + _LATEST,
                                (client, project_id, conn_key)).fetchone()
        return conn.execute(_OPEN + _LATEST, (client, project_id)).fetchone()

    def touch(self, client: str, project_id: int, event: Optional[str] = None,
              conn_key: Optional[str] = None, ident=None) -> int:
        """Record activity; open a session if none is open for this connection. Returns
        sessions.id. `ident` (identity.Identity) is recorded with a new session."""
        client = (client or "unknown")[:80]
        conn_key = (conn_key or "")[:80] or None
        now = _utcnow()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = self._find(conn, client, project_id, conn_key)
            if row is not None and now - _parse(row["last_activity"]) > self.idle:
                self._close_row(conn, row, "stale")
                row = None
            if row is None:
                cur = conn.execute(
                    "INSERT INTO sessions (project_id, started_at) VALUES (?, ?)",
                    (project_id, _ts(now)))
                sid = cur.lastrowid
                conn.execute(
                    """INSERT INTO auto_sessions (session_id, project_id, client,
                       server_instance, opened_at, last_activity, call_count, events)
                       VALUES (?, ?, ?, ?, ?, ?, 0, '[]')""",
                    (sid, project_id, client, self.instance, _ts(now), _ts(now)))
                if conn_key and _has_runs(conn):
                    who = ident.as_dict() if ident is not None else {}
                    conn.execute(
                        """INSERT INTO session_runs (session_id, conn_key, app, model, machine,
                           identity_source) VALUES (?, ?, ?, ?, ?, ?)""",
                        (sid, conn_key, who.get("app") or client, who.get("model"),
                         who.get("machine"), who.get("identity_source")))
                events: list = []
                count = 0
            else:
                sid = row["session_id"]
                events = json.loads(row["events"] or "[]")
                count = row["call_count"]
            if event:
                events.append(event[:120])
                events = events[-MAX_EVENTS:]
            conn.execute(
                """UPDATE auto_sessions SET last_activity = ?, call_count = ?, events = ?,
                   server_instance = ? WHERE session_id = ?""",
                (_ts(now), count + 1, json.dumps(events), self.instance, sid))
            conn.commit()
            if conn_key:
                dbconn.after(self.db_path, lambda ok: ok and self._last.__setitem__(conn_key, sid))
            return sid
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def note(self, session_id: int, event: str) -> None:
        """Append an event label to an open session (used after a call succeeds)."""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT events FROM auto_sessions WHERE session_id = ? "
                               "AND closed_at IS NULL", (session_id,)).fetchone()
            if row is not None:
                events = (json.loads(row["events"] or "[]") + [event[:120]])[-MAX_EVENTS:]
                conn.execute("UPDATE auto_sessions SET events = ? WHERE session_id = ?",
                             (json.dumps(events), session_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def current(self, client: str, project_id: int,
                conn_key: Optional[str] = None) -> Optional[dict]:
        conn = self._conn()
        try:
            row = self._find(conn, (client or "unknown")[:80], project_id,
                             (conn_key or "")[:80] or None)
            return dict(row) if row else None
        finally:
            conn.close()

    def own(self, client: str, project_id: Optional[int], conn_key: Optional[str]) -> Optional[dict]:
        """This connection's open automatic session (in `project_id`, else the one it used
        last). Never another connection's: two windows of one app each get their own."""
        if not conn_key:
            return self.current(client, project_id) if project_id else None
        conn = self._conn()
        try:
            if not _has_runs(conn):
                return None
            last = self._last.get(conn_key[:80]) if not project_id else None
            if last is not None:   # exact: the session this connection's last call touched
                row = conn.execute("SELECT * FROM auto_sessions WHERE session_id = ? AND "
                                   "closed_at IS NULL", (last,)).fetchone()
                if row is not None:
                    return dict(row)
            sql = ("SELECT a.* FROM auto_sessions a WHERE a.closed_at IS NULL AND a.client = ?"
                   + (" AND a.project_id = ?" if project_id else "") + _BY_CONN
                   + " ORDER BY a.last_activity DESC, a.session_id DESC LIMIT 1")
            args = (client[:80] if client else "unknown",) + ((project_id,) if project_id else ()) \
                + (conn_key[:80],)
            row = conn.execute(sql, args).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def is_auto(self, session_id: int) -> bool:
        conn = self._conn()
        try:
            return conn.execute("SELECT 1 FROM auto_sessions WHERE session_id = ?",
                                (session_id,)).fetchone() is not None
        finally:
            conn.close()

    # ---------------------------------------------------------------- closing
    def close_idle(self) -> list[int]:
        """Close every open auto session idle longer than the timeout (any process)."""
        cutoff = _ts(_utcnow() - self.idle)
        return self._close_where("last_activity < ?", (cutoff,), "idle")

    def close_instance(self, reason: str = "shutdown") -> list[int]:
        """Close sessions this process owns (client disconnected / server stopping)."""
        return self._close_where("server_instance = ?", (self.instance,), reason)

    def mark_explicit_end(self, session_id: int) -> None:
        """Agent ended the session itself: keep its summary, just stop tracking."""
        conn = self._conn()
        try:
            cur = conn.execute(
                """UPDATE auto_sessions SET closed_at = ?, close_reason = 'explicit'
                   WHERE session_id = ? AND closed_at IS NULL""",
                (_ts(_utcnow()), session_id))
            if cur.rowcount:
                pid = conn.execute("SELECT project_id FROM auto_sessions WHERE session_id = ?",
                                   (session_id,)).fetchone()
                self._release_claims(conn, session_id, pid["project_id"], "explicit")
            conn.commit()
        finally:
            conn.close()

    def _close_where(self, where: str, params: tuple, reason: str) -> list[int]:
        conn = self._conn()
        closed = []
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"SELECT * FROM auto_sessions WHERE closed_at IS NULL AND {where}",
                params).fetchall()
            for row in rows:
                self._close_row(conn, row, reason)
                closed.append(row["session_id"])
            conn.commit()
            return closed
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _close_row(self, conn: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        opened, last = _parse(row["opened_at"]), _parse(row["last_activity"])
        minutes = max(0, int((last - opened).total_seconds() // 60))
        events = json.loads(row["events"] or "[]")
        summary = build_summary(row["client"], row["call_count"], minutes, events, reason)
        outcomes = {"auto_captured": True, "needs_review": True, "close_reason": reason,
                    "client": row["client"], "calls": row["call_count"]}
        from .review_mode import get_with
        if get_with(conn, row["project_id"])["mode"] == "off":
            # The user turned review off: keep the summary as a normal one, queue nothing.
            summary = OFF_TAG + summary[len(AUTO_TAG):]
            outcomes.update({"needs_review": False, "review": "off"})
        # Only fill the session if the agent hasn't ended it with its own summary. The
        # summary stays flagged as provisional and waits in the review queue for the
        # user's OK (approve replaces it with the reviewed text).
        cur = conn.execute(
            """UPDATE sessions SET ended_at = ?, summary = ?, outcomes = ?,
               duration_minutes = ? WHERE id = ? AND ended_at IS NULL""",
            (row["last_activity"], summary, json.dumps(outcomes), minutes,
             row["session_id"]))
        if cur.rowcount and outcomes["needs_review"]:
            from .review import add_item
            from .identity import on_behalf_of
            item = add_item(self.db_path, row["project_id"], "session_summary", summary,
                            payload={"close_reason": reason, "calls": row["call_count"]},
                            source=row["client"], session_id=row["session_id"], conn=conn,
                            ident=on_behalf_of(row["client"]))
            if item is not None:
                outcomes["review_item_id"] = item
                conn.execute("UPDATE sessions SET outcomes = ? WHERE id = ?",
                             (json.dumps(outcomes), row["session_id"]))
        conn.execute(
            "UPDATE auto_sessions SET closed_at = ?, close_reason = ? WHERE session_id = ?",
            (_ts(_utcnow()), reason, row["session_id"]))
        self._release_claims(conn, row["session_id"], row["project_id"], reason)

    @staticmethod
    def _release_claims(conn, session_id: int, project_id: int, reason: str) -> None:
        """A claim ends with its session (claims.py): drop what this connection held here."""
        if not _has_runs(conn):
            return
        key = conn.execute("SELECT conn_key, app FROM session_runs WHERE session_id = ?",
                           (session_id,)).fetchone()
        if key and key["conn_key"]:
            from . import claims
            claims.release_for_session(conn, key["conn_key"], key["app"], project_id, reason)


_REASON_TEXT = {"idle": "closed after idle timeout", "shutdown": "client disconnected",
                "stale": "closed on next activity after idle timeout",
                "explicit": "ended by agent"}


def build_summary(client: str, calls: int, minutes: int, events: list, reason: str) -> str:
    """Short, factual summary from the event log. Flagged for the user's review."""
    writes = [e for e in events if not e.startswith("read:")]
    reads: dict[str, int] = {}
    for e in events:
        if e.startswith("read:"):
            name = e[5:]
            reads[name] = reads.get(name, 0) + 1
    head = (f"{AUTO_TAG} {client}, {calls} call{'s' if calls != 1 else ''} over "
            f"{minutes} min ({_REASON_TEXT.get(reason, reason)}).")
    parts = [head]
    if writes:
        parts.append(" Logged: " + "; ".join(writes) + ".")
    else:
        parts.append(" Nothing was logged.")
    if reads:
        parts.append(" Read: " + ", ".join(f"{k} x{v}" for k, v in reads.items()) + ".")
    text = "".join(parts)
    if len(text) > MAX_SUMMARY:
        text = text[:MAX_SUMMARY - 3] + "..."
    return text
