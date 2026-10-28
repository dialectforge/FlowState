"""Project history (task 73): one project's whole path in time order, for the local review
page's "Project history" view (history_page.py). Read-only. No MCP tool, no relay route and
no tunnel route use it.

Entries come from who.py's sources (handoffs, hard rules, review-mode changes, todo claims)
and from these:
- every edit-history row: created, edited, deleted, undone (kept until `flowstate erase`);
- records the edit history doesn't know were created: older ones (before 2.4.0, "not
  recorded" unless the activity log still knows the app) and ones only the activity log
  names (deleted before the edit history knew them, or erased);
- review items queued and decided, links added and removed (a decision replacing another
  is a `supersedes` link), possible conflicts, disagreement cards, automatic sessions;
- a todo claim that ran out with nobody releasing it (worked out from the claim events).

Every entry has its time, app, model (only when the app gave it), machine and how the app
signed in, and names a record (`record_type`, `record_id`) and sometimes a second one (the
record a link points to, the record an approved review item became).

Erased records (`flowstate erase`): their rows and their edit history are gone. An entry
that still names one shows `[erased]` as its text; search and the record view find nothing
of it. The activity log and the session ledger fade after FLOWSTATE_ACTIVITY_DAYS (90):
`faded` says when the shown range reaches past that.
"""

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import activity, dbconn, history, identity, who

PER_PAGE = 50
MAX_PER_PAGE = 100
MAX_EDITS = 200     # edit-history rows shown on one record's page
FIELD_WIDTH = 2000  # longest value shown for one field, before and after
ERASED = "[erased]"
_TYPE_OF = dict(history.TYPE_OF, decision_conflicts="conflict", review_items="review_item")
_SIDE = tuple(history.SIDE)
# created by their own source below (who.py's, or the links source): not again from history
_OWN_CREATE = ("record_links", "project_rules", "handoff_notes")
# records with a creation time of their own: (type, table, project expression, join)
_CREATED = (("todo", "todos", "x.project_id", ""),
            ("decision", "conversations", "x.project_id", ""),
            ("learning", "learnings", "x.project_id", ""),
            ("component", "components", "x.project_id", ""),
            ("change", "changes", "c.project_id", "JOIN components c ON c.id = x.component_id"),
            ("problem", "problems", "c.project_id", "JOIN components c ON c.id = x.component_id"))
_OWN_TYPES = ("project", "rule", "handoff", "link")
# the text that names a record (live row or a stored copy in the edit history)
_LABEL = {"todos": ("title",), "conversations": ("key_decisions", "user_prompt_summary"),
          "changes": ("field_name",), "problems": ("title",), "learnings": ("insight",),
          "components": ("name",), "projects": ("name",), "project_rules": ("rule",),
          "handoff_notes": ("where_i_was",), "solutions": ("summary", "description"),
          "solution_attempts": ("description",), "attachments": ("file_name", "name"),
          "project_variables": ("name",), "project_methods": ("name",),
          "review_items": ("summary",)}
# searched in live rows: table -> (columns, project expression, join)
_SEARCH = {"todos": (("title", "description"), "x.project_id", ""),
           "conversations": (("user_prompt_summary", "assistant_response_summary",
                              "key_decisions"), "x.project_id", ""),
           "learnings": (("insight", "context"), "x.project_id", ""),
           "components": (("name", "description"), "x.project_id", ""),
           "changes": (("field_name", "reason", "old_value", "new_value"), "c.project_id",
                       "JOIN components c ON c.id = x.component_id"),
           "problems": (("title", "description"), "c.project_id",
                        "JOIN components c ON c.id = x.component_id"),
           "project_rules": (("rule",), "x.project_id", ""),
           "handoff_notes": (("where_i_was", "next_step"), "x.project_id", ""),
           "review_items": (("summary", "details"), "x.project_id", "")}
TODO_OPEN = ("pending", "in_progress", "blocked")
TODO_STATUSES = ("open", "done", "cancelled", "deleted", "all")
_MINUTES = re.compile(r"(?:for |\()(\d{1,4})m(?:\)|\b)")


class HistoryPageError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _claim_minutes(summary) -> Optional[int]:
    """How long a claim or renewal was for, from its event text ("... for 15m", "(15m)")."""
    m = _MINUTES.findall(str(summary or ""))
    return int(m[-1]) if m else None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.create_function("claim_minutes", 1, _claim_minutes, deterministic=True)
    if "record_type" in who._tables(conn).get("activity_log", ()):
        # who created what, while the activity log keeps it: one scan, then indexed (a temp
        # table: this connection's own, never in the database file)
        conn.execute("CREATE TEMP TABLE created_by AS SELECT record_type, record_id, MIN(id) "
                     "AS id FROM activity_log WHERE record_type IS NOT NULL AND ok = 1 GROUP BY "
                     "record_type, record_id")
        conn.execute("CREATE INDEX temp.created_by_rec ON created_by(record_type, record_id)")
    conn.execute("PRAGMA query_only = ON")   # this page never writes
    return conn


