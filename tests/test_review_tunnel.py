"""(task 38) The owner's review page on the public (tunnel) listener (review_tunnel.py):
/review/tunnel behind the owner passphrase, on listener "public" only.

Sign-in (passphrase, lockout, per-address limit, no-passphrase page), the session cookie
(__Host-, Secure, HttpOnly, SameSite=Strict, idle and total expiry, sign-out), CSRF and
same-origin on every POST, strict headers, no agent token opens it, and the decisions it can
make (user-only items, deletes, review off with approvals on), labelled "tunnel page" and
logged in approval_log. Temp DBs, a temp auth DB and the in-process app only (conftest.py)."""

import asyncio
import json
import logging
import re
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from flowstate import approvals, review, review_mode, review_tunnel
from flowstate.remote_auth import AuthStore

URL = "https://tunnel.example.com"
PASS = "correct horse battery staple"
OWNER = "o" * 32
ORIGIN = {"Origin": URL}
CSRF = re.compile(r"""name=["']csrf["'] value=["']([^"']+)["']""")


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _app(db, auth, public_url=URL, relay=False, token=OWNER):
    from flowstate.http_app import create_http_app
    return create_http_app(token, db_path=db, public_url=public_url, allow_master_token=True,
                           auth_db=auth, relay=relay)


@pytest.fixture
def store(tmp_path):
    s = AuthStore(tmp_path / "auth.db")
    s.set_passphrase(PASS)
    return s


@pytest.fixture
def tunnel(seeded_db, store):
    from flowstate import server as S
    with TestClient(_app(seeded_db, store.path), base_url=URL, follow_redirects=False) as c:
        yield c, seeded_db
    S.backend.tracker = None


def form_token(c):
    r = c.get("/review/tunnel")
    assert r.status_code == 200 and "Owner passphrase" in r.text, r.text
    return CSRF.search(r.text).group(1)


def sign_in(c, pw=PASS, **kw):
    return c.post("/review/tunnel/sign-in", data={"csrf": form_token(c), "passphrase": pw},
                  headers={**ORIGIN, **kw.pop("headers", {})}, **kw)


def page_csrf(c):
    r = c.get("/review/tunnel/")
    assert r.status_code == 200 and "Review queue" in r.text, r.text
    return CSRF.search(r.text).group(1), r


def status(db, item):
    return _q(db, "SELECT status, decided_by FROM review_items WHERE id = ?", item)[0]


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return json.loads(asyncio.run(go()).content[0].text)


# ---------------------------------------------------------------- sign-in
def test_sign_in_sets_a_strict_session_cookie_and_shows_the_page(tunnel):
    c, db = tunnel
    item = review.add_item(db, 1, "todo", "Ship the tunnel page", None, {}, "cursor")
    r = c.get("/review/tunnel")
    assert "Ship the tunnel page" not in r.text and "<script" not in r.text
    r = sign_in(c)
    assert r.status_code == 303 and r.headers["location"] == "/review/tunnel/"
    ck = r.headers["set-cookie"]
    assert ck.startswith("__Host-fs_review_tunnel=")
    low = ck.lower()
    for flag in ("secure", "httponly", "samesite=strict", "path=/"):
        assert flag in low, ck
    assert "domain=" not in low
    csrf, page = page_csrf(c)
    assert "Ship the tunnel page" in page.text and "Sign out" in page.text and f"#{item}" in page.text
    assert 'action="/review/tunnel/approve"' in page.text and "href='/review/tunnel/?view=who'" in page.text
    who = c.get("/review/tunnel/?view=who")
    assert who.status_code == 200 and "Who did what" in who.text and "Sign out" in who.text


def test_wrong_passphrase_then_lockout(tunnel, store):
    c, _db = tunnel
    for _ in range(5):
        r = sign_in(c, "wrong passphrase!")
        assert r.status_code == 403 and "Wrong passphrase" in r.text and "set-cookie" not in r.headers
    r = sign_in(c)                                   # locked: even the right one waits
    assert r.status_code == 429 and "Too many tries" in r.text and "set-cookie" not in r.headers
    assert int(r.headers["retry-after"]) > 0 and store.locked_for() > 0
    assert "Review queue" not in c.get("/review/tunnel").text


