"""Accountability (migration 012): who (app), what model, where (machine) on every recorded
change; per-app LAN keys (store + CLI); "Who did what" on the review page and through
history(action="who"). In-process only, temp DBs and temp settings folders. The HTTP
transports are in test_accountability_http.py."""

import asyncio
import json
import re
import socket
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import identity, migrations as M

AFFECTED = {   # table -> the columns 012 adds
    "record_history": ["app", "model", "machine", "identity_source"],
    "review_items": ["app", "model", "machine", "identity_source", "decided_app",
                     "decided_model", "decided_machine", "decided_identity_source"],
    "record_links": ["app", "model", "machine", "identity_source", "removed_app",
                     "removed_model", "removed_machine", "removed_identity_source"],
    "handoff_notes": ["app", "model", "machine", "identity_source", "picked_up_app",
                      "picked_up_model", "picked_up_machine", "picked_up_identity_source"],
    "project_rules": ["app", "model", "machine", "identity_source", "removed_app",
                      "removed_model", "removed_machine", "removed_identity_source"],
    "review_mode_log": ["app", "model", "machine", "identity_source"],
    "activity_log": ["model", "machine", "identity_source", "record_type", "record_id"],
}


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _cols(db, table):
    return [r[1] for r in _q(db, f'PRAGMA table_info("{table}")')]


def _call(S, calls, name="Test App"):
    """Run tool calls through the real MCP server in-process (the stdio code path)."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=name, version="1")) as c:
            return [await c.call_tool(n, a) for n, a in calls]
    return asyncio.run(go())


# ---------------------------------------------------------------- migration 012
@pytest.fixture
def pre_012_db(tmp_path, monkeypatch):
    """A database exactly as the server before this change left it (migrations 001-011)."""
    from flowstate.database import Database
    real = M.available
    monkeypatch.setattr(M, "available", lambda: [m for m in real() if m[0] < 12])
    db = tmp_path / "old.db"
    d = Database(db)
    a = d.create_project("Alpha", "First")
    b = d.create_project("Beta", "Second")
    c = d.create_component(a.id, "Core")
    d.create_component(b.id, "Web")
    d.add_todo(a.id, "Alpha todo", None, "high")
    monkeypatch.setattr(M, "available", real)
    assert M.pending(db) and M.pending(db)[0][1] == "accountability"
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        INSERT INTO record_history (edit_id, table_name, record_id, project_id, tool, old_row,
            new_row, client) VALUES ('e1', 'todos', 1, {a.id}, 'todo/update', '{{}}', '{{}}', 'Old App');
        INSERT INTO review_items (project_id, kind, summary, source, decided_at, decided_by, status)
            VALUES ({a.id}, 'todo', 'queued long ago', 'Old App', '2026-01-02 03:04:05', 'Old App', 'approved');
        INSERT INTO record_links (from_type, from_id, from_project_id, link_type, to_type, to_id,
            to_project_id, created_by, removed_at, removed_by)
            VALUES ('project', {b.id}, {b.id}, 'uses', 'component', {c.id}, {a.id}, 'Old App',
                    '2026-01-03 00:00:00', 'Other App');
        INSERT INTO handoff_notes (project_id, where_i_was, next_step, created_by, picked_up_at,
            picked_up_by) VALUES ({a.id}, 'half way', 'finish', 'Old App', '2026-01-04 00:00:00', 'Next App');
        INSERT INTO project_rules (project_id, rule, created_by) VALUES ({a.id}, 'Never push to main', 'Old App');
        INSERT INTO review_mode_log (scope, old_mode, new_mode, client, caller, user_request)
            VALUES ('install', NULL, 'types', 'Old App', 'stdio', 'the user asked');
        INSERT INTO activity_log (app, caller, tool, action, project_id, write, ok)
            VALUES ('Old App', 'stdio', 'todo', 'update', {a.id}, 1, 1);
    """)
    conn.commit()
    conn.close()
    return db


def _snapshot(db):
    out = {}
    for t in AFFECTED:
        old_cols = [c for c in _cols(db, t) if c not in AFFECTED[t]]
        out[t] = _q(db, f'SELECT {", ".join(old_cols)} FROM "{t}" ORDER BY rowid')
    return out


