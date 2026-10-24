"""Tool definitions: what every MCP client sees (names, descriptions, schemas, labels).

v2.1 public surface (see surface.py for how each public call becomes the internal operation
the rest of the server runs, and how the older tool names keep working). Naming: every
listed name starts with a verb; tools with several operations are manage_<things> and take
`action`. Descriptions live in agent_guide.py; labels and profiles in tool_labels.py.
"""

from mcp.types import Tool

from . import agent_guide as guide
from . import deletes, history, links, meaning, priority, provenance, review_mode, tool_labels
from .brief import SIZE_NAMES

_PRIORITY = {"type": "string", "enum": list(priority.ACCEPTED)}
_LEARNING_CATEGORIES = ["pattern", "gotcha", "best_practice", "tool_tip", "architecture",
                        "performance", "security", "other"]
PROJECT = guide.PROJECT_PROPERTY
MODEL = guide.MODEL_PROPERTY
USER_REQUEST = guide.USER_REQUEST_PROPERTY


def _p(desc: str, type_="string", **kw) -> dict:
    return {"type": type_, "description": desc, **kw}


def _action(actions) -> dict:
    return {"type": "string", "enum": list(actions),
            "description": "The operation; see the tool description."}


def _tool(name: str, description: str, props: dict, required=()) -> Tool:
    schema = {"type": "object", "properties": props}
    if required:
        schema["required"] = list(required)
    return Tool(name=name, description=description, inputSchema=schema)


# ============================================================
# DEFAULT LIST (12 tools)
# ============================================================

START_HERE = _tool("start_here", guide.START_HERE_DESCRIPTION, {
    "project": _p("Name or id. Omit to list projects.", ["string", "integer"]),
    "folder": _p("Working folder, if no project is given."),
    "size": _p("quick, standard (default) or deep.", enum=list(SIZE_NAMES), default="standard"),
})

LOG_WORK = _tool("log_work", guide.LOG_WORK_DESCRIPTION, {
    "project": PROJECT,
    "kind": _p("The record kind (see the description).",
               enum=["decision", "change", "problem", "learning", "todo"]),
    "summary": _p("The record in one short line."),
    "details": _p("Why, or old and new values. Max 10,000 chars."),
    "component": _p("Component name; created if missing. Shared ones too."),
    "priority": {**_PRIORITY, "description": "todo or problem. Default normal."},
    "category": _p("learning only: the kind of lesson.", enum=_LEARNING_CATEGORIES),
    "suggested": _p("true = queue it for the user's review.", "boolean"),
    "replaces": _p("Decision id this one replaces.", "integer"),
    "evidence": {"type": "array", "items": {"type": "string"},
                 "description": "Up to 2: file:line or output: text."},
    "conflicts_with": _p("Record id this disagrees with.", "integer"),
    "conflicts_with_type": _p("Default decision.", enum=list(links.ENDPOINTS)),
    "topic": _p("Short topic, e.g. API sessions."),
    "source": _p("user_stated, ai_inferred or from_code.",
                 enum=list(provenance.SOURCES)),
    "model": MODEL,
}, ["project", "kind", "summary"])

SEARCH = _tool("search", guide.SEARCH_DESCRIPTION, {
    "query": _p("Words, or a question when answer=true. Omit to list newest."),
    "answer": _p("true = answer the query, quoting records.", "boolean"),
    "project": _p("Omit to search every project.", ["string", "integer"]),
    "content_types": {"type": "array", "items": {"type": "string", "enum": list(meaning.TYPES)},
                      "description": "Only these record types. Omit for all."},
    "since_hours": _p("No query only: from the last N hours.", "integer"),
    "mode": _p("auto, keyword or meaning.", enum=["auto", "keyword", "meaning"], default="auto"),
    "limit": _p("Default 10, max 50.", "integer"),
})

_TODO_ACTIONS = ("list", "next", "update", "block", "unblock", "add", "claim", "renew", "release",
                 "handoff")
