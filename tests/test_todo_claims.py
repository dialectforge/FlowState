"""To-do claims with an expiry (2.3.0, migration 013): claim / renew / release / handoff on
manage_todos, what a second agent sees (refusal, brief, next, list), no hard lock (override
warning), done / cancelled clears, read-only scope, the v2.2.0 -> 2.3.0 migration, and a race.
In-process only, temp databases only; claims.utcnow is frozen where time matters."""

import asyncio
import json
import sqlite3
import threading
from datetime import datetime, timedelta

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import claims, identity, migrations as M

T0 = datetime(2026, 10, 21, 12, 0, 0)


class Clock:
    def __init__(self, monkeypatch, start=T0):
        self.now = start
        monkeypatch.setattr(claims, "utcnow", lambda: self.now)

    def advance(self, minutes: float):
        self.now += timedelta(minutes=minutes)


@pytest.fixture
def clock(monkeypatch):
    return Clock(monkeypatch)


@pytest.fixture
def S(backend, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_WORKSPACE", "mac-mini")
    return backend


def call(S, name, args, app="Cursor"):
    """One tool call through the real MCP server in-process, as app `app` (stdio path).
    Returns (is_error, parsed JSON or text)."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    text = r.content[0].text
    try:
        return r.isError, json.loads(text)
    except ValueError:
        return r.isError, text


def todo(S, args, app="Cursor"):
    return call(S, "manage_todos", {"project": "Alpha", **args}, app)


def ok(res):
    err, body = res
    assert not err, body
    return body


def fail(res, code):
    err, body = res
    assert err and body["error"] == code, body
    return body


def _add(S, title, prio="normal"):
    return ok(todo(S, {"action": "add", "title": title, "priority": prio}))["id"]


def _rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- claim / refuse / renew
def test_claim_then_second_claimer_is_refused_with_holder(S, clock):
    got = ok(todo(S, {"action": "claim", "todo_id": 1}))
    assert got["claimed"] and not got["renewed"] and got["expires_in"] == "15m"
    assert got["expires_at"] == "2026-10-21 12:15:00"
    body = fail(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"), "claimed_by_other")
    assert "claimed by Cursor on mac-mini" in body["message"]
    assert "expires in 15m" in body["message"] and 'action="next"' in body["message"]
    assert body["holder"] == "Cursor on mac-mini"
    row = _rows(S.backend.db_path, "SELECT app, machine, session, claimed_at, expires_at "
                                   "FROM todo_claims WHERE todo_id = 1")
    assert row == [("Cursor", "mac-mini", identity.PROCESS_SESSION, "2026-10-21 12:00:00",
                    "2026-10-21 12:15:00")]


def test_same_claimer_reclaim_renews_and_keeps_claimed_at(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 10}))
    clock.advance(5)
    got = ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 30}))
    assert got["renewed"] and got["expires_at"] == "2026-10-21 12:35:00"
    assert _rows(S.backend.db_path, "SELECT claimed_at FROM todo_claims") == [("2026-10-21 12:00:00",)]


def test_expiry_lets_a_new_claimer_in(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 1}))
    clock.advance(0.5)
    fail(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"), "claimed_by_other")
    clock.advance(1)
    got = ok(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"))
    assert got["claimed"] and not got["renewed"]
    assert _rows(S.backend.db_path, "SELECT app FROM todo_claims") == [("Claude Code",)]
    who = ok(call(S, "manage_history", {"action": "who", "project": "Alpha"}))["rows"]
    assert any("took over an expired claim by Cursor on mac-mini" in r["what"] for r in who)


def test_renew_extends_own_claim_and_fails_otherwise(S, clock):
    body = fail(todo(S, {"action": "renew", "todo_id": 1}), "not_claimed")
    assert 'action=\\"claim\\"' in json.dumps(body) or "claim" in body["message"]
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    clock.advance(10)
    got = ok(todo(S, {"action": "renew", "todo_id": 1, "minutes": 30}))
    assert got["expires_at"] == "2026-10-21 12:40:00" and got["expires_in"] == "30m"
    fail(todo(S, {"action": "renew", "todo_id": 1}, app="Claude Code"), "claimed_by_other")


def test_minutes_bounds(S):
    for bad in (0, 121, -5):
        fail(todo(S, {"action": "claim", "todo_id": 1, "minutes": bad}), "bad_minutes")
    for bad in ("soon", True, 2.5):
        with pytest.raises(claims.ClaimError):
            claims._minutes(bad)
    assert claims._minutes(None) == 15 and claims._minutes("30") == 30
    assert ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 120}))["expires_in"] == "2h"
    assert ok(todo(S, {"action": "renew", "todo_id": 1, "minutes": 1}))["expires_in"] == "1m"


def test_claim_needs_an_open_todo_in_this_project(S):
    fail(todo(S, {"action": "claim"}), "todo_id_required")
    fail(todo(S, {"action": "claim", "todo_id": 999}), "record_not_found")
    fail(todo(S, {"action": "claim", "todo_id": 2}), "project_mismatch")   # Beta's todo
    ok(todo(S, {"action": "update", "todo_id": 1, "status": "done"}))
    fail(todo(S, {"action": "claim", "todo_id": 1}), "todo_closed")


def test_two_sessions_of_the_same_app_are_told_apart(S, clock):
    a = identity.Identity("Cursor", None, "mac-mini", "self_reported", "sess-a")
    b = identity.Identity("Cursor", None, "mac-mini", "self_reported", "sess-b")
    db, pid = S.backend.db_path, 1
    with identity.use(a):
        claims.claim(db, pid, 1)
    with identity.use(b):
        with pytest.raises(claims.ClaimError) as e:
            claims.claim(db, pid, 1)
        assert "Cursor on mac-mini (another session)" in e.value.payload["message"]
    with identity.use(a):
        assert claims.claim(db, pid, 1)["renewed"]


# ---------------------------------------------------------------- release / handoff
def test_release_only_your_own(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    body = fail(todo(S, {"action": "release", "todo_id": 1}, app="Claude Code"), "not_your_claim")
    assert "Cursor on mac-mini" in body["message"]
    got = ok(todo(S, {"action": "release", "todo_id": 1}))
    assert got["released"] is True
    again = ok(todo(S, {"action": "release", "todo_id": 1}))
    assert again["released"] is False and "nothing to release" in again["note"]
    ok(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"))


def test_handoff_drops_claim_and_leaves_a_note(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    fail(todo(S, {"action": "handoff", "todo_id": 1, "where_i_was": "x"}, app="Claude Code"),
         "not_your_claim")
    got = ok(todo(S, {"action": "handoff", "todo_id": 1, "where_i_was": "Login form done",
                           "next_step": "Wire up the API call"}))
    assert got["released"] is True and got["handoff_id"]
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]
    notes = ok(call(S, "manage_handoffs", {"action": "list", "project": "Alpha"}))["handoffs"]
    assert notes[0]["where_i_was"] == "Todo #1 Alpha todo: Login form done"
    assert notes[0]["next_step"] == "Wire up the API call"
    assert notes[0]["app"] == "Cursor" and notes[0]["machine"] == "mac-mini"
    _e, brief = call(S, "start_here", {"project": "Alpha"}, app="Claude Code")
    assert "Todo #1 Alpha todo: Login form done" in brief and "claimed by" not in brief
    ok(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"))


def test_handoff_defaults_when_no_text_given(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    ok(todo(S, {"action": "handoff", "todo_id": 1}))
    n = ok(call(S, "manage_handoffs", {"action": "list", "project": "Alpha"}))["handoffs"][0]
    assert n["where_i_was"].startswith("Todo #1 Alpha todo:")
    assert "manage_todos action=claim" in n["next_step"]


def test_handoff_text_is_secret_scanned(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    err, body = todo(S, {"action": "handoff", "todo_id": 1,
                              "where_i_was": "token " + "ghp_" + "aB3" * 12})
    assert err and body["error"] == "secret_detected"
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(1,)]


# ---------------------------------------------------------------- done / cancelled, override
def test_done_by_holder_clears_claim_without_warning(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    got = ok(todo(S, {"action": "update", "todo_id": 1, "status": "done"}))
    assert "claim_warning" not in got and got["status"] == "done"
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]
    assert _rows(S.backend.db_path, "SELECT event FROM todo_claim_events ORDER BY id") == \
        [("claim",), ("cleared",)]


def test_cancel_by_someone_else_warns_and_clears(S):
    tid = _add(S, "Second")
    ok(todo(S, {"action": "claim", "todo_id": tid}))
    got = ok(todo(S, {"action": "update", "todo_id": tid, "status": "cancelled"}, app="Claude Code"))
    assert "claimed by Cursor on mac-mini" in got["claim_warning"]
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]


def test_override_warning_and_history(S):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    got = ok(todo(S, {"action": "update", "todo_id": 1, "priority": "low"}, app="Claude Code"))
    assert got["priority"] == "low"                       # no hard lock: saved
    assert "claimed by Cursor on mac-mini" in got["claim_warning"]
    assert _rows(S.backend.db_path, "SELECT app FROM todo_claims") == [("Cursor",)]   # still held
    blocked = ok(todo(S, {"action": "block", "todo_id": 1, "blocked_by": [_add(S, "Dep")]},
                      app="Claude Code"))
    assert "claim_warning" in blocked
    # the holder itself gets no warning
    assert "claim_warning" not in ok(todo(S, {"action": "update", "todo_id": 1,
                                                   "description": "more"}))
    who = ok(call(S, "manage_history", {"action": "who", "project": "Alpha"}))["rows"]
    events = {(r["event"], r["app"]) for r in who}
    assert {("todo_claim", "Cursor"), ("todo_override", "Claude Code")} <= events
    ok(todo(S, {"action": "release", "todo_id": 1}))
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    ok(todo(S, {"action": "handoff", "todo_id": 1}))
    kinds = [r["event"] for r in ok(call(S, "manage_history",
                                         {"action": "who", "project": "Alpha", "limit": 50}))["rows"]]
    assert {"todo_release", "todo_handoff", "todo_override"} <= set(kinds)


# ---------------------------------------------------------------- what others see
def test_next_skips_others_claims_but_lists_them(S, clock):
    first = _add(S, "Fix login", "critical")
    second = _add(S, "Write docs", "critical")
    ok(todo(S, {"action": "claim", "todo_id": first}))
    clock.advance(6)
    theirs = ok(todo(S, {"action": "next"}, app="Claude Code"))
    assert first not in [i["id"] for i in theirs["next"]]
    assert theirs["next"][0]["id"] == second
    assert theirs["claimed_by_others"] == \
        [f"#{first} Fix login (claimed by Cursor on mac-mini, expires in 9m)"]
    mine = ok(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"))
    assert mine["claimed"]
    own = ok(todo(S, {"action": "next"}, app="Claude Code"))
    assert own["next"][0]["id"] == 1 and own["next"][0]["claim"] == "yours, expires in 15m"
    holder = ok(todo(S, {"action": "next"}))
    assert holder["next"][0] == {**holder["next"][0], "id": first, "claim": "yours, expires in 9m"}
    clock.advance(10)                                       # Cursor's claim expired
    after = ok(todo(S, {"action": "next"}, app="Claude Code"))
    assert "claimed_by_others" not in after
    assert first in [i["id"] for i in after["next"]]


def test_next_and_list_unchanged_without_claims(S):
    import flowstate.tasks as tasks
    got = ok(todo(S, {"action": "next"}))
    assert got == json.loads(json.dumps(tasks.what_next(S.backend.db_path, 1, 5)))
    assert "claimed_by_others" not in got and all("claim" not in i for i in got["next"])
    assert all("claim" not in r for r in ok(todo(S, {"action": "list"})))


def test_list_shows_claim_on_each_row(S, clock):
    tid = _add(S, "Second")
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    rows = {r["id"]: r for r in ok(todo(S, {"action": "list"}, app="Claude Code"))}
    assert rows[1]["claim"] == "claimed by Cursor on mac-mini, expires in 15m"
    assert "claim" not in rows[tid]
    compact = {r["id"]: r for r in ok(todo(S, {"action": "list", "compact": True}))}
    assert compact[1]["claim"] == "claimed by you, expires in 15m"


def test_brief_shows_claim_note_and_hides_expired(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1}))
    clock.advance(6)
    _e, brief = call(S, "start_here", {"project": "Alpha"}, app="Claude Code")
    assert "#1 Alpha todo (claimed by Cursor on mac-mini, expires in 9m)" in brief
    _e, own = call(S, "start_here", {"project": "Alpha"})
    assert "#1 Alpha todo (claimed by you, expires in 9m)" in own
    clock.advance(10)
    _e, later = call(S, "start_here", {"project": "Alpha"}, app="Claude Code")
    assert "claimed by" not in later and "#1 Alpha todo" in later


def test_brief_keeps_its_size_with_many_claims(S, clock):
    from flowstate.brief import SIZES
    for i in range(40):
        tid = _add(S, f"Task number {i} with a reasonably long title to fill the line up", "urgent")
        ok(todo(S, {"action": "claim", "todo_id": tid}, app=f"Agent {i % 3}"))
    for size in ("quick", "standard", "deep"):
        _e, brief = call(S, "start_here", {"project": "Alpha", "size": size}, app="Claude Code")
        assert len(brief) <= SIZES[size]["cap"], size
        assert "claimed by Agent" in brief


# ---------------------------------------------------------------- permissions
def test_read_only_scope_cannot_claim(S, monkeypatch):
    from flowstate.remote_auth import SCOPE_READ, SCOPE_WRITE, refusal
    from flowstate import surface
    for act in ("claim", "renew", "release", "handoff"):
        name, args = surface.translate("manage_todos", {"action": act, "project": "Alpha",
                                                        "todo_id": 1})
        assert refusal(name, args, [SCOPE_READ])[0] == "read_only_client", act
        assert refusal(name, args, [SCOPE_READ, SCOPE_WRITE]) is None
    name, args = surface.translate("manage_todos", {"action": "next", "project": "Alpha"})
    assert refusal(name, args, [SCOPE_READ]) is None
    # through the server, as a read-only HTTP / relay caller
    monkeypatch.setattr(S, "_caller", lambda: ("remote-app", [SCOPE_READ]))
    fail(todo(S, {"action": "claim", "todo_id": 1}), "read_only_client")
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]
    monkeypatch.setattr(S, "_caller", lambda: ("remote-app", [SCOPE_READ, SCOPE_WRITE]))
    ok(todo(S, {"action": "claim", "todo_id": 1}))


def test_claim_is_a_labelled_write_that_needs_a_project(S):
    from flowstate import tool_labels
    acts = tool_labels.LABELS["manage_todos"][1]
    assert {"claim", "renew", "release", "handoff"} <= set(acts["additive"])
    body = fail(call(S, "todo", {"action": "claim", "todo_id": 1}), "project_required")
    assert "project" in body["message"]


# ---------------------------------------------------------------- migration from v2.2.0
@pytest.fixture
def v220_db(tmp_path, monkeypatch):
    """A database as FlowState 2.2.0 left it (migrations 001-012), with data."""
    from flowstate.database import Database
    real = M.available
    monkeypatch.setattr(M, "available", lambda: [m for m in real() if m[0] < 13])
    db = tmp_path / "v220.db"
    d = Database(db)
    a = d.create_project("Alpha", "First")
    d.create_component(a.id, "Core")
    d.add_todo(a.id, "Old todo", None, "high")
    d.add_todo(a.id, "Older todo", None, "low")
    monkeypatch.setattr(M, "available", real)
    assert [n for _v, n, _p in M.pending(db)] == ["todo_claims"]
    return db


def test_migration_from_v220_is_additive(v220_db, tmp_path):
    db = v220_db
    before = {t: _rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in ("projects", "todos")}
    cols = _rows(db, 'PRAGMA table_info("todos")')
    res = M.migrate(db, tmp_path / "bk")
    assert res["applied"] == ["013_todo_claims"] and res["backup"]
    assert {t: _rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in before} == before
    assert _rows(db, 'PRAGMA table_info("todos")') == cols          # v1 table untouched
    names = {r[0] for r in _rows(db, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"todo_claims", "todo_claim_events"} <= names
    assert _rows(db, "PRAGMA integrity_check") == [("ok",)]
    assert M.migrate(db, tmp_path / "bk") == {"applied": [], "backup": None}
    # lost bookkeeping: running it again is harmless
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schema_migrations WHERE version = 13")
    conn.commit()
    conn.close()
    assert M.migrate(db, tmp_path / "bk")["applied"] == ["013_todo_claims"]


def test_upgraded_v220_db_works_with_the_server(v220_db, monkeypatch):
    from flowstate import server as srv
    monkeypatch.setenv("FLOWSTATE_WORKSPACE", "mac-mini")
    srv.init_backend(v220_db)
    try:
        assert srv.backend.error is None and srv.backend.migration_error is None
        ok(todo(srv, {"action": "claim", "todo_id": 1}))
        _e, brief = call(srv, "start_here", {"project": "Alpha"}, app="Claude Code")
        assert "#1 Old todo (claimed by Cursor on mac-mini" in brief
    finally:
        srv.backend.tracker = None


def test_server_without_013_still_works(v220_db, monkeypatch):
    """If the migration could not run, everything else behaves as in 2.2.0."""
    from flowstate import server as srv
    monkeypatch.setattr(srv, "_migrate", lambda path: None)
    srv.init_backend(v220_db)
    try:
        fail(todo(srv, {"action": "claim", "todo_id": 1}), "claims_unavailable")
        assert ok(todo(srv, {"action": "next"}))["next"][0]["id"] == 1
        assert ok(todo(srv, {"action": "list"}))
        ok(todo(srv, {"action": "update", "todo_id": 1, "status": "done"}))
        _e, brief = call(srv, "start_here", {"project": "Alpha"})
        assert "Older todo" in brief
    finally:
        srv.backend.tracker = None


# ---------------------------------------------------------------- race
def test_race_two_connections_exactly_one_wins(S):
    db = S.backend.db_path
    ids = [_add(S, f"Race {i}") for i in range(15)]
    for tid in ids:
        barrier = threading.Barrier(2)
        results = {}

        def worker(app):
            ident = identity.Identity(app, None, "mac-mini", "self_reported", app)
            with identity.use(ident):
                barrier.wait()
                try:
                    results[app] = claims.claim(db, 1, tid)
                except claims.ClaimError as e:
                    results[app] = e.payload
        threads = [threading.Thread(target=worker, args=(a,)) for a in ("Agent A", "Agent B")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        wins = [a for a, r in results.items() if r.get("claimed")]
        losses = [r for r in results.values() if r.get("error") == "claimed_by_other"]
        assert len(wins) == 1 and len(losses) == 1, results
        assert _rows(db, "SELECT app FROM todo_claims WHERE todo_id = ?", tid) == [(wins[0],)]
        assert _rows(db, "SELECT COUNT(*) FROM todo_claim_events WHERE todo_id = ? AND "
                         "event = 'claim'", tid) == [(1,)]


def test_session_comes_from_the_mcp_session_header_over_http():
    class Tok:
        identity_source, client_name, workspace = "app_key", "Cursor", "mac-mini"
    a = identity.resolve("x", http=True, token=Tok(), headers={"mcp-session-id": "abc123"})
    b = identity.resolve("x", http=True, token=Tok(), headers={"mcp-session-id": "def456"})
    assert (a.session, b.session) == ("abc123", "def456")
    assert a.as_dict() == b.as_dict() == {"app": "Cursor", "model": None, "machine": "mac-mini",
                                          "identity_source": "app_key"}   # logs unchanged
    assert identity.resolve("Cursor").session == identity.PROCESS_SESSION
