"""The user-only review step (2.4.0), registered by cli.py:

    flowstate review   [--project P] [--all] [--db FILE]     list what waits for you (read-only)
    flowstate approve  ID [ID ...] [--db FILE]               asks you to confirm, then approves
    flowstate reject   ID [ID ...] [--reason TEXT] [--db FILE]  asks you to confirm, then rejects
    flowstate approvals [status|on|off] [--project P] [--clear]   user-only approvals (approvals.py)
    flowstate approvals phone on|off / popup add|remove NAME        phone link, popup apps

With approvals on, an agent can't approve its own delete requests or turn review off; it asks
you to run one of these (or to use the popup or the phone link). approve / reject and
`approvals off` read your "y" from the controlling
terminal (/dev/tty; on Windows the console), never from stdin, so an agent piping "y" into
the command from its own shell can't confirm, and with no terminal they refuse. This stops
agents approving their own work through FlowState; an agent with full control of your own
terminal window could still type into it.
"""

import json
import os
from pathlib import Path

CLIENT = "command-line"
USER_WORDS = "The user confirmed this in their own terminal (flowstate {what})."
COMMANDS = ("review", "approve", "reject", "approvals")


def say(msg: str) -> None:
    print(msg, flush=True)


def _db(a) -> Path:
    from .cli import _db as base
    return base(a)


class Terminal:
    """The controlling terminal, opened directly (not stdin / stdout)."""

    def __init__(self):
        if os.name == "nt":
            self.inp = open("CONIN$", "r", encoding="utf-8", errors="replace")
            self.out = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        else:
            self.inp = open("/dev/tty", "r", encoding="utf-8", errors="replace")
            self.out = open("/dev/tty", "w", encoding="utf-8", errors="replace")
        if not self.inp.isatty():
            self.close()
            raise OSError("not a terminal")

    def ask(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        return self.inp.readline().strip()

    def tell(self, text: str) -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def close(self) -> None:
        for f in {self.inp, self.out}:
            try:
                f.close()
            except OSError:
                pass


def open_terminal():
    try:
        return Terminal()
    except OSError:
        return None


def _one_line(text, n: int = 100) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 3] + "..."


def _describe(item: dict, project: str) -> list:
    lines = [f"Review item #{item['id']} ({item['kind']}) in {project}, queued by "
             f"{item.get('app') or item.get('source') or 'unknown'}"
             + (f" on {item['machine']}" if item.get("machine") else "") + ":",
             f"  {_one_line(item.get('summary'), 300)}"]
    if item.get("details"):
        lines.append(f"  details: {_one_line(item['details'], 400)}")
    if item["kind"] == "delete":
        p = json.loads(item.get("payload") or "{}")
        lines.append(f"  deletes: {p.get('record_type')} {p.get('project_name') or ''}".rstrip())
    return lines


def _project_name(db: Path, pid) -> str:
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        return row[0] if row else f"project #{pid}"
    finally:
        conn.close()


def cmd_review(a) -> int:
    from . import review
    from .cli_more import _project_id
    db = _db(a)
    res = review.list_items(db, _project_id(db, a.project), "all" if a.all else "pending", a.limit)
    items = res.get("items") or []
    if not items:
        say("Nothing waits for review." if not a.all else "No review items.")
        return 0
    for it in items:
        mark = "" if it.get("status") == "pending" else f" [{it.get('status')}]"
        say(f"#{it['id']:<5} {it.get('project') or it.get('project_id')}: {it['kind']}{mark}, by "
            f"{it.get('app') or it.get('source') or '?'}: {_one_line(it.get('summary'), 80)}")
    say("Decide one with `flowstate approve <id>` or `flowstate reject <id>` (asks you to confirm here).")
    return 0


def _decide(a, approve: bool) -> int:
    from . import review
    from . import server as S
    db = _db(a)
    verb = "approve" if approve else "reject"
    tty = open_terminal()
    if tty is None:
        say(f"Refused: `flowstate {verb}` asks you to confirm in a terminal, and there is none here "
            f"(piped input doesn't count). Run it yourself in a terminal window.")
        return 2
    done, failed = [], 0
    try:
        S.init_backend(db)   # the same code as the MCP tools and the review page
        if S.backend.error:
            say(f"FAILED: FlowState database unavailable: {S.backend.error}")
            return 1
        items = []
        for i in dict.fromkeys(a.ids):
            item = review.get_item(db, i)
            if not item:
                say(f"FAILED: no review item #{i}.")
                failed += 1
            elif item["status"] != "pending":
                say(f"Review item #{i} is already {item['status']}.")
                failed += 1
            else:
                items.append(item)
        if not items:
            return 1
        for item in items:
            for line in _describe(item, _project_name(db, item["project_id"])):
                tty.tell(line)
        which = f"#{items[0]['id']}" if len(items) == 1 else f"these {len(items)} items"
        if tty.ask(f"{verb.capitalize()} {which}? Type y to confirm: ").lower() not in ("y", "yes"):
            say("Not confirmed; nothing changed.")
            return 1
        for item in items:
            args = {"item_id": item["id"], "user_request": USER_WORDS.format(what=f"{verb} {item['id']}")}
            payload = json.loads(item.get("payload") or "{}")
            if approve and item["kind"] == "delete" and payload.get("record_type") == "project":
                args["confirm_name"] = tty.ask(f"#{item['id']} deletes a whole project. Type its exact name: ")
            if not approve:
                args["reason"] = a.reason or "rejected in the user's terminal"
            res = S._review_decide(f"review_{verb}", item["project_id"], args, CLIENT)
            if isinstance(res, dict) and "error" in res:
                say(f"FAILED #{item['id']}: {res.get('message') or res['error']}")
                failed += 1
            else:
                done.append(item["id"])
    finally:
        tty.close()
        S.backend.tracker = None
    if done:
        say(f"{'Approved' if approve else 'Rejected'} review item{'s' if len(done) > 1 else ''} "
            + ", ".join(f"#{i}" for i in done) + ".")
    return 1 if failed else 0


