"""Deletes (v2.1; 2.4.0: at once unless approvals are on).

manage_projects(action="delete" / "delete_component") and manage_memory(action="delete")
remove the record and every row that goes with it (a project's components, todos, decisions,
...; a component's changes and problems; a problem's attempts and solution). With approvals
off (the default, approvals.py) delete_now does it at once, after a safety copy. With them on,
request() queues a review item of kind `delete` that says what would go, and nothing is removed
until the user approves it themselves (popup, phone page or terminal; a project also needs its
exact name). Rejecting it leaves everything as it was. Apps without the local scope (remote,
OAuth, per-app LAN keys) can neither delete, ask nor approve.

When it is carried out, in one transaction: every row the delete will remove or change is worked out
from the database's own foreign keys (plus the cross-project links that point at a removed
record), copied into the edit history as one edit (record_history, tool `delete/<type>`),
the record is deleted (the ON DELETE rules remove its children), and the result is checked
against the copy, row by row and table by table; any difference rolls everything back.
manage_history(action="undo") on the record then puts every row back. Undoing that undo
is refused (history.py): deleting again needs a new request.

Retention: the copies are edit-history rows, kept as long as the database (FlowState never
prunes record_history) and in every backup. So a delete is recoverable, not an erasure: the
text stays in the database file. Not copied: the meaning-search cache (meaning_index), which
is rebuilt from the records. Left in place: rows that only name a record by type and id
without a foreign key (record_meta, memory_fts, link_alerts.record_id); ids are never reused
(AUTOINCREMENT), so they can't attach to another record, and undo finds them as they were.
"""

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Optional

from . import dbconn
from . import history, identity, review

# record type -> (table, SQL for a one-line title)
TYPES = {
    "project": ("projects", "name"),
    "component": ("components", "name"),
    "decision": ("conversations", "COALESCE(key_decisions, user_prompt_summary)"),
    "change": ("changes", "field_name"),
    "learning": ("learnings", "insight"),
    "todo": ("todos", "title"),
    "problem": ("problems", "title"),
    "attempt": ("solution_attempts", "description"),
    "handoff": ("handoff_notes", "where_i_was"),
    "rule": ("project_rules", "rule"),
    "link": ("record_links", "link_type || ' ' || COALESCE(url, to_type || ' #' || "
                             "COALESCE(to_id, ''))"),
    "variable": ("project_variables", "name"),
    "method": ("project_methods", "name"),
    "attachment": ("attachments", "file_name"),
}
RECORD_TYPES = tuple(t for t in TYPES if t not in ("project", "component"))

_OWNER_SQL = {
    "project": "SELECT id FROM projects WHERE id = ?",
    "change": """SELECT c.project_id FROM changes ch JOIN components c ON c.id = ch.component_id
                 WHERE ch.id = ?""",
    "problem": """SELECT c.project_id FROM problems p JOIN components c ON c.id = p.component_id
                  WHERE p.id = ?""",
    "attempt": """SELECT c.project_id FROM solution_attempts a JOIN problems p ON p.id = a.problem_id
                  JOIN components c ON c.id = p.component_id WHERE a.id = ?""",
    "link": "SELECT from_project_id, to_project_id FROM record_links WHERE id = ?",
}

# How removed rows are counted for the user (table -> plural label). Everything else is
# bookkeeping (session tracking, alerts, file text locations) and shown as "other rows".
LABELS = {"components": "components", "conversations": "decisions", "changes": "changes",
          "learnings": "learnings", "todos": "todos", "problems": "problems",
          "solution_attempts": "attempts", "solutions": "solutions", "project_rules": "rules",
          "handoff_notes": "handoffs", "record_links": "links",
          "project_variables": "variables", "project_methods": "methods",
          "attachments": "attachments", "learned_skills": "skills",
          "review_items": "review items", "sessions": "sessions"}
# Counts that must not change between the request and the approval (the user approved
# these numbers). Sessions and review items move on their own while the user decides.
_FIXED = tuple(v for v in LABELS.values() if v not in ("sessions", "review items"))

