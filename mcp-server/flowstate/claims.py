"""To-do claims with an expiry (2.3.0, migration 013).

One database serves every agent, so two agents starting at once can both pick the same
"next" todo. A claim says "I'm on this until <time>": the brief, `next` and `list` show it,
so the second agent picks something else instead of doing the same work silently.

- claim: one conditional UPSERT decides, inside one write transaction. It succeeds when the
  todo is unclaimed, its claim has expired, or the same claimer holds it (then it renews).
- The same claimer = same app, machine and session (identity.py; the session is this server
  process for stdio, the MCP session id over HTTP), so two sessions of one app are told apart.
- A claim ends with its session (2.4.0): when the session closes (the app quits, the agent
  ends it, or it goes idle) its claims are released, and the same app on the same machine
  may take over a claim whose session is no longer open (it reconnected as a new session).
  Other apps stay blocked until the claim expires or is released.
- No hard lock: anyone may still update a claimed todo; they get a warning and an `override`
  event. done / cancelled clears the claim.
- Every event goes into todo_claim_events, which manage_history who lists.

All times come from utcnow() (tests freeze it); stored UTC, shown relative ("expires in 9m").
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import handoffs, identity, links

DEFAULT_MINUTES, MIN_MINUTES, MAX_MINUTES = 15, 1, 120
TS_FMT = "%Y-%m-%d %H:%M:%S"
OPEN = (None, "pending", "in_progress", "blocked")
CLOSED = ("done", "cancelled")
_ID = ("app", "model", "machine", "identity_source", "session")


def utcnow() -> datetime:
    """The one clock for claims (naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(dt: datetime) -> str:
    return dt.strftime(TS_FMT)


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts[:19].replace("T", " "), TS_FMT)


class ClaimError(links.LinkError):
    pass


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='todo_claims'").fetchone() is not None


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# ---------------------------------------------------------------- who
def me(client: Optional[str] = None) -> dict:
    """{app, model, machine, identity_source, session, holder} of the caller."""
    ident = identity.current() or identity.local(client or "unknown")
    who = {**ident.as_dict(), "session": getattr(ident, "session", None)}
    who["holder"] = "\x1f".join(str(who.get(k) or "") for k in ("app", "machine", "session"))
    return who


def holder_name(row) -> str:
    """'cursor on mac-mini' (the app, and the machine when known)."""
    app = row["app"] or "another agent"
    return f"{app} on {row['machine']}" if row["machine"] else app


