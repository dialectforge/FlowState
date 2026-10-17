"""Tool definitions: what every MCP client sees (names, descriptions, schemas, labels).

Split out of server.py (v2 Phase D); server.py lists these and handles the calls.
"""

import json

from mcp.types import Tool

from . import agent_guide as guide
from . import history, links, priority, provenance, review_mode, tool_labels
from .brief import SIZE_NAMES
from .guard import WRITES


# ============================================================
# TOOL DEFINITIONS - v1.5 Consolidated
# ============================================================

TOOLS = [
    # ----------------------------------------
    # PROJECT TOOLS (3 tools)
    # ----------------------------------------
    Tool(
        name="list_projects",
        description=(
            "List projects, newest first (read-only). Use start_here to open a brief. "
            "Returns id, name, description, status, and timestamps."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "completed", "archived"],
                    "description": "Only this status. Omit for all."
                }
            }
        }
    ),
    Tool(
        name="create_project",
        description=(
            "Create a project when the user asks for one. name must be unique. "
            "Duplicates are rejected. Open it with start_here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique project name."},
                "description": {"type": "string", "description": "Longer description. Omit to leave empty."}
            },
            "required": ["name"]
        }
    ),
    Tool(
        name="update_project",
        description=(
            "Change a project's name, description, or status. history undo restores "
            "the row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id to update."},
                "name": {"type": "string", "description": "New unique name. Omit to keep the current one."},
                "description": {"type": "string", "description": "New description. Omit to keep the current one."},
                "status": {"type": "string", "enum": ["active", "paused", "completed", "archived"],
                           "description": "New status. Omit to keep the current one."}
            },
            "required": ["project_id"]
        }
    ),
    
    # ----------------------------------------
    # CONTEXT TOOL (1 tool - replaces 2)
    # ----------------------------------------
    Tool(
        name="get_context",
        description=(
            "Raw context by exact name. Prefer start_here. lean=true (default) is short; "
            "false includes the working records."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Exact project name. Required, not an id."},
                "lean": {"type": "boolean", "description": "Counts and titles (default), or full objects.", "default": True},
                "hours": {"type": "integer", "description": "Hours of changes when lean is false.", "default": 48}
            },
            "required": ["project_name"]
        }
    ),
    
    # ----------------------------------------
    # COMPONENT TOOLS (3 tools)
    # ----------------------------------------
    Tool(
        name="create_component",
        description=(
            "Add a named part of a project. name is unique in it. A change is "
            "log_work(kind=change)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Owning project id, or pass project=."},
                "name": {"type": "string", "description": "Name, unique in the project."},
                "description": {"type": "string", "description": "What this component is."},
                "parent_component_id": {"type": "integer", "description": "Parent component in the same project."}
            },
            "required": ["project_id", "name"]
        }
    ),
    Tool(
        name="list_components",
        description=(
            "List one project's components (read-only). For the working brief, use start_here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id whose components to list."}
            },
            "required": ["project_id"]
        }
    ),
    Tool(
        name="update_component",
        description=(
            "Change a component's name, description, or status. history undo restores "
            "the row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer", "description": "Component id."},
                "name": {"type": "string", "description": "New name, unique in the project. Omit to keep."},
                "description": {"type": "string", "description": "New description. Omit to keep."},
                "status": {"type": "string", "enum": ["planning", "in_progress", "testing", "complete", "deprecated"],
                           "description": "New status. Omit to keep the current one."}
            },
            "required": ["component_id"]
        }
    ),
    
    # ----------------------------------------
    # LEARNING TOOLS (2 tools) - Core memory!
    # ----------------------------------------
    Tool(
        name="log_learning",
        description=(
            "Save a lesson. Prefer log_work(kind=learning). May be queued. Secrets "
            "are refused. Undo does not remove it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id, or pass project=."},
                "insight": {"type": "string", "description": "The lesson, one or two sentences."},
                "component_id": {"type": "integer", "description": "Component this lesson is about."},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                             "description": "Learning category. Omit for none."},
                "context": {"type": "string", "description": "When this lesson applies."},
                "source": {"type": "string", "enum": ["experience", "documentation", "conversation", "error", "research"], "default": "experience",
                           "description": "Where the lesson came from. Default experience."}
            },
            "required": ["project_id", "insight"]
        }
    ),
    Tool(
        name="get_learnings",
        description=(
            "List lessons (read-only). compact=true shortens rows. For a question use "
            "ask; for every record type use search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Only this project. Omit for all."},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                             "description": "Only this category."},
                "compact": {"type": "boolean", "default": False,
                            "description": "Shorter rows: drop empties and timestamps."}
            }
        }
    ),
    
    # ----------------------------------------
    # CHANGE TOOLS (2 tools)
    # ----------------------------------------
    Tool(
        name="log_change",
        description=(
            "Append a change on a component. Prefer log_work(kind=change). Omit "
            "component_id for General. Not undone by history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer", "description": "Component that changed. Omit for General."},
                "field_name": {"type": "string", "description": "Short label of what changed."},
                "old_value": {"type": "string", "description": "Previous value, as text."},
                "new_value": {"type": "string", "description": "New value, as text."},
                "reason": {"type": "string", "description": "Why it changed."},
                "change_type": {"type": "string", "enum": ["config", "code", "architecture", "dependency", "documentation", "other"],
                                "description": "Kind of change. Omit to store none."}
            },
            "required": ["component_id", "field_name"]
        }
    ),
    Tool(
        name="get_recent_changes",
        description=(
            "Logged changes from the last hours (default 24), read-only. Not row-edit history; "
            "for old/new values or undo, use history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Only this project. Omit for all."},
                "component_id": {"type": "integer", "description": "Only this component. Omit for all."},
                "hours": {"type": "integer", "default": 24, "description": "How far back, in hours. Default 24."},
                "compact": {"type": "boolean", "default": False,
                            "description": "Shorter rows: drop empties and timestamps."}
            }
        }
    ),
    
    # ----------------------------------------
    # SEARCH TOOL (1 tool)
    # ----------------------------------------
    Tool(
        name="search",
        description=(
            "Find records by words. Word endings match. auto adds meaning if a local "
            "model exists. For a quoted answer, use ask."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match."},
                "project": {"type": ["string", "integer"], "description": "Only this project. Omit to search all."},
                "project_id": {"type": "integer", "description": "Only this project id. A matching name wins."},
                "content_types": {"type": "array", "items": {"type": "string"},
                                  "description": "todo, decision, change, learning, problem, or solution."},
                "mode": {"type": "string", "enum": ["auto", "keyword", "meaning"], "default": "auto",
                         "description": "auto uses meaning when a local model exists; else keywords."},
                "limit": {"type": "integer", "default": 10, "description": "Max results. Default 10, max 50."}
            },
            "required": ["query"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: PROBLEM (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="problem",
        description=(
            "Bugs and blockers. Prefer log_work(kind=problem) to open one. undo "
            "reopens a solve. get_tree reads attempts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "list", "solve", "get_tree"],
                           "description": "log, list, solve, or get_tree."},
                # For log:
                "component_id": {"type": "integer", "description": "log: component (General if omitted). list: filter."},
                "title": {"type": "string", "description": "log: short title. Required for log."},
                "description": {"type": "string", "description": "log: what is wrong."},
                "severity": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "log: severity. high and medium are older names."},
                # For list:
                "project_id": {"type": "integer", "description": "list: only this project. Omit for all."},
                "compact": {"type": "boolean", "default": False,
                            "description": "Shorter rows: drop empties and timestamps."},
                # For solve:
                "problem_id": {"type": "integer", "description": "solve/get_tree: problem id. Required."},
                "summary": {"type": "string", "description": "solve: what fixed it. Required for solve."},
                "key_insight": {"type": "string", "description": "solve: the lesson."},
                "winning_attempt_id": {"type": "integer", "description": "solve: attempt id that worked."},
                "code_snippet": {"type": "string", "description": "solve: the fix as text. Optional. No secrets."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: ATTEMPT (1 tool replaces 2)
    # ----------------------------------------
    Tool(
        name="attempt",
        description=(
            "Tries against a problem. log adds one; outcome sets how it ended and can "
            "be undone. Close it with problem action=solve."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "outcome"],
                           "description": "log a try, or set how it ended."},
                # For log:
                "problem_id": {"type": "integer", "description": "log: problem this try is for. Required."},
                "description": {"type": "string", "description": "log: what was tried. Required for log."},
                "parent_attempt_id": {"type": "integer", "description": "log: earlier attempt this branches from."},
                # For outcome:
                "attempt_id": {"type": "integer", "description": "outcome: attempt id. Required."},
                "outcome": {"type": "string", "enum": ["success", "failure", "partial", "abandoned"],
                            "description": "How the try ended. Required for outcome."},
                "notes": {"type": "string", "description": "outcome: what was learned."},
                "confidence": {"type": "string", "enum": ["attempted", "worked_once", "verified", "proven", "deprecated"], "default": "attempted",
                               "description": "outcome: how sure. Default attempted."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: TODO (1 tool replaces 3)
    # ----------------------------------------
    Tool(
        name="todo",
        description=(
            "Todos. Prefer log_work(kind=todo) to add. Undo restores an update, not a new "
            "todo (set done or cancelled). next is the top unblocked ones. requires project."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "update", "list", "next", "block", "unblock"],
                           "description": "Which operation."},
                "parent_todo_id": {"type": "integer", "description": "add: parent todo id, in the same project."},
                "blocked_by": {"type": "array", "items": {"type": "integer"},
                               "description": "Ids it waits on. Omit both fields to clear all."},
                "blocked_by_problem": {"type": "integer", "description": "add/block/unblock: problem id it waits on."},
                "limit": {"type": "integer", "default": 5, "description": "next: how many ready todos. Default 5."},
                # For add:
                "project_id": {"type": "integer", "description": "Project id or name. Required for add, list, and next."},
                "title": {"type": "string", "description": "add: title, required. update: new title."},
                "description": {"type": "string", "description": "add/update: longer detail."},
                "component_id": {"type": "integer", "description": "add: component this todo is about."},
                "priority": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "Priority. high and medium are older names."},
                "due_date": {"type": "string", "description": "add: due date, preferably ISO."},
                "min_priority": {"type": "string", "enum": list(priority.ACCEPTED),
                                 "description": "list: this level or more urgent."},
                "sort": {"type": "string", "enum": ["priority", "due", "newest"], "default": "priority",
                         "description": "list: sort order. Default priority."},
                # For update:
                "todo_id": {"type": "integer", "description": "Todo id for update, block, or unblock. Required."},
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "done", "cancelled"],
                           "description": "update: new status. list: only this status."},
                # For list:
                "compact": {"type": "boolean", "default": False,
                            "description": "Shorter rows."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: SESSION (1 tool replaces 6)
    # ----------------------------------------
    Tool(
        name="session",
        description=(
            "Sessions open and close on their own. start reuses the open one or creates "
            "one. end stops it (not undone)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "end", "current", "log_conversation", "list_conversations", "list_sessions"],
                           "description": "Which operation."},
                # For start:
                "project_id": {"type": "integer", "description": "Project id or project=. Optional for current."},
                "focus_component_id": {"type": "integer", "description": "start: component in focus. Ignored if a session is open."},
                "focus_problem_id": {"type": "integer", "description": "start: problem in focus. Ignored if a session is open."},
                # For end:
                "session_id": {"type": "integer", "description": "Session id. end requires it; log uses the open session."},
                "summary": {"type": "string", "description": "end: what was accomplished."},
                "outcomes": {"type": "array", "items": {"type": "string"}, "description": "end: short outcome lines."},
                # For log_conversation:
                "user_prompt_summary": {"type": "string", "description": "log_conversation: what was asked. Required."},
                "assistant_response_summary": {"type": "string", "description": "log_conversation: what was answered."},
                "key_decisions": {"type": "array", "items": {"type": "string"}, "description": "Decision lines. Review mode may queue the first."},
                # For list:
                "limit": {"type": "integer", "default": 20, "description": "Max rows for a list. Default 20."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: FILE (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="file",
        description=(
            "Attach, list, remove, or search local files. Remote attach is refused. "
            "Copies into the bundle by default. delete_file removes that copy for good."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["attach", "list", "remove", "search"],
                           "description": "attach, list, remove, or search."},
                # For attach:
                "project_id": {"type": "integer", "description": "attach: project id or project=. list/search may filter."},
                "file_path": {"type": "string", "description": "attach: path on this computer. Required."},
                "component_id": {"type": "integer", "description": "Component to attach to, or a list filter."},
                "problem_id": {"type": "integer", "description": "Problem to attach to, or a list filter."},
                "user_description": {"type": "string", "description": "attach: why this file matters."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "attach: tags, stored comma-separated."},
                "copy_to_bundle": {"type": "boolean", "default": True,
                                   "description": "Copy into the bundle (default), or store the path only."},
                # For remove:
                "attachment_id": {"type": "integer", "description": "remove: attachment id. Required."},
                "delete_file": {"type": "boolean", "default": False,
                                "description": "Also delete the bundle copy. Undo cannot restore it."},
                # For search:
                "query": {"type": "string", "description": "search: text in the index. Required for search."},
                "file_types": {"type": "array", "items": {"type": "string"},
                               "description": "search: extensions without a dot, such as md or py."},
                "limit": {"type": "integer", "default": 10, "description": "search: max hits. Default 10."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: GIT (1 tool replaces 6)
    # ----------------------------------------
    Tool(
        name="git",
        description=(
            "Git for the FlowState data folder, not the user's repo. Remote clients "
            "are refused. sync may push (not undone)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["init", "status", "sync", "set_remote", "clone", "history"],
                           "description": "Which operation."},
                # For sync:
                "commit_message": {"type": "string", "description": "sync: commit subject. Omit for an automatic one."},
                # For set_remote/clone:
                "remote_url": {"type": "string", "description": "Git URL for set_remote or clone. Required."},
                "local_path": {"type": "string", "description": "Clone directory. Must be empty. Default: data folder."},
                # For history:
                "limit": {"type": "integer", "default": 20, "description": "history: max commits. Default 20."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: VARIABLE (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="variable",
        description=(
            "Non-secret settings (hosts, ports). Secrets are refused. is_secret does "
            "not hide the value. update and delete can be undone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"],
                           "description": "create, list, update, or delete."},
                "project_id": {"type": "integer", "description": "Project id or project=. Required for create and list."},
                "variable_id": {"type": "integer", "description": "update/delete: variable id. Required."},
                "name": {"type": "string", "description": "Name, unique per project and category. Required on create."},
                "value": {"type": "string", "description": "The setting as text. Not for secrets."},
                "description": {"type": "string", "description": "create/update: what it is for."},
                "category": {"type": "string", "enum": ["server", "credentials", "config", "environment", "endpoint", "custom"], "default": "custom",
                             "description": "Category, default custom. Not a place for secrets."},
                "is_secret": {"type": "boolean", "default": False,
                              "description": "Flag as sensitive. list still returns the value."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: METHOD (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="method",
        description=(
            "A reusable procedure (prefer log_work for one decision). update replaces "
            "steps. update and delete can be undone."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"],
                           "description": "create, list, update, or delete."},
                "project_id": {"type": "integer", "description": "Project id or project=. Required for create and list."},
                "method_id": {"type": "integer", "description": "update/delete: method id. Required."},
                "name": {"type": "string", "description": "Name, unique in the project. Required on create."},
                "description": {"type": "string", "description": "What the procedure is. Required on create."},
                "category": {"type": "string", "enum": ["auth", "deployment", "testing", "architecture", "workflow", "convention", "api", "security", "other"],
                             "description": "Category. list: filter."},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "Ordered steps. update replaces the whole list."},
                "code_example": {"type": "string", "description": "Sample code. Do not include secrets."},
                "related_component_id": {"type": "integer", "description": "Component this procedure belongs to."}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: SELF_IMPROVE (1 tool replaces 23!)
    # Learned skills, session checkpoints and quality metrics for the connected app.
    # ----------------------------------------
    Tool(
        name="self_improve",
        description=(
            "Skills, checkpoints, and metrics for this app. Use log_work for project "
            "memory. confirm_skill may promote after 3 sessions at rate 0.8. "
            "Undo does not delete a new row."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["learn_skill", "get_skills", "confirm_skill", "save_state", "get_state", "log_metric", "get_metrics"],
                           "description": "Which operation."},
                # For learn_skill:
                "skill": {"type": "string", "description": "learn_skill: what was learned. Required."},
                "skill_type": {"type": "string", "enum": ["tool_capability", "user_preference", "approach", "gotcha", "project_specific"],
                               "description": "Skill kind. Required to learn; optional filter."},
                "context": {"type": "string", "description": "When a skill applies, or where a metric was scored."},
                "tool_name": {"type": "string", "description": "Related tool, or a get_skills filter."},
                # For get_skills:
                "project_id": {"type": "integer", "description": "Project id or project=. Required to save or get state. get_skills includes global skills."},
                "promoted_only": {"type": "boolean", "default": False,
                                  "description": "get_skills: only promoted skills."},
                # For confirm_skill:
                "skill_id": {"type": "integer", "description": "confirm_skill: skill id. Required."},
                # For save_state:
                "focus_summary": {"type": "string", "description": "save_state: what the session was doing."},
                "key_facts": {"type": "array", "items": {"type": "string"}, "description": "save_state: facts to keep."},
                "pending_decisions": {"type": "array", "items": {"type": "string"}, "description": "save_state: decisions not made yet."},
                "active_problem_ids": {"type": "array", "items": {"type": "integer"}, "description": "save_state: problem ids in progress."},
                "active_component_ids": {"type": "array", "items": {"type": "integer"}, "description": "save_state: component ids in progress."},
                # For get_state:
                # (uses project_id)
                # For log_metric:
                "metric_type": {"type": "string", "enum": ["checkpoint_timing", "tool_choice", "response_quality", "prediction_accuracy", "user_satisfaction"],
                                "description": "Metric to log, or a get_metrics filter."},
                "effectiveness_score": {"type": "number", "description": "log_metric: score from 0.0 to 1.0."},
                "action_taken": {"type": "string", "description": "log_metric: what was done."},
                "outcome": {"type": "string", "description": "log_metric: what happened."}
            },
            "required": ["action"]
        }
    ),
]

# Legacy tools (21). v2 tools are added in _decorate_tools (35 listed).


# ============================================================
# v2 PHASE A: ENTRY TOOL, SIMPLE WRITE TOOL, PROJECT ON WRITES
# ============================================================

START_HERE = Tool(
    name="start_here",
    description=guide.START_HERE_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": {"type": ["string", "integer"], "description": "Name or id. Omit to list projects."},
            "folder": {"type": "string", "description": "Selects the project when project is omitted."},
            "size": {"type": "string", "enum": list(SIZE_NAMES), "default": "standard",
                     "description": "quick ~1,500; standard 4,000; deep 12,000."},
        },
    },
)

LOG_WORK = Tool(
    name="log_work",
    description=guide.LOG_WORK_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "kind": {"type": "string", "enum": ["decision", "change", "problem", "learning", "todo"],
                    "description": "Which record to write."},
            "summary": {"type": "string", "description": "One short line."},
            "details": {"type": "string", "description": "Longer reason or context."},
            "component": {"type": "string", "description": "Changes and problems default to General."},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED),
                         "description": "Priority or severity. high and medium are older names."},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                         "description": "Learnings only. Omit otherwise."},
            "suggested": {"type": "boolean", "default": False,
                          "description": "Queue for review instead of saving."},
            "replaces": {"type": "integer",
                         "description": "Decisions only: id this one replaces."},
            "source": {"type": "string", "enum": list(provenance.SOURCES),
                       "description": "From the user, your inference, or the code."},
        },
        "required": ["project", "kind", "summary"],
    },
)

