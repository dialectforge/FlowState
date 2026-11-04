# Changelog

## 2.4.8 - 2026-11-04

### Changed

- **Agents point you to the FlowState skill once.** In apps that support Agent Skills
  (Claude Code, Codex, Cursor and others), if the `flowstate` skill isn't loaded, the connect-time
  instructions tell the agent to mention once where to get it (`setup_flowstate.py --install-skill`
  or the GitHub folder). Web-only apps (claude.ai, ChatGPT) are never nagged.
- **Agents check FlowState's learnings when something breaks.** The Agent Skill (version 2.5)
  and the connect-time instructions now say: on an error, failing test, bug or when stuck,
  first `search` the project's learnings, problems and solutions and try what's recorded;
  after a non-obvious fix, log a `learning` (no secrets). AGENTS.md repeats the line.
- **The Agent Skill's author is now DialectForge** instead of a personal name.

### Tests

- The HTTP idle auto-close test no longer fails at random on slow CI runners.

## 2.4.7 - 2026-11-03

### Changed

- **The pages FlowState shows in a browser use the new FlowStateMCP look; no behavior changes.**
  The review page, the phone approve and review pages reached through FlowState Relay, the
  remote review sign-in and the remote access consent page now match flowstatemcp.com (light
  palette, larger tap targets, 16px fields on phones). Forms, links, routes and security headers
  are unchanged; a new test pins them to 2.4.6.

### Tests

- `test_display_name`: the wait for the fourth relay name message is longer (it timed out on
  slow Windows CI runners).

## 2.4.6 - 2026-11-02

### Fixed

- **Creating a method without a description no longer crashes.** `manage_methods` create
  without a name or description crashed with `KeyError: 'description'`. It now returns a clear
  `bad_request` that says what is missing, and nothing is saved.
- **Erasing a project now removes every row that refers to it.** `flowstate erase project:N`
  left rows for the erased project in the activity log (`activity_log`), the run ledger
  (`run_writes`, `run_views`) and the project's own approval and review settings
  (`approval_settings`, `review_settings`, `review_mode_log`). These rows are now found by
  column in every table and removed. Per-app settings, your own settings and sign-ins are not
  project data and stay.

### Tests

- New `tests/test_h11_project_erase.py` checks both fixes.

## 2.4.5 - 2026-10-31

### Fixed

- **A computer with a long random host name is recorded again.** Some cloud and CI machines have
  a name that looks like a token to the secret check, so the "machine" of a change was left
  empty. Such a name is now recorded as a short label, `computer-` plus 8 characters, the same on
  every run. A name that holds a real key format still records nothing.
- **Windows: settings writers no longer crash when several run at once.** While one writer's lock
  file was being removed, the next could get "Permission denied" on `config.json.lock`. That now
  counts as busy: it waits and tries again, then says another FlowState command is changing the
  settings.

### Tests and CI

- Three tests that could not pass on Windows now run there: the drive-without-permissions test
  checks what Windows really does (icacls fails on a drive without access lists), the `file://`
  test builds a proper Windows URL, and the stand-in for ssh is a Python script instead of a `sh`
  one.
- The CI test job lists the 25 slowest tests and stops after 150 minutes.

## 2.4.4 - 2026-10-30

### Upgrading to 2.4.4

Most setups need nothing. Check these after updating:

- **Approve / Reject popups now go only to apps on this computer.** An app this computer starts
  itself (the usual Claude Code, Cursor or VS Code setup) keeps its popups; nothing to do.
  - Claude Code or Cursor on this computer that connects to FlowState's LAN listener with its own
    key: run `flowstate approvals popup add --sign-in "<key name>"` once (the name
    `flowstate keys list` shows; it asks for your `y`), or tick the sign-in on the review page's
    Popup row. The name the app gives itself no longer counts.
  - Claude Code or Cursor on **another computer** on your home network no longer gets popups,
    even with `--sign-in`. Decide its items on the review page, your phone, or with
    `flowstate approve <id>` on the computer where FlowState runs. The shared LAN token never
    gets popups either.