MANAGE_TODOS = _tool("manage_todos", guide.TODOS_DESCRIPTION, {
    "action": _action(_TODO_ACTIONS),
    "project": PROJECT,
    "todo_id": _p("#id in the brief.", "integer"),
    "status": _p("update: new. list: filter.",
                 enum=["pending", "in_progress", "blocked", "done", "cancelled"]),
    "priority": {**_PRIORITY, "description": "add / update. list: filter."},
    "title": _p("add (required) / update."),
    "description": _p("add / update."),
    "min_priority": {**_PRIORITY, "description": "list: this or more urgent."},
    "sort": _p("list: default priority.", enum=["priority", "due", "newest"]),
    "limit": _p("next: how many (default 5).", "integer"),
    "compact": _p("list: shorter rows.", "boolean"),
    "parent_todo_id": _p("add: parent todo id.", "integer"),
    "blocked_by": {"type": "array", "items": {"type": "integer"},
                   "description": "Todo ids it waits on."},
    "blocked_by_problem": _p("Problem id it waits on.", "integer"),
    "due_date": _p("add: ISO due date."),
    "component_id": _p("add: component id.", "integer"),
    "minutes": _p("claim / renew: 1-120, default 15.", "integer"),
    "where_i_was": _p("handoff: your progress."),
    "next_step": _p("handoff: the next step."),
    "model": MODEL,
}, ["action", "project"])

_PROBLEM_ACTIONS = ("list", "tree", "add_attempt", "end_attempt", "solve")
MANAGE_PROBLEMS = _tool("manage_problems", guide.PROBLEMS_DESCRIPTION, {
    "action": _action(_PROBLEM_ACTIONS),
    "project": _p("Project name or id. Required for writes; list: omit for all projects.",
                  ["string", "integer"]),
    "problem_id": _p("tree / add_attempt / solve: the problem's id.", "integer"),
    "description": _p("add_attempt: what you tried (required)."),
    "parent_attempt_id": _p("add_attempt: an earlier attempt this one builds on.", "integer"),
    "attempt_id": _p("end_attempt: the attempt's id (from add_attempt or tree).", "integer"),
    "outcome": _p("end_attempt: how it ended (required).",
                  enum=["success", "failure", "partial", "abandoned"]),
    "notes": _p("end_attempt: what you learned."),
    "confidence": _p("end_attempt: how sure. Default attempted.",
                     enum=["attempted", "worked_once", "verified", "proven", "deprecated"]),
    "summary": _p("solve: what fixed it (required)."),
    "key_insight": _p("solve: the lesson, one sentence."),
    "winning_attempt_id": _p("solve: the attempt that worked.", "integer"),
    "code_snippet": _p("solve: the fix as text. No secrets."),
    "component_id": _p("list: only this component's problems.", "integer"),
    "compact": _p("list: shorter rows.", "boolean"),
    "model": MODEL,
}, ["action"])

_REVIEW_ACTIONS = ("list", "walk", "get_mode", "approve", "reject", "set_mode")
MANAGE_REVIEW = _tool("manage_review", guide.REVIEW_DESCRIPTION, {
    "action": _action(_REVIEW_ACTIONS),
    "project": _p("Required to approve or reject.", ["string", "integer"]),
    "status": _p("list: which ones. Default pending.",
                 enum=["pending", "approved", "rejected", "all"]),
    "limit": _p("list: how many (20; max 100).", "integer"),
    "after": _p("list, walk: the page after this id.", "integer"),
    "item_id": _p("list: one in full. approve / reject: its id.", "integer"),
    "user_request": {**USER_REQUEST, "description": "The user's own words (required)."},
    "reason": _p("reject: the user's reason (required)."),
    "summary": _p("approve: new summary."),
    "details": _p("approve: new details."),
    "priority": {**_PRIORITY, "description": "approve: for a todo or problem."},
    "category": _p("approve: for a learning.", enum=_LEARNING_CATEGORIES),
    "component": _p("approve: component to file it under."),
    "move_to": _p("Project to move a misfiled item to.", ["string", "integer"]),
    "mode": _p("set_mode: the new mode (default auto_only).",
               enum=list(review_mode.MODES)),
    "kinds": {"type": "array", "items": {"type": "string", "enum": list(review_mode.KINDS)},
              "description": "set_mode, mode=types: kinds that wait."},
    "clear": _p("set_mode: drop this project's override.", "boolean"),
    "confirm_name": _p("Project delete: the exact name."),
    "keep": _p("Disagreement: a (older), b (newer) or both.",
               enum=["a", "b", "both"]),
    "model": MODEL,
}, ["action"])

_HANDOFF_ACTIONS = ("leave", "pickup", "list")
MANAGE_HANDOFFS = _tool("manage_handoffs", guide.HANDOFFS_DESCRIPTION, {
    "action": _action(_HANDOFF_ACTIONS),
    "project": PROJECT,
    "where_i_was": _p("leave: progress so far. Max 1,000 chars."),
    "next_step": _p("leave: the next step. Max 500 chars."),
    "handoff_id": _p("pickup: the note id.", "integer"),
    "include_picked_up": _p("list: include picked-up notes.", "boolean"),
    "limit": _p("list: max notes. Default 20.", "integer"),
    "model": MODEL,
}, ["action", "project"])

