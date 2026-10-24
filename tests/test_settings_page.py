"""(task 39) The Settings section of the local review page (review_page.py).

Every user setting is listed with its value and changed there, on the page `flowstate
review-page` opens (launch-key session) only: CSRF, Origin, Host and session checks on every
route; a confirm page (single-use token bound to the session and the change, expiring) for
turning approvals off and relay chat approvals on; every change logged in review_mode_log as
"review-page" with identity fields. Read-only on the open-link page, absent on the tunnel page,
the phone pages and the relay. Relay rows greyed out (and their POSTs ignored) without the
relay. No MCP tool reads or writes any of it. Temp DBs and sandboxed folders only."""

import re
import sqlite3

import pytest
from starlette.testclient import TestClient

from flowstate import activity, approvals, review_mode, review_page, updates
from flowstate.review_page import HOST, Page, create_app

PORT = 8770
BASE = f"http://{HOST}:{PORT}"
ORIGIN = {"origin": BASE}
CSRF = re.compile(r"""name=["']csrf["'] value=["']([^"']+)["']""")
TOKEN = re.compile(r"""name=["']token["'] value=["']([^"']+)["']""")


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("FLOWSTATE_UPDATE_CHECK", raising=False)
    return tmp_path / "config" / "config.json"


@pytest.fixture
def with_relay(monkeypatch):
    monkeypatch.setattr(approvals, "relay_set_up", lambda: True)


@pytest.fixture
def without_relay(monkeypatch):
    monkeypatch.setattr(approvals, "relay_set_up", lambda: False)


@pytest.fixture
def page(backend, seeded_db, cfg):
    p = Page(seeded_db, PORT)
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    yield p, c, seeded_db


def login(p, c):
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    return CSRF.search(html).group(1), html


def post(c, path, csrf, **data):
    return c.post(path, data={"csrf": csrf, **data}, headers=ORIGIN)


def flash(r) -> str:
    assert r.status_code == 303, r.text
    from urllib.parse import parse_qs, urlparse
    return parse_qs(urlparse(r.headers["location"]).query).get("flash", [""])[0]


def log(db, scope=None):
    rows = _q(db, "SELECT scope, old_mode, new_mode, client, caller, user_request, app, machine, "
                  "identity_source FROM review_mode_log ORDER BY id")
    return [r for r in rows if scope is None or r[0] == scope]


def assert_logged(row, new):
    scope, _old, now, client, caller, words, app, machine, source = row
    assert now == new and client == "review-page" and caller == "review-page", row
    assert words == review_page.SETTING_WORDS
    assert app == "review-page" and machine and source == "self_reported"


# ---------------------------------------------------------------- what the section shows
def test_every_setting_renders_with_its_value(page, with_relay, monkeypatch):
    p, c, db = page
    monkeypatch.setenv("FLOWSTATE_IDLE_MINUTES", "45")
    approvals.set_on(db, True, 2)
    activity.record(db, "Cursor", "start_here", None, 1, False, True)
    activity.set_paused(db, "Cursor", True, "test")
    review_mode.set_mode(db, "everything", None, 1, "the user asked", "test", "test")
    _csrf, html = login(p, c)
    assert "<h2>Settings</h2>" in html
    for name in ("Review mode", "App pauses", "Approvals for deletes and turning review off",
                 "Apps that may show an approval popup", "Phone approval links",
                 "Chat approvals through the relay", "Updates by flowstate update",
                 "Daily update check", "Set in the app's config", "Set in your terminal",
                 "Recent changes"):
        assert name in html, name
    assert "Alpha: <b>everything</b>" in html                  # per-project review mode
    assert "paused: <b>Cursor</b>" in html and "(paused)" in html
    assert "<b>off</b> for the whole install." in html          # approvals install-wide
    assert "Beta Project: <b>on</b>" in html                   # ... and per project
    assert "claude-code" in html and "never get a popup" in html
    assert "<b>allowed</b>" in html
    assert "<code>FLOWSTATE_IDLE_MINUTES</code>" in html and "<b>45</b>" in html
    assert "FLOWSTATE_REVIEW_PORT" in html and "<script" not in html
    # the existing Review mode form and Apps forms are inside the section, same routes
    sec = html.split("<h2>Settings</h2>", 1)[1]
    assert 'action="/mode"' in sec and 'action="/app"' in sec


