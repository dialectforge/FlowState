# Changelog

## 2.4.4 - 2026-10-24

- No new dependencies and no database migration. `flowstate update` from 2.4.0 to 2.4.3
  needs no new `.venv`.
- **A name for this FlowState.** Each install now has a display name, "Main FlowState" until
  you change it, so people see a name instead of the relay handle (the random letters in your
  relay address). Change it with `flowstate name "Office laptop"` (no word shows it,
  `flowstate name --reset` goes back to "Main FlowState") or in the new first row of the review
  page's Settings ("Name of this FlowState"; editable on the page `flowstate review-page` opens,
  read-only on the page an AI's link opens, absent on the tunnel page). 1 to 40 characters,
  trimmed, plain text, no control characters. It is kept in `config.json` as `"display_name"`
  and every change is logged with the other settings (Recent changes).
- Where it shows: the review page's title and header (local and tunnel), and, once the phone link
  has checked out, the phone list ("Waiting for you on Office laptop (3)") and the phone approve
  page. Never in a URL: every link still uses the handle. No MCP tool reads or writes it, and
  the brief doesn't carry it.
- With the relay, the name is shared only once you set one. The agent then sends it in its
  signed-in hello (and again when you rename it while `flowstate relay run` is running, within a
  heartbeat), so flowstatemcp.com can say "Open what's waiting for you on Office laptop?"
  instead of the handle. The default "Main FlowState" is never sent: without a name of your own
  the agent sends an empty (null) name and the site says "your FlowState". `flowstate name
  --reset` (or the page's "Back to" button) clears it on the relay too, also if the agent was
  stopped at the time (its next hello clears it). A relay or site that doesn't know the name
  ignores it and nothing else changes (docs/relay-protocol.md 3.1). The Settings row says so.
- **Session summaries show what the session saved.** A summary waiting for review used to say
  only "saved 1 change; also problem solved", so Keep / Don't keep was a blind choice. Now every
  place that shows one (the local review page, the tunnel review page, the phone list, the
  phone approve page, the in-chat review card and its text, and the chat walk's boxes) lists
  each record the session saved: its kind, id, title and the first 200 characters of its text,
  the component, and for a solved problem the solution. It also shows the app and the session's
  start and end time: in the reader's own time on the phone and the card, in this computer's
  local time on the local page, and in UTC, labelled, on the tunnel page. Past 10 records the
  local and tunnel pages open the rest in place; the phone list and the phone approve page have
  a "Show N more" button that opens them in place too (they are already in the page, so no
  request is made; up to 100 records, past that the page points to the review page on your
  computer, which it also mentions). The in-chat card counts the rest ("3 more").
- The summary line itself stays the short one ("claude-code session in Alpha: saved 1 change
  and 1 problem; also problem solved."): it is what Keep saves as the session's record, and the
  review page's edit box still lets you change it first. The record list is a separate section
  shown beside it everywhere, also in `manage_review(action="list", item_id=...)`
  (`saved_records`) and in the confirmation `flowstate approve <id>` prints.
- New summaries store the ids of what was saved (from the run ledger, ids only, never the text)
  with their review item; the text is read from the records when shown, so an edited record
  shows as it is now and a deleted one says "no longer in FlowState". Summaries queued before
  this change fall back to the run ledger while it still has the session (the activity log's
  days); after that they say only the counts are known. All of it is escaped text (HTML
  escaping on the pages, textContent in the scripts); the phone page shows nothing it couldn't
  before (record text from this project's memory, no paths, keys or tokens).
- **Keep all, and Keep / Remove for each record of a session summary.** The records a session
  lists were saved when the AI wrote them, so the only way to drop a bad one used to be asking
  the AI to undo it. Now each record has its own **Keep** (marks it reviewed) and **Remove**
  (takes it back) on the local and tunnel review pages, the phone list, the phone approve page
  and the in-chat review card, with a line saying the records are already saved. Remove goes
  through the user's delete path: a verified safety copy first, then the record and what belongs
  to it (a problem's solution and attempts, a decision's links) as one edit in the edit history,
  so it can be restored: **Bring back** on the same pages (while the summary waits), or
  `manage_history(action="undo", ...)` at any time. A removed record shows as "removed by you";
  one that is already gone or was changed after the session shows that and has no buttons. Only
  records the session created can be removed there; one it only updated is Keep only.
- The summary's own Keep is now **Keep all**: it keeps the summary line (edit-before-keep still
  works) and marks every record you haven't decided kept; removed records stay removed.
  **Don't keep** decides only the summary line and removes nothing. With every record decided,
  the summary still waits for its own Keep all / Don't keep. The session's saved note ends with
  "Removed by the user: change #12 "..."" when you removed any.
- Only you decide records, as with review decisions: there is no tool for the AI. The card's
  per-record buttons use its app-only `resolve_review_card` (decision `keep_record` /
  `remove_record` / `restore_record`) with the card's rules (no chat text, MCP Apps hosts only;
  through the relay phone only unless chat approvals are on). In a terminal:
  `flowstate review records <id>` lists a summary's records with their state, and
  `flowstate review record <id> <type:id> keep|remove|restore` decides one (asks for your `y`).
  Each decision is logged ("record removed: change #12", with where it was made) and shown in
  `manage_review` lists as read-only states ("[2 records: 1 kept, 1 removed by you]";
  `record_states` with `item_id`). Stored in the review item (ids only); no migration.
- A **Settings** section on the review page that `flowstate review-page` opens (this computer
  only). It lists every user setting in plain words with its current value, and lets you change
  it there: review mode (whole install and per project; the existing form, moved in), app
  pauses, approvals on / off (whole install and per project), the apps that may show an
  approval popup (claude.ai and Anthropic apps still never get one), phone approval links, chat
  approvals through the relay, whether `flowstate update` may update this computer
  (`config.json` `"self_update"`) and the daily update check. Settings that only come from the
  app's environment (`FLOWSTATE_REVIEW_PORT`, `FLOWSTATE_IDLE_MINUTES`, ...) are shown
  read-only, and settings with their own terminal commands (backups, keys, remote access) are
  listed with the command. No token, key or passphrase is ever shown.
- Turning approvals **off**, or chat approvals through the relay **on**, asks on a second page
  first, the page's stand-in for the terminal's `y`: its button carries a single-use token tied
  to your browser session and to that exact change, valid for 5 minutes. The other directions
  need no confirm, like the commands. The commands themselves are unchanged.
- Every change made there is logged in `review_mode_log` as `review-page` (with the app,
  machine and identity fields), including app pauses, popup-list and phone-link changes and the
  `config.json` settings. The section ends with the last few setting changes.
- Without the relay on this computer, the relay rows (phone links, chat approvals) are greyed
  out with "Only used with the FlowState relay (not set up here)", and the page ignores a
  change sent to them anyway.
- Where it isn't: the page FlowState opens from a link in the brief or the walk (no one-time
  key) shows the new settings read-only, with "open `flowstate review-page` to change these"
  (review mode and app pauses work there as before). The tunnel page, the phone pages and the
  relay have no Settings section and no settings routes. No MCP tool reads or writes any of
  these settings (tests check every tool, and that only the terminal commands and the local
  page call the writers).
- The local review page is now readable in a phone-sized window (viewport, 44 px buttons,
  tables scroll inside themselves).
- In apps that draw MCP Apps cards, the brief now says how many review items wait and names
  `show_review_card(project=...)`, as it already did for a disagreement card. When both wait
  it is one line naming both calls. Apps without cards get the review-queue line as before.
- Fixed: items, claims and changes written by an AI connected through the relay showed the
  name of the sign-in it used (for example a remote token named "Cursor", or one OAuth sign-in
  shared by several cloud apps) as the app that wrote them, on the review page, the phone list
  and the cards. Through the relay the app recorded is now the name the connecting app sends
  at connect time, or "Relay (unknown app)" if it sends none, and the machine is "relay
  (signed in as <sign-in name>)", so the sign-in is still on record. Pausing the sign-in's
  name still stops it, and an app can't decide its own review item by sending a different
  name (the same sign-in counts as the same app). Rows already saved are not changed. Local,
  LAN and own-tunnel callers are recorded as before.
- **Both names, where the app name is only the app's claim.** Wherever FlowState shows who wrote
  something (the local and tunnel review pages, Who did what, Recent activity and the app list
  in Settings, the phone list and approve page, the in-chat cards, the chat walk's boxes, a
  session summary's "by ..." line, the brief's handoff and "Since your last visit" lines, undo
  and claim messages), an app that came in through the relay or your own tunnel now shows with
  the sign-in it used when the two differ: "Grok Bot (via Cursor)", or "Relay (unknown app, via
  Cursor)". Same name, local (stdio) apps, per-app keys and the LAN token show one name as
  before. `manage_history(action="who")` rows carry a `via` field for these. Nothing new is
  stored: the sign-in is already in the machine column ("relay / tunnel (signed in as ...)"),
  and only rows from a remote token or OAuth sign-in are read that way, so a LAN app can't fake
  one with its workspace header. Older rows show as they are.
