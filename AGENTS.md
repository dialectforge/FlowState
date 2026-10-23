# AGENTS.md: installing FlowState (instructions for AI agents)

You are an AI agent and your user asked you to set up FlowState from this repository.
FlowState is a local MCP server that gives AI apps a shared project memory (todos,
changes, decisions, problems, learnings) in a SQLite file on the user's machine.
Follow these steps in order. Each step says how to check it worked.

Ground rules while installing:
- Tell the user which app config files you are about to change before you change them.
  The setup script backs each one up first (`<file>.flowstate-backup-<time>`).
- Never print, paste or commit tokens or passwords. Use the placeholders shown here.
- Don't install system software. Everything goes into `.venv` inside this folder.
- If a step fails, stop and tell the user what failed; don't work around it silently.

(If instead you are working on FlowState's own code, see "Working on this repo" at the end.)

## 1. Detect the OS and Python

```bash
python3 --version    # macOS / Linux
python --version     # Windows (or: py -3 --version)
```

FlowState needs Python 3.11 or newer. If it is missing or older, stop and ask the user
to install Python 3.11+ from python.org (Windows: tick "Add python.exe to PATH").
Below, `PY` means whichever command worked (`python3`, `python` or `py -3`).

## 2. Get the code

If you are not already inside a clone of this repository:

```bash
git clone https://github.com/dialectforge/FlowState.git FlowState
cd FlowState
```

Check: `setup_flowstate.py`, `mcp-server/` and `database/schema.sql` exist.

## 3. Ask which app(s) to connect

Ask the user which AI apps should use FlowState. Known names for the script:

| App | `--clients` name | Config file the script edits |
|---|---|---|
| Claude Desktop | `claude-desktop` | Windows `%APPDATA%\Claude\claude_desktop_config.json` (Store install: `%LOCALAPPDATA%\Packages\Claude_*\LocalCache\Roaming\Claude\...`), macOS `~/Library/Application Support/Claude/claude_desktop_config.json`, Linux `~/.config/Claude/claude_desktop_config.json` |
| Cursor | `cursor` | `~/.cursor/mcp.json` |
| Windsurf | `windsurf` | `~/.codeium/windsurf/mcp_config.json` |
| Claude Code | `claude-code` | `~/.claude.json` (quit Claude Code first) |
| Codex CLI | `codex` | `~/.codex/config.toml` |
| Grok Build | `grok` | `~/.grok/config.toml` |
| Cline, or anything else with an `mcpServers` JSON file | `--config <path>` | that file |
| VS Code (Copilot), Zed | not supported; don't use `--config` | see "Manual config" below |

`--clients auto` picks every app whose config folder exists. Ask the user to fully quit
the apps you will configure (some apps rewrite their config when they exit).

Also ask the user two backup questions (explain them in one line each):
- "Should FlowState back up your project memory every night on this computer?" (recommended
  yes; adds `--schedule-backups`, default time 02:30, `--backup-time HH:MM` to change it).
- "Do you also want a nightly off-site copy in a **private** GitHub repository you own?"
  Opt-in only. If yes, ask for the repo as `<owner>/<repo>` (it must already exist and be
  private; FlowState checks before every push and refuses a public repo) and add
  `--offsite-github <owner>/<repo>`. It uses the user's existing `gh` login or git
  credentials; never ask for, paste or store a GitHub token. If no, add `--no-offsite`.

And one skill question:
- "Should I install the FlowState skill, so your AI apps follow the FlowState habits by
  themselves?" (recommended yes; adds `--install-skill`, which copies `skills/flowstate` to
  `~/.agents/skills/flowstate` and `~/.claude/skills/flowstate` after backing up any older
  copy to `~/.agents/flowstate-skill-backups/` / `~/.claude/flowstate-skill-backups/`). If no,
  add `--no-skill`. For Claude apps or ChatGPT, tell the user to zip the `skills/flowstate`
  folder and upload the ZIP in the app's Skills settings (the script doesn't build the ZIP).

## 4. Run the setup script

