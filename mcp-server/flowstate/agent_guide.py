"""What FlowState tells any MCP client: initialize instructions, the start prompt, and every
listed tool's description. Kept short: clients paste this into model context.

Each tool description follows the same order: what it is for, which sibling to use
instead, side effects (and which actions only read), and what it returns. Parameter
meanings are in the schemas (tooldefs.py)."""

INSTRUCTIONS = """FlowState is the user's project memory (todos, changes, decisions, problems, learnings).
1. At the start of every conversation about a project, call start_here(project="<name>") first and read the brief. If you don't know the project, call start_here() with no arguments (or with folder="<working folder>") to see the list, then pick one.
2. As you work, record what matters with log_work(project="<name>", kind=..., summary=...): kind=decision for choices made, change for things changed, problem for bugs/blockers, learning for lessons, todo for follow-ups. One short line each; add details only when useful. Look things up with search (answer=true for a question).
3. Every write must name the project. Never put passwords, tokens or keys in any text. If you know your model name, pass it as model="<name>" on writes; never guess or invent it (leave it out).
4. A session is opened and closed for you automatically; you don't need to manage it. Its auto-captured summary (none when it only read, undid or decided review items), and anything you log with suggested=true, waits in a review queue for the user. Never approve or reject review items unless the user explicitly asks you to in this chat. Delete (manage_projects or manage_memory action=delete) only when the user asks: it goes through after a safety copy and manage_history undo brings it back; if the user turned approvals on, it waits for them and the answer's next line says what to tell them. The user's review mode (shown in the brief) decides what else waits; never change it unless the user asks you to in this chat.
5. Things shared between projects (a protocol, a library) are linked with manage_links; log changes to a shared component by its name and the other projects are alerted.
6. The brief starts with the project's HARD RULES: never break them. If it shows a HANDOFF note, continue from it and mark it picked up (manage_handoffs action=pickup). If you must stop mid-task, leave one: manage_handoffs(action="leave", project=..., where_i_was=..., next_step=...).
7. If FlowState is slow, errors, or is unavailable, tell the user once and carry on with their task without it. Never retry more than once. A line on top saying a newer FlowState is out: tell the user once; never update FlowState unless they ask (then: flowstate update --dry-run in a terminal, show it, then flowstate update --yes --user-said "<their words>")."""

START_HERE_DESCRIPTION = (
    "CALL THIS FIRST in every conversation about a project. Returns the brief as text: hard "
    "rules, a handoff note if any, review mode, possible conflicts, open todos (and claims) "
    "and problems, recent changes, decisions and learnings. No project: lists projects. "
    "Side effects: opens or reuses the automatic session, notes when you looked, runs the "
    "user's opt-in git capture."
)

LOG_WORK_DESCRIPTION = (
    "Record one new decision (a choice and why), change (code or setup), problem (bug or "
    "blocker), learning (reusable lesson) or todo in a project. To update todos or problems: "
    "manage_todos / manage_problems. Only adds. Review mode or suggested=true may queue it for "
    "the user. A change on a shared component alerts linked projects. A decision close to a "
    "current one gets a possible-conflict note; act on it. conflicts_with opens a disagreement "
    "card. Secrets are refused. Returns {logged, record, note} or {queued_for_review}."
)

SEARCH_DESCRIPTION = (
    "Find stored todos, decisions, changes, learnings, problems and solutions. query: ranked "
    "matches. query + answer=true: a short quoted answer with record ids, or says FlowState "
    "has nothing. No query: the newest records. Read-only. Overview: start_here. "
    "Returns {results, count} or {answer, sources}."
)

TODOS_DESCRIPTION = (
    "Todos. list; next: top unblocked (others' claims last). update: status (done/cancelled), "
    "priority, title, description. block / unblock: waits on todos or a problem. add: "
    "subtask, blocker or due date only (else log_work). claim before you start (fails if "
    "someone has it); renew; release or handoff (with a note). Returns rows."
)

PROBLEMS_DESCRIPTION = (
    "Work on a project's existing problems (bugs, blockers). list: open problems. tree: "
    "one problem with every attempt and its solution. add_attempt: record a try. "
    "end_attempt: how a try ended. solve: close it with what fixed it (manage_history undo "
    "reopens it). Open a new problem with log_work kind=problem. Reads: list, tree. Writes "
    "need project. Returns problem rows, the tree, or the saved attempt or solution."
)

