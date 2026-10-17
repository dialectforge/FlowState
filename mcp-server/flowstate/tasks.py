"""Task structure (v2 Phase D): subtasks, blockers and "what's next".

Stored as links (migration 003): a subtask is `todo --subtask_of--> todo`, a blocker is
`todo|problem --blocks--> todo`. The older `todos.blocked_by_problem_id` column counts as a
blocker too. "What's next" = open todos with no open blocker and no open subtasks, highest
priority first, work in progress before new work, then by due date and age.
Priorities are shown with the new names (critical / urgent / normal / low; see priority.py).
"""

from pathlib import Path
from typing import Optional

from . import links, priority

OPEN = ("pending", "in_progress", "blocked")
_OPEN_PROBLEM = "(status IS NULL OR status NOT IN ('solved', 'wont_fix', 'closed'))"


class TaskError(links.LinkError):
    pass


def _todo(conn, tid: int) -> Optional[dict]:
    r = conn.execute("SELECT * FROM todos WHERE id = ?", (tid,)).fetchone()
    return dict(r) if r else None


def link_new_todo(db_path: Path, pid: int, todo_id: int, parent_todo_id=None, blocked_by=None,
                  blocked_by_problem=None, client: str = "") -> dict:
    """After `todo add`: attach it under a parent and/or to its blockers."""
    out = {}
    conn = links.connect(db_path)
    try:
        if parent_todo_id is not None:
            parent = _todo(conn, int(parent_todo_id))
            if parent is None:
                raise TaskError("record_not_found", f"No todo with id {parent_todo_id}.")
            if parent["project_id"] != pid:
                raise TaskError("project_mismatch", f"Parent todo #{parent_todo_id} is in another "
                                                    f"project; subtasks stay in one project.")
    finally:
        conn.close()
    if parent_todo_id is not None:
        out["parent"] = links.add(db_path, pid, "subtask_of", item_type="todo", item_id=todo_id,
                                  target_type="todo", target_id=int(parent_todo_id),
                                  client=client)["link"]["id"]
    if blocked_by or blocked_by_problem is not None:
        out["blockers"] = block(db_path, pid, todo_id, blocked_by, blocked_by_problem, client)["links"]
    return out


def block(db_path: Path, pid: int, todo_id: int, blocked_by=None, blocked_by_problem=None,
          client: str = "") -> dict:
    """Mark todo `todo_id` as blocked by other todos (any project) and/or a problem."""
    ids = [int(x) for x in (blocked_by or [])]
    if not ids and blocked_by_problem is None:
        raise TaskError("blocker_required", "Give blocked_by (todo ids) or blocked_by_problem.")
    if todo_id in ids:
        raise TaskError("self_link", "A todo can't block itself.")
    made = []
    for b in ids:
        conn = links.connect(db_path)
        try:
            bpid = links.owner(conn, "todo", b)
        finally:
            conn.close()
        if bpid is None:
            raise TaskError("record_not_found", f"No todo with id {b}.")
        if _blocks_transitively(db_path, todo_id, b):
            raise TaskError("cycle", f"Todo #{b} already waits on #{todo_id}; that would loop.")
        # the blocker is the link's source, so the link belongs to the blocker's project
        made.append(links.add(db_path, bpid, "blocks", item_type="todo", item_id=b,
                              target_type="todo", target_id=todo_id, client=client)["link"]["id"])
    if blocked_by_problem is not None:
        conn = links.connect(db_path)
        try:
            ppid = links.owner(conn, "problem", blocked_by_problem)
        finally:
            conn.close()
        if ppid is None:
            raise TaskError("record_not_found", f"No problem with id {blocked_by_problem}.")
        made.append(links.add(db_path, ppid, "blocks", item_type="problem",
                              item_id=int(blocked_by_problem), target_type="todo",
                              target_id=todo_id, client=client)["link"]["id"])
    return {"todo_id": todo_id, "links": made, "blocked_by": ids,
            **({"blocked_by_problem": int(blocked_by_problem)} if blocked_by_problem is not None else {})}


def _blocks_transitively(db_path: Path, start: int, target: int) -> bool:
    """Does `start` (directly or not) block `target`?"""
    conn = links.connect(db_path)
    try:
        seen, todo = set(), [start]
        while todo:
            cur = todo.pop()
            if cur == target:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            todo += [r[0] for r in conn.execute(
                """SELECT to_id FROM record_links WHERE link_type='blocks' AND from_type='todo'
                   AND from_id=? AND removed_at IS NULL""", (cur,))]
        return False
    finally:
        conn.close()


