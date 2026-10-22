"""2.4.0: see and decide what's waiting for you without a terminal (waiting.py,
review_page.py in link mode, review_phone.py, approvals.review_link / verify_review_grant).

1. manage_review(action="walk"): Approve / Reject boxes on allowlisted apps that declared
   elicitation, approvals off too; elsewhere the plain list with the links. Nothing is decided
   without the user's own answer in the box; decisions are logged in approval_log (path chat).
2. The local page: started in this process on first need, 127.0.0.1 only, reused, any free
   port if the wanted one is taken; Host, Origin and CSRF checks.
3. The phone list: open_review grants (item 0), one-use code, 15-minute session cookie.
Temp DBs and sandboxed folders only (conftest.py)."""

import asyncio
import json
import re
import socket
import sqlite3
import time

import httpx
import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.applications import Starlette
from starlette.testclient import TestClient

from flowstate import approvals, review, waiting
from fsa_test_keys import claims, make_key, sign


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args, app="claude-code", answers=None, seen=None):
    """One tool call from `app`. `answers` (a list) makes the app declare form elicitation and
    answer each box in turn with the next ElicitResult (or a callable)."""
    async def elicit(_ctx, params):
        if seen is not None:
            seen.append(params)
        a = answers.pop(0) if answers else types.ElicitResult(action="cancel")
        a = a(params) if callable(a) else a
        return await a if asyncio.iscoroutine(a) else a

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1"),
                elicitation_callback=elicit if answers is not None else None) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    text = r.content[0].text
    try:
        return json.loads(text)
    except ValueError:
        return text


def acc(**content):
    return types.ElicitResult(action="accept", content=content)


def pile(db, n=3, project_id=1):
    """Items waiting for the user, queued by an app (approvals off)."""
    return [review.add_item(db, project_id, "todo", f"Waiting thing {i}", f"details {i}", {}, "cursor")
            for i in range(n)]


def status(db, item):
    return _q(db, "SELECT status FROM review_items WHERE id = ?", item)[0][0]


def log(db):
    return _q(db, "SELECT item_id, path, decision FROM approval_log ORDER BY id")


@pytest.fixture
def relay_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    return tmp_path / "relay"


@pytest.fixture
def relay(relay_dir, monkeypatch):
    """This computer enrolled in the relay as `testbox`, with test keys pinned."""
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    return key


