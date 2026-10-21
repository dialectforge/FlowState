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

from . import activity, identity


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
        row = [sid, conn_key, pid, who.get("app"), who.get("machine"), tool[:60],
               (action or None) and str(action)[:40], _now(), answer_hash(answer),
               json.dumps(shown[:200]) if shown is not None else None]
        cols = "session_id, conn_key, project_id, app, machine, tool, action, at, answer_hash, records"
        if meta.get("upto_write") is not None and _has_upto(conn):
            cols, row = cols + ", upto_write", row + [int(meta["upto_write"])]
        conn.execute(f"INSERT INTO run_views ({cols}) VALUES ({', '.join('?' * len(row))})", row)
        if random.random() < 0.01:   # prune now and then, like the activity log
            cutoff = (datetime.now(timezone.utc) - timedelta(days=activity.keep_days())).strftime(
                "%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM run_views WHERE at < ?", (cutoff,))
            conn.execute("DELETE FROM run_writes WHERE finished_at < ?", (cutoff,))
        conn.commit()
    finally:
        conn.close()


_UPTO: set = set()   # databases known to have run_views.upto_write (018)


def _has_upto(conn) -> bool:
    key = conn.execute("PRAGMA database_list").fetchone()[2]
    if key in _UPTO:
        return True
    ok = any(r[1] == "upto_write" for r in conn.execute("PRAGMA table_info(run_views)"))
    if ok:
        _UPTO.add(key)
    return ok


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


# ---------------------------------------------------------------- since your last visit
TAG = "Since your last visit"
SINCE_MAX = 240          # one line, never more
LONG_GAP_DAYS = 14
_KINDS = (("decision", "decisions"), ("change", "changes"), ("problem", "problems"),
          ("learning", "learnings"), ("todo", "todos"))


def _local(utc: str) -> datetime:
    return datetime.strptime(utc[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone()


def _when(utc: str, now: datetime) -> str:
    dt = _local(utc)
    return f"{dt:%b} {dt.day}" + (f" {dt:%H:%M}" if (now - dt) < timedelta(days=2) else "")


def _names(apps: list, room: int = 3) -> str:
    shown = [a if len(a) <= 30 else a[:27] + "..." for a in apps[:room]]
    rest = len(apps) - len(shown)
    if rest:
        return ", ".join(shown) + f" and {rest} more"
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def since_line(db_path: Path, project: dict, ident, session_id: Optional[int],
               meta: Optional[dict] = None, now: Optional[datetime] = None) -> Optional[str]:
    """One line for the brief: what other sessions (other apps, and other windows of this
    app) saved in this project since this window's previous brief here (a new window: since
    this app's latest brief here). None on a first visit or when nothing changed. Sets
    meta["upto_write"]: the newest save counted, so the next brief starts after it."""
    if ident is None:
        return None
    now = now or datetime.now(timezone.utc)
    key = identity.call_key()
    conn = _conn(db_path)
    try:
        if not available(conn):
            return None
        upto = conn.execute("SELECT COALESCE(MAX(id), 0) FROM run_writes").fetchone()[0]
        if meta is not None:
            meta["upto_write"] = upto
        cols = "at, session_id, conn_key" + (", upto_write" if _has_upto(conn) else ", NULL AS upto_write")
        last = conn.execute(
            f"""SELECT {cols} FROM run_views WHERE project_id = ? AND conn_key = ?
                AND tool = 'start_here' ORDER BY id DESC LIMIT 1""",
            (project["id"], key)).fetchone() if key else None
        mine = last is not None
        if last is None:   # this window's first brief here: since this app's latest one
            last = conn.execute(
                f"""SELECT {cols} FROM run_views WHERE project_id = ? AND app = ?
                    AND machine IS ? AND tool = 'start_here' ORDER BY id DESC LIMIT 1""",
                (project["id"], ident.app, ident.machine)).fetchone()
        if last is None:
            return None
        # Not counted: this window's own saves, and (for a new window) those of the app's
        # previous window if it has closed (most likely this one before a restart).
        skip_keys = [key] if key else []
        if not mine and last["conn_key"]:
            closed = conn.execute("SELECT closed_at FROM auto_sessions WHERE session_id = ?",
                                  (last["session_id"] or -1,)).fetchone()
            if closed is None or closed[0] is not None:
                skip_keys.append(last["conn_key"])
        after, arg = (("id > ?", last["upto_write"]) if last["upto_write"] is not None
                      else ("finished_at >= ?", last["at"]))
        marks = ", ".join("?" * len(skip_keys))
        rows = conn.execute(
            f"""SELECT app, session_id, records FROM run_writes WHERE project_id = ?
                AND status = 'saved' AND {after} AND id <= ?
                AND (session_id IS NULL OR session_id != ?)
                {f"AND (conn_key IS NULL OR conn_key NOT IN ({marks}))" if skip_keys else ""}
                ORDER BY id""",
            (project["id"], arg, upto, session_id or -1, *skip_keys)).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    apps: list = []
    for r in rows:
        who = r["app"] or "an app"
        who = f"another {who} session" if who == ident.app else who
        if who not in apps:
            apps.append(who)
    when = _when(last["at"], now)
    gap = now - datetime.strptime(last["at"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if gap > timedelta(days=LONG_GAP_DAYS):
        day = f"{_local(last['at']):%Y-%m-%d}"   # manage_history who takes local dates
        line = (f"{TAG} ({when}): {len(rows)} save{'s' if len(rows) != 1 else ''} by "
                f"{len(apps)} app{'s' if len(apps) != 1 else ''}; "
                f"manage_history(action=\"who\", project=\"{project['name']}\", since=\"{day}\") "
                f"lists them.")
        return line if len(line) <= SINCE_MAX else line[:SINCE_MAX - 3] + "..."
    counts: dict = {}
    for r in rows:
        for ref in json.loads(r["records"] or "[]") or ["other:0"]:
            kind = ref.split(":", 1)[0]
            counts[kind] = counts.get(kind, 0) + 1
    parts = [f"{counts[k]} {k if counts[k] == 1 else many}" for k, many in _KINDS if counts.get(k)]
    other = sum(n for k, n in counts.items() if k not in dict(_KINDS))
    if other:
        parts.append(f"{other} other")
    for room in (3, 2, 1):
        line = f"{TAG} ({when}): {_names(apps, room)} saved {', '.join(parts)}."
        if len(line) <= SINCE_MAX:
            return line
    return line[:SINCE_MAX - 3] + "..."
