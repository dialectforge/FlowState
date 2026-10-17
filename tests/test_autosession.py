"""Implicit sessions: open on first call, reuse, idle close, shutdown close, explicit end."""

import json
import sqlite3
import time
from pathlib import Path

from flowstate.autosession import AUTO_SESSIONS_DDL, AUTO_TAG, AutoSessions, build_summary


def _session(db, sid):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return dict(c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone())
    finally:
        c.close()


def test_schema_and_code_ddl_match():
    schema = (Path(__file__).resolve().parents[1] / "database" / "schema.sql").read_text(encoding="utf-8")
    assert AUTO_SESSIONS_DDL in schema.replace("\r\n", "\n")


def test_first_touch_opens_and_later_touches_reuse(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    sid = t.touch("Claude Desktop", 1, "read:start_here")
    assert t.touch("Claude Desktop", 1) == sid
    assert _session(seeded_db, sid)["ended_at"] is None
    assert t.current("Claude Desktop", 1)["call_count"] == 2


def test_split_by_client_and_project(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    a = t.touch("Claude Desktop", 1)
    assert t.touch("grok-cli", 1) != a
    assert t.touch("Claude Desktop", 2) != a


def test_idle_reaper_closes_with_flagged_summary(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=0.01)  # 0.6 s
    sid = t.touch("Claude Desktop", 1, "read:start_here")
    t.note(sid, "change: Added a thing")
    assert t.close_idle() == []
    time.sleep(1.8)  # timestamps have 1 s resolution
    assert t.close_idle() == [sid]
    s = _session(seeded_db, sid)
    assert s["ended_at"] is not None
    assert s["summary"].startswith(AUTO_TAG)
    assert "change: Added a thing" in s["summary"] and "Claude Desktop" in s["summary"]
    out = json.loads(s["outcomes"])
    assert out["auto_captured"] and out["needs_review"] and out["close_reason"] == "idle"
    assert t.close_idle() == []  # idempotent


def test_reaper_closes_sessions_of_other_processes(seeded_db):
    crashed = AutoSessions(seeded_db, idle_minutes=0.01, instance="crashed")
    sid = crashed.touch("Cursor", 1)
    time.sleep(1.8)  # timestamps have 1 s resolution
    survivor = AutoSessions(seeded_db, idle_minutes=0.01, instance="other")
    assert survivor.close_idle() == [sid]


def test_stale_touch_closes_and_reopens(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=0.01)
    old = t.touch("Claude Desktop", 1)
    time.sleep(1.8)  # timestamps have 1 s resolution
    new = t.touch("Claude Desktop", 1)
    assert new != old
    assert _session(seeded_db, old)["ended_at"] is not None
    assert json.loads(_session(seeded_db, old)["outcomes"])["close_reason"] == "stale"


def test_shutdown_closes_only_own_sessions(seeded_db):
    mine = AutoSessions(seeded_db, idle_minutes=60, instance="me")
    theirs = AutoSessions(seeded_db, idle_minutes=60, instance="them")
    a = mine.touch("Claude Desktop", 1)
    b = theirs.touch("Cursor", 1)
    assert mine.close_instance() == [a]
    assert _session(seeded_db, a)["ended_at"] is not None
    assert _session(seeded_db, b)["ended_at"] is None
    assert "client disconnected" in _session(seeded_db, a)["summary"]


def test_explicit_end_keeps_agent_summary(seeded_db):
    from flowstate.database import Database
    t = AutoSessions(seeded_db, idle_minutes=0.01)
    sid = t.touch("Claude Desktop", 1)
    Database(seeded_db).end_session(sid, "Agent wrote this", None)
    t.mark_explicit_end(sid)
    time.sleep(1.8)  # timestamps have 1 s resolution
    assert t.close_idle() == []
    assert _session(seeded_db, sid)["summary"] == "Agent wrote this"


def test_summary_is_short():
    s = build_summary("X", 500, 90, [f"change: item {i} " + "y" * 60 for i in range(60)], "idle")
    assert len(s) <= 600 and s.startswith(AUTO_TAG)