def test_each_change_works_and_is_logged_as_review_page(page, with_relay, cfg):
    p, c, db = page
    csrf, _ = login(p, c)
    activity.record(db, "Cursor", "start_here", None, 1, False, True)
    # approvals on (no confirm), install-wide and per project
    assert flash(post(c, "/settings/approvals", csrf, scope="", state="on")) \
        == "Approvals are now on for the whole install."
    assert approvals.is_on(db) and approvals.is_on(db, 1)
    assert_logged(log(db, "approvals")[-1], "on")
    assert "Alpha" in flash(post(c, "/settings/approvals", csrf, scope="1", state="on"))
    assert_logged(log(db, "approvals:project:1")[-1], "on")
    # relay chat approvals off (no confirm)
    assert "now off" in flash(post(c, "/settings/relay-chat", csrf, state="off"))
    assert_logged(log(db, approvals.RELAY_CHAT_KEY)[-1], "off")
    # phone links
    assert flash(post(c, "/settings/phone", csrf, state="off")) == "Phone approval links are off."
    assert approvals.phone_on(db) is False
    assert_logged(log(db, "phone_approvals")[-1], "off")
    # popup list: add, remove, and claude.ai stays refused
    assert flash(post(c, "/settings/popup", csrf, op="add", name="My Editor")) == "Added my editor."
    assert "my editor" in approvals.popup_clients(db)
    assert_logged(log(db, "popup_clients")[-1], "added my editor")
    post(c, "/settings/popup", csrf, op="remove", name="cursor")
    assert "cursor" not in approvals.popup_clients(db)
    msg = flash(post(c, "/settings/popup", csrf, op="add", name="claude-ai"))
    assert "never get a popup" in msg and not approvals.popup_allowed(db, "claude-ai", True)
    # self_update (config.json)
    assert "by hand" in flash(post(c, "/settings/self-update", csrf, state="off"))
    assert updates.self_update_allowed() is False and '"self_update": false' in cfg.read_text()
    assert_logged(log(db, "config:self_update")[-1], "by hand")
    post(c, "/settings/self-update", csrf, state="on")
    assert updates.self_update_allowed() and "self_update" not in cfg.read_text()
    # update check (config.json)
    post(c, "/settings/update-check", csrf, state="off")
    assert updates.is_on() is False
    assert_logged(log(db, "config:update_check")[-1], "off")
    # app pause, now also logged
    post(c, "/app", csrf, app="Cursor", paused="1")
    assert activity.is_paused(db, "Cursor")
    assert_logged(log(db, "app:Cursor")[-1], "paused")
    html = c.get("/").text
    recent = html.split("Recent changes", 1)[1]
    for label in ("Pause Cursor", "Daily update check", "Updates by flowstate update", "Popup apps"):
        assert label in recent, label


def test_review_mode_form_behaves_as_before(page):
    p, c, db = page
    csrf, _ = login(p, c)
    post(c, "/mode", csrf, mode="types", kind_decision="on", project_id="")
    m = review_mode.get(db)
    assert m["mode"] == "types" and m["kinds"] == ["decision"]
    row = _q(db, "SELECT client, user_request FROM review_mode_log")[-1]
    assert row[0] == "review-page" and "review page" in row[1]


def test_bad_values_change_nothing(page, with_relay):
    p, c, db = page
    csrf, _ = login(p, c)
    before = log(db)
    assert "Could not do that" in flash(post(c, "/settings/approvals", csrf, scope="", state="maybe"))
    assert "Could not do that" in flash(post(c, "/settings/approvals", csrf, scope="999", state="on"))
    assert "Could not do that" in flash(post(c, "/settings/approvals", csrf, scope="", state="clear"))
    assert "Could not do that" in flash(post(c, "/settings/self-update", csrf, state="yes"))
    assert "client name" in flash(post(c, "/settings/popup", csrf, op="add", name="  "))
    assert log(db) == before and not approvals.is_on(db)


# ---------------------------------------------------------------- refusals on every new route
ROUTES = {"approvals": {"scope": "", "state": "on"}, "relay-chat": {"state": "off"},
          "phone": {"state": "off"}, "popup": {"op": "add", "name": "x-app"},
          "self-update": {"state": "off"}, "update-check": {"state": "off"},
          "name": {"name": "Renamed"},
          "confirm": {"token": "x", "setting": "approvals", "scope": "", "state": "off"},
          "cancel": {"token": "x"}}