- Session summaries FlowState writes for an app now carry the app, model, machine and identity
  its session recorded (they had only the client name), so a relay session's summary says
  "by Grok Bot (via Cursor)" and the local page can say "by claude-code on <this computer>".
- **Own tunnel (`--public-url`) callers are recorded like relay callers.** With a remote token or
  OAuth sign-in on the public listener the app recorded is the name the app sends, else
  "Tunnel (unknown app)", and the machine is "tunnel (signed in as <sign-in name>)". Pausing the
  sign-in's name stops it, it can't decide its own review items by sending another name, and
  undo only lets it take back records made through the same sign-in (a remote app calling
  itself "claude-code" can no longer take back your local claude-code's records; this check now
  also applies through the relay). Remote tokens on the LAN listener are recorded as before.
- Docs: one token per app is recommended wherever remote tokens are documented (REMOTE-ACCESS,
  AGENTS.md, README), with how to replace a shared token (`flowstate remote clients`, a new
  token per app, then revoke the old one). The docs' example token is now "my-laptop-app"
  instead of "Cursor", and `flowstate remote add-client` says to use each token in one app.
- **`flowstate remote rename "<old name>" "<new name>"`** gives a remote token or OAuth app a new
  name without making a new token: the same token keeps working and nothing changes in the app.
  The old name may also be the id from `flowstate remote clients`. Refused, with a sentence
  saying why: no active app of that name (or several; then rename by id), an empty or
  secret-looking new name, or a name another app has or had before a rename. Past records keep
  the name they were saved under; new ones show the new name. The safeguards follow the token,
  not the name: a paused token stays paused under its new name, it still can't decide review
  items it queued under its old name, and undo still lets it take back only its own records
  (old names are kept in the settings database's new `client_names` table, so `remote clients`
  can show them as "was ..."; the FlowState database is not changed).
- **Both names on the LAN too.** Per-app keys (`flowstate keys add`) and remote tokens used on
  the LAN listener now follow the same rule as the relay and the tunnel: the app recorded is
  the name the app sends (else the key's or token's name), and the key's or token's name is
  recorded beside it in the machine column: "<the key's workspace> (signed in as <key name>)",
  "lan (signed in as <name>)" without a workspace or for a remote token. Every place that shows
  "Grok Bot (via Cursor)" for relay and tunnel callers now does it for these too, one name when
  they match; a key's workspace still shows as "on <workspace>" where pages show the machine.
  Pausing the key's or token's name stops it, it can't decide its own review items by sending
  another name, and undo is keyed to the key or token (a key and a remote token of the same
  name are different callers). Before, these recorded the key's or token's name as the app;
  those rows show as they were saved and still count as that key's or token's own. The shared
  LAN token is unchanged: the name the app sends, "shared token (legacy)" on Who did what.
- Who did what's app filter (the review page and `manage_history(action="who", by_app=...)`)
  also finds rows by the key's, token's or OAuth app's name they came in on, so filtering by a
  key's name still finds what it wrote.
- `flowstate update self-update on|off`: whether `flowstate update` may update this computer,
  from the terminal (`config.json` `"self_update"`, the same setting as on the review page's
  Settings). With no word it shows the value and where it comes from: `config.json`, the
  `.flowstate-no-self-update` file, or the default. A change is logged in `review_mode_log` like
  the page's (as `command-line`, "set in the user's terminal") and shows in the page's recent
  changes. No confirm prompt, as on the page. If the `.flowstate-no-self-update` file is there,
  `on` says it still keeps the folder updated by hand and how to delete it; FlowState never
  deletes it.
- Review cards in the chat. Hosts that support MCP Apps (such as Claude Desktop) now get a card
  for everything else that waits in the review queue: session summaries, suggestions, imports,
  clean-up ideas, markdown edits and possible conflicts. The AI calls a new read-only tool,
  `show_review_card` (one item, or up to 5 waiting items of a project, oldest first, with
  `more`), whose definition names `ui://flowstate/review-card.html`. Each card says what the
  item is, what saving it would do, which app wrote it and when, with Approve and Reject; Reject
  asks for a short optional reason inside the card. Disagreements keep their own card:
  `show_review_card` points to `show_conflict_card` for them. `manage_review` list and walk
  answers on those hosts include the call to make (`show_card`; both calls when both kinds
  wait). Apps without MCP Apps see no new tool and the same plain text as before.
- The card's buttons call `resolve_review_card`, hidden from the model (`visibility: ["app"]`)
  like `resolve_conflict_card`, with the same rules: chat text (`user_request`, `message`,
  `text`, `reason`) is refused, so are apps without MCP Apps, and through the relay a tap is
  phone only unless `flowstate relay chat-approvals on`. Items only the user decides (with
  approvals on: deletes and turning review off; every "did you say this?" check) and every
  delete show without buttons, with the phone or review-page link, and are refused if the tool
  is called anyway. Every card decision is logged in the approvals log with path `card`, for
  every kind. Approving or rejecting runs the same step as `manage_review` (one shared module,
  `review_apply.py`); `manage_review`'s own rules are unchanged. Since the fixes below, a tap
  also needs the card's one-use code and an app on your card list.
- Fix: the app whose save opened a disagreement card could dismiss it through `manage_review`
  by quoting its own decision: the card's own words were only its headline ("Two AIs disagree
  about ..."). The own-item check now compares against both claims on the card, as it did for
  the possible-conflict item the card replaced in 2.4.3. (Found by the end-to-end run, S3.)
- Fix: the review page's port note (`review-page/<db>.json`) now goes under `FLOWSTATE_HOME`
  when that is set, instead of always the per-OS data folder, so a second install or a test
  database no longer writes into the main one's folder. New data-folder git repos ignore
  `review-page/`.

### Fixed before release (audit of 2.4.4)

- **Cards only count when you tap them.** The in-chat cards (review card and disagreement card)
  used to trust any app that said it could show them, so a program pretending to be a card host
  could approve its own review items, and the log said "card" as if you had tapped. Now:
  - only apps on your card list get the cards and their buttons. The list starts with
    `claude-ai` (Claude Desktop and claude.ai); `flowstate approvals cards add|remove NAME`
    (adding asks for your `y` in the terminal) or the new Settings row "Apps that may show
    review cards in the chat" changes it;
  - every card with buttons gets a one-use code that only the card's own page receives (in
    the tool result's `_meta`, never in the text the AI reads). A tap without it, with another
    card's code, a used code or one older than 15 minutes is refused (`not_a_card_tap`), so the
    AI can't press the buttons itself. A refused tap gets a fresh code so you can tap again;
  - the app that queued an item (a suggestion, a clean-up, a disagreement it flagged) gets no
    buttons for it on its own card and is refused (`own_item`), as in the chat. Session
    summaries keep their buttons: FlowState queues them itself when a session closes;
  - a tap is recorded as "Approve tapped on the review card in <app> (its one-use card code
    checked)", never as "the user".
- **Remove and Bring back on a card follow the delete rules.** A session summary record's
  Remove (or Bring back) on a card is refused for apps that may not delete (remote tokens, LAN
  keys, OAuth apps, the relay), and with approvals on it is only done on the review page or your
  phone. Keep still works everywhere.
- **`flowstate review-page` always gives you editable Settings.** When FlowState's read-only
  link page was already on port 8770, the command opened that page and stopped, so Settings
  could never be changed there. It now starts its own key-protected page on a free port. The
  "FlowState review" desktop shortcut runs the same command.
- **The two review pages no longer share a cookie.** The Settings page now lives under a secret
  address (`http://127.0.0.1:<port>/s/<secret>/`) and its cookie is only sent there, so the
  link page can't sign you out of Settings and another local web server (a dev server, say)
  never receives your Settings session. The link page has its own cookie.
- **The link page can't be used to approve what only you decide.** Any program on this computer
  can open the review link page, so with approvals on it no longer approves a delete or other
  user-only item, does Remove / Bring back, or turns review off; it points you to the page
  `flowstate review-page` opens or your terminal. Without approvals nothing changes.
- **Review page messages come from FlowState only.** A `?flash=` text in the address is no
  longer shown; each action's answer is kept on the page's side and shown once. A program
  visiting the link page over and over can't sign you out any more (the least recently used
  session goes first, and at most 10 new ones a minute are made).
- **Pause follows the key.** Pausing an app (`flowstate apps pause --app NAME` or the page
  button) now also pauses the key, token or sign-in it uses, so it can't come back under a new
  name ("Grok Bot 2"). `flowstate apps list` shows both names ("Grok Bot (via Desk key)").
