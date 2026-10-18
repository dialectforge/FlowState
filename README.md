# FlowState

**A shared project memory for your AI apps. Free, open source, and stored on your own machine.**

FlowState is a local [MCP](https://modelcontextprotocol.io) server. It gives every AI app you
use the same memory of your projects: todos, changes, decisions, problems and lessons learned.
Your AI reads a short brief when it starts work on a project and writes down what happens as it
goes. The next chat (or the next app) picks up where the last one stopped.

Everything lives in one SQLite file on your computer. Nothing is sent anywhere unless you turn
on an optional feature that does so (an off-site backup to your own private GitHub repo, or remote
access through your own Cloudflare Tunnel).

## Many AIs, one memory, at the same time

Claude, ChatGPT, Cursor and Grok can all use FlowState at once, on your computer, across your network, or from your phone. When one AI logs a decision or a change, the others see it the next time they check. Hard rules and handoff notes reach every AI, and every change records which AI made it, so you always know who did what. Work across projects is linked too, so a change to something shared shows up in every project that uses it.

## Why I built it

I built FlowState v1 in January 2026 as a gift to the vibe coder community, back when context was a serious bottleneck and every new AI chat started from scratch. This week I updated it, because the bottleneck moved. Now most of us work across several AI platforms at once, and keeping context between them is the new problem. FlowState stays free and local.

*John Martin, DialectForge*

---

## Quick start: point your AI at this repo

Paste this into an AI app that can run commands on your computer (Claude Code, Cursor,
Windsurf, Codex CLI, Grok Build, ...):

> Set up FlowState for me from https://github.com/dialectforge/FlowState by following its AGENTS.md.

The AI reads [AGENTS.md](AGENTS.md), asks you which apps to connect and whether you want
nightly backups and the FlowState skill, shows you a dry run, then runs `setup_flowstate.py`.
When it's done, fully quit and reopen the apps it connected.

Claude Desktop's chat can't run commands by itself. Set it up from one of the apps above
("...and connect it to Claude Desktop"), or run the script yourself (below).

### What `setup_flowstate.py` does

It uses the Python standard library only, and it's safe to run again (it reports `unchanged`
for anything already set up). In order, it:

1. checks for Python 3.11 or newer;
2. creates `.venv` in the repo folder and runs `pip install -e mcp-server` into it (skip with `--skip-install`);
3. creates the database if there isn't one, or keeps your existing one and adds only the new
   FlowState tables and columns (numbered migrations, with an integrity-checked backup first when the
   database already has data);
4. adds a `flowstate` entry to each chosen app's MCP config, after saving a copy of that file
   as `<file>.flowstate-backup-<time>`. Other servers and settings are left alone; a file it
   can't parse is refused and left untouched;
5. with `--offsite-github OWNER/NAME`, checks that the GitHub repo is **private** and saves it for
   nightly off-site backups (`--no-offsite` skips the question);
6. with `--schedule-backups`, adds a nightly local backup to your OS scheduler (Windows Task
   Scheduler, macOS launchd, Linux systemd user timer or cron), at 02:30 unless you pass `--backup-time HH:MM`;
7. with `--install-skill`, installs the FlowState skill (see below; `--no-skill` skips the question);
8. with `--verify`, starts the server the way your app will and calls `start_here` to check it answers.

Useful flags: `--dry-run` (show what would change, change nothing), `--help`,
`--clients auto|all|none|<list>` (`claude-desktop`, `cursor`, `windsurf`, `claude-code`,
`codex`, `grok`; `auto` = every app whose config folder exists), `--config <file>` (any other
JSON config with an `mcpServers` object), `--db <file>`, `--python <path>`.

### Manual install

Needs Python 3.11+ and git.

```bash
git clone https://github.com/dialectforge/FlowState.git FlowState
cd FlowState
python -m venv .venv
# Windows: .venv\Scripts\python   macOS/Linux: .venv/bin/python
.venv/bin/python -m pip install -e mcp-server            # just the server
.venv/bin/python -m pip install -e "mcp-server[dev]"     # plus test tools (pytest)
python setup_flowstate.py --clients claude-desktop --schedule-backups --no-offsite --install-skill --dry-run
python setup_flowstate.py --clients claude-desktop --schedule-backups --no-offsite --install-skill --verify
.venv/bin/python -m pytest tests -q                      # run the tests
```

`setup_flowstate.py` installs into `.venv` itself, so the `pip install` lines are only needed
if you want the test tools or manage the venv yourself (then add `--skip-install`). Config
snippets for every app, if you'd rather edit configs by hand, are in [AGENTS.md](AGENTS.md#manual-config-if-you-cant-run-the-script).