_RULE_ACTIONS = ("add", "list", "remove")
MANAGE_RULES = _tool("manage_rules", guide.RULES_DESCRIPTION, {
    "action": _action(_RULE_ACTIONS),
    "project": PROJECT,
    "rule": _p("add: the rule, one short sentence."),
    "rule_id": _p("remove: the rule's id (R<id> in the brief).", "integer"),
    "user_request": {**USER_REQUEST, "description": "remove (required): the user's own words "
                                                    "asking to remove it."},
    "include_removed": _p("list: also removed rules.", "boolean"),
    "model": MODEL,
}, ["action", "project"])

_LINK_ACTIONS = ("add", "remove", "list", "flag_conflict")
MANAGE_LINKS = _tool("manage_links", guide.LINKS_DESCRIPTION, {
    "action": _action(_LINK_ACTIONS),
    "project": _p("The source project, by name or id. Required.", ["string", "integer"]),
    "link_type": _p("How the source relates to the target: uses, depends_on, part_of, same_as, "
                    "supersedes, subtask_of, blocks, about, or url.", enum=list(links.LINK_TYPES)),
    "component": _p("Source: a component of this project, by name."),
    "item_type": _p("Source: a record type, with item_id.", enum=list(links.ENDPOINTS)),
    "item_id": _p("Source: the record's id.", "integer"),
    "target_project": _p("add: the target project, by name or id.", ["string", "integer"]),
    "target_component": _p("add: a component name in the target project."),
    "target_type": _p("add: a target record type, with target_id.", enum=list(links.ENDPOINTS)),
    "target_id": _p("add: the target record's id.", "integer"),
    "url": _p("add: an https URL to attach to the source record."),
    "note": _p("add: a short note kept on the link."),
    "topic": _p("flag_conflict: what they disagree about, in plain words."),
    "evidence": {"type": "array", "items": {"type": "string"},
                 "description": "flag_conflict: up to 2 snippets for the source record."},
    "target_evidence": {"type": "array", "items": {"type": "string"},
                        "description": "flag_conflict: up to 2 snippets for the other record."},
    "link_id": _p("remove: the link's id (from list); or give the add fields instead.", "integer"),
    "include_removed": _p("list: also removed links.", "boolean"),
    "model": MODEL,
}, ["action", "project"])

_HISTORY_ACTIONS = ("list", "who", "undo")
MANAGE_HISTORY = _tool("manage_history", guide.HISTORY_DESCRIPTION, {
    "action": _action(_HISTORY_ACTIONS),
    "project": _p("Project name or id. Required for list and undo; who: omit for all.",
                  ["string", "integer"]),
    "record_type": _p("list: only this record type. undo: required.", enum=list(history.TABLES)),
    "record_id": _p("list: only this record. undo: required.", "integer"),
    "limit": _p("list / who: max rows. Default 20, max 100.", "integer"),
    "by_app": _p("who: only apps whose name contains this."),
    "by_model": _p("who: only models whose name contains this."),
    "by_machine": _p("who: only this machine or workspace."),
    "since": _p("who: from this local time, such as 2026-10-17."),
    "until": _p("who: up to this local time."),
    "page": _p("who: page number. Default 1.", "integer"),
    "model": MODEL,
}, ["action"])

_PROJECT_ACTIONS = ("list", "list_components", "create", "add_component", "update",
                    "update_component", "delete", "delete_component")
MANAGE_PROJECTS = _tool("manage_projects", guide.PROJECTS_DESCRIPTION, {
    "action": _action(_PROJECT_ACTIONS),
    "project": _p("Every action but list and create: the project, by name or id.",
                  ["string", "integer"]),
    "name": _p("create / add_component: the new name (required, unique). update / "
               "update_component: a new name."),
    "description": _p("create / update / add_component / update_component: what it is."),
    "status": _p("list: only this status. update: active, paused, completed or archived. "
                 "update_component: planning, in_progress, testing, complete or deprecated.",
                 enum=["active", "paused", "completed", "archived", "planning", "in_progress",
                       "testing", "complete", "deprecated"]),
    "component_id": _p("update_component / delete_component: the component's id "
                       "(list_components shows ids).", "integer"),
    "parent_component_id": _p("add_component: a parent component in the same project.", "integer"),
    "model": MODEL,
}, ["action"])