- **Names of tokens, keys and OAuth apps are never reused.** `remote add-client`, `keys add` and
  `remote rename` refuse a name another token or app has, had before a rename, or had before it
  was revoked (case doesn't count), so a new token can never pass for an old app. An OAuth app
  signing up under a taken name gets a number ("Claude (2)"). New names are at most 50
  characters, so "(signed in as <name>)" is always recorded whole and the key keeps knowing its
  own records. `remote add-client --to-file` into a git folder now refuses before anything is
  made.
- **An app can't imitate a sign-in in its name.** "(via ...)" and "(signed in as ...)" are taken
  out of the names apps send, so "Grok Bot (via Laptop key)" on the shared token can't look like
  a key-signed app.
- **A damaged `config.json` is never wiped.** If the settings file exists but can't be read (a
  typo, or a moment while another program replaces it), settings changes (`flowstate name`,
  `update check`, `update self-update`, the Settings page, `offsite setup`) now refuse with a
  plain sentence instead of saving only their own setting, `flowstate update` treats it as
  "updated by hand", and the relay agent leaves the relay's copy of the name alone. Writers
  now use a lock and their own temporary file, so two at once can't fail or lose a change.
- **Session summary records:**
  - a Remove racing with Keep all (from the page, the phone, a card or the terminal, in any
    process) can no longer delete a record while the history says it was kept: Remove marks the
    record first, and Keep all / Don't keep wait for it ("try again in a moment");
  - Remove isn't offered on a record another app added to later (a fix attempt on this
    session's problem), since it would take that work with it; Keep still is;
  - only a record one of the session's own writes created can be removed: a link that was
    already there reads "already there", and a record the session deleted reads "deleted";
  - the session's own last update, saved a moment after its last call began, no longer shows
    as "changed since this session";
  - Keep all now records how it was decided (card, agent, review page, phone, terminal).
- **Smaller fixes:** the cards show dates in your own time zone; the terminal's `approvals phone`,
  `approvals popup` and `update check` changes are logged like the page's (and popup names are
  tidied the same way); `approvals popup add` asks for your `y` like `approvals off`; names with
  newer emoji are accepted (only control, format, surrogate and private-use characters are
  refused, the same on every Python and on the relay); "within a minute" for the name reaching
  your phone now says "normally".

## 2.4.3 - 2026-10-23

- No new dependencies. One additive database migration (023: three new tables for
  disagreement cards; a backup is taken first and existing rows are not changed).
  `flowstate update` from 2.4.0, 2.4.1 or 2.4.2 needs no new `.venv`.
- Disagreement cards. When two entries disagree, FlowState queues a review item (kind
  `disagreement`) the user settles by keeping the older entry, the newer one, or both, or by
  dismissing the card. The loser is marked superseded, not deleted. An agent flags a pair with
  `log_work(..., conflicts_with=<id>, topic="...", evidence=[...])` or
  `manage_links(action="flag_conflict", topic="...", item_id=..., target_id=..., evidence=[...])`.
  Evidence is at most two short snippets (`file:line`, `file:line — excerpt`, or `output: excerpt`).
  A new decision on the same component that the existing possible-conflict check already treats
  as a same-component hit also opens a card; unrelated decisions on that component do not. The
  review page shows claim A and claim B (stacked on a narrow screen, side by side on a wide
  one) with who wrote each, when, and the evidence. The tool reply includes that same card as
  plain text, plus the review link. Hosts that support MCP Apps (spec 2026-01-26), such as
  Claude Desktop, also get an in-chat card (`ui://flowstate/conflict-card.html`). They list one
  more tool, `show_conflict_card` (read-only), whose definition names the card; MCP Apps hosts
  draw a card for a tool, not for a result. Every answer that surfaces a waiting disagreement
  (log_work, flag_conflict, `manage_review` list and walk, the brief) gives the AI the call to
  make (`show_card`). The card follows the MCP Apps handshake, reports its size, follows the
  host's light or dark theme, and can show several cards at once. A card replaces the
  "possible conflict" review item on the same two decisions: a `conflicts_with` flag no
  longer queues both, and a same-component card closes the possible-conflict item (reason
  "merged into disagreement #N"), so the user decides once. Its buttons call `resolve_conflict_card`,
  which is hidden from the model (`visibility: ["app"]`), refused for apps that don't support
  MCP Apps, and phone only through the relay unless `flowstate relay chat-approvals on`. Approving through any model-visible
  tool still follows chat approvals (off by default: refused, with the review link). Every
  resolution is audited with its path (`card`, `review page`, `chat`, or `automatic` when a
  later `replaces=` or a dismissed possible-conflict closes the card). Card taps are not
  cryptographically proven to be a person yet; that check is a known follow-up.
- Queued deletes (opt-in approvals, 2.4.0) now show the exact rows the database will remove:
  type, name, project and key fields, and for a project or component the count and the named
  child records. That list is read from the database when the page or the tool reply is built.
  An agent-written sentence is shown only as a secondary line, "AI's note".
- Tests: tool calls in the test suite get a 120-second time budget
  (`FLOWSTATE_CALL_TIMEOUT`), so slow CI runners no longer fail with `flowstate_timeout`.
  Nothing changes for users.

## 2.4.2 - 2026-10-23

- Changed, please read: after updating, an AI connected through the relay (claude.ai, ChatGPT
  and other cloud AIs) can no longer approve or reject review items in the chat. It gives you
  the item's phone link instead. To allow it again, run `flowstate relay chat-approvals on` in
  your own terminal. Apps connected directly are not affected. Details below.
- No new dependencies and no database changes. `flowstate update` from 2.4.0 or 2.4.1 needs no
  new `.venv`, so it is not refused on Windows.
- Fixed: an answer that puts something in the review queue (log_work suggested or held by
  review mode, manage_memory import / cleanup / delete, manage_projects delete with approvals
  on, turning review off with approvals on, possible conflicts, shared-component alert todos,
  "did you say this?" checks) now says how the user decides it, the same way
  manage_review(action="walk") does: `how_to_decide`, the phone link (`phone`) when the relay
  link is known, the local page (`review_page`) for apps on this computer, and `dont_approve`,
  a plain line telling the AI not to approve it itself but to show the user the link. Before,
  through the relay the answer had no link, so an AI offered to approve the item in the chat.
- Changed: an AI connected through the relay can no longer approve or reject review items in
  the chat by default (`manage_review` approve / reject, and the older `review_approve` /
  `review_reject` names), even when it quotes the user. The answer is `error: phone_only`:
  nothing was decided, with the item's phone link (`phone`, `next`), `how_to_decide` and
  `dont_approve`, so the user decides it on their phone (or on the computer where FlowState
  runs). New setting `flowstate relay chat-approvals on|off` (off by default; stored with the
  other approval settings in the database; turning it on asks for `y` in your own terminal):
  on, relay AIs approve and reject as before when they quote you. Items that are yours alone
  stay locked either way (deletes and turning review off with approvals on, "did you say this
  yourself?" checks). Apps connected directly (stdio, your own network, your own tunnel) are
  not affected. There is no tool for the setting, so an AI can't turn it on through FlowState.
  `flowstate approvals` shows it too, and the `manage_review` description says so in one line.
- Tests: the setup-script shortcut test checks the exact `[shortcut] <path>: would be created
  (dry run)` line for the OS's real shortcut path (it failed on Linux, where the file is
  `flowstate-review.desktop`), and `tests/conftest.py` no longer crashes on stock Debian /
  Ubuntu Python, whose own `sitecustomize` has no docstring.
- Tests: the wheel install test failed on Windows (`No module named 'pywintypes'`). The test's
  temporary venv borrows mcp and its dependencies from the test environment, but did not read
  pywin32's own `.pth` file, which mcp needs on Windows. The test now repeats what that file
  adds. A real install was not affected: pip installs pywin32 with mcp on Windows the normal way.
- README: two phone screenshots in "Where to see what's waiting for you".

## 2.4.1 - 2026-10-23

- Fixed: installing with plain pip (`pip install mcp-server`, not `-e`, or from a built wheel)
  left out FlowState's database file (the base schema), so nothing worked: every call answered
  `flowstate_unavailable`. Installs from the repository (`setup_flowstate.py`, `pip install -e`)
  were not affected. The schema now ships inside the package (`mcp-server/flowstate/schema.sql`,
  the same as `database/schema.sql`); the migrations and `release_signers` already did.
- Fixed: the failed start also left an empty database file behind, which kept FlowState
  unavailable even after reinstalling. An empty database file is now set up as a new one.
- No new dependencies and no database changes. `flowstate update` from 2.4.0 needs no new
  `.venv`, so it is not refused on Windows.
- Tests: a new test builds a wheel, installs it (not editable) into a temporary venv away from
  the repository and checks that FlowState starts, saves and replaces a decision, reads the
  brief and migrates a new database.

## 2.4.0 - 2026-10-23

### In short
- More reliable and easier to trust; no new tools and no new required arguments. Out of the
  box FlowState still asks you nothing: an agent's delete goes through at once, after a safety
  copy, and undo brings all of it back. Opt in with `flowstate approvals on` and only you can
  approve an agent's deletes or turn review off: in the app's own popup, on your phone, or in
  your terminal.
- Writes say what they saved, and a write that timed out is no longer a mystery (or a
  duplicate when the agent tries again).
- See and decide what's waiting for you without a terminal: in the chat, from a link to the
  review page on your computer or the "FlowState review" shortcut on your desktop, (with the
  relay) on your phone, or (with your own tunnel) on a passphrase page at
  `https://<tunnel>/review/tunnel`. Nothing pops up unless you ask.