REVIEW_LIST = Tool(
    name="review_list",
    description=guide.REVIEW_LIST_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": {"type": ["string", "integer"], "description": "Project name or id. Omit for all projects."},
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "all"], "default": "pending",
                      "description": "Which slice of the queue."},
            "limit": {"type": "integer", "default": 20, "description": "Max items. Default 20, max 100."},
        },
    },
)

_USER_REQUEST = {"type": "string",
                 "description": "The user's words in this chat."}

REVIEW_APPROVE = Tool(
    name="review_approve",
    description=guide.REVIEW_APPROVE_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "item_id": {"type": "integer", "description": "Review item id from review_list."},
            "user_request": _USER_REQUEST,
            "summary": {"type": "string", "description": "New summary. Omit to keep."},
            "details": {"type": "string", "description": "New details. Omit to keep."},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED),
                         "description": "Todo or problem. high and medium still work."},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                         "description": "For a learning. Omit otherwise."},
            "component": {"type": "string", "description": "Component to file the approved record under."},
            "move_to": {"type": ["string", "integer"],
                        "description": "Project to move a misfiled item to."},
        },
        "required": ["item_id", "user_request"],  # project: checked by the guard (clear error)
    },
)

REVIEW_REJECT = Tool(
    name="review_reject",
    description=guide.REVIEW_REJECT_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "item_id": {"type": "integer", "description": "Review item id from review_list."},
            "reason": {"type": "string", "description": "The user's reason. Required. The item is kept."},
            "user_request": _USER_REQUEST,
        },
        "required": ["item_id", "reason", "user_request"],  # project: checked by the guard
    },
)

