"""Who did what: one list of recent recorded changes across FlowState's logs (v2, after
migration 012), newest first, with the app, model, machine and identity source of each.

Sources: edit history (edits and undos), the review queue (queued items, approvals,
rejections), links (added / removed), handoff notes (left / picked up), hard rules (added /
removed), review-mode changes, records created by a write (activity log), and todo claims
(claim, renew, release, handoff, override, cleared; migration 013), and possible conflicting
decisions (flagged, resolved by a replacing decision, dismissed as compatible; 014). Rows written
before migration 012 show the app name the older columns kept and "not recorded" for the
rest.

Used by the review page ("Who did what") and, read-only, by history(action="who").
Filters: app, model, machine (each: contains, any case), project id, since / until (local
time of this computer: "YYYY-MM-DD" or "YYYY-MM-DD HH:MM").
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
from . import history, identity

PER_PAGE = 50
MAX_PER_PAGE = 100
_ID = ("app", "model", "machine", "identity_source")


class WhoError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _tables(conn) -> dict:
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return {n: {r[1] for r in conn.execute(f'PRAGMA table_info("{n}")')} for n in names}


def _id_cols(cols: set, prefix: str = "", fallback_app: str = "NULL") -> str:
    """app, model, machine, identity_source expressions for one source (NULL before 012)."""
    out = []
    for f in _ID:
        c = prefix + f
        expr = c if c in cols else "NULL"
        if f == "app":
            expr = f"COALESCE({expr}, {fallback_app})" if expr != "NULL" else fallback_app
        out.append(f"{expr} AS {f}")
    return ", ".join(out)


def _sel(at: str, ids: str, project: str, rtype: str, rid: str, what: str, kind: str,
         extra: str, rest: str) -> str:
    return (f"SELECT {at} AS at, {ids}, {project} AS project_id, {rtype} AS record_type, "
            f"{rid} AS record_id, {what} AS what, '{kind}' AS kind, id AS ref, "
            f"{extra} AS extra {rest}")


def _sources(t: dict) -> list[str]:
    """One SELECT per kind of event, all with the same columns:
    at, app, model, machine, identity_source, project_id, record_type, record_id, what,
    kind, ref, extra."""
    q = []
    if "record_history" in t:
        c = t["record_history"]
        q.append(_sel("created_at", _id_cols(c, fallback_app="client"), "project_id",
                      "table_name", "record_id", "tool", "edit", "undone_at",
                      # a creation is listed once, from the activity log (and the review queue)
                      "FROM record_history WHERE NOT (old_row IS NULL AND (tool LIKE 'create/%' "
                      "OR tool LIKE 'approve/%'))"))
    if "review_items" in t:
        c = t["review_items"]
        q.append(_sel("created_at", _id_cols(c, fallback_app="source"), "project_id",
                      "'review_item'", "id",
                      "'queued ' || kind || ' for review: ' || substr(summary, 1, 60)",
                      "queued", "NULL", "FROM review_items"))
        q.append(_sel("decided_at", _id_cols(c, "decided_", "decided_by"), "project_id",
                      "'review_item'", "id",
                      "status || ' review item #' || id || CASE WHEN result_table IS NOT NULL "
                      "AND result_id IS NOT NULL THEN ' (became ' || result_table || ' #' "
                      "|| result_id || ')' ELSE '' END",
                      "decided", "NULL", "FROM review_items WHERE decided_at IS NOT NULL"))
    if "record_links" in t:
        c = t["record_links"]
        target = "COALESCE(url, to_type || ' #' || to_id)"
        q.append(_sel("created_at", _id_cols(c, fallback_app="created_by"), "from_project_id",
                      "'link'", "id",
                      f"'linked ' || from_type || ' #' || from_id || ' ' || link_type || ' ' "
                      f"|| {target}", "link_added", "NULL", "FROM record_links"))
        q.append(_sel("removed_at", _id_cols(c, "removed_", "removed_by"), "from_project_id",
                      "'link'", "id",
                      f"'removed link #' || id || ' (' || link_type || ' ' || {target} || ')'",
                      "link_removed", "NULL", "FROM record_links WHERE removed_at IS NOT NULL"))
    if "handoff_notes" in t:
        c = t["handoff_notes"]
        q.append(_sel("created_at", _id_cols(c, fallback_app="created_by"), "project_id",
                      "'handoff'", "id", "'left handoff: ' || substr(where_i_was, 1, 60)",
                      "handoff_left", "NULL", "FROM handoff_notes"))
        q.append(_sel("picked_up_at", _id_cols(c, "picked_up_", "picked_up_by"), "project_id",
                      "'handoff'", "id", "'picked up handoff H' || id", "handoff_picked_up",
                      "NULL", "FROM handoff_notes WHERE picked_up_at IS NOT NULL"))
    if "project_rules" in t:
        c = t["project_rules"]
        q.append(_sel("created_at", _id_cols(c, fallback_app="created_by"), "project_id",
                      "'rule'", "id", "'added hard rule: ' || substr(rule, 1, 60)",
                      "rule_added", "NULL", "FROM project_rules"))
        q.append(_sel("removed_at", _id_cols(c, "removed_", "removed_by"), "project_id",
                      "'rule'", "id", "'removed hard rule R' || id", "rule_removed", "NULL",
                      "FROM project_rules WHERE removed_at IS NOT NULL"))
    if "review_mode_log" in t:
        c = t["review_mode_log"]
        q.append(_sel("changed_at", _id_cols(c, fallback_app="client"),
                      "CASE WHEN scope LIKE 'project:%' THEN CAST(substr(scope, 9) AS INTEGER) END",
                      "'review_mode'", "id",
                      "'review mode ' || COALESCE(new_mode, 'override cleared') || ' (' || scope "
                      "|| ')'", "mode", "NULL", "FROM review_mode_log"))
    if "activity_log" in t and "record_type" in t["activity_log"]:
        c = t["activity_log"]
        q.append(_sel("at", _id_cols(c, fallback_app="app"), "project_id", "record_type",
                      "record_id",
                      "'created ' || record_type || ' #' || record_id || ' (' || tool "
                      "|| COALESCE('/' || action, '') || ')'", "created", "NULL",
                      "FROM activity_log WHERE record_type IS NOT NULL AND ok = 1"))
    if "todo_claim_events" in t:
        c = t["todo_claim_events"]
        q.append(f"SELECT at, {_id_cols(c)}, project_id, 'todo' AS record_type, todo_id AS "
                 f"record_id, summary AS what, 'todo_' || event AS kind, id AS ref, NULL AS "
                 f"extra FROM todo_claim_events")
    if "decision_conflicts" in t:
        c = t["decision_conflicts"]
        pair = "'D' || new_id || ' vs D' || old_id"
        q.append(_sel("created_at", _id_cols(c), "project_id", "'decision'", "new_id",
                      f"'possible conflict flagged: ' || {pair} || ' (score ' || "
                      f"printf('%.2f', score) || ')'", "conflict_flagged", "NULL",
                      "FROM decision_conflicts"))
        q.append(_sel("resolved_at", _id_cols(c, "resolved_"), "project_id", "'decision'",
                      "new_id",
                      f"'possible conflict ' || {pair} || CASE status WHEN 'resolved_replaced' "
                      f"THEN ' resolved: D' || resolved_by_decision || ' replaced one' "
                      f"ELSE ' dismissed: the user said they are compatible' END",
                      "conflict_resolved", "NULL",
                      "FROM decision_conflicts WHERE resolved_at IS NOT NULL"))
    return q


def parse_time(value, end: bool = False) -> Optional[str]:
    """Local "YYYY-MM-DD[ HH:MM[:SS]]" -> UTC "YYYY-MM-DD HH:MM:SS" (how FlowState stores
    times). A bare date as `end` means the end of that day."""
    if value in (None, ""):
        return None
    text = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt == "%Y-%m-%d" and end:
            dt += timedelta(days=1, seconds=-1)
        return dt.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    raise WhoError("bad_time", f"Times look like 2026-10-17 or 2026-10-17 14:30 (local time), "
                               f"not {value!r}.")


def local_time(utc: Optional[str]) -> str:
    if not utc:
        return ""
    try:
        dt = datetime.strptime(utc[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return utc
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _undoable(conn, row: dict) -> bool:
    """The edit history's own rule: the last not-undone edit of a record can be undone."""
    if row["kind"] != "edit" or row["extra"] or row["project_id"] is None \
            or row["record_type"] not in history.TYPE_OF:
        return False
    last = conn.execute("SELECT MAX(id) FROM record_history WHERE table_name = ? AND "
                        "record_id = ? AND undone_at IS NULL",
                        (row["record_type"], row["record_id"])).fetchone()[0]
    return last == row["ref"]


