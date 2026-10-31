"""Task 74: a clean-up suggestion that would change records (move a misfiled record, cancel a
duplicate todo, cancel a stale one) is bound to the exact rows FlowState listed when it queued
the suggestion, the same way task 72 binds a delete. If a listed row changed, or the set of
rows differs (added, missing, already removed), approving it changes nothing, the suggestion
is closed, and running the clean-up again queues a new one. The review page shows the list
and warns before the user approves. A clean-up that changes nothing behaves as before.
Erased text never shows. Temp DBs only."""

import json
import sqlite3

from flowstate import cleanup, review

from test_cleanup import _item, _j, _q, call, messy  # noqa: F401

ASK = "User: yes, approve that clean-up"
VOIDED_BY = "flowstate (automatic)"


def _x(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, a)
        conn.commit()
    finally:
        conn.close()


def _payload(db, iid):
    return json.loads(_q(db, "SELECT payload FROM review_items WHERE id = ?", iid)[0][0])


def _approve(S, iid, **extra):
    return _j(call(S, "manage_review", {"action": "approve", "project": "FlowState",
                                        "item_id": iid, "user_request": ASK, **extra}))


def _state(db, iid):
    return _q(db, "SELECT status, decided_by, reject_reason FROM review_items WHERE id = ?", iid)[0]


def _records(db):
    """Every record table and the edit history: what a voided approval must not touch."""
    return {t: _q(db, f"SELECT * FROM {t} ORDER BY id") for t in
            ("todos", "learnings", "problems", "changes", "projects", "components",
             "conversations", "record_links", "record_history")}


def _stale(S, db, ids):
    _x(db, "UPDATE todos SET created_at = '2025-01-01 00:00:00' WHERE id = ?", ids["kern"])
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]})
    return _item(db, "payload LIKE '%\"stale\"%'")[0]


def _dup(S, db):
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["duplicate"]})
    iid, p = _item(db, "payload LIKE '%\"duplicate\"%'")
    old, new = [r[1] for r in json.loads(p)["records"]]
    return iid, old, new


def _move(S, db, ids):
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    return _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")[0]


def _link(db, ids, to_todo):
    """A link from the misfiled learning to a todo (as manage_links would store it)."""
    _x(db, "INSERT INTO record_links (from_type, from_id, from_project_id, to_type, to_id, "
           "to_project_id, link_type, created_at) VALUES ('learning', ?, ?, 'todo', ?, ?, "
           "'relates_to', '2026-10-28 10:00:00')", ids["misfiled"], ids["fs"], to_todo, ids["fs"])


def _assert_voided(S, db, iid, before, says):
    out = _approve(S, iid)
    assert out.get("error") == "changed_since_request", out
    assert says in out["message"], out["message"]
    assert "Nothing was changed" in out["message"] and "clean-up again" in out["message"]
    status, by, reason = _state(db, iid)
    assert (status, by) == ("rejected", VOIDED_BY) and says in reason
    assert _records(db) == before       # the voided approval changed no record, no history
    again = _approve(S, iid)            # closed: can't be used any more
    assert again.get("error") == "already_decided", again
    return out


# ---------------------------------------------------------------- what is stored
def test_each_suggestion_stores_the_exact_rows_it_changes(messy):
    S, db, ids = messy
    _link(db, ids, ids["kern"])
    move = _payload(db, _move(S, db, ids))["bound"]
    lid = _q(db, "SELECT id FROM record_links WHERE from_id = ?", ids["misfiled"])[0][0]
    assert move["rows"] == {"learnings": [ids["misfiled"]], "record_links": [lid]}
    assert len(move["digest"]) == 64
    iid, old, new = _dup(S, db)
    assert _payload(db, iid)["bound"]["rows"] == {"todos": sorted([old, new])}   # kept + cancelled
    stale = _payload(db, _stale(S, db, ids))["bound"]
    assert stale["rows"] == {"todos": [ids["kern"]]}
    # ids and a hash only: no record text in the stored list
    assert "Kernel" not in json.dumps(stale) and "VPN" not in json.dumps(move)


