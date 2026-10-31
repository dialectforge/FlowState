"""The user-only review step (2.4.0), registered by cli.py:

    flowstate review   [--project P] [--all] [--db FILE]     list what waits for you (read-only)
    flowstate review records ID                              a session summary's records, with state
    flowstate review record ID TYPE:ID keep|remove|restore   decide one record (asks you to confirm)
    flowstate approve  ID [ID ...] [--db FILE]               asks you to confirm, then approves
    flowstate reject   ID [ID ...] [--reason TEXT] [--db FILE]  asks you to confirm, then rejects
    flowstate approvals [status|on|off] [--project P] [--clear]   user-only approvals (approvals.py)
    flowstate approvals phone on|off / popup|cards add|remove [--sign-in] NAME  phone link, popup / card apps

With approvals on, an agent can't approve its own delete requests or turn review off; it asks
you to run one of these (or to use the popup or the phone link). approve / reject and
`approvals off` read your "y" from the controlling
terminal (/dev/tty; on Windows the console, and only one with a window), never from stdin, so
an agent piping "y" into the command from its own shell can't confirm, and with no terminal
they refuse. This stops agents approving their own work through FlowState; an agent with
full control of your own terminal window could still type into it, and so could one that
opens a terminal of its own (a pseudo-terminal, or a new console on Windows) to run it in.
"""

import json
import os
from pathlib import Path

from . import term

CLIENT = "command-line"
USER_WORDS = "The user confirmed this in their own terminal (flowstate {what})."
COMMANDS = ("review", "approve", "reject", "approvals")


def say(msg: str) -> None:
    # text from apps and records is in many lines: no escape code reaches the terminal (NEW-7)
    print(term.safe(msg, newlines=True), flush=True)


def _db(a) -> Path:
    from .cli import _db as base
    return base(a)


class Terminal:
    """The controlling terminal, opened directly (not stdin / stdout)."""

    def __init__(self):
        if os.name == "nt":
            if not _console_has_window():
                raise OSError("a console without a window, not the user's terminal")
            self.inp = open("CONIN$", "r", encoding="utf-8", errors="replace")
            self.out = open("CONOUT$", "w", encoding="utf-8", errors="replace")
        else:
            self.inp = open("/dev/tty", "r", encoding="utf-8", errors="replace")
            self.out = open("/dev/tty", "w", encoding="utf-8", errors="replace")
        if not self.inp.isatty():
            self.close()
            raise OSError("not a terminal")

    def ask(self, prompt: str) -> str:
        self.out.write(term.safe(prompt, newlines=True))
        self.out.flush()
        return self.inp.readline().strip()

    def tell(self, text: str) -> None:
        self.out.write(term.safe(text, newlines=True) + "\n")
        self.out.flush()

    def close(self) -> None:
        for f in {self.inp, self.out}:
            try:
                f.close()
            except OSError:
                pass


def _console_has_window() -> bool:
    """Windows: is this a console the user can see and type into? An AI app's shell runs with
    a hidden console that has no window (CREATE_NO_WINDOW): CONIN$ opens there too, but nobody
    can type into it, so reading it would wait forever (and only that app could feed it). A
    console window, or Windows Terminal / VS Code's pseudo-console, has one."""
    import ctypes
    k32 = ctypes.WinDLL("kernel32")
    k32.GetConsoleWindow.restype = ctypes.c_void_p
    return bool(k32.GetConsoleWindow())


def open_terminal():
    try:
        return Terminal()
    except OSError:
        return None


def _one_line(text, n: int = 100) -> str:
    t = " ".join(str(text or "").split())
    t = t if len(t) <= n else t[: n - 3] + "..."
    return term.safe(t)   # escape codes shown, never run (NEW-7)


def _describe(item: dict, project: str, db: Path = None) -> list:
    lines = [f"Review item #{item['id']} ({term.safe(item['kind'])}) in {term.safe(project)}, queued by "
             f"{term.safe(item.get('app') or item.get('source') or 'unknown')}"
             + (f" on {term.safe(item['machine'])}" if item.get("machine") else "") + ":",
             f"  {_one_line(item.get('summary'), 300)}"]
    if item.get("details"):
        lines.append(f"  details: {_one_line(item['details'], 400)}")
    if item["kind"] == "delete":
        p = json.loads(item.get("payload") or "{}")
        lines.append(f"  deletes: {term.safe(p.get('record_type'))} "
                     f"{term.safe(p.get('project_name') or '')}".rstrip())
    if item["kind"] == "session_summary" and db is not None:   # what Keep would keep
        from . import session_contents
        c = session_contents.for_item(db, {**item, "project": project})
        lines.extend("  " + term.safe(x) for x in [session_contents.when_line(c)]
                     + session_contents.lines(c) if x)
        if c and c["entries"] and item.get("status") == "pending":
            from .review import KEEP_ALL_HOW
            lines.append(f"  Approving is Keep all: {KEEP_ALL_HOW} One record: "
                         f"`flowstate review record {item['id']} TYPE:ID keep|remove`.")
    return lines


