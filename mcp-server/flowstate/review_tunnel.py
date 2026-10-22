"""The owner's review page on the public (tunnel) listener, for users who reach FlowState
through their own tunnel (docs/REMOTE-ACCESS.md) instead of the relay.

    https://<your tunnel>/review/tunnel

Only on the listener started with --public-url (http_app.py mounts it there and nowhere else:
the relay has the signed-in phone list, review_phone.py; this computer has
`flowstate review-page` and its desktop shortcut). It is the same page as review_page.py
(Page: the queue with Approve / Edit / Reject, the review mode, apps, "Who did what" with
Undo), so it decides everything the local page can, including user-only items (deletes,
review off while approvals are on, "did you say this yourself?"): typing the owner
passphrase shows it's the user. Decisions are labelled "tunnel page" and every one is
logged in approval_log (approvals.LOG_EVERY_DECISION).

  GET  /review/tunnel            signed in: the page; else the sign-in form
  POST /review/tunnel/sign-in    the owner passphrase (remote_auth.AuthStore, the OAuth
                                 consent page's): its lockout (5 wrong in 15 minutes) plus
                                 SIGNIN_PER_IP tries per 15 minutes per address
  POST /review/tunnel/sign-out
  POST /review/tunnel/{approve,reject,mode,app,undo}   the page's forms
  GET  /review/tunnel/page.css

Session: a random cookie __Host-fs_review_tunnel (Secure, HttpOnly, SameSite=Strict, Path=/),
kept here in memory as a hash with its CSRF token; it ends after IDLE_SECONDS without use or
MAX_SECONDS in all, on sign-out, and when FlowState restarts. Every POST needs the form's CSRF
token and an Origin equal to the public URL's (a missing or "null" Origin is refused). Strict
headers: CSP without inline script or style, frame-ancestors 'none', X-Frame-Options DENY,
no-store, no referrer.

No agent token opens it: the owner token, per-client tokens, OAuth access tokens and LAN app
keys are all ignored here (remote_auth.Gate checks bearer tokens on the MCP path only and sets
no identity on other paths; this page reads only its own cookie). Nothing here logs the
passphrase, the cookie or the CSRF token.
"""

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from . import review_page

log = logging.getLogger("flowstate.review_tunnel")

PATH = "/review/tunnel"
COOKIE = "__Host-fs_review_tunnel"
CLIENT = "tunnel page"
USER_WORDS = "The user did this on the FlowState tunnel review page ({what})."
IDLE_SECONDS = 15 * 60
MAX_SECONDS = 60 * 60
MAX_SESSIONS = 20
SIGNIN_PER_IP, SIGNIN_WINDOW = 10, 15 * 60
FORM_SECONDS = 15 * 60       # a sign-in form must be sent within this
_clock = time.time

# The public URL of this process's tunnel listener, once its page is mounted (http_app.py).
# approvals.ask_line names the page to apps that came in through that listener.
PUBLIC_URL: Optional[str] = None

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'self'; img-src 'self'; "
                               "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# review_page's look, plus what a phone needs: no sideways scroll at 390 px, big tap targets
CSS = review_page._CSS + """
html{-webkit-text-size-adjust:100%}body{overflow-wrap:anywhere}
button{min-height:44px;padding:.6rem 1.1rem;font-size:1rem}
input,select,textarea{min-height:44px;font-size:1rem;padding:.4rem;box-sizing:border-box;max-width:100%}
input[type=checkbox]{min-height:0;width:1.3rem;height:1.3rem;vertical-align:middle}
.row{flex-wrap:wrap}.row>*{min-width:8rem}
table{display:block;max-width:100%;overflow-x:auto}
.signin{max-width:26rem}.signin input{width:100%}
"""


def page_url() -> Optional[str]:
    """The tunnel review page's address, when the app asking came in through this process's
    public listener (so the user has one, and it's reachable the way the app reached us)."""
    if not PUBLIC_URL:
        return None
    from .popup import listener
    return PUBLIC_URL + PATH if listener() == "public" else None


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


