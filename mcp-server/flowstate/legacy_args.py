"""Arguments of the older (v2.0) tool names, moved out of server.py: priority names in
both vocabularies, and the older create tools as log_work arguments so the review mode can
hold them."""

from pathlib import Path
from typing import Optional

from . import links, priority


def component_name(db_path: Path, cid) -> Optional[str]:
    if cid is None:
        return None
    conn = links.connect(db_path)
    try:
        row = conn.execute("SELECT name FROM components WHERE id = ?", (cid,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def priority_args(name: str, args: dict) -> None:
    """Accept both priority vocabularies; the tools below see the stored (old) names."""
    act = args.get("action")
    keys = ()
    if name == "todo" and act in ("add", "update", "list"):
        keys = ("priority",)
    elif name in ("log_work", "review_approve"):
        keys = ("priority",)
    elif name == "problem" and act == "log":
        keys = ("severity",)
    for k in keys:
        if args.get(k) not in (None, ""):
            args[k] = priority.store(args[k])
    if name == "todo" and args.get("min_priority") not in (None, ""):
        priority.store(args["min_priority"])   # validate; tasks.list_todos filters


def legacy_record(db_path: Path, name: str, args: dict, pid: Optional[int]) -> Optional[dict]:
    """The older create tools as log_work arguments, so the review mode can hold them."""
    if pid is None:
        return None
    act = args.get("action")
    if name not in ("todo", "log_learning", "log_change", "problem", "session"):
        return None
    if name == "todo" and act == "add" and args.get("title"):
        d = {"kind": "todo", "summary": args["title"], "details": args.get("description"),
             "priority": args.get("priority")}
    elif name == "log_learning" and args.get("insight"):
        d = {"kind": "learning", "summary": args["insight"], "details": args.get("context"),
             "category": args.get("category")}
    elif name == "log_change" and args.get("field_name"):
        extra = "; ".join(f"{k}: {args[k]}" for k in ("reason", "old_value", "new_value")
                          if args.get(k))
        d = {"kind": "change", "summary": args["field_name"], "details": extra or None}
    elif name == "problem" and act == "log" and args.get("title"):
        d = {"kind": "problem", "summary": args["title"], "details": args.get("description"),
             "priority": args.get("severity")}
    elif name == "session" and act == "log_conversation" and args.get("key_decisions"):
        d = {"kind": "decision", "summary": str(args["key_decisions"][0]),
             "details": args.get("assistant_response_summary") or args.get("user_prompt_summary")}
    else:
        return None
    d.update(project=pid, component=component_name(db_path, args.get("component_id")))
    return {k: v for k, v in d.items() if v not in (None, "")}
