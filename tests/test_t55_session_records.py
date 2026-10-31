"""Task 55 (re-audit 53): a session summary's per-record Remove (N-F19, N-F38, M6, M7, the
`_others_below` fail-open) and the summary line's counts (N-F39). Temp DBs only."""

import json
import sqlite3
import time

import pytest

from flowstate import review, review_apply, safety, session_contents, session_records
from test_session_contents import _q, _work, call


def _entries(db, item):
    return {e["key"]: e for e in session_contents.for_item(db, review.get_item(db, item))["entries"]}


def _decisions(db, item):
    return json.loads(review.get_item(db, item)["payload"]).get("record_decisions") or {}


def _remove(db, item, key):
    return session_records.decide(db, item, key, "remove", "review-page")


def _exec(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, a)
        conn.commit()
    finally:
        conn.close()


def _refused(db, item, key) -> str:
    with pytest.raises(session_records.RecordError) as e:
        _remove(db, item, key)
    return e.value.payload["message"]


# ---------------------------------------------------------------- N-F19
def test_a_slow_remove_keeps_its_marker_fresh(backend, monkeypatch):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    monkeypatch.setattr(session_records, "MARK_SECONDS", 2)
    real, seen = safety.copy_before, {}

    def slow_copy(*a, **kw):   # a big database: the safety copy takes longer than the marker
        time.sleep(3.5)
        seen["busy"] = session_records.busy(review.get_item(db, item))
        try:
            review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})
            seen["approved"] = True
        except review.ReviewError as e:
            seen["refused"] = e.payload["error"]
        return real(*a, **kw)
    monkeypatch.setattr(safety, "copy_before", slow_copy)
    out = _remove(db, item, f"change:{cid}")
    assert seen == {"busy": True, "refused": "busy"}
    assert out["now"] == "removed" and _decisions(db, item)[f"change:{cid}"]["d"] == "removed"
    assert review.get_item(db, item)["status"] == "pending"


def test_remove_and_its_decision_are_saved_together(backend):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    conn = sqlite3.connect(db)   # saving the "removed" decision fails (disk full, say)
    conn.execute("""CREATE TRIGGER t55_fail BEFORE UPDATE OF payload ON review_items
                    WHEN NEW.payload LIKE '%"d": "removed"%'
                    BEGIN SELECT RAISE(ABORT, 'database or disk is full'); END""")
    conn.commit()
    conn.close()
    with pytest.raises(sqlite3.Error):
        _remove(db, item, f"change:{cid}")
    assert _q(db, "SELECT COUNT(*) FROM changes WHERE id = ?", cid) == [(1,)]   # not lost
    assert f"change:{cid}" not in _decisions(db, item)
    assert not session_records.busy(review.get_item(db, item))
    _exec(db, "DROP TRIGGER t55_fail")
    assert "remove" in _entries(db, item)[f"change:{cid}"]["actions"]
    assert _remove(db, item, f"change:{cid}")["now"] == "removed"
    assert "restore" in _entries(db, item)[f"change:{cid}"]["actions"]


def test_a_remove_whose_marker_ran_out_and_was_kept_meanwhile_deletes_nothing(
        backend, monkeypatch):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    real = safety.copy_before

    def kept_meanwhile(*a, **kw):   # the marker ran out (a stopped heartbeat) and Keep all ran
        session_records._write(db, item, f"change:{cid}", {"d": "removing", "at": "2000-01-01 00:00:00"},
                               held=True)
        review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})
        return real(*a, **kw)
    monkeypatch.setattr(safety, "copy_before", kept_meanwhile)
    with pytest.raises(session_records.RecordError) as e:
        _remove(db, item, f"change:{cid}")
    assert e.value.payload["error"] == "decided_meanwhile"
    assert _q(db, "SELECT COUNT(*) FROM changes WHERE id = ?", cid) == [(1,)]
    assert _decisions(db, item)[f"change:{cid}"]["d"] == "kept"   # Keep all's decision stays


