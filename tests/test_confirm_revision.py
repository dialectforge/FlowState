"""2.4.9: a confirmation belongs to the words that were approved (provenance.py, migration 025).

A confirm (source_set_by = user) stores a hash of those words. Editing the words drops it
back to an AI guess and queues a fresh question. A small set of edits keeps it: a to-do's
status, priority and completed_at; what blocks it; resolving a problem or changing its
severity; marking a variable sensitive; pointing a method at a component; moving a record
to another project or component. An approval queued for older words is refused. The check
sits on the one approve path used by the MCP review tools, the command line, the review
page, the phone page, a popup and a card. Temp databases only.
"""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import history, popup, provenance, review, review_apply
from flowstate.review import ReviewError
from flowstate.review_page import Page


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return _j(asyncio.run(go()))


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def meta(db, rtype, rid):
    rows = _q(db, "SELECT source, source_set_by, confirmed_hash FROM record_meta "
                  "WHERE record_type = ? AND record_id = ?", rtype, rid)
    return rows[0] if rows else None


def words(db, table, rid):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (rid,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def guess(S, kind="learning", summary="Deploys go out on Fridays only", **extra):
    r = call(S, "log_work", {"project": "Alpha", "kind": kind, "summary": summary,
                             "source": "ai_inferred", **extra})
    assert r.get("record", {}).get("id"), r
    return r["record"]["id"]


def confirm(S, db, rtype, rid):
    """The user confirms an AI guess. Returns (review item id, hash of the words)."""
    out = call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                    "record_type": rtype, "record_id": rid,
                                    "source": "user_stated"})
    item = out["confirm_requested"]
    done = popup.as_user("review_approve", {
        "item_id": item,
        "user_request": "The user confirmed this in their own terminal."}, "command-line")
    assert done.get("approved") == item, done
    src, by, hashed = meta(db, rtype, rid)
    assert (src, by) == ("user_stated", "user") and hashed
    table = {"learning": "learnings", "todo": "todos", "problem": "problems",
             "variable": "project_variables", "method": "project_methods"}[rtype]
    assert hashed == provenance.content_hash(rtype, words(db, table, rid))
    return item, hashed


def pending_source(db, rtype, rid):
    rows = _q(db, "SELECT id, payload FROM review_items WHERE kind = 'source' AND status = 'pending' "
                  "AND json_extract(payload, '$.record_type') = ? "
                  "AND json_extract(payload, '$.record_id') = ?", rtype, rid)
    return [(i, json.loads(p).get("content_hash")) for i, p in rows]


# ---------------------------------------------------------------- the confirmation is bound
def test_confirming_stores_a_hash_of_the_words(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S)
    _item, hashed = confirm(S, db, "learning", rid)
    body = json.loads(_q(db, "SELECT payload FROM review_items WHERE id = ?", _item)[0][0])
    assert body["content_hash"] == hashed


def test_backfill_keeps_a_confirmation_made_before_the_column(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, summary="Ship on Friday")
    confirm(S, db, "learning", rid)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE record_meta SET confirmed_hash = NULL "
                 "WHERE record_type = 'learning' AND record_id = ?", (rid,))
    conn.commit()
    conn.close()
    assert provenance.backfill_confirmed_hashes(db) == 1
    hashed = meta(db, "learning", rid)[2]
    assert hashed == provenance.content_hash("learning", words(db, "learnings", rid))
    assert provenance.backfill_confirmed_hashes(db) == 0


# ---------------------------------------------------------------- words change: the confirm goes, and the question is asked again
def test_editing_the_words_drops_the_confirm_and_requeues_it(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="todo", summary="Ship the notes")
    confirm(S, db, "todo", rid)
    out = call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": rid,
                                   "title": "Ship the notes later"})
    assert "error" not in out, out
    assert meta(db, "todo", rid)[:2] == ("ai_inferred", "Cursor")
    assert meta(db, "todo", rid)[2] is None
    waiting = pending_source(db, "todo", rid)
    assert len(waiting) == 1
    assert waiting[0][1] == provenance.content_hash("todo", words(db, "todos", rid))
    done = popup.as_user("review_approve", {
        "item_id": waiting[0][0],
        "user_request": "The user confirmed the new words in their own terminal."}, "command-line")
    assert done.get("approved") == waiting[0][0], done
    assert meta(db, "todo", rid)[0] == "user_stated"
    assert meta(db, "todo", rid)[2] == waiting[0][1]


