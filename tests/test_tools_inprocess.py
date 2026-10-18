"""Through a real MCP client session (in-memory transport): instructions, prompt,
start_here, project-required writes, log_work, session adoption."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def run(coro):
    return asyncio.run(coro)


async def _with_client(S, fn, name="pytest-client"):
    async with create_connected_server_and_client_session(
            S.server, client_info=types.Implementation(name=name, version="1")) as c:
        return await fn(c)


def _json(r):
    return json.loads(r.content[0].text)


def test_tools_prompt_and_instructions(backend):
    S = backend

    async def go(c):
        tools = (await c.list_tools()).tools
        names = [t.name for t in tools]
        assert names[:2] == ["start_here", "log_work"]
        assert "CALL THIS FIRST" in tools[0].description
        todo = next(t for t in tools if t.name == "manage_todos")
        assert "project" in todo.inputSchema["required"]
        prompts = (await c.list_prompts()).prompts
        assert [p.name for p in prompts] == ["flowstate-start"]
        p = await c.get_prompt("flowstate-start", {"project": "Alpha"})
        text = p.messages[0].content.text
        assert "start_here" in text and "FlowState brief: Alpha" in text
    run(_with_client(S, go))
    assert "start_here" in S.server.instructions and "log_work" in S.server.instructions


@pytest.mark.parametrize("name,args", [
    ("todo", {"action": "add", "title": "x"}),
    ("log_learning", {"insight": "x"}),
    ("log_work", {"kind": "change", "summary": "x", "project": ""}),
    ("create_component", {"name": "x"}),
    ("problem", {"action": "log", "title": "x"}),
    ("todo", {"action": "update", "todo_id": 1, "status": "done"}),
    ("variable", {"action": "create", "name": "x", "value": "y"}),
    ("self_improve", {"action": "learn_skill", "skill": "x", "skill_type": "approach"}),
])
def test_writes_without_project_are_refused(backend, name, args):
    async def go(c):
        return await c.call_tool(name, args)
    r = run(_with_client(backend, go))
    assert r.isError
    body = _json(r)
    assert body["error"] == "project_required"
    assert "Alpha" in body["candidates"] and "project=" in body["message"]


def test_unknown_and_mismatched_projects_are_refused(backend):
    async def go(c):
        unknown = await c.call_tool("todo", {"action": "add", "title": "x", "project": "Nope"})
        mismatch = await c.call_tool("todo", {"action": "update", "todo_id": 1, "status": "done",
                                              "project": "Beta Project"})
        both = await c.call_tool("todo", {"action": "add", "title": "x", "project": "Alpha",
                                          "project_id": 2})
        missing = await c.call_tool("todo", {"action": "update", "todo_id": 999, "project": "Alpha"})
        return unknown, mismatch, both, missing
    unknown, mismatch, both, missing = run(_with_client(backend, go))
    assert _json(unknown)["error"] == "project_not_found"
    m = _json(mismatch)
    assert m["error"] == "project_mismatch" and "'Alpha'" in m["message"]
    assert _json(both)["error"] == "project_mismatch"
    assert _json(missing)["error"] == "record_not_found"


def test_writes_with_project_name_or_id_succeed(backend, seeded_db):
    async def go(c):
        a = await c.call_tool("todo", {"action": "add", "title": "named", "project": "alpha"})
        b = await c.call_tool("todo", {"action": "add", "title": "by id", "project_id": 1})
        u = await c.call_tool("todo", {"action": "update", "todo_id": 1, "status": "done",
                                       "project": "Alpha"})
        return a, b, u
    for r in run(_with_client(backend, go)):
        assert not r.isError, r.content[0].text
    conn = sqlite3.connect(seeded_db)
    n = conn.execute("SELECT COUNT(*) FROM todos WHERE project_id=1 AND title IN ('named','by id')")
    assert n.fetchone()[0] == 2


def test_reads_accept_a_project_name(backend):
    """Phase C fix: reads resolved the name but passed no project_id to the tool."""
    async def go(c):
        return [await c.call_tool("todo", {"action": "list", "project": "Alpha"}),
                await c.call_tool("todo", {"action": "list", "project": "alpha"})]
    for r in run(_with_client(backend, go)):
        assert not r.isError, r.content[0].text
        assert "Alpha todo" in r.content[0].text and "Beta todo" not in r.content[0].text


def test_reads_do_not_need_project(backend):
    async def go(c):
        return [await c.call_tool("list_projects", {}),
                await c.call_tool("search", {"query": "Alpha"}),
                await c.call_tool("todo", {"action": "list", "project_id": 1})]
    for r in run(_with_client(backend, go)):
        assert not r.isError, r.content[0].text


def test_log_work_all_kinds_and_session(backend, seeded_db):
    async def go(c):
        await c.call_tool("start_here", {"project": "Alpha"})
        out = []
        for kind in ("decision", "change", "problem", "learning", "todo"):
            out.append(await c.call_tool("log_work", {"project": "Alpha", "kind": kind,
                                                      "summary": f"{kind} one", "details": "d"}))
        adopted = await c.call_tool("session", {"action": "start", "project": "Alpha"})
        brief = await c.call_tool("start_here", {"project": "Alpha"})
        return out, adopted, brief
    out, adopted, brief = run(_with_client(backend, go, name="Claude Desktop"))
    assert not any(r.isError for r in out), [r.content[0].text for r in out]
    assert _json(adopted)["automatic"] is True
    text = brief.content[0].text
    for needle in ("decision one", "change one", "problem one", "learning one", "todo one"):
        assert needle in text
    conn = sqlite3.connect(seeded_db)
    row = conn.execute("SELECT client, call_count, events FROM auto_sessions").fetchone()
    assert row[0] == "Claude Desktop" and row[1] >= 7
    assert "change: change one" in row[2]
    general = conn.execute("SELECT COUNT(*) FROM components WHERE project_id=1 AND name='General'")
    assert general.fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1  # adopted, not duplicated


def test_no_project_no_session(backend, seeded_db):
    async def go(c):
        await c.call_tool("list_projects", {})
        return await c.call_tool("start_here", {})
    r = run(_with_client(backend, go))
    assert "Projects:" in r.content[0].text
    conn = sqlite3.connect(seeded_db)
    assert conn.execute("SELECT COUNT(*) FROM auto_sessions").fetchone()[0] == 0


def test_explicit_end_closes_auto_session(backend, seeded_db):
    async def go(c):
        await c.call_tool("start_here", {"project": "Alpha"})
        sid = backend.backend.tracker.current("pytest-client", 1)["session_id"]
        r = await c.call_tool("session", {"action": "end", "session_id": sid, "project": "Alpha",
                                          "summary": "done by agent"})
        return sid, r
    sid, r = run(_with_client(backend, go))
    assert not r.isError, r.content[0].text
    conn = sqlite3.connect(seeded_db)
    reason = conn.execute("SELECT close_reason FROM auto_sessions WHERE session_id=?", (sid,))
    assert reason.fetchone()[0] == "explicit"
    summary = conn.execute("SELECT summary FROM sessions WHERE id=?", (sid,))
    assert summary.fetchone()[0] == "done by agent"
