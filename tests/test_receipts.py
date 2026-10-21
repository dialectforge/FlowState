"""2.4.0 write receipts. Every write answer carries a short receipt (record ids + a change
id). A write that timed out but saved later is reported on the connection's next answer.
An identical retry from the same session within the window returns the original receipt
instead of saving a copy (also while the original is still saving)."""

import asyncio
import json
import sqlite3
import time

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import receipts


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _j(r):
    return json.loads(r.content[0].text)


def session(S, steps, app="Cursor"):
    """Run `steps(c)` (an async function of one client connection) and return its result."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await steps(c)
    return asyncio.run(go())


LESSON = {"project": "Alpha", "kind": "learning", "summary": "Receipts make writes knowable"}


def _count(db, text=LESSON["summary"]):
    return _q(db, "SELECT COUNT(*) FROM learnings WHERE insight = ?", text)[0][0]


@pytest.fixture
def slow_saves(backend, monkeypatch):
    """log_work for a learning takes 1s; the call timeout is 0.3s."""
    S = backend
    monkeypatch.setenv("FLOWSTATE_CALL_TIMEOUT", "0.3")
    real = S.tools.log_learning

    def slow(*a, **k):
        time.sleep(1.0)
        return real(*a, **k)
    monkeypatch.setattr(S.tools, "log_learning", slow)
    return S


def test_every_write_carries_a_receipt_with_record_and_change_id(backend):
    S, db = backend, backend.backend.db_path

    async def steps(c):
        w = _j(await c.call_tool("log_work", LESSON))
        h = _j(await c.call_tool("manage_history", {"action": "list", "project": "Alpha",
                                                     "record_type": "learning"}))
        return w, h
    w, h = session(S, steps)
    rid = w["record"]["id"]
    assert w["receipt"]["saved"] == [f"learning #{rid}"]
    change = w["receipt"]["change"]
    assert len(change) == 12
    assert h["history"][0]["edit_id"] == change          # the change id is the history's
    assert _q(db, "SELECT status, records FROM run_writes WHERE change_id = ?", change) == \
        [("saved", json.dumps([f"learning:{rid}"]))]


def test_edit_receipt_names_the_edited_record(backend):
    async def steps(c):
        return _j(await c.call_tool("manage_todos", {"action": "update", "project": "Alpha",
                                                      "todo_id": 1, "status": "done"}))
    w = session(backend, steps)
    assert w["receipt"]["saved"] == ["todo #1"]


def test_timed_out_write_that_saved_is_reported_on_the_next_answer(slow_saves):
    S, db = slow_saves, slow_saves.backend.db_path

    async def steps(c):
        first = await c.call_tool("log_work", LESSON)
        await asyncio.sleep(1.4)                        # it finishes in the background
        nxt = await c.call_tool("manage_todos", {"action": "list", "project": "Alpha"})
        again = await c.call_tool("manage_todos", {"action": "list", "project": "Alpha"})
        return first, nxt, again
    first, nxt, again = session(S, steps)
    body = _j(first)
    assert first.isError and body["error"] == "flowstate_timeout"
    assert "next FlowState answer" in body["message"] and "don't repeat" in body["message"].lower()
    rid = _q(db, "SELECT id FROM learnings WHERE insight = ?", LESSON["summary"])[0][0]
    notes = [b.text for b in nxt.content[1:]]
    assert any("did save" in n and f"learning #{rid}" in n for n in notes), notes
    assert len(again.content) == 1                      # told once


def test_retry_while_the_first_is_still_saving_is_not_a_duplicate(slow_saves, monkeypatch):
    S, db = slow_saves, slow_saves.backend.db_path

    async def steps(c):
        first = await c.call_tool("log_work", LESSON)   # times out, keeps saving
        monkeypatch.setenv("FLOWSTATE_CALL_TIMEOUT", "5")
        retry = await c.call_tool("log_work", LESSON)   # waits for the first one instead
        return first, retry
    first, retry = session(S, steps)
    assert _j(first)["error"] == "flowstate_timeout"
    body = _j(retry)
    assert not retry.isError and body["already_saved"] is True, body
    assert body["receipt"]["saved"][0].startswith("learning #")
    time.sleep(0.2)
    assert _count(db) == 1


def test_identical_repeat_in_the_same_session_returns_the_original_receipt(backend):
    S, db = backend, backend.backend.db_path

    async def steps(c):
        return [_j(await c.call_tool("log_work", LESSON)) for _ in range(2)]
    a, b = session(S, steps)
    assert b["already_saved"] is True and b["receipt"] == a["receipt"]
    assert "not saved again" in b["note"]
    assert _count(db) == 1


def test_repeat_from_another_session_is_saved(backend):
    S, db = backend, backend.backend.db_path

    async def one(c):
        return _j(await c.call_tool("log_work", LESSON))
    a = session(S, one)
    b = session(S, one)                                 # a second window of the same app
    assert "already_saved" not in b and a["receipt"]["change"] != b["receipt"]["change"]
    assert _count(db) == 2


def test_repeat_after_the_window_is_saved(backend, monkeypatch):
    S, db = backend, backend.backend.db_path
    monkeypatch.setattr(receipts, "WINDOW_SECONDS", 0)

    async def steps(c):
        return [_j(await c.call_tool("log_work", LESSON)) for _ in range(2)]
    a, b = session(S, steps)
    assert "already_saved" not in b and _count(db) == 2


def test_different_content_is_never_a_duplicate(backend):
    S, db = backend, backend.backend.db_path

    async def steps(c):
        await c.call_tool("log_work", LESSON)
        return _j(await c.call_tool("log_work", {**LESSON, "details": "with more detail"}))
    b = session(S, steps)
    assert "already_saved" not in b and _count(db) == 2


def test_failed_write_has_no_receipt_and_can_be_retried(backend):
    S = backend

    async def steps(c):
        bad = {"project": "Alpha", "kind": "decision", "summary": "x", "replaces": 9999}
        return [await c.call_tool("log_work", bad) for _ in range(2)]
    a, b = session(S, steps)
    assert a.isError and b.isError
    assert _j(b)["error"] == "record_not_found"         # tried again, not "already saved"


def test_undo_twice_is_still_a_redo(backend):
    S, db = backend, backend.backend.db_path

    async def steps(c):
        await c.call_tool("manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1,
                                           "status": "done"})
        undo = {"action": "undo", "project": "Alpha", "record_type": "todo", "record_id": 1}
        return [_j(await c.call_tool("manage_history", undo)) for _ in range(2)]
    a, b = session(S, steps)
    assert "already_saved" not in b, b
    assert _q(db, "SELECT status FROM todos WHERE id = 1") == [("done",)]


def test_receipts_store_ids_and_a_hash_never_text(backend):
    S, db = backend, backend.backend.db_path

    async def steps(c):
        return _j(await c.call_tool("log_work", LESSON))
    session(S, steps)
    rows = _q(db, "SELECT * FROM run_writes")
    assert rows and LESSON["summary"] not in json.dumps(rows) and "Receipts" not in json.dumps(rows)