DERIVED = {"meaning_index"}   # a search cache rebuilt from the records; not copied
# record_links endpoint type -> table (links have no foreign key to the record itself)
_ENDPOINTS = {"projects": "project", "components": "component", "conversations": "decision",
              "changes": "change", "learnings": "learning", "problems": "problem",
              "todos": "todo"}


class DeleteError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.payload = {"error": code, "message": message, **extra}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")   # the ON DELETE rules do the removing
    return conn


def _one_line(s, n: int = 80) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 3] + "..."


def owners(conn, rtype: str, rid: int) -> Optional[tuple]:
    """The project id(s) a record belongs to (a link: both ends), or None if it is missing."""
    sql = _OWNER_SQL.get(rtype) or f'SELECT project_id FROM "{TYPES[rtype][0]}" WHERE id = ?'
    row = conn.execute(sql, (rid,)).fetchone()
    return None if row is None else tuple(v for v in row if v is not None)


def _title(conn, rtype: str, rid: int) -> str:
    table, expr = TYPES[rtype]
    row = conn.execute(f'SELECT {expr} FROM "{table}" WHERE id = ?', (rid,)).fetchone()
    return _one_line(row[0] if row else "")


# ---------------------------------------------------------------- what a delete removes
def _children(conn) -> dict:
    """parent table -> [(child table, child column, parent column or None, on_delete)]."""
    out = defaultdict(list)
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                             "AND name NOT LIKE 'sqlite_%'"):
        try:
            fks = conn.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()
        except sqlite3.DatabaseError:
            continue
        for fk in fks:
            out[fk[2]].append((t, fk[3], fk[4], (fk[6] or "NO ACTION").upper()))
    return out


def plan(conn, table: str, rid: int) -> tuple[dict, dict]:
    """(rows the delete removes, rows it changes), each {(table, key): row}, worked out from
    the foreign keys exactly as SQLite will apply them, plus links pointing at a removed
    record. Raises DeleteError if a row would go that can't be copied for undo, or if
    something outside the delete still needs a removed row."""
    kids = _children(conn)
    root = history._row(conn, table, rid)
    if root is None:
        raise DeleteError("record_not_found", f"No {history.TYPE_OF.get(table, table)} with id {rid}.")
    gone, changed, needed = {(table, rid): root}, {}, []
    has_links = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='record_links'").fetchone() is not None
    queue = [(table, rid, root)]
    while queue:
        t, key, row = queue.pop()
        refs = []
        for child, col, pcol, on_delete in kids.get(t, ()):
            val = row.get(pcol or history.row_key(conn, t) or "id")
            if val is None or child in DERIVED:
                continue
            if history.row_key(conn, child) is None:   # no key to copy it by
                if conn.execute(f'SELECT 1 FROM "{child}" WHERE "{col}" = ? LIMIT 1',
                                (val,)).fetchone():
                    raise DeleteError("delete_unsupported",
                                      f"Deleting this would also change rows of {child}, which "
                                      f"FlowState can't copy for undo, so it was not done.")
                continue
            refs.append((child, history._rows(conn, child, col, val), on_delete))
        if has_links and t in _ENDPOINTS:
            found = {r["id"]: {k: history._val(r[k]) for k in r.keys()} for r in conn.execute(
                """SELECT * FROM record_links WHERE (from_type = ? AND from_id = ?)
                   OR (to_type = ? AND to_id = ?)""", (_ENDPOINTS[t], key, _ENDPOINTS[t], key))}
            refs.append(("record_links", found, "CASCADE"))
        for child, found, on_delete in refs:
            for ck, crow in found.items():
                if (child, ck) in gone:
                    continue
                if on_delete == "CASCADE":
                    gone[(child, ck)] = crow
                    queue.append((child, ck, crow))
                elif on_delete in ("SET NULL", "SET DEFAULT"):
                    changed.setdefault((child, ck), crow)
                else:
                    needed.append((child, ck))
    blocked = [n for n in needed if n not in gone]
    if blocked:
        t, k = blocked[0]
        raise DeleteError("delete_blocked", f"{history.TYPE_OF.get(t, t)} #{k} still needs it; "
                                            f"nothing was deleted.")
    return gone, {k: v for k, v in changed.items() if k not in gone}


