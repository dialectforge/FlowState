"""Task 48: a session summary's records each get Keep / Remove (session_records.py), and the
summary's Keep is Keep all. Remove goes through the user's delete path (safety copy, one edit
in the edit history) and Bring back / manage_history undo restores it. Only the user decides
records: the local and tunnel pages, the phone list and approve page, the review card's
app-only tool (with its rules) and `flowstate review record` in a terminal. Temp DBs only."""

import json
import re
import sqlite3
import time

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from flowstate import (approvals, cli_review, conflict_cards, popup, review, review_cards,
                       review_phone, session_contents, session_records, waiting)
from fsa_test_keys import claims, make_key, sign
from test_session_contents import CHANGE, PROBLEM, SOLUTION, _q, _work, call

ORIGIN = {"origin": "http://127.0.0.1:8770"}


def _entries(db, item):
    return {e["key"]: e for e in session_contents.for_item(db, review.get_item(db, item))["entries"]}


def _decisions(db, item):
    return json.loads(review.get_item(db, item)["payload"]).get("record_decisions") or {}


def _page(db):
    from flowstate.review_page import Page, create_app
    p = Page(db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    return c


def _csrf(html):
    from test_settings_page import CSRF
    return CSRF.search(html).group(1)


def _flash(c, r):
    from test_settings_page import flash
    return flash(c, r)


# ---------------------------------------------------------------- the core
def test_records_start_undecided_with_keep_and_remove(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    e = _entries(db, item)
    assert e[f"change:{cid}"]["actions"] == ["keep", "remove"]
    assert e[f"problem:{pid}"]["actions"] == ["keep", "remove"]   # saved, then solved: still its own
    assert all(x["state"] == "" for x in e.values())
    c = session_contents.for_item(db, review.get_item(db, item))
    assert session_contents.lines(c)[0] == "What this session saved (2):"   # nothing decided yet
    assert session_contents.public(c)["explain"] == session_records.EXPLAIN


def test_keep_marks_reviewed_and_changes_nothing_else(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    before = _q(db, "SELECT * FROM changes WHERE id = ?", cid)
    out = session_records.decide(db, item, f"change:{cid}", "keep", "review-page")
    assert out["now"] == "kept" and _q(db, "SELECT * FROM changes WHERE id = ?", cid) == before
    d = _decisions(db, item)[f"change:{cid}"]
    assert d["d"] == "kept" and d["via"] == "review page" and CHANGE not in json.dumps(d)
    e = _entries(db, item)[f"change:{cid}"]
    assert e["state"] == "kept" and e["actions"] == ["remove"]     # can still be taken back
    assert review.get_item(db, item)["status"] == "pending"        # the summary still waits
    assert ("session_summary", "review page", f"record kept: change #{cid}") in \
        _q(db, "SELECT kind, path, decision FROM approval_log")
    text = "\n".join(session_contents.lines(session_contents.for_item(db, review.get_item(db, item))))
    assert "records: 1 kept, 1 not decided yet" in text and f'change #{cid} "{CHANGE}" (Auth) [kept]' in text


def test_remove_uses_the_delete_path_with_a_safety_copy_and_bring_back_restores(backend):
    from flowstate import backup, safety
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    sol = _q(db, "SELECT id FROM solutions WHERE problem_id = ?", pid)
    out = session_records.decide(db, item, f"problem:{pid}", "remove", "review-page")
    assert out["now"] == "removed" and out["safety_copy"] and "manage_history" in out["undo"]
    assert any(p.name == out["safety_copy"] for _ts, p in backup.list_backups(safety._dir()))
    assert _q(db, "SELECT id FROM problems WHERE id = ?", pid) == []
    assert _q(db, "SELECT id FROM solutions WHERE problem_id = ?", pid) == []   # its solution too
    hist = _q(db, "SELECT tool, client, undone_at FROM record_history WHERE table_name = 'problems' "
                  "AND record_id = ? ORDER BY id DESC LIMIT 1", pid)
    assert hist == [("delete/problem", "review-page", None)]
    e = _entries(db, item)[f"problem:{pid}"]
    assert e["state"] == "removed" and e["title"] == PROBLEM and e["actions"] == ["restore"]
    assert f'problem #{pid} "{PROBLEM}", saved, solved [removed by you]' in "\n".join(
        session_contents.lines(session_contents.for_item(db, review.get_item(db, item))))
    back = session_records.decide(db, item, f"problem:{pid}", "restore", "review-page")
    assert "Brought back" in back["message"]
    assert _q(db, "SELECT title FROM problems WHERE id = ?", pid) == [(PROBLEM,)]
    assert _q(db, "SELECT id FROM solutions WHERE problem_id = ?", pid) == sol
    e = _entries(db, item)[f"problem:{pid}"]
    assert e["state"] == "" and e["actions"] == ["keep", "remove"]   # undecided, not "changed"
    log = [r[0] for r in _q(db, "SELECT decision FROM approval_log ORDER BY id")]
    assert log[-2:] == [f"record removed: problem #{pid}", f"record brought back: problem #{pid}"]


def test_a_removed_record_comes_back_with_manage_history_undo_too(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    out = session_records.decide(db, item, f"change:{cid}", "remove", "review-page")
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []
    res = call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "change",
                                     "record_id": cid})
    assert "error" not in res, res
    assert _q(db, "SELECT field_name FROM changes WHERE id = ?", cid) == [(CHANGE,)]
    assert out["undo"].startswith('manage_history(action="undo"')
    assert _entries(db, item)[f"change:{cid}"]["state"] == ""      # back, undecided again


def test_remove_then_keep_all_leaves_it_removed_and_edit_before_keep_still_works(backend):
    """The review page: Remove one, edit the line, Keep all. The edited text is the session's
    record plus which record was removed; the other record is marked kept."""
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    c = _page(db)
    html = c.get("/").text
    assert html.count('action="/record"') == 4                     # Keep + Remove for each
    assert "Keep all (with any edits)" in html and session_records.EXPLAIN[:40] in html
    r = c.post("/record", data={"csrf": _csrf(html), "item_id": str(item), "record": f"change:{cid}",
                                "action": "remove"}, headers=ORIGIN)
    assert r.status_code == 303 and "Removed change" in _flash(c, r)
    html = c.get("/").text
    assert "removed by you" in html and ">Bring back</button>" in html
    sid = review.get_item(db, item)["session_id"]
    edited = "Fixed the sign-in loop (return path kept)."
    r = c.post("/approve", data={"csrf": _csrf(html), "item_id": str(item), "summary": edited,
                                 "details": ""}, headers=ORIGIN)
    assert r.status_code == 303 and review.get_item(db, item)["status"] == "approved"
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []      # still removed
    saved = _q(db, "SELECT summary FROM sessions WHERE id = ?", sid)[0][0]
    assert saved == f'{edited} Removed by the user: change #{cid} "{CHANGE}".'
    d = _decisions(db, item)
    assert d[f"change:{cid}"]["d"] == "removed"
    assert d[f"problem:{pid}"]["d"] == "kept" and d[f"problem:{pid}"]["keep_all"] is True
    e = _entries(db, item)
    assert all(not x["actions"] for x in e.values())               # decided: no more buttons
    assert e[f"change:{cid}"]["state"] == "removed" and e[f"problem:{pid}"]["state"] == "kept"


def test_keep_all_without_edits_keeps_the_short_line(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    sid = review.get_item(db, item)["session_id"]
    review.approve(db, item, "the user said keep it", "command-line", lambda *_: ("x", None))
    assert _q(db, "SELECT summary FROM sessions WHERE id = ?", sid)[0][0] == \
        "claude-code session in Alpha: saved 1 change and 1 problem; also problem solved."
    assert {v["d"] for v in _decisions(db, item).values()} == {"kept"}


def test_dont_keep_decides_only_the_summary_line(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    session_records.decide(db, item, f"problem:{pid}", "remove", "review-page")
    sid = review.get_item(db, item)["session_id"]
    review.reject(db, item, "too vague", "the user said no", "command-line")
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]   # not removed
    note = _q(db, "SELECT summary FROM sessions WHERE id = ?", sid)[0][0]
    assert note.startswith("[auto-captured summary rejected: too vague")
    assert note.endswith(f'Removed by the user: problem #{pid} "{PROBLEM}".')
    assert f"change:{cid}" not in _decisions(db, item)               # left undecided
    with pytest.raises(session_records.RecordError) as e:            # decided: no more taps
        session_records.decide(db, item, f"change:{cid}", "keep", "review-page")
    assert e.value.payload["error"] == "already_decided"


def test_every_record_decided_still_waits_for_the_summary(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    session_records.decide(db, item, f"change:{cid}", "keep", "review-page")
    session_records.decide(db, item, f"problem:{pid}", "remove", "review-page")
    assert review.get_item(db, item)["status"] == "pending"
    assert any(i["id"] == item for i in review.list_items(db)["items"])


def test_gone_or_changed_since_shows_its_state_and_no_buttons(backend):
    S, db = backend, backend.backend.db_path
    t = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Write the docs"})
    item, pid, cid = _work(S, db)
    tid = t["record"]["id"]
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                             "status": "in_progress"})   # another session, after this one
    conn = sqlite3.connect(db)
    conn.execute("UPDATE record_history SET created_at = '2999-01-01 00:00:00' WHERE "
                 "table_name = 'todos' AND tool = 'todo/update'")   # (same second in a test)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM changes WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    e = _entries(db, item)
    assert e[f"change:{cid}"]["state"] == "gone" and e[f"change:{cid}"]["actions"] == []
    assert e[f"todo:{tid}"]["state"] == "changed" and e[f"todo:{tid}"]["actions"] == []
    assert e[f"problem:{pid}"]["actions"] == ["keep", "remove"]
    for rec in (f"change:{cid}", f"todo:{tid}"):
        with pytest.raises(session_records.RecordError) as err:
            session_records.decide(db, item, rec, "remove", "review-page")
        assert err.value.payload["error"] == "not_allowed"
    with pytest.raises(session_records.RecordError) as err:
        session_records.decide(db, item, "learning:999", "keep", "review-page")
    assert err.value.payload["error"] == "not_in_summary"


def test_a_record_the_session_only_updated_is_keep_only(backend):
    S, db = backend, backend.backend.db_path
    t = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Older todo"})
    S.backend.tracker.close_instance()
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": t["record"]["id"],
                             "status": "done"})
    S.backend.tracker.close_instance()
    [(item,)] = _q(db, "SELECT id FROM review_items WHERE kind = 'session_summary' ORDER BY id DESC LIMIT 1")
    e = _entries(db, item)[f"todo:{t['record']['id']}"]
    assert e["verb"] == "updated" and e["actions"] == ["keep"]
    with pytest.raises(session_records.RecordError) as err:
        session_records.decide(db, item, e["key"], "remove", "review-page")
    assert "can only be kept" in err.value.payload["message"]


