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
    "CALL THIS FIRST in every conversation about a project, before get_context or "
    "list_projects. Returns a text brief (standard max 4,000 characters; size=quick about "
    "1,500; size=deep up to 12,000) with hard rules (never break them), any handoff, open "
    "todos and problems, recent changes, decisions and learnings, and opens a session. "
    "project is a name or id. With no project, or only folder (its name is matched to a "
    "project), returns the project list as text instead of a brief. Does not log work; "
    "use log_work."
)

LOG_WORK_DESCRIPTION = (
    "Record work as you go (write; project required). Prefer this over log_learning, "
    "log_change, problem action=log and todo action=add. kind: decision (a choice made and "
    "why), change (something changed), problem (bug or blocker), learning (lesson or gotcha), "
    "todo (follow-up). summary: one short line. details: optional. component: optional "
    "component name (changes and problems default to 'General'). suggested=true: you are "
    "not sure the user wants this recorded, so it goes to the review queue instead of being "
    "saved. priority (todos; severity for problems): critical, urgent, normal (default), low. "
    "source: user_stated (the user said it), ai_inferred (your conclusion) or from_code (read "
    "in code or commits). Never include secrets (writes containing keys, tokens or passwords "
    "are refused). Returns {logged, project_id, record}, or {queued_for_review, kind} when "
    "suggested or the review mode holds that kind. A new record is not deleted by history undo."
)

REVIEW_LIST_DESCRIPTION = (
    "List items waiting in the review queue (auto-captured session summaries, records "
    "logged with suggested=true, and kinds the review mode holds). Read-only. Show them "
    "to the user; approve or reject only with review_approve / review_reject when they "
    "ask. status: pending (default), approved, rejected or all. limit default 20 (max 100); "
    "project optional. Returns {items: [{id, project, kind, status, summary, details, "
    "source, created_at}], total, status}."
)

_USER_ONLY = (
    " ONLY call this when the user has explicitly told you, in this chat, to approve or "
    "reject this specific item. Never decide on your own initiative, and never approve "
    "your own suggestions unless the user explicitly says so. user_request must quote "
    "the user's words."
)

REVIEW_APPROVE_DESCRIPTION = (
    "Approve a review item so it becomes a normal FlowState record (a session summary is "
    "written onto that session instead). To edit then approve, pass the corrected "
    "summary/details (or priority/category/component). move_to places a misfiled item in "
    "another project. Returns {approved, kind, edited, record: {table, id}}. Undo later "
    "edits with history; undo does not delete the new record." + _USER_ONLY
    + " Write: requires project=\"<name>\"."
)

REVIEW_REJECT_DESCRIPTION = (
    "Reject a review item. It is kept, marked rejected, with the user's reason (nothing is "
    "deleted). Returns {rejected, kind, reason}." + _USER_ONLY
    + " Write: requires project=\"<name>\"."
)

LINK_DESCRIPTION = (
    "Link things across projects (write for add/remove; project required). Use when projects "
    "share something, e.g. several apps use one protocol: link(action=\"add\", project=\"App A\", "
    "link_type=\"uses\", target_project=\"Owner\", target_component=\"Wire Protocol\"). Source "
    "is the project, or component=\"<name in project>\", or item_type+item_id (decision, "
    "change, learning, problem, todo). Target: target_project (+ target_component), "
    "target_type+target_id, or url (a GitHub/Linear link to keep on a record). link_type: uses, "
    "depends_on, part_of, same_as, supersedes, subtask_of, blocks, about or url. Changes logged on a "
    "shared component alert every project linked to it (an update todo + a note in its brief). "
    "FlowState only alerts; it does not change other apps' code. remove: link_id (or the add "
    "fields) deletes the link; history undo (record_type=link) puts it back. list (read) "
    "returns the project's links. add returns the new link. Record a change to a shared "
    "component with log_work, not with link."
)

REVIEW_MODE_DESCRIPTION = (
    "Read or change the review mode: which new records wait in the review queue for the "
    "user's OK. Modes: off (nothing waits), auto_only (default: only auto-captured items), "
    "types (also the listed kinds), everything. action=get is read-only. action=set ONLY when "
    "the user has explicitly asked you, in this chat, to change the review mode; never change "
    "it on your own initiative or to get your writes saved. user_request must quote the user's "
    "words; every change is logged with them. project=... sets an override for one project "
    "(clear=true removes it); no project = the whole install."
)

