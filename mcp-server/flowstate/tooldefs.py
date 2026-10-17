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
        description="List all projects with their status.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "completed", "archived"],
                    "description": "Filter by status"
                }
            }
        }
    ),
    Tool(
        name="create_project",
        description="Create a new project.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name (unique)"},
                "description": {"type": "string", "description": "Project description"}
            },
            "required": ["name"]
        }
    ),
    Tool(
        name="update_project",
        description="Update project details.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string", "enum": ["active", "paused", "completed", "archived"]}
            },
            "required": ["project_id"]
        }
    ),
    
    # ----------------------------------------
    # CONTEXT TOOL (1 tool - replaces 2)
    # ----------------------------------------
    Tool(
        name="get_context",
        description="Get project context. Use lean=True (default) for minimal tokens at session start. Use lean=False for full details when needed.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Project name"},
                "lean": {"type": "boolean", "description": "Minimal tokens (default True)", "default": True},
                "hours": {"type": "integer", "description": "Hours back for changes", "default": 48}
            },
            "required": ["project_name"]
        }
    ),
    
    # ----------------------------------------
    # COMPONENT TOOLS (3 tools)
    # ----------------------------------------
    Tool(
        name="create_component",
        description="Create a component within a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "parent_component_id": {"type": "integer", "description": "For nesting"}
            },
            "required": ["project_id", "name"]
        }
    ),
    Tool(
        name="list_components",
        description="List components in a project.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"}
            },
            "required": ["project_id"]
        }
    ),
    Tool(
        name="update_component",
        description="Update a component.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string", "enum": ["planning", "in_progress", "testing", "complete", "deprecated"]}
            },
            "required": ["component_id"]
        }
    ),
    
    # ----------------------------------------
    # LEARNING TOOLS (2 tools) - Core memory!
    # ----------------------------------------
    Tool(
        name="log_learning",
        description="Capture an insight or learning. This is your memory - use it!",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "insight": {"type": "string", "description": "The learning/insight"},
                "component_id": {"type": "integer"},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"]},
                "context": {"type": "string", "description": "When this applies"},
                "source": {"type": "string", "enum": ["experience", "documentation", "conversation", "error", "research"], "default": "experience"}
            },
            "required": ["project_id", "insight"]
        }
    ),
    Tool(
        name="get_learnings",
        description="Get learnings. Use compact=True to save tokens.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"]},
                "compact": {"type": "boolean", "default": False}
            }
        }
    ),
    
    # ----------------------------------------
    # CHANGE TOOLS (2 tools)
    # ----------------------------------------
    Tool(
        name="log_change",
        description="Log a change to a component.",
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer"},
                "field_name": {"type": "string", "description": "What changed"},
                "old_value": {"type": "string"},
                "new_value": {"type": "string"},
                "reason": {"type": "string"},
                "change_type": {"type": "string", "enum": ["config", "code", "architecture", "dependency", "documentation", "other"]}
            },
            "required": ["component_id", "field_name"]
        }
    ),
    Tool(
        name="get_recent_changes",
        description="Get recent changes. Use compact=True to save tokens.",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer"},
                "component_id": {"type": "integer"},
                "hours": {"type": "integer", "default": 24},
                "compact": {"type": "boolean", "default": False}
            }
        }
    ),
    
    # ----------------------------------------
    # SEARCH TOOL (1 tool)
    # ----------------------------------------
    Tool(
        name="search",
        description=("Search todos, decisions, changes, learnings, problems and solutions. "
                     "Matches words (and word endings); also by meaning when a local model is "
                     "installed (the reply's mode says which was used)."),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "project": {"type": ["string", "integer"], "description": "Project name or id (optional)"},
                "project_id": {"type": "integer"},
                "content_types": {"type": "array", "items": {"type": "string"},
                                  "description": "Filter: todo, decision, change, learning, problem, solution"},
                "mode": {"type": "string", "enum": ["auto", "keyword", "meaning"], "default": "auto"},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["query"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: PROBLEM (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="problem",
        description="Manage problems. Actions: log, list, solve, get_tree",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "list", "solve", "get_tree"]},
                # For log:
                "component_id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "severity": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "critical, urgent, normal, low (older names high, medium also work)"},
                # For list:
                "project_id": {"type": "integer"},
                "compact": {"type": "boolean", "default": False},
                # For solve:
                "problem_id": {"type": "integer"},
                "summary": {"type": "string"},
                "key_insight": {"type": "string"},
                "winning_attempt_id": {"type": "integer"},
                "code_snippet": {"type": "string"}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: ATTEMPT (1 tool replaces 2)
    # ----------------------------------------
    Tool(
        name="attempt",
        description="Manage solution attempts. Actions: log, outcome",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "outcome"]},
                # For log:
                "problem_id": {"type": "integer"},
                "description": {"type": "string"},
                "parent_attempt_id": {"type": "integer"},
                # For outcome:
                "attempt_id": {"type": "integer"},
                "outcome": {"type": "string", "enum": ["success", "failure", "partial", "abandoned"]},
                "notes": {"type": "string"},
                "confidence": {"type": "string", "enum": ["attempted", "worked_once", "verified", "proven", "deprecated"], "default": "attempted"}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: TODO (1 tool replaces 3)
    # ----------------------------------------
    Tool(
        name="todo",
        description=("Manage todos. Actions: add, update, list, next (what to do next: open, "
                     "unblocked, highest priority), block / unblock (todo_id waits on blocked_by "
                     "todos or blocked_by_problem). add takes parent_todo_id for a subtask. "
                     "Priority: critical, urgent, normal (default), low (older names high and "
                     "medium also work). list filters by priority or min_priority and sorts by "
                     "priority (default), due or newest."),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "update", "list", "next", "block", "unblock"]},
                "parent_todo_id": {"type": "integer", "description": "add: make it a subtask of this todo"},
                "blocked_by": {"type": "array", "items": {"type": "integer"},
                               "description": "add/block/unblock: todo ids it waits on"},
                "blocked_by_problem": {"type": "integer", "description": "add/block/unblock: a problem it waits on"},
                "limit": {"type": "integer", "default": 5, "description": "next: how many"},
                # For add:
                "project_id": {"type": "integer"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "component_id": {"type": "integer"},
                "priority": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "add/update: the priority; list: only this level"},
                "due_date": {"type": "string"},
                "min_priority": {"type": "string", "enum": list(priority.ACCEPTED),
                                 "description": "list: this level or more urgent"},
                "sort": {"type": "string", "enum": ["priority", "due", "newest"], "default": "priority",
                         "description": "list: order"},
                # For update:
                "todo_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "done", "cancelled"]},
                # For list:
                "compact": {"type": "boolean", "default": False}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: SESSION (1 tool replaces 6)
    # ----------------------------------------
    Tool(
        name="session",
        description="Manage sessions. Actions: start, end, current, log_conversation, list_conversations, list_sessions",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "end", "current", "log_conversation", "list_conversations", "list_sessions"]},
                # For start:
                "project_id": {"type": "integer"},
                "focus_component_id": {"type": "integer"},
                "focus_problem_id": {"type": "integer"},
                # For end:
                "session_id": {"type": "integer"},
                "summary": {"type": "string"},
                "outcomes": {"type": "array", "items": {"type": "string"}},
                # For log_conversation:
                "user_prompt_summary": {"type": "string"},
                "assistant_response_summary": {"type": "string"},
                "key_decisions": {"type": "array", "items": {"type": "string"}},
                # For list:
                "limit": {"type": "integer", "default": 20}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: FILE (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="file",
        description="Manage file attachments. Actions: attach, list, remove, search",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["attach", "list", "remove", "search"]},
                # For attach:
                "project_id": {"type": "integer"},
                "file_path": {"type": "string"},
                "component_id": {"type": "integer"},
                "problem_id": {"type": "integer"},
                "user_description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "copy_to_bundle": {"type": "boolean", "default": True},
                # For remove:
                "attachment_id": {"type": "integer"},
                "delete_file": {"type": "boolean", "default": False},
                # For search:
                "query": {"type": "string"},
                "file_types": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 10}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: GIT (1 tool replaces 6)
    # ----------------------------------------
    Tool(
        name="git",
        description="Git operations. Actions: init, status, sync, set_remote, clone, history",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["init", "status", "sync", "set_remote", "clone", "history"]},
                # For sync:
                "commit_message": {"type": "string"},
                # For set_remote/clone:
                "remote_url": {"type": "string"},
                "local_path": {"type": "string"},
                # For history:
                "limit": {"type": "integer", "default": 20}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: VARIABLE (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="variable",
        description="Manage project variables (servers, credentials, config). Actions: create, list, update, delete",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"]},
                "project_id": {"type": "integer"},
                "variable_id": {"type": "integer"},
                "name": {"type": "string"},
                "value": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string", "enum": ["server", "credentials", "config", "environment", "endpoint", "custom"], "default": "custom"},
                "is_secret": {"type": "boolean", "default": False}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: METHOD (1 tool replaces 4)
    # ----------------------------------------
    Tool(
        name="method",
        description="Manage project methods (auth flows, deployment steps, patterns). Actions: create, list, update, delete",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"]},
                "project_id": {"type": "integer"},
                "method_id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "category": {"type": "string", "enum": ["auth", "deployment", "testing", "architecture", "workflow", "convention", "api", "security", "other"]},
                "steps": {"type": "array", "items": {"type": "string"}},
                "code_example": {"type": "string"},
                "related_component_id": {"type": "integer"}
            },
            "required": ["action"]
        }
    ),
    
    # ----------------------------------------
    # CONSOLIDATED: SELF_IMPROVE (1 tool replaces 23!)
    # The living skill system - keeps Claude learning
    # ----------------------------------------
    Tool(
        name="self_improve",
        description="Manage Claude's learned skills and session state. Actions: learn_skill, get_skills, confirm_skill, save_state, get_state, log_metric, get_metrics",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["learn_skill", "get_skills", "confirm_skill", "save_state", "get_state", "log_metric", "get_metrics"]},
                # For learn_skill:
                "skill": {"type": "string", "description": "What was learned"},
                "skill_type": {"type": "string", "enum": ["tool_capability", "user_preference", "approach", "gotcha", "project_specific"]},
                "context": {"type": "string", "description": "When this applies"},
                "tool_name": {"type": "string"},
                # For get_skills:
                "project_id": {"type": "integer"},
                "promoted_only": {"type": "boolean", "default": False},
                # For confirm_skill:
                "skill_id": {"type": "integer"},
                # For save_state:
                "focus_summary": {"type": "string"},
                "key_facts": {"type": "array", "items": {"type": "string"}},
                "pending_decisions": {"type": "array", "items": {"type": "string"}},
                "active_problem_ids": {"type": "array", "items": {"type": "integer"}},
                "active_component_ids": {"type": "array", "items": {"type": "integer"}},
                # For get_state:
                # (uses project_id)
                # For log_metric:
                "metric_type": {"type": "string", "enum": ["checkpoint_timing", "tool_choice", "response_quality", "prediction_accuracy", "user_satisfaction"]},
                "effectiveness_score": {"type": "number", "description": "0.0-1.0"},
                "action_taken": {"type": "string"},
                "outcome": {"type": "string"}
            },
            "required": ["action"]
        }
    ),
]