LINK = Tool(
    name="link",
    description=guide.LINK_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"],
                       "description": "add, remove, or list."},
            "project": guide.PROJECT_PROPERTY,
            "link_type": {"type": "string", "enum": list(links.LINK_TYPES),
                          "description": "Relation to the target."},
            "component": {"type": "string", "description": "Source: a component of this project, by name."},
            "item_type": {"type": "string", "enum": list(links.ENDPOINTS),
                          "description": "Source record type. Use with item_id."},
            "item_id": {"type": "integer", "description": "Source record id. remove can use this instead of link_id."},
            "target_project": {"type": ["string", "integer"], "description": "add: target project name or id."},
            "target_component": {"type": "string", "description": "add: component name in the target project."},
            "target_type": {"type": "string", "enum": list(links.ENDPOINTS),
                            "description": "add: target record type. Use with target_id."},
            "target_id": {"type": "integer", "description": "add: target record id. Use with target_type."},
            "url": {"type": "string", "description": "add: https URL stored on the source record."},
            "note": {"type": "string", "description": "add: short note stored on the link."},
            "link_id": {"type": "integer", "description": "remove: link id, or the same fields as add."},
            "include_removed": {"type": "boolean", "default": False,
                                "description": "list: also return removed links."},
        },
        "required": ["action"],  # project: checked by the guard (clear error)
    },
)

