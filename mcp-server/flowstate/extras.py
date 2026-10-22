"""Calls for the tools added in the Phase D continuation (E2, E3, E5-E7): rules, handoff,
verify, ask. server.handle_call has already run the guard (writes: project resolved and
checked) and the secret scan; `pid` is the resolved project id (None for a read that named
no project, or named one that doesn't exist)."""

from typing import Any, Optional

from . import approvals, handoffs, provenance, rules
from .guard import explicit_project

TOOLS = ("rules", "handoff", "verify", "ask")


def _need_project(pid: Optional[int], args: dict, what: str) -> Optional[dict]:
    if pid is not None:
        return None
    if explicit_project(args) is not None:
        return {"error": "project_not_found",
                "message": f"No single project matches {explicit_project(args)!r}."}
    return {"error": "project_required", "message": f"{what} needs project=\"<name>\"."}


def handle(name: str, pid: Optional[int], args: dict, client: str, db_path) -> Any:
    act = args.get("action")
    try:
        if name == "ask":
            if explicit_project(args) is not None and pid is None:
                return _need_project(pid, args, "ask")
            from . import ask
            return ask.ask(db_path, args.get("question") or "", pid, args.get("limit") or 5)
        bad = _need_project(pid, args, f"{name}({act})")
        if bad:
            return bad
        if name == "rules":
            if act == "add":
                return rules.add(db_path, pid, args.get("rule") or "", client)
            if act == "remove":
                return rules.remove(db_path, pid, args.get("rule_id"), args.get("user_request") or "",
                                    client)
            if act == "list":
                return rules.list_rules(db_path, pid, bool(args.get("include_removed")))
            return {"error": "bad_action", "message": "action must be add, list or remove."}
        if name == "handoff":
            if act == "leave":
                return handoffs.leave(db_path, pid, args.get("where_i_was") or "",
                                      args.get("next_step") or "", client)
            if act == "pickup":
                return handoffs.pickup(db_path, pid, args.get("handoff_id"), client)
            if act == "list":
                return handoffs.list_notes(db_path, pid, bool(args.get("include_picked_up")),
                                           args.get("limit") or 20)
            return {"error": "bad_action", "message": "action must be leave, list or pickup."}
        if name == "verify":
            if act == "stale":
                return provenance.stale(db_path, pid, args.get("days"), args.get("limit") or 20)
            if act in ("mark", "source"):
                if act == "source" and not args.get("source"):
                    return {"error": "source_required", "message": "Give source: "
                            + ", ".join(provenance.SOURCES) + "."}
                out = provenance.mark(db_path, pid, args.get("record_type"), args.get("record_id"),
                                      client, args.get("source"), verified=(act == "mark"))
                if out.get("confirm_requested") and approvals.is_on(db_path, pid):
                    from .server import _user_only   # popup / phone / terminal, as for deletes
                    return _user_only(out, out["confirm_requested"], client)
                return out
            return {"error": "bad_action", "message": "action must be stale, mark or source."}
    except (rules.RuleError, handoffs.HandoffError) as e:
        return e.payload
    except provenance.MetaError as e:
        return e.payload
    return {"error": f"Unknown tool: {name}"}
