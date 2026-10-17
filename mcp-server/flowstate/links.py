"""Cross-project links (v2 Phase D, migration 003).

A link joins two things: a project, a component, a record or a URL. User-facing link
types say how projects share things ("Atlas VPN uses the wire protocol component of
Atlas OS"); internal types carry superseded decisions, subtasks, blockers and
"this decision is about that component".

When a change or decision is logged on a component that other projects link to, each of
those projects gets an alert ("changed since you last looked") and, unless it already has
an open one for that component, an update todo. FlowState only tracks and alerts; it never
changes code in the other apps.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import identity

ENDPOINTS = ("project", "component", "decision", "change", "learning", "problem", "todo")
SHARE_TYPES = ("uses", "depends_on", "part_of", "same_as")
LINK_TYPES = SHARE_TYPES + ("supersedes", "subtask_of", "blocks", "about", "url")
OPEN_TODO = ("pending", "in_progress", "blocked")

_OWNER_SQL = {
    "project": "SELECT id FROM projects WHERE id = ?",
    "component": "SELECT project_id FROM components WHERE id = ?",
    "decision": "SELECT project_id FROM conversations WHERE id = ?",
    "change": """SELECT c.project_id FROM changes ch JOIN components c ON c.id = ch.component_id
                 WHERE ch.id = ?""",
    "learning": "SELECT project_id FROM learnings WHERE id = ?",
    "problem": """SELECT c.project_id FROM problems p JOIN components c ON c.id = p.component_id
                  WHERE p.id = ?""",
    "todo": "SELECT project_id FROM todos WHERE id = ?",
}
_TEXT_SQL = {
    "project": "SELECT name FROM projects WHERE id = ?",
    "component": "SELECT name FROM components WHERE id = ?",
    "decision": "SELECT COALESCE(key_decisions, user_prompt_summary) FROM conversations WHERE id = ?",
    "change": "SELECT field_name FROM changes WHERE id = ?",
    "learning": "SELECT insight FROM learnings WHERE id = ?",
    "problem": "SELECT title FROM problems WHERE id = ?",
    "todo": "SELECT title FROM todos WHERE id = ?",
}

# Which (from, to) endpoint types each internal link type allows.
_ALLOWED = {
    "supersedes": ({"decision"}, {"decision"}),
    "subtask_of": ({"todo"}, {"todo"}),
    "blocks": ({"todo", "problem"}, {"todo"}),
    "about": ({"decision", "change", "learning", "problem", "todo"}, {"component"}),
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class LinkError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.payload = {"error": code, "message": message, **extra}


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='record_links'").fetchone() is not None


def _need(conn):
    if not available(conn):
        raise LinkError("links_unavailable",
                        "Links aren't set up in this database yet (migration 003 has not run). "
                        "Carry on; tell the user to restart FlowState.")


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _one_line(s: Optional[str], n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


def owner(conn, kind: str, rid) -> Optional[int]:
    """Project id that owns a thing, or None if it doesn't exist."""
    if kind not in _OWNER_SQL or rid is None:
        return None
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    row = conn.execute(_OWNER_SQL[kind], (rid,)).fetchone()
    return row[0] if row else None


def label(conn, kind: str, rid) -> str:
    if kind == "url":
        return str(rid)
    row = conn.execute(_TEXT_SQL[kind], (rid,)).fetchone() if kind in _TEXT_SQL else None
    text = row[0] if row else ""
    if kind == "decision" and text and text.startswith("["):
        try:
            items = json.loads(text)
            text = str(items[0]) if items else ""
        except (ValueError, TypeError):
            pass
    return _one_line(text, 90)


def project_name(conn, pid) -> str:
    row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
    return row[0] if row else f"#{pid}"


def find_component(conn, pid: int, name: str) -> Optional[int]:
    """Component of a project by name: exact (case-insensitive), then normalised."""
    rows = conn.execute("SELECT id, name FROM components WHERE project_id = ? ORDER BY id",
                        (pid,)).fetchall()
    for r in rows:
        if r["name"].lower() == name.strip().lower():
            return r["id"]
    hits = [r["id"] for r in rows if _norm(r["name"]) == _norm(name)]
    return hits[0] if len(hits) == 1 else None


