"""Write receipts (2.4.0, migration 016): every write answer says what it saved.

Each write gets a change id (12 hex characters). The edit history uses it as its edit id, so
`manage_history list` finds the same change. When the write ends, one row goes into
`run_writes`: the change id, the connection and session, the tool, a hash of the arguments,
saved or failed, and the record ids. Never the text.

- Receipt: a saved write's answer gets {"receipt": {"change": id, "saved": ["todo #4"]}}.
- Late saves: a write that timed out often still saves a moment later. The connection's next
  answer then says so ("did save, as learning #31"), or that it failed.
- Duplicates: the same arguments from the same connection within WINDOW_SECONDS of a saved
  write return that write's receipt instead of saving a copy. If the first one is still
  saving (it timed out), the repeat waits for it. Undo (undo again = redo) and claim renewals
  are never treated as repeats. This check is in memory: a connection's writes all pass
  through this process, so it costs the write nothing.

Nothing here may break a write: callers wrap these in try/except, and a database without
run_writes simply gets no receipts.
"""

import hashlib
import itertools
import json
import sqlite3
import threading
import time
import uuid
import weakref
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

WINDOW_SECONDS = 120
NOT_REPEATS = {("history", "undo"), ("todo", "renew")}
_IGNORED_ARGS = ("model", "project_id")   # the same content however the caller names itself

_inflight: dict = {}            # (conn_key, hash) -> threading.Event while a write runs
_recent: dict = {}              # (conn_key, hash) -> (monotonic time, receipt, started utc)
_told: set = set()              # change ids whose caller already has the outcome
_ready: set = set()             # databases known to have run_writes
_late: dict = {}                # conn_key -> [(change_id, tool, started utc)] timed out
_lock = threading.Lock()
_conns: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_counter = itertools.count(1)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn, db_path=None) -> bool:
    if db_path is not None and str(db_path) in _ready:
        return True
    ok = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                      "AND name='run_writes'").fetchone() is not None
    if ok and db_path is not None:
        _ready.add(str(db_path))   # tables are only ever added
    return ok


def new_change_id() -> str:
    return uuid.uuid4().hex[:12]


def connection_key(ident, session_obj=None) -> Optional[str]:
    """The connection a call came on: the HTTP MCP session id, else this server process plus
    which client connection of it (a stdio process has one)."""
    from .identity import PROCESS_SESSION
    if ident is None:
        return None
    if ident.session and ident.session != PROCESS_SESSION:
        return ident.session
    if session_obj is None:
        return ident.session
    with _lock:
        n = _conns.get(session_obj)
        if n is None:
            n = _conns[session_obj] = next(_counter)
    return f"{ident.session}.{n}"