_MEMORY_ACTIONS = ("stale", "cleanup", "import", "mark_checked", "set_source", "delete")
MANAGE_MEMORY = _tool("manage_memory", guide.MEMORY_DESCRIPTION, {
    "action": _action(_MEMORY_ACTIONS),
    "project": PROJECT,
    "kinds": {"type": "array", "items": {"type": "string", "enum": ["duplicate", "stale", "misfiled"]},
              "description": "cleanup: which checks. Omit for all three."},
    "text": _p("import: the file contents. Max 200,000 chars."),
    "format": _p("import: memory (default; one memory per line or bullet) or flowstate_markdown "
                 "(an edited `flowstate export-md` file).", enum=["memory", "flowstate_markdown"]),
    "origin": _p("import: where it came from, such as Claude or ChatGPT."),
    "kind": _p("import (memory format): record kind for each memory. Default learning.",
               enum=["learning", "decision", "todo", "problem", "change"]),
    "record_type": _p("delete / mark_checked / set_source: the record's type (the last two: "
                      f"{', '.join(provenance.TABLES)}).", enum=list(deletes.RECORD_TYPES)),
    "record_id": _p("delete / mark_checked / set_source: the record's id.", "integer"),
    "source": _p("set_source (required) / mark_checked (optional): user_stated, ai_inferred or "
                 "from_code.", enum=list(provenance.SOURCES)),
    "days": _p("stale: unchecked for this many days. Default 60.", "integer"),
    "limit": _p("stale: max facts. Default 20.", "integer"),
    "model": MODEL,
}, ["action", "project"])


# ============================================================
# FULL PROFILE ONLY (FLOWSTATE_TOOLS=full): specialist tools
# ============================================================

_SESSION_ACTIONS = ("current", "list", "conversations", "start", "end")
MANAGE_SESSIONS = _tool("manage_sessions", guide.SESSIONS_DESCRIPTION, {
    "action": _action(_SESSION_ACTIONS),
    "project": _p("Project name or id. Required except for current.", ["string", "integer"]),
    "session_id": _p("end: the session's id (required). conversations: only this session.", "integer"),
    "summary": _p("end: what was accomplished."),
    "outcomes": {"type": "array", "items": {"type": "string"}, "description": "end: short outcome lines."},
    "focus_component_id": _p("start: component in focus (ignored if a session is open).", "integer"),
    "focus_problem_id": _p("start: problem in focus (ignored if a session is open).", "integer"),
    "limit": _p("list / conversations: max rows. Default 20.", "integer"),
    "model": MODEL,
}, ["action"])

_FILE_ACTIONS = ("list", "search", "attach", "remove")
MANAGE_FILES = _tool("manage_files", guide.FILES_DESCRIPTION, {
    "action": _action(_FILE_ACTIONS),
    "project": _p("Project name or id. Required for attach and remove; list / search filter.",
                  ["string", "integer"]),
    "file_path": _p("attach: path of the file on this computer (required)."),
    "component_id": _p("attach: component to attach to. list: filter.", "integer"),
    "problem_id": _p("attach: problem to attach to. list: filter.", "integer"),
    "user_description": _p("attach: why this file matters."),
    "tags": {"type": "array", "items": {"type": "string"}, "description": "attach: tags."},
    "copy_to_bundle": _p("attach: copy into the bundle (default true) or store the path only.",
                         "boolean", default=True),
    "attachment_id": _p("remove: the attachment's id (required).", "integer"),
    "delete_file": _p("remove: also delete the bundle copy for good.", "boolean"),
    "query": _p("search: text to find in attached files (required)."),
    "file_types": {"type": "array", "items": {"type": "string"},
                   "description": "search: extensions without a dot, such as md or py."},
    "limit": _p("search: max hits. Default 10.", "integer"),
    "model": MODEL,
}, ["action"])

_GIT_ACTIONS = ("status", "history", "init", "set_remote", "clone", "sync")
MANAGE_DATA_GIT = _tool("manage_data_git", guide.DATA_GIT_DESCRIPTION, {
    "action": _action(_GIT_ACTIONS),
    "commit_message": _p("sync: commit subject. Omit for an automatic one."),
    "remote_url": _p("set_remote / clone: the git URL (required)."),
    "local_path": _p("clone: an empty target folder. Default: the data folder."),
    "limit": _p("history: max commits. Default 20.", "integer"),
}, ["action"])