def _component_names(conn, pid: int, limit: int = 40) -> list:
    return [r[0] for r in conn.execute(
        "SELECT name FROM components WHERE project_id = ? ORDER BY name LIMIT ?", (pid, limit))]


def url_kind(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "github.com" or host.endswith(".github.com"):
        return "github"
    if host == "linear.app" or host.endswith(".linear.app"):
        return "linear"
    return "other"


def _check_url(url: str) -> str:
    url = (url or "").strip()
    p = urlparse(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise LinkError("bad_url", "url must be a full http(s) address, e.g. "
                                   "https://github.com/owner/repo/issues/12.")
    if p.username or p.password or len(url) > 500:
        raise LinkError("bad_url", "url must not contain a user name or password, and must be "
                                   "under 500 characters.")
    return url


def _resolve_project(db_path: Path, value) -> int:
    from .brief import resolve_project
    res = resolve_project(db_path, value, fuzzy=False)
    if "project" not in res:
        raise LinkError("project_not_found", f"No single project matches {value!r}.",
                        candidates=res.get("candidates", []))
    return res["project"]["id"]


# ---------------------------------------------------------------- add / remove / list
def _endpoint(conn, db_path: Path, pid_hint: Optional[int], kind: Optional[str], rid,
              project=None, component: Optional[str] = None, side: str = "target") -> tuple:
    """(type, id, project_id) for one end of a link, validated."""
    if kind:
        kind = {"conversation": "decision"}.get(kind, kind)
        if kind not in ENDPOINTS:
            raise LinkError("bad_type", f"{side} type must be one of: {', '.join(ENDPOINTS)}.")
        if rid is None:
            raise LinkError("id_required", f"Give the {side} id with the {side} type.")
        pid = owner(conn, kind, rid)
        if pid is None:
            raise LinkError("record_not_found", f"No {kind} with id {rid}.")
        return kind, int(rid), pid
    if project is not None:
        pid = _resolve_project(db_path, project)
    else:
        pid = pid_hint
    if pid is None:
        raise LinkError("target_required", "Say what to link to: target_project (with "
                                           "target_component), target_type + target_id, or url.")
    if component:
        cid = find_component(conn, pid, component)
        if cid is None:
            raise LinkError("component_not_found",
                            f"Project '{project_name(conn, pid)}' has no component named "
                            f"{component!r}.", components=_component_names(conn, pid))
        return "component", cid, pid
    return "project", pid, pid


def _view(conn, row) -> dict:
    d = {"id": row["id"], "link_type": row["link_type"],
         "from": {"type": row["from_type"], "id": row["from_id"],
                  "project": project_name(conn, row["from_project_id"]),
                  "label": label(conn, row["from_type"], row["from_id"])},
         "created_at": row["created_at"]}
    if row["to_type"] == "url":
        d["to"] = {"type": "url", "url": row["url"], "kind": row["url_kind"]}
    else:
        d["to"] = {"type": row["to_type"], "id": row["to_id"],
                   "project": project_name(conn, row["to_project_id"]),
                   "label": label(conn, row["to_type"], row["to_id"])}
    if row["note"]:
        d["note"] = row["note"]
    if row["removed_at"]:
        d["removed_at"] = row["removed_at"]
    return d


def add(db_path: Path, pid: int, link_type: str, *, item_type=None, item_id=None,
        component=None, target_project=None, target_component=None, target_type=None,
        target_id=None, url=None, note=None, client=None,
        conn: Optional[sqlite3.Connection] = None) -> dict:
    """Make a link from the named project (or one of its things) to a target."""
    own = conn is None
    conn = conn or connect(db_path)
    try:
        _need(conn)
        link_type = (link_type or ("url" if url else "uses")).strip()
        if link_type not in LINK_TYPES:
            raise LinkError("bad_link_type", f"link_type must be one of: {', '.join(LINK_TYPES)}.")
        ftype, fid, fpid = _endpoint(conn, db_path, pid, item_type, item_id,
                                     component=component, side="item")
        if fpid != pid:
            raise LinkError("project_mismatch",
                            f"{ftype.capitalize()} #{fid} belongs to project "
                            f"'{project_name(conn, fpid)}', not '{project_name(conn, pid)}'.")
        if url or link_type == "url":
            if not url:
                raise LinkError("url_required", "link_type url needs url=...")
            u = _check_url(url)
            dup = conn.execute(
                """SELECT * FROM record_links WHERE from_type=? AND from_id=? AND to_type='url'
                   AND url=? AND removed_at IS NULL""", (ftype, fid, u)).fetchone()
            if dup:
                return {"link": _view(conn, dup), "existing": True}
            cur = identity.insert(conn, "record_links", {
                "from_type": ftype, "from_id": fid, "from_project_id": fpid, "link_type": "url",
                "to_type": "url", "url": u, "url_kind": url_kind(u), "note": note,
                "created_by": (client or "")[:80], "created_at": now()}, client)
        else:
            ttype, tid, tpid = _endpoint(conn, db_path, None, target_type, target_id,
                                         project=target_project, component=target_component)
            if (ftype, fid) == (ttype, tid):
                raise LinkError("self_link", "A thing can't be linked to itself.")
            if link_type in _ALLOWED:
                okf, okt = _ALLOWED[link_type]
                if ftype not in okf or ttype not in okt:
                    raise LinkError("bad_link_type",
                                    f"{link_type} links go from {'/'.join(sorted(okf))} to "
                                    f"{'/'.join(sorted(okt))}.")
            elif ftype == "project" and ttype == "project" and fid == tid:
                raise LinkError("self_link", "A project can't be linked to itself.")
            if link_type == "subtask_of" and _would_cycle(conn, fid, tid):
                raise LinkError("cycle", f"Todo #{tid} is already under #{fid}; that would loop.")
            dup = conn.execute(
                """SELECT * FROM record_links WHERE from_type=? AND from_id=? AND link_type=?
                   AND to_type=? AND to_id=? AND removed_at IS NULL""",
                (ftype, fid, link_type, ttype, tid)).fetchone()
            if dup:
                return {"link": _view(conn, dup), "existing": True}
            cur = identity.insert(conn, "record_links", {
                "from_type": ftype, "from_id": fid, "from_project_id": fpid,
                "link_type": link_type, "to_type": ttype, "to_id": tid, "to_project_id": tpid,
                "note": note, "created_by": (client or "")[:80], "created_at": now()}, client)
        if own:
            conn.commit()
        row = conn.execute("SELECT * FROM record_links WHERE id = ?", (cur.lastrowid,)).fetchone()
        return {"link": _view(conn, row)}
    finally:
        if own:
            conn.close()


def _would_cycle(conn, child: int, parent: int) -> bool:
    """True if `parent` is already (transitively) a subtask of `child`."""
    seen, todo = set(), [parent]
    while todo:
        cur = todo.pop()
        if cur == child:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        todo += [r[0] for r in conn.execute(
            """SELECT to_id FROM record_links WHERE link_type='subtask_of' AND from_type='todo'
               AND from_id=? AND removed_at IS NULL""", (cur,))]
    return False


def get_row(db_path: Path, link_id: int) -> Optional[dict]:
    conn = connect(db_path)
    try:
        if not available(conn):
            return None
        row = conn.execute("SELECT * FROM record_links WHERE id = ?", (link_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def find_id(db_path: Path, pid: int, link_type: Optional[str], **spec) -> int:
    """The active link matching an add-style description (for remove without an id)."""
    conn = connect(db_path)
    try:
        _need(conn)
        ftype, fid, _ = _endpoint(conn, db_path, pid, spec.get("item_type"), spec.get("item_id"),
                                  component=spec.get("component"), side="item")
        if spec.get("url"):
            rows = conn.execute("""SELECT id FROM record_links WHERE from_type=? AND from_id=?
                                   AND to_type='url' AND url=? AND removed_at IS NULL""",
                                (ftype, fid, spec["url"].strip())).fetchall()
        else:
            ttype, tid, _ = _endpoint(conn, db_path, None, spec.get("target_type"),
                                      spec.get("target_id"), project=spec.get("target_project"),
                                      component=spec.get("target_component"))
            sql = """SELECT id FROM record_links WHERE removed_at IS NULL AND
                     ((from_type=? AND from_id=? AND to_type=? AND to_id=?) OR
                      (link_type='same_as' AND from_type=? AND from_id=? AND to_type=? AND to_id=?))"""
            params = [ftype, fid, ttype, tid, ttype, tid, ftype, fid]
            if link_type:
                sql += " AND link_type = ?"
                params.append(link_type)
            rows = conn.execute(sql, params).fetchall()
        if not rows:
            raise LinkError("link_not_found", "No active link matches that description.")
        if len(rows) > 1:
            raise LinkError("ambiguous", "Several links match; give link_type or link_id.",
                            link_ids=[r[0] for r in rows])
        return rows[0][0]
    finally:
        conn.close()


def remove(db_path: Path, pid: int, link_id: int, client: Optional[str] = None) -> dict:
    conn = connect(db_path)
    try:
        _need(conn)
        row = conn.execute("SELECT * FROM record_links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise LinkError("record_not_found", f"No link with id {link_id}.")
        if pid not in (row["from_project_id"], row["to_project_id"]):
            raise LinkError("project_mismatch",
                            f"Link #{link_id} joins '{project_name(conn, row['from_project_id'])}'"
                            + (f" and '{project_name(conn, row['to_project_id'])}'"
                               if row["to_project_id"] else "")
                            + f", not '{project_name(conn, pid)}'.")
        if row["removed_at"]:
            return {"removed": link_id, "already_removed": row["removed_at"]}
        sets, who = identity.set_sql(conn, "record_links", client, prefix="removed_")
        conn.execute(f"UPDATE record_links SET removed_at = ?, removed_by = ?{sets} WHERE id = ?",
                     (now(), (client or "")[:80], *who, link_id))
        conn.commit()
        return {"removed": link_id, "link": _view(conn, conn.execute(
            "SELECT * FROM record_links WHERE id = ?", (link_id,)).fetchone())}
    finally:
        conn.close()


def list_links(db_path: Path, pid: int, item_type=None, item_id=None, component=None,
               include_removed: bool = False, limit: int = 100) -> dict:
    conn = connect(db_path)
    try:
        _need(conn)
        gone = "" if include_removed else " AND removed_at IS NULL"
        if item_type or component:
            ftype, fid, fpid = _endpoint(conn, db_path, pid, item_type, item_id,
                                         component=component, side="item")
            rows = conn.execute(
                f"""SELECT * FROM record_links WHERE ((from_type=? AND from_id=?) OR
                    (to_type=? AND to_id=?)){gone} ORDER BY id LIMIT ?""",
                (ftype, fid, ftype, fid, limit)).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT * FROM record_links WHERE (from_project_id=? OR to_project_id=?){gone}
                    ORDER BY id LIMIT ?""", (pid, pid, limit)).fetchall()
        return {"project": project_name(conn, pid), "links": [_view(conn, r) for r in rows],
                "count": len(rows)}
    finally:
        conn.close()


# ---------------------------------------------------------------- shared components
def _share_marks(types=SHARE_TYPES) -> str:
    return ",".join(f"'{t}'" for t in types)


def linked_components(conn, pid: int) -> list[dict]:
    """Components of other projects that this project (or anything in it) links to."""
    if not available(conn):
        return []
    rows = conn.execute(
        f"""SELECT DISTINCT l.to_id cid, c.name, c.project_id owner, p.name owner_name, l.link_type
            FROM record_links l JOIN components c ON c.id = l.to_id
            JOIN projects p ON p.id = c.project_id
            WHERE l.to_type='component' AND l.removed_at IS NULL
              AND l.link_type IN ({_share_marks()}) AND l.from_project_id = ? AND c.project_id != ?
            UNION
            SELECT DISTINCT l.from_id cid, c.name, c.project_id owner, p.name owner_name, l.link_type
            FROM record_links l JOIN components c ON c.id = l.from_id
            JOIN projects p ON p.id = c.project_id
            WHERE l.from_type='component' AND l.link_type='same_as' AND l.removed_at IS NULL
              AND l.to_project_id = ? AND c.project_id != ?
            ORDER BY 2""", (pid, pid, pid, pid)).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["cid"] not in seen:
            seen.add(r["cid"])
            out.append(dict(r))
    return out


def shared_component_by_name(conn, pid: int, name: str) -> Optional[int]:
    """A component linked into this project with this name (for log_work component=...)."""
    hits = [c["cid"] for c in linked_components(conn, pid)
            if c["name"].lower() == name.strip().lower()]
    if not hits:
        hits = [c["cid"] for c in linked_components(conn, pid) if _norm(c["name"]) == _norm(name)]
    return hits[0] if len(hits) == 1 else None


def project_links_to(conn, pid: int, cid: int) -> bool:
    """Does this project link to the component (so writes against it are allowed)?"""
    return any(c["cid"] == cid for c in linked_components(conn, pid))


def projects_watching(conn, cid: int) -> list[int]:
    """Projects that should hear about a change to this component: everyone linking to it,
    the owner, and the projects of components it is same_as."""
    if not available(conn):
        return []
    own = conn.execute("SELECT project_id FROM components WHERE id = ?", (cid,)).fetchone()
    out = {own[0]} if own else set()
    for r in conn.execute(
            f"""SELECT from_project_id FROM record_links WHERE to_type='component' AND to_id=?
                AND removed_at IS NULL AND link_type IN ({_share_marks()})
                UNION SELECT to_project_id FROM record_links WHERE from_type='component'
                AND from_id=? AND link_type='same_as' AND removed_at IS NULL""", (cid, cid)):
        if r[0] is not None:
            out.add(r[0])
    return sorted(out)


def is_watched(conn, cid: int) -> bool:
    return len(projects_watching(conn, cid)) > 1


def on_component_event(db_path: Path, acting_pid: int, cid: int, record_type: str,
                       record_id: Optional[int], summary: str,
                       make_task: Callable[[int, str, str], tuple[Optional[int], Optional[int]]]
                       ) -> list[dict]:
    """A change/decision was logged on component `cid` from project `acting_pid`. Alert every
    other watching project. `make_task(project_id, title, details)` creates the update todo
    (or queues it) and returns (todo_id, review_item_id). Never raises into the caller's
    write: errors come back as an empty list after logging."""
    conn = connect(db_path)
    out = []
    try:
        if not available(conn):
            return []
        targets = [p for p in projects_watching(conn, cid) if p != acting_pid]
        if not targets:
            return []
        comp = conn.execute("SELECT name, project_id FROM components WHERE id = ?",
                            (cid,)).fetchone()
        cname, owner_name = comp["name"], project_name(conn, comp["project_id"])
        src = project_name(conn, acting_pid)
        for t in targets:
            unseen = conn.execute("SELECT 1 FROM link_alerts WHERE project_id=? AND component_id=? "
                                  "AND seen_at IS NULL LIMIT 1", (t, cid)).fetchone()
            open_todo = conn.execute(
                f"""SELECT la.todo_id FROM link_alerts la JOIN todos td ON td.id = la.todo_id
                    WHERE la.project_id=? AND la.component_id=? AND td.status IN
                    ({','.join('?' * len(OPEN_TODO))}) ORDER BY la.id DESC LIMIT 1""",
                (t, cid, *OPEN_TODO)).fetchone()
            todo_id, queued = (open_todo[0] if open_todo else None), None
            if not unseen and not open_todo:
                conn.commit()  # make_task opens its own connection
                tname = project_name(conn, t)
                title = f"{_one_line(cname, 60)} changed: check {tname}"
                details = (f"Shared component '{cname}' (from {owner_name}) had a {record_type} "
                           f"logged by {src}: {_one_line(summary, 200)}. FlowState only alerts; "
                           f"check whether {tname} needs a matching change.")
                todo_id, queued = make_task(t, title, details)
            conn.execute(
                """INSERT INTO link_alerts (project_id, component_id, source_project_id,
                   record_type, record_id, summary, todo_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (t, cid, acting_pid, record_type, record_id, _one_line(summary, 300), todo_id,
                 now()))
            out.append({"project_id": t, "todo_id": todo_id, "review_item_id": queued})
        conn.commit()
        return out
    finally:
        conn.close()


# ---------------------------------------------------------------- brief support
def mark_looked(db_path: Path, pid: int, client: str) -> None:
    conn = connect(db_path)
    try:
        if not available(conn):
            return
        t = now()
        conn.execute("""INSERT INTO project_views (project_id, last_looked_at, last_client)
                        VALUES (?, ?, ?) ON CONFLICT(project_id) DO UPDATE SET
                        last_looked_at = excluded.last_looked_at,
                        last_client = excluded.last_client""", (pid, t, (client or "")[:80]))
        conn.execute("UPDATE link_alerts SET seen_at = ? WHERE project_id = ? AND seen_at IS NULL",
                     (t, pid))
        conn.commit()
    finally:
        conn.close()


def alert_lines(conn, pid: int, limit: int = 3) -> list[str]:
    """"CHANGED since you last looked" lines (one per shared component, newest first). They
    are about this project, so the brief shows them near the top, not in the trimmed part."""
    if not available(conn):
        return []
    lines = []
    alerts = conn.execute(
        """SELECT a.component_id, c.name comp, p.name owner, COUNT(*) n, MAX(a.created_at) last,
                  MAX(a.todo_id) todo_id
           FROM link_alerts a JOIN components c ON c.id = a.component_id
           JOIN projects p ON p.id = c.project_id
           WHERE a.project_id = ? AND a.seen_at IS NULL
           GROUP BY a.component_id ORDER BY last DESC LIMIT ?""", (pid, limit)).fetchall()
    for a in alerts:
        latest = conn.execute(
            """SELECT a.summary, sp.name src FROM link_alerts a
               LEFT JOIN projects sp ON sp.id = a.source_project_id
               WHERE a.project_id=? AND a.component_id=? AND a.seen_at IS NULL
               ORDER BY a.id DESC LIMIT 1""", (pid, a["component_id"])).fetchone()
        lines.append(f"CHANGED since you last looked: {_one_line(a['comp'], 50)} ({a['owner']}), "
                     f"{a['n']} update{'s' if a['n'] != 1 else ''}, latest from "
                     f"{latest['src'] or '?'}: {_one_line(latest['summary'], 80)}"
                     + (f" (todo #{a['todo_id']})" if a["todo_id"] else ""))
    more = conn.execute("SELECT COUNT(DISTINCT component_id) FROM link_alerts WHERE project_id = ? "
                        "AND seen_at IS NULL", (pid,)).fetchone()[0] - len(alerts)
    if more > 0:
        lines.append(f"(and {more} more shared component{'s' if more != 1 else ''} changed; see link list)")
    return lines


def brief_lines(conn, pid: int) -> list[str]:
    """Lines for the brief's "Linked projects" section, most important first. The brief
    adds them only while budget remains, so this section is what gets trimmed."""
    if not available(conn):
        return []
    lines = []
    for c in linked_components(conn, pid):
        last = conn.execute(
            """SELECT created_at, field_name txt FROM changes WHERE component_id = ?
               ORDER BY created_at DESC, id DESC LIMIT 1""", (c["cid"],)).fetchone()
        verb = "same as" if c["link_type"] == "same_as" else c["link_type"].replace("_", " ")
        lines.append(f"- {verb} {_one_line(c['name'], 50)} ({c['owner_name']})"
                     + (f"; latest change {last['created_at'][:10]}: {_one_line(last['txt'], 70)}"
                        if last else ""))
    for r in conn.execute(
            f"""SELECT l.link_type, p.name FROM record_links l JOIN projects p ON p.id = l.to_id
                WHERE l.from_type='project' AND l.from_id=? AND l.to_type='project'
                AND l.removed_at IS NULL AND l.link_type IN ({_share_marks()}) ORDER BY p.name""",
            (pid,)):
        lines.append(f"- {r['link_type'].replace('_', ' ')} project {r['name']}")
    used = conn.execute(
        f"""SELECT c.name comp, GROUP_CONCAT(DISTINCT p.name) users
            FROM record_links l JOIN components c ON c.id = l.to_id
            JOIN projects p ON p.id = l.from_project_id
            WHERE l.to_type='component' AND l.removed_at IS NULL AND c.project_id = ?
              AND l.from_project_id != ? AND l.link_type IN ({_share_marks()})
            GROUP BY c.id ORDER BY c.name LIMIT 8""", (pid, pid)).fetchall()
    for u in used:
        lines.append(f"- {_one_line(u['comp'], 50)} is used by {_one_line(u['users'].replace(',', ', '), 90)}")
    return lines


def superseded_decisions(conn) -> dict:
    """{old decision id: (new decision id, since)} for active supersedes links."""
    if not available(conn):
        return {}
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        """SELECT to_id, from_id, created_at FROM record_links WHERE link_type='supersedes'
           AND removed_at IS NULL""")}
