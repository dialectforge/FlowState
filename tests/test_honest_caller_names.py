"""Honest caller names (2.4.4, task 44). Where the recorded app name is only the app's own
claim (relay, and remote tokens / OAuth sign-ins on the owner's own tunnel), FlowState shows it
with the sign-in it came in on when the two differ ("Grok Bot (via Cursor)"); the own tunnel
records the app's own name like the relay does, with pause and own-item checks keyed to the
sign-in; the docs recommend one token per app. Temp databases only."""

import json
import re
import sqlite3
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from flowstate import activity, handoffs, history, identity, review, who
from flowstate import remote_auth as RA
from test_relay_app_label import http_calls, queued, suggest

ROOT = Path(__file__).resolve().parents[1]
TUNNEL = "https://tunnel.example.test"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def tunnel_calls(db, token, calls, client_name):
    """Tool calls over the real HTTP app on the public (own tunnel) listener."""
    from flowstate import http_app
    real = http_app.create_http_app

    def public(*a, **kw):
        kw.pop("relay", None)
        return real(*a, public_url=TUNNEL, **kw)
    http_app.create_http_app = public
    try:
        return http_calls(db, token, calls, client_name, relay=False)
    finally:
        http_app.create_http_app = real


@pytest.fixture
def cursor_token(backend):
    _cid, tok = RA.AuthStore().add_token_client("Cursor", list(RA.SCOPES))
    yield tok
    backend.backend.tracker = None


# ---------------------------------------------------------------- part 1: shown()
def test_shown_names_both_only_when_they_differ():
    s = identity.shown
    assert s("Grok Bot", "relay (signed in as Cursor)", "remote_token") == "Grok Bot (via Cursor)"
    assert s("Grok Bot", "tunnel (signed in as Cursor)", "oauth_client") == "Grok Bot (via Cursor)"
    assert s("Cursor", "relay (signed in as Cursor)", "remote_token") == "Cursor"
    assert s("cursor", "tunnel (signed in as Cursor)", "remote_token") == "cursor"
    assert s(identity.RELAY_UNKNOWN, "relay (signed in as Cursor)", "remote_token") == \
        "Relay (unknown app, via Cursor)"
    assert s(identity.TUNNEL_UNKNOWN, "tunnel (signed in as X)", "oauth_client") == \
        "Tunnel (unknown app, via X)"
    # stdio, per-app keys, LAN tokens and old rows: one name, as stored
    assert s("claude-code", "laptop", "self_reported") == "claude-code"
    assert s("claude-code", "laptop", "self_reported", on_machine=True) == "claude-code on laptop"
    assert s("My key", "ws", "app_key") == "My key"
    assert s("Cursor", "Cursor", "remote_token") == "Cursor"
    assert s("claude-code", None, None) == "claude-code"
    assert s(None, None, None, fallback="-") == "-"


def test_a_lan_app_cant_fake_a_via_with_its_workspace_header():
    fake = "relay (signed in as Cursor)"
    assert identity.via(fake, "shared_token") is None
    assert identity.via(fake, "self_reported") is None
    assert identity.shown("Grok Bot", fake, "shared_token") == "Grok Bot"
    # a per-app key's header workspace can't put a sign-in of its own in front (task 47)
    key = type("T", (), {"identity_source": "app_key", "client_name": "My key",
                         "workspace": None})()
    ident = identity.resolve("Grok Bot", True, key, {"x-flowstate-workspace": fake})
    assert identity.via(ident.machine, "app_key") == "My key"
    assert identity.shown("Grok Bot", ident.machine, "app_key") == "Grok Bot (via My key)"


# ---------------------------------------------------------------- part 2: own tunnel
def test_tunnel_write_carries_the_apps_own_name_and_the_sign_in(backend, cursor_token):
    db = backend.backend.db_path
    out = tunnel_calls(db, cursor_token, [suggest("Use Redis on the tunnel")], "Grok Bot")
    assert "queued_for_review" in out[0], out[0]
    assert queued(db, "Use Redis on the tunnel") == \
        [("Grok Bot", "tunnel (signed in as Cursor)", "remote_token")]


def test_tunnel_write_without_a_client_name_is_tunnel_unknown_app(backend, cursor_token):
    db = backend.backend.db_path
    tunnel_calls(db, cursor_token, [suggest("Tunnel nameless")], "")
    assert queued(db, "Tunnel nameless") == \
        [("Tunnel (unknown app)", "tunnel (signed in as Cursor)", "remote_token")]