def counts(gone: dict, root: tuple) -> dict:
    """What goes with the record, by user-facing type (the record itself not counted)."""
    c = Counter(LABELS.get(t, "other rows") for (t, k) in gone if (t, k) != root)
    order = list(LABELS.values()) + ["other rows"]   # records first, bookkeeping last
    return {k: c[k] for k in order if c.get(k)}


def _table_counts(conn) -> dict:
    virtual = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                          "AND sql LIKE 'CREATE VIRTUAL%'")]
    out = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                             "AND name NOT LIKE 'sqlite_%'"):
        if t in DERIVED or any(t == v or t.startswith(v + "_") for v in virtual):
            continue
        out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    return out


# ---------------------------------------------------------------- request (the AI's side)
def undo_call(project: str, rtype: str, rid: int) -> str:
    return (f'manage_history(action="undo", project="{project}", record_type="{rtype}", '
            f'record_id={rid})')


# Key fields shown with a row. Anything not on the row is skipped.
_KEY_FIELDS = {
    "projects": ("description", "status"),
    "components": ("description", "status"),
    "todos": ("status", "priority"),
    "problems": ("status", "severity"),
    "changes": ("change_type",),
    "learnings": ("category",),
    "solution_attempts": ("outcome",),
    "handoff_notes": ("next_step",),
}
# Counted, but not named: they move on their own and are not the user's records.
_UNNAMED = {"sessions", "review_items"}
_NAMED = {"components": "component", "conversations": "decision", "changes": "change",
          "learnings": "learning", "todos": "todo", "problems": "problem",
          "solution_attempts": "attempt", "solutions": "solution", "project_rules": "rule",
          "handoff_notes": "handoff", "record_links": "link", "project_variables": "variable",
          "project_methods": "method", "attachments": "attachment", "learned_skills": "skill"}
_CHILD_CAP = 60


def _row_name(table: str, row: dict) -> str:
    if table == "conversations":
        text = row.get("key_decisions") or row.get("user_prompt_summary") or ""
        if isinstance(text, str) and text.startswith("["):
            try:
                items = json.loads(text)
                text = str(items[0]) if items else ""
            except (ValueError, TypeError):
                pass
        text = str(text)
        if text.startswith("Decision:"):
            text = text[len("Decision:"):].strip()
        return _one_line(text, 80)
    if table == "record_links":
        return _one_line(row.get("link_type") or "link", 80)
    for key in ("name", "title", "insight", "field_name", "rule", "where_i_was", "description",
                "file_name"):
        if row.get(key):
            return _one_line(row[key], 80)
    return ""


def _fields_of(table: str, row: dict) -> dict:
    return {k: _one_line(row.get(k), 80) for k in _KEY_FIELDS.get(table, ())
            if row.get(k) not in (None, "")}


def _named_row(table: str, key, row: dict, project: str) -> dict:
    return {"type": _NAMED.get(table, history.TYPE_OF.get(table, table)), "id": key,
            "name": _row_name(table, row), "project": project, "fields": _fields_of(table, row)}


def _inventory(rtype: str, rid: int, title: str, project: str, gone: dict, root: tuple) -> dict:
    """The exact rows a delete removes, from the plan, not from anything an agent wrote."""
    root_table = TYPES[rtype][0]
    root_row = gone.get(root) or {}
    children, unnamed = [], 0
    for (table, key) in sorted(gone, key=lambda tk: (tk[0], tk[1] if isinstance(tk[1], int) else 0)):
        if (table, key) == root:
            continue
        if table in _UNNAMED or table not in _NAMED:
            unnamed += 1
            continue
        children.append(_named_row(table, key, gone[(table, key)], project))
    return {"record_type": rtype, "id": rid, "title": title, "project": project,
            "fields": _fields_of(root_table, root_row), "children": children[:_CHILD_CAP],
            "children_more": max(0, len(children) - _CHILD_CAP), "unnamed": unnamed}


