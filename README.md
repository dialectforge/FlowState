# FlowState

**A shared project memory for your AI apps. Free, open source, and stored on your own machine.**

FlowState is a local [MCP](https://modelcontextprotocol.io) server. It gives every AI app you
use the same memory of your projects: todos, changes, decisions, problems and lessons learned.
Your AI reads a short brief when it starts work on a project and writes down what happens as it
goes. The next chat (or the next app) picks up where the last one stopped.

Everything lives in one SQLite file on your computer. Your memory is not sent anywhere unless you
turn on an optional feature that does so (an off-site backup to your own private GitHub repo,
remote access through your own Cloudflare Tunnel, or the optional relay). FlowState itself makes
one network call: once a day it asks GitHub whether a newer release is out. It sends nothing
about you or your projects, and `flowstate update check off` turns it off (see "Upgrading").

If FlowState saves you some re-explaining, a [star on GitHub](https://github.com/dialectforge/FlowState) helps others find it.

## Many AIs, one memory, at the same time

Claude, ChatGPT, Cursor and Grok can all use FlowState at once, on your computer, across your network, or from your phone (through the optional relay or your own tunnel). When one AI logs a decision or a change, the others see it the next time they check. Hard rules and handoff notes reach every AI, and every change records which AI made it, so you always know who did what. Work across projects is linked too, so a change to something shared shows up in every project that uses it.

### Several agents at once

When two agents start on the same project at the same time, both can see the same "next" todo
and quietly do the same work. So an agent claims a todo before it starts
(`manage_todos(action="claim", todo_id=4)`, 15 minutes by default, up to 120) and releases it, or
hands it off with a note, when it stops. Other agents see the claim in their brief
(`#4 Fix login (claimed by cursor on mac-mini, expires in 9m)`), `next` skips it and lists it
at the end, and a second claim is refused with the holder's name. Nothing is hidden or locked:
a claim simply runs out if the agent goes away, and it is released when that app's session
ends (quit, ended or idle; the same app reconnecting may take its claim back). Anyone (you included) can still update or
finish a claimed todo; they just get a warning, and `manage_history(action="who")` shows it.

A decision that reads like a current one (and doesn't say it replaces it) is still saved, but
flagged as a *possible* conflict (`D31 vs D12` in the brief) for the agent and then you to settle
in the review queue; FlowState matches words (and meaning, if the local model is installed), it doesn't judge.
When two entries clearly disagree, FlowState also queues a disagreement card: claim A and claim B,
who wrote each, and up to two short evidence snippets. You keep the older, the newer, or both, or
dismiss the card. The one you don't keep is marked superseded, not deleted. An agent flags a pair
with `log_work` (`conflicts_with`, `topic`, `evidence`) or `manage_links(action="flag_conflict")`.
The review page shows the card (side by side on a wide screen), and the tool reply includes the
same card as plain text plus the review link. Chat apps that support MCP Apps can show the card
in the chat; its buttons are not yet cryptographically proven to be a person.

Each window of an app gets its own session, and the brief opens with one line about what the
others did meanwhile (`Since your last visit (Oct 19): Claude Desktop and Grok saved 3 decisions,
5 changes, 2 todos.`), and nothing when nothing changed.

<p align="center"><a href="docs/media/flowstate-side-by-side.mp4"><img src="docs/media/flowstate-side-by-side.gif" width="520" alt="Claude and Grok on an iPhone, side by side, both reading the same FlowState project brief and both knowing what was just shipped from the PC"></a></p>

<p align="center"><em>Worked on my PC with Grok Build, then asked Claude and Grok on my iPhone. Both knew what I did last. (Click for the full-quality video.)</em></p>

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
8. puts a "FlowState review" shortcut on your desktop that opens the review page
   (`flowstate review-page`, this computer only). It never replaces a file of that name it didn't make;
9. with `--verify`, starts the server the way your app will and calls `start_here` to check it answers.

At the end it tells you about the daily update check and how to turn it off.

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
if you want the test tools or manage the venv yourself (then add `--skip-install`). A plain,
non-editable `pip install mcp-server` (or of a built wheel) works too from 2.4.1 on; in 2.4.0 it
left out the database schema, so every FlowState call failed, and the failed start left an empty
database file behind. 2.4.1 sets up such an empty file as a new database, so reinstalling is
enough. `flowstate update` needs the git clone set up by `setup_flowstate.py`. Config
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
- **Write receipts**: every write's answer says what it saved (`receipt: {change: "3f9a1d2c4b5e",
  saved: ["decision #31"]}`). A write that timed out but saved a moment later is reported on the
  next answer, and the same write repeated within 2 minutes returns the first receipt instead
  of saving a copy.
- **Automatic sessions**: a session opens on the first call for a project (one per app window
  or connection) and closes after 2 hours idle (or when the app quits), with a short summary
  marked `[auto-captured, needs review]`. A small housekeeping session (only reads, undo,
  review decisions, a handoff pickup or todo claims) isn't queued for review. FlowState keeps a lean record of each session's
  reads and writes (record ids and hashes, never the text).

**You stay in charge**
- **Review queue**: auto-captured summaries, and anything the AI marks as a suggestion, wait
  for your OK. Approve, edit or reject them in chat, on the review page, or with
  `flowstate approve <id>` in your terminal. The app that queued an item needs your own words
  to decide it. An AI that reaches FlowState through the relay can't approve or reject anything
  in the chat: it gives you the item's phone link instead (see "Chat approvals through the
  relay" below).
- **Review mode** (yours to set): `off`, `auto_only` (default), chosen record types, or
  `everything`. Set it for the whole install or per project. The AI may change it only when you
  ask in the chat, and every change is logged with your words. With approvals on, turning review
  off also waits for your own click or key.
- **Hard rules**: "never do this" rules per project, always shown in full at the top of the brief.
- **Handoff notes**: an AI that has to stop mid-task leaves "where I was / next step"; the next
  AI sees it first until it picks it up.
- **Undo and history**: edits made through FlowState are kept; you can see them and undo the
  last change to a record (an undo can itself be undone). The app that created a record can
  take it back with undo while nothing refers to it yet. If part of a change was changed again
  since, undo puts back the rest and lists what it skipped and why.
- **Deletes you can always take back**: out of the box FlowState never interrupts you. When an
  AI deletes a project, a component or a record, FlowState first takes a safety copy and keeps
  every removed row (children and links too) in the edit history, so undo brings all of it
  back exactly. The answer and the brief say in one line what went and how to undo it.
- **Approvals, if you want them** (`flowstate approvals on`; off by default, also after an
  upgrade): then an AI's delete, and turning review off, wait for you, and no app can pass your
  OK along. You decide in the app's own popup (Claude Code, Cursor, VS Code, when they connect
  directly and not through the relay or the internet), on your phone (only if you use the
  optional FlowStateMCP hosted relay: you sign in at flowstatemcp.com, your phone talks to your
  own FlowState, and the site never sees the item), or with `flowstate approve <id>` in your
  terminal. Every decision is logged with how it was made. With approvals off, an AI can delete
  without asking you; the safety copy and undo are what protect you then.
- **Chat approvals through the relay: phone only, unless you allow them**
  (`flowstate relay chat-approvals on|off`; off by default). An AI connected through the
  optional relay (claude.ai, ChatGPT and other cloud AIs) can't approve or reject review items
  in the chat, even when you tell it to there: its answer says nothing was decided and gives
  the item's phone link, so you decide it yourself on your phone (or on the computer where
  FlowState runs). Turn it on in a terminal on that computer (it asks you to type `y`) and
  those AIs can approve and reject again when they quote your words, as before. Either way,
  the items that are yours alone stay yours alone: deletes and turning review off while
  approvals are on, and "did you say this yourself?" checks. Apps that connect directly (on
  this computer or your own network) are not affected. There is no tool for this setting, so
  an AI can't switch it on through FlowState.
- **Superseded decisions**: a new decision can name the one it replaces, so only the current
  one shows in the brief.
- **Source and last-verified flags**: records can say where they came from (`user_stated`,
  `ai_inferred`, `from_code`), and FlowState stores who set that tag and when (the app, the
  model if the app gave it, or "user" when you confirmed it). Facts like paths, ports and config
  not checked for 60 days are listed for re-checking.
- **Only you turn a guess into "you said so"**: an app may tag what it writes as
  `user_stated` when it quotes you, and the brief then says `(user, via <app>)`: that is the
  app's claim. Once you confirm it yourself it says `(user, confirmed by the user)`, and
  `search` and `manage_history` show who set the tag and when. Changing an
  existing record to `user_stated` is yours alone, approvals on or off: the app's request
  leaves the tag as it is and queues one review item, which you confirm on the review page or
  with `flowstate approve <id>` (with approvals on, also in the popup or on your phone). No app
  can confirm it for you. A `user_stated` write that repeats another app's `ai_inferred` record
  is saved as `ai_inferred`. What this doesn't cover: FlowState can't check that an app
  quoting you at log time is telling the truth; the tag then names that app.

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
- **Clean-up suggestions**: duplicate and stale todos, and records filed under the wrong
  project, sent to the review queue as one plain question each (`Move learning "..." from A to
  B? It mentions B.`). It only suggests a move when the record names the other project or one
  of its parts, never a guess it isn't sure of. Unsure move ideas left over from older versions
  are withdrawn (no record moves). Nothing moves until you approve.

**Your controls (command line and browser)**
- **Local review page**: a small page in your browser, on this computer only: review queue,
  review mode, pause apps, "Who did what". Click the link in your AI's answer (see "Where to see
  what's waiting for you"), double-click the "FlowState review" shortcut setup puts on your
  desktop, or run `flowstate review-page` (one-time link).
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
- **Safety copies**: right before a delete, an approved clean-up or markdown edit, and before a
  restore or a migration, FlowState takes a verified copy that is kept at least 7 days whatever
  the limits above say; if it can't, the change isn't made. This doesn't depend on nightly
  backups, and FlowState never adds a daily copy you didn't ask for.
- **Health check**: on start, a quick check of the database. Only a problem shows, first in the
  brief, with the newest good backup. FlowState never repairs anything by itself.
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

Many AI memory tools now put their best features behind subscriptions: [Mem0](https://mem0.ai/pricing) ($19 to $249/mo), [Zep](https://www.getzep.com/pricing) ($125/mo and up), [Supermemory](https://supermemory.ai/pricing) ($19/mo and up). Some have dropped their free or self-hosted options entirely, like Zep's self-hosted edition and Pieces' free tier. FlowState is MIT-licensed and runs on your own machine. Local FlowState is free and MIT forever. An optional paid relay and hosting service is at [flowstatemcp.com](https://flowstatemcp.com); paid plans charge for hosting, never for features.

*Prices as listed on each vendor's pricing page, October 2026.*

## Add your own features

FlowState is open source and built to be worked on by AI: [AGENTS.md](AGENTS.md) and
[llms.txt](llms.txt) explain the repo, the code is split into small modules, and there's a test
suite that runs against temporary databases. If you think of a feature, have your AI build it.
You're not waiting on a vendor's roadmap.

The v1.1 desktop GUI was an unfinished experiment and has been removed. v2 is used through your AI and the browser review page.

---

## Where to see what's waiting for you

Some things wait for your OK: session summaries FlowState wrote on its own, records an AI
suggested, clean-up ideas, "did you say this yourself?" checks, and (with approvals on) an AI's
deletes. You don't need a terminal to see and decide them. Nothing pops up unless you ask, or
you have turned approvals on.

1. **In the chat (everyone).** Ask your AI "show me what's waiting for me". It calls
   `manage_review(action="walk")`. In Claude Code, Cursor and VS Code (the apps FlowState knows
   show a real dialog, the same list as the approvals popup), each item comes up one at a time
   as a box: tick **Approve** and submit to approve, submit it unticked to reject (the optional
   note is the reason), decline to skip it, close the box to stop. FlowState acts only on your
   answer in that box; your AI can't answer for you. This works with approvals off too, because
   you asked. In other apps, and in any app that reaches FlowState through the relay or over
   the internet, there are no boxes: the AI gets a plain list (20 at a time, oldest first, and
   how to ask for the next 20) with the links below to show you, and FlowState says plainly
   that nothing was decided. If an app "answers" a box faster than a person could (under a
   second), FlowState takes it that you never saw the box: it stops, decides nothing and shows
   the plain list instead.
2. **A link to the review page on the computer where FlowState runs (apps on that computer).**
   When something waits, and your AI app runs on the same computer as FlowState, the brief
   your AI reads, and the walk's answer, include a link like `http://127.0.0.1:8770/`. Click it
   and the review page opens; no command needed. FlowState starts the page itself the first
   time a link is needed, one per computer (another FlowState on the same computer reuses it),
   on 127.0.0.1 only; if 8770 is taken it picks a free port. `FLOWSTATE_REVIEW_PORT` sets the
   port (`0` any free one, `off` no page or link). `flowstate review-page` still works as before,
   and so does the "FlowState review" shortcut `setup_flowstate.py` puts on your desktop (it runs
   that command; if FlowState already has the page open, it opens that one).

   `127.0.0.1` always means "this computer", so on any other computer that link would open
   that computer itself, not yours. FlowState therefore gives it only to apps it can see are on
   its own computer: apps it starts directly, and apps on its local network connection that
   connect from the same computer. Apps on another computer (your Mac through the relay, or
   another computer on your network) never get it. They get your phone link (3) if you have
   the relay, and otherwise a plain note: ask your AI to show what's waiting (it shows the
   list in the chat), or open the review page on the computer where FlowState runs (the
   FlowState review shortcut, or `flowstate review-page`), or the tunnel review page (4) if you
   have your own tunnel. FlowState decides this from the connection itself, never from anything
   the app says about itself.
3. **On your phone (relay users).** If the relay agent is set up, the brief and the walk's
   answer also have your own phone link, `https://flowstatemcp.com/review/<handle>` (your
   handle is your computer's relay name). It never changes, so you can bookmark it or add it
   to your home screen. When you tap it:
   - flowstatemcp.com asks you to sign in, if you aren't already, and checks the computer is
     yours;
   - it asks your own FlowState for a fresh one-time code (with a 5-minute signed ticket only
     the site can make, so nobody else can use up your hourly limit), then shows a **Continue**
     button;
   - tap Continue: your phone goes to your own FlowState (`https://<handle>.fsmcp.com/review`)
     with a 5-minute signed pass. Your FlowState checks it with the site's public key (pinned
     in this release), uses up the one-time code, and shows the waiting list with Approve /
     Reject on each item (a 15-minute session; the oldest 50 at a time). The site never sees
     what's in it.

   `flowstate approvals phone off` turns every phone link off.
4. **Through your own tunnel (no relay).** If you run the public listener behind your own
   tunnel (docs/REMOTE-ACCESS.md), open `https://<your tunnel>/review/tunnel` in any browser and
   sign in with the owner passphrase. It is the same review page as on your computer, sized for
   a phone, and decides everything that page can, the user-only items too. Your AI apps' tokens
   don't open it; only the passphrase does.

Who gets which: an app on the computer where FlowState runs gets 1 and 2 (and 3 with the
relay); an app on another computer gets 1, and 3 with the relay. Every decision made in
the chat walk, on the phone list or on the tunnel page is logged with how it was made (`chat`,
`phone` or `tunnel page`); see `manage_history(action="list")`.

The phone list (3), on an iPhone:

<table>
  <tr>
    <td><img src="docs/images/review-waiting.png" width="280" alt="The FlowState phone page titled Waiting for you, showing a learning an AI saved for review with Approve and Reject buttons"></td>
    <td><img src="docs/images/review-approved.png" width="280" alt="The FlowState phone page after approving: Nothing is waiting for you, Approved #129"></td>
  </tr>
  <tr>
    <td><em>An item an AI saved for review, waiting on your phone (FlowStateMCP relay).</em></td>
    <td><em>After you approve an item.</em></td>
  </tr>
</table>

Honest limits:
- The review page answers only on 127.0.0.1 and only to same-origin form posts carrying the
  page's CSRF token, so other websites can't use it. But a program on your own computer (an AI
  that runs commands or drives your browser) can open `http://127.0.0.1:<port>/` too, just as
  it could run `flowstate approve`.
- The chat walk's boxes are only as trustworthy as the app that shows them. FlowState only
  shows them to apps it knows show a real dialog, connected directly (on this computer or your
  own network, not through the relay or the internet), and treats an answer
  under a second as "not seen".
- The phone link is protected by flowstatemcp.com's sign-in and its signing key: anyone can
  get your FlowState to make a one-time code, but only the site can turn it into a pass, and
  only for you. If the site's signing key ever leaked, someone could open your list on a
  reachable computer (approving a single item from its own link still needs that item's code
  from your computer). `flowstate approvals phone off` turns the phone list off.
- If your computer is off or the relay agent isn't running, the phone link can't open the
  list.

## Commands for you (not the AI)

Run with the venv Python: `<venv python> -m flowstate <command>` (or `flowstate <command>` with
the venv active). Add `-h` to any command for its options.

| Command | What it does |
|---|---|
| `review [--project NAME]` | List what waits in the review queue |
| `approve ID [ID ...]` / `reject ID [--reason TEXT]` | Decide review items; asks you to type `y` in this terminal (piped input doesn't count) |
| `approvals [on\|off] [--project P]` | Opt-in approvals (off by default). `on`: an AI's deletes and turning review off wait for you. Turning them `off` asks for `y` in this terminal. Also `approvals phone on\|off` and `approvals popup add\|remove NAME` |
| `relay chat-approvals [on\|off]` | May AIs connected through the relay approve and reject review items in the chat? Off by default (phone only). Turning it `on` asks for `y` in this terminal; with no argument it shows the setting |
| `review-page` | Review page in your browser (127.0.0.1 only): approve / edit / reject, review mode, pause apps, "Who did what" (with undo). You can also just click the link in your AI's answer (see "Where to see what's waiting for you") |
| `keys add --name N [--read-only] [--workspace W]` / `keys list` / `keys revoke --id ID` (or `--name N`) | Per-app keys for the local/LAN HTTP server; the key is shown once, only a hash is kept; a revoked key stops at once |
| `apps list` / `apps activity` / `apps pause --app NAME` / `apps resume --app NAME` | Which app did what; stop one app from writing |
| `backup [--offsite]` / `restore <file> [--to <new file>]` | Back up now / restore into a new, verified file |
| `schedule-backups [--time HH:MM] [--offsite]` / `schedule-backups --remove` | Nightly backup on / off |
| `offsite setup --repo OWNER/NAME` / `offsite status` / `offsite push --dry-run` / `offsite disable` | Private GitHub off-site copy |
| `migrate [--dry-run]` | Apply new database migrations (backup first) |
| `update --dry-run` / `update` / `update --rollback` | Update to the newest signed release (dry run first; asks for `y`; rolls back on any failure). See "Upgrading" |
| `update check` / `update check on\|off\|now` | The daily update check: show the last result, turn it on or off, or check now |
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
| `manage_todos` | list, next, update (done / cancelled), block / unblock; add for subtasks, blockers and due dates; claim / renew / release / handoff so several agents don't do the same todo |
| `manage_problems` | list, tree, add_attempt, end_attempt, solve |
| `manage_review` | The review queue and review mode: list (one line per item; `item_id` for one in full), get_mode; walk (when you ask to see what's waiting: you decide each item in a box, or you get the links); approve, reject, set_mode only when you ask |
| `manage_handoffs` | Leave / list / pick up "where I was, next step" notes |
| `manage_rules` | A project's hard rules |
| `manage_links` | Link projects, shared components, records and URLs |
| `manage_history` | See edits; undo the last change to a record; `action="who"`: who did what (app, model, machine), with filters |
| `manage_projects` | Projects and components: list, create, update, list_components, add_component, update_component; delete / delete_component only when you ask (undoable; with approvals on they wait for you) |
| `manage_memory` | Upkeep: cleanup suggestions and memory imports (to the review queue), stale facts, mark checked, set source; delete one record when you ask (undoable; with approvals on it waits for you) |

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
from your computer, or ask for or approve a delete; per-app keys, per-app tokens and OAuth
apps are refused.

---

## Optional: reach FlowState from anywhere

Want claude.ai, ChatGPT, Cursor or your phone to reach the FlowState on your own computer?
The optional relay gives it an address like `https://<handle>.fsmcp.com/mcp`. Traffic is
encrypted end to end (TLS passthrough), so the relay can't read it, and your data stays on
your machine. Get a code at [flowstatemcp.com/account](https://flowstatemcp.com/account), then
run `flowstate relay enroll --code <code>`. The relay is in a 10-spot beta. Step-by-step guide:
[flowstatemcp.com/setup](https://flowstatemcp.com/setup).

**Local FlowState stays free and MIT-licensed. It needs no account.** Local FlowState makes one
network call: once a day it asks GitHub whether there is a newer release. It sends nothing about
you or your projects (only what any web request reveals: your IP address). Turn it off with
`flowstate update check off` (or `FLOWSTATE_UPDATE_CHECK=0`); off means no request at all. The
relay is off by default. Nothing in it runs until you enroll.

### How the relay works

If you want claude.ai, ChatGPT or Cursor to reach the FlowState on your computer, and you don't
want to set up your own domain and Cloudflare Tunnel ([docs/REMOTE-ACCESS.md](docs/REMOTE-ACCESS.md)),
the relay agent connects out to `tunnel.fsmcp.com`, an optional hosted service by
DialectForge on its own domain, `fsmcp.com`. Your apps then use `https://<handle>.fsmcp.com/mcp`.

The relay can't read your traffic. The TLS key for your address is made on your computer and
never leaves it. Your computer gets its own Let's Encrypt certificate (the relay only publishes
the DNS check value), and it ends every encrypted connection itself. The relay just passes the
encrypted bytes through. Sign-in is the same OAuth + passphrase consent page as above, served by
your FlowState. A daily Certificate Transparency check warns you loudly if anyone else ever
gets a certificate for your address.

```bash
<venv python> -m flowstate remote passphrase              # once, if not set yet
<venv python> -m flowstate relay enroll --code <one-time code>
<venv python> -m flowstate relay run                      # keep it running
<venv python> -m flowstate relay status                   # handle, URL, certificate, tunnel, CT check
<venv python> -m flowstate relay disable                  # stop (keeps keys); `enable` to turn back on
<venv python> -m flowstate relay forget                   # delete the device token, keys and certificate
<venv python> -m flowstate relay chat-approvals           # may relay AIs approve in the chat? (off: phone only)
```

During the beta the relay has room for 10 computers, one per account. If it is full,
`relay enroll` says so in plain words with your place in line ("The FlowState Relay beta is
full right now. You're on the waitlist (number N). ...") and saves nothing. Places are first come, first served: when there is room, it is held for the
oldest code in line for 48 hours. Your code stays valid for 7 days after that, so just run
the same command again.

The relay uses the Python package `cryptography`, which comes with `mcp` today. The wire
protocol is in [docs/relay-protocol.md](docs/relay-protocol.md).

---

## Honest limits

- **Search by meaning needs a model download.** For this, FlowState never downloads a model or
  calls a cloud API. You install `sentence-transformers` into the venv, put a model (default
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
  misbehaving AI could make them up. With approvals off (the default) that includes deletes:
  they go through at once, and what protects you is the safety copy and undo. With approvals
  on, an AI's deletes and turning review off need your own click or key (popup, phone or
  terminal), whatever words it passes. That stops an AI approving its own work through
  FlowState; an AI that controls your own logged-in browser or your terminal window could still
  get through, and the popup is only as trustworthy as the app that shows it. Through the
  relay, quoting your words isn't enough by default: an AI there can't approve or reject
  anything in the chat unless you run `flowstate relay chat-approvals on`. Hard rules are
  shown first in every brief; FlowState can't stop an AI from doing something outside FlowState.
- **Phone approvals and the phone list need the relay and trust flowstatemcp.com's sign-in.**
  They only exist if you use the optional FlowStateMCP hosted relay. This release pins the
  site's two public keys (`fsa-1`, `fsa-2`). The site vouches for who signed in but never sees
  the items. If the site's signing key ever leaked, someone could open your waiting list (with
  its Approve and Reject buttons) on a reachable computer; approving a single item from its own
  link would still need that item's one-use code from your computer. `flowstate approvals phone
  off` turns the phone links off.
- **The daily update check is a network call.** It asks api.github.com, with no login, and sends
  nothing about you or your projects, but GitHub sees your IP address. `flowstate update check
  off` or `FLOWSTATE_UPDATE_CHECK=0` stops it. Nothing updates on its own: `flowstate update`
  runs only when you start it (or your AI does, after you asked).
- **`flowstate update` is careful, so it often says no.** It needs a plain clone with no local
  changes and a real `.venv`, and `ssh-keygen` to check the release signature. On Windows a
  release that changes the Python packages is refused; update that one by hand.
- **Long details are refused, not cut.** `log_work` details over 10,000 characters get a clear
  `too_long` answer and nothing is saved.
- **The local review page has no password.** It answers only on 127.0.0.1, checks the Host and
  Origin headers and needs the page's CSRF token for every change, so websites can't use it; a
  program running on your computer can still open it (see "Where to see what's waiting for you").
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
- **Undo covers changes made through FlowState.** Creating a record can be undone only by the
  app that made it and only while nothing refers to it (records created before 2.4, projects
  and components can't; delete them instead). Changes made directly in SQLite aren't tracked.
- **Receipts are per connection.** A repeated write is recognised only from the same app window
  within 2 minutes and while the server keeps running; a late-save notice reaches the
  connection that made the write, not another one.
- **A delete is recoverable, not an erasure.** The deleted rows stay in the edit history (that is
  what undo uses) and in your backups, so the text is still in the database file. There is no
  "erase for good" yet. Deleting a file attachment keeps the file itself.
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
│       ├── approvals.py    # opt-in approvals (off by default)
│       ├── updates.py, selfupdate.py # daily update check, `flowstate update`
│       ├── cli.py, cli_more.py # `flowstate <command>`
│       ├── migrations/     # numbered, additive database migrations
│       ├── schema.sql      # the base schema the code reads (same as database/schema.sql)
│       └── tools/          # older tool implementations
├── database/schema.sql     # base SQLite schema (kept the same as mcp-server/flowstate/schema.sql)
├── skills/flowstate/       # the Agent Skill
├── scripts/                # helpers: HTTP check, Cloudflare Tunnel template, Windows launchers
├── docs/                   # REMOTE-ACCESS.md and notes
└── tests/                  # pytest suite (temporary folders only; guard_site/ refuses real data)
```

The database is created at `FLOWSTATE_DB` if set, else Windows
`%LOCALAPPDATA%\flowstate\flowstate.db`, macOS `~/Library/Application Support/flowstate/flowstate.db`,
Linux `~/.local/share/flowstate/flowstate.db`. More settings are listed in [AGENTS.md](AGENTS.md#settings-environment-variables).

## Upgrading

FlowState (2.4.0 and later) checks once a day whether a newer release is out and, if so, puts
one line on top of the brief; your AI tells you once. Nothing updates on its own.
`flowstate update check` shows the last result, `flowstate update check now` checks at once, and
`flowstate update check off` (or the setting `FLOWSTATE_UPDATE_CHECK=0`) turns the check off
completely: no request, no line. `flowstate update check on` turns it back on.

**From 2.4.0 on: `flowstate update`.** Ask your AI to "update FlowState". It runs
`<venv python> -m flowstate update --dry-run` in a terminal on this computer and shows you the
plan, then `<venv python> -m flowstate update --yes --user-said "<your words>"`. Or run
`<venv python> -m flowstate update` yourself: it asks you to type `y` in your terminal. It:

1. refuses, and changes nothing, unless this folder is a plain clone of
   `github.com/dialectforge/FlowState` with no local changes, at a release tag or on `main`
   with no commits of your own, with one worktree and a real `.venv` folder;
2. downloads only the newest release's tag and checks that it is signed with a FlowState release
   key listed in the copy you already have (`mcp-server/flowstate/release_signers`). An unsigned
   tag, an unknown key, a missing `ssh-keygen` or any check error: nothing is changed;
3. backs up your database (labelled `pre-update`, kept at least 7 days);
4. stops this folder's FlowState services (scheduled tasks, LaunchAgents or systemd user units
   that run from this folder; your AI apps are listed, not stopped), moves the code to the new
   tag (a fresh `.venv` only when the release changes the Python packages, and then every app
   must be quit first; on Windows such a release is refused up front, because the command runs
   from that `.venv`: update it by hand), migrates the database, starts the services again and
   checks them;
5. if anything in step 4 fails, it puts the old version back on its own: the same commit and
   branch, the same `.venv`, the old services. Your database is never put back or overwritten
   (migrations only add tables and columns, so the old version reads it as it is).

Then quit and reopen your AI apps; until you do, an app still running the old version says so
in its brief. `flowstate update --rollback` goes back to the version before the last update.
A developer setup (your own branch or commits, extra worktrees, a linked `.venv`) is refused:
update it by hand. To turn `flowstate update` off on a computer, put `"self_update": false` in
FlowState's `config.json` (or a `.flowstate-no-self-update` file in the folder).

**By hand (and to install 2.4.0 or later the first time).** In the FlowState folder run `git pull`, then
`python setup_flowstate.py --clients <your apps> --verify` with the same options as when you
set it up, then quit and reopen your AI apps. 2.4.0, the first release that has
`flowstate update`, has to be installed this way; if you don't have 2.4.0 yet, install the
newest release (2.4.2) the same way. From 2.4.0 or 2.4.1 to 2.4.2 `flowstate update` works,
on Windows too (2.4.2 needs no new `.venv`). (If your folder is at a release tag instead
of `main`, `git pull` says you are not on a branch: run `git fetch --tags` and
`git checkout v2.4.2` instead.)

### From v1.1

Pull the new code and run `setup_flowstate.py` again (or just restart your app). Your existing
database is kept. Before new migrations run on a database with data, FlowState takes an
integrity-checked backup, then it adds new tables, and (migration 012, "who did what") new empty
columns on FlowState's own v2 tables only. v1.1's tables and all existing data are never
changed, so a v1.1 server can still open the same file. You can also run
`<venv python> -m flowstate migrate --dry-run` first to see what would be applied. See
[CHANGELOG.md](CHANGELOG.md) for what's new.

## License

MIT. See [LICENSE](LICENSE).
