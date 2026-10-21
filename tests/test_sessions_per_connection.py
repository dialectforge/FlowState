"""2.4.0: each connection (a stdio server process, or one HTTP MCP session) gets its own
automatic session, keeping the app's name. Before, two windows of the same app shared one
session (keyed only by app name + project), so their work was lumped together."""

import asyncio
import sqlite3
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from conftest import SERVER_DIR, server_env
from flowstate import identity


def _rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def test_two_windows_of_one_app_get_two_sessions(seeded_db):
    params = StdioServerParameters(command=sys.executable, args=["-m", "flowstate"],
                                   env=server_env(seeded_db), cwd=str(SERVER_DIR))
    info = types.Implementation(name="Cursor", version="1")

    async def go():
        async with stdio_client(params) as (r1, w1), stdio_client(params) as (r2, w2):
            async with ClientSession(r1, w1, client_info=info) as a, \
                    ClientSession(r2, w2, client_info=info) as b:
                await a.initialize()
                await b.initialize()
                for s, text in ((a, "window one change"), (b, "window two change")):
                    await s.call_tool("start_here", {"project": "Alpha"})
                    r = await s.call_tool("log_work", {"project": "Alpha", "kind": "change",
                                                       "summary": text})
                    assert not r.isError, r.content[0].text
                return _rows(seeded_db, "SELECT session_id, client, events FROM auto_sessions "
                                        "WHERE closed_at IS NULL ORDER BY session_id")
    rows = asyncio.run(go())
    assert len(rows) == 2, rows
    assert {r[1] for r in rows} == {"Cursor"}                 # the app name is kept
    assert "window one change" in rows[0][2] and "window two" not in rows[0][2]
    assert "window two change" in rows[1][2] and "window one" not in rows[1][2]


def _ident(session):
    return identity.Identity("Cursor", None, "mac-mini", "self_reported", session)


def test_same_connection_reuses_its_session(backend):
    S = backend
    with identity.use(_ident("conn-a")):
        one = S.handle_call("log_work", {"project": "Alpha", "kind": "learning", "summary": "a1"},
                            "Cursor")
        two = S.handle_call("log_work", {"project": "Alpha", "kind": "learning", "summary": "a2"},
                            "Cursor")
    with identity.use(_ident("conn-b")):
        S.handle_call("log_work", {"project": "Alpha", "kind": "learning", "summary": "b1"},
                      "Cursor")
    assert "error" not in one and "error" not in two
    rows = _rows(S.backend.db_path, "SELECT a.session_id, a.client, r.conn_key, r.app, r.machine "
                                    "FROM auto_sessions a JOIN session_runs r USING (session_id) "
                                    "ORDER BY a.session_id")
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [
        ("Cursor", "conn-a", "Cursor", "mac-mini"), ("Cursor", "conn-b", "Cursor", "mac-mini")]


def test_brief_names_the_connections_own_session(backend):
    S = backend
    with identity.use(_ident("conn-a")):
        a = S.handle_call("start_here", {"project": "Alpha"}, "Cursor")
    with identity.use(_ident("conn-b")):
        b = S.handle_call("start_here", {"project": "Alpha"}, "Cursor")
    sids = [r[0] for r in _rows(S.backend.db_path,
                                "SELECT session_id FROM auto_sessions ORDER BY session_id")]
    assert len(sids) == 2
    assert f"Session #{sids[0]} is open" in a and f"Session #{sids[1]} is open" in b
