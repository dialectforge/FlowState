"""Audit 46, M5-M8 and L14: a session summary's Keep / Remove only ever takes back the
session's own work, says so honestly, and records how it was decided. Temp DBs only."""

import json
from datetime import datetime, timedelta, timezone

from flowstate import deletes, history, links, review, review_apply, session_contents, session_records
from test_session_contents import _q, _work, call


def _entries(db, item):
    return {e["key"]: e for e in session_contents.for_item(db, review.get_item(db, item))["entries"]}


def _decisions(db, item):
    return json.loads(review.get_item(db, item)["payload"]).get("record_decisions") or {}


# ---------------------------------------------------------------- M5
def test_keep_all_landing_in_the_middle_of_a_remove_waits_and_the_removal_is_recorded(
        backend, monkeypatch):
    """The audit's forced-timing reproduction: Keep all from elsewhere (another process, the
    terminal) while Remove was between its check and its write. The record was gone, the
    summary approved, and the decisions said "kept"."""
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    real = deletes._carry_out
    seen = {}

    def keep_all_now(*a, **kw):   # what another process does at this moment
        try:
            review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})
            seen["approved"] = True
        except review.ReviewError as e:
            seen["refused"] = e.payload["error"]
        return real(*a, **kw)
    monkeypatch.setattr(deletes, "_carry_out", keep_all_now)
    out = session_records.decide(db, item, f"change:{cid}", "remove", "review-page")
    assert seen == {"refused": "busy"}
    assert out["now"] == "removed" and _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []
    assert _decisions(db, item)[f"change:{cid}"]["d"] == "removed"
    assert review.get_item(db, item)["status"] == "pending"
    monkeypatch.setattr(deletes, "_carry_out", real)
    review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})
    assert review.get_item(db, item)["status"] == "approved"
    assert _decisions(db, item)[f"change:{cid}"]["d"] == "removed"          # not "kept"
    [(note,)] = _q(db, "SELECT summary FROM sessions WHERE id = (SELECT session_id FROM "
                       "review_items WHERE id = ?)", item)
    assert f"change #{cid}" in note and "Removed by the user" in note


def test_a_failed_remove_clears_its_marker(backend, monkeypatch):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)

    def fails(*a, **kw):
        raise deletes.DeleteError("delete_unsupported", "no")
    monkeypatch.setattr(deletes, "_carry_out", fails)
    try:
        session_records.decide(db, item, f"change:{cid}", "remove", "review-page")
    except session_records.RecordError as e:
        assert e.payload["error"] == "delete_unsupported"
    assert f"change:{cid}" not in _decisions(db, item)
    assert not session_records.busy(review.get_item(db, item))
    review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})


def test_a_marker_left_by_a_stopped_process_doesnt_block_for_ever(backend):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    old = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=session_records.MARK_SECONDS + 5)
           ).strftime("%Y-%m-%d %H:%M:%S")
    session_records._write(db, item, f"change:{cid}", {"d": "removing", "at": old})
    assert not session_records.busy(review.get_item(db, item))
    assert _entries(db, item)[f"change:{cid}"]["state"] == ""
    review_apply.approve(db, review.get_item(db, item), {}, "keep all", "terminal", {})


# ---------------------------------------------------------------- M6
def test_no_remove_on_a_record_another_app_added_to_later(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    assert "remove" in _entries(db, item)[f"problem:{pid}"]["actions"]
    call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": pid,
                                "description": "Tried clearing the cache"}, app="Cursor")
    e = _entries(db, item)[f"problem:{pid}"]
    assert e["actions"] == ["keep"] and e.get("others_below")
    try:
        session_records.decide(db, item, f"problem:{pid}", "remove", "review-page")
        raise AssertionError("removed")
    except session_records.RecordError as err:
        assert "Another app added to problem" in err.payload["message"]
    assert _q(db, "SELECT COUNT(*) FROM solution_attempts WHERE problem_id = ?", pid)[0][0] == 1
    assert "remove" in _entries(db, item)[f"change:{cid}"]["actions"]       # the rest as before


# ---------------------------------------------------------------- M7
def test_a_link_that_was_already_there_is_not_the_sessions_to_remove(backend):
    S, db = backend, backend.backend.db_path
    links.add(db, 1, "depends_on", item_type="todo", item_id=1, target_type="learning",
              target_id=1, client="Cursor")
    [(lid,)] = _q(db, "SELECT id FROM record_links")
    call(S, "manage_links", {"action": "add", "project": "Alpha", "link_type": "depends_on",
                             "item_type": "todo", "item_id": 1, "target_type": "learning",
                             "target_id": 1})
    item, _pid, _cid = _work(S, db)
    e = _entries(db, item).get(f"link:{lid}")
    assert e is not None and e["actions"] == ["keep"] and e["verb"] == "already there"
    assert _q(db, "SELECT COUNT(*) FROM record_links")[0][0] == 1


def test_a_record_the_session_deleted_is_listed_as_deleted_and_never_removable(backend):
    S, db = backend, backend.backend.db_path
    out = call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                    "record_id": 1})
    assert "error" not in out, out
    item, _pid, _cid = _work(S, db)
    e = _entries(db, item)["todo:1"]
    assert e["verb"] == "deleted" and "remove" not in e["actions"]
    history.undo(db, 1, "todo", 1, "review-page")                 # the user brings it back
    e = _entries(db, item)["todo:1"]
    assert not e["gone"] and "remove" not in e["actions"]
    text = "\n".join(session_contents.lines(session_contents.for_item(db, review.get_item(db, item))))
    assert "deleted" in text


# ---------------------------------------------------------------- M8
def test_the_sessions_own_slow_last_update_is_not_someone_elses_change(backend):
    S, db = backend, backend.backend.db_path
    item, pid, _cid = _work(S, db)
    [(ended,)] = _q(db, "SELECT ended_at FROM sessions WHERE id = (SELECT session_id FROM "
                        "review_items WHERE id = ?)", item)
    later = (datetime.strptime(ended[:19], "%Y-%m-%d %H:%M:%S") + timedelta(seconds=2)
             ).strftime("%Y-%m-%d %H:%M:%S")
    import sqlite3
    conn = sqlite3.connect(db)   # the solve's history row landed after the call began
    conn.execute("UPDATE record_history SET created_at = ? WHERE table_name = 'problems' AND "
                 "record_id = ? AND old_row IS NOT NULL", (later, pid))
    conn.commit()
    conn.close()
    e = _entries(db, item)[f"problem:{pid}"]
    assert e["state"] != "changed" and e["actions"] == ["keep", "remove"]


# ---------------------------------------------------------------- L14
def test_keep_all_records_the_real_path(backend):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    review_apply.for_card(db, "claude-ai")(review.get_item(db, item), "approve", "")
    d = _decisions(db, item)[f"change:{cid}"]
    assert d["via"] == "card" and d["by"] == "claude-ai" and d["keep_all"]
    item2, _p, cid2 = _work(S, db)
    review_apply.approve(db, review.get_item(db, item2), {}, "keep all", "review-page", {})
    assert _decisions(db, item2)[f"change:{cid2}"]["via"] == "review page"
