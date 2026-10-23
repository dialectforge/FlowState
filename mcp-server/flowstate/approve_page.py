"""The phone approval page (approvals.py, path 2), served by FlowState's HTTP app, which the
relay agent reaches at https://<handle>.fsmcp.com (TLS ends on this computer).

flowstatemcp.com signs the user in, then sends the browser to /approve/<item>#g=<grant>. The
fragment never reaches a server or a log. This page:

  GET  /approve/<item>          an empty shell (no data); its script reads #g=, removes it from
                                the address bar at once (history.replaceState) and posts it
  POST /approve/<item>/open     checks the grant (approvals.verify_grant). Each grant opens
                                the page once. Answers with the item from the local database and
                                a page token, also set as a SameSite=Strict, Secure, HttpOnly
                                cookie, bound to that grant
  POST /approve/<item>/decide   the tap: the same grant, checked again, plus the page token in
                                the body and the cookie. Approve or Reject uses the grant and the
                                item's one-use code

Strict CSP (no inline script or style: both are files under /approve/assets/), frame-ancestors
'none' + X-Frame-Options DENY, no-store, no referrer. POSTs must be JSON, from this origin.
"""

import asyncio
import hashlib
import json
import secrets
from datetime import datetime, timezone

from . import approvals

HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Frame-Options": "DENY", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
COOKIE = "fsa_page"

SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>FlowState approval</title>
<link rel="stylesheet" href="/approve/assets/approve.css">
<script src="/approve/assets/approve.js" defer></script>
</head><body><main class="wrap">
<p class="brand">FlowState</p>
<h1 id="title">Checking your approval link...</h1>
<p id="status" class="muted"></p>
<noscript><p class="muted">This page needs JavaScript to read your approval link.</p></noscript>
<section id="item" hidden>
  <p class="eyebrow" id="project"></p>
  <p id="summary"></p>
  <p id="details" class="muted"></p>
  <p id="who" class="faint"></p>
  <label id="name-row" hidden>Type the project's exact name to delete it
    <input id="confirm-name" autocomplete="off" autocapitalize="off" spellcheck="false"></label>
  <div class="actions">
    <button id="approve" class="btn" type="button">Approve</button>
    <button id="reject" class="btn ghost" type="button">Reject</button>
  </div>
