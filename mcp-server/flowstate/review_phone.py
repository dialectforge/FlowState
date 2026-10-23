"""The phone review list (waiting.py, way 3), served by FlowState's HTTP app, which the relay
agent reaches at https://<handle>.fsmcp.com (TLS ends on this computer).

The link FlowState gives is the bookmarkable https://flowstatemcp.com/review/<handle>
(approvals.review_link; no code in it). The site signs the user in, checks the device is on
their account and sends the browser (303) to /review/start?t=<ticket> here. The ticket is the
same ES256 grant as the approve page, with action "start_review", item 0, the site user's id
in `sub`, a fresh nonce and a 5-minute life (approvals.verify_start_ticket). With a good
ticket this device makes a fresh one-use code and sends the browser (303) back to
https://flowstatemcp.com/review/<handle>?n=<code>, with the handle from this device's
relay.json, never from the request. There the user taps Continue, and only then does the
site sign a grant and send the browser (303) to /review#g=<grant>: action "open_review",
item 0 (approvals.verify_review_grant). The fragment never reaches a server or a log.

  GET  /review/start     ?t=<ticket> from the site (no other sign-in here). No, bad, expired
                         or already used ticket: a plain page (400 malformed, else 403) saying
                         to open the "Waiting for you" link again; no code, nothing counted,
                         the ticket never echoed or logged. Good ticket: a 15-minute code, at
                         most 10 open, 30 an hour per site user and 120 for everyone (then
                         429); each ticket makes one code. Phone review off or unavailable
                         (checked first): a plain page

Compatibility. Tickets came with flowstatemcp.com's matching change, which must be deployed
first: a site without it sends no ticket and this device refuses with the plain page (phone
review is unavailable until the site is updated). Older devices ignore ?t= (their
/review/start reads no query) and work as before. Chat links with 7-day codes and the
per-item /approve links don't use /review/start and are unchanged.
  GET  /review           an empty shell (no data); its script reads #g=, removes it from the
                         address bar at once (history.replaceState) and posts it
  POST /review/open      checks the grant and uses up its one-use code; starts a session:
                         cookie fs_review_phone (HttpOnly, Secure, SameSite=Strict, 15 min,
                         path /review) plus a page token for the body; answers with the list
  POST /review/decide    {token, item_id, decision, confirm_name?, reason?}: the session cookie
                         and its token; Approve or Reject through the user-only path
                         (popup.as_user, label "phone (<handle>)"), logged in approval_log

Same headers as /approve: strict CSP (no inline script or style), frame-ancestors 'none',
X-Frame-Options DENY, no-store, no referrer. POSTs must be JSON, from this origin.
"""

import asyncio
import hashlib
import hmac
import json
import secrets
import threading
import time

from . import approvals
from .approve_page import CSS, HEADERS, _same_origin

COOKIE = "fs_review_phone"
SESSION_SECONDS = 15 * 60
LIST_MAX = 50
_clock = time.time

SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FlowState: waiting for you</title>
<link rel="stylesheet" href="/review/assets/review.css">
<script src="/review/assets/review.js" defer></script>
</head><body><main class="wrap">
<p class="brand">FlowState</p>
<h1 id="title">Opening your list...</h1>
<p id="status" class="muted"></p>
<noscript><p class="muted">This page needs JavaScript to read your link.</p></noscript>
<section id="list"></section>
<p class="faint small">Your FlowState, on your own computer, shows this page. flowstatemcp.com
only confirmed it's you; it never sees what's in it.</p>
</main></body></html>
"""

EXTRA_CSS = """.card{border-top:1px solid var(--line);padding:18px 0 6px}
.card .eyebrow{margin-top:0}.card .actions{margin-top:14px}
.done{color:var(--accent);font-size:15px}
.sides{display:flex;flex-direction:column;gap:10px}
.side{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fff}
.mark{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0}
.who,.ev{color:var(--muted);font-size:14px;margin:4px 0}
.claim{margin:6px 0;overflow-wrap:anywhere}
.actions.stack{flex-direction:column}
.actions.stack .btn{min-height:44px;width:100%}
.ainote{color:var(--muted)}"""

JS = r"""(function () {
  "use strict";
  var m = /(?:^#|&)g=([A-Za-z0-9_.-]+)/.exec(location.hash || "");
  var grant = m ? m[1] : null;
  history.replaceState(null, "", location.pathname);   // the grant leaves the address bar now
  var token = null;
  function $(id) { return document.getElementById(id); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text) { e.textContent = text; }
    return e;
  }
  function say(title, text, bad) {
    $("title").textContent = title;
    $("status").textContent = text || "";
    $("status").className = bad ? "err" : "muted";
  }
  function post(path, body) {
    return fetch(path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, j: j}; });
    });
  }
  function show(items, total) {
    var list = $("list");
    while (list.firstChild) { list.removeChild(list.firstChild); }
    total = total || items.length;
    say(items.length ? "Waiting for you (" + total + ")" : "Nothing is waiting for you",
        total > items.length ? "Showing the oldest " + items.length + "; decide some to see more." : "");
    items.forEach(function (it) {
      var card = el("div", "card");
      card.appendChild(el("p", "eyebrow", it.project + " · " + it.kind_label + " #" + it.id));
      if (it.card && it.card.sides) {
        card.appendChild(el("p", "", it.card.headline || it.question));
        var sides = el("div", "sides");
        it.card.sides.forEach(function (s) {
          var box = el("div", "side");
          box.appendChild(el("p", "mark", "Claim " + (s.mark || "")));
          box.appendChild(el("p", "who", (s.who || "Unknown") + (s.when ? ", " + s.when : "")));
          box.appendChild(el("p", "claim", s.claim || ""));
          var evs = s.evidence || [];
          if (!evs.length) { box.appendChild(el("p", "ev", "Evidence: none attached")); }
          evs.forEach(function (ev) {
            var line = (ev.label || "") + (ev.excerpt ? " — " + ev.excerpt : "");
            box.appendChild(el("p", "ev", "Evidence: " + (line || "none")));
          });
          sides.appendChild(box);
        });
        card.appendChild(sides);
        var row = el("div", "actions stack");
        [["keep_a", "Keep A", "btn"], ["keep_b", "Keep B", "btn ghost"],
         ["keep_both", "Keep both", "btn ghost"], ["dismiss", "Dismiss", "btn ghost"]].forEach(function (b) {
          var btn = el("button", b[2], b[1]);
          btn.type = "button";
          btn.addEventListener("click", function () {
            var buttons = row.querySelectorAll("button");
            for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = true; }
            post("/review/decide", {token: token, item_id: it.id, decision: b[0]}).then(function (r) {
              if (r.ok) { show(r.j.items, r.j.total); $("status").textContent = r.j.message; }
              else { say("Not done", r.j.message || "", true); }
            }, function () { say("Not done", "Your FlowState didn't answer.", true); });
          });
          row.appendChild(btn);
        });
        card.appendChild(row);
        list.appendChild(card);
        return;
      }
      card.appendChild(el("p", "", it.question));
      if (it.summary) { card.appendChild(el("p", "", it.summary)); }
      if (it.details) { card.appendChild(el("p", "muted", it.details)); }
      card.appendChild(el("p", "faint", "From " + it.queued_by + ", " + it.queued_at + " UTC"));
      var name = null;
      if (it.needs_name) {
        var label = el("label", "", "To approve, type the project's exact name");
        name = el("input");
        name.setAttribute("autocomplete", "off");
        label.appendChild(name);
        card.appendChild(label);
      }
      var row = el("div", "actions");
      var yes = el("button", "btn", it.yes || "Approve"), no = el("button", "btn ghost", it.no || "Reject");
      yes.type = no.type = "button";
      function decide(decision) {
        yes.disabled = no.disabled = true;
        var body = {token: token, item_id: it.id, decision: decision};
        if (name) { body.confirm_name = name.value; }
        post("/review/decide", body).then(function (r) {
          if (r.ok) { show(r.j.items, r.j.total); $("status").textContent = r.j.message; }
          else { yes.disabled = no.disabled = false; say("Not done", r.j.message || "", true); }
        }, function () { say("Not done", "Your FlowState didn't answer.", true); });
      }
      yes.addEventListener("click", function () { decide("approve"); });
      no.addEventListener("click", function () { decide("reject"); });
      row.appendChild(yes);
      row.appendChild(no);
      card.appendChild(row);
      $("list").appendChild(card);
    });
  }
  if (!grant) { say("This link is incomplete", "Open it again from the chat.", true); return; }
  post("/review/open", {grant: grant}).then(function (r) {
    if (!r.ok) { say("Can't show your list", r.j.message || "", true); return; }
    token = r.j.token;
    show(r.j.items, r.j.total);
  }, function () { say("Can't show your list", "Your FlowState didn't answer.", true); });
})();
"""


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


class Sessions:
    """Phone sessions in memory (15 minutes): hash of the cookie -> page token, handle, expiry."""

    def __init__(self):
        self._lock = threading.Lock()
        self._s: dict = {}

    def start(self, handle: str) -> tuple[str, str]:
        cookie, token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = _clock()
        with self._lock:
            self._s = {k: v for k, v in self._s.items() if v["exp"] > now}
            self._s[_sha(cookie)] = {"token": _sha(token), "handle": handle,
                                     "exp": now + SESSION_SECONDS}
        return cookie, token

    def check(self, cookie, token) -> dict:
        if not isinstance(cookie, str) or not isinstance(token, str):
            raise approvals.ApprovalError("bad_token", "This page's session has ended. Open "
                                                       "the link again from the chat.")
        with self._lock:
            s = self._s.get(_sha(cookie))
        if not s or s["exp"] <= _clock() or not hmac.compare_digest(s["token"], _sha(token)):
            raise approvals.ApprovalError("bad_token", "This page's session has ended. Open "
                                                       "the link again from the chat.")
        return s


def items(db_path) -> list:
    """The waiting list, from the local database (all projects, oldest first, LIST_MAX)."""
    from . import review
    from .approve_page import view
    from .waiting import _KIND
    from . import conflict_cards
    hidden = conflict_cards.hidden_conflict_items(db_path)
    out = []
    for it in review.list_items(db_path, None, "pending", LIST_MAX)["items"]:
        if it["id"] in hidden:   # the card already asks this pair
            continue
        v = view(db_path, it["id"])
        v["kind_label"] = _KIND.get(it["kind"], it["kind"])
        if it["kind"] not in approvals.USER_ONLY_KINDS + ("cleanup", "session_summary", "disagreement"):
            v["question"] = "Approve to keep it; reject to leave it out."
        cap = 4000 if it["kind"] == "delete" else 1000
        v["details"] = (v["details"] or "")[:cap]
        out.append(v)
    return out


def _note_page(title: str, text: str) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FlowState: {title}</title>
<link rel="stylesheet" href="/review/assets/review.css">
</head><body><main class="wrap">
<p class="brand">FlowState</p>
<h1>{title}</h1>
<p class="muted">{text}</p>
</main></body></html>
"""