```bash
PY setup_flowstate.py --clients claude-desktop --schedule-backups --no-offsite --install-skill --dry-run   # shows what would change
PY setup_flowstate.py --clients claude-desktop --schedule-backups --no-offsite --install-skill --verify    # does it
# with the off-site copy: replace --no-offsite by --offsite-github <owner>/<repo>
```

Use a comma list for several apps: `--clients claude-desktop,grok`.
Options: `--db <file>` to use a specific database file (default: `FLOWSTATE_DB` if set,
else Windows `%LOCALAPPDATA%\flowstate\flowstate.db`, macOS
`~/Library/Application Support/flowstate/flowstate.db`, Linux
`~/.local/share/flowstate/flowstate.db`); `--skip-install` if the user manages Python
packages themselves; `--python <path>` to run the server with another Python;
`--backup-time HH:MM`; `--config <file>` (repeatable) for any other JSON config with an
`mcpServers` object. `PY setup_flowstate.py --help` lists every option.

What it does: creates `.venv`, `pip install -e mcp-server` into it, creates the database
if it does not exist (an existing one is kept; if it holds data it is backed up, then only
new FlowState tables and columns are added by numbered migrations; existing data is never changed),
adds a `flowstate` entry to each chosen config (everything else in the file is left as
is; a file it can't parse is refused and left untouched), with `--schedule-backups` adds a
nightly backup to the OS scheduler (Windows Task Scheduler, macOS launchd, Linux systemd
user timer or cron), with `--offsite-github` checks the repo is private and saves it for
the nightly push, puts a "FlowState review" shortcut on the desktop that opens the review page
(Windows `FlowState review.cmd`, macOS `FlowState review.command`, Linux
`flowstate-review.desktop`; a file of that name it didn't make is left alone; no desktop
folder: skipped with a line), and with `--verify` starts the server exactly as the app will
and calls `start_here`.

Check: the output ends with `[verify] OK: ...` and `Done.`; exit code 0. Re-running is
safe and reports `unchanged`.

## 5. Restart the app and check

Ask the user to reopen the app, then to ask it: "What projects are in FlowState?"
The app should call `start_here` (in Grok Build the tool shows as
`flowstate__start_here`). A brand-new database has no projects; the user can say
"Create a FlowState project called <name>".

Tell the user, in one line: "Session summaries FlowState writes on its own wait in a review
queue; ask me to show it any time, and tell me which to approve, edit or reject."

## 6. Backups

All commands run with the venv Python: `<venv python> -m flowstate <command>`.

| Task | Command |
|---|---|
| back up now | `flowstate backup` (add `--offsite` to also push the private GitHub copy) |
| nightly backup on / off | `flowstate schedule-backups [--time 02:30] [--offsite]` / `flowstate schedule-backups --remove` |
| set up the off-site copy | `flowstate offsite setup --repo <owner>/<repo>` (refused unless the repo is private) |
| check off-site without pushing | `flowstate offsite push --dry-run` |
| restore | `flowstate restore <backup file>` (into a new file, verified; `--to <new file>`) |
| approvals (user) | `flowstate approvals` shows them; `approvals on [--project P]`: an AI's deletes and turning review off wait for the user (popup, phone link, terminal); `approvals off` asks for `y` on the terminal; `approvals phone on\|off`; `approvals popup add\|remove NAME` |
| update FlowState (only when the user asks) | `flowstate update --dry-run` (the plan; changes nothing), then `flowstate update --yes --user-said "<the user's words>"` (or without `--yes`: the user types `y` in their terminal); `flowstate update --rollback`. Signed releases only; see README "Upgrading" |
| update check | `flowstate update check` shows it; `update check on\|off\|now` (once a day from `start_here`, asks GitHub for the newest release; sends nothing about you) |
| review queue (user) | `flowstate review` lists it; `flowstate approve <id> [<id> ...]` / `flowstate reject <id> [--reason ...]` ask for `y` on the terminal (never piped input; no terminal = refused) |
| review page (user) | `flowstate review-page` (127.0.0.1:8770 only; opens the browser with a one-time link; includes "Who did what"), or the "FlowState review" desktop shortcut setup makes (runs that command; opens the page FlowState already has open, if any). Without a command: the `http://127.0.0.1:<port>/` link in the brief and in `manage_review(action="walk")`'s answer, shown only to apps on the same computer (FlowState starts the page on first need, 127.0.0.1 only; `FLOWSTATE_REVIEW_PORT`) |
| tunnel review page (user, own tunnel) | `https://<your tunnel>/review/tunnel` on the public listener (`--public-url`), signed in with the owner passphrase (`flowstate remote passphrase`); the same page, user-only items included; agent tokens don't open it; see `docs/REMOTE-ACCESS.md` section 4 |
| phone review list (user, relay) | the bookmarkable `https://flowstatemcp.com/review/<handle>` in the brief and the walk's answer when the relay is enrolled and phone links are on (`approvals phone on\|off`); see `docs/APPROVALS.md` section 5 |
| per-app keys for the local/LAN HTTP server (user) | `flowstate keys add --name NAME [--read-only] [--workspace W]` (shows the key once) / `keys list` / `keys revoke --id ID` (or `--name NAME`) |
| pause an app (user) | `flowstate apps list` / `apps pause --app NAME` / `apps resume --app NAME` |

Backups go to `FLOWSTATE_BACKUP_DIR` or the per-OS default (Windows
`%LOCALAPPDATA%\flowstate\backups`, macOS `~/Library/Application Support/flowstate/backups`,
Linux `~/.local/share/flowstate/backups`). Each is integrity-checked; the newest per day for
14 days and per week for 8 weeks are kept. Restore never replaces an existing database
unless `--force` is given, and then backs it up first. Copies taken right before a restore, a
migration or a delete / approved clean-up / markdown edit are labelled `pre-restore`,
`pre-migration` or `pre-bulk` and kept at least 7 days regardless of those limits. FlowState
takes them even without nightly backups, and never adds a daily copy of its own. The off-site copy pushes only the
backup file and a manifest, to its own branch (`flowstate-backup`), after checking the repo
is private.

## Manual config (if you can't run the script)

Use absolute paths. `<REPO>` is this folder. The Python is the venv one:
Windows `<REPO>\.venv\Scripts\python.exe`, macOS/Linux `<REPO>/.venv/bin/python`
(create it with `PY -m venv .venv` then `<venv python> -m pip install -e mcp-server`).
`<DB>` is the database file. In JSON on Windows, double every backslash.

### Claude Desktop (stdio; also Cursor `~/.cursor/mcp.json`, Windsurf `~/.codeium/windsurf/mcp_config.json`)

```json
{
  "mcpServers": {
    "flowstate": {
      "command": "<REPO>/.venv/bin/python",
      "args": ["-m", "flowstate"],
      "env": {
        "FLOWSTATE_DB": "<DB>",
        "PYTHONPATH": "<REPO>/mcp-server"
      }
    }
  }
}
```

Merge the `flowstate` entry into the existing `mcpServers` object; keep everything else.
Claude Desktop ignores `cwd`, which is why `PYTHONPATH` is set.

### Claude Code

```bash
claude mcp add --scope user flowstate -e FLOWSTATE_DB=<DB> -e PYTHONPATH=<REPO>/mcp-server -- <venv python> -m flowstate
claude mcp list
```

### Codex CLI (`~/.codex/config.toml`)

```toml
[mcp_servers.flowstate]
command = "<venv python>"
args = ["-m", "flowstate"]
env = { FLOWSTATE_DB = "<DB>", PYTHONPATH = "<REPO>/mcp-server" }
```

### Grok Build

Grok Build adds MCP servers with its own CLI (writes `~/.grok/config.toml`; add
`--scope project` for `./.grok/config.toml`):

```bash
grok mcp add flowstate -e FLOWSTATE_DB=<DB> -e PYTHONPATH=<REPO>/mcp-server -- <venv python> -m flowstate
grok mcp list
grok mcp doctor flowstate
```

Equivalent TOML: the same `[mcp_servers.flowstate]` block as Codex above. Grok also
picks up servers from `~/.claude.json` and `~/.cursor/mcp.json`. It reaches MCP tools
through its `search_tool` / `use_tool` meta-tools, so the tools are named
`flowstate__start_here`, `flowstate__log_work`, and so on. Grok reads `AGENTS.md` project
rules; to make it use FlowState in a project, add a line to that project's `AGENTS.md`:
"At the start of each task call flowstate__start_here with this project's FlowState name,
and record decisions, changes and problems with flowstate__log_work."

### VS Code (GitHub Copilot agent mode)

VS Code reads `.vscode/mcp.json` in a workspace, or the user-level `mcp.json` (open it with
the Command Palette: **MCP: Open User Configuration**). Its top-level key is `servers`, not
`mcpServers`, so the Claude Desktop snippet does not work as is:

```json
{
  "servers": {
    "flowstate": {
      "type": "stdio",
      "command": "<venv python>",
      "args": ["-m", "flowstate"],
      "env": {
        "FLOWSTATE_DB": "<DB>",
        "PYTHONPATH": "<REPO>/mcp-server"
      }
    }
  }
}
```

Don't use `setup_flowstate.py --config <file>` on this file: the script only knows the
`mcpServers` format and would add an `mcpServers` object that VS Code ignores. Edit it by
hand. Then start the server from the **MCP: List Servers** view
and use FlowState from Copilot Chat in Agent mode.

### Cline (VS Code extension)

Cline keeps its servers in `cline_mcp_settings.json` under an `mcpServers` object, so
the Claude Desktop snippet above works as is. Open it from the Cline panel: **MCP Servers**
icon, **Configure** tab, **Configure MCP Servers**. With VS Code it is at:

- Windows: `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`
- macOS: `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- Linux: `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- Cline CLI: `~/.cline/data/settings/cline_mcp_settings.json` (or `CLINE_MCP_SETTINGS_PATH`)

(VS Code Insiders, Cursor and other VS Code forks use their own folder instead of `Code`.)
Because it is an `mcpServers` file, the script can register it:
`PY setup_flowstate.py --config "<path to cline_mcp_settings.json>"`.

### Zed

Zed reads MCP servers from `context_servers` in its `settings.json` (open it with the
`zed: open settings file` action; macOS and Linux `~/.config/zed/settings.json`, Windows
`%APPDATA%\Zed\settings.json`), or from **Settings > AI > MCP Servers**:

```json
{
  "context_servers": {
    "flowstate": {
      "command": "<venv python>",
      "args": ["-m", "flowstate"],
      "env": {
        "FLOWSTATE_DB": "<DB>",
        "PYTHONPATH": "<REPO>/mcp-server"
      }
    }
  }
}
```

Merge the `flowstate` entry into any existing `context_servers` object. The shape has
changed between Zed versions; this is the one in Zed's MCP docs (zed.dev/docs/ai/mcp) as
of October 2026, so check there if an older Zed rejects it. Don't use the script's `--config` on this
file (it would add an `mcpServers` object Zed ignores); edit it by hand.

### Any other MCP client

stdio command: `<venv python> -m flowstate`, environment `FLOWSTATE_DB=<DB>` and
`PYTHONPATH=<REPO>/mcp-server`. Or use the HTTP server below.

## HTTP server

For several machines on a network, or (later) a public URL through a tunnel:

```bash
PY -c "import secrets; print(secrets.token_urlsafe(32))" > <token file>   # outside any synced/git folder
<venv python> -m flowstate token migrate --from <token file>   # copies it into the OS credential store
<venv python> -m flowstate --transport http --host 127.0.0.1 --port 8766
```

The server looks for the token in `FLOWSTATE_TOKEN`, then `--token-file` (older setups;
it warns if that file sits in a git folder), then the OS credential store (Windows
Credential Manager, macOS Keychain, Linux `~/.config/flowstate/http-token` with mode 0600).
`flowstate token status` says whether one is stored, without showing it. After migrating,
the user deletes the token file themselves once the server works without it.

**Per-app keys (recommended).** Instead of giving every app the one shared token, give each
app its own key: `<venv python> -m flowstate keys add --name "<app>" [--read-only]
[--workspace "<machine or workspace>"]`. The key is shown once (only a hash is kept); the user
pastes it into that app as its bearer token. FlowState then records the app's name from the
key, not from what the app says. A read-only key can only read; `keys revoke --id <id>` (or
`--name <app>`) stops a key at once (it then gets 401); `keys list` shows them (never the key).
Keys work on the local/LAN listener only, never on the public one, and can't use `manage_data_git`,
attach files, or ask for or approve deletes. The shared token keeps working as before; changes made with it are marked
"shared token (legacy)". Apps may also send `X-FlowState-Model: <model name>` and
`X-FlowState-Workspace: <name>` headers (a key's own workspace, if set, wins).

Endpoint: `http://<host>:8766/mcp`, header `Authorization: Bearer <token>`. `GET /health`
answers `{"ok": true}` without a token (for tunnel health checks). Bind to `127.0.0.1`
behind a tunnel or reverse proxy, or to the machine's LAN address for LAN use. The token must be 16+ characters.

Client snippets for HTTP (replace `<url>`; keep the token out of shared files):

- Grok Build: `grok mcp add --transport http flowstate <url>` then in
  `~/.grok/config.toml` under `[mcp_servers.flowstate]` set
  `bearer_token_file = "~/<path to token file>"` (or
  `headers = { "Authorization" = "Bearer ${FLOWSTATE_TOKEN}" }`).
- Claude Code: `claude mcp add --transport http flowstate <url> --header "Authorization: Bearer <token>"`
- Cursor (`~/.cursor/mcp.json`): `"flowstate": {"url": "<url>", "headers": {"Authorization": "Bearer ${env:FLOWSTATE_TOKEN}"}}`
- Codex (`~/.codex/config.toml`): `[mcp_servers.flowstate]` with `url = "<url>"` and
  `bearer_token_env_var = "FLOWSTATE_TOKEN"` (recent Codex versions).
- Claude Desktop: use the stdio setup above (its config file has no URL form).

Per-app tokens (each revocable on its own, optionally read-only):
`<venv python> -m flowstate remote add-client --name "<app>" [--read-only]`, then
`remote clients` / `remote revoke --client <id>`. For cloud AIs over HTTPS (claude.ai,
ChatGPT, Grok) see `docs/REMOTE-ACCESS.md`: a `--public-url` listener on 127.0.0.1 behind a
Cloudflare Tunnel, with OAuth 2.1 sign-in approved by the owner passphrase. Don't set up a
tunnel or anything at Cloudflare without the user's explicit go-ahead.

## How agents should use FlowState once it's connected

The server tells every client this at connect time; it is repeated here for clients that
don't show server instructions:

1. Call `start_here(project="<name>")` first in every conversation about a project
   (no arguments, or `folder="<working folder>"`, to list projects).
2. Record work as you go with `log_work(project=..., kind=decision|change|problem|learning|todo, summary=...)`.
3. Every write must name the project. Never store secrets in text (writes that contain
   keys, tokens, passwords or card numbers are refused with `secret_detected`). If you know
   your model name, pass it as `model="<name>"` on writes; never guess or invent it.
4. Sessions open and close automatically (closed after 2 hours idle or when the app quits,
   with a summary marked `[auto-captured, needs review]`). A small housekeeping session (only
   reads, undo or review decisions) isn't queued for review.
5. If FlowState is slow or errors, tell the user once and carry on without it. Every write's
   answer has a `receipt` (`change` id and `saved` record ids). If a write times out, don't
   repeat it: the next FlowState answer says whether it saved. The same write repeated within
   2 minutes returns the first receipt (`already_saved`) instead of a copy.
6. Auto-captured session summaries, and anything logged with `suggested=true`, wait in the
   review queue (`manage_review(action="list")`: one line per item, 20 at a time, `next_page`
   for more; `item_id=<id>` gives one item in full, read it before approving with edits). Only
   call `manage_review` with `action="approve"` / `"reject"` when the user explicitly asks in
   the chat, quoting their
   words in `user_request`. An item your own app queued needs the user's own words (not one
   word, not the item's text), or the user decides it with `flowstate approve <id>` in their
   terminal. Don't run that for them: it asks for their `y` on the terminal and refuses piped
   input. When the user asks to see what's waiting, call `manage_review(action="walk")`: in
   apps that show MCP form dialogs (Claude Code, Cursor, VS Code) connected directly (stdio or LAN),
   FlowState asks the user about each item itself and acts only on their answer in that box;
   elsewhere (other apps, the relay, read-only) you get one page of the list (20, oldest
   first; `next_page` gives the call for more) and the links (`review_page` on the computer
   where FlowState runs, only for apps on that computer; `phone` with the relay) to show the
   user, and nothing was decided.
   Never approve or reject them yourself afterwards. The brief's review-queue line carries the
   same links.
7. The **review mode** (`off`, `auto_only` default, `types`, `everything`; shown in the brief)
   decides what else waits for review. Only call `manage_review(action="set_mode")` when the
   user asks for that change in the chat, quoting their words in `user_request`; every change
   is logged. Never change it to get your own writes saved. With approvals on (below),
   turning review off waits for the user like a delete.
8. Things several projects share live in one project; the others **link** to them
   (`manage_links(action="add", project=..., link_type="uses", target_project=...,
   target_component=...)`). Log changes to a shared component by its name; every linked
   project gets an update todo and a "changed since you last looked" note. FlowState only
   alerts; it never changes code in other apps.
9. Other helpers: `manage_todos(action="next")` (what to do next; `update` to mark done;
   other agents' claims are listed last, so pick something else),
   `manage_problems` (attempts and solve), `manage_history` (see edits; undo the last change
   to a record, or take back a record you created while nothing refers to it; rows changed
   since are skipped and listed), `manage_memory(action="cleanup")` (duplicate / stale todos, misfiled records, as review
   suggestions only; it only suggests a move when the record names the other project or one of
   its parts), `manage_memory(action="import")` (memory exports into the review queue),
   `manage_projects` (projects and components). **Deletes:** only when the user asks,
   `manage_projects(action="delete" / "delete_component")` and `manage_memory(action="delete",
   record_type=..., record_id=...)` delete at once after a safety copy; the answer says what
   went (children and links too) and gives the `manage_history(action="undo", ...)` call that
   brings it all back. If the user turned **approvals** on (`flowstate approvals on`), a delete
   (and turning review off) waits for the user instead: their app may show them a popup, else
   the answer's `next` line is one sentence to pass on (a flowstatemcp.com link for their
   phone, or `flowstate approve <id>` for their terminal). Pass it on as it is; you can't
   approve it, and no app can pass the user's OK along. (Limit: this stops agents approving their
   own work through FlowState; one that controls the user's own logged-in browser or terminal
   could still get through.) Through the relay, `manage_review` approve / reject answer
   `phone_only` by default (the user's own setting, `flowstate relay chat-approvals`, with no
   tool for it): nothing was decided; show the user the phone link in the answer and don't
   retry. The review page, pausing apps and git commit
   capture are the user's controls (command line / `flowstate review-page`); there is no tool
   for them.

10. The brief starts with the project's **hard rules** (`manage_rules`; never break them;
   remove one only when the user asks, quoting them in `user_request`) and any **handoff** note
   from the last AI (continue from it, then `manage_handoffs(action="pickup")`; leave one with
   `manage_handoffs(action="leave", where_i_was=..., next_step=...)` if you stop mid-task).
   `start_here(size="quick"|"deep")` for a shorter or longer brief. A line `Since your last
   visit (...)` says what other sessions saved since this app last read the brief; after a
   long gap, use the `manage_history(action="who", since=...)` call it gives. A line on top
   `FlowState X.Y.Z is out (this computer has ...)` comes from the daily update check: tell the
   user once, and never update FlowState unless they ask. When they ask, run
   `flowstate update --dry-run` in a terminal on that computer, show them the plan, then
   `flowstate update --yes --user-said "<their words>"` (there is no tool for it, so a remote app
   can't update anyone's computer). `FlowState was updated to X; this app still runs Y` means:
   ask the user to restart the app. Local FlowState makes one network
   call: once a day it asks GitHub whether there is a newer release. It sends nothing about you
   or your projects (only what any web request reveals: your IP address). Turn it off with
   `flowstate update check off`.
