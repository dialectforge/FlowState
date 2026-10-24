"""Task 42: a session summary shows what the session really saved, on every page that shows
it: each record's kind, id, title and an excerpt, the solved problem with its solution, the
component, the app and the session's times. New summaries store the record ids; older ones
fall back to the run ledger, else say that only the counts are known. Temp DBs only."""

import asyncio
import json
import re
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from flowstate import review, review_cards, review_phone, session_contents, waiting
from flowstate.approve_page import view as approve_view
from flowstate.autosession import AUTO_TAG, AutoSessions

CHANGE = "Login redirect now keeps the return path"
CHANGE_BODY = ("The callback dropped ?next= when the session cookie was refreshed; it is now "
               "carried through the state parameter. " + "x" * 300)
PROBLEM = "Users loop back to the sign-in page"
SOLUTION = "Carry the return path in the OAuth state instead of the cookie"


def call(S, name, args, app="claude-code"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    out = asyncio.run(go()).content[0].text
    try:
        return json.loads(out)
    except ValueError:
        return out


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _work(S, db, changes=1):
    """A claude-code session in Alpha: `changes` changes, a problem logged and solved. Returns
    (review item id, problem id, first change id)."""
    first = None
    for i in range(changes):
        r = call(S, "log_work", {"project": "Alpha", "kind": "change", "component": "Auth",
                                 "summary": CHANGE if i == 0 else f"Change number {i}",
                                 "details": CHANGE_BODY if i == 0 else f"body {i}"})
        first = first or r["record"]["id"]
    p = call(S, "log_work", {"project": "Alpha", "kind": "problem", "summary": PROBLEM,
                             "details": "After a refresh the app forgets where you were."})
    pid = p["record"]["id"]
    call(S, "manage_problems", {"action": "solve", "project": "Alpha", "problem_id": pid,
                                "summary": SOLUTION})
    S.backend.tracker.close_instance()
    [(item,)] = _q(db, "SELECT id FROM review_items WHERE kind = 'session_summary' ORDER BY id DESC LIMIT 1")
    return item, pid, first


def _old_item(db, sid=None):
    """A summary queued before 2.4.4: counts only, no ids, no ledger rows."""
    return review.add_item(db, 1, "session_summary",
                           f"{AUTO_TAG} claude-code session in Alpha: saved 1 change; also problem solved.",
                           payload={"close_reason": "idle", "calls": 3}, source="claude-code",
                           session_id=sid)


# ---------------------------------------------------------------- the writer
def test_new_summary_stores_ids_and_names_the_records(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    it = review.get_item(db, item)
    recs = json.loads(it["payload"])["records"]
    assert ["change", cid, "saved"] in recs and ["problem", pid, "saved, solved"] in recs
    assert not any(r[0] in ("solution", "link", "component") for r in recs)   # ids only, no noise
    assert CHANGE_BODY[:40] not in it["payload"]                                  # never the text
    # the summary line (what every list and notification shows) names them
    assert "saved 1 change and 1 problem; also problem solved." in it["summary"]
    assert f'Records: change #{cid} "{CHANGE}"; problem #{pid} "{PROBLEM}" (saved, solved).' in it["summary"]


def test_contents_change_and_solved_problem(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    c = session_contents.for_item(db, review.get_item(db, item))
    assert c["source"] == "stored" and c["total"] == 2 and c["app"] == "claude-code"
    assert c["project"] == "Alpha" and c["started_utc"] and c["ended_utc"]
    ch, pr = c["entries"]
    assert (ch["kind"], ch["id"], ch["title"], ch["component"]) == ("change", cid, CHANGE, "Auth")
    assert ch["excerpt"].startswith(CHANGE_BODY[:100]) and len(ch["excerpt"]) <= session_contents.EXCERPT
    assert (pr["kind"], pr["id"], pr["title"]) == ("problem", pid, PROBLEM)
    assert pr["solution"] == SOLUTION and "solved" in pr["verb"]
    text = "\n".join(session_contents.lines(c))
    assert f'change #{cid} "{CHANGE}" (Auth): The callback dropped' in text
    assert f'problem #{pid} "{PROBLEM}"' in text and f"Solution: {SOLUTION}" in text


def test_old_summary_falls_back_to_the_ledger_then_says_counts_only(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    sid = review.get_item(db, item)["session_id"]
    old = _old_item(db, sid)          # no stored ids, but the session's ledger rows exist
    c = session_contents.for_item(db, review.get_item(db, old))
    assert c["source"] == "ledger" and [e["id"] for e in c["entries"]] == [cid, pid]
    bare = _old_item(db)              # nothing to go on
    c = session_contents.for_item(db, review.get_item(db, bare))
    assert c["source"] == "none" and c["entries"] == []
    assert session_contents.lines(c) == [session_contents.NONE_NOTE]
    assert "only the counts above are known" in session_contents.html(c)


def test_deleted_record_and_more_than_ten(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db, changes=12)
    c = session_contents.for_item(db, review.get_item(db, item))
    assert c["total"] == 13
    text = session_contents.lines(c)
    assert sum(1 for t in text if t.startswith("- ")) == 10 and text[-1].startswith("... and 3 more")
    page = session_contents.html(c)
    assert page.count("<li>") == 13 and "<details><summary>3 more</summary>" in page
    pub = session_contents.public(c)
    assert len(pub["entries"]) == 10 and pub["more"] == 3 and pub["total"] == 13
    assert "; and 8 more." in review.get_item(db, item)["summary"]     # 5 named, 8 more
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DELETE FROM changes WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    c = session_contents.for_item(db, review.get_item(db, item))
    assert c["entries"][0]["gone"] and "no longer in FlowState" in session_contents.lines(c)[1]


def test_text_is_escaped(backend):
    S, db = backend, backend.backend.db_path
    call(S, "log_work", {"project": "Alpha", "kind": "change", "summary": "<script>alert(1)</script>",
                         "details": "a & b <img src=x onerror=alert(1)>"})
    S.backend.tracker.close_instance()
    [(item,)] = _q(db, "SELECT id FROM review_items WHERE kind = 'session_summary'")
    page = session_contents.html(session_contents.for_item(db, review.get_item(db, item)))
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "a &amp; b &lt;img" in page


# ---------------------------------------------------------------- the renderers
def test_local_page_shows_contents_in_local_time(backend):
    from flowstate.review_page import Page, create_app
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db, changes=12)
    p = Page(db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    assert f"change #{cid} &quot;{CHANGE}&quot; (Auth)" in html
    assert f"problem #{pid} &quot;{PROBLEM}&quot;" in html and f"Solution: {SOLUTION}" in html
    assert "<details><summary>3 more</summary>" in html          # expandable past ten
    # the session's machine (session_runs) now comes with its summary
    assert re.search(r"by claude-code( on [^,<]+)?, in Alpha\.", html) and "this computer&#x27;s local time" in html
    old = _old_item(db)
    html = c.get("/").text
    assert html.count("only the counts above are known") == 1 and f"#{old}" in html


def test_tunnel_page_shows_contents_in_utc(backend, tmp_path):
    from test_review_tunnel import URL, _app, page_csrf, sign_in
    from flowstate.remote_auth import AuthStore
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase("correct horse battery staple")
    with TestClient(_app(db, store.path), base_url=URL, follow_redirects=False) as c:
        sign_in(c)
        _csrf, r = page_csrf(c)
    assert f"change #{cid} &quot;{CHANGE}&quot; (Auth)" in r.text and f"Solution: {SOLUTION}" in r.text
    assert re.search(r"Session \d{4}-\d\d-\d\d \d\d:\d\d[^<]* UTC, by claude-code", r.text)


def test_phone_list_and_approve_page_carry_contents(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db, changes=12)
    [v] = [x for x in review_phone.items(db) if x["id"] == item]
    con = v["contents"]
    assert con["total"] == 13 and con["more"] == 3 and len(con["entries"]) == 10
    assert con["entries"][0]["head"] == f'change #{cid} "{CHANGE}" (Auth)'
    prob = [e for e in con["entries"] if e["kind"] == "problem"]
    assert not prob   # the problem is the 13th: past the first ten
    assert v["queued_at_utc"] and "contents" in review_phone.JS and "localTime" in review_phone.JS
    # the JS writes it all with textContent, never innerHTML
    assert "innerHTML" not in review_phone.JS
    item2, pid2, _ = _work(S, db)
    a = approve_view(db, item2)
    [pe] = [e for e in a["contents"]["entries"] if e["kind"] == "problem"]
    assert pe["title"] == PROBLEM and pe["solution"] == SOLUTION
    old = _old_item(db)
    [o] = [x for x in review_phone.items(db) if x["id"] == old]
    assert o["contents"]["entries"] == [] and o["contents"]["note"] == session_contents.NONE_NOTE


def test_phone_page_privacy_unchanged(backend):
    """Only record text from this database: no paths, no tokens, no conn keys in the JSON."""
    S, db = backend, backend.backend.db_path
    item, _pid, _cid = _work(S, db)
    [v] = [x for x in review_phone.items(db) if x["id"] == item]
    blob = json.dumps(v)
    assert str(db) not in blob and "conn_key" not in blob and "change_id" not in blob


def test_chat_card_and_text_fallback(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    it = review.get_item(db, item)
    v = review_cards.view(db, it, "Alpha")
    assert v["contents"]["total"] == 2
    text = review_cards.plain(v)
    assert f'change #{cid} "{CHANGE}" (Auth): The callback dropped' in text
    assert f'problem #{pid} "{PROBLEM}"' in text and f"Solution: {SOLUTION}" in text
    assert "UTC, by claude-code" in text
    from flowstate.review_card_ui import CARD_HTML
    assert "contents" in CARD_HTML and "localTime" in CARD_HTML
    old = review.get_item(db, _old_item(db))
    assert session_contents.NONE_NOTE in review_cards.plain(review_cards.view(db, old, "Alpha"))


def test_chat_card_truncates_past_ten(backend):
    S, db = backend, backend.backend.db_path
    item, _pid, _cid = _work(S, db, changes=12)
    v = review_cards.view(db, review.get_item(db, item), "Alpha")
    assert len(v["contents"]["entries"]) == 10 and v["contents"]["more"] == 3
    assert "... and 3 more" in review_cards.plain(v)


def test_walk_box_lists_the_records(backend):
    S, db = backend, backend.backend.db_path
    item, pid, cid = _work(S, db)
    msg, _schema = waiting._form({**review.get_item(db, item), "project": "Alpha"}, 1, 1)
    assert f'change #{cid} "{CHANGE}" (Auth)' in msg and f"Solution: {SOLUTION}" in msg
