"""2.4.0 run ledger (record only, no new tool): per session, its start, end, app, and the
ids / hashes of what it read and wrote. Never the text."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import ledger
from flowstate.tooldefs import ALL_TOOLS

SECRET_ISH = "Zanzibar-lighthouse-protocol"     # text that must never reach the ledger


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _work(S, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            brief = (await c.call_tool("start_here", {"project": "Alpha"})).content[0].text
            w = json.loads((await c.call_tool("log_work", {
                "project": "Alpha", "kind": "decision",
                "summary": f"Adopt the {SECRET_ISH}"})).content[0].text)
            await c.call_tool("search", {"query": SECRET_ISH, "project": "Alpha"})
            return brief, w
    return asyncio.run(go())


def test_a_run_records_reads_and_writes_by_id(backend):
    S, db = backend, backend.backend.db_path
    brief, w = _work(S)
    [(sid,)] = _q(db, "SELECT session_id FROM auto_sessions")
    run = ledger.run(db, sid)
    assert run["app"] == "Cursor" and run["opened_at"] and run["closed_at"] is None
    assert [r["tool"] for r in run["reads"]] == ["start_here", "search"]
    shown = run["reads"][0]["records"]
    assert "todo:1" in shown and "problem:1" in shown and "learning:1" in shown
    assert run["reads"][0]["answer_hash"] == ledger.answer_hash(brief)
    assert run["writes"] == [{**run["writes"][0], "tool": "log_work", "status": "saved",
                              "records": [f"decision:{w['record']['id']}"]}]
    assert run["writes"][0]["change_id"] == w["receipt"]["change"]
    S.backend.tracker.close_instance("shutdown")
    assert ledger.run(db, sid)["closed_at"]                 # the end is the session's end


def test_the_ledger_stores_no_text(backend):
    S, db = backend, backend.backend.db_path
    _work(S)
    for table in ("session_runs", "run_writes", "run_views"):
        rows = json.dumps(_q(db, f"SELECT * FROM {table}"))
        assert SECRET_ISH not in rows and "Adopt" not in rows, table
        assert "Alpha todo" not in rows and "Alpha lesson" not in rows, table


def test_brief_rules_and_handoffs_are_recorded_as_shown(backend):
    S, db = backend, backend.backend.db_path

    async def go():
        async with create_connected_server_and_client_session(S.server) as c:
            await c.call_tool("manage_rules", {"action": "add", "project": "Alpha",
                                               "rule": "Never push to main"})
            await c.call_tool("manage_handoffs", {"action": "leave", "project": "Alpha",
                                                  "where_i_was": "halfway", "next_step": "finish"})
            await c.call_tool("start_here", {"project": "Alpha"})
    asyncio.run(go())
    records = json.loads(_q(db, "SELECT records FROM run_views WHERE tool = 'start_here' "
                                "ORDER BY id DESC LIMIT 1")[0][0])
    assert "rule:1" in records and "handoff:1" in records


def test_no_new_tool():
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == 18 and not any("run" in n or "ledger" in n for n in names)