def agent_note(item: dict) -> Optional[str]:
    """Agent-written wording, if it isn't the server's own 'Delete …' / 'Deletes …' line."""
    try:
        payload = json.loads(item.get("payload") or "{}")
    except ValueError:
        payload = {}
    raw = payload.get("agent_note")
    if raw:
        return _one_line(raw, 400)
    summary = " ".join(str(item.get("summary") or "").split())
    if summary and not summary.startswith("Delete "):
        return _one_line(summary, 400)
    details = " ".join(str(item.get("details") or "").split())
    if details and not details.startswith("Deletes "):
        return _one_line(details, 400)
    return None


def _field_text(fields: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in fields.items())


def _root_line(inv: dict) -> str:
    name = inv["title"] or "(untitled)"
    line = f'{inv["record_type"].capitalize()} "{name}" in {inv["project"]}'
    extra = _field_text(inv["fields"])
    return line + (f" ({extra})" if extra else "")


def _child_line(row: dict) -> str:
    name = f' "{row["name"]}"' if row.get("name") else ""
    extra = _field_text(row.get("fields") or {})
    return f'{row["type"].capitalize()} #{row["id"]}{name}' + (f" ({extra})" if extra else "")


def _body(inv: dict, with_: dict, undo: str) -> str:
    lines = [_root_line(inv)]
    if with_:
        lines.append("Also deletes " + ", ".join(f"{n} {k}" for k, n in with_.items()) + ":")
    shown = inv["children"]
    lines.extend("- " + _child_line(c) for c in shown)
    if inv.get("children_more"):
        lines.append(f"- and {inv['children_more']} more")
    if inv.get("unnamed") and not with_:
        lines.append(f"Also {inv['unnamed']} bookkeeping rows (sessions or review items).")
    if undo:
        lines.append(f"Undo after it is deleted: {undo}")
    return "\n".join(lines)


def preview(db_path: Path, item: dict) -> dict:
    """Live inventory for a queued delete. Missing when the record is already gone."""
    try:
        payload = json.loads(item.get("payload") or "{}")
    except ValueError:
        payload = {}
    rtype, rid = payload.get("record_type"), payload.get("record_id")
    note = agent_note(item)
    project = payload.get("project_name") or ""
    if rtype not in TYPES or rid is None:
        return {"missing": True, "headline": "This delete request is incomplete.",
                "root_line": "", "details": "", "plain": "This delete request is incomplete.",
                "ai_note": note, "would_delete": None}
    conn = connect(db_path)
    try:
        if not project:
            row = conn.execute("SELECT name FROM projects WHERE id = ?",
                               (item.get("project_id"),)).fetchone()
            project = row[0] if row else ""
        title = _title(conn, rtype, int(rid))
        try:
            gone, changed = plan(conn, TYPES[rtype][0], int(rid))
        except DeleteError:
            text = f'{rtype.capitalize()} "{title or rid}" is already gone. Nothing will be deleted.'
            return {"missing": True, "headline": text, "root_line": text, "details": "",
                    "plain": text, "ai_note": note, "would_delete": None}
        with_ = counts(gone, (TYPES[rtype][0], int(rid)))
        inv = _inventory(rtype, int(rid), title, project, gone, (TYPES[rtype][0], int(rid)))
        undo = undo_call(project, rtype, int(rid))
        body = _body(inv, with_, undo)
        headline = f"These records will be deleted from {project}" if with_ else \
            f"This record will be deleted from {project}"
        plain = headline + "\n" + body
        if note:
            plain += f"\nAI's note: {note}"
        would = {"record_type": rtype, "id": int(rid), "title": title, "project": project,
                 "fields": inv["fields"], "with": with_, "children": inv["children"]}
        if inv["children_more"]:
            would["children_more"] = inv["children_more"]
        return {"missing": False, "headline": headline, "root_line": _root_line(inv),
                "details": body + (f"\nAI's note: {note}" if note else ""),
                "plain": plain, "ai_note": note, "would_delete": would, "changed_rows": len(changed)}
    finally:
        conn.close()