# ---------------------------------------------------------------- unchanged: approves
def test_unchanged_move_approves(messy):
    S, db, ids = messy
    _link(db, ids, ids["kern"])
    out = _approve(S, _move(S, db, ids))
    assert out["record"] == {"table": "learnings", "id": ids["misfiled"]}, out
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["vpn"],)]
    assert _q(db, "SELECT from_project_id FROM record_links WHERE from_id = ?",
              ids["misfiled"]) == [(ids["vpn"],)]


def test_unchanged_duplicate_and_stale_approve(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    assert _approve(S, iid)["record"] == {"table": "todos", "id": new}
    assert _q(db, "SELECT status FROM todos WHERE id IN (?, ?) ORDER BY id", old, new) == [
        ("pending",), ("cancelled",)]
    sid = _stale(S, db, ids)
    assert _approve(S, sid)["record"] == {"table": "todos", "id": ids["kern"]}
    assert _q(db, "SELECT status FROM todos WHERE id = ?", ids["kern"]) == [("cancelled",)]


# ---------------------------------------------------------------- changed row: voids
def test_an_edited_row_voids_a_move(messy):
    S, db, ids = messy
    iid = _move(S, db, ids)
    _x(db, "UPDATE learnings SET context = 'edited by another app' WHERE id = ?", ids["misfiled"])
    _assert_voided(S, db, iid, _records(db), f"learning #{ids['misfiled']} was edited")


def test_an_edited_row_voids_a_duplicate_merge(messy):
    """The todo that is kept counts too: if it changed, the merge is not what the user saw."""
    S, db, ids = messy
    iid, old, _new = _dup(S, db)
    _x(db, "UPDATE todos SET status = 'done' WHERE id = ?", old)
    _assert_voided(S, db, iid, _records(db), f"todo #{old} was edited")


def test_an_edited_row_voids_a_stale_archive(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    _x(db, "UPDATE todos SET priority = 'critical' WHERE id = ?", ids["kern"])
    _assert_voided(S, db, iid, _records(db), f"todo #{ids['kern']} was edited")


# ---------------------------------------------------------------- set differs: voids
def test_an_added_link_voids_a_move(messy):
    S, db, ids = messy
    iid = _move(S, db, ids)
    _link(db, ids, ids["kern"])
    lid = _q(db, "SELECT id FROM record_links WHERE from_id = ?", ids["misfiled"])[0][0]
    _assert_voided(S, db, iid, _records(db), f"now also lists link #{lid}")


def test_a_missing_link_voids_a_move(messy):
    S, db, ids = messy
    _link(db, ids, ids["kern"])
    iid = _move(S, db, ids)
    lid = _q(db, "SELECT id FROM record_links WHERE from_id = ?", ids["misfiled"])[0][0]
    _x(db, "DELETE FROM record_links WHERE id = ?", lid)
    _assert_voided(S, db, iid, _records(db), f"no longer lists link #{lid}")


def test_a_kept_todo_removed_voids_the_duplicate(messy):
    S, db, ids = messy
    iid, old, _new = _dup(S, db)
    _x(db, "DELETE FROM todos WHERE id = ?", old)
    _assert_voided(S, db, iid, _records(db), f"no longer lists todo #{old}")


def test_an_already_removed_record_voids_the_stale_archive(messy):
    """Deleted by another app through FlowState (approvals off: at once) before the user
    approved: the clean-up has nothing left to do and says so."""
    S, db, ids = messy
    iid = _stale(S, db, ids)
    out = _j(call(S, "manage_memory", {"action": "delete", "project": "FlowState",
                                       "record_type": "todo", "record_id": ids["kern"]},
                  client="Cursor"))
    assert out.get("deleted"), out
    _assert_voided(S, db, iid, _records(db), f"no longer lists todo #{ids['kern']}")


def test_an_already_moved_record_voids_the_move(messy):
    S, db, ids = messy
    iid = _move(S, db, ids)
    _x(db, "UPDATE learnings SET project_id = ? WHERE id = ?", ids["os"], ids["misfiled"])
    _assert_voided(S, db, iid, _records(db), f"learning #{ids['misfiled']} was edited")


# ---------------------------------------------------------------- asking again
def test_after_a_void_the_clean_up_suggests_it_again_and_that_approves(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    _x(db, "UPDATE todos SET description = 'more words' WHERE id = ?", ids["kern"])
    assert _approve(S, iid)["error"] == "changed_since_request"
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]}))
    assert r["queued"] == 1, r
    fresh = r["item_ids"][0]
    assert fresh != iid and _payload(db, fresh)["bound"]["rows"] == {"todos": [ids["kern"]]}
    assert _approve(S, fresh)["record"] == {"table": "todos", "id": ids["kern"]}
    assert _q(db, "SELECT status FROM todos WHERE id = ?", ids["kern"]) == [("cancelled",)]