HISTORY = Tool(
    name="history",
    description=guide.HISTORY_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "undo", "who"],
                       "description": "list, undo, or who."},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(history.TABLES),
                            "description": "Record table. Required for undo."},
            "record_id": {"type": "integer", "description": "Record id. Required for undo."},
            "limit": {"type": "integer", "default": 20,
                      "description": "list/who: max rows. Default 20, max 100."},
            "by_app": {"type": "string", "description": "who: only this app (part of the name)."},
            "by_model": {"type": "string", "description": "who: only this model (part of the name)."},
            "by_machine": {"type": "string", "description": "who: only this machine or workspace."},
            "since": {"type": "string", "description": "who: from this local time, such as 2026-10-17."},
            "until": {"type": "string", "description": "who: up to this local time."},
            "page": {"type": "integer", "default": 1, "description": "who: page number. Default 1."},
        },
        "required": ["action"],
    },
)

CLEANUP_SUGGEST = Tool(
    name="cleanup_suggest",
    description=guide.CLEANUP_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["duplicate", "stale", "misfiled"]},
                     "description": "Which checks to run. Omit for all three."},
        },
    },
)

IMPORT_MEMORY = Tool(
    name="import_memory",
    description=guide.IMPORT_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "text": {"type": "string", "description": "File contents. Max 200,000 characters."},
            "format": {"type": "string", "enum": ["memory", "flowstate_markdown"], "default": "memory",
                      "description": "memory (default) or flowstate_markdown (export-md)."},
            "source": {"type": "string", "description": "Where it came from, such as Claude or ChatGPT."},
            "kind": {"type": "string", "enum": ["learning", "decision", "todo", "problem", "change"],
                     "default": "learning", "description": "Record kind for a memory import. Default learning."},
        },
        "required": ["text"],
    },
)