class Sessions:
    """Signed-in browsers: hash of the cookie -> CSRF token, start, last use, a flash line."""

    def __init__(self):
        self._lock = threading.Lock()
        self._s: dict = {}
        self._forms: dict = {}      # sign-in form tokens (hashed) -> made at
        self._tries: dict = {}      # address -> sign-in attempt times

    def _live(self, s: dict, now: float) -> bool:
        return now - s["last"] < IDLE_SECONDS and now - s["start"] < MAX_SECONDS

    def start(self) -> str:
        cookie, now = secrets.token_urlsafe(32), _clock()
        with self._lock:
            self._s = {k: v for k, v in self._s.items() if self._live(v, now)}
            while len(self._s) >= MAX_SESSIONS:
                self._s.pop(min(self._s, key=lambda k: self._s[k]["last"]))
            self._s[_sha(cookie)] = {"csrf": secrets.token_urlsafe(24), "start": now,
                                     "last": now, "flash": ""}
        return cookie

    def get(self, cookie) -> Optional[dict]:
        if not isinstance(cookie, str) or not cookie:
            return None
        now = _clock()
        with self._lock:
            s = self._s.get(_sha(cookie))
            if s is None:
                return None
            if not self._live(s, now):
                self._s.pop(_sha(cookie), None)
                return None
            s["last"] = now
            return s

    def end(self, cookie) -> None:
        with self._lock:
            self._s.pop(_sha(cookie or ""), None)

    def form_token(self) -> str:
        tok, now = secrets.token_urlsafe(24), _clock()
        with self._lock:
            self._forms = {k: t for k, t in self._forms.items() if now - t < FORM_SECONDS}
            if len(self._forms) >= 500:
                self._forms.pop(min(self._forms, key=self._forms.get))
            self._forms[_sha(tok)] = now
        return tok

    def use_form_token(self, tok) -> bool:
        if not isinstance(tok, str) or not tok:
            return False
        with self._lock:
            made = self._forms.pop(_sha(tok), None)
        return made is not None and _clock() - made < FORM_SECONDS

    def try_allowed(self, ip: str) -> bool:
        """At most SIGNIN_PER_IP passphrase tries per SIGNIN_WINDOW from one address."""
        now = _clock()
        with self._lock:
            if len(self._tries) > 10000:
                self._tries = {k: v for k, v in self._tries.items() if v and now - v[-1] < SIGNIN_WINDOW}
            tries = [t for t in self._tries.get(ip, []) if now - t < SIGNIN_WINDOW]
            ok = len(tries) < SIGNIN_PER_IP
            if ok:
                tries.append(now)
            self._tries[ip] = tries
            return ok


def _note(title: str, text: str, form: str = "") -> str:
    e = review_page._e
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<meta name='referrer' content='no-referrer'><title>FlowState: {e(title)}</title>"
            f"<link rel='stylesheet' href='{PATH}/page.css'></head><body><h1>{e(title)}</h1>"
            f"<p>{text}</p>{form}</body></html>")


def signin_html(form_token: str, error: str = "") -> str:
    e = review_page._e
    err = f"<p class='err'>{e(error)}</p>" if error else ""
    form = (f"{err}<form method='post' action='{PATH}/sign-in' class='signin'>"
            f"<input type='hidden' name='csrf' value='{e(form_token)}'>"
            f"<label>Owner passphrase<input type='password' name='passphrase' "
            f"autocomplete='current-password' required></label><button>Sign in</button></form>"
            f"<p class='meta'>The passphrase you set with <code>flowstate remote passphrase</code> "
            f"on the computer where FlowState runs. AI apps' tokens and keys don't open this page.</p>")
    return _note("FlowState review", "Sign in to see and decide what's waiting for you.", form)


NO_PASSPHRASE = _note("FlowState review", "No owner passphrase is set on this FlowState. On the "
                      "computer where FlowState runs, run <code>flowstate remote passphrase</code>, "
                      "then open this page again.")