# Total: 21 tools (down from 79!)


# ============================================================
# v2 PHASE A: ENTRY TOOL, SIMPLE WRITE TOOL, PROJECT ON WRITES
# ============================================================

START_HERE = Tool(
    name="start_here",
    description=guide.START_HERE_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": {"type": ["string", "integer"], "description": "Project name or id"},
            "folder": {"type": "string", "description": "Working folder; its name is matched to a project if project is not given"},
            "size": {"type": "string", "enum": list(SIZE_NAMES), "default": "standard",
                     "description": "quick (about 1,500 characters), standard (4,000) or deep (up to 12,000)"},
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
            "kind": {"type": "string", "enum": ["decision", "change", "problem", "learning", "todo"]},
            "summary": {"type": "string", "description": "One short line"},
            "details": {"type": "string", "description": "Optional detail / reason / context"},
            "component": {"type": "string", "description": "Optional component name (default 'General')"},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED),
                         "description": "Todos: priority. Problems: severity. critical, urgent, "
                                        "normal (default), low; older names high/medium also work."},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                         "description": "Learnings only"},
            "suggested": {"type": "boolean", "default": False,
                          "description": "true = put it in the review queue for the user instead of saving it"},
            "replaces": {"type": "integer",
                         "description": "Decisions only: id of the earlier decision this one replaces"},
            "source": {"type": "string", "enum": list(provenance.SOURCES),
                       "description": "Where it comes from: user_stated, ai_inferred, from_code"},
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
            "project": {"type": ["string", "integer"], "description": "Project name or id (optional: all projects)"},
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "all"], "default": "pending"},
            "limit": {"type": "integer", "default": 20},
        },
    },
)