REVIEW_MODE = Tool(
    name="review_mode",
    description=guide.REVIEW_MODE_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["get", "set"],
                       "description": "get, or set when the user asks."},
            "project": {"type": ["string", "integer"],
                        "description": "One project. Omit to change the whole install."},
            "mode": {"type": "string", "enum": list(review_mode.MODES),
                    "description": "off, auto_only (default), types, or everything."},
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(review_mode.KINDS)},
                      "description": "mode=types: which kinds also wait for review."},
            "clear": {"type": "boolean", "description": "set: remove this project's override."},
            "user_request": _USER_REQUEST,
        },
        "required": ["action"],
    },
)

RULES = Tool(
    name="rules",
    description=guide.RULES_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "remove"],
                       "description": "add, list, or remove."},
            "project": guide.PROJECT_PROPERTY,
            "rule": {"type": "string", "description": "add: the rule, one short sentence."},
            "rule_id": {"type": "integer", "description": "remove: rule id (R<id> in the brief)."},
            "user_request": _USER_REQUEST,
            "include_removed": {"type": "boolean", "default": False,
                                "description": "list: include removed rules."},
        },
        "required": ["action"],  # project: checked by the guard (clear error)
    },
)

HANDOFF = Tool(
    name="handoff",
    description=guide.HANDOFF_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["leave", "list", "pickup"],
                       "description": "leave, list, or pickup."},
            "project": guide.PROJECT_PROPERTY,
            "where_i_was": {"type": "string", "description": "leave: progress so far. Max 1,000 characters."},
            "next_step": {"type": "string", "description": "leave: the next step. Max 500 characters."},
            "handoff_id": {"type": "integer", "description": "pickup: note id (H<id> in the brief)."},
            "include_picked_up": {"type": "boolean", "default": False,
                                   "description": "list: include notes already picked up."},
            "limit": {"type": "integer", "default": 20, "description": "list: max notes. Default 20."},
        },
        "required": ["action"],
    },
)

