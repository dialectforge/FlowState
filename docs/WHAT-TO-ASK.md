# What you can ask FlowState

You don't need commands. Talk to your AI app in plain words, and it uses FlowState for you.
Name the project when you can ("in Atlas, ..."), because every save goes into one project.
The examples below use a made-up project called Atlas. Change the words to suit you.

## Getting started and the brief

- "Start FlowState for Atlas."
- "What was I working on in Atlas?"
- "Give me the deep brief for Atlas."
- "Which projects do I have in FlowState?"

What happens: the AI reads the project brief. It shows hard rules first, then any handoff
note, open todos, problems, recent changes, decisions and lessons. With no project named, it
lists your projects.

## Saving decisions, changes, problems, learnings and todos

- "Save a decision: we're using Postgres, not SQLite, because of multiple users."
- "Log that I changed the login timeout to 30 minutes."
- "Note a problem: the export button does nothing on Safari."
- "Remember this lesson: always restart the app after changing the port."
- "Add a todo: write the backup docs."

What happens: the AI saves one record and its answer says what was saved. Depending on your
review mode, some saves wait for your OK first. A new decision can say which older one it
replaces, so only the current one shows in the brief.

## Todos

- "What should I do next in Atlas?"
- "Mark the backup docs todo as done."
- "The release todo is blocked by the signing todo."
- "Claim the login bug todo so no other AI works on it."

What happens: "next" lists open work that isn't blocked, highest priority first. The AI can
also mark todos done or cancelled, change priority, add subtasks or new todos with a due date,
block and unblock them, and claim, renew, release or hand off a todo.

## Problems and attempts

- "What problems are open in Atlas?"
- "Show me everything we've tried on the Safari export problem."
- "Log that clearing the cache didn't fix it."
- "Mark the Safari problem solved: it was the popup blocker."

What happens: the AI lists open problems, shows a problem with all its attempts, saves a new
attempt and how it ended, or closes the problem with what fixed it. Undo reopens it.

## Searching and asking questions

- "Search FlowState for backups."
- "What did we decide about the database?"
- "Show me decision 12."
- "What's new in Atlas since yesterday?"

What happens: FlowState searches your own records. When you ask a question, it answers by
quoting your records with their ids, and says so when it has nothing. It doesn't make
anything up.

## The review list and cards

- "What's waiting for me in FlowState?"
- "Show me what's waiting for review in Atlas."
- "Show me the disagreement card."

What happens: some things wait for your OK, such as session summaries FlowState wrote on its
own, suggestions, imports and clean-up ideas. In apps that draw cards, you see up to 5 cards
at a time. Each has **Approve** and **Reject** (Reject lets you type a short reason in the
card). A session summary has **Keep all** (**Keep** if it lists no records) and **Don't keep**,
and each record in it has its own **Keep** and **Remove** (**Bring back** after a remove; only
records that session created can be removed). A possible conflict has **They conflict** and
**They fit together**. A disagreement card has **Keep A**, **Keep B** and **Keep both**.

You decide by tapping the card, not by typing in the chat. The AI can't tap for you. Some
items are yours alone (for example "did you say this?" checks). Those show without buttons,
and you decide them on the review page, your phone or with `flowstate approve <id>`.

In Claude Code, Cursor and VS Code on the computer where FlowState runs, you may get a box for
each item instead of a card. In other apps you get a plain list and a link.

## Undo and history

- "Undo the last change to that decision."
- "Undo that undo."
- "Show me the edit history for Atlas."
- "Who changed what in Atlas this week?"

What happens: undo takes back one edit at a time, the last change to one record. An undo can
itself be undone, except the undo that brought back a delete: deleting again takes a new
request. Undo happens at once and doesn't make a review card, with one exception: undoing a
decision that replaced another asks you once whether to reopen the possible conflicts it had
closed (**Reopen** or **Leave closed**). If part of a change was changed again since, undo puts
back the rest and tells you what it skipped.

## Clean-up

- "Clean up Atlas."
- "Look for duplicate todos in Atlas."
- "Find stale or misfiled records."

What happens: FlowState looks for duplicate and stale todos, and records filed in the wrong
project. Each idea becomes a suggestion card with one plain question, such as "Cancel the
todo ... in Atlas?" Nothing changes until you approve. Clean-up never deletes: approving
cancels a todo (it is kept, and undo reopens it) or moves a record.

## Hard rules

- "Add a hard rule for Atlas: never push straight to main."
- "What are the hard rules for Atlas?"
- "Remove the hard rule about main."

What happens: hard rules show first in every brief. When the AI adds or removes one, it waits
for your approval on the review page, your phone or with `flowstate approve <id>`. No app can
approve it for you.

## Links between projects

- "Atlas VPN uses the Wire Protocol component of Atlas OS. Link them."
- "Attach this GitHub issue to the Safari problem: \<link\>."
- "What is Atlas linked to?"

What happens: when a change is logged on a shared part, every linked project gets an update
todo and a "changed since you last looked" note. FlowState alerts. It never changes other
projects.

## Handoffs

- "I have to stop. Leave a handoff for the next AI."
- "Are there any handoff notes in Atlas?"
- "Pick up the handoff and keep going."

What happens: the AI saves "where I was" and "next step". The next AI sees it near the top of
the brief until it picks it up.

## Importing memory

- "Import this Claude memory export into Atlas." (paste it or point the AI to the file)
- "Import my ChatGPT memories as learnings."

What happens: each memory goes into the review list, except repeats and anything that looks
like a secret, which are skipped. Nothing is saved until you approve it.

## Stale facts

- "Which facts in Atlas need re-checking?"
- "I checked the server port. It's still right."

What happens: paths, ports, URLs and settings not checked for 60 days are listed. You can
mark one as checked. If a fact changed, ask the AI to log the change.

## Deleting

- "Delete the old test project."
- "Delete that duplicate learning."
- "Bring back what I just deleted."

What happens: the AI deletes only when you ask. FlowState first takes a safety copy and keeps
everything removed in the edit history, so undo brings it all back. The answer says in one
line what went and how to undo it. If you turned approvals on, the delete waits for you.

## Updating FlowState

- "Is there a new version of FlowState?"
- "Update FlowState."

What happens: FlowState checks for a new release once a day and your AI tells you once. Nothing
updates until you ask. The AI needs to be able to run terminal commands on the computer where
FlowState runs. It shows you the plan first. From 2.4.0 to 2.4.3 on macOS or Linux,
updating to 2.4.4 means quitting your AI apps and running `flowstate update` in your own
terminal, because it replaces the Python packages those apps are using. An app still using them
stops the update, and the old version is put back. On Windows, or with a version older
than 2.4.0, you update by hand: see "Upgrading" in the [README](../README.md#upgrading). FlowState
is not on PyPI, so never use `pip install --upgrade flowstate`: that name is an unrelated
project.

## Wording matters

FlowState follows your words, so small changes can mean different things:

- "Show my pending todos" searches your todos.
- "What's waiting for review?" opens the review list.
- "Save this" makes a new record. "Mark it done" changes an existing todo.

If the AI does the wrong thing, say it another way.

## Where the cards show

Tap cards show in apps that support MCP Apps. On Oct 30, 2026 they were checked in Claude
Desktop, claude.ai and the Claude iPhone app. (Through the relay or your own tunnel, FlowState
first asks you once on your review page: "Allow tap cards in \<app\>?" Tap Allow.)

Apps that don't draw cards, such as Grok today, show the same items as text plus a review
link. If you use the relay, your phone link looks like
`https://flowstatemcp.com/review/<your-handle>`. Bookmark it.
