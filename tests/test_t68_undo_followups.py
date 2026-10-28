"""Task 68: undo steps over a card's outcome to older ordinary edits (and never changes the
outcome), review-queue changes are in the history (never undone by an app, erased by erase),
and undo's "changed again since by ..." names the right record's app. Temp databases only."""

import json

import pytest

from flowstate import meaning
from test_t67_evidence_history import call, flag, ok, rows, two_decisions

KEEP_A = "User: keep the older entry, the JWT one"


@pytest.fixture(autouse=True)
def _no_real_model():
    meaning.set_embedder(None)
    meaning._State.tried, meaning._State.model = True, None   # behave as "not installed"
    yield
    meaning.set_embedder(None)


def undo(S, rtype, rid, app="Cursor"):
    return call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                      "record_type": rtype, "record_id": rid}, app=app)


def standing(db, rid):
    return rows(db, "SELECT record_type, record_id, standing, by_type, by_id, card_id, at, app "
                    "FROM entry_standing WHERE record_type = 'decision' AND record_id = ?", rid)


def card_status(db):
    return rows(db, "SELECT id, status, resolved_at FROM conflict_cards ORDER BY id")


def source(db, rid, typ="decision"):
    got = rows(db, "SELECT source FROM record_meta WHERE record_type = ? AND record_id = ?",
               typ, rid)
    return got[0][0] if got else None


def decided_card(S):
    """a and b; Cursor tags b from_code; then a card on (a, b) is decided: keep a."""
    a, b = two_decisions(S)
    ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                 "record_type": "decision", "record_id": b,
                                 "source": "from_code"}))
    item = flag(S, a, b, ["src/auth.py:42"])["flagged"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                 "keep": "a", "user_request": KEEP_A}, app="Claude Code"))
    return a, b, item


# ---------------------------------------------------------------- item 1
def test_undo_steps_over_the_card_outcome_to_the_older_edit(backend, seeded_db):
    S = backend
    a, b, _item = decided_card(S)
    mark, cards = standing(seeded_db, b), card_status(seeded_db)
    assert mark and mark[0][2] == "superseded" and mark[0][4] == a
    assert source(seeded_db, b) == "from_code"
    undone = ok(undo(S, "decision", b))
    assert undone["tool"] == "source/set", undone
    assert source(seeded_db, b) is None                       # the older edit is reversed
    assert standing(seeded_db, b) == mark                     # the card's outcome is not
    assert card_status(seeded_db) == cards
    assert all(r["record_type"] != "standing" for r in undone["rows"])
    redo = ok(undo(S, "decision", b))                          # undo again redoes it
    assert redo["tool"] == "history/undo" and source(seeded_db, b) == "from_code"
    assert standing(seeded_db, b) == mark
    card_rows = rows(seeded_db, "SELECT undone_at FROM record_history "
                                "WHERE table_name = 'entry_standing'")
    assert card_rows and all(u is None for (u,) in card_rows)


def test_no_app_tool_changes_a_card_outcome(backend, seeded_db):
    S = backend
    a, b, item = decided_card(S)
    mark, cards = standing(seeded_db, b), card_status(seeded_db)
    for app in ("Cursor", "Claude", "Claude Code"):
        for rtype, rid in (("decision", b), ("decision", a)):
            for _ in range(4):   # every undo an app can reach on either entry
                undo(S, rtype, rid, app=app)
                assert standing(seeded_db, b) == mark
                assert card_status(seeded_db) == cards
    links = rows(seeded_db, "SELECT id FROM record_links WHERE link_type = 'supersedes'")
    for (lid,) in links:
        for app in ("Cursor", "Claude Code"):
            undo(S, "link", lid, app=app)
    for action, extra in (("approve", {"keep": "b"}), ("reject", {"reason": "changed my mind"})):
        r, body = call(S, "manage_review", {"action": action, "project": "Alpha", "item_id": item,
                                            "user_request": "User: actually keep the newer one",
                                            **extra}, app="Claude Code")
        assert r.isError, body
    r, body = call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                        "record_type": "standing", "record_id": b})
    assert r.isError
    assert standing(seeded_db, b) == mark
    assert card_status(seeded_db) == cards