- Fewer review items: a session that saved nothing new (only reads, undo or review
  decisions) isn't queued, and the summaries that are say plainly what was saved.
- Four bugs fixed: timed-out writes, pre-restore backups pruned the same day, two windows of
  one app sharing a session, and undo that couldn't take back a new record or refused a
  whole change over one row. Then 14 more found by an end-to-end mock-project test (below).
- The first release that can update itself. A quiet daily check says when a newer release is
  out, and `flowstate update` (only when you ask) installs a signed release and rolls back on
  its own if anything fails. 2.4.0 itself is installed by hand by everyone (`git pull`, then
  `setup_flowstate.py --verify`); later releases can use `flowstate update`.

### Update check
- **A quiet daily update check.** Once a day, the first `start_here` starts one background
  check that asks GitHub for the newest FlowState release (`GET
  api.github.com/repos/dialectforge/FlowState/releases/latest`, 5 s timeout, no login, no
  `Authorization` header, `User-Agent: FlowState-update-check` without a version). The answer
  is saved in `update.json` in the FlowState data folder; the brief only reads that file, so it
  never waits for the network. When a newer release is out, one line goes on top of the brief:
  `FlowState 2.4.1 is out (this computer has 2.4.0). Tell the user once; ...`. Offline, a
  `403` / `429` or an odd answer is stored as the last error and tried again after 6 hours.
  Only exact `vX.Y.Z` tags of published, non-pre-release releases of this repository count.
- `flowstate update check` (show the last result), `flowstate update check on|off|now`.
- Local FlowState makes one network call: once a day it asks GitHub whether there is a newer
  release. It sends nothing about you or your projects (only what any web request reveals:
  your IP address). Turn it off with `flowstate update check off`, or with the environment
  `FLOWSTATE_UPDATE_CHECK=0` (CI, air-gapped machines). Off means no request at all.
  The README, AGENTS.md, llms.txt, the skill and setup's closing output say so.
- Tests: the check is off in every test run and in the e2e harness, and the test guard refuses
  any lookup of `api.github.com`.

### `flowstate update`
- **`flowstate update`: update to the newest release when you ask, and roll back on its own if
  anything fails.** `flowstate update --dry-run` checks everything and shows the plan without
  changing anything; `flowstate update` asks you to type `y` in your own terminal; an AI app runs
  `flowstate update --yes --user-said "<your words>"` only after you asked (your words and the
  app's name go into the edit history). It refuses, and changes nothing, unless the folder is a
  plain clone of `github.com/dialectforge/FlowState` with no local changes, at a release tag or
  on `main` with nothing of your own, with one worktree and a real `.venv` folder, and not opted
  out (`"self_update": false` in `config.json`, or a `.flowstate-no-self-update` file).
- **Signed releases only.** It downloads only the release's tag and checks its SSH signature
  against `mcp-server/flowstate/release_signers` read from the copy you already have, before
  anything changes. An unsigned tag, a key that isn't listed, an empty list ("no release signing
  key is set up yet"), a missing `ssh-keygen` or any check error: nothing is changed, and it says
  how to update by hand. `release_signers` lists one key, the FlowState release key
  (`flowstate-release`, `SHA256:3ZexAB20E6w1x7qWXivjtxhlqQ0RSBBuHNPilbiBzvE`); see `docs/RELEASING.md`.
- Then: a `pre-update` backup of the database (kept at least 7 days, like `pre-migration`),
  this folder's services stopped (Windows scheduled tasks, macOS LaunchAgents, Linux systemd
  user units that run from this folder; a leftover FlowState process from this `.venv` is
  stopped; AI apps are listed, not stopped), the code moved to the tag (a fresh `.venv` only when
  the release changes the Python packages; the old one is kept as `.venv.pre-update`), the
  database migrated, the services started and checked (the version, a stdio `start_here`, the
  HTTP server's `/health`, the relay tunnel). Any failure puts the exact old commit, branch and
  `.venv` back and starts the old services. The database is never put back or overwritten.
- `flowstate update --rollback`: back to the version before the last update.
- Known limit: on Windows a release that changes the Python packages is refused before anything
  changes (the command runs from the `.venv` it would have to replace); update that one by hand.
- An app still running the old code after an update says in its brief: `FlowState was updated
  to X; this app still runs Y. Ask the user to restart it.`
- The update-check line says "run flowstate update --dry-run" when the installed copy has
  that command and the computer isn't opted out; otherwise it gives the manual steps.
- README "Upgrading", AGENTS.md, the skill and the server instructions: update only when the
  user asks; dry run first.

### Fixed
- **Timed-out writes.** A write that hit the call timeout usually still saved a moment later,
  but the agent was told "may or may not have been saved", so it often logged it again. The
  timeout answer now says not to repeat it, the connection's next answer reports how it ended
  (`your earlier log_work from 14:14:05 UTC that timed out did save, as learning #31 (change
  3f9a1d2c4b5e). Don't log it again.`), and a repeat while it is still saving waits for it
  instead of saving a copy.
- **Pre-restore backups pruned the same day.** Retention kept only the newest backup per day,
  so any later backup that day removed the copy taken just before a restore. Copies labelled
  `pre-restore`, `pre-migration` or `pre-bulk` are now kept at least 7 days regardless of the
  14-daily / 8-weekly limits. Also: two backups in the same second could let the newer one
  remove itself; the copy just made is never pruned.
- **Two windows, one session.** Automatic sessions were keyed by app name + project, so two
  windows of the same app (two stdio processes, or two HTTP MCP sessions) shared one session
  and one summary. Each connection now gets its own session; the app keeps its name.
- **Undo.** Creating a record is now recorded in the edit history (with the links and conflict
  flags saved with it), so undo can take a new record back: only for the app that made it, and
  only while nothing else refers to it (another app's record, or one the user approved, needs
  a delete instead). Undo again brings it back with the same id. And
  when only some rows of a change were changed again since, undo now restores the rest and
  returns `skipped: [{record_type, record_id, why}]` instead of refusing all of it; it refuses
  only when nothing could be undone.

- **Found by the end-to-end test** (`tests/e2e/run_e2e.py`, now 15 scenarios, four agents):
  - Window A never heard about window B's work: "Since your last visit" now counts from this
    window's own previous brief, so other windows of the same app count too. A save in the
    same second as a brief is no longer reported again on the next one.
  - `manage_sessions current` answered with another window's session; now this connection's.
  - Data-git sync said `pushed: true` with the remote unreachable; now `success: false,
    pushed: false, offline: true` ("committed locally"). The first sync to a brand-new empty
    remote failed as a "conflict"; now it pushes. Only a real conflict is called one, and the
    rebase is undone so the data folder is left as it was.
  - An agent could approve its own review item with invented "user words": see "Approvals"
    below.
  - File search never found anything (also in 2.3): attached text files are now indexed when
    attached, older ones on the first search. No new dependency.
  - A claim outlived its app: when a session ends (quit, ended, idle) its claims are
    released, and the same app reconnecting may take over its old claim. Other apps still
    wait until it expires or is released.
  - Receipts listed nothing for rules, links, file attachments, imports, skills, sessions and
    the review mode; now every write names what it saved.
  - A repeat naming the project by number (or another case) saved a second copy; the
    project is now compared by its id.
  - A fully locked database could delay the answer past `FLOWSTATE_CALL_TIMEOUT` by up to
    5 s; the whole call, bookkeeping included, now answers by the timeout.
  - `manage_problems tree` with an unknown id answered `{}`; now `record_not_found`.
  - With `flowstate --db <file>`, attachments and data-git used the default folder; they now
    live next to that database. And the test suite's "never touch real data" guard now covers
    every path (see "Working on FlowState").
- **Windows.** Data-git (`manage_data_git` init / sync / history) timed out after 15 s in
  the stdio server: git inherited the MCP pipe as its stdin, and on Windows that blocks while
  the server is reading it. git (data-git, commit capture, off-site backup) now gets no stdin.
  `flowstate approve` / `reject` / `approvals off` run from an AI app's shell (a hidden console
  with no window) waited forever for a `y` nobody could type; they now refuse there, as with no
  terminal, and still work in a console window, Windows Terminal or VS Code's terminal.
  The relay agent's status file write was refused ("access denied") whenever something was
  reading it, so `flowstate relay disable` could leave the status at "connected"; it now
  waits a moment and writes. Relay keys, certificates and the device token were written in
  text mode (every newline byte became CR LF); they are now written byte for byte.
