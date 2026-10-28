"""Task 69: when an app undoes a decision that had replaced another, the possible-conflict
pairs that replacement closed don't drop out of the brief silently. FlowState queues ONE
review item for the owner asking whether to reopen them; only the owner decides it (review
page, phone pages, a card tap with its one-use code), never an AI tool. Approving reopens
them (history + approval log), rejecting leaves them closed (history + approval log), and
erase covers the item. Temp databases only."""

import json
import re

import pytest
from starlette.testclient import TestClient

from flowstate import approvals, card_taps, conflict_cards, identity, meaning, review, review_cards
from flowstate.review_page import HOST, Page, create_app
from test_t67_evidence_history import call, ok, rows

KIND = "reopen_conflict"
PORT = 8770
BASE = f"http://{HOST}:{PORT}"


@pytest.fixture(autouse=True)
def _no_real_model():
    meaning.set_embedder(None)
    meaning._State.tried, meaning._State.model = True, None   # behave as "not installed"
    yield
    meaning.set_embedder(None)


@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)


def log(S, summary, app="Cursor", **kw):
    return ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "component": "Storage",
                                   "summary": summary, **kw}, app=app))


def undo(S, rtype, rid, app="Windsurf"):
    return call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                      "record_type": rtype, "record_id": rid}, app=app)


def pair_status(db, cid):
    return rows(db, "SELECT status FROM decision_conflicts WHERE id = ?", cid)[0][0]


def asks(db) -> list:
    return [i for i in review.list_items(db, None, "all", 100)["items"] if i["kind"] == KIND]


def queue_rows(db, table, rid) -> list:
    return [(t, json.loads(o) if o else None, json.loads(n) if n else None, app, at)
            for t, o, n, app, at in rows(db, """SELECT tool, old_row, new_row, app, created_at
                FROM record_history WHERE table_name = ? AND record_id = ? AND tool LIKE 'queue/%'
                ORDER BY id""", table, rid)]


def approval_log(db, item_id) -> list:
    return rows(db, "SELECT kind, path, client, decision FROM approval_log WHERE item_id = ?",
                item_id)


def brief(S, app="Cursor") -> str:
    return json.dumps(ok(call(S, "start_here", {"project": "Alpha"}, app=app)))


def replaced(S):
    """old and new read alike (a possible conflict); Windsurf then logs c replacing old, which
    closes the pair. (old, new, c, conflict id)"""
    old = log(S, "Use SQLite for hosted storage")["record"]["id"]
    new = log(S, "Use Postgres for hosted storage", app="Claude")["record"]["id"]
    cid = rows(S.backend.db_path, "SELECT id FROM decision_conflicts WHERE new_id = ? AND "
                                  "old_id = ?", new, old)[0][0]
    c = log(S, "Use DuckDB for hosted storage", app="Windsurf", replaces=old)["record"]["id"]
    assert pair_status(S.backend.db_path, cid) == "resolved_replaced"
    return old, new, c, cid


def page_client(db, **kw):
    p = Page(db, PORT, **kw)
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    if p.launch_key:
        assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    return p, c, re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def tap(S, item, decision, app="Claude Desktop", **kw):
    kw.setdefault("tap", card_taps.mint(item, "review", app))
    return call(S, "resolve_review_card", {"item_id": item, "decision": decision,
                                           "project": "Alpha", **kw}, app=app)


