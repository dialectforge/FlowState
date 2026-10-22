# Open follow-ons

Work found during a change that did not ship with it. Add an entry when you defer something;
remove it when it ships.

## From the v2.1 tool-surface redesign (branch `redesign/tool-surface`)

- **Category filter when listing learnings.** `get_learnings(category=...)` has no listed
  equivalent: `search` lists learnings but cannot filter by category. Add
  `category` to `search` if anyone needs it.
- **Dead modules.** `mcp-server/flowstate/server_v14.py` and `split_tools.py` are imported
  by nothing. Remove them in a cleanup change.
- **Re-check the Glama score** after the next release. The 2026-10-17 score was taken from an
  older build (descriptions such as "Manage problems." that are not on main).
- **Older names in error messages.** A few error texts still name the internal operation
  (e.g. `rules(add) needs project`). They are correct but use the v2.0 names.

## From user-approved deletes (task 2, same branch)

Closed: the "user-gated hard delete" follow-on. Deletes ship as user-approved requests
(`deletes.py`, see `docs/TOOL_CONSOLIDATION.md`).

- **Erase for good.** An approved delete is kept in `record_history` (that is how undo works)
  and in backups, so the text stays in the database file. If someone needs a real erasure
  (privacy), add a user-only purge of a delete's copies (CLI or review page, never a tool).
- **Attachment file on delete.** Approving an attachment's delete removes the row and keeps
  the bundle copy of the file (so undo is complete). The older `delete_file=true` is ignored
  in the request flow; add a user-side "also delete the file" if wanted.
- **Unknown project name on reads.** A read naming a project that matches nothing (for
  example `manage_history(action="list", project=<a deleted project>)`) answers
  `project_required` instead of `project_not_found`. Wording bug in server.py's read path.
- **Mixed server versions.** A v2.0 server on the same database can undo an approved delete
  (fine) but would also allow undoing that undo, re-deleting without approval, and can't undo
  a project delete. Once deletes are used, run only v2.1+ servers against the database.
- **Plural wording.** Delete request summaries say "1 attempts", "1 links".

## From the Windows fixes (task 28, branch `release/2.4-trust`)

- **Data-git without a git identity.** On a computer with no `user.name` / `user.email` (Git
  for Windows doesn't guess one), `manage_data_git init` answers `initialized` but its first
  commit failed silently; `sync` then reports `step: commit, Author identity unknown` (honest,
  but late). Either say so at init, or commit the data folder with a FlowState identity
  (`git -c user.name=FlowState -c user.email=...`) when none is set. Not Windows-only.
- **The terminal step's honest limit, on Windows too.** `flowstate approve` needs a console
  with a window; an agent that starts a console of its own (hidden window) and types into it,
  as tests/conin_typist.py does, gets through, just as one that opens a pseudo-terminal does
  on macOS / Linux. Documented in cli_review.py; the popup and phone paths don't have it.

## From "only the user sets user_stated" (task 30, branch `release/2.4-trust`)

- **Delete and log again.** With approvals off an agent's delete goes through at once, so an
  agent could delete another app's `ai_inferred` record and log the same words as
  `user_stated`: the duplicate check finds nothing to match. It shows (the brief's "Deleted in
  the last 7 days" line, `(user, via <app>)` on the new record, undo brings the old one back),
  but it isn't refused. With approvals on, the delete waits for the user. A fix could keep
  deleted records' texts in the duplicate check for a while.
- **App names on stdio and the shared token are the app's own say-so.** "Another app" (rule 5)
  and the `via <app>` stamp use identity.py's app name; an app can claim another's name
  unless it uses a per-app key, remote token or OAuth. Same limit as every other "who" field.
- **Paraphrases.** The duplicate check is cleanup's word match (same words, or Jaccard 0.8+
  on 3+ words). A reworded copy of a guess is saved `user_stated`, shown with the app's name.
Closed in task 31: "Docs say the phone keys are placeholders". CHANGELOG 2.4.0, README's
honest limits and docs/APPROVALS.md section 3 now say what `approvals.py` does: it pins
`fsa-1` and `fsa-2`.

## From "see what's waiting without a terminal" (task 31, branch `release/2.4-trust`)

- **The site's side of the phone review list.** `https://flowstatemcp.com/review/<handle>?n=`
  (sign-in, device check, 303 to `https://<handle>.fsmcp.com/review#g=<grant>` with action
  `open_review`, item 0) is built in the web repo, not here. FlowState's side follows
  docs/APPROVALS.md section 5 and is tested with test keys only; check it end to end against
  the live site once that ships.
- **A link outlives the process that serves it.** The local page runs inside one FlowState
  process (the first that needed a link). When that app quits, a link shown earlier stops
  answering until the next brief or walk starts the page again in another process (on 8770
  again when free, else another port, so an old link with another port stays dead). A small
  separate page process, or handing the page over on exit, would fix it.
- **Phone review sessions live in memory.** Restarting the HTTP server (or the relay agent)
  ends them; the user opens a new link from the chat. Persist them (hashed, like
  `approval_grants`) if that turns out to be annoying.

Tradeoffs (decided, not debt): the walk shows at most 20 items per call (`waiting.WALK_MAX`;
asking again shows the rest); the brief's 127.0.0.1 link is also shown to apps on other
machines (it says "on this computer"); the local page has no key, by design (the link is
meant to be clicked), and its limit is documented.

## From one commit per save (task 29, branch `release/2.4-trust`)

Closed: "Save latency on Windows" (task 28). A save now commits once (`saves.py`,
`dbconn.py`); median save on the host PC 58.5 ms -> about 15 ms, the 40 ms bound unchanged.

- **Reads still commit their bookkeeping one by one.** A read that names a project
  (`start_here` most of all) writes the session touch, the "looked at" time, its run-ledger
  row and its activity row, each with its own commit (about 4 disk flushes on Windows). Not
  in task 29's scope (saves only). `saves.run` could wrap reads the same way; `start_here`'s
  git capture (runs git) would have to stay outside it.

Tradeoffs (decided, not debt): deletes, approvals (`review_approve`), data-git and undo
still commit step by step. Deletes and approvals take a safety copy first and git is slow,
and neither may hold the write lock; undo restores rows with foreign-key checks off and
finds orphans itself, while a save's connection has them on. Session tracking that fails
inside a save is still skipped without failing the write (its own rows are undone).
