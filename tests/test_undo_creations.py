"""2.4.0 undo: a record a change created can be taken back (when nothing refers to it yet,
and by the app that made it); and when only some rows of an edit changed since, undo puts
the rest back and lists what it skipped and why, instead of refusing all of it."""

import json
import sqlite3

from flowstate import identity, surface


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _call(S, name, args, app="Cursor"):
    name, args = surface.translate(name, dict(args))     # public or internal name
    with identity.use(identity.Identity(app, None, "mac-mini", "self_reported", "conn-" + app)):
        return S.handle_call(name, args, app)


def _undo(S, rtype, rid, app="Cursor", project="Alpha"):
    return _call(S, "history", {"action": "undo", "project": project, "record_type": rtype,
                                "record_id": rid}, app)


def _log(S, kind, summary, app="Cursor", **kw):
    r = _call(S, "log_work", {"project": "Alpha", "kind": kind, "summary": summary, **kw}, app)
    assert "error" not in r, r
    return r["record"]["id"]


def test_undo_removes_a_record_the_change_created(backend):
    S, db = backend, backend.backend.db_path
    did = _log(S, "decision", "Use SQLite for local storage")
    assert _q(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", did) == [(1,)]
    out = _undo(S, "decision", did)
    assert "error" not in out, out
    assert out["rows"][0] == {"record_type": "decision", "record_id": did, "now": "deleted"}
    assert _q(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", did) == [(0,)]
    # undo again = redo: it comes back with the same id
    again = _undo(S, "decision", did)
    assert "error" not in again, again
    assert _q(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", did) == [(1,)]


def test_undo_of_a_creation_takes_its_own_links_along(backend):
    S, db = backend, backend.backend.db_path
    old = _log(S, "decision", "Ship on Fridays")
    new = _log(S, "decision", "Never ship on Fridays", replaces=old, component="Core")
    assert _q(db, "SELECT COUNT(*) FROM record_links WHERE from_type='decision' AND from_id=? "
                  "AND removed_at IS NULL", new) == [(2,)]
    out = _undo(S, "decision", new)
    assert "error" not in out, out
    assert _q(db, "SELECT COUNT(*) FROM record_links WHERE from_type='decision' AND from_id=?",
              new) == [(0,)]
    brief = _call(S, "start_here", {"project": "Alpha"})
    assert "Ship on Fridays" in brief                       # the old decision is current again


def test_undo_of_a_creation_refused_while_something_refers_to_it(backend):
    S, db = backend, backend.backend.db_path
    parent = _log(S, "todo", "Parent task")
    child = _call(S, "todo", {"action": "add", "project": "Alpha", "title": "Child task",
                              "parent_todo_id": parent})
    assert "error" not in child, child
    out = _undo(S, "todo", parent)
    assert out["error"] == "still_referenced", out
    assert "todo" in out["message"] and "Nothing was changed" in out["message"]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = ?", parent) == [(1,)]


def test_undo_of_another_apps_creation_goes_through_the_user(backend):
    S, db = backend, backend.backend.db_path
    lid = _log(S, "learning", "Cursor's lesson", app="Cursor")
    out = _undo(S, "learning", lid, app="Claude Desktop")
    assert out["error"] == "not_your_record", out
    assert "Cursor" in out["message"] and "manage_memory" in out["message"]
    assert _q(db, "SELECT COUNT(*) FROM learnings WHERE id = ?", lid) == [(1,)]


def test_undo_of_a_creation_changed_outside_flowstate_is_refused(backend):
    S, db = backend, backend.backend.db_path
    tid = _log(S, "todo", "Write the docs")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE todos SET title = 'edited elsewhere' WHERE id = ?", (tid,))
    conn.commit()
    conn.close()
    out = _undo(S, "todo", tid)
    assert out["error"] == "changed_since", out
    assert _q(db, "SELECT title FROM todos WHERE id = ?", tid) == [("edited elsewhere",)]


def test_partial_undo_restores_the_rest_and_lists_what_it_skipped(backend):
    S, db = backend, backend.backend.db_path
    # a delete of a problem takes its attempt with it (one edit, several rows)
    att = _call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha",
                                       "problem_id": 1, "description": "try one"})
    assert "error" not in att, att
    done = _call(S, "delete_request", {"action": "record", "project": "Alpha",
                                       "record_type": "problem", "record_id": 1})
    assert "error" not in done and done["deleted"]["record_type"] == "problem", done
    gone = _q(db, "SELECT table_name, record_id FROM record_history WHERE tool = 'delete/problem'")
    assert ("problems", 1) in gone and len(gone) >= 2
    # someone puts a different row back in one of those places outside FlowState
    att_id = next(r for t, r in gone if t == "solution_attempts")
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO solution_attempts (id, problem_id, description) VALUES (?, 1, 'x')",
                 (att_id,))
    conn.commit()
    conn.close()
    out = _undo(S, "problem", 1, app="review-page")
    assert "error" not in out, out
    assert _q(db, "SELECT COUNT(*) FROM problems WHERE id = 1") == [(1,)]
    assert out["skipped"] == [{"record_type": "attempt", "record_id": att_id,
                               "why": out["skipped"][0]["why"]}]
    assert "changed" in out["skipped"][0]["why"]
    assert "skipped 1" in out["note"]


def test_undo_still_refuses_when_every_row_changed(backend):
    S, db = backend, backend.backend.db_path
    _call(S, "todo", {"action": "update", "project": "Alpha", "todo_id": 1, "status": "done"})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE todos SET title = 'edited elsewhere' WHERE id = 1")
    conn.commit()
    conn.close()
    out = _undo(S, "todo", 1)
    assert out["error"] == "changed_since" and "todo #1" in out["message"]


def test_creation_rows_are_not_listed_twice_in_who(backend):
    import asyncio
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session
    S = backend

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            await c.call_tool("log_work", {"project": "Alpha", "kind": "learning",
                                           "summary": "One lesson"})
            r = await c.call_tool("manage_history", {"action": "who", "project": "Alpha"})
            return json.loads(r.content[0].text)
    who = asyncio.run(go())
    created = [r for r in who["rows"] if "learning" in r["what"]]
    assert len(created) == 1, json.dumps(who["rows"], indent=1)