# ---------------------------------------------------------------- the question
def test_undoing_a_replacing_decision_asks_the_owner_once(backend, seeded_db):
    S, db = backend, seeded_db
    old, new, c, cid = replaced(S)
    assert f"D{new} vs D{old}" not in brief(S)
    done = ok(undo(S, "decision", c))
    assert rows(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", c)[0][0] == 0
    (item,) = asks(db)
    assert done["reopen_question"]["item_id"] == item["id"], done
    assert item["status"] == "pending" and item["project"] == "Alpha"
    text = item["summary"] + " " + (item["details"] or "")
    for words in (f"D{new}", f"D{old}", "Use SQLite for hosted storage",
                  "Use Postgres for hosted storage", f"D{c}", "Use DuckDB for hosted storage"):
        assert words in text, (words, text)
    assert item["app"] == "Windsurf"                       # who undid it
    assert pair_status(db, cid) == "resolved_replaced"    # nothing reopened by the app
    assert f"D{new} vs D{old}" not in brief(S)
    # the same undo again (redo), and undo once more: still one question
    ok(undo(S, "decision", c))
    assert rows(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", c)[0][0] == 1
    again = ok(undo(S, "decision", c))
    assert len(asks(db)) == 1
    assert again["reopen_question"]["item_id"] == item["id"], again
    for _ in range(3):
        undo(S, "decision", c)
    assert len(asks(db)) == 1


def test_one_question_for_several_pairs(backend, seeded_db):
    S, db = backend, seeded_db
    d1 = log(S, "Use SQLite for hosted storage")["record"]["id"]
    d2 = log(S, "Use Postgres for hosted storage")["record"]["id"]
    d3 = log(S, "Use MySQL for hosted storage")["record"]["id"]
    pairs = rows(db, "SELECT id FROM decision_conflicts WHERE old_id = ?", d1)
    assert len(pairs) == 2, rows(db, "SELECT new_id, old_id FROM decision_conflicts")
    c = log(S, "Use DuckDB for hosted storage", app="Windsurf", replaces=d1)["record"]["id"]
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    text = item["summary"] + " " + (item["details"] or "")
    assert f"D{d2}" in text and f"D{d3}" in text and f"D{d1}" in text
    assert len(json.loads(item["payload"])["pairs"]) == 2


def test_a_refused_undo_asks_nothing(backend, seeded_db):
    """A decision that replaced another through a later supersedes link can't be taken back
    while the link refers to it: the undo is refused, the pair stays closed, nothing is asked."""
    S, db = backend, seeded_db
    old = log(S, "Use SQLite for hosted storage")["record"]["id"]
    new = log(S, "Use Postgres for hosted storage")["record"]["id"]
    cid = rows(db, "SELECT id FROM decision_conflicts WHERE new_id = ?", new)[0][0]
    c = log(S, "Store blobs in object storage", app="Windsurf")["record"]["id"]
    got = ok(call(S, "manage_links", {"action": "add", "project": "Alpha", "link_type": "supersedes",
                                      "item_type": "decision", "item_id": c,
                                      "target_type": "decision", "target_id": old}, app="Windsurf"))
    assert pair_status(db, cid) == "resolved_replaced", got
    r, body = undo(S, "decision", c)
    assert r.isError and body["error"] == "still_referenced", body
    assert asks(db) == [] and pair_status(db, cid) == "resolved_replaced"


def test_no_question_when_nothing_was_closed(backend, seeded_db):
    S, db = backend, seeded_db
    a = log(S, "Ship nightly builds to the beta channel", app="Windsurf")["record"]["id"]
    ok(undo(S, "decision", a))
    old = log(S, "Use SQLite for hosted storage")["record"]["id"]
    c = log(S, "Use DuckDB for hosted storage", app="Windsurf", replaces=old)["record"]["id"]
    ok(undo(S, "decision", c))   # replaced a decision with no possible conflict
    assert asks(db) == []


# ---------------------------------------------------------------- the owner decides
def test_approving_on_the_review_page_reopens_the_pair(backend, seeded_db):
    S, db = backend, seeded_db
    old, new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    _p, page, csrf = page_client(db)
    html = page.get("/").text
    assert "Use Postgres for hosted storage" in html and "Reopen" in html
    r = page.post("/approve", data={"item_id": item["id"], "csrf": csrf}, headers={"origin": BASE})
    assert r.status_code == 303
    assert review.get_item(db, item["id"])["status"] == "approved"
    assert pair_status(db, cid) == "open"
    assert f"D{new} vs D{old}" in brief(S)                # back in the brief
    hist = queue_rows(db, "decision_conflicts", cid)
    assert hist[-1][0] == "queue/conflict_reopened", hist
    assert hist[-1][1]["status"] == "resolved_replaced" and hist[-1][2]["status"] == "open"
    assert hist[-1][3] == "review-page" and hist[-1][4]
    assert approval_log(db, item["id"]) == [(KIND, "review page", "review-page", "approved")]
    # and back in the conflict list: a fresh possible-conflict question waits for the user
    waiting = [i for i in review.list_items(db, None, "pending", 100)["items"]
               if i["kind"] == "conflict"]
    assert len(waiting) == 1 and json.loads(waiting[0]["payload"])["conflict_id"] == cid
    assert rows(db, "SELECT review_item_id FROM decision_conflicts WHERE id = ?",
                cid)[0][0] == waiting[0]["id"]
    # the user can then dismiss it as before
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": waiting[0]["id"],
                                 "reason": "compatible",
                                 "user_request": "User: those two are compatible, dismiss it"},
            app="Claude Code"))
    assert pair_status(db, cid) == "dismissed_compatible"