_VAR_ACTIONS = ("list", "create", "update", "delete")
MANAGE_VARIABLES = _tool("manage_variables", guide.VARIABLES_DESCRIPTION, {
    "action": _action(_VAR_ACTIONS),
    "project": PROJECT,
    "variable_id": _p("update / delete: the variable's id (required).", "integer"),
    "name": _p("create: name, unique per project and category (required). update: a new name."),
    "value": _p("create / update: the setting as text. Never a secret."),
    "description": _p("create / update: what it is for."),
    "category": _p("create / update: the group, default custom. list: filter.",
                   enum=["server", "credentials", "config", "environment", "endpoint", "custom"]),
    "is_secret": _p("create / update: label as sensitive. It is still returned by list.", "boolean"),
    "model": MODEL,
}, ["action", "project"])

_METHOD_ACTIONS = ("list", "create", "update", "delete")
MANAGE_METHODS = _tool("manage_methods", guide.METHODS_DESCRIPTION, {
    "action": _action(_METHOD_ACTIONS),
    "project": PROJECT,
    "method_id": _p("update / delete: the method's id (required).", "integer"),
    "name": _p("create: name, unique in the project (required). update: a new name."),
    "description": _p("create (required) / update: what the procedure is."),
    "category": _p("create / update: the group. list: filter.",
                   enum=["auth", "deployment", "testing", "architecture", "workflow", "convention",
                         "api", "security", "other"]),
    "steps": {"type": "array", "items": {"type": "string"},
              "description": "create / update: ordered steps. update replaces the whole list."},
    "code_example": _p("create / update: sample code. No secrets."),
    "related_component_id": _p("create / update: the component it belongs to.", "integer"),
    "model": MODEL,
}, ["action", "project"])

_SKILL_ACTIONS = ("list", "get_state", "list_metrics", "learn", "confirm", "save_state", "log_metric")
MANAGE_SKILLS = _tool("manage_skills", guide.SKILLS_DESCRIPTION, {
    "action": _action(_SKILL_ACTIONS),
    "project": _p("Project name or id. Required except for list and list_metrics; list adds "
                  "global skills.", ["string", "integer"]),
    "skill": _p("learn: what was learned (required)."),
    "skill_type": _p("learn (required) / list (filter): the kind of skill.",
                     enum=["tool_capability", "user_preference", "approach", "gotcha",
                           "project_specific"]),
    "context": _p("learn: when it applies. log_metric: where it was scored."),
    "tool_name": _p("learn: the related tool. list: filter."),
    "promoted_only": _p("list: only promoted skills.", "boolean"),
    "skill_id": _p("confirm: the skill's id (required).", "integer"),
    "focus_summary": _p("save_state: what the session was doing."),
    "key_facts": {"type": "array", "items": {"type": "string"}, "description": "save_state: facts to keep."},
    "pending_decisions": {"type": "array", "items": {"type": "string"},
                          "description": "save_state: decisions not made yet."},
    "active_problem_ids": {"type": "array", "items": {"type": "integer"},
                           "description": "save_state: problem ids in progress."},
    "active_component_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "save_state: component ids in progress."},
    "metric_type": _p("log_metric (required) / list_metrics (filter): which metric.",
                      enum=["checkpoint_timing", "tool_choice", "response_quality",
                            "prediction_accuracy", "user_satisfaction"]),
    "effectiveness_score": _p("log_metric: score from 0.0 to 1.0.", "number"),
    "action_taken": _p("log_metric: what was done."),
    "outcome": _p("log_metric: what happened."),
    "model": MODEL,
}, ["action"])


DEFAULT_TOOLS = [START_HERE, LOG_WORK, SEARCH, MANAGE_TODOS, MANAGE_PROBLEMS, MANAGE_REVIEW,
                 MANAGE_HANDOFFS, MANAGE_RULES, MANAGE_LINKS, MANAGE_HISTORY, MANAGE_PROJECTS,
                 MANAGE_MEMORY]
FULL_ONLY_TOOLS = [MANAGE_SESSIONS, MANAGE_FILES, MANAGE_DATA_GIT, MANAGE_VARIABLES,
                   MANAGE_METHODS, MANAGE_SKILLS]

# Every public tool, annotated; tool_labels.visible() picks the profile's subset.
ALL_TOOLS = [tool_labels.annotate(t) for t in DEFAULT_TOOLS + FULL_ONLY_TOOLS]
