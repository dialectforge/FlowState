"""Review queue through a real MCP client: auto summaries and suggested records wait for
the user; approve (with edits), reject (kept, with reason); brief shows the count."""

import asyncio
import json
import sqlite3
import time

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate.autosession import AutoSessions

ASK = "User: yes, approve that one"


def run(coro):
    return asyncio.run(coro)


async def _with(S, fn, name="Claude Desktop"):
    async with create_connected_server_and_client_session(
            S.server, client_info=types.Implementation(name=name, version="1")) as c:
        return await fn(c)


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def test_tools_listed_with_user_only_wording(backend):
    async def go(c):
        return {t.name: t for t in (await c.list_tools()).tools}
    tools = run(_with(backend, go))
    for n in ("review_list", "review_approve", "review_reject", "review_mode"):
        assert n not in tools   # older names: still callable, no longer listed
    d = tools["manage_review"].description
    assert "approve, reject, set_mode: ONLY call this when the user has explicitly told you" in d
    assert "never approve your own suggestions" in d
    assert "(required)" in tools["manage_review"].inputSchema["properties"]["user_request"]["description"]
    assert "Never approve or reject review items" in backend.server.instructions


def test_auto_summary_goes_to_queue_and_brief_counts_it(backend, seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=0.01)
    sid = t.touch("Claude Desktop", 1, "read:start_here")
    t.note(sid, "change: wired the thing")
    time.sleep(1.8)
    assert t.close_idle() == [sid]
    rows = _q(seeded_db, "SELECT kind, status, session_id, summary FROM review_items")
    assert rows[0][:3] == ("session_summary", "pending", sid)
    out = json.loads(_q(seeded_db, "SELECT outcomes FROM sessions WHERE id=?", sid)[0][0])
    assert out["needs_review"] is True and out["review_item_id"]

    async def go(c):
        return await c.call_tool("start_here", {"project": "Alpha"})
    text = run(_with(backend, go)).content[0].text
    assert "Review queue: 1 item waiting for the user's OK" in text
    assert "Last session" not in text  # provisional summaries aren't shown as final


def test_suggested_record_waits_then_edit_and_approve(backend, seeded_db):
    async def go(c):
        s = await c.call_tool("log_work", {"project": "Alpha", "kind": "learning",
                                           "summary": "maybe a lesson", "suggested": True})
        item = _j(s)["queued_for_review"]
        before = _q(seeded_db, "SELECT COUNT(*) FROM learnings WHERE insight = 'maybe a lesson'")
        listed = await c.call_tool("review_list", {"project": "Alpha"})
        return item, before, listed

    item, before, listed = run(_with(backend, go))

    async def decide(c):   # the user's go-ahead, relayed by another app (not the one that queued it)
        no_words = await c.call_tool("review_approve", {"project": "Alpha", "item_id": item,
                                                        "user_request": "  "})
        ok = await c.call_tool("review_approve", {"project": "Alpha", "item_id": item,
                                                  "user_request": ASK,
                                                  "summary": "Edited lesson text"})
        again = await c.call_tool("review_approve", {"project": "Alpha", "item_id": item,
                                                     "user_request": ASK})
        return item, before, listed, no_words, ok, again
    item, before, listed, no_words, ok, again = run(_with(backend, decide, "Claude Code"))
    assert before[0][0] == 0                                   # not saved as a record yet
    assert [i["id"] for i in _j(listed)["items"]] == [item]
    assert no_words.isError and _j(no_words)["error"] == "user_request_required"
    assert not ok.isError, ok.content[0].text
    body = _j(ok)
    assert body["edited"] and body["record"]["table"] == "learnings"
    assert _q(seeded_db, "SELECT insight FROM learnings WHERE id=?", body["record"]["id"])[0][0] \
        == "Edited lesson text"
    assert _j(again)["error"] == "already_decided"
    st = _q(seeded_db, "SELECT status, user_request, decided_by FROM review_items WHERE id=?", item)
    assert st[0] == ("approved", ASK, "Claude Code")


def test_reject_keeps_item_with_reason(backend, seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    sid = t.touch("Grok", 1)
    t.close_instance()
    item = _q(seeded_db, "SELECT id FROM review_items WHERE session_id=?", sid)[0][0]

    async def go(c):
        no_reason = await c.call_tool("review_reject", {"project": "Alpha", "item_id": item,
                                                        "reason": "", "user_request": ASK})
        r = await c.call_tool("review_reject", {"project": "Alpha", "item_id": item,
                                                "reason": "wrong project",
                                                "user_request": "User: reject it"})
        listed = await c.call_tool("review_list", {"project": "Alpha", "status": "rejected"})
        return no_reason, r, listed
    no_reason, r, listed = run(_with(backend, go))
    assert _j(no_reason)["error"] == "reason_required"
    assert not r.isError, r.content[0].text
    row = _q(seeded_db, "SELECT status, reject_reason, summary FROM review_items WHERE id=?", item)[0]
    assert row[0] == "rejected" and row[1] == "wrong project" and row[2].startswith("[auto-captured")
    assert "rejected: wrong project" in _q(seeded_db, "SELECT summary FROM sessions WHERE id=?", sid)[0][0]
    assert [i["id"] for i in _j(listed)["items"]] == [item]


def test_approve_session_summary_and_project_checks(backend, seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    sid = t.touch("Grok", 1, "change: x")
    t.close_instance()
    item = _q(seeded_db, "SELECT id FROM review_items WHERE session_id=?", sid)[0][0]

    async def go(c):
        wrong = await c.call_tool("review_approve", {"project": "Beta Project", "item_id": item,
                                                     "user_request": ASK})
        missing = await c.call_tool("review_approve", {"item_id": item, "user_request": ASK})
        ok = await c.call_tool("review_approve", {"project": "Alpha", "item_id": item,
                                                  "user_request": ASK})
        return wrong, missing, ok
    wrong, missing, ok = run(_with(backend, go))
    assert _j(wrong)["error"] == "project_mismatch"
    assert _j(missing)["error"] == "project_required"
    assert not ok.isError, ok.content[0].text
    s = _q(seeded_db, "SELECT summary, outcomes FROM sessions WHERE id=?", sid)[0]
    assert not s[0].startswith("[auto-captured") and "Grok" in s[0]
    assert json.loads(s[1])["needs_review"] is False

    async def brief(c):
        return await c.call_tool("start_here", {"project": "Alpha"})
    text = run(_with(backend, brief)).content[0].text
    assert "Review queue: 0 items" in text and "Last session" in text