- **In-chat cards for claude.ai through your tunnel or the relay** (or any app that signs in
  over the network): the first time such an app could show you a card, FlowState asks you once
  on your review page and your phone review page: "Allow tap cards in <app>?". Tap Allow there.
  (Or, as before, run `flowstate approvals cards add --sign-in "<name>"`, with the OAuth app's
  or token's own name; `flowstate remote clients` lists them.) The built-in `claude-ai` entry
  covers Claude Desktop on this computer only.
- **Data-git: update every computer to 2.4.4 first**, then run `flowstate data-git migrate` once
  on each. A computer still on 2.4.3 that pulls a migrated repository can lose its database.
  From 2.4.4 data-git shares the markdown exports, not the database, and when two computers
  wrote the same export file, this computer's own copy wins at its sync.
- **"Did you say this?" checks** (an app asking to mark a record as something you said) are
  decided only on the review page, your phone or with `flowstate approve <id>`, never in a
  popup, a review-walk box or an in-chat card.
- **Erase and review items:** `flowstate erase` removes every quote of the erased record from
  review items about it (made from it, naming it, or its disagreement card). Other review items
  lose only a whole sentence quoted from it (5 words and 30 characters or more), never a short
  shared phrase such as a component's name.
- **2.4.4 needs `mcp` 1.30 or later** (two security fixes), so it comes with a new `.venv`. On
  macOS and Linux `flowstate update` makes it (quit your AI apps first). On Windows
  `flowstate update` refuses a release that changes the Python packages. Quit your AI apps, then
  in the FlowState folder run `git fetch --tags`, `git checkout v2.4.4` and
  `python setup_flowstate.py` with the options you set it up with. Restart FlowState's HTTP and
  relay services if you run them, and reopen your AI apps (README, "Upgrading", "By hand").
- **Remote access: update cloudflared to 2026.10.2 or later** (`cloudflared --version`). Older
  versions still trust compromised eMudhra certificates
  (https://blog.cloudflare.com/emudhra-certificate-compromise). `flowstate remote status` now
  mentions it if yours is older. FlowState doesn't ship or update cloudflared.

### Changes

- `flowstate update` now installs a new `.venv`'s packages after moving the code, so they come
  from the release's own `pyproject.toml`, not the installed copy's. Updates from 2.4.0 to 2.4.3
  still run their own older steps: `flowstate migrate` now refuses, and the update rolls back, if
  the `.venv` has an `mcp` older than 1.30 (task 87).
- `.venv.pre-update/` is ignored by git, so after an update that made a new `.venv`,
  `flowstate update --rollback` and the next update are no longer refused as "local changes". A
  release installed on `main` no longer counts as a local commit at the next update.
  Agents no longer run `flowstate update --yes` while their own app is running FlowState: they
  show the plan and you run `flowstate update` in your own terminal. The steps to update by hand
  work at a release tag (`git fetch --tags`, `git checkout v<version>`), not only on `main`
  (task 87).
- New guide, "What you can ask FlowState" ([docs/WHAT-TO-ASK.md](docs/WHAT-TO-ASK.md)): plain-word
  requests for each thing FlowState does and what happens; the README has a short version and
  its MCP tools table now lists the four card tools (task 86).
- One changed dependency: `mcp>=1.30,<2` (was `>=1.26`), for two security advisories (task 80,
  below), so `flowstate update` to 2.4.4 makes a new `.venv` (the old one is kept as
  `.venv.pre-update`; quit every AI app using FlowState first; on Windows update by hand, see
  "Upgrading to 2.4.4"). One small migration (024: an
  empty column on hard rules saying how the user set each one; existing data is not changed).
  The off-site encryption uses the `cryptography` package that already comes with `mcp`;
  FlowState now lists it as its `offsite` extra (and in requirements.txt).
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
- **Unsettled decisions in the brief.** A superseded decision already dropped out of "Recent
  decisions", but an open possible conflict (`Possible conflicts: D31 vs D12`) left both
  decisions there as plain current ones, and an AI skimming the list could follow the older one.
  While the pair is open, the newer decision now stays, tagged `(unsettled: may conflict with
  #12)`, and the older one's text is left out. In its place is one short pointer, `#12 in
  dispute with #31, text left out: search(query="D12")`; if the older one isn't recent enough
  for the list, the pointer goes on the newer line instead. An older decision in several open
  pairs gets one pointer. The check is a guess, so the old text stays one step away:
  `search(query="D12")` (or `"decision 12"`) now returns that decision in full. Keep both,
  dismissed, resolved or replaced pairs show exactly as before, the "Possible conflicts" line
  stays, and the tags fit inside the decisions section's usual room (the newer line's text is
  shortened a little to make space), so no other section loses lines.
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
  - a record you removed and that was then brought back with `manage_history` undo (instead of
    Bring back) no longer shows as "changed since this session" with no buttons: it is
    undecided again, with Keep and Remove;
  - Keep all now records how it was decided (card, agent, review page, phone, terminal).
- **Smaller fixes:** the cards show dates in your own time zone; the terminal's `approvals phone`,
  `approvals popup` and `update check` changes are logged like the page's (and popup names are
  tidied the same way); `approvals popup add` asks for your `y` like `approvals off`; names with
  newer emoji are accepted (only control, format, surrogate and private-use characters are
  refused, the same on every Python and on the relay); "within a minute" for the name reaching
  your phone now says "normally".


### Data security fixes (audit of 2.4.4, part 2)

- **Data-git can't send your data anywhere on its own.** An AI app on this computer could turn
  FlowState's data folder into a git repository, point it at any address and push everything:
  the database, every backup and the relay's private key and device token. Now:
  - setting a remote, syncing and cloning are your own terminal commands (`flowstate data-git
    remote URL`, `sync`, `clone`; each asks for your `y`). `manage_data_git` answers `user_only`
    for them; `status`, `history` and `init` still work;
  - only `README.md`, `projects/` (attachments) and `export/` are ever committed. `sync` writes a
    markdown export of every project into `export/`. The database and its -wal/-shm files,
    `backups/`, `relay/`, `config.json`, `remote-auth.db`, `update.json` and anything that looks
    like a key, token or log never are, wherever they sit, and an older repository that tracked
    the database stops tracking it at the next sync (older commits still hold it);
  - a github.com remote must be private, checked before every push like the off-site backup;
    any other remote needs `--unchecked`, and a remote changed outside the command is refused;
  - the specialist tools (`manage_data_git`, `manage_files`, `manage_sessions`,
    `manage_variables`, `manage_methods`, `manage_skills`, and their older names `git`, `file`,
    ...) are refused outside the full profile (`not_in_profile`), not just left unlisted.
- **Only you set hard rules.** A rule any app adds (local, LAN, tunnel or relay) now waits in
  the review queue as a "hard rule" item that only your own click or key decides (review page,
  phone or `flowstate approve`); no app can approve it, approvals on or off. `flowstate rules add
  --project P "TEXT"` sets one at once. The brief says who set each rule: "(set by the user)",
  or, for one an older version let an app add, "(added by Grok Build, not confirmed by the
  user)". Waiting rules count toward the 25-rule / 2,000-character limits.
- **Only you remove hard rules too.** Before, an app could remove a rule by passing any words as
  "the user's request", or with `manage_memory` delete. Now `manage_rules` remove, from any app,
  only queues a "remove this hard rule?" item for your own click or key (review page, phone or
  `flowstate approve`), whatever words it passes; the rule keeps holding until you approve.
  New `flowstate rules remove <id>` (your own terminal, asks for `y`) removes one at once.
  `manage_memory` delete refuses `record_type=rule`. The chat review walk shows no Approve /
  Reject box for hard rules (adding or removing): they stay listed with where you decide them.
  A session summary now says "hard rule request" / "rule removal request" instead of "new
  rule" / "rule removed" for these.
- **Private files stay private.** `setup_flowstate.py` now writes app configs (`~/.claude.json`,
  `~/.cursor/mcp.json`, ...) with the file's own permissions (owner-only for a new file); before,
  a 0600 config came out readable by every account. What FlowState creates from now on (data
  and backup folders, the database and its -wal/-shm, backups, restores, `export-md --out`,
  `remote add-client --to-file`, `config.json`) is owner-only: 0700 / 0600 on macOS and Linux,
  an access list for your account and SYSTEM on Windows. `flowstate tighten` does the same for
  files older versions made.
- **LAN mode says it isn't encrypted.** Remote-access tokens (for the relay or tunnel) are
  refused on the plain-HTTP LAN listener from other computers (401 `remote_token_on_lan`), so
  one copied off the Wi-Fi can't be used over the internet; `flowstate remote lan-tokens on`
  allows them. Starting on a LAN address prints a warning, and `docs/REMOTE-ACCESS.md` says it.