def _edit_what(conn, row: dict) -> str:
    h = conn.execute("SELECT old_row, new_row, undo_of FROM record_history WHERE id = ?",
                     (row["ref"],)).fetchone()
    if h is None:
        return row["what"] or ""
    old = json.loads(h["old_row"]) if h["old_row"] else None
    new = json.loads(h["new_row"]) if h["new_row"] else None
    ch = history._changed_fields(old, new)
    what = "created" if ch.get("created") else "deleted" if ch.get("deleted") else \
        ", ".join(sorted(ch)) or "no field changes"
    out = f"{row['what']}: {what}"
    if row["extra"]:
        out += " (undone)"
    return out[:120]


_TYPE_OF_EVENT = {"link_added": "link", "link_removed": "link", "handoff_left": "handoff",
                  "handoff_picked_up": "handoff", "rule_added": "rule", "rule_removed": "rule",
                  "queued": "review_item", "decided": "review_item", "mode": "review_mode_change"}


def _session_of(conn, row: dict, rtype: Optional[str], has: dict) -> Optional[int]:
    """The automatic session (sessions.id) behind a row, from the 2.4 run ledger, so two
    windows of one app are told apart. None when it wasn't recorded (before 2.4)."""
    if "run_writes" not in has:
        return None
    if row["kind"] == "edit":
        r = conn.execute("SELECT w.session_id FROM record_history h JOIN run_writes w "
                         "ON w.change_id = h.edit_id WHERE h.id = ?", (row["ref"],)).fetchone()
        return r[0] if r else None
    if row["kind"] == "queued" and "session_id" in has.get("review_items", ()):
        r = conn.execute("SELECT session_id FROM review_items WHERE id = ?", (row["ref"],)).fetchone()
        if r and r[0]:
            return r[0]
    if row["kind"].startswith("todo_") and "session_runs" in has:
        r = conn.execute(
            "SELECT r.session_id FROM todo_claim_events e JOIN session_runs r ON r.conn_key = e.session "
            "JOIN auto_sessions a ON a.session_id = r.session_id AND a.project_id IS e.project_id "
            "WHERE e.id = ? ORDER BY r.session_id DESC LIMIT 1", (row["ref"],)).fetchone()
        return r[0] if r else None
    kind = _TYPE_OF_EVENT.get(row["kind"], rtype)
    if not kind or row["record_id"] is None:
        return None
    r = conn.execute("SELECT session_id FROM run_writes WHERE status = 'saved' AND records LIKE ? "
                     "ORDER BY abs(julianday(finished_at) - julianday(?)) LIMIT 1",
                     (f'%"{kind}:{row["record_id"]}"%', row["at"])).fetchone()
    return r[0] if r else None


