"""Task 73: the review page's Project history (project_history.py, history_page.py).

The timeline query (ordering, each filter, search, pagination, the faded-detail notice),
erased records (their text appears nowhere: timeline, search, record view, todos, HTML),
todos as first-class records (who created each, every later change with who did it, the
Todos view), the pages themselves, and who can reach them (the review page's own session
only; not the tunnel page, not an MCP tool, not the HTTP / relay listeners). Temp databases
and the in-process server only (conftest.py)."""

import asyncio
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import erase, project_history as H

SECRET = "Zebra quantum marmalade protocol for the north gate"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        out = conn.execute(sql, a).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def call(S, name, args, app="App A"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return json.loads(asyncio.run(go()).content[0].text)


@pytest.fixture
def S(backend, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_WORKSPACE", "test-box")
    return backend


@pytest.fixture
def page(S, seeded_db):
    from starlette.testclient import TestClient
    from flowstate.review_page import HOST, Page, create_app
    p = Page(seeded_db, 8770)
    c = TestClient(create_app(p), base_url=f"http://{HOST}:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/").text).group(1)
    return c, csrf, seeded_db


def _todo(S, title, app="App A", **kw):
    r = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": title, **kw}, app)
    return r["record"]["id"]


def _decision(S, text, app="App A", **kw):
    r = call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": text, **kw}, app)
    return r["record"]["id"]


def _all(db, pid=1, **kw):
    return H.timeline(db, pid, per_page=100, **kw)


# ---------------------------------------------------------------- ordering
def test_ordering_follows_time_oldest_first_and_newest_first(S, seeded_db):
    db = seeded_db
    ids = [_decision(S, t) for t in ("Ship on Fridays only", "Paint the shed green",
                                     "Write tests before code")]
    # the project's own creation comes first
    first = _all(db)["rows"][0]
    assert first["record_type"] == "project" and first["what"] == "project created"
    # make the third the oldest and the first the newest, whatever their ids
    for n, rid in enumerate(reversed(ids)):
        _q(db, "UPDATE record_history SET created_at = ? WHERE table_name = 'conversations' "
               "AND record_id = ?", f"2026-10-0{n + 1} 10:00:00", rid)
    old = [r["record_id"] for r in _all(db, rtype="decision")["rows"] if r["event"] == "edit_created"]
    new = [r["record_id"] for r in _all(db, rtype="decision", newest_first=True)["rows"]
           if r["event"] == "edit_created"]
    assert old == list(reversed(ids)) and new == ids


def test_same_second_a_record_is_created_before_it_is_edited(S, seeded_db):
    tid = _todo(S, "Same-second todo")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                             "status": "in_progress"})
    _q(seeded_db, "UPDATE record_history SET created_at = '2026-10-20 10:00:00' WHERE "
                  "table_name = 'todos' AND record_id = ?", tid)
    rows = _all(seeded_db, record=("todo", tid))["rows"]
    whats = [r["what"] for r in rows if r["at_utc"] == "2026-10-20 10:00:00"]
    assert whats == ["created", "status pending -> in_progress"]


# ---------------------------------------------------------------- filters
def test_filter_by_type_app_and_dates(S, seeded_db):
    db = seeded_db
    _todo(S, "Todo by A", model="model-a")
    _decision(S, "Decision by B", app="App B")
    todos = _all(db, rtype="todo")["rows"]
    assert todos and all(r["type"] == "todo" or r["record_type"] == "todo" for r in todos)
    decisions = _all(db, rtype="decision")["rows"]
    assert {r["label"] for r in decisions} == {"Decision by B"}
    by_b = _all(db, app="app b")["rows"]
    assert by_b and {r["app"] for r in by_b} == {"App B"}
    by_a = _all(db, app="App A", rtype="todo")["rows"]
    assert [r["model"] for r in by_a if r["event"] == "edit_created"] == ["model-a"]
    # machine counts for the app box too
    boxed = _all(db, app="test-box")["rows"]
    assert boxed and all("test-box" in (r["machine"] or "") for r in boxed)
    assert _all(db, app="zzz-none")["total"] == 0
    # dates: local time, a bare `until` date means the end of that day
    assert _all(db, until="2000-01-01")["total"] == 0
    today = datetime.now().strftime("%Y-%m-%d")
    assert _all(db, since=today, until=today)["total"] >= 2
    with pytest.raises(H.HistoryPageError) as e:
        _all(db, since="last tuesday")
    assert e.value.payload["error"] == "bad_time"
    # another project's entries never show
    assert all(r["label"] != "Beta todo" for r in _all(db)["rows"])
    assert {r["label"] for r in _all(db, 2, rtype="todo")["rows"]} == {"Beta todo"}