# ---------------------------------------------------------------- N-F38
def test_a_handoff_note_and_a_link_the_session_left_can_be_removed(backend):
    S, db = backend, backend.backend.db_path
    h = call(S, "manage_handoffs", {"action": "leave", "project": "Alpha",
                                    "where_i_was": "Halfway through the login rewrite",
                                    "next_step": "finish the tests"})
    ln = call(S, "manage_links", {"action": "add", "project": "Alpha", "link_type": "depends_on",
                                  "item_type": "todo", "item_id": 1, "target_type": "learning",
                                  "target_id": 1})
    item, _pid, _cid = _work(S, db)
    hk, lk = f"handoff:{h['handoff_id']}", f"link:{ln['link']['id']}"
    e = _entries(db, item)
    assert e[hk]["verb"] == "saved" and e[hk]["actions"] == ["keep", "remove"], e[hk]
    assert e[lk]["verb"] == "saved" and e[lk]["actions"] == ["keep", "remove"], e[lk]
    assert _remove(db, item, hk)["now"] == "removed"
    assert _q(db, "SELECT COUNT(*) FROM handoff_notes") == [(0,)]
    assert session_records.decide(db, item, hk, "restore", "review-page")["now"] == ""
    assert _q(db, "SELECT COUNT(*) FROM handoff_notes") == [(1,)]


# ---------------------------------------------------------------- M6
def test_no_remove_when_another_apps_addition_has_no_edit_history(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    assert "remove" in _entries(db, item)[f"change:{cid}"]["actions"]
    # another app links to this session's change (links added on their own have no creation
    # row in the edit history) ...
    r = call(S, "manage_links", {"action": "add", "project": "Alpha", "link_type": "depends_on",
                                 "item_type": "todo", "item_id": 1, "target_type": "change",
                                 "target_id": cid}, app="Cursor")
    assert "link" in r, r
    e = _entries(db, item)[f"change:{cid}"]
    assert e["actions"] == ["keep"] and e.get("others_below"), e
    assert "Another app added to change" in _refused(db, item, f"change:{cid}")
    # ... and adds a fix attempt to its problem, whose history row is then erased
    call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": pid,
                                "description": "Tried clearing the cache"}, app="Cursor")
    _exec(db, "DELETE FROM record_history WHERE table_name = 'solution_attempts'")
    assert _entries(db, item)[f"problem:{pid}"]["actions"] == ["keep"]
    assert _q(db, "SELECT COUNT(*) FROM solution_attempts WHERE problem_id = ?", pid)[0][0] == 1


# ---------------------------------------------------------------- M7
def test_no_remove_once_the_sessions_write_log_is_gone(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    [(sid,)] = _q(db, "SELECT session_id FROM review_items WHERE id = ?", item)
    _exec(db, "DELETE FROM run_writes WHERE session_id = ?", sid)   # pruned after 90 days
    e = _entries(db, item)
    assert e[f"change:{cid}"]["actions"] == ["keep"] and e[f"problem:{pid}"]["actions"] == ["keep"]
    msg = _refused(db, item, f"change:{cid}")
    assert "write log" in msg and "can only be kept" in msg
    assert _q(db, "SELECT COUNT(*) FROM changes WHERE id = ?", cid) == [(1,)]


# ---------------------------------------------------------------- _others_below
def test_remove_isnt_offered_when_what_it_takes_cant_be_checked(backend, monkeypatch):
    from flowstate import deletes
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)

    def broken(*a, **kw):
        raise sqlite3.OperationalError("database disk image is malformed")
    monkeypatch.setattr(deletes, "plan", broken)
    assert _entries(db, item)[f"change:{cid}"]["actions"] == ["keep"]
    assert "couldn't work out" in _refused(db, item, f"change:{cid}")


# ---------------------------------------------------------------- N-F39
def test_summary_line_counts_what_is_still_there(backend):
    S, db = backend, backend.backend.db_path
    call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "First lesson here"})
    two = call(S, "log_work", {"project": "Alpha", "kind": "learning",
                               "summary": "Second lesson here"})["record"]["id"]
    call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "learning",
                               "record_id": two})
    call(S, "manage_rules", {"action": "add", "project": "Alpha", "rule": "Never push to main"})
    S.backend.tracker.close_instance()
    [(line,)] = _q(db, "SELECT summary FROM review_items WHERE kind = 'session_summary' "
                       "ORDER BY id DESC LIMIT 1")
    assert "saved 1 learning" in line and "2 learnings" not in line, line
    assert "new rule" not in line and "hard rule request" in line, line
