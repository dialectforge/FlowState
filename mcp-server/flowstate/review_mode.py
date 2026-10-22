"""Review mode (v2 Phase D, migration 004): which writes wait in the review queue.

  off         nothing waits: suggested=true saves directly, auto summaries are kept as is
  auto_only   only auto-captured session summaries and suggested=true items (default)
  types       also every new record of the chosen kinds (e.g. decision, todo)
  everything  every new record waits for the user's OK

Set per install, with an optional per-project override. Only the user changes it (an AI
must quote the user's request); every change is logged. With approvals on (approvals.py),
turning review off waits for the user's own click or key: queue_if_off queues it as a review
item of kind `setting`, and apply_queued makes the change once the user approves it.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
from . import identity

MODES = ("off", "auto_only", "types", "everything")
DEFAULT = "auto_only"
KINDS = ("decision", "change", "problem", "learning", "todo")

_TEXT = {
    "off": "nothing waits for review",
    "auto_only": "only auto-captured items wait for review",
    "everything": "every new record waits for the user's OK",
}


class ModeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='review_settings'").fetchone() is not None


def _scope(project_id: Optional[int]) -> str:
    return "install" if project_id is None else f"project:{int(project_id)}"


def _row(conn, scope: str) -> Optional[dict]:
    r = conn.execute("SELECT * FROM review_settings WHERE scope = ?", (scope,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["kinds"] = [k for k in json.loads(d["kinds"] or "[]") if k in KINDS]
    except ValueError:
        d["kinds"] = []
    return d


def get(db_path: Path, project_id: Optional[int] = None) -> dict:
    """Effective mode for a project (override, else install-wide, else the default)."""
    conn = _conn(db_path)
    try:
        return get_with(conn, project_id)
    finally:
        conn.close()


def get_with(conn, project_id: Optional[int] = None) -> dict:
    if not available(conn):
        return {"mode": DEFAULT, "kinds": [], "source": "default", "available": False}
    if project_id is not None:
        r = _row(conn, _scope(project_id))
        if r:
            return {"mode": r["mode"], "kinds": r["kinds"], "source": "project",
                    "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
    r = _row(conn, "install")
    if r:
        return {"mode": r["mode"], "kinds": r["kinds"], "source": "install",
                "updated_at": r["updated_at"], "updated_by": r["updated_by"]}
    return {"mode": DEFAULT, "kinds": [], "source": "default"}


def describe(m: dict) -> str:
    if m["mode"] == "types":
        return (f"{', '.join(m['kinds']) or 'no kinds'} and auto-captured items wait for review")
    return _TEXT[m["mode"]]


def brief_line(db_path: Path, project_id: int) -> str:
    m = get(db_path, project_id)
    where = " (this project)" if m.get("source") == "project" else ""
    return (f"Review mode: {m['mode']}{where}, {describe(m)}. Only the user may change it.")


def should_queue(db_path: Path, project_id: int, kind: str, suggested: bool = False,
                 auto: bool = False) -> bool:
    """Does a new record of this kind go to the review queue instead of being saved?"""
    m = get(db_path, project_id)
    mode = m["mode"]
    if mode == "off":
        return False
    if suggested or auto:
        return True
    if mode == "everything":
        return True
    if mode == "types":
        return kind in m["kinds"]
    return False


def queues_auto_summaries(db_path: Path, project_id: int) -> bool:
    return get(db_path, project_id)["mode"] != "off"


def set_mode(db_path: Path, mode: Optional[str], kinds=None, project_id: Optional[int] = None,
             user_request: str = "", client: str = "unknown", caller: str = "stdio",
             clear: bool = False) -> dict:
    """Change the mode (or clear a project override). Logged with the user's words."""
    user_request = (user_request or "").strip()
    if not user_request:
        raise ModeError("user_request_required",
                        "Only the user can change the review mode. Ask them; pass their words "
                        "as user_request.")
    if clear:
        if project_id is None:
            raise ModeError("bad_request", "clear removes a project override; name the project.")
    else:
        if mode not in MODES:
            raise ModeError("bad_mode", f"mode must be one of: {', '.join(MODES)}.")
        kinds = [k for k in (kinds or []) if k]
        bad = [k for k in kinds if k not in KINDS]
        if bad:
            raise ModeError("bad_kinds", f"kinds must be from: {', '.join(KINDS)}.")
        if mode == "types" and not kinds:
            raise ModeError("kinds_required", "mode 'types' needs kinds, e.g. [\"decision\"].")
        if mode != "types":
            kinds = []
    scope = _scope(project_id)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn(db_path)
    try:
        if not available(conn):
            raise ModeError("review_mode_unavailable",
                            "Review mode isn't set up in this database yet (migration 004).")
        conn.execute("BEGIN IMMEDIATE")
        old = _row(conn, scope)
        if clear:
            conn.execute("DELETE FROM review_settings WHERE scope = ?", (scope,))
            new_mode, new_kinds = None, None
        else:
            conn.execute(
                """INSERT INTO review_settings (scope, mode, kinds, updated_at, updated_by)
                   VALUES (?, ?, ?, ?, ?) ON CONFLICT(scope) DO UPDATE SET mode = excluded.mode,
                   kinds = excluded.kinds, updated_at = excluded.updated_at,
                   updated_by = excluded.updated_by""",
                (scope, mode, json.dumps(kinds), now, client[:80]))
            new_mode, new_kinds = mode, json.dumps(kinds)
        log_id = identity.insert(conn, "review_mode_log", {
            "scope": scope, "old_mode": old["mode"] if old else None,
            "old_kinds": json.dumps(old["kinds"]) if old else None, "new_mode": new_mode,
            "new_kinds": new_kinds, "client": client[:80], "caller": (caller or "")[:80],
            "user_request": user_request[:500], "changed_at": now}, client).lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    eff = get(db_path, project_id)
    return {"scope": scope, "old": ({"mode": old["mode"], "kinds": old["kinds"]} if old else None),
            "now": eff, "text": describe(eff), "logged": True, "log_id": log_id}