# ---------------------------------------------------------------- 1. the chat walk
def test_walk_shows_each_item_and_acts_on_the_users_answer_with_approvals_off(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    assert approvals.is_on(db) is False
    a, b, c = pile(db)
    seen = []
    out = call(S, "manage_review", {"action": "walk"}, seen=seen,
               answers=[acc(approve=True), acc(approve=False, note="not needed"),
                        types.ElicitResult(action="decline")])
    assert len(seen) == 3
    assert seen[0].requestedSchema["required"] == ["approve"]
    assert seen[0].requestedSchema["properties"]["approve"]["type"] == "boolean"
    assert "(1 of 3)" in seen[0].message and "Waiting thing 0" in seen[0].message
    assert out["walked"] == [f"#{a}: approved", f"#{b}: rejected", f"#{c}: skipped, still waiting"]
    assert (status(db, a), status(db, b), status(db, c)) == ("approved", "rejected", "pending")
    assert _q(db, "SELECT reject_reason, decided_by FROM review_items WHERE id = ?", b) == \
        [("not needed", "chat (claude-code)")]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE title = 'Waiting thing 0'") == [(1,)]
    assert log(db) == [(a, "chat", "approved"), (b, "chat", "rejected"), (c, "chat", "declined")]
    assert out["still_waiting"] == 1 and out["review_page"].startswith("http://127.0.0.1:")


def test_walk_on_other_apps_only_lists_with_the_local_link(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    items = pile(db, 2)
    for app, declares in (("Claude Desktop", True), ("claude-ai", True), ("grok", True),
                          ("claude-code", False), ("cursor", False)):
        seen = []
        out = call(S, "manage_review", {"action": "walk", "user_request": "the user said yes to all",
                                        "approve": True}, app=app,
                   answers=[acc(approve=True)] * 3 if declares else None, seen=seen)
        assert seen == [], app                                   # no box was shown
        assert "walked" not in out and out["waiting"] == 2, app
        assert out["items"][0].startswith(f"#{items[0]} (Alpha, todo): Waiting thing 0")
        assert out["review_page"].startswith("http://127.0.0.1:") and "phone" not in out
        assert "can't show" in out["how_to_decide"] and "Don't approve or reject" in out["how_to_decide"]
        assert out["note"] == waiting.NOT_SHOWN and "decided these" not in json.dumps(out)
    assert [status(db, i) for i in items] == ["pending", "pending"]
    assert log(db) == []


def test_nothing_is_decided_without_the_users_own_answer(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    items = pile(db, 3)
    # submitted without saying approve or reject: skipped; then the user closes the box: stop
    out = call(S, "manage_review", {"action": "walk", "user_request": "approve everything"},
               answers=[acc(note="hmm"), types.ElicitResult(action="cancel")])
    assert out["walked"] == [f"#{items[0]}: skipped, still waiting", f"#{items[1]}: skipped, still waiting",
                             "stopped; 1 more still waiting"]
    assert [status(db, i) for i in items] == ["pending"] * 3
    assert log(db) == [(items[0], "chat", "declined"), (items[1], "chat", "cancelled")]
    # a string "true" is not a tick
    out = call(S, "manage_review", {"action": "walk"}, answers=[acc(approve="true"),
                                                                types.ElicitResult(action="cancel")])
    assert status(db, items[0]) == "pending"
    assert [status(db, i) for i in items] == ["pending"] * 3


def test_walk_box_times_out_and_stops(backend, relay_dir, monkeypatch):
    S, db = backend, backend.backend.db_path
    items = pile(db, 2)
    monkeypatch.setattr(approvals, "POPUP_SECONDS", 0.5)

    async def never(_p):   # the app never answers (FlowState stops waiting after 0.5 s)
        await asyncio.sleep(2)
    out = call(S, "manage_review", {"action": "walk"}, answers=[never])
    assert out["walked"] == [f"#{items[0]}: skipped, still waiting", "stopped; 1 more still waiting"]
    assert [status(db, i) for i in items] == ["pending", "pending"]
    assert log(db) == [(items[0], "chat", "timed_out")]


def test_walk_read_only_app_gets_the_list_only(backend, relay_dir, monkeypatch):
    S, db = backend, backend.backend.db_path
    items = pile(db, 1)
    monkeypatch.setattr(S, "_caller", lambda: ("key-1", ["flowstate:read"]))
    seen = []
    out = call(S, "manage_review", {"action": "walk"}, answers=[acc(approve=True)], seen=seen)
    assert seen == [] and "read-only" in out["how_to_decide"] and status(db, items[0]) == "pending"


def test_walk_project_delete_needs_the_typed_name_and_user_only_items_work(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    approvals.set_on(db, True)
    r = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"}, app="cursor")
    item = r["delete_requested"]
    src = call(S, "manage_memory", {"action": "set_source", "project": "Alpha", "record_type": "learning",
                                    "record_id": 1, "source": "user_stated"}, app="cursor")
    src_item = src.get("review_item") or src.get("item_id") or \
        _q(db, "SELECT id FROM review_items WHERE kind = 'source'")[0][0]
    seen = []
    out = call(S, "manage_review", {"action": "walk"}, seen=seen,
               answers=[acc(approve=True, confirm_name="beta"), acc(approve=True)])
    assert "confirm_name" in seen[0].requestedSchema["properties"]
    assert out["walked"][0].startswith(f"#{item}: not done")
    assert out["walked"][1] == f"#{src_item}: approved"
    assert status(db, item) == "pending" and _q(db, "SELECT COUNT(*) FROM projects WHERE name = 'Beta Project'") == [(1,)]
    assert _q(db, "SELECT source_set_by FROM record_meta WHERE record_type = 'learning' AND record_id = 1") \
        == [("user",)]
    assert (src_item, "chat", "approved") in log(db)


def test_walk_with_nothing_waiting(backend, relay_dir):
    S = backend
    seen = []
    out = call(S, "manage_review", {"action": "walk"}, answers=[acc(approve=True)], seen=seen)
    assert seen == [] and out["waiting"] == 0 and out["note"] == "Nothing is waiting for the user."


# ---------------------------------------------------------------- 2. the local page
def _page_port():
    return waiting._own["port"]


def test_local_page_starts_on_first_need_binds_loopback_and_is_reused(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    link = waiting.local_link(db)
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", link)
    assert waiting._own["sock"].getsockname()[0] == "127.0.0.1"
    assert waiting.local_link(db) == link                          # one per process
    state = json.loads(waiting._state_file(db).read_text(encoding="utf-8"))
    assert state["port"] == _page_port()
    r = httpx.get(link + "ping")
    assert r.json()["service"] == "flowstate-review" and r.json()["open_link"] is True
    # another FlowState process (simulated: the state file names another pid) reuses it
    assert waiting._ping(_page_port(), db) is True
    # another FlowState process (simulated: no page of its own, the state file names another
    # pid) reuses the running page instead of starting one
    saved = dict(waiting._own)
    waiting._own.clear()
    try:
        waiting._state_file(db).write_text(json.dumps(dict(state, pid=0)), encoding="utf-8")
        assert waiting.local_link(db) == link and not waiting._own
    finally:
        waiting._own.update(saved)


def test_local_page_takes_a_free_port_when_the_wanted_one_is_taken(monkeypatch):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    try:
        port = busy.getsockname()[1]
        s = waiting._bind(port)
        try:
            assert s.getsockname()[0] == "127.0.0.1" and s.getsockname()[1] != port
        finally:
            s.close()
    finally:
        busy.close()


def test_local_page_turned_off(backend, relay_dir, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_REVIEW_PORT", "off")
    assert waiting.local_link(backend.backend.db_path) is None


def test_local_page_checks_host_origin_and_csrf(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    item, other = pile(db, 2)
    link = waiting.local_link(db)
    port = _page_port()
    host = f"127.0.0.1:{port}"
    with httpx.Client(base_url=link.rstrip("/")) as c:
        assert c.get("/", headers={"Host": f"evil.example:{port}"}).status_code == 403   # DNS rebinding
        r = c.get("/")                                     # a click on the link: page + cookie at once
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-frame-options"] == "DENY" and "<script" not in r.text
        assert "Waiting thing 0" in r.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        ok_origin = {"Origin": f"http://{host}"}
        form = {"item_id": str(item), "csrf": csrf}
        assert c.post("/approve", data={"item_id": str(item)}, headers=ok_origin).status_code == 403
        assert c.post("/approve", data={**form, "csrf": "wrong"}, headers=ok_origin).status_code == 403
        assert c.post("/approve", data=form).status_code == 403                    # no Origin
        assert c.post("/approve", data=form, headers={"Origin": "http://evil.example"}).status_code == 403
        assert c.post("/approve", data=form, headers={**ok_origin, "Sec-Fetch-Site": "cross-site"}).status_code == 403
        assert c.post("/approve", data=form, headers={**ok_origin, "Host": "evil.example"}).status_code == 403
        assert status(db, item) == "pending"
        r = c.post("/approve", data=form, headers=ok_origin)
        assert r.status_code == 303 and status(db, item) == "approved"
    # without the cookie, a post is refused even with the CSRF value
    with httpx.Client(base_url=link.rstrip("/")) as fresh:
        r = fresh.post("/reject", data={"item_id": str(other), "csrf": csrf, "reason": "x"},
                       headers={"Origin": f"http://{host}"})
        assert r.status_code == 403 and status(db, other) == "pending"


def test_review_page_command_keeps_its_one_time_key():
    from flowstate.review_page import Page
    p = Page("x.db", 8770)
    assert p.launch_key and "key=" in p.launch_url()
    q = Page("x.db", 8770, open_link=True)
    assert q.launch_key is None and q.launch_url() == "http://127.0.0.1:8770/"


def test_brief_shows_the_local_link_only_when_something_waits(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    text = call(S, "start_here", {"project": "Alpha"})
    line = next(l for l in text.splitlines() if l.startswith("Review queue:"))
    assert line == "Review queue: 0 items waiting for the user's OK."
    pile(db, 1)
    text = call(S, "start_here", {"project": "Alpha"})
    line = next(l for l in text.splitlines() if l.startswith("Review queue:"))
    assert re.search(r"on the computer where FlowState runs at http://127\.0\.0\.1:\d+/ or in this chat when they ask "
                     r'\(manage_review action="walk"\)\.$', line), line
    assert "flowstatemcp.com" not in line


# ---------------------------------------------------------------- 3. the phone list
def test_phone_link_only_with_relay_keys_and_phone_on(backend, relay_dir, monkeypatch):
    S, db = backend, backend.backend.db_path
    pile(db, 1)
    assert approvals.review_link(db) is None                        # no relay
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": None, "fsa-2": None})
    assert approvals.review_link(db) is None                        # no keys pinned
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    link = approvals.review_link(db)
    assert link == "https://flowstatemcp.com/review/testbox"         # bookmarkable: no code in it
    assert approvals.review_link(db) == link
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces") == [(0,)]   # no code made for chat
    text = call(S, "start_here", {"project": "Alpha"})
    assert f"on their phone: {link}" in text
    out = call(S, "manage_review", {"action": "walk"}, app="grok")
    assert out["phone"] == link and link in out["how_to_decide"]
    approvals.set_value(db, "phone_approvals", "off")
    assert approvals.review_link(db) is None
    text = call(S, "start_here", {"project": "Alpha"})
    assert "flowstatemcp.com" not in text and "http://127.0.0.1:" in text
    out = call(S, "manage_review", {"action": "walk"}, app="grok")
    assert "phone" not in out and "flowstatemcp.com" not in json.dumps(out)


def _grant(key, db, **over):
    nonce = approvals.start_nonce(db)
    c = claims(0, nonce, action="open_review")
    c.update(over)
    return sign(key, c), nonce


@pytest.mark.parametrize("case,expect", [
    ("expired", "grant_expired"), ("wrong_aud", "wrong_device"), ("wrong_handle", "wrong_device"),
    ("wrong_action", "wrong_device"), ("approve_action", "wrong_device"), ("item_1", "wrong_item"),
    ("item_str", "wrong_item"), ("other_key", "bad_signature"), ("unknown_kid", "unknown_key"),
    ("unknown_nonce", "grant_used"), ("long_life", "bad_grant")])
def test_phone_grant_refused(backend, relay, case, expect):
    db = backend.backend.db_path
    key, t = relay, int(time.time())
    over = {"expired": {"iat": t - 400, "exp": t - 100},
            "wrong_aud": {"aud": "otherbox.fsmcp.com"}, "wrong_handle": {"handle": "otherbox"},
            "wrong_action": {"action": "open_everything"}, "approve_action": {"action": "approve_review"},
            "item_1": {"item": 1}, "item_str": {"item": "0"},
            "unknown_nonce": {"nonce": "A" * 43}, "long_life": {"exp": t + 3600}}.get(case, {})
    if case == "other_key":
        grant, _n = _grant(make_key()[0], db)
    elif case == "unknown_kid":
        nonce = approvals.start_nonce(db)
        grant = sign(key, claims(0, nonce, action="open_review"), kid="fsa-9")
    else:
        grant, _n = _grant(key, db, **over)
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_review_grant(db, grant)
    assert e.value.code == expect


def test_phone_grant_is_one_use(backend, relay):
    db = backend.backend.db_path
    grant, nonce = _grant(relay, db)
    assert approvals.verify_review_grant(db, grant)["action"] == "open_review"
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_review_grant(db, grant)
    assert e.value.code == "grant_used"
    assert approvals.start_nonce(db) != nonce                       # a fresh code next time
    # a code made for an approve link doesn't open the list
    item = pile(db, 1)[0]
    with pytest.raises(approvals.ApprovalError):
        approvals.verify_review_grant(db, sign(relay, claims(0, approvals.nonce_for(db, item),
                                                             action="open_review")))


def _phone_client(db):
    from flowstate.review_phone import routes
    return TestClient(Starlette(routes=routes(lambda: db)), base_url="https://testbox.fsmcp.com")


JSON_H = {"Origin": "https://testbox.fsmcp.com", "Content-Type": "application/json"}


def test_phone_list_session_cookie_and_decisions(backend, relay):
    S, db = backend, backend.backend.db_path
    a, b = pile(db, 2)
    c = _phone_client(db)
    shell = c.get("/review")
    assert "Waiting thing" not in shell.text and "<script src=\"/review/assets/review.js\"" in shell.text
    for h, v in (("x-frame-options", "DENY"), ("cache-control", "no-store"), ("referrer-policy", "no-referrer")):
        assert shell.headers[h] == v
    assert "frame-ancestors 'none'" in shell.headers["content-security-policy"]
    assert "script-src 'self'" in shell.headers["content-security-policy"]
    assert "replaceState" in c.get("/review/assets/review.js").text
    grant, _n = _grant(relay, db)
    assert c.post("/review/open", content=json.dumps({"grant": grant}),
                  headers={**JSON_H, "Origin": "https://evil.example"}).status_code == 403
    assert c.post("/review/open", data={"grant": grant}).status_code == 403          # not JSON
    r = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H)
    assert r.status_code == 200, r.text
    ck = r.headers["set-cookie"]
    low = ck.lower()
    assert ck.startswith("fs_review_phone=") and "httponly" in low and "secure" in low
    assert "samesite=strict" in low and "max-age=900" in low and "path=/review" in low
    body = r.json()
    assert [i["id"] for i in body["items"]] == [a, b] and body["items"][0]["summary"] == "Waiting thing 0"
    token = body["token"]
    again = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H)
    assert again.status_code == 403 and again.json()["error"] == "grant_used"
    # the session's page token must come with the cookie
    bad = c.post("/review/decide", content=json.dumps({"token": "x" * 43, "item_id": a,
                                                       "decision": "approve"}), headers=JSON_H)
    assert bad.status_code == 403 and status(db, a) == "pending"
    other = _phone_client(db)                                        # no cookie
    r = other.post("/review/decide", content=json.dumps({"token": token, "item_id": a,
                                                         "decision": "approve"}), headers=JSON_H)
    assert r.status_code == 403 and status(db, a) == "pending"
    r = c.post("/review/decide", content=json.dumps({"token": token, "item_id": a, "decision": "approve"}),
               headers=JSON_H)
    assert r.status_code == 200 and status(db, a) == "approved"
    assert [i["id"] for i in r.json()["items"]] == [b]
    r = c.post("/review/decide", content=json.dumps({"token": token, "item_id": b, "decision": "reject"}),
               headers=JSON_H)
    assert r.status_code == 200 and status(db, b) == "rejected"
    assert log(db) == [(a, "phone", "approved"), (b, "phone", "rejected")]
    assert _q(db, "SELECT decided_by FROM review_items WHERE id = ?", a) == [("phone (testbox)",)]


def test_phone_session_expires(backend, relay, monkeypatch):
    from flowstate import review_phone
    db = backend.backend.db_path
    a = pile(db, 1)[0]
    c = _phone_client(db)
    grant, _n = _grant(relay, db)
    token = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H).json()["token"]
    monkeypatch.setattr(review_phone, "_clock", lambda: time.time() + review_phone.SESSION_SECONDS + 1)
    r = c.post("/review/decide", content=json.dumps({"token": token, "item_id": a, "decision": "approve"}),
               headers=JSON_H)
    assert r.status_code == 403 and r.json()["error"] == "bad_token" and status(db, a) == "pending"


def test_phone_list_is_served_by_the_http_app(backend, relay):
    from flowstate.http_app import create_http_app
    app = create_http_app("x" * 32, db_path=backend.backend.db_path)
    paths = {getattr(r, "path", None) for r in app.app.routes}
    assert {"/review", "/review/open", "/review/decide"} <= paths


# ---------------------------------------------------------------- 4. (task 33) honest walk
def test_relay_connection_named_cursor_gets_the_plain_list_and_no_boxes(backend, relay_dir, monkeypatch):
    """The live bug (Oct 22, 2026): a relay connection calling itself "Cursor" and declaring
    elicitation got 20 boxes, each "declined" at once, and the answer said the user decided."""
    from flowstate import popup
    S, db = backend, backend.backend.db_path
    items = pile(db, 3)
    for via in ("relay", "public", "unknown"):
        monkeypatch.setattr(popup, "listener", lambda via=via: via)
        seen = []
        out = call(S, "manage_review", {"action": "walk"}, app="Cursor", seen=seen,
                   answers=[types.ElicitResult(action="decline")] * 3)
        assert seen == [], via                                         # zero forms sent
        assert out["note"] == waiting.NOT_SHOWN and "walked" not in out
        assert "decided these" not in json.dumps(out)
        assert out["waiting"] == 3 and len(out["items"]) == 3 and out["review_page"]
        assert ("relay" in out["how_to_decide"]) == (via == "relay")
        assert "on the computer where FlowState runs" in out["how_to_decide"]
    assert log(db) == [] and [status(db, i) for i in items] == ["pending"] * 3


def test_the_approvals_popup_is_refused_on_relay_connections(backend, relay_dir, monkeypatch):
    from flowstate import popup
    S, db = backend, backend.backend.db_path
    approvals.set_on(db, True)
    monkeypatch.setattr(popup, "listener", lambda: "relay")
    seen = []
    r = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"}, app="Cursor",
             answers=[acc(approve=True, confirm_name="Beta Project")], seen=seen)
    assert seen == [] and "flowstate approve" in r["next"]
    assert _q(db, "SELECT COUNT(*) FROM projects WHERE name = 'Beta Project'") == [(1,)]
    assert log(db) == []


def test_listener_is_set_by_the_server_never_by_a_header():
    from types import SimpleNamespace
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    from flowstate import popup
    from flowstate.http_app import create_http_app
    from flowstate.remote_auth import LISTENER_KEY, Gate
    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})
    c = TestClient(Gate(app, None, "/mcp", listener="relay"))
    c.get("/health", headers={"x-flowstate-listener": "lan", LISTENER_KEY: "lan"})
    assert seen[LISTENER_KEY] == "relay"
    assert create_http_app("x" * 32).listener == "lan"
    assert create_http_app("x" * 32, relay=True).listener == "relay"
    assert create_http_app(None, public_url="https://box.example").listener == "public"
    assert create_http_app(None, public_url="https://testbox.fsmcp.com", relay=True).listener == "relay"
    # popup.listener reads the marker from the request FlowState is answering
    assert popup.listener() == "stdio"
    for scope, want in (({LISTENER_KEY: "relay"}, "relay"), ({LISTENER_KEY: "lan"}, "lan"), ({}, "unknown")):
        tok = request_ctx.set(RequestContext(1, None, None, None, request=SimpleNamespace(scope=scope)))
        try:
            assert popup.listener() == want
        finally:
            request_ctx.reset(tok)


def test_the_relay_agent_marks_its_listener_as_relay(relay, monkeypatch):
    from flowstate import server as S
    from flowstate.relay import runner
    got = {}

    class Stop(Exception):
        pass

    def fake(*a, **kw):
        got.update(kw)
        raise Stop
    monkeypatch.setattr(S, "create_http_app", fake)
    with pytest.raises(Stop):
        asyncio.run(runner.run(port=0, ct_fetch=False))
    assert got.get("relay") is True and got.get("public_url") == "https://testbox.fsmcp.com"


def test_boxes_answered_faster_than_a_person_were_not_shown(backend, relay_dir, instant_answers):
    S, db = backend, backend.backend.db_path
    items = pile(db, 3)
    for answer in (types.ElicitResult(action="decline"), acc(approve=True), acc(approve=False),
                   types.ElicitResult(action="cancel")):
        seen = []
        out = call(S, "manage_review", {"action": "walk"}, seen=seen, answers=[answer] * 3)
        assert len(seen) == 1                                          # stopped at once
        assert out["note"] == waiting.NOT_SHOWN and "walked" not in out
        assert "faster than a person" in out["how_to_decide"] and len(out["items"]) == 3
    assert log(db) == [] and [status(db, i) for i in items] == ["pending"] * 3


def test_a_real_walk_then_a_too_fast_box(backend, relay_dir, monkeypatch):
    from flowstate import popup
    S, db = backend, backend.backend.db_path
    a, b, c = pile(db, 3)
    ticks = iter([0.0, 5.0, 10.0, 13.0, 20.0, 20.1])                   # 5 s, 3 s, then 0.1 s
    monkeypatch.setattr(popup, "_clock", lambda: next(ticks))
    out = call(S, "manage_review", {"action": "walk"},
               answers=[acc(approve=True), types.ElicitResult(action="decline"), acc(approve=True)])
    assert out["walked"] == [f"#{a}: approved", f"#{b}: skipped, still waiting"]
    assert out["note"].startswith("In the boxes the user approved 1, rejected 0 and skipped 1; "
                                  "only those answers were logged.")
    assert "that box was not shown and nothing more was decided" in out["note"]
    assert log(db) == [(a, "chat", "approved"), (b, "chat", "declined")]
    assert status(db, c) == "pending" and out["still_waiting"] == 2


def test_real_slow_answers_note_has_counts(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    a, b, c = pile(db, 3)
    out = call(S, "manage_review", {"action": "walk"},
               answers=[acc(approve=True), acc(approve=False), types.ElicitResult(action="decline")])
    assert out["note"] == ("In the boxes the user approved 1, rejected 1 and skipped 1; only those "
                           "answers were logged.")
    assert log(db) == [(a, "chat", "approved"), (b, "chat", "rejected"), (c, "chat", "declined")]


def test_the_approvals_popup_answered_too_fast_is_not_a_decision(backend, relay_dir, instant_answers):
    S, db = backend, backend.backend.db_path
    approvals.set_on(db, True)
    seen = []
    r = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"}, app="claude-code",
             answers=[acc(approve=True, confirm_name="Beta Project")], seen=seen)
    assert len(seen) == 1 and r["next"].startswith("No popup was shown")
    assert _q(db, "SELECT COUNT(*) FROM projects WHERE name = 'Beta Project'") == [(1,)]
    assert log(db) == []


def test_walk_and_list_page_through_102_items(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    ids = pile(db, 102)
    out = call(S, "manage_review", {"action": "walk"}, app="grok")
    assert out["waiting"] == 102 and len(out["items"]) == 20
    assert out["items"][0].startswith(f"#{ids[0]} ") and out["items"][-1].startswith(f"#{ids[19]} ")
    assert out["showing"] == f"20 of 102, oldest first: #{ids[0]} to #{ids[19]}"
    assert out["next_page"] == f'manage_review(action="walk", after={ids[19]})'
    seen, after, pages = [], None, 0
    while True:
        args = {"action": "walk", **({"after": after} if after else {})}
        out = call(S, "manage_review", args, app="grok")
        pages += 1
        seen += [int(x.split(" ")[0][1:]) for x in out["items"]]
        assert out["waiting"] == 102
        if "next_page" not in out:
            break
        after = int(out["next_page"].split("after=")[1].rstrip(")"))
    assert pages == 6 and seen == ids and len(out["items"]) == 2
    assert out["showing"] == f"2 of 102, oldest first: #{ids[100]} to #{ids[101]}"
    out = call(S, "manage_review", {"action": "walk", "after": ids[-1]}, app="grok")
    assert out["items"] == [] and out["note"] == f"Nothing more is waiting after #{ids[-1]}."
    # list pages the same way (its own limit)
    out = call(S, "manage_review", {"action": "list"})
    assert out["total"] == 102 and len(out["items"]) == 20 and "more" not in out
    assert out["next_page"] == f'manage_review(action="list", after={ids[19]})'
    out = call(S, "manage_review", {"action": "list", "after": ids[99], "limit": 50})
    assert [i["id"] for i in out["items"]] == ids[100:] and "next_page" not in out
    assert out["total"] == 102


# ---------------------------------------------------------------- 5. (task 33) bookmark link
def _start(c, **kw):
    return c.get("/review/start", follow_redirects=False, **kw)


def _open_nonces(db):
    return _q(db, "SELECT nonce FROM review_open_nonces WHERE used_at IS NULL AND expires_at > ?",
              approvals._ts())


def test_review_start_sends_a_fresh_code_back_to_the_site(backend, relay):
    from datetime import datetime
    db = backend.backend.db_path
    pile(db, 2)
    c = _phone_client(db)
    r = _start(c, params={"handle": "evil", "next": "https://evil.example"},
               headers={"Host": "evil.fsmcp.com", "X-Forwarded-Host": "evil.example"})
    assert r.status_code == 303
    m = re.fullmatch(r"https://flowstatemcp\.com/review/testbox\?n=([A-Za-z0-9_-]{43})", r.headers["location"])
    assert m, r.headers["location"]
    for h, v in (("cache-control", "no-store"), ("referrer-policy", "no-referrer"), ("x-frame-options", "DENY")):
        assert r.headers[h] == v
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    nonce = m.group(1)
    row = _q(db, "SELECT created_at, expires_at FROM review_open_nonces WHERE nonce = ?", nonce)[0]
    life = datetime.fromisoformat(row[1]) - datetime.fromisoformat(row[0])
    assert life.total_seconds() == 15 * 60
    assert _start(c).headers["location"] != r.headers["location"]       # fresh every time
    # the site signs a grant with it (after the user's Continue); it opens the list once
    grant = sign(relay, claims(0, nonce, action="open_review"))
    o = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H)
    assert o.status_code == 200 and len(o.json()["items"]) == 2 and o.json()["total"] == 2
    again = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H)
    assert again.status_code == 403 and again.json()["error"] == "grant_used"


def test_review_start_code_expires_after_15_minutes(backend, relay, monkeypatch):
    from datetime import timedelta
    db = backend.backend.db_path
    c = _phone_client(db)
    nonce = _start(c).headers["location"].split("?n=")[1]
    later = approvals._now() + timedelta(minutes=16)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    t = int(later.timestamp())
    grant = sign(relay, claims(0, nonce, action="open_review", iat=t, exp=t + 300))
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_review_grant(db, grant)
    assert e.value.code == "grant_used"


def test_review_start_cap_and_rate_limit_and_old_codes(backend, relay):
    from datetime import timedelta
    db = backend.backend.db_path
    c = _phone_client(db)
    made = [_start(c).headers["location"].split("?n=")[1] for _ in range(12)]
    assert sorted(n for (n,) in _open_nonces(db)) == sorted(made[2:])   # 10 open; the oldest expired
    with pytest.raises(approvals.ApprovalError):
        approvals.verify_review_grant(db, sign(relay, claims(0, made[0], action="open_review")))
    for _ in range(approvals.START_PER_HOUR - 12):
        assert _start(c).status_code == 303
    n = _q(db, "SELECT COUNT(*) FROM review_open_nonces")[0][0]
    r = _start(c)
    assert r.status_code == 429 and "Too many" in r.text and r.headers["cache-control"] == "no-store"
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces")[0][0] == n
    # an older release's 7-day chat code doesn't count against the start limits and still works
    old = "B" * 43
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO review_open_nonces (nonce, action, created_at, expires_at) VALUES "
                 "(?, 'open_review', ?, ?)", (old, approvals._ts(),
                                              approvals._ts(approvals._now() + timedelta(days=7))))
    conn.commit()
    conn.close()
    assert approvals.verify_review_grant(db, sign(relay, claims(0, old, action="open_review")))["nonce"] == old


