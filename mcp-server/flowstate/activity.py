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


def pause_names(db_path: Path, app: str, paused: bool = True) -> list:
    """The names Pause / Resume on `app` set: the app's own name and every key, token or
    sign-in it came in on (apps()' "via"). An app's name is its own claim, so pausing only
    that name would let it come back as "Grok Bot 2" on the same key; the key's name it can't
    change. Resume (paused=False) leaves out a key that another paused app also uses
    (still_paused_by): resuming one app must not let the other dodge its pause by renaming
    itself (audit N-F18)."""
    app = (app or "").strip()
    rows = apps(db_path)
    row = next((a for a in rows if a["app"] == app), None)
    names = [app] + [v for v in (row or {}).get("via", []) if v.casefold() != app.casefold()]
    if paused:
        return names
    held = still_paused_by(db_path, app, rows)
    return [n for n in names if n not in held]


def still_paused_by(db_path: Path, app: str, rows: Optional[list] = None) -> dict:
    """{key / sign-in name: [other apps]} for the keys `app` uses that stay paused when `app`
    is resumed, because another app on the same key is paused by its own name."""
    app = (app or "").strip()
    rows = rows if rows is not None else apps(db_path)
    row = next((a for a in rows if a["app"] == app), None)
    mine = {v for v in (row or {}).get("via", []) if v.casefold() != app.casefold()}
    if not mine:
        return {}
    own = _own_pauses(db_path)
    held: dict = {}
    for other in rows:
        if other["app"] == app or not own.get(other["app"]):
            continue
        for v in mine.intersection(other.get("via") or []):
            if v.casefold() != other["app"].casefold():
                held.setdefault(v, []).append(other["app"])
    return held


def _own_pauses(db_path: Path) -> dict:
    """{name: paused} as the user set it, name by name (no key inheritance)."""
    conn = _conn(db_path)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='app_controls'").fetchone():
            return {}
        return {r["app"]: bool(r["paused"]) for r in conn.execute(
            "SELECT app, paused FROM app_controls")}
    finally:
        conn.close()


def held_text(app: str, key: str, others: list) -> str:
    return (f"{key!r} stays paused: {', '.join(repr(o) for o in others)} also use(s) it and "
            f"{'is' if len(others) == 1 else 'are'} paused, so {app!r}'s writes through it are "
            f"still refused. Resume {'that app' if len(others) == 1 else 'those apps'} too, or "
            f"give {app!r} its own key or token.")


def set_paused_all(db_path: Path, app: str, paused: bool, by: str) -> list:
    """set_paused for each of pause_names(app, paused); returns the names."""
    names = pause_names(db_path, app, paused)
    for n in names:
        set_paused(db_path, n, paused, by)
    return names


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
        controls = {}
        for r in conn.execute("SELECT app, paused, changed_at FROM app_controls"):
            controls[r["app"]] = bool(r["paused"])
            rows.setdefault(r["app"], {"app": r["app"], "last_seen": None, "calls": 0,
                                       "writes": 0, "errors": 0})
            rows[r["app"]]["paused"] = bool(r["paused"])
            rows[r["app"]]["paused_changed_at"] = r["changed_at"]
        if identity.has_column(conn, "activity_log", "identity_source"):
            for r in conn.execute("SELECT DISTINCT app, machine, identity_source FROM activity_log "
                                  "WHERE identity_source IN ('oauth_client', 'remote_token', 'app_key')"):
                signed = identity.via(r["machine"], r["identity_source"])
                if signed and r["app"] in rows and signed not in rows[r["app"]].setdefault("via", []):
                    rows[r["app"]]["via"].append(signed)
        for v in rows.values():
            v.setdefault("paused", False)
            v["via"] = sorted(v.get("via", []))
            # writes are refused when the key or sign-in it uses is paused too
            v["paused"] = v["paused"] or any(controls.get(x) for x in v["via"])
        return sorted(rows.values(), key=lambda v: v["last_seen"] or "", reverse=True)
    finally:
        conn.close()


def app_label(a: dict) -> str:
    """An apps() row for people: 'Grok Bot (via Cursor)' when it came in on keys, tokens or
    sign-ins of another name (pausing either name stops it), else the name."""
    others = [v for v in a.get("via") or [] if v.casefold() != (a["app"] or "").casefold()]
    return f"{a['app']} (via {', '.join(others)})" if others else a["app"]


def recent(db_path: Path, app: Optional[str] = None, limit: int = 50) -> list[dict]:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        who = ", ".join(f"a.{c}" if identity.has_column(conn, "activity_log", c) else f"NULL AS {c}"
                        for c in ("machine", "identity_source"))
        sql = f"""SELECT a.at, a.app, a.tool, a.action, a.project_id, p.name project, a.write, a.ok,
                         a.error, {who} FROM activity_log a LEFT JOIN projects p ON p.id = a.project_id"""
        params: list = []
        if app:
            sql += " WHERE a.app = ?"
            params.append(app)
        sql += " ORDER BY a.id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
