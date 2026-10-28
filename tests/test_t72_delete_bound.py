"""Task 72: a delete approval is bound to the exact rows FlowState worked out from the
database when the delete was asked for. If that list changes before the user approves (a
row added, removed or edited), the approval is void: nothing is deleted, the request is
closed, and asking again makes a new request. The item's summary or an edit to it never
changes what goes, and a used or closed request can't be approved again. Temp DBs only."""

import json

from flowstate import approvals, deletes

from test_deletes import ASK, _j, _pending, _q, _request, call, dump, rich  # noqa: F401


def _payload(db, item):
    return json.loads(_q(db, "SELECT payload FROM review_items WHERE id = ?", item)[0][0])


def _approve(S, item, **extra):
    return _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "user_request": ASK, **extra}))


def _status(db, item):
    return _q(db, "SELECT status FROM review_items WHERE id = ?", item)[0][0]


def _deleted_rows(db, edit_id):
    return sorted((t, k) for t, k in _q(db, "SELECT table_name, record_id FROM record_history "
                                            "WHERE edit_id = ?", edit_id))


def _at_once(S, db, args):
    """Another app's delete with approvals off for a moment (goes through at once)."""
    approvals.set_on(db, False)
    try:
        out = _j(call(S, "manage_memory", args))
        assert "deleted" in out, out
    finally:
        approvals.set_on(db, True)


def test_the_request_stores_the_exact_rows_and_approval_deletes_only_those(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                        "record_id": 1})
    bound = _payload(db, item)["bound"]
    assert bound["rows"] == {"problems": [1], "record_links": bound["rows"]["record_links"],
                             "solution_attempts": [1], "solutions": [1]}
    assert bound["digest"] and len(bound["digest"]) == 64
    out = _approve(S, item)
    assert out.get("deleted", {}).get("id") == 1, out
    went = {(t, str(k)) for t, k in _deleted_rows(db, out["edit_id"])
            if t in deletes._BOUND or t == "problems"}
    listed = {(t, str(k)) for t, ks in bound["rows"].items() for k in ks}
    assert went == listed


def test_same_count_but_other_rows_voids_the_approval(rich, seeded_db):
    """A component's change swapped for another: the count is the same, the list is not."""
    S, db = rich, seeded_db
    changes = [r[0] for r in _q(db, "SELECT ch.id FROM changes ch JOIN components c ON "
                                    "c.id = ch.component_id WHERE c.id = 1 ORDER BY ch.id")]
    item = _request(S, {"tool": "manage_projects", "action": "delete_component",
                        "project": "Alpha", "component_id": 1})
    _at_once(S, db, {"action": "delete", "project": "Alpha", "record_type": "change",
                     "record_id": changes[0]})
    assert not _j(call(S, "log_work", {"project": "Alpha", "kind": "change", "component": "Core",
                                       "summary": "a different change"})).get("error")
    before = dump(db)
    out = _approve(S, item)
    assert out["error"] == "changed_since_request", out
    assert f"no longer deletes change #{changes[0]}" in out["message"]
    assert "now also deletes change #" in out["message"]
    assert dump(db) == before                                  # nothing deleted
    assert _status(db, item) == "rejected"                     # void: closed, not pending
    again = _approve(S, item)
    assert again["error"] == "already_decided"
    fresh = _request(S, {"tool": "manage_projects", "action": "delete_component",
                         "project": "Alpha", "component_id": 1})
    assert fresh != item
    assert "deleted" in _approve(S, fresh)


def test_an_edited_row_voids_the_approval(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                        "record_id": 1})
    assert not _j(call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1,
                                           "title": "now about something else"})).get("error")
    before = dump(db)
    out = _approve(S, item)
    assert out["error"] == "changed_since_request" and "edited" in out["message"], out
    assert dump(db) == before and _status(db, item) == "rejected"


def test_a_removed_child_voids_the_approval(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                        "record_id": 1})
    link = _payload(db, item)["bound"]["rows"]["record_links"][0]
    _at_once(S, db, {"action": "delete", "project": "Alpha", "record_type": "link",
                     "record_id": link})
    out = _approve(S, item)
    assert out["error"] == "changed_since_request" and f"#{link}" in out["message"], out
    assert _q(db, "SELECT COUNT(*) FROM problems WHERE id = 1")[0][0] == 1


