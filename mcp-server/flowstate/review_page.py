"""A small review page for the user, on this computer only (v2 Phase D).

    flowstate review-page [--db FILE] [--port 8770] [--no-browser] [--print-url]

Lists the review queue with Approve / Edit / Reject, sets the review mode, pauses or
resumes apps, and shows "Who did what" (app, model, machine and identity source of every
recorded change, with Undo where the edit history allows it). Everything done here counts as the user acting (it is logged as
"review-page").

Safety:
  * its own process, bound to 127.0.0.1 only; never mounted on the HTTP or public listener
  * opening it needs a one-time key (in the URL the command opens in your browser); the
    key is swapped for an HttpOnly, SameSite=Strict cookie and then stops working
  * every form carries a CSRF token; the Host header must be 127.0.0.1/localhost (blocks
    DNS-rebinding pages); strict Content-Security-Policy, no caching, no scripts
"""

import hmac
import html
import json
import secrets
import sqlite3
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

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
.row{display:flex;gap:1rem}.row>*{flex:1}"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


class Page:
    """State for one review-page process."""

    def __init__(self, db_path: Path, port: int = DEFAULT_PORT):
        self.db_path = Path(db_path)
        self.port = port
        self.launch_key: Optional[str] = secrets.token_urlsafe(32)
        self.sessions: dict[str, str] = {}   # cookie value -> csrf token

    def launch_url(self) -> str:
        return f"http://{HOST}:{self.port}/?{urlencode({'key': self.launch_key})}"

    # ------------------------------------------------------------ data
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
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
        args = {"item_id": item["id"], "user_request": USER_WORDS.format(what="Approve"),
                **{k: form[k] for k in ("summary", "details", "move_to") if form.get(k, "").strip()}}
        if args.get("summary") == item["summary"]:
            args.pop("summary")
        if args.get("details") == (item["details"] or ""):
            args.pop("details")
        res = S._review_decide("review_approve", item["project_id"], args, CLIENT)
        return res.get("message") if isinstance(res, dict) and "error" in res else \
            f"Approved #{item['id']}."

    def reject(self, form: dict) -> str:
        from . import server as S
        item = review.get_item(self.db_path, int(form["item_id"]))
        if not item:
            return "That item no longer exists."
        reason = (form.get("reason") or "").strip() or "rejected on the review page"
        res = S._review_decide("review_reject", item["project_id"],
                               {"item_id": item["id"], "reason": reason,
                                "user_request": USER_WORDS.format(what="Reject")}, CLIENT)
        return res.get("message") if isinstance(res, dict) and "error" in res else \
            f"Rejected #{item['id']}."

    def set_mode(self, form: dict) -> str:
        pid = int(form["project_id"]) if form.get("project_id") else None
        try:
            r = review_mode.set_mode(self.db_path, form.get("mode"),
                                     [k for k in review_mode.KINDS if form.get(f"kind_{k}")], pid,
                                     USER_WORDS.format(what="set review mode"), CLIENT, CLIENT,
                                     clear=bool(form.get("clear")))
        except review_mode.ModeError as e:
            return e.payload["message"]
        return f"Review mode for {r['scope']}: {r['now']['mode']} ({r['text']})."

    def set_app(self, form: dict) -> str:
        app = form.get("app", "")
        paused = form.get("paused") == "1"
        activity.set_paused(self.db_path, app, paused, CLIENT)
        return f"{app}: {'paused' if paused else 'resumed'}."

    def undo(self, form: dict) -> str:
        """Undo from "Who did what": the same code as history(action="undo")."""
        from . import server as S
        if form.get("record_type") not in history.TABLES:
            return "Unknown record type."
        res = S._history(int(form["project_id"]),
                         {"action": "undo", "record_type": form["record_type"],
                          "record_id": int(form["record_id"])}, CLIENT)
        if isinstance(res, dict) and "error" in res:
            return res.get("message") or res["error"]
        return f"Undid the last change to {form['record_type']} #{form['record_id']}."

    # ------------------------------------------------------------ html
    def _head(self, flash: str, view: str) -> list:
        out = [f"<!doctype html><html><head><meta charset='utf-8'><title>FlowState review</title>"
               f"<style>{_CSS}</style></head><body><h1>FlowState review</h1>"
               f"<p class='meta'><a href='/'>Review queue</a> · <a href='/?view=who'>Who did what</a>"
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
        out = self._head(flash, "who")
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
                undo = (f'<form method="post" action="/undo">{hidden}'
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
                nav.append(f"<a href='/?{_e(urlencode({'view': 'who', **keep, 'page': res['page'] - 1}))}'>Newer</a>")
            nav.append(f"page {res['page']} of {res['pages']}")
            if res["page"] < res["pages"]:
                nav.append(f"<a href='/?{_e(urlencode({'view': 'who', **keep, 'page': res['page'] + 1}))}'>Older</a>")
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
        out = self._head(flash, "queue")
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
                    move = ('<label>Move to (empty = the suggested project)<select name="move_to">'
                            f'<option value=""></option>{targets}</select></label>')
                out.append(f"""<form method="post" action="/approve">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<textarea name="summary" rows="2">{_e(it['summary'])}</textarea><textarea name="details" rows="2" placeholder="details">{_e(it['details'])}</textarea>{move}
<button>Approve (with any edits)</button></form>
<form method="post" action="/reject">{hidden}<input type="hidden" name="item_id" value="{it['id']}">
<input type="text" name="reason" placeholder="Reason for rejecting"><button>Reject</button></form>""")
            else:
                out.append(f"<p>{_e(it['summary'])}</p>"
                           + (f"<p class='meta'>Reason: {_e(it['reject_reason'])}</p>" if it.get("reject_reason") else ""))
            out.append("</div>")
        # review mode
        m = review_mode.get(self.db_path, None)
        kinds = "".join(f'<label><input type="checkbox" name="kind_{k}"{" checked" if k in m["kinds"] else ""}> {k}</label> '
                        for k in review_mode.KINDS)
        out.append(f"""<h2>Review mode</h2><p>Whole install: <b>{_e(m['mode'])}</b>, {_e(review_mode.describe(m))}.</p>
<form method="post" action="/mode">{hidden}<div class="row"><select name="mode">{''.join(f'<option{" selected" if x == m["mode"] else ""}>{x}</option>' for x in review_mode.MODES)}</select>
<select name="project_id"><option value="">Whole install</option>{opts.replace(' selected', '')}</select></div>
<p>For "types": {kinds}</p><label><input type="checkbox" name="clear"> Remove this project's own setting</label><br><button>Set review mode</button></form>""")
        # apps
        out.append("<h2>Apps</h2><table><tr><th>App</th><th>Last seen</th><th>Calls</th><th>Writes</th><th></th></tr>")
        for a in activity.apps(self.db_path):
            out.append(f"""<tr><td>{_e(a['app'])}{' <b>(paused)</b>' if a['paused'] else ''}</td><td>{_e(a['last_seen'] or '-')}</td>
<td>{a['calls']}</td><td>{a['writes'] or 0}</td><td><form method="post" action="/app">{hidden}<input type="hidden" name="app" value="{_e(a['app'])}">
<input type="hidden" name="paused" value="{'0' if a['paused'] else '1'}"><button>{'Resume' if a['paused'] else 'Pause writes'}</button></form></td></tr>""")
        out.append("</table><h2>Recent activity</h2><table><tr><th>When (UTC)</th><th>App</th><th>Tool</th><th>Project</th><th>Result</th></tr>")
        for r in activity.recent(self.db_path, None, 30):
            out.append(f"<tr><td>{_e(r['at'])}</td><td>{_e(r['app'])}</td><td>{_e(r['tool'])}{('/' + _e(r['action'])) if r['action'] else ''}</td>"
                       f"<td>{_e(r['project'] or '')}</td><td>{'ok' if r['ok'] else 'error ' + _e(r['error'])}</td></tr>")
        out.append("</table></body></html>")
        return "".join(out)


def create_app(page: Page):
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
    from starlette.routing import Route

    headers = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                          "form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
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

    async def index(request):
        if not host_ok(request):
            return deny(403, "This page only answers on 127.0.0.1.")
        key = request.query_params.get("key")
        if key is not None:
            if not page.launch_key or not hmac.compare_digest(key, page.launch_key):
                return deny(403, "This link has expired. Run `flowstate review-page` again.")
            page.launch_key = None   # one time only
            sess = secrets.token_urlsafe(32)
            page.sessions[sess] = secrets.token_urlsafe(24)
            resp = RedirectResponse("/", status_code=303, headers=headers)
            resp.set_cookie(COOKIE, sess, httponly=True, samesite="strict", path="/")
            return resp
        sess = session_of(request)
        if not sess:
            return deny(403, "Open the page with the link from `flowstate review-page`.")
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

    def action(fn, back: str = ""):
        async def handler(request):
            if not host_ok(request):
                return deny(403, "This page only answers on 127.0.0.1.")
            origin = request.headers.get("origin")
            if origin and origin.lower() not in {f"http://{h}" for h in allowed_hosts}:
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
    page = Page(db_path, port)
    app = create_app(page)
    url = page.launch_url()
    print(f"FlowState review page on http://{HOST}:{port}/ (this computer only). Ctrl+C stops it.",
          flush=True)
    if print_url:
        print(f"One-time link: {url}", flush=True)
    if open_browser:
        import threading
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        if not print_url:
            print("Opening it in your browser (use --print-url if it doesn't open).", flush=True)
    uvicorn.run(app, host=HOST, port=port, log_level="warning", access_log=False)