def test_running_the_clean_up_again_replaces_an_out_of_date_waiting_one(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    again = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]}))
    assert again["queued"] == 0 and again["already_suggested"] == 1   # still matches: kept
    _x(db, "UPDATE todos SET description = 'more words' WHERE id = ?", ids["kern"])
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]}))
    assert r["queued"] == 1 and r.get("replaced") == [iid], r
    status, by, reason = _state(db, iid)
    assert (status, by) == ("rejected", VOIDED_BY) and "was edited" in reason


def test_a_rejected_suggestion_still_does_not_come_back(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    call(S, "manage_review", {"action": "reject", "project": "FlowState", "item_id": iid,
                              "reason": "keep it", "user_request": "User: no, keep that todo"})
    _x(db, "UPDATE todos SET description = 'more words' WHERE id = ?", ids["kern"])
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]}))
    assert r["queued"] == 0 and r["already_suggested"] == 1


def test_an_older_suggestion_without_a_list_is_refused_and_closed(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    p = _payload(db, iid)
    p.pop("bound")
    _x(db, "UPDATE review_items SET payload = ? WHERE id = ?", json.dumps(p), iid)
    before = _records(db)
    out = _approve(S, iid)
    assert out.get("error") == "request_outdated", out
    assert "older FlowState" in out["message"] and "Nothing was changed" in out["message"]
    assert _state(db, iid)[:2] == ("rejected", VOIDED_BY)
    assert _records(db) == before
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["stale"]}))
    assert r["queued"] == 1   # asked again: a new one, with its list


def test_an_edit_on_the_page_or_move_to_cannot_change_the_list(messy):
    """Picking another target project moves the same rows: still the list the user saw."""
    S, db, ids = messy
    iid = _move(S, db, ids)
    out = _approve(S, iid, move_to="Atlas OS")
    assert out["record"] == {"table": "learnings", "id": ids["misfiled"]}, out
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["os"],)]


# ---------------------------------------------------------------- no-op clean-ups
def test_a_clean_up_that_changes_nothing_behaves_as_before(messy):
    """A duplicate of two learnings (queued by an older version): approving notes it, changes
    nothing, needs no list, and is approved as before."""
    S, db, ids = messy
    lids = [r[0] for r in _q(db, "SELECT id FROM learnings WHERE project_id = ? ORDER BY id LIMIT 2",
                             ids["fs"])]
    iid = review.add_item(db, ids["fs"], "cleanup", 'Cancel learning "x" in FlowState?', "d",
                          {"type": "duplicate", "records": [["learning", lids[0]],
                                                            ["learning", lids[1]]],
                           "fingerprint": f"duplicate:learning#{lids[0]},learning#{lids[1]}"},
                          "cleanup (cli)")
    before = _records(db)
    out = _approve(S, iid)
    assert out["record"] == {"table": "none", "id": None}, out
    assert _state(db, iid)[0] == "approved"
    assert {k: v for k, v in _records(db).items()} == before


def test_no_list_is_stored_for_a_suggestion_that_changes_nothing(messy):
    S, db, ids = messy
    lids = [r[0] for r in _q(db, "SELECT id FROM learnings ORDER BY id LIMIT 2")]
    s = {"type": "duplicate", "project_id": ids["fs"], "records": [["learning", lids[0]],
                                                                   ["learning", lids[1]]],
         "summary": "Cancel learning?", "details": "d"}
    iid = cleanup.queue(db, [s])["item_ids"][0]
    assert "bound" not in _payload(db, iid)