def _type_case(col: str) -> str:
    return "CASE " + col + " " + " ".join(f"WHEN '{t}' THEN '{k}'" for t, k in _TYPE_OF.items()) \
        + f" ELSE {col} END"


def _table_case(col: str) -> str:
    return "CASE " + col + " " + " ".join(f"WHEN '{k}' THEN '{t}'" for k, t in history.TABLES.items()) \
        + f" ELSE {col} END"


def _ids(cols: set, prefix: str = "", fallback: str = "NULL") -> str:
    return who._id_cols(cols, prefix, fallback)


_NO2 = "NULL AS rt2, NULL AS rid2, NULL AS pid2"


def _sources(conn, t: dict) -> list[str]:
    """One SELECT per kind of entry, all with the same columns: at, app, model, machine,
    identity_source, project_id, record_type, record_id, what, kind, ref, extra, rt2, rid2,
    pid2 (a second record and project the entry is about)."""
    q = [f"SELECT w.*, {_NO2} FROM ({s}) w" for s in who._sources(
        {k: v for k, v in t.items() if k in ("handoff_notes", "project_rules", "review_mode_log",
                                             "todo_claim_events")})]
    if "record_history" in t:
        c = t["record_history"]
        json_of = "CASE WHEN json_valid(COALESCE(h.new_row, h.old_row)) THEN json_extract(" \
                  "COALESCE(h.new_row, h.old_row), '$.{}') END"
        side = ", ".join(f"'{s}'" for s in _SIDE)
        q.append(
            f"SELECT h.created_at AS at, {_ids(c, fallback='client')}, "
            f"h.project_id, CASE WHEN h.table_name IN ({side}) THEN {json_of.format('record_type')} "
            f"ELSE {_type_case('h.table_name')} END AS record_type, "
            f"CASE WHEN h.table_name IN ({side}) THEN {json_of.format('record_id')} ELSE h.record_id "
            f"END AS record_id, h.tool AS what, CASE WHEN h.old_row IS NULL AND h.tool != 'history/undo' "
            f"AND h.table_name NOT IN ({side}) THEN 'edit_created' ELSE 'edit' END AS kind, h.id AS ref, h.undone_at AS extra, "
            f"{_NO2} FROM record_history h WHERE NOT (h.old_row IS NULL AND h.table_name IN "
            f"({', '.join(repr(x) for x in _OWN_CREATE)}))")
    rh = "record_history" in t
    made = ("EXISTS (SELECT 1 FROM record_history h WHERE h.table_name = '{table}' AND "
            "h.record_id = {rid} AND h.old_row IS NULL)") if rh else "0"
    act = "activity_log" in t and "record_type" in t["activity_log"]
    ac = t["activity_log"] if act else set()   # (created_by: _connect)
    who_cols = ", ".join(f"a.{f} AS {f}" if f in ac else f"NULL AS {f}" for f in who._ID)
    for rtype, table, proj, join in _CREATED:
        if table not in t or "created_at" not in t[table]:
            continue
        q.append(
            f"SELECT x.created_at AS at, {who_cols}, {proj} AS project_id, '{rtype}' AS record_type, "
            f"x.id AS record_id, 'created {rtype} #' || x.id AS what, 'created' AS kind, x.id AS ref, "
            f"NULL AS extra, {_NO2} FROM {table} x {join} "
            + (f"LEFT JOIN created_by cb ON cb.record_type = '{rtype}' AND cb.record_id = x.id "
               f"LEFT JOIN activity_log a ON a.id = cb.id " if act else "")
            + f"WHERE x.created_at IS NOT NULL AND NOT {made.format(table=table, rid='x.id')}")
    if act:
        # creations only the activity log names: the record is gone (deleted before the edit
        # history knew it, or erased) or has no creation time of its own
        mapped = {r: tb for r, tb, _p, _j in _CREATED if tb in t}
        gone = " AND ".join(
            f"NOT (a.record_type = '{r}' AND EXISTS (SELECT 1 FROM {tb} z WHERE z.id = a.record_id))"
            for r, tb in mapped.items()) or "1"
        hist = (f"NOT EXISTS (SELECT 1 FROM record_history h WHERE h.table_name = "
                f"{_table_case('a.record_type')} AND h.record_id = a.record_id AND h.old_row IS NULL)"
                if rh else "1")
        q.append(
            f"SELECT a.at, {who_cols}, "
            f"a.project_id, a.record_type, a.record_id, 'created ' || a.record_type || ' #' || "
            f"a.record_id AS what, 'created' AS kind, a.id AS ref, NULL AS extra, {_NO2} "
            f"FROM created_by cb JOIN activity_log a ON a.id = cb.id WHERE a.record_type NOT IN "
            f"({', '.join(repr(x) for x in _OWN_TYPES)}) AND {gone} AND {hist}")
    if "projects" in t:
        q.append(
            f"SELECT p.created_at AS at, {who_cols}"
            ", p.id AS project_id, 'project' AS record_type, p.id AS record_id, 'project created' "
              f"AS what, 'created' AS kind, p.id AS ref, NULL AS extra, {_NO2} FROM projects p "
            + ("LEFT JOIN created_by cb ON cb.record_type = 'project' AND cb.record_id = p.id "
               "LEFT JOIN activity_log a ON a.id = cb.id" if act else ""))
    if "review_items" in t:
        c = t["review_items"]
        res = (f"{_type_case('result_table')}", "result_id")
        q.append(
            f"SELECT created_at AS at, {_ids(c, fallback='source')}, project_id, 'review_item' AS "
            f"record_type, id AS record_id, 'queued ' || kind || ' for review: ' || "
            f"substr(summary, 1, 80) AS what, 'queued' AS kind, id AS ref, NULL AS extra, "
            f"CASE WHEN kind = 'delete' AND json_valid(payload) THEN json_extract(payload, "
            f"'$.record_type') END AS rt2, CASE WHEN kind = 'delete' AND json_valid(payload) THEN "
            f"json_extract(payload, '$.record_id') END AS rid2, NULL AS pid2 FROM review_items")
        q.append(
            f"SELECT decided_at AS at, {_ids(c, 'decided_', 'decided_by')}, project_id, "
            f"'review_item' AS record_type, id AS record_id, status || ' review item #' || id || "
            f"' (' || kind || ')' || CASE WHEN result_table IS NOT NULL AND result_id IS NOT NULL "
            f"THEN ', became ' || {res[0]} || ' #' || result_id ELSE '' END AS what, 'decided' AS "
            f"kind, id AS ref, NULL AS extra, {res[0]} AS rt2, {res[1]} AS rid2, NULL AS pid2 "
            f"FROM review_items WHERE decided_at IS NOT NULL")
    if "record_links" in t:
        c = t["record_links"]
        target = "COALESCE(url, to_type || ' #' || to_id)"
        q.append(
            f"SELECT created_at AS at, {_ids(c, fallback='created_by')}, from_project_id AS "
            f"project_id, from_type AS record_type, from_id AS record_id, from_type || ' #' || "
            f"from_id || ' ' || link_type || ' ' || {target} AS what, 'link_added' AS kind, id AS "
            f"ref, link_type AS extra, to_type AS rt2, to_id AS rid2, to_project_id AS pid2 "
            f"FROM record_links")
        q.append(
            f"SELECT removed_at AS at, {_ids(c, 'removed_', 'removed_by')}, from_project_id AS "
            f"project_id, from_type AS record_type, from_id AS record_id, 'removed link: ' || "
            f"from_type || ' #' || from_id || ' ' || link_type || ' ' || {target} AS what, "
            f"'link_removed' AS kind, id AS ref, link_type AS extra, to_type AS rt2, to_id AS rid2, "
            f"to_project_id AS pid2 FROM record_links WHERE removed_at IS NOT NULL")
    if "decision_conflicts" in t:
        c = t["decision_conflicts"]
        pair = "'decision #' || new_id || ' and decision #' || old_id"
        q.append(
            f"SELECT created_at AS at, {_ids(c)}, project_id, 'decision' AS record_type, new_id AS "
            f"record_id, 'possible conflict flagged: ' || {pair} AS what, 'conflict_flagged' AS "
            f"kind, id AS ref, NULL AS extra, 'decision' AS rt2, old_id AS rid2, NULL AS pid2 "
            f"FROM decision_conflicts")
        q.append(
            f"SELECT resolved_at AS at, {_ids(c, 'resolved_')}, project_id, 'decision' AS "
            f"record_type, new_id AS record_id, 'possible conflict ' || {pair} || CASE status WHEN "
            f"'resolved_replaced' THEN ' resolved: decision #' || resolved_by_decision || "
            f"' replaced one' WHEN 'confirmed' THEN ' confirmed by the user' ELSE ' dismissed: the "
            f"user said they are compatible' END AS what, 'conflict_resolved' AS kind, id AS ref, "
            f"NULL AS extra, 'decision' AS rt2, old_id AS rid2, NULL AS pid2 FROM "
            f"decision_conflicts WHERE resolved_at IS NOT NULL")
    if "conflict_cards" in t:
        c = t["conflict_cards"]
        pair = "a_type || ' #' || a_id || ' and ' || b_type || ' #' || b_id"
        q.append(
            f"SELECT created_at AS at, {_ids(c)}, project_id, a_type AS record_type, a_id AS "
            f"record_id, 'disagreement flagged: ' || {pair} || ' (' || topic || ')' AS what, "
            f"'card_flagged' AS kind, id AS ref, NULL AS extra, b_type AS rt2, b_id AS rid2, NULL "
            f"AS pid2 FROM conflict_cards")
        q.append(
            f"SELECT resolved_at AS at, {_ids(c, 'resolved_')}, project_id, a_type AS record_type, "
            f"a_id AS record_id, 'disagreement ' || {pair} || CASE status WHEN 'kept_a' THEN "
            f"': kept the older one' WHEN 'kept_b' THEN ': kept the newer one' WHEN 'kept_both' "
            f"THEN ': kept both' ELSE ': dismissed' END || COALESCE(' (' || resolved_path || ')', "
            f"'') AS what, 'card_resolved' AS kind, id AS ref, NULL AS extra, b_type AS rt2, b_id "
            f"AS rid2, NULL AS pid2 FROM conflict_cards WHERE resolved_at IS NOT NULL")
    if "todo_claim_events" in t:
        c = t["todo_claim_events"]
        ends = "datetime(x.at, '+' || x.mins || ' minutes')"
        q.append(
            f"SELECT {ends} AS at, {', '.join(f'x.{f} AS {f}' if f in c else f'NULL AS {f}' for f in who._ID)}, "
            f"x.project_id, 'todo' AS record_type, x.todo_id AS record_id, 'claim on todo #' || "
            f"x.todo_id || ' expired (nobody renewed or released it)' AS what, 'todo_expired' AS "
            f"kind, x.id AS ref, NULL AS extra, {_NO2} FROM (SELECT e.*, claim_minutes(e.summary) "
            f"AS mins, LEAD(e.at) OVER (PARTITION BY e.todo_id ORDER BY e.at, e.id) AS next_at, "
            f"LEAD(e.event) OVER (PARTITION BY e.todo_id ORDER BY e.at, e.id) AS next_event "
            f"FROM todo_claim_events e) x WHERE x.event IN ('claim', 'renew') AND x.mins IS NOT "
            f"NULL AND {ends} <= '{_now()}' AND (x.next_at IS NULL OR x.next_at > {ends} OR "
            f"x.next_event = 'claim')")
    if "auto_sessions" in t:
        runs = "session_runs" in t
        ids = ("COALESCE(r.app, s.client) AS app, r.model AS model, r.machine AS machine, "
               "r.identity_source AS identity_source" if runs else
               "s.client AS app, NULL AS model, NULL AS machine, NULL AS identity_source")
        join = "LEFT JOIN session_runs r ON r.session_id = s.session_id" if runs else ""
        q.append(
            f"SELECT s.opened_at AS at, {ids}, s.project_id, 'session' AS record_type, "
            f"s.session_id AS record_id, 'session #' || s.session_id || ' opened' AS what, "
            f"'session_opened' AS kind, s.session_id AS ref, NULL AS extra, {_NO2} "
            f"FROM auto_sessions s {join}")
        q.append(
            f"SELECT s.closed_at AS at, {ids}, s.project_id, 'session' AS record_type, "
            f"s.session_id AS record_id, 'session #' || s.session_id || ' closed (' || "
            f"COALESCE(s.close_reason, 'closed') || ', ' || s.call_count || ' calls)' AS what, "
            f"'session_closed' AS kind, s.session_id AS ref, NULL AS extra, {_NO2} "
            f"FROM auto_sessions s {join} WHERE s.closed_at IS NOT NULL")
    return q