VERIFY = Tool(
    name="verify",
    description=guide.VERIFY_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["stale", "mark", "source"],
                       "description": "stale, mark (you re-checked one), or source."},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(provenance.TABLES),
                            "description": "mark/source: record type."},
            "record_id": {"type": "integer", "description": "mark/source: record id."},
            "source": {"type": "string", "enum": list(provenance.SOURCES),
                       "description": "Required for source. Optional when you mark."},
            "days": {"type": "integer", "description": "stale: age in days. Default 60."},
            "limit": {"type": "integer", "default": 20, "description": "stale: max facts. Default 20."},
        },
        "required": ["action"],
    },
)

ASK = Tool(
    name="ask",
    description=guide.ASK_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The question in plain words."},
            "project": {"type": ["string", "integer"], "description": "Name or id. Omit to search every project."},
            "limit": {"type": "integer", "default": 5, "description": "Max records to quote. Default 5."},
        },
        "required": ["question"],
    },
)

_WRITE_TOOLS = {n for (n, _a) in WRITES}
_NEW_TOOLS = [START_HERE, LOG_WORK, REVIEW_LIST, REVIEW_APPROVE, REVIEW_REJECT, LINK, REVIEW_MODE,
              HISTORY, CLEANUP_SUGGEST, IMPORT_MEMORY, RULES, HANDOFF, VERIFY, ASK]