@pytest.mark.parametrize("case", ["phone_off", "not_enrolled", "no_keys"])
def test_review_start_off_or_unavailable_shows_a_plain_page(backend, relay_dir, monkeypatch, case):
    from flowstate.relay.store import RelayStore
    db = backend.backend.db_path
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    if case != "not_enrolled":
        RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    if case == "phone_off":
        approvals.set_value(db, "phone_approvals", "off")
    if case == "no_keys":
        monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": None, "fsa-2": None})
    r = _start(_phone_client(db))
    assert r.status_code == 200 and "Phone review is off" in r.text and "flowstate approvals phone on" in r.text
    assert "location" not in r.headers and "<script" not in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces") == [(0,)]


def test_review_start_is_get_only(backend, relay):
    db = backend.backend.db_path
    c = _phone_client(db)
    assert c.head("/review/start").status_code == 405
    assert c.post("/review/start").status_code == 405
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces") == [(0,)]


def test_brief_and_walk_show_the_bare_bookmark_link(backend, relay):
    S, db = backend, backend.backend.db_path
    pile(db, 1)
    text = call(S, "start_here", {"project": "Alpha"})
    assert "on their phone: https://flowstatemcp.com/review/testbox or in this chat" in text and "?n=" not in text
    out = call(S, "manage_review", {"action": "walk"}, app="grok")
    assert out["phone"] == "https://flowstatemcp.com/review/testbox" and "?n=" not in json.dumps(out)
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces") == [(0,)]