# ---------------------------------------------------------------- the AI can't decide records
def test_no_model_tool_decides_records(backend):
    from flowstate import surface, tooldefs
    blob = json.dumps({"surface": {k: list(v) for k, v in surface.__dict__.items()
                                   if isinstance(v, dict)}}, default=str)
    assert "keep_record" not in blob and "remove_record" not in blob
    assert "keep_record" not in json.dumps([getattr(t, "inputSchema", {}) for t in
                                            getattr(tooldefs, "TOOLS", [])], default=str)
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    out = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                    "record": f"change:{cid}", "user_request": "remove change"})
    # whatever manage_review answers, the record itself is untouched and undecided
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    assert isinstance(out, (dict, str))


@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)


def _tap(S, item, decision, record, app="Cursor"):
    from flowstate import card_taps
    from test_review_card import call as card_call
    return card_call(S, "resolve_review_card", {"item_id": item, "decision": decision,
                                                "record": record, "project": "Alpha",
                                                "tap": card_taps.mint(item, "review", app)},
                     app=app)[1]


def test_card_record_tap_needs_a_card_host(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    out = _tap(S, item, "remove_record", f"change:{cid}")
    assert out["error"] == "not_a_card_tap"
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]


def test_card_record_taps_are_the_users_and_refuse_chat_text(backend, ui):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    from test_review_card import call as card_call
    _r, out = card_call(S, "resolve_review_card", {"item_id": item, "decision": "remove_record",
                                                   "record": f"change:{cid}", "project": "Alpha",
                                                   "user_request": "the user said remove it"})
    assert out["error"] == "not_a_card_tap"
    _r, out = card_call(S, "resolve_review_card", {"item_id": item, "decision": "approve",
                                                   "record": f"change:{cid}", "project": "Alpha"})
    assert out["error"] == "bad_request" and review.get_item(db, item)["status"] == "pending"
    out = _tap(S, item, "remove_record", f"change:{cid}")
    assert out["record_decided"] == f"change:{cid}" and out["now"] == "removed", out
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []
    card = review_cards.view(db, review.get_item(db, item), "Alpha")
    [e] = [x for x in card["contents"]["entries"] if x["key"] == f"change:{cid}"]
    assert e["state"] == "removed" and e["actions"] == ["restore"]
    assert card["approve_label"] == review.KEEP_ALL and card["does"] == review.KEEP_ALL_HOW
    assert _q(db, "SELECT client FROM record_history WHERE table_name = 'changes' AND record_id = ? "
                  "ORDER BY id DESC LIMIT 1", cid) == [("card (Cursor)",)]
    assert ("card", f"record removed: change #{cid}") in _q(db, "SELECT path, decision FROM approval_log")
    out = _tap(S, item, "restore_record", f"change:{cid}")
    assert out["now"] == "undecided" and _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    assert "Keep / Remove on each record" in out["text"]


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    return key


