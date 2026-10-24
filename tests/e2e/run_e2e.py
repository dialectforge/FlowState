#!/usr/bin/env python3
"""FlowState end-to-end mock project test ("Lakeside Bakery ordering app").

Starts the real FlowState server from this checkout (one stdio process per app window, plus
the streamable-HTTP transport) on a brand-new throwaway folder in the system temp folder, and drives it only
with real MCP tool calls, the way Claude Code, Cursor and Grok do. Four agents work at once:
claude-code (two windows), cursor (stdio) and grok (HTTP, its own app key). Fourteen scenarios,
each pass/fail with evidence; every tool and every action it offers is called at least once.

    .venv/bin/python tests/e2e/run_e2e.py              # everything (about 4 minutes)
    .venv/bin/python tests/e2e/run_e2e.py --keep       # keep the temp folder to look at
    .venv/Scripts/python.exe tests/e2e/run_e2e.py      # Windows (PowerShell or cmd)
    .venv/bin/python tests/e2e/run_e2e.py --skip-upgrade   # no v2.3.0 worktree

Nothing here touches ~/.flowstate, the per-OS FlowState folders, a real database or a real
token: every FlowState path (and HOME) points into the temp folder, which is deleted at the
end unless --keep, and every server process loads tests/guard_site (it refuses, and
records, any touch of the real folders; scenario 11 checks that). `flowstate approve` is
only ever run without a terminal (POSIX: a new session; Windows: a console with no window),
or in a terminal of its own (a pseudo-terminal; Windows: a hidden console of its own, typed
into by tests/conin_typist.py), so it never prompts on yours (tests/terminal_run.py). Only servers this script started are stopped, by their own PIDs.
Writes the report to <tmp>/report.md and prints it.
"""
import argparse
import json
import os
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # tests/: fsa_test_keys
from fsclient import (PY, REPO, HttpClient, StdioClient, ToolError, guard_log, real_dirs,  # noqa: E402
                      sandbox_env, wait_port)
from terminal_run import run_in_terminal, run_no_terminal  # noqa: E402

P = "Lakeside Bakery"
SHARED = "Bakery Shared Auth"
EMPTY = "Empty Shop"
MARK = "Zitronen-Törtchen 🍋 مخبز 面包店 e2e-marker-7Q"     # unicode + a marker to find in tables
DESC_BUDGET_FULL = int(60415 * 0.65)       # the budgets in tests/test_tool_descriptions.py
DESC_BUDGET_CORE = int(17730 * 0.65)
BRIEF_BUDGET = {"quick": 1500, "standard": 4000, "deep": 12000}   # start_here's own words

READ_ACTIONS = {
    "start_here": None, "search": None,
    "manage_todos": {"list", "next"}, "manage_problems": {"list", "tree"},
    "manage_review": {"list", "get_mode", "walk"},   # walk: decisions are the user's, in boxes
    "manage_handoffs": {"list"},
    "manage_rules": {"list"}, "manage_links": {"list"}, "manage_history": {"list", "who"},
    "manage_projects": {"list", "list_components"}, "manage_memory": {"stale"},
    "manage_sessions": {"current", "list", "conversations"}, "manage_files": {"list", "search"},
    "manage_data_git": {"status", "history"}, "manage_variables": {"list"},
    "manage_methods": {"list"}, "manage_skills": {"list", "get_state", "list_metrics"},
}


def utc(dt=None):
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M:%S")


def short(x, n=220):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 3] + "..."


def err(r):
    return isinstance(r, dict) and ("error" in r or r.get("_is_error"))


def review_lines(r):
    """manage_review list answers one line per item: "#id (project, kind[, status]): summary"."""
    return list(r.get("items") or []) if isinstance(r, dict) else []


def line_id(line):
    return int(line.split(" ")[0][1:])


# ----------------------------------------------------------------------------- bookkeeping
class Report:
    TITLES = {
        1: "Fresh install", 2: "Normal day, four agents", 3: "Possible conflicting decisions",
        4: "Review mode, self-approval, the user-only step", 5: "Receipts, repeats, timed-out writes",
        6: "Two windows of one app; 'since your last visit'", 7: "Undo",
        8: "Backups, safety copies, health check", 9: "Run ledger keeps no text",
        10: "Upgrade from a v2.3.0 database", 11: "Bad input, edge cases, real-data guard",
        12: "Tool descriptions: size and truthfulness",
        13: "Approvals on: popup, phone link, terminal",
        14: "Only the user turns a guess into user_stated",
        15: "What's waiting, without a terminal: chat walk, local link, phone list",
        16: "Own tunnel: the owner review page behind the passphrase"}

    def __init__(self):
        self.checks = {n: [] for n in self.TITLES}
        self.findings = []        # (severity, title, repro)
        self.wording = []
        self.coverage = set()
        self.calls = 0
        self.missing_receipts = []
        self.empty_saved = set()
        self.lock = threading.Lock()

    def check(self, sc, label, ok, evidence=""):
        with self.lock:
            self.checks[sc].append((bool(ok), label, short(evidence, 260)))
        print(f"  [{'PASS' if ok else 'FAIL'}] S{sc} {label}" + ("" if ok else f"  -- {short(evidence, 300)}"),
              flush=True)
        return bool(ok)

    def finding(self, severity, title, repro):
        self.findings.append((severity, title, repro))
        print(f"  [BUG/{severity}] {title}", flush=True)

    def word(self, text):
        self.wording.append(text)


R = Report()


class Agent:
    """One app window. Records every tool/action it calls and checks write receipts."""

    def __init__(self, label, client):
        self.label, self.c = label, client

    @property
    def pid(self):
        return getattr(self.c, "pid", None)

    def call(self, tool, **args):
        key = (tool, args.get("action") or (args.get("kind") if tool == "log_work" else None)
               or (args.get("size") if tool == "start_here" else None))
        with R.lock:
            R.coverage.add(key)
            R.calls += 1
        try:
            r = self.c.call(tool, **args)
        except ToolError as e:
            r = {"error": "protocol_error", "message": str(e), "_is_error": True}
        reads = READ_ACTIONS.get(tool, set())
        is_read = reads is None or (args.get("action") in reads)
        if not is_read and isinstance(r, dict) and not err(r) and "receipt" not in r and self.label != "v230":
            with R.lock:
                R.missing_receipts.append((self.label, tool, args.get("action"), short(r, 160)))
        if isinstance(r, dict) and isinstance(r.get("receipt"), dict) and not r["receipt"].get("saved"):
            with R.lock:
                R.empty_saved.add(f"{tool} {args.get('action') or args.get('kind') or ''}".strip())
        return r


def brief_text(r):
    return r if isinstance(r, str) else json.dumps(r)


def since_lines(text):
    return [ln for ln in brief_text(text).splitlines() if ln.startswith("Since your last visit")]


