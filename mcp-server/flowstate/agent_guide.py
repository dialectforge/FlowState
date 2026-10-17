"""What FlowState tells any MCP client: initialize instructions, the start prompt, and the
wording shared by tool descriptions. Kept short: clients paste this into model context."""

INSTRUCTIONS = """FlowState is the user's project memory (todos, changes, decisions, problems, learnings).
1. At the start of every conversation about a project, call start_here(project="<name>") first and read the brief. If you don't know the project, call start_here() with no arguments (or with folder="<working folder>") to see the list, then pick one.
2. As you work, record what matters with log_work(project="<name>", kind=..., summary=...): kind=decision for choices made, change for things changed, problem for bugs/blockers, learning for lessons, todo for follow-ups. One short line each; add details only when useful.
3. Every write must name the project. Never put passwords, tokens or keys in any text. If you know your model name, pass it as model="<name>" on writes; never guess or invent it (leave it out).
4. A session is opened and closed for you automatically; you don't need to manage it. Its auto-captured summary, and anything you log with suggested=true, waits in a review queue for the user. Never approve or reject review items unless the user explicitly asks you to in this chat. The user's review mode (shown in the brief) decides what else waits; never change it unless the user asks you to in this chat.
5. Things shared between projects (a protocol, a library) are linked with the link tool; log changes to a shared component by its name and the other projects are alerted.
6. The brief starts with the project's HARD RULES: never break them. If it shows a HANDOFF note, continue from it and mark it picked up (handoff action=pickup). If you must stop mid-task, leave one: handoff(action="leave", project=..., where_i_was=..., next_step=...).
7. If FlowState is slow, errors, or is unavailable, tell the user once and carry on with their task without it. Never retry more than once."""

START_HERE_DESCRIPTION = (
    "CALL THIS FIRST, before get_context or list_projects. Returns a brief and opens "
    "a session. No project, or only folder, lists projects."
)

LOG_WORK_DESCRIPTION = (
    "Record one decision, change, problem, learning, or todo. suggested=true queues it. "
    "Secrets are refused. Undo does not delete it."
)

REVIEW_LIST_DESCRIPTION = (
    "List the review queue. Read-only. Approve or reject only when the user asks. "
    "Returns {items, total, status}."
)

_USER_ONLY = (
    " ONLY call this when the user has explicitly told you, in this chat, to approve or "
    "reject this item, and never approve your own suggestions."
)

REVIEW_APPROVE_DESCRIPTION = (
    "Approve one queued item into a normal record (session summaries go on that "
    "session). Edit fields first, or move_to for a misfiled item. Undo does not "
    "delete the new record."
    + _USER_ONLY
)

REVIEW_REJECT_DESCRIPTION = (
    "Reject one queued item. It stays marked rejected; nothing is deleted."
    + _USER_ONLY
)

LINK_DESCRIPTION = (
    "Link projects, components, records, or a URL. A shared-component change alerts "
    "linked projects and does not edit other apps. history undoes a remove "
    "(record_type=link)."
)

REVIEW_MODE_DESCRIPTION = (
    "Which new records wait: off, auto_only (default), types, or everything. "
    "set ONLY when the user has explicitly asked you, in this chat, to change the "
    "review mode; quote user_request; never to get your own writes saved. "
    "clear=true removes an override."
)

HISTORY_DESCRIPTION = (
    "list reads edits. undo restores the last edit or a deletion, unless it changed "
    "again. who lists app, model, and machine. Logged changes are get_recent_changes."
)

MODEL_PROPERTY = {
    "type": "string",
    "description": "Model name if you know it. Never guess.",
}

CLEANUP_DESCRIPTION = (
    "Queue duplicate, stale, and misfiled suggestions. Nothing changes until approved; "
    "history can undo that. Rejected ideas are not repeated."
)

IMPORT_DESCRIPTION = (
    "Copy memories into the review queue; nothing is saved until approved. "
    "Secret-like lines are skipped. One note is log_work."
)

RULES_DESCRIPTION = (
    "Hard rules, shown first on every brief. add one sentence (max 300; 25 rules, "
    "2,000 total) when the user states one. remove only when they ask; quote them."
)

HANDOFF_DESCRIPTION = (
    "Leave a note when you stop mid-task (where_i_was, next_step); the next start_here "
    "shows it. pickup hides it. Not a session summary."
)

VERIFY_DESCRIPTION = (
    "stale lists paths, ports, hosts, URLs, and config unchecked for 60+ days. "
    "mark records a re-check. A changed fact is log_work."
)

ASK_DESCRIPTION = (
    "Answer from stored records (read-only), with ids. Meaning search if a local "
    "model is installed, else keywords. Says when nothing matches; never guesses."
)

WRITE_NOTE = ""

PROJECT_PROPERTY = {
    "type": ["string", "integer"],
    "description": "Name or id. Required on writes.",
}

PROMPT_NAME = "flowstate-start"
PROMPT_DESCRIPTION = "Load FlowState project memory and work with it for this conversation."


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