def question(db_path: Path, item: dict, project: str) -> Optional[str]:
    """One sentence for the approval question, naming the record and the children."""
    got = preview(db_path, item)
    w = got.get("would_delete")
    if not w:
        return None
    title = w["title"] or ""
    if w["record_type"] == "project":
        what = f'delete the whole project "{project}"'
    else:
        what = f'delete the {w["record_type"]} "{title}" from {project}' if title else \
            f'delete {w["record_type"]} #{w["id"]} from {project}'
    names = [c["name"] or f'#{c["id"]}' for c in (w.get("children") or [])[:8]]
    extra = ", ".join(f"{n} {k}" for k, n in (w.get("with") or {}).items())
    if names:
        more = w.get("children_more") or 0
        tail = ", ".join(f'"{n}"' for n in names)
        if more:
            tail += f", and {more} more"
        return what + (f" (with {extra}: {tail})" if extra else f" (with {tail})")
    return what + (f" (with {extra})" if extra else "")


def list_line(db_path: Path, it: dict, status: bool = False) -> str:
    """The review-list line: the database inventory, never the agent's summary."""
    got = preview(db_path, it)
    kind = "delete request" + (f", {it['status']}" if status else "")
    head = f"#{it['id']} ({it.get('project') or ''}, {kind}): "
    text = " ".join(got["plain"].split())
    if len(text) > 1500:
        text = text[:1497] + "..."
    return head + text


def html_for(db_path: Path, it: dict) -> str:
    """Escaped inventory for the review page. The agent's words are only 'AI's note'."""
    import html as _html

    def e(v) -> str:
        return _html.escape("" if v is None else str(v), quote=True)

    got = preview(db_path, it)
    if got.get("missing") or not got.get("would_delete"):
        note = f"<p class='ainote'><b>AI's note: </b>{e(got.get('ai_note'))}</p>" \
            if got.get("ai_note") else ""
        return f"<div class='delcard'><p><strong>{e(got.get('headline'))}</strong></p>{note}</div>"
    w = got["would_delete"]
    fields = "".join(f"<li>{e(k)}: {e(v)}</li>" for k, v in (w.get("fields") or {}).items())
    kids = []
    for c in w.get("children") or []:
        extra = ", ".join(f"{e(k)}: {e(v)}" for k, v in (c.get("fields") or {}).items())
        name = f' "{e(c["name"])}"' if c.get("name") else ""
        kids.append(f"<li>{e(c['type'].capitalize())} #{e(c['id'])}{name}"
                    + (f" <span class='meta'>({extra})</span>" if extra else "") + "</li>")
    if w.get("children_more"):
        kids.append(f"<li>and {e(w['children_more'])} more</li>")
    counts = ", ".join(f"{n} {e(k)}" for k, n in (w.get("with") or {}).items())
    note = f"<p class='ainote'><b>AI's note: </b>{e(got['ai_note'])}</p>" if got.get("ai_note") else ""
    listed = f"<p>Also deletes {counts}:</p><ul class='kids'>{''.join(kids)}</ul>" if kids or counts else ""
    field_list = f"<ul class='fields'>{fields}</ul>" if fields else ""
    return (f"<div class='delcard'><p class='headline'>{e(got['headline'])}</p>"
            f"<p><strong>{e(got['root_line'])}</strong></p>{field_list}{listed}"
            f"<p class='meta'>Undo after it is deleted: {e(undo_call(w['project'], w['record_type'], w['id']))}</p>"
            f"{note}</div>")