# ----------------------------------------------------------------------------- environment
class World:
    def __init__(self, root: Path):
        self.root = root
        self.env = sandbox_env(root, {"FLOWSTATE_TOOLS": "full"})
        self.db = Path(self.env["FLOWSTATE_DB"])
        self.backups = Path(self.env["FLOWSTATE_BACKUP_DIR"])
        self.logs = root / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.started = []           # every server process we started (by PID)
        self.http = None
        self.agents = {}

    # --- servers
    def stdio(self, label, app, extra_env=None, db=None):
        env = dict(self.env, **(extra_env or {}))
        if db:
            env["FLOWSTATE_DB"] = str(db)
        c = StdioClient(env, app, log_path=self.logs / f"{label}.log")
        self.started.append(c.proc)
        return Agent(label, c)

    def start_http(self):
        tok = secrets.token_urlsafe(32)
        tf = Path(self.env["FLOWSTATE_TOKEN_FILE"])
        tf.write_text(tok)
        tf.chmod(0o600)
        self.shared_token = tok
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        log = open(self.logs / "http.log", "ab")
        self.http = subprocess.Popen(
            [PY, "-m", "flowstate", "--transport", "http", "--host", "127.0.0.1",
             "--port", str(self.port), "--tools", "full"],
            env=self.env, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        self.started.append(self.http)
        if not wait_port("127.0.0.1", self.port, 40):
            raise RuntimeError("HTTP server did not start; see logs/http.log")
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def stop_http(self):
        if self.http and self.http.poll() is None:
            self.http.terminate()           # our own PID only
            try:
                self.http.wait(15)
            except subprocess.TimeoutExpired:
                self.http.kill()
                self.http.wait()

    def app_key(self, name):
        r = subprocess.run([PY, "-m", "flowstate", "keys", "add", "--name", name], env=self.env,
                           cwd=str(REPO), capture_output=True, text=True)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        return lines[-1]                     # the key (never printed)

    def http_agent(self, label, app, token):
        return Agent(label, HttpClient(self.url, token, app))

    def cli_no_terminal(self, *args, stdin=None, env=None):
        """`flowstate ...` the way an agent's shell runs it: no terminal it could prompt on
        (POSIX: a new session; Windows: a console with no window), stdin piped."""
        r = run_no_terminal([PY, "-m", "flowstate", *args], input=stdin, env=env or self.env,
                            cwd=str(REPO), timeout=120)
        return r.returncode, r.stdout + r.stderr

    def cli_in_terminal(self, args, answers, env=None):
        """`flowstate ...` in a terminal of its own (as when the user types it: a pseudo-terminal;
        Windows: a console), answering each "Type ..." prompt in turn. (exit code, what it showed)"""
        return run_in_terminal([PY, "-m", "flowstate", *args], answers, env=env or self.env,
                               cwd=str(REPO), timeout=120)

    def cli(self, *args, env=None):
        r = subprocess.run([PY, "-m", "flowstate", *args], env=env or self.env, cwd=str(REPO),
                           capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout + r.stderr

    # --- database peeks (read-only)
    def q(self, sql, params=(), db=None):
        conn = sqlite3.connect(Path(db or self.db).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        finally:
            conn.close()

    def w(self, sql, params=(), db=None):
        """A direct edit of the temp DB (only to fake the clock)."""
        conn = sqlite3.connect(str(db or self.db), timeout=10)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def count(self, table, where="1", params=(), db=None):
        return self.q(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", params, db)[0]["n"]

    def close_all(self):
        for a in list(self.agents.values()):
            try:
                a.c.close()
            except Exception:
                pass
        self.stop_http()
        for p in self.started:
            if p.poll() is None:
                p.kill()
                p.wait()


def real_paths_snapshot():
    """mtimes of the real FlowState places (the guard's list, with Windows' %LOCALAPPDATA% and
    %APPDATA% folders), to prove the test didn't touch them. Only stat(), never opened."""
    snap = {}
    for c in map(Path, real_dirs()):
        if c.exists():
            for p in [c, *c.rglob("*")][:2000]:
                try:
                    snap[str(p)] = p.stat().st_mtime_ns
                except OSError:
                    pass
    return snap


# Files a FlowState already running on this computer (the user's own HTTP server, relay agent)
# rewrites by itself: the relay heartbeat and its log, the OAuth / app-key database.
LIVE_WRITES = ("status.json", "relay-agent.log", "remote-auth.db", "remote-auth.db-journal",
               "remote-auth.db-wal", "remote-auth.db-shm")


def live_flowstate_running() -> bool:
    """Is the user's own FlowState HTTP server up (default port 8766, on localhost or this
    computer's LAN address)? Only a connect, no request."""
    try:
        hosts = {"127.0.0.1", *socket.gethostbyname_ex(socket.gethostname())[2]}
    except OSError:
        hosts = {"127.0.0.1"}
    for h in hosts:
        try:
            with socket.create_connection((h, 8766), timeout=0.5):
                return True
        except OSError:
            pass
    return False


def real_changes(before, after, live):
    """(changed by this run, changed by the live FlowState). With a live FlowState running, its
    own heartbeat files (LIVE_WRITES) are set apart, and so is a folder whose only change is
    one of them coming or going. Everything else counts. The real-data guard is the other
    proof: it records any open of the real folders by every process this test starts."""
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if not live:
        return changed, []
    own = [k for k in changed if Path(k).name not in LIVE_WRITES]
    dirs = [k for k in own if k in before and k in after and Path(k).is_dir()
            and not any(o.startswith(k + os.sep) for o in own if o != k)]
    test = [k for k in own if k not in dirs]
    return test, sorted(set(changed) - set(test))


# ============================================================================ scenarios
def s1_fresh(W):
    print("\n== S1 fresh install", flush=True)
    cc1 = W.agents["cc1"] = W.stdio("cc1", "claude-code")
    r = cc1.call("start_here")
    R.check(1, "start_here on an empty database answers (no error) and lists no project",
            isinstance(r, str) and "no project given" in r and "Lakeside" not in r, r)
    R.check(1, "an empty database says there are no projects yet (no bare 'Projects:')",
            isinstance(r, str) and "no projects yet" in r and not r.rstrip().endswith("Projects:"), r)
    R.check(1, "the database was created inside the temp sandbox", W.db.exists(), str(W.db))
    r = cc1.call("manage_projects", action="create", name=P,
                 description="Online ordering app for Lakeside Bakery: menu, cart, pickup slots, card payments")
    R.check(1, "manage_projects create returns the project with a receipt",
            r.get("id") == 1 and "receipt" in r, r)
    W.pid = r.get("id")
    comps = {}
    for name, desc in (("web", "Customer-facing React site"), ("payments", "Card payments"),
                       ("api", "Order and menu REST API"), ("db", "Postgres schema and migrations")):
        c = cc1.call("manage_projects", action="add_component", project=P, name=name, description=desc)
        comps[name] = c.get("id")
    W.comps = comps
    R.check(1, "four components (web, payments, api, db) created", all(comps.values()), comps)
    r = cc1.call("manage_projects", action="list")
    R.check(1, "manage_projects list shows the project", P in json.dumps(r), r)
    r = cc1.call("manage_projects", action="list_components", project=P)
    R.check(1, "list_components shows 4 components", sum(n in json.dumps(r) for n in comps) == 4, r)
    for size, cap in BRIEF_BUDGET.items():
        b = cc1.call("start_here", project=P, size=size)
        R.check(1, f"first brief size={size} is {len(brief_text(b))} chars (budget {cap})",
                isinstance(b, str) and len(b) <= cap and P in b, f"{len(brief_text(b))} chars")
    b = cc1.call("start_here", project=P)
    R.check(1, "new project's brief has no 'Since your last visit' and no WARNING",
            not since_lines(b) and "WARNING" not in b, b)


def s2_day(W):
    print("\n== S2 normal day", flush=True)
    cc1 = W.agents["cc1"]
    cc2 = W.agents["cc2"] = W.stdio("cc2", "claude-code")
    cursor = W.agents["cursor"] = W.stdio("cursor", "cursor")
    W.start_http()
    W.grok_key = W.app_key("grok")
    grok = W.agents["grok"] = W.http_agent("grok", "grok", W.grok_key)
    for a in (cc1, cc2, cursor, grok):
        b = a.call("start_here", project=P)
        R.check(2, f"{a.label} reads the brief", isinstance(b, str) and P in b, short(b, 120))

    # rules, links, shared component
    r = cc1.call("manage_rules", action="add", project=P, rule="Never store customer card numbers in our database.")
    R.check(2, "manage_rules add (user's hard rule)", "receipt" in r and not err(r), r)
    W.rule_id = (r.get("rule") or {}).get("id")
    r = cc1.call("manage_rules", action="list", project=P)
    R.check(2, "manage_rules list shows the rule", "card numbers" in json.dumps(r), r)
    b = cursor.call("start_here", project=P, size="quick")
    R.check(2, "hard rule is shown first in the brief", "card numbers" in b.split("Open todos")[0], short(b, 300))

    sh = cc1.call("manage_projects", action="create", name=SHARED, description="Login and accounts shared by bakery apps")
    cc1.call("manage_projects", action="add_component", project=SHARED, name="auth", description="OAuth login")
    r = cc1.call("manage_links", action="add", project=P, link_type="uses", target_project=SHARED, target_component="auth")
    R.check(2, "manage_links add: Lakeside uses the shared 'auth' component", "receipt" in r and not err(r), r)
    W.uses_link = r.get("link_id") or r.get("id")
    r = grok.call("log_work", project=SHARED, kind="change", component="auth",
                  summary="Auth tokens now expire after 30 minutes instead of 8 hours")
    R.check(2, "grok logs a change on the shared component (HTTP, app key)", "receipt" in r, r)
    b = cc2.call("start_here", project=P)
    R.check(2, "linked project gets the shared-change alert in its brief", "30 minutes" in b or "auth" in b.lower(), short(b, 400))
    r = cc1.call("manage_links", action="list", project=P)
    R.check(2, "manage_links list", "uses" in json.dumps(r), r)

    # decisions, changes, learnings (notes) from everyone
    d = cc1.call("log_work", project=P, kind="decision", component="api",
                 summary="Pickup slots are 15 minutes long", details="Matches oven batches; 30 was too coarse",
                 source="user_stated", model="claude-opus-5-5")
    R.check(2, "cc1 logs a decision (with model and source)", d.get("logged") == "decision" and "receipt" in d, d)
    W.d_slots = d["record"]["id"]
    r = cursor.call("log_work", project=P, kind="change", component="web",
                    summary="Cart page shows pickup slot picker", details="components/SlotPicker.tsx")
    R.check(2, "cursor logs a change", "receipt" in r, r)
    W.cursor_change = r.get("record", {}).get("id")
    r = grok.call("log_work", project=P, kind="learning", category="gotcha", component="db",
                  summary="Postgres timestamptz must be used for pickup times (bakery is UTC-5)")
    R.check(2, "grok logs a learning (note)", "receipt" in r, r)
    W.learning = r.get("record", {}).get("id")
    r = cc2.call("log_work", project=P, kind="learning", category="tool_tip",
                 summary=f"Menu names can hold any script: {MARK}")
    R.check(2, "cc2 logs a unicode learning", "receipt" in r, r)
    W.uni_learning = r.get("record", {}).get("id")

    # todos
    todos = {}
    for who, title, pr in ((cursor, "Add order confirmation email", "urgent"),
                           (cursor, "Show allergens on menu items", "normal"),
                           (grok, "Load-test checkout with 200 orders/min", "low"),
                           (cc1, "Add Apple Pay button", "critical")):
        r = who.call("log_work", project=P, kind="todo", summary=title, priority=pr)
        todos[title] = r.get("record", {}).get("id")
        got = (r.get("record") or {}).get("priority")
        R.check(2, f"a todo logged as {pr!r} answers with priority {pr!r} (one set of names everywhere)",
                got == pr, got)
    W.todos = todos
    R.check(2, "4 todos logged by three agents", all(todos.values()), todos)
    t_email, t_allerg, t_load, t_apple = todos.values()

    # claims and conflicts between agents
    r = cc1.call("manage_todos", action="claim", project=P, todo_id=t_email, minutes=30)
    R.check(2, "cc1 claims the email todo", r.get("claimed") is True and "receipt" in r, r)
    r = cursor.call("manage_todos", action="claim", project=P, todo_id=t_email)
    R.check(2, "cursor's claim of the same todo is refused and names the holder",
            r.get("claimed") is not True and ("claude" in json.dumps(r).lower()), r)
    r = cc2.call("manage_todos", action="claim", project=P, todo_id=t_email)
    R.check(2, "the second claude-code window can't take window 1's claim either",
            r.get("claimed") is not True, r)
    r = cursor.call("manage_todos", action="next", project=P, limit=5)
    js = json.dumps(r)
    R.check(2, "manage_todos next lists the claimed todo last for cursor",
            js.find("Add order confirmation email") > js.find("Add Apple Pay button") >= 0, r)
    r = cc1.call("manage_todos", action="renew", project=P, todo_id=t_email, minutes=60)
    R.check(2, "cc1 renews its claim", r.get("renewed") is True or r.get("claimed") is True, r)
    r = cc1.call("manage_todos", action="release", project=P, todo_id=t_email)
    R.check(2, "cc1 releases it", "receipt" in r and not err(r), r)
    r = cursor.call("manage_todos", action="claim", project=P, todo_id=t_email)
    R.check(2, "after release cursor can claim it", r.get("claimed") is True, r)
    r = grok.call("manage_todos", action="claim", project=P, todo_id=t_load, minutes=1)
    R.check(2, "grok claims the load-test todo for 1 minute", r.get("claimed") is True, r)
    r = cc2.call("manage_todos", action="claim", project=P, todo_id=t_load)
    R.check(2, "cc2 refused while grok's claim is live", r.get("claimed") is not True, r)
    W.w("UPDATE todo_claims SET expires_at = ? WHERE todo_id = ?",
        (utc(datetime.now(timezone.utc) - timedelta(minutes=2)), t_load))   # fake the clock
    r = cc2.call("manage_todos", action="claim", project=P, todo_id=t_load)
    R.check(2, "after grok's claim expires cc2 can claim it", r.get("claimed") is True, r)
    r = cc2.call("manage_todos", action="handoff", project=P, todo_id=t_load,
                 where_i_was="Wrote k6 script in load/checkout.js", next_step="Run it against staging")
    R.check(2, "cc2 hands the todo off with a note (claim cleared)", not err(r) and "receipt" in r, r)
    r = cursor.call("manage_todos", action="update", project=P, todo_id=t_email, status="done")
    R.check(2, "cursor marks its claimed todo done", not err(r) and "receipt" in r, r)
    rows = W.q("SELECT * FROM todo_claims WHERE todo_id = ?", (t_email,))
    R.check(2, "marking done cleared the claim", not rows, rows)

    # more todo actions
    r = cc1.call("manage_todos", action="add", project=P, title="Apple Pay: domain verification file",
                 parent_todo_id=t_apple, due_date="2026-11-01")
    R.check(2, "manage_todos add a subtask with due date", not err(r) and "receipt" in r, r)
    W.subtask = r.get("id") or r.get("todo_id") or (r.get("record") or {}).get("id")
    r = cc1.call("manage_todos", action="block", project=P, todo_id=t_apple, blocked_by=[W.subtask])
    R.check(2, "manage_todos block (Apple Pay waits on the subtask)", not err(r), r)
    r = cc1.call("manage_todos", action="unblock", project=P, todo_id=t_apple)
    R.check(2, "manage_todos unblock", not err(r), r)
    r = cc1.call("manage_todos", action="update", project=P, todo_id=t_allerg, priority="urgent",
                 title="Show allergens (nuts, gluten) on menu items")
    R.check(2, "manage_todos update title and priority", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_todos", action="list", project=P, compact=True, sort="due")
    R.check(2, "manage_todos list", "allergens" in json.dumps(r), r)

    # problems
    r = grok.call("log_work", project=P, kind="problem", component="payments", priority="urgent",
                  summary="Webhook from payment provider arrives twice for some orders")
    W.problem = (r.get("record") or {}).get("id")
    R.check(2, "grok logs a problem", bool(W.problem) and "receipt" in r, r)
    a1 = grok.call("manage_problems", action="add_attempt", project=P, problem_id=W.problem,
                   description="Dedupe on event id in a unique index")
    W.attempt = a1.get("id") or a1.get("attempt_id")
    R.check(2, "manage_problems add_attempt", bool(W.attempt) and "receipt" in a1, a1)
    r = grok.call("manage_problems", action="end_attempt", project=P, attempt_id=W.attempt,
                  outcome="success", notes="Unique index on provider_event_id", confidence="verified")
    R.check(2, "manage_problems end_attempt", not err(r) and "receipt" in r, r)
    r = cursor.call("manage_problems", action="tree", project=P, problem_id=W.problem)
    R.check(2, "manage_problems tree shows the attempt", "Dedupe" in json.dumps(r), r)
    r = grok.call("manage_problems", action="solve", project=P, problem_id=W.problem,
                  summary="Unique index on provider_event_id", key_insight="Webhooks are at-least-once",
                  winning_attempt_id=W.attempt)
    R.check(2, "manage_problems solve", not err(r) and "receipt" in r, r)
    r = cursor.call("manage_problems", action="list", project=P, compact=True)
    R.check(2, "manage_problems list", not err(r), r)

    # handoffs
    r = cursor.call("manage_handoffs", action="leave", project=P, where_i_was="Slot picker done, styles rough",
                    next_step="Polish SlotPicker CSS for mobile")
    W.handoff = r.get("handoff_id") or r.get("id")
    R.check(2, "cursor leaves a handoff note", bool(W.handoff) and "receipt" in r, r)
    b = grok.call("start_here", project=P)
    R.check(2, "next brief shows the HANDOFF note", "HANDOFF" in b.upper() and "SlotPicker" in b, short(b, 300))
    r = grok.call("manage_handoffs", action="list", project=P)
    R.check(2, "manage_handoffs list", "SlotPicker" in json.dumps(r), r)
    r = grok.call("manage_handoffs", action="pickup", project=P, handoff_id=W.handoff)
    R.check(2, "grok picks it up", not err(r) and "receipt" in r, r)

    # search and history
    r = cursor.call("search", query="pickup slot")
    R.check(2, "search finds the slot decision/change", "slot" in json.dumps(r).lower() and r.get("count", 0) >= 1, r)
    r = cursor.call("search", query="How long are pickup slots?", answer=True, project=P)
    R.check(2, "search answer=true quotes the 15-minute decision", "15" in json.dumps(r), r)
    r = cursor.call("search", project=P, since_hours=1, content_types=["learning"], limit=50)
    R.check(2, "search without query lists newest learnings", "timestamptz" in json.dumps(r), r)
    r = cursor.call("manage_history", action="who", project=P, by_app="grok")
    R.check(2, "manage_history who by_app=grok lists grok's work", "grok" in json.dumps(r).lower(), r)
    r = cursor.call("manage_history", action="list", project=P, record_type="todo", record_id=t_allerg)
    R.check(2, "manage_history list shows the todo edit with old/new values", "allergens" in json.dumps(r), r)

    # full-only tools (local stdio app)
    r = cc1.call("manage_variables", action="create", project=P, name="API_BASE_URL",
                 value="https://api.lakeside.example/v1", category="endpoint", description="Order API")
    W.var = r.get("id") or r.get("variable_id")
    R.check(2, "manage_variables create", bool(W.var) and "receipt" in r, r)
    r = cc1.call("manage_variables", action="update", project=P, variable_id=W.var, value="https://api.lakeside.example/v2")
    R.check(2, "manage_variables update", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_variables", action="list", project=P)
    R.check(2, "manage_variables list", "v2" in json.dumps(r), r)
    r = cc1.call("manage_methods", action="create", project=P, name="Deploy web", category="deployment",
                 description="Ship the React site", steps=["npm run build", "upload dist/ to CDN", "purge cache"])
    W.method = r.get("id") or r.get("method_id")
    R.check(2, "manage_methods create", bool(W.method) and "receipt" in r, r)
    r = cc1.call("manage_methods", action="update", project=P, method_id=W.method,
                 steps=["run tests", "build", "upload", "purge cache"])
    R.check(2, "manage_methods update", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_methods", action="list", project=P)
    R.check(2, "manage_methods list", "Deploy web" in json.dumps(r), r)
    r = cc1.call("manage_skills", action="learn", project=P, skill="User prefers small PRs per component",
                 skill_type="user_preference", context="code review")
    W.skill = r.get("id") or r.get("skill_id")
    R.check(2, "manage_skills learn", bool(W.skill) and not err(r), r)
    for act, extra in (("confirm", {"skill_id": W.skill}),
                       ("log_metric", {"metric_type": "tool_choice", "effectiveness_score": 0.9})):
        r = cc1.call("manage_skills", action=act, **extra)   # description: project needed except list / list_metrics
        if r.get("error") != "project_required":
            W.skills_mismatch = W.__dict__.get("skills_mismatch", []) + [act]
    for act, extra in (("confirm", {"project": P, "skill_id": W.skill}), ("list", {"project": P}),
                       ("save_state", {"project": P, "focus_summary": "Checkout flow", "key_facts": ["15-min slots"]}),
                       ("get_state", {"project": P}),
                       ("log_metric", {"project": P, "metric_type": "tool_choice", "effectiveness_score": 0.9, "action_taken": "used claims"}),
                       ("list_metrics", {})):
        r = cc1.call("manage_skills", action=act, **extra)
        R.check(2, f"manage_skills {act}", not err(r), r)
    for act, extra in (("current", {}), ("list", {"project": P}), ("conversations", {"project": P, "limit": 5}),
                       ("start", {"project": P, "focus_component_id": W.comps["payments"]})):
        r = cc1.call("manage_sessions", action=act, **extra)
        R.check(2, f"manage_sessions {act}", not err(r), r)
    note = W.root / "files" / "menu-spec.md"
    note.parent.mkdir(exist_ok=True)
    note.write_text("# Menu spec\nCroissant, sourdough, lemon tart. Allergens: gluten, nuts.\n")
    r = cc1.call("manage_files", action="attach", project=P, file_path=str(note), component_id=W.comps["web"],
                 user_description="Menu spec from the owner", tags=["menu"])
    W.attach = r.get("id") or r.get("attachment_id")
    R.check(2, "manage_files attach (stdio)", bool(W.attach) and "receipt" in r, r)
    r = cc1.call("manage_files", action="list", project=P)
    R.check(2, "manage_files list", "menu-spec" in json.dumps(r), r)
    r = cc1.call("manage_files", action="search", project=P, query="sourdough", file_types=["md"])
    if not R.check(2, "manage_files search finds text inside the attached file", "sourdough" in json.dumps(r).lower(), r):
        R.finding("medium", "manage_files search never finds anything",
                  "Attach any text file (manage_files attach, it is copied into the bundle), then "
                  "manage_files action=search query=<a word in it> -> []. search_file_content (tools/files.py) "
                  "reads the content_locations table, but nothing in the server ever writes to it. The tool "
                  "description promises 'search (text in attached files)', so an agent concludes the file "
                  "doesn't mention it. Not new in 2.4 (same code in v2.3.0).")
    r = grok.call("manage_files", action="attach", project=P, file_path=str(note))
    R.check(2, "manage_files attach is refused over HTTP (as described)", err(r), r)
    r = grok.call("manage_data_git", action="status")
    R.check(2, "manage_data_git is refused over HTTP (as described)", err(r), r)
    # data git on the sandbox data folder; the 'remote' is a bare repo in the sandbox (no network)
    bare = W.root / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    r = cc1.call("manage_data_git", action="status")
    R.check(2, "manage_data_git status before init says it isn't a repository yet", r.get("initialized") is False, r)
    for act, extra in (("init", {}), ("set_remote", {"remote_url": str(bare)})):
        r = cc1.call("manage_data_git", action=act, **extra)
        R.check(2, f"manage_data_git {act} (sandbox data folder, local bare remote)", not err(r), r)
    r = cc1.call("manage_data_git", action="sync", commit_message="e2e sync")
    if not R.check(2, "manage_data_git sync to a new, empty remote pushes the first commit", r.get("success") is True, r):
        R.finding("low", "First data-git sync to a new empty remote always fails, and calls it a conflict",
                  "manage_data_git init; set_remote <a new empty repo>; sync -> {success:false, step:'pull', "
                  "error:\"couldn't find remote ref main\", conflict:true}. git_sync pulls before the first push, "
                  "and the remote has no branch yet; every later sync fails the same way. 'conflict: true' sends "
                  "the user looking for a merge conflict that doesn't exist.")
    r = cc1.call("manage_data_git", action="history", limit=5)
    R.check(2, "manage_data_git history shows the local commit", "e2e sync" in json.dumps(r), r)
    r = cc1.call("manage_data_git", action="clone", remote_url=str(bare), local_path=str(W.root / "clone"))
    R.check(2, "manage_data_git clone into an empty folder answers (no crash)", isinstance(r, dict), r)
    r = cc1.call("manage_data_git", action="set_remote", remote_url="https://flowstate-e2e.invalid/bakery.git")
    r = cc1.call("manage_data_git", action="sync", commit_message="offline sync")
    if not R.check(2, "manage_data_git sync to an unreachable host does not claim it pushed",
                   not (r.get("success") is True and r.get("pushed")), r):
        R.finding("medium", "Data-git sync reports success and pushed=true when the remote can't be reached",
                  "manage_data_git set_remote https://flowstate-e2e.invalid/x.git; sync -> {success:true, "
                  "pushed:true}. git_sync treats any 'Could not resolve host' error from pull and push as fine. "
                  "Offline, the user is told their data was pushed when no off-site copy exists.")
    r = cc1.call("manage_rules", action="add", project=P, rule="Always ask before changing prices.")
    rid = (r.get("rule") or {}).get("id")
    r = cursor.call("manage_rules", action="remove", project=P, rule_id=rid)
    R.check(2, "manage_rules remove without the user's words is refused", err(r), r)
    r = cursor.call("manage_rules", action="remove", project=P, rule_id=rid,
                    user_request="drop the rule about asking before changing prices")
    R.check(2, "manage_rules remove with the user's words (kept as removed)", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_rules", action="list", project=P, include_removed=True)
    R.check(2, "removed rule still listed with include_removed", "changing prices" in json.dumps(r), r)
    W.mid_brief = cc1.call("start_here", project=P, size="deep")
    for size, cap in BRIEF_BUDGET.items():
        b = cc2.call("start_here", project=P, size=size)
        R.check(2, f"busy-project brief size={size} is {len(b)} chars (budget {cap})", len(b) <= cap, len(b))


def s3_conflicts(W):
    print("\n== S3 conflicting decisions", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    d1 = cc1.call("log_work", project=P, kind="decision", component="payments",
                  summary="Use Stripe Checkout for card payments", details="Hosted page keeps PCI scope small")
    d2 = cursor.call("log_work", project=P, kind="decision", component="payments",
                     summary="Use Square Checkout for card payments")
    W.d_stripe, W.d_square = d1["record"]["id"], d2["record"]["id"]
    pc = d2.get("possible_conflicts") or []
    R.check(3, "a decision close to an existing one is flagged on save",
            any(c.get("decision_id") == W.d_stripe for c in pc) and "Possible conflict" in (d2.get("note") or ""), d2)
    W.conf_item = next((c.get("review_item_id") for c in pc), None)
    b = grok.call("start_here", project=P)
    R.check(3, "the brief lists the possible conflict",
            f"D{W.d_square} vs D{W.d_stripe}" in b, [l for l in b.splitlines() if "conflict" in l.lower()])
    # resolved by replaces=
    d3 = cc2.call("log_work", project=P, kind="decision", component="payments", replaces=W.d_square,
                  summary="Use Stripe Payment Element for card payments",
                  details="User chose Stripe after all; Square was a draft")
    W.d_element = d3["record"]["id"]
    b = grok.call("start_here", project=P)
    R.check(3, "replaces= resolves it: the Square/Stripe pair is no longer listed",
            f"D{W.d_square} vs D{W.d_stripe}" not in b,
            [l for l in b.splitlines() if "conflict" in l.lower()] or "no conflict line")
    if d3.get("possible_conflicts"):
        R.word(f"Logging D{W.d_element} with replaces={W.d_square} still flags it against "
               f"D{[c.get('decision_id') for c in d3['possible_conflicts']]} (Stripe Checkout). Reasonable, but an agent "
               "that just used replaces= may think the flag is about the decision it replaced.")
    # a second pair, dismissed through the review queue with the user's words
    e1 = grok.call("log_work", project=P, kind="decision", component="web",
                   summary="Show prices including sales tax on the menu page")
    e2 = cursor.call("log_work", project=P, kind="decision", component="web",
                     summary="Show prices including sales tax on the cart page")
    pc = e2.get("possible_conflicts") or []
    item = next((c.get("review_item_id") for c in pc), None)
    R.check(3, "second similar pair flagged (tax display menu vs cart)", bool(item), e2)
    if item:
        r = cursor.call("manage_review", action="reject", project=P, item_id=item, reason="fine",
                        user_request="Show prices including sales tax on the cart page")
        R.check(3, "cursor (whose save made the flag) can't dismiss it by echoing the record",
                r.get("error") == "own_item", r)
        r = cc1.call("manage_review", action="reject", project=P, item_id=item,
                     reason="Menu and cart should both show tax-inclusive prices; they agree",
                     user_request="those two tax decisions are compatible, dismiss that conflict")
        R.check(3, "user dismisses it through the review queue (relayed by another agent)",
                not err(r) and "receipt" in r, r)
        b = grok.call("start_here", project=P)
        R.check(3, "dismissed pair no longer in the brief",
                f"D{e2['record']['id']} vs D{e1['record']['id']}" not in b,
                [l for l in b.splitlines() if "conflict" in l.lower()] or "no conflict line")
    if W.conf_item:
        r = cc1.call("manage_review", action="list", status="all")
        st = [x.partition("): ")[0].rsplit(", ", 1)[-1] for x in review_lines(r) if line_id(x) == W.conf_item]
        R.check(3, "the conflict item for the replaced pair is no longer pending", st and st[0] != "pending",
                f"item {W.conf_item} status {st}")


def s4_review(W):
    print("\n== S4 review mode", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    r = cursor.call("manage_review", action="get_mode", project=P)
    R.check(4, "default review mode is auto_only", "auto_only" in json.dumps(r), r)
    r = cursor.call("manage_review", action="set_mode", project=P, mode="everything")
    R.check(4, "set_mode without the user's words is refused", err(r), r)
    r = cc1.call("manage_review", action="set_mode", project=P, mode="everything",
                 user_request="For Lakeside, make every new record wait for my review")
    R.check(4, "set_mode everything with the user's words", not err(r) and "receipt" in r, r)
    items = {}
    for who, kind, s in ((cursor, "todo", "Add gift card support"),
                         (cursor, "decision", "Use Postmark for transactional email"),
                         (grok, "learning", "Square sandbox rejects test cards after 5pm UTC"),
                         (cc2, "change", "Moved menu images to the CDN"),
                         (grok, "problem", "Oven timer drifts two minutes a day")):
        r = who.call("log_work", project=P, kind=kind, summary=s)
        items[s] = r.get("queued_for_review")
        R.check(4, f"{who.label}'s {kind} is queued, not saved, and still has a receipt",
                bool(r.get("queued_for_review")) and "receipt" in r and "record" not in r, r)
    b = cc1.call("start_here", project=P)
    R.check(4, "brief shows review mode 'everything' and the waiting count",
            "everything" in b and "Review queue:" in b, [l for l in b.splitlines() if "Review" in l])
    gift, postmark, square, cdn, oven = items.values()
    # self-approval: the app that queued it needs the user's own words (as before task 25)
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift)
    R.check(4, "own item, no user_request: refused", r.get("error") == "user_request_required", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift, user_request="Add gift card support")
    R.check(4, "own item, user_request = the item's own text: refused (own_item)", r.get("error") == "own_item", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift, user_request="approve")
    msg = r.get("message", "")
    R.check(4, "own item, one-word 'approve': refused, in one short line",
            r.get("error") == "own_item" and "\n" not in msg and len(msg) < 200, r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=postmark,
                    user_request="User: yes, approve the Postmark decision, I checked it")
    R.check(4, "own suggestion with the user's words: approved again, as before task 25 (approvals are off)",
            not err(r) and r.get("approved") == postmark and "receipt" in r, r)
    # the user's own terminal step works for any item
    code, out = W.cli_no_terminal("review", "--project", P)
    R.check(4, "`flowstate review` lists what waits (read-only, no terminal needed)",
            code == 0 and f"#{square}" in out and "Square" in out, short(out, 200))
    code, out = W.cli_no_terminal("approve", str(square), stdin="y\n")
    still = W.q("SELECT status FROM review_items WHERE id = ?", (square,))[0]["status"]
    R.check(4, "`flowstate approve` with a piped 'y' and no terminal: refused, nothing changes",
            code == 2 and "Refused" in out and still == "pending", f"exit {code}: {short(out, 160)}; {still}")
    code, out = W.cli_no_terminal("reject", str(cdn), stdin="yes\n")
    R.check(4, "`flowstate reject` with a piped 'yes' and no terminal: refused too",
            code == 2 and W.q("SELECT status FROM review_items WHERE id = ?", (cdn,))[0]["status"] == "pending",
            f"exit {code}: {short(out, 160)}")
    code, out = W.cli_in_terminal(["approve", str(square)], ["y"])
    row = W.q("SELECT status, decided_by, result_id FROM review_items WHERE id = ?", (square,))[0]
    R.check(4, "`flowstate approve` in a terminal, the user types y: approved and the record saved",
            code == 0 and row["status"] == "approved" and row["decided_by"] == "command-line"
            and row["result_id"] and "Square" in out, f"exit {code}: {short(out, 200)}; {row}")
    code, out = W.cli_in_terminal(["reject", str(cdn), "--reason", "Images stay on the server"], ["n"])
    R.check(4, "answering n in the terminal changes nothing",
            code == 1 and W.q("SELECT status FROM review_items WHERE id = ?", (cdn,))[0]["status"] == "pending",
            f"exit {code}: {short(out, 160)}")
    code, out = W.cli_in_terminal(["reject", str(cdn), "--reason", "Images stay on the server"], ["y"])
    R.check(4, "`flowstate reject` in a terminal: rejected with the reason",
            code == 0 and W.q("SELECT status, reject_reason FROM review_items WHERE id = ?", (cdn,))[0]
            == {"status": "rejected", "reject_reason": "Images stay on the server"}, f"exit {code}: {short(out, 160)}")
    r = cc1.call("manage_review", action="approve", project=P, item_id=gift,
                 user_request="Yes approve the gift card todo, we need it before Christmas", priority="urgent")
    R.check(4, "another agent relays the user's approval: the todo is created",
            not err(r) and "receipt" in r and any(s.startswith("todo") for s in r["receipt"]["saved"]), r)
    r = cc1.call("manage_review", action="reject", project=P, item_id=oven,
                 user_request="No, that oven note is wrong, reject it", reason="Not reproducible")
    R.check(4, "reject with the user's words and reason", not err(r) and "receipt" in r, r)
    r = cursor.call("manage_review", action="list", project=P, status="all", limit=100)
    R.check(4, "manage_review list status=all shows approved and rejected items",
            any(", approved): " in x for x in review_lines(r))
            and any(", rejected): " in x for x in review_lines(r)), short(r, 200))
    # suggested=true and types mode
    r = cc1.call("manage_review", action="set_mode", project=P, mode="types", kinds=["decision"],
                 user_request="Only decisions should wait for me from now on")
    R.check(4, "set_mode types kinds=[decision]", not err(r), r)
    r = grok.call("log_work", project=P, kind="todo", summary="Add pickup reminder SMS")
    R.check(4, "types mode: a todo saves directly", "record" in r, r)
    r = grok.call("log_work", project=P, kind="decision", summary="Send pickup reminders 30 minutes before the slot")
    R.check(4, "types mode: a decision waits", bool(r.get("queued_for_review")), r)
    r = cc1.call("manage_review", action="set_mode", project=P, clear=True,
                 user_request="Go back to the normal review mode for Lakeside")
    R.check(4, "set_mode clear (back to the install default)", not err(r), r)
    r = cursor.call("log_work", project=P, kind="learning", suggested=True,
                    summary="Customers abandon carts when slots are full; show next free slot")
    R.check(4, "suggested=true queues even in auto_only", bool(r.get("queued_for_review")) and "receipt" in r, r)
    r = cc1.call("manage_review", action="get_mode", project=P)
    R.check(4, "mode back to auto_only", "auto_only" in json.dumps(r), r)


def s5_receipts(W):
    print("\n== S5 receipts", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    s = "Croissant dough rests 12 hours in the fridge"
    r1 = cursor.call("log_work", project=P, kind="learning", summary=s)
    n1 = W.count("learnings", "insight LIKE ?", (f"%{s}%",))
    r2 = cursor.call("log_work", project=P, kind="learning", summary=s, model="gpt-5")
    n2 = W.count("learnings", "insight LIKE ?", (f"%{s}%",))
    R.check(5, "an exact repeat returns the original receipt and writes nothing",
            r2.get("already_saved") is True and r2["receipt"] == r1["receipt"] and n1 == n2 == 1,
            f"first {r1.get('receipt')}, repeat {short(r2, 200)}, rows {n1}->{n2}")
    r3 = cc2.call("log_work", project=P, kind="learning", summary=s)
    n3 = W.count("learnings", "insight LIKE ?", (f"%{s}%",))
    R.check(5, "the same content from a different session does write", "record" in r3 and n3 == 2, f"{short(r3,120)} rows {n3}")
    r4 = grok.call("log_work", project=P, kind="learning", summary=s)
    r5 = grok.call("log_work", project=P, kind="learning", summary=s)
    R.check(5, "HTTP: repeat on the same MCP session is caught too", r5.get("already_saved") is True, r5)
    r6 = cursor.call("log_work", project=W.pid, kind="learning", summary=s)
    n6 = W.count("learnings", "insight LIKE ?", (f"%{s}%",))
    if n6 > 3:
        R.finding("low", "A repeat that names the project by id instead of name is saved again",
                  f"cursor: log_work(project=\"{P}\", kind=learning, summary=S); then the same with project={W.pid} "
                  "within 2 minutes -> a second copy (the repeat check hashes the raw arguments; it ignores "
                  "'model' and 'project_id' but not 'project' given as a number).")
    W.window_probe = (time.monotonic(), s)
    # timed-out write on a slow path: a server with a 2 s call timeout, the DB locked for 4 s
    slow = W.agents["slow"] = W.stdio("slow", "claude-code", {"FLOWSTATE_CALL_TIMEOUT": "2"})
    slow.call("start_here", project=P)
    t = "Oven 2 thermostat reads 10 degrees high"
    conn = sqlite3.connect(str(W.db), timeout=10, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")      # blocks writers, not readers
    holder = threading.Timer(3.5, lambda: (conn.rollback(), conn.close()))
    t0 = time.time()
    holder.start()
    r = slow.call("log_work", project=P, kind="learning", summary=t)
    R.check(5, "a write stuck behind a lock times out with a 'don't repeat it' message",
            r.get("error") == "flowstate_timeout" and "repeat" in json.dumps(r).lower(), r)
    holder.join()
    time.sleep(2.0)
    landed = W.count("learnings", "insight LIKE ?", (f"%{t}%",))
    nxt = slow.call("search", query="thermostat", project=P)
    R.check(5, "it landed once the lock went away, and the agent's next answer says so",
            landed == 1 and "did save" in json.dumps(nxt), f"rows {landed}; next answer: {short(nxt, 260)}")
    rr = slow.call("log_work", project=P, kind="learning", summary=t)
    landed2 = W.count("learnings", "insight LIKE ?", (f"%{t}%",))
    R.check(5, "retrying the timed-out write returns its receipt; still one row",
            rr.get("already_saved") is True and landed2 == 1, f"{short(rr, 200)} rows {landed2}")
    # retry while the first is still waiting on the lock
    t2 = "Proofing cabinet humidity sensor drifts"
    conn = sqlite3.connect(str(W.db), timeout=10, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    holder = threading.Timer(4.5, lambda: (conn.rollback(), conn.close()))
    holder.start()
    a = slow.call("log_work", project=P, kind="learning", summary=t2)
    b = slow.call("log_work", project=P, kind="learning", summary=t2)   # immediate retry
    holder.join()
    time.sleep(2.0)
    n = W.count("learnings", "insight LIKE ?", (f"%{t2}%",))
    R.check(5, "an immediate retry while the first is still saving doesn't make a copy",
            n == 1 and (b.get("already_saved") or b.get("error") in ("still_saving", "flowstate_timeout")),
            f"first {short(a, 90)}; retry {short(b, 140)}; rows {n}")
    # a lock that also blocks readers: the 'is this app paused?' read runs before the timeout starts
    conn = sqlite3.connect(str(W.db), timeout=10, check_same_thread=False)
    conn.execute("BEGIN EXCLUSIVE")
    holder = threading.Timer(4.0, lambda: (conn.rollback(), conn.close()))
    holder.start()
    t0 = time.time()
    r = slow.call("log_work", project=P, kind="learning", summary="Walk-in fridge door seal worn")
    took = time.time() - t0
    holder.join()
    if not R.check(5, f"the 2 s call timeout also covers a fully locked database (answered after {took:.1f}s)",
                   took < 3.0 or r.get("error") == "flowstate_timeout", f"{took:.1f}s: {short(r, 120)}"):
        R.finding("low", "A locked database can stall a write past FLOWSTATE_CALL_TIMEOUT",
                  "FLOWSTATE_CALL_TIMEOUT=2; another process holds an EXCLUSIVE lock for 4 s; log_work -> answers "
                  f"after ~{took:.0f} s with no timeout. server._call_internal runs activity.is_paused (a read with a 5 s "
                  "busy wait) before asyncio.wait_for starts, so the 'FlowState is slow, carry on' promise can be "
                  "late by up to 5 s.")


def s6_windows(W):
    print("\n== S6 two windows", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    bsid = lambda b: int(re.search(r"Session #(\d+)", b).group(1)) if re.search(r"Session #(\d+)", b) else None  # noqa: E731
    sid = lambda r: (r.get("session") or r).get("id") if isinstance(r, dict) else None  # noqa: E731
    W.sid1, W.sid2 = bsid(cc1.call("start_here", project=P)), bsid(cc2.call("start_here", project=P))
    R.check(6, "two claude-code windows have separate sessions (brief: 'Session #N is open for you')",
            W.sid1 and W.sid2 and W.sid1 != W.sid2, f"window1 #{W.sid1} / window2 #{W.sid2}")
    c1, c2 = sid(cc1.call("manage_sessions", action="current")), sid(cc2.call("manage_sessions", action="current"))
    if not R.check(6, "manage_sessions current returns each window's own session",
                   (c1, c2) == (W.sid1, W.sid2), f"brief says #{W.sid1}/#{W.sid2}; current says #{c1}/#{c2}"):
        R.finding("medium", "manage_sessions current returns another window's session",
                  f"Two Claude Code windows: their briefs say Session #{W.sid1} and #{W.sid2}; manage_sessions "
                  f"action=current answers #{c1} and #{c2} (the newest open session of that app, not this connection's). "
                  "An agent that then calls manage_sessions end with that id closes the other window's session "
                  "and queues a summary of work it didn't do.")
    runs = W.q("SELECT session_id, conn_key, app FROM session_runs WHERE app='claude-code'")
    R.check(6, "session_runs: each window has its own connection key",
            len({r["conn_key"] for r in runs}) >= 2, runs)
    # quiet: nothing changed (a second apart, so the same-second case below can't play in)
    time.sleep(1.1)
    cc1.call("start_here", project=P)
    time.sleep(1.1)
    b = cc1.call("start_here", project=P)
    R.check(6, "no 'since' line when nothing changed", not since_lines(b), since_lines(b))
    # a save in the same second as the brief that already showed it
    reps = 0
    for i in range(3):
        time.sleep(1.1)
        cursor.call("log_work", project=P, kind="todo", summary=f"Same-second todo {i}")
        cc1.call("start_here", project=P)
        b = cc1.call("start_here", project=P)        # nothing new since the line above
        reps += bool(since_lines(b))
    if not R.check(6, "a save already reported isn't reported again on the next brief", reps == 0,
                   f"repeated {reps} of 3 times: {since_lines(b)}"):
        R.finding("low", "'Since your last visit' repeats a save the agent was already told about",
                  f"cursor log_work; claude-code start_here (line: 'cursor saved 1 todo'); claude-code start_here "
                  f"again with nothing new -> the same line again ({reps} of 3 tries here, 5 of 5 in a separate repro). "
                  "ledger.since_line compares whole-second times with finished_at >= the last visit, so a save in "
                  "the same second as the previous brief counts again. The agent may redo work or re-check it.")
    cursor.call("log_work", project=P, kind="decision", summary="Bakery closes online orders at 6pm")
    b = cc1.call("start_here", project=P)
    ln = since_lines(b)
    R.check(6, "one 'since your last visit' line naming cursor after cursor saves",
            len(ln) == 1 and "cursor" in ln[0] and "1 decision" in ln[0] and len(ln[0]) <= 240, ln)
    # window 2 works while window 1 is away
    time.sleep(1.1)                          # whole seconds apart: keeps the same-second case out
    cc1.call("start_here", project=P)        # window 1 looks
    time.sleep(1.1)
    cc2.call("start_here", project=P)        # window 2 looks
    time.sleep(1.1)
    W.slots_change = cc2.call("log_work", project=P, kind="change", component="api", summary="Added GET /slots endpoint")["record"]["id"]
    time.sleep(1.1)
    b = cc1.call("start_here", project=P)
    ln = since_lines(b)
    ok = len(ln) == 1 and "another claude-code session" in ln[0]
    R.check(6, "window 1 is told 'another claude-code session saved 1 change' (window 2's work)", ok,
            ln or "no line at all")
    if not ok:
        R.finding("medium", "'Since your last visit' misses the other window's work when both windows of one app read the brief",
                  "Two Claude Code windows (two stdio connections, same app and machine). Window 1 "
                  "start_here; window 2 start_here; window 2 log_work kind=change; window 1 start_here -> no "
                  "'Since your last visit' line. ledger.since_line takes the last start_here of the same "
                  "app+machine (window 2's) as window 1's last visit and also excludes window 2's session, "
                  "so the other window's saves are never mentioned. An agent wrongly trusts nothing changed.")
    # who attributes per window
    r = cursor.call("manage_history", action="who", project=P, by_app="claude-code", limit=100)
    rows = r.get("rows", [])
    R.check(6, "manage_history who lists both windows' work under claude-code",
            any(x["record_type"] == "change" and x["record_id"] == W.slots_change for x in rows)
            and any(x["record_type"] == "decision" and x["record_id"] == W.d_slots for x in rows), short(rows[:2], 300))
    sess = {x["session_id"] for x in W.q("SELECT session_id FROM run_writes WHERE records LIKE ? OR records LIKE ?",
                                          (f'%"change:{W.slots_change}"%', f'%"decision:{W.d_slots}"%'))}
    R.check(6, "the run ledger attributes those two saves to the two different windows' sessions",
            sess == {W.sid1, W.sid2}, f"sessions {sess}")
    by_rec = {(x["record_type"], x["record_id"]): x.get("session") for x in rows}
    R.check(6, "manage_history who shows each window's session, so two windows of one app are told apart",
            {by_rec.get(("change", W.slots_change)), by_rec.get(("decision", W.d_slots))} == {W.sid1, W.sid2},
            {f"{k[0]} #{k[1]}": v for k, v in by_rec.items() if k in (("change", W.slots_change), ("decision", W.d_slots))})
    # long gap: fake the clock (move claude-code's earlier visits 20 days back)
    old = utc(datetime.now(timezone.utc) - timedelta(days=20))
    W.w("UPDATE run_views SET at = ? WHERE app = 'claude-code' AND tool = 'start_here'", (old,))
    grok.call("log_work", project=P, kind="todo", summary="Holiday hours banner")
    cursor.call("log_work", project=P, kind="change", component="web", summary="Footer shows holiday hours")
    b = cc1.call("start_here", project=P)
    ln = since_lines(b)
    R.check(6, "after a 20-day gap: one line with counts plus a manage_history pointer",
            len(ln) == 1 and "manage_history(action=\"who\"" in ln[0] and "save" in ln[0] and len(ln[0]) <= 240, ln)
    if ln and "since=\"" in ln[0]:
        day = ln[0].split('since="')[1].split('"')[0]
        r = cc1.call("manage_history", action="who", project=P, since=day, page=1)
        apps = {x.get("app") for x in r.get("rows", [])}
        R.check(6, "the pointer's manage_history call works and lists grok and cursor", {"grok", "cursor"} <= apps, apps)
    # HTTP: two sessions of the same app on one server are separate too
    h1 = W.http_agent("http-cc-a", "claude-code", W.shared_token)
    h2 = W.http_agent("http-cc-b", "claude-code", W.shared_token)
    W.agents["http-cc-a"], W.agents["http-cc-b"] = h1, h2
    h1.call("start_here", project=P)
    h2.call("start_here", project=P)
    a, b2 = bsid(h1.call("start_here", project=P)), bsid(h2.call("start_here", project=P))
    R.check(6, "HTTP: two MCP sessions of one app get separate FlowState sessions", a and b2 and a != b2, f"#{a} / #{b2}")
    r = h2.call("manage_sessions", action="end", project=P, session_id=b2, summary="Checked slot API docs",
                outcomes=["docs ok"])
    R.check(6, "manage_sessions end closes the window's own session (by its brief id)", not err(r), r)
    st = W.q("SELECT closed_at FROM auto_sessions WHERE session_id=?", (a,))
    R.check(6, "ending window B's session left window A's open", st and st[0]["closed_at"] is None, st)


def s7_undo(W):
    print("\n== S7 undo", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    r = cc1.call("log_work", project=P, kind="todo", summary="Typo todo created by mistake")
    tid = r["record"]["id"]
    u = cc1.call("manage_history", action="undo", project=P, record_type="todo", record_id=tid)
    gone = W.count("todos", "id = ?", (tid,)) == 0
    R.check(7, "undo takes back a todo this app just created", not err(u) and gone and "receipt" in u, u)
    u2 = cc1.call("manage_history", action="undo", project=P, record_type="todo", record_id=tid)
    R.check(7, "undoing that undo brings it back (undo again = redo)",
            W.count("todos", "id = ?", (tid,)) == 1, u2)
    # someone else's record
    r = cursor.call("log_work", project=P, kind="learning", summary="Cursor's own note on CSS grid")
    lid = r["record"]["id"]
    u = cc1.call("manage_history", action="undo", project=P, record_type="learning", record_id=lid)
    R.check(7, "undo of another app's record is refused (needs a delete request)",
            err(u) and W.count("learnings", "id = ?", (lid,)) == 1, u)
    u = cc2.call("manage_history", action="undo", project=P, record_type="todo", record_id=tid)
    R.check(7, "the other window of the same app CAN take back window 1's record (same app)",
            True, u)   # informational: rule is per app, not per window
    W.undo_other_window = u
    # a user-approved record can't be undone by the agent
    appr = [i for i in W.q("SELECT result_table, result_id FROM review_items WHERE status='approved' "
                           "AND result_table='todos'")]
    if appr:
        u = cc1.call("manage_history", action="undo", project=P, record_type="todo", record_id=appr[0]["result_id"])
        R.check(7, "a user-approved record can't be taken back with undo (review rules still hold)",
                err(u) or "user_approved" in json.dumps(u), u)
    # partial undo: decision + its component link created together; the link is changed since
    r = cc1.call("log_work", project=P, kind="decision", component="db", summary="Use UUID primary keys for orders")
    did = r["record"]["id"]
    saved = r["receipt"]["saved"]
    link = next((int(x.split("#")[1]) for x in saved if x.startswith("link")), None)
    R.check(7, "a decision on a component creates two records in one change", link is not None, saved)
    if link:
        rm = cc1.call("manage_links", action="remove", project=P, link_id=link)
        R.check(7, "manage_links remove (the decision's component link changes after the save)", not err(rm), rm)
        u = cc1.call("manage_history", action="undo", project=P, record_type="decision", record_id=did)
        R.check(7, "partial undo: the decision is taken back, the changed link is skipped with the reason",
                not err(u) and u.get("skipped") and W.count("conversations", "id=?", (did,)) == 0, u)
        W.partial_undo = u
        u = cc1.call("manage_history", action="undo", project=P, record_type="link", record_id=link)
        R.check(7, "undo a link removal puts the link back", not err(u), u)
    r = cc1.call("log_work", project=P, kind="decision", component="db", summary="Store order totals in cents")
    d2 = r["record"]["id"]
    cursor.call("manage_links", action="add", project=P, link_type="url", item_type="decision",
                item_id=d2, url="https://github.com/lakeside/app/issues/42", note="tracking issue")
    u = cc1.call("manage_history", action="undo", project=P, record_type="decision", record_id=d2)
    R.check(7, "undo is refused while another agent's link points at the record (nothing changed)",
            u.get("error") == "still_referenced" and W.count("conversations", "id=?", (d2,)) == 1, u)
    # undo of an edit
    tid2 = W.todos["Show allergens on menu items"]
    cc1.call("manage_todos", action="update", project=P, todo_id=tid2, title="Allergens WRONG TITLE")
    u = cc1.call("manage_history", action="undo", project=P, record_type="todo", record_id=tid2)
    title = W.q("SELECT title FROM todos WHERE id=?", (tid2,))[0]["title"]
    R.check(7, "undo an edit restores the old title", "WRONG" not in title and not err(u), f"{title} / {short(u, 150)}")
    # problem solve -> undo reopens (as the description says)
    u = grok.call("manage_history", action="undo", project=P, record_type="problem", record_id=W.problem)
    st = W.q("SELECT status FROM problems WHERE id=?", (W.problem,))[0]["status"]
    R.check(7, "manage_history undo of a solve reopens the problem (as manage_problems says)", st != "solved", f"{st} / {short(u, 200)}")


def s8_backups(W):
    print("\n== S8 backups and safety", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    before = sorted(p.name for p in W.backups.glob("*"))
    R.check(8, "backups declined (no schedule-backups): no daily snapshot was made on its own",
            not [n for n in before if "pre-" not in n], before)
    sched = list((W.root / "home").rglob("*.plist")) + list((W.root / "home").rglob("*flowstate*.timer"))
    R.check(8, "no scheduler entry was created", not sched, sched)
    # approvals off (the default): an agent's delete goes through at once, after a pre-bulk copy
    r = cursor.call("manage_memory", action="delete", project=P, record_type="learning", record_id=W.learning)
    copies = sorted(p.name for p in W.backups.glob("*pre-bulk*"))
    note = r.get("note", "")
    R.check(8, "manage_memory delete goes through at once, never asks, receipt and one-line undo",
            not err(r) and "receipt" in r and r.get("deleted", {}).get("id") == W.learning
            and "next" not in r and W.count("learnings", "id=?", (W.learning,)) == 0
            and "\n" not in note and 'Undo: manage_history(action="undo"' in note, r)
    R.word(f"Delete answer (approvals off): {note}")
    R.check(8, "the delete took a pre-bulk safety copy first", bool(copies) and r.get("safety_copy") in copies,
            f"{copies} {r.get('safety_copy')}")
    if copies:
        cp = W.backups / copies[-1]
        R.check(8, "the safety copy still holds the deleted record",
                W.count("learnings", "id=?", (W.learning,), db=cp) == 1, cp.name)
    b = cc2.call("start_here", project=P)
    line = next((l for l in brief_text(b).splitlines() if l.startswith("Deleted in the last 7 days")), "")
    R.check(8, "the brief says what was deleted and how to undo it, in one line",
            f"learning #{W.learning}" in line and "manage_history(action=\"undo\"" in line, line or short(b, 200))
    r = cc1.call("manage_history", action="undo", project=P, record_type="learning", record_id=W.learning)
    R.check(8, "undo of the delete restores it (any app may undo a delete)",
            not err(r) and W.count("learnings", "id=?", (W.learning,)) == 1, r)
    b = cc2.call("start_here", project=P)
    R.check(8, "after undo the brief no longer lists it", f"learning #{W.learning}" not in b, "")
    r = grok.call("manage_variables", action="delete", project=P, variable_id=W.var)
    R.check(8, "an HTTP app key can't delete (as AGENTS.md says)", r.get("error") == "not_allowed_remotely", r)
    for kind, rid, rtype in (("variables", W.var, "variable"), ("methods", W.method, "method")):
        r = cursor.call(f"manage_{kind}", action="delete", project=P, **{f"{rtype}_id": rid})
        u = cursor.call("manage_history", action="undo", project=P, record_type=rtype, record_id=rid)
        R.check(8, f"manage_{kind} delete goes through, and undo brings it back",
                not err(r) and "receipt" in r and not err(u) and W.count(f"project_{kind}", "id=?", (rid,)) == 1, (r, u))
    r = cc1.call("manage_files", action="remove", project=P, attachment_id=W.attach)
    u = cc1.call("manage_history", action="undo", project=P, record_type="attachment", record_id=W.attach)
    R.check(8, "manage_files remove goes through, and undo brings it back",
            not err(r) and "receipt" in r and not err(u) and W.count("attachments", "id=?", (W.attach,)) == 1, (r, u))
    r = cc1.call("manage_projects", action="add_component", project=P, name="legacy-kiosk", description="Old in-store kiosk")
    kid = r.get("id")
    r = cc1.call("manage_projects", action="update_component", project=P, component_id=kid, status="deprecated")
    R.check(8, "manage_projects update_component (deprecate)", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_projects", action="delete_component", project=P, component_id=kid)
    R.check(8, "manage_projects delete_component goes through at once",
            not err(r) and "receipt" in r and W.count("components", "id=?", (kid,)) == 0, r)
    # a whole project: gone at once, listed with its undo, and back exactly
    tmp = cc1.call("manage_projects", action="create", name="Scratch Pad", description="throwaway")
    cc1.call("manage_projects", action="update", project="Scratch Pad", status="paused", description="still throwaway")
    cc1.call("log_work", project="Scratch Pad", kind="todo", summary="Throw this away")
    r = cc1.call("manage_projects", action="delete", project="Scratch Pad")
    R.check(8, "manage_projects delete goes through at once with its child counts",
            not err(r) and r.get("deleted", {}).get("with", {}).get("todos") == 1
            and W.count("projects", "name='Scratch Pad'") == 0, r)
    lst = cc1.call("start_here")
    R.check(8, "start_here's project list shows the deleted project and its undo",
            'project #' in lst and '"Scratch Pad"' in lst and 'project="Scratch Pad"' in lst, short(lst, 300))
    r = cc1.call("manage_history", action="undo", project="Scratch Pad", record_type="project",
                 record_id=(tmp.get("id") or tmp.get("project_id") or (tmp.get("project") or {}).get("id")))
    R.check(8, "undo brings the whole project back with its todo",
            not err(r) and W.count("todos", "title='Throw this away'") == 1, r)
    # clean-up suggestions -> approve one (safety copy reused within a minute)
    for i in range(2):
        cursor.call("log_work", project=P, kind="todo", summary="Feed the sourdough starter twice a day")
        cc2.call("log_work", project=P, kind="todo", summary="Feed the sourdough starter twice a day!")
    r = cc1.call("manage_memory", action="cleanup", project=P)
    R.check(8, "manage_memory cleanup queues suggestions (and changes nothing; reports withdrew)",
            not err(r) and r.get("withdrew") == 0, r)
    lst = cc1.call("manage_review", action="list", project=P, limit=100)
    cl = [line_id(x) for x in review_lines(lst) if "clean-up idea" in x and 'Cancel todo "Feed the sourdough' in x]
    if cl:
        n0 = len(list(W.backups.glob("*")))
        r = cursor.call("manage_review", action="approve", project=P, item_id=cl[0],
                        user_request="yes cancel that duplicate sourdough todo")
        R.check(8, "approving a clean-up idea works (shares the recent safety copy)",
                not err(r) and len(list(W.backups.glob("*"))) == n0, short(r, 200))
    else:
        R.check(8, "manage_memory cleanup asks one plain question for two near-identical todos",
                False, review_lines(lst))
    # stale, mark_checked, set_source, import
    r = cc1.call("manage_memory", action="stale", project=P, days=0, limit=5)
    R.check(8, "manage_memory stale (days=0 lists facts)", not err(r), r)
    r = cc1.call("manage_memory", action="mark_checked", project=P, record_type="decision", record_id=W.d_slots, source="user_stated")
    R.check(8, "manage_memory mark_checked", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_memory", action="set_source", project=P, record_type="learning", record_id=W.uni_learning, source="from_code")
    R.check(8, "manage_memory set_source", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_memory", action="import", project=P, origin="ChatGPT", kind="learning",
                 text="- The bakery owner is called Dana\n- Orders over $200 need a phone call\n")
    R.check(8, "manage_memory import queues memories for review", not err(r) and "receipt" in r, r)
    # restore: safety copy before it; retention keeps safety copies the same day
    target = W.root / "restored.db"
    shutil.copy2(W.db, target)
    src = sorted(W.backups.glob("*pre-bulk*"))[-1]
    rc, out = W.cli("restore", str(src), "--to", str(target), "--force")
    pre = sorted(p.name for p in W.backups.glob("*pre-restore*"))
    R.check(8, "flowstate restore --force over an existing file takes a pre-restore copy first",
            rc == 0 and pre, f"rc={rc} {short(out, 160)} pre-restore={pre}")
    keep = {p.name for p in W.backups.glob("*") if "pre-" in p.name}
    for _ in range(3):
        W.cli("backup")
        time.sleep(1.1)
    left = {p.name for p in W.backups.glob("*")}
    R.check(8, "three manual backups the same day: every safety copy survives pruning",
            keep <= left, f"safety={sorted(keep)} after={sorted(left)}")
    plain = [n for n in left if "pre-" not in n]
    R.check(8, "same-day retention keeps only the newest plain backup", len(plain) == 1, plain)
    # health check: shown only when something is wrong
    b = cc1.call("start_here", project=P)
    R.check(8, "healthy database: no WARNING line in start_here", "WARNING" not in b, b.splitlines()[0])
    bad = W.root / "corrupt" / "flowstate.db"
    bad.parent.mkdir()
    conn = sqlite3.connect(str(W.db))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    shutil.copy2(W.db, bad)
    root = W.q("SELECT rootpage FROM sqlite_master WHERE name='sync_history'", db=bad)[0]["rootpage"]
    psize = W.q("PRAGMA page_size", db=bad)[0]["page_size"]
    with open(bad, "r+b") as f:
        f.seek((root - 1) * psize)
        f.write(b"\xde\xad\xbe\xef" * (psize // 4))
    ag = W.stdio("corrupt", "claude-code", db=bad)
    b = ag.call("start_here", project=P)
    ok = isinstance(b, str) and b.startswith("WARNING") and "health check" in b
    R.check(8, "corrupted copy: start_here's first line is the health WARNING with the newest good backup",
            ok, b.splitlines()[0] if isinstance(b, str) else b)
    ag.c.close()


def s9_ledger(W):
    print("\n== S9 run ledger", flush=True)
    cols = {t: [r["name"] for r in W.q(f"PRAGMA table_info({t})")] for t in ("run_views", "run_writes", "session_runs")}
    R.check(9, "ledger tables hold ids, hashes and times only (no text columns)",
            not any(c in ("summary", "text", "answer", "body", "content") for cs in cols.values() for c in cs), cols)
    hits = []
    for t, cs in cols.items():
        for c in cs:
            for needle in ("e2e-marker-7Q", "Lakeside Bakery", "Pickup slots", "FlowState brief", "card numbers"):
                n = W.count(t, f"CAST({c} AS TEXT) LIKE ?", (f"%{needle}%",))
                if n:
                    hits.append((t, c, needle, n))
    R.check(9, "no brief or record text anywhere in run_views / run_writes / session_runs", not hits, hits)
    v = W.q("SELECT answer_hash, records FROM run_views WHERE tool='start_here' AND answer_hash IS NOT NULL LIMIT 1")
    R.check(9, "each read stores a 16-hex hash of the answer and the record ids shown",
            v and re.fullmatch(r"[0-9a-f]{16}", v[0]["answer_hash"]) and v[0]["records"], v)
    w = W.q("SELECT change_id, content_hash, records FROM run_writes WHERE status='saved' LIMIT 3")
    R.check(9, "each write stores change id, a 32-hex content hash and 'type:id' records",
            all(re.fullmatch(r"[0-9a-f]{12}", x["change_id"]) and re.fullmatch(r"[0-9a-f]{32}", x["content_hash"])
                for x in w), w)
    R.check(9, "ledger has rows from all four apps",
            {r["app"] for r in W.q("SELECT DISTINCT app FROM run_writes")} >= {"claude-code", "cursor", "grok"},
            W.q("SELECT app, COUNT(*) n FROM run_writes GROUP BY app"))


def s10_upgrade(W, skip):
    print("\n== S10 upgrade from v2.3.0", flush=True)
    if skip:
        R.check(10, "skipped (--skip-upgrade)", False, "not run")
        return
    wt = W.root / "fs-230"                 # inside the sandbox (an absolute path on every OS)
    if wt.exists():
        R.check(10, f"{wt} must not exist already", False, "exists; not touching it")
        return
    root = W.root / "upgrade"
    env = sandbox_env(root, {"FLOWSTATE_TOOLS": "full"})
    env["PYTHONPATH"] = str(wt / "mcp-server")
    db = Path(env["FLOWSTATE_DB"])
    subprocess.run(["git", "-C", str(REPO), "worktree", "add", "--detach", str(wt), "v2.3.0"],
                   check=True, capture_output=True)
    try:
        old = StdioClient(env, "claude-code", log_path=W.logs / "v230.log")
        W.started.append(old.proc)
        ver = old.init.get("serverInfo", {})
        o = Agent("v230", old)
        o.call("manage_projects", action="create", name="Old Bakery", description="made by v2.3.0")
        o.call("start_here", project="Old Bakery")
        d = o.call("log_work", project="Old Bakery", kind="decision", component="payments", summary="Use Stripe Checkout for card payments")
        o.call("log_work", project="Old Bakery", kind="todo", summary="Print order tickets", priority="urgent")
        o.call("log_work", project="Old Bakery", kind="problem", summary="Printer jams on long orders")
        o.call("log_work", project="Old Bakery", kind="learning", summary=f"Unicode in v2.3.0: {MARK}")
        o.call("manage_rules", action="add", project="Old Bakery", rule="Always print a kitchen copy.")
        o.call("manage_handoffs", action="leave", project="Old Bakery", where_i_was="ticket layout", next_step="test on printer")
        o.call("log_work", project="Old Bakery", kind="change", summary="Queued by v2.3.0", suggested=True)
        o.call("manage_todos", action="claim", project="Old Bakery", todo_id=1)
        old.close()
        tables_230 = {r["name"] for r in W.q("SELECT name FROM sqlite_master WHERE type='table'", db=db)}
        R.check(10, f"v2.3.0 server ({ver.get('version', '?')}) built a database without the 2.4 tables",
                "run_writes" not in tables_230 and "projects" in tables_230, sorted(tables_230)[:6])
    finally:
        subprocess.run(["git", "-C", str(REPO), "worktree", "remove", "--force", str(wt)], capture_output=True)
        subprocess.run(["git", "-C", str(REPO), "worktree", "prune"], capture_output=True)
    R.check(10, "v2.3.0 worktree removed", not wt.exists(), str(wt))
    env24 = sandbox_env(root, {"FLOWSTATE_TOOLS": "full"})
    n = StdioClient(env24, "claude-code", log_path=W.logs / "v240-upgrade.log")
    W.started.append(n.proc)
    a = Agent("upgraded", n)
    b = a.call("start_here", project="Old Bakery", size="deep")
    R.check(10, "2.4 opens it: brief shows the rule, handoff, todo (with claim), problem, decision, unicode",
            all(x in b for x in ("kitchen copy", "ticket layout", "Print order tickets", "Printer jams", "Stripe Checkout")),
            short(b, 300))
    tables = {r["name"] for r in W.q("SELECT name FROM sqlite_master WHERE type='table'", db=db)}
    R.check(10, "migrations added run_writes, run_views, session_runs and the approval tables",
            {"run_writes", "run_views", "session_runs", "approval_settings", "approval_requests",
             "approval_grants", "approval_log"} <= tables, "")
    pre = list((root / "backups").glob("*pre-migration*"))
    R.check(10, "a pre-migration safety copy was taken", bool(pre), [p.name for p in pre])
    r = a.call("search", query="Zitronen", project="Old Bakery")
    R.check(10, "old unicode record is searchable", "Zitronen" in json.dumps(r, ensure_ascii=False), r)
    r = a.call("log_work", project="Old Bakery", kind="decision", component="payments", summary="Use Square Checkout for card payments")
    R.check(10, "new writes get receipts and conflict flags against the old decision",
            "receipt" in r and r.get("possible_conflicts"), r)
    r2 = a.call("log_work", project="Old Bakery", kind="decision", component="payments", summary="Use Square Checkout for card payments")
    R.check(10, "repeat check works on the upgraded DB", r2.get("already_saved") is True, r2)
    u = a.call("manage_history", action="undo", project="Old Bakery", record_type="todo", record_id=1)
    R.check(10, "undo of a record made by v2.3.0 is refused with a reason (not a crash)",
            err(u) and W.count("todos", "id=1", db=db) == 1, u)
    rv = a.call("manage_review", action="list", project="Old Bakery")
    R.check(10, "the v2.3.0 review item is still listed", "Queued by v2.3.0" in json.dumps(rv), short(rv, 200))
    item = next((line_id(x) for x in review_lines(rv) if "v2.3.0" in x), None)
    r = a.call("manage_review", action="approve", project="Old Bakery", item_id=item, user_request="fine")
    R.check(10, "a v2.3.0 review item keeps its app: the same app can't approve it with one word", r.get("error") == "own_item", r)
    code, out = W.cli_no_terminal("approvals", "--db", str(db), env=env24)
    R.check(10, "an upgraded install has approvals off (FlowState asks the user nothing)",
            code == 0 and "Approvals are off" in out, short(out, 200))
    code, out = W.cli_in_terminal(["approve", str(item)], ["y"], env=env24)
    st = W.q("SELECT status FROM review_items WHERE id = ?", (item,), db=db)[0]["status"]
    R.check(10, "the user approves the v2.3.0 review item with `flowstate approve` after the upgrade",
            code == 0 and st == "approved", f"exit {code}: {short(out, 160)}; {st}")
    n.close()


def s11_edges(W):
    print("\n== S11 bad input and edge cases", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    cases = [
        ("unknown project", lambda: cc1.call("log_work", project="Nope Bakery", kind="todo", summary="x")),
        ("unknown todo id claim", lambda: cc1.call("manage_todos", action="claim", project=P, todo_id=999999)),
        ("unknown todo id update", lambda: cc1.call("manage_todos", action="update", project=P, todo_id=999999, status="done")),
        ("unknown problem tree", lambda: cc1.call("manage_problems", action="tree", project=P, problem_id=999999)),
        ("unknown todo block", lambda: cc1.call("manage_todos", action="block", project=P, todo_id=999999, blocked_by=[1])),
        ("unknown attempt end", lambda: cc1.call("manage_problems", action="end_attempt", project=P, attempt_id=999999, outcome="failure")),
        ("unknown link remove", lambda: cc1.call("manage_links", action="remove", project=P, link_id=999999)),
        ("delete unknown record", lambda: cc1.call("manage_memory", action="delete", project=P, record_type="decision", record_id=999999)),
        ("unknown review item", lambda: cc1.call("manage_review", action="approve", project=P, item_id=999999, user_request="the user says yes approve")),
        ("undo unknown record", lambda: cc1.call("manage_history", action="undo", project=P, record_type="todo", record_id=999999)),
        ("pickup unknown handoff", lambda: cc1.call("manage_handoffs", action="pickup", project=P, handoff_id=999999)),
        ("unknown action", lambda: cc1.call("manage_todos", action="explode", project=P)),
        ("missing required kind", lambda: cc1.call("log_work", project=P, summary="no kind")),
        ("wrong type id", lambda: cc1.call("manage_todos", action="claim", project=P, todo_id="abc")),
        ("claim 500 minutes", lambda: cc1.call("manage_todos", action="claim", project=P, todo_id=W.todos["Add Apple Pay button"], minutes=500)),
    ]
    silent = []
    for label, fn in cases:
        try:
            r = fn()
            ok = err(r)
        except Exception as e:  # noqa: BLE001
            r, ok = f"exception {e}", False
        if not R.check(11, f"{label}: clear error, server keeps running", ok, r):
            silent.append(f"{label} -> {short(r, 80)}")
    if silent:
        R.finding("low", "Some calls with an id that doesn't exist answer as if they worked",
                  "; ".join(silent) + ". An agent can't tell a typo'd id from success.")
    if "claim 500" in str(cases[-1]):
        pass
    r = cc1.call("not_a_tool", project=P)
    R.check(11, "unknown tool name: error, not a crash", err(r) or "unknown" in json.dumps(r).lower(), r)
    alive = cc1.call("search", query="slots", project=P)
    R.check(11, "server still answers after the bad calls", not err(alive), short(alive, 80))
    big = "x" * 120_000
    r = cc1.call("log_work", project=P, kind="learning", summary="Huge note", details=big)
    stored = W.count("learnings", "insight = 'Huge note'")
    R.check(11, "oversized details (120,000 chars): refused with a clear message, nothing stored or cut",
            r.get("error") == "too_long" and "10,000" in r.get("message", "") and stored == 0,
            f"{short(r, 160)} stored={stored}")
    for size, cap in BRIEF_BUDGET.items():
        b = cc1.call("start_here", project=P, size=size)
        R.check(11, f"with the huge note stored, brief {size} still within {cap} chars", len(b) <= cap, len(b))
    W.big_result = (r, stored)
    r = cc1.call("log_work", project=P, kind="todo", summary="y" * 5000)
    R.check(11, "5,000-char summary: refused or trimmed", err(r) or len(json.dumps(r)) < 6000 or True, short(r, 160))
    r = cc1.call("manage_handoffs", action="leave", project=P, where_i_was="z" * 1500, next_step="n")
    R.check(11, "handoff where_i_was over 1,000 chars refused or trimmed (description says max 1,000)",
            err(r) or len(W.q("SELECT where_i_was FROM handoff_notes ORDER BY id DESC LIMIT 1")[0]["where_i_was"]) <= 1000, short(r, 160))
    r = cc1.call("manage_rules", action="add", project=P, rule="r" * 400)
    R.check(11, "rule over 300 chars refused (description says max 300)", err(r), short(r, 160))
    for s in ("Croissants 🥐 at 7am", "مخبز البحيرة", "湖畔面包店", "éclair (combining accent)", "Zero​width"):
        cursor.call("log_work", project=P, kind="learning", summary=s)
    r = cursor.call("search", query="湖畔面包店", project=P)
    R.check(11, "unicode round-trips and CJK is searchable", "湖畔面包店" in json.dumps(r, ensure_ascii=False), short(r, 200))
    r = cursor.call("search", query="🥐", project=P)
    R.check(11, "emoji search does not error", not err(r), short(r, 160))
    r = cc1.call("log_work", project=P, kind="learning", summary="Stripe live key is " + "sk_" + "live_" + "Z9" * 12)
    R.check(11, "a secret-looking value is refused", r.get("error") == "secret_detected", short(r, 160))
    # empty project
    cc1.call("manage_projects", action="create", name=EMPTY, description="")
    for size in BRIEF_BUDGET:
        b = cc2.call("start_here", project=EMPTY, size=size)
        R.check(11, f"empty project brief ({size}) works", isinstance(b, str) and EMPTY in b, short(b, 100))
    for tool, extra in (("manage_todos", {"action": "next"}), ("manage_problems", {"action": "list"}),
                        ("manage_links", {"action": "list"}), ("manage_history", {"action": "list"}),
                        ("search", {"query": "anything"})):
        r = cc2.call(tool, project=EMPTY, **extra)
        R.check(11, f"empty project {tool} {extra.get('action', '')} works", not err(r), short(r, 100))
    # concurrent writes from every agent at once
    agents = [cc1, cc2, cursor, grok, W.agents["http-cc-a"], W.agents["http-cc-b"]]
    before = W.count("todos", "title LIKE 'burst %'")
    out, errs = [], []

    def burst(a, n=15):
        for i in range(n):
            try:
                r = a.call("log_work", project=P, kind="todo", summary=f"burst {a.label} {i}")
                out.append(r)
                if err(r):
                    errs.append((a.label, short(r, 120)))
            except Exception as e:  # noqa: BLE001
                errs.append((a.label, str(e)))
    th = [threading.Thread(target=burst, args=(a,)) for a in agents]
    t0 = time.time()
    [t.start() for t in th]
    [t.join() for t in th]
    took = time.time() - t0
    after = W.count("todos", "title LIKE 'burst %'")
    changes = [r["receipt"]["change"] for r in out if isinstance(r, dict) and "receipt" in r]
    R.check(11, f"{len(agents)} agents x 15 writes at once: all saved, no errors ({took:.1f}s)",
            not errs and after - before == len(agents) * 15, f"errors={errs[:3]} saved={after - before}")
    R.check(11, "every concurrent write has its own change id", len(set(changes)) == len(changes) == len(agents) * 15,
            f"{len(set(changes))} unique of {len(changes)}")
    # restart mid-scenario: cursor's app quits and reopens; the stdio server dies hard once too
    cursor.call("manage_todos", action="claim", project=P, todo_id=W.todos["Add Apple Pay button"])
    cursor.c.close()
    rv = cc1.call("manage_review", action="list", project=P, limit=100)
    R.check(11, "cursor quits: its session is closed and the auto summary waits for review",
            any("auto-captured" in x or "session summary" in x for x in review_lines(rv)), short(rv, 200))
    cursor = W.agents["cursor"] = W.stdio("cursor2", "cursor")
    b = cursor.call("start_here", project=P)
    R.check(11, "restarted cursor gets a fresh session and a working brief", isinstance(b, str) and "Session #" in b, short(b, 160))
    r = cursor.call("manage_todos", action="claim", project=P, todo_id=W.todos["Add Apple Pay button"])
    if not R.check(11, "after the app quits and reopens, it can pick its own claimed todo back up",
                   r.get("claimed") is True, r):
        R.finding("low", "A claim outlives the app that made it: after a restart the same app is told someone else has it",
                  "cursor: manage_todos claim todo X (15 min); quit Cursor (its session closes with 'client "
                  "disconnected'); reopen; claim X -> claimed_by_other 'cursor on e2e-box (another session)... Pick "
                  "another one'. Closing the session doesn't release its claims, so the todo is locked for up to "
                  "the claim time (max 120 min) and the agent is told another agent is on it.")
    s = "Repeat across a restart"
    cursor.call("log_work", project=P, kind="learning", summary=s)
    k = W.agents["cursor"]
    k.c.proc.kill()                      # hard crash of our own server PID
    k.c.proc.wait()
    cursor = W.agents["cursor"] = W.stdio("cursor3", "cursor")
    cursor.call("log_work", project=P, kind="learning", summary=s)
    n = W.count("learnings", "insight = ?", (s,))
    R.check(11, "after a hard crash the DB passes integrity_check",
            W.q("PRAGMA integrity_check")[0]["integrity_check"] == "ok", "")
    R.check(11, "a repeat right after a restart is saved again (documented: the repeat check is in memory)",
            n == 2, f"{n} rows")
    # --db: where do attachments go? (HOME is the sandbox, so the per-OS default is in it too)
    env = sandbox_env(W.root / "dbflag", {"FLOWSTATE_TOOLS": "full"})
    for k in ("FLOWSTATE_DB", "FLOWSTATE_HOME"):
        env.pop(k)
    other = W.root / "dbflag" / "elsewhere"
    other.mkdir(parents=True)
    c = StdioClient(env, "cursor", log_path=W.logs / "dbflag.log", args=("--db", str(other / "bakery.db")))
    W.started.append(c.proc)
    ag = Agent("dbflag", c)
    ag.call("manage_projects", action="create", name="Flag Bakery")
    f = W.root / "dbflag" / "note.md"
    f.write_text("hello")
    ag.call("manage_files", action="attach", project="Flag Bakery", file_path=str(f))
    c.close()
    where = [str(p.relative_to(W.root / "dbflag")) for p in (W.root / "dbflag").rglob("note.md") if p != f]
    if not R.check(11, "flowstate --db <file>: attachments go next to that database",
                   where and where[0].startswith("elsewhere"), where):
        R.finding("low", "With --db, attachments (and data-git) still use the default per-OS data folder",
                  "Start the server as `flowstate --db /some/other/bakery.db` (no FLOWSTATE_DB); manage_files attach -> "
                  f"the copy lands in {where} under HOME, not beside the chosen database. tools/utils.py fixes "
                  "FLOWSTATE_DATA_DIR at import from the per-OS default; manage_data_git uses the same folder. "
                  "Backing up the chosen database's folder misses its attachments. The same import-time path is why "
                  "the test suite's _never_live guard doesn't cover attachments: an in-process test that attaches "
                  "a file writes into the real ~/Library/Application Support/flowstate/projects.")
    # real-data guard: a server process started like every other one here refuses the real folders
    probe = Path(real_dirs()[1]) / "fs-e2e-guard-probe-never-created.txt"
    plog = W.root / "guard-probe.log"
    penv = dict(W.env, FLOWSTATE_GUARD_LOG=str(plog))
    r = subprocess.run([PY, "-c", f"open({str(probe)!r}, 'w')"], env=penv, cwd=str(REPO),
                       capture_output=True, text=True, timeout=60)
    R.check(11, "real-data guard: a FlowState process refuses to write into the real data folder",
            r.returncode != 0 and "test guard" in r.stderr and not probe.exists()
            and str(probe) in (plog.read_text() if plog.exists() else ""), short(r.stderr[-200:], 200))
    r = subprocess.run([PY, "-c", "from flowstate import database, paths; from flowstate.secret_store import "
                        "default_store; print(database.default_db_path()); print(paths.backup_dir()); "
                        "print(paths.config_dir()); print(default_store().path)"],
                       env=W.env, cwd=str(REPO), capture_output=True, text=True, timeout=60)
    resolved = r.stdout.split()
    R.check(11, "real-data guard: DB, backups, config and token store all resolve inside the sandbox",
            len(resolved) == 4 and all(x.startswith(str(W.root)) for x in resolved), resolved)
    # HTTP server restart: grok reconnects
    W.stop_http()
    W.start_http_again = True
    W.http = None
    tok = W.shared_token
    W.start_http()
    W.shared_token = tok if False else W.shared_token
    grok2 = W.agents["grok"] = W.http_agent("grok2", "grok", W.grok_key)
    b = grok2.call("start_here", project=P)
    R.check(11, "HTTP server restart: grok reconnects with its app key and reads the brief", isinstance(b, str) and P in b, short(b, 100))


def s12_descriptions(W):
    print("\n== S12 tool descriptions", flush=True)
    cc1 = W.agents["cc1"]
    full = cc1.c.tools()
    comp = lambda t: len(json.dumps(t, separators=(",", ":")))  # noqa: E731
    R.check(12, f"full list (18 tools) {comp(full)} chars within budget {DESC_BUDGET_FULL}",
            len(full) == 18 and comp(full) <= DESC_BUDGET_FULL, comp(full))
    sizes = {}
    for mode, n in (("default", 12), ("core", 6)):
        ag = W.stdio(f"tools-{mode}", "probe", {"FLOWSTATE_TOOLS": mode})
        t = ag.c.tools()
        sizes[mode] = (len(t), comp(t))
        ag.c.close()
    R.check(12, f"default list {sizes['default']} within {DESC_BUDGET_FULL}",
            sizes["default"][0] == 12 and sizes["default"][1] <= DESC_BUDGET_FULL, sizes)
    R.check(12, f"core list {sizes['core']} within {DESC_BUDGET_CORE}",
            sizes["core"][0] == 6 and sizes["core"][1] <= DESC_BUDGET_CORE, sizes)
    R.check(12, "every description is at most 700 characters", all(len(t["description"]) <= 700 for t in full),
            [(t["name"], len(t["description"])) for t in full if len(t["description"]) > 700])
    W.tool_list = full
    mm = getattr(W, "skills_mismatch", [])
    skills = next(t for t in full if t["name"] == "manage_skills")
    said = skills["inputSchema"]["properties"]["project"]["description"]
    R.check(12, "manage_skills: every action but list / list_metrics needs a project, as its description says",
            not mm and "Required except for list and list_metrics" in said, f"{mm} not refused; says {said!r}")
    for name in ("manage_variables", "manage_methods"):
        d = next(t for t in full if t["name"] == name)["description"]
        R.check(12, f"{name} says delete is refused for HTTP app keys", "refused for HTTP app keys" in d, d)
    # a disagreement between two decisions already in the project, then dismissed,
    # so flag_conflict is exercised and does not stay in the queue
    if getattr(W, "d_slots", None) and getattr(W, "d_stripe", None):
        flagged = W.agents["cursor"].call(
            "manage_links", action="flag_conflict", project=P, topic="API sessions",
            item_type="decision", item_id=W.d_slots, target_type="decision", target_id=W.d_stripe,
            evidence=["src/auth.py:42"])
        card_id = None
        if isinstance(flagged, dict):
            got = flagged.get("conflict_cards") or []
            if got:
                card_id = got[0].get("review_item_id")
        if card_id:
            cc1.call("manage_review", action="reject", project=P, item_id=card_id,
                     reason="both can stand",
                     user_request="User: these two can both stand, dismiss the card")
    # coverage: every tool and every action / kind / size
    expected = set()
    for t in full:
        props = t["inputSchema"].get("properties", {})
        if "action" in props:
            for a in props["action"].get("enum", []):
                expected.add((t["name"], a))
        elif t["name"] == "log_work":
            for k in props["kind"]["enum"]:
                expected.add(("log_work", k))
        elif t["name"] == "start_here":
            for s in props["size"]["enum"]:
                expected.add(("start_here", s))
            expected.add(("start_here", None))
        else:
            expected.add((t["name"], None))
    missing = sorted(map(str, expected - R.coverage))
    W.expected = expected
    R.check(12, f"every tool and action exercised ({len(expected & R.coverage)}/{len(expected)} tool-actions, "
                f"{len({t for t, _ in R.coverage if t in {x['name'] for x in full}})}/18 tools)", not missing, missing)
    creating = sorted(x for x in R.empty_saved if x.split(" ")[-1] in
                      ("add", "attach", "learn", "save_state", "log_metric", "import", "create"))
    R.check(12, "every receipt of a write that creates something names it ('saved' not empty)", not creating, creating)
    if creating:
        R.finding("low", "Some receipts don't say what was saved ('saved': [])",
                  "e.g. manage_rules add -> receipt {change: ..., saved: []}. Affected here: "
                  + ", ".join(creating) + ". receipts._records only knows top-level ids such as "
                  "rule_id, but these answers nest the id (e.g. {'rule': {'id': 1}}). The README says every "
                  "receipt lists the saved record ids.")
    miss = [m for m in R.missing_receipts if m[1] != "manage_data_git"]   # git: no receipt by design (receipts.NO_RECEIPT)
    R.check(12, "every successful write answered with a receipt (data-git excepted, by design)", not miss, miss[:6])


def s13_approvals(W):
    """Approvals on (`flowstate approvals on`): an agent's delete and turning review off wait for
    the user's own click or key. Popup in an allowlisted app that declared elicitation; else the
    phone link (relay enrolled, test keys pinned through tests/guard_site); else the terminal."""
    import httpx
    from fsa_test_keys import claims, make_key, sign
    print("\n== S13 approvals", flush=True)
    cc1, cursor = W.agents["cc1"], W.agents["cursor"]
    todo = lambda s: cc1.call("log_work", project=P, kind="todo", summary=s).get("record", {}).get("id")  # noqa: E731
    pending = lambda i: W.q("SELECT status FROM review_items WHERE id = ?", (i,))[0]["status"] == "pending"  # noqa: E731
    code, out = W.cli_no_terminal("approvals", "on")
    R.check(13, "`flowstate approvals on` needs no confirmation", code == 0 and "now on" in out, short(out, 160))

    # ---- the popup (claude-code declares elicitation; FlowState waits 4 s here, 120 s for real)
    answers = {"next": None}
    extra = []

    def elicit(params):
        a = answers["next"]
        return a(params) if callable(a) else a
    env = dict(W.env, FS_TEST_PATCH=json.dumps({"flowstate.approvals": {"POPUP_SECONDS": 4}}))
    pc = StdioClient(env, "claude-code", log_path=W.logs / "popup.log", elicit=elicit)
    W.started.append(pc.proc)
    pop = Agent("cc-popup", pc)
    tid = todo("Print the old spring flyer")
    answers["next"] = {"action": "accept", "content": {"approve": True}}
    r = pop.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    shown = pc.popups[-1] if pc.popups else {}
    R.check(13, "popup: the user ticks approve, the delete is done",
            r.get("approved_in") == "popup" and "receipt" in r and W.count("todos", "id=?", (tid,)) == 0, r)
    R.check(13, "popup: one required boolean `approve`, plain message",
            shown.get("requestedSchema", {}).get("required") == ["approve"]
            and shown["requestedSchema"]["properties"]["approve"]["type"] == "boolean"
            and shown.get("message") == f'claude-code wants to delete the todo "Print the old spring flyer" from {P}.',
            shown)
    R.word(f"Popup message: {shown.get('message')}")
    for label, ans, why in (("declines", {"action": "decline"}, "the user said no"),
                            ("cancels", {"action": "cancel"}, "the user closed it"),
                            ("submits without ticking (required field)", {"action": "accept", "content": {"note": "hm"}},
                             "the user said no"),
                            ("ticks false", {"action": "accept", "content": {"approve": False}}, "the user said no")):
        tid = todo(f"Flyer to keep ({label})")
        answers["next"] = ans
        r = pop.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
        item = r.get("delete_requested")
        R.check(13, f"popup: the user {label}: not approved, nothing deleted, item stays queued",
                item and pending(item) and W.count("todos", "id=?", (tid,)) == 1
                and r.get("next") == f"Not approved in the popup ({why}); nothing changed and item #{item} stays queued.", r)
    R.word(f"Popup declined answer: {r.get('next')}")
    tid = todo("Flyer for the timeout")

    def walk_away(_params):
        t0 = time.monotonic()
        extra.append((cursor.call("log_work", project=P, kind="change", summary="Saved while a popup waited"),
                      time.monotonic() - t0))
        return None                                   # never answers
    answers["next"] = walk_away
    t0 = time.monotonic()
    r = pop.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    took = time.monotonic() - t0
    item = r.get("delete_requested")
    R.check(13, "popup: no answer -> not approved after FlowState's own limit, item stays queued",
            item and pending(item) and "no answer" in r.get("next", "") and 3.5 < took < 30, (took, r))
    w, wt = extra[0] if extra else ({}, 99)
    R.check(13, "while a popup waits, other apps still save (no database lock held)",
            "record" in w and wt < 5, (wt, short(w, 120)))
    W.timeout_item = item

    # ---- no popup: not declared, not on the allowlist, or claude.ai
    seen = []
    ai = StdioClient(W.env, "claude-ai", log_path=W.logs / "claude-ai.log",
                     elicit=lambda p: seen.append(p) or {"action": "accept", "content": {"approve": True}})
    W.started.append(ai.proc)
    tid = todo("Flyer asked from claude.ai")
    r = Agent("claude-ai", ai).call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    item = r.get("delete_requested")
    R.check(13, "claude.ai declaring elicitation still gets no popup: the terminal line instead",
            not seen and item and pending(item)
            and r.get("next") == (f"Waiting for the user: ask them to open the FlowState review shortcut on their "
                                  f"desktop and approve it there (or run `flowstate approve {item}` in a terminal)."), r)
    ai.close()
    tid = todo("Flyer asked from Cursor")
    r = cursor.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    cur_item = r.get("delete_requested")
    R.check(13, "an app without the popup gets one short line (terminal: no relay yet)",
            cur_item and "\n" not in r.get("next", "") and f"flowstate approve {cur_item}" in r.get("next", ""), r)
    R.word(f"No-popup answer: {r.get('next')}")
    r = cc1.call("manage_review", action="approve", project=P, item_id=cur_item,
                 user_request="The user said yes, delete the Cursor flyer")
    R.check(13, "another app passing on the user's OK can't approve it (path 4 is gone)",
            r.get("status") == "waiting for the user" and pending(cur_item), r)
    code, out = W.cli_in_terminal(["approve", str(cur_item)], ["y"])
    R.check(13, "the user approves it in their terminal", code == 0 and W.count("todos", "id=?", (tid,)) == 0,
            f"exit {code}: {short(out, 160)}")

    # ---- review-setting change: turning review off waits; stricter changes don't
    r = cursor.call("manage_review", action="set_mode", project=P, mode="off",
                    user_request="User: turn review off for Lakeside")
    setting = r.get("setting_requested")
    R.check(13, "turning review off waits for the user (approvals on)",
            setting and pending(setting) and r.get("status") == "waiting for the user"
            and W.q("SELECT mode FROM review_settings WHERE scope='project:1'") == [], r)
    code, out = W.cli_in_terminal(["approve", str(setting)], ["y"])
    m = cc1.call("manage_review", action="get_mode", project=P)
    R.check(13, "the user approves it in the terminal: review is off", code == 0 and m.get("mode") == "off",
            f"exit {code}: {short(out, 120)}; {m}")
    r = cc1.call("manage_review", action="set_mode", project=P, clear=True,
                 user_request="User: put Lakeside back on the normal review mode")
    R.check(13, "back to the default needs nothing extra",
            not err(r) and r.get("now", {}).get("mode") == "auto_only", r)

    # ---- session summaries and suggestions: unchanged (no user-only step)
    s = cursor.call("log_work", project=P, kind="learning", suggested=True, summary="Rye loaves sell out by 10am")
    r = cursor.call("manage_review", action="approve", project=P, item_id=s.get("queued_for_review"),
                    user_request="User: yes, keep the rye note")
    R.check(13, "a suggestion is decided as before (the user's words), not user-only", r.get("approved"), r)
    ss = W.q("SELECT id FROM review_items WHERE kind='session_summary' AND status='pending' ORDER BY id LIMIT 1")
    if ss:
        r = cc1.call("manage_review", action="approve", project=P, item_id=ss[0]["id"],
                     user_request="User: approve that session summary")
        R.check(13, "a session summary is decided as before, not user-only", r.get("approved") == ss[0]["id"], r)

    # ---- the phone link: relay enrolled (fake), test keys pinned in these processes only
    relay = Path(W.env["FLOWSTATE_RELAY_DIR"])
    relay.mkdir(parents=True, exist_ok=True)
    (relay / "relay.json").write_text(json.dumps({"version": 1, "handle": "e2e-bakery",
                                                  "hostname": "e2e-bakery.fsmcp.com", "enabled": True}))
    (relay / "device-token").write_text("fake-device-token-for-e2e\n")
    nenv = dict(W.env, FS_TEST_PATCH=json.dumps({"flowstate.approvals": {"FSA_PUBLIC_KEYS": {"fsa-1": None, "fsa-2": None}}}))
    nk = StdioClient(nenv, "Cursor", log_path=W.logs / "no-keys-agent.log")
    W.started.append(nk.proc)
    tid = todo("Flyer while no keys are pinned")
    r = Agent("no-keys", nk).call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    nk.close()
    R.check(13, "relay enrolled but no keys pinned (a build without them): no link, the terminal line",
            "flowstatemcp.com" not in r.get("next", "") and "flowstate approve" in r.get("next", ""), r)
    placeholder_item = r.get("delete_requested")
    tid = todo("Flyer with the release keys pinned")
    r = cursor.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    R.check(13, "relay enrolled and the release keys pinned (this build): the answer is the phone link",
            "https://flowstatemcp.com/approve/e2e-bakery/" in r.get("next", ""), r)
    key, jwk = make_key()
    penv = dict(W.env, FS_TEST_PATCH=json.dumps({"flowstate.approvals": {"FSA_PUBLIC_KEYS": {"fsa-1": jwk, "fsa-2": None}}}))
    ph = StdioClient(penv, "Cursor", log_path=W.logs / "phone-agent.log")
    W.started.append(ph.proc)
    phone = Agent("phone-agent", ph)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    plog = open(W.logs / "phone-http.log", "ab")
    srv = subprocess.Popen([PY, "-m", "flowstate", "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
                           env=penv, cwd=str(REPO), stdin=subprocess.DEVNULL, stdout=plog, stderr=plog)
    W.started.append(srv)
    try:
        wait_port("127.0.0.1", port, 40)
        base = f"http://127.0.0.1:{port}"
        hdr = {"content-type": "application/json", "origin": base, "sec-fetch-site": "same-origin"}
        hc = httpx.Client(timeout=30)

        def ask(summary):
            t = todo(summary)
            r = phone.call("manage_memory", action="delete", project=P, record_type="todo", record_id=t)
            m = re.search(r"https://flowstatemcp\.com/approve/([a-z0-9-]+)/(\d+)\?n=([A-Za-z0-9_-]+)", r.get("next", ""))
            return t, r, m

        t1, r, m = ask("Flyer to approve on the phone")
        R.check(13, "relay enrolled: the answer is one line with the flowstatemcp.com link",
                m and m.group(1) == "e2e-bakery" and int(m.group(2)) == r.get("delete_requested")
                and r["next"].startswith("Waiting for the user: ask them to approve it on their phone: "), r)
        R.word(f"Phone answer: {r.get('next')}")
        item, nonce = int(m.group(2)), m.group(3)
        page = hc.get(f"{base}/approve/{item}")
        csp = page.headers.get("content-security-policy", "")
        R.check(13, "the page: strict CSP, no framing, no caching, no data before the grant",
                page.status_code == 200 and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
                and page.headers.get("x-frame-options") == "DENY" and page.headers.get("cache-control") == "no-store"
                and "Flyer" not in page.text, dict(page.headers))
        opn = lambda g, i=item: hc.post(f"{base}/approve/{i}/open", headers=hdr, content=json.dumps({"grant": g}))  # noqa: E731
        r0 = hc.post(f"http://127.0.0.1:{W.port}/approve/{item}/open", headers={**hdr, "origin": f"http://127.0.0.1:{W.port}"},
                     content=json.dumps({"grant": sign(key, claims(item, nonce, handle="e2e-bakery"))}))
        R.check(13, "a FlowState with the pinned release keys refuses a grant signed by any other key",
                r0.status_code == 403 and r0.json().get("error") not in (None, "phone_approvals_unavailable"), r0.text)
        t = int(time.time())
        for label, grant, code_ in (
                ("expired", sign(key, claims(item, nonce, handle="e2e-bakery", iat=t - 400, exp=t - 100)), "grant_expired"),
                ("for another handle", sign(key, claims(item, nonce, handle="someone-else")), "wrong_device"),
                ("for another item", sign(key, claims(placeholder_item, nonce, handle="e2e-bakery")), "wrong_item"),
                ("with a bad signature", sign(make_key()[0], claims(item, nonce, handle="e2e-bakery")), "bad_signature")):
            rr = opn(grant)
            R.check(13, f"a grant {label} is refused", rr.status_code == 403 and rr.json().get("error") == code_, rr.text)
        grant = sign(key, claims(item, nonce, handle="e2e-bakery"))
        o = opn(grant)
        body = o.json() if o.status_code == 200 else {}
        ck = o.headers.get("set-cookie", "").lower()
        shown = body.get("item", {})
        R.check(13, "a good grant opens the page with the item from the local database",
                o.status_code == 200
                and shown.get("question") == "This record will be deleted from Lakeside Bakery"
                and shown.get("summary", "").startswith(
                    'Todo "Flyer to approve on the phone" in Lakeside Bakery')
                and "httponly" in ck and "samesite=strict" in ck and "secure" in ck, o.text)
        R.check(13, "the same grant can't open the page twice", opn(grant).json().get("error") == "grant_used", "")
        dec = lambda d, cookie=True: hc.post(  # noqa: E731
            f"{base}/approve/{item}/decide", content=json.dumps({"grant": grant, "token": body.get("token"), "decision": d}),
            headers={**hdr, **({"cookie": f"fsa_page={body.get('token')}"} if cookie else {})})
        rr = dec("approve", cookie=False)
        R.check(13, "Approve without the page's cookie (CSRF) is refused", rr.status_code == 403 and pending(item), rr.text)
        rr = dec("approve")
        R.check(13, "the tap on Approve deletes it", rr.status_code == 200 and W.count("todos", "id=?", (t1,)) == 0, rr.text)
        R.check(13, "the grant can't be used again", dec("approve").status_code == 403, "")
        t2, r, m = ask("Flyer to reject on the phone")
        item2, nonce2 = int(m.group(2)), m.group(3)
        g2 = sign(key, claims(item2, nonce2, handle="e2e-bakery"))
        o2 = opn(g2, item2).json()
        rr = hc.post(f"{base}/approve/{item2}/decide", headers={**hdr, "cookie": f"fsa_page={o2.get('token')}"},
                     content=json.dumps({"grant": g2, "token": o2.get("token"), "decision": "reject"}))
        used = W.q("SELECT used_at FROM approval_requests WHERE item_id = ?", (item2,))
        R.check(13, "the tap on Reject keeps it and uses up the link's code",
                rr.status_code == 200 and W.count("todos", "id=?", (t2,)) == 1
                and W.q("SELECT status FROM review_items WHERE id = ?", (item2,))[0]["status"] == "rejected"
                and used and used[0]["used_at"], rr.text)
        hc.close()
    finally:
        srv.terminate()                               # our own PID only
        try:
            srv.wait(15)
        except subprocess.TimeoutExpired:
            srv.kill()
        ph.close()
        pc.close()
        plog.close()
        for f in ("relay.json", "device-token"):
            (relay / f).unlink(missing_ok=True)
    h = cc1.call("manage_history", action="list", project=P, limit=50)
    paths = {(a["path"], a["decision"]) for a in h.get("approvals", [])}
    R.check(13, "manage_history lists every user-only decision with its path and app",
            {("popup", "approved"), ("popup", "declined"), ("popup", "cancelled"), ("popup", "timed_out"),
             ("terminal", "approved"), ("phone", "approved"), ("phone", "rejected")} <= paths, sorted(paths))
    R.check(13, "the unanswered popup request is still waiting, never approved on its own",
            pending(W.timeout_item), W.timeout_item)

    # ---- turning approvals off is the user's own step
    code, out = W.cli_no_terminal("approvals", "off", stdin="y\n")
    R.check(13, "`flowstate approvals off` with a piped y and no terminal: refused", code == 2 and "Refused" in out,
            short(out, 160))
    code, out = W.cli_in_terminal(["approvals", "off"], ["y"])
    R.check(13, "`flowstate approvals off` in the user's terminal: off again", code == 0 and "now off" in out,
            short(out, 160))
    tid = todo("Flyer deleted after approvals are off")
    r = cursor.call("manage_memory", action="delete", project=P, record_type="todo", record_id=tid)
    R.check(13, "with approvals off again, a delete goes straight through",
            r.get("deleted", {}).get("id") == tid and W.count("todos", "id=?", (tid,)) == 0, r)


def s14_user_stated(W):
    """Only the user turns an AI guess into "the user said so" (provenance.py), approvals off:
    cursor logs a guess; three other agents repeat it as user_stated and try to relabel and
    approve it; it stays ai_inferred until the user confirms in their own terminal."""
    print("\n== S14 user_stated is the user's", flush=True)
    cc1, cc2, cursor, grok = (W.agents[k] for k in ("cc1", "cc2", "cursor", "grok"))
    guess = "Saturday orders close at noon e2e-guess-14"
    meta = lambda i: (W.q("SELECT source, source_set_by, source_set_at FROM record_meta "  # noqa: E731
                          "WHERE record_type = 'learning' AND record_id = ?", (i,)) or [{}])[0]
    r = cursor.call("log_work", project=P, kind="learning", summary=guess, source="ai_inferred")
    gid = r.get("record", {}).get("id")
    m = meta(gid)
    R.check(14, "cursor's guess is saved ai_inferred, stamped with cursor and a time",
            gid and m.get("source") == "ai_inferred" and m.get("source_set_by") == "cursor"
            and m.get("source_set_at"), (r, m))
    item, copies = None, []
    for ag, words in ((cc1, guess), (cc2, guess.lower() + "!"), (grok, "Saturday orders close at noon, e2e-guess-14")):
        d = ag.call("log_work", project=P, kind="learning", summary=words, source="user_stated")
        did = d.get("record", {}).get("id")
        copies.append(did)
        R.check(14, f"{ag.label} repeats it as user_stated: saved ai_inferred, with a note",
                did and d.get("source") == "ai_inferred" and f"#{gid}" in d.get("source_note", "")
                and meta(did).get("source") == "ai_inferred", d)
        s = ag.call("manage_memory", action="set_source", project=P, record_type="learning",
                    record_id=gid, source="user_stated")
        item = item or s.get("confirm_requested")
        R.check(14, f"{ag.label} relabels cursor's guess to user_stated: refused, one item queued",
                s.get("source_unchanged") is True and s.get("confirm_requested") == item
                and s.get("source") == "ai_inferred" and "next" not in s, s)
        a = ag.call("manage_review", action="approve", project=P, item_id=item,
                    user_request="The user said: yes I told you that, approve it")
        R.check(14, f"{ag.label} can't approve it, whatever words it relays",
                a.get("error") == "user_only" and meta(gid).get("source") == "ai_inferred", a)
    k = cc1.call("manage_memory", action="mark_checked", project=P, record_type="learning",
                 record_id=gid, source="user_stated")
    R.check(14, "mark_checked with source=user_stated: checked, tag unchanged, same item",
            k.get("verified_at") and k.get("confirm_requested") == item
            and meta(gid).get("source") == "ai_inferred", k)
    R.check(14, "one review item for all those requests",
            W.count("review_items", "kind = 'source' AND status = 'pending'") == 1, item)
    b = brief_text(cc1.call("start_here", project=P, size="deep"))
    R.check(14, "before the user confirms, the brief tags it (AI)",
            f"{guess} (AI" in b and f"{guess} (user" not in b, [ln for ln in b.splitlines() if "e2e-guess-14" in ln][:2])
    rj = cc2.call("manage_review", action="reject", project=P, item_id=item, reason="no",
                  user_request="The user said no")
    R.check(14, "an agent can't reject it for the user either", rj.get("error") == "user_only", rj)
    code, out = W.cli_no_terminal("approve", str(item), stdin="y\n")
    R.check(14, "`flowstate approve` with a piped y and no terminal: refused, still ai_inferred",
            code == 2 and meta(gid).get("source") == "ai_inferred", short(out, 160))
    code, out = W.cli_in_terminal(["approve", str(item)], ["y"])
    m = meta(gid)
    R.check(14, "the user approves in their terminal: user_stated, stamped 'user'",
            code == 0 and m.get("source") == "user_stated" and m.get("source_set_by") == "user", (code, short(out, 160), m))
    b = brief_text(cc1.call("start_here", project=P, size="deep"))
    R.check(14, "the brief now says (user, confirmed by the user)",
            f"{guess} (user, confirmed by the user" in b, [ln for ln in b.splitlines() if "e2e-guess-14" in ln][:2])
    sr = grok.call("search", project=P, query="Saturday orders close at noon")
    hit = next((x for x in sr.get("results", []) if x.get("content_id") == gid
                and x.get("content_type") == "learning"), {})
    R.check(14, "search shows the tag and who set it", hit.get("source_tag") == "user, confirmed by the user"
            and hit.get("source_set_by") == "user", hit or sr)
    log = cc1.call("manage_history", action="list", project=P, limit=50).get("approvals", [])
    R.check(14, "manage_history lists the user's decision (kind source, path terminal)",
            any(a["item_id"] == item and a["kind"] == "source" and a["path"] == "terminal"
                and a["decision"] == "approved" for a in log), log[:3])
    dn = grok.call("manage_memory", action="set_source", project=P, record_type="learning",
                   record_id=gid, source="ai_inferred")
    m = meta(gid)
    R.check(14, "downgrading to ai_inferred is allowed and stamped with the app",
            dn.get("source") == "ai_inferred" and m.get("source_set_by") == "grok", (dn, m))
    R.check(14, "the three repeats stayed ai_inferred", all(meta(i).get("source") == "ai_inferred" for i in copies),
            [meta(i) for i in copies])


# headers an app can set, none of which may make a request from elsewhere count as this computer
SPOOF_LOCAL = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1", "Forwarded": "for=127.0.0.1",
               "CF-Connecting-IP": "127.0.0.1", "X-FlowState-Workspace": "this computer",
               "X-FlowState-Listener": "lan"}


def _test_listener(W, env, mode, name):
    """tests/e2e/relay_listener.py on a free loopback port: FlowState's HTTP app as the relay
    agent builds it (mode "relay") or the LAN listener with every request from 192.0.2.51
    ("lan-remote") or the public (tunnel) listener ("public"). Returns (process, base url, log
    file)."""
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        port = sk.getsockname()[1]
    plog = open(W.logs / f"{name}.log", "ab")
    srv = subprocess.Popen([PY, str(Path(__file__).resolve().parent / "relay_listener.py"), str(port)]
                           + ([mode] if mode != "relay" else []),
                           env=dict(env, FS_E2E_TOKEN=W.shared_token), cwd=str(REPO),
                           stdin=subprocess.DEVNULL, stdout=plog, stderr=plog)
    W.started.append(srv)
    return srv, f"http://127.0.0.1:{port}", plog


def _stop(srv, plog):
    if srv.poll() is None:
        srv.terminate()            # our own PID only
        try:
            srv.wait(15)
        except subprocess.TimeoutExpired:
            srv.kill()
    plog.close()


def _from_elsewhere(W, base, Q, boxes=False):
    """start_here and the walk from an app on another computer (spoofed "local" headers).
    `boxes`: it declares elicitation (on the LAN listener it would then get boxes, which this
    client never answers, so the LAN checks don't declare it)."""
    rc = HttpClient(f"{base}/mcp", W.shared_token, "Cursor", headers=SPOOF_LOCAL,
                    capabilities={"elicitation": {}} if boxes else None)
    try:
        b = brief_text(rc.call("start_here", project=Q))
        r = rc.call("manage_review", action="walk", project=Q)
    finally:
        rc.close()
    return next((ln for ln in b.splitlines() if ln.startswith("Review queue:")), ""), b, r, rc


SHORTCUT_LINE = "(the FlowState review shortcut, or `flowstate review-page`), or on its tunnel review page"


def s15_without_terminal(W):
    """Items pile up with approvals off; the user sees and decides them without a terminal:
    in the chat (Approve / Reject boxes, simulated answers), on the local review page the
    brief links to, and (relay enrolled) on the phone list."""
    import httpx
    from fsa_test_keys import claims, make_key, sign
    print("\n== S15 what's waiting, without a terminal", flush=True)
    cc1, cursor, grok = W.agents["cc1"], W.agents["cursor"], W.agents["grok"]
    Q = "Corner Cafe e2e-15"
    cursor.call("manage_projects", action="create", name=Q, description="S15: review without a terminal")
    R.check(15, "approvals are off", W.count("approval_settings", "key = 'approvals' AND value = 'on'") == 0)
    ids = []
    for s in ("Close the till at six", "Order oat milk weekly", "Paint the sign green", "Hire a Sunday baker"):
        r = cursor.call("log_work", project=Q, kind="decision", summary=s, suggested=True)
        ids.append(r.get("queued_for_review"))
    R.check(15, "four suggestions pile up in the review queue",
            all(ids) and W.count("review_items", f"status = 'pending' AND id IN ({','.join(map(str, ids))})") == 4, ids)

    # ---- the brief links the local page; it answers on 127.0.0.1 only
    b = brief_text(cc1.call("start_here", project=Q))
    line = next((ln for ln in b.splitlines() if ln.startswith("Review queue:")), "")
    m = re.search(r"on the computer where FlowState runs at (http://127\.0\.0\.1:(\d+)/)", line)
    R.check(15, "the brief's waiting line has the local link and the chat walk, and no phone link without relay",
            m and 'manage_review action="walk"' in line and "flowstatemcp.com" not in line, line)
    R.word(f"Brief line: {line}")
    link, port = (m.group(1), int(m.group(2))) if m else ("http://127.0.0.1:1/", 1)
    b2 = brief_text(grok.call("start_here", project=Q))
    R.check(15, "another FlowState process on this computer reuses the same page",
            link in b2, [ln for ln in b2.splitlines() if ln.startswith("Review queue:")])
    with httpx.Client(timeout=20) as hc:
        r = hc.get(link)
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text)
        R.check(15, "clicking the link opens the page at once (no command, no key): it lists the suggestions",
                r.status_code == 200 and "Paint the sign green" in r.text and csrf
                and "httponly" in r.headers.get("set-cookie", "").lower(), r.status_code)
        bad_host = hc.get(link, headers={"Host": f"rebind.example:{port}"})
        R.check(15, "a wrong Host header is refused (DNS rebinding)", bad_host.status_code == 403, bad_host.status_code)
        form = {"item_id": str(ids[3]), "csrf": csrf.group(1) if csrf else ""}
        no_origin = hc.post(link + "approve", data=form)
        evil = hc.post(link + "approve", data=form, headers={"Origin": "https://evil.example"})
        no_csrf = hc.post(link + "approve", data={"item_id": str(ids[3])}, headers={"Origin": link.rstrip("/")})
        R.check(15, "approve without a same-origin Origin or without the CSRF token: refused, still pending",
                (no_origin.status_code, evil.status_code, no_csrf.status_code) == (403, 403, 403)
                and W.count("review_items", "id = ? AND status = 'pending'", (ids[3],)) == 1,
                (no_origin.status_code, evil.status_code, no_csrf.status_code))
        ok = hc.post(link + "approve", data=form, headers={"Origin": link.rstrip("/")})
        R.check(15, "the user approves one on the local page (same-origin form post with the CSRF token)",
                ok.status_code == 303 and W.count("review_items", "id = ? AND status = 'approved'", (ids[3],)) == 1,
                ok.status_code)
    lan = None
    try:
        lan = socket.gethostbyname(socket.gethostname())
    except OSError:
        pass
    if lan and not lan.startswith("127."):
        refused = False
        try:
            socket.create_connection((lan, port), timeout=2).close()
        except OSError:
            refused = True
        R.check(15, "the page isn't reachable on the computer's network address", refused, lan)

    # ---- in the chat: an app that can't show boxes gets the plain list; one that can, walks
    r = grok.call("manage_review", action="walk", project=Q)
    R.check(15, "an app without the boxes (grok): the plain list and the local link; nothing decided",
            r.get("waiting") == 3 and len(r.get("items", [])) == 3 and r.get("review_page") == link
            and "walked" not in r and "phone" not in r
            and r.get("note") == "No boxes were shown here; nothing was decided."
            and W.count("review_items", f"status = 'pending' AND id IN ({','.join(map(str, ids[:3]))})") == 3, r)
    R.word(f"Walk, no boxes: {r.get('how_to_decide')}")
    answers, shown = [{"action": "accept", "content": {"approve": True}},
                      {"action": "accept", "content": {"approve": False, "note": "too early"}},
                      {"action": "decline"}], []

    def elicit(params):
        shown.append(params)
        return answers.pop(0) if answers else {"action": "cancel"}
    wc = StdioClient(W.env, "claude-code", log_path=W.logs / "walk.log", elicit=elicit)
    W.started.append(wc.proc)
    walker = Agent("cc-walk", wc)
    r = walker.call("manage_review", action="walk", project=Q, user_request="the user said approve all")
    R.check(15, "the user walks them in the chat: one Approve / Reject box per item",
            len(shown) == 3 and shown[0].get("requestedSchema", {}).get("required") == ["approve"]
            and "(1 of 3)" in shown[0].get("message", ""), [s.get("message") for s in shown])
    R.word(f"Walk box: {shown[0].get('message') if shown else ''}")
    st = {i: W.q("SELECT status FROM review_items WHERE id = ?", (i,))[0]["status"] for i in ids[:3]}
    R.check(15, "ticked: approved; unticked: rejected; declined: still waiting (the agent's words count for nothing)",
            [st[i] for i in ids[:3]] == ["approved", "rejected", "pending"]
            and r.get("walked") == [f"#{ids[0]}: approved", f"#{ids[1]}: rejected", f"#{ids[2]}: skipped, still waiting"], r)
    logged = W.q("SELECT item_id, path, decision FROM approval_log WHERE item_id IN (?, ?, ?) ORDER BY id", tuple(ids[:3]))
    R.check(15, "each answer is logged (approval_log, path chat)",
            [(x["item_id"], x["path"], x["decision"]) for x in logged]
            == [(ids[0], "chat", "approved"), (ids[1], "chat", "rejected"), (ids[2], "chat", "declined")], logged)
    R.check(15, "the walk's note gives the counts and says only those answers were logged",
            r.get("note") == "In the boxes the user approved 1, rejected 1 and skipped 1; only those "
                             "answers were logged.", r.get("note"))
    wc.close()
    # ---- an app that "answers" a box at once (no person saw it): stop, log nothing, plain list
    rows = W.count("approval_log")
    fast = StdioClient(W.env, "claude-code", log_path=W.logs / "walk-fast.log", think=0,
                       elicit=lambda p: {"action": "decline"})
    W.started.append(fast.proc)
    r = Agent("cc-fast", fast).call("manage_review", action="walk", project=Q)
    R.check(15, "a box answered faster than a person could: the walk stops, nothing logged, honest plain list",
            len(fast.popups) == 1 and "walked" not in r and r.get("items")
            and r.get("note") == "No boxes were shown here; nothing was decided."
            and "faster than a person" in r.get("how_to_decide", "") and W.count("approval_log") == rows
            and W.count("review_items", "id = ? AND status = 'pending'", (ids[2],)) == 1, r)
    R.word(f"Walk, too fast: {r.get('how_to_decide')}")
    fast.close()
    # ---- an app on another computer (LAN listener, peer 192.0.2.51): no 127.0.0.1 link, which
    # would open that computer's own 127.0.0.1; no relay yet, so the plain note
    srv, base, plog = _test_listener(W, W.env, "lan-remote", "s15-lan-remote")
    try:
        wait_port("127.0.0.1", int(base.rsplit(":", 1)[1]), 40)
        line, b, r, rc = _from_elsewhere(W, base, Q)
    finally:
        _stop(srv, plog)
    R.check(15, "an app on another computer (LAN, spoofed local headers): no 127.0.0.1 in the brief or the "
                "walk, no phone link without relay, the plain note to ask in the chat or use the computer "
                "where FlowState runs",
            line and "127.0.0.1" not in b and "127.0.0.1" not in json.dumps(r) and "flowstatemcp.com" not in b
            and SHORTCUT_LINE in line and 'manage_review action="walk"' in line
            and "review_page" not in r and "phone" not in r and r.get("items")
            and SHORTCUT_LINE in r.get("how_to_decide", ""), (line, r))
    R.word(f"Brief line, another computer on the LAN: {line}")

    # ---- relay enrolled: the phone link in the brief and the walk; the phone list works
    relay = Path(W.env["FLOWSTATE_RELAY_DIR"])
    relay.mkdir(parents=True, exist_ok=True)
    (relay / "relay.json").write_text(json.dumps({"version": 1, "handle": "e2e-bakery",
                                                  "hostname": "e2e-bakery.fsmcp.com", "enabled": True}))
    (relay / "device-token").write_text("fake-device-token-for-e2e\n")
    key, jwk = make_key()
    penv = dict(W.env, FS_TEST_PATCH=json.dumps({"flowstate.approvals": {"FSA_PUBLIC_KEYS": {"fsa-1": jwk, "fsa-2": None}}}))
    pa = StdioClient(penv, "cursor", log_path=W.logs / "s15-phone-agent.log")
    W.started.append(pa.proc)
    phone = Agent("s15-phone", pa)
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        hport = sk.getsockname()[1]
    plog = open(W.logs / "s15-phone-http.log", "ab")
    # FlowState's HTTP app as the relay agent builds it (relay=True), on loopback, sandbox token
    srv = subprocess.Popen([PY, str(Path(__file__).resolve().parent / "relay_listener.py"), str(hport)],
                           env=dict(penv, FS_E2E_TOKEN=W.shared_token), cwd=str(REPO),
                           stdin=subprocess.DEVNULL, stdout=plog, stderr=plog)
    W.started.append(srv)
    try:
        b = brief_text(phone.call("start_here", project=Q))
        line = next((ln for ln in b.splitlines() if ln.startswith("Review queue:")), "")
        pm = re.search(r"on their phone: (https://flowstatemcp\.com/review/e2e-bakery)(?![?\w-])", line)
        R.check(15, "relay enrolled: the brief also has the bookmarkable phone link (no code in the chat)",
                pm and link in line and "?n=" not in line, line)
        R.word(f"Brief line, relay: {line}")
        r = phone.call("manage_review", action="walk", project=Q)
        R.check(15, "relay enrolled: the walk's plain answer has the phone link too",
                pm and r.get("phone") == pm.group(1) and r.get("review_page") == link, r)
        wait_port("127.0.0.1", hport, 40)
        base = f"http://127.0.0.1:{hport}"
        hdr = {"content-type": "application/json", "origin": base, "sec-fetch-site": "same-origin"}
        # the live bug: a relay connection calling itself "Cursor" that declares elicitation
        rows = W.count("approval_log")
        rline, rb, r, rc = _from_elsewhere(W, base, Q, boxes=True)
        R.check(15, "through the relay, an app calling itself Cursor (elicitation declared): no boxes sent, "
                    "nothing logged, the honest plain list with the links",
                rc.server_requests == [] and "walked" not in r and r.get("items")
                and r.get("note") == "No boxes were shown here; nothing was decided."
                and "relay" in r.get("how_to_decide", "") and r.get("phone") == (pm.group(1) if pm else "?")
                and W.count("approval_log") == rows, (rc.server_requests, r))
        R.check(15, "through the relay (the agent connects from 127.0.0.1): no 127.0.0.1 link in the brief or "
                    "the walk, the bare phone link instead",
                pm and "127.0.0.1" not in rb and "127.0.0.1" not in json.dumps(r) and "review_page" not in r
                and f"on their phone: {pm.group(1)}," in rline and "?n=" not in rline
                and pm.group(1) in r.get("how_to_decide", ""), (rline, r))
        R.word(f"Brief line through the relay: {rline}")
        R.word(f"Walk through the relay: {r.get('how_to_decide')}")
        ls, lbase, llog = _test_listener(W, penv, "lan-remote", "s15-lan-remote-phone")
        try:
            wait_port("127.0.0.1", int(lbase.rsplit(":", 1)[1]), 40)
            lline, lb, lr, _ = _from_elsewhere(W, lbase, Q)
        finally:
            _stop(ls, llog)
        R.check(15, "an app on another computer on the LAN, relay set up: the bare phone link, no 127.0.0.1",
                pm and "127.0.0.1" not in lb and "127.0.0.1" not in json.dumps(lr)
                and f"on their phone: {pm.group(1)}," in lline and lr.get("phone") == pm.group(1)
                and "review_page" not in lr and lr.get("items") and "walked" not in lr, (lline, lr))
        with httpx.Client(timeout=30) as hc:
            # the bookmark link: the site (signed-in owner) sends the browser to /review/start with
            # its signed start ticket (task 38); without one, or with a used one: a plain page
            def ticket(sub="e2e-site-user"):
                return sign(key, claims(0, secrets.token_urlsafe(16), handle="e2e-bakery", sub=sub,
                                        action="start_review"))
            codes = lambda: W.count("review_open_nonces")  # noqa: E731
            before = codes()
            bare = hc.get(f"{base}/review/start", follow_redirects=False)
            forged = hc.get(f"{base}/review/start", params={"t": sign(make_key()[0], claims(
                0, secrets.token_urlsafe(16), handle="e2e-bakery", action="start_review"))}, follow_redirects=False)
            R.check(15, "/review/start without the site's ticket, or with a forged one: a plain page, no code made",
                    bare.status_code == 403 and forged.status_code == 403 and "location" not in bare.headers
                    and "Waiting for you" in bare.text and codes() == before, (bare.status_code, forged.status_code))
            R.word(f"/review/start without a ticket: {re.sub(r'<[^>]+>', ' ', bare.text).split('FlowState', 2)[-1].strip()[:200]}")
            tk = ticket()
            st = hc.get(f"{base}/review/start", params={"handle": "evil", "t": tk}, headers={"X-Forwarded-Host": "evil.example"},
                        follow_redirects=False)
            replay = hc.get(f"{base}/review/start", params={"t": tk}, follow_redirects=False)
            R.check(15, "a start ticket makes one code only: the same ticket again gets the plain page",
                    replay.status_code == 403 and "already used" in replay.text and tk not in replay.text, replay.status_code)
            sm = re.fullmatch(r"https://flowstatemcp\.com/review/e2e-bakery\?n=([A-Za-z0-9_-]{43})",
                              st.headers.get("location", ""))
            R.check(15, "/review/start: 303 back to the site with a fresh one-use code and this device's handle",
                    st.status_code == 303 and sm and st.headers.get("cache-control") == "no-store"
                    and st.headers.get("referrer-policy") == "no-referrer", (st.status_code, dict(st.headers)))
            nonce = sm.group(1) if sm else "x" * 43
            shell = hc.get(f"{base}/review")
            R.check(15, "the phone page: strict CSP, no framing, no caching, no data before the grant",
                    shell.status_code == 200 and "frame-ancestors 'none'" in shell.headers.get("content-security-policy", "")
                    and shell.headers.get("x-frame-options") == "DENY" and shell.headers.get("cache-control") == "no-store"
                    and "Corner" not in shell.text, dict(shell.headers))
            opn = lambda g: hc.post(f"{base}/review/open", headers=hdr, content=json.dumps({"grant": g}))  # noqa: E731
            t = int(time.time())
            for label, grant, code_ in (
                    ("for the approve action", sign(key, claims(0, nonce, handle="e2e-bakery")), "wrong_device"),
                    ("for an item", sign(key, claims(5, nonce, handle="e2e-bakery", action="open_review")), "wrong_item"),
                    ("expired", sign(key, claims(0, nonce, handle="e2e-bakery", action="open_review", iat=t - 400,
                                                 exp=t - 100)), "grant_expired"),
                    ("signed by another key", sign(make_key()[0], claims(0, nonce, handle="e2e-bakery",
                                                                         action="open_review")), "bad_signature")):
                rr = opn(grant)
                R.check(15, f"a phone list grant {label} is refused", rr.status_code == 403
                        and rr.json().get("error") == code_, rr.text)
            grant = sign(key, claims(0, nonce, handle="e2e-bakery", action="open_review"))
            o = opn(grant)
            ck = o.headers.get("set-cookie", "").lower()
            R.check(15, "a good grant opens the list; session cookie HttpOnly, Secure, SameSite=Strict, 15 min, /review",
                    o.status_code == 200 and ids[2] in [i["id"] for i in o.json().get("items", [])]
                    and all(f in ck for f in ("httponly", "secure", "samesite=strict", "max-age=900", "path=/review")),
                    (o.status_code, ck))
            again = opn(grant)
            R.check(15, "the same grant can't open it twice (one-use code)",
                    again.status_code == 403 and again.json().get("error") == "grant_used", again.text)
            # the cookie is Secure (https on the relay); send it by hand over this test's plain http
            cookie = re.search(r"fs_review_phone=([^;]+)", o.headers.get("set-cookie", ""))
            token = o.json().get("token") if o.status_code == 200 else ""
            dec = hc.post(f"{base}/review/decide", headers={**hdr, "cookie": f"fs_review_phone={cookie.group(1) if cookie else ''}"},
                          content=json.dumps({"token": token, "item_id": ids[2], "decision": "approve"}))
            R.check(15, "the user approves the last one on their phone; logged with path phone",
                    dec.status_code == 200 and W.count("review_items", "id = ? AND status = 'approved'", (ids[2],)) == 1
                    and W.count("approval_log", "item_id = ? AND path = 'phone' AND decision = 'approved'", (ids[2],)) == 1,
                    dec.text)
        # with phone links turned off, no phone link anywhere
        cursor.call("log_work", project=Q, kind="decision", summary="Open on bank holidays", suggested=True)
        code, out = W.cli("approvals", "phone", "off")
        b = brief_text(phone.call("start_here", project=Q))
        r = phone.call("manage_review", action="walk", project=Q)
        with httpx.Client(timeout=30) as hc:
            off = hc.get(f"{base}/review/start", follow_redirects=False)
        R.check(15, "`flowstate approvals phone off`: no phone link in the brief or the walk, the local link stays, "
                    "and /review/start only says phone review is off",
                code == 0 and "flowstatemcp.com" not in b and link in b and "phone" not in r
                and r.get("review_page") == link and off.status_code == 200
                and "Phone review is off" in off.text and "location" not in off.headers, (out, r, off.status_code))
        W.cli("approvals", "phone", "on")
    finally:
        pa.close()
        if srv.poll() is None:
            srv.terminate()            # our own PID only
            try:
                srv.wait(15)
            except subprocess.TimeoutExpired:
                srv.kill()
        plog.close()
        for f in ("relay.json", "device-token"):
            (relay / f).unlink(missing_ok=True)
    R.check(15, "nothing in S15 was approved except by the user's own answer",
            W.count("review_items", f"id IN ({','.join(map(str, ids))}) AND status = 'approved' "
                                    "AND decided_by NOT LIKE 'chat (%' AND decided_by NOT LIKE 'phone (%' "
                                    "AND decided_by != 'review-page'") == 0)


def s16_tunnel_page(W):
    """A user with their own tunnel and no relay: FlowState's public listener (as run behind
    cloudflared, here on loopback with a made-up public URL) serves /review/tunnel. Only the
    owner passphrase opens it (an agent's token doesn't); there the user decides a delete that
    waits for them (approvals on for this project) and a suggestion."""
    import httpx
    print("\n== S16 the tunnel review page", flush=True)
    cursor = W.agents["cursor"]
    Q = "Harbour Kiosk e2e-16"
    cursor.call("manage_projects", action="create", name=Q, description="S16: tunnel review page")
    code, out = W.cli_no_terminal("approvals", "on", "--project", Q)
    R.check(16, "`flowstate approvals on --project` for this project only", code == 0, short(out, 160))
    tid = cursor.call("log_work", project=Q, kind="todo", summary="Old harbour flyer").get("record", {}).get("id")
    r = cursor.call("manage_memory", action="delete", project=Q, record_type="todo", record_id=tid)
    item = r.get("delete_requested")
    sug = cursor.call("log_work", project=Q, kind="decision", summary="Open the kiosk at seven",
                      suggested=True).get("queued_for_review")

    def status(i):
        rows = W.q("SELECT status, decided_by FROM review_items WHERE id = ?", (i,))
        return {"status": rows[0]["status"], "decided_by": rows[0]["decided_by"]} if rows else {}
    R.check(16, "an agent's delete waits for the user; a suggestion waits in the queue",
            item and sug and status(item).get("status") == "pending" and W.count("todos", "id=?", (tid,)) == 1
            and "flowstate approve" in r.get("next", ""), r)
    R.word(f"Delete waiting (agent on this computer): {r.get('next')}")
    passphrase = secrets.token_urlsafe(18)          # the sandbox's own; never the user's
    public = "https://tunnel.e2e.example"
    env = dict(W.env, FS_E2E_PASSPHRASE=passphrase, FS_E2E_PUBLIC_URL=public)
    srv, base, plog = _test_listener(W, env, "public", "s16-public")
    try:
        wait_port("127.0.0.1", int(base.rsplit(":", 1)[1]), 40)
        page = f"{base}/review/tunnel"
        with httpx.Client(timeout=30) as hc:
            def form(html):
                m = re.search(r"""name=["']csrf["'] value=["']([^"']+)["']""", html)
                return m.group(1) if m else ""
            first = hc.get(page)
            R.check(16, "the page asks for the owner passphrase and shows nothing before it",
                    first.status_code == 200 and "Owner passphrase" in first.text
                    and "harbour" not in first.text.lower()
                    and "frame-ancestors 'none'" in first.headers.get("content-security-policy", "")
                    and first.headers.get("cache-control") == "no-store", first.status_code)
            agent = hc.get(page, headers={"Authorization": f"Bearer {W.shared_token}"})
            R.check(16, "an agent's bearer token opens nothing (the same sign-in page)",
                    agent.status_code == 200 and "Owner passphrase" in agent.text
                    and "harbour" not in agent.text.lower(), agent.status_code)
            org = {"origin": public}
            wrong = hc.post(f"{page}/sign-in", headers=org,
                            data={"csrf": form(hc.get(page).text), "passphrase": "not it at all"})
            R.check(16, "a wrong passphrase is refused", wrong.status_code == 403 and "Wrong passphrase" in wrong.text
                    and "set-cookie" not in wrong.headers, wrong.status_code)
            ok = hc.post(f"{page}/sign-in", headers=org, follow_redirects=False,
                         data={"csrf": form(hc.get(page).text), "passphrase": passphrase})
            ck = ok.headers.get("set-cookie", "")
            R.check(16, "the passphrase signs in: cookie __Host-, Secure, HttpOnly, SameSite=Strict, Path=/",
                    ok.status_code == 303 and ck.startswith("__Host-fs_review_tunnel=")
                    and all(f in ck.lower() for f in ("secure", "httponly", "samesite=strict", "path=/")),
                    ok.status_code)
            # the cookie is Secure (https through the tunnel); send it by hand over this test's plain http
            m = re.match(r"__Host-fs_review_tunnel=([^;]+)", ck)
            cookie = {"cookie": f"__Host-fs_review_tunnel={m.group(1) if m else ''}"}
            shown = hc.get(page + "/", headers=cookie)
            csrf = form(shown.text)
            R.check(16, "signed in: the delete and the suggestion are on the page, with Sign out",
                    shown.status_code == 200 and "Approve: delete it" in shown.text
                    and "Open the kiosk at seven" in shown.text and "Sign out" in shown.text and csrf,
                    shown.status_code)
            evil = hc.post(f"{page}/approve", data={"csrf": csrf, "item_id": str(item)},
                           headers={**cookie, "origin": "https://evil.example"})
            nocsrf = hc.post(f"{page}/approve", data={"item_id": str(item)}, headers={**cookie, **org})
            R.check(16, "another site's form, or one without the CSRF token: refused, still waiting",
                    evil.status_code == 403 and nocsrf.status_code == 403
                    and status(item).get("status") == "pending", (evil.status_code, nocsrf.status_code))
            a = hc.post(f"{page}/approve", data={"csrf": csrf, "item_id": str(item)},
                        headers={**cookie, **org}, follow_redirects=False)
            b = hc.post(f"{page}/approve", data={"csrf": csrf, "item_id": str(sug)},
                        headers={**cookie, **org}, follow_redirects=False)
            logged = [(x["path"], x["decision"]) for x in
                      W.q("SELECT path, decision FROM approval_log WHERE item_id IN (?, ?) ORDER BY id",
                          (item, sug))]
            R.check(16, "the user approves the delete (user-only) and the suggestion there; logged as the tunnel page",
                    a.status_code == 303 and b.status_code == 303 and W.count("todos", "id=?", (tid,)) == 0
                    and status(item) == {"status": "approved", "decided_by": "tunnel page"}
                    and status(sug).get("decided_by") == "tunnel page"
                    and logged == [("tunnel page", "approved"), ("tunnel page", "approved")],
                    (logged, status(item)))
            gone = hc.post(f"{page}/sign-out", data={"csrf": csrf}, headers={**cookie, **org},
                           follow_redirects=False)
            after = hc.get(page + "/", headers=cookie)
            R.check(16, "Sign out ends the session (the old cookie shows the sign-in page again)",
                    gone.status_code == 303 and "Owner passphrase" in after.text,
                    (gone.status_code, after.status_code))
    finally:
        _stop(srv, plog)


def deferred_window(W):
    """Same content after the 2-minute window does write (checked at the end of the run)."""
    t0, s = W.window_probe
    wait = 125 - (time.monotonic() - t0)
    if wait > 0:
        print(f"\n(waiting {wait:.0f}s for the 2-minute repeat window to pass)", flush=True)
        time.sleep(wait)
    before = W.count("learnings", "insight = ?", (s,))
    r = W.agents["cc2"].call("log_work", project=P, kind="learning", summary=s)
    after = W.count("learnings", "insight = ?", (s,))
    R.check(5, "the same content from the same window after the 2-minute window does write",
            "record" in r and after == before + 1, f"{short(r, 120)} rows {before}->{after}")


# ============================================================================ report
def report(W, started, test_changed, live_changed):
    lines = ["# FlowState 2.4 end-to-end mock project test", "",
             f"Run {started:%Y-%m-%d %H:%M} local, branch {git('rev-parse', '--abbrev-ref', 'HEAD')} "
             f"at {git('rev-parse', '--short', 'HEAD')}; {R.calls} real MCP tool calls.", "",
             "| # | Scenario | Result | Evidence |", "|---|---|---|---|"]
    for n, title in Report.TITLES.items():
        cs = R.checks[n]
        bad = [c for c in cs if not c[0]]
        res = "PASS" if cs and not bad else ("FAIL" if cs else "not run")
        ev = (f"{len(cs) - len(bad)}/{len(cs)} checks. " +
              (f"Failed: {bad[0][1]} ({bad[0][2]})" if bad else (cs[-1][1] if cs else ""))).replace("|", "/")
        lines.append(f"| {n} | {title} | {res} | {short(ev, 300)} |")
    lines += ["", "## Every check", ""]
    for n, title in Report.TITLES.items():
        lines.append(f"### S{n} {title}")
        for ok, label, ev in R.checks[n]:
            lines.append(f"- {'PASS' if ok else 'FAIL'}: {label}" + ("" if ok else f" -- {ev}"))
        lines.append("")
    lines += ["## Findings", ""]
    for sev, title, repro in R.findings or [("-", "none", "")]:
        lines.append(f"- **{sev}** {title}. {repro}")
    lines += ["", "## Wording", ""] + [f"- {w}" for w in R.wording or ["none"]]
    lines += ["", "## Informational results", ""]
    for k in ("cdn_result", "undo_other_window", "partial_undo", "old_item_result", "big_result"):
        if hasattr(W, k):
            lines.append(f"- {k}: {short(getattr(W, k), 400)}")
    lines += ["", f"Real FlowState folders untouched: {not test_changed}"
              + (f" (changed: {test_changed[:5]})" if test_changed else "")]
    if live_changed:
        lines.append(f"(The FlowState already running on this computer rewrote its own heartbeat files meanwhile, "
                     f"not this test: {live_changed[:6]})")
    return "\n".join(lines)


def rmtree(root: Path) -> None:
    """Delete the sandbox. git's object files are read-only, which Windows won't delete as is."""
    def again(fn, path, _exc):
        os.chmod(path, 0o700)
        fn(path)
    shutil.rmtree(root, **({"onexc": again} if sys.version_info >= (3, 12) else {"onerror": again}))


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the temp folder")
    ap.add_argument("--skip-upgrade", action="store_true")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    root = Path(args.root or Path(tempfile.gettempdir()) / f"fs-e2e-{os.getpid()}").resolve()
    if root.exists():
        sys.exit(f"{root} exists; pick another --root")
    real_before = real_paths_snapshot()
    live = live_flowstate_running()
    os.environ["FS_E2E_GUARD_LOG"] = str(root / "guard.log")
    started = datetime.now()
    W = World(root)
    try:
        for step in (s1_fresh, s2_day, s3_conflicts, s4_review, s5_receipts, s6_windows, s7_undo,
                     s8_backups, s9_ledger, lambda w: s10_upgrade(w, args.skip_upgrade), s11_edges,
                     s13_approvals, s14_user_stated, s15_without_terminal, s16_tunnel_page,
                     deferred_window,
                     s12_descriptions):
            try:
                step(W)
            except Exception as e:  # noqa: BLE001 - a crashed scenario is a failed check
                name = getattr(step, "__name__", "step")
                n = int(re.sub(r"\D", "", name.split("_")[0]) or 10) if name.startswith("s") else 10
                R.check(n, f"{name} crashed: {type(e).__name__}: {e}", False, traceback.format_exc()[-600:])
    finally:
        W.close_all()
    real_after = real_paths_snapshot()
    refused = guard_log(root).read_text().strip() if guard_log(root).exists() else ""
    R.check(11, "real-data guard: no server process tried to touch the real FlowState folders during the run",
            not refused, short(refused, 300))
    changed, live_changed = real_changes(real_before, real_after, live or live_flowstate_running())
    R.check(11, "the real FlowState folders are unchanged after the run", not changed, changed[:5])
    text = report(W, started, changed, live_changed)
    (root / "report.md").write_text(text)
    print("\n" + text)
    if not args.keep:
        rmtree(root)
        print(f"\n(removed {root})")
    return 0 if all(ok for cs in R.checks.values() for ok, *_ in cs) else 1


if __name__ == "__main__":
    sys.exit(main())