11. Todo priorities are critical / urgent / normal / low (older names high and medium still
   work). `log_work(source=user_stated|ai_inferred|from_code)` says where a record came from
   (who set it is stored). Changing an existing record to `user_stated` only queues it for the
   user to confirm themselves (review page or terminal; approvals on or off); no app can approve it.
   `manage_memory(action="stale")` lists facts (paths, ports, config) not checked for 60 days;
   `manage_memory(action="mark_checked")` after checking one. `search(query=..., answer=true)`
   answers a question from the records, with their ids, and says so when FlowState has nothing
   on it; `search` without a query lists the newest records.
12. Every recorded change keeps the app, the model (only if supplied), the machine or
   workspace, and how the app was identified (`app_key`, `oauth_client`, `remote_token`,
   `shared_token` or `self_reported`). `manage_history(action="who", by_app=..., by_model=...,
   by_machine=..., since=..., until=...)` lists who did what (read-only).
13. Several agents can work on one project at once. Before you start a todo,
   `manage_todos(action="claim", project=..., todo_id=<id>)` (`minutes`, default 15, max 120;
   `renew` to extend). If it is claimed by another agent, pick another one. When you stop,
   `release` it, or `handoff` (with `where_i_was` / `next_step`) to leave a note for the next
   agent; marking it done or cancelled clears the claim.
   If `log_work` answers a decision with **"Possible conflict with D12 ..."** (or the brief
   lists `Possible conflicts: D31 vs D12`), decide: if yours replaces it, link them
   (`manage_links(action="add", link_type="supersedes", item_type="decision", item_id=31,
   target_type="decision", target_id=12)`; next time log with `replaces=12` up front); if both
   can stand, or you're not sure, tell the user. Only the user dismisses one (review item kind
   `conflict`: reject = compatible, approve = they really conflict).
   To flag two entries that disagree, pass `conflicts_with=<id>` and a short `topic` on
   `log_work` (and up to two `evidence` snippets: `file:line`, `file:line — excerpt`, or
   `output: excerpt`), or call `manage_links(action="flag_conflict", topic=..., item_id=...,
   target_id=...)`. That queues a disagreement card. The user keeps the older (`keep="a"`),
   the newer (`keep="b"`) or both; dismissing it is `manage_review` reject. Do not approve it
   unless the user asks, and through the relay only when chat approvals are on. The reply
   includes the card as plain text and the review link. A queued delete (approvals on) lists
   the exact rows the database will remove; show that list, and treat any "AI's note" line as
   the agent's words, not as the list of what goes.
