"""2.4.0: one line in start_here, "since you were last here": what other sessions saved in
this project since this app's previous brief (counts by kind plus who). After a long gap,
counts plus a pointer to manage_history. Nothing when nothing changed or on a first visit."""

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import ledger

TAG = "Since your last visit"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        out = conn.execute(sql, a).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def run(S, app, *calls):
    """One connection of `app`; returns the text of every answer."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return [(await c.call_tool(n, a)).content[0].text for n, a in calls]
    return asyncio.run(go())


BRIEF = ("start_here", {"project": "Alpha"})


def log(kind, summary):
    return ("log_work", {"project": "Alpha", "kind": kind, "summary": summary})


def _since(text):
    return [line for line in text.splitlines() if line.startswith(TAG)]


def test_first_visit_shows_nothing(backend):
    assert _since(run(backend, "Cursor", BRIEF)[0]) == []


def test_other_apps_work_is_counted_by_kind_and_who(backend):
    S = backend
    run(S, "Cursor", BRIEF)
    run(S, "Claude Desktop", log("decision", "Use WAL mode"), log("decision", "Keep one writer"),
        log("change", "Moved the DB"),
        ("manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1, "status": "done"}))
    run(S, "Grok", log("learning", "WAL needs shared memory"))
    [line] = _since(run(S, "Cursor", BRIEF)[0])
    assert line.startswith(TAG + " (")
    assert "2 decisions, 1 change, 1 learning, 1 todo" in line
    assert "Claude Desktop" in line and "Grok" in line and "Cursor" not in line.split("): ", 1)[1]


def test_nothing_changed_by_others_shows_nothing(backend):
    S = backend
    run(S, "Cursor", BRIEF, log("change", "my own change"))
    assert _since(run(S, "Cursor", BRIEF)[0]) == []


def test_another_window_of_the_same_app_counts_as_someone_else(backend, monkeypatch):
    S = backend
    from flowstate import identity
    run(S, "Cursor", BRIEF)
    monkeypatch.setattr(identity, "PROCESS_SESSION", "other-process")   # a second window
    run(S, "Cursor", log("todo", "from the other window"))
    monkeypatch.undo()
    [line] = _since(run(S, "Cursor", BRIEF)[0])
    assert "1 todo" in line and "another Cursor session" in line


def test_long_gap_gives_counts_and_a_pointer(backend):
    S, db = backend, backend.backend.db_path
    run(S, "Cursor", BRIEF)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
    _q(db, "UPDATE run_views SET at = ?", old)
    run(S, "Claude Desktop", log("decision", "a"), log("change", "b"), log("change", "c"))
    [line] = _since(run(S, "Cursor", BRIEF)[0])
    assert "3 saves by 1 app" in line and "manage_history(action=\"who\"" in line
    assert "decision" not in line


def test_the_line_is_capped_to_one_short_line(backend):
    S = backend
    run(S, "Cursor", BRIEF)
    for i in range(6):
        run(S, f"Some Very Long Application Name Number {i}",
            *(log(k, f"{k} {i}") for k in ("decision", "change", "problem", "learning", "todo")))
    text = run(S, "Cursor", BRIEF)[0]
    [line] = _since(text)
    assert len(line) <= ledger.SINCE_MAX and "\n" not in line
    assert "and 3 more" in line


def test_line_sits_inside_the_brief_cap(backend):
    S = backend
    run(S, "Cursor", BRIEF)
    run(S, "Claude Desktop", log("decision", "x"))
    text = run(S, "Cursor", ("start_here", {"project": "Alpha", "size": "quick"}))[0]
    assert _since(text) and len(text) <= 1500