_CAT = ("CASE WHEN u.kind IN ('conflict_flagged', 'conflict_resolved', 'card_flagged', "
        "'card_resolved') THEN 'conflict' WHEN u.kind IN ('queued', 'decided') THEN 'review' "
        "WHEN u.kind IN ('link_added', 'link_removed') THEN 'link' ELSE u.record_type END")
TYPE_WORDS = {"decision": "Decisions", "todo": "Todos", "change": "Changes", "problem": "Problems",
              "learning": "Learnings", "component": "Components", "project": "The project",
              "review": "Review items", "link": "Links (and what replaced what)",
              "conflict": "Conflicts", "session": "Sessions", "rule": "Hard rules",
              "handoff": "Handoffs", "review_mode_change": "Review mode",
              "attempt": "Attempts", "solution": "Solutions", "attachment": "Files"}


def _where(project_id: int, rtype, app, since_u, until_u, keys, record) -> tuple:
    where, params = ["u.at IS NOT NULL", "(u.project_id = ? OR u.pid2 = ?)"], [project_id] * 2
    if rtype:
        where.append(f"({_CAT} = ? OR u.record_type = ?)")
        params += [rtype, rtype]
    if app not in (None, ""):
        where.append("(instr(lower(COALESCE(u.app, '')), lower(?)) > 0 OR instr(lower("
                     "COALESCE(u.machine, '')), lower(?)) > 0)")
        params += [str(app).strip()] * 2
    if since_u:
        where.append("u.at >= ?")
        params.append(since_u)
    if until_u:
        where.append("u.at <= ?")
        params.append(until_u)
    if keys is not None:
        where.append("(instr(lower(COALESCE(u.what, '')), lower(?)) > 0 OR (u.record_type || ':' || "
                     "u.record_id) IN (SELECT value FROM json_each(?)) OR (u.rt2 || ':' || u.rid2) "
                     "IN (SELECT value FROM json_each(?)))")
        params += [keys[0], json.dumps(sorted(keys[1])), json.dumps(sorted(keys[1]))]
    if record:
        where.append("((u.record_type = ? AND u.record_id = ?) OR (u.rt2 = ? AND u.rid2 = ?))")
        params += [record[0], int(record[1]), record[0], int(record[1])]
    return " AND ".join(where), params


