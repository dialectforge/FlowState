"""A small review page for the user, on this computer only (v2 Phase D).

    flowstate review-page [--db FILE] [--port 8770] [--no-browser] [--print-url]

Lists the review queue with Approve / Edit / Reject, shows "Who did what" (app, model, machine
and identity source of every recorded change, with Undo where the edit history allows it), and
a Settings section (render_settings): this FlowState's display name, review mode, app pauses, approvals on/off (install and
per project), popup apps, phone links, chat approvals through the relay, config.json
"self_update" and the update check, environment-only settings read-only, and the recent
setting changes. Everything done here counts as the user acting (it is logged as
"review-page"; setting changes in review_mode_log).

Settings (Page.settings): "edit" only on the page `flowstate review-page` opens (a launch-key
session); "read" on the open-link page below (review mode and pausing an app still work there,
except turning review off where approvals are on, and resuming an app; the rest is shown
read-only and its POSTs change nothing); "off" on the tunnel page, which
renders only its old Review mode and Apps blocks and has no /settings/... routes. Turning
approvals off or relay chat approvals on answers with a confirm page whose form carries a
single-use token bound to the session and the exact change, valid CONFIRM_SECONDS; it stands
in for the terminal's `y`. Without the relay here, the relay rows are disabled and ignored.

Two ways to run it:
  * `flowstate review-page`: its own process; opening it needs a one-time key (in the URL
    the command opens in your browser), swapped for an HttpOnly, SameSite=Strict cookie
  * the link in the brief and in manage_review(action="walk") (waiting.py): FlowState starts
    it inside its own process the first time a link is needed, one per computer and
    database. Opening http://127.0.0.1:<port>/ gives the browser a fresh session cookie at
    once (no key: the link is meant to be clicked); every action still needs that cookie,
    the page's CSRF token and a same-origin Origin header

The desktop shortcut setup_flowstate.py makes ("FlowState review") runs the command; if a
FlowState on this computer already serves the linked page for this database on that port, the
command opens that one instead of failing on the busy port (another busy port: any free one).

On the public (tunnel) listener, review_tunnel.py shows this same page behind the owner
passphrase; this module itself is never mounted on the HTTP or public listener.

Safety (both): bound to 127.0.0.1 only; every
form carries a CSRF token; the Host header must be 127.0.0.1/localhost (blocks DNS-rebinding
pages); strict Content-Security-Policy, no caching, no scripts, no framing. Honest limit: a
program on this computer (an agent that runs commands or drives the user's browser) can open
the page too, as it could run `flowstate approve`.
"""

import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional, Union
from urllib.parse import urlencode

from . import dbconn
from . import activity, approvals, history, identity, install_name, paths, review, review_mode, updates, who

HOST = "127.0.0.1"
DEFAULT_PORT = 8770   # 8766 = LAN server, 8767 = public listener
COOKIE = "fs_review"            # the page `flowstate review-page` opens (Settings you can change)
LINK_COOKIE = "fs_review_link"  # the open-link page (read-only Settings)
CLIENT = "review-page"
USER_WORDS = "The user did this on the FlowState review page ({what})."

