"""Task 39: a session that saved nothing new (reads, undo, review decisions, a handoff pickup,
todo claims) files no session_summary review item; a session that saved real work keeps its
item, with plainer words. The phone, PC and tunnel pages ask "Keep this session summary?".
Temp DBs only."""

import asyncio
import json
import re
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from flowstate import review, review_mode, review_phone, waiting
from flowstate.autosession import (AUTO_TAG, HOUSEKEEPING, MAX_EVENTS, MAX_SUMMARY, OFF_TAG,
                                   SKIP_TAG, AutoSessions, build_summary, is_housekeeping)

ASK = "User: yes, do that"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _session(db, sid):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone())
    finally:
        conn.close()


def _items(db, sid):
    return _q(db, "SELECT id, kind, status, summary FROM review_items WHERE session_id = ?", sid)


def _closed(db, events, client="Cursor", project=1):
    """A session in `project` with these event labels, closed as the app quits."""
    t = AutoSessions(db, idle_minutes=60)
    sid = t.touch(client, project, "read:start_here")
    for e in events:
        t.note(sid, e)
    assert t.close_instance() == [sid]
    return sid


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    out = asyncio.run(go()).content[0].text
    try:
        return json.loads(out)
    except ValueError:
        return out


# ---------------------------------------------------------------- the classifier
@pytest.mark.parametrize("events", [
    [], ["read:search", "read:start_here", "read:review_list"],
    ["history/undo"], ["history/undo: todo #3"], ["review_approve"], ["review_reject: wrong"],
    ["handoff/pickup"], ["session/start"], ["todo/claim: todo #1", "todo/renew", "todo/release"],
    sorted(HOUSEKEEPING) + ["read:search"]])
def test_housekeeping_sessions_file_no_item(seeded_db, events):
    assert is_housekeeping(events)
    sid = _closed(seeded_db, events)
    s = _session(seeded_db, sid)
    assert _items(seeded_db, sid) == []
    assert s["ended_at"] is not None and s["summary"].startswith(SKIP_TAG)
    assert s["summary"].startswith(SKIP_TAG + " Cursor session in Alpha: nothing new saved")
    out = json.loads(s["outcomes"])
    assert out["needs_review"] is False and out["review"] == "skipped_housekeeping"
    assert out["close_reason"] == "shutdown" and "review_item_id" not in out
    assert _q(seeded_db, "SELECT close_reason FROM auto_sessions WHERE session_id = ?", sid) == [("shutdown",)]


@pytest.mark.parametrize("label", sorted(HOUSEKEEPING))
def test_each_housekeeping_label_alone_files_no_item(seeded_db, label):
    sid = _closed(seeded_db, [label])
    assert _items(seeded_db, sid) == []


@pytest.mark.parametrize("events", [
    ["learning: Retry the backup at 3am"],
    ["history/undo", "review_approve", "read:search", "todo: Write the release note"],
    ["some_future_tool/act: something new"],          # unknown write label: kept (fail safe)
    ["suggested learning: maybe"], ["review_mode/set"], ["link/add"], ["todo/update: todo #1"],
    ["delete_request/record"], ["import_memory: notes"]])
def test_real_work_keeps_its_item(seeded_db, events):
    assert not is_housekeeping(events)
    sid = _closed(seeded_db, events)
    [(item, kind, status, summary)] = _items(seeded_db, sid)
    assert (kind, status) == ("session_summary", "pending") and summary.startswith(AUTO_TAG)
    s = _session(seeded_db, sid)
    out = json.loads(s["outcomes"])
    assert out["needs_review"] is True and out["review_item_id"] == item
    assert s["summary"] == summary