def blocker_links(db_path: Path, todo_id: int, blocked_by=None, blocked_by_problem=None) -> list:
    """Active 'blocks' link ids pointing at this todo (all, or only the named blockers)."""
    conn = links.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT id, from_type, from_id FROM record_links WHERE link_type='blocks'
               AND to_type='todo' AND to_id=? AND removed_at IS NULL""", (todo_id,)).fetchall()
    finally:
        conn.close()
    if not blocked_by and blocked_by_problem is None:
        return [r["id"] for r in rows]
    want = {("todo", int(x)) for x in (blocked_by or [])}
    if blocked_by_problem is not None:
        want.add(("problem", int(blocked_by_problem)))
    return [r["id"] for r in rows if (r["from_type"], r["from_id"]) in want]


def _open_blockers(conn, t: dict) -> list:
    out = []
    for r in conn.execute(
            """SELECT from_type, from_id FROM record_links WHERE link_type='blocks'
               AND to_type='todo' AND to_id=? AND removed_at IS NULL""", (t["id"],)):
        if r["from_type"] == "todo":
            b = conn.execute("SELECT status, title FROM todos WHERE id = ?", (r["from_id"],)).fetchone()
            if b and (b["status"] or "pending") in OPEN:
                out.append(f"todo #{r['from_id']} {links._one_line(b['title'], 50)}")
        else:
            b = conn.execute(f"SELECT title FROM problems WHERE id = ? AND {_OPEN_PROBLEM}",
                             (r["from_id"],)).fetchone()
            if b:
                out.append(f"problem #{r['from_id']} {links._one_line(b['title'], 50)}")
    if t.get("blocked_by_problem_id"):
        b = conn.execute(f"SELECT title FROM problems WHERE id = ? AND {_OPEN_PROBLEM}",
                         (t["blocked_by_problem_id"],)).fetchone()
        if b:
            out.append(f"problem #{t['blocked_by_problem_id']} {links._one_line(b['title'], 50)}")
    return out


def what_next(db_path: Path, pid: int, limit: int = 5) -> dict:
    conn = links.connect(db_path)
    try:
        has_links = links.available(conn)
        todos = [dict(r) for r in conn.execute(
            "SELECT * FROM todos WHERE project_id = ? AND (status IS NULL OR status IN "
            "('pending', 'in_progress', 'blocked'))", (pid,))]
        ready, blocked = [], []
        for t in todos:
            why = _open_blockers(conn, t) if has_links else []
            if (t["status"] == "blocked") and not why:
                why = ["marked blocked"]
            kids = []
            if has_links:
                kids = [r[0] for r in conn.execute(
                    """SELECT l.from_id FROM record_links l JOIN todos c ON c.id = l.from_id
                       WHERE l.link_type='subtask_of' AND l.to_type='todo' AND l.to_id=?
                       AND l.removed_at IS NULL AND (c.status IS NULL OR c.status IN
                       ('pending', 'in_progress', 'blocked'))""", (t["id"],))]
            parent = None
            if has_links:
                p = conn.execute("""SELECT to_id FROM record_links WHERE link_type='subtask_of'
                                    AND from_type='todo' AND from_id=? AND removed_at IS NULL""",
                                 (t["id"],)).fetchone()
                parent = p[0] if p else None
            item = {"id": t["id"], "title": t["title"], "priority": priority.label(t["priority"]),
                    "status": t["status"] or "pending", "_rank": priority.rank(t["priority"])}
            if parent:
                item["subtask_of"] = parent
            if t.get("due_date"):
                item["due"] = t["due_date"]
            urls = [r[0] for r in conn.execute(
                """SELECT url FROM record_links WHERE from_type='todo' AND from_id=?
                   AND to_type='url' AND removed_at IS NULL""", (t["id"],))] if has_links else []
            if urls:
                item["urls"] = urls
            if why:
                blocked.append({**item, "blocked_by": why})
            elif kids:
                continue  # do the open subtasks first; they are listed themselves
            else:
                ready.append(item)
        ready.sort(key=lambda i: (i["_rank"], 0 if i["status"] == "in_progress" else 1,
                                  i.get("due") or "9999", i["id"]))
        for i in ready + blocked:
            i.pop("_rank", None)
        return {"project": links.project_name(conn, pid), "next": ready[:max(1, int(limit or 5))],
                "ready_total": len(ready), "blocked": blocked[:10], "blocked_total": len(blocked),
                "note": "Ready = open, nothing open blocking it, no open subtasks."}
    finally:
        conn.close()


SORTS = ("priority", "due", "newest")


def list_todos(pid: int, status=None, stored_priority=None, min_priority=None, sort=None,
               compact: bool = False) -> list:
    """`todo list`: the older tool's rows plus `priority_level` (the new name), filtered to
    `min_priority` or more urgent, sorted by priority (default), due date or newest."""
    from . import tools
    from .tools.utils import _compact_list
    rows = tools.get_todos(pid, status, stored_priority, False)
    if min_priority:
        keep = set(priority.at_least(min_priority))
        rows = [r for r in rows if (r.get("priority") or priority.DEFAULT) in keep]
    sort = sort or "priority"
    if sort not in SORTS:
        raise TaskError("bad_sort", f"sort must be one of {', '.join(SORTS)}.")
    if sort == "priority":
        rows.sort(key=lambda r: priority.rank(r.get("priority")))   # stable: newest first within a level
    elif sort == "due":
        rows.sort(key=lambda r: (r.get("due_date") is None, str(r.get("due_date") or ""),
                                 priority.rank(r.get("priority"))))
    else:
        rows.sort(key=lambda r: (str(r.get("created_at") or ""), r.get("id") or 0), reverse=True)
    for r in rows:
        r["priority_level"] = priority.label(r.get("priority"))
    return _compact_list(rows) if compact else rows