def test_types_list_only_what_the_project_has(S, seeded_db):
    _todo(S, "Typed todo")
    types = _all(seeded_db)["types"]
    assert "todo" in types and "project" in types and "conflict" not in types


def test_search_finds_live_text_old_versions_and_entry_lines(S, seeded_db):
    db = seeded_db
    tid = _todo(S, "Café crème machine")
    other = _todo(S, "Unrelated chore")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                             "title": "Coffee machine"})
    hits = _all(db, q="coffee")["rows"]
    assert hits and {r["record_id"] for r in hits if r["record_type"] == "todo"} == {tid}
    # the old title is only in the edit history now (stored with JSON escapes): still found
    old = _all(db, q="crème")["rows"]
    assert old and {r["record_id"] for r in old if r["record_type"] == "todo"} == {tid}
    assert other not in {r["record_id"] for r in old}
    # the entry's own line: "session" finds the session entries
    assert all(r["record_type"] == "session" or "session" in r["what"].lower()
               for r in _all(db, q="session #")["rows"])
    assert _all(db, q="no such words anywhere")["total"] == 0


# ---------------------------------------------------------------- pagination
def test_pagination_pages_are_disjoint_and_complete(S, seeded_db):
    for i in range(7):
        _todo(S, f"Paged todo {i}")
    every = [(r["at_utc"], r["event"], r["record_id"], r["what"])
             for r in H.timeline(seeded_db, 1, rtype="todo", per_page=100)["rows"]]
    seen = []
    first = H.timeline(seeded_db, 1, rtype="todo", per_page=3)
    assert first["pages"] == -(-first["total"] // 3) and first["total"] == len(every)
    for p in range(1, first["pages"] + 1):
        rows = H.timeline(seeded_db, 1, rtype="todo", per_page=3, page=p)["rows"]
        assert 0 < len(rows) <= 3
        seen += [(r["at_utc"], r["event"], r["record_id"], r["what"]) for r in rows]
    assert seen == every
    assert H.timeline(seeded_db, 1, per_page=1000)["per_page"] == H.MAX_PER_PAGE
    assert H.timeline(seeded_db, 1, page=999)["rows"] == []


def test_the_page_paginates(page, S):
    c, _, db = page
    for i in range(60):
        _q(db, "INSERT INTO record_history (edit_id, table_name, record_id, project_id, tool, "
               "old_row, new_row, client, created_at) VALUES (?, 'todos', 1, 1, 'todo/update', "
               "'{\"status\": \"pending\"}', '{\"status\": \"blocked\"}', 'Bulk', ?)",
           f"e{i}", f"2026-10-01 10:{i:02d}:00")
    html = c.get("/?view=history&project=1").text
    assert html.count("<li>") == H.PER_PAGE and "page 1 of" in html
    assert "view=history&amp;project=1&amp;page=2" in html
    p2 = c.get("/?view=history&project=1&page=2").text
    assert "page 2 of" in p2 and "Previous" in p2


# ---------------------------------------------------------------- erased
def _erased_decision(S, db):
    did = _decision(S, SECRET)
    _decision(S, "Use Postgres for the archive", replaces=did)
    call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "decision",
                              "record_id": did})
    erase.run(db, erase.plan(db, [f"decision:{did}"]))
    return did


