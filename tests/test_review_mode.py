"""Review mode (Phase D2): off / auto_only / types / everything, per install with a
per-project override, set only with the user's words, every change logged, shown in the
brief. Real MCP client sessions on temp DBs only."""

import asyncio
import json
import sqlite3
import time

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate.autosession import AutoSessions, OFF_TAG

ASK = "User: please hold every new record for my review"


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


def test_default_is_auto_only_and_brief_shows_it(backend):
    m = _j(call(backend, "review_mode", {"action": "get"}))
    assert m["mode"] == "auto_only" and m["source"] == "default"
    brief = call(backend, "start_here", {"project": "Alpha"}).content[0].text
    assert "Review mode: auto_only, only auto-captured items wait for review." in brief


def test_set_needs_the_users_words_and_is_logged(backend, seeded_db):
    refused = call(backend, "review_mode", {"action": "set", "mode": "everything"})
    assert refused.isError and _j(refused)["error"] == "user_request_required"
    bad = call(backend, "review_mode", {"action": "set", "mode": "types", "user_request": ASK})
    assert _j(bad)["error"] == "kinds_required"
    r = _j(call(backend, "review_mode", {"action": "set", "mode": "everything",
                                         "user_request": ASK}, client="Grok Build"))
    assert r["old"] is None and r["now"]["mode"] == "everything" and r["logged"]
    log = _q(seeded_db, "SELECT scope, old_mode, new_mode, client, caller, user_request "
                        "FROM review_mode_log")
    assert log == [("install", None, "everything", "Grok Build", "stdio", ASK)]
    brief = call(backend, "start_here", {"project": "Alpha"}).content[0].text
    assert "Review mode: everything, every new record waits for the user's OK" in brief


def test_everything_holds_new_records_including_older_tools(backend, seeded_db):
    call(backend, "review_mode", {"action": "set", "mode": "everything", "user_request": ASK})
    todos = _q(seeded_db, "SELECT COUNT(*) FROM todos")[0][0]
    r = _j(call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Ship it"}))
    assert r["queued_for_review"]
    old = _j(call(backend, "todo", {"action": "add", "project": "Alpha", "title": "Old-style todo",
                                    "priority": "high"}))
    assert old["queued_for_review"] and "review mode" in old["note"]
    learn = _j(call(backend, "log_learning", {"project": "Alpha", "insight": "Old-style lesson"}))
    assert learn["kind"] == "learning"
    assert _q(seeded_db, "SELECT COUNT(*) FROM todos")[0][0] == todos     # nothing saved yet
    # Edits are not held (history covers them)
    upd = call(backend, "todo", {"action": "update", "project": "Alpha", "todo_id": 1,
                                 "status": "in_progress"})
    assert not upd.isError and "queued_for_review" not in _j(upd)
    # The user approves one: it becomes a real todo with its priority.
    item = old["queued_for_review"]
    call(backend, "review_approve", {"project": "Alpha", "item_id": item,   # relayed by another app
                                     "user_request": "User: approve the old-style todo"}, "Claude Code")
    assert _q(seeded_db, "SELECT priority FROM todos WHERE title = 'Old-style todo'") == [("high",)]


def test_types_holds_only_chosen_kinds(backend, seeded_db):
    call(backend, "review_mode", {"action": "set", "mode": "types", "kinds": ["decision"],
                                  "user_request": "User: just hold decisions"})
    d = _j(call(backend, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Go"}))
    t = _j(call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Do"}))
    assert "queued_for_review" in d and t["logged"] == "todo"
    old = _j(call(backend, "session", {"action": "log_conversation", "project": "Alpha",
                                       "user_prompt_summary": "chat", "key_decisions": ["Pick B"]}))
    assert old["queued_for_review"]
    plain = _j(call(backend, "session", {"action": "log_conversation", "project": "Alpha",
                                         "user_prompt_summary": "just a chat"}))
    assert "queued_for_review" not in plain    # not a decision, nothing to hold


def test_off_saves_suggestions_and_keeps_auto_summaries(backend, seeded_db):
    call(backend, "review_mode", {"action": "set", "mode": "off",
                                  "user_request": "User: turn the review queue off"})
    r = _j(call(backend, "log_work", {"project": "Alpha", "kind": "learning",
                                      "summary": "Maybe this", "suggested": True}))
    assert r["logged"] == "learning" and "Review mode is off" in r["note"]
    t = AutoSessions(seeded_db, idle_minutes=0.01)
    sid = t.touch("Claude Desktop", 1, "read:start_here")
    t.note(sid, "learning: real work")   # (joined or fresh, by timing) a read-only one files none
    time.sleep(1.8)
    assert t.close_idle() == [sid]
    assert _q(seeded_db, "SELECT COUNT(*) FROM review_items")[0][0] == 0
    summary = _q(seeded_db, "SELECT summary FROM sessions WHERE id = ?", sid)[0][0]
    assert summary.startswith(OFF_TAG)
    assert "Last session" in call(backend, "start_here", {"project": "Alpha"}).content[0].text


def test_project_override_and_clear(backend, seeded_db):
    call(backend, "review_mode", {"action": "set", "project": "Beta Project", "mode": "everything",
                                  "user_request": "User: review everything in Beta"})
    a = _j(call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "a"}))
    b = _j(call(backend, "log_work", {"project": "Beta Project", "kind": "todo", "summary": "b"}))
    assert a["logged"] == "todo" and "queued_for_review" in b
    g = _j(call(backend, "review_mode", {"action": "get", "project": "Beta Project"}))
    assert g["mode"] == "everything" and g["source"] == "project"
    assert "(this project)" in call(backend, "start_here",
                                    {"project": "Beta Project"}).content[0].text
    call(backend, "review_mode", {"action": "set", "project": "Beta Project", "clear": True,
                                  "user_request": "User: back to normal for Beta"})
    assert _j(call(backend, "review_mode", {"action": "get", "project": 2}))["source"] == "default"
    rows = _q(seeded_db, "SELECT scope, old_mode, new_mode FROM review_mode_log ORDER BY id")
    assert rows == [("project:2", None, "everything"), ("project:2", "everything", None)]


def test_wording_and_remote_policy(backend):
    async def go(c):
        return {t.name: t for t in (await c.list_tools()).tools}

    async def with_client():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="x", version="1")) as c:
            return await go(c)
    tools = run(with_client())
    d = tools["manage_review"].description
    assert "set_mode: ONLY call this when the user has explicitly told you" in d and "user_request" in d
    assert "change the mode to get your own writes saved" in d
    assert "never change it unless the user asks" in backend.server.instructions
    from flowstate.remote_auth import refusal
    from flowstate.surface import translate
    assert refusal("review_mode", {"action": "set"}, ["flowstate:read"])[0] == "read_only_client"
    assert refusal("review_mode", {"action": "get"}, ["flowstate:read"]) is None
    # the public name is checked as the same internal operation
    assert refusal(*translate("manage_review", {"action": "set_mode"}),
                   ["flowstate:read"])[0] == "read_only_client"
    assert refusal(*translate("manage_review", {"action": "get_mode"}), ["flowstate:read"]) is None
    # secrets in the quote are refused like any write
    r = call(backend, "review_mode", {"action": "set", "mode": "off",
                                      "user_request": "password=hunter2hunter2hunter2"})
    assert r.isError and _j(r)["error"] == "secret_detected"