---

## The FlowState skill

[`skills/flowstate`](skills/flowstate/SKILL.md) is an Agent Skill: a short set of habits (read
the brief first, log to the right project, link shared components, never approve your own review
items, leave a handoff if you stop mid-task). Apps that load skills follow it without being told.

- **With setup:** `python setup_flowstate.py --install-skill` (or answer "yes" when it asks)
  copies the folder to `~/.agents/skills/flowstate` and `~/.claude/skills/flowstate`. An older,
  different copy is moved to `~/.agents/flowstate-skill-backups/` or
  `~/.claude/flowstate-skill-backups/` first.
- **By hand:** copy the `skills/flowstate` folder into `~/.agents/skills/` or `~/.claude/skills/`.
- **Claude apps and ChatGPT:** zip the `skills/flowstate` folder yourself and upload the ZIP in
  the app's Skills settings. (FlowState doesn't build the ZIP for you.)

No skill support in your app? Paste this once per chat, or into its custom instructions:

> Use FlowState as my project memory. First call start_here for the project we're working on (ask me which if unsure). As we work, log decisions, changes, problems, lessons and todos to that project with log_work. If something is shared between projects, link it with manage_links instead of copying it. Never approve or reject review items, and never change my review mode, unless I ask you to in this chat. At the end, log a short summary of what we did and what's next.

The server also sends these rules to every app when it connects, and offers them as the MCP
prompt `flowstate-start`.

---

## Features

**Memory your AI actually reads**
- **`start_here` brief**: one call gives the AI the project's hard rules, handoff notes, open
  todos, problems, recent changes, decisions and key lessons. Sizes: `quick` (about 1,500
  characters), `standard` (4,000, the default) or `deep` (up to 12,000).
- **`log_work`**: one simple tool to record a decision, change, problem, learning or todo.
  Every write must name a project, so nothing lands in the wrong place.
- **Automatic sessions**: a session opens on the first call for a project and closes after
  2 hours idle (or when the app quits), with a short summary marked `[auto-captured, needs review]`.

**You stay in charge**
- **Review queue**: auto-captured summaries, and anything the AI marks as a suggestion, wait
  for your OK. Approve, edit or reject them in chat or on the review page.
- **Review mode** (yours to set): `off`, `auto_only` (default), chosen record types, or
  `everything`. Set it for the whole install or per project. The AI may change it only when you
  ask in the chat, and every change is logged with your words.
- **Hard rules**: "never do this" rules per project, always shown in full at the top of the brief.
- **Handoff notes**: an AI that has to stop mid-task leaves "where I was / next step"; the next
  AI sees it first until it picks it up.
- **Undo and history**: edits made through FlowState are kept; you can see them and undo the
  last change to a record (an undo can itself be undone).
- **Superseded decisions**: a new decision can name the one it replaces, so only the current
  one shows in the brief.
- **Source and last-verified flags**: records can say where they came from (`user_stated`,
  `ai_inferred`, `from_code`). Facts like paths, ports and config not checked for 60 days are
  listed for re-checking.

**Accountability**
- **Who, model and machine on every change**: edit history, review queue items and decisions,
  links, handoffs, hard rules, review-mode changes and the activity log all record which app
  made the change, the AI model (only when the app supplies it; never guessed), the machine or
  workspace, and how the app was identified (per-app key, remote token, OAuth app, shared token
  or self-reported).
- **"Who did what" view**: on the review page, filter by app, model, machine, project and time
  (50 per page) and undo a change from there (the normal undo, with the page's CSRF and origin
  checks). AIs can read the same list with `manage_history(action="who")`.
- **Per-app keys for the local/LAN HTTP server**: `flowstate keys add --name "<app>"` gives
  each app its own key (optionally read-only), so the recorded app name comes from the key, not
  from what the app says. The key is shown once and only a hash is kept; a revoked key is
  refused (401) at once. Keys aren't accepted on the public listener, and can't use `manage_data_git` or
  attach files. The old shared LAN token still works; its changes are marked "shared token (legacy)".

**Projects that share things**
- **Cross-project links with change alerts**: when several projects use one thing (for example,
  "Atlas VPN uses the Wire Protocol component of Atlas OS"), link them. A change logged on the
  shared component gives every linked project an update todo and a "changed since you last
  looked" note. FlowState alerts; it never changes code in other projects.
- **GitHub and Linear links**: attach an issue or PR URL to a todo, problem or other record.