def _tables(conn) -> dict:
    return who._tables(conn)


def _matches(conn, t: dict, project_id: int, text: str) -> set:
    """"type:id" of every record of the project whose live text or a stored copy in its edit
    history contains `text` (any case). An erased record has neither, so it never matches."""
    needle = text.lower()
    escaped = json.dumps(text, ensure_ascii=True)[1:-1].lower()
    out = set()
    for table, (cols, proj, join) in _SEARCH.items():
        if table not in t:
            continue
        have = [c for c in cols if c in t[table]]
        if not have:
            continue
        test = " OR ".join(f"instr(lower(COALESCE(x.{c}, '')), ?) > 0" for c in have)
        for r in conn.execute(f"SELECT x.id FROM {table} x {join} WHERE {proj} = ? AND ({test})",
                              (project_id, *[needle] * len(have))):
            out.add(f"{_TYPE_OF.get(table, table)}:{r[0]}")
    if "record_history" in t:
        for r in conn.execute(
                "SELECT table_name, record_id, old_row, new_row FROM record_history WHERE "
                "project_id = ? AND (instr(lower(COALESCE(old_row, '')), ?) > 0 OR instr(lower("
                "COALESCE(new_row, '')), ?) > 0 OR instr(lower(COALESCE(old_row, '')), ?) > 0 OR "
                "instr(lower(COALESCE(new_row, '')), ?) > 0)",
                (project_id, needle, needle, escaped, escaped)):
            if r["table_name"] in _SIDE:
                row = _load(r["new_row"]) or _load(r["old_row"]) or {}
                if row.get("record_type") and row.get("record_id") is not None:
                    out.add(f"{row['record_type']}:{row['record_id']}")
            else:
                out.add(f"{_TYPE_OF.get(r['table_name'], r['table_name'])}:{r['record_id']}")
    return out