- **Saves now commit once.** A save wrote to the disk five or six times (the record, its
  search entry, its history and receipt, the session, the activity log), and on Windows each
  of those waits for the disk: a save took about 58 ms. Now everything one save writes goes
  to the disk together, in one go, or not at all: if any part fails, nothing of that save is
  kept and the answer says it failed. A save now takes about 15 ms on the same Windows PC.
  Deletes, approvals, undo and data-git still save step by step (a safety copy first, git, or
  undo's own checks). The database settings are unchanged.
- **Wording.** To-do priorities are shown as critical / urgent / normal / low in every answer
  (high and medium are still accepted). An empty database says "no projects yet".
  `manage_history who` shows each row's session. The variables and methods tools say delete is
  refused for HTTP app keys; `manage_skills` says which actions need a project. `log_work`
  details over 10,000 characters are refused with a clear message (`too_long`), never cut.

### Write receipts
- Every saved write's answer has `receipt: {"change": "3f9a1d2c4b5e", "saved": ["decision #31"]}`.
  The change id is also the edit id in `manage_history(action="list")`.
- The same write (same arguments) from the same connection within 2 minutes of a saved one
  returns `already_saved: true` with the first receipt and saves nothing. Different content,
  another window or app, or a later repeat is saved as usual. Undo (undo again = redo) and
  claim renewals are never treated as repeats.

### Approvals (opt-in; off by default)
- **Off (the default, also after upgrading):** FlowState never asks you anything. An agent's
  delete (`manage_projects` delete / delete_component, `manage_memory` delete, and the
  variables, methods and files deletes) goes through at once, after a verified safety copy, as
  one edit: `manage_history(action="undo")` brings back the record and everything removed with
  it (children, links) exactly. The answer says in one line what went and the undo call, and
  the brief shows `Deleted in the last 7 days: ... Undo: manage_history(...)` (deleted projects
  in the project list). Review-mode changes just happen (logged, shown in the brief).
  Suggestions and session summaries work as before 2.4: the app that queued one decides it only
  with your own words (not one word, not the item's own text).
- **On** (`flowstate approvals on`, or `--project P` for one project): an agent's delete and
  turning review off wait for your own click or key; no app can pass your OK along. Turning
  approvals on needs nothing; `flowstate approvals off` asks for your `y` in your terminal.
  FlowState tries, in order:
  1. **Popup in the app** (MCP elicitation, form mode, with the installed SDK's
     server-to-client request): `Claude Code wants to delete the todo "Print flyers" from
     Lakeside Bakery.` with one required checkbox, `approve` (a project also asks for its
     name). Only for apps on a list known to show a real dialog (Claude Code, Cursor, VS Code;
     `flowstate approvals popup add|remove NAME`), only when the connection declared it, never
     claude.ai / Claude Desktop / the Anthropic connector. FlowState waits at most 2 minutes,
     holding no database lock. Decline, close, unticked or no answer: nothing happens and the
     item stays queued. The popup is only as trustworthy as the app that shows it.
  2. **Your phone**, if the relay agent is set up: the answer has one link,
     `https://flowstatemcp.com/approve/<handle>/<item>?n=<code>`. flowstatemcp.com signs you in
     and sends your browser to your own FlowState at `https://<handle>.fsmcp.com/approve/<item>`
     with a 5-minute signed grant; your FlowState checks it with the site's public key pinned in
     this release, shows the item from your own database and decides on your tap. The site never
     sees the item. (`flowstate approvals phone off` turns links off.)
  3. **Your terminal:** `flowstate approve <id>` / `reject <id>` ask for `y` on the terminal
     itself, never piped input; with no terminal they refuse.
  The answer to the agent is one short line: the popup's result, else the link, else the
  command. Every decision is logged with how it was made (popup / phone / terminal / review
  page), the app and the time; `manage_history(action="list")` shows them under `approvals`.
- flowstatemcp.com's public keys (`fsa-1`, `fsa-2`) are pinned in `approvals.py`
  (`FSA_PUBLIC_KEYS`); a grant signed with any other key is refused.
- **Honest limit:** this stops agents approving their own work through FlowState. An agent that
  controls your own logged-in browser or your terminal window could still get through.
- The installed MCP SDK (1.30, protocol up to 2025-11-25) has no support for the 2026-07-28
  way of asking the user, so only the server-to-client request is used.

### See and decide what's waiting for you, without a terminal
- **In the chat:** ask your AI to show what's waiting; it calls `manage_review(action="walk")`
  (a new action on an existing tool; no new tool). In Claude Code, Cursor and VS Code (the
  approvals popup's list, when the app declared form dialogs) each waiting item comes up as a
  box: ticked = approve, unticked = reject, decline = skip, close = stop; at most 2 minutes per
  box, holding no database lock. Approvals off too, because you asked. Other apps, and
  read-only apps, get the list in plain words with the links below. Only your answer in the box
  decides; nothing the app passes counts.
- **No boxes through the relay or over the internet, and none that "answer" themselves.** On
  connections that came through the relay or the public listener the app's name is only its own
  say-so, so neither the walk nor the approvals popup shows a box there (FlowState marks every
  request with the listener it came in on). A box answered in under a second was not seen by a
  person: the walk stops, logs nothing for it and gives the plain list; the popup passes on its
  one-line fallback. (Before this, an app reaching FlowState through the relay with the name
  "Cursor" got 20 boxes, each declined at once, and the answer wrongly said you had decided
  them; nothing had been approved or rejected.)
- **The walk says what happened.** The plain answer says "No boxes were shown here; nothing was
  decided." After a real walk it gives the counts approved / rejected / skipped and says only
  those answers were logged.
- **Paging.** `manage_review` `list` and `walk` show 20 at a time, oldest first, with the total,
  which part is shown and the call for the next part (new `after` parameter: the page after
  that item id).
- **`manage_review` `list` is compact.** One line per item, like the walk's ("#id (project,
  kind): summary", cut to fit; with `status` other than pending the line also says approved /
  rejected / pending), plus the total, `showing` and `next_page`. A full page of 100 long items
  stays well under 16,000 characters, so it fits in one reply (before, every item came with its
  details and payload, and 100 of them didn't). `list` with `item_id=<id>` gives that one item
  with every field, any status; agents that approve with edits read it first. The review page,
  the phone list and `flowstate review` are unchanged.
- **A link to the review page:** when something waits, the brief and the walk's answer include
  `http://127.0.0.1:<port>/`. FlowState starts the review page inside its own process the first
  time a link is needed (stdio or HTTP), one per computer and database (another FlowState
  process reuses it), on 127.0.0.1 only, port 8770 or a free one if that is taken
  (`FLOWSTATE_REVIEW_PORT`; `off` turns it off). Clicking the link opens it at once; every
  change still needs the page's cookie, its CSRF token, a 127.0.0.1 Host and a same-origin
  Origin. `flowstate review-page` keeps its one-time key. The link is only given to apps on
  the computer where FlowState runs (apps it starts directly, or its LAN listener with a
  connection from that same computer). An app on another computer (the relay, the public
  listener, another computer on your network) would open its own 127.0.0.1, so it gets the
  phone link (relay users) or a note to ask for the list in the chat or use `flowstate
  review-page` on the computer where FlowState runs; the page isn't started for it. FlowState
  decides from the connection, never from a header.
- **Your phone (relay users):** a link you can bookmark, `https://flowstatemcp.com/review/<handle>`,
  in the brief and the walk's answer (relay enrolled, phone links on; this release pins the
  site's keys). No code in the chat: the site signs you in and sends your phone to your own
  FlowState's new `/review/start` with a 5-minute signed start ticket, which makes a fresh
  one-use code (15 minutes; at most 10 open, 30 an hour per site user, one per ticket) and
  sends it back to the site; you tap Continue and the site sends your phone
  to `/review` with a 5-minute signed grant (`action` `open_review`, `item` 0; same keys and
  checks as the approve page). The code is used up, and you get the list with Approve / Reject
  for 15 minutes (cookie HttpOnly, Secure, SameSite=Strict, path `/review`). Phone review off:
  `/review/start` shows a short page saying so, and points to the desktop shortcut and, for
  your own tunnel, the tunnel review page (the relay is one option). Contract:
  `docs/APPROVALS.md` section 5.
- **No lockout through `/review/start`.** It used to make a code for anyone who asked, 30 an
  hour in total, so anyone could use up the hour and lock you out of phone review.
  flowstatemcp.com now signs a start ticket for the signed-in owner (`action`
  `start_review`, `item` 0, your site user id, a fresh nonce, 5 minutes; checked like every
  grant, against this computer's own handle). Without a good ticket `/review/start` shows a
  plain page ("Open the "Waiting for you" link from flowstatemcp.com again"; an expired one:
  "This link expired after 5 minutes"), makes no code and counts nothing. Each ticket makes
  one code; the hourly limit is per site user, with a 120-an-hour backstop for everyone.
  The site must be updated first: an older FlowState ignores the ticket and works as before;
  this FlowState with an older site shows the plain page until the site is updated.
- **Your own tunnel: a review page.** The public listener (`--public-url`) now serves
  `/review/tunnel`: sign in with the owner passphrase (the consent page's, with its lockout,
  plus 10 tries per 15 minutes per address), then the same page as `flowstate review-page`,
  sized for a phone, deciding everything it can, including deletes and "Did you say this
  yourself?" items and turning review off while approvals are on. Session cookie
  `__Host-fs_review_tunnel` (Secure, HttpOnly, SameSite=Strict; 15 minutes idle, 1 hour at
  most; Sign out), a CSRF token on every form and same-origin checks; no scripts, no inline
  styles. Your AI apps' tokens and keys don't open it. Only on the public listener: the LAN and
  relay listeners answer 404. See `docs/REMOTE-ACCESS.md` section 4.
- **A desktop shortcut.** `setup_flowstate.py` puts "FlowState review" on your desktop
  (Windows `FlowState review.cmd`, macOS `FlowState review.command`, Linux
  `flowstate-review.desktop`), which runs `flowstate review-page` with your setup's Python and
  database. It never replaces a file of that name it didn't make; `--dry-run` only says what it
  would do. `flowstate review-page` now opens the page FlowState already has open for your
  database instead of failing on the busy port (another program on the port: any free one).
- **Plainer "waiting for you" lines.** When a delete or a review-off waits for you and there is
  no phone link, the agent now says: "Waiting for the user: ask them to open the FlowState
  review shortcut on their desktop and approve it there (or run `flowstate approve <id>` in a
  terminal)."; an app that came in through your tunnel names the tunnel page first. Apps on
  another computer are told "on the computer where FlowState runs (the FlowState review
  shortcut, or `flowstate review-page`), or on its tunnel review page if it has one".
- **The review page's buttons work in browsers.** The page (`flowstate review-page` and the
  127.0.0.1 link) sent `Referrer-Policy: no-referrer`, under which browsers send `Origin: null`
  on the page's own form posts, and the page refuses that, so every button was refused as
  cross-site (seen in Edge; the tests sent a proper Origin by hand). It now sends
  `Referrer-Policy: same-origin`, which still sends no referrer to other sites. The phone
  pages post with `fetch()` and were not affected.
- Decisions in the chat walk, on the phone list and on the tunnel page go through the same
  user-only path as the popup and are all logged with how they were made (`chat` / `phone` /
  `tunnel page`).
- Honest limits: a program on your own computer (an AI that runs commands or drives your browser)
  can open the 127.0.0.1 page too, as it could run `flowstate approve`. The phone list is
  protected by the site's sign-in and signing key: a leaked site signing key alone could open
  the list on a reachable computer (approving one item from its own link still needs that
  item's code from your computer). With your computer off or the relay agent down, the phone
  link can't open the list.

### Fewer session summaries to review
- **Housekeeping sessions aren't queued.** A session that saved nothing new (only reads,
  briefs and searches, undo, review decisions, a handoff pickup or todo claims) no longer puts
  a session summary in your review queue; one undo run used to leave a dozen. The session still
  closes and keeps a plain record (`[auto summary, housekeeping] ...`), which the brief's "Last
  session" line skips. A session with any other write (a learning, decision, change, problem,
  todo, link, rule, review-mode change, or anything FlowState doesn't know) is queued as before.
  Items already waiting are left as they are.
- **Plainer summaries.** `Cursor session in FlowState: saved 2 learnings and 1 todo.` instead
  of `Cursor, 3 calls over 12 min (closed after idle timeout). Logged: ...`. Why the session
  closed is still kept with the session.
- **"Keep this session summary?"** The phone list, the review page and the tunnel page ask
  that, with Keep / Don't keep, and say what each does (Keep saves it as the session's record;
  Don't keep marks it rejected and nothing is deleted). They show the text without the
  `[auto-captured, needs review]` tag; the chat list and popup say the same.
- A long session no longer loses its saves from its activity list to later reads, and a
  review-mode change is recorded as a write in the session's activity, not as a read.

### Clean-up questions you can judge at a glance
- **No unsure guesses.** A "misfiled" clean-up idea is only queued when FlowState is sure:
  the record clearly resembles the other project's records, and it names that project or one
  of its parts (a component), and doesn't name its own project. Similar words alone (push,
  pull, commit) are no longer enough. Before, `flowstate cleanup --queue` could ask about
  moves it marked "unsure", which nobody could judge.
