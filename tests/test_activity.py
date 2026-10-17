"""Per-app activity log and pause switch (Phase D4f): one row per call without arguments or
text; a paused app can read but its writes get "paused by the user". Temp DBs only."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import activity, cli


def run(coro):
    return asyncio.run(coro)


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, client):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


def test_activity_rows_have_no_text(backend, seeded_db):
    secret_ish = "Remember the launch codeword is BLUEBIRD"
    call(backend, "log_work", {"project": "Alpha", "kind": "learning", "summary": secret_ish}, "Grok Build")
    call(backend, "start_here", {"project": "Beta Project"}, "Claude Desktop")
    call(backend, "todo", {"action": "add", "title": "x"}, "Cursor")         # refused: no project
    rows = activity.recent(seeded_db)
    got = [(r["app"], r["tool"], r["action"], r["project"], r["write"], r["ok"], r["error"]) for r in rows]
    assert ("Grok Build", "log_work", None, "Alpha", 1, 1, None) in got
    assert ("Claude Desktop", "start_here", None, "Beta Project", 0, 1, None) in got
    assert ("Cursor", "todo", "add", None, 1, 0, "project_required") in got
    dump = "\n".join(" ".join(map(str, r)) for r in sqlite3.connect(seeded_db).execute(
        "SELECT * FROM activity_log"))
    assert "BLUEBIRD" not in dump and "launch" not in dump
    apps = {a["app"]: a for a in activity.apps(seeded_db)}
    assert apps["Cursor"]["errors"] == 1 and apps["Grok Build"]["writes"] == 1


def test_paused_app_can_read_but_not_write(backend, seeded_db, capsys):
    assert cli.main(["apps", "pause", "--app", "Grok Build", "--db", str(seeded_db)]) == 0
    r = call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "sneaky"}, "Grok Build")
    assert r.isError and _j(r)["error"] == "paused_by_user" and "paused" in _j(r)["message"]
    assert not sqlite3.connect(seeded_db).execute("SELECT 1 FROM todos WHERE title='sneaky'").fetchall()
    read = call(backend, "start_here", {"project": "Alpha"}, "Grok Build")
    assert not read.isError and "FlowState brief: Alpha" in read.content[0].text
    other = call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "ok"}, "Claude Desktop")
    assert not other.isError
    assert cli.main(["apps", "list", "--db", str(seeded_db)]) == 0
    assert "Grok Build: PAUSED" in capsys.readouterr().out
    cli.main(["apps", "resume", "--app", "Grok Build", "--db", str(seeded_db)])
    assert not call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "now"},
                    "Grok Build").isError
    assert cli.main(["apps", "activity", "--app", "Grok Build", "--db", str(seeded_db)]) == 0
    out = capsys.readouterr().out
    assert "error paused_by_user" in out and "log_work" in out


def test_no_mcp_tool_can_pause(backend):
    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="x", version="1")) as c:
            return [t.name for t in (await c.list_tools()).tools]
    names = run(go())
    assert not any("pause" in n or n == "apps" for n in names)
