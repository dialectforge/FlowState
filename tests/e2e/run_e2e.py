#!/usr/bin/env python3
"""FlowState end-to-end mock project test ("Lakeside Bakery ordering app").

Starts the real FlowState server from this checkout (one stdio process per app window, plus
the streamable-HTTP transport) on a brand-new throwaway folder under /tmp, and drives it only
with real MCP tool calls, the way Claude Code, Cursor and Grok do. Four agents work at once:
claude-code (two windows), cursor (stdio) and grok (HTTP, its own app key). Twelve scenarios,
each pass/fail with evidence; every tool and every action it offers is called at least once.

    .venv/bin/python tests/e2e/run_e2e.py              # everything (about 4 minutes)
    .venv/bin/python tests/e2e/run_e2e.py --keep       # keep the /tmp folder to look at
    .venv/bin/python tests/e2e/run_e2e.py --skip-upgrade   # no v2.3.0 worktree

Nothing here touches ~/.flowstate, the per-OS FlowState folders, a real database or a real
token: every FlowState path (and HOME) points into the /tmp folder, which is deleted at the
end unless --keep, and every server process loads tests/guard_site (it refuses, and
records, any touch of the real folders; scenario 11 checks that). `flowstate approve` is
only ever run in a new session without a terminal, or in a pseudo-terminal of its own, so
it never prompts on yours. Only servers this script started are stopped, by their own PIDs.
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
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fsclient import (PY, REPO, HttpClient, StdioClient, ToolError, guard_log, real_dirs,  # noqa: E402
                      sandbox_env, wait_port)

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
    "manage_review": {"list", "get_mode"}, "manage_handoffs": {"list"},
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


# ----------------------------------------------------------------------------- bookkeeping
class Report:
    TITLES = {
        1: "Fresh install", 2: "Normal day, four agents", 3: "Possible conflicting decisions",
        4: "Review mode, self-approval, the user-only step", 5: "Receipts, repeats, timed-out writes",
        6: "Two windows of one app; 'since your last visit'", 7: "Undo",
        8: "Backups, safety copies, health check", 9: "Run ledger keeps no text",
        10: "Upgrade from a v2.3.0 database", 11: "Bad input, edge cases, real-data guard",
        12: "Tool descriptions: size and truthfulness"}

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

    def cli_no_terminal(self, *args, stdin=None):
        """`flowstate ...` the way an agent's shell runs it: a new session with no controlling
        terminal (so it can never prompt on the terminal this script runs in), stdin piped."""
        r = subprocess.run([PY, "-m", "flowstate", *args], env=self.env, cwd=str(REPO), input=stdin,
                           capture_output=True, text=True, timeout=120, start_new_session=True)
        return r.returncode, r.stdout + r.stderr

    def cli_in_terminal(self, args, answers, env=None):
        """`flowstate ...` with a pseudo-terminal as its controlling terminal (as when the user
        types it), answering each "Type ..." prompt in turn. (exit code, what it showed)"""
        import pty
        import select
        import warnings
        with warnings.catch_warnings():   # forkpty in a threaded process: the child execs at once
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, master = pty.fork()
        if pid == 0:
            os.execve(PY, [PY, "-m", "flowstate", *args], env or self.env)
        seen, todo, deadline = b"", list(answers), time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.2)[0]:
                    try:
                        data = os.read(master, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    seen += data
                    if todo and seen.count(b"Type ") > len(answers) - len(todo):
                        os.write(master, todo.pop(0).encode() + b"\n")
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(master)
        return os.waitstatus_to_exitcode(status), seen.decode(errors="replace")

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
    """mtimes of the real FlowState places, to prove the test didn't touch them."""
    home = Path.home()
    cands = [home / ".flowstate", home / "Library" / "Application Support" / "flowstate",
             home / ".local" / "share" / "flowstate", home / ".config" / "flowstate"]
    snap = {}
    for c in cands:
        if c.exists():
            for p in [c, *c.rglob("*")][:2000]:
                try:
                    snap[str(p)] = p.stat().st_mtime_ns
                except OSError:
                    pass
    return snap


