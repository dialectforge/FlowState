"""Every write must name a project.

A write is refused unless the call carries `project` (name or id) or `project_id`
that resolves to exactly one existing project. If the write targets an existing record,
that record must belong to the named project. Error codes: project_required,
project_not_found, project_mismatch, record_not_found.
"""

import sqlite3
from pathlib import Path
from typing import Optional

from . import dbconn
from .brief import resolve_project

# (tool, action) -> argument holding the target record id, or None for creates.
# action None = tool without actions.
WRITES = {
    ("update_project", None): None,
    ("create_component", None): None,
    ("update_component", None): "component_id",
    ("log_learning", None): None,
    ("log_change", None): "component_id",
    ("log_work", None): None,
    ("problem", "log"): "component_id",
    ("problem", "solve"): "problem_id",
    ("attempt", "log"): "problem_id",
    ("attempt", "outcome"): "attempt_id",
    ("todo", "add"): None,
    ("todo", "update"): "todo_id",
    ("session", "start"): None,
    ("session", "end"): "session_id",
    ("session", "log_conversation"): None,
    ("file", "attach"): None,
    ("file", "remove"): "attachment_id",
    ("variable", "create"): None,
    ("variable", "update"): "variable_id",
    ("variable", "delete"): "variable_id",
    ("method", "create"): None,
    ("method", "update"): "method_id",
    ("method", "delete"): "method_id",
    ("self_improve", "learn_skill"): None,
    ("self_improve", "confirm_skill"): "skill_id",
    ("self_improve", "save_state"): None,
    ("self_improve", "log_metric"): None,
    ("review_approve", None): "item_id",
    ("review_reject", None): "item_id",
    ("link", "add"): None,
    ("link", "flag_conflict"): None,
    ("resolve_conflict_card", None): "item_id",
    ("todo", "block"): "todo_id",
    ("todo", "unblock"): "todo_id",
    ("todo", "claim"): "todo_id",       # claims.py: who is working on it, until when
    ("todo", "renew"): "todo_id",
    ("todo", "release"): "todo_id",
    ("todo", "handoff"): "todo_id",
    ("history", "undo"): None,
    ("cleanup_suggest", None): None,
    ("import_memory", None): None,     # history.undo checks the edit belongs to the project
    ("link", "remove"): None,   # links.remove checks the project is one end of the link
    # Phase D continuation: these modules check that the rule / note / record is the project's
    ("rules", "add"): None,
    ("rules", "remove"): None,
    ("handoff", "leave"): None,
    ("handoff", "pickup"): None,
    ("verify", "mark"): None,
    ("verify", "source"): None,
    # User-approved deletes (deletes.py): these only queue a request; deletes.py checks the
    # record is the project's. A project names itself; a component is checked here.
    ("delete_request", "project"): None,
    ("delete_request", "component"): "component_id",
    ("delete_request", "record"): None,
}
# Not project records: create_project names itself; git syncs the data repo.
EXEMPT = {"create_project", "git"}
# Writes that are not about one project (install-wide settings): secret-scanned and
# refused for read-only apps, but no project is required.
EXEMPT_ACTIONS = {("review_mode", "set")}

# target argument -> SQL returning the owning project id (NULL allowed for global skills)
_OWNER_SQL = {
    "component_id": "SELECT project_id FROM components WHERE id = ?",
    "problem_id": """SELECT c.project_id FROM problems p JOIN components c ON c.id = p.component_id
                     WHERE p.id = ?""",
    "attempt_id": """SELECT c.project_id FROM solution_attempts a JOIN problems p ON p.id = a.problem_id
                     JOIN components c ON c.id = p.component_id WHERE a.id = ?""",
    "todo_id": "SELECT project_id FROM todos WHERE id = ?",
    "session_id": "SELECT project_id FROM sessions WHERE id = ?",
    "attachment_id": "SELECT project_id FROM attachments WHERE id = ?",
    "variable_id": "SELECT project_id FROM project_variables WHERE id = ?",
    "method_id": "SELECT project_id FROM project_methods WHERE id = ?",
    "skill_id": "SELECT project_id FROM learned_skills WHERE id = ?",
    "item_id": "SELECT project_id FROM review_items WHERE id = ?",
}
_LABEL = {k: k[:-3].replace("_", " ") for k in _OWNER_SQL}
_LABEL["item_id"] = "review item"