def request(db_path: Path, project_id: int, rtype: str, rid, client: str = "unknown",
            options: Optional[dict] = None) -> dict:
    """Queue a delete request for the user. Deletes nothing."""
    if rtype not in TYPES:
        raise DeleteError("bad_type", f"record_type must be one of: {', '.join(RECORD_TYPES)}.")
    if rid is None:
        raise DeleteError("record_required", f"Name the {rtype} to delete by its id.")
    rid = int(rid)
    table = TYPES[rtype][0]
    conn = connect(db_path)
    try:
        if not review.available(conn):
            raise DeleteError("review_queue_unavailable", "The review queue isn't set up in this "
                              "database yet, so a delete can't be requested. Nothing was deleted.")
        own = owners(conn, rtype, rid)
        if own is None:
            raise DeleteError("record_not_found", f"No {rtype} with id {rid}.")
        if project_id not in own:
            raise DeleteError("project_mismatch", f"{rtype.capitalize()} #{rid} is not in this "
                              f"project; name the project it belongs to.")
        pname = conn.execute("SELECT name FROM projects WHERE id = ?", (project_id,)).fetchone()[0]
        title = _title(conn, rtype, rid)
        gone, changed = plan(conn, table, rid)
        with_ = counts(gone, (table, rid))
        for (iid, payload) in conn.execute("SELECT id, payload FROM review_items WHERE kind = "
                                           "'delete' AND status = 'pending' AND project_id = ?",
                                           (project_id,)).fetchall():
            p = json.loads(payload or "{}")
            if p.get("record_type") == rtype and p.get("record_id") == rid:
                inv = _inventory(rtype, rid, title, pname, gone, (table, rid))
                return _request_reply(iid, rtype, rid, title, with_, pname, inv, already=True)
    finally:
        conn.close()
    what = f'{rtype} #{rid} "{_one_line(title, 60)}"'
    if with_:
        what += " with " + ", ".join(f"{n} {k}" for k, n in with_.items())
    details = (f"Deletes {what}" + (f"; {len(changed)} other rows only lose their link to it"
                                    if changed else "") +
               f". Approve to delete (kept in the edit history: {undo_call(pname, rtype, rid)} "
               f"restores it); reject to keep everything.")
    if rtype == "project":
        details += f" Approving needs the project's exact name typed by the user: {pname}"
    payload = {"record_type": rtype, "record_id": rid, "title": title, "counts": with_,
               "changed_rows": len(changed), "project_name": pname, **(options or {})}
    item = review.add_item(db_path, project_id, "delete", f"Delete {what}"[:300], details, payload,
                           client)
    inv = _inventory(rtype, rid, title, pname, gone, (table, rid))
    return _request_reply(item, rtype, rid, title, with_, pname, inv)


def _request_reply(item: int, rtype: str, rid: int, title: str, with_: dict, pname: str,
                   inv: Optional[dict] = None, already: bool = False) -> dict:
    approve = (f'manage_review(action="approve", project="{pname}", item_id={item}, '
               f'user_request="<their words>"' +
               (', confirm_name="<the project name, typed by the user>"' if rtype == "project"
                else "") + ")")
    return {"delete_requested": item, "status": "waiting for the user's approval",
            "already_requested": already or None,
            "would_delete": {"record_type": rtype, "id": rid, "title": title, "with": with_,
                             "project": pname,
                             "fields": (inv or {}).get("fields") or {},
                             "children": (inv or {}).get("children") or [],
                             **({"children_more": inv["children_more"]}
                                if inv and inv.get("children_more") else {})},
            "note": (f"Nothing was deleted. Ask the user whether to delete this; only if they say "
                     f"so in this chat, call {approve}, or they approve it on the review page. "
                     f"Rejecting keeps everything.")}


# ---------------------------------------------------------------- approval (the user's side)
def check_approval(item: dict, confirm_name: Optional[str]) -> None:
    """Refuse before anything changes: a project delete needs the exact project name."""
    payload = json.loads(item.get("payload") or "{}")
    if payload.get("record_type") != "project":
        return
    if not (confirm_name or "").strip():
        raise DeleteError("confirm_name_required",
                          f"Deleting a project needs its exact name in confirm_name, typed by the "
                          f"user (it is {payload.get('project_name')!r}). Nothing was deleted.")
    if confirm_name != payload.get("project_name"):
        raise DeleteError("confirm_name_mismatch",
                          f"confirm_name {confirm_name!r} is not the project's exact name. "
                          f"Nothing was deleted.")


def execute(db_path: Path, item: dict, client: str = "unknown",
            confirm_name: Optional[str] = None) -> dict:
    """Carry out an approved delete request (one transaction). Returns what went."""
    payload = json.loads(item.get("payload") or "{}")
    rtype, rid = payload.get("record_type"), payload.get("record_id")
    if rtype not in TYPES or rid is None:
        raise DeleteError("bad_request", "This delete request is incomplete; reject it.")
    check_approval(item, confirm_name)
    return _carry_out(db_path, item["project_id"], rtype, rid, client,
                      asked=payload.get("counts") or {}, confirm_name=confirm_name,
                      pname=payload.get("project_name"), title=payload.get("title"))


