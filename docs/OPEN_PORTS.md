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

## From no summaries for housekeeping sessions (task 39, branch `dev/2.4-review-fixes`)

Closed in task 39: on Oct 22, 2026 one undo run left 18 session summaries (#105-#122) John
had to reject one by one. A session made only of reads, undo, review decisions, a handoff
pickup or todo claims (`autosession.HOUSEKEEPING`) now files no review item; summaries count
saves by kind; the pages ask "Keep this session summary?" (Keep / Don't keep).
Also closed: `test_save_latency_on_2000_records` failed at random under disk load on
example-host (about 15 ms alone, 36-59 ms when the disk was busy, against a fixed 40 ms wall-clock
bound). It now checks what the code controls: each save commits exactly once (counted), and the
save's time without that one commit's disk wait stays under 40 ms (about 10 ms here, also with
three disk-flooding writers running).

Open:
- **Optional one-time clean-up of already-queued housekeeping summaries.** Not built: nothing
  decides old items. On the 2026-10-22 copy no such item was pending (all 32 were decided), so
  it isn't needed now; if older installs have some, a `flowstate cleanup` rule that queues
  "withdraw these N housekeeping summaries?" as one question would be the low-touch way.
- **15 tagged sessions without an item on the live copy.** Sessions 110-136 (closed Oct 17-19)
  carry `[auto-captured, needs review]` and `needs_review: true` but no `review_item_id` and no
  review item. The close code on `release/2.4-trust` and here always records the item id when
  it queues one, so they were most likely closed by an older build; not investigated further
  (read-only copy, no logs). Worth one look at the live logs for those dates.
- **The phone list and tunnel page wording on a real phone.** Keep / Don't keep is checked in
  tests (the list's JSON, the page HTML), not looked at in a browser.

Tradeoffs (decided, not debt):
- **What counts as housekeeping** is a fixed allow-list in `autosession.py`; every other write
  label keeps the summary (fail safe). Review-mode changes, clean-up suggestions, memory imports,
  `verify` marks, link/rule changes, deletes and `session/end` stay real work. Labels written
  before this build called a review-mode change `read:review_mode`, so such old sessions count
  as housekeeping in the copy numbers.
- **"saved N ..." counts calls, not where they went**: in review mode `types` / `everything` a
  learning that was held for review still counts as "saved 1 learning" in the summary.
- **Skipped sessions win over review mode off**: with review off, a housekeeping session gets
  the housekeeping tag (not `[auto summary, review off]`), so the brief's "Last session" line
  skips it in both modes. Real sessions with review off are unchanged.

## From the /review/start lockout and tunnel review (task 38, branch `dev/2.4-review-fixes`)

Closed in task 38: "Rate limits on `/review/start` are per database" (task 33). It now needs the
site's signed start ticket; limits are per site user (30 an hour) with a 120-an-hour backstop,
one code per ticket; ticketless requests count nothing.

Open:
- **Deploy order and the live check.** The site's half (`/review/<handle>` 303 to
  `/review/start?t=<ticket>`) is built separately to docs/APPROVALS.md section 5a. Deploy the
  site first; a device on this build with the old site shows the plain "open the link again"
  page, so phone review is unavailable until the site ships. Then check the whole flow end to
  end against the live site (FlowState's side is tested with test keys only).
- **The tunnel page on a real phone.** Sizing for 390 px (viewport, 44 px tap targets, wrapped
  rows, tables that scroll inside themselves) is checked in tests by its CSS rules only, not
  rendered in a browser. Look at it on a phone once.
- **Passphrase lockout is shared and global.** The tunnel page uses the consent page's lockout
  (5 wrong in 15 minutes for everyone, as before), so someone who reaches the tunnel can lock
  the owner out of both pages for 15 minutes; the per-address limit (10 tries per 15 minutes)
  doesn't stop many addresses. A Cloudflare Access rule on `/review/tunnel` and `/consent`
  (docs/REMOTE-ACCESS.md) avoids it; a per-address lockout would soften it in FlowState.
- **Linux desktop folder.** The shortcut goes to `~/Desktop` (skipped with a line when it
  doesn't exist); a desktop renamed through `XDG_DESKTOP_DIR` (other languages) isn't read.

Tradeoffs (decided, not debt):
- **Ticket storage**: two NULL columns on `review_open_nonces` (022) instead of a new table; the
  ticket's nonce is kept only as a hash on the code it made, so a ticket refused for the limit
  is not remembered (it made no code) and can be retried within its 5 minutes.
- **Tunnel sessions and sign-in form tokens live in memory** (like the phone list's): a
  restart signs the user out. Form tokens are capped at 500 (the oldest go first), so a flood
  of page loads can make an open sign-in form say "expired; try again".
- **Windows shortcut is a `.cmd` file**, not a `.lnk`: no PowerShell or COM, and its console
  window shows the review page's "Ctrl+C stops it" line while the page runs.

## From plain clean-up questions (task 37, branch `dev/2.4-review-fixes`)

Closed in task 37: on Oct 22, 2026 John approved 50 "Misfiled?" items he couldn't judge (27
"unsure"); all were undone. A move is now only suggested when it is "likely" and the record
names the target project or one of its components (and not its own project); unsure leftovers
are withdrawn at startup and on each clean-up run; every item is one plain question.

Tradeoffs (decided, not debt):
- **Withdraw rule.** TASK.md said "unsure (or no `reason`)" but also that an old-shape
  "likely" item (which has no `reason`) stays pending. Implemented: withdraw when confidence
  is "unsure", or when it is neither "likely" nor has a `reason`. An old "likely" item waiting
  from before 2.4 is still asked as it was (none were pending on Oct 22, 2026).
- **Titles cut to 40 characters**, not about 50, so the move ("from A to B") still shows in a
  `list limit=100` line (120 characters, quotes cost 2 each in JSON).
- **Duplicate learnings/problems are not suggested at all** (approving them only noted it). A
  real merge for them would be a new feature; not planned.

## From the compact review list (task 35, branch `dev/2.4-review-fixes`)

Closed in task 35: `manage_review list limit=100` on the live install (102 items, Oct 22, 2026)
answered more text than fits in one reply. `list` now answers one line per item (a full page of
100 long items is about 13,000 characters); `list item_id=<id>` gives one item in full.

Tradeoffs (decided, not debt): `items` in `list` are now text lines ("#id (project, kind):
summary"), not objects; a script that read `items[].id`, `.kind` or `.details` must read the
id from the line's `#id` and fetch the item with `item_id`. At limit 100 each line is cut to
120 characters (160 for pages of up to 75), so long summaries end in "...".

## From the local link only for local apps (task 34, branch `dev/2.4-review-fixes`)

Closed in task 34: the 127.0.0.1 review link went to apps on other computers too (John's Mac
through the relay, Oct 22, 2026, opened the Mac's own 127.0.0.1).

Tradeoffs (decided, not debt): "on this computer" is the LAN listener with a loopback socket
peer. A program on this computer that forwards other computers' connections to the LAN
listener (a tunnel or proxy the user set up themselves, not FlowState's relay) looks local,
and its apps get the 127.0.0.1 link; FlowState can't tell. An app on this computer that
connects to the LAN listener through the computer's network address (not 127.0.0.1) looks
remote and gets the phone link or the note instead; that is the safe side.

## From the honest walk and the bookmark link (task 33, branch `dev/2.4-review-fixes`)

Closed in task 33:
- **The walk said "The user decided these" when no box was shown** (live, Oct 22, 2026: a
  relay connection named "Cursor" got 20 boxes, each declined in milliseconds). Relay and
  public-listener requests get no boxes (walk or popup); an answer under 1 s counts as not
  shown and is not logged; the note says what happened. The 20 `declined` rows already in the
  live approval_log are the user's to report; nothing was approved or rejected.
- **The walk showed at most 20 items with no way to the rest** (was a tradeoff). `list` and
  `walk` page with `after=<id>` and `next_page`.
- **"Docs say the phone keys are placeholders"**: re-checked every doc; APPROVALS.md's "at
  least one real key" hedges and the 302 in section 2 (the site uses 303 and a Continue page)
  are fixed. The pinned `fsa-1` / `fsa-2` match the published file (checked Oct 22, 2026).

Open:
- **The site's side of the bookmark link.** `GET https://flowstatemcp.com/review/<handle>`
  (bare: sign-in, device check, 303 to `/review/start`) and `/review/<handle>?n=` (Continue
  page, grant) are built in the web repo at the same time; on Oct 22, 2026 13:07 UTC the live
  site answered 404 for both. Since task 38 the bare link must also sign a start ticket
  (`?t=`, docs/APPROVALS.md section 5a); see task 38's list. FlowState's side follows docs/APPROVALS.md section 5 and is
  tested with test keys only (unit tests and e2e S15); check it end to end against the live
  site once that ships.
- **The 1-second "too fast" rule is a heuristic.** An app that waits a second and then
  auto-declines still looks like a person to FlowState (it decides nothing: a decline only
  skips; an auto-approve after a pause would count, which is why boxes stay limited to the
  allowlisted apps connected directly, never through the relay). A signed or app-level "a person answered" signal would
  be the real fix; MCP has none today.
- **Phone list shows the oldest 50.** The phone page says how many wait in total and that
  deciding some shows more; it has no "next page" button yet.

## From "see what's waiting without a terminal" (task 31, branch `release/2.4-trust`)

- (Moved to task 33's list: the site's side of the phone review list, now the bookmark
  link.)
- **A link outlives the process that serves it.** The local page runs inside one FlowState
  process (the first that needed a link). When that app quits, a link shown earlier stops
  answering until the next brief or walk starts the page again in another process (on 8770
  again when free, else another port, so an old link with another port stays dead). A small
  separate page process, or handing the page over on exit, would fix it.
- **Phone review sessions live in memory.** Restarting the HTTP server (or the relay agent)
  ends them; the user opens a new link from the chat. Persist them (hashed, like
  `approval_grants`) if that turns out to be annoying.

Tradeoffs (decided, not debt): the local page has no key, by design (the link is meant to
be clicked), and its limit is documented. (Closed in task 34: the brief's 127.0.0.1 link was
also shown to apps on other machines; it is now only given to apps on the same computer.) (The 20-per-walk limit
now pages: task 33.)

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