# Tools that can record a change get the optional `model` argument (identity.py).
_MODEL_TOOLS = _WRITE_TOOLS | {"create_project", "review_mode"}


def _with_model(t: Tool) -> Tool:
    if t.name not in _MODEL_TOOLS:
        return t
    schema = json.loads(json.dumps(t.inputSchema))
    schema.setdefault("properties", {})["model"] = guide.MODEL_PROPERTY
    return Tool(name=t.name, description=t.description, inputSchema=schema)


def _decorate_tools(base: list) -> list:
    """Add the project argument and the write note to every tool that can write, and the
    optional model argument to every tool that can record a change."""
    out = [_with_model(t) for t in _NEW_TOOLS]
    for t in base:
        if t.name in _WRITE_TOOLS:
            schema = json.loads(json.dumps(t.inputSchema))
            schema.setdefault("properties", {})["project"] = guide.PROJECT_PROPERTY
            # project/project_id are checked by the guard (clear error + candidates),
            # not by schema validation; log_change's component defaults to 'General'.
            schema["required"] = [r for r in schema.get("required", [])
                                  if r not in ("project_id",)
                                  and not (t.name == "log_change" and r == "component_id")]
            desc = t.description + guide.WRITE_NOTE
            t = Tool(name=t.name, description=desc, inputSchema=schema)
        out.append(_with_model(t))
    return out


ALL_TOOLS = [tool_labels.annotate(t) for t in _decorate_tools(TOOLS)]