# ---------------------------------------------------------------- the review page
def _block(db, iid):
    from flowstate.review_page import Page
    html = Page(db).render("csrf")
    block = html[html.index(f"#{iid} · cleanup"):]
    return block[:block.index("</div>", block.index("Reject"))]


def test_the_review_page_shows_the_exact_list(messy):
    S, db, ids = messy
    _link(db, ids, ids["kern"])
    block = _block(db, _move(S, db, ids))
    lid = _q(db, "SELECT id FROM record_links WHERE from_id = ?", ids["misfiled"])[0][0]
    assert f"learning #{ids['misfiled']}" in block and f"link #{lid}" in block
    assert "not the one" not in block and "Approve: move it" in block


def test_the_review_page_warns_when_the_list_changed(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    block = _block(db, iid)
    assert f"todo #{old}" in block and f"todo #{new}" in block
    assert "This list is not the one the suggestion was made with" in block
    assert f"todo #{old} was edited" in block and "Approving it changes nothing" in block


def test_the_phone_page_warns_too(messy):
    S, db, ids = messy
    from flowstate import review_phone
    iid = _stale(S, db, ids)
    _x(db, "DELETE FROM todos WHERE id = ?", ids["kern"])
    v = [v for v in review_phone.items(db) if v["id"] == iid][0]
    assert "This list is not the one the suggestion was made with" in v["details"], v
    assert f"no longer lists todo #{ids['kern']}" in v["details"]


# ---------------------------------------------------------------- erased text
SECRETISH = "Quarterly reconciliation of the harbour ledger with Mirela before audit"


def test_erased_text_never_appears_in_the_list_page_or_message(messy):
    S, db, ids = messy
    from flowstate import erase
    _x(db, "UPDATE todos SET title = ? WHERE id = ?", SECRETISH, ids["kern"])
    iid = _stale(S, db, ids)
    out = _j(call(S, "manage_memory", {"action": "delete", "project": "FlowState",
                                       "record_type": "todo", "record_id": ids["kern"]},
                  client="Cursor"))
    assert out.get("deleted"), out
    S.backend.tracker.close_instance()
    erase.run(db, erase.plan(db, [f"todo:{ids['kern']}"]))
    S.init_backend(db)
    words = [" ".join(SECRETISH.split()[i:i + 3]) for i in range(len(SECRETISH.split()) - 2)]
    row = json.dumps(_q(db, "SELECT * FROM review_items WHERE id = ?", iid))
    block = _block(db, iid)
    assert "This list is not the one" in block or "already" in block
    msg = json.dumps(_approve(S, iid))
    for w in words + ["Mirela", "harbour"]:
        assert w not in row and w not in block and w not in msg, w
    assert _state(db, iid)[:2] == ("rejected", VOIDED_BY)
    assert "Mirela" not in json.dumps(_q(db, "SELECT * FROM review_items WHERE id = ?", iid))


def test_running_it_again_on_the_same_connection_suggests_it_anew(messy):
    """The repeat guard (receipts.py) must not hand back the first run's answer."""
    import asyncio
    from mcp import types
    from mcp.shared.memory import create_connected_server_and_client_session
    S, db, ids = messy
    _x(db, "UPDATE todos SET created_at = '2025-01-01 00:00:00' WHERE id = ?", ids["kern"])
    args = {"action": "cleanup", "project": "FlowState", "kinds": ["stale"]}

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            first = json.loads((await c.call_tool("manage_memory", args)).content[0].text)
            _x(db, "UPDATE todos SET description = 'more words' WHERE id = ?", ids["kern"])
            again = json.loads((await c.call_tool("manage_memory", args)).content[0].text)
            return first, again
    first, again = asyncio.run(go())
    assert first["queued"] == 1, first
    assert again.get("queued") == 1 and again.get("replaced") == first["item_ids"], again