def delete_now(db_path: Path, project_id: int, rtype: str, rid, client: str = "unknown") -> dict:
    """Approvals off (approvals.py): an agent's delete goes through at once, after a verified
    safety copy, as one undoable edit. Returns what went and the one-line undo."""
    from . import safety
    if rtype not in TYPES:
        raise DeleteError("bad_type", f"record_type must be one of: {', '.join(RECORD_TYPES)}.")
    if rid is None:
        raise DeleteError("record_required", f"Name the {rtype} to delete by its id.")
    copy = safety.copy_before(db_path)   # SafetyError: no copy, no delete
    out = _carry_out(db_path, project_id, rtype, int(rid), client)
    d = out["deleted"]
    what = f'{rtype} #{d["id"]} "{_one_line(d["title"], 60)}"'
    if d["with"]:
        what += " with " + ", ".join(f"{n} {k}" for k, n in d["with"].items())
    out["safety_copy"] = copy.name
    out["note"] = f"Deleted {what} (safety copy taken). Undo: {out['undo']}"
    return out


def _carry_out(db_path: Path, pid: int, rtype: str, rid: int, client: str,
               asked: Optional[dict] = None, confirm_name: Optional[str] = None,
               pname: Optional[str] = None, title: Optional[str] = None) -> dict:
    """The delete itself, in one transaction. `asked` (an approved request's counts): refuse
    if what would go changed since; None for a delete made at once."""
    table = TYPES[rtype][0]
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        own = owners(conn, rtype, rid)
        if own is None:
            raise DeleteError("record_not_found", f"{rtype} #{rid} no longer exists"
                              + ("; reject the request." if asked is not None else "."))
        if pid not in own:
            raise DeleteError("project_mismatch",
                              f"{rtype} #{rid} has moved to another project since the request; "
                              f"reject it and ask again." if asked is not None else
                              f"{rtype.capitalize()} #{rid} is not in this project; name the "
                              f"project it belongs to.")
        if pname is None:
            pname = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()[0]
        if title is None:
            title = _title(conn, rtype, rid)
        if rtype == "project" and asked is not None:
            name = conn.execute("SELECT name FROM projects WHERE id = ?", (rid,)).fetchone()[0]
            if name != confirm_name:
                raise DeleteError("confirm_name_mismatch", f"The project is now called {name!r}; "
                                                           f"reject the request and ask again.")
        gone, changed = plan(conn, table, rid)
        now = counts(gone, (table, rid))
        if asked is not None and any(now.get(k, 0) != asked.get(k, 0) for k in _FIXED):
            raise DeleteError("changed_since_request",
                              f"What would be deleted changed since the request (then {asked}, now "
                              f"{now}). Nothing was deleted; reject this request and ask again.")
        before = _table_counts(conn)
        key = history.row_key(conn, table)
        conn.execute(f'DELETE FROM "{table}" WHERE "{key}" = ?', (rid,))
        for (t, k) in gone:   # rows without a foreign key to it (links); the rest are gone
            conn.execute(f'DELETE FROM "{t}" WHERE "{history.row_key(conn, t)}" = ?', (k,))
        _verify(conn, gone, changed, before)
        edit_id = history.new_edit_id()
        at = history._now()
        for (t, k), old in list(gone.items()) + list(changed.items()):
            new = history._row(conn, t, k)
            identity.insert(conn, "record_history", {
                "edit_id": edit_id, "table_name": t, "record_id": k, "project_id": pid,
                "tool": f"delete/{rtype}", "old_row": json.dumps(old),
                "new_row": json.dumps(new) if new is not None else None,
                "client": (client or "")[:80], "created_at": at}, client)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return {"deleted": {"record_type": rtype, "id": rid, "title": title,
                        "with": now, "changed_rows": len(changed)},
            "edit_id": edit_id,
            "undo": undo_call(pname or str(pid), rtype, rid),
            "note": ("Deleted. Everything is kept in the edit history; the undo call above puts "
                     "it all back.")}