def test_migration_012_is_additive_and_keeps_every_row(pre_012_db, tmp_path):
    db = pre_012_db
    before = _snapshot(db)
    assert all(before[t] for t in AFFECTED), "every affected table has a row"
    for t, new in AFFECTED.items():
        assert not set(new) & set(_cols(db, t))
    other = {t: _q(db, f'SELECT * FROM "{t}" ORDER BY rowid')
             for t in ("projects", "components", "todos", "conversations", "learnings")}
    res = M.migrate(db, tmp_path / "bk")
    assert res["applied"][:1] == ["012_accountability"] and res["backup"]
    assert _snapshot(db) == before                       # counts and old values unchanged
    for t, new in AFFECTED.items():
        assert set(new) <= set(_cols(db, t))
        nulls = _q(db, f'SELECT {", ".join(new)} FROM "{t}"')
        assert all(v is None for row in nulls for v in row), t   # existing rows: NULL
    for t, rows in other.items():                         # v1 record tables untouched
        assert _q(db, f'SELECT * FROM "{t}" ORDER BY rowid') == rows
    assert _q(db, "PRAGMA integrity_check") == [("ok",)]
    assert M.migrate(db, tmp_path / "bk") == {"applied": [], "backup": None}


def test_migration_012_reruns_safely_and_old_writers_still_work(pre_012_db, tmp_path):
    db = pre_012_db
    M.migrate(db, tmp_path / "bk")
    # lost bookkeeping: running it again skips columns that are already there
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schema_migrations WHERE version = 12")
    conn.commit()
    conn.close()
    assert M.migrate(db, tmp_path / "bk")["applied"] == ["012_accountability"]
    # an older server writes with its own column lists: still fine, new columns NULL
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO record_links (from_type, from_id, from_project_id, link_type, "
                 "to_type, to_id, to_project_id, created_by) VALUES ('project', 2, 2, 'uses', "
                 "'project', 1, 1, 'v1.1 server')")
    conn.execute("INSERT INTO activity_log (app, tool, write, ok) VALUES ('old', 'search', 0, 1)")
    conn.commit()
    conn.close()
    assert _q(db, "SELECT app, identity_source FROM record_links ORDER BY id DESC LIMIT 1") \
        == [(None, None)]
    from flowstate import server as S
    S.init_backend(db)
    try:
        assert S.backend.error is None and S.backend.migration_error is None
    finally:
        S.backend.tracker = None


def test_recording_still_works_before_012(pre_012_db):
    """Writers check the columns, so a database 012 has not reached keeps working."""
    from flowstate import review, rules
    item = review.add_item(pre_012_db, 1, "todo", "still works", None, {}, "Cursor")
    assert item and rules.add(pre_012_db, 1, "Keep it short", "Cursor")["rule"]["id"]


