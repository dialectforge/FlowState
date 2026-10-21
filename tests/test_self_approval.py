"""2.4.0: an agent can't approve or reject its own review items, enforced in code. With
review mode on, the app (on the same machine) that queued an item can't decide it through
MCP at all; the user does (`flowstate approve`, the review page; test_user_approval.py).
With review off, only with the user's own words (not one word, not the item's own text)."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import review_page


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def _suggest(S, app="Cursor", summary="Adopt trunk-based development"):
    r = _j(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": summary,
                                "suggested": True}, app))
    return r["queued_for_review"]


def _status(S, item):
    conn = sqlite3.connect(S.backend.db_path)
    try:
        return conn.execute("SELECT status FROM review_items WHERE id = ?", (item,)).fetchone()[0]
    finally:
        conn.close()


def test_agent_cannot_approve_its_own_item_with_a_token_phrase(backend):
    S = backend
    item = _suggest(S)
    for words in ("approve", "Adopt trunk-based development", "ok"):
        r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                      "user_request": words})
        body = _j(r)
        assert r.isError and body["error"] == "own_item", body
        assert body["message"].startswith(f"You queued review item #{item}")
        assert len(body["message"]) < 200
    assert _status(S, item) == "pending"


def test_agent_cannot_reject_its_own_item_either(backend):
    S = backend
    item = _suggest(S)
    r = call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                  "reason": "nah", "user_request": "no"})
    assert _j(r)["error"] == "own_item" and _status(S, item) == "pending"


def test_own_item_with_review_on_is_refused_even_with_the_users_words(backend):
    S = backend
    item = _suggest(S)
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "User: yes, approve the trunk-based one"})
    assert _j(r)["error"] == "own_item" and "flowstate approve" in _j(r)["message"]
    assert _status(S, item) == "pending"


def test_another_app_relaying_the_user_is_unchanged(backend):
    S = backend
    item = _suggest(S, app="Cursor")
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "approve"}, app="Claude Desktop")
    assert not r.isError, r.content[0].text


def test_own_session_summary_counts_as_own(backend):
    S = backend
    call(S, "start_here", {"project": "Alpha"}, app="Cursor")
    S.backend.tracker.close_instance("shutdown")
    conn = sqlite3.connect(S.backend.db_path)
    item = conn.execute("SELECT id FROM review_items WHERE kind = 'session_summary'").fetchone()[0]
    conn.close()
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "fine"}, app="Cursor")
    assert _j(r)["error"] == "own_item"


def test_review_page_is_the_user(backend):
    S = backend
    item = _suggest(S)
    msg = review_page.Page(S.backend.db_path).approve({"item_id": str(item)})
    assert _status(S, item) == "approved", msg