def test_taking_back_a_new_record_a_card_decided_about_is_refused(backend, seeded_db):
    """Undoing an app's own create would remove an entry a decided card names (the winner):
    the loser would be left 'superseded by' nothing. Refused; the outcome stays."""
    S = backend
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Ship nightly builds to the beta channel"}))["record"]["id"]
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "learning",
                                "summary": "Beta users want weekly builds"}))["record"]["id"]
    item = ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                       "topic": "Beta cadence", "item_type": "decision",
                                       "item_id": a, "target_type": "learning",
                                       "target_id": b}))["flagged"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                 "keep": "a", "user_request": "User: keep the nightly decision"},
            app="Claude Code"))
    mark = rows(seeded_db, "SELECT * FROM entry_standing WHERE record_type = 'learning' "
                           "AND record_id = ?", b)
    assert mark
    r, body = undo(S, "decision", a)
    assert r.isError and body["error"] == "user_only", body
    assert rows(seeded_db, "SELECT COUNT(*) FROM conversations WHERE id = ?", a)[0][0] == 1
    r, body = undo(S, "learning", b)
    assert r.isError, body
    assert rows(seeded_db, "SELECT COUNT(*) FROM learnings WHERE id = ?", b)[0][0] == 1
    assert rows(seeded_db, "SELECT * FROM entry_standing WHERE record_type = 'learning' "
                           "AND record_id = ?", b) == mark


# ---------------------------------------------------------------- item 2
def pair(S, comp="Storage"):
    """Two decisions FlowState flags as a possible conflict. (old, new, conflict id, item)."""
    old = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "component": comp,
                                  "summary": "Use SQLite for hosted storage"}))["record"]["id"]
    got = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "component": comp,
                                  "summary": "Use Postgres for hosted storage"}, app="Claude"))
    new = got["record"]["id"]
    flags = got["possible_conflicts"]
    cid = rows(S.backend.db_path, "SELECT id FROM decision_conflicts WHERE new_id = ? "
                                  "AND old_id = ?", new, old)[0][0]
    return old, new, cid, flags[0]["review_item_id"]


def queue_history(db, table, rid):
    return [{"tool": t, "old": json.loads(o) if o else None, "new": json.loads(n) if n else None,
             "app": app, "at": at, "undone_at": u}
            for t, o, n, app, at, u in rows(db, """SELECT tool, old_row, new_row, app, created_at,
                undone_at FROM record_history WHERE table_name = ? AND record_id = ?
                AND tool LIKE 'queue/%' ORDER BY id""", table, rid)]


def test_dismissing_a_possible_conflict_is_in_history(backend, seeded_db):
    S = backend
    _old, _new, cid, item = pair(S)
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                 "reason": "compatible",
                                 "user_request": "User: those two are compatible, dismiss it"},
            app="Claude Code"))
    hist = queue_history(seeded_db, "decision_conflicts", cid)
    assert len(hist) == 1, hist
    h = hist[0]
    assert h["old"]["status"] == "open" and h["new"]["status"] == "dismissed_compatible"
    assert h["app"] == "Claude Code" and h["at"]


def test_a_replacement_closing_a_pair_is_in_history_and_no_undo_reopens_it(backend, seeded_db):
    S = backend
    old, new, cid, _item = pair(S)
    c = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "component": "Storage",
                                "summary": "Use DuckDB for hosted storage", "replaces": old},
                app="Windsurf"))["record"]["id"]
    assert rows(seeded_db, "SELECT status FROM decision_conflicts WHERE id = ?",
                cid) == [("resolved_replaced",)]
    hist = queue_history(seeded_db, "decision_conflicts", cid)
    assert [(h["old"]["status"], h["new"]["status"], h["app"]) for h in hist] == \
        [("open", "resolved_replaced", "Windsurf")]
    undone = ok(undo(S, "decision", c, app="Windsurf"))   # its own new decision may go
    assert rows(seeded_db, "SELECT COUNT(*) FROM conversations WHERE id = ?", c)[0][0] == 0
    assert any(s["record_type"] != "decision" for s in undone.get("skipped", [])), undone
    assert rows(seeded_db, "SELECT status FROM decision_conflicts WHERE id = ?",
                cid) == [("resolved_replaced",)]   # the pair's closing stays: not an app's undo
    assert queue_history(seeded_db, "decision_conflicts", cid)[0]["undone_at"] is None
    for rtype, rid in (("decision", old), ("decision", new)):
        undo(S, rtype, rid, app="Windsurf")
        assert rows(seeded_db, "SELECT status FROM decision_conflicts WHERE id = ?",
                    cid) == [("resolved_replaced",)]


