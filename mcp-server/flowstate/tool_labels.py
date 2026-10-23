"""MCP tool annotations (read vs write labels) and the listing profiles.

Clients use the labels to auto-allow read-only tools and to ask before tools that change
or remove things. MCP annotations are per tool, so a tool's hints are the most cautious of
its actions; the per-action split is published in the tool's `_meta["flowstate/actions"]`
(and said in its description), so `manage_todos(action="list")` is still known to be a read.

destructive = an action overwrites or removes stored content (an update, an undo, a review
decision or settings change, a delete, a push). A delete action is destructive even though it
only queues a request: approving it (manage_review) removes data. Adding rows, or setting a flag that keeps
the content (a handoff picked up, a link or rule removed but kept and listable), is additive.

Profiles (FLOWSTATE_TOOLS or `flowstate --tools`): unset = the 12 default tools; core = the
6 everyday tools; full (or all, the older spelling) = the default tools plus the 6 specialist
ones. Every profile still answers every tool name, including the older names (surface.py).
"""

import os

from mcp.types import ToolAnnotations

# name: (title, {"read": [...], "additive": [...], "destructive": [...]}). A tool without actions has
# one key. Every public tool must be listed (a test checks).
LABELS = {
    "start_here": ("Project brief (start here)", {"read": ["*"]}),
    "log_work": ("Log work", {"additive": ["*"]}),
    "search": ("Search and ask", {"read": ["*"]}),
    "manage_todos": ("Todos", {"read": ["list", "next"],
                               "additive": ["add", "block", "claim", "renew", "release", "handoff"],
                               "destructive": ["update", "unblock"]}),
    "manage_problems": ("Problems and attempts", {"read": ["list", "tree"], "additive": ["add_attempt"],
                                                  "destructive": ["end_attempt", "solve"]}),
    "manage_review": ("Review queue (user decides)",
                      {"read": ["list", "get_mode"],
                       "destructive": ["walk", "approve", "reject", "set_mode"]}),
    "manage_handoffs": ("Handoff notes", {"read": ["list"], "additive": ["leave", "pickup"]}),
    "manage_rules": ("Hard rules", {"read": ["list"], "additive": ["add", "remove"]}),
    "manage_links": ("Cross-project links", {"read": ["list"],
                                            "additive": ["add", "remove", "flag_conflict"]}),
    "manage_history": ("Edit history, undo, who did what", {"read": ["list", "who"],
                                                            "destructive": ["undo"]}),
    "manage_projects": ("Projects and components",
                        {"read": ["list", "list_components"], "additive": ["create", "add_component"],
                         "destructive": ["update", "update_component", "delete",
                                         "delete_component"]}),
    "manage_memory": ("Memory upkeep (clean-up, import, fact checks)",
                      {"read": ["stale"], "additive": ["cleanup", "import", "mark_checked"],
                       "destructive": ["set_source", "delete"]}),
    "manage_sessions": ("Sessions", {"read": ["current", "list", "conversations"],
                                     "additive": ["start"], "destructive": ["end"]}),
    "manage_files": ("File attachments", {"read": ["list", "search"], "additive": ["attach"],
                                          "destructive": ["remove"]}),
    "manage_data_git": ("Data folder git (not your repo)",
                        {"read": ["status", "history"], "additive": ["init", "set_remote", "clone"],
                         "destructive": ["sync"]}),
    "manage_variables": ("Project settings", {"read": ["list"], "additive": ["create"],
                                              "destructive": ["update", "delete"]}),
    "manage_methods": ("Project procedures", {"read": ["list"], "additive": ["create"],
                                              "destructive": ["update", "delete"]}),
    "manage_skills": ("App skills and checkpoints",
                      {"read": ["list", "get_state", "list_metrics"],
                       "additive": ["learn", "confirm", "save_state", "log_metric"]}),
}
OPEN_WORLD = {"manage_data_git"}   # talks to a git remote

CORE = ("start_here", "log_work", "search", "manage_todos", "manage_review", "manage_handoffs")
FULL_ONLY = ("manage_sessions", "manage_files", "manage_data_git", "manage_variables",
             "manage_methods", "manage_skills")


def hints(name: str) -> tuple[bool, bool]:
    """(read_only, destructive) for a public tool: the most cautious of its actions."""
    groups = LABELS[name][1]
    read_only = set(groups) == {"read"}
    return read_only, bool(groups.get("destructive"))


def annotate(tool):
    title, groups = LABELS[tool.name]
    ro, destructive = hints(tool.name)
    ann = ToolAnnotations(title=title, readOnlyHint=ro,
                          destructiveHint=None if ro else destructive,
                          idempotentHint=ro,
                          openWorldHint=tool.name in OPEN_WORLD)
    update = {"annotations": ann}
    if "*" not in sum(groups.values(), []):
        update["meta"] = {"flowstate/actions": groups}
    return tool.model_copy(update=update)


def profile() -> str:
    p = (os.environ.get("FLOWSTATE_TOOLS") or "default").strip().lower()
    return "full" if p == "all" else p


def visible(tools: list) -> list:
    """The tools to list for the current profile (default, core or full)."""
    p = profile()
    if p == "core":
        return [t for t in tools if t.name in CORE]
    if p == "full":
        return tools
    return [t for t in tools if t.name not in FULL_ONLY]