def test_asking_again_after_a_change_replaces_the_stale_request(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                        "record_id": 1})
    same = _j(call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                        "record_type": "problem", "record_id": 1}))
    assert same["delete_requested"] == item and same["already_requested"] is True
    call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": 1,
                                "description": "another try"})
    new = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                       "record_id": 1})
    assert new != item
    assert _status(db, item) == "rejected"
    reason, by = _q(db, "SELECT reject_reason, decided_by FROM review_items WHERE id = ?", item)[0]
    assert "changed since this request" in reason and "now also deletes attempt #" in reason
    assert by == "flowstate (automatic)"
    assert _approve(S, item)["error"] == "already_decided"
    out = _approve(S, new)
    assert out["deleted"]["with"]["attempts"] == 2


def test_the_summary_or_an_edit_can_not_widen_or_change_the_list(rich, seeded_db):
    S, db = rich, seeded_db
    todos = [r[0] for r in _q(db, "SELECT id FROM todos WHERE project_id = 1 ORDER BY id")]
    assert len(todos) >= 2
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                        "record_id": todos[0]})
    # the stored wording says more than the request: it changes nothing
    conn_text = f"Delete todo #{todos[0]} and todo #{todos[1]} and the whole project"
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE review_items SET summary = ?, details = ? WHERE id = ?",
                 (conn_text, conn_text, item))
    conn.commit()
    conn.close()
    out = _approve(S, item, summary=conn_text, details="also delete todo " + str(todos[1]))
    assert out["deleted"]["id"] == todos[0], out
    left = {r[0] for r in _q(db, "SELECT id FROM todos WHERE project_id = 1")}
    assert todos[0] not in left and todos[1] in left
    assert _q(db, "SELECT COUNT(*) FROM projects WHERE id = 1")[0][0] == 1
    assert _payload(db, item)["bound"]["rows"]["todos"] == [todos[0]]


def test_a_used_approval_can_not_be_used_again(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                        "record_id": 1})
    assert "deleted" in _approve(S, item)
    undo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "todo", "record_id": 1}))
    assert "error" not in undo, undo
    before = dump(db)
    again = _approve(S, item)            # same rows are back: still a used approval
    assert again["error"] == "already_decided"
    assert dump(db) == before


def test_a_request_without_a_bound_list_is_refused_and_closed(rich, seeded_db):
    """A delete queued by an older FlowState (counts only) is not approvable any more."""
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                        "record_id": 1})
    p = _payload(db, item)
    p.pop("bound")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.execute("UPDATE review_items SET payload = ? WHERE id = ?", (json.dumps(p), item))
    conn.commit()
    conn.close()
    before = dump(db)
    out = _approve(S, item)
    assert out["error"] == "request_outdated", out
    assert dump(db) == before and _status(db, item) == "rejected"
    fresh = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                         "record_id": 1})
    assert fresh != item and "bound" in _payload(db, fresh)
    assert (fresh, "delete", "pending") in _pending(db)


def test_the_review_page_preview_says_when_the_list_changed(rich, seeded_db):
    S, db = rich, seeded_db
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo",
                        "record_id": 1})
    from flowstate import review
    got = deletes.preview(db, review.get_item(db, item))
    assert got["changed_since_request"] is False and "not the one" not in got["plain"]
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1,
                             "title": "renamed while it waits"})
    got = deletes.preview(db, review.get_item(db, item))
    assert got["changed_since_request"] is True
    assert "not the one the request was made with (a record on the list was edited)" in got["plain"]


def test_asking_again_on_the_same_connection_gets_a_new_request(rich, seeded_db):
    """The repeat guard (receipts.py) must not hand back the closed request's answer."""
    import asyncio
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session
    S, db = rich, seeded_db
    args = {"project": "Alpha", "action": "delete", "record_type": "todo", "record_id": 1}

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            first = json.loads((await c.call_tool("manage_memory", args)).content[0].text)
            same = json.loads((await c.call_tool("manage_memory", args)).content[0].text)
            import sqlite3   # another app edits the todo meanwhile
            conn = sqlite3.connect(db)
            conn.execute("UPDATE todos SET title = 'edited while it waits' WHERE id = 1")
            conn.commit()
            conn.close()
            assert _approve(S, first["delete_requested"])["error"] == "changed_since_request"
            again = json.loads((await c.call_tool("manage_memory", args)).content[0].text)
            return first, same, again
    first, same, again = asyncio.run(go())
    assert same["delete_requested"] == first["delete_requested"] and same["already_requested"]
    assert again.get("delete_requested") not in (None, first["delete_requested"]), again
    assert "deleted" in _approve(S, again["delete_requested"])