# ============================================================================ scenarios
def s1_fresh(W):
    print("\n== S1 fresh install", flush=True)
    cc1 = W.agents["cc1"] = W.stdio("cc1", "claude-code")
    r = cc1.call("start_here")
    R.check(1, "start_here on an empty database answers (no error) and lists no project",
            isinstance(r, str) and "no project given" in r and "Lakeside" not in r, r)
    R.check(1, "an empty database says there are no projects yet (no bare 'Projects:')",
            isinstance(r, str) and "no projects yet" in r and not r.rstrip().endswith("Projects:"), r)
    R.check(1, "the database was created inside the /tmp sandbox", W.db.exists(), str(W.db))
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
    # data git on the sandbox data folder; the 'remote' is a bare repo in /tmp (no network)
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
        st = [i["status"] for i in r.get("items", []) if i["id"] == W.conf_item]
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
                         (cc2, "change", "Moved menu images to the CDN")):
        r = who.call("log_work", project=P, kind=kind, summary=s)
        items[s] = r.get("queued_for_review")
        R.check(4, f"{who.label}'s {kind} is queued, not saved, and still has a receipt",
                bool(r.get("queued_for_review")) and "receipt" in r and "record" not in r, r)
    b = cc1.call("start_here", project=P)
    R.check(4, "brief shows review mode 'everything' and the waiting count",
            "everything" in b and "Review queue:" in b, [l for l in b.splitlines() if "Review" in l])
    gift, postmark, square, cdn = items.values()
    # self-approval attempts
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift)
    R.check(4, "own item, no user_request: refused", r.get("error") == "user_request_required", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift, user_request="Add gift card support")
    R.check(4, "own item, user_request = the item's own text: refused (own_item)", r.get("error") == "own_item", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=gift, user_request="approve")
    R.check(4, "own item, one-word 'approve': refused", r.get("error") == "own_item", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=postmark,
                    user_request="The user said this looks good, approve it")
    fake_ok = r.get("error") == "own_item"
    R.check(4, "own item, invented 'user words' that aren't the user's: refused", fake_ok, r)
    if not fake_ok:
        R.finding("medium", "An agent can approve its own review item by inventing 'user words'",
                  "Review mode everything; cursor logs a decision (queued). Cursor calls manage_review "
                  "approve item_id=<it> user_request=\"The user said this looks good, approve it\" -> approved, "
                  "the record is saved as user-approved. Any 2+ words not copied from the item pass "
                  "(review.own_item_refusal); the other Claude Code window approved its own queued change with just "
                  "'ok fine'. Documented as a limit in task 23, but a user reading "
                  "'approved' would wrongly trust the user saw it.")
    r = cc2.call("manage_review", action="approve", project=P, item_id=cdn, user_request="ok fine")
    R.check(4, "own item, 'ok fine' (two generic words): refused", r.get("error") == "own_item", r)
    r = cursor.call("manage_review", action="approve", project=P, item_id=postmark,
                    user_request="User: yes, approve the Postmark decision, I checked it")
    msg = r.get("message", "")
    R.check(4, "own item, the user's real-looking words: still refused, one line naming the user's step",
            r.get("error") == "own_item" and f"flowstate approve {postmark}" in msg and "\n" not in msg
            and len(msg) < 200, r)
    # the user-only step: flowstate review / approve / reject in the user's own terminal
    code, out = W.cli_no_terminal("review", "--project", P)
    R.check(4, "`flowstate review` lists what waits (read-only, no terminal needed)",
            code == 0 and f"#{postmark}" in out and "Postmark" in out, short(out, 200))
    code, out = W.cli_no_terminal("approve", str(postmark), stdin="y\n")
    still = W.q("SELECT status FROM review_items WHERE id = ?", (postmark,))[0]["status"]
    R.check(4, "`flowstate approve` with a piped 'y' and no terminal: refused, nothing changes",
            code == 2 and "Refused" in out and still == "pending", f"exit {code}: {short(out, 160)}; {still}")
    code, out = W.cli_no_terminal("reject", str(cdn), stdin="yes\n")
    R.check(4, "`flowstate reject` with a piped 'yes' and no terminal: refused too",
            code == 2 and W.q("SELECT status FROM review_items WHERE id = ?", (cdn,))[0]["status"] == "pending",
            f"exit {code}: {short(out, 160)}")
    code, out = W.cli_in_terminal(["approve", str(postmark)], ["y"])
    row = W.q("SELECT status, decided_by, result_id FROM review_items WHERE id = ?", (postmark,))[0]
    R.check(4, "`flowstate approve` in a terminal, the user types y: approved and the decision saved",
            code == 0 and row["status"] == "approved" and row["decided_by"] == "command-line"
            and row["result_id"] and "Postmark" in out, f"exit {code}: {short(out, 200)}; {row}")
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
    r = cc1.call("manage_review", action="reject", project=P, item_id=square,
                 user_request="No, that Square note is wrong, reject it", reason="Not reproducible")
    R.check(4, "reject with the user's words and reason", not err(r) and "receipt" in r, r)
    r = cursor.call("manage_review", action="list", project=P, status="all", limit=100)
    R.check(4, "manage_review list status=all shows approved and rejected items",
            '"approved"' in json.dumps(r) and '"rejected"' in json.dumps(r), short(r, 200))
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
    # delete requests -> approval -> pre-bulk copy
    r = cursor.call("manage_memory", action="delete", project=P, record_type="learning", record_id=W.learning)
    item = r.get("delete_requested") or r.get("queued_for_review") or r.get("item_id")
    R.check(8, "manage_memory delete only queues a request (with a receipt)", bool(item) and "receipt" in r
            and W.count("learnings", "id=?", (W.learning,)) == 1, r)
    r = cc1.call("manage_review", action="approve", project=P, item_id=item,
                 user_request="yes delete that timestamptz learning, it's outdated")
    copies = sorted(p.name for p in W.backups.glob("*pre-bulk*"))
    R.check(8, "approving the delete took a pre-bulk safety copy first",
            not err(r) and copies and W.count("learnings", "id=?", (W.learning,)) == 0, f"{short(r, 150)} copies={copies}")
    if copies:
        cp = W.backups / copies[-1]
        R.check(8, "the safety copy still holds the deleted record",
                W.count("learnings", "id=?", (W.learning,), db=cp) == 1, cp.name)
    r = cc1.call("manage_history", action="undo", project=P, record_type="learning", record_id=W.learning)
    R.check(8, "undo of the approved delete restores it", W.count("learnings", "id=?", (W.learning,)) == 1, r)
    r = grok.call("manage_variables", action="delete", project=P, variable_id=W.var)
    R.check(8, "an HTTP app key can't ask for a delete (as AGENTS.md says)", r.get("error") == "not_allowed_remotely", r)
    for kind, rid in (("variable", W.var), ("method", W.method)):
        r = cursor.call(f"manage_{kind}s", action="delete", project=P, **{f"{kind}_id": rid})
        R.check(8, f"manage_{kind}s delete queues a request", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_files", action="remove", project=P, attachment_id=W.attach)
    R.check(8, "manage_files remove queues a request", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_projects", action="add_component", project=P, name="legacy-kiosk", description="Old in-store kiosk")
    kid = r.get("id")
    r = cc1.call("manage_projects", action="update_component", project=P, component_id=kid, status="deprecated")
    R.check(8, "manage_projects update_component (deprecate)", not err(r) and "receipt" in r, r)
    r = cc1.call("manage_projects", action="delete_component", project=P, component_id=kid)
    R.check(8, "manage_projects delete_component queues a request", not err(r) and "receipt" in r, r)
    # project delete needs the exact name
    tmp = cc1.call("manage_projects", action="create", name="Scratch Pad", description="throwaway")
    cc1.call("manage_projects", action="update", project="Scratch Pad", status="paused", description="still throwaway")
    r = cc1.call("manage_projects", action="delete", project="Scratch Pad")
    pitem = r.get("delete_requested") or r.get("queued_for_review") or r.get("item_id")
    R.check(8, "manage_projects delete queues a request with child counts", bool(pitem), r)
    r = cursor.call("manage_review", action="approve", project="Scratch Pad", item_id=pitem,
                    user_request="delete the Scratch Pad project please")
    R.check(8, "project delete without confirm_name is refused", err(r), r)
    r = cursor.call("manage_review", action="approve", project="Scratch Pad", item_id=pitem, confirm_name="Scratch Pad",
                    user_request="delete the Scratch Pad project please")
    R.check(8, "project delete with confirm_name goes through", not err(r), r)
    # clean-up suggestions -> approve one (safety copy reused within a minute)
    for i in range(2):
        cursor.call("log_work", project=P, kind="learning", summary="Sourdough starter needs feeding twice a day")
        cc2.call("log_work", project=P, kind="learning", summary="Sourdough starter needs feeding twice a day!")
    r = cc1.call("manage_memory", action="cleanup", project=P)
    R.check(8, "manage_memory cleanup queues suggestions (and changes nothing)", not err(r), r)
    lst = cc1.call("manage_review", action="list", project=P, limit=100)
    cl = [i for i in lst.get("items", []) if i["kind"] == "cleanup"]
    if cl:
        n0 = len(list(W.backups.glob("*")))
        r = cursor.call("manage_review", action="approve", project=P, item_id=cl[0]["id"],
                        user_request="yes merge those duplicate sourdough notes")
        R.check(8, "approving a clean-up idea works (shares the recent safety copy)",
                not err(r) and len(list(W.backups.glob("*"))) == n0, short(r, 200))
    else:
        R.word("manage_memory cleanup found no duplicate in two near-identical learnings "
               "('Sourdough starter needs feeding twice a day' vs the same with '!').")
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
    wt = Path("/tmp/fs-230")
    if wt.exists():
        R.check(10, "/tmp/fs-230 must not exist already", False, "exists; not touching it")
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
    R.check(10, "migrations added run_writes, run_views, session_runs", {"run_writes", "run_views", "session_runs"} <= tables, "")
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
    item = next((i["id"] for i in rv.get("items", []) if "v2.3.0" in i["summary"]), None)
    r = a.call("manage_review", action="approve", project="Old Bakery", item_id=item, user_request="fine")
    R.check(10, "a v2.3.0 review item keeps its app: the same app can't approve it with one word", r.get("error") == "own_item", r)
    r = a.call("manage_review", action="approve", project="Old Bakery", item_id=item,
               user_request="Yes, keep the change v2.3.0 queued about order tickets")
    R.check(10, "the same app can't approve it with the user's words either (review is on)",
            r.get("error") == "own_item" and "flowstate approve" in r.get("message", ""), r)
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
            any("auto-captured" in (i.get("summary") or "") + (i.get("details") or "") or i["kind"] == "session"
                for i in rv.get("items", [])), short(rv, 200))
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
    # --db: where do attachments go? (HOME is the sandbox, so the per-OS default is in /tmp too)
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
def report(W, started, real_before, real_after):
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
    changed = sorted(k for k in set(real_before) | set(real_after) if real_before.get(k) != real_after.get(k))
    lines += ["", f"Real FlowState folders untouched: {not changed}" + (f" (changed: {changed[:5]})" if changed else "")]
    return "\n".join(lines)


def git(*a):
    return subprocess.run(["git", "-C", str(REPO), *a], capture_output=True, text=True).stdout.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the /tmp folder")
    ap.add_argument("--skip-upgrade", action="store_true")
    ap.add_argument("--root", default=None)
    args = ap.parse_args()
    root = Path(args.root or f"/tmp/fs-e2e-{os.getpid()}")
    if root.exists():
        sys.exit(f"{root} exists; pick another --root")
    real_before = real_paths_snapshot()
    os.environ["FS_E2E_GUARD_LOG"] = str(root / "guard.log")
    started = datetime.now()
    W = World(root)
    try:
        for step in (s1_fresh, s2_day, s3_conflicts, s4_review, s5_receipts, s6_windows, s7_undo,
                     s8_backups, s9_ledger, lambda w: s10_upgrade(w, args.skip_upgrade), s11_edges,
                     deferred_window, s12_descriptions):
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
    changed = sorted(k for k in set(real_before) | set(real_after) if real_before.get(k) != real_after.get(k))
    R.check(11, "the real FlowState folders are unchanged after the run", not changed, changed[:5])
    text = report(W, started, real_before, real_after)
    (root / "report.md").write_text(text)
    print("\n" + text)
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
        print(f"\n(removed {root})")
    return 0 if all(ok for cs in R.checks.values() for ok, *_ in cs) else 1


if __name__ == "__main__":
    sys.exit(main())