**Tasks**
- **Priorities**: critical, urgent, normal, low (the older names high and medium still work).
- **Subtasks, blockers and "what's next"**: `todo(action="next")` lists open, unblocked work,
  highest priority first.

**Finding things**
- **Ask a question**: `search(query=..., answer=true)` takes a question in plain words; FlowState
  answers by quoting your own records, with their ids, and says so when it has nothing on it. It doesn't make anything up.
- **Search**: keyword search over todos, decisions, changes, learnings, problems and solutions
  (word endings match too). Search by meaning is optional and uses a local model you install yourself.
- **Clean-up suggestions**: likely duplicates, stale items and records filed under the wrong
  project, sent to the review queue as suggestions. Nothing moves until you approve.

**Your controls (command line and browser)**
- **Local review page**: `flowstate review-page` opens a small page in your browser (this
  computer only, one-time link): review queue, review mode, pause apps, "Who did what".
- **Per-app activity log and pause**: see which app did what (`flowstate apps list` /
  `apps activity`) and pause one app's writes (`apps pause --app NAME`). A paused app can still read.
- **Markdown export and import**: `export-md` writes a readable copy of a project; `import-md`
  brings your edits back through the review queue.
- **Memory import**: bring a Claude or ChatGPT memory export (text, markdown or JSON) into the
  review queue (`import-memory`, or the `import_memory` tool).
- **Opt-in git capture**: off by default. Turn it on per project and new commit subjects
  become changes (subject lines only; no author, e-mail or diff).

**Safety and backups**
- **Secret refusal**: a write that looks like it contains a key, token, password, private key
  or card number is refused (`secret_detected`) and not saved. `flowstate scan-secrets` lists
  older records that look suspicious, without showing values.
- **Token in the OS credential store**: the HTTP bearer token goes in Windows Credential
  Manager, the macOS Keychain, or on Linux a `~/.config/flowstate/http-token` file with mode 0600.
- **Nightly backups**: integrity-checked; the newest per day for 14 days and per week for
  8 weeks are kept. Restore always goes to a new file unless you add `--force` (and then the
  current file is backed up first).
- **Optional private GitHub off-site copy**: opt-in. FlowState checks the repo is private
  before every push and refuses otherwise. It pushes only the backup file and a manifest, to its
  own branch, using your existing `gh` or git login. It never stores a GitHub token.

**Connecting**
- **stdio and HTTP transports**: stdio for apps on the same computer; streamable HTTP with a
  bearer token for other machines on your network, or a separate key per app
  (`flowstate keys add`).
- **Optional remote access**: for cloud AIs (claude.ai, ChatGPT and others), a public listener
  behind your own Cloudflare Tunnel with OAuth 2.1 sign-in that you approve with an owner
  passphrase, plus revocable per-app tokens (optionally read-only). See
  [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md).
- **Fewer or more tools**: 12 tools are listed by default. `--tools core` (or
  `FLOWSTATE_TOOLS=core`) lists only the 6 everyday ones to save context; `--tools full` adds the
  6 specialist ones. Every tool still answers when called, in every profile. Every tool is
  labelled read-only or read-write (per action in its `_meta`).

---

## Works with

- **Claude Desktop**, **Claude Code**, **Cursor**, **Windsurf**, **Codex CLI**, **Grok Build**:
  local stdio, set up by `setup_flowstate.py`.
- **claude.ai** and **ChatGPT**: through the optional remote access (HTTPS + OAuth), see
  [docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md).
- **Any other MCP client**: stdio (`<venv python> -m flowstate`) or HTTP with a bearer token.

## Why FlowState