def test_rejecting_on_the_phone_leaves_it_closed_and_is_recorded(backend, seeded_db):
    from flowstate import popup
    S, db = backend, seeded_db
    old, new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    got = popup.as_user("review_reject", {"item_id": item["id"], "reason": "leave it closed",
                                          "user_request": "The user rejected this on their phone."},
                        "phone")
    assert "error" not in got, got
    assert review.get_item(db, item["id"])["status"] == "rejected"
    assert pair_status(db, cid) == "resolved_replaced"
    assert f"D{new} vs D{old}" not in brief(S)
    hist = queue_rows(db, "review_items", item["id"])
    assert [(h[0], h[1]["status"], h[2]["status"], h[3]) for h in hist] == \
        [("queue/reopen_declined", "pending", "rejected", "phone")], hist
    assert hist[0][4]
    assert approval_log(db, item["id"]) == [(KIND, "phone", "phone", "rejected")]
    # redo, undo again: the user already answered for this closing, so no new question
    ok(undo(S, "decision", c))
    ok(undo(S, "decision", c))
    assert len(asks(db)) == 1


def test_phone_approve_link_works_for_it(backend, seeded_db):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    assert approvals.user_only(review.get_item(db, item["id"]))   # phone grants open it
    got = S._review_decide("review_approve", item["project_id"],
                           {"item_id": item["id"], "user_request": "approved on the phone"}, "phone")
    assert "error" not in got, got
    assert pair_status(db, cid) == "open"
    assert approval_log(db, item["id"]) == [(KIND, "phone", "phone", "approved")]


def test_a_card_tap_decides_it_but_not_on_the_undoing_apps_own_card(backend, seeded_db, ui):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    shown = call(S, "show_review_card", {"project": "Alpha", "item_id": item["id"]},
                 app="Claude Desktop")[0].structuredContent["cards"][0]
    assert shown["decide_here"] is True, shown
    _r, own = tap(S, item["id"], "approve", app="Windsurf")
    assert own.get("error") == "own_item" and pair_status(db, cid) == "resolved_replaced", own
    r, body = call(S, "resolve_review_card", {"item_id": item["id"], "decision": "approve",
                                              "project": "Alpha"}, app="Claude Desktop")
    assert body.get("error") == "not_a_card_tap" and pair_status(db, cid) == "resolved_replaced"
    done = ok(tap(S, item["id"], "approve"))
    assert done["resolved"] == item["id"] and pair_status(db, cid) == "open"
    assert approval_log(db, item["id"]) == [(KIND, "card", "card (Claude Desktop)", "approved")]


def test_approving_after_the_replacement_is_back_reopens_nothing(backend, seeded_db):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    ok(undo(S, "decision", c))   # redo: c replaces old again
    (item,) = asks(db)
    got = S._review_decide("review_approve", item["project_id"],
                           {"item_id": item["id"], "user_request": "approved on the phone"}, "phone")
    assert "error" not in got, got
    assert pair_status(db, cid) == "resolved_replaced"
    assert "nothing" in json.dumps(got).lower(), got