def _load(v) -> Optional[dict]:
    if not v:
        return None
    try:
        d = json.loads(v)
    except (TypeError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def _label_of(table: str, row: Optional[dict]) -> str:
    if not row:
        return ""
    for col in _LABEL.get(table, ()):
        v = row.get(col)
        if col == "key_decisions" and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            s = " ".join(str(v).split())
            return s[len("Decision: "):] if s.startswith("Decision: ") else s
    return ""


def record_state(conn, t: dict, rtype: str, rid) -> dict:
    """{"state": live | deleted | erased | "", "label": text, "row": the live row or None}.
    Erased: no live row and no copy in the edit history; nothing of its text is left."""
    table = history.TABLES.get(rtype) or {"review_item": "review_items",
                                          "conflict": "decision_conflicts"}.get(rtype)
    if not table or table not in t or rid is None:
        return {"state": "", "label": "", "row": None}
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
    if row is not None:
        d = dict(row)
        label = _label_of(table, d)
        if table == "decision_conflicts":
            label = f"decision #{d.get('new_id')} and decision #{d.get('old_id')}"
        return {"state": "live", "label": label, "row": d}
    if "record_history" in t:
        h = conn.execute("SELECT old_row, new_row FROM record_history WHERE table_name = ? AND "
                         "record_id = ? ORDER BY id DESC LIMIT 1", (table, rid)).fetchone()
        if h is not None:
            stored = _load(h["new_row"]) or _load(h["old_row"])
            return {"state": "deleted", "label": _label_of(table, stored), "row": None}
    return {"state": "erased", "label": ERASED, "row": None}


def _short(v, width: int = 80) -> str:
    s = " ".join(("" if v is None else str(v)).split())
    return s if len(s) <= width else s[: width - 3] + "..."


def describe_edit(table: str, old: Optional[dict], new: Optional[dict], tool: str,
                  undone: bool = False) -> str:
    """One plain line for one edit-history row: created, deleted, a todo completed or
    reopened, which fields changed; an undo and an undone edit say so."""
    tool = tool or ""
    if table in _SIDE:
        name = history.SIDE[table]
        cur = new or old or {}
        if table == "entry_standing":
            out = (f"marked {cur.get('standing')}" + (f" (by {cur.get('by_type')} #{cur.get('by_id')})"
                                                      if cur.get("by_id") else "")
                   if new else "standing cleared")
        else:
            out = f"{name} " + ("added" if old is None else "removed" if new is None else "changed")
    elif old is None:
        out = "brought back (undo of a delete)" if tool == "history/undo" else "created"
    elif new is None:
        out = "taken back (undo of its creation)" if tool == "history/undo" else "deleted"
    else:
        ch = history._changed_fields(old, new)
        parts = []
        if table == "todos" and "status" in ch:
            a, b = old.get("status"), new.get("status")
            if b == "done":
                parts.append(f"completed ({a} -> done)")
            elif b == "cancelled":
                parts.append(f"cancelled ({a} -> cancelled)")
            elif a in ("done", "cancelled"):
                parts.append(f"reopened ({a} -> {b})")
            else:
                parts.append(f"status {a} -> {b}")
        rest = sorted(k for k in ch if not (table == "todos" and k in ("status", "completed_at")))
        if rest:
            parts.append("edited " + ", ".join(rest))
        out = "; ".join(parts) or "no field changes"
        if tool == "history/undo":
            out = "undo: " + out
    if tool.startswith("queue/"):
        out = f"{history.QUEUE.get(table, table)} {out}"
    return out + (" (later undone)" if undone else "")


def _decorate(conn, t: dict, rows: list) -> list:
    out, states = [], {}

    def state(rt, rid):
        if (rt, rid) not in states:
            states[(rt, rid)] = record_state(conn, t, rt, rid)
        return states[(rt, rid)]
    for r in rows:
        what = r["what"] or ""
        if r["kind"] in ("edit", "edit_created"):
            h = conn.execute("SELECT table_name, old_row, new_row, tool, undone_at FROM "
                             "record_history WHERE id = ?", (r["ref"],)).fetchone()
            if h is not None:
                what = describe_edit(h["table_name"], _load(h["old_row"]), _load(h["new_row"]),
                                     h["tool"], bool(h["undone_at"]))
        st = state(r["record_type"], r["record_id"])
        other = state(r["rt2"], r["rid2"]) if r["rt2"] and r["rid2"] is not None else None
        if r["kind"] in ("link_added", "link_removed") and r["extra"] == "supersedes":
            what = (("removed: " if r["kind"] == "link_removed" else "")
                    + f"{r['record_type']} #{r['record_id']} replaces {r['rt2']} #{r['rid2']}")
        out.append({
            "at": who.local_time(r["at"]), "at_utc": r["at"], "event": r["kind"],
            "type": r["cat"], "what": " ".join(what.split())[:200],
            "app": r["app"], "model": r["model"], "machine": r["machine"],
            "identity_source": r["identity_source"],
            "identity": identity.label(r["identity_source"]),
            "who": identity.shown_row(r, fallback="not recorded"),
            **({"via": v} if (v := identity.via(r["machine"], r["identity_source"])) else {}),
            "record_type": r["record_type"], "record_id": r["record_id"],
            "label": st["label"], "state": st["state"],
            **({"other": {"record_type": r["rt2"], "record_id": r["rid2"],
                          "label": other["label"], "state": other["state"]}} if other else {})})
    return out


def _range(since, until):
    try:
        return who.parse_time(since), who.parse_time(until, end=True)
    except who.WhoError as e:
        raise HistoryPageError("bad_time", e.payload["message"]) from e


def cutoff() -> str:
    """Before this (UTC), the activity log and the session ledger have faded."""
    return (datetime.now(timezone.utc) - timedelta(days=activity.keep_days())).strftime(
        "%Y-%m-%d %H:%M:%S")


def timeline(db_path: Path, project_id: int, rtype: Optional[str] = None,
             app: Optional[str] = None, since=None, until=None, q: Optional[str] = None,
             record: Optional[tuple] = None, page: int = 1, per_page: int = PER_PAGE,
             newest_first: bool = False) -> dict:
    """One page of a project's entries, oldest first (or newest first). Filters: type (a
    record type, or review / link / conflict / session), app (app or machine contains, any
    case), since / until (this computer's local time), q (search: the entry's line, or the
    live text or any stored copy of a record it names), record = (type, id): that record's
    entries only. {"rows", "total", "page", "pages", "per_page", "types", "faded"}."""
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or PER_PAGE), MAX_PER_PAGE))
    since_u, until_u = _range(since, until)
    conn = _connect(db_path)
    try:
        t = _tables(conn)
        parts = _sources(conn, t)
        empty = {"rows": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page,
                 "types": [], "faded": None}
        if not parts:
            return empty
        union = " UNION ALL ".join(parts)
        q = (q or "").strip()
        keys = (q, _matches(conn, t, project_id, q)) if q else None
        cond, params = _where(project_id, rtype, app, since_u, until_u, keys, record)
        base = f"SELECT u.*, {_CAT} AS cat FROM ({union}) u WHERE {cond}"
        total = conn.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]
        order = "DESC" if newest_first else "ASC"
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM ({base}) ORDER BY at {order}, CASE WHEN record_type = 'project' AND "
            f"kind = 'created' THEN 0 WHEN kind IN ('session_opened', 'created', 'edit_created') "
            f"THEN 1 ELSE 2 END {order}, ref {order} LIMIT ? OFFSET ?",
            (*params, per_page, (page - 1) * per_page))]
        all_cond, all_params = _where(project_id, None, None, None, None, None, record)
        span = conn.execute(f"SELECT MIN(u.at), group_concat(DISTINCT {_CAT}) FROM ({union}) u "
                            f"WHERE {all_cond}", all_params).fetchone()
        types = sorted(x for x in (span[1] or "").split(",") if x)
        cut = cutoff()
        faded = None
        if span[0] and span[0] < cut and (not since_u or since_u < cut):
            faded = {"before": who.local_time(cut)[:10], "days": activity.keep_days()}
        return {"rows": _decorate(conn, t, rows), "total": total, "page": page,
                "per_page": per_page, "pages": max(1, -(-total // per_page)), "types": types,
                "faded": faded}
    finally:
        conn.close()


def record_detail(db_path: Path, project_id: int, rtype: str, rid: int) -> dict:
    """One record: its state and label, its live fields, and its whole edit history (each
    edit with who and every changed field before and after). An erased record: state
    "erased", label "[erased]", no fields, no history."""
    conn = _connect(db_path)
    try:
        t = _tables(conn)
        st = record_state(conn, t, rtype, rid)
        out = {"record_type": rtype, "record_id": rid, "state": st["state"],
               "label": st["label"], "fields": None, "edits": [], "more_edits": 0}
        if st["state"] == "live" and st["row"] is not None:
            out["fields"] = {k: v for k, v in st["row"].items()}
        table = history.TABLES.get(rtype) or {"review_item": "review_items",
                                              "conflict": "decision_conflicts"}.get(rtype)
        if st["state"] == "erased" or "record_history" not in t or not table:
            return out
        where = "(table_name = ? AND record_id = ?)"
        params = [table, rid]
        for side in _SIDE:
            where += f" OR ({history._SIDE_OF})"
            params += [side, rtype, rid]
        total = conn.execute(f"SELECT COUNT(*) FROM record_history WHERE {where}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM record_history WHERE {where} ORDER BY id LIMIT ?",
                            (*params, MAX_EDITS)).fetchall()
        out["more_edits"] = max(0, total - MAX_EDITS)
        for r in rows:
            old, new = _load(r["old_row"]), _load(r["new_row"])
            keys = r.keys()
            changes = {}
            if old is not None and new is not None:
                changes = {k: {"old": _short(old.get(k), FIELD_WIDTH), "new": _short(new.get(k), FIELD_WIDTH)}
                           for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)}
            elif (old or new) is not None:
                whole = new if new is not None else old
                changes = {k: {"old": "" if new is not None else _short(v, FIELD_WIDTH),
                               "new": _short(v, FIELD_WIDTH) if new is not None else ""}
                           for k, v in whole.items() if v not in (None, "")}
            out["edits"].append({
                "at": who.local_time(r["created_at"]), "at_utc": r["created_at"],
                "what": describe_edit(r["table_name"], old, new, r["tool"], bool(r["undone_at"])),
                "tool": r["tool"], "changes": changes,
                **{k: (r[k] if k in keys else None) for k in identity.FIELDS},
                "identity": identity.label(r["identity_source"] if "identity_source" in keys else None),
                "who": identity.shown_row({**dict(r), "app": (r["app"] if "app" in keys else None)
                                           or r["client"]}, fallback="not recorded")})
        return out
    finally:
        conn.close()


def todos(db_path: Path, project_id: int, status: str = "all", page: int = 1,
          per_page: int = PER_PAGE) -> dict:
    """A project's todos (live ones, and deleted ones the edit history still has), each with
    who created it (app, model, machine, sign-in; "not recorded" before 2.4.0 once the
    activity log has faded), its current status, any live claim and its last entry."""
    status = status if status in TODO_STATUSES else "all"
    page = max(1, int(page or 1))
    per_page = max(1, min(int(per_page or PER_PAGE), MAX_PER_PAGE))
    conn = _connect(db_path)
    try:
        t = _tables(conn)
        if "todos" not in t:
            return {"todos": [], "total": 0, "page": 1, "pages": 1, "per_page": per_page,
                    "counts": {}}
        rh = "record_history" in t
        deleted = ("SELECT DISTINCT h.record_id AS id FROM record_history h WHERE h.table_name = "
                   "'todos' AND h.project_id = ? AND NOT EXISTS (SELECT 1 FROM todos z WHERE z.id = "
                   "h.record_id)") if rh else "SELECT NULL AS id WHERE 0"
        allq = (f"SELECT id, status, created_at, 0 AS gone FROM todos WHERE project_id = ? UNION ALL "
                f"SELECT d.id, 'deleted', NULL, 1 FROM ({deleted}) d")
        counts = {r[0]: r[1] for r in conn.execute(
            f"SELECT CASE WHEN status IN ({', '.join('?' * len(TODO_OPEN))}) THEN 'open' ELSE status "
            f"END, COUNT(*) FROM ({allq}) GROUP BY 1", (*TODO_OPEN, project_id, project_id))}
        cond, params = "1", []
        if status == "open":
            cond, params = f"status IN ({', '.join('?' * len(TODO_OPEN))})", list(TODO_OPEN)
        elif status != "all":
            cond, params = "status = ?", [status]
        total = conn.execute(f"SELECT COUNT(*) FROM ({allq}) WHERE {cond}",
                             (project_id, project_id, *params)).fetchone()[0]
        ids = conn.execute(f"SELECT id, gone FROM ({allq}) WHERE {cond} ORDER BY gone, id DESC "
                           f"LIMIT ? OFFSET ?", (project_id, project_id, *params, per_page,
                                                 (page - 1) * per_page)).fetchall()
        claims = "todo_claims" in t
        out = []
        for i in ids:
            tid = i["id"]
            st = record_state(conn, t, "todo", tid)
            row = st["row"] or {}
            maker = None
            if rh:
                maker = conn.execute("SELECT * FROM record_history WHERE table_name = 'todos' AND "
                                     "record_id = ? AND old_row IS NULL ORDER BY id LIMIT 1",
                                     (tid,)).fetchone()
            if maker is None and "activity_log" in t and "record_type" in t["activity_log"]:
                maker = conn.execute("SELECT * FROM activity_log WHERE record_type = 'todo' AND "
                                     "record_id = ? AND ok = 1 ORDER BY id LIMIT 1", (tid,)).fetchone()
            m = dict(maker) if maker is not None else {}
            if m and not m.get("app"):
                m["app"] = m.get("client")
            created = m.get("created_at") or m.get("at") or row.get("created_at")
            claim = None
            if claims and st["state"] == "live":
                c = conn.execute("SELECT * FROM todo_claims WHERE todo_id = ? AND expires_at > ?",
                                 (tid, _now())).fetchone()
                if c is not None:
                    claim = {"by": identity.shown_row(c), "until": who.local_time(c["expires_at"])}
            last = None
            if rh:
                last = conn.execute("SELECT MAX(created_at) FROM record_history WHERE table_name = "
                                    "'todos' AND record_id = ?", (tid,)).fetchone()[0]
            if "todo_claim_events" in t:
                ev = conn.execute("SELECT MAX(at) FROM todo_claim_events WHERE todo_id = ?",
                                  (tid,)).fetchone()[0]
                last = max(x for x in (last, ev) if x) if (last or ev) else None
            out.append({"id": tid, "title": st["label"], "state": st["state"],
                        "status": row.get("status") if st["state"] == "live" else st["state"],
                        "priority": row.get("priority"),
                        "created_at": who.local_time(created) if created else "",
                        "creator": {"who": identity.shown_row(m, fallback="not recorded") if m
                                    else "not recorded", "app": m.get("app"),
                                    "model": m.get("model"), "machine": m.get("machine"),
                                    "identity_source": m.get("identity_source"),
                                    "identity": identity.label(m.get("identity_source"))},
                        "claim": claim, "last_at": who.local_time(last) if last else ""})
        return {"todos": out, "total": total, "page": page, "per_page": per_page,
                "pages": max(1, -(-total // per_page)), "counts": counts, "status": status}
    finally:
        conn.close()