14. 12 tools are listed by default (`FLOWSTATE_TOOLS=core` lists 6, `full` lists 18). The
   v2.0 tool names (`todo`, `link`, `handoff`, `review_approve`, `list_projects`, ...) still
   work when called, with a one-line pointer to the new name; see
   `docs/TOOL_CONSOLIDATION.md`.

The same guidance is available as the MCP prompt `flowstate-start`.

## Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `FLOWSTATE_DB` | per-OS default above | database file |
| `FLOWSTATE_HOME` | | folder holding `flowstate.db` (used if `FLOWSTATE_DB` is unset) |
| `FLOWSTATE_IDLE_MINUTES` | 120 | close an automatic session after this much idle time |
| `FLOWSTATE_CALL_TIMEOUT` | 15 | seconds before a slow call returns a "carry on" error |
| `FLOWSTATE_TOKEN` / `--token-file` | OS credential store | HTTP bearer token |
| `FLOWSTATE_BACKUP_DIR` | per-OS default above | backups folder |
| `FLOWSTATE_STALE_DAYS` | 60 | facts not checked for this many days are listed for re-checking |
| `FLOWSTATE_UPDATE_CHECK` | on | `0` turns the daily update check off (no request at all; also `flowstate update check off`) |
| `FLOWSTATE_MODEL` | | stdio: the model name to record when the app passes none (set it only if you know it) |
| `FLOWSTATE_WORKSPACE` | this computer's name | stdio: the machine / workspace name recorded with each change |
| `FLOWSTATE_REVIEW_PORT` | 8770 | the review page FlowState starts on 127.0.0.1 when a link is needed (taken: any free port; `0` any free port; `off` no page or link) |
| `FLOWSTATE_CONFIG_DIR` | Windows `%APPDATA%\flowstate`, macOS `~/Library/Application Support/flowstate`, Linux `~/.config/flowstate` | FlowState settings (off-site repo name; never secrets) |