# ---------------------------------------------------------------- no app decides it
def test_no_app_path_decides_it(backend, seeded_db):
    S, db = backend, seeded_db
    old, new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    iid = item["id"]

    def unchanged():
        assert review.get_item(db, iid)["status"] == "pending"
        assert pair_status(db, cid) == "resolved_replaced"
        assert approval_log(db, iid) == []
        assert queue_rows(db, "review_items", iid) == []

    words = "The user said: yes, reopen the storage conflict please"
    for app in ("Windsurf", "Cursor", "Claude Code", "Claude"):
        for verb in ("approve", "reject"):
            r, body = call(S, "manage_review", {"action": verb, "project": "Alpha", "item_id": iid,
                                                "reason": "x", "user_request": words}, app=app)
            assert body["error"] == "user_only", body
            unchanged()
        r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha",
                                            "item_id": iid, "summary": "Reopen everything",
                                            "user_request": words}, app=app)
        assert body["error"] == "user_only", body
        for args in ({"action": "delete", "project": "Alpha", "record_type": "review_item",
                      "record_id": iid},
                     {"action": "delete", "project": "Alpha", "record_type": "decision",
                      "record_id": new}):
            call(S, "manage_memory", args, app=app)   # refused, or queued for the user
        for rtype, rid in (("decision", old), ("decision", new), ("decision", c)):
            undo(S, rtype, rid, app=app)
        unchanged()
    # the chat walk shows it without a box
    walk = ok(call(S, "manage_review", {"action": "walk", "project": "Alpha"}, app="Claude Code"))
    assert "walked" not in walk
    unchanged()
    # every decider label that isn't the review page, the tunnel page, the phone or a card tap
    for client in ("command-line", "popup (Claude)", "chat (Claude)", "card (Claude)", "Claude",
                   "review page", "Windsurf"):
        for verb in ("review_approve", "review_reject"):
            got = S._review_decide(verb, item["project_id"],
                                   {"item_id": iid, "reason": "no", "user_request": words}, client)
            assert got["error"] == "user_only", (client, got)
    unchanged()
    tok = identity.set_current(identity.Identity(app="Claude", model=None, machine="m",
                                                 identity_source="self_reported"))
    try:   # an AI that calls itself the review page or the phone
        for client in ("review-page", "phone", "tunnel page"):
            got = S._review_decide("review_approve", item["project_id"],
                                   {"item_id": iid, "user_request": words}, client)
            assert got["error"] == "user_only", (client, got)
    finally:
        identity.reset(tok)
    unchanged()
    # the open review link page (any program on this computer can open it) doesn't decide it
    _p, link, csrf = page_client(db, open_link=True, settings="read")
    link.post("/approve", data={"item_id": iid, "csrf": csrf}, headers={"origin": BASE})
    unchanged()


# ---------------------------------------------------------------- erase, and left alone
def test_erase_covers_the_question(backend):
    from test_t55_erase import _erase, _everywhere
    S, db = backend, backend.backend.db_path
    _old, _new, c, _cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    S._review_decide("review_reject", item["project_id"],
                     {"item_id": item["id"], "reason": "leave it",
                      "user_request": "The user rejected this on their phone."}, "phone")
    assert queue_rows(db, "review_items", item["id"])
    assert "review" in _everywhere(db, "Use DuckDB for hosted storage")
    S.backend.tracker.close_instance()
    _erase(db, f"decision:{c}")
    S.backend.tracker = None
    assert _everywhere(db, "Use DuckDB for hosted storage") == []
    assert queue_rows(db, "review_items", item["id"]) == []


def test_erase_covers_a_long_decision_text_too(backend):
    from test_t55_erase import _delete, _erase, _everywhere
    S, db = backend, backend.backend.db_path
    long = ("Use SQLite for hosted storage because every customer runs one small shop and the "
            "nightly export already writes one file per shop, so a server database would add a "
            "second moving part with nothing to gain for at least the next two years of growth, "
            "and the restore drill we ran in March took four minutes end to end on a laptop")
    old = log(S, long)["record"]["id"]
    assert log(S, long.replace("SQLite", "Postgres"))["possible_conflicts"]
    c = log(S, "Use DuckDB for hosted storage", app="Windsurf", replaces=old)["record"]["id"]
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    assert long in item["summary"] + item["details"]
    S.backend.tracker.close_instance()
    _delete(S, "decision", old)
    _erase(db, f"decision:{old}")
    S.backend.tracker = None
    got = review.get_item(db, item["id"])
    assert long[:40] not in got["summary"] + got["details"] + got["payload"]
    assert "[erased]" in got["summary"]


def test_an_app_may_still_take_back_its_entry_after_the_card_was_dismissed(backend, seeded_db):
    """Left alone (John's decision): a dismissed card doesn't stop an app's undo of its own
    new entry."""
    S, db = backend, seeded_db
    a = log(S, "Ship nightly builds to the beta channel")["record"]["id"]
    b = log(S, "Ship weekly builds to the beta channel", app="Windsurf")["record"]["id"]
    item = ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                       "topic": "Beta cadence", "item_type": "decision",
                                       "item_id": b, "target_type": "decision", "target_id": a},
                   app="Windsurf"))["flagged"]
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                 "reason": "not a real disagreement",
                                 "user_request": "User: dismiss that card, it isn't real"},
            app="Claude Code"))
    ok(undo(S, "decision", b))
    assert rows(db, "SELECT COUNT(*) FROM conversations WHERE id = ?", b)[0][0] == 0