def test_pausing_the_sign_in_name_stops_tunnel_writes(backend, cursor_token):
    db = backend.backend.db_path
    activity.set_paused(db, "Cursor", True, "test")
    out = tunnel_calls(db, cursor_token, [suggest("Paused tunnel write")], "Grok Bot")
    assert out[0]["error"] == "paused_by_user"
    assert queued(db, "Paused tunnel write") == []


def test_a_renamed_tunnel_app_cant_decide_its_own_item():
    item = {"id": 7, "app": "Grok Bot", "machine": "tunnel (signed in as Cursor)",
            "summary": "Use Redis for caching", "details": None}
    renamed = identity.resolve("Other Name", True,
                               type("T", (), {"identity_source": "remote_token",
                                              "client_name": "Cursor"})(), {}, listener="public")
    refused = review.own_item_refusal(item, renamed, "Use Redis for caching")
    assert refused and refused["error"] == "own_item"


def test_lan_remote_tokens_get_the_same_rule(backend, cursor_token):   # 2.4.4, task 47
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("LAN token too")], "Grok Bot", relay=False)
    assert queued(db, "LAN token too") == [("Grok Bot", "lan (signed in as Cursor)", "remote_token")]


def test_undo_take_back_is_keyed_to_the_sign_in_not_the_claimed_name():
    tok = type("T", (), {"identity_source": "remote_token", "client_name": "Cursor"})()
    via_cursor = identity.resolve("claude-code", True, tok, {}, listener="public")
    renamed = identity.resolve("Something Else", True, tok, {}, listener="public")
    other = identity.resolve("claude-code", True,
                             type("T", (), {"identity_source": "remote_token",
                                            "client_name": "Phone"})(), {}, listener="relay")
    local = identity.Identity("claude-code", None, "laptop", "self_reported")
    sc = identity.same_caller
    # a local app's record: a remote app calling itself the same name can't take it back
    assert sc("claude-code", "laptop", "self_reported", local)
    assert not sc("claude-code", "laptop", "self_reported", via_cursor)
    # a record made through the tunnel sign-in: that sign-in, whatever name it sends now
    row = ("claude-code", "tunnel (signed in as Cursor)", "remote_token")
    assert sc(*row, via_cursor) and sc(*row, renamed)
    assert not sc(*row, other) and not sc(*row, local)


def test_undo_refuses_a_tunnel_app_taking_back_a_local_apps_record(backend, cursor_token):
    db = backend.backend.db_path
    with identity.use(identity.Identity("claude-code", None, "laptop", "self_reported", "s0")):
        backend.handle_call("log_work", {"project": "Alpha", "kind": "todo",
                                         "summary": "Local todo"}, "claude-code", {})
    tid = _q(db, "SELECT id FROM todos WHERE title = 'Local todo'")[0][0]
    out = tunnel_calls(db, cursor_token, [("manage_history", {
        "action": "undo", "project": "Alpha", "record_type": "todo", "record_id": tid})],
        "claude-code")
    assert _q(db, "SELECT id FROM todos WHERE id = ?", tid), out
    assert "not_your_record" in json.dumps(out)


# ---------------------------------------------------------------- part 1: the pages
def test_review_page_activity_and_who_show_the_sign_in(backend, cursor_token):
    from flowstate.review_page import Page, create_app
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("Shown via relay")], "Grok Bot")
    tunnel_calls(db, cursor_token, [("log_work", {"project": "Alpha", "kind": "change",
                                                  "summary": "Tunnel change"})], "Grok Bot")
    p = Page(db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    assert "from Grok Bot (via Cursor)" in html                    # review queue item
    recent = html[html.index("<h2>Recent activity</h2>"):]
    assert "<td>Grok Bot (via Cursor)</td>" in recent               # settings Recent activity
    assert "Grok Bot (via Cursor)" in html[html.index("App pauses"):]   # apps list
    page = c.get("/?view=who").text
    assert "<td>Grok Bot (via Cursor)</td>" in page                 # who did what
    rows = who.recent(db)["rows"]
    assert any(r.get("via") == "Cursor" and r["app"] == "Grok Bot" for r in rows)


def test_cards_walk_and_phone_page_show_the_sign_in(backend, cursor_token):
    from flowstate import approve_page, review_cards, waiting
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("Card via relay")], "Grok Bot")
    it = next(i for i in review.list_items(db)["items"] if i["summary"] == "Card via relay")
    assert review_cards._who(it) == "Grok Bot (via Cursor)"
    assert approve_page.view(db, it["id"])["queued_by"] == "Grok Bot (via Cursor)"
    assert "Grok Bot (via Cursor) wants to" in approve_page.view(db, it["id"])["question"]
    msg, _form = waiting._form(it, 1, 1)
    assert "from Grok Bot (via Cursor)" in msg