def test_an_erased_record_shows_erased_and_its_text_appears_nowhere(S, seeded_db, page):
    c, _, db = page
    did = _erased_decision(S, db)
    words = SECRET.lower().split()
    every = _all(db)
    named = [r for r in every["rows"] if r["record_type"] == "decision" and r["record_id"] == did]
    assert named and all(r["label"] == "[erased]" and r["state"] == "erased" for r in named)
    linked = [r for r in every["rows"] if (r.get("other") or {}).get("record_id") == did]
    assert all(r["other"]["label"] == "[erased]" for r in linked)   # (the link went with it)

    def clean(blob):
        low = json.dumps(blob, ensure_ascii=False).lower() if not isinstance(blob, str) else blob.lower()
        assert SECRET.lower() not in low
        for i in range(len(words) - 2):   # no three-word piece of it either
            assert " ".join(words[i:i + 3]) not in low
    clean(every)
    for q in (SECRET, "zebra", "marmalade", "north gate"):
        res = _all(db, q=q)
        clean(res)
        assert all(r["record_id"] != did or r["label"] == "[erased]" for r in res["rows"])
        assert not any(r["record_type"] == "decision" and r["record_id"] == did
                       and r["event"] != "link_added" for r in res["rows"])
        html = c.get(f"/?view=history&project=1&q={q}").text
        # the search box shows back what the user typed; nothing else may have it
        clean(html.replace(f'name="q" placeholder="search" value="{q}"', ""))
    d = H.record_detail(db, 1, "decision", did)
    assert d["state"] == "erased" and d["label"] == "[erased]" and not d["fields"] and not d["edits"]
    clean(d)
    for url in ("/?view=history&project=1", "/?view=history&project=1&order=newest",
                f"/?view=record&project=1&type=decision&id={did}", "/?view=todos&project=1",
                "/?view=history&project=1&type=link", "/?view=history&project=1&type=decision"):
        html = c.get(url).text
        clean(html)
    rec = c.get(f"/?view=record&project=1&type=decision&id={did}").text
    assert "[erased]" in rec and "erased for good" in rec


def test_a_deleted_but_not_erased_record_still_shows(S, seeded_db):
    did = _decision(S, "Keep the old logo for now")
    call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "decision",
                              "record_id": did})
    d = H.record_detail(seeded_db, 1, "decision", did)
    assert d["state"] == "deleted" and d["label"] == "Keep the old logo for now"
    assert [e["what"] for e in d["edits"]] == ["created", "deleted"]
    assert d["edits"][1]["changes"]["user_prompt_summary"]["old"] == "Decision: Keep the old logo for now"


def test_a_record_nothing_names_is_not_called_erased(page):
    c, _, _ = page
    html = c.get("/?view=record&project=1&type=decision&id=4242").text
    assert "Nothing in this project's history names this record" in html and "[erased]" not in html


# ---------------------------------------------------------------- faded detail
def test_faded_notice_only_when_the_range_reaches_past_the_cutoff(S, seeded_db, page):
    c, _, db = page
    _todo(S, "Recent todo")
    assert _all(db)["faded"] is None
    _q(db, "UPDATE record_history SET created_at = '2025-01-02 03:04:05' WHERE id = "
           "(SELECT MIN(id) FROM record_history)")
    f = _all(db)["faded"]
    assert f and f["days"] == 90 and f["before"] == H.who.local_time(H.cutoff())[:10]
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    assert _all(db, since=recent)["faded"] is None
    assert _all(db, since="2024-01-01")["faded"] is not None
    assert "Older detail has faded" in c.get("/?view=history&project=1").text
    assert "Older detail has faded" not in c.get(f"/?view=history&project=1&since={recent}").text
    # the old entry is still listed, with what the edit history kept
    old = [r for r in _all(db)["rows"] if r["at_utc"] == "2025-01-02 03:04:05"]
    assert old and old[0]["what"] == "created"


