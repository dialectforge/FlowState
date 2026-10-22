"""Review queue: items that wait for the user's OK before they count as records.

Sources: summaries of automatically closed sessions, records an agent logs with
suggested=true, imports, clean-up suggestions and delete requests (deletes.py). Approve turns
an item into a normal record (optionally edited first), or applies it; reject keeps the
item, marked rejected, with the user's reason, and changes nothing else.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import dbconn
from . import identity

AUTO_TAG = "[auto-captured, needs review]"
RECORD_KINDS = ("decision", "change", "problem", "learning", "todo")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='review_items'").fetchone() is not None


class ReviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _need_table(conn):
    if not available(conn):
        raise ReviewError("review_queue_unavailable",
                          "The review queue isn't set up in this database yet (migration 002 "
                          "has not run). Carry on; tell the user to restart FlowState.")


def add_item(db_path: Path, project_id: int, kind: str, summary: str,
             details: Optional[str] = None, payload: Optional[dict] = None,
             source: Optional[str] = None, session_id: Optional[int] = None,
             conn: Optional[sqlite3.Connection] = None,
             ident: Optional["identity.Identity"] = None) -> Optional[int]:
    """Queue an item. Returns its id (None if the queue table doesn't exist). The current
    caller is recorded as who queued it, unless `ident` says otherwise."""
    own = conn is None
    conn = conn or _conn(db_path)
    try:
        if not available(conn):
            return None
        cur = identity.insert(conn, "review_items", {
            "project_id": project_id, "kind": kind, "summary": summary, "details": details,
            "payload": json.dumps(payload or {}), "source": source, "session_id": session_id,
            "created_at": _now()}, source, ident=ident)
        if own:
            conn.commit()
        return cur.lastrowid
    finally:
        if own:
            conn.close()


def pending_count(db_path: Path, project_id: int) -> Optional[int]:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return None
        return conn.execute("SELECT COUNT(*) FROM review_items WHERE project_id = ? "
                            "AND status = 'pending'", (project_id,)).fetchone()[0]
    finally:
        conn.close()


def list_items(db_path: Path, project_id: Optional[int] = None, status: str = "pending",
               limit: int = 20, after: Optional[int] = None) -> dict:
    """Oldest first, at most `limit` (max 100); `after` an item id: the page after it.
    `total` counts every match; `more` says whether items follow this page."""
    conn = _conn(db_path)
    try:
        _need_table(conn)
        where, params = ["1=1"], []
        if project_id is not None:
            where.append("r.project_id = ?")
            params.append(project_id)
        if status != "all":
            where.append("r.status = ?")
            params.append(status)
        total = conn.execute(
            f"SELECT COUNT(*) FROM review_items r WHERE {' AND '.join(where)}",
            params).fetchone()[0]
        if after:
            where.append("r.id > ?")
            params.append(int(after))
        limit = max(1, min(int(limit or 20), 100))
        rows = conn.execute(
            f"""SELECT r.id, r.project_id, p.name AS project, r.kind, r.status, r.summary,
                       r.details, r.source, r.session_id, r.created_at, r.decided_at,
                       r.reject_reason, r.result_table, r.result_id, r.payload
                FROM review_items r JOIN projects p ON p.id = r.project_id
                WHERE {' AND '.join(where)} ORDER BY r.id LIMIT ?""",
            (*params, limit + 1)).fetchall()
        return {"items": [dict(r) for r in rows[:limit]], "total": total, "status": status,
                "more": len(rows) > limit,
                "note": "Only approve or reject when the user explicitly asks you to."}
    finally:
        conn.close()


def get_item(db_path: Path, item_id: int) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        _need_table(conn)
        row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in (text or "")).split())


def _own(item: Optional[dict], ident) -> bool:
    """Did this caller's app (on the same machine, when recorded) queue the item?"""
    if not item or ident is None or not item.get("app") or item["app"] != ident.app:
        return False
    return not (item.get("machine") and ident.machine and item["machine"] != ident.machine)


def own_item_refusal(item: Optional[dict], ident, user_request: str,
                     db_path: Optional[Path] = None) -> Optional[dict]:
    """The app that queued an item (same app, and same machine when recorded) decides it only
    with the user's own words (two words or more, not the item's own text). Anyone else
    relaying the user is checked as before. None = allowed. `ident` is None on the command
    line, the review page, the popup and the phone page (the user). Items only the user's own
    click or key decides (approvals on) are handled before this (approvals.user_only)."""
    if not _own(item, ident):
        return None
    words = _norm(user_request)
    own = " ".join(_norm(item.get(k) or "") for k in ("summary", "details"))
    if len(words.split()) >= 2 and words not in own:
        return None
    return {"error": "own_item",
            "message": f"You queued review item #{item['id']}, so you can't decide it yourself. "
                       f"Ask the user; pass their exact words as user_request."}


def _claim(db_path: Path, item_id: int, status: str, client: str, user_request: str,
           reason: Optional[str] = None) -> dict:
    conn = _conn(db_path)
    try:
        _need_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            conn.rollback()
            raise ReviewError("record_not_found", f"No review item with id {item_id}.")
        if row["status"] != "pending":
            conn.rollback()
            raise ReviewError("already_decided",
                              f"Review item #{item_id} is already {row['status']}.")
        sets, who = identity.set_sql(conn, "review_items", client, prefix="decided_")
        conn.execute(f"""UPDATE review_items SET status = ?, decided_at = ?, decided_by = ?,
                        user_request = ?, reject_reason = ?{sets} WHERE id = ?""",
                     (status, _now(), client[:80], user_request[:500], reason, *who, item_id))
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def _unclaim(db_path: Path, item_id: int) -> None:
    conn = _conn(db_path)
    try:
        sets, _ = identity.set_sql(conn, "review_items", prefix="decided_", clear=True)
        conn.execute(f"""UPDATE review_items SET status = 'pending', decided_at = NULL,
                        decided_by = NULL, user_request = NULL{sets} WHERE id = ?""", (item_id,))
        conn.commit()
    finally:
        conn.close()


def _set_session(db_path: Path, session_id: int, summary: str, decision: str,
                 item_id: int) -> None:
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT outcomes FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return
        try:
            out = json.loads(row["outcomes"] or "{}")
            out = out if isinstance(out, dict) else {"previous": out}
        except ValueError:
            out = {"previous": row["outcomes"]}
        out.update({"needs_review": False, "review": decision, "review_item_id": item_id})
        conn.execute("UPDATE sessions SET summary = ?, outcomes = ? WHERE id = ?",
                     (summary, json.dumps(out), session_id))
        conn.commit()
    finally:
        conn.close()


def strip_tag(text: str) -> str:
    text = (text or "").strip()
    return text[len(AUTO_TAG):].strip() if text.startswith(AUTO_TAG) else text


def approve(db_path: Path, item_id: int, user_request: str, client: str,
            apply: Callable[[dict, dict], tuple[str, Optional[int]]],
            edits: Optional[dict] = None, others: Optional[dict] = None) -> dict:
    """Approve (optionally after editing summary/details). `apply(item, payload)` creates
    the record for log_work kinds and returns (table, id). `others` maps further item kinds
    (cleanup, edit) to `fn(item, edits) -> (table, id)`."""
    edits = {k: v for k, v in (edits or {}).items() if v not in (None, "")}
    item = _claim(db_path, item_id, "approved", client, user_request)
    try:
        summary = (edits.get("summary") or item["summary"]).strip()
        details = edits.get("details", item["details"])
        if item["kind"] == "session_summary":
            if item["session_id"] is None:
                raise ReviewError("session_missing", "The session for this summary no longer exists.")
            _set_session(db_path, item["session_id"], strip_tag(summary), "approved", item_id)
            table, rid = "sessions", item["session_id"]
        elif item["kind"] in RECORD_KINDS:
            payload = json.loads(item["payload"] or "{}")
            payload.update({"kind": item["kind"], "summary": summary, "details": details})
            for k in ("priority", "category", "component"):
                if edits.get(k):
                    payload[k] = edits[k]
            table, rid = apply(item, payload)
        elif others and item["kind"] in others:
            table, rid = others[item["kind"]](item, edits)
        else:
            raise ReviewError("bad_kind", f"Unknown review item kind {item['kind']!r}.")
    except Exception:
        _unclaim(db_path, item_id)
        raise
    conn = _conn(db_path)
    try:
        conn.execute("""UPDATE review_items SET summary = ?, details = ?, result_table = ?,
                        result_id = ? WHERE id = ?""", (summary, details, table, rid, item_id))
        conn.commit()
    finally:
        conn.close()
    return {"approved": item_id, "kind": item["kind"], "edited": bool(edits),
            "record": {"table": table, "id": rid}}


def reject(db_path: Path, item_id: int, reason: str, user_request: str, client: str) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise ReviewError("reason_required", "Rejecting needs a short reason from the user.")
    item = _claim(db_path, item_id, "rejected", client, user_request, reason[:500])
    if item["kind"] == "session_summary" and item["session_id"] is not None:
        # The text stays in the review item; the session says what happened to it.
        _set_session(db_path, item["session_id"],
                     f"[auto-captured summary rejected: {reason[:200]}; kept as review item "
                     f"#{item_id}]", "rejected", item_id)
    return {"rejected": item_id, "kind": item["kind"], "reason": reason[:500],
            "note": "Kept in the review queue as rejected; nothing was deleted."}