def test_card_record_taps_through_the_relay_follow_chat_approvals(backend, relay, ui, monkeypatch):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    monkeypatch.setattr(popup, "listener", lambda: "relay")
    out = _tap(S, item, "remove_record", f"change:{cid}")
    assert out["error"] == "phone_only"                              # off by default
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)] and not _decisions(db, item)
    approvals.set_relay_chat(db, True)
    out = _tap(S, item, "keep_record", f"change:{cid}")
    assert out["now"] == "kept"


def test_manage_review_shows_record_states_read_only(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    session_records.decide(db, item, f"change:{cid}", "remove", "review-page")
    listing = call(S, "manage_review", {"action": "list", "project": "Alpha"})
    [line] = [x for x in listing["items"] if x.startswith(f"#{item} ")]
    assert line.endswith("[2 records: 1 removed by you, 1 not decided yet]")
    assert "read-only for you" in listing["session_summary"]
    one = call(S, "manage_review", {"action": "list", "item_id": item})
    assert one["item"]["record_states"] == [{"record": f"change:{cid}", "state": "removed"},
                                            {"record": f"problem:{pid}", "state": "undecided"}]
    assert "you can't decide records" in one["record_note"]
    assert any("[removed by you]" in x for x in one["item"]["saved_records"])


# ---------------------------------------------------------------- the pages
def test_local_page_record_forms_need_csrf_and_origin(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    c = _page(db)
    html = c.get("/").text
    form = {"item_id": str(item), "record": f"change:{cid}", "action": "remove"}
    assert c.post("/record", data={**form, "csrf": "nope"}, headers=ORIGIN).status_code == 403
    assert c.post("/record", data={**form, "csrf": _csrf(html)},
                  headers={"origin": "https://evil.example"}).status_code == 403
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    r = c.post("/record", data={**form, "csrf": _csrf(html), "action": "explode"}, headers=ORIGIN)
    assert "keep, remove or restore" in _flash(c, r)
    r = c.post("/record", data={**form, "csrf": _csrf(html), "action": "keep"}, headers=ORIGIN)
    assert f"Kept change #{cid}" in _flash(c, r)
    html = c.get("/").text
    assert "records: 1 kept, 1 not decided yet" in html and "<script" not in html


def test_tunnel_page_has_the_record_buttons(backend, tmp_path):
    from test_review_tunnel import URL, _app, page_csrf, sign_in
    from flowstate.remote_auth import AuthStore
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase("correct horse battery staple")
    with TestClient(_app(db, store.path), base_url=URL, follow_redirects=False) as c:
        sign_in(c)
        csrf, r = page_csrf(c)
        assert 'action="/review/tunnel/record"' in r.text
        r = c.post("/review/tunnel/record", data={"csrf": csrf, "item_id": str(item),
                                                  "record": f"problem:{pid}", "action": "remove"},
                   headers={"origin": URL})
        assert r.status_code == 303
    assert _q(db, "SELECT id FROM problems WHERE id = ?", pid) == []
    assert _decisions(db, item)[f"problem:{pid}"]["via"] == "tunnel page"


JSON_H = {"Origin": "https://testbox.fsmcp.com", "Content-Type": "application/json"}


def test_phone_list_record_taps_then_keep_all(backend, relay):
    from test_review_without_terminal import _grant, _phone_client
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    c = _phone_client(db)
    body = c.post("/review/open", content=json.dumps({"grant": _grant(relay, db)[0]}),
                  headers=JSON_H).json()
    token = body["token"]
    [v] = [x for x in body["items"] if x["id"] == item]
    assert v["yes"] == review.KEEP_ALL and review.KEEP_ALL_HOW in v["question"]
    assert v["contents"]["explain"] == session_records.EXPLAIN
    assert [e["actions"] for e in v["contents"]["entries"]] == [["keep", "remove"]] * 2
    bad = c.post("/review/record", content=json.dumps({"token": "x" * 43, "item_id": item,
                 "record": f"change:{cid}", "action": "remove"}), headers=JSON_H)
    assert bad.status_code == 403 and _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    r = c.post("/review/record", content=json.dumps({"token": token, "item_id": item,
               "record": f"change:{cid}", "action": "remove"}), headers=JSON_H)
    assert r.status_code == 200, r.text
    [v] = [x for x in r.json()["items"] if x["id"] == item]
    [e] = [x for x in v["contents"]["entries"] if x["kind"] == "change"]
    assert e["state"] == "removed" and e["actions"] == ["restore"]
    assert _decisions(db, item)[f"change:{cid}"]["by"] == "phone"   # no handle (audit D19)
    r = c.post("/review/record", content=json.dumps({"token": token, "item_id": item,
               "record": f"change:{cid}", "action": "keep"}), headers=JSON_H)
    assert r.status_code == 409 and "already removed" in r.json()["message"]
    r = c.post("/review/decide", content=json.dumps({"token": token, "item_id": item,
                                                     "decision": "approve"}), headers=JSON_H)
    assert r.status_code == 200 and review.get_item(db, item)["status"] == "approved"
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []
    assert _decisions(db, item)[f"problem:{pid}"]["d"] == "kept"


def test_approve_page_record_taps_keep_the_grant_for_the_final_tap(backend, relay, monkeypatch):
    """Per-item phone links are made only for user-only items today (approvals.verify_grant),
    so a session summary reaches the phone through the list; the approve page still draws and
    handles its records if one is ever sent there (here: user_only patched to say so)."""
    from test_approvals import H, _decide, _open, _page as approve_client
    S, db = backend, backend.backend.db_path
    real = approvals.user_only
    monkeypatch.setattr(approvals, "user_only",
                        lambda it: real(it) or (it or {}).get("kind") == "session_summary")
    approvals.set_value(db, "phone_approvals", "on")
    item, pid, cid = _work(S, db)
    grant = sign(relay, claims(item, approvals.nonce_for(db, item)))
    c = approve_client(S)
    r = _open(c, item, grant)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    it = r.json()["item"]
    assert it["yes"] == review.KEEP_ALL and it["contents"]["entries"][0]["actions"] == ["keep", "remove"]

    def rec(record, action, tok=token):
        c.cookies.clear()
        return c.post(f"/approve/{item}/record", headers={**H, "cookie": f"fsa_page={tok}"},
                      content=json.dumps({"grant": grant, "token": tok, "record": record,
                                          "action": action}))
    assert rec(f"problem:{pid}", "remove", "y" * 43).status_code == 403
    r = rec(f"problem:{pid}", "remove")
    assert r.status_code == 200, r.text
    assert _q(db, "SELECT id FROM problems WHERE id = ?", pid) == []
    [e] = [x for x in r.json()["item"]["contents"]["entries"] if x["kind"] == "problem"]
    assert e["state"] == "removed" and e["actions"] == ["restore"]
    assert rec(f"problem:{pid}", "restore").status_code == 200
    assert _q(db, "SELECT id FROM problems WHERE id = ?", pid) == [(pid,)]
    assert rec(f"problem:{pid}", "remove").status_code == 200
    r = _decide(c, item, grant, token)                                # Keep all still works
    assert r.status_code == 200, r.text
    assert review.get_item(db, item)["status"] == "approved"
    assert _q(db, "SELECT id FROM problems WHERE id = ?", pid) == []
    assert rec(f"change:{cid}", "keep").status_code == 403            # the grant is used now


def test_phone_scripts_draw_record_buttons_as_text():
    from flowstate import approve_page
    for js in (review_phone.JS, approve_page.JS):
        assert "innerHTML" not in js and js.count("function savedItem(") == 1
        assert 'restore: "Bring back"' in js and "e.actions" in js
    assert '"/review/record"' in review_phone.JS and 'post("/record"' in approve_page.JS
    assert ".recrow{" in approve_page.CSS


def test_card_script_has_record_buttons():
    from flowstate.review_card_ui import CARD_HTML
    assert "chooseRecord" in CARD_HTML and '"_record"' in CARD_HTML and "innerHTML" not in CARD_HTML
    tool = review_cards.app_tool()
    assert tool.meta["ui"]["visibility"] == ["app"]
    assert "remove_record" in tool.inputSchema["properties"]["decision"]["enum"]


def test_walk_box_is_keep_all(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    msg, schema = waiting._form({**review.get_item(db, item), "project": "Alpha"}, 1, 1)
    assert "Tick Keep all and submit" in msg and schema["properties"]["approve"]["title"] == "Keep all"
    assert "review page or your phone" in msg


# ---------------------------------------------------------------- the terminal
class FakeTTY:
    def __init__(self, answers):
        self.answers, self.told = list(answers), []

    def ask(self, prompt):
        self.told.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def tell(self, text):
        self.told.append(text)

    def close(self):
        pass


def _cli(*argv):
    from flowstate import cli
    return cli.main(list(argv))


def test_terminal_lists_records_and_decides_one(backend, capsys, monkeypatch):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    assert _cli("review", "records", str(item), "--db", str(db)) == 0
    out = capsys.readouterr().out
    assert f"change:{cid}" in out and "not decided yet" in out and "[keep/remove]" in out
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)
    assert _cli("review", "record", str(item), f"change:{cid}", "remove", "--db", str(db)) == 2
    assert "Refused" in capsys.readouterr().out
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    tty = FakeTTY(["n"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("review", "record", str(item), f"change:{cid}", "remove", "--db", str(db)) == 1
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    tty = FakeTTY(["y"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("review", "record", str(item), f"change#{cid}", "remove", "--db", str(db)) == 0
    assert "Removed change" in capsys.readouterr().out
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []
    assert _decisions(db, item)[f"change:{cid}"]["via"] == "terminal"
    assert _cli("review", "records", str(item), "--db", str(db)) == 0
    assert "removed by you" in capsys.readouterr().out
    assert _cli("review", "record", str(item), "--db", str(db)) == 2   # incomplete: usage


def test_terminal_approve_says_keep_all(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    lines = cli_review._describe(review.get_item(db, item), "Alpha", db)
    assert any("Approving is Keep all" in x for x in lines)


def test_docs_and_changelog_mention_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert "flowstate review record" in (root / "README.md").read_text(encoding="utf-8")
    assert "Keep all" in (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "/review/record" in (root / "docs" / "OPEN_PORTS.md").read_text(encoding="utf-8")