def history(db_path: Path, limit: int = 20) -> list[dict]:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        return [dict(r) for r in conn.execute(
            "SELECT * FROM review_mode_log ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        conn.close()


def queue_if_off(db_path: Path, mode: Optional[str], kinds, project_id: Optional[int],
                 user_request: str, client: str, clear: bool = False) -> Optional[int]:
    """Approvals on: a change that turns review off (mode off, or clearing a project's
    override when the install is off) becomes a user-only review item. Returns its id, or
    None when the change may go ahead as usual."""
    if not (user_request or "").strip():
        return None   # set_mode refuses it as before
    if clear:
        if project_id is None or get(db_path, None)["mode"] != "off":
            return None
    elif mode != "off":
        return None
    if get(db_path, project_id)["mode"] == "off":
        return None   # already off: nothing to turn off
    from . import review
    pid = project_id
    if pid is None:   # an install-wide change is filed under the most recently updated project
        conn = _conn(db_path)
        try:
            row = conn.execute("SELECT id FROM projects ORDER BY COALESCE(updated_at, created_at) "
                               "DESC, id DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        pid = row[0]
    where = "the whole install" if project_id is None else "this project"
    return review.add_item(db_path, pid, "setting", f"Turn review off for {where}",
                           "Approve to turn review off (nothing waits for review then); reject to "
                           "keep it as it is.",
                           {"scope": _scope(project_id), "mode": None if clear else mode,
                            "kinds": [], "clear": bool(clear), "user_request": user_request[:500]},
                           client)


def apply_queued(db_path: Path, item: dict, client: str) -> tuple[str, int]:
    """Make a review-setting change the user approved (queue_if_off)."""
    p = json.loads(item.get("payload") or "{}")
    scope = p.get("scope") or "install"
    pid = None if scope == "install" else int(scope.split(":", 1)[1])
    out = set_mode(db_path, p.get("mode"), p.get("kinds"), pid,
                   p.get("user_request") or "approved by the user", client, "user approval",
                   clear=bool(p.get("clear")))
    return "review_mode_log", out["log_id"]