- **Leftovers withdrawn.** Unsure move ideas still waiting from older versions are withdrawn
  when FlowState starts and on every clean-up run (rejected as "Withdrawn by FlowState: it
  wasn't sure enough to ask you."; never asked again). Only the review items change; no record
  moves. The clean-up answer and the CLI say how many ("withdrew N").
- **One plain question per item**, with the record's title, the projects and the reason:
  `Move learning "..." from FlowState to Atlas VPN? It mentions Atlas VPN's "VPN handler"
  component.`, `Cancel todo "..." in FlowState? It repeats todo "...".`, `Cancel todo "..." in
  FlowState? It has waited since 2026-01-05 (over 120 days).` The phone list asks the same
  ("Should FlowState move the learning ...?") instead of "Approve to keep it", and the review
  page shows it with one button ("Approve: move it" / "Approve: cancel it", plus the optional
  "Or move it to" choice) instead of edit boxes.
- **No duplicate ideas that do nothing.** Approving a duplicate learning or problem only noted
  it, so those are no longer suggested; duplicate todos still are (approving cancels the newer).

### Only you can turn an AI guess into "the user said so"
- A record's source tag (`user_stated`, `ai_inferred`, `from_code`) now stores who set it and
  when: the app, the model if the app supplied it, or `user` when you confirmed it yourself.
- An app may still tag what it is writing (`log_work(source="user_stated")` when it quotes
  you). But changing an existing record to `user_stated` is yours alone, approvals on or off:
  `manage_memory` `set_source` / `mark_checked` with `source="user_stated"` from an app leaves
  the tag as it is and queues one review item ("Did you say this yourself?"; a repeat from any
  app reuses it). You confirm it on the review page or with `flowstate approve <id>`; with
  approvals on, also in the app's popup or on your phone, as for deletes. No app can approve or
  reject it for you, whatever words it relays. Moving a tag away from `user_stated` is allowed
  and recorded.
- A `user_stated` write that repeats (same or nearly the same words, the clean-up duplicate
  check) another app's `ai_inferred` record of the same kind in the same project is saved as
  `ai_inferred`, with a note saying how to ask you.
- The brief shows `(user, via Cursor)` or `(user, confirmed by the user)`; `search` and
  `manage_history` results carry `source_tag`, `source_set_by` and `source_set_at`.
- Limit: a tag set at log time is the app's own say-so (shown with its name); FlowState can't
  check that you really said it. Tags set before 2.4.0 have no "who" and show as before.

### Safety copies and health check
- Right before a delete, or an approved clean-up or markdown edit, FlowState takes a verified
  `pre-bulk` copy (changes within a minute share one). If it can't, nothing is changed
  (`safety_copy_failed`). This does not depend on nightly backups; FlowState adds no daily or
  nightly copy of its own, so a user who said no to backups gets none.
- On start, `PRAGMA quick_check`. Only a problem (or a failed migration) is shown, as the first
  line of `start_here`, with the newest backup that passes its own check. Nothing is repaired
  automatically.

### Run ledger and "since your last visit"
- Per automatic session FlowState records its start, end, app and machine, and the ids of
  what it read and wrote (for a brief: every rule, handoff and record it showed; for other
  reads a hash of the answer). Never the text. Record only; there is no new tool. Pruned with
  the activity log (90 days by default).
- `start_here` shows at most one line about other sessions' work in the project since this
  app's previous brief there: `Since your last visit (Oct 19): Claude Desktop and Grok saved 3
  decisions, 5 changes, 2 todos.` After more than 14 days it is just counts plus a
  `manage_history(action="who", since=...)` call. Nothing on a first visit or when nothing
  changed; capped at 240 characters and inside the brief's size limits.

### Speed
- Measured through the MCP call path on a temp database with 2,000 records (450 saves):
  median save 8.3 ms in 2.3.0, 9.7 ms in 2.4.0; p95 10.2 ms and 11.8 ms. Re-measured after
  the end-to-end fixes on the same Mac (it ran a little slower that day): 8.9 ms for 2.3.0,
  10.5 ms before the fixes and 10.3 ms after; p95 12.7 ms. With approvals: 10.0 ms before and
  10.0-10.1 ms after (p95 12.3-12.4 ms), the same day on the same Mac.

### Database
- Migration 015 adds `session_runs` (session -> connection key, app, model, machine,
  identity source), 016 `run_writes` (one row per write: change id, connection, session,
  project, app, machine, tool, action, a hash of the arguments, saved / failed, record ids,
  error code, times), 017 `run_views` (one row per read in a session: tool, a hash of the
  answer, and for a brief the record ids it showed). New tables only; no existing table is
  changed, so a v2.3.0 database upgrades cleanly (tested with a database made by the v2.3.0
  code). A backup is taken first, as for every migration. 018 adds one nullable column,
  `run_views.upto_write` (the newest save a brief counted). 019 adds `approval_settings`,
  `approval_requests` (each phone link's one-use code), `approval_grants` (hashes of phone
  grants seen) and `approval_log` (every user-only decision). No row = approvals off.
  020 adds three nullable columns to `record_meta` (`source_set_by`, `source_set_model`,
  `source_set_at`: who set a source tag); older rows keep NULL. 021 adds `review_open_nonces`
  (the phone review list's one-use codes; `/review/start` makes them). 022 adds two nullable
  columns to it (`sub`: the site user whose start ticket made the code; `ticket`: a hash of
  that ticket's nonce, so a ticket makes one code) and an index; older rows keep NULL.
- Edit history rows for created records use the tool names `create/<type>` and
  `approve/<type>`. "Who did what" still lists each creation once.

## 2.3.0 - 2026-10-21

### In short
- Several agents at once: an agent can **claim** a todo for a while, so a second agent that
  starts at the same moment sees the claim and picks something else, instead of both quietly
  doing the same work. Claims run out by themselves; nothing is locked or hidden.
- Possible conflicting decisions: a decision that reads like a current one on the same project
  or component, logged without `replaces=`, is still saved but flagged, so the next agent
  doesn't silently get two answers. The user settles it in the review queue.

### Todo claims
- New actions on `manage_todos` (no new tool; the tool count is unchanged):
  - `claim` (`todo_id`, `minutes` 1-120, default 15): succeeds if the todo is unclaimed, its
    claim has expired, or you already hold it (then it renews). If another agent holds it,
    it is refused with the holder's name and when the claim expires, and suggests `next`.
  - `renew`: extends your own claim. `release`: drops it. `handoff`: drops it and leaves a
    handoff note (`where_i_was`, `next_step`, both optional) naming the todo, shown first in
    the next brief.
  - Setting a todo to done or cancelled clears its claim.
- Who "you" are is the app, the machine and the session (stdio: this server process; HTTP:
  the MCP session id), recorded the same way as every other write, so two sessions of one app
  are told apart.
- Nothing silently hidden: the `start_here` brief shows
  `#4 Fix login (claimed by cursor on mac-mini, expires in 9m)` (expired claims show nothing;
  the brief keeps its size limits); `next` lists your own claims first, skips other agents'
  and lists them at the end under `claimed_by_others`; `list` shows the claim on each row.
