"""The public tool names (v2.1) and the older names, mapped onto the internal operations.

Inside the server every operation keeps the id it has had since v1.5/v2.0: a tool name plus
an optional action (`todo` + `update`, `review_approve`, `verify` + `mark`, ...). The guard,
the secret scan, remote-access policy, edit history, review-mode holds and the activity log
are all keyed on those ids, and earlier versions stored them in the database. So the public
surface is translated once, at the top of call_tool, before any of those run:

    manage_todos(action="update", ...)  ->  todo(action="update", ...)

The older names are the internal ids themselves, so an old client's call runs exactly as
before; it only gets one extra line naming the new tool (hint()).
"""

from typing import Optional

# public tool -> {public action: (internal tool, internal action or None)}
ROUTES: dict[str, dict[str, tuple[str, Optional[str]]]] = {
    "manage_todos": {a: ("todo", a) for a in ("list", "next", "add", "update", "block", "unblock",
                                                       "claim", "renew", "release", "handoff")},
    "manage_problems": {"list": ("problem", "list"), "tree": ("problem", "get_tree"),
                        "solve": ("problem", "solve"), "add_attempt": ("attempt", "log"),
                        "end_attempt": ("attempt", "outcome")},
    "manage_review": {"list": ("review_list", None), "walk": ("review_walk", None),
                      "approve": ("review_approve", None),
                      "reject": ("review_reject", None), "get_mode": ("review_mode", "get"),
                      "set_mode": ("review_mode", "set")},
    "manage_handoffs": {a: ("handoff", a) for a in ("leave", "list", "pickup")},
    "manage_rules": {a: ("rules", a) for a in ("add", "list", "remove")},
    "manage_links": {a: ("link", a) for a in ("add", "remove", "list", "flag_conflict")},
    "manage_history": {a: ("history", a) for a in ("list", "undo", "who")},
    "manage_projects": {"list": ("list_projects", None), "create": ("create_project", None),
                        "update": ("update_project", None),
                        "list_components": ("list_components", None),
                        "add_component": ("create_component", None),
                        "update_component": ("update_component", None),
                        "delete": ("delete_request", "project"),
                        "delete_component": ("delete_request", "component")},
    "manage_memory": {"cleanup": ("cleanup_suggest", None), "import": ("import_memory", None),
                      "stale": ("verify", "stale"), "mark_checked": ("verify", "mark"),
                      "set_source": ("verify", "source"),
                      "delete": ("delete_request", "record")},
    "manage_sessions": {"current": ("session", "current"), "list": ("session", "list_sessions"),
                        "conversations": ("session", "list_conversations"),
                        "start": ("session", "start"), "end": ("session", "end")},
    "manage_files": {a: ("file", a) for a in ("attach", "list", "remove", "search")},
    "manage_data_git": {a: ("git", a) for a in ("init", "status", "sync", "set_remote", "clone",
                                                "history")},
    "manage_variables": {a: ("variable", a) for a in ("create", "list", "update", "delete")},
    "manage_methods": {a: ("method", a) for a in ("create", "list", "update", "delete")},
    "manage_skills": {"learn": ("self_improve", "learn_skill"), "list": ("self_improve", "get_skills"),
                      "confirm": ("self_improve", "confirm_skill"),
                      "save_state": ("self_improve", "save_state"),
                      "get_state": ("self_improve", "get_state"),
                      "log_metric": ("self_improve", "log_metric"),
                      "list_metrics": ("self_improve", "get_metrics")},
}

# Public argument names that differ from the internal ones: (public tool, action) -> renames.
RENAMES = {("manage_memory", "import"): {"origin": "source"}}

# The v2.0.x names that changed (32 of the 35 it listed; start_here, log_work and search kept
# theirs). They stay callable in every profile and are never listed.
OLD_NAMES = (
    "list_projects", "create_project", "update_project", "get_context", "create_component",
    "list_components", "update_component", "log_learning", "get_learnings", "log_change",
    "get_recent_changes", "problem", "attempt", "todo", "session", "file", "git", "variable",
    "method", "self_improve", "review_list", "review_approve", "review_reject", "link",
    "review_mode", "history", "cleanup_suggest", "import_memory", "rules", "handoff", "verify",
    "ask",
)

# Older calls whose new home is not simply the reverse of ROUTES.
_MOVED = {
    ("log_learning", None): 'log_work(kind="learning")',
    ("log_change", None): 'log_work(kind="change")',
    ("problem", "log"): 'log_work(kind="problem")',
    ("session", "log_conversation"): 'log_work(kind="decision")',
    ("get_learnings", None): 'search(content_types=["learning"])',
    ("get_recent_changes", None): 'search(content_types=["change"], since_hours=24)',
    ("get_context", None): 'start_here(size="deep")',
    ("ask", None): "search(query=<question>, answer=true)",
}
_REVERSE = {internal: f'{tool}(action="{action}")'
            for tool, acts in ROUTES.items() for action, internal in acts.items()}


class SurfaceError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def translate(name: str, args: dict) -> tuple[str, dict]:
    """(internal tool, internal arguments) for a public or older tool call. Older names and
    unknown names pass through unchanged (unknown ones fail later as before)."""
    args = dict(args or {})
    if name == "search":
        if args.pop("answer", False):
            out = {"question": args.get("query") or ""}
            for k in ("project", "project_id", "limit"):
                if args.get(k) not in (None, ""):
                    out[k] = args[k]
            return "ask", out
        return "search", args
    acts = ROUTES.get(name)
    if acts is None:
        return name, args
    action = args.get("action")
    if action not in acts:
        raise SurfaceError("bad_action", f"{name} action must be one of: {', '.join(acts)}.")
    internal, iaction = acts[action]
    for public, inner in RENAMES.get((name, action), {}).items():
        if public in args:
            args[inner] = args.pop(public)
    if iaction is None:
        args.pop("action", None)
    else:
        args["action"] = iaction
    return internal, args


def is_old_name(name: str) -> bool:
    return name in OLD_NAMES


def new_call(name: str, args: dict) -> Optional[str]:
    """The v2.1 call for an older tool name and action (None for a current name)."""
    if not is_old_name(name):
        return None
    action = (args or {}).get("action")
    found = next((table[k] for k in ((name, action), (name, None))
                  for table in (_MOVED, _REVERSE) if k in table), None)
    if found is None:   # an older action without its own public spelling
        tool = next((t for t, acts in ROUTES.items()
                     if any(i[0] == name for i in acts.values())), None)
        found = f"{tool}(action=...)" if tool else None
    if name == "list_projects":
        found += " or start_here()"
    return found


def hint(name: str, args: dict) -> Optional[str]:
    call = new_call(name, args)
    if call is None:
        return None
    return (f"FlowState: `{name}` is an older tool name and still works. Next time call "
            f"{call}.")