# ---------------------------------------------------------------- identity: stdio
def test_stdio_identity_self_reported_with_workspace(backend, seeded_db, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_WORKSPACE", "ws-desk")
    monkeypatch.delenv("FLOWSTATE_MODEL", raising=False)
    S = backend
    out = _call(S, [("todo", {"action": "update", "todo_id": 1, "status": "in_progress",
                              "project": "Alpha"}),
                    ("log_work", {"project": "Alpha", "kind": "todo", "summary": "with model",
                                  "model": "model-one"})])
    assert not any(r.isError for r in out), [r.content[0].text for r in out]
    h = _q(seeded_db, "SELECT app, model, machine, identity_source FROM record_history")
    # the edit, then (2.4.0) the created todo, each with its own model
    assert h == [("Test App", None, "ws-desk", "self_reported"),
                 ("Test App", "model-one", "ws-desk", "self_reported")]
    act = _q(seeded_db, "SELECT tool, app, model, machine, identity_source, record_type "
                        "FROM activity_log ORDER BY id")
    assert act == [("todo", "Test App", None, "ws-desk", "self_reported", None),
                   ("log_work", "Test App", "model-one", "ws-desk", "self_reported", "todo")]


def test_stdio_machine_defaults_to_hostname_and_model_to_env(backend, seeded_db, monkeypatch):
    monkeypatch.delenv("FLOWSTATE_WORKSPACE", raising=False)
    monkeypatch.setenv("FLOWSTATE_MODEL", "env-model")
    _call(backend, [("rules", {"action": "add", "project": "Alpha", "rule": "No force push"})])
    assert _q(seeded_db, "SELECT app, model, machine, identity_source FROM project_rules") == \
        [("Test App", "env-model", identity.clean(socket.gethostname()), "self_reported")]
    # the argument wins over the environment
    _call(backend, [("handoff", {"action": "leave", "project": "Alpha", "where_i_was": "x",
                                 "next_step": "y", "model": "arg-model"})])
    assert _q(seeded_db, "SELECT model FROM handoff_notes") == [("arg-model",)]


def test_model_is_never_guessed(backend, seeded_db, monkeypatch):
    """No model argument, header or FLOWSTATE_MODEL: NULL, even when the app's own name
    looks like a model name."""
    monkeypatch.delenv("FLOWSTATE_MODEL", raising=False)
    _call(backend, [("log_work", {"project": "Alpha", "kind": "decision", "summary": "d1"}),
                    ("link", {"action": "add", "project": "Beta Project", "link_type": "uses",
                              "target_project": "Alpha", "target_component": "Core"})],
          name="claude-opus-desktop")
    rows = _q(seeded_db, "SELECT model FROM activity_log") + \
        _q(seeded_db, "SELECT model FROM record_links")
    assert rows and all(r == (None,) for r in rows)


def test_model_argument_with_a_secret_is_refused(backend, seeded_db):
    r = _call(backend, [("log_work", {"project": "Alpha", "kind": "todo", "summary": "x",
                                      "model": "ghp_" + "a" * 36})])[0]
    assert r.isError and json.loads(r.content[0].text)["error"] == "secret_detected"
    assert all(m is None for (m,) in _q(seeded_db, "SELECT model FROM activity_log"))


def test_review_queue_and_decisions_record_both_sides(backend, seeded_db):
    S = backend
    _call(S, [("log_work", {"project": "Alpha", "kind": "learning", "summary": "maybe",
                            "suggested": True, "model": "m-suggest"})], name="Suggester")
    item = _q(seeded_db, "SELECT id, app, model, identity_source FROM review_items")[0]
    assert item[1:] == ("Suggester", "m-suggest", "self_reported")
    _call(S, [("review_approve", {"project": "Alpha", "item_id": item[0],
                                  "user_request": "the user said: approve it"})], name="Approver")
    assert _q(seeded_db, "SELECT decided_app, decided_model, decided_identity_source "
                         "FROM review_items") == [("Approver", None, "self_reported")]


def test_auto_session_summary_is_marked_for_its_app(seeded_db, tmp_path):
    from flowstate.autosession import AutoSessions
    t = AutoSessions(seeded_db)
    with identity.use(identity.Identity("Someone Else", "m", "elsewhere", "app_key")):
        sid = t.touch("Session App", 1, "read:start_here")
        t.note(sid, "learning: a real save (a session with none files no item)")
        t.close_instance("shutdown")
    assert _q(seeded_db, "SELECT app, model, machine, identity_source FROM review_items "
                         "WHERE kind = 'session_summary'") == [("Session App", None, None, "self_reported")]


def test_resolve_rules():
    class Tok:
        def __init__(self, src, name="Key App", ws=None):
            self.identity_source, self.client_name, self.workspace = src, name, ws
    h = {"x-flowstate-model": "hdr-model", "x-flowstate-workspace": "hdr-ws"}
    r = identity.resolve
    assert r("Claimed", True, Tok("app_key", ws="key-ws"), h).as_dict() == \
        {"app": "Key App", "model": "hdr-model", "machine": "key-ws", "identity_source": "app_key"}
    assert r("Claimed", True, Tok("app_key"), h, "arg-model").machine == "hdr-ws"
    assert r("Claimed", True, Tok("app_key"), h, "arg-model").model == "arg-model"
    assert r("Claimed", True, Tok("shared_token", "owner (LAN token)"), h).as_dict() == \
        {"app": "Claimed", "model": "hdr-model", "machine": "hdr-ws",
         "identity_source": "shared_token"}
    assert r("Claimed", True, Tok("oauth_client", "claude.ai"), h).as_dict() == \
        {"app": "claude.ai", "model": "hdr-model", "machine": "claude.ai",
         "identity_source": "oauth_client"}
    assert r("Claimed", True, Tok("remote_token", "Phone"), {}).model is None
    assert identity.label("shared_token") == "shared token (legacy)"


# ---------------------------------------------------------------- keys: store + CLI
def test_keys_cli_add_list_revoke(tmp_path, monkeypatch, capsys):
    from flowstate import app_keys, cli
    from flowstate.remote_auth import AuthStore, TokenChecker
    auth = tmp_path / "config" / "remote-auth.db"
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(auth))
    assert cli.main(["keys", "add", "--name", "Cursor laptop", "--workspace", "laptop"]) == 0
    out = capsys.readouterr().out
    key = re.search(r"^(fs_lk_\S+)$", out, re.M).group(1)
    kid = re.search(r"added as (key_[0-9a-f]+)", out).group(1)
    assert cli.main(["keys", "add", "--name", "Grok", "--read-only"]) == 0
    capsys.readouterr()
    assert cli.main(["keys", "list"]) == 0
    listed = capsys.readouterr().out
    assert kid in listed and "'Cursor laptop'" in listed and "read-only" in listed
    assert "laptop" in listed and "fs_lk_" not in listed            # never the key
    assert key.encode() not in auth.read_bytes()                     # only a hash is kept
    store = AuthStore(auth)
    lan, public = TokenChecker(store, None, app_keys=True), TokenChecker(store, None)
    tok = lan.check(key)
    assert (tok.client_name, tok.identity_source, tok.workspace) == ("Cursor laptop", "app_key", "laptop")
    assert tok.scopes == ["flowstate:read", "flowstate:write"]
    assert public.check(key) is None                                 # LAN keys only
    assert cli.main(["keys", "revoke", "--id", kid]) == 0
    assert lan.check(key) is None
    assert cli.main(["keys", "revoke", "--id", kid]) == 1            # already revoked
    assert cli.main(["keys", "revoke", "--name", "Grok"]) == 0
    assert all(k["revoked_at"] for k in app_keys.list_keys(store))
    assert cli.main(["keys", "add", "--name", "  "]) == 1


