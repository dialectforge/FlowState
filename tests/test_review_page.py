"""Review page (Phase D4h): 127.0.0.1 only, one-time key -> cookie, CSRF on every form,
Host check; approve / edit / reject, set review mode, pause apps; all as the user.
Starlette test client against a temp DB only."""

import re
import sqlite3

import pytest
from starlette.testclient import TestClient

from flowstate import activity, review, review_mode
from flowstate.review_page import HOST, Page, create_app

PORT = 8770
BASE = f"http://{HOST}:{PORT}"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


@pytest.fixture
def page(backend, seeded_db):
    p = Page(seeded_db, PORT)
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    yield p, c, seeded_db


def _login(p, c):
    r = c.get("/?key=" + p.launch_key)
    assert r.status_code == 303 and "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=strict" in r.headers["set-cookie"].lower()
    html = c.get("/").text
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1), html


def test_needs_key_once_and_right_host(page):
    p, c, _db = page
    assert c.get("/").status_code == 403
    assert c.get("/?key=wrong").status_code == 403
    key = p.launch_key
    _login(p, c)
    fresh = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    assert fresh.get("/?key=" + key).status_code == 403          # one time only
    other = TestClient(create_app(p), base_url="http://evil.example:8770")
    assert other.get("/").status_code == 403                      # DNS rebinding blocked
    r = c.get("/")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store" and "<script" not in r.text


def test_csrf_and_origin_required(page):
    p, c, db = page
    csrf, _ = _login(p, c)
    item = review.add_item(db, 1, "todo", "From the queue", None, {}, "test")
    assert c.post("/approve", data={"item_id": item}).status_code == 403
    assert c.post("/approve", data={"item_id": item, "csrf": csrf},
                  headers={"origin": "http://evil.example"}).status_code == 403
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]


def test_approve_with_edit_reject_and_html_escaping(page):
    p, c, db = page
    csrf, _ = _login(p, c)
    a = review.add_item(db, 1, "todo", "<b>bold</b> idea", None, {"priority": "high"}, "Grok Build")
    b = review.add_item(db, 1, "learning", "Not true", None, {}, "Grok Build")
    html = c.get("/").text
    assert "&lt;b&gt;bold&lt;/b&gt;" in html and "<b>bold</b>" not in html
    r = c.post("/approve", data={"item_id": a, "csrf": csrf, "summary": "Edited idea", "details": ""})
    assert r.status_code == 303 and "Approved" in r.headers["location"].replace("+", " ")
    assert _q(db, "SELECT title, priority FROM todos WHERE title = 'Edited idea'") == [("Edited idea", "high")]
    c.post("/reject", data={"item_id": b, "csrf": csrf, "reason": "wrong"})
    rows = _q(db, "SELECT id, status, decided_by, reject_reason, user_request FROM review_items ORDER BY id")
    assert rows[0][1:3] == ("approved", "review-page") and "review page" in rows[0][4]
    assert rows[1][1:4] == ("rejected", "review-page", "wrong")


def test_mode_and_pause_from_the_page(page):
    p, c, db = page
    csrf, _ = _login(p, c)
    c.post("/mode", data={"csrf": csrf, "mode": "types", "kind_decision": "on", "project_id": ""})
    m = review_mode.get(db)
    assert m["mode"] == "types" and m["kinds"] == ["decision"]
    log = _q(db, "SELECT client, user_request FROM review_mode_log")
    assert log[0][0] == "review-page" and "review page" in log[0][1]
    activity.record(db, "Cursor", "start_here", None, 1, False, True)
    c.post("/app", data={"csrf": csrf, "app": "Cursor", "paused": "1"})
    assert activity.is_paused(db, "Cursor")
    assert "(paused)" in c.get("/").text


def test_never_bound_beyond_loopback():
    import inspect
    from flowstate import review_page, server, waiting
    assert review_page.HOST == "127.0.0.1"
    assert "_bind(port)" in inspect.getsource(review_page.serve)          # waiting._bind:
    assert "s.bind((HOST, p))" in inspect.getsource(waiting._bind)       # 127.0.0.1 only
    assert "review_page" not in inspect.getsource(server.create_http_app)


# ---------------------------------------------------------------- (task 38) the shortcut's command
def test_review_page_command_reuses_the_page_already_open_for_this_database(backend, seeded_db, monkeypatch):
    import socket
    import webbrowser
    import uvicorn
    from flowstate import review_page, waiting
    opened, ran = [], []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: ran.append(sockets))
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen(1)
    port = busy.getsockname()[1]
    try:
        # a FlowState here already serves this database's page on that port: open it, start nothing
        monkeypatch.setattr(waiting, "_ping", lambda p, db: p == port)
        review_page.serve(seeded_db, port, open_browser=True)
        assert opened == [f"http://127.0.0.1:{port}/"] and ran == []
        # something else holds the port: the page starts on another free port, 127.0.0.1 only
        monkeypatch.setattr(waiting, "_ping", lambda p, db: False)
        review_page.serve(seeded_db, port, open_browser=False)
        assert len(ran) == 1
        host, other = ran[0][0].getsockname()
        assert host == "127.0.0.1" and other != port
        ran[0][0].close()
    finally:
        busy.close()