def test_same_name_and_local_callers_show_one_name(backend, cursor_token):
    from flowstate import approve_page
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("Same name")], "Cursor")
    backend.handle_call("log_work", {"project": "Alpha", "kind": "decision", "summary": "Local",
                                     "suggested": True}, "claude-code", {})
    items = {i["summary"]: i for i in review.list_items(db)["items"]}
    assert approve_page.view(db, items["Same name"]["id"])["queued_by"] == "Cursor"
    assert approve_page.view(db, items["Local"]["id"])["queued_by"].startswith("claude-code")
    assert "via" not in approve_page.view(db, items["Local"]["id"])["queued_by"]


def test_relay_session_summary_names_the_sign_in(backend, cursor_token):
    from flowstate import session_contents
    db = backend.backend.db_path
    http_calls(db, cursor_token, [("log_work", {"project": "Alpha", "kind": "change",
                                                "summary": "Relay session work"})], "Grok Bot")
    if backend.backend.tracker is not None:   # the app's shutdown normally closed it already
        backend.backend.tracker.close_instance("test")
    it = next(i for i in review.list_items(db)["items"] if i["kind"] == "session_summary")
    assert (it["app"], it["machine"], it["identity_source"]) == \
        ("Grok Bot", "relay (signed in as Cursor)", "remote_token")
    line = session_contents.when_line(session_contents.for_item(db, it))
    assert "by Grok Bot (via Cursor), in Alpha." in line


def test_brief_handoff_line_shows_the_sign_in(backend, cursor_token):
    db = backend.backend.db_path
    http_calls(db, cursor_token, [("manage_handoffs", {"action": "leave", "project": "Alpha",
                                                       "where_i_was": "midway",
                                                       "next_step": "finish"})], "Grok Bot")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        lines = handoffs.brief_lines(conn, 1)
    finally:
        conn.close()
    assert any("Grok Bot (via Cursor)) Where I was: midway" in l for l in lines), lines


def test_old_rows_render_as_they_are():
    old = {"app": "Cursor", "machine": "Cursor", "identity_source": "remote_token"}
    assert identity.shown_row(old) == "Cursor"
    assert identity.shown_row({"app": "x"}) == "x"
    assert activity.app_label({"app": "Grok Bot", "via": ["Cursor"]}) == "Grok Bot (via Cursor)"
    assert activity.app_label({"app": "Cursor", "via": ["Cursor"]}) == "Cursor"


# ---------------------------------------------------------------- part 3: docs
DOCS = ["README.md", "AGENTS.md", "docs/REMOTE-ACCESS.md", "skills/flowstate/SKILL.md"]


def _text(rel):
    p = ROOT / rel
    return p.read_text(encoding="utf-8") if p.exists() else ""


def test_docs_dont_use_an_app_name_as_the_example_token():
    for rel in DOCS + [str(p.relative_to(ROOT)) for p in (ROOT / "docs").glob("*.md")]:
        text = _text(rel)
        assert not re.search(r'--name\s+"?Cursor"?', text), rel


def test_docs_recommend_one_token_per_app():
    for rel in ("AGENTS.md", "docs/REMOTE-ACCESS.md"):
        text = _text(rel)
        assert "one token per app" in text.lower(), rel
        assert "flowstate remote clients" in text or "remote clients" in text, rel


def test_brief_since_last_visit_shows_the_sign_in(backend, cursor_token):
    from test_since_last_visit import BRIEF, _since, run
    db = backend.backend.db_path
    run(backend, "claude-code", BRIEF)
    http_calls(db, cursor_token, [("log_work", {"project": "Alpha", "kind": "change",
                                                "summary": "Saved through the relay"})], "Grok Bot")
    [line] = _since(run(backend, "claude-code", BRIEF)[0])
    assert "Grok Bot (via Cursor)" in line, line