@pytest.mark.parametrize("name", sorted(ROUTES))
def test_csrf_origin_host_and_session_refused(page, with_relay, name):
    p, c, db = page
    csrf, _ = login(p, c)
    data, path = ROUTES[name], f"/settings/{name}"
    before = log(db)
    assert c.post(path, data=data, headers=ORIGIN).status_code == 403                 # no CSRF
    assert c.post(path, data={**data, "csrf": "wrong"}, headers=ORIGIN).status_code == 403
    for bad in ("http://evil.example", "null", f"http://{HOST}:9999"):
        assert c.post(path, data={**data, "csrf": csrf}, headers={"origin": bad}).status_code == 403
    assert c.post(path, data={**data, "csrf": csrf},
                  headers={**ORIGIN, "sec-fetch-site": "cross-site"}).status_code == 403
    other = TestClient(create_app(p), base_url="http://evil.example:8770")
    assert other.post(path, data={**data, "csrf": csrf}).status_code == 403            # Host
    stranger = TestClient(create_app(p), base_url=BASE)                                 # no cookie
    assert stranger.post(path, data={**data, "csrf": csrf}, headers=ORIGIN).status_code == 403
    assert log(db) == before and not approvals.is_on(db)
    assert approvals.phone_on(db) and updates.self_update_allowed() and updates.is_on()


# ---------------------------------------------------------------- the confirm step
def ask(c, csrf, path="/settings/approvals", **data):
    r = post(c, path, csrf, **data)
    assert r.status_code == 200, r.headers.get("location")
    assert "<script" not in r.text and "default-src 'none'" in r.headers["content-security-policy"]
    return TOKEN.search(r.text).group(1), r.text


def test_approvals_off_needs_the_confirm_page(page):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True)
    tok, html = ask(c, csrf, scope="", state="off")
    assert "Turn approvals off for the whole install?" in html and "Only do this if" in html
    assert approvals.is_on(db)                                  # nothing yet
    r = post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="", state="off")
    assert flash(r) == "Approvals are now off for the whole install."
    assert not approvals.is_on(db)
    assert_logged(log(db, "approvals")[-1], "off")
    # replay: the token was used up
    approvals.set_on(db, True)
    r = post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="", state="off")
    assert "no longer valid" in flash(r) and approvals.is_on(db)


def test_project_off_and_clear_need_it_too(page):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True, 1)
    tok, html = ask(c, csrf, scope="1", state="clear")
    assert "Remove Alpha&#x27;s own approvals setting?" in html
    post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="1", state="clear")
    assert not approvals.is_on(db, 1)
    assert log(db, "approvals:project:1")[-1][2] is None
    # clearing a project's own "off" while it's off: no confirm needed (as the command line)
    approvals.set_on(db, False, 2)
    assert "now off" in flash(post(c, "/settings/approvals", csrf, scope="2", state="clear"))


def test_relay_chat_on_needs_the_confirm_page(page, with_relay):
    p, c, db = page
    csrf, _ = login(p, c)
    tok, html = ask(c, csrf, "/settings/relay-chat", state="on")
    assert "This lets an AI connected through the FlowState relay approve" in html
    assert approvals.relay_chat_on(db) is False
    r = post(c, "/settings/confirm", csrf, token=tok, setting="relay_chat", scope="", state="on")
    assert "now on" in flash(r) and approvals.relay_chat_on(db)
    assert_logged(log(db, approvals.RELAY_CHAT_KEY)[-1], "on")
    # already on: turning it "on" again asks nothing; off never asks
    assert "now on" in flash(post(c, "/settings/relay-chat", csrf, state="on"))
    assert "now off" in flash(post(c, "/settings/relay-chat", csrf, state="off"))


def test_expired_confirm_changes_nothing(page, monkeypatch):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True)
    now = [1000.0]
    monkeypatch.setattr(review_page, "_clock", lambda: now[0])
    tok, _ = ask(c, csrf, scope="", state="off")
    now[0] += review_page.CONFIRM_SECONDS + 1
    r = post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="", state="off")
    assert "no longer valid" in flash(r) and approvals.is_on(db)


def test_wrong_session_confirm_changes_nothing(page):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True)
    tok, _ = ask(c, csrf, scope="", state="off")
    # a second browser session (made like the launch key's) can't use the first one's token
    sess2 = p.new_session()
    c2 = TestClient(create_app(p), base_url=BASE, follow_redirects=False,
                    cookies={review_page.COOKIE: sess2})
    r = c2.post("/settings/confirm", data={"csrf": p.sessions[sess2], "token": tok,
                                           "setting": "approvals", "scope": "", "state": "off"},
                headers=ORIGIN)
    assert "no longer valid" in flash(r) and approvals.is_on(db)
    # ... and the try used it up for the first session as well
    r = post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="", state="off")
    assert "no longer valid" in flash(r) and approvals.is_on(db)