def test_rows_from_before_tracking_say_not_recorded(S, seeded_db):
    # the seeded todo was written straight into the database: no edit history, no activity
    rows = _all(seeded_db, record=("todo", 1))["rows"]
    made = [r for r in rows if r["event"] == "created"]
    assert made and made[0]["who"] == "not recorded"
    assert made[0]["identity"].startswith("not recorded")


def test_faded_days_follow_the_activity_setting(S, seeded_db, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_ACTIVITY_DAYS", "10")
    _q(seeded_db, "INSERT INTO record_history (edit_id, table_name, record_id, project_id, tool, "
                  "old_row, new_row, client, created_at) VALUES ('x', 'todos', 1, 1, 'todo/update', "
                  "'{}', '{}', 'Old', ?)",
       (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"))
    assert _all(seeded_db)["faded"]["days"] == 10


# ---------------------------------------------------------------- todos, first-class
def _todo_story(S, db):
    """One todo through every kind of change, by two apps."""
    tid = _todo(S, "Draft the release notes", model="model-a")
    t = {"project": "Alpha", "todo_id": tid}
    call(S, "manage_todos", {"action": "claim", **t, "minutes": 15})
    call(S, "manage_todos", {"action": "renew", **t, "minutes": 30})
    call(S, "manage_todos", {"action": "release", **t})
    call(S, "manage_todos", {"action": "claim", **t, "minutes": 10})
    # that claim ran out an hour ago with nobody renewing or releasing it
    _q(db, "UPDATE todo_claim_events SET at = datetime('now', '-2 hours') WHERE id = "
           "(SELECT MAX(id) FROM todo_claim_events)")
    _q(db, "UPDATE todo_claims SET expires_at = datetime('now', '-110 minutes')")
    call(S, "manage_todos", {"action": "claim", **t}, app="App B")      # took over
    call(S, "manage_todos", {"action": "update", **t, "title": "Draft the 2.4.4 notes"})  # override
    call(S, "manage_todos", {"action": "handoff", **t, "where_i_was": "half done",
                             "next_step": "finish it"}, app="App B")
    call(S, "manage_todos", {"action": "update", **t, "status": "in_progress"}, app="App B")
    call(S, "manage_todos", {"action": "update", **t, "status": "done"}, app="App B")
    call(S, "manage_todos", {"action": "update", **t, "status": "pending"})            # reopened
    call(S, "manage_todos", {"action": "update", **t, "status": "cancelled"})
    call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                              "record_id": tid}, app="App B")
    return tid


def test_a_todo_timeline_has_every_change_with_who_did_it(S, seeded_db):
    db = seeded_db
    tid = _todo_story(S, db)
    rows = _all(db, record=("todo", tid))["rows"]
    events = [(r["event"], r["what"], r["app"]) for r in rows]
    have = {e for e, _, _ in events}
    for want in ("edit_created", "todo_claim", "todo_renew", "todo_release", "todo_expired",
                 "todo_override", "todo_handoff", "edit"):
        assert want in have, (want, events)
    whats = [w for _, w, _ in events]
    assert ("created", "App A") in [(w, a) for _, w, a in events]
    assert any(w.startswith("claimed") and "took over an expired claim" in w for w in whats)
    assert any("expired (nobody renewed or released it)" in w for w in whats)
    assert "edited title" in whats
    assert ("status pending -> in_progress", "App B") in [(w, a) for _, w, a in events]
    assert ("completed (in_progress -> done)", "App B") in [(w, a) for _, w, a in events]
    assert ("reopened (done -> pending)", "App A") in [(w, a) for _, w, a in events]
    assert "cancelled (pending -> cancelled)" in whats
    assert ("deleted", "App B") in [(w, a) for _, w, a in events]
    created = next(r for r in rows if r["event"] == "edit_created")
    assert (created["model"], created["machine"], created["identity"]) == \
        ("model-a", "test-box", "self-reported")
    # the expiry sits at the claim's end, between the claim and the take-over
    at = [r["event"] for r in rows]
    assert at.index("todo_expired") < max(i for i, e in enumerate(at) if e == "todo_claim")
    # an undo of the delete brings it back, and says so
    call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                               "record_id": tid})
    again = [r["what"] for r in _all(db, record=("todo", tid))["rows"]]
    assert "brought back (undo of a delete)" in again and "deleted (later undone)" in again


