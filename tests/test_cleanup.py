"""Clean-up suggestions (Phase D4e): duplicates, stale todos, misfiled records go to the
review queue only; approving applies one suggestion (undoable); rejected ones don't come
back. Temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import cleanup


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
    # The user's go-ahead comes in through another app: with review on, an app can't
    # decide what it queued itself (test_user_approval.py covers that).
    if client == "Claude Desktop" and (name in ("review_approve", "review_reject") or (
            name == "manage_review" and args.get("action") in ("approve", "reject"))):
        client = "Claude Code"
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


@pytest.fixture
def messy(fresh_db):
    """FlowState with a few records that belong to the VPN and kernel projects."""
    from flowstate import server as S
    from flowstate.database import Database
    d = Database(fresh_db)
    fs = d.create_project("FlowState", "Project memory MCP server with review queue")
    vpn = d.create_project("Atlas VPN", "WireGuard style VPN with custom protocol")
    os_ = d.create_project("Atlas OS", "Linux kernel distro hardening")
    d.create_component(fs.id, "Review queue")
    d.create_component(fs.id, "MCP server")
    for n in ("VPN handler", "Tunnel transport", "Device activation"):
        d.create_component(vpn.id, n)
    for n in ("Kernel hardening", "Pentest suite"):
        d.create_component(os_.id, n)
    for t in ("VPN handler drops tunnel on reconnect", "Tunnel transport UDP fallback",
              "Device activation limits per account", "VPN tunnel keepalive interval"):
        d.log_learning(vpn.id, t, "gotcha")
    for t in ("Kernel hardening flags for pentest", "Pentest kernel kptr_restrict"):
        d.log_learning(os_.id, t, "security")
    d.log_learning(fs.id, "Review queue items wait for the user", "pattern")
    misfiled = d.log_learning(fs.id, "VPN handler tunnel reconnect needs device activation check",
                              "gotcha")
    kern = d.add_todo(fs.id, "Kernel hardening pentest follow-up", None, "high")
    d.add_todo(fs.id, "Write the review queue docs", None, "medium")
    d.add_todo(fs.id, "Write the review queue docs page", None, "medium")
    S.init_backend(fresh_db)
    assert S.backend.error is None
    yield S, fresh_db, {"fs": fs.id, "vpn": vpn.id, "os": os_.id, "misfiled": misfiled.id,
                        "kern": kern.id}
    S.backend.tracker = None


def test_finds_misfiled_duplicates_and_stale(messy):
    S, db, ids = messy
    conn = sqlite3.connect(db)
    conn.execute("UPDATE todos SET created_at = '2025-01-01 00:00:00' WHERE title LIKE 'Kernel%'")
    conn.commit()
    conn.close()
    found = cleanup.suggest(db, ids["fs"])
    types_ = [(s["type"], tuple(map(tuple, s["records"]))) for s in found]
    assert ("misfiled", (("learning", ids["misfiled"]),)) in types_
    assert ("misfiled", (("todo", ids["kern"]),)) in types_
    mis = {tuple(s["records"][0]): s for s in found if s["type"] == "misfiled"}
    assert mis[("learning", ids["misfiled"])]["target"] == "Atlas VPN"
    assert mis[("todo", ids["kern"])]["target"] == "Atlas OS"
    assert any(s["type"] == "duplicate" for s in found)
    assert any(s["type"] == "stale" and s["records"] == [["todo", ids["kern"]]] for s in found)
    # the record that does belong here is not flagged
    assert not any(s["type"] == "misfiled" and "Review queue items" in s["summary"] for s in found)


def test_suggest_tool_only_queues(messy):
    S, db, ids = messy
    before = _q(db, "SELECT project_id, id FROM learnings ORDER BY id")
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState"}))
    assert r["queued"] >= 3 and r["already_suggested"] == 0
    assert _q(db, "SELECT project_id, id FROM learnings ORDER BY id") == before   # nothing moved
    again = _j(call(S, "cleanup_suggest", {"project": "FlowState"}))
    assert again["queued"] == 0 and again["already_suggested"] == r["queued"]    # no repeats
    items = _j(call(S, "review_list", {"project": "FlowState"}))["items"]
    assert items and all(", clean-up idea): " in i for i in items)   # one line per item


def _item(db, kind_sql):
    return _q(db, f"SELECT id, payload FROM review_items WHERE kind='cleanup' AND {kind_sql}")[0]


def test_approve_moves_and_can_be_undone(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    iid, _ = _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")
    r = _j(call(S, "review_approve", {"project": "FlowState", "item_id": iid,
                                      "user_request": "User: yes, move that one"}))
    assert r["record"] == {"table": "learnings", "id": ids["misfiled"]}
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["vpn"],)]
    call(S, "history", {"action": "undo", "project": "Atlas VPN", "record_type": "learning",
                        "record_id": ids["misfiled"]})
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["fs"],)]


def test_approve_with_other_target_and_reject_is_remembered(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    iid, _ = _item(db, f"payload LIKE '%todo#{ids['kern']}%'")
    call(S, "review_approve", {"project": "FlowState", "item_id": iid, "move_to": "Atlas VPN",
                               "user_request": "User: move it to the VPN project instead"})
    assert _q(db, "SELECT project_id FROM todos WHERE id = ?", ids["kern"]) == [(ids["vpn"],)]
    other, _ = _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")
    call(S, "review_reject", {"project": "FlowState", "item_id": other, "reason": "it's fine here",
                              "user_request": "User: no, keep that"})
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["fs"],)]
    again = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]}))
    assert again["queued"] == 0        # the rejected one is not suggested again


def test_duplicate_todo_is_cancelled_not_deleted(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["duplicate"]})
    iid, payload = _item(db, "payload LIKE '%duplicate%'")
    newer = json.loads(payload)["records"][-1][1]
    call(S, "review_approve", {"project": "FlowState", "item_id": iid,
                               "user_request": "User: approve the duplicate clean-up"})
    assert _q(db, "SELECT status FROM todos WHERE id = ?", newer) == [("cancelled",)]
