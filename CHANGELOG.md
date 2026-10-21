# Changelog

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