# Writes that may target a component another project owns, if this project links to it.
SHARED_COMPONENT_WRITES = {("log_change", None), ("problem", "log")}


def _links_to(db_path: Path, pid: int, cid) -> bool:
    from . import links
    conn = links.connect(db_path)
    try:
        return links.project_links_to(conn, pid, int(cid))
    except (sqlite3.Error, ValueError, TypeError):
        return False
    finally:
        conn.close()


class GuardError(Exception):
    def __init__(self, payload: dict):
        super().__init__(payload.get("message", "refused"))
        self.payload = payload


def writes_text(name: str, args: dict) -> bool:
    """Any call that stores text (guarded writes and the exempt ones): secret-scanned."""
    return (name in EXEMPT or (name, args.get("action")) in EXEMPT_ACTIONS
            or write_key(name, args) is not None)


def is_write(name: str, args: dict) -> bool:
    return name not in EXEMPT and write_key(name, args) is not None


def write_key(name: str, args: dict):
    key = (name, args.get("action"))
    if key in WRITES:
        return key
    if (name, None) in WRITES:
        return (name, None)
    return None


def explicit_project(args: dict):
    """The project the caller named, if any (name or id)."""
    for k in ("project", "project_id", "project_name"):
        v = args.get(k)
        if v not in (None, ""):
            return v
    return None


def _project_name(db_path: Path, pid) -> str:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        return row[0] if row else f"#{pid}"
    finally:
        conn.close()


DETAILS_MAX = 10_000   # log_work details: a few pages; longer text belongs in an attached file


def check_size(name: str, args: dict) -> None:
    """Refuse (never cut) a log_work whose details are over DETAILS_MAX characters."""
    details = args.get("details") if name == "log_work" else None
    if isinstance(details, str) and len(details) > DETAILS_MAX:
        raise GuardError({
            "error": "too_long",
            "message": f"details is {len(details):,} characters; log_work keeps at most "
                       f"{DETAILS_MAX:,}. Nothing was saved. Log the key points, or attach the "
                       f"full text as a file (manage_files attach) and mention it."})


def check_write(db_path: Path, name: str, args: dict) -> int:
    """Return the resolved project id for a write, or raise GuardError."""
    check_size(name, args)
    key = write_key(name, args)
    named = explicit_project(args)
    what = name + (f"(action={args['action']})" if args.get("action") else "")
    if named is None:
        res = resolve_project(db_path, None)
        raise GuardError({
            "error": "project_required",
            "message": (f"{what} writes to FlowState, so it must name a project. Repeat the call "
                        f"with project=\"<project name>\" (or project_id). If this is new work, "
                        f"call manage_projects(action=\"create\") first."),
            "candidates": res.get("candidates", []),
        })
    res = resolve_project(db_path, named, fuzzy=False)
    if "project" not in res:
        raise GuardError({
            "error": "project_not_found",
            "message": f"No single project matches {named!r} ({res.get('reason')}). "
                       f"Use one of the names below, or manage_projects(action=\"create\").",
            "candidates": res.get("candidates", []),
        })
    pid = res["project"]["id"]
    if args.get("project") not in (None, "") and args.get("project_id") not in (None, ""):
        other = resolve_project(db_path, args["project_id"], fuzzy=False)
        if other.get("project", {}).get("id") != pid:
            raise GuardError({"error": "project_mismatch",
                              "message": f"project={args['project']!r} and project_id="
                                         f"{args['project_id']!r} name different projects."})

    target = WRITES.get(key) if key else None
    if target and args.get(target) is not None:
        conn = dbconn.connect(str(db_path), timeout=5.0)
        try:
            row = conn.execute(_OWNER_SQL[target], (args[target],)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise GuardError({"error": "record_not_found",
                              "message": f"No {_LABEL[target]} with id {args[target]}."})
        owner = row[0]
        if (owner is not None and owner != pid and target == "component_id"
                and key in SHARED_COMPONENT_WRITES and _links_to(db_path, pid, args[target])):
            owner = pid  # a shared component this project uses (cross-project link)
        if owner is not None and owner != pid:
            raise GuardError({
                "error": "project_mismatch",
                "message": (f"{_LABEL[target].capitalize()} #{args[target]} belongs to project "
                            f"'{_project_name(db_path, owner)}' (#{owner}), not "
                            f"'{res['project']['name']}'. Check you are filing this in the right "
                            f"project; if so, repeat with project=\"{_project_name(db_path, owner)}\"."),
            })
    return pid
