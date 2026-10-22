"""2.4.0: one database commit per save (dbconn.py). The record, its edit history, receipt,
run-ledger row, session and activity-log rows commit together, or not at all."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import activity, dbconn, receipts


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _session(S, steps, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await steps(c)
    return asyncio.run(go())


def _counts(db) -> dict:
    tables = ("todos", "learnings", "record_history", "run_writes", "activity_log",
              "auto_sessions", "sessions")
    out = {t: _q(db, f"SELECT COUNT(*) FROM {t}")[0][0] for t in tables}
    out["session_state"] = _q(db, "SELECT session_id, last_activity, call_count, events "
                                  "FROM auto_sessions ORDER BY session_id")
    out["saved_writes"] = _q(db, "SELECT COUNT(*) FROM run_writes WHERE status = 'saved'")[0][0]
    return out


@pytest.mark.parametrize("step", ["receipt", "activity"])
def test_a_failing_bookkeeping_step_leaves_nothing_of_the_save(backend, monkeypatch, step):
    S = backend
    db = S.backend.db_path

    def broken(*a, **k):
        raise sqlite3.OperationalError("disk I/O error (forced by the test)")

    async def steps(c):
        await c.call_tool("start_here", {"project": "Alpha"})
        await c.call_tool("log_work", {"project": "Alpha", "kind": "learning",
                                       "summary": "warm-up lesson"})
        before = _counts(db)
        if step == "receipt":   # after the record, its history row and the session
            monkeypatch.setattr(receipts, "_records", broken)
        else:                   # the last step of the save
            monkeypatch.setattr(activity, "record", broken)
        r = await c.call_tool("log_work", {"project": "Alpha", "kind": "todo",
                                           "summary": "never half saved"})
        return before, r
    before, r = _session(S, steps)
    assert r.isError and json.loads(r.content[0].text)["error"] == "tool_failed"
    after = _counts(db)
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE title = ?", "never half saved")[0][0] == 0
    for t in ("todos", "learnings", "record_history", "auto_sessions", "sessions",
              "session_state", "saved_writes"):
        assert after[t] == before[t], t
    # what is left is the bookkeeping of the failure itself, outside the rolled-back save
    assert after["run_writes"] == before["run_writes"] + 1
    assert _q(db, "SELECT status, records FROM run_writes ORDER BY id DESC LIMIT 1") ==         [("failed", "[]")]


def test_a_retry_after_a_rolled_back_save_saves(backend, monkeypatch):
    """The duplicate check only remembers committed saves."""
    S = backend
    db = S.backend.db_path
    real, calls = receipts._records, []

    def once(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("forced")
        return real(*a, **k)
    monkeypatch.setattr(receipts, "_records", once)
    args = {"project": "Alpha", "kind": "todo", "summary": "second time lucky"}

    async def steps(c):
        return [await c.call_tool("log_work", args) for _ in range(2)]
    first, second = _session(S, steps)
    assert first.isError
    body = json.loads(second.content[0].text)
    assert "already_saved" not in body and body["record"]["id"]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE title = ?", args["summary"])[0][0] == 1


def test_each_save_commits_once(backend, monkeypatch):
    S = backend
    commits: list = []
    real = sqlite3.connect

    def counting(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(lambda sql: commits.append(sql)
                             if sql.strip().upper().startswith(("COMMIT", "END")) else None)
        return c

    async def steps(c):
        await c.call_tool("start_here", {"project": "Alpha"})
        monkeypatch.setattr(sqlite3, "connect", counting)
        out = {}
        for kind in ("learning", "change", "todo", "decision", "problem"):
            commits.clear()
            r = await c.call_tool("log_work", {"project": "Alpha", "kind": kind,
                                               "summary": f"one commit {kind}"})
            assert not r.isError, r.content[0].text
            assert json.loads(r.content[0].text)["receipt"]["saved"]
            out[kind] = len(commits)
        return out
    assert _session(S, steps) == {k: 1 for k in ("learning", "change", "todo", "decision",
                                                 "problem")}
    db = S.backend.db_path
    rows = _q(db, "SELECT COUNT(*) FROM activity_log WHERE tool = 'log_work'")[0][0]
    assert rows == 5   # one activity row per save, written in the save


# ---------------------------------------------------------------- dbconn itself
def _db(tmp_path):
    p = tmp_path / "t.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t (v TEXT)")
    c.commit()
    c.close()
    return p


def _rows(p):
    return [r[0] for r in _q(p, "SELECT v FROM t ORDER BY rowid")]


def test_a_part_rolls_back_alone_and_the_save_commits_once(tmp_path):
    p = _db(tmp_path)
    with dbconn.save(p):
        a = dbconn.connect(p)
        a.execute("INSERT INTO t VALUES ('kept')")
        a.commit()
        a.close()
        b = dbconn.connect(p)
        b.execute("INSERT INTO t VALUES ('rolled back')")
        b.rollback()
        b.execute("INSERT INTO t VALUES ('closed without commit')")
        b.close()
        c = dbconn.connect(p, isolation_level=None)
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO t VALUES ('explicit')")
        c.execute("COMMIT")
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO t VALUES ('explicit rollback')")
        c.execute("ROLLBACK")
        c.close()
        assert _rows(p) == []           # nothing on disk before the save commits
        r = dbconn.connect(p)
        assert [x[0] for x in r.execute("SELECT v FROM t ORDER BY rowid")] == ["kept", "explicit"]
        r.close()
    assert _rows(p) == ["kept", "explicit"]


def test_a_failing_save_leaves_nothing_and_runs_nothing_after(tmp_path):
    p = _db(tmp_path)
    seen = []
    with pytest.raises(RuntimeError):
        with dbconn.save(p):
            a = dbconn.connect(p)
            a.execute("INSERT INTO t VALUES ('x')")
            a.commit()
            a.close()
            dbconn.after(p, seen.append)
            raise RuntimeError("boom")
    assert _rows(p) == [] and seen == [False]
    dbconn.after(p, seen.append)   # outside a save: at once
    assert seen == [False, True]


def test_a_part_left_open_fails_the_save(tmp_path):
    p = _db(tmp_path)
    with pytest.raises(dbconn.SaveBroken):
        with dbconn.save(p):
            a = dbconn.connect(p)
            a.execute("INSERT INTO t VALUES ('never committed nor closed')")
    assert _rows(p) == []


def test_reads_take_no_lock_until_the_first_write(tmp_path):
    p = _db(tmp_path)
    with dbconn.save(p):
        r = dbconn.connect(p)
        r.execute("SELECT * FROM t").fetchall()
        other = sqlite3.connect(p, timeout=0)
        other.execute("INSERT INTO t VALUES ('other writer')")   # not blocked
        other.commit()
        w = dbconn.connect(p)
        w.execute("INSERT INTO t VALUES ('mine')")
        w.commit()
        with pytest.raises(sqlite3.OperationalError):   # now the save holds the write lock
            other.execute("INSERT INTO t VALUES ('blocked')")
        other.rollback()
        other.close()
    assert _rows(p) == ["other writer", "mine"]


def test_another_database_is_not_joined(tmp_path):
    p, q = _db(tmp_path), tmp_path / "other.db"
    with dbconn.save(p):
        c = dbconn.connect(q)
        assert isinstance(c, sqlite3.Connection)
        c.execute("CREATE TABLE u (v)")
        c.commit()
        c.close()