def relative(expires_at: str, now: Optional[datetime] = None) -> str:
    """'9m', '1h 5m', or 'now'."""
    secs = (_parse(expires_at) - (now or utcnow())).total_seconds()
    mins = int(-(-secs // 60)) if secs > 0 else 0   # round up: 8m30s left -> 9m
    if mins <= 0:
        return "now"
    return f"{mins}m" if mins < 60 else f"{mins // 60}h {mins % 60}m" if mins % 60 else f"{mins // 60}h"


def _who(row, mine: str) -> str:
    """'you', 'cursor on mac-mini', or 'cursor on mac-mini (another session)'."""
    if row["holder"] == mine:
        return "you"
    me_app, me_machine = (mine.split("\x1f") + ["", ""])[:2]
    same = (row["app"] or "") == me_app and (row["machine"] or "") == me_machine
    return holder_name(row) + (" (another session)" if same else "")


def describe(row, mine: str, now: Optional[datetime] = None) -> str:
    """'claimed by cursor on mac-mini, expires in 9m' (or 'claimed by you, ...')."""
    return f"claimed by {_who(row, mine)}, expires in {relative(row['expires_at'], now)}"


def active(conn, todo_ids=None, pid: Optional[int] = None) -> dict:
    """{todo_id: row} of unexpired claims (by ids, or for a whole project)."""
    if not available(conn):
        return {}
    now = _ts(utcnow())
    if todo_ids is not None:
        ids = [int(i) for i in todo_ids]
        if not ids:
            return {}
        rows = conn.execute(f"SELECT * FROM todo_claims WHERE expires_at > ? AND todo_id IN "
                            f"({', '.join('?' * len(ids))})", (now, *ids))
    else:
        rows = conn.execute("SELECT * FROM todo_claims WHERE expires_at > ? AND project_id = ?",
                            (now, pid))
    return {r["todo_id"]: r for r in rows}


def notes(db_path: Path, todo_ids, client: Optional[str] = None) -> dict:
    """{todo_id: 'claimed by ..., expires in ...'} for the active claims among todo_ids."""
    conn = connect(db_path)
    try:
        mine, now = me(client)["holder"], utcnow()
        return {tid: describe(r, mine, now) for tid, r in active(conn, todo_ids).items()}
    finally:
        conn.close()


def annotate(db_path: Path, rows, client: Optional[str] = None):
    """`todo list`: add "claim" to each row that has an active claim."""
    ids = [r["id"] for r in rows if isinstance(r, dict) and isinstance(r.get("id"), int)]
    found = notes(db_path, ids, client) if ids else {}
    for r in rows:
        if isinstance(r, dict) and r.get("id") in found:
            r["claim"] = found[r["id"]]
    return rows


def split_next(conn, ready: list, blocked: list, client: Optional[str] = None) -> tuple:
    """`todo next`: (ready with your claims first and others' claims removed, the removed
    ones as short lines). Unchanged when nothing is claimed."""
    claimed = active(conn, [i["id"] for i in ready + blocked])
    if not claimed:
        return ready, []
    mine, now = me(client)["holder"], utcnow()
    own, free, others = [], [], []
    for i in ready:
        c = claimed.get(i["id"])
        if c is None:
            free.append(i)
        elif c["holder"] == mine:
            own.append({**i, "claim": f"yours, expires in {relative(c['expires_at'], now)}"})
        else:
            others.append(f"#{i['id']} {links._one_line(i['title'], 60)} "
                          f"({describe(c, mine, now)})")
    for i in blocked:
        if i["id"] in claimed:
            i["claim"] = describe(claimed[i["id"]], mine, now)
    return own + free, others


# ---------------------------------------------------------------- events
def _event(conn, todo_id: int, pid, event: str, summary: str, who: dict) -> None:
    row = {"todo_id": todo_id, "project_id": pid, "event": event, "summary": summary[:200],
           "at": _ts(utcnow()), **{k: who.get(k) for k in _ID}}
    conn.execute(f"INSERT INTO todo_claim_events ({', '.join(row)}) "
                 f"VALUES ({', '.join('?' * len(row))})", list(row.values()))


# ---------------------------------------------------------------- actions
def _need(conn) -> None:
    if not available(conn):
        raise ClaimError("claims_unavailable", "Todo claims aren't set up in this database yet "
                         "(migration 013 has not run). Restart FlowState.")


def _minutes(value) -> int:
    if value in (None, ""):
        return DEFAULT_MINUTES
    m = None
    if isinstance(value, int) and not isinstance(value, bool):
        m = value
    elif isinstance(value, float) and value.is_integer():
        m = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        m = int(value.strip())
    if m is None or not MIN_MINUTES <= m <= MAX_MINUTES:
        raise ClaimError("bad_minutes", f"minutes must be a whole number from {MIN_MINUTES} to "
                         f"{MAX_MINUTES} (default {DEFAULT_MINUTES}).")
    return m


def _todo(conn, pid: int, todo_id) -> sqlite3.Row:
    if todo_id is None:
        raise ClaimError("todo_id_required", "Give todo_id (#id in the brief).")
    t = conn.execute("SELECT id, project_id, title, status FROM todos WHERE id = ?",
                     (int(todo_id),)).fetchone()
    if t is None:
        raise ClaimError("record_not_found", f"No todo with id {todo_id}.")
    if t["project_id"] != pid:
        raise ClaimError("project_mismatch", f"Todo #{todo_id} is in project "
                         f"'{links.project_name(conn, t['project_id'])}'.")
    return t


def _ended_session_sql(conn) -> Optional[str]:
    """SQL (for the claim upsert) that is true when the existing claim's session is known
    and no longer open for its app. None before migration 015."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_runs'"
                    ).fetchone() is None:
        return None
    return ("EXISTS (SELECT 1 FROM session_runs r WHERE r.conn_key = todo_claims.session) "
            "AND NOT EXISTS (SELECT 1 FROM session_runs r JOIN auto_sessions a USING (session_id) "
            "WHERE r.conn_key = todo_claims.session AND r.app IS todo_claims.app "
            "AND a.closed_at IS NULL)")


def release_for_session(conn, session_key: Optional[str], app: Optional[str], project_id: int,
                        reason: str) -> int:
    """Inside the caller's transaction: drop the claims a closed session (this connection
    and app) held in this project, with a `release` event each. Returns how many."""
    if not session_key or not available(conn):
        return 0
    rows = conn.execute("SELECT * FROM todo_claims WHERE session = ? AND app IS ? AND project_id = ?",
                        (session_key, app, project_id)).fetchall()
    for r in rows:
        conn.execute("DELETE FROM todo_claims WHERE todo_id = ?", (r["todo_id"],))
        _event(conn, r["todo_id"], project_id, "release",
               f"claim on todo #{r['todo_id']} released: its session ended ({reason})",
               {k: r[k] for k in _ID})
    return len(rows)


def _held_elsewhere(t, row, now: datetime, mine: str) -> ClaimError:
    return ClaimError(
        "claimed_by_other",
        f"Todo #{t['id']} is claimed by {_who(row, mine)} until {row['expires_at']} UTC "
        f"(expires in {relative(row['expires_at'], now)}). Pick another one: "
        f"manage_todos(action=\"next\") lists what is free.",
        holder=holder_name(row), expires_at=row["expires_at"])


def claim(db_path: Path, pid: int, todo_id, minutes=None, client: Optional[str] = None) -> dict:
    mins = _minutes(minutes)
    who = me(client)
    now = utcnow()
    expires = _ts(now + timedelta(minutes=mins))
    conn = connect(db_path)
    try:
        _need(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            t = _todo(conn, pid, todo_id)
            if t["status"] not in OPEN:
                raise ClaimError("todo_closed", f"Todo #{t['id']} is {t['status']}; claim an open one.")
            before = conn.execute("SELECT * FROM todo_claims WHERE todo_id = ?",
                                  (t["id"],)).fetchone()
            vals = {"todo_id": t["id"], "project_id": pid, "holder": who["holder"],
                    **{k: who.get(k) for k in _ID}, "claimed_at": _ts(now), "expires_at": expires}
            # The decision is this one statement: insert, or take over only when the current
            # claim is ours, has expired, or is this app's on this machine from a session
            # that is no longer open (the app reconnected).
            ended = _ended_session_sql(conn)
            same_app = (f" OR (todo_claims.app IS excluded.app AND todo_claims.machine IS "
                        f"excluded.machine AND {ended})" if ended else "")
            cur = conn.execute(
                f"""INSERT INTO todo_claims ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})
                    ON CONFLICT(todo_id) DO UPDATE SET
                      claimed_at = CASE WHEN todo_claims.holder = excluded.holder
                                        AND todo_claims.expires_at > ?
                                        THEN todo_claims.claimed_at ELSE excluded.claimed_at END,
                      project_id = excluded.project_id, holder = excluded.holder,
                      app = excluded.app, model = excluded.model, machine = excluded.machine,
                      identity_source = excluded.identity_source, session = excluded.session,
                      expires_at = excluded.expires_at
                    WHERE todo_claims.holder = excluded.holder OR todo_claims.expires_at <= ?"""
                + same_app,
                (*vals.values(), _ts(now), _ts(now)))
            if cur.rowcount != 1:
                row = conn.execute("SELECT * FROM todo_claims WHERE todo_id = ?",
                                   (t["id"],)).fetchone()
                raise _held_elsewhere(t, row, now, who["holder"])
            live = before is not None and before["expires_at"] > _ts(now)
            renewed = live and before["holder"] == who["holder"]
            title = links._one_line(t["title"], 60)
            if renewed:
                _event(conn, t["id"], pid, "renew", f"renewed claim on todo #{t['id']} ({mins}m)", who)
            else:
                took = ""
                if before is not None and before["holder"] != who["holder"]:
                    took = (f" (took over the claim of an ended {holder_name(before)} session)"
                            if live else f" (took over an expired claim by {holder_name(before)})")
                _event(conn, t["id"], pid, "claim",
                       f"claimed todo #{t['id']} {title} for {mins}m{took}", who)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"todo_id": t["id"], "claimed": True, "renewed": renewed, "expires_at": expires,
                "expires_in": relative(expires, now),
                "note": "Other agents see it as claimed. Renew before it expires if you need "
                        "longer; release or handoff when you stop."}
    finally:
        conn.close()


def renew(db_path: Path, pid: int, todo_id, minutes=None, client: Optional[str] = None) -> dict:
    """Extend your own claim. Fails if you don't hold it (a claim of yours that ran out is
    renewed too, as long as nobody else has taken it since)."""
    mins = _minutes(minutes)
    who = me(client)
    now = utcnow()
    expires = _ts(now + timedelta(minutes=mins))
    conn = connect(db_path)
    try:
        _need(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            t = _todo(conn, pid, todo_id)
            cur = conn.execute("UPDATE todo_claims SET expires_at = ?, model = ? "
                               "WHERE todo_id = ? AND holder = ?",
                               (expires, who.get("model"), t["id"], who["holder"]))
            if cur.rowcount != 1:
                row = conn.execute("SELECT * FROM todo_claims WHERE todo_id = ? AND expires_at > ?",
                                   (t["id"], _ts(now))).fetchone()
                if row is not None:
                    raise _held_elsewhere(t, row, now, who["holder"])
                raise ClaimError("not_claimed", f"You don't hold a claim on todo #{t['id']}. "
                                 f"Claim it first: manage_todos(action=\"claim\").")
            _event(conn, t["id"], pid, "renew", f"renewed claim on todo #{t['id']} ({mins}m)", who)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"todo_id": t["id"], "renewed": True, "expires_at": expires,
                "expires_in": relative(expires, now)}
    finally:
        conn.close()


def _drop_own(conn, t, pid: int, who: dict, event: str, summary: str) -> bool:
    """Delete the caller's claim on t (inside the open transaction). True if there was one;
    raises if someone else holds it."""
    now = utcnow()
    cur = conn.execute("DELETE FROM todo_claims WHERE todo_id = ? AND holder = ?",
                       (t["id"], who["holder"]))
    if cur.rowcount != 1:
        row = conn.execute("SELECT * FROM todo_claims WHERE todo_id = ? AND expires_at > ?",
                           (t["id"], _ts(now))).fetchone()
        if row is not None:
            raise ClaimError("not_your_claim", f"Todo #{t['id']} is claimed by {holder_name(row)} "
                             f"(expires in {relative(row['expires_at'], now)}), not by you.",
                             holder=holder_name(row), expires_at=row["expires_at"])
        return False
    _event(conn, t["id"], pid, event, summary, who)
    return True


def release(db_path: Path, pid: int, todo_id, client: Optional[str] = None) -> dict:
    who = me(client)
    conn = connect(db_path)
    try:
        _need(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            t = _todo(conn, pid, todo_id)
            had = _drop_own(conn, t, pid, who, "release", f"released claim on todo #{t['id']}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"todo_id": t["id"], "released": had,
                "note": "Others can claim it now." if had else
                        "You held no claim on it (it may have expired); nothing to release."}
    finally:
        conn.close()


def handoff(db_path: Path, pid: int, todo_id, where_i_was: Optional[str] = None,
            next_step: Optional[str] = None, client: Optional[str] = None) -> dict:
    """Leave a handoff note (handoffs.py) about this todo and drop your claim."""
    who = me(client)
    conn = connect(db_path)
    try:
        _need(conn)
        t = _todo(conn, pid, todo_id)
        row = active(conn, [t["id"]]).get(t["id"])
        if row is not None and row["holder"] != who["holder"]:
            raise ClaimError("not_your_claim", f"Todo #{t['id']} is claimed by {holder_name(row)}, "
                             f"not by you; leave a note with manage_handoffs instead.",
                             holder=holder_name(row), expires_at=row["expires_at"])
    finally:
        conn.close()
    title = links._one_line(t["title"], 80)
    where = (where_i_was or "").strip()
    nxt = (next_step or "").strip() or (f"Claim todo #{t['id']} (manage_todos action=claim) "
                                        f"and carry on.")
    note = handoffs.leave(db_path, pid, f"Todo #{t['id']} {title}: "
                          + (where or "started, not finished."), nxt, client or who["app"])
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            had = _drop_own(conn, t, pid, who, "handoff",
                            f"handed off todo #{t['id']} (note H{note['handoff_id']})")
            if not had:   # still log the handoff in the trail
                _event(conn, t["id"], pid, "handoff",
                       f"handed off todo #{t['id']} (note H{note['handoff_id']})", who)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"todo_id": t["id"], "released": had, "handoff_id": note["handoff_id"],
            "note": "Claim dropped; the next start_here shows your handoff note."}


def after_update(db_path: Path, pid: int, todo_id: int, action: str, status: Optional[str],
                 client: Optional[str] = None) -> Optional[str]:
    """After someone changed a todo: an `override` event and a warning when another agent
    holds it; done / cancelled clears the claim. Returns the warning text, or None."""
    who = me(client)
    conn = connect(db_path)
    try:
        if not available(conn):
            return None
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = active(conn, [todo_id]).get(todo_id)
            warning = None
            if row is not None and row["holder"] != who["holder"]:
                left = relative(row["expires_at"])
                warning = (f"Heads up: todo #{todo_id} is claimed by {holder_name(row)} (expires "
                           f"in {left}). Your {action} was saved anyway; tell the user or that "
                           f"agent if this was not intended.")
                _event(conn, todo_id, pid, "override",
                       f"{action} on todo #{todo_id} claimed by {holder_name(row)}", who)
            if status in CLOSED:
                cur = conn.execute("DELETE FROM todo_claims WHERE todo_id = ?", (todo_id,))
                if cur.rowcount and row is not None:
                    _event(conn, todo_id, pid, "cleared",
                           f"claim on todo #{todo_id} cleared ({status})", who)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return warning
    finally:
        conn.close()