HISTORY_DESCRIPTION = (
    "Edit history of FlowState records, and undo. action=list (read): recent edits and "
    "deletes in a project, or of one record (record_type + record_id), with old and new "
    "values. action=undo (write; project required): put back the last change to that record "
    "(a deleted record comes back); recorded too, so undoing again redoes it. Refused if the "
    "record changed again since. New records are not undone here (set them done/cancelled). "
    "action=who (read): who did what, newest first, across projects or one project: app, "
    "model, machine and how the app was identified, for edits, review decisions, links, "
    "handoffs, rules and new records; filters by_app, by_model, by_machine, since, until "
    "(local time, e.g. 2026-10-17 or 2026-10-17 14:30), page. list returns {history, count} "
    "with old/new fields; undo returns {undone, rows}; who returns {rows, total, page}. "
    "For what was logged as a change, use get_recent_changes, not history."
)

MODEL_PROPERTY = {
    "type": "string",
    "description": "Your model name, if you know it (optional; never guessed)",
}

CLEANUP_DESCRIPTION = (
    "Find likely duplicates, stale todos and records filed in the wrong project, and put them "
    "in the review queue as suggestions (write; project required; additive: nothing is moved, "
    "closed or deleted). The user approves or rejects each one; approving applies only that "
    "suggestion and it can be undone with history. Suggestions the user rejected are not "
    "repeated. kinds: any of duplicate, stale, misfiled (default all). Returns {queued, "
    "item_ids, already_suggested, found, examples}. Applying one is review_approve, not "
    "this tool."
)

IMPORT_DESCRIPTION = (
    "Bring memories or notes into a project's REVIEW QUEUE (write; project required; nothing is "
    "saved as a record until the user approves). format=memory (default): a Claude or ChatGPT "
    "memory export or copy, as text/markdown (one per line) or JSON. format=flowstate_markdown: "
    "a file made by `flowstate export-md`; new lines and edited lines become review items. "
    "Pass the file's contents as text. Lines that look like secrets are skipped, not stored. "
    "format=memory returns {found, queued, secrets_skipped, duplicates_skipped}; "
    "flowstate_markdown returns counts of new, edits, unchanged and secrets_skipped. One new "
    "note is log_work, not import."
)

RULES_DESCRIPTION = (
    "A project's HARD RULES: things never to do in it (e.g. \"Never push to main\"). They are "
    "shown first in every start_here brief, in full. action=list (read). action=add (write; "
    "project required): rule = one short sentence (max 300 characters; 2,000 per project); add "
    "one when the user states a rule. action=remove (write): rule_id, ONLY when the user asks "
    "you in this chat; user_request must quote their words. remove keeps the text, marked "
    "removed. list returns {rules, count, characters_used, characters_max}. A one-off choice "
    "is log_work kind=decision, not a rule."
)

HANDOFF_DESCRIPTION = (
    "Handoff notes between AIs. Stopping mid-task (out of time, context or tokens, or the user "
    "ends the chat)? action=leave (write; project required) with where_i_was (what was being "
    "done and its state) and next_step. The next AI sees it first in start_here. When you "
    "continue from a note, action=pickup with handoff_id so it stops showing (returns "
    "{picked_up: true}). action=list (read) returns {handoffs, count}. leave returns "
    "{handoff_id}. This is not a session summary; sessions close on their own."
)

VERIFY_DESCRIPTION = (
    "Keep facts current. action=stale (read): facts (paths, ports, hosts, URLs, config) in a "
    "project not checked for 60+ days; check them. action=mark (write): you confirmed a record "
    "still holds (record_type + record_id; optional source). action=source (write): set where a "
    "record came from: user_stated, ai_inferred or from_code. If a fact changed, log the "
    "change with log_work instead. stale returns {stale, total, stale_after_days} and writes "
    "nothing. mark and source return that record's provenance."
)

ASK_DESCRIPTION = (
    "Ask FlowState a question in plain words (read-only), e.g. \"which port does the LAN server "
    "use?\". Returns a short answer quoted from the best-matching records, with their ids "
    "(type #id), plus matching hard rules and handoff notes. Local only (meaning search if a "
    "local model is installed, keywords otherwise). If nothing matches it says so; it never "
    "guesses. project optional (default: all projects). Returns {question, answer, sources: "
    "[{type, id, project, text}], mode}. To list matches without an answer, use search."
)

WRITE_NOTE = " Write: requires project=\"<name>\" (or project_id)."

PROJECT_PROPERTY = {
    "type": ["string", "integer"],
    "description": "Project name or id. Required for every write.",
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