_USER_REQUEST = {"type": "string",
                 "description": "The user's own words in this chat asking for this decision"}

REVIEW_APPROVE = Tool(
    name="review_approve",
    description=guide.REVIEW_APPROVE_DESCRIPTION,
    inputSchema={
        "type": "object",
        "properties": {
            "project": guide.PROJECT_PROPERTY,
            "item_id": {"type": "integer"},
            "user_request": _USER_REQUEST,
            "summary": {"type": "string", "description": "Edited text (optional)"},
            "details": {"type": "string", "description": "Edited details (optional)"},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED)},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"]},
            "component": {"type": "string"},
            "move_to": {"type": ["string", "integer"],
                        "description": "Clean-up 'misfiled' items: the project to move it to, if not the suggested one"},
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
            "item_id": {"type": "integer"},
            "reason": {"type": "string", "description": "Why the user rejected it"},
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
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "project": guide.PROJECT_PROPERTY,
            "link_type": {"type": "string", "enum": list(links.LINK_TYPES)},
            "component": {"type": "string", "description": "Source: a component of project, by name"},
            "item_type": {"type": "string", "enum": list(links.ENDPOINTS),
                          "description": "Source: a record type (with item_id)"},
            "item_id": {"type": "integer"},
            "target_project": {"type": ["string", "integer"], "description": "Target project name or id"},
            "target_component": {"type": "string", "description": "Target: a component of target_project, by name"},
            "target_type": {"type": "string", "enum": list(links.ENDPOINTS)},
            "target_id": {"type": "integer"},
            "url": {"type": "string", "description": "Target: a GitHub / Linear / other https URL"},
            "note": {"type": "string"},
            "link_id": {"type": "integer", "description": "remove: the link"},
            "include_removed": {"type": "boolean", "default": False},
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
            "action": {"type": "string", "enum": ["list", "undo", "who"]},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(history.TABLES)},
            "record_id": {"type": "integer"},
            "limit": {"type": "integer", "default": 20},
            "by_app": {"type": "string", "description": "who: only this app (part of the name)"},
            "by_model": {"type": "string", "description": "who: only this model (part of the name)"},
            "by_machine": {"type": "string", "description": "who: only this machine / workspace"},
            "since": {"type": "string", "description": "who: from this local time, e.g. 2026-10-17"},
            "until": {"type": "string", "description": "who: up to this local time"},
            "page": {"type": "integer", "default": 1, "description": "who: page (limit rows each)"},
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
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["duplicate", "stale", "misfiled"]}},
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
            "text": {"type": "string", "description": "The export / file contents (max 200,000 characters)"},
            "format": {"type": "string", "enum": ["memory", "flowstate_markdown"], "default": "memory"},
            "source": {"type": "string", "description": "Where it came from, e.g. Claude, ChatGPT"},
            "kind": {"type": "string", "enum": ["learning", "decision", "todo", "problem", "change"],
                     "default": "learning", "description": "format=memory: record kind for the items"},
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
            "action": {"type": "string", "enum": ["get", "set"]},
            "project": {"type": ["string", "integer"],
                        "description": "Optional: one project's override (default: whole install)"},
            "mode": {"type": "string", "enum": list(review_mode.MODES)},
            "kinds": {"type": "array", "items": {"type": "string", "enum": list(review_mode.KINDS)},
                      "description": "mode=types: which kinds wait for review"},
            "clear": {"type": "boolean", "description": "set: remove this project's override"},
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
            "action": {"type": "string", "enum": ["add", "list", "remove"]},
            "project": guide.PROJECT_PROPERTY,
            "rule": {"type": "string", "description": "add: the rule, one short sentence"},
            "rule_id": {"type": "integer", "description": "remove: the rule (R<id> in the brief)"},
            "user_request": _USER_REQUEST,
            "include_removed": {"type": "boolean", "default": False},
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
            "action": {"type": "string", "enum": ["leave", "list", "pickup"]},
            "project": guide.PROJECT_PROPERTY,
            "where_i_was": {"type": "string", "description": "leave: what you were doing and how far it got (max 1,000)"},
            "next_step": {"type": "string", "description": "leave: the very next thing to do (max 500)"},
            "handoff_id": {"type": "integer", "description": "pickup: the note (H<id> in the brief)"},
            "include_picked_up": {"type": "boolean", "default": False},
            "limit": {"type": "integer", "default": 20},
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
            "action": {"type": "string", "enum": ["stale", "mark", "source"]},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(provenance.TABLES)},
            "record_id": {"type": "integer"},
            "source": {"type": "string", "enum": list(provenance.SOURCES)},
            "days": {"type": "integer", "description": "stale: not checked for this many days (default 60)"},
            "limit": {"type": "integer", "default": 20},
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
            "question": {"type": "string"},
            "project": {"type": ["string", "integer"], "description": "Project name or id (optional)"},
            "limit": {"type": "integer", "default": 5},
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