def test_an_app_tag_is_not_cleared_when_the_words_change(backend):
    """Only a confirmation the user made (source_set_by = user) is tied to the words."""
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Ship the notes",
                             "source": "user_stated"})
    rid = r["record"]["id"]
    assert meta(db, "todo", rid)[:2] == ("user_stated", "Cursor")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": rid,
                             "title": "Ship the notes later"})
    assert meta(db, "todo", rid)[:2] == ("user_stated", "Cursor")
    assert meta(db, "todo", rid)[2] is None


def test_undo_of_the_words_does_not_silently_restore_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="todo", summary="Ship the notes")
    confirm(S, db, "todo", rid)
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": rid,
                             "title": "Ship the notes later"})
    undone = call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                        "record_type": "todo", "record_id": rid})
    assert undone.get("undone"), undone
    assert _q(db, "SELECT title FROM todos WHERE id = ?", rid) == [("Ship the notes",)]
    assert meta(db, "todo", rid)[0] == "ai_inferred"
    waiting = pending_source(db, "todo", rid)
    assert len(waiting) == 1
    assert waiting[0][1] == provenance.content_hash("todo", words(db, "todos", rid))


# ---------------------------------------------------------------- edits that keep the confirmation
def test_todo_status_priority_and_blockers_keep_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="todo", summary="Ship the notes")
    other = guess(S, kind="todo", summary="Write the notes")
    _item, hashed = confirm(S, db, "todo", rid)
    for args in ({"status": "done"}, {"priority": "urgent"},
                 {"status": "pending", "priority": "low"}):
        out = call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": rid,
                                       **args})
        assert "error" not in out, out
        assert meta(db, "todo", rid) == ("user_stated", "user", hashed)
    blocked = call(S, "manage_todos", {"action": "block", "project": "Alpha", "todo_id": rid,
                                       "blocked_by": [other]})
    assert "error" not in blocked, blocked
    assert meta(db, "todo", rid) == ("user_stated", "user", hashed)
    assert pending_source(db, "todo", rid) == []


def test_resolving_a_problem_or_changing_its_severity_keeps_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="problem", summary="The notes page is blank")
    _item, hashed = confirm(S, db, "problem", rid)
    with history.tracked(db, [("problems", "id", rid)], "problem/severity", "Cursor"):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE problems SET severity = 'critical' WHERE id = ?", (rid,))
        conn.commit()
        conn.close()
    assert meta(db, "problem", rid) == ("user_stated", "user", hashed)
    solved = call(S, "manage_problems", {"action": "solve", "project": "Alpha", "problem_id": rid,
                                         "summary": "The template was missing"})
    assert "error" not in solved, solved
    assert _q(db, "SELECT status FROM problems WHERE id = ?", rid) == [("solved",)]
    assert meta(db, "problem", rid) == ("user_stated", "user", hashed)


def test_editing_a_problem_title_drops_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="problem", summary="The notes page is blank")
    confirm(S, db, "problem", rid)
    with history.tracked(db, [("problems", "id", rid)], "problem/title", "Cursor"):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE problems SET title = ? WHERE id = ?", ("The notes page 404s", rid))
        conn.commit()
        conn.close()
    assert meta(db, "problem", rid)[0] == "ai_inferred"
    assert pending_source(db, "problem", rid)


def test_marking_a_variable_sensitive_keeps_the_confirm_and_editing_it_does_not(backend):
    S, db = backend, backend.backend.db_path
    made = call(S, "manage_variables", {"action": "create", "project": "Alpha", "name": "port",
                                        "value": "8766", "description": "the review page"})
    rid = made["id"]
    _item, hashed = confirm(S, db, "variable", rid)
    kept = call(S, "manage_variables", {"action": "update", "project": "Alpha",
                                        "variable_id": rid, "is_secret": True})
    assert "error" not in kept, kept
    assert meta(db, "variable", rid) == ("user_stated", "user", hashed)
    changed = call(S, "manage_variables", {"action": "update", "project": "Alpha",
                                           "variable_id": rid, "description": "a different port"})
    assert "error" not in changed, changed
    assert meta(db, "variable", rid)[0] == "ai_inferred"
    assert pending_source(db, "variable", rid)