def test_per_address_limit_on_tries(seeded_db, store, monkeypatch):
    from flowstate import server as S
    monkeypatch.setattr(review_tunnel, "SIGNIN_PER_IP", 2)
    app = _app(seeded_db, store.path)
    # through a proxy on this computer (as cloudflared is): the caller's address is CF-Connecting-IP
    with TestClient(app, base_url=URL, follow_redirects=False, client=("127.0.0.1", 50000)) as c:
        a, b = {"CF-Connecting-IP": "203.0.113.7"}, {"CF-Connecting-IP": "198.51.100.9"}
        for _ in range(2):
            assert sign_in(c, "wrong passphrase!", headers=a).status_code == 403
        r = sign_in(c, headers=a)
        assert r.status_code == 429 and "set-cookie" not in r.headers
        assert sign_in(c, headers=b).status_code == 303          # another address isn't blocked
    S.backend.tracker = None


def test_no_passphrase_page(seeded_db, tmp_path):
    from flowstate import server as S
    with TestClient(_app(seeded_db, tmp_path / "empty-auth.db"), base_url=URL,
                    follow_redirects=False) as c:
        r = c.get("/review/tunnel")
        assert r.status_code == 503 and "flowstate remote passphrase" in r.text
        assert "<form" not in r.text and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        r = c.post("/review/tunnel/sign-in", data={"csrf": "x", "passphrase": PASS}, headers=ORIGIN)
        assert r.status_code == 403 and "set-cookie" not in r.headers
    S.backend.tracker = None


def test_sign_in_needs_its_form_token_and_same_origin(tunnel):
    c, _db = tunnel
    tok = form_token(c)
    for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.example"},
                    {"Origin": "http://tunnel.example.com"},
                    {**ORIGIN, "Sec-Fetch-Site": "cross-site"}):
        r = c.post("/review/tunnel/sign-in", data={"csrf": tok, "passphrase": PASS}, headers=headers)
        assert r.status_code == 403 and "set-cookie" not in r.headers, headers
    r = c.post("/review/tunnel/sign-in", data={"passphrase": PASS}, headers=ORIGIN)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = c.post("/review/tunnel/sign-in", data={"csrf": "made-up", "passphrase": PASS}, headers=ORIGIN)
    assert r.status_code == 403 and "set-cookie" not in r.headers
    r = c.post("/review/tunnel/sign-in", data={"csrf": tok, "passphrase": PASS}, headers=ORIGIN)
    assert r.status_code == 303                                   # the form token, once
    r = c.post("/review/tunnel/sign-in", data={"csrf": tok, "passphrase": PASS}, headers=ORIGIN)
    assert r.status_code == 403