OFF_PAGE = _note_page("Phone review is off", "Phone review is turned off on this computer, or "
                      "this FlowState can't offer it. You can still review what's waiting: on "
                      "the computer where FlowState runs, open the FlowState review shortcut on "
                      "the desktop (or run <code>flowstate review-page</code>). If you reach "
                      "FlowState through your own tunnel, open "
                      "<code>https://&lt;your tunnel&gt;/review/tunnel</code> and sign in with "
                      "the owner passphrase. Phone review through flowstatemcp.com is one more "
                      "option: it needs the relay (<code>flowstate relay enroll</code>) and "
                      "<code>flowstate approvals phone on</code> on that computer.")
BUSY_PAGE = _note_page("Too many tries", "This list was opened too many times in the last hour. "
                       "Try again later.")
AGAIN = "Open the “Waiting for you” link from flowstatemcp.com again."
TICKET_PAGES = {   # no ticket, or one that can't be used: never echoed, nothing counted
    "missing": _note_page("Open the link again", "This page needs the sign-in from "
                          "flowstatemcp.com, and it didn't come with this visit. " + AGAIN),
    "bad": _note_page("Open the link again", "This link isn't valid for this FlowState. " + AGAIN),
    "expired": _note_page("This link expired", "This link expired after 5 minutes. " + AGAIN),
    "used": _note_page("This link was already used", "Each link opens your list once. " + AGAIN),
}


def _total(db_path) -> int:
    from .waiting import _pending_total
    return _pending_total(db_path)