def content_hash(name: str, args: dict) -> str:
    body = {k: v for k, v in (args or {}).items() if k not in _IGNORED_ARGS}
    raw = json.dumps([name, body], sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _receipt(row) -> dict:
    recs = json.loads(row["records"] or "[]")
    return {"change": row["change_id"], "saved": [r.replace(":", " #", 1) for r in recs]}


class Pending:
    """One write in progress (between begin and finish)."""

    def __init__(self, change_id: str, conn_key: Optional[str], name: str, args: dict):
        self.change_id, self.conn_key, self.name = change_id, conn_key, name
        self.action = args.get("action")
        self.hash = content_hash(name, args)
        self.started = _now()
        self.event: Optional[threading.Event] = None


def begin(db_path: Path, change_id: str, conn_key: Optional[str], name: str, args: dict,
          wait: float) -> tuple[Optional[Pending], Optional[dict]]:
    """(pending write, None) to go ahead, or (None, reply) for a repeat of a saved write.
    In memory only: every write of a connection passes through this process."""
    p = Pending(change_id, conn_key, name, args)
    if not conn_key or (name, args.get("action")) in NOT_REPEATS:
        return p, None
    key = (conn_key, p.hash)
    with _lock:
        running = _inflight.get(key)
    if running is not None:   # the same write is still saving (it timed out): wait for it
        running.wait(max(0.1, wait))
    with _lock:
        hit = _recent.get(key)
        if hit is not None and time.monotonic() - hit[0] < WINDOW_SECONDS:
            _told.add(hit[1]["change"])
            return None, {"already_saved": True, "receipt": hit[1],
                          "note": f"This exact write was already saved at {hit[2][11:19]} UTC "
                                  f"in this session, so it was not saved again."}
        if key in _inflight:   # still running after the wait: don't start a second copy
            return None, {"error": "still_saving",
                          "message": "The same write from this session is still being saved. "
                                     "Don't repeat it; the next FlowState answer will say "
                                     "whether it landed."}
        p.event = _inflight[key] = threading.Event()
    return p, None


def finish(db_path: Path, p: Pending, result, project_id: Optional[int],
           session_id: Optional[int], ident, created: Optional[tuple] = None,
           client: str = "unknown") -> Optional[dict]:
    """Record how the write ended, in one transaction with the created record's history
    row (if any); returns the receipt for a saved write (else None)."""
    receipt = None
    try:
        failed = result is None or (isinstance(result, dict) and "error" in result)
        conn = _conn(db_path)
        try:
            if created and not failed:
                from .history import record_created
                record_created(db_path, *created, "create", client, conn=conn)
            if not available(conn, db_path):
                conn.commit()
                return None
            recs = [] if failed else _records(conn, p.change_id, result, created)
            who = ident.as_dict() if ident is not None else {}
            conn.execute(
                """INSERT INTO run_writes (change_id, conn_key, session_id, project_id, app,
                   machine, tool, action, content_hash, status, records, error, started_at,
                   finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.change_id, p.conn_key, session_id, project_id, who.get("app"),
                 who.get("machine"), p.name[:60], (p.action or None) and str(p.action)[:40],
                 p.hash, "failed" if failed else "saved", json.dumps(recs),
                 (str(result.get("error"))[:60] if isinstance(result, dict) and failed
                  else None), p.started, _now()))
            conn.commit()
            if not failed:
                receipt = {"change": p.change_id,
                           "saved": [r.replace(":", " #", 1) for r in recs]}
            return receipt
        finally:
            conn.close()
    finally:
        with _lock:
            if receipt is not None and p.conn_key:
                now = time.monotonic()
                _recent[(p.conn_key, p.hash)] = (now, receipt, p.started)
                if len(_recent) > 500:   # forget what is past the window
                    for k in [k for k, v in _recent.items() if now - v[0] >= WINDOW_SECONDS]:
                        _recent.pop(k, None)
            if p.event is not None:
                _inflight.pop((p.conn_key, p.hash), None)
        if p.event is not None:
            p.event.set()


_TYPE_OF_TABLE = None


def _records(conn, change_id: str, result, created: Optional[tuple]) -> list[str]:
    """'type:id' of what the write saved: the record it created, the rows the edit history
    recorded under this change id, else an id the answer names."""
    global _TYPE_OF_TABLE
    if _TYPE_OF_TABLE is None:
        from .history import TYPE_OF
        _TYPE_OF_TABLE = TYPE_OF
    out = []
    if created:
        out.append(f"{created[0]}:{int(created[1])}")
    try:
        rows = conn.execute("SELECT DISTINCT table_name, record_id FROM record_history "
                            "WHERE edit_id = ? ORDER BY id", (change_id,)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for t, rid in rows:
        ref = f"{_TYPE_OF_TABLE.get(t, t)}:{rid}"
        if ref not in out and t in _TYPE_OF_TABLE:
            out.append(ref)
    if not out and isinstance(result, dict):
        for key, rtype in (("item_id", "review_item"), ("queued_for_review", "review_item"),
                           ("delete_requested", "review_item"), ("link_id", "link"),
                           ("handoff_id", "handoff"), ("rule_id", "rule"),
                           ("todo_id", "todo"), ("session_id", "session")):
            v = result.get(key)
            if isinstance(v, int) and not isinstance(v, bool):
                out.append(f"{rtype}:{v}")
                break
    return out[:20]


def attach(result, receipt: Optional[dict]):
    """The answer with its receipt (a dict gets a key; text gets one line)."""
    if not receipt:
        return result
    if isinstance(result, dict):
        return {**result, "receipt": receipt}
    if isinstance(result, str):
        saved = ", ".join(receipt["saved"]) or "saved"
        return f"{result}\n(receipt: {saved}; change {receipt['change']})"
    return result


# ---------------------------------------------------------------- late saves
def timed_out(conn_key: Optional[str], change_id: Optional[str], tool: str) -> None:
    if conn_key and change_id:
        with _lock:
            _late.setdefault(conn_key, []).append((change_id, tool, _now()))


def waiting(conn_key: Optional[str]) -> bool:
    """Does this connection have a timed-out write whose outcome it wasn't told yet?"""
    return bool(conn_key) and bool(_late.get(conn_key))


def late_news(db_path: Path, conn_key: Optional[str]) -> list[str]:
    """One line per earlier write of this connection that timed out and has ended since."""
    with _lock:
        waiting = list(_late.get(conn_key or "", ()))
    if not waiting:
        return []
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        lines, done = [], set()
        for change_id, tool, at in waiting:
            row = conn.execute("SELECT * FROM run_writes WHERE change_id = ?",
                               (change_id,)).fetchone()
            if row is None:
                continue                       # still saving: say so next time
            done.add(change_id)
            if row["told_at"] or change_id in _told:
                _told.discard(change_id)
                continue                       # a repeat already got its receipt
            when = f"{tool} from {at[11:19]} UTC"
            if row["status"] == "saved":
                saved = ", ".join(_receipt(row)["saved"]) or "it"
                lines.append(f"FlowState: your earlier {when} that timed out did save, as "
                             f"{saved} (change {change_id}). Don't log it again.")
            else:
                lines.append(f"FlowState: your earlier {when} that timed out did not save "
                             f"({row['error'] or 'error'}). You can try it once more.")
            conn.execute("UPDATE run_writes SET told_at = ? WHERE id = ?", (_now(), row["id"]))
        conn.commit()
    finally:
        conn.close()
    with _lock:
        left = [w for w in _late.get(conn_key, []) if w[0] not in done]
        if left:
            _late[conn_key] = left
        else:
            _late.pop(conn_key, None)
    return lines