def test_scanner_refuses_app_keys():
    from flowstate.secrets_scan import scan_text
    assert scan_text("key fs_lk_" + "B" * 43) == "FlowState access token"


def test_read_only_key_policy():
    from flowstate.remote_auth import refusal
    ro = ["flowstate:read"]
    assert refusal("log_work", {"project": "A"}, ro)[0] == "read_only_client"
    assert refusal("history", {"action": "who"}, ro) is None         # who is a read
    assert refusal("history", {"action": "undo"}, ro)[0] == "read_only_client"
    assert refusal("git", {"action": "status"}, ["flowstate:read", "flowstate:write"])[0] \
        == "not_allowed_remotely"


# ---------------------------------------------------------------- who did what
def _make_history(S, db):
    """Two apps edit, one with a model; returns nothing (rows are in the DB)."""
    _call(S, [("todo", {"action": "update", "todo_id": 1, "status": "in_progress",
                        "project": "Alpha", "model": "model-a"})], name="App A")
    _call(S, [("todo", {"action": "update", "todo_id": 2, "status": "done",
                        "project": "Beta Project"}),
              ("log_work", {"project": "Beta Project", "kind": "todo", "summary": "new one"})],
          name="App B")


def test_history_who_action(backend, seeded_db):
    S = backend
    _make_history(S, seeded_db)
    r = _call(S, [("history", {"action": "who"}),
                  ("history", {"action": "who", "by_app": "app a"}),
                  ("history", {"action": "who", "by_model": "model-a"}),
                  ("history", {"action": "who", "project": "Beta Project", "limit": 1}),
                  ("history", {"action": "who", "until": "2000-01-01"}),
                  ("history", {"action": "who", "since": "not a time"}),
                  ("history", {"action": "who", "project": "Nope"})])
    every, by_app, by_model, beta, old, bad, nope = (json.loads(x.content[0].text) for x in r)
    assert every["total"] >= 3 and every["rows"][0]["app"] == "App B"     # newest first
    assert {x["app"] for x in by_app["rows"]} == {"App A"}
    a = by_app["rows"][0]
    assert (a["model"], a["identity"], a["record_type"], a["record_id"]) == \
        ("model-a", "self-reported", "todo", 1)
    assert a["undo"] == {"record_type": "todo", "record_id": 1} and "status" in a["what"]
    assert [x["model"] for x in by_model["rows"]] == ["model-a"]
    assert beta["total"] == 2 and beta["pages"] == 2 and len(beta["rows"]) == 1
    assert beta["rows"][0]["what"].startswith("created todo")
    assert old["total"] == 0
    assert bad["error"] == "bad_time" and nope["error"] == "project_not_found"