def test_a_renewed_claim_is_not_reported_expired(S, seeded_db):
    tid = _todo(S, "Renewed in time")
    call(S, "manage_todos", {"action": "claim", "project": "Alpha", "todo_id": tid})
    call(S, "manage_todos", {"action": "renew", "project": "Alpha", "todo_id": tid})
    assert "todo_expired" not in {r["event"] for r in _all(seeded_db, record=("todo", tid))["rows"]}
    assert H._claim_minutes("claimed todo #3 Fix for 5m bug for 15m") == 15
    assert H._claim_minutes("renewed claim on todo #3 (30m)") == 30


def test_todos_view_lists_creator_and_status(S, seeded_db, page):
    c, _, db = page
    a = _todo(S, "Open todo", model="model-a")
    b = _todo(S, "Finished todo", app="App B")
    d = _todo(S, "Gone todo")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": b, "status": "done"})
    call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                              "record_id": d})
    call(S, "manage_todos", {"action": "claim", "project": "Alpha", "todo_id": a}, app="App B")
    res = H.todos(db, 1)
    by_id = {t["id"]: t for t in res["todos"]}
    assert by_id[a]["status"] == "pending" and by_id[a]["creator"]["app"] == "App A"
    assert (by_id[a]["creator"]["model"], by_id[a]["creator"]["machine"],
            by_id[a]["creator"]["identity"]) == ("model-a", "test-box", "self-reported")
    assert by_id[a]["claim"]["by"] == "App B"
    assert by_id[b]["status"] == "done" and by_id[b]["creator"]["app"] == "App B"
    assert by_id[d]["status"] == "deleted" and by_id[d]["title"] == "Gone todo"
    assert by_id[1]["creator"]["who"] == "not recorded"     # the seeded one, before tracking
    assert res["counts"] == {"open": 2, "done": 1, "deleted": 1}
    assert [t["id"] for t in H.todos(db, 1, "done")["todos"]] == [b]
    assert [t["id"] for t in H.todos(db, 1, "deleted")["todos"]] == [d]
    assert {t["id"] for t in H.todos(db, 1, "open")["todos"]} == {1, a}
    html = c.get("/?view=todos&project=1").text
    assert "Open todo" in html and "Gone todo" in html and "model model-a" in html
    assert "on test-box" in html and "sign-in: self-reported" in html
    assert f"view=record&amp;project=1&amp;type=todo&amp;id={a}" in html
    assert "<b>Done</b>" in c.get("/?view=todos&project=1&status=done").text


def test_a_todo_record_page_shows_before_and_after(S, seeded_db, page):
    c, _, db = page
    tid = _todo(S, "Old wording")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                             "title": "New wording"}, app="App B")
    d = H.record_detail(db, 1, "todo", tid)
    assert d["state"] == "live" and d["fields"]["title"] == "New wording"
    edit = d["edits"][-1]
    assert edit["changes"]["title"] == {"old": "Old wording", "new": "New wording"}
    assert edit["app"] == "App B" and edit["machine"] == "test-box"
    html = c.get(f"/?view=record&project=1&type=todo&id={tid}").text
    assert "class='old'>Old wording" in html and "class='new'>New wording" in html
    assert "Edit history (2)" in html and "Everything about it" in html