def routes(db_path_fn, store, public_url: str):
    """Starlette routes for the public listener. `store`: the remote_auth.AuthStore holding the
    owner passphrase; `public_url`: the tunnel's https URL (the only Origin POSTs may have)."""
    from starlette.responses import HTMLResponse, RedirectResponse, Response
    from starlette.routing import Route
    from . import identity
    from .remote_auth import CLIENT_IP_KEY

    global PUBLIC_URL
    PUBLIC_URL = public_url.rstrip("/")
    u = urlparse(PUBLIC_URL)
    origin = f"{u.scheme}://{u.netloc}".lower()
    sessions = Sessions()
    page = review_page.Page(db_path_fn, port=0, base=PATH, client=CLIENT, user_words=USER_WORDS,
                            stylesheet=f"{PATH}/page.css", sign_out=f"{PATH}/sign-out")
    acts = {"approve": page.approve, "reject": page.reject, "mode": page.set_mode,
            "app": page.set_app, "undo": page.undo}

    def html(body: str, status: int = 200, extra=None):
        return HTMLResponse(body, status_code=status, headers={**HEADERS, **(extra or {})})

    def deny(status: int, text: str):
        return html(_note("FlowState review", review_page._e(text)), status)

    def back(cookie: Optional[str] = None, clear: bool = False, view: str = ""):
        r = RedirectResponse(PATH + (f"/?view={view}" if view else "/"), status_code=303,
                             headers=HEADERS)
        if cookie is not None:
            r.set_cookie(COOKIE, cookie, max_age=MAX_SECONDS, path="/", secure=True,
                         httponly=True, samesite="strict")
        if clear:
            r.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return r

    def same_origin(request) -> bool:
        o = (request.headers.get("origin") or "").strip().lower()
        if not o or o == "null" or not hmac.compare_digest(o, origin):
            return False
        return request.headers.get("sec-fetch-site", "same-origin") in ("same-origin", "none")

    async def form_of(request) -> dict:
        return {k: v for k, v in (await request.form()).items() if isinstance(v, str)}

    async def css(request):
        return Response(CSS, media_type="text/css", headers=HEADERS)

    async def index(request):
        s = sessions.get(request.cookies.get(COOKIE))
        if s is None:
            has = await asyncio.to_thread(store.has_passphrase)
            if not has:
                return html(NO_PASSPHRASE, 503)
            return html(signin_html(sessions.form_token()))
        flash, s["flash"] = s["flash"], ""
        q = request.query_params
        if q.get("view") == "who":
            body = await asyncio.to_thread(page.render_who, s["csrf"], flash, dict(q))
        else:
            status = q.get("status") if q.get("status") in ("pending", "approved", "rejected", "all") \
                else "pending"
            body = await asyncio.to_thread(page.render, s["csrf"], flash, q.get("project", ""), status)
        return html(body)

    async def sign_in(request):
        if not same_origin(request):
            return deny(403, "Cross-site request refused.")
        form = await form_of(request)
        if not sessions.use_form_token(form.get("csrf")):
            return html(signin_html(sessions.form_token(), "The form expired; try again."), 403)
        if not await asyncio.to_thread(store.has_passphrase):
            return html(NO_PASSPHRASE, 503)
        ip = request.scope.get(CLIENT_IP_KEY) or (request.client.host if request.client else "?")
        locked = await asyncio.to_thread(store.locked_for)
        if locked or not sessions.try_allowed(ip):
            wait = max(1, (locked or SIGNIN_WINDOW) // 60)
            return html(signin_html(sessions.form_token(), f"Too many tries. Try again in about "
                                                            f"{wait} minute(s)."), 429,
                        {"Retry-After": str(locked or SIGNIN_WINDOW)})
        ok = await asyncio.to_thread(store.check_passphrase, form.get("passphrase", ""))
        if not ok:
            log.warning("wrong owner passphrase on the tunnel review page")
            return html(signin_html(sessions.form_token(), "Wrong passphrase."), 403)
        log.info("owner signed in to the tunnel review page")
        return back(cookie=sessions.start())

    async def sign_out(request):
        if not same_origin(request):
            return deny(403, "Cross-site request refused.")
        cookie = request.cookies.get(COOKIE)
        s = sessions.get(cookie)
        form = await form_of(request)
        if s is None or not hmac.compare_digest(form.get("csrf", ""), s["csrf"]):
            return deny(403, "Not signed in, or the form expired; reload the page.")
        sessions.end(cookie)
        return back(clear=True)

    def action(name: str):
        fn = acts[name]

        def as_user(form: dict) -> str:
            tok = identity.set_current(None)   # the user, never an agent's identity
            try:
                return fn(form)
            finally:
                identity.reset(tok)

        async def handler(request):
            if not same_origin(request):
                return deny(403, "Cross-site request refused.")
            s = sessions.get(request.cookies.get(COOKIE))
            if s is None:
                return deny(403, "Not signed in. Open the page again and sign in.")
            form = await form_of(request)
            if not hmac.compare_digest(form.get("csrf", ""), s["csrf"]):
                return deny(403, "The form expired; reload the page.")
            try:
                msg = await asyncio.to_thread(as_user, form)
            except (ValueError, KeyError, sqlite3.Error) as e:
                msg = f"Could not do that: {type(e).__name__}"
            s["flash"] = (msg or "")[:300]
            return back(view="who" if name == "undo" else "")
        return handler

    return [Route(PATH, index, methods=["GET"]),
            Route(PATH + "/", index, methods=["GET"]),
            Route(PATH + "/page.css", css, methods=["GET"]),
            Route(PATH + "/sign-in", sign_in, methods=["POST"]),
            Route(PATH + "/sign-out", sign_out, methods=["POST"])] + \
        [Route(f"{PATH}/{n}", action(n), methods=["POST"]) for n in acts]