def test_a_long_session_keeps_its_saves_over_its_reads(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    sid = t.touch("Cursor", 1, "read:start_here")
    t.note(sid, "learning: the one real save")
    for _ in range(MAX_EVENTS + 20):
        t.touch("Cursor", 1, "read:search")
    events = json.loads(_q(seeded_db, "SELECT events FROM auto_sessions WHERE session_id = ?", sid)[0][0])
    assert len(events) == MAX_EVENTS and "learning: the one real save" in events
    t.close_instance()
    [(_id, _k, _s, summary)] = _items(seeded_db, sid)
    assert summary == f"{AUTO_TAG} Cursor session in Alpha: saved 1 learning."


def test_a_full_list_of_saves_counts_as_work(seeded_db):
    t = AutoSessions(seeded_db, idle_minutes=60)
    sid = t.touch("Cursor", 1)
    for _ in range(MAX_EVENTS + 5):
        t.touch("Cursor", 1, "history/undo")   # older saves may have dropped out of the list
    t.close_instance()
    [(_id, _k, _s, summary)] = _items(seeded_db, sid)
    assert summary.endswith(f"Only its last {MAX_EVENTS} saves were counted.")


# ---------------------------------------------------------------- the wording
def test_summary_wording_counts_by_kind():
    b = lambda ev: build_summary("Cursor", "FlowState", ev)   # noqa: E731
    assert b(["learning: a", "learning: b", "todo: c", "read:search"]) == \
        f"{AUTO_TAG} Cursor session in FlowState: saved 2 learnings and 1 todo."
    assert b(["decision: x"]) == f"{AUTO_TAG} Cursor session in FlowState: saved 1 decision."
    assert b(["todo: a", "change: b", "learning: c", "decision: d", "decision: e", "problem: f"]) == \
        (f"{AUTO_TAG} Cursor session in FlowState: saved 1 learning, 2 decisions, 1 change, "
         "1 problem and 1 todo.")
    assert b(["log_learning: old tool", "todo/add: old tool", "history/undo"]) == \
        f"{AUTO_TAG} Cursor session in FlowState: saved 1 learning and 1 todo."
    assert b(["change: x", "suggested learning: y", "link/add", "link/add: z", "todo/update: todo #2",
              "brand_new/thing"]) == \
        (f"{AUTO_TAG} Cursor session in FlowState: saved 1 change; suggested 1 learning for review; "
         "also new link (x2), todo update, brand new thing.")
    assert b(["history/undo", "history/undo", "review_approve"]) == \
        f"{AUTO_TAG} Cursor session in FlowState: nothing new saved (2 undos and 1 review decision)."
    assert b([]) == f"{AUTO_TAG} Cursor session in FlowState: nothing new saved."


def test_summary_is_capped_and_has_no_jargon():
    s = build_summary("C" * 80, "P" * 200, [f"tool{i}/act: x" for i in range(MAX_EVENTS)], trimmed=True)
    assert len(s) <= MAX_SUMMARY and s.startswith(AUTO_TAG) and s.endswith("...")
    plain = build_summary("Cursor", "Alpha", ["learning: x", "read:search"])
    assert not re.search(r"idle|timeout|disconnect|stale|calls?\b|min\b", plain), plain


def test_idle_close_keeps_the_reason_in_outcomes_not_in_the_text(seeded_db):
    import time
    t = AutoSessions(seeded_db, idle_minutes=0.01)
    sid = t.touch("Grok", 2, "read:start_here")
    t.note(sid, "learning: x")
    time.sleep(1.8)   # timestamps have 1 s resolution
    assert t.close_idle() == [sid]
    s = _session(seeded_db, sid)
    assert s["summary"] == f"{AUTO_TAG} Grok session in Beta Project: saved 1 learning."
    assert json.loads(s["outcomes"])["close_reason"] == "idle"
    assert _q(seeded_db, "SELECT payload FROM review_items WHERE session_id = ?", sid) == \
        [(json.dumps({"close_reason": "idle", "calls": 1}),)]


# ---------------------------------------------------------------- review mode off, claims, brief
def test_review_mode_off_path_unchanged_for_real_work(seeded_db):
    review_mode.set_mode(seeded_db, "off", [], 1, "User: turn review off", "test")
    sid = _closed(seeded_db, ["learning: x"])
    s = _session(seeded_db, sid)
    assert _items(seeded_db, sid) == []
    assert s["summary"] == f"{OFF_TAG} Cursor session in Alpha: saved 1 learning."
    out = json.loads(s["outcomes"])
    assert out["needs_review"] is False and out["review"] == "off"
    # housekeeping with review off: the same plain housekeeping record
    sid = _closed(seeded_db, ["history/undo"])
    assert _session(seeded_db, sid)["summary"].startswith(SKIP_TAG) and _items(seeded_db, sid) == []


def test_claims_are_released_for_a_skipped_session(backend):
    S = backend
    assert call(S, "manage_todos", {"action": "claim", "project": "Alpha", "todo_id": 1}).get("claimed")
    assert _q(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(1,)]
    [sid] = S.backend.tracker.close_instance("shutdown")
    assert _q(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]
    assert _items(S.backend.db_path, sid) == []
    assert json.loads(_session(S.backend.db_path, sid)["outcomes"])["review"] == "skipped_housekeeping"


def test_brief_skips_housekeeping_sessions_in_its_last_session_line(backend):
    S, db = backend, backend.backend.db_path
    review_mode.set_mode(db, "off", [], 1, "User: turn review off", "test")
    _closed(db, ["learning: x"], client="Grok")          # a real session (review off: final)
    _closed(db, ["history/undo", "review_approve"])      # then a housekeeping one
    brief = call(S, "start_here", {"project": "Alpha"}, app="Claude Code")
    line = next(x for x in brief.splitlines() if x.startswith("Last session"))
    assert "Grok session in Alpha: saved 1 learning." in line and "housekeeping" not in brief


# ---------------------------------------------------------------- the real labels, end to end
def test_real_undo_and_review_calls_are_housekeeping(backend):
    S, db = backend, backend.backend.db_path
    made = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Undo me later"})
    S.backend.tracker.close_instance("shutdown")          # the first session: real work, an item
    [(setup_item, *_)] = _q(db, "SELECT id FROM review_items WHERE kind = 'session_summary'")
    tid = made["record"]["id"]
    for name, args in (("start_here", {"project": "Alpha"}),
                       ("search", {"project": "Alpha", "query": "undo"}),
                       ("manage_history", {"action": "list", "project": "Alpha"}),
                       ("manage_history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                                           "record_id": tid}),
                       ("manage_review", {"action": "list", "project": "Alpha"}),
                       ("manage_review", {"action": "approve", "project": "Alpha", "item_id": setup_item,
                                          "user_request": "User: approve that summary"}),
                       ("manage_todos", {"action": "claim", "project": "Alpha", "todo_id": 1}),
                       ("manage_todos", {"action": "release", "project": "Alpha", "todo_id": 1})):
        out = call(S, name, args)
        assert not (isinstance(out, dict) and "error" in out), (name, out)
    open_ = _q(db, "SELECT session_id, events FROM auto_sessions WHERE closed_at IS NULL")
    [(sid, events)] = open_
    labels = [e.split(": ")[0] for e in json.loads(events)]
    assert {"history/undo", "review_approve", "todo/claim", "todo/release"} <= set(labels), labels
    S.backend.tracker.close_instance("shutdown")
    assert _items(db, sid) == []
    assert _session(db, sid)["summary"].startswith(SKIP_TAG)


def test_a_review_mode_change_is_real_work(backend):
    S, db = backend, backend.backend.db_path
    out = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "everything",
                                    "user_request": "User: make everything wait for review"})
    assert not out.get("error"), out
    [(sid, events)] = _q(db, "SELECT session_id, events FROM auto_sessions WHERE closed_at IS NULL")
    assert "review_mode/set" in json.loads(events)
    S.backend.tracker.close_instance("shutdown")
    [(_id, _k, _s, summary)] = _items(db, sid)
    assert summary == f"{AUTO_TAG} Cursor session in Alpha: also review mode change."