_USER_ONLY = (
    " approve, reject, set_mode: ONLY call this when the user has explicitly told you, in "
    "this chat; quote them in user_request; never approve your own suggestions or change "
    "the mode to get your own writes saved. Relay: approve/reject are phone-only by "
    "default; share the link."
)

REVIEW_DESCRIPTION = (
    "The user's review queue and mode: summaries, suggestions, imports, clean-ups, deletes, "
    "conflicts. list (a line each; item_id: one in full), get_mode: read-only. walk: when "
    "the user asks what's waiting (boxes or links). approve: make it a record (edit / "
    "move_to first), delete (confirm_name), confirm a conflict (keep=a|b|both). set_mode: which "
    "new records wait." + _USER_ONLY +
    " Returns {items, total}, the mode, or the result."
)

HANDOFFS_DESCRIPTION = (
    "Notes from one AI to the next. leave: when you must stop mid-task, save where_i_was and "
    "next_step; the next start_here shows it after the hard rules. pickup: once you continue "
    "from a note, mark it so it stops showing. list: open notes. Not a session summary and "
    "not a todo. Adds or marks notes; never deletes. Returns the note or {notes}."
)

RULES_DESCRIPTION = (
    "A project's hard rules: short never/always rules from the user, shown first in every "
    "brief and never cut. add: when the user states one (one sentence, max 300 chars; 25 "
    "rules, 2,000 chars in total). list: active rules. remove: ONLY when the user asks in "
    "this chat; quote them in user_request; the rule is kept as removed. Preferences and "
    "lessons go in log_work. Requires project. Returns the rule or {rules}."
)

LINKS_DESCRIPTION = (
    "Connect things across projects. add: a project uses a shared component "
    "(link_type=uses, target_project, target_component), depends on a project, two "
    "components are the same, records relate, or a record gets a URL (e.g. an issue). Then "
    "a change or decision logged on the shared component (log_work component=<name>) gives "
    "each linked project an update todo and a 'changed since' line; FlowState never edits "
    "other projects. remove: by link_id or the add fields; kept as removed (manage_history "
    "undo, record_type=link). flag_conflict: two records disagree (topic, evidence); the user "
    "keeps one. list: read-only. Requires project. Returns the link, the card, or {links}."
)

HISTORY_DESCRIPTION = (
    "The edit trail of records. list: edits with old and new values. undo: take back a "
    "record's last edit, a record you created (if nothing refers to it), or a "
    "delete with everything removed with it (a deleted project: its old name and id); rows "
    "changed since are skipped and listed. who: who did what (app, model, machine), filtered and paged. "
    "Logged changes (kind=change): find them with search. Reads: list, who. "
    "list and undo require project. Returns {history}, the undo result, or {rows, page}."
)

PROJECTS_DESCRIPTION = (
    "Projects and their components (named parts of a project). list: projects (by status). "
    "create: only when the user asks; names are unique. update: rename, describe, or set "
    "status (archived retires it). list_components / add_component / update_component: the "
    "same for components (deprecated retires one). delete / delete_component: only when the "
    "user asks. Deletes it with its child records after a safety copy; the answer gives the "
    "undo call. If the user turned approvals on it waits for their own approve instead "
    "(manage_review lists it; tell them the answer's next line). Reads: list, "
    "list_components. Returns project or component rows, or what was deleted."
)

MEMORY_DESCRIPTION = (
    "Memory upkeep. cleanup: queue likely duplicate, "
    "stale and misfiled records as suggestions (rejected ideas not repeated). import: a "
    "memory export (Claude, ChatGPT) or edited FlowState markdown into the review queue. "
    "stale: facts (paths, ports, URLs, config) unchecked for 60+ days (read-only). "
    "mark_checked: you re-checked one. set_source: correct an origin (user_stated waits for "
    "the user). delete: only "
    "when the user asks; deletes one record (and its attempts or links) after a safety copy, "
    "undo restores it; with approvals on it waits for the user's approve (manage_review). A "
    "changed fact is log_work. Requires project. Returns counts and ids, {facts} or the result."
)

SESSIONS_DESCRIPTION = (
    "Inspect FlowState's sessions. They open and close by themselves (closed after 2 hours "
    "idle or when the app quits, with a summary queued for review), so this is rarely "
    "needed. current, list, conversations: read. start: returns the automatic session. "
    "end: close one with a summary (not undoable). Decisions go in log_work. Returns "
    "session or conversation rows."
)