def _verify(conn, gone: dict, changed: dict, before: dict) -> None:
    """The delete removed exactly the copied rows and nothing else."""
    after = _table_counts(conn)
    expected = Counter(t for (t, _k) in gone)
    for t in set(before) | set(after):
        if before.get(t, 0) - after.get(t, 0) != expected.get(t, 0):
            raise DeleteError("delete_mismatch",
                              f"The delete would have removed {before.get(t, 0) - after.get(t, 0)} "
                              f"rows of {t}, not the {expected.get(t, 0)} copied for undo. Nothing "
                              f"was deleted.")
    for (t, k) in gone:
        if history._row(conn, t, k) is not None:
            raise DeleteError("delete_mismatch", f"{t} #{k} was not removed. Nothing was deleted.")
    for (t, k) in changed:
        if history._row(conn, t, k) is None:
            raise DeleteError("delete_mismatch", f"{t} #{k} was removed, not only unlinked. "
                                                 f"Nothing was deleted.")


# ---------------------------------------------------------------- recent deletes (the brief)
_TITLE_KEYS = ("name", "title", "insight", "key_decisions", "user_prompt_summary", "rule",
               "field_name", "file_name", "where_i_was", "description", "link_type")


def _recent(conn, project_id: Optional[int], days: int, limit: int) -> list:
    from datetime import datetime, timedelta, timezone
    if not history.available(conn):
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    where = "project_id = ?" if project_id is not None else "table_name = 'projects'"
    rows = conn.execute(
        f"""SELECT h.* FROM record_history h JOIN (SELECT MIN(id) AS first FROM record_history
            WHERE tool LIKE 'delete/%' AND new_row IS NULL AND created_at >= ? AND {where}
            GROUP BY edit_id) f ON f.first = h.id ORDER BY h.id DESC LIMIT ?""",
        (since, *(() if project_id is None else (project_id,)), limit)).fetchall()
    out = []
    for r in rows:
        if r["undone_at"] or (project_id is None and r["table_name"] != "projects"):
            continue
        old = json.loads(r["old_row"] or "{}")
        title = next((old[k] for k in _TITLE_KEYS if old.get(k)), "")
        rtype = r["tool"].split("/", 1)[1]
        app = r["app"] if "app" in r.keys() and r["app"] else r["client"]
        out.append((rtype, r["record_id"], _one_line(title, 40), app, r["created_at"][5:10],
                    old.get("name") if rtype == "project" else None))
    return out


def recent_line(conn, project_id: Optional[int], project: str = "", days: int = 7) -> Optional[str]:
    """One line: what agents deleted here in the last `days` (not undone) and how to undo it.
    project_id None: deleted projects (for the project list)."""
    try:
        got = _recent(conn, project_id, days, 4)
    except sqlite3.Error:
        return None
    if not got:
        return None
    shown = "; ".join(f'{t} #{i} "{title}" ({app}, {when})' for t, i, title, app, when, _ in got[:3])
    more = f"; and more (manage_history list)" if len(got) > 3 else ""
    t, i, _title, _app, _when, pname = got[0]
    return (f"Deleted in the last {days} days: {shown}{more}. Undo: "
            f"{undo_call(pname or project, t, i)}.")


# ---------------------------------------------------------------- undo of a project delete
def deleted_project(db_path: Path, named, record_id) -> Optional[int]:
    """For manage_history undo of a deleted project: the project's id if `named` (its old
    name or id) is a project removed by an approved delete that hasn't been undone."""
    if record_id is None or named in (None, ""):
        return None
    try:
        rid = int(record_id)
    except (TypeError, ValueError):
        return None
    conn = connect(db_path)
    try:
        if conn.execute("SELECT 1 FROM projects WHERE id = ?", (rid,)).fetchone():
            return None   # it exists: the normal project check applies
        if not history.available(conn):
            return None
        row = conn.execute("""SELECT old_row FROM record_history WHERE table_name = 'projects'
                              AND record_id = ? AND new_row IS NULL AND undone_at IS NULL
                              AND tool = 'delete/project' ORDER BY id DESC LIMIT 1""",
                           (rid,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    old = json.loads(row["old_row"])
    return rid if str(named) in (old.get("name"), str(rid)) else None