# ---------------------------------------------------------------- the rest of the path
def test_decisions_replaced_reviews_and_conflicts_are_in_the_path(S, seeded_db, page):
    c, csrf, db = page
    old = _decision(S, "Store files on the local disk")
    new = _decision(S, "Store files in the object store", replaces=old, app="App B")
    rows = _all(db)["rows"]
    rep = [r for r in rows if r["what"] == f"decision #{new} replaces decision #{old}"]
    assert rep and rep[0]["other"]["label"] == "Store files on the local disk"
    assert rep[0]["app"] == "App B" and rep[0]["type"] == "link"
    a = _decision(S, "Deploy the web app every Monday morning")
    b = _decision(S, "Deploy the web app every Friday evening", app="App B",
                  conflicts_with=a, topic="deploy day")
    flagged = _all(db, rtype="conflict")["rows"]
    assert any("disagreement flagged" in r["what"] or "possible conflict" in r["what"]
               for r in flagged)
    # a suggested todo: queued, then approved on the review page
    r = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Suggested chore",
                             "suggested": True})
    item = r.get("review_item_id") or r.get("item_id") or \
        _q(db, "SELECT MAX(id) FROM review_items WHERE kind = 'todo'")[0][0]
    assert c.post("/approve", data={"item_id": item, "csrf": csrf, "summary": "Suggested chore",
                                    "details": ""}).status_code == 303
    rev = _all(db, rtype="review")["rows"]
    assert any(r["event"] == "queued" and r["app"] == "App A" for r in rev)
    done = [r for r in rev if r["event"] == "decided"]
    assert done and done[0]["app"] == "review-page" and "approved review item" in done[0]["what"]
    tid = done[0]["other"]["record_id"]
    assert done[0]["other"]["label"] == "Suggested chore"
    assert any(r["event"] == "decided" for r in _all(db, record=("todo", tid))["rows"])
    assert b


def test_sessions_are_listed_with_their_app(S, seeded_db):
    _todo(S, "Session maker", model="model-s")
    s = _all(seeded_db, rtype="session")["rows"]
    assert s and s[0]["event"] == "session_opened" and s[0]["app"] == "App A"
    assert s[0]["model"] == "model-s"


# ---------------------------------------------------------------- the pages, and who can reach them
def test_pages_render_read_only_and_phone_ready(S, seeded_db, page):
    c, _, db = page
    _todo(S, "Rendered todo")
    for url in ("/?view=history", "/?view=history&project=1", "/?view=todos&project=1",
                "/?view=record&project=1&type=todo&id=1", "/?view=history&project=1&type=todo"
                "&app=App&since=2020-01-01&until=2099-01-01&q=todo&order=newest"):
        r = c.get(url)
        assert r.status_code == 200, url
        assert "<script" not in r.text and "width=device-width" in r.text
        assert "Project history" in r.text
        assert r.headers["cache-control"] == "no-store"
        assert "default-src 'none'" in r.headers["content-security-policy"]
        # nothing on it changes anything
        assert "method='post'" not in r.text.split("<h2>Project history</h2>")[1].lower().replace('"', "'")
    assert "Pick a project" in c.get("/?view=history").text
    assert "Rendered todo" in c.get("/?view=history&project=1").text
    assert "not 'nope'" in c.get("/?view=history&project=1&since=nope").text.replace("&#x27;", "'")
    assert "Project history" in c.get("/").text and "view=history" in c.get("/?view=who").text


def test_values_are_escaped(S, seeded_db, page):
    c, _, _ = page
    _todo(S, "<img src=x onerror=alert(1)> todo")
    html = c.get("/?view=history&project=1").text + c.get("/?view=todos&project=1").text
    assert "<img src=x" not in html and "&lt;img src=x" in html


def test_no_session_no_page(page, seeded_db):
    c, _, _ = page
    fresh = c.__class__(c.app, base_url=str(c.base_url), follow_redirects=False)
    for url in ("/?view=history&project=1", "/?view=todos&project=1",
                "/?view=record&project=1&type=todo&id=1"):
        r = fresh.get(url)
        assert r.status_code == 403 and "Alpha todo" not in r.text
        r = c.get(url, headers={"host": "evil.example:8770"})   # DNS rebinding
        assert r.status_code == 403