Many AI memory tools now put their best features behind subscriptions: [Mem0](https://mem0.ai/pricing) ($19 to $249/mo), [Zep](https://www.getzep.com/pricing) ($125/mo and up), [Supermemory](https://supermemory.ai/pricing) ($19/mo and up). Some have dropped their free or self-hosted options entirely, like Zep's self-hosted edition and Pieces' free tier. FlowState is MIT-licensed, runs on your own machine, and is free with no paid tier.

*Prices as listed on each vendor's pricing page, October 2026.*

## Add your own features

FlowState is open source and built to be worked on by AI: [AGENTS.md](AGENTS.md) and
[llms.txt](llms.txt) explain the repo, the code is split into small modules, and there's a test
suite that runs against temporary databases. If you think of a feature, have your AI build it.
You're not waiting on a vendor's roadmap.

The v1.1 desktop GUI was an unfinished experiment and has been removed. v2 is used through your AI and the browser review page.

---

## Commands for you (not the AI)

Run with the venv Python: `<venv python> -m flowstate <command>` (or `flowstate <command>` with
the venv active). Add `-h` to any command for its options.

| Command | What it does |
|---|---|
| `review-page` | Review page in your browser (127.0.0.1 only): approve / edit / reject, review mode, pause apps, "Who did what" (with undo) |
| `keys add --name N [--read-only] [--workspace W]` / `keys list` / `keys revoke --id ID` (or `--name N`) | Per-app keys for the local/LAN HTTP server; the key is shown once, only a hash is kept; a revoked key stops at once |
| `apps list` / `apps activity` / `apps pause --app NAME` / `apps resume --app NAME` | Which app did what; stop one app from writing |
| `backup [--offsite]` / `restore <file> [--to <new file>]` | Back up now / restore into a new, verified file |
| `schedule-backups [--time HH:MM] [--offsite]` / `schedule-backups --remove` | Nightly backup on / off |
| `offsite setup --repo OWNER/NAME` / `offsite status` / `offsite push --dry-run` / `offsite disable` | Private GitHub off-site copy |
| `migrate [--dry-run]` | Apply new database migrations (backup first) |
| `cleanup --project NAME [--queue]` | Clean-up suggestions (printed; `--queue` sends them to the review queue) |
| `export-md --project NAME --out FILE` / `import-md --project NAME FILE` | Readable markdown copy; edits come back through the review queue |
| `import-memory --project NAME FILE --source Claude` | A Claude / ChatGPT memory export into the review queue |
| `git-capture enable --project NAME --repo FOLDER` / `disable` / `status` / `run` | Opt-in: commit subjects become changes |
| `meaning status` / `meaning index` | Search by meaning (needs a local model; see Honest limits) |
| `scan-secrets` | List records that look like they hold a secret (values never shown) |
| `token status` / `token migrate --from FILE` | HTTP bearer token in the OS credential store |
| `remote status` / `passphrase` / `add-client --name N [--read-only]` / `clients` / `revoke --client ID` | Remote access: owner passphrase, per-app tokens |

## MCP tools

12 tools by default. Every name starts with a verb; tools that do several things are
`manage_<things>` and take an `action`. The first three are the ones every agent needs.

| Tool | What it does |
|---|---|
| `start_here` | **Call first.** Project brief (`size`: quick / standard / deep; deep is the full context); lists projects when called without one |
| `log_work` | Record a new decision, change, problem, learning or todo (`replaces=`, `source=`, `suggested=`) |
| `search` | Find records by words (and by meaning, if set up); `answer=true` answers a plain question from your records with their ids; no query lists the newest records (`since_hours`) |
| `manage_todos` | list, next, update (done / cancelled), block / unblock; add for subtasks, blockers and due dates |
| `manage_problems` | list, tree, add_attempt, end_attempt, solve |
| `manage_review` | The review queue and review mode: list, get_mode; approve, reject, set_mode only when you ask |
| `manage_handoffs` | Leave / list / pick up "where I was, next step" notes |
| `manage_rules` | A project's hard rules |
| `manage_links` | Link projects, shared components, records and URLs |
| `manage_history` | See edits; undo the last change to a record; `action="who"`: who did what (app, model, machine), with filters |
| `manage_projects` | Projects and components: list, create, update, list_components, add_component, update_component |
| `manage_memory` | Upkeep: cleanup suggestions and memory imports (to the review queue), stale facts, mark checked, set source |

With `FLOWSTATE_TOOLS=full` six specialist tools are listed too: `manage_sessions` (sessions
also run automatically), `manage_files` (file attachments), `manage_data_git` (git for the
FlowState data folder, not your repo), `manage_variables` (non-secret project settings),
`manage_methods` (documented procedures) and `manage_skills` (the AI's learned skills and
saved state).

**Older tool names still work.** The 35 names of v2.0 (`todo`, `link`, `handoff`,
`review_approve`, `list_projects`, `log_learning`, `git`, ...) are not listed any more but
answer exactly as before, with one extra line naming the new tool. The full mapping is in
[docs/TOOL_CONSOLIDATION.md](docs/TOOL_CONSOLIDATION.md).

Over HTTP, only the owner's LAN token may use `manage_data_git` (or `git`) or attach files
from your computer; per-app keys, per-app tokens and OAuth apps are refused.

---

## Honest limits

- **Search by meaning needs a model download.** FlowState never downloads a model or calls a
  cloud API. You install `sentence-transformers` into the venv, put a model (default
  `all-MiniLM-L6-v2`) on disk, set `FLOWSTATE_MEANING_MODEL`, and run `flowstate meaning index`.
  Without it, search (also with `answer=true`) matches words only, so a reworded question can miss.
- **Model names are recorded only when the app supplies them** (the tool's `model` argument,
  the `X-FlowState-Model` header, or `FLOWSTATE_MODEL` for a desktop app). FlowState never
  guesses one; if the app says nothing, the model is left empty.
- **Desktop (stdio) app and machine names are self-reported**, and so are names sent with the
  shared LAN token. They are a convenience, not security (this includes the activity log and the
  pause switch). The machine is `FLOWSTATE_WORKSPACE` or this computer's name, or what the app
  sends. "Who did what" says so in its identity column ("self-reported", "shared token (legacy)").
- **Only per-app keys, per-app remote tokens and approved OAuth apps give an identity an app
  can't pick for itself.** Even then the model, and for a key without a workspace the machine,
  are the app's own say-so. Changes recorded before this was added show "not recorded".
- **"Only when the user asks" is enforced by wording**, not cryptography: approving review items,
  changing the review mode and removing rules require the AI to quote your words, but a
  misbehaving AI could make them up. Hard rules are shown first in every brief; FlowState can't
  stop an AI from doing something outside FlowState.
- **Answers are extractive** (`search` with `answer=true`): it quotes the best-matching sentences of your records; it doesn't write new answers.
- **Tested mainly on Windows and macOS.** Linux should work but is less tested.
- **Remote access is optional and takes setup**: your own domain on Cloudflare, `cloudflared`
  on your computer, and an owner passphrase. The helper launch scripts in `scripts/windows/` are
  Windows-only. FlowState supports OAuth dynamic client registration but not client ID metadata
  documents (CIMD) yet.
- **The secret scanner is pattern-based**: it can refuse harmless text (for example a 64-character
  hex checksum) and miss a short secret without a label like `password=`. It also refuses
  secret values in `variable`. Older records are not re-scanned automatically; run `flowstate scan-secrets`.
- **The off-site copy keeps one backup** (a single commit, replaced each night); history depth
  comes from your local backups. Pushing it means your memory leaves your machine, to your private repo.
- **Undo covers edits made through FlowState.** Creating a record isn't undone (mark it done or
  cancelled instead), and changes made directly in SQLite aren't tracked.