_CSS = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:1.5rem auto;padding:0 1rem;color:#222}
h1{font-size:1.4rem}h1 .inst{color:#666;font-weight:normal;overflow-wrap:anywhere}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid #ddd}
.item{border:1px solid #ccc;border-radius:8px;padding:.7rem 1rem;margin:.8rem 0}
.meta{color:#666;font-size:.85rem}textarea,input[type=text],select{width:100%;box-sizing:border-box;margin:.2rem 0}
button{padding:.35rem .9rem;margin:.3rem .4rem 0 0}.ok{color:#0a6b0a}.err{color:#b00020}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eee;padding:.25rem;text-align:left;font-size:.9rem}
.row{display:flex;gap:1rem}.row>*{flex:1}
.contents ul{margin:.3rem 0 .5rem 1.2rem;padding:0}.contents li{margin:.3rem 0;overflow-wrap:anywhere}
.contents details summary{cursor:pointer;color:#444;font-size:.9rem}
.disagree,.delcard{background:#fbfaf7;border:1px solid #e7e4dd;border-radius:12px;padding:14px 16px;margin:.4rem 0}
.disagree h3,.delcard .headline{font-family:ui-serif,Georgia,serif;font-weight:500;font-size:1.35rem;margin:.2rem 0 .8rem}
.sides{display:flex;flex-direction:column;gap:12px}
.side{border:1px solid #e7e4dd;border-radius:12px;padding:12px 14px;background:#fff}
.mark{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#1f5f5b;margin:0 0 4px}
.who{color:#62656b;font-size:.9rem;margin:0 0 8px}.claim{margin:0 0 8px;overflow-wrap:anywhere}
.ev{color:#62656b;font-size:.9rem;margin:.2rem 0;overflow-wrap:anywhere}
.keep-row form{display:block;margin:.35rem 0}.keep-row button,.delcard+form button{min-height:44px;border-radius:999px;padding:.6rem 1rem}
.done{color:#1f5f5b}.ainote{color:#62656b;font-size:.95rem}
.kids,.fields{margin:.3rem 0 .6rem;padding-left:1.2rem}
.state{font-size:.8rem;border:1px solid #cfd8d6;border-radius:999px;padding:0 .5rem;color:#1f5f5b;white-space:nowrap}
.state.st-removed,.state.st-gone{color:#8a2d22;border-color:#e2c4bf}.state.st-changed{color:#62656b}
.recbtn{display:inline;margin:0 .3rem 0 0}.recbtn button{padding:.15rem .7rem;margin:.25rem 0 0;border-radius:999px;font-size:.85rem}
@media (min-width:800px){.sides{flex-direction:row;align-items:stretch}.side{flex:1}}
.settings h2,.confirm h2{font-family:ui-serif,Georgia,serif;font-weight:500;font-size:1.35rem}
.settings h3.group{font-family:ui-serif,Georgia,serif;font-weight:500;font-size:1.1rem;color:#1f5f5b;margin:1.4rem 0 .2rem}
.set{background:#fbfaf7;border:1px solid #e7e4dd;border-radius:12px;padding:12px 16px;margin:.6rem 0}
.set h4{font-family:ui-serif,Georgia,serif;font-weight:500;font-size:1.15rem;margin:.1rem 0 .3rem}
.set p{margin:.3rem 0}.set .now b{color:#1f5f5b}.note{color:#62656b;font-size:.9rem}
.set form{margin:.4rem 0}.set ul{margin:.3rem 0;padding-left:1.2rem}
.set button,.confirm button{min-height:44px;border-radius:999px;padding:.5rem 1.1rem;border:1px solid #1f5f5b;background:#fff;color:#1f5f5b;font-size:1rem}
.set button.go,.confirm button.go{background:#1f5f5b;color:#fff}
.set button:disabled{border-color:#c9c9c9;color:#8a8a8a;background:#f3f3f3}
.set.unused{opacity:.65}
.set .inline{display:inline}.set li form{display:inline;margin-left:.4rem}
.set li button{min-height:36px;padding:.2rem .8rem}
.confirm{max-width:38rem}.confirm .set{background:#fff8ec;border-color:#e9d8b4}
@media (max-width:600px){html{-webkit-text-size-adjust:100%}body{overflow-wrap:anywhere}
button{min-height:44px;font-size:1rem}input,select,textarea{min-height:44px;font-size:1rem;max-width:100%}
input[type=checkbox]{min-height:0;width:1.3rem;height:1.3rem;vertical-align:middle}
.row{flex-wrap:wrap}.row>*{min-width:8rem}table{display:block;max-width:100%;overflow-x:auto}
.set li button{min-height:44px}}"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


MAX_SESSIONS = 50   # the least recently used browser session goes first
NEW_PER_MINUTE = 10   # open-link mode: new browser sessions a minute (more: wait a moment)
# Open-link mode: a session used in the last PROTECT_SECONDS is never pushed out by a new
# visit without a cookie (anything on this computer can make those); a new visit that would
# need to is refused instead.
PROTECT_SECONDS = 30 * 60
LINK_BUSY = ("Too many new visits to this page just now; try again in a minute, or run "
             "`flowstate review-page` (its own one-time link isn't affected).")
LINK_ONLY = ("Only you decide this one, and this page can be opened by any program on this "
             "computer, so it isn't decided here. Nothing changed. Use the page "
             "`flowstate review-page` opens, or `flowstate approve {id}` in your terminal.")
LINK_REVIEW_OFF = ("Approvals are on, so turning review off isn't done on this page (any program "
                   "on this computer can open it). Nothing changed. Use the page "
                   "`flowstate review-page` opens, or `flowstate review-mode` in your terminal.")
LINK_RECORD = ("Approvals are on, so Remove and Bring back aren't done on this page (any program "
               "on this computer can open it). Nothing changed. Use the page "
               "`flowstate review-page` opens.")
LINK_RESUME = ("Resuming an app isn't done on this page (any program on this computer can open "
               "it, including the app you paused). Nothing changed. Use the page "
               "`flowstate review-page` opens, or `flowstate apps resume --app \"{app}\"` in "
               "your terminal.")
CONFIRM_SECONDS = 5 * 60   # a settings confirm page must be answered within this
MAX_CONFIRMS = 50
SETTING_WORDS = "set on the FlowState review page"
READ_ONLY = ("These settings can only be changed on the page `flowstate review-page` opens; "
             "nothing changed.")
NO_RELAY = "Only used with the FlowState relay (not set up here); nothing changed."
_clock = time.time


class _Html(str):
    """An action's answer that is a whole page (the settings confirm step), not a flash line."""


# Environment-only settings, shown read-only (set in the app's MCP config): name, plain words,
# default. Never a secret here: FLOWSTATE_TOKEN and the like are not listed and never shown.
ENV_SETTINGS = (
    ("FLOWSTATE_REVIEW_PORT", "The port of this review page when FlowState starts it for a link.",
     "8770; taken: any free port"),
    ("FLOWSTATE_IDLE_MINUTES", "Close an automatic session after this many idle minutes.", "120"),
    ("FLOWSTATE_CALL_TIMEOUT", "Seconds before a slow call answers \"carry on without FlowState\".",
     "15"),
    ("FLOWSTATE_STALE_DAYS", "Facts not checked for this many days are listed for re-checking.", "60"),
    ("FLOWSTATE_TOOLS", "How many tools apps see: core, the default set, or full.", "the default set"),
    ("FLOWSTATE_MODEL", "The model name recorded when a local app gives none.", "none"),
    ("FLOWSTATE_WORKSPACE", "The machine name recorded with each change.", "this computer's name"),
)
# Settings with their own terminal commands (some hold keys or passphrases, so never here)
TERMINAL_SETTINGS = (
    ("Nightly backups", "flowstate schedule-backups [--time HH:MM] / --remove"),
    ("Off-site copy on GitHub", "flowstate offsite setup --repo OWNER/REPO / offsite disable"),
    ("Per-app keys for the HTTP server", "flowstate keys add / list / revoke"),
    ("Remote access (passphrase, app tokens)", "flowstate remote ..."),
    ("HTTP token", "flowstate token migrate / status"),
    ("Git commit capture", "flowstate git-capture ..."),
    ("The relay itself", "flowstate relay enroll / enable / disable / forget"),
)


def db_tag(db_path) -> str:
    """A short, stable tag for a database file (which page serves which database)."""
    return hashlib.sha256(str(Path(db_path).resolve()).lower().encode()).hexdigest()[:16]


class Page:
    """State for one review page. `db_path` is a file, or a callable giving the database in
    use (the in-process page follows the server's backend). `open_link`: no launch key; a
    plain visit to / starts a browser session (waiting.py)."""

    def __init__(self, db_path: Union[Path, Callable[[], Path]], port: int = DEFAULT_PORT,
                 open_link: bool = False, base: str = "", client: str = CLIENT,
                 user_words: str = USER_WORDS, stylesheet: Optional[str] = None,
                 sign_out: Optional[str] = None, settings: Optional[str] = None,
                 secret_path: bool = False):
        self._db = db_path if callable(db_path) else Path(db_path)
        self.port = port
        self.open_link = open_link
        # review_tunnel.py serves the same page under /review/tunnel on the public listener:
        # its links and forms start with `base`, its decisions are labelled `client`, and its
        # CSS is a file (`stylesheet`) because that page's CSP allows no inline style, and it
        # has a Sign out button (posting to `sign_out`)
        self.base, self.client, self.user_words = base, client, user_words
        self.stylesheet, self.sign_out = stylesheet, sign_out
        self.launch_key: Optional[str] = None if open_link else secrets.token_urlsafe(32)
        self.sessions: dict[str, str] = {}   # cookie value -> csrf token
        self.seen: dict[str, float] = {}     # cookie value -> when it was last used
        # The Settings section: "edit" on the page `flowstate review-page` opens (a launch-key
        # session), "read" on the open-link page (anything on this computer can get a session
        # there without a key), "off" anywhere else (the tunnel page: not rendered, no routes)
        self.settings = settings or ("off" if base else "read" if open_link else "edit")
        if self.settings not in ("off", "read", "edit"):
            raise ValueError("settings must be off, read or edit")
        # `flowstate review-page`: the page lives under a secret path, and its cookie is sent
        # only there. Browsers send a 127.0.0.1 cookie to every port, so without it any other
        # local web server (a dev server an agent started) would get the Settings session.
        self.secret_path = bool(secret_path and not base)
        if self.secret_path:
            self.base = "/s/" + secrets.token_urlsafe(18)
        self.cookie = COOKIE if not open_link else LINK_COOKIE   # the two pages never share one
        self.flashes: dict[str, str] = {}   # session -> the last action's answer (shown once)
        self._new: list = []                # when recent sessions were made (open-link mode)
        self.confirms: dict[str, dict] = {}   # hash of a confirm token -> session, change, time

    @property
    def db_path(self) -> Path:
        return Path(self._db()) if callable(self._db) else self._db

    def launch_url(self) -> str:
        if self.open_link:
            return f"http://{HOST}:{self.port}/"
        return f"http://{HOST}:{self.port}{self.base}/?{urlencode({'key': self.launch_key})}"

    def new_session(self) -> Optional[str]:
        """A fresh browser session. Open-link mode (anything on this computer can visit without
        a key): None when too many were made just now, or when making room would push out a
        session used in the last PROTECT_SECONDS, so a program visiting over and over can't
        sign the user out. The launch-key page (one session per one-time key) never refuses."""
        now = _clock()
        if self.open_link:
            self._new = [t for t in self._new if now - t < 60]
            if len(self._new) >= NEW_PER_MINUTE:
                return None
            if len(self.sessions) >= MAX_SESSIONS:
                idle = next(iter(self.sessions))   # the least recently used
                if now - self.seen.get(idle, 0) < PROTECT_SECONDS:
                    return None
            self._new.append(now)
        sess = secrets.token_urlsafe(32)
        self.sessions[sess] = secrets.token_urlsafe(24)
        self.seen[sess] = now
        while len(self.sessions) > MAX_SESSIONS:
            old = next(iter(self.sessions))
            self.sessions.pop(old)
            self.seen.pop(old, None)
            self.flashes.pop(old, None)
        return sess

    def used(self, sess: str) -> None:
        """Mark a session as just used: the least recently used one goes first."""
        self.sessions[sess] = self.sessions.pop(sess)
        self.seen[sess] = _clock()

    # ------------------------------------------------------------ data
    def _conn(self):
        conn = dbconn.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def projects(self) -> list:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute("SELECT id, name FROM projects ORDER BY name")]
        finally:
            conn.close()

    # ------------------------------------------------------------ actions
    def approve(self, form: dict) -> str:
        from . import server as S
        item = review.get_item(self.db_path, int(form["item_id"]))
        if not item:
            return "That item no longer exists."
        if self.open_link and approvals.user_only(item) and item["status"] == "pending":
            return LINK_ONLY.format(id=item["id"])
        args = {"item_id": item["id"], "user_request": self.user_words.format(what="Approve"),
                **{k: form[k] for k in ("summary", "details", "move_to", "confirm_name", "keep")
                   if str(form.get(k, "")).strip()}}
        if args.get("summary") in (item["summary"], review.strip_tag(item["summary"])):
            args.pop("summary")
        if args.get("details") == (item["details"] or ""):
            args.pop("details")
        res = S._review_decide("review_approve", item["project_id"], args, self.client)
        if isinstance(res, dict) and "error" in res:
            return res.get("message") or res["error"]
        if item["kind"] == "disagreement":
            return {"a": "Kept the older entry.", "b": "Kept the newer entry.",
                    "both": "Kept both."}.get(form.get("keep"), "Saved.")
        return f"{'Kept' if item['kind'] == 'session_summary' else 'Approved'} #{item['id']}."

    def reject(self, form: dict) -> str:
        from . import server as S
        item = review.get_item(self.db_path, int(form["item_id"]))
        if not item:
            return "That item no longer exists."
        reason = (form.get("reason") or "").strip() or "rejected on the review page"
        res = S._review_decide("review_reject", item["project_id"],
                               {"item_id": item["id"], "reason": reason,
                                "user_request": self.user_words.format(what="Reject")}, self.client)
        return res.get("message") if isinstance(res, dict) and "error" in res else \
            f"{'Not kept' if item['kind'] == 'session_summary' else 'Rejected'} #{item['id']}."

    def record(self, form: dict) -> str:
        """Keep / Remove / Bring back on one record of a waiting session summary
        (session_records.py), as the user, labelled with this page."""
        from . import session_records
        if self.open_link and form.get("action") in ("remove", "restore"):
            item = review.get_item(self.db_path, int(form["item_id"]))
            if item and approvals.is_on(self.db_path, item["project_id"]):
                return LINK_RECORD
        try:
            res = session_records.decide(self.db_path, int(form["item_id"]), form.get("record"),
                                         form.get("action") or "", self.client)
        except session_records.RecordError as e:
            return e.payload["message"]
        return f"#{res['item_id']}: {res['message']}"

    def set_mode(self, form: dict) -> str:
        pid = int(form["project_id"]) if form.get("project_id") else None
        if self.open_link and review_mode.off_needs_user(self.db_path, form.get("mode"), pid,
                                                         bool(form.get("clear"))):
            return LINK_REVIEW_OFF   # the same "turns review off" rule as the MCP tool
        try:
            r = review_mode.set_mode(self.db_path, form.get("mode"),
                                     [k for k in review_mode.KINDS if form.get(f"kind_{k}")], pid,
                                     self.user_words.format(what="set review mode"), self.client, self.client,
                                     clear=bool(form.get("clear")))
        except review_mode.ModeError as e:
            return e.payload["message"]
        return f"Review mode for {r['scope']}: {r['now']['mode']} ({r['text']})."

    def set_app(self, form: dict) -> str:
        app = form.get("app", "")
        paused = form.get("paused") == "1"
        if self.open_link and not paused:   # a paused app (or anything here) could resume itself
            return LINK_RESUME.format(app=app.strip()[:80])
        held = {} if paused else activity.still_paused_by(self.db_path, app)
        names = activity.pause_names(self.db_path, app, paused)
        for n in names:
            was = activity.is_paused(self.db_path, n)
            activity.set_paused(self.db_path, n, paused, self.client)
            self._log(f"app:{n.strip()[:70]}", "paused" if was else "running",
                      "paused" if paused else "running")
        also = f" (with {', '.join(names[1:])}, the key or sign-in it uses)" if names[1:] else ""
        return f"{app}{also}: {'paused' if paused else 'resumed'}." + "".join(
            " " + activity.held_text(app, k, o) for k, o in held.items())

    # ------------------------------------------------------------ settings (local page only)
    def _log(self, scope: str, old: Optional[str], new: Optional[str]) -> None:
        """One review_mode_log row for a setting changed here (the settings that don't log
        themselves: app pauses, phone links, popup apps, config.json)."""
        approvals.log_setting(self.db_path, scope, old, new, self.client, SETTING_WORDS)

    def _project_ids(self) -> set:
        return {p["id"] for p in self.projects()}

    def _approvals_change(self, form: dict) -> tuple:
        scope, state = str(form.get("scope", "")).strip(), form.get("state")
        if state not in ("on", "off", "clear"):
            raise ValueError("state")
        if scope and (not scope.isdigit() or int(scope) not in self._project_ids()):
            raise ValueError("scope")
        if state == "clear" and not scope:
            raise ValueError("clear needs a project")
        return ("approvals", scope, state)

    def _relay_change(self, form: dict) -> tuple:
        if form.get("state") not in ("on", "off"):
            raise ValueError("state")
        return ("relay_chat", "", form["state"])

    def _needs_confirm(self, change: tuple) -> bool:
        """The page's stand-in for the terminal's `y`, in the same cases as the commands:
        approvals off (or clearing a project's own setting while it is on), relay chat
        approvals on."""
        what, scope, state = change
        if what == "approvals":
            pid = int(scope) if scope else None
            return state == "off" or (state == "clear" and approvals.is_on(self.db_path, pid))
        return what == "relay_chat" and state == "on" and not approvals.relay_chat_on(self.db_path)

    def _apply(self, change: tuple) -> str:
        what, scope, state = change
        if what == "approvals":
            pid = int(scope) if scope else None
            res = approvals.set_on(self.db_path, None if state == "clear" else state == "on", pid,
                                   self.client, SETTING_WORDS)
            where = "the whole install" if pid is None else self._name(pid)
            return f"Approvals are now {res['now']} for {where}."
        if not approvals.relay_set_up():
            return NO_RELAY
        res = approvals.set_relay_chat(self.db_path, state == "on", self.client, SETTING_WORDS)
        return f"Chat approvals through the relay are now {res['now']}."

    def _name(self, pid: int) -> str:
        return next((p["name"] for p in self.projects() if p["id"] == pid), f"project {pid}")

    def _ask(self, sess: str, change: tuple) -> "_Html":
        """The confirm page: a fresh single-use token bound to this session and this change."""
        tok, now = secrets.token_urlsafe(24), _clock()
        self.confirms = {k: v for k, v in self.confirms.items()
                         if now - v["at"] < CONFIRM_SECONDS}
        while len(self.confirms) >= MAX_CONFIRMS:
            self.confirms.pop(next(iter(self.confirms)))
        self.confirms[hashlib.sha256(tok.encode()).hexdigest()] = {
            "sess": sess, "change": change, "at": now}
        return _Html(self.render_confirm(self.sessions[sess], tok, change))

    def set_approvals(self, form: dict, sess: str):
        if self.settings != "edit":
            return READ_ONLY
        change = self._approvals_change(form)
        return self._ask(sess, change) if self._needs_confirm(change) else self._apply(change)

    def set_relay_chat(self, form: dict, sess: str):
        if self.settings != "edit":
            return READ_ONLY
        if not approvals.relay_set_up():
            return NO_RELAY
        change = self._relay_change(form)
        return self._ask(sess, change) if self._needs_confirm(change) else self._apply(change)

    def confirm(self, form: dict, sess: str) -> str:
        """The confirm page's Yes. The token is used up whatever happens next; it must belong to
        this session, be younger than CONFIRM_SECONDS and name exactly the change posted."""
        if self.settings != "edit":
            return READ_ONLY
        entry = self.confirms.pop(hashlib.sha256(str(form.get("token", "")).encode()).hexdigest(),
                                  None)
        try:
            posted = (self._approvals_change(form) if form.get("setting") == "approvals"
                      else self._relay_change(form) if form.get("setting") == "relay_chat"
                      else None)
        except ValueError:
            posted = None
        if entry is None or not hmac.compare_digest(entry["sess"], sess) \
                or _clock() - entry["at"] >= CONFIRM_SECONDS or posted != entry["change"]:
            return "That confirmation is no longer valid; nothing changed. Try again."
        return self._apply(entry["change"])

    def cancel(self, form: dict, sess: str) -> str:
        entry = self.confirms.get(hashlib.sha256(str(form.get("token", "")).encode()).hexdigest())
        if entry is not None and hmac.compare_digest(entry["sess"], sess):
            self.confirms.pop(hashlib.sha256(str(form["token"]).encode()).hexdigest(), None)
        return "Cancelled; nothing changed."

    def set_phone(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        if not approvals.relay_set_up():
            return NO_RELAY
        state = form.get("state")
        if state not in ("on", "off"):
            raise ValueError("state")
        old = "on" if approvals.phone_on(self.db_path) else "off"
        approvals.set_value(self.db_path, "phone_approvals", state, self.client)
        self._log("phone_approvals", old, state)
        return f"Phone approval links are {state}."

    def set_popup(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        name, op = " ".join(str(form.get("name", "")).split())[:80], form.get("op")
        if op not in ("add", "remove") or not name:
            return "Give the app's name as it connects (its MCP client name)."
        before = approvals.popup_clients(self.db_path)
        approvals.change_popup_clients(self.db_path, name, op == "add", self.client)
        self._log("popup_clients", ", ".join(sorted(before)),
                  f"{'added' if op == 'add' else 'removed'} {name.lower()}")
        if op == "add" and any(m in name.lower() for m in approvals.CONNECTOR_MARKS):
            return (f"Added {name.lower()}, but claude.ai and Anthropic apps never get a popup, "
                    "whatever the list says.")
        return f"{'Added' if op == 'add' else 'Removed'} {name.lower()}."

    def set_cards(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        name, op = approvals.clean_client_name(form.get("name", "")), form.get("op")
        if op not in ("add", "remove") or not name:
            return "Give the app's name as it connects (its MCP client name)."
        before = approvals.card_clients(self.db_path)
        approvals.change_card_clients(self.db_path, name, op == "add", self.client)
        self._log("card_clients", ", ".join(sorted(before)),
                  f"{'added' if op == 'add' else 'removed'} {name}")
        return f"{'Added' if op == 'add' else 'Removed'} {name}."

    def set_self_update(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        state = form.get("state")
        if state not in ("on", "off"):
            raise ValueError("state")
        new = updates.set_self_update(state == "on", self.db_path, self.client, SETTING_WORDS)["now"]
        return ("`flowstate update` may update this computer again." if new == "allowed" else
                "This computer is now updated by hand: `flowstate update` refuses to run.")

    def set_display_name(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        reset = form.get("op") == "reset"
        try:
            res = install_name.set_name(None if reset else form.get("name", ""), self.db_path,
                                        self.client, SETTING_WORDS)
        except install_name.BadName as e:
            return f"Not changed: {e}"
        if not res["changed"]:
            return f"This FlowState is already called \"{res['now']}\"."
        return f"This FlowState is now called \"{res['now']}\"."

    def set_update_check(self, form: dict, sess: str) -> str:
        if self.settings != "edit":
            return READ_ONLY
        state = form.get("state")
        if state not in ("on", "off"):
            raise ValueError("state")
        old = "on" if updates.is_on() else "off"
        updates.set_on(state == "on")
        self._log("config:update_check", old, state)
        note = (" (FLOWSTATE_UPDATE_CHECK in the environment still keeps it off)"
                if state == "on" and not updates.is_on() else "")
        return f"The daily update check is {state}{note}."

    def undo(self, form: dict) -> str:
        """Undo from "Who did what": the same code as history(action="undo")."""
        from . import server as S
        if form.get("record_type") not in history.TABLES:
            return "Unknown record type."
        res = S._history(int(form["project_id"]),
                         {"action": "undo", "record_type": form["record_type"],
                          "record_id": int(form["record_id"])}, self.client)
        if isinstance(res, dict) and "error" in res:
            return res.get("message") or res["error"]
        return f"Undid the last change to {form['record_type']} #{form['record_id']}."

    # ------------------------------------------------------------ html
    def _head(self, flash: str, view: str, csrf: str = "") -> list:
        style = (f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                 f"<link rel='stylesheet' href='{_e(self.stylesheet)}'>" if self.stylesheet
                 else f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                      f"<style>{_CSS}</style>")
        name = _e(install_name.get())
        out = [f"<!doctype html><html><head><meta charset='utf-8'><title>FlowState review: {name}</title>"
               f"{style}</head><body><h1>FlowState review <span class='inst'>· {name}</span></h1>"
               + (f"<form method='post' action='{_e(self.sign_out)}'><input type='hidden' "
                  f"name='csrf' value='{_e(csrf)}'><button>Sign out</button></form>"
                  if self.sign_out else "") +
               f"<p class='meta'><a href='{self.base}/'>Review queue</a> · <a href='{self.base}/?view=who'>Who did what</a>"
               f"{' (this page)' if view == 'who' else ''}</p>"]
        if flash:
            out.append(f"<p class='ok'>{_e(flash)}</p>")
        return out

    def render_who(self, csrf: str, flash: str = "", q: Optional[dict] = None) -> str:
        """Who did what: recent recorded changes with app, model, machine and identity source;
        filters; an Undo button on rows the edit history can undo."""
        q = {k: (v or "").strip() for k, v in (q or {}).items()}
        projects = self.projects()
        pid = int(q["project"]) if q.get("project", "").isdigit() else None
        page_no = int(q["page"]) if q.get("page", "").isdigit() else 1
        out = self._head(flash, "who", csrf)
        try:
            res = who.recent(self.db_path, q.get("app"), q.get("model"), q.get("machine"), pid,
                             q.get("since"), q.get("until"), page_no, who.PER_PAGE)
        except who.WhoError as e:
            res = {"rows": [], "total": 0, "page": 1, "pages": 1}
            out.append(f"<p class='err'>{_e(e.payload['message'])}</p>")
        opts = "".join(f'<option value="{p["id"]}"{" selected" if p["id"] == pid else ""}>{_e(p["name"])}</option>'
                       for p in projects)
        def field(n: str, ph: str) -> str:
            return f'<input type="text" name="{n}" placeholder="{ph}" value="{_e(q.get(n, ""))}">'
        out.append(f"""<h2>Who did what ({res['total']})</h2>
<form method="get" class="row"><input type="hidden" name="view" value="who">{field('app', 'app')}{field('model', 'model')}{field('machine', 'machine')}
<select name="project"><option value="">All projects</option>{opts}</select>{field('since', 'since (2026-10-17)')}{field('until', 'until')}<button>Filter</button></form>
<p class="meta">Times are this computer's local time. Model is shown only when the app supplied it. Identity says how the app name was established: only per-app keys, remote tokens and OAuth apps can't pick their own name.</p>""")
        hidden = f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        out.append("<table><tr><th>Time</th><th>App</th><th>Model</th><th>Machine</th>"
                   "<th>Identity</th><th>Project</th><th>Record</th><th>What changed</th><th></th></tr>")
        for r in res["rows"]:
            undo = ""
            if r["undo"]:
                u = r["undo"]
                undo = (f'<form method="post" action="{self.base}/undo">{hidden}'
                        f'<input type="hidden" name="record_type" value="{_e(u["record_type"])}">'
                        f'<input type="hidden" name="record_id" value="{_e(u["record_id"])}">'
                        f'<input type="hidden" name="project_id" value="{_e(u["project_id"])}">'
                        f'<button>Undo</button></form>')
            out.append(f"<tr><td>{_e(r['at'])}</td><td>{_e(identity.shown_row(r, fallback='-'))}</td>"
                       f"<td>{_e(r['model'] or '-')}</td><td>{_e(r['machine'] or '-')}</td>"
                       f"<td>{_e(r['identity'])}</td><td>{_e(r['project'] or '')}</td>"
                       f"<td>{_e(r['record_type'])} #{_e(r['record_id'])}</td>"
                       f"<td>{_e(r['what'])}</td><td>{undo}</td></tr>")
        out.append("</table>")
        if res["pages"] > 1:
            keep = {k: v for k, v in q.items() if v and k not in ("page", "flash", "view")}
            nav = []
            if res["page"] > 1:
                nav.append(f"<a href='{self.base}/?{_e(urlencode({'view': 'who', **keep, 'page': res['page'] - 1}))}'>Newer</a>")
            nav.append(f"page {res['page']} of {res['pages']}")
            if res["page"] < res["pages"]:
                nav.append(f"<a href='{self.base}/?{_e(urlencode({'view': 'who', **keep, 'page': res['page'] + 1}))}'>Older</a>")
            out.append(f"<p>{' · '.join(nav)}</p>")
        out.append("</body></html>")
        return "".join(out)

    def render(self, csrf: str, flash: str = "", project: str = "", status: str = "pending") -> str:
        projects = self.projects()
        pid = int(project) if project.isdigit() else None
        try:
            items = review.list_items(self.db_path, pid, status, 100)["items"]
        except review.ReviewError as e:
            items, flash = [], e.payload["message"]
        hidden = f'<input type="hidden" name="csrf" value="{_e(csrf)}">'
        opts = "".join(f'<option value="{p["id"]}"{" selected" if p["id"] == pid else ""}>{_e(p["name"])}</option>'
                       for p in projects)
        out = self._head(flash, "queue", csrf)
        out.append(f"""<form method="get" class="row"><select name="project"><option value="">All projects</option>{opts}</select>
<select name="status">{''.join(f'<option{" selected" if s == status else ""}>{s}</option>' for s in ("pending", "approved", "rejected", "all"))}</select>
<button>Show</button></form>""")
        out.append(f"<h2>Review queue ({len(items)} {status})</h2>")
        if not items:
            out.append("<p>Nothing here.</p>")
        for it in items:
            out.append(f"""<div class="item"><div class="meta">#{it['id']} · {_e(it['kind'])} · {_e(it['project'])} · from {_e(identity.shown_row(it, fallback=it['source'] or 'an app') if it.get('app') else it['source'])} · {_e(self._when(it['created_at']))} · {_e(it['status'])}</div>""")
            if it["status"] == "pending":
                move = ""
                if it["kind"] == "cleanup" and '"misfiled"' in (it.get("payload") or ""):
                    targets = "".join(f'<option value="{p["id"]}">{_e(p["name"])}</option>'
                                      for p in projects)
                    move = ('<label>Or move it to (empty = the suggested project)<select name="move_to">'
                            f'<option value=""></option>{targets}</select></label>')
                if it["kind"] == "cleanup":   # one plain question: nothing to edit
                    verb = "move" if move else "cancel"
                    out.append(f"""<p><strong>{_e(it['summary'])}</strong></p><p class="meta">{_e(it['details'])}</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">{move}
<button>Approve: {verb} it</button></form>""")
                elif it["kind"] == "conflict":   # possible conflicting decisions: nothing to edit
                    out.append(f"""<p><strong>{_e(it['summary'])}</strong></p><p class="meta">{_e(it['details'])}</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<button>Approve: they really conflict</button></form>""")
                elif it["kind"] == "rule":   # a hard rule an app asked for: nothing to edit
                    out.append(f"""<p><strong>{_e(it['summary'])}</strong></p><p class="meta">{_e(it['details'])}</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<button>Approve: {"remove" if '"action": "remove"' in (it.get("payload") or "") else "add"} this rule</button></form>""")
                elif it["kind"] == "source":   # "did you say this?": nothing to edit
                    out.append(f"""<p><strong>{_e(it['summary'])}</strong></p><p class="meta">{_e(it['details'])}</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<button>Approve: I said this</button></form>""")
                elif it["kind"] == "disagreement":
                    from . import conflict_cards
                    out.append(conflict_cards.html_block(self.db_path, it, hidden, self.base, True))
                elif it["kind"] == "delete":   # nothing to edit; a project needs its name typed
                    from . import deletes
                    confirm = ""
                    if '"record_type": "project"' in (it.get("payload") or ""):
                        confirm = ('<label>Type the project name to confirm '
                                   '<input type="text" name="confirm_name" autocomplete="off"></label>')
                    out.append(deletes.html_for(self.db_path, it))
                    out.append(f"""<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">{confirm}
<button>Approve: delete it</button></form>""")
                elif it["kind"] == "session_summary":   # Keep / Don't keep: what they really do
                    block, listed = self._contents(it, hidden)
                    out.append(block)
                    how, keep = (review.KEEP_ALL_HOW, review.KEEP_ALL) if listed else \
                        (review.KEEP_HOW, review.KEEP)
                    out.append(f"""<p><strong>{review.KEEP_QUESTION}</strong> {_e(how)} You can edit it first.</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<textarea name="summary" rows="2">{_e(review.strip_tag(it['summary']))}</textarea><textarea name="details" rows="2" placeholder="details">{_e(it['details'])}</textarea>
<button>{_e(keep)} (with any edits)</button></form>""")
                else:
                    out.append(f"""<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<textarea name="summary" rows="2">{_e(it['summary'])}</textarea><textarea name="details" rows="2" placeholder="details">{_e(it['details'])}</textarea>{move}
<button>Approve (with any edits)</button></form>""")
                no = (f'<input type="text" name="reason" placeholder="Reason (optional)"><button>{_e(review.DONT_KEEP)}</button>'
                      if it["kind"] == "session_summary" else
                      '<input type="text" name="reason" placeholder="Reason for dismissing"><button>Dismiss</button>'
                      if it["kind"] == "disagreement" else
                      '<input type="text" name="reason" placeholder="Reason for rejecting"><button>Reject</button>')
                out.append(f"""
<form method="post" action="{self.base}/reject">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
{no}</form>""")
            else:
                if it["kind"] == "disagreement":
                    from . import conflict_cards
                    out.append(conflict_cards.html_block(self.db_path, it, hidden, self.base, False))
                elif it["kind"] == "delete":
                    from . import deletes
                    out.append(deletes.html_for(self.db_path, it))
                else:
                    out.append(f"<p>{_e(review.strip_tag(it['summary']))}</p>")
                    if it["kind"] == "session_summary":
                        out.append(self._contents(it)[0])
                if it.get("reject_reason"):
                    out.append(f"<p class='meta'>Reason: {_e(it['reject_reason'])}</p>")
            out.append("</div>")
        if self.settings != "off":
            out.append(self.render_settings(hidden, opts))
            out.append(self._recent_activity())
            out.append("</body></html>")
            return "".join(out)
        # review mode (the tunnel page: these two blocks only, no Settings section)
        m = review_mode.get(self.db_path, None)
        kinds = "".join(f'<label><input type="checkbox" name="kind_{k}"{" checked" if k in m["kinds"] else ""}> {k}</label> '
                        for k in review_mode.KINDS)
        out.append(f"""<h2>Review mode</h2><p>Whole install: <b>{_e(m['mode'])}</b>, {_e(review_mode.describe(m))}.</p>
<form method="post" action="{self.base}/mode">{hidden}<div class="row"><select name="mode">{''.join(f'<option{" selected" if x == m["mode"] else ""}>{x}</option>' for x in review_mode.MODES)}</select>
<select name="project_id"><option value="">Whole install</option>{opts.replace(' selected', '')}</select></div>
<p>For "types": {kinds}</p><label><input type="checkbox" name="clear"> Remove this project's own setting</label><br><button>Set review mode</button></form>""")
        # apps
        out.append("<h2>Apps</h2><table><tr><th>App</th><th>Last seen</th><th>Calls</th><th>Writes</th><th></th></tr>")
        for a in activity.apps(self.db_path):
            out.append(f"""<tr><td>{_e(activity.app_label(a))}{' <b>(paused)</b>' if a['paused'] else ''}</td><td>{_e(a['last_seen'] or '-')}</td>
<td>{a['calls']}</td><td>{a['writes'] or 0}</td><td><form method="post" action="{self.base}/app">{hidden}<input type="hidden" name="app" value="{_e(a['app'])}">
<input type="hidden" name="paused" value="{'0' if a['paused'] else '1'}"><button>{'Resume' if a['paused'] else 'Pause writes'}</button></form></td></tr>""")
        out.append("</table>")
        out.append(self._recent_activity())
        out.append("</body></html>")
        return "".join(out)

    def _when(self, utc: str) -> str:
        """This computer's local time on the local page; UTC, labelled, on the tunnel page."""
        return who.local_time(utc) if self.settings != "off" else f"{utc} UTC"

    def _contents(self, it: dict, hidden: str = "") -> tuple:
        """(html, whether records are listed): what a session summary's session saved
        (session_contents.py), escaped, with each waiting record's Keep / Remove / Bring back
        forms when `hidden` (the CSRF field) is given. This computer's local time on the local
        page; UTC, labelled, on the tunnel page (its reader may be anywhere)."""
        from . import session_contents, session_records
        local = who.local_time if self.settings != "off" else None
        c = session_contents.for_item(self.db_path, it)

        def buttons(e: dict) -> str:
            return "<div>" + "".join(
                f'<form class="recbtn" method="post" action="{self.base}/record">{hidden}'
                f'<input type="hidden" name="item_id" value="{it["id"]}">'
                f'<input type="hidden" name="record" value="{_e(e["key"])}">'
                f'<input type="hidden" name="action" value="{a}">'
                f'<button>{session_records.ACTION_WORDS[a]}</button></form>'
                for a in e["actions"]) + "</div>"
        return (session_contents.html(c, local, buttons=buttons if hidden else None),
                bool(c and c["entries"]))

    def _recent_activity(self) -> str:
        out = ["<h2>Recent activity</h2><table><tr><th>When (UTC)</th><th>App</th><th>Tool</th><th>Project</th><th>Result</th></tr>"]
        for r in activity.recent(self.db_path, None, 30):
            out.append(f"<tr><td>{_e(r['at'])}</td><td>{_e(identity.shown_row(r))}</td><td>{_e(r['tool'])}{('/' + _e(r['action'])) if r['action'] else ''}</td>"
                       f"<td>{_e(r['project'] or '')}</td><td>{'ok' if r['ok'] else 'error ' + _e(r['error'])}</td></tr>")
        out.append("</table>")
        return "".join(out)

    # ------------------------------------------------------------ settings html
    def _row(self, name: str, what: str, now: str, control: str = "", note: str = "",
             unused: bool = False) -> str:
        return (f"<div class='set{' unused' if unused else ''}'><h4>{_e(name)}</h4>"
                f"<p class='what'>{what}</p><p class='now'>Now: {now}</p>{control}"
                + (f"<p class='note'>{note}</p>" if note else "") + "</div>")

    def _buttons(self, action: str, hidden: str, choices, extra: str = "",
                 disabled: bool = False) -> str:
        """A form with one button per (value, label, primary) choice, posting name=state."""
        if self.settings != "edit":
            return ""
        dis = " disabled" if disabled else ""
        btns = "".join(f"<button name='state' value='{v}'{' class=go' if go else ''}{dis}>{_e(t)}</button> "
                       for v, t, go in choices)
        return f"<form method='post' action='{action}'>{hidden}{extra}{btns}</form>"

    def _scope_label(self, scope: str, names: dict) -> str:
        def proj(pid: str) -> str:
            return names.get(int(pid), f"project {pid}") if pid.isdigit() else pid
        if scope == "install":
            return "Review mode, whole install"
        if scope.startswith("project:"):
            return f"Review mode, {proj(scope[8:])}"
        if scope == "approvals":
            return "Approvals, whole install"
        if scope.startswith("approvals:project:"):
            return f"Approvals, {proj(scope[18:])}"
        if scope == approvals.RELAY_CHAT_KEY:
            return "Chat approvals through the relay"
        if scope.startswith("app:"):
            return f"Pause {scope[4:]}"
        return {"phone_approvals": "Phone approval links", "popup_clients": "Popup apps",
                "card_clients": "Review card apps",
                "config:self_update": "Updates by flowstate update",
                install_name.SCOPE: "Name of this FlowState",
                "config:update_check": "Daily update check"}.get(scope, scope)

    def recent_changes(self, limit: int = 8) -> list:
        conn = self._conn()
        try:
            if not review_mode.available(conn):
                return []
            return [dict(r) for r in conn.execute(
                "SELECT * FROM review_mode_log ORDER BY id DESC LIMIT ?", (limit,))]
        finally:
            conn.close()

    def render_settings(self, hidden: str, opts: str) -> str:
        """The Settings section: every user setting in plain words, its value and its control.
        Never a token, key or passphrase."""
        db, edit = self.db_path, self.settings == "edit"
        projects = self.projects()
        names = {p["id"]: p["name"] for p in projects}
        popts = opts.replace(" selected", "")
        relay = approvals.relay_set_up()
        out = ["<section class='settings'><h2>Settings</h2>"]
        if edit:
            out.append("<p class='note'>Only you change these, here or in your terminal. No AI "
                       "app can read or change them through FlowState.</p>")
        else:
            out.append("<p class='note'>Review mode and app pauses can be changed here. The other "
                       "settings are shown read-only: open <code>flowstate review-page</code> in "
                       "your terminal to change these (this page opened from a link, which "
                       "anything on this computer could open too).</p>")
        # -- this FlowState
        out.append("<h3 class='group'>This FlowState</h3>")
        name = install_name.get()
        rename = (f"<form method='post' action='{self.base}/settings/name'>{hidden}<div class='row'>"
                  f"<input type='text' name='name' maxlength='{install_name.MAX_LEN}' required "
                  f"value='{_e(name)}' aria-label='New name' autocomplete='off'>"
                  "<button class='go'>Save name</button>"
                  + ("" if install_name.is_default() else
                     "<button name='op' value='reset' formnovalidate>Back to "
                     f"&quot;{_e(install_name.DEFAULT)}&quot;</button>")
                  + "</div></form>") if edit else ""
        out.append(self._row(
            "Name of this FlowState",
            "The name you see for this FlowState on its pages. With the relay, it is shared with "
            "flowstatemcp.com only once you set one (\"Open what's waiting for you on ...\"); "
            "until then, and after going back to the default, the site just says \"your "
            "FlowState\". Only a label: your relay address stays the same. 1 to 40 characters, "
            "plain text.",
            f"<b>{_e(name)}</b>" + (" (the default)." if install_name.is_default() else "."), rename,
            "In a terminal: <code>flowstate name \"New name\"</code>, or <code>flowstate name "
            "--reset</code>. With the relay, a name you set is sent to it (on its signed-in "
            "connection) so the site can show it to you; the default is never sent, and "
            "<code>--reset</code> clears it there too."))
        # -- review
        out.append("<h3 class='group'>Review</h3>")
        m = review_mode.get(db, None)
        own = [(p, review_mode.get(db, p["id"])) for p in projects]
        own = [(p, x) for p, x in own if x.get("source") == "project"]
        per = "".join(f"<li>{_e(p['name'])}: <b>{_e(x['mode'])}</b>, {_e(review_mode.describe(x))}</li>"
                      for p, x in own)
        kinds = "".join(f'<label><input type="checkbox" name="kind_{k}"{" checked" if k in m["kinds"] else ""}> {k}</label> '
                        for k in review_mode.KINDS)
        form = f"""<form method="post" action="{self.base}/mode">{hidden}<div class="row"><select name="mode">{''.join(f'<option{" selected" if x == m["mode"] else ""}>{x}</option>' for x in review_mode.MODES)}</select>
<select name="project_id"><option value="">Whole install</option>{popts}</select></div>
<p>For "types": {kinds}</p><label><input type="checkbox" name="clear"> Remove this project's own setting</label><br><button class="go">Set review mode</button></form>"""
        out.append(self._row(
            "Review mode", "Which new records wait in the review queue for your OK before they "
            "are saved: off, auto_only (only auto-captured summaries and suggestions), types "
            "(also the kinds you tick) or everything.",
            f"<b>{_e(m['mode'])}</b> for the whole install, {_e(review_mode.describe(m))}."
            + (f"<ul>{per}</ul>" if per else " No project has its own setting."), form))
        apps = activity.apps(db)

        def act(a) -> str:
            if a["paused"] and self.open_link:   # resuming is not done on the link page
                return (f"<span class='note'>Resume: <code>flowstate apps resume --app "
                        f"&quot;{_e(a['app'])}&quot;</code></span>")
            return (f"""<form method="post" action="{self.base}/app">{hidden}<input type="hidden" name="app" value="{_e(a['app'])}">
<input type="hidden" name="paused" value="{'0' if a['paused'] else '1'}"><button>{'Resume' if a['paused'] else 'Pause writes'}</button></form>""")
        rows = "".join(
            f"""<tr><td>{_e(activity.app_label(a))}{' <b>(paused)</b>' if a['paused'] else ''}</td><td>{_e(a['last_seen'] or '-')}</td>
<td>{a['calls']}</td><td>{a['writes'] or 0}</td><td>{act(a)}</td></tr>"""
            for a in apps)
        paused = [a["app"] for a in apps if a["paused"]]
        out.append(self._row(
            "App pauses", "A paused app can still read, but FlowState refuses its writes until you "
            "resume it.", (f"paused: <b>{_e(', '.join(paused))}</b>." if paused else
                           "<b>no app paused</b>."),
            ("<table><tr><th>App</th><th>Last seen</th><th>Calls</th><th>Writes</th><th></th></tr>"
             f"{rows}</table>") if apps else "<p class='note'>No app has connected yet.</p>"))
        # -- approvals
        out.append("<h3 class='group'>Approvals</h3>")
        on = approvals.is_on(db)
        per = "".join(f"<li>{_e(p['name'])}: <b>{'on' if approvals.is_on(db, p['id']) else 'off'}</b></li>"
                      for p in projects if self._own_approvals(p["id"]) is not None)
        scope = (f"<select name='scope'><option value=''>Whole install</option>{popts}</select>"
                 if edit else "")
        out.append(self._row(
            "Approvals for deletes and turning review off",
            "When on, an AI's delete requests and turning review off wait for your own click or key "
            "(a popup, your phone, this page or your terminal). When off, deletes go through at "
            "once, after a safety copy, and can be undone.",
            f"<b>{'on' if on else 'off'}</b> for the whole install."
            + (f"<ul>{per}</ul>" if per else " No project has its own setting."),
            self._buttons(f"{self.base}/settings/approvals", hidden,
                          (("on", "Turn on", True), ("off", "Turn off", False),
                           ("clear", "Remove the project's own setting", False)), scope),
            "Turning them off asks you to confirm on a second page, like "
            "<code>flowstate approvals off</code> asks for your <code>y</code>."))
        popup = sorted(approvals.popup_clients(db))
        items = "".join(
            f"<li>{_e(n)}" + (f"<form method='post' action='{self.base}/settings/popup'>{hidden}"
                              f"<input type='hidden' name='op' value='remove'><input type='hidden' "
                              f"name='name' value='{_e(n)}'><button>Remove</button></form>"
                              if edit else "") + "</li>" for n in popup)
        add = (f"<form method='post' action='{self.base}/settings/popup'>{hidden}"
               "<input type='hidden' name='op' value='add'><div class='row'>"
               "<input type='text' name='name' maxlength='80' placeholder=\"the app's MCP client name\" "
               "autocomplete='off'><button class='go'>Add</button></div></form>") if edit else ""
        out.append(self._row(
            "Apps that may show an approval popup",
            "These apps get a popup (a form in the app) asking you to approve a delete while "
            "approvals are on. Others get your phone link or the terminal command.",
            f"<ul>{items}</ul>" if popup else "<b>no apps</b>.", add,
            "claude.ai, Claude Desktop and other Anthropic apps never get a popup, whatever this "
            "list says (they don't show one)."))
        cards = sorted(approvals.card_clients(db))
        items = "".join(
            f"<li>{_e(n)}" + (f"<form method='post' action='{self.base}/settings/cards'>{hidden}"
                              f"<input type='hidden' name='op' value='remove'><input type='hidden' "
                              f"name='name' value='{_e(n)}'><button>Remove</button></form>"
                              if edit else "") + "</li>" for n in cards)
        add = (f"<form method='post' action='{self.base}/settings/cards'>{hidden}"
               "<input type='hidden' name='op' value='add'><div class='row'>"
               "<input type='text' name='name' maxlength='80' placeholder=\"the app's MCP client name\" "
               "autocomplete='off'><button class='go'>Add</button></div></form>") if edit else ""
        out.append(self._row(
            "Apps that may show review cards in the chat",
            "These apps get the in-chat cards with Approve, Reject, Keep and Remove buttons. A "
            "tap counts as yours, so only add an app that draws the cards itself (claude-ai is "
            "Claude Desktop and claude.ai). Other apps get the text and your review page link.",
            f"<ul>{items}</ul>" if cards else "<b>no apps</b>.", add,
            "Each tap carries a one-use code only the card gets; an app never decides what it "
            "queued itself on its own card."))
        no_relay = "" if relay else "Only used with the FlowState relay (not set up here)."
        out.append(self._row(
            "Phone approval links",
            "With the relay, the answer to an AI's delete request carries a flowstatemcp.com link "
            "you open on your phone to approve it.",
            f"<b>{'on' if approvals.phone_on(db) else 'off'}</b>.",
            self._buttons(f"{self.base}/settings/phone", hidden,
                          (("on", "Turn on", True), ("off", "Turn off", False)), disabled=not relay),
            no_relay, unused=not relay))
        chat = approvals.relay_chat_on(db)
        out.append(self._row(
            "Chat approvals through the relay",
            "When on, an AI connected through the relay may approve or reject review items in the "
            "chat when you ask it to. When off, you decide them on your phone or on this computer. "
            "Items only you decide stay yours either way.",
            f"<b>{'on' if chat else 'off'}</b>.",
            self._buttons(f"{self.base}/settings/relay-chat", hidden,
                          (("on", "Turn on", False), ("off", "Turn off", True)), disabled=not relay),
            no_relay or "Turning it on asks you to confirm on a second page, like "
            "<code>flowstate relay chat-approvals on</code> asks for your <code>y</code>.",
            unused=not relay))
        # -- updates
        out.append("<h3 class='group'>Updates</h3>")
        allowed = updates.self_update_allowed()
        from .selfupdate import FOLDER, OPT_OUT_FILE
        file_note = (f"A {OPT_OUT_FILE} file in the FlowState folder also keeps this folder "
                     "updated by hand (delete it yourself to allow updates). "
                     if (FOLDER / OPT_OUT_FILE).exists() else "")
        file_note += ("In a terminal: <code>flowstate update self-update on|off</code> (no word "
                      "shows the value and where it comes from).")
        out.append(self._row(
            "Updates by flowstate update",
            "Whether <code>flowstate update</code> may update FlowState on this computer when you "
            "ask for it. \"By hand\" means it refuses, and you update with git yourself.",
            f"<b>{'allowed' if allowed else 'by hand'}</b>.",
            self._buttons(f"{self.base}/settings/self-update", hidden,
                          (("on", "Allow", True), ("off", "This computer is updated by hand", False))),
            file_note))
        env_off = (os.environ.get("FLOWSTATE_UPDATE_CHECK") or "").strip().lower() in ("0", "false", "off", "no")
        out.append(self._row(
            "Daily update check",
            "Once a day FlowState asks GitHub whether a newer release exists, and tells you in the "
            "brief. It sends nothing about you or your projects.",
            f"<b>{'on' if updates.is_on() else 'off'}</b>.",
            self._buttons(f"{self.base}/settings/update-check", hidden,
                          (("on", "Turn on", True), ("off", "Turn off", False))),
            "FLOWSTATE_UPDATE_CHECK=0 in the environment keeps it off whatever you choose here."
            if env_off else ""))
        # -- read-only
        out.append("<h3 class='group'>Set in the app's config</h3><p class='note'>These come from "
                   "the environment of the app that starts FlowState (its MCP config). Shown as "
                   "this FlowState process sees them; change them in the app's config.</p>")
        out.append("<table><tr><th>Setting</th><th>What it does</th><th>Now</th></tr>")
        for name, what, default in ENV_SETTINGS:
            v = identity.clean(os.environ.get(name))
            out.append(f"<tr><td><code>{name}</code></td><td>{_e(what)}</td><td>"
                       + (f"<b>{_e(v)}</b>" if v else f"not set ({_e(default)})") + "</td></tr>")
        out.append("</table><h3 class='group'>Set in your terminal</h3><ul>")
        out.extend(f"<li>{_e(n)}: <code>{_e(c)}</code></li>" for n, c in TERMINAL_SETTINGS)
        out.append("</ul>")
        # -- recent changes
        out.append("<h3 class='group'>Recent changes</h3>")
        log = self.recent_changes()
        if not log:
            out.append("<p class='note'>No setting has been changed yet.</p>")
        else:
            out.append("<table><tr><th>When (UTC)</th><th>Setting</th><th>From</th><th>To</th>"
                       "<th>How</th><th>Machine</th></tr>")
            for r in log:
                out.append(f"<tr><td>{_e(r['changed_at'])}</td><td>{_e(self._scope_label(r['scope'], names))}</td>"
                           f"<td>{_e(r['old_mode'] or 'default')}</td><td>{_e(r['new_mode'] or 'default')}</td>"
                           f"<td>{_e(r['client'] or '-')}</td><td>{_e(r.get('machine') or '-')}</td></tr>")
            out.append("</table>")
        out.append("</section>")
        return "".join(out)

    def _own_approvals(self, pid: int) -> Optional[str]:
        conn = self._conn()
        try:
            if not approvals.available(conn):
                return None
            return approvals._get(conn, f"approvals:project:{int(pid)}")
        finally:
            conn.close()

    def render_confirm(self, csrf: str, token: str, change: tuple) -> str:
        what, scope, state = change
        if what == "relay_chat":
            title = "Turn on chat approvals through the relay?"
            text = ("This lets an AI connected through the FlowState relay approve or reject review "
                    "items in the chat when you ask it to. Deletes, turning review off while "
                    "approvals are on, and \"did you say this?\" checks stay yours alone. Only do "
                    "this if you trust every AI app you connect through the relay to pass on only "
                    "what you really said.")
        else:
            where = "the whole install" if not scope else self._name(int(scope))
            title = (f"Turn approvals off for {where}?" if state == "off" else
                     f"Remove {where}'s own approvals setting?")
            text = (("This project then follows the whole install's setting. " if state == "clear"
                     else "") +
                    "With approvals off, an AI's delete requests go through at once (after a "
                    "safety copy, and they can be undone), and an AI can turn review off when it "
                    "quotes you. Only do this if you are at this computer yourself and no AI app "
                    "asked you to.")
        f = (f"<input type='hidden' name='csrf' value='{_e(csrf)}'>"
             f"<input type='hidden' name='token' value='{_e(token)}'>")
        mins = CONFIRM_SECONDS // 60
        return (f"<!doctype html><html><head><meta charset='utf-8'><title>FlowState: confirm</title>"
                f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<style>{_CSS}</style></head><body class='confirm'><h1>FlowState review</h1>"
                f"<h2>{_e(title)}</h2><div class='set'><p>{_e(text)}</p>"
                f"<p class='note'>This confirmation works once, from this browser, within {mins} "
                f"minutes.</p>"
                f"<form method='post' action='{self.base}/settings/confirm'>{f}"
                f"<input type='hidden' name='setting' value='{_e(what)}'>"
                f"<input type='hidden' name='scope' value='{_e(scope)}'>"
                f"<input type='hidden' name='state' value='{_e(state)}'>"
                f"<button class='go'>Yes, {_e(title[0].lower() + title[1:-1])}</button></form>"
                f"<form method='post' action='{self.base}/settings/cancel'>{f}"
                f"<button>No, keep it as it is</button></form></div></body></html>")


def _cookie_values(header: str, name: str) -> list:
    """All values of cookie `name` in a Cookie header, in the order sent (a browser sends one
    per path; the most specific path first). Empty and oversized values are skipped."""
    out = []
    for part in (header or "").split(";")[:100]:
        k, sep, v = part.strip().partition("=")
        v = v.strip().strip('"')
        if sep and k.strip() == name and 0 < len(v) <= 200:
            out.append(v)
    return out


def create_app(page: Page):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
    from starlette.routing import Route

    headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                          "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
               # same-origin, not no-referrer: under no-referrer browsers send "Origin: null" on
               # the page's own form posts (checked in Edge), which the Origin check refuses;
               # same-origin still sends no referrer to any other site
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
               "Cache-Control": "no-store", "X-Frame-Options": "DENY"}
    allowed_hosts = {f"{HOST}:{page.port}", f"localhost:{page.port}"}

    def deny(code: int, text: str):
        return PlainTextResponse(text, status_code=code, headers=headers)

    def session_of(request) -> Optional[str]:
        # Every value the browser sent under this cookie's name: a stale one (a 2.4.3 page, or
        # any other web server on 127.0.0.1 can set one for path /) comes along with ours, and
        # is ignored rather than shutting the user out
        for tok in _cookie_values(request.headers.get("cookie", ""), page.cookie):
            for s in page.sessions:
                if hmac.compare_digest(tok, s):
                    page.used(s)
                    return s
        return None

    def set_cookie(resp, sess: str):
        resp.set_cookie(page.cookie, sess, httponly=True, samesite="strict", path=f"{page.base}/")
        return resp

    def host_ok(request) -> bool:
        return request.headers.get("host", "").lower() in allowed_hosts

    def page_html(request, sess: str):
        flash = page.flashes.pop(sess, "")   # only FlowState's own answer, never the address's
        if request.query_params.get("view") == "who":
            return HTMLResponse(page.render_who(page.sessions[sess], flash,
                                                dict(request.query_params)), headers=headers)
        return HTMLResponse(page.render(page.sessions[sess], flash,
                                        request.query_params.get("project", ""),
                                        request.query_params.get("status", "pending")
                                        if request.query_params.get("status") in
                                        ("pending", "approved", "rejected", "all") else "pending"),
                            headers=headers)

    async def ping(request):
        """Which database this page serves (waiting.py reuses a running page)."""
        if not host_ok(request):
            return deny(403, "This page only answers on 127.0.0.1.")
        return JSONResponse({"service": "flowstate-review", "db": db_tag(page.db_path),
                             "open_link": page.open_link}, headers=headers)

    async def index(request):
        if not host_ok(request):
            return deny(403, "This page only answers on 127.0.0.1.")
        key = request.query_params.get("key")
        if key is not None and not page.open_link:
            if not page.launch_key or not hmac.compare_digest(key, page.launch_key):
                return deny(403, "This link has expired. Run `flowstate review-page` again.")
            page.launch_key = None   # one time only
            sess = page.new_session()
            return set_cookie(RedirectResponse(f"{page.base}/", status_code=303, headers=headers),
                              sess)
        sess = session_of(request)
        if not sess and page.open_link:
            # A click on the link (often from another app or site, so a SameSite=Strict cookie
            # wouldn't survive a redirect): the page and its cookie come in this one answer.
            sess = page.new_session()
            if sess is None:
                return deny(429, LINK_BUSY)
            return set_cookie(page_html(request, sess), sess)
        if not sess:
            return deny(403, "Open the page with the link from `flowstate review-page`.")
        return page_html(request, sess)

    def action(fn, back: str = "", with_session: bool = False):
        async def handler(request):
            if not host_ok(request):
                return deny(403, "This page only answers on 127.0.0.1.")
            origin = request.headers.get("origin")
            if (origin and origin.lower() not in {f"http://{h}" for h in allowed_hosts}) \
                    or (page.open_link and not origin):   # link mode: a same-origin form post only
                return deny(403, "Cross-site request refused.")
            if request.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return deny(403, "Cross-site request refused.")
            sess = session_of(request)
            if not sess:
                return deny(403, "Not signed in to the review page.")
            form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
            if not hmac.compare_digest(form.get("csrf", ""), page.sessions[sess]):
                return deny(403, "Form expired; reload the page.")
            try:
                msg = fn(form, sess) if with_session else fn(form)
            except (paths.ConfigUnreadable, TimeoutError) as e:   # a plain sentence, no path secrets
                msg = str(e)
            except (ValueError, KeyError, sqlite3.Error, approvals.ApprovalError, OSError) as e:
                msg = f"Could not do that: {type(e).__name__}"
            if isinstance(msg, _Html):   # the settings confirm step: a page of its own
                return HTMLResponse(msg, headers=headers)
            page.flashes[sess] = msg[:300]
            return RedirectResponse(f"{page.base}/" + (f"?{urlencode({'view': back})}" if back else ""),
                                    status_code=303, headers=headers)
        return handler

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/ping", ping, methods=["GET"]),
        Route("/approve", action(page.approve), methods=["POST"]),
        Route("/reject", action(page.reject), methods=["POST"]),
        Route("/record", action(page.record), methods=["POST"]),
        Route("/mode", action(page.set_mode), methods=["POST"]),
        Route("/app", action(page.set_app), methods=["POST"]),
        Route("/undo", action(page.undo, back="who"), methods=["POST"]),
        # Settings (render_settings): change only with a launch-key session ("edit"); the
        # open-link page answers READ_ONLY; the tunnel page has none of these routes
        *[Route(f"/settings/{name}", action(fn, with_session=True), methods=["POST"])
          for name, fn in (("approvals", page.set_approvals), ("relay-chat", page.set_relay_chat),
                           ("phone", page.set_phone), ("popup", page.set_popup),
                           ("cards", page.set_cards),
                           ("self-update", page.set_self_update),
                           ("name", page.set_display_name),
                           ("update-check", page.set_update_check),
                           ("confirm", page.confirm), ("cancel", page.cancel))],
    ]
    if not page.base:
        return Starlette(routes=routes)
    from starlette.routing import Mount
    ping_route = [r for r in routes if r.path == "/ping"]
    return Starlette(routes=ping_route + [Mount(page.base, routes=routes)])


def serve(db_path: Path, port: int = DEFAULT_PORT, open_browser: bool = True,
          print_url: bool = False) -> None:
    import uvicorn
    from . import server as S
    S.init_backend(db_path)    # approve uses the same code as the MCP tools
    if S.backend.error:
        raise SystemExit(f"FlowState database unavailable: {S.backend.error}")
    from .waiting import _bind, _ping
    try:
        sock = _bind(port)
    except OSError as e:
        raise SystemExit(f"Couldn't start the review page on 127.0.0.1: {e}") from e
    if sock.getsockname()[1] != port and _ping(port, db_path):
        # FlowState's link page has the usual port. That page is read-only (any program here
        # can open it), so this one, with Settings you can change, takes a free port.
        print(f"The read-only review link page is on port {port}; this page, with Settings you "
              f"can change, uses port {sock.getsockname()[1]}.", flush=True)
    port = sock.getsockname()[1]
    page = Page(db_path, port, settings="edit", secret_path=True)
    app = create_app(page)
    url = page.launch_url()
    # Not the address itself: the page lives under a secret path (http://127.0.0.1:<port>/
    # answers 404), and only the one-time link below opens it
    print(f"FlowState review page on {HOST}, port {port} (this computer only). Ctrl+C stops it.",
          flush=True)
    if print_url or not open_browser:
        # --no-browser: the one-time link is the only way in, so it is shown, on the user's
        # terminal only, so an agent reading our output can't use it
        from .cli_review import open_terminal
        tty = open_terminal()
        if tty is None:
            raise SystemExit(f"{'--print-url' if print_url else '--no-browser'} shows the one-time "
                             "link on your terminal, and there is none here. Run it in a terminal "
                             "window, or leave it out to open the browser.")
        tty.tell(f"One-time link: {url}")
        tty.close()
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        if not print_url:
            print("Opening it in your browser (use --print-url if it doesn't open).", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[sock])