def recent(db_path: Path, app: Optional[str] = None, model: Optional[str] = None,
           machine: Optional[str] = None, project_id: Optional[int] = None,
           since=None, until=None, page: int = 1, per_page: int = PER_PAGE) -> dict:
    """{"rows": [...], "total": n, "page": p, "pages": n, "per_page": k}."""
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or PER_PAGE), MAX_PER_PAGE))
    since_u, until_u = parse_time(since), parse_time(until, end=True)
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        has = _tables(conn)
        parts = _sources(has)
        if not parts:
            return {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page}
        where, params = ["u.at IS NOT NULL"], []
        for col, val in (("app", app), ("model", model), ("machine", machine)):
            if val not in (None, ""):
                where.append(f"instr(lower(COALESCE(u.{col}, '')), lower(?)) > 0")
                params.append(str(val).strip())
        if project_id is not None:
            where.append("u.project_id = ?")
            params.append(int(project_id))
        if since_u:
            where.append("u.at >= ?")
            params.append(since_u)
        if until_u:
            where.append("u.at <= ?")
            params.append(until_u)
        union = " UNION ALL ".join(parts)
        cond = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM ({union}) u WHERE {cond}", params).fetchone()[0]
        rows = [dict(r) for r in conn.execute(
            f"""SELECT u.*, p.name AS project FROM ({union}) u
                LEFT JOIN projects p ON p.id = u.project_id WHERE {cond}
                ORDER BY u.at DESC, u.kind, u.ref DESC LIMIT ? OFFSET ?""",
            (*params, per_page, (page - 1) * per_page))]
        out = []
        for r in rows:
            undo = _undoable(conn, r)
            what = _edit_what(conn, r) if r["kind"] == "edit" else (r["what"] or "")
            rtype = history.TYPE_OF.get(r["record_type"], r["record_type"]) \
                if r["kind"] == "edit" else r["record_type"]
            out.append({"at": local_time(r["at"]), "at_utc": r["at"], "app": r["app"],
                        "model": r["model"], "machine": r["machine"],
                        "identity_source": r["identity_source"],
                        "identity": identity.label(r["identity_source"]),
                        "session": _session_of(conn, r, rtype, has),
                        "project_id": r["project_id"], "project": r["project"],
                        "record_type": rtype, "record_id": r["record_id"],
                        "what": " ".join(what.split())[:120], "event": r["kind"],
                        "undo": {"record_type": rtype, "record_id": r["record_id"],
                                 "project_id": r["project_id"]} if undo else None})
        return {"rows": out, "total": total, "page": page, "per_page": per_page,
                "pages": max(1, -(-total // per_page))}
    finally:
        conn.close()