def cmd_approve(a) -> int:
    return _decide(a, True)


def cmd_reject(a) -> int:
    return _decide(a, False)


def cmd_approvals(a) -> int:
    from . import approvals
    from . import server as S
    from .cli_more import _project_id
    db = _db(a)
    S.init_backend(db)   # migrations first (019 holds the settings)
    S.backend.tracker = None
    if S.backend.error:
        say(f"FAILED: FlowState database unavailable: {S.backend.error}")
        return 1
    pid = _project_id(db, a.project)
    where = "this project" if pid is not None else "this install"
    what, arg = a.what, a.arg
    if what == "phone":
        if arg not in ("on", "off"):
            say("Use: flowstate approvals phone on|off")
            return 2
        approvals.set_value(db, "phone_approvals", arg)
        say(f"Phone approval links are {arg}.")
        return 0
    if what == "popup":
        if arg not in ("add", "remove") or not a.name:
            say("Use: flowstate approvals popup add|remove NAME (the app's MCP client name)")
            return 2
        names = approvals.change_popup_clients(db, a.name, arg == "add")
        say("Popup apps: " + ", ".join(sorted(names)))
        return 0
    if what == "off" or (a.clear and pid is not None and approvals.is_on(db, pid)):
        tty = open_terminal()
        if tty is None:
            say("Refused: turning approvals off asks you to confirm in a terminal, and there is "
                "none here (piped input doesn't count). Run it yourself in a terminal window.")
            return 2
        try:
            if tty.ask(f"Turn approvals off for {where}? Agents' deletes then go through at once "
                       f"(with a safety copy and undo). Type y to confirm: ").lower() not in ("y", "yes"):
                say("Not confirmed; nothing changed.")
                return 1
        finally:
            tty.close()
    if what in ("on", "off"):
        res = approvals.set_on(db, what == "on", pid)
    elif a.clear:
        if pid is None:
            say("--clear removes a project's own setting; name it with --project.")
            return 2
        res = approvals.set_on(db, None, pid)
    else:
        on = approvals.is_on(db, pid)
        say(f"Approvals are {'on' if on else 'off'} for {where}. "
            + ("Agents' deletes and turning review off wait for your own click or key (popup, "
               "phone link, or `flowstate approve <id>`)." if on else
               "Agents' deletes go through at once, after a safety copy, and can be undone."))
        say(f"Phone links: {'on' if approvals.phone_on(db) else 'off'}"
            + ("" if approvals.relay_handle() else " (no relay set up)")
            + ("" if approvals.keys_ready() else "; not available in this release yet") + ".")
        say("Popup apps: " + ", ".join(sorted(approvals.popup_clients(db))))
        return 0
    say(f"Approvals are now {res['now']} for {where}.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("review", help="list what waits in the review queue (read-only)")
    p.add_argument("--project", help="one project (name or id); default: all")
    p.add_argument("--all", action="store_true", help="also decided items")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--db")
    p.set_defaults(fn=cmd_review)
    p = sub.add_parser("approve", help="approve review items (you confirm in this terminal)")
    p.add_argument("ids", type=int, nargs="+", metavar="ID")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("reject", help="reject review items (you confirm in this terminal)")
    p.add_argument("ids", type=int, nargs="+", metavar="ID")
    p.add_argument("--reason")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_reject)
    p = sub.add_parser("approvals", help="user-only approvals: status, on, off (off asks you to confirm)")
    p.add_argument("what", nargs="?", default="status", choices=["status", "on", "off", "phone", "popup"])
    p.add_argument("arg", nargs="?", help="phone: on|off; popup: add|remove")
    p.add_argument("name", nargs="?", help="popup: the app's MCP client name")
    p.add_argument("--project", help="one project's own setting (name or id)")
    p.add_argument("--clear", action="store_true", help="drop the project's own setting")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_approvals)