# ---------------------------------------------------------------- phone / PC / tunnel / chat wording
def _summary_item(db):
    sid = _closed(db, ["learning: a", "todo: b"])
    return _items(db, sid)[0][0], sid


def test_phone_list_asks_keep_this_session_summary(seeded_db):
    item, _sid = _summary_item(seeded_db)
    [v] = [x for x in review_phone.items(seeded_db) if x["id"] == item]
    assert v["question"].startswith("Keep this session summary? Keep saves it as the session's record.")
    assert "nothing is deleted" in v["question"]
    assert (v["yes"], v["no"]) == ("Keep", "Don't keep")
    assert v["summary"] == "Cursor session in Alpha: saved 1 learning and 1 todo."
    other = review.add_item(seeded_db, 1, "learning", "A suggestion", None, {}, "cursor")
    [o] = [x for x in review_phone.items(seeded_db) if x["id"] == other]
    assert (o["yes"], o["no"]) == ("Approve", "Reject")
    assert o["question"] == "Approve to keep it; reject to leave it out."
    assert 'it.yes || "Approve"' in review_phone.JS and 'it.no || "Reject"' in review_phone.JS


def test_pc_page_keep_and_dont_keep(backend, seeded_db):
    from flowstate.review_page import Page, create_app
    item, sid = _summary_item(seeded_db)
    p = Page(seeded_db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    assert "<strong>Keep this session summary?</strong>" in html and "Keep (with any edits)" in html
    assert "Don&#x27;t keep</button>" in html and "Reason (optional)" in html
    assert ">Cursor session in Alpha: saved 1 learning and 1 todo.</textarea>" in html
    assert AUTO_TAG not in html
    text = "Cursor session in Alpha: saved 1 learning and 1 todo."
    r = c.post("/approve", data={"csrf": csrf, "item_id": str(item), "summary": text, "details": ""},
               headers={"Origin": "http://127.0.0.1:8770"})
    assert r.status_code == 303
    assert _session(seeded_db, sid)["summary"] == text            # stored without the tag
    assert _q(seeded_db, "SELECT status FROM review_items WHERE id = ?", item) == [("approved",)]
    assert "Kept #" in c.get(r.headers["location"]).text
    item2, sid2 = _summary_item(seeded_db)
    r = c.post("/reject", data={"csrf": csrf, "item_id": str(item2), "reason": ""},
               headers={"Origin": "http://127.0.0.1:8770"})
    assert _q(seeded_db, "SELECT status FROM review_items WHERE id = ?", item2) == [("rejected",)]
    assert _session(seeded_db, sid2)["summary"].startswith("[auto-captured summary rejected")
    assert "Not kept #" in c.get(r.headers["location"]).text


def test_tunnel_page_asks_keep_this_session_summary(seeded_db, tmp_path):
    from test_review_tunnel import ORIGIN, URL, _app, page_csrf, sign_in
    from flowstate import server as S
    from flowstate.remote_auth import AuthStore
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase("correct horse battery staple")
    item, sid = _summary_item(seeded_db)
    try:
        with TestClient(_app(seeded_db, store.path), base_url=URL, follow_redirects=False) as c:
            sign_in(c)
            csrf, r = page_csrf(c)
            assert "<strong>Keep this session summary?</strong>" in r.text
            assert "Keep (with any edits)" in r.text and "Don&#x27;t keep</button>" in r.text
            c.post("/review/tunnel/approve", data={"csrf": csrf, "item_id": str(item),
                                                   "summary": "Cursor session in Alpha: saved 1 learning and 1 todo."},
                   headers=ORIGIN)
            assert _session(seeded_db, sid)["summary"] == "Cursor session in Alpha: saved 1 learning and 1 todo."
    finally:
        S.backend.tracker = None


def test_chat_list_and_popup_say_what_keep_does(backend):
    S, db = backend, backend.backend.db_path
    item, _sid = _summary_item(db)
    out = call(S, "manage_review", {"action": "list", "project": "Alpha"})
    assert out["items"] == [f"#{item} (Alpha, session summary): Cursor session in Alpha: saved 1 learning "
                            "and 1 todo."]
    assert out["session_summary"] == waiting.KEEP_NOTE and "nothing is deleted" in waiting.KEEP_NOTE
    it = review.get_item(db, item)
    msg, schema = waiting._form({**it, "project": "Alpha"}, 1, 1)
    assert "Keep this session summary?" in msg and AUTO_TAG not in msg
    assert schema["properties"]["approve"]["title"] == "Keep"
    other = review.add_item(db, 1, "learning", "A suggestion", None, {}, "cursor")
    msg, schema = waiting._form({**review.get_item(db, other), "project": "Alpha"}, 1, 1)
    assert schema["properties"]["approve"]["title"] == "Approve" and "Keep this" not in msg
    assert "session_summary" not in call(S, "manage_review", {"action": "list", "project": "Beta Project"})