def test_pointing_a_method_at_a_component_keeps_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    comp = call(S, "manage_projects", {"action": "add_component", "project": "Alpha",
                                       "name": "Notes"})
    made = call(S, "manage_methods", {"action": "create", "project": "Alpha", "name": "publish",
                                      "description": "how the notes go out"})
    rid = made["id"]
    _item, hashed = confirm(S, db, "method", rid)
    kept = call(S, "manage_methods", {"action": "update", "project": "Alpha", "method_id": rid,
                                      "related_component_id": comp["id"]})
    assert "error" not in kept, kept
    assert meta(db, "method", rid) == ("user_stated", "user", hashed)
    changed = call(S, "manage_methods", {"action": "update", "project": "Alpha", "method_id": rid,
                                         "description": "how the notes go out on Fridays"})
    assert "error" not in changed, changed
    assert meta(db, "method", rid)[0] == "ai_inferred"


def test_moving_to_another_project_keeps_the_confirm(backend):
    S, db = backend, backend.backend.db_path
    rid = guess(S, kind="todo", summary="Ship the notes")
    other = call(S, "manage_projects", {"action": "create", "name": "Beta shelf"})
    _item, hashed = confirm(S, db, "todo", rid)
    with history.tracked(db, [("todos", "id", rid)], "cleanup/move", "Cursor"):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE todos SET project_id = ? WHERE id = ?", (other["id"], rid))
        conn.commit()
        conn.close()
    assert meta(db, "todo", rid) == ("user_stated", "user", hashed)
    assert pending_source(db, "todo", rid) == []


# ---------------------------------------------------------------- a stale approval is refused, on every approve path
def _stale_question(S, db):
    rid = guess(S, summary="Deploys go out on Fridays only")
    out = call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                    "record_type": "learning", "record_id": rid,
                                    "source": "user_stated"})
    item = out["confirm_requested"]
    conn = sqlite3.connect(db)
    conn.execute("UPDATE learnings SET insight = ? WHERE id = ?",
                 ("Deploys go out on Mondays only", rid))
    conn.commit()
    conn.close()
    return rid, item


def test_the_command_line_refuses_a_stale_confirmation(backend):
    from flowstate import server as Srv
    S, db = backend, backend.backend.db_path
    rid, item = _stale_question(S, db)
    row = review.get_item(db, item)
    out = Srv._review_decide("review_approve", row["project_id"], {
        "item_id": item, "user_request": "The user confirmed this in their own terminal."},
        "command-line")
    assert out["error"] == "stale_approval", out
    assert "different words" in out["message"]
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("rejected",)]
    assert meta(db, "learning", rid)[0] == "ai_inferred"


def test_the_review_page_refuses_a_stale_confirmation(backend):
    S, db = backend, backend.backend.db_path
    rid, item = _stale_question(S, db)
    page = Page(db)
    message = page.approve({"item_id": str(item)})
    assert "different words" in message
    assert meta(db, "learning", rid)[0] == "ai_inferred"
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("rejected",)]


def test_a_card_approve_refuses_a_stale_confirmation(backend):
    S, db = backend, backend.backend.db_path
    rid, item = _stale_question(S, db)
    row = review.get_item(db, item)
    try:
        review_apply.approve(db, row, {}, "The user tapped Approve on the card.", "card (Cursor)",
                             {}, path="card")
    except ReviewError as e:
        assert e.payload["error"] == "stale_approval"
    else:
        raise AssertionError("a stale card approval was applied")
    assert meta(db, "learning", rid)[0] == "ai_inferred"
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("rejected",)]


def test_a_popup_approve_refuses_a_stale_confirmation(backend):
    S, db = backend, backend.backend.db_path
    rid, item = _stale_question(S, db)
    done = popup.as_user("review_approve", {
        "item_id": item, "user_request": "The user approved this on their phone."}, "phone")
    assert done["error"] == "stale_approval", done
    assert meta(db, "learning", rid)[:2] == ("ai_inferred", "Cursor")