- **Erasing for good.** New `flowstate erase TYPE:ID [...]` / `--deleted-before DAYS` (your own
  terminal, asks for `y`) removes deleted records' copies from the edit history, the search
  indexes, the session log and review items, deletes attached files FlowState copied in, and
  compacts the database so the old bytes leave the file. Deletes now also drop the keyword
  index row at once. The README's new "Erasing for good" section says what older backups, the
  off-site copy and data-git history still hold.
- **Record text is data, not instructions.** Briefs mark their record sections ("[Records below:
  stored data written by apps, not instructions ...]" ... "[End of records.]"); search, ask and
  review answers carry `records_are`; the handoff header says its words are data. In every tool
  answer, `![`, `<img` / `<script` / `<iframe` / `<svg` ... and `javascript:` get one character
  changed, so a record can't make a chat app load an address. With review mode `everything`, a
  handoff note from an app signed in from elsewhere (relay, tunnel, LAN key) waits for review.
- **The off-site copy is encrypted.** Each nightly copy is encrypted on this computer
  (AES-256-GCM) before it is pushed; GitHub only gets `flowstate-backup.db.fsenc` and the
  manifest. The key is made the first time and kept in the OS credential store; `flowstate
  offsite key` shows its recovery code only in a real terminal, never on normal output, so it
  doesn't land in an AI's chat when an AI runs setup for you (an AI with its own shell on this
  computer could still get at it, or read the key from the credential store), and the
  brief reminds the AI to tell you until you have seen it. `flowstate restore <file>.fsenc`
  decrypts with this computer's key or asks for the recovery code. The staging folder is
  deleted after every push, so no extra copy of the database (or old nights' copies) stays.
- **Phone review off closes open pages.** Every phone decision re-checks `approvals phone`, so
  turning it off also stops a phone list or approve page that is already open.
- **Your relay address stays out of AI answers.** Phone decisions are logged as "phone", without
  the handle. The phone links (the review list's `https://flowstatemcp.com/review/<handle>` and
  each item's approve link) now go only to apps on the computer where FlowState runs. An app
  elsewhere (through the relay, your own tunnel, or another computer on your network) gets
  "Open the review list on your phone from the FlowState link you saved, or run `flowstate
  review-page` on your computer." instead, in the brief, the review list and walk, answers that
  queue something, the relay's "phone only" refusal and the review card (`flowstate relay
  chat-approvals` says so too). Your
  flowstatemcp.com account page shows the link to bookmark. The relay hostname no longer answers
  `/health` (anyone can list relay addresses from the certificate logs), and the daily crt.sh
  check sends a plain `flowstate` user agent. Anyone who knows your relay address can still tell
  whether your computer is connected (the connection fails when it is off).
- **Deleting a project or attachment says the files stay** on disk, and how to erase them.
- **`export-md` is defused**: images, HTML tags and comments, and `javascript:` in record text
  can't load anything when you open the export in a markdown viewer; importing an unchanged
  export still sees no edits.
- **Stronger passphrase hashing.** New and changed owner passphrases use scrypt N=2^17 (was
  2^14); an older hash keeps working and is upgraded at the next successful sign-in.

### Fixes after the re-audit (audit of 2.4.4, part 3)

Review cards
- An app that connects over the network (a LAN key, a remote token, or an OAuth app through your
  tunnel or the relay) now gets the in-chat cards only if you added that key's, token's or OAuth
  app's own name to the card list (`flowstate approvals cards add --sign-in "<name>"`). The name the app
  gives itself no longer counts, so an app can't get the cards by calling itself "claude-ai". The
  built-in `claude-ai` entry covers Claude Desktop on this computer only, and the shared LAN
  token never gets cards.
- A card decision is recorded the same way everywhere: "card (claude-ai)", plus "signed in as
  <key>" for an app that connects over the network.
- Card answers get the same protection as every other answer, in the text and in the data the
  card itself shows: stored text can't show live images or links to the AI, and it is marked as
  data, not instructions.
- A chat where you only tap cards no longer leaves a new session summary to review.
- If a card tap fails or times out, the card's buttons keep working. A card left open too long
  says it has expired and asks you to show it again. An AI showing cards over and over can no
  longer stop other cards' buttons from working.
- The cards' plain-text version shows this computer's local date and time instead of UTC.

Erase and session summaries
- Erasing a record no longer changes other review items. Before, it replaced dates, status words
  and other short values in unrelated items with "[erased]" and joined their lines. Now it only
  removes the erased record's own text, and leaves the rest of each item exactly as it was.
- `flowstate erase` also erases records an AI took back with undo, and removes a deleted
  decision's text from the session log. Before, it said "no delete of it is on record" for
  undone records, and decisions stayed in the log.
- Removing one record of a session summary is saved together with the decision: if saving the
  decision fails, nothing is removed. A Remove that takes a long time (a big safety copy) keeps
  the summary on hold until it finishes.
- Handoff notes and links a session left can be removed from its summary. Before, they showed as
  "already there".
- Remove is no longer offered when FlowState can't be sure a record is only that session's work:
  another app added to it, the check failed, or the session's write log is older than 90 days.
  Those records can still be kept.
- A session summary's short line counts only records still there. Before, it said "saved 2
  learnings" when one had been undone.

Off-site copy, backups and file permissions
- Backups and new databases work again on USB sticks, exFAT drives and network shares that don't
  support file permissions (broken since 2.4.3). FlowState says once that files there may be
  readable by other users of this computer.
- If the Keychain (or Credential Manager) can't be read, for example because it's locked or you
  denied the prompt, the off-site push stops with a message. Before, FlowState made a new key
  over your old one, and the old key is the only way to read your earlier copies. An existing
  key is never overwritten now.
- The "save your recovery code" reminder is tied to the key itself. If the key ever changes, the
  next push says so loudly and the reminder comes back.
- The off-site staging folder (which could hold an unencrypted copy left by 2.4.3) is deleted on
  every push attempt, even a failed one, and by `flowstate offsite setup` and `offsite disable`.
  On Windows its read-only files are deleted too, and FlowState checks the folder is really gone.
- The small unencrypted note pushed next to the encrypted copy holds only its date, size and
  checksum. Before, it also listed how many records each table had.
- Encrypted copies record, in a way that can't be changed, when they were made, and `flowstate
  restore` shows that time. A copy that has been cut short is refused. A push no longer
  overwrites a newer copy on GitHub (from another computer, or after a wrong clock) unless you
  add `--replace-newer`. A damaged off-site key on this computer gives a plain message instead of
  an error dump.
- After upgrading from 2.4.3, FlowState makes its database, backups and folders owner-only by
  itself when it starts and at each backup (macOS and Linux). You no longer need to run
  `flowstate tighten`, which now also covers restored copies and a backups folder you chose
  yourself.
- `flowstate remote add-client --to-file` refuses a symbolic link. Before, it could write the
  token, readable by everyone, wherever the link pointed.
- Windows: FlowState's owner-only access lists name your account by its ID, and setup keeps an
  app config file's own access list when it rewrites the file.
- FlowState now lists `cryptography`, which off-site encryption needs, as its `offsite` extra
  and in requirements.txt (it still comes with `mcp`, so it adds nothing to install).

Data-git and stored text
- Data-git: if you sync from several computers, update every one of them to 2.4.4 before
  syncing. Then run `flowstate data-git migrate` once on each computer. It takes the database out
  of git without touching the files on disk, after a safety copy. Until then, `flowstate data-git
  sync` refuses a repository that still holds the database, because removing it would make git
  delete the database on a computer still running 2.4.3.
- Data-git sync checks what the remote would bring in before merging it. It refuses a database,
  settings or keys, links, and files that would replace files git doesn't track on this
  computer. It makes a safety copy of the database before it brings in other computers' changes.
- Data-git never writes through a link someone pushed. The export folder and files are readable
  by your account only.
- `flowstate data-git init` keeps an existing README.md and .gitignore. File attachments can't be
  written outside FlowState's projects folder, whatever the project's name.
- More HTML and link tricks in stored text are made harmless in answers and in the markdown
  export, including the export's header and Links section. Text that imitates the brief's
  "records below" markers can no longer fake the end of them. History answers say records are
  data. An edited markdown import keeps your original text where you didn't change it.
- Disagreements an AI flags are marked "unsettled" in the brief, like FlowState's own possible
  conflicts. The brief's "Two AIs disagree" line names both records and sits inside the records
  section.

Remote access, names and pausing
- Wrong passphrases sent all at once no longer get past the 5-try limit: FlowState counts each
  try before checking it and checks one at a time, so a burst can't use up the computer's memory.
- Key, token and OAuth app names share one list, and names that only look different (capital
  letters, invisible characters, Cyrillic or Greek lookalike letters, full-width letters) count
  as the same name. Names FlowState uses itself, like "unknown", "phone" or "user", can't be
  taken. Existing duplicates keep working.
- An app can no longer pretend to be signed in through another key by writing "(via …)" with
  lookalike brackets or letters, and can't call itself "user" or "phone".
- If a remote token is sent over the plain-HTTP network listener from another computer,
  FlowState still refuses it and now warns you, in `flowstate remote clients` and in the brief,
  that it could have been read; revoke it, or keep it with `flowstate remote keep-token --client
  <id>`.
- If two apps share one key and you resume one of them, the key stays paused while the other is
  still paused, so that app can't get back in by changing its name.

Review pages, settings and rules
- The review link page can no longer resume a paused app (use `flowstate apps resume` or the page
  `flowstate review-page` opens). With approvals on, it can't turn review off in any way: not by
  clearing a project's own setting, and not install-wide when only one project has approvals on.
  AI apps follow the same rule.
- A cookie left by an older review page no longer locks you out of Settings. Other programs
  visiting the link page can no longer sign you out of a tab you used in the last 30 minutes.
- A damaged settings file keeps the daily update check off instead of turning it on. `flowstate
  remote lan-tokens on|off` says plainly when the settings file can't be read, and its changes
  are logged like other settings.
- A FlowState command that crashed while changing settings no longer blocks the next one for 30
  seconds.
- `flowstate rules list` says who set each rule. New `flowstate rules confirm ID` makes a rule an
  app added in an older version yours. Rules waiting for your OK no longer use up the project's
  rule space; at most 20 can wait per project, 10 from one app.
- Searching "D12" shows that decision in full, and says "No decision D12" when there isn't one.
- `flowstate review-page --no-browser` shows its working one-time link on your terminal.
- A hand-typed "Main FlowState" (any capitalization) counts as the default name and is never sent
  to the relay.
- `flowstate update --rollback` to a version before 2.4.4 reminds you to set your remote-access
  passphrase again (`flowstate remote passphrase`): 2.4.4 stores it in a stronger form older
  versions can't check.

### Fixes after the second re-check (audit of 2.4.4, part 4)

Approve / Reject boxes
- An app with a LAN key, LAN token or LAN sign-in can no longer answer its own Approve / Reject
  boxes by calling itself "claude-code". Boxes go only to apps on this computer: one this
  computer starts (stdio, matched by the name it sends, as before), or one on this computer that
  uses the LAN listener and whose key's or token's own name you added with `flowstate approvals
  popup add --sign-in "<name>"` (or the tick on the review page's Popup row). Apps on other
  computers, the shared LAN token, the relay and the tunnel never get boxes. The log says which
  sign-in answered: "chat (claude-code, signed in as Laptop key)".
- The popup and card lists now keep app names and sign-in names apart, so adding the app name
  `claude-ai` never trusts a network sign-in that happens to be called `claude-ai`. An OAuth app
  that registers again and is named "Claude (2)" keeps the place "Claude" had.
- "Did you say this?" items get no box in the review walk, like hard rules: you confirm them on
  the review page, your phone or with `flowstate approve <id>`.
- A card tap whose save fails no longer uses up the card's code.

Data-git
- `flowstate data-git migrate` works on the second computer (it failed with "use -f to force
  removal" when another computer had pushed first), and its commit adds FlowState's
  `.gitignore`, so a computer still on 2.4.3 can't commit its database again.
- From 2.4.4 data-git shares the markdown exports, not the database. With two 2.4.4 computers,
  an export file both wrote no longer stops every sync with a clash: this computer's copy wins
  (it is made again from this computer's database at each sync), and exports of projects this
  computer doesn't have are left alone. The README says so.
- A data folder that is itself a link works with data-git. `.gitattributes` and `.gitmodules`
  are never committed or pulled. `data-git clone` refuses a repository with links before it
  writes anything, and the clone is owner-only at once. An attachment is never written through
  a link. The export's "run this to import" line names the project by its number, so no
  AI-written name lands in a shell command.

Your terminal
- Nothing an app wrote (a hard rule, a review item, a project, app or key name) can send escape
  codes to your terminal any more: control and invisible characters show as visible escapes
  (`\x1b`), so you type `y` to exactly what is stored. A new hard rule can't contain them.
  Joined emoji (a family, a rainbow flag) and Persian or Hindi words print as they are, and a
  rule may hold them.
- The per-app limit on waiting hard rules counts the app's sign-in, not the name it sends.

Erase
- Erase also takes the text of earlier versions of a record (a renamed todo's first title), its
  evidence snippets, a todo's claim history and the topic of a disagreement card about it. A
  delete now takes a decision's evidence with it (undo brings it back).
- Erase no longer changes review items that only share a short phrase with the erased record
  (a component called "Authentication"); a whole sentence quoted from it still goes.
- A short title like "fix" no longer blanks another record's session-log line. A record deleted
  together with its project can be erased (the project's delete can still be undone). A hard
  rule you removed can be erased: `flowstate erase rule:ID` (an active rule points you to
  `flowstate rules remove`). Its summary says the rule's text was blanked. `--deleted-before 0`
  includes deletes made the same second, a huge number no longer crashes, and composed /
  decomposed accents match.

Files and the off-site copy
- `flowstate backup --dir DIR` makes the folder you chose owner-only like the default one, and
  `flowstate tighten --dir DIR` does it on request. An empty folder or a `flowstate-*` folder
  doesn't count as FlowState's, and `._` files from exFAT / SMB drives don't stop it.
- On a drive where chmod is accepted but changes nothing, FlowState warns instead of saying it
  made files owner-only at every start. The warning is once per drive; a Windows access-list
  failure (USB drive) warns too.
- `flowstate tighten` lists a file as "owner-only now" only when it worked. Otherwise it prints
  `NOT changed: <file>: <why>` and exits 1. On Windows the reason is now the real one: a file
  owned by another account (named, such as Administrators: run it from an administrator window
  or `takeown /f` the file first), a FAT / exFAT drive, or icacls's own message, instead of
  always "this drive doesn't support file permissions".
- An empty off-site key file or Keychain item now says so and what to do, instead of "try
  again"; it is never replaced. Two first-time pushes at the same moment make one key (Windows).
  `flowstate offsite setup` exits 1 when the key can't be read, and `restore` no longer says
  "nothing was pushed".
- Setup no longer crashes on a drive without file permissions when the `--config` file exists.

Remote access and names
- A request through a proxy on this computer no longer counts as coming from this computer
  (FlowState sees the proxy's forwarding headers; set `FLOWSTATE_LAN_PROXY=1` for a proxy that
  adds none). A remote token on it counts as sent over the LAN.
- Old "phone (<handle>)" labels are hidden from apps elsewhere also after you turn the relay off
  or enrol again under a new handle.
- More lookalike letters (Armenian, Cherokee, Lisu, Coptic) and angle brackets in "(via ...)"
  are caught.
- An OAuth registration you never approved no longer holds a name such as "Cursor", so your own
  `flowstate keys add --name Cursor` works; the app gets a number if you approve it later.
- `flowstate remote keep-token` asks for your `y` in a terminal, and the brief's warning no
  longer gives an AI the command.

Brief and defusing
- The project's description, linked-project alerts and the last session's summary now sit
  inside the brief's "records" section, and links hidden with entities encoded twice are broken
  too.

### In-chat cards in Claude Desktop again, and through the relay (task 65)

- Claude Desktop shows the tap-to-decide review and disagreement cards again. Since the card
  safety checks earlier in 2.4.4, only apps on the card list got cards, and the list only had
  `claude-ai`, the name Claude Desktop's chat sends. Claude Desktop's Cowork (agent) mode sends
  a different name, `local-agent-mode-` plus the server's name (`local-agent-mode-flowstate`),
  so it got only the text. The card list now starts with `local-agent-mode-*` too: a name
  ending in `*` matches every app name that starts with it, for apps this computer starts
  only, never for a key, token or sign-in. Remove it with
  `flowstate approvals cards remove "local-agent-mode-*"` if you don't want cards there.
- The cards now work through the relay too (the FlowStateMCP connector in Claude Desktop or
  claude.ai). As before, a connector gets them only after you add its sign-in's own name:
  `flowstate approvals cards add --sign-in "<name>"` on your computer (it asks for your `y`),
  or the tick on the review page's Settings. When an app could show cards but you haven't
  turned them on, the AI's answer now gives you that exact command (`cards_off`) instead of
  saying nothing. Once on, your tap goes through the relay to your own FlowState, which checks
  the card's one-use code (15 minutes, once, for that card and that sign-in); the relay only
  carries the encrypted bytes and can't see or keep it. A card tap now counts through the relay
  the same as on your computer or your own tunnel. Before, it was refused unless chat
  approvals were on. The AI's own approve / reject in the chat still follows that setting (off
  by default: phone only), and items only you decide (deletes and turning review off with
  approvals on, "did you say this?" checks) still have no buttons on a card.
- Apps without card support still get the text card and your phone or review-page link.
- **Cards are offered, not set up by hand.** The first time an app that signs in over the
  network (the FlowStateMCP connector through the relay, claude.ai through your tunnel, a LAN
  key) could show you a card but isn't on your card list, it gets the text card as before, and
  one item waits in your review queue: "Allow tap cards in <app>?", with the app's name and the
  sign-in it used.
  - Only you answer it, and only on the review page or your phone review page. No AI can
    approve or reject it, with chat approvals on or off; nor can a card tap, a popup, the chat
    walk or `flowstate approve`.
  - **Allow** puts that sign-in's own name on the card list (the same list as
    `flowstate approvals cards add --sign-in` and Settings), never the name the app gives
    itself. Its next answer shows the card, and your taps count.
  - **No** keeps the text. Each sign-in is asked once: not again while it waits, after a No,
    or after you take the cards away again (Settings, or
    `flowstate approvals cards remove --sign-in "<name>"`).
  - To ask one again: Settings ("Offer the cards to apps on a sign-in", Offer again) or
    `flowstate approvals cards offer-again "<name>"`. To stop offers altogether:
    `flowstate approvals cards offers off`, or the same row in Settings.
  - Every answer is logged with your other approvals, and the change with your other settings
    changes.

### Evidence, source tags and standings are in the edit history (task 67)

- **Earlier evidence is no longer lost.** Saving new evidence for a record (`log_work`
  `evidence`, `flag_conflict` `evidence` / `target_evidence`) replaced the old snippets without
  keeping them. Every evidence change is now in the edit history, with the old and new
  snippets and the app that made it. `manage_history(action="list", record_type=...,
  record_id=...)` shows it (as `evidence`), and `manage_history(action="undo", ...)` on the
  record brings the old evidence back. Saving the same evidence again changes nothing.
- **Undoing your own new record works when it has evidence.** Undo of a record created with
  evidence was refused ("entry_evidence refers to it"); the evidence now goes with it.
- **Source tags and checked dates keep their history too.** `set_source` and `mark_checked`
  overwrote the earlier tag and date. Both are now in the edit history, and undo puts the
  earlier tag back. Undo never sets or takes back `user_stated`: that stays the user's own
  decision.
- **A second disagreement card on the same entry keeps the first outcome in history.** The
  entry's "superseded" mark from the first card was replaced without a trace. It is recorded
  now; undo leaves card outcomes alone (they are the user's).
- `flowstate erase` also removes these history copies of an erased record.
- Nothing in the database is rewritten: the history uses the existing `record_history` table.
  Changes made before 2.4.4 have no earlier version to show.

### Undo around card outcomes, and history for review-queue changes (task 68)

- **A card's outcome no longer blocks undo of older edits.** When an entry's newest history
  row was a disagreement card's "superseded" mark, undo on that entry was refused, so older
  ordinary edits (its evidence, its source tag) couldn't be undone either. Undo now steps
  over the card's mark to the newest ordinary edit and undoes that. The mark itself stays as
  it is: no app tool changes it.
- **An app can't take back its own new entry once a card was decided about it.** Undoing the
  create of the entry a card kept would have left the other one "superseded" by an entry that
  no longer exists. That undo is now refused (`user_only`).
- **Review-queue changes are in the history.** A possible conflict's status (dismissed as
  compatible, closed by a replacing decision or by a card, confirmed) and a review item's
  text before and after "approve with edits" are recorded, with the app and the time.
  `manage_history(action="list", project=...)` shows them (as `possible conflict` and
  `review item`). They record the user's decisions, so undo never changes them back.
- `flowstate erase` also removes a review item's earlier text from the history (and from the
  session log) when it erases the record that item became.
- **Fixed:** when undo was refused because a source tag or card mark changed again, the
  message could name the app of a different record with the same id (a todo #3 for decision
  #3). It now looks only at the same kind of record.
- Nothing in the database is rewritten; the history uses the existing `record_history` table.

### The user is asked whether to reopen possible conflicts after an undo (task 69)

- **A possible conflict no longer drops out of the brief silently.** When an app undoes its
  own decision that had replaced another (`log_work replaces=`), the possible conflicts that
  replacement had closed stay closed, because no app may change the review queue. FlowState
  now adds one review item for the user in that project ("Reopen the possible conflict between
  D12 "..." and D11 "..."?"), naming the decisions in plain words. The undo's answer says so.
- **One question per undo**, for all the pairs it closed. Retrying the undo, or undo / redo /
  undo, asks nothing new, and neither does a closing the user already answered.
- **Reopen** puts the pairs back in the brief and asks again whether they conflict (a new
  possible-conflict item, unless the user had already said they conflict). **Leave closed**
  changes nothing. If the replacement is back by then (redo), nothing is reopened and the
  answer says why. Both answers are in the edit history (with who and when) and in the
  approval log.
- **Only the user answers it:** on the review page (this computer's or the tunnel page), the
  phone pages, or by tapping its review card (since task 70 also the card of the app that did
  the undo).
  Every AI tool, the chat walk, a popup and the terminal are refused, and nothing changes.
- `flowstate erase` removes the question's text and history with the decisions it names.
- No migration and nothing rewritten: a new review-item kind (`reopen_conflict`) in the
  existing tables.
- Unchanged: an app may still take back its own new entry when the only card naming it was
  dismissed.

### Erase and cut quotes, the reopen question in the same chat, link removal (task 70)

- **`flowstate erase` no longer leaves the start of a long decision in a possible-conflict
  item.** Those items (since 2.3.0) quoted each decision cut to about 70 characters with
  "...". Erase now also removes a quote cut short with "..." or "…", and finds a
  possible-conflict item by the decisions it names (the pair's ids), not only by its text.
  This works on existing databases too: items an older version stored are cleaned the same
  way, and their edit history goes with them. An unrelated item that only shares a few
  opening words is left as it was.
- **New possible-conflict items quote each decision whole** and name both decisions by id,
  as the reopen question already did. Pages and lists still shorten long lines when they
  show them.
- **The "reopen?" question can be answered in the same chat.** FlowState itself now queues
  it (shown as "FlowState"; the details still say which app undid what), so the app that
  did the undo shows working buttons on its review card. A tap still needs the card's hidden
  one-use code: no code, a wrong code or a used one is refused.
- Still refused, with nothing changed: every AI tool (`manage_review` approve / reject, with
  chat approvals on or off, approve with edits), the chat walk, popups, an AI calling itself
  the review page, phone or tunnel page, the open review link page, and the terminal's
  `flowstate approve`.
- No app can be FlowState: "FlowState", "system" and lookalikes are reserved app names, and
  no key, token, header or tool argument gives FlowState's own identity. Only the reopen
  question is recorded with it.
- **Removing a supersedes link asks too.** `manage_links remove` of a link that had closed
  possible conflicts now queues the same single question as an undo (deduplicated the same
  way: retrying, or adding and removing the link again, asks nothing new).
- Unchanged: pairs closed and undone before this version get no question; an app may still
  take back its own new entry when the only card naming it was dismissed. No migration.

### Erase keeps other items' history, without the erased text (task 71)

- **`flowstate erase` no longer deletes the whole edit history of other items.** When it
  takes an erased record's text out of a review item (a possible-conflict item, a
  disagreement card's item, or an unrelated item that quoted it), that item's history rows
  now stay. Only the erased text is replaced by `[erased]` inside them, by the same rules as
  the item itself: whole quotes and quotes cut short with "..." or "…"; in an item that isn't
  about the record, only a distinctive sentence (never a short shared phrase). Every stored
  field of those rows is cleaned: the text before and after an "approve with edits", details,
  evidence snippets, titles and the payload (also when it was stored as escaped JSON).
- An item that came from the erased record (or asked to delete it) keeps its history rows,
  blanked whole, as the item itself is.
- **Also fixed:** text an "approve with edits" replaced stayed in that item's history when the
  item no longer showed it. Erase now looks through the history of every review item.
- Unchanged: the erased record's own history still goes entirely. History rows an older
  version wrote (fewer columns, a payload stored as an object, a row that isn't JSON) are
  cleaned the same way; rows without the text stay byte for byte. The history list, "who did
  what" and undo work as before. The erase report gives the number of history rows blanked.
  No migration.

### Delete approvals bound to the exact rows, expired claims, README on conflicts (task 72)

- **A delete approval covers exactly the rows the user was asked about.** With approvals on,
  a delete request now stores the exact list of rows it would remove, worked out from the
  database (the record and every record row that goes with it, by id and content, plus the
  rows that would only lose their link to it). When the user approves, FlowState works the
  list out again; if a row was added, removed or edited in the meantime, nothing is deleted,
  the request is closed, and the answer says what changed ("now also deletes change #9").
  Asking for the delete again makes a new request with the current list. Before, only the
  number of rows of each kind was compared, so one row swapped for another, or an edited row,
  went through.
- Asking again while an out-of-date request is still waiting closes the old one (rejected by
  "flowstate (automatic)", with the reason) and queues a new one. A delete request queued by
  an older version has no such list: approving it is refused (`request_outdated`) and it is
  closed; ask again. Delete requests are no longer answered from the "same write within 2
  minutes" memory, so asking again right after a request was closed gets a new one (an
  identical request that is still waiting is still answered with that one).
- Unchanged: the request's summary, details and an "approve with edits" never change what is
  deleted; a used or closed request can't be approved again; phone links and card codes stay
  one-use. Sessions and review items that go with a project or component are still not part
  of the list (they move on their own while the user decides).
- **An expired todo claim can't be renewed.** `manage_todos(action="renew")` used to extend a
  claim its holder had let run out, as long as nobody else had taken it. Now it answers
  `claim_expired` (holder) or `not_claimed` (anyone else); claim the todo again, which any
  agent can do once it has expired. Renewing before it expires works as before. Releasing an
  expired claim answers `released: false` (it was no longer held).
- README: new sections "When two decisions clash" (how a clash is flagged, where it shows, and
  that the user picks the winner) and "Who, which model, which machine" (what each change
  records).

### Project history on the review page (task 73)

- **New: Project history.** The local review page has a "Project history" view. Pick a project
  and see its whole path in time order: records created, edited, deleted and undone, decisions
  and what replaced them, possible conflicts and disagreement cards and how they were settled,
  review items and their outcome, todo claims (also a claim that ran out with nobody renewing
  or releasing it), and sessions. Each entry has the time, app, model (when the app gave it),
  machine and how the app signed in.
- Filters by type, app or machine, and date range; a search box that also finds a record by
  an older wording kept in its edit history; oldest or newest first; 50 entries a page.
- Tapping a record shows its fields now and its whole edit history, every changed field before
  and after, and every entry that names it.
- **Todos tab:** each todo of the project with who created it (app, model, machine, sign-in) and
  its status now; tapping one gives its full timeline (created, claimed, renewed, released,
  claim expired, edited, status changes, completed, reopened, cancelled, deleted, undone).
- Read-only: no form on these pages changes anything. Same session as the rest of the review
  page on this computer; not on the tunnel page, the phone pages or the relay, and no MCP tool.
- A record removed with `flowstate erase` shows as `[erased]`, and its text is in no entry,
  search result or record page. When the shown range reaches back past the activity log's 90
  days, the page says that older detail has faded.
- No migration. The page opens the database read-only (`query_only`).

### Clean-up suggestions bound to the exact rows (task 74)

- **Approving a clean-up suggestion changes exactly the records it was suggested with.** A
  clean-up item that moves a misfiled record, cancels a duplicate todo (the "merge") or
  cancels a stale one (the "archive") now stores the exact list of rows approving it would
  change, the same way a delete request does since task 72: the record (a move also lists
  its links; a duplicate also the todo it keeps), by id and content. Ids and hashes only, no
  record text.
- When the user approves, FlowState works the list out again inside the change itself. If a
  row on it was edited, or a row was added, is missing or was already removed (deleted,
  moved, done), nothing is changed, the suggestion is closed (rejected by "flowstate
  (automatic)", with the reason), and the answer `changed_since_request` says what differs
  ("todo #12 was edited", "now also lists link #4", "no longer lists todo #9"). Running the
  clean-up again suggests it anew with the current list. A suggestion queued by an older
  version has no list: approving it is refused (`request_outdated`) and closes it; running
  the clean-up again replaces it (`replaced` in the answer) with one that has its list.
  `manage_memory(action="cleanup")` is no longer answered from the "same write within 2
  minutes" memory, so running it again right after a suggestion was closed suggests it anew
  (a suggestion that is waiting and still matches is not queued twice).
- The review page and the phone page show that list under the question ("Approving changes
  exactly these records: ...") and, when it no longer matches, say so before the user taps:
  "This list is not the one the suggestion was made with (...). Approving it changes
  nothing; run the clean-up again for a new suggestion." A record that is gone (deleted or
  erased) is shown by its id only.
- Unchanged: a clean-up that changes nothing (a duplicate of two learnings, queued by an
  older version) is approved and noted as before; a suggestion the user rejected is still not
  suggested again; choosing another target project for a move is still allowed.
- Deletes: when the list changed because a row was edited, the message now names the row
  ("todo #1 was edited") instead of "a record on the list was edited".

### Project history and clean-up follow-ups (task 75)

- **Project history from the brief's link.** The page the brief's link opens has the
  Project history link at the top, next to "Who did what", as the page `flowstate
  review-page` opens does. From the review queue filtered to one project, the link opens that
  project's history. Still only on this computer: never on the tunnel page, the phone pages,
  an AI tool or the relay.
- **Newest first.** The history timeline now starts with the newest entries. "Oldest first"
  is still in the order menu (`order=oldest`).
- **Erased todos are not in the Todos tab.** An erased todo has nothing left to show, so the
  Todos tab leaves it out of its list and its counts. The timeline still shows `[erased]`
  where an entry names it.
- **A record page under the wrong project.** A record page whose address names a project the
  record doesn't belong to now sends the browser to the same page under the record's own
  project (a link: either of its two projects is fine; a deleted record: the project its edit
  history has). It never shows a record under another project. An erased record, or an id
  nothing knows, is shown as before.
- **The AI's review list says when a clean-up is out of date.** In `manage_review` list and
  walk lines, a waiting clean-up whose exact list no longer matches ends with "(out of date:
  approving changes nothing)", as the review page already warns. The walk's Approve / Reject
  box shows the review page's warning too.
- **`flowstate erase` removes a clean-up's stored list.** A clean-up suggestion about an
  erased record (it names the record, or its list has it, a link included) loses its stored
  list of ids, digest and per-row hashes, in the item and in every copy of it in the edit
  history. The item stays; approving it changes nothing and says "a record on its list was
  erased".

### A clean-up's exact list in the one-item view and the review card (task 76)

- **One item in full.** `manage_review` list with `item_id=<id>` for a waiting clean-up now
  also gives `would_change`: the review page's "Approving changes exactly these records: ..."
  text. When the list no longer matches, `out_of_date` has the review page's warning (approving
  it changes nothing). Erased records are never shown with text, only the records still there.
  Decided clean-ups and other kinds of item are shown as before.
- **The review card.** The in-chat review card (MCP Apps) for a waiting clean-up shows the same
  warning when its list is out of date, as the review page does. The card's plain text has it
  too.
- **A clean-up whose records are all gone** (for example its only todo was erased) now reads
  "Approving changes exactly these records: none (no record on its list is still there)."
  on the review page, the phone page and in these answers, instead of an empty list.

### A clean-up's exact list on the review card (task 77)

- **The review card.** The in-chat review card (MCP Apps) for a waiting clean-up now also shows
  the review page's "Approving changes exactly these records: ..." list (`would_change`), above
  the out-of-date warning when there is one. The card's plain text has the same line. Erased
  records are never shown, by text or by id; when none of its records is left the list reads
  "none (no record on its list is still there)". Decided clean-ups and other kinds of item are
  shown as before.

### OAuth issuer without a trailing slash (task 78)

- **One exact issuer.** Found by a security reviewer's scan: a relay address, an own tunnel
  (`--public-url`) and a local public-mode listener published their OAuth issuer with a
  trailing slash (`https://abc123.fsmcp.com/`), which strict issuer comparison (RFC 8414
  section 3.3) treats as a different issuer from the address. `issuer` in
  `/.well-known/oauth-authorization-server` and `authorization_servers` in the protected
  resource metadata (both `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-protected-resource/mcp`) are now the address exactly, without the slash,
  also when `--public-url` is given with one. The endpoint URLs (`/authorize`, `/token`,
  `/register`, `/revoke`) and `resource` (`<address>/mcp`) were already right and are
  unchanged. Apps already connected keep their sign-in: registrations and tokens are not tied
  to the issuer string. The LAN listener has no OAuth, as before.
- **Consent link uses the same address as the issuer (task 79).** The link to the passphrase
  page is now built from the published issuer, so an unusual `--public-url` (a mixed-case host
  or an explicit default port, such as `https://FlowState.Example.com:443`) no longer sends you
  to a differently written address. Usual addresses get exactly the same link as before.

### Security dependency floor and token audience check (task 80)

- **`mcp` 1.30 or later.** Versions before 1.30.0 are affected by GHSA-w4fh-qvv9-3v23 (a bearer
  token issued for another server could be accepted) and GHSA-84m7-p3x7-pcfv (Streamable HTTP
  sessions were never reclaimed, so memory could be run out). 1.30 closes idle sessions after
  30 minutes and caps them at 10,000; an app whose session was closed starts a new one.
- **Token audience check.** The tunnel and relay listeners now use the SDK's own check
  (`AuthSettings(resource_server_url=<address>/mcp, validate_token_resource=True)`): a token
  is accepted only when it was issued for this FlowState's `resource`. FlowState's tokens have
  always carried that exact `resource`, so apps already connected keep their sign-in; `resource`
  and the issuer are unchanged. The LAN listener has no OAuth, as before.
- **Reproducible test installs.** `requirements-dev.lock` pins every dev/test package with
  hashes (for Windows, macOS and Linux, Python 3.11 to 3.13); CI installs from it. What
  `pip install -e mcp-server` installs still comes from `pyproject.toml`. The GitHub Actions in
  CI are pinned to full commit SHAs.
- README: FlowState is not on PyPI; the `flowstate` package there is an unrelated project.

### No CORS on the OAuth endpoints, and a working token revocation (task 81)

- `/token`, `/register` and `/revoke` no longer send `Access-Control-Allow-Origin: *` or grant
  CORS preflights (no FlowState client runs in a browser page; the metadata documents keep it).
- `/revoke` follows RFC 7009 for Basic, form-secret and public clients (Basic and public ones got
  400 before): it ends that sign-in's access and refresh token whatever the hint, answers 200 for
  an unknown token, and refuses another app's token, which stays valid.

### Maintenance (internal, task 82)

- Code clean-up, no change in behaviour: removed two unused modules (`server_v14.py`,
  `split_tools.py`), unused imports and comments that only repeated the code; one helper
  (`urls.py`) for the trailing '/' on addresses and issuers; an up-to-date `server.py` header,
  without the byte-order mark at the start of the file.

### Data-git branch and `rules add` on Linux (task 83)

- **Data-git uses one branch on every computer.** `flowstate data-git init` now starts on
  `main` whatever git's own default is (it was `master` on many Linux gits and `main` on
  others, so two computers could each sync their own branch and never see each other's
  changes). `sync`, `migrate` and `clone` follow the remote's own branch: a computer on
  `master` joining a remote on `main` (or the other way round) moves its changes to the
  remote's branch, says so, and takes its name. When the remote already has both `main` and
  `master` with changes this computer doesn't have, nothing is pulled or pushed and the
  message says which computer to sync first; the old branch is removed from the remote only
  once all of it is on the other one.
- **A plain `git clone` of a data-git remote checks out the files.** A bare repository made
  where git's default is `master` has a default branch that doesn't exist once FlowState pushes
  `main`, so a plain clone came up empty. `sync` now points a bare repository on this computer
  (a path or `file://` URL) at the pushed branch. A remote FlowState can't change (ssh, https)
  still syncs, and `sync` says to set its default branch on the host. A default branch that
  exists is never changed. (task 84)
- **`flowstate rules add --project P "TEXT"`** works on Python 3.11 and 3.12 before 3.12.7,
  which rejected a rule typed after `--project` ("unrecognized arguments"). The text may come
  before or after the options; rules with control or invisible characters are refused as
  before.
- **Test tools:** the `dev` extra adds `httpx2`, which Starlette's test client now uses, and a
  test sets its cookie on the client, and a new `pytest.ini` sets pytest-asyncio's fixture loop
  scope, so the suite runs without deprecation warnings. Nothing changes for users. (task 85)

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