def routes(db_path_fn):
    """Starlette routes; `db_path_fn()` gives the database in use."""
    from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
    from starlette.routing import Route

    sessions = Sessions()

    def answer(body: dict, status: int = 200, cookie: str = None):
        r = JSONResponse(body, status_code=status, headers=HEADERS)
        if cookie is not None:
            r.set_cookie(COOKIE, cookie, max_age=SESSION_SECONDS, path="/review", secure=True,
                         httponly=True, samesite="strict")
        return r

    def refuse(e: approvals.ApprovalError, status: int = 403):
        return answer({"error": e.code, "message": str(e)}, status)

    async def start(request):
        """The bookmark link's second hop: a fresh one-use code, back to the site to sign."""
        if request.method != "GET":   # Starlette adds HEAD to GET routes: no code for it
            return Response(status_code=405, headers={**HEADERS, "Allow": "GET"})
        db = db_path_fn()
        handle = await asyncio.to_thread(approvals.review_handle, db)
        if not handle:
            return HTMLResponse(OFF_PAGE, headers=HEADERS)
        ticket = request.query_params.get("t")
        if not ticket:
            return HTMLResponse(TICKET_PAGES["missing"], status_code=403, headers=HEADERS)
        if not approvals.TICKET_RE.match(ticket):   # cheap: before any crypto or database
            return HTMLResponse(TICKET_PAGES["bad"], status_code=400, headers=HEADERS)
        try:
            claims = await asyncio.to_thread(approvals.verify_start_ticket, db, ticket, handle)
            nonce = await asyncio.to_thread(approvals.start_nonce, db, claims["sub"],
                                            claims["nonce"])
        except approvals.ApprovalError as e:
            if e.code == "rate_limited":
                return HTMLResponse(BUSY_PAGE, status_code=429,
                                    headers={**HEADERS, "Retry-After": "3600"})
            page = {"grant_expired": "expired", "ticket_used": "used"}.get(e.code, "bad")
            return HTMLResponse(TICKET_PAGES[page], status_code=403, headers=HEADERS)
        if not nonce:
            return HTMLResponse(OFF_PAGE, headers=HEADERS)
        return RedirectResponse(f"{approvals.SITE}/review/{handle}?n={nonce}", status_code=303,
                                headers=HEADERS)

    async def shell(request):
        return HTMLResponse(SHELL, headers=HEADERS)

    async def asset(request):
        name = request.path_params["name"]
        if name == "review.css":
            return Response(CSS + EXTRA_CSS, media_type="text/css", headers=HEADERS)
        if name == "review.js":
            return Response(JS, media_type="text/javascript", headers=HEADERS)
        return answer({"error": "not found"}, 404)

    async def _body(request) -> dict:
        if not _same_origin(request):
            raise approvals.ApprovalError("bad_request", "This request didn't come from the page.")
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise approvals.ApprovalError("bad_request", "The request is incomplete.")
        return body

    async def open_(request):
        db = db_path_fn()
        try:
            body = await _body(request)
            if not isinstance(body.get("grant"), str):
                raise approvals.ApprovalError("bad_request", "The link is incomplete.")
            claims = await asyncio.to_thread(approvals.verify_review_grant, db, body["grant"])
            cookie, token = sessions.start(claims["handle"])
            listing = await asyncio.to_thread(items, db)
            total = await asyncio.to_thread(_total, db)
        except approvals.ApprovalError as e:
            return refuse(e)
        return answer({"items": listing, "total": total, "token": token}, cookie=cookie)

    async def decide(request):
        db = db_path_fn()
        try:
            body = await _body(request)
            sess = sessions.check(request.cookies.get(COOKIE), body.get("token"))
            item_id = body.get("item_id")
            if not isinstance(item_id, int) or isinstance(item_id, bool) \
                    or body.get("decision") not in ("approve", "reject", "keep_a", "keep_b",
                                                    "keep_both", "dismiss"):
                raise approvals.ApprovalError("bad_request", "Choose Approve or Reject for an item.")
        except approvals.ApprovalError as e:
            return refuse(e)
        from .popup import as_user
        label = f"phone ({sess['handle']})"
        keep = {"keep_a": "a", "keep_b": "b", "keep_both": "both"}.get(body["decision"])
        if body["decision"] == "approve" or keep:
            args = {"item_id": item_id, "confirm_name": body.get("confirm_name"),
                    "user_request": "The user approved this in their phone list (flowstatemcp.com)."}
            if keep:
                args["keep"] = keep
                args["user_request"] = "The user tapped a button in their phone list (flowstatemcp.com)."
            res = await asyncio.to_thread(as_user, "review_approve", args, label)
        else:
            reason = " ".join(str(body.get("reason") or "").split())[:300]
            args = {"item_id": item_id, "reason": reason or "rejected on the user's phone",
                    "user_request": "The user rejected this in their phone list (flowstatemcp.com)."}
            res = await asyncio.to_thread(as_user, "review_reject", args, label)
        listing = await asyncio.to_thread(items, db)
        total = await asyncio.to_thread(_total, db)
        if isinstance(res, dict) and "error" in res:
            return answer({"error": res["error"], "message": res.get("message") or res["error"],
                           "items": listing, "total": total}, 409)
        done = {"approve": "Approved", "keep_a": "Kept the older entry",
                "keep_b": "Kept the newer entry", "keep_both": "Kept both",
                "dismiss": "Dismissed"}.get(body["decision"], "Rejected")
        if isinstance(res, dict) and res.get("kind") == "session_summary":
            done = "Kept" if body["decision"] == "approve" else "Not kept"
        return answer({"ok": True, "message": f"{done} #{item_id}.", "items": listing,
                       "total": total})

    return [Route("/review/assets/{name}", asset, methods=["GET"]),
            Route("/review/start", start, methods=["GET"]),
            Route("/review", shell, methods=["GET"]),
            Route("/review/open", open_, methods=["POST"]),
            Route("/review/decide", decide, methods=["POST"])]