def test_wrong_change_confirm_changes_nothing(page, with_relay):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True)
    approvals.set_on(db, True, 1)
    tok, _ = ask(c, csrf, scope="1", state="off")                # confirm for Alpha only
    for wrong in ({"setting": "approvals", "scope": "", "state": "off"},    # whole install
                  {"setting": "relay_chat", "scope": "", "state": "on"}):   # another setting
        r = post(c, "/settings/confirm", csrf, token=tok, **wrong)
        assert "no longer valid" in flash(r)
        tok, _ = ask(c, csrf, scope="1", state="off")
    assert approvals.is_on(db) and approvals.is_on(db, 1) and not approvals.relay_chat_on(db)
    r = post(c, "/settings/confirm", csrf, token="made-up", setting="approvals", scope="1", state="off")
    assert "no longer valid" in flash(r) and approvals.is_on(db, 1)


def test_cancel_drops_the_token(page):
    p, c, db = page
    csrf, _ = login(p, c)
    approvals.set_on(db, True)
    tok, html = ask(c, csrf, scope="", state="off")
    assert "No, keep it as it is" in html
    assert flash(post(c, "/settings/cancel", csrf, token=tok)) == "Cancelled; nothing changed."
    r = post(c, "/settings/confirm", csrf, token=tok, setting="approvals", scope="", state="off")
    assert "no longer valid" in flash(r) and approvals.is_on(db)


# ---------------------------------------------------------------- without the relay
def test_relay_rows_greyed_out_and_posts_ignored_without_the_relay(page, without_relay):
    p, c, db = page
    csrf, html = login(p, c)
    for title in ("Phone approval links", "Chat approvals through the relay"):
        row = html.split(f"<h4>{title}</h4>", 1)[0].rsplit("<div class='set", 1)[1]
        assert row.startswith(" unused'"), title
        block = html.split(f"<h4>{title}</h4>", 1)[1].split("</div>", 1)[0]
        assert "Only used with the FlowState relay (not set up here)." in block
        assert block.count(" disabled>") == 2
    before = log(db)
    assert flash(post(c, "/settings/relay-chat", csrf, state="on")) == review_page.NO_RELAY
    assert flash(post(c, "/settings/phone", csrf, state="off")) == review_page.NO_RELAY
    assert log(db) == before and not approvals.relay_chat_on(db) and approvals.phone_on(db)
    # everything else works the same
    assert "now on" in flash(post(c, "/settings/approvals", csrf, scope="", state="on"))
    assert "Added" in flash(post(c, "/settings/popup", csrf, op="add", name="my-editor"))


def test_relay_rows_enabled_with_the_relay(page, with_relay):
    p, c, _db = page
    _csrf, html = login(p, c)
    assert "set unused" not in html and "not set up here" not in html
    assert " disabled>" not in html