FILES_DESCRIPTION = (
    "File attachments of a project. attach: a file on this computer, to the project, a "
    "component or a problem; copied into FlowState's bundle by default. list, search (text "
    "in attached files): read. remove: detaches it (the bundle copy is kept; undo restores "
    "it; with approvals on it waits for the user). Local apps only: "
    "attach and remove are refused over HTTP. Requires project for writes. Returns "
    "attachment rows, hits or the request."
)

DATA_GIT_DESCRIPTION = (
    "Git for FlowState's own data folder, NOT the user's code repository; never use it for "
    "the user's project. status, history: read. init, set_remote, clone (into an empty "
    "folder): set up. sync: commit and push to the remote (reaches the network; not "
    "undoable). Local apps only: refused over HTTP. Returns git results."
)

VARIABLES_DESCRIPTION = (
    "Non-secret project settings: hosts, ports, endpoints, config. create, list (read), "
    "update (undo with manage_history), delete (undo restores it; with approvals on it waits "
    "for the user; refused for HTTP app keys and remote apps). Secrets "
    "are refused; is_secret only "
    "labels a value and does not hide it, so never store passwords, tokens or keys. "
    "Requires project. Returns variable rows."
)

METHODS_DESCRIPTION = (
    "Documented reusable procedures of a project (deploy, test, a code convention), with "
    "ordered steps. create, list (read), update (steps replaces the whole list; undo with "
    "manage_history), delete (undo restores it; with approvals on it waits for the user; "
    "refused for HTTP app keys and remote apps). A one-off choice is log_work kind=decision; a "
    "lesson is kind=learning. Requires project. Returns method rows."
)

SKILLS_DESCRIPTION = (
    "The connected app's own learned skills, checkpoints and metrics: how the AI works, not "
    "project facts (those go in log_work). learn: save a skill. list: a project's and "
    "global skills. confirm: one more session where it held (promoted after 3 at a 0.8 "
    "rate). save_state / get_state: a checkpoint. log_metric / list_metrics: scores. "
    "Reads: list, get_state, list_metrics; the rest only add rows or counts. Returns rows."
)

MODEL_PROPERTY = {
    "type": "string",
    "description": "Model name if known. Never guess.",
}

PROJECT_PROPERTY = {
    "type": ["string", "integer"],
    "description": "Project name or id. Required.",
}

USER_REQUEST_PROPERTY = {
    "type": "string",
    "description": "The user's words in this chat asking for this.",
}

PROMPT_NAME = "flowstate-start"
PROMPT_DESCRIPTION = "Load FlowState project memory and work with it for this conversation."


def register_prompts(server) -> None:
    """The flowstate-start prompt. Lives here so server.py stays a thin router."""
    import asyncio

    from mcp import types
    from mcp.types import TextContent

    @server.list_prompts()
    async def list_prompts():
        return [types.Prompt(
            name=PROMPT_NAME, description=PROMPT_DESCRIPTION,
            arguments=[types.PromptArgument(name="project", description="Project name (optional)",
                                            required=False)])]

    @server.get_prompt()
    async def get_prompt(name: str, arguments):
        if name != PROMPT_NAME:
            raise ValueError(f"Unknown prompt: {name}")
        project = (arguments or {}).get("project") or ""
        brief = ""
        if project:
            from . import server as flow
            if not flow.backend.error:
                try:
                    text = await asyncio.wait_for(
                        asyncio.to_thread(flow._start_here, {"project": project}, flow._client_name()),
                        timeout=flow.call_timeout())
                    brief = text if isinstance(text, str) and text.startswith("FlowState brief:") else ""
                except Exception:  # noqa: BLE001 - the prompt still works without the brief
                    brief = ""
        return types.GetPromptResult(
            description=PROMPT_DESCRIPTION,
            messages=[types.PromptMessage(role="user", content=TextContent(
                type="text", text=prompt_text(project, brief)))])


def prompt_text(project: str = "", brief: str = "") -> str:
    lines = [
        "Use FlowState (my project memory) for this conversation.",
        INSTRUCTIONS,
    ]
    if project and brief:
        lines += ["", f"Here is the current FlowState brief for {project}:", brief]
    elif project:
        lines += ["", f"Start by calling start_here(project=\"{project}\")."]
    else:
        lines += ["", "Start by calling start_here() to see my projects, then ask me which one."]
    return "\n".join(lines)