</section>
<p class="faint small">Your FlowState, on your own computer, shows this page. flowstatemcp.com
only confirmed it's you; it never sees what's in it.</p>
</main></body></html>
"""

CSS = """:root{--bg:#fbfaf7;--ink:#1c1d1f;--muted:#62656b;--faint:#9a9ca1;--line:#e7e4dd;
--accent:#1f5f5b;--accent-soft:#e6efed;--danger:#8a2d22;
--sans:ui-sans-serif,-apple-system,"SF Pro Text","Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
--serif:ui-serif,"New York","Iowan Old Style",Georgia,"Times New Roman",serif}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.6 var(--sans);-webkit-font-smoothing:antialiased}
.wrap{max-width:560px;margin:0 auto;padding:28px 22px 48px}
.brand{font-weight:600;letter-spacing:-.01em;margin:0 0 32px}
h1{font-family:var(--serif);font-weight:500;font-size:28px;line-height:1.2;letter-spacing:-.01em;margin:0 0 14px}
.eyebrow{font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent);margin:24px 0 8px}
.muted{color:var(--muted)}.faint{color:var(--faint)}.small{font-size:14px;margin-top:40px}
#summary{font-size:19px;margin:0 0 10px;overflow-wrap:anywhere}
#details{overflow-wrap:anywhere;white-space:pre-wrap}
label{display:block;margin:20px 0 0;font-size:15px;color:var(--muted)}
input{display:block;width:100%;margin-top:6px;font:inherit;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink)}
.actions{display:flex;gap:14px;margin-top:28px}
.btn{flex:1;font:inherit;font-size:16px;font-weight:500;padding:15px 22px;border-radius:999px;border:0;cursor:pointer;background:var(--ink);color:var(--bg)}
.btn:hover{background:var(--accent)}.btn:disabled{opacity:.5;cursor:default}
.btn.ghost{background:none;color:var(--ink);border:1px solid var(--line)}
.btn.ghost:hover{border-color:var(--ink);background:none}
.err{color:var(--danger)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

JS = r"""(function () {
  "use strict";
  var m = /(?:^#|&)g=([A-Za-z0-9_.-]+)/.exec(location.hash || "");
  var grant = m ? m[1] : null;
  history.replaceState(null, "", location.pathname);   // the grant leaves the address bar now
  var base = location.pathname.replace(/\/+$/, "");
  var token = null, needsName = false;
  function $(id) { return document.getElementById(id); }
  function say(title, text, bad) {
    $("title").textContent = title;
    $("status").textContent = text || "";
    $("status").className = bad ? "err" : "muted";
  }
  function post(path, body) {
    return fetch(base + path, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
    }).then(function (r) {
      return r.json().then(function (j) { return {ok: r.ok, j: j}; });
    });
  }
  function decide(decision) {
    $("approve").disabled = $("reject").disabled = true;
    var body = {grant: grant, token: token, decision: decision};
    if (needsName) { body.confirm_name = $("confirm-name").value; }
    post("/decide", body).then(function (r) {
      $("item").hidden = true;
      if (r.ok) { say(decision === "approve" ? "Approved" : "Rejected", r.j.message); }
      else { say("Not done", r.j.message || "Something went wrong.", true); }
    }, function () { say("Not done", "Your FlowState didn't answer.", true); });
  }
  if (!grant) { say("This link is incomplete", "Open it again from the chat.", true); return; }
  post("/open", {grant: grant}).then(function (r) {
    if (!r.ok) { say("Can't show this request", r.j.message || "", true); return; }
    var it = r.j.item;
    token = r.j.token;
    needsName = !!it.needs_name;
    say(it.question, "");
    $("project").textContent = it.project;
    $("summary").textContent = it.summary;
    $("details").textContent = it.details || "";
    $("who").textContent = "Asked by " + it.queued_by + ", " + it.queued_at + " UTC";
    $("name-row").hidden = !needsName;
    $("item").hidden = false;
    if (it.card && it.card.sides) {
      var box = document.querySelector(".actions");
      $("approve").hidden = $("reject").hidden = true;
      $("summary").textContent = "";
      [["keep_a", "Keep A", "btn"], ["keep_b", "Keep B", "btn ghost"],
       ["keep_both", "Keep both", "btn ghost"], ["dismiss", "Dismiss", "btn ghost"]].forEach(function (b) {
        var btn = document.createElement("button");
        btn.className = b[2];
        btn.type = "button";
        btn.textContent = b[1];
        btn.addEventListener("click", function () { decide(b[0]); });
        box.appendChild(btn);
      });
      var sides = document.createElement("div");
      it.card.sides.forEach(function (s) {
        var node = document.createElement("p");
        var ev = (s.evidence || []).map(function (e) {
          return (e.label || "") + (e.excerpt ? " — " + e.excerpt : "");
        }).filter(Boolean).join("; ");
        node.textContent = "Claim " + (s.mark || "") + " (" + (s.who || "Unknown") +
          (s.when ? ", " + s.when : "") + "): " + (s.claim || "") +
          " Evidence: " + (ev || "none");
        sides.appendChild(node);
      });
      $("details").textContent = "";
      $("details").appendChild(sides);
    }
    $("approve").addEventListener("click", function () { decide("approve"); });
    $("reject").addEventListener("click", function () { decide("reject"); });
  }, function () { say("Can't show this request", "Your FlowState didn't answer.", true); });
})();
"""


def _sha(s: str) -> str:
    return hashlib.sha256((s or "").encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def view(db_path, item_id: int) -> dict:
    """What the page shows, from the local database."""
    from . import review
    item = review.get_item(db_path, item_id)
    conn = approvals._conn(db_path)
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (item["project_id"],)).fetchone()
    finally:
        conn.close()
    project = row[0] if row else f"project #{item['project_id']}"
    p = json.loads(item.get("payload") or "{}")
    app = item.get("app") or item.get("source") or "An app"
    ask = approvals.describe(item, project, db_path)
    card = None
    summary = review.strip_tag(item.get("summary") or "")
    details = item.get("details") or ""
    if item["kind"] == "cleanup":   # FlowState's own idea: one plain question and its reason
        why = p.get("reason") or ""
        question = f"Should FlowState {ask}?" + (f" {why[:1].upper()}{why[1:]}." if why else "")
    elif item["kind"] == "session_summary":   # what Keep / Don't keep really do
        question = f"{review.KEEP_QUESTION} {review.KEEP_HOW}"
    elif item["kind"] == "delete":   # the rows the database says will go, not the agent's words
        from . import deletes
        got = deletes.preview(db_path, item)
        question, summary, details = got["headline"], got["root_line"], got["details"]
    elif item["kind"] == "disagreement":
        from . import conflict_cards
        card = conflict_cards.phone_card(db_path, item)
        question = (card or {}).get("headline") or "Two AIs disagree."
        summary, details = "", ""
    else:
        question = f"{app} wants to {ask}."
    keep = item["kind"] == "session_summary"
    out = {"id": item["id"], "project": project, "kind": item["kind"], "question": question,
           "summary": summary, "details": details,
           "yes": review.KEEP if keep else "Approve", "no": review.DONT_KEEP if keep else "Reject",
           "queued_by": app + (f" on {item['machine']}" if item.get("machine") else ""),
           "queued_at": item.get("created_at") or "",
           "needs_name": item["kind"] == "delete" and p.get("record_type") == "project"}
    if card:
        out["card"] = card
    return out


def _same_origin(request) -> bool:
    """A JSON POST from this page's own origin (no form posts, no other sites)."""
    if (request.headers.get("content-type") or "").split(";")[0].strip() != "application/json":
        return False
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return False
    origin = request.headers.get("origin")
    if origin:
        from urllib.parse import urlparse
        if (urlparse(origin).netloc or "").lower() != (request.headers.get("host") or "").lower():
            return False
    return True


def routes(db_path_fn):
    """Starlette routes; `db_path_fn()` gives the database in use."""
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    def answer(body: dict, status: int = 200, cookie: str = None, path: str = "/approve"):
        r = JSONResponse(body, status_code=status, headers=HEADERS)
        if cookie is not None:
            r.set_cookie(COOKIE, cookie, max_age=approvals.GRANT_SECONDS, path=path, secure=True,
                         httponly=True, samesite="strict")
        return r

    def refuse(e: approvals.ApprovalError, status: int = 403):
        return answer({"error": e.code, "message": str(e)}, status)

    async def shell(request):
        return HTMLResponse(SHELL, headers=HEADERS)

    async def asset(request):
        name = request.path_params["name"]
        if name == "approve.css":
            return Response(CSS, media_type="text/css", headers=HEADERS)
        if name == "approve.js":
            return Response(JS, media_type="text/javascript", headers=HEADERS)
        return answer({"error": "not found"}, 404)

    async def _body(request):
        if not _same_origin(request):
            raise approvals.ApprovalError("bad_request", "This request didn't come from the page.")
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("grant"), str):
            raise approvals.ApprovalError("bad_request", "The approval link is incomplete.")
        return body

    async def open_(request):
        item_id = request.path_params["item"]
        db = db_path_fn()
        try:
            body = await _body(request)
            claims = await asyncio.to_thread(approvals.verify_grant, db, body["grant"], item_id)
            token = secrets.token_urlsafe(32)
            fresh = await asyncio.to_thread(_remember, db, body["grant"], item_id, token,
                                            claims["exp"])
            if not fresh:
                raise approvals.ApprovalError("grant_used", "This approval link was already "
                                                            "opened. Open it again from the chat.")
            item = await asyncio.to_thread(view, db, item_id)
        except approvals.ApprovalError as e:
            return refuse(e)
        return answer({"item": item, "token": token}, cookie=token, path=f"/approve/{item_id}")

    async def decide(request):
        item_id = request.path_params["item"]
        db = db_path_fn()
        try:
            body = await _body(request)
            if body.get("decision") not in ("approve", "reject", "keep_a", "keep_b", "keep_both",
                                             "dismiss"):
                raise approvals.ApprovalError("bad_request", "Choose Approve or Reject.")
            claims = await asyncio.to_thread(approvals.verify_grant, db, body["grant"], item_id)
            token = body.get("token")
            if not isinstance(token, str) or request.cookies.get(COOKIE) != token:
                raise approvals.ApprovalError("bad_token", "This page's session doesn't match. "
                                                           "Open the link again from the chat.")
            if not await asyncio.to_thread(_use, db, body["grant"], item_id, token):
                raise approvals.ApprovalError("grant_used", "This approval link was already "
                                                            "used. Open it again from the chat.")
        except approvals.ApprovalError as e:
            return refuse(e)
        from .popup import as_user
        label = f"phone ({claims.get('handle')})"
        keep = {"keep_a": "a", "keep_b": "b", "keep_both": "both"}.get(body["decision"])
        if body["decision"] == "approve" or keep:
            args = {"item_id": item_id, "confirm_name": body.get("confirm_name"),
                    "user_request": "The user approved this on their phone (flowstatemcp.com)."}
            if keep:
                args["keep"] = keep
                args["user_request"] = "The user tapped a button on their phone (flowstatemcp.com)."
            res = await asyncio.to_thread(as_user, "review_approve", args, label)
        else:
            args = {"item_id": item_id, "reason": "rejected on the user's phone",
                    "user_request": "The user rejected this on their phone (flowstatemcp.com)."}
            res = await asyncio.to_thread(as_user, "review_reject", args, label)
        if isinstance(res, dict) and "error" in res:
            return answer({"error": res["error"], "message": res.get("message") or res["error"]}, 409)
        done = {"approve": "Approved", "keep_a": "Kept the older entry",
                "keep_b": "Kept the newer entry", "keep_both": "Kept both",
                "dismiss": "Dismissed"}.get(body["decision"], "Rejected")
        extra = " It can be undone from the chat (manage_history undo)." \
            if body["decision"] == "approve" and isinstance(res, dict) and res.get("undo") else ""
        return answer({"ok": True, "message": f"{done}. You can close this page.{extra}"})

    return [Route("/approve/assets/{name}", asset, methods=["GET"]),
            Route("/approve/{item:int}", shell, methods=["GET"]),
            Route("/approve/{item:int}/open", open_, methods=["POST"]),
            Route("/approve/{item:int}/decide", decide, methods=["POST"])]


def _remember(db_path, grant: str, item_id: int, token: str, exp: int) -> bool:
    """First page load for this grant: store its hash and the page token's. False if seen."""
    conn = approvals._conn(db_path)
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO approval_grants (grant_hash, item_id, page_token_hash, seen_at,
               expires_at) VALUES (?, ?, ?, ?, ?)""",
            (approvals.grant_hash(grant), item_id, _sha(token), _now(),
             datetime.fromtimestamp(exp, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _use(db_path, grant: str, item_id: int, token: str) -> bool:
    """The tap: this grant's page token, not used yet and not expired. Marks it used."""
    conn = approvals._conn(db_path)
    try:
        cur = conn.execute(
            """UPDATE approval_grants SET used_at = ? WHERE grant_hash = ? AND item_id = ?
               AND page_token_hash = ? AND used_at IS NULL AND expires_at > ?""",
            (_now(), approvals.grant_hash(grant), item_id, _sha(token), _now()))
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()