def test_open_link_page_needs_its_own_session_cookie(S, seeded_db):
    from starlette.testclient import TestClient
    from flowstate.review_page import HOST, Page, create_app
    p = Page(seeded_db, 8770, open_link=True)
    c = TestClient(create_app(p), base_url=f"http://{HOST}:8770", follow_redirects=False)
    r = c.get("/?view=history&project=1")
    assert r.status_code == 200 and "Project history" in r.text and "fs_review_link" in r.headers["set-cookie"]
    assert c.get("/?view=history&project=1", headers={"host": "evil.example"}).status_code == 403


def test_not_on_the_tunnel_page(S, seeded_db, tmp_path):
    import test_review_tunnel as T
    from starlette.testclient import TestClient
    from flowstate.remote_auth import AuthStore
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase(T.PASS)
    with TestClient(T._app(seeded_db, store.path), base_url=T.URL, follow_redirects=False) as c:
        anon = c.get("/review/tunnel/?view=history&project=1")
        assert "Project history" not in anon.text and "Alpha todo" not in anon.text
        T.sign_in(c)
        for url in ("/review/tunnel/?view=history&project=1", "/review/tunnel/?view=todos&project=1",
                    "/review/tunnel/?view=record&project=1&type=todo&id=1"):
            r = c.get(url)
            assert "Project history" not in r.text and "Everything about it" not in r.text, url
        assert "view=history" not in c.get("/review/tunnel/").text
    from flowstate.review_page import Page
    from flowstate.history_page import NOT_HERE
    off = Page(seeded_db, 8770, base="/review/tunnel")
    assert off.settings == "off" and NOT_HERE in off.render_history("x", "", {"project": "1"})


def test_no_mcp_tool_no_http_or_relay_route(S, seeded_db, monkeypatch):
    # no tool in any profile names or reaches it
    for profile in ("core", "default", "full"):
        monkeypatch.setenv("FLOWSTATE_TOOLS", profile)

        async def go():
            async with create_connected_server_and_client_session(S.server) as c:
                return (await c.list_tools()).tools
        for t in asyncio.run(go()):
            blob = (t.name + json.dumps(t.inputSchema) + (t.description or "")).lower()
            assert "project history" not in blob and "project_history" not in blob
    # only the review page imports it (never server.py, http_app.py, the relay or the tunnel)
    pkg = Path(H.__file__).parent
    users = {f.name for f in pkg.rglob("*.py")
             if re.search(r"\b(project_history|history_page)\b", f.read_text(encoding="utf-8"))
             and f.name not in ("project_history.py", "history_page.py")}
    assert users == {"review_page.py"}
    # the HTTP listener (LAN / tunnel / relay requests come in here) has no such page
    from starlette.testclient import TestClient
    from flowstate.http_app import create_http_app
    tok = "t" * 32
    with TestClient(create_http_app(tok, db_path=seeded_db), base_url="http://127.0.0.1:8766") as c:
        for url in ("/?view=history&project=1", "/history", "/review/?view=history"):
            r = c.get(url, headers={"Authorization": f"Bearer {tok}"})
            assert r.status_code in (401, 404, 405) and "Project history" not in r.text, url


def test_the_page_never_writes(S, seeded_db, page):
    c, _, db = page
    _todo(S, "Read-only check")
    before = Path(db).read_bytes()
    conn = H._connect(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM todos")
    finally:
        conn.close()
    for url in ("/?view=history&project=1", "/?view=todos&project=1",
                "/?view=record&project=1&type=todo&id=1", "/?view=history&project=1&q=todo"):
        c.get(url)
    assert _q(db, "SELECT COUNT(*) FROM todos")[0][0] >= 2
    assert Path(db).read_bytes() == before
