"""Phase D4a/c/d: read vs write labels and the core tool list; edit history and undo;
subtasks, blockers and "what's next". Real MCP client sessions on temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def run(coro):
    return asyncio.run(coro)


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


def list_tools(S):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="x", version="1")) as c:
            return (await c.list_tools()).tools
    return run(go())


# ---------------------------------------------------------------- D4a labels
def test_every_tool_is_labelled(backend, monkeypatch):
    from flowstate.tool_labels import LABELS
    monkeypatch.setenv("FLOWSTATE_TOOLS", "full")
    tools = list_tools(backend)
    assert {t.name for t in tools} == set(LABELS)
    by = {t.name: t.annotations for t in tools}
    for name in ("start_here", "search"):
        assert by[name].readOnlyHint is True and by[name].idempotentHint is True
    for name in ("log_work", "manage_handoffs", "manage_rules", "manage_links", "manage_skills"):
        assert by[name].readOnlyHint is False and by[name].destructiveHint is False
    for name in ("manage_variables", "manage_files", "manage_history", "manage_review",
                 "manage_todos", "manage_projects"):
        assert by[name].readOnlyHint is False and by[name].destructiveHint is True
    assert by["manage_data_git"].openWorldHint is True and by["manage_todos"].openWorldHint is False
    assert all(a.title for a in by.values())


def test_core_profile_lists_few_tools_but_all_still_work(backend, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_TOOLS", "core")
    names = [t.name for t in list_tools(backend)]
    assert names == ["start_here", "log_work", "search", "manage_todos", "manage_review",
                     "manage_handoffs"]
    r = call(backend, "manage_projects", {"action": "list"})   # not listed, still answers
    assert not r.isError and "Alpha" in r.content[0].text
    r = call(backend, "list_projects", {})                     # older name, still answers
    assert not r.isError and "Alpha" in r.content[0].text
    monkeypatch.setenv("FLOWSTATE_TOOLS", "all")               # older spelling of full
    assert len(list_tools(backend)) == 18


# ---------------------------------------------------------------- D4c history
def test_edit_is_recorded_and_undone(backend, seeded_db):
    call(backend, "todo", {"action": "update", "project": "Alpha", "todo_id": 1,
                           "status": "done", "title": "Alpha todo (renamed)"})
    h = _j(call(backend, "history", {"action": "list", "project": "Alpha", "record_type": "todo",
                                     "record_id": 1}))
    assert h["count"] == 1
    ch = h["history"][0]["changes"]
    assert ch["status"] == {"old": "pending", "new": "done"}
    assert ch["title"]["old"] == "Alpha todo" and h["history"][0]["client"] == "Claude Desktop"
    u = _j(call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                                     "record_id": 1}))
    assert u["rows"] == [{"record_type": "todo", "record_id": 1, "now": "restored"}]
    assert _q(seeded_db, "SELECT title, status FROM todos WHERE id = 1") == [("Alpha todo", "pending")]
    # undo again = redo
    call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "todo", "record_id": 1})
    assert _q(seeded_db, "SELECT status FROM todos WHERE id = 1") == [("done",)]
    # wrong project, nothing to undo
    wrong = call(backend, "history", {"action": "undo", "project": "Beta Project",
                                      "record_type": "todo", "record_id": 1})
    assert _j(wrong)["error"] == "project_mismatch"
    none = call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                                     "record_id": 2})
    assert _j(none)["error"] == "nothing_to_undo"


def test_delete_comes_back_and_outside_changes_block_undo(backend, seeded_db):
    v = _j(call(backend, "variable", {"action": "create", "project": "Alpha", "name": "port",
                                      "value": "8766"}))
    vid = v["id"]
    call(backend, "variable", {"action": "delete", "project": "Alpha", "variable_id": vid})
    assert not _q(seeded_db, "SELECT 1 FROM project_variables WHERE id = ?", vid)
    call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "variable",
                              "record_id": vid})
    assert _q(seeded_db, "SELECT name, value FROM project_variables WHERE id = ?", vid) == [("port", "8766")]
    # A change made outside FlowState's tools after an edit: undo refuses, nothing changes.
    call(backend, "todo", {"action": "update", "project": "Alpha", "todo_id": 1, "status": "done"})
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE todos SET title = 'edited elsewhere' WHERE id = 1")
    conn.commit()
    conn.close()
    r = call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                                  "record_id": 1})
    assert _j(r)["error"] == "changed_since"
    assert _q(seeded_db, "SELECT title, status FROM todos WHERE id = 1") == [("edited elsewhere", "done")]


def test_solve_is_undone_with_its_solution(backend, seeded_db):
    call(backend, "problem", {"action": "solve", "project": "Alpha", "problem_id": 1,
                              "summary": "fixed it"})
    assert _q(seeded_db, "SELECT status FROM problems WHERE id = 1") == [("solved",)]
    call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "problem",
                              "record_id": 1})
    assert _q(seeded_db, "SELECT status FROM problems WHERE id = 1") == [("open",)]
    assert not _q(seeded_db, "SELECT 1 FROM solutions WHERE problem_id = 1")
    again = call(backend, "problem", {"action": "solve", "project": "Alpha", "problem_id": 1,
                                      "summary": "fixed it properly"})
    assert not again.isError      # can be solved again (the solution row was removed too)


def test_link_remove_is_undoable(backend, seeded_db):
    lid = _j(call(backend, "link", {"action": "add", "project": "Alpha",
                                    "target_project": "Beta Project"}))["link"]["id"]
    call(backend, "link", {"action": "remove", "project": "Alpha", "link_id": lid})
    call(backend, "history", {"action": "undo", "project": "Alpha", "record_type": "link",
                              "record_id": lid})
    assert _q(seeded_db, "SELECT removed_at FROM record_links WHERE id = ?", lid) == [(None,)]


# ---------------------------------------------------------------- D4d tasks
def _add(S, title, **kw):
    r = call(S, "todo", {"action": "add", "project": "Alpha", "title": title, **kw})
    assert not r.isError, r.content[0].text
    return _j(r)


def test_whats_next_respects_blockers_subtasks_and_priority(backend, seeded_db):
    # seeded: todo #1 "Alpha todo" high
    ship = _add(backend, "Ship v1", priority="critical")
    parts = [_add(backend, f"Part {i}", parent_todo_id=ship["id"], priority="medium")
             for i in (1, 2)]
    assert parts[0]["parent"]
    docs = _add(backend, "Write docs", priority="critical", blocked_by=[parts[0]["id"]])
    low = _add(backend, "Tidy up", priority="low")
    nxt = _j(call(backend, "todo", {"action": "next", "project": "Alpha", "limit": 10}))
    ids = [t["id"] for t in nxt["next"]]
    assert ship["id"] not in ids and docs["id"] not in ids     # parent waits on parts; docs blocked
    assert ids == [1, parts[0]["id"], parts[1]["id"], low["id"]]  # high, then medium (older first), low
    assert nxt["blocked"][0]["id"] == docs["id"] and "Part 1" in nxt["blocked"][0]["blocked_by"][0]

    call(backend, "todo", {"action": "update", "project": "Alpha", "todo_id": parts[0]["id"],
                           "status": "done"})
    ids = [t["id"] for t in _j(call(backend, "todo", {"action": "next", "project": "Alpha",
                                                      "limit": 10}))["next"]]
    assert ids[0] == docs["id"]         # unblocked, critical
    # blocked by a problem until it is solved
    call(backend, "todo", {"action": "block", "project": "Alpha", "todo_id": docs["id"],
                           "blocked_by_problem": 1})
    ids = [t["id"] for t in _j(call(backend, "todo", {"action": "next", "project": "Alpha"}))["next"]]
    assert docs["id"] not in ids
    un = _j(call(backend, "todo", {"action": "unblock", "project": "Alpha", "todo_id": docs["id"]}))
    assert len(un["unblocked_links"]) == 2
    ids = [t["id"] for t in _j(call(backend, "todo", {"action": "next", "project": "Alpha"}))["next"]]
    assert ids[0] == docs["id"]


def test_task_validation(backend, seeded_db):
    before = _q(seeded_db, "SELECT COUNT(*) FROM todos")[0][0]
    r = call(backend, "todo", {"action": "add", "project": "Alpha", "title": "x", "parent_todo_id": 2})
    assert _j(r)["error"] == "project_mismatch"           # parent in Beta
    r = call(backend, "todo", {"action": "add", "project": "Alpha", "title": "x", "blocked_by": [99]})
    assert _j(r)["error"] == "record_not_found"
    assert _q(seeded_db, "SELECT COUNT(*) FROM todos")[0][0] == before   # nothing half-made
    a, b = _add(backend, "A"), _add(backend, "B")
    call(backend, "todo", {"action": "block", "project": "Alpha", "todo_id": b["id"], "blocked_by": [a["id"]]})
    loop = call(backend, "todo", {"action": "block", "project": "Alpha", "todo_id": a["id"],
                                  "blocked_by": [b["id"]]})
    assert _j(loop)["error"] == "cycle"
    # a todo in another project can block this one (cross-project dependency)
    ok = call(backend, "todo", {"action": "block", "project": "Alpha", "todo_id": a["id"], "blocked_by": [2]})
    assert not ok.isError
    wrong = call(backend, "todo", {"action": "block", "project": "Beta Project", "todo_id": a["id"],
                                   "blocked_by": [2]})
    assert _j(wrong)["error"] == "project_mismatch"