def test_page_headers_and_phone_css(tunnel):
    c, _db = tunnel
    for r in (c.get("/review/tunnel"), (sign_in(c), page_csrf(c)[1])[1]):
        csp = r.headers["content-security-policy"]
        assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
        assert "form-action 'self'" in csp and "unsafe-inline" not in csp and "script-src" not in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["cache-control"] == "no-store"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "<style" not in r.text and "<script" not in r.text and " style=" not in r.text
        assert "width=device-width" in r.text and "/review/tunnel/page.css" in r.text
    css = c.get("/review/tunnel/page.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    from flowstate.review_page import _CSS
    assert css.text.startswith(_CSS)                             # the review page's own look
    for rule in ("min-height:44px", "overflow-x:auto", "flex-wrap:wrap", "overflow-wrap:anywhere"):
        assert rule in css.text


# ---------------------------------------------------------------- session
def test_session_idle_and_total_expiry(tunnel, monkeypatch):
    c, _db = tunnel
    now = [1_000_000.0]
    monkeypatch.setattr(review_tunnel, "_clock", lambda: now[0])
    sign_in(c)
    now[0] += review_tunnel.IDLE_SECONDS - 60
    page_csrf(c)                                                  # still signed in; refreshed
    now[0] += review_tunnel.IDLE_SECONDS + 1
    assert "Owner passphrase" in c.get("/review/tunnel").text     # idle too long
    sign_in(c)
    for _ in range(6):                                            # busy, but at most an hour
        now[0] += 10 * 60
        r = c.get("/review/tunnel/")
    assert "Owner passphrase" in r.text


def test_sign_out_ends_the_session(tunnel):
    c, db = tunnel
    item = review.add_item(db, 1, "todo", "Stay pending", None, {}, "cursor")
    sign_in(c)
    csrf, _ = page_csrf(c)
    cookie = c.cookies.get("__Host-fs_review_tunnel")
    assert c.post("/review/tunnel/sign-out", data={"csrf": "wrong"}, headers=ORIGIN).status_code == 403
    assert c.post("/review/tunnel/sign-out", data={"csrf": csrf}).status_code == 403   # no Origin
    r = c.post("/review/tunnel/sign-out", data={"csrf": csrf}, headers=ORIGIN)
    assert r.status_code == 303 and "__Host-fs_review_tunnel=" in r.headers["set-cookie"]
    assert "Owner passphrase" in c.get("/review/tunnel").text
    # the old cookie is dead on the server too
    r = c.post("/review/tunnel/approve", data={"csrf": csrf, "item_id": str(item)}, headers=ORIGIN,
               cookies={"__Host-fs_review_tunnel": cookie})
    assert r.status_code == 403 and status(item and db, item)[0] == "pending"


# ---------------------------------------------------------------- CSRF / origin on actions
def test_actions_need_session_csrf_and_same_origin(tunnel):
    c, db = tunnel
    item = review.add_item(db, 1, "todo", "Guarded item", None, {}, "cursor")
    sign_in(c)
    csrf, _ = page_csrf(c)
    form = {"csrf": csrf, "item_id": str(item)}
    for data, headers in (({"item_id": str(item)}, ORIGIN),                        # no CSRF
                          ({"csrf": "wrong", "item_id": str(item)}, ORIGIN),       # wrong CSRF
                          (form, {}),                                              # no Origin
                          (form, {"Origin": "null"}),
                          (form, {"Origin": "https://evil.example"}),
                          (form, {**ORIGIN, "Sec-Fetch-Site": "cross-site"})):
        r = c.post("/review/tunnel/approve", data=data, headers=headers)
        assert r.status_code == 403, (data, headers)
    for name in ("reject", "mode", "app", "undo"):
        assert c.post(f"/review/tunnel/{name}", data={"item_id": str(item)}, headers=ORIGIN).status_code == 403
    assert status(db, item)[0] == "pending"
    r = c.post("/review/tunnel/approve", data=form, headers=ORIGIN)
    assert r.status_code == 303 and status(db, item) == ("approved", "tunnel page")
    assert f"Approved #{item}." in c.get("/review/tunnel/").text      # the flash, once
    assert f"Approved #{item}." not in c.get("/review/tunnel/").text


# ---------------------------------------------------------------- no agent token opens it
def test_no_agent_token_opens_the_page(tunnel, store):
    from flowstate import app_keys
    c, db = tunnel
    item = review.add_item(db, 1, "todo", "Secret-ish waiting thing", None, {}, "cursor")
    _cid, client_token = store.add_token_client("Cursor remote", ["flowstate:read", "flowstate:write"])
    oc, _ = store.add_token_client("Pretend OAuth app", ["flowstate:read", "flowstate:write"])
    access = store.issue("access", oc, ["flowstate:read", "flowstate:write"], "fam-1", 3600,
                         resource=URL + "/mcp")
    _kid, lan_key = app_keys.add(store, "LAN app")
    tokens = {"owner": OWNER, "client": client_token, "oauth": access, "app key": lan_key}
    # they're real: the MCP endpoint takes the owner, client and OAuth tokens on this listener
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    for name in ("owner", "client", "oauth"):
        r = c.post("/mcp", content=json.dumps(init), headers={
            "Authorization": f"Bearer {tokens[name]}", "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream"})
        assert r.status_code == 200, (name, r.status_code)
    anon = c.get("/review/tunnel")
    for name, tok in tokens.items():
        h = {"Authorization": f"Bearer {tok}"}
        r = c.get("/review/tunnel", headers=h)
        assert r.status_code == 200 and "Owner passphrase" in r.text, name
        assert "Secret-ish" not in r.text and "Review queue" not in r.text
        assert re.sub(CSRF, "", r.text) == re.sub(CSRF, "", anon.text)      # same as anonymous
        r = c.get("/review/tunnel/?view=who", headers=h)
        assert "Who did what" not in r.text
        for path, data in (("approve", {"item_id": str(item), "csrf": "x"}), ("mode", {"mode": "off", "csrf": "x"})):
            r = c.post(f"/review/tunnel/{path}", data=data, headers={**h, **ORIGIN})
            assert r.status_code == 403, (name, path)
    assert status(db, item)[0] == "pending" and review_mode.get(db, None)["mode"] != "off"


# ---------------------------------------------------------------- what it can decide
def test_decides_a_user_only_delete_with_approvals_on(tunnel):
    from flowstate import server as S
    c, db = tunnel
    approvals.set_on(db, True)
    todo = _q(db, "SELECT id FROM todos WHERE project_id = 1 ORDER BY id LIMIT 1")[0][0]
    out = call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                    "record_id": todo})
    item = out["delete_requested"]
    assert out["status"] == "waiting for the user" and _q(db, "SELECT COUNT(*) FROM todos WHERE id = ?", todo) == [(1,)]
    sign_in(c)
    csrf, page = page_csrf(c)
    assert "Approve: delete it" in page.text
    r = c.post("/review/tunnel/approve", data={"csrf": csrf, "item_id": str(item)}, headers=ORIGIN)
    assert r.status_code == 303
    assert status(db, item) == ("approved", "tunnel page")
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = ?", todo) == [(0,)]
    assert _q(db, "SELECT path, client, decision FROM approval_log WHERE item_id = ?", item) == \
        [("tunnel page", "tunnel page", "approved")]