- No hard lock: anyone can still update, block or finish a claimed todo. The answer includes
  a `claim_warning` naming the holder. Claim, renew, release, handoff, override and cleared
  events show in `manage_history(action="who")`.
- Claiming is a write: read-only remote apps (read scope over HTTP or the relay) can't claim.
- The claim is decided by one conditional write inside one transaction, so when two agents
  claim at the same instant exactly one wins.
- `manage_todos` and `start_here` descriptions mention claims; to keep the tool list within
  its size budget, the other `manage_todos` parameter descriptions are shorter.

### Possible conflicting decisions
- FlowState has no language model, so it can't know that two decisions contradict each other.
  When a decision is logged without `replaces=`, it compares it with the project's current
  (not replaced) decisions, newest 2,000 at most: word overlap and word order, with the same
  word endings as search, plus search by meaning when the optional local model is installed.
  Decisions about the same component (or with none) need a similarity of 0.55; decisions about
  different components need 0.65. At most 3 per save. Tuned so that unrelated decisions on
  the same component are not flagged: it would rather miss a weak match than nag.
- The decision is always saved (the review mode applies exactly as before; a decision held for
  review is checked when the user approves it). The `log_work` answer adds `possible_conflicts`
  and a short note: `Possible conflict with D12 "Use Postgres for hosted storage" (same
  component). If D31 replaces it, link them: manage_links(...supersedes...). If they fit
  together, tell the user: only they can mark them compatible (manage_review).`
- The `start_here` brief shows one line, `Possible conflicts: D31 vs D12, D30 vs D7 (+2 more)`
  (open pairs, newest first, at most 3), and nothing when there are none.
- Each pair is stored once and waits in the review queue as an item of kind `conflict`.
  Approve = "they really conflict" (it stays in the brief until one decision replaces the
  other); reject = "compatible" (dismissed). As for every review item, only with the user's
  own words in `user_request`. The review page has the same buttons.
- A later decision that replaces either one (`log_work replaces=` or a `supersedes` link via
  `manage_links`) resolves the pair and closes its review item. Decisions logged with
  `replaces=` are not checked. Other record kinds are not checked.
- Flagged, resolved and dismissed pairs show in `manage_history(action="who")`.
- The check takes about 25 ms on a project with 2,000 decisions.
- `log_work`, `start_here` and `manage_review` descriptions mention it; a few `log_work`,
  `start_here` and `manage_review` parameter descriptions are shorter, to keep the core tool
  list within its size budget.

### Database
- Migration 013 adds two new tables, `todo_claims` (one row per claimed todo: holder, app,
  model, machine, identity source, session, claimed_at, expires_at, UTC) and
  `todo_claim_events` (the trail). The v1 `todos` table is not changed, so older servers can
  still open the database. A backup is taken first, as for every migration.
- Migration 014 adds one new table, `decision_conflicts` (new and older decision, score, same
  component, status open / resolved_replaced / dismissed_compatible, created_at, confirmed_at,
  resolved_at, the replacing decision, the review item, and who flagged and resolved it). No
  existing table is changed.

## 2.2.0 - 2026-10-21

### In short
- New, optional: the FlowState Relay. Reach the FlowState on your own computer from any AI app
  (claude.ai, ChatGPT, Cursor, your phone) at `https://<handle>.fsmcp.com/mcp`, without your own
  domain or tunnel. TLS passthrough: the encryption ends on your computer, so the relay can't
  read your traffic, and your data stays on your machine.
- To start: get a code at flowstatemcp.com/account, then run
  `flowstate relay enroll --code <code>` and `flowstate relay run`. Guide:
  https://flowstatemcp.com/setup.
- The relay is in a 10-spot beta. When it is full, `relay enroll` tells you your place in line.
- Local FlowState stays free and MIT forever. If you never enroll, nothing changes: same
  defaults, no account, no outbound connections, no new ports, nothing starts on its own.

### Optional relay agent (off by default)
- `flowstate relay enroll --code <code>` / `run` / `status` / `renew` / `enable` / `disable` /
  `forget` / `ct-check` / `ct-ack`: reach this computer's FlowState from claude.ai, ChatGPT or
  Cursor at `https://<handle>.fsmcp.com/mcp` without your own domain. TLS is
  end-to-end: the key is generated locally and never leaves, the certificate comes from Let's
  Encrypt via ACME DNS-01 run by the agent (`--staging` for testing), renewal 30 days before
  expiry, and the relay only splices encrypted bytes. Sign-in is the existing OAuth 2.1 + PKCE
  consent page, unchanged.
- Daily Certificate Transparency check (crt.sh): a certificate for your relay address with a
  key that isn't yours is logged as an error, shown by `flowstate relay status` and at the top
  of `start_here`.
- The relay lives on its own domain, `fsmcp.com` (`<handle>.fsmcp.com`, `api.fsmcp.com`,
  `tunnel.fsmcp.com`), instead of `relay.flowstatemcp.com`, so its DNS access can never touch
  the website or mail, and it has its own Let's Encrypt limit. All names come from one setting
  (`RELAY_DOMAIN` in `flowstate/relay/__init__.py`). Reserved names (`api`, `tunnel`, `www`,
  `mail`, `relay`, `status`, `admin`, `app`, `docs`, `help`) are refused as handles.
- When the relay is full, `relay enroll` says so in plain words with your place in line; the
  code is not used and nothing is saved. Beta full: "The FlowState Relay beta is full right
  now. You're on the waitlist (number N). Your code wasn't used, so try again later and you'll
  get in when it's your turn." 40 new computers this week: "FlowState Relay is full this week,
  you're on the waitlist (number N), try again after <time>". Places are first come, first
  served (the relay holds an opening for the oldest code for 48 hours). Other `429` answers
  say "busy, try again later".
- `FLOWSTATE_RELAY_DOMAIN` (relay developers only): every `flowstate relay` command now prints
  a warning when it points anywhere other than `fsmcp.com`.
- Wire protocol for the relay server: `docs/relay-protocol.md`.
- Nothing changes for local use: no account, no network calls, the relay code isn't loaded.

## 2.1.0 - 2026-10-18

### Working with your AI
- A smaller, consistent tool list: 12 tools instead of 35, and every name starts with a
  verb. Tools that do several things are `manage_<things>` and take an `action`:
  `start_here`, `log_work`, `search`, `manage_todos`, `manage_problems`, `manage_review`,
  `manage_handoffs`, `manage_rules`, `manage_links`, `manage_history`, `manage_projects`,
  `manage_memory`. The full old -> new mapping is in `docs/TOOL_CONSOLIDATION.md`.
- Nothing your apps do today breaks: every v2.0 tool name (`todo`, `link`, `handoff`,
  `review_approve`, `list_projects`, `log_learning`, `git`, ...) still works when called,
  with the same arguments and result, plus one extra line naming the new tool. The old
  names are just not listed any more.
- `search` now also answers a plain question (`answer=true`, what `ask` did) and, with no
  query, lists the newest records (`content_types`, `since_hours`), which replaces
  `get_learnings` and `get_recent_changes`.
- Every tool description now says what the tool is for, which tool to use instead, what it
  changes, which actions only read, and what it returns; every parameter is described.