def test_relay_set_up_follows_the_enrolment(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    assert approvals.relay_set_up() is False
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    assert approvals.relay_set_up() is True


def test_relay_disappearing_before_the_confirm_changes_nothing(page, monkeypatch):
    p, c, db = page
    csrf, _ = login(p, c)
    monkeypatch.setattr(approvals, "relay_set_up", lambda: True)
    tok, _ = ask(c, csrf, "/settings/relay-chat", state="on")
    monkeypatch.setattr(approvals, "relay_set_up", lambda: False)
    r = post(c, "/settings/confirm", csrf, token=tok, setting="relay_chat", scope="", state="on")
    assert flash(r) == review_page.NO_RELAY and not approvals.relay_chat_on(db)


# ---------------------------------------------------------------- open-link page: read-only
def test_open_link_page_shows_settings_read_only(backend, seeded_db, cfg, with_relay):
    db = seeded_db
    p = Page(db, PORT, open_link=True, settings="read")
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    html = c.get("/").text
    csrf = CSRF.search(html).group(1)
    sec = html.split("<h2>Settings</h2>", 1)[1]
    assert "open <code>flowstate review-page</code>" in sec
    for route in ("approvals", "relay-chat", "phone", "popup", "self-update", "update-check",
                  "name"):
        assert f"/settings/{route}" not in sec, route              # no forms for these
    assert 'action="/mode"' in sec                                 # review mode as before
    before = log(db)
    for name, data in ROUTES.items():
        r = post(c, f"/settings/{name}", csrf, **data)
        if name != "cancel":
            assert flash(r) == review_page.READ_ONLY, name
    assert log(db) == before and not approvals.is_on(db) and updates.self_update_allowed()
    assert approvals.phone_on(db) and "x-app" not in approvals.popup_clients(db)
    # review mode and app pauses still work there, as before
    post(c, "/mode", csrf, mode="everything", project_id="")
    assert review_mode.get(db)["mode"] == "everything"


def test_waiting_starts_the_open_link_page_read_only():
    import inspect
    from flowstate import waiting
    assert 'settings="read"' in inspect.getsource(waiting._start)
    assert Page("x.db", 8770, open_link=True).settings == "read"
    assert Page("x.db", 8770).settings == "edit"
    assert 'settings="edit"' in inspect.getsource(review_page.serve)


# ---------------------------------------------------------------- never remote
def test_tunnel_page_has_no_settings(seeded_db, tmp_path):
    from test_review_tunnel import URL, _app, page_csrf, sign_in
    from flowstate import server as S
    from flowstate.remote_auth import AuthStore
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase("correct horse battery staple")
    try:
        with TestClient(_app(seeded_db, store.path), base_url=URL, follow_redirects=False) as c:
            sign_in(c)
            csrf, r = page_csrf(c)
            assert "<h2>Settings</h2>" not in r.text and "/settings/" not in r.text
            assert "Review mode" in r.text                       # its existing blocks stay
            for name, data in ROUTES.items():
                for path in (f"/review/tunnel/settings/{name}", f"/settings/{name}"):
                    resp = c.post(path, data={**data, "csrf": csrf}, headers={"Origin": URL})
                    assert resp.status_code in (401, 404), (path, resp.status_code)
            assert not approvals.is_on(seeded_db) and updates.self_update_allowed()
    finally:
        S.backend.tracker = None


def test_tunnel_page_object_renders_no_settings(seeded_db):
    from flowstate import review_tunnel
    import inspect
    assert 'settings="off"' in inspect.getsource(review_tunnel.routes)
    p = Page(seeded_db, 0, base="/review/tunnel")
    assert p.settings == "off" and "<h2>Settings</h2>" not in p.render("csrf")


def test_relay_and_phone_apps_have_no_settings_routes(seeded_db, tmp_path):
    """The relay and phone pages (review_phone.py, approve_page.py) and the HTTP / public
    listeners: no route takes a setting."""
    from flowstate.http_app import create_http_app
    from flowstate import approve_page, review_phone
    from flowstate import server as S
    try:
        for kw in ({"relay": True}, {"public_url": "https://tunnel.example.com",
                                     "auth_db": tmp_path / "a.db"}, {}):
            app = create_http_app("o" * 32, db_path=seeded_db, allow_master_token=True, **kw)
            with TestClient(app, base_url="https://tunnel.example.com") as c:
                for name, data in ROUTES.items():
                    for path in (f"/settings/{name}", f"/review/settings/{name}",
                                 f"/approve/settings/{name}"):
                        r = c.post(path, data=data, headers={"Origin": "https://tunnel.example.com"})
                        assert r.status_code in (401, 404, 405), (kw, path, r.status_code)
    finally:
        S.backend.tracker = None
    for mod in (review_phone, approve_page):
        import inspect
        src = inspect.getsource(mod)
        assert "render_settings" not in src and "set_relay_chat" not in src
        assert "change_popup_clients" not in src and "set_self_update" not in src
    assert not approvals.is_on(seeded_db)


# ---------------------------------------------------------------- no secrets
def test_no_secret_in_the_page(page, with_relay, monkeypatch, tmp_path):
    p, c, db = page
    token = "tok_" + "Z9" * 20
    monkeypatch.setenv("FLOWSTATE_TOKEN", token)
    monkeypatch.setenv("FLOWSTATE_MODEL", "sk-ant-api03-" + "A" * 40)   # looks like a key
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    from flowstate import app_keys
    from flowstate.remote_auth import AuthStore
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase("correct horse battery staple")
    _kid, key = app_keys.add(store, "Phone app")
    _csrf, html = login(p, c)
    for secret in (token, "sk-ant-api03", "fake-device-token-for-tests", p.launch_key or "x" * 40):
        assert secret not in html, secret
    assert key not in html and "correct horse" not in html
    assert "FLOWSTATE_TOKEN" not in html and "passphrase" not in html.split("Settings", 1)[1].lower() \
        .replace("remote access (passphrase, app tokens)", "")