- **Not backed up nightly:** the remote-access settings file (`remote-auth.db`). Losing it only
  means approving your apps again.

---

## Project layout

```
FlowState/
├── setup_flowstate.py      # one-command setup (venv, database, app configs, backups, skill)
├── AGENTS.md               # instructions for AI agents (install + working on the code)
├── llms.txt                # map of the repo for AI tools
├── CHANGELOG.md
├── mcp-server/             # the MCP server (Python package "flowstate")
│   ├── pyproject.toml
│   └── flowstate/
│       ├── server.py       # tools, stdio and HTTP transports
│       ├── tooldefs.py     # tool definitions; agent_guide.py: the wording sent to apps
│       ├── brief.py, autosession.py, guard.py, review.py, review_mode.py, links.py, ...
│       ├── backup.py, offsite.py, scheduler.py, secrets_scan.py, secret_store.py, remote_auth.py
│       ├── app_keys.py, identity.py, who.py # per-app keys, who/model/machine, "Who did what"
│       ├── cli.py, cli_more.py # `flowstate <command>`
│       ├── migrations/     # numbered, additive database migrations
│       └── tools/          # older tool implementations
├── database/schema.sql     # base SQLite schema
├── skills/flowstate/       # the Agent Skill
├── scripts/                # helpers: HTTP check, Cloudflare Tunnel template, Windows launchers
├── docs/                   # REMOTE-ACCESS.md and notes
└── tests/                  # pytest suite (temporary databases only)
```

The database is created at `FLOWSTATE_DB` if set, else Windows
`%LOCALAPPDATA%\flowstate\flowstate.db`, macOS `~/Library/Application Support/flowstate/flowstate.db`,
Linux `~/.local/share/flowstate/flowstate.db`. More settings are listed in [AGENTS.md](AGENTS.md#settings-environment-variables).

## Upgrading from v1.1

Pull the new code and run `setup_flowstate.py` again (or just restart your app). Your existing
database is kept. Before new migrations run on a database with data, FlowState takes an
integrity-checked backup, then it adds new tables, and (migration 012, "who did what") new empty
columns on FlowState's own v2 tables only. v1.1's tables and all existing data are never
changed, so a v1.1 server can still open the same file. You can also run
`<venv python> -m flowstate migrate --dry-run` first to see what would be applied. See
[CHANGELOG.md](CHANGELOG.md) for what's new.

## License

MIT. See [LICENSE](LICENSE).
