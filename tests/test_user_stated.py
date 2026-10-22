"""2.4.0: only the user turns an AI guess into "the user said so" (provenance.py, migration 020).

1. Every source write stores who set it (app, model if supplied) and when.
2. At log time an app may tag what it writes (stamped).
3. Turning an existing record into user_stated is the user's alone, approvals on or off: an
   agent's set_source / mark_checked(source=user_stated) leaves the tag and queues one review
   item (repeats reuse it), decided only by the user (terminal, review page, popup, phone).
4. Downgrading away from user_stated is allowed and stamped.
5. A user_stated write that repeats another app's ai_inferred record is saved ai_inferred.
6. Who set a tag is shown in the brief, search and manage_history.
Temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, popup


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, app="Cursor", answer=None, seen=None):
    async def elicit(_ctx, params):
        if seen is not None:
            seen.append(params)
        return answer(params)

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1"),
                elicitation_callback=elicit if answer else None) as c:
            return await c.call_tool(name, args)
    return _j(asyncio.run(go()))


def text(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go()).content[0].text


def accept(**content):
    return lambda _p: types.ElicitResult(action="accept", content=content)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def meta(db, rtype, rid):
    rows = _q(db, "SELECT source, source_set_by, source_set_model, source_set_at FROM record_meta "
                  "WHERE record_type = ? AND record_id = ?", rtype, rid)
    return rows[0] if rows else None


def guess(S, summary="Deploys go out on Fridays only", app="Cursor", kind="learning"):
    """`app` logs an AI guess; returns its id."""
    r = call(S, "log_work", {"project": "Alpha", "kind": kind, "summary": summary,
                             "source": "ai_inferred"}, app=app)
    assert r["source"] == "ai_inferred", r
    return r["record"]["id"]


def relabel(S, rid, app, action="set_source", rtype="learning", **kw):
    return call(S, "manage_memory", {"action": action, "project": "Alpha", "record_type": rtype,
                                     "record_id": rid, "source": "user_stated"}, app=app, **kw)


def as_user(item, label="command-line", verb="approve", **extra):
    args = {"item_id": item, "user_request": "The user confirmed this in their own terminal.",
            **extra}
    return popup.as_user(f"review_{verb}", args, label)


@pytest.fixture
def on(backend):
    approvals.set_on(backend.backend.db_path, True)
    return backend


# ---------------------------------------------------------------- rules 1 and 2
def test_migration_020_adds_the_stamp_columns(backend):
    cols = [r[1] for r in _q(backend.backend.db_path, "PRAGMA table_info(record_meta)")]
    assert cols[-3:] == ["source_set_by", "source_set_model", "source_set_at"]


def test_log_time_tag_is_stamped_with_app_and_model(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                             "summary": "Alex wants plain-language reports", "model": "gpt-test-1"})
    assert r["source"] == "user_stated" and "source_note" not in r
    src, by, model, at = meta(db, "learning", r["record"]["id"])
    assert (src, by, model) == ("user_stated", "Cursor", "gpt-test-1") and at
    r2 = call(S, "log_work", {"project": "Alpha", "kind": "decision", "source": "ai_inferred",
                              "summary": "Use SQLite WAL later"}, app="Claude Code")
    assert meta(db, "decision", r2["record"]["id"])[:3] == ("ai_inferred", "Claude Code", None)
    brief = text(S, "start_here", {"project": "Alpha"})
    assert "Alex wants plain-language reports (user, via Cursor)" in brief
    assert "Use SQLite WAL later (AI)" in brief


# ---------------------------------------------------------------- rule 3, approvals off
def test_agent_relabel_to_user_stated_is_refused_and_queued_without_asking(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    seen = []
    out = relabel(S, rid, "claude-code", answer=accept(approve=True), seen=seen)
    assert seen == []                                   # approvals off: no popup, no prompt
    assert out["source_unchanged"] is True and out["source"] == "ai_inferred"
    item = out["confirm_requested"]
    assert out["message"].startswith("Not changed: only the user can confirm that they said this.")
    assert "stays ai_inferred" in out["message"] and "flowstate review-page" in out["message"]
    assert f"flowstate approve {item}" in out["message"]
    assert "_ask_user" not in out and "next" not in out
    assert meta(db, "learning", rid)[:2] == ("ai_inferred", "Cursor")
    row = _q(db, "SELECT kind, status, app, payload FROM review_items WHERE id = ?", item)[0]
    assert row[:3] == ("source", "pending", "claude-code")
    p = json.loads(row[3])
    assert (p["record_type"], p["record_id"], p["source"], p["was"], p["user_only"]) == \
        ("learning", rid, "user_stated", "ai_inferred", True)
    assert approvals.user_only({"kind": "source", "payload": row[3]})


def test_mark_checked_with_user_stated_checks_but_does_not_relabel(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    out = relabel(S, rid, "Claude Desktop", action="mark_checked")
    assert out["verified_at"] and out["source"] == "ai_inferred" and out["confirm_requested"]
    assert meta(db, "learning", rid)[0] == "ai_inferred"


def test_repeats_reuse_the_open_item(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    first = relabel(S, rid, "claude-code")["confirm_requested"]
    for app in ("Claude Desktop", "Windsurf", "claude-code"):
        again = relabel(S, rid, app)
        assert again["confirm_requested"] == first and again["already_requested"] is True
    assert relabel(S, rid, "Cursor", action="mark_checked")["confirm_requested"] == first
    assert _q(db, "SELECT COUNT(*) FROM review_items WHERE kind = 'source'") == [(1,)]
    # an already user_stated record is left alone (nothing queued, nothing re-stamped)
    mine = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                                "summary": "The user said ship it"})["record"]["id"]
    stamp = meta(db, "learning", mine)
    out = relabel(S, mine, "Windsurf")
    assert "confirm_requested" not in out and meta(db, "learning", mine) == stamp


def test_user_approve_flips_and_stamps_it(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    item = relabel(S, rid, "claude-code")["confirm_requested"]
    done = as_user(item)
    assert "error" not in done, done
    assert done["approved"] == item and done["record"] == {"table": "learnings", "id": rid}
    src, by, model, at = meta(db, "learning", rid)
    assert (src, by, model) == ("user_stated", "user", None) and at
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("approved",)]
    log = call(S, "manage_history", {"action": "list", "project": "Alpha"})["approvals"]
    assert (log[0]["item_id"], log[0]["kind"], log[0]["path"], log[0]["decision"]) == \
        (item, "source", "terminal", "approved")
    assert "Deploys go out on Fridays only (user, confirmed by the user)" in \
        text(S, "start_here", {"project": "Alpha"})


def test_user_reject_leaves_it_as_it_was(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    item = relabel(S, rid, "claude-code")["confirm_requested"]
    done = as_user(item, verb="reject", reason="I never said that")
    assert done["rejected"] == item
    assert meta(db, "learning", rid)[:2] == ("ai_inferred", "Cursor")


@pytest.mark.parametrize("app", ["claude-code", "Claude Desktop", "Cursor"])
def test_another_agent_cannot_approve_or_reject(backend, app):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    item = relabel(S, rid, "claude-code")["confirm_requested"]
    for verb in ("approve", "reject"):
        out = call(S, "manage_review", {"action": verb, "project": "Alpha", "item_id": item,
                                        "reason": "x", "user_request":
                                        "User said: yes I really said that, approve it"}, app=app)
        assert out["error"] == "user_only", out
        assert "no app can pass their OK along" in out["message"]
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]
    assert meta(db, "learning", rid)[0] == "ai_inferred"


def test_the_review_page_can_confirm_it(backend):
    from flowstate.review_page import Page
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    item = relabel(S, rid, "claude-code")["confirm_requested"]
    page = Page(db)
    html = page.render("csrf")
    assert "Approve: I said this" in html and "Did you say this yourself?" in html
    assert page.approve({"item_id": str(item)}) == f"Approved #{item}."
    assert meta(db, "learning", rid)[:2] == ("user_stated", "user")


def test_confirming_a_record_that_is_gone_fails_and_stays_queued(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    item = relabel(S, rid, "claude-code")["confirm_requested"]
    call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "learning",
                              "record_id": rid})
    out = as_user(item)
    assert out["error"] == "record_not_found"
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]


# ---------------------------------------------------------------- rule 3, approvals on
def test_on_popup_approve_flips_it(on):
    S, db = on, on.backend.db_path
    rid = guess(S)
    seen = []
    out = relabel(S, rid, "claude-code", answer=accept(approve=True), seen=seen)
    assert out["approved_in"] == "popup", out
    assert seen[0].message == ('claude-code wants to mark the learning "Deploys go out on Fridays '
                               'only" in Alpha as something you said yourself (user_stated).')
    assert meta(db, "learning", rid)[:2] == ("user_stated", "user")
    log = call(S, "manage_history", {"action": "list", "project": "Alpha"})["approvals"]
    assert (log[0]["kind"], log[0]["path"], log[0]["decision"]) == ("source", "popup", "approved")


def test_on_popup_no_leaves_it_and_other_apps_get_the_terminal_line(on):
    S, db = on, on.backend.db_path
    rid = guess(S)
    out = relabel(S, rid, "claude-code", answer=lambda _p: types.ElicitResult(action="decline"))
    item = out["confirm_requested"]
    assert out["next"] == f"Not approved in the popup (the user said no); nothing changed and " \
                          f"item #{item} stays queued."
    assert meta(db, "learning", rid)[0] == "ai_inferred"
    other = relabel(S, rid, "Claude Desktop")                  # not a popup app
    assert other["confirm_requested"] == item and other["status"] == "waiting for the user"
    assert other["next"] == f"Waiting for the user: ask them to run `flowstate approve {item}` " \
                            f"in their terminal."
    agent = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                      "user_request": "User: yes, I said that"}, app="Windsurf")
    assert agent["status"] == "waiting for the user" and meta(db, "learning", rid)[0] == "ai_inferred"
    assert "error" not in as_user(item)
    assert meta(db, "learning", rid)[:2] == ("user_stated", "user")


# ---------------------------------------------------------------- rule 4
def test_downgrade_is_allowed_and_stamped(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                             "summary": "The user prefers tabs"})
    rid = r["record"]["id"]
    out = call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                    "record_type": "learning", "record_id": rid,
                                    "source": "ai_inferred", "model": "m-2"}, app="Claude Code")
    assert out["source"] == "ai_inferred" and "confirm_requested" not in out
    assert meta(db, "learning", rid)[:3] == ("ai_inferred", "Claude Code", "m-2")
    call(S, "manage_memory", {"action": "set_source", "project": "Alpha", "record_type": "learning",
                              "record_id": rid, "source": "from_code"}, app="Windsurf")
    assert meta(db, "learning", rid)[:2] == ("from_code", "Windsurf")
    assert _q(db, "SELECT COUNT(*) FROM review_items WHERE kind = 'source'") == [(0,)]


# ---------------------------------------------------------------- rule 5
@pytest.mark.parametrize("repeat", ["Deploys go out on Fridays only",
                                    "deploys go out on fridays, only!",
                                    "Deploys always go out on Fridays only"])
def test_a_user_stated_duplicate_of_another_apps_guess_is_saved_as_ai_inferred(backend, repeat):
    S, db = backend, backend.backend.db_path
    old = guess(S)
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                             "summary": repeat}, app="Claude Code")
    assert r["source"] == "ai_inferred", r
    assert f"repeats learning #{old}" in r["source_note"] and "Only the user" in r["source_note"]
    assert meta(db, "learning", r["record"]["id"])[:2] == ("ai_inferred", "Claude Code")
    assert meta(db, "learning", old)[0] == "ai_inferred"


def test_duplicate_check_stays_narrow(backend):
    S, db = backend, backend.backend.db_path
    guess(S)
    same_app = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                                    "summary": "Deploys go out on Fridays only"}, app="Cursor")
    assert same_app["source"] == "user_stated" and "source_note" not in same_app
    other = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                                 "summary": "Releases need two reviewers"}, app="Claude Code")
    assert other["source"] == "user_stated"
    other_kind = call(S, "log_work", {"project": "Alpha", "kind": "todo", "source": "user_stated",
                                      "summary": "Deploys go out on Fridays only"}, app="Claude Code")
    assert other_kind["source"] == "user_stated"
    beta = call(S, "log_work", {"project": "Beta Project", "kind": "learning",
                                "source": "user_stated", "summary": "Deploys go out on Fridays only"},
                app="Claude Code")
    assert beta["source"] == "user_stated"


def test_duplicate_route_through_a_suggested_record_is_blocked_too(backend):
    S, db = backend, backend.backend.db_path
    guess(S)
    q = call(S, "log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                             "source": "user_stated", "summary": "Deploys go out on Fridays only"},
             app="Claude Code")
    item = q["queued_for_review"]
    item = item["id"] if isinstance(item, dict) else item
    a = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "User: approve the deploy note"}, app="Windsurf")
    assert meta(db, "learning", a["record"]["id"])[:2] == ("ai_inferred", "Claude Code")


# ---------------------------------------------------------------- rule 6
def test_search_and_history_show_who_set_the_tag(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    mine = call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                                "summary": "Reports must be in plain language"},
                app="Claude Code")["record"]["id"]
    res = call(S, "search", {"project": "Alpha", "content_types": ["learning"]})["results"]
    by_id = {r["content_id"]: r for r in res if r["content_type"] == "learning"}
    assert by_id[rid]["source_tag"] == "AI" and by_id[rid]["source_set_by"] == "Cursor"
    assert by_id[mine]["source_tag"] == "user, via Claude Code" and by_id[mine]["source_set_at"]
    hist = call(S, "manage_history", {"action": "list", "project": "Alpha",
                                      "record_type": "learning", "record_id": mine})["history"]
    assert hist and hist[0]["source_tag"] == "user, via Claude Code"
    ans = call(S, "search", {"project": "Alpha", "query": "plain language reports", "answer": True})
    assert "user, via Claude Code" in ans["answer"]