@pytest.fixture
def page(backend, seeded_db):
    from starlette.testclient import TestClient
    from flowstate.review_page import HOST, Page, create_app
    p = Page(seeded_db, 8770)
    c = TestClient(create_app(p), base_url=f"http://{HOST}:8770", follow_redirects=False)
    r = c.get("/?key=" + p.launch_key)
    assert r.status_code == 303
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/").text).group(1)
    return p, c, csrf, seeded_db


def test_review_page_who_did_what_filters(page, backend):
    p, c, csrf, db = page
    _make_history(backend, db)
    html = c.get("/?view=who").text
    assert "Who did what" in html and "App A" in html and "App B" in html
    assert "model-a" in html and "self-reported" in html and "<script" not in html
    only_a = c.get("/?view=who&app=App+A").text
    assert "App A" in only_a and "App B" not in only_a
    assert "App A" not in c.get("/?view=who&project=2").text
    assert "App B" in c.get("/?view=who&model=&machine=&since=2000-01-01").text
    assert "Who did what (0)" in c.get("/?view=who&until=2000-01-01").text
    assert "not 'nonsense'" in c.get("/?view=who&since=nonsense").text.replace("&#x27;", "'")
    fresh = c.__class__(c.app, base_url=str(c.base_url))
    assert fresh.get("/?view=who").status_code == 403                # still needs the login


def test_review_page_undo_link_is_protected_and_uses_history(page, backend):
    p, c, csrf, db = page
    _make_history(backend, db)
    html = c.get("/?view=who&app=App+A").text
    assert html.count('action="/undo"') == 1 and 'name="record_id" value="1"' in html
    form = {"record_type": "todo", "record_id": "1", "project_id": "1"}
    assert c.post("/undo", data=form).status_code == 403                             # no CSRF
    assert c.post("/undo", data={**form, "csrf": csrf},
                  headers={"origin": "http://evil.example"}).status_code == 403      # cross-site
    assert _q(db, "SELECT status FROM todos WHERE id = 1") == [("in_progress",)]
    r = c.post("/undo", data={**form, "csrf": csrf})
    assert r.status_code == 303 and "view=who" in r.headers["location"]
    assert _q(db, "SELECT status FROM todos WHERE id = 1") == [("pending",)]
    undo = _q(db, "SELECT tool, app, identity_source FROM record_history ORDER BY id DESC LIMIT 1")
    assert undo == [("history/undo", "review-page", "self_reported")]
    # the undone edit has no Undo button any more; the undo itself can be undone (redo)
    after = c.get("/?view=who&app=App+A").text
    assert 'action="/undo"' not in after and "(undone)" in after
    assert 'action="/undo"' in c.get("/?view=who&app=review-page").text
    bad = c.post("/undo", data={"record_type": "nonsense", "record_id": "1", "project_id": "1",
                                "csrf": csrf})
    assert "Unknown+record+type" in bad.headers["location"] or \
        "Unknown%20record%20type" in bad.headers["location"]


def test_who_paging(backend, seeded_db):
    from flowstate import who
    S = backend
    for i in range(7):
        S.handle_call("todo", {"action": "update", "todo_id": 1, "status": "pending" if i % 2
                               else "in_progress", "project": "Alpha"}, "Pager")
    first = who.recent(seeded_db, app="Pager", per_page=3)
    last = who.recent(seeded_db, app="Pager", per_page=3, page=3)
    assert (first["total"], first["pages"], len(first["rows"]), len(last["rows"])) == (7, 3, 3, 1)
    assert first["rows"][0]["at_utc"] >= last["rows"][0]["at_utc"]
    # only the newest edit of the record offers undo
    assert [bool(r["undo"]) for r in first["rows"]] == [True, False, False]
