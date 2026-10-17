"""Read vs write labels on every tool (MCP tool annotations) and the small "core" list.

Clients use the labels to auto-allow read-only tools and to ask before tools that change
or remove things. FLOWSTATE_TOOLS=core (or `flowstate --tools core`) lists only the core
tools, to save context in every chat; the other tools still answer if a client calls them.
"""

import os

from mcp.types import ToolAnnotations

# name: (title, read_only, destructive). destructive = may change or remove existing data
# (an update or delete); False = only adds. Every tool must be listed (a test checks).
LABELS = {
    "start_here": ("Project brief (start here)", True, False),
    "log_work": ("Log work", False, False),
    "review_list": ("Review queue", True, False),
    "review_approve": ("Approve review item (user only)", False, True),
    "review_reject": ("Reject review item (user only)", False, True),
    "link": ("Cross-project links", False, True),
    "review_mode": ("Review mode (user only)", False, True),
    "history": ("Edit history and undo", False, True),
    "import_memory": ("Import memories to review", False, False),
    "cleanup_suggest": ("Suggest clean-ups (to review)", False, False),
    "rules": ("Hard rules", False, True),
    "handoff": ("Handoff notes", False, True),
    "verify": ("Verify facts and sources", False, True),
    "ask": ("Ask a question", True, False),
    "list_projects": ("List projects", True, False),
    "create_project": ("Create project", False, False),
    "update_project": ("Update project", False, True),
    "get_context": ("Project context", True, False),
    "create_component": ("Create component", False, False),
    "list_components": ("List components", True, False),
    "update_component": ("Update component", False, True),
    "log_learning": ("Log learning", False, False),
    "get_learnings": ("Get learnings", True, False),
    "log_change": ("Log change", False, False),
    "get_recent_changes": ("Recent changes", True, False),
    "search": ("Search", True, False),
    "problem": ("Problems", False, True),
    "attempt": ("Solution attempts", False, True),
    "todo": ("Todos", False, True),
    "session": ("Sessions", False, True),
    "file": ("File attachments", False, True),
    "git": ("Data folder git", False, True),
    "variable": ("Project variables", False, True),
    "method": ("Project methods", False, True),
    "self_improve": ("Skills and state", False, True),
}
OPEN_WORLD = {"git"}   # talks to a git remote

CORE = ("start_here", "log_work", "search", "todo", "link", "review_list", "review_approve",
        "review_reject", "handoff")


def annotate(tool):
    title, ro, destructive = LABELS[tool.name]
    ann = ToolAnnotations(title=title, readOnlyHint=ro,
                          destructiveHint=None if ro else destructive,
                          idempotentHint=True if ro else None,
                          openWorldHint=tool.name in OPEN_WORLD)
    return tool.model_copy(update={"annotations": ann})


def profile() -> str:
    return (os.environ.get("FLOWSTATE_TOOLS") or "all").strip().lower()


def visible(tools: list) -> list:
    """The tools to list for the current profile (core or all)."""
    if profile() == "core":
        return [t for t in tools if t.name in CORE]
    return tools
