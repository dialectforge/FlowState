"""A small review page for the user, on this computer only (v2 Phase D).

    flowstate review-page [--db FILE] [--port 8770] [--no-browser] [--print-url]

Lists the review queue with Approve / Edit / Reject, sets the review mode, pauses or
resumes apps, and shows "Who did what" (app, model, machine and identity source of every
recorded change, with Undo where the edit history allows it). Everything done here counts as the user acting (it is logged as
"review-page").

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
import secrets
import sqlite3
from pathlib import Path
from typing import Callable, Optional, Union
from urllib.parse import urlencode

from . import dbconn
from . import activity, history, review, review_mode, who

HOST = "127.0.0.1"
DEFAULT_PORT = 8770   # 8766 = LAN server, 8767 = public listener
COOKIE = "fs_review"
CLIENT = "review-page"
USER_WORDS = "The user did this on the FlowState review page ({what})."

_CSS = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:1.5rem auto;padding:0 1rem;color:#222}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid #ddd}
.item{border:1px solid #ccc;border-radius:8px;padding:.7rem 1rem;margin:.8rem 0}
.meta{color:#666;font-size:.85rem}textarea,input[type=text],select{width:100%;box-sizing:border-box;margin:.2rem 0}
button{padding:.35rem .9rem;margin:.3rem .4rem 0 0}.ok{color:#0a6b0a}.err{color:#b00020}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #eee;padding:.25rem;text-align:left;font-size:.9rem}
.row{display:flex;gap:1rem}.row>*{flex:1}
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
@media (min-width:800px){.sides{flex-direction:row;align-items:stretch}.side{flex:1}}"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


MAX_SESSIONS = 20   # open-link mode: the oldest browser session goes first


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
                 sign_out: Optional[str] = None):
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

    @property
    def db_path(self) -> Path:
        return Path(self._db()) if callable(self._db) else self._db

    def launch_url(self) -> str:
        if self.open_link:
            return f"http://{HOST}:{self.port}/"
        return f"http://{HOST}:{self.port}/?{urlencode({'key': self.launch_key})}"

    def new_session(self) -> str:
        sess = secrets.token_urlsafe(32)
        self.sessions[sess] = secrets.token_urlsafe(24)
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.pop(next(iter(self.sessions)))
        return sess

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

    def set_mode(self, form: dict) -> str:
        pid = int(form["project_id"]) if form.get("project_id") else None
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
        activity.set_paused(self.db_path, app, paused, self.client)
        return f"{app}: {'paused' if paused else 'resumed'}."

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
                 else f"<style>{_CSS}</style>")
        out = [f"<!doctype html><html><head><meta charset='utf-8'><title>FlowState review</title>"
               f"{style}</head><body><h1>FlowState review</h1>"
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
            out.append(f"<tr><td>{_e(r['at'])}</td><td>{_e(r['app'] or '-')}</td>"
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
            out.append(f"""<div class="item"><div class="meta">#{it['id']} · {_e(it['kind'])} · {_e(it['project'])} · from {_e(it['source'])} · {_e(it['created_at'])} · {_e(it['status'])}</div>""")
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
                    out.append(f"""<p><strong>{review.KEEP_QUESTION}</strong> {review.KEEP_HOW} You can edit it first.</p>
<form method="post" action="{self.base}/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<textarea name="summary" rows="2">{_e(review.strip_tag(it['summary']))}</textarea><textarea name="details" rows="2" placeholder="details">{_e(it['details'])}</textarea>
<button>{review.KEEP} (with any edits)</button></form>""")
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
                if it.get("reject_reason"):
                    out.append(f"<p class='meta'>Reason: {_e(it['reject_reason'])}</p>")
            out.append("</div>")
        # review mode
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
            out.append(f"""<tr><td>{_e(a['app'])}{' <b>(paused)</b>' if a['paused'] else ''}</td><td>{_e(a['last_seen'] or '-')}</td>
<td>{a['calls']}</td><td>{a['writes'] or 0}</td><td><form method="post" action="{self.base}/app">{hidden}<input type="hidden" name="app" value="{_e(a['app'])}">
<input type="hidden" name="paused" value="{'0' if a['paused'] else '1'}"><button>{'Resume' if a['paused'] else 'Pause writes'}</button></form></td></tr>""")
        out.append("</table><h2>Recent activity</h2><table><tr><th>When (UTC)</th><th>App</th><th>Tool</th><th>Project</th><th>Result</th></tr>")
        for r in activity.recent(self.db_path, None, 30):
            out.append(f"<tr><td>{_e(r['at'])}</td><td>{_e(r['app'])}</td><td>{_e(r['tool'])}{('/' + _e(r['action'])) if r['action'] else ''}</td>"
                       f"<td>{_e(r['project'] or '')}</td><td>{'ok' if r['ok'] else 'error ' + _e(r['error'])}</td></tr>")
        out.append("</table></body></html>")
        return "".join(out)


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
        tok = request.cookies.get(COOKIE, "")
        for s in page.sessions:
            if tok and hmac.compare_digest(tok, s):
                return s
        return None

    def host_ok(request) -> bool:
        return request.headers.get("host", "").lower() in allowed_hosts

    def page_html(request, sess: str):
        if request.query_params.get("view") == "who":
            return HTMLResponse(page.render_who(page.sessions[sess],
                                                request.query_params.get("flash", ""),
                                                dict(request.query_params)), headers=headers)
        return HTMLResponse(page.render(page.sessions[sess], request.query_params.get("flash", ""),
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
            resp = RedirectResponse("/", status_code=303, headers=headers)
            resp.set_cookie(COOKIE, sess, httponly=True, samesite="strict", path="/")
            return resp
        sess = session_of(request)
        if not sess and page.open_link:
            # A click on the link (often from another app or site, so a SameSite=Strict cookie
            # wouldn't survive a redirect): the page and its cookie come in this one answer.
            sess = page.new_session()
            resp = page_html(request, sess)
            resp.set_cookie(COOKIE, sess, httponly=True, samesite="strict", path="/")
            return resp
        if not sess:
            return deny(403, "Open the page with the link from `flowstate review-page`.")
        return page_html(request, sess)

    def action(fn, back: str = ""):
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
                msg = fn(form)
            except (ValueError, KeyError, sqlite3.Error) as e:
                msg = f"Could not do that: {type(e).__name__}"
            return RedirectResponse("/?" + urlencode({**({"view": back} if back else {}),
                                                       "flash": msg[:300]}),
                                    status_code=303, headers=headers)
        return handler

    return Starlette(routes=[
        Route("/", index, methods=["GET"]),
        Route("/ping", ping, methods=["GET"]),
        Route("/approve", action(page.approve), methods=["POST"]),
        Route("/reject", action(page.reject), methods=["POST"]),
        Route("/mode", action(page.set_mode), methods=["POST"]),
        Route("/app", action(page.set_app), methods=["POST"]),
        Route("/undo", action(page.undo, back="who"), methods=["POST"]),
    ])


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
        # the shortcut, while a FlowState here already serves this database's linked page
        sock.close()
        print(f"FlowState's review page is already open at http://{HOST}:{port}/ (this computer only).",
              flush=True)
        if open_browser:
            import webbrowser
            webbrowser.open(f"http://{HOST}:{port}/")
        return
    port = sock.getsockname()[1]
    page = Page(db_path, port)
    app = create_app(page)
    url = page.launch_url()
    print(f"FlowState review page on http://{HOST}:{port}/ (this computer only). Ctrl+C stops it.",
          flush=True)
    if print_url:   # on the user's terminal only, so an agent reading our output can't use it
        from .cli_review import open_terminal
        tty = open_terminal()
        if tty is None:
            raise SystemExit("--print-url shows the one-time link on your terminal, and there is "
                             "none here. Run it in a terminal window, or leave it out to open the browser.")
        tty.tell(f"One-time link: {url}")
        tty.close()
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        if not print_url:
            print("Opening it in your browser (use --print-url if it doesn't open).", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False)).run(sockets=[sock])
