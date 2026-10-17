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
            "List every FlowState project (read-only), newest update first. Use start_here "
            "to open one project's brief; use this for the structured list or a status filter. "
            "Returns [{id, name, description, status, created_at, updated_at}]. Does not create "
            "or change projects."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["active", "paused", "completed", "archived"],
                    "description": "Only this status: active, paused, completed or archived. Omit for all."
                }
            }
        }
    ),
    Tool(
        name="create_project",
        description=(
            "Create a new FlowState project (write). Use when the user asks for a project; "
            "recording work inside one is log_work, and opening it is start_here. name must "
            "be unique. Returns {id, name, description, status: active, created_at, updated_at}. "
            "A duplicate name is rejected. No components or records are created with it."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique project name. Required."},
                "description": {"type": "string", "description": "Optional longer description. Omit to leave it empty."}
            },
            "required": ["name"]
        }
    ),
    Tool(
        name="update_project",
        description=(
            "Change an existing project's name, description or status (write). Only fields you "
            "pass are overwritten. name must stay unique. Returns the project. Restore the "
            "previous row with history undo (record_type=project, record_id=project_id). Does "
            "not move that project's records."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id to update. Required."},
                "name": {"type": "string", "description": "New unique name. Omit to keep the current name."},
                "description": {"type": "string", "description": "New description. Omit to keep the current one."},
                "status": {"type": "string", "enum": ["active", "paused", "completed", "archived"],
                           "description": "active, paused, completed or archived. Omit to keep the current status."}
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
            "Read a raw context dump for one project by exact name (read-only). Prefer "
            "start_here to begin work: it includes hard rules and handoffs and opens the "
            "session. lean=true (default) returns {project: {id, name, status}, stats, "
            "blocking (up to 3 critical or urgent problems), quick_todos (up to 3 pending titles), "
            "recent_focus, has_active_session}. lean=false returns the project, components, "
            "open problems, changes within hours (default 48), pending todos, up to 10 "
            "learnings, the current session and attachments. An unknown name returns null."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "description": "Exact project name. Required. Ids are not accepted; use start_here for a name or id."},
                "lean": {"type": "boolean", "description": "true (default): short counts and titles. false: full objects.", "default": True},
                "hours": {"type": "integer", "description": "How many hours of changes to include (default 48).", "default": 48}
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
            "Add a component, a named part of a project such as a service or module (write). "
            "Changes and problems attach to one. name must be unique in the project. Returns "
            "{id, project_id, name, description, parent_component_id, status: in_progress, "
            "timestamps}. To record that something changed, use log_work(kind=change), not a "
            "new component."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project that owns the component. Required (or project= name)."},
                "name": {"type": "string", "description": "Component name, unique within the project. Required."},
                "description": {"type": "string", "description": "Optional what this component is."},
                "parent_component_id": {"type": "integer", "description": "Optional parent component id in the same project, to nest it."}
            },
            "required": ["project_id", "name"]
        }
    ),
    Tool(
        name="list_components",
        description=(
            "List one project's components by name (read-only). Each item is {id, project_id, "
            "parent_component_id, name, description, status, created_at, updated_at}. status is "
            "planning, in_progress, testing, complete or deprecated. Pass an id to log_change "
            "or problem. For the working brief, call start_here."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id whose components to list. Required."}
            },
            "required": ["project_id"]
        }
    ),
    Tool(
        name="update_component",
        description=(
            "Overwrite a component's name, description or status (write). Only fields you pass "
            "change. Returns the component, or null if the id does not exist. Restore the "
            "previous row with history undo (record_type=component). This does not write a "
            "change record; use log_work(kind=change) for that."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer", "description": "Component id to update. Required."},
                "name": {"type": "string", "description": "New name. Omit to keep the current name. Must stay unique in the project."},
                "description": {"type": "string", "description": "New description. Omit to keep the current one."},
                "status": {"type": "string", "enum": ["planning", "in_progress", "testing", "complete", "deprecated"],
                           "description": "planning, in_progress, testing, complete or deprecated. Omit to keep the current status."}
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
            "Save a lesson on a project (write). Prefer log_work(kind=learning, summary=the "
            "insight); use this when you want category and context on the row. Returns the "
            "learning {id, project_id, insight, category, context, source, verified: false}. "
            "If the review mode holds learnings, it is queued instead of saved. Secrets are "
            "refused. A new learning is not removed by history undo."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Project id. Required (or project= name)."},
                "insight": {"type": "string", "description": "The lesson, one or two sentences. Required."},
                "component_id": {"type": "integer", "description": "Optional component id this lesson is about."},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                             "description": "pattern, gotcha, best_practice, tool_tip, architecture, performance, security or other. Omit for none."},
                "context": {"type": "string", "description": "When this lesson applies. Optional."},
                "source": {"type": "string", "enum": ["experience", "documentation", "conversation", "error", "research"], "default": "experience",
                           "description": "experience (default), documentation, conversation, error or research."}
            },
            "required": ["project_id", "insight"]
        }
    ),
    Tool(
        name="get_learnings",
        description=(
            "List saved lessons (read-only). Omit filters to list every project. compact=true "
            "drops empty fields and timestamps and truncates long text to 100 characters. "
            "Returns learning objects {id, insight, category, context, source, verified, ...}. "
            "To ask a question, use ask; to search every record type, use search."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Only this project's lessons. Omit for all projects."},
                "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                             "description": "Only this category: pattern, gotcha, best_practice, tool_tip, architecture, performance, security or other."},
                "compact": {"type": "boolean", "default": False,
                            "description": "true: drop empties and timestamps, truncate long text (default false)."}
            }
        }
    ),
    
    # ----------------------------------------
    # CHANGE TOOLS (2 tools)
    # ----------------------------------------
    Tool(
        name="log_change",
        description=(
            "Append a change record on a component (write). Prefer log_work(kind=change, "
            "summary=what changed, component=name); it alerts linked projects the same way. "
            "component_id may be omitted; the project's General component is used or created. "
            "Returns the change {id, component_id, field_name, old_value, new_value, "
            "change_type, reason}, plus alerted_projects when other projects link to it. "
            "Review mode may queue it instead. Creating it is not undone by history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "component_id": {"type": "integer", "description": "Component that changed. Omit to use or create the project's General component."},
                "field_name": {"type": "string", "description": "What changed, a short label (the summary). Required."},
                "old_value": {"type": "string", "description": "Previous value, as text. Optional."},
                "new_value": {"type": "string", "description": "New value, as text. Optional."},
                "reason": {"type": "string", "description": "Why it changed. Optional."},
                "change_type": {"type": "string", "enum": ["config", "code", "architecture", "dependency", "documentation", "other"],
                                "description": "config, code, architecture, dependency, documentation or other. Omit to store none."}
            },
            "required": ["component_id", "field_name"]
        }
    ),
    Tool(
        name="get_recent_changes",
        description=(
            "List change records from the last hours (default 24), for one project and/or "
            "component (read-only). This is what was logged as changed, not the edit history "
            "of FlowState rows. For old/new values of an edit, who changed a row, or undo, "
            "use history. compact=true shortens text and drops timestamps. Returns changes "
            "{id, component_id, field_name, old_value, new_value, change_type, reason, created_at}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "integer", "description": "Only changes in this project. Omit for every project."},
                "component_id": {"type": "integer", "description": "Only changes on this component. Omit for every component."},
                "hours": {"type": "integer", "default": 24, "description": "How far back, in hours (default 24)."},
                "compact": {"type": "boolean", "default": False,
                            "description": "true: drop empties and timestamps, truncate long text (default false)."}
            }
        }
    ),
    
    # ----------------------------------------
    # SEARCH TOOL (1 tool)
    # ----------------------------------------
    Tool(
        name="search",
        description=(
            "Find records by words across todos, decisions, changes, learnings, problems and "
            "solutions (read-only). Word endings match. mode=auto (default) also ranks by "
            "meaning when a local model is installed; the reply's mode is keywords or "
            "meaning+keywords. For a quoted answer to a question, use ask. Returns {query, "
            "mode, count, results: [{content_type, content_id, project, title, snippet, "
            "matched_by}], note?}. limit default 10, capped at 50."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Words to match. Required."},
                "project": {"type": ["string", "integer"], "description": "Limit to this project name or id. Omit to search all. Prefer this over project_id."},
                "project_id": {"type": "integer", "description": "Limit to this integer project id. Omit to search all. A project name that matches is used instead."},
                "content_types": {"type": "array", "items": {"type": "string"},
                                  "description": "Only these types: todo, decision, change, learning, problem, solution. Omit for all six."},
                "mode": {"type": "string", "enum": ["auto", "keyword", "meaning"], "default": "auto",
                         "description": "auto (default): meaning plus keywords when a local model exists, else keywords. keyword: words only. meaning: meaning, falling back to keywords."},
                "limit": {"type": "integer", "default": 10, "description": "Max results (default 10, capped at 50)."}
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
            "Track bugs and blockers. Prefer log_work(kind=problem) to open one. action=log "
            "(write) stores a problem; component_id may be omitted (General is used or created). "
            "If review mode holds problems, log is queued instead. action=list (read) returns "
            "open problems. action=solve (write) sets status to solved and stores a solution; "
            "history undo (record_type=problem) restores the open problem and drops that "
            "solution. action=get_tree (read) returns {problem, attempts, solution}, or {} if "
            "the id is missing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "list", "solve", "get_tree"],
                           "description": "log (create), list (read open ones), solve (mark solved) or get_tree (read attempts). Required."},
                # For log:
                "component_id": {"type": "integer", "description": "log: component id. Omit to use or create the project's General component. list: optional filter."},
                "title": {"type": "string", "description": "log: short problem title. Required for log."},
                "description": {"type": "string", "description": "log: what is wrong, in more detail. Optional."},
                "severity": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "log: critical, urgent, normal (default) or low. Older names high and medium also work."},
                # For list:
                "project_id": {"type": "integer", "description": "list: only this project. Omit to list open problems in every project."},
                "compact": {"type": "boolean", "default": False,
                            "description": "list: true drops empties and timestamps and truncates text (default false)."},
                # For solve:
                "problem_id": {"type": "integer", "description": "solve and get_tree: problem id. Required for those actions."},
                "summary": {"type": "string", "description": "solve: what fixed it. Required for solve."},
                "key_insight": {"type": "string", "description": "solve: the lesson, optional."},
                "winning_attempt_id": {"type": "integer", "description": "solve: attempt id that worked. Optional."},
                "code_snippet": {"type": "string", "description": "solve: code that fixed it, as text. Optional. Do not include secrets."}
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
            "Record tries against a problem (write). action=log adds an attempt and returns "
            "{id, problem_id, description, confidence: attempted}; outcome is unset until "
            "action=outcome. action=outcome overwrites outcome, notes and confidence and "
            "returns the attempt, or null if the id is missing. That overwrite can be restored "
            "with history undo (record_type=attempt). Close the problem with problem "
            "action=solve, not with outcome."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["log", "outcome"],
                           "description": "log (add a try) or outcome (set how it ended). Required."},
                # For log:
                "problem_id": {"type": "integer", "description": "log: problem id this try is for. Required for log."},
                "description": {"type": "string", "description": "log: what was tried. Required for log."},
                "parent_attempt_id": {"type": "integer", "description": "log: earlier attempt id this branches from. Optional."},
                # For outcome:
                "attempt_id": {"type": "integer", "description": "outcome: attempt id to update. Required for outcome."},
                "outcome": {"type": "string", "enum": ["success", "failure", "partial", "abandoned"],
                            "description": "outcome: success, failure, partial or abandoned. Required for outcome."},
                "notes": {"type": "string", "description": "outcome: what was learned. Optional."},
                "confidence": {"type": "string", "enum": ["attempted", "worked_once", "verified", "proven", "deprecated"], "default": "attempted",
                               "description": "outcome: attempted (default), worked_once, verified, proven or deprecated."}
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
            "Manage todos. Prefer log_work(kind=todo) to add one. action=add creates a todo "
            "(optional subtask and blockers). action=update overwrites only the fields you "
            "pass; history undo (record_type=todo) restores that edit. A new todo is not "
            "deleted by undo: set status done or cancelled. action=list (read) returns rows "
            "plus priority_level. action=next (read) returns {next, ready_total, blocked, "
            "blocked_total} for open, unblocked, highest-priority todos. action=block / "
            "unblock (write) add or remove wait-on links. Review mode may queue an add."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "update", "list", "next", "block", "unblock"],
                           "description": "add, update, list, next (what to do next), block or unblock. Required."},
                "parent_todo_id": {"type": "integer", "description": "add: make this a subtask of this todo id, in the same project."},
                "blocked_by": {"type": "array", "items": {"type": "integer"},
                               "description": "add/block: todo ids it waits on. unblock: remove only these; omit both blocker fields to remove every blocker."},
                "blocked_by_problem": {"type": "integer", "description": "add/block/unblock: a problem id it waits on."},
                "limit": {"type": "integer", "default": 5, "description": "next: how many ready todos to return (default 5)."},
                # For add:
                "project_id": {"type": "integer", "description": "add/list/next: project id, or pass project= name. Required for those actions."},
                "title": {"type": "string", "description": "add: todo title. Required for add. update: new title."},
                "description": {"type": "string", "description": "add/update: longer detail. Optional."},
                "component_id": {"type": "integer", "description": "add: component id this todo is about. Optional."},
                "priority": {"type": "string", "enum": list(priority.ACCEPTED), "default": "normal",
                             "description": "add/update: critical, urgent, normal (default) or low (high and medium also work). list: only this level."},
                "due_date": {"type": "string", "description": "add: due date as text, preferably ISO (2026-10-17). Optional. Not changed by update."},
                "min_priority": {"type": "string", "enum": list(priority.ACCEPTED),
                                 "description": "list: this level or more urgent (critical, urgent, normal, low)."},
                "sort": {"type": "string", "enum": ["priority", "due", "newest"], "default": "priority",
                         "description": "list: priority (default), due or newest."},
                # For update:
                "todo_id": {"type": "integer", "description": "update/block/unblock: todo id. Required for those actions."},
                "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "done", "cancelled"],
                           "description": "update: pending, in_progress, blocked, done or cancelled. list: only this status."},
                # For list:
                "compact": {"type": "boolean", "default": False,
                            "description": "list: true drops empties and timestamps and truncates text (default false)."}
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
            "Read or close work sessions. A session opens and closes on its own (idle, or "
            "when the app quits); do not start one unless you need a manual session. "
            "action=start returns the automatic session when one is open ({session_id, "
            "automatic: true}); otherwise it creates one. action=end stores summary and "
            "outcomes and stops that session from being continued; it is not undone by "
            "history. action=current returns the open session or null. action=log_conversation "
            "stores a prompt/response summary; if key_decisions is set and review mode holds "
            "decisions, a decision is queued instead of the conversation. list_conversations "
            "and list_sessions return newest first."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["start", "end", "current", "log_conversation", "list_conversations", "list_sessions"],
                           "description": "start, end, current (read), log_conversation, list_conversations or list_sessions. Required."},
                # For start:
                "project_id": {"type": "integer", "description": "start/current/log_conversation/list_*: project id, or project= name. Required except current, where it filters."},
                "focus_component_id": {"type": "integer", "description": "start: component id in focus. Optional. Ignored when an automatic session is reused."},
                "focus_problem_id": {"type": "integer", "description": "start: problem id in focus. Optional. Ignored when an automatic session is reused."},
                # For end:
                "session_id": {"type": "integer", "description": "end: session id. log_conversation/list_conversations: optional; log defaults to the automatic session."},
                "summary": {"type": "string", "description": "end: what was accomplished. Optional."},
                "outcomes": {"type": "array", "items": {"type": "string"}, "description": "end: short outcome lines. Optional."},
                # For log_conversation:
                "user_prompt_summary": {"type": "string", "description": "log_conversation: what was asked. Required for that action."},
                "assistant_response_summary": {"type": "string", "description": "log_conversation: what was answered. Optional."},
                "key_decisions": {"type": "array", "items": {"type": "string"}, "description": "log_conversation: decision lines. If set, review mode may queue the first as a decision instead."},
                # For list:
                "limit": {"type": "integer", "default": 20, "description": "list_conversations and list_sessions: max rows (default 20)."}
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
            "Attach files to a project, component or problem. action=attach (write, this "
            "computer only; remote clients are refused) copies file_path into the project "
            "bundle when copy_to_bundle is true (default) and returns the attachment row. "
            "action=list (read) returns attachment rows; omit filters for all. action=remove "
            "(write) deletes the row. delete_file=true also deletes the bundle copy; that "
            "file is not restored. The row can be put back with history undo "
            "(record_type=attachment). action=search (read) matches indexed snippets, not "
            "file bytes, and returns content locations plus file name and path."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["attach", "list", "remove", "search"],
                           "description": "attach (copy or reference a file), list (read), remove (delete the row) or search (indexed text). Required."},
                # For attach:
                "project_id": {"type": "integer", "description": "attach: project id (or project= name). list/search: optional filter."},
                "file_path": {"type": "string", "description": "attach: path on this computer. Required for attach. Missing files error."},
                "component_id": {"type": "integer", "description": "attach: component to hang the file on. list: filter. Optional."},
                "problem_id": {"type": "integer", "description": "attach: problem to hang the file on. list: filter. Optional."},
                "user_description": {"type": "string", "description": "attach: why this file matters. Optional."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "attach: tag strings, stored comma-separated. Optional."},
                "copy_to_bundle": {"type": "boolean", "default": True,
                                   "description": "attach: true (default) copies into the project bundle; false stores the external path only."},
                # For remove:
                "attachment_id": {"type": "integer", "description": "remove: attachment id. Required for remove."},
                "delete_file": {"type": "boolean", "default": False,
                                "description": "remove: true also deletes the bundle copy from disk (not restored by undo). Default false."},
                # For search:
                "query": {"type": "string", "description": "search: substring matched against indexed description and snippet. Required for search."},
                "file_types": {"type": "array", "items": {"type": "string"},
                               "description": "search: only these extensions without the dot, e.g. md, py. Optional."},
                "limit": {"type": "integer", "default": 10, "description": "search: max hits (default 10)."}
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
            "Git for the FlowState data folder on this computer, not the user's project "
            "repository. Remote clients are refused. action=init creates the repo, a "
            ".gitignore and README, and an initial commit, or returns status if it exists. "
            "action=status (read) returns {initialized, branch, remote_url, pending_changes, "
            "last_commit}. action=sync commits non-secret files, then pull --rebase and push "
            "when a remote exists; that push is not undone by history. action=set_remote "
            "sets or replaces origin. action=clone clones into local_path or the data folder "
            "and refuses a non-empty directory. action=history (read) returns commits "
            "{hash, message, date, author}, or [] if there is no repo. This is not "
            "history (the edit-undo tool) and not the off-site backup."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["init", "status", "sync", "set_remote", "clone", "history"],
                           "description": "init, status (read), sync (commit and maybe push), set_remote, clone or history (read commits). Required."},
                # For sync:
                "commit_message": {"type": "string", "description": "sync: commit subject. Omit for 'FlowState sync' plus a timestamp."},
                # For set_remote/clone:
                "remote_url": {"type": "string", "description": "set_remote and clone: git URL for origin. Required for those actions."},
                "local_path": {"type": "string", "description": "clone: directory to clone into. Omit for the FlowState data folder. Refused if not empty."},
                # For history:
                "limit": {"type": "integer", "default": 20, "description": "history: max commits (default 20)."}
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
            "Store non-secret project settings such as host names and ports. Do not put "
            "passwords, tokens or keys here; writes that look like secrets are refused. "
            "is_secret only flags the row; list still returns value. action=create returns "
            "the row (name is unique per project and category). action=list (read) returns "
            "rows. action=update overwrites only fields you pass, or returns null if nothing "
            "was passed or the id is missing. action=delete returns true, or false if missing. "
            "update and delete can be restored with history undo (record_type=variable)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"],
                           "description": "create, list (read), update or delete. Required."},
                "project_id": {"type": "integer", "description": "create/list: project id (or project= name). Required for those actions."},
                "variable_id": {"type": "integer", "description": "update/delete: variable id. Required for those actions."},
                "name": {"type": "string", "description": "create: name, unique in the project and category. Required for create. update: new name."},
                "value": {"type": "string", "description": "create/update: the setting as text. Not a place for secrets."},
                "description": {"type": "string", "description": "create/update: what the variable is for. Optional."},
                "category": {"type": "string", "enum": ["server", "credentials", "config", "environment", "endpoint", "custom"], "default": "custom",
                             "description": "create: server, credentials, config, environment, endpoint or custom (default). list: filter. The credentials category is still not for real secrets."},
                "is_secret": {"type": "boolean", "default": False,
                              "description": "create/update: flag the row as sensitive (default false). The value is still returned by list."}
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
            "Store a reusable procedure for a project (steps, not a one-off decision). Prefer "
            "log_work(kind=learning or decision) for a single lesson or choice. action=create "
            "returns the method with steps as a list; name is unique in the project. "
            "action=list (read) returns methods. action=update overwrites only fields you "
            "pass (steps replaces the whole list) or returns null if none were passed. "
            "action=delete returns true, or false if missing. update and delete undo with "
            "history (record_type=method)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "delete"],
                           "description": "create, list (read), update or delete. Required."},
                "project_id": {"type": "integer", "description": "create/list: project id (or project= name). Required for those actions."},
                "method_id": {"type": "integer", "description": "update/delete: method id. Required for those actions."},
                "name": {"type": "string", "description": "create: name, unique in the project. Required for create. update: new name."},
                "description": {"type": "string", "description": "create: what the procedure is. Required for create. update: new text."},
                "category": {"type": "string", "enum": ["auth", "deployment", "testing", "architecture", "workflow", "convention", "api", "security", "other"],
                             "description": "auth, deployment, testing, architecture, workflow, convention, api, security or other. list: filter."},
                "steps": {"type": "array", "items": {"type": "string"},
                          "description": "create/update: ordered step strings. update replaces the whole list."},
                "code_example": {"type": "string", "description": "create/update: optional sample code. Do not include secrets."},
                "related_component_id": {"type": "integer", "description": "create/update: component id this procedure belongs to. Optional."}
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
            "Record this app's learned skills, a checkpoint, or a quality metric. Not a "
            "substitute for log_work (that is the user's project memory). action=learn_skill "
            "(write) inserts a skill row. action=get_skills (read) lists them. "
            "action=confirm_skill (write) increments the session count and may set promoted "
            "when sessions are at least 3 and the success rate is at least 0.8; history undo "
            "restores the skill row. action=save_state (write) stores a checkpoint and returns "
            "it. action=get_state (read) returns the latest checkpoint or null. "
            "action=log_metric (write) stores one score. action=get_metrics (read) returns "
            "recent metric rows. New skills, states and metrics are not deleted by undo."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["learn_skill", "get_skills", "confirm_skill", "save_state", "get_state", "log_metric", "get_metrics"],
                           "description": "learn_skill, get_skills (read), confirm_skill, save_state, get_state (read), log_metric or get_metrics (read). Required."},
                # For learn_skill:
                "skill": {"type": "string", "description": "learn_skill: what was learned. Required for learn_skill."},
                "skill_type": {"type": "string", "enum": ["tool_capability", "user_preference", "approach", "gotcha", "project_specific"],
                               "description": "learn_skill: tool_capability, user_preference, approach, gotcha or project_specific. Required for learn_skill. get_skills: optional filter."},
                "context": {"type": "string", "description": "learn_skill: when this applies. log_metric: where the score was taken. Optional."},
                "tool_name": {"type": "string", "description": "learn_skill: tool this skill is about. get_skills: optional filter."},
                # For get_skills:
                "project_id": {"type": "integer", "description": "learn_skill/get_skills/save_state/get_state/log_metric: project id (or project=). get_skills includes global skills. Required for save_state and get_state."},
                "promoted_only": {"type": "boolean", "default": False,
                                  "description": "get_skills: true returns only promoted skills (default false)."},
                # For confirm_skill:
                "skill_id": {"type": "integer", "description": "confirm_skill: skill id. Required for confirm_skill."},
                # For save_state:
                "focus_summary": {"type": "string", "description": "save_state: what the session was doing. Optional."},
                "key_facts": {"type": "array", "items": {"type": "string"}, "description": "save_state: fact lines to keep. Optional."},
                "pending_decisions": {"type": "array", "items": {"type": "string"}, "description": "save_state: decisions not made yet. Optional."},
                "active_problem_ids": {"type": "array", "items": {"type": "integer"}, "description": "save_state: problem ids in progress. Optional."},
                "active_component_ids": {"type": "array", "items": {"type": "integer"}, "description": "save_state: component ids in progress. Optional."},
                # For get_state:
                # (uses project_id)
                # For log_metric:
                "metric_type": {"type": "string", "enum": ["checkpoint_timing", "tool_choice", "response_quality", "prediction_accuracy", "user_satisfaction"],
                                "description": "log_metric: checkpoint_timing, tool_choice, response_quality, prediction_accuracy or user_satisfaction. Required for log_metric. get_metrics: optional filter."},
                "effectiveness_score": {"type": "number", "description": "log_metric: score from 0.0 to 1.0. Optional."},
                "action_taken": {"type": "string", "description": "log_metric: what was done. Optional."},
                "outcome": {"type": "string", "description": "log_metric: what happened. Optional. Not the attempt outcome enum."}
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
            "project": {"type": ["string", "integer"], "description": "Project name or id. Omit, or pass only folder, to get the project list instead of a brief."},
            "folder": {"type": "string", "description": "Working folder path. Used only when project is omitted; the folder's name is matched to a project."},
            "size": {"type": "string", "enum": list(SIZE_NAMES), "default": "standard",
                     "description": "quick (about 1,500 characters), standard (default, 4,000) or deep (up to 12,000)."},
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
                    "description": "decision, change, problem, learning or todo. Required."},
            "summary": {"type": "string", "description": "One short line: the decision, change, problem, lesson or todo. Required."},
            "details": {"type": "string", "description": "Optional longer reason or context. Omit if the summary is enough."},
            "component": {"type": "string", "description": "Component name. changes and problems default to General, created if missing. Others: optional."},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED),
                         "description": "Todos: priority. Problems: severity. critical, urgent, "
                                        "normal (default), low; older names high/medium also work."},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                         "description": "Learnings only: pattern, gotcha, best_practice, tool_tip, architecture, performance, security or other."},
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
            "status": {"type": "string", "enum": ["pending", "approved", "rejected", "all"], "default": "pending",
                      "description": "pending (default), approved, rejected, or all."},
            "limit": {"type": "integer", "default": 20, "description": "Max items (default 20, capped at 100)."},
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
            "item_id": {"type": "integer", "description": "Review item id from review_list. Required."},
            "user_request": _USER_REQUEST,
            "summary": {"type": "string", "description": "Replacement summary. Omit to keep the queued text."},
            "details": {"type": "string", "description": "Replacement details. Omit to keep the queued details."},
            "priority": {"type": "string", "enum": list(priority.ACCEPTED),
                         "description": "For a todo or problem: critical, urgent, normal or low (high and medium also work)."},
            "category": {"type": "string", "enum": ["pattern", "gotcha", "best_practice", "tool_tip", "architecture", "performance", "security", "other"],
                         "description": "For a learning: pattern, gotcha, best_practice, tool_tip, architecture, performance, security or other."},
            "component": {"type": "string", "description": "Component name to file the approved record under. Optional."},
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
            "item_id": {"type": "integer", "description": "Review item id from review_list. Required."},
            "reason": {"type": "string", "description": "Why the user rejected it, in their words. Required. Kept on the item; nothing is deleted."},
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
                       "description": "add (create a link), remove (delete one) or list (read). Required."},
            "project": guide.PROJECT_PROPERTY,
            "link_type": {"type": "string", "enum": list(links.LINK_TYPES),
                          "description": "uses, depends_on, part_of, same_as, supersedes, subtask_of, blocks, about or url. Required for add."},
            "component": {"type": "string", "description": "Source: a component of project, by name"},
            "item_type": {"type": "string", "enum": list(links.ENDPOINTS),
                          "description": "Source: a record type (with item_id)"},
            "item_id": {"type": "integer", "description": "Source record id. Use with item_type. remove can use this instead of link_id."},
            "target_project": {"type": ["string", "integer"], "description": "add: target project name or id, with target_component or alone."},
            "target_component": {"type": "string", "description": "add: component name in target_project."},
            "target_type": {"type": "string", "enum": list(links.ENDPOINTS),
                            "description": "add: target record type (project, component, decision, change, learning, problem, todo). Use with target_id."},
            "target_id": {"type": "integer", "description": "add: target record id. Use with target_type."},
            "url": {"type": "string", "description": "add: https URL (GitHub, Linear or similar) stored on the source record."},
            "note": {"type": "string", "description": "add: optional short note stored on the link."},
            "link_id": {"type": "integer", "description": "remove: link id from list. Omit if you repeat the add fields."},
            "include_removed": {"type": "boolean", "default": False,
                                "description": "list: true also returns removed links (default false)."},
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
                       "description": "list (edits), undo (revert the last edit of one record) or who (who did what). Required."},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(history.TABLES),
                            "description": "list/undo: todo, project, component, problem, solution, attempt, variable, method, attachment, link, learning, decision or change. Required for undo."},
            "record_id": {"type": "integer", "description": "list: optional, with record_type. undo: the record's id. Required for undo."},
            "limit": {"type": "integer", "default": 20,
                      "description": "list/who: max rows (default 20, capped at 100). who uses it as the page size."},
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
            "kinds": {"type": "array", "items": {"type": "string", "enum": ["duplicate", "stale", "misfiled"]},
                     "description": "Which checks: duplicate, stale, misfiled. Omit for all three."},
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
            "format": {"type": "string", "enum": ["memory", "flowstate_markdown"], "default": "memory",
                      "description": "memory (default): Claude/ChatGPT export or lines. flowstate_markdown: a flowstate export-md file."},
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
            "action": {"type": "string", "enum": ["get", "set"],
                       "description": "get (read the mode) or set (change it; user must ask). Required."},
            "project": {"type": ["string", "integer"],
                        "description": "Optional: one project's override (default: whole install)"},
            "mode": {"type": "string", "enum": list(review_mode.MODES),
                    "description": "set: off (nothing waits), auto_only (default), types (also kinds) or everything. Ignored when clear=true."},
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
            "action": {"type": "string", "enum": ["add", "list", "remove"],
                       "description": "add (write a rule), list (read) or remove (mark removed; user must ask). Required."},
            "project": guide.PROJECT_PROPERTY,
            "rule": {"type": "string", "description": "add: the rule, one short sentence"},
            "rule_id": {"type": "integer", "description": "remove: the rule (R<id> in the brief)"},
            "user_request": _USER_REQUEST,
            "include_removed": {"type": "boolean", "default": False,
                                "description": "list: true includes removed rules (default false)."},
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
                       "description": "leave (write a note), list (read) or pickup (stop showing a note). Required."},
            "project": guide.PROJECT_PROPERTY,
            "where_i_was": {"type": "string", "description": "leave: what you were doing and how far it got (max 1,000)"},
            "next_step": {"type": "string", "description": "leave: the very next thing to do (max 500)"},
            "handoff_id": {"type": "integer", "description": "pickup: the note (H<id> in the brief)"},
            "include_picked_up": {"type": "boolean", "default": False,
                                   "description": "list: true includes notes already picked up (default false)."},
            "limit": {"type": "integer", "default": 20, "description": "list: max notes (default 20)."},
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
                       "description": "stale (read facts not re-checked), mark (you confirmed one) or source (set where it came from). Required."},
            "project": guide.PROJECT_PROPERTY,
            "record_type": {"type": "string", "enum": list(provenance.TABLES),
                            "description": "mark/source: todo, decision, change, learning, problem, variable or method. Required for those actions."},
            "record_id": {"type": "integer", "description": "mark/source: id of that record. Required for those actions."},
            "source": {"type": "string", "enum": list(provenance.SOURCES),
                       "description": "source: required, user_stated, ai_inferred or from_code. mark: optional, stored only if passed."},
            "days": {"type": "integer", "description": "stale: not checked for this many days (default 60, or FLOWSTATE_STALE_DAYS)."},
            "limit": {"type": "integer", "default": 20, "description": "stale: max facts to return (default 20)."},
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
            "question": {"type": "string", "description": "The question in plain words. Required. Example: which port does the LAN server use?"},
            "project": {"type": ["string", "integer"], "description": "Project name or id. Omit to search every project."},
            "limit": {"type": "integer", "default": 5, "description": "Max records to quote (default 5)."},
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