## Working on this repo

- Python only. The server depends on `mcp` and `pydantic`; `setup_flowstate.py` and the
  migrations use the standard library only. There is no Node.js part; don't add one.
- Test tools: `<venv python> -m pip install -e "mcp-server[dev]"` (pytest, pytest-asyncio, httpx).
- Tests: `.venv/Scripts/python -m pytest tests -q` (Windows) or `.venv/bin/python -m pytest tests -q`.
  They use temporary databases and fake config folders only: `tests/conftest.py` points HOME
  and every FlowState path (database, backups, config, token store, auth DB, relay, project
  files, data-git) into a session temp folder before FlowState is imported, and
  `tests/guard_site/sitecustomize.py` makes pytest and every server it starts refuse (and
  record) any touch of the real FlowState folders; the run fails if anything was refused.
  End-to-end: `.venv/bin/python tests/e2e/run_e2e.py` (about 5 minutes; same guard).
- Never point tests or experiments at a real database or a real app config.
- Server code: `mcp-server/flowstate/` (`server.py` tools and transports, `tooldefs.py` tool
  definitions, `surface.py` public and older tool names -> internal operations,
  `agent_guide.py` the wording sent to apps, `saves.py` and `dbconn.py` (a write is one save:
  one commit, or nothing), `brief.py`, `autosession.py`,
  `guard.py`, `review.py`, `review_mode.py`, `links.py`, `tasks.py`, `history.py`,
  `rules.py`, `handoffs.py`, `provenance.py`, `ask.py`, `meaning.py`, `cleanup.py`,
  `activity.py`, `review_page.py`, `markdown_io.py`, `memory_import.py`, `git_capture.py`, `deletes.py`,
  `secrets_scan.py`, `secret_store.py`, `remote_auth.py`, `app_keys.py`, `identity.py`,
  `who.py`, `claims.py`, `conflicts.py`, `backup.py`, `offsite.py`, `scheduler.py`, `cli.py`, `cli_more.py`,
  `cli_review.py` (the user's `review` / `approve` / `reject` / `approvals`), `approvals.py` (opt-in
  user-only approvals, phone grants, pinned `FSA_PUBLIC_KEYS`), `popup.py` (MCP elicitation),
  `approve_page.py` (the phone page at `/approve/<item>`), `review_phone.py` (the phone review list at
  `/review`), `waiting.py` (the chat walk, the local review link and the page started on first need), `connections.py`, `http_app.py`; the optional
  relay agent in `relay/`, wire protocol in `docs/relay-protocol.md`). Schema:
  `database/schema.sql` (base; the code reads its copy `mcp-server/flowstate/schema.sql`, which ships
  in the package; keep the two the same, a test checks) plus numbered migrations in `mcp-server/flowstate/migrations/`
  (new tables only, or `ADD COLUMN ... DEFAULT NULL` on FlowState's own v2 tables as in 012;
  never touch v1.1 tables, never DROP or rewrite, so a v1.1 server can still open the same database).
- Record identity only through `identity.py` (`identity.insert` / `identity.set_sql`), so
  every log stores app, model, machine and identity source the same way.
- Every public tool needs a label with its read / additive / destructive actions in
  `tool_labels.py`, and each action a route in `surface.py` (tests check). Keep older tool
  names working: they are the internal operation ids.
- Changes for users go in `CHANGELOG.md`.