def test_approve_with_edits_keeps_the_text_before_and_after(backend, seeded_db):
    S = backend
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                   "summary": "Retry failed webhooks three times"}))["queued_for_review"]
    got = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "summary": "Retry failed webhooks five times",
                                       "user_request": "User: approve it but make it five times"},
                  app="Claude Code"))
    rid = got["record"]["id"]
    hist = queue_history(seeded_db, "review_items", item)
    assert len(hist) == 1, hist
    h = hist[0]
    assert h["tool"] == "queue/review_edit"
    assert h["old"]["summary"] == "Retry failed webhooks three times"
    assert h["new"]["summary"] == "Retry failed webhooks five times"
    assert h["app"] == "Claude Code" and h["at"]
    for app in ("Cursor", "Claude Code"):   # no app's undo puts the old text back
        undo(S, "decision", rid, app=app)
    assert rows(seeded_db, "SELECT summary FROM review_items WHERE id = ?",
                item) == [("Retry failed webhooks five times",)]
    assert queue_history(seeded_db, "review_items", item)[0]["undone_at"] is None


def test_approving_without_edits_adds_no_review_text_history(backend, seeded_db):
    S = backend
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                   "summary": "Page the on-call after two failures"}))["queued_for_review"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                 "user_request": "User: approve the paging one as it is"},
            app="Claude Code"))
    assert queue_history(seeded_db, "review_items", item) == []


def test_history_lists_queue_rows_as_not_undoable(backend, seeded_db):
    S = backend
    _old, _new, cid, item = pair(S)
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                 "reason": "compatible",
                                 "user_request": "User: those two are compatible, dismiss it"},
            app="Claude Code"))
    listed = ok(call(S, "manage_history", {"action": "list", "project": "Alpha"}))["history"]
    mine = [h for h in listed if h["tool"].startswith("queue/")]
    assert mine and mine[0]["record_type"] == "possible conflict"


def test_erase_takes_the_edited_review_text_out_of_the_history(backend):
    from test_t55_erase import _delete, _erase, _everywhere
    S, db = backend, backend.backend.db_path
    first = "Store the payroll export on the shared drive forever"
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                   "summary": first}))["queued_for_review"]
    rid = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "summary": "Keep the payroll export for one year",
                                       "user_request": "User: approve, but one year only"},
                  app="Claude Code"))["record"]["id"]
    assert queue_history(db, "review_items", item)
    assert "history" in _everywhere(db, first)
    S.backend.tracker.close_instance()
    _delete(S, "decision", rid)
    _erase(db, f"decision:{rid}")
    S.backend.tracker = None
    hist = queue_history(db, "review_items", item)   # the row stays, without the text (task 71)
    assert len(hist) == 1 and hist[0]["old"]["summary"] == "[erased]"
    assert _everywhere(db, first) == []


def test_erase_takes_a_pairs_status_history(backend):
    from test_t55_erase import _delete, _erase
    S, db = backend, backend.backend.db_path
    _old, new, cid, item = pair(S)
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                 "reason": "compatible",
                                 "user_request": "User: those two are compatible, dismiss it"},
            app="Claude Code"))
    assert queue_history(db, "decision_conflicts", cid)
    S.backend.tracker.close_instance()
    _delete(S, "decision", new, )
    _erase(db, f"decision:{new}")
    S.backend.tracker = None
    assert queue_history(db, "decision_conflicts", cid) == []


# ---------------------------------------------------------------- item 3
def test_changed_since_names_the_app_of_the_same_record_type(backend, seeded_db):
    S = backend
    d = t = None
    n = 0
    while d is None or t is None or d != t:   # a decision and a todo with the same id
        n += 1
        if d is None or (t is not None and d < t):
            d = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                        "summary": f"Pick vendor {n} for invoices"}))["record"]["id"]
        else:
            t = ok(call(S, "log_work", {"project": "Alpha", "kind": "todo",
                                        "summary": f"Email supplier {n} about pricing"}))["record"]["id"]
        assert n < 200
    ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                 "record_type": "decision", "record_id": d,
                                 "source": "from_code"}))
    conn = __import__("sqlite3").connect(seeded_db)   # changed outside FlowState
    conn.execute("UPDATE record_meta SET source = 'ai_inferred' WHERE record_type = 'decision' "
                 "AND record_id = ?", (d,))
    conn.commit()
    conn.close()
    ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                 "record_type": "todo", "record_id": t,
                                 "source": "from_code"}, app="Grok"))
    r, body = undo(S, "decision", d)
    assert r.isError and body["error"] == "changed_since", body
    assert "Grok" not in body["message"], body
    assert "outside FlowState" in body["message"], body