- Read / write labels are per action now (`_meta["flowstate/actions"]`), and the tool-level
  labels are the most cautious action. Links, hard rules and handoff notes are no longer
  marked destructive (their removes keep the record).
- Profiles: `FLOWSTATE_TOOLS` unset lists the 12 tools; `core` lists 6 (start_here,
  log_work, search, manage_todos, manage_review, manage_handoffs); `full` (or `all`) adds
  `manage_sessions`, `manage_files`, `manage_data_git`, `manage_variables`,
  `manage_methods` and `manage_skills`. Before, unset listed all 35. Every tool name answers
  in every profile.
- The `tools/list` payload is smaller: 25,053 characters by default (was 39,221).
- The skill (`skills/flowstate`, version 2.3), AGENTS.md, README and llms.txt use the new
  names.

### Deletes, approved by you
- You can now delete a project, a component or a single record (decision, change, learning,
  todo, problem, attempt, handoff note, hard rule, link, setting, procedure, attachment). An
  AI can only ask: `manage_projects(action="delete" / "delete_component")` and
  `manage_memory(action="delete")` put a delete request in the review queue that shows what
  would go, including how many records go with a project or component. Nothing is removed
  until you approve it (`manage_review(action="approve")` with your words, or on the review
  page). Deleting a project also needs its exact name typed. Rejecting keeps everything.
- Before an approved delete runs, every row it removes or changes is copied into the edit
  history as one edit; `manage_history(action="undo")` puts it all back (a deleted project:
  undo with its old name and id). The delete is checked against that copy and rolled back if
  anything else would have gone. The copies are kept like the rest of the edit history (never
  pruned) and are in your backups, so a delete is recoverable, not an erasure.
- Only apps on this computer and the owner's LAN token can ask for or approve a delete;
  per-app keys, remote tokens, OAuth apps and read-only keys are refused.
- Changed: `manage_variables(action="delete")`, `manage_methods(action="delete")` and
  `manage_files(action="remove")` (and the older `variable`, `method`, `file` names) now make a
  delete request instead of deleting at once. Approving an attachment's delete keeps the file.
- Undo: undoing the undo of a delete is refused (that would delete again without you); a
  restored record whose parent was deleted after it is refused with a clear message (undo the
  parent first); a restore that clashes with a newer row (for example a new project with the
  same name) is refused instead of failing.
- No database migration: delete requests are review items, the copies are edit-history rows.
  Older v2.0 servers list a delete request but can't approve it (it stays pending); they can't
  undo a project delete (refused, nothing changed). Use v2.1 to undo deletes.

### Fixes
- Updating a component without giving its name failed, and updating it without a
  description erased the description. Omitted fields are now kept.

### Getting started
- AGENTS.md: setup snippets for VS Code (GitHub Copilot), Cline and Zed.

### For contributors
- CONTRIBUTING.md, and a GitHub Actions workflow that runs the tests on Windows, macOS and
  Linux with Python 3.11, 3.12 and 3.13.
- Package metadata: new description, project URLs, keywords and classifiers.

## 2.0.2 - 2026-10-17

### Working with your AI
- Shorter MCP tool and parameter descriptions. The `tools/list` payload is about 35% smaller
  for both the full list (35 tools) and the core list (9 tools). Every tool and every
  parameter still has a description. No change to tool behavior, names, or argument types.

## 2.0.1 - 2026-10-17

### Working with your AI
- Clearer tool and parameter descriptions. Every MCP tool says what it does, when to use a
  sibling tool instead, what a write changes and whether history can undo it, and what it
  returns. Every input parameter has a description (meaning, valid values, default, and which
  action uses it). No change to tool behavior, names, or argument types.

## 2.0.0 - 2026-10-17

FlowState 2 turns the v1.1 MCP server into a shared project memory that any AI app can use
safely, with you in charge of what gets saved. Existing v1.1 databases are upgraded in place
(backup first; new tables, plus new empty columns on FlowState's own v2 tables).

### Getting started
- `setup_flowstate.py`: one command (standard library only) creates the venv, sets up or
  upgrades the database, registers FlowState in Claude Desktop, Cursor, Windsurf, Claude Code,
  Codex CLI, Grok Build or any `mcpServers` JSON config (backing up each config first), and can
  schedule backups, install the skill and verify the server answers. `--dry-run` shows what it
  would change; re-running is safe.
- `AGENTS.md` and `llms.txt`: an AI can install FlowState by itself from this repo.
- An Agent Skill (`skills/flowstate`) that teaches any AI the FlowState routine.

### Working with your AI
- `start_here`: a short project brief (quick, standard or deep) as the first call; with no
  project it lists them, or matches the working folder.
- `log_work`: one tool for decisions, changes, problems, learnings and todos.
- Every write must name a project; writes to the wrong project are refused with a clear error.
- Sessions open and close automatically (after 2 hours idle or when the app quits), with an
  auto-captured summary.
- The server tells every app how to use it when it connects, and offers the `flowstate-start` prompt.
- If FlowState is slow or its database is unavailable, the app gets a short "carry on" error
  instead of hanging.
- Tools are labelled read-only or read-write; `--tools core` lists only the 9 core tools.

### You decide what is kept
- Review queue for auto-captured summaries and suggested records: approve, edit or reject.
- Review mode (`off`, `auto_only`, chosen types, `everything`), per install or per project;
  only changed when you ask, and every change is logged.
- Hard rules per project, always shown first in the brief.
- Handoff notes ("where I was / next step") for the next AI.
- Edit history with undo, superseded decisions (`replaces=`), and where each record came from
  (`source=`), with a list of facts not checked for 60 days.
- Local review page in the browser (`flowstate review-page`, 127.0.0.1 only).
- Per-app activity log, and a pause switch for each app's writes.

### Accountability
- Every recorded change (edit history, review queue items and decisions, links, handoffs, hard
  rules, review-mode changes, activity log) also records the app, the AI model, the machine or
  workspace, and how the app was identified (`app_key`, `oauth_client`, `remote_token`,
  `shared_token`, `self_reported`).
- The model is recorded only when the app supplies it (the tool's `model` argument, the
  `X-FlowState-Model` header, or `FLOWSTATE_MODEL` for stdio); it is never guessed.
- The machine: for stdio, `FLOWSTATE_WORKSPACE` or the computer's name; for a LAN key, the key's
  workspace or the `X-FlowState-Workspace` header; for remote apps, the client's name. Stdio
  identity is self-reported.
- Per-app keys for the local/LAN HTTP server: `flowstate keys add --name N [--read-only]
  [--workspace W]`, `keys list`, `keys revoke --id ID | --name N`. The key is shown once and
  only a hash is kept; read-only keys can only read; revoked keys get 401. The old shared LAN
  token still works and its changes are labelled "shared token (legacy)". LAN keys are not
  accepted on the public listener, and are refused for `git` and file attach.
- "Who did what" on the review page: filter by app, model, machine, project and time, 50 per
  page, with an Undo button (CSRF and origin protected) that uses the normal undo. AIs can read
  it with `history(action="who", ...)`.
- Migration `012_accountability`: additive, new NULL columns only on FlowState's own v2 tables;
  existing data is unchanged and older rows show "not recorded".

### Projects and tasks
- Cross-project links: link a project to a shared component of another; a change to it gives
  every linked project an update todo and a "changed since you last looked" note.
- Links to GitHub, Linear or other URLs from any record.
- Todo priorities critical / urgent / normal / low (older names still accepted), subtasks,
  blockers and "what's next".
- Clean-up suggestions for duplicates, stale items and misfiled records, as review items.

### Finding and moving things
- Keyword search reads all record tables directly (word endings match); optional search by
  meaning with a local model you install.
- `ask`: answers a question by quoting your own records, with their ids.
- Markdown export of a project, and import of your edits through the review queue.
- Import of Claude or ChatGPT memory exports into the review queue.
- Opt-in git capture: new commit subjects become changes on a project.

### Safety and backups
- Writes that look like they contain keys, tokens, passwords or card numbers are refused;
  `flowstate scan-secrets` lists older records that look suspicious.
- The HTTP token lives in the OS credential store (Windows Credential Manager, macOS Keychain,
  or a 0600 file on Linux), not in a plain file.
- Nightly, integrity-checked local backups (14 daily + 8 weekly kept) through the OS scheduler;
  restore always into a new, verified file unless you force it.
- Optional off-site copy to a private GitHub repo you own; refused if the repo isn't private.
- Numbered, additive database migrations with a backup first.

### Connecting
- HTTP transport with a bearer token, alongside stdio.
- Optional remote access for cloud AIs through your own Cloudflare Tunnel: OAuth 2.1 approved
  with your owner passphrase, per-app tokens (optionally read-only), revocation, rate limits,
  and no `git` or file access for remote apps. See `docs/REMOTE-ACCESS.md`.

### Removed
- The v1.1 desktop GUI was an unfinished experiment and has been removed. v2 is used through
  your AI and the browser review page.

### Known limits
- See "Honest limits" in `README.md`.

## 1.1.0

The previous release; see the `v1.1.0` tag.