def _project_name(db: Path, pid) -> str:
    import sqlite3
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        return term.safe(row[0]) if row else f"project #{pid}"
    finally:
        conn.close()


def cmd_review(a) -> int:
    from . import review
    from .cli_more import _project_id
    if a.sub:
        return _records(a)
    db = _db(a)
    res = review.list_items(db, _project_id(db, a.project), "all" if a.all else "pending", a.limit)
    items = res.get("items") or []
    if not items:
        say("Nothing waits for review." if not a.all else "No review items.")
        return 0
    for it in items:
        mark = "" if it.get("status") == "pending" else f" [{it.get('status')}]"
        say(f"#{it['id']:<5} {term.safe(it.get('project') or it.get('project_id'))}: "
            f"{term.safe(it['kind'])}{mark}, by "
            f"{term.safe(it.get('app') or it.get('source') or '?')}: {_one_line(it.get('summary'), 80)}")
    say("Decide one with `flowstate approve <id>` or `flowstate reject <id>` (asks you to confirm here).")
    return 0


RECORD_USE = ("Use: flowstate review records ID (list a session summary's records) or "
              "flowstate review record ID TYPE:ID keep|remove|restore")


def _records(a) -> int:
    """`review records ID`: a session summary's records and their state (read-only).
    `review record ID TYPE:ID keep|remove|restore`: decide one, confirmed with y here."""
    from . import review, session_contents, session_records
    from . import server as S
    rest = list(a.rest or [])
    if not rest or not str(rest[0]).isdigit() or (a.sub == "records" and len(rest) != 1) \
            or (a.sub == "record" and (len(rest) != 3 or rest[2] not in session_records.ACTION_WORDS)):
        say(RECORD_USE)
        return 2
    db = _db(a)
    item = review.get_item(db, int(rest[0]))
    if not item or item["kind"] != "session_summary":
        say(f"FAILED: no session summary #{rest[0]}." if not item else
            f"Review item #{rest[0]} is a {item['kind']}, not a session summary; it has no records.")
        return 1
    project = _project_name(db, item["project_id"])
    c = session_contents.for_item(db, {**item, "project": project})
    entries = (c or {}).get("entries") or []
    if a.sub == "records":
        say(f"Session summary #{item['id']} in {project} ({item['status']}): "
            f"{_one_line(review.strip_tag(item.get('summary')), 200)}")
        if not entries:
            say(session_contents.NONE_NOTE)
            return 0
        for e in entries:
            acts = "/".join(e.get("actions") or [])
            say(f"  {term.safe(e['key']):<16} {term.safe(e.get('state_label', ''))}: "
                f"{term.safe(session_contents.head(e))}"
                + (f"  [{acts}]" if acts else ""))
        if item["status"] == "pending":
            say(session_records.EXPLAIN)
            say(f"Decide one with `flowstate review record {item['id']} TYPE:ID keep|remove|restore` "
                f"(asks you to confirm here); `flowstate approve {item['id']}` keeps all.")
        return 0
    record, action = rest[1], rest[2]
    got = session_records.parse(record)
    e = next((x for x in entries if got and (x["kind"], x["id"]) == got), None)
    if e is None:
        say(f"FAILED: {record} is not one of the records #{item['id']} lists "
            f"(`flowstate review records {item['id']}` lists them).")
        return 1
    tty = open_terminal()
    if tty is None:
        say("Refused: `flowstate review record` asks you to confirm in a terminal, and there is "
            "none here (piped input doesn't count). Run it yourself in a terminal window.")
        return 2
    try:
        tty.tell(f"Session summary #{item['id']} in {project}: {term.safe(session_contents.head(e))}"
                 f" ({term.safe(e.get('state_label', ''))})")
        word = session_records.ACTION_WORDS[action]
        extra = " It is deleted after a safety copy; restore brings it back." \
            if action == "remove" else ""
        if tty.ask(f"{word} {e['key']}?{extra} Type y to confirm: ").lower() not in ("y", "yes"):
            say("Not confirmed; nothing changed.")
            return 1
    finally:
        tty.close()
    S.init_backend(db)   # migrations first, the same code as the review page
    S.backend.tracker = None
    try:
        res = session_records.decide(db, item["id"], e["key"], action, CLIENT)
    except session_records.RecordError as err:
        say(f"FAILED: {err.payload['message']}")
        return 1
    say(res["message"])
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
            for line in _describe(item, _project_name(db, item["project_id"]), db):
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
        old = "on" if approvals.phone_on(db) else "off"
        approvals.set_value(db, "phone_approvals", arg)
        approvals.log_setting(db, "phone_approvals", old, arg)
        say(f"Phone approval links are {arg}.")
        return 0
    if what == "cards" and arg in ("offers", "offer-again"):   # card_offer.py (task 65b)
        from . import card_offer
        if arg == "offers":
            if a.name not in ("on", "off", None):
                say("Use: flowstate approvals cards offers [on|off]")
                return 2
            old = "on" if card_offer.offers_on(db) else "off"
            if a.name:
                card_offer.set_offers(db, a.name == "on")
                approvals.log_setting(db, card_offer.OFFERS, old, a.name)
            say(f"Card offers are {a.name or old}. Already asked (not offered again): "
                + term.safe(", ".join(sorted(card_offer.offered(db))) or "none"))
            return 0
        name = approvals.clean_client_name(a.name)
        if not name:
            say("Use: flowstate approvals cards offer-again NAME (the sign-in's own name)")
            return 2
        before = card_offer.offered(db)
        card_offer.mark_offered(db, name, False)
        approvals.log_setting(db, card_offer.OFFERED, ", ".join(sorted(before)),
                              f"offer again: {name}")
        say(f"{term.safe(name)} is offered the cards again the next time it would show one "
            f"(on your review page and phone; you decide there).")
        return 0
    if what in ("popup", "cards"):
        popup = what == "popup"
        signin = bool(getattr(a, "sign_in", False))
        if arg not in ("add", "remove") or not a.name:
            say(f"Use: flowstate approvals {what} add|remove NAME (the app's MCP client name, for "
                f"an app this computer starts), or add|remove --sign-in NAME (the own name of the "
                f"key or token an app on this computer uses over the LAN"
                + (", or of an OAuth sign-in)" if not popup else ")"))
            return 2
        name = approvals.clean_client_name(a.name)
        if arg == "add":   # its answers then count as yours: your own y, as for approvals off
            tty = open_terminal()
            if tty is None:
                say(f"Refused: adding an app asks you to confirm in a terminal, and there is "
                    f"none here (piped input doesn't count). Run it yourself in a terminal window.")
                return 2
            try:
                does = ("its approval popups count as your own answer" if popup else
                        "taps on its in-chat cards count as your own decisions")
                whom = f"the sign-in {name}" if signin else name
                if tty.ask(f"Add {term.safe(whom)}? Then {does}. Type y to confirm: ").lower() \
                        not in ("y", "yes"):
                    say("Not confirmed; nothing changed.")
                    return 1
            finally:
                tty.close()
        kind = "popup" if popup else "card"
        if signin:
            before = approvals.signins(db, kind)
            names = approvals.change_signins(db, kind, name, arg == "add")
            setting = f"{kind}_signins"
        else:
            lister = approvals.popup_clients if popup else approvals.card_clients
            before = lister(db)
            if popup:
                names = approvals.change_popup_clients(db, name, arg == "add")
            else:
                names = approvals.change_card_clients(db, name, arg == "add")
            setting = "popup_clients" if popup else "card_clients"
        approvals.log_setting(db, setting, ", ".join(sorted(before)),
                              f"{'added' if arg == 'add' else 'removed'} {name}")
        say(("Popup " if popup else "Card ") + ("sign-ins: " if signin else "apps: ")
            + term.safe(", ".join(sorted(names)) or "none"))
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
        say("Popup apps: " + term.safe(", ".join(sorted(approvals.popup_clients(db)))))
        say("Popup sign-ins (LAN keys / tokens of apps on this computer): "
            + term.safe(", ".join(sorted(approvals.signins(db, "popup"))) or "none"))
        say("Card apps (their in-chat card taps count as yours): "
            + term.safe(", ".join(sorted(approvals.card_clients(db))) or "none"))
        say("Card sign-ins (keys, tokens, OAuth apps): "
            + term.safe(", ".join(sorted(approvals.signins(db, "card"))) or "none"))
        say("Chat approvals through the relay: "
            + ("on (AIs on the relay may approve / reject items that aren't yours alone)"
               if approvals.relay_chat_on(db) else "off (phone only)")
            + "; flowstate relay chat-approvals on|off.")
        return 0
    say(f"Approvals are now {res['now']} for {where}.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("review", help="list what waits in the review queue (read-only); "
                                      "records / record: a session summary's records")
    p.add_argument("sub", nargs="?", choices=["records", "record"],
                   help="records ID: list a session summary's records; record ID TYPE:ID "
                        "keep|remove|restore: decide one (you confirm in this terminal)")
    p.add_argument("rest", nargs="*",
                   help="with records / record: the item id, then TYPE:ID and keep|remove|restore")
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
    p.add_argument("what", nargs="?", default="status", choices=["status", "on", "off", "phone", "popup", "cards"])
    p.add_argument("arg", nargs="?", help="phone: on|off; popup / cards: add|remove; cards: "
                   "offers [on|off] (offer cards to network sign-ins once), offer-again NAME")
    p.add_argument("name", nargs="?", help="popup / cards: the app's MCP client name (an app this "
                   "computer starts); with --sign-in, a key's, token's or OAuth app's own name")
    p.add_argument("--sign-in", dest="sign_in", action="store_true",
                   help="popup / cards: NAME is a sign-in's own name (an app over the network)")
    p.add_argument("--project", help="one project's own setting (name or id)")
    p.add_argument("--clear", action="store_true", help="drop the project's own setting")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_approvals)