def test_decides_a_did_you_say_this_item(tunnel):
    from flowstate import server as S
    c, db = tunnel
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "Deploys on Fridays",
                             "source": "ai_inferred"})
    rid = r["record"]["id"]
    out = call(S, "manage_memory", {"action": "set_source", "project": "Alpha", "record_type": "learning",
                                    "record_id": rid, "source": "user_stated"})
    item = out["confirm_requested"]
    sign_in(c)
    csrf, page = page_csrf(c)
    assert "Approve: I said this" in page.text
    c.post("/review/tunnel/approve", data={"csrf": csrf, "item_id": str(item)}, headers=ORIGIN)
    assert status(db, item) == ("approved", "tunnel page")
    assert _q(db, "SELECT source, source_set_by FROM record_meta WHERE record_type = 'learning' "
                  "AND record_id = ?", rid) == [("user_stated", "user")]
    assert _q(db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item) == \
        [("tunnel page", "approved")]


def test_reject_and_turn_review_off_with_approvals_on(tunnel):
    c, db = tunnel
    approvals.set_on(db, True)
    item = review.add_item(db, 1, "decision", "Maybe this", None, {}, "cursor")
    sign_in(c)
    csrf, _ = page_csrf(c)
    c.post("/review/tunnel/reject", data={"csrf": csrf, "item_id": str(item), "reason": "no"}, headers=ORIGIN)
    assert status(db, item) == ("rejected", "tunnel page")
    assert _q(db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item) == [("tunnel page", "rejected")]
    r = c.post("/review/tunnel/mode", data={"csrf": csrf, "mode": "off"}, headers=ORIGIN)
    assert r.status_code == 303 and review_mode.get(db, None)["mode"] == "off"
    assert _q(db, "SELECT new_mode, client FROM review_mode_log ORDER BY id DESC LIMIT 1") == [("off", "tunnel page")]


def test_passphrase_and_cookie_are_never_logged(tunnel, caplog):
    c, _db = tunnel
    with caplog.at_level(logging.DEBUG):
        sign_in(c, "not the passphrase")
        sign_in(c)
        page_csrf(c)
    cookie = c.cookies.get("__Host-fs_review_tunnel")
    assert PASS not in caplog.text and "not the passphrase" not in caplog.text
    assert cookie and cookie not in caplog.text


# ---------------------------------------------------------------- only on the public listener
def test_not_on_the_lan_or_relay_listener(seeded_db, store):
    from flowstate import server as S
    from flowstate.http_app import create_http_app
    lan = create_http_app(OWNER, db_path=seeded_db, auth_db=store.path)
    relay = _app(seeded_db, store.path, public_url="https://testbox.fsmcp.com", relay=True)
    for app, base in ((lan, "http://127.0.0.1:8766"), (relay, "https://testbox.fsmcp.com")):
        with TestClient(app, base_url=base, follow_redirects=False) as c:
            for path in ("/review/tunnel", "/review/tunnel/", "/review/tunnel/page.css"):
                assert c.get(path).status_code == 404, (base, path)
            r = c.post("/review/tunnel/sign-in", data={"passphrase": PASS}, headers={"Origin": base})
            assert r.status_code == 404
    S.backend.tracker = None


def test_ask_line_names_the_tunnel_page_only_through_the_public_listener(backend, store, monkeypatch):
    from flowstate import popup
    db = backend.backend.db_path
    item = review.add_item(db, 1, "delete", "Delete it", None, {"record_type": "todo", "record_id": 1,
                                                                 "user_only": True}, "cursor")
    review_tunnel.routes(lambda: db, store, URL)          # this process now has a tunnel listener
    assert approvals.ask_line(db, item) == (
        "Waiting for the user: ask them to open the FlowState review shortcut on their desktop and "
        f"approve it there (or run `flowstate approve {item}` in a terminal).")
    monkeypatch.setattr(popup, "listener", lambda: "public")
    assert approvals.ask_line(db, item) == (
        f"Waiting for the user: ask them to approve it on their tunnel review page ({URL}/review/tunnel) "
        f"or with the FlowState review shortcut on their desktop (or run `flowstate approve {item}` "
        f"in a terminal).")
    monkeypatch.setattr(review_tunnel, "PUBLIC_URL", None)
    assert "tunnel" not in approvals.ask_line(db, item)
