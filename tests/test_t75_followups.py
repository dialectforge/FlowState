"""Task 75: follow-ups to the project history page (task 73) and the clean-up exact list (task 74).

a. The page the brief's link opens (waiting.py's open-link page) reaches Project history the
   way it reaches "Who did what"; still never the tunnel page.
b. The timeline is newest first unless the user picks oldest first.
c. The Todos tab leaves erased todos out of its list and counts (the timeline keeps [erased]).
d. A record page whose address names another project than the record's own sends the user
   to the record's own project (303); a record with no project left is not shown.
e. manage_review list / walk lines say when a clean-up's list is out of date, and
   `flowstate erase` removes the stored list (`bound`: ids, digest, per-row hashes) from
   clean-up items about an erased record.
Temp databases and the in-process server only (conftest.py)."""

import json
import re

import pytest

from flowstate import erase, project_history as H

from test_t73_project_history import S, _decision, _q, _todo, call, page  # noqa: F401
from test_cleanup import _j, messy  # noqa: F401
from test_cleanup import call as ccall
from test_t74_cleanup_bound import _approve, _dup, _link, _move, _payload, _stale, _state, _x

OUT_OF_DATE = "(out of date: approving changes nothing)"


# ---------------------------------------------------------------- a. the brief's page
@pytest.fixture
def link_page(S, seeded_db, monkeypatch):
    """The page waiting.py starts for the brief's link, exactly as it makes it (uvicorn not run)."""
    import uvicorn
    from starlette.testclient import TestClient
    from flowstate import waiting
    from flowstate.review_page import HOST, create_app

    class Quiet:
        def __init__(self, config):
            self.config, self.should_exit = config, False

        def run(self, sockets=None):
            return None
    monkeypatch.setattr(uvicorn, "Server", Quiet)
    monkeypatch.setenv("FLOWSTATE_REVIEW_PORT", "0")
    saved = dict(waiting._own)
    try:
        port = waiting._start(lambda: seeded_db)
        p = waiting._own["page"]
        p._db = seeded_db
        waiting._own["sock"].close()
        c = TestClient(create_app(p), base_url=f"http://{HOST}:{port}", follow_redirects=False)
        yield c, p
    finally:
        waiting._own.clear()
        waiting._own.update(saved)


def test_the_brief_link_page_links_to_project_history_like_who_did_what(link_page, S):
    c, p = link_page
    assert p.open_link and p.settings == "read"
    _todo(S, "Seen from the brief page")
    queue = c.get("/")
    assert queue.status_code == 200
    head = queue.text.split("<h2>")[0]   # the header, above the queue
    assert "?view=who'>Who did what</a>" in head and "?view=history'>Project history</a>" in head
    assert "?view=history'>Project history</a>" in c.get("/?view=who").text
    hist = c.get("/?view=history&project=1")
    assert hist.status_code == 200 and "Seen from the brief page" in hist.text
    assert "Project history</a> (this page)" in hist.text
    assert c.get("/?view=todos&project=1").status_code == 200
    # owner-only: another Host, or no session cookie, gets nothing
    assert c.get("/?view=history&project=1", headers={"host": "evil.example"}).status_code == 403


def test_the_queue_filtered_by_project_links_to_that_projects_history(link_page):
    c, _ = link_page
    html = c.get("/?project=2").text
    assert "?view=history&amp;project=2'>Project history</a>" in html
    assert "?view=history'>Project history</a>" in c.get("/").text


# ---------------------------------------------------------------- b. newest first
def test_the_timeline_is_newest_first_by_default(S, page):
    c, _, db = page
    first = _decision(S, "Earliest decision here")
    _x(db, "UPDATE record_history SET created_at = '2026-01-01 08:00:00' WHERE table_name = "
           "'conversations' AND record_id = ?", first)
    _decision(S, "Latest decision here")
    html = c.get("/?view=history&project=1").text
    assert html.index("Latest decision here") < html.index("Earliest decision here")
    assert '<option value="newest" selected>Newest first</option>' in html.replace("' selected", " selected")
    old = c.get("/?view=history&project=1&order=oldest").text
    assert old.index("Earliest decision here") < old.index("Latest decision here")
    assert '<option value="oldest" selected>' in old


def test_newest_first_pages_and_oldest_pages_keep_their_order(S, page):
    c, _, db = page
    for i in range(55):
        _todo(S, f"Paged todo {i:02d}")
    one = c.get("/?view=history&project=1").text
    assert "Paged todo 54" in one and "Paged todo 00" not in one
    nxt = re.search(r"href='([^']*page=2[^']*)'>Next", one).group(1).replace("&amp;", "&")
    assert "order" not in nxt   # the default needs no parameter
    assert "Paged todo 00" in c.get(nxt).text
    old = c.get("/?view=history&project=1&order=oldest").text
    nxt = re.search(r"href='([^']*page=2[^']*)'>Next", old).group(1).replace("&amp;", "&")
    assert "order=oldest" in nxt and "Paged todo 54" in c.get(nxt).text


# ---------------------------------------------------------------- c. erased todos
def test_the_todos_tab_leaves_erased_todos_out(S, page):
    c, _, db = page
    gone = _todo(S, "Erase me fully please")
    kept = _todo(S, "Deleted but kept")
    for t in (gone, kept):
        call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                  "record_id": t})
    S.backend.tracker.close_instance()
    erase.run(db, erase.plan(db, [f"todo:{gone}"]))
    S.init_backend(db)
    res = H.todos(db, 1)
    ids = [t["id"] for t in res["todos"]]
    assert gone not in ids and kept in ids
    assert res["counts"] == {"open": 1, "deleted": 1} and res["total"] == 2
    assert gone not in [t["id"] for t in H.todos(db, 1, "deleted")["todos"]]
    assert H.todos(db, 1, "deleted")["total"] == 1
    html = c.get("/?view=todos&project=1").text
    assert "[erased]" not in html and f"todo #{gone}<" not in html and "All</b> (2)" in html
    assert "Deleted</a> (1)" in html
    # the timeline keeps its marker
    tl = H.timeline(db, 1, per_page=100)["rows"]
    assert any(r["record_type"] == "todo" and r["record_id"] == gone and r["label"] == "[erased]"
               for r in tl)


# ---------------------------------------------------------------- d. record under another project
def test_a_record_page_under_another_project_goes_to_its_own(page):
    c, _, db = page
    r = c.get("/?view=record&project=1&type=todo&id=2")   # Beta todo, in project 2
    assert r.status_code == 303
    assert r.headers["location"].endswith("/?view=record&project=2&type=todo&id=2")
    assert "Beta todo" not in r.text
    ok = c.get("/?view=record&project=2&type=todo&id=2")
    assert ok.status_code == 200 and "Beta todo" in ok.text


def test_the_page_itself_never_shows_a_record_under_another_project(page):
    _, _, db = page
    from flowstate.review_page import Page
    html = Page(db, 8770).render_history("x", "", {"view": "record", "project": "1", "type": "todo",
                                                    "id": "2"})
    assert "Beta todo" not in html and "belongs to another project" in html


def test_a_deleted_record_under_another_project_goes_to_its_own(S, page):
    c, _, db = page
    call(S, "manage_memory", {"action": "delete", "project": "Beta Project", "record_type": "todo",
                              "record_id": 2})
    r = c.get("/?view=record&project=1&type=todo&id=2")
    assert r.status_code == 303 and "project=2" in r.headers["location"]
    assert "Beta todo" in c.get(r.headers["location"]).text


def test_a_record_in_its_own_project_and_unknown_ids_are_as_before(S, page):
    c, _, db = page
    assert c.get("/?view=record&project=1&type=todo&id=1").status_code == 200
    r = c.get("/?view=record&project=1&type=decision&id=4242")
    assert r.status_code == 200 and "Nothing in this project's history names this record" in r.text
    assert c.get("/?view=record&project=1&type=nonsense&id=2").status_code == 200


def test_an_erased_record_page_is_as_before(S, page):
    c, _, db = page
    from test_t73_project_history import _erased_decision
    did = _erased_decision(S, db)
    r = c.get(f"/?view=record&project=1&type=decision&id={did}")
    assert r.status_code == 200 and "erased for good" in r.text
    assert c.get(f"/?view=record&project=2&type=decision&id={did}").status_code == 200


def test_the_tunnel_page_still_has_no_record_page(S, seeded_db, tmp_path):
    import test_review_tunnel as T
    from starlette.testclient import TestClient
    from flowstate.remote_auth import AuthStore
    store = AuthStore(tmp_path / "auth.db")
    store.set_passphrase(T.PASS)
    with TestClient(T._app(seeded_db, store.path), base_url=T.URL, follow_redirects=False) as c:
        T.sign_in(c)
        r = c.get("/review/tunnel/?view=record&project=1&type=todo&id=2")
        assert r.status_code != 303 and "Everything about it" not in r.text


# ---------------------------------------------------------------- e1. out of date in the AI's lines
def _lines(S, action="list"):
    out = _j(ccall(S, "manage_review", {"action": action, "project": "FlowState"}, client="Grok"))
    return out["items"]


def _line(lines, iid):
    return [x for x in lines if x.startswith(f"#{iid} ")][0]


def test_list_and_walk_lines_say_when_a_clean_up_is_out_of_date(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    fresh = _stale(S, db, ids)
    for action in ("list", "walk"):
        assert OUT_OF_DATE not in _line(_lines(S, action), iid)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    for action in ("list", "walk"):
        got = _lines(S, action)
        assert _line(got, iid).endswith(OUT_OF_DATE), _line(got, iid)
        assert OUT_OF_DATE not in _line(got, fresh)
    # the line stays within its length budget
    assert len(json.dumps(_line(_lines(S), iid))) - 2 <= 160


def test_an_older_clean_up_without_a_list_is_out_of_date_too(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    p = _payload(db, iid)
    p.pop("bound")
    _x(db, "UPDATE review_items SET payload = ? WHERE id = ?", json.dumps(p), iid)
    assert _line(_lines(S), iid).endswith(OUT_OF_DATE)


def test_decided_clean_ups_get_no_out_of_date_note(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    _approve(S, iid)   # voided and closed
    out = _j(ccall(S, "manage_review", {"action": "list", "project": "FlowState", "status": "all"},
                   client="Grok"))
    assert OUT_OF_DATE not in _line(out["items"], iid)


# ---------------------------------------------------------------- e2. erase drops the stored list
def _everywhere(db) -> str:
    """Every table's every row, as text (what a digest or hash must not be left in)."""
    names = [r[0] for r in _q(db, "SELECT name FROM sqlite_master WHERE type = 'table' AND name "
                                  "NOT LIKE 'sqlite_%' AND name NOT LIKE '%fts%'")]
    return json.dumps({n: _q(db, f'SELECT * FROM "{n}"') for n in names}, default=str)


def _erase(S, db, spec):
    S.backend.tracker.close_instance()
    erase.run(db, erase.plan(db, [spec]))
    S.init_backend(db)


def test_erase_removes_the_stored_list_of_clean_ups_about_the_record(messy):
    S, db, ids = messy
    iid = _stale(S, db, ids)
    bound = _payload(db, iid)["bound"]
    out = _j(ccall(S, "manage_memory", {"action": "delete", "project": "FlowState",
                                        "record_type": "todo", "record_id": ids["kern"]},
                   client="Cursor"))
    assert out.get("deleted"), out
    _erase(S, db, f"todo:{ids['kern']}")
    p = _payload(db, iid)
    assert "bound" not in p and p.get("bound_erased") is True
    every = _everywhere(db)
    assert bound["digest"] not in every
    for h in bound["each"].values():
        assert h not in every
    # the item still can't change anything, and says why
    got = _approve(S, iid)
    assert got.get("error") == "changed_since_request" and "erased" in got["message"], got
    assert _state(db, iid)[0] == "rejected"


def test_erase_also_drops_it_from_a_closed_clean_up_and_its_history(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    bound = _payload(db, iid)["bound"]
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", new)
    _approve(S, iid)   # voided: closed, but its list was still stored
    assert _payload(db, iid)["bound"]["digest"] == bound["digest"]
    # a copy of the item in the edit history, as an approve-with-edits leaves one
    _x(db, "INSERT INTO record_history (edit_id, table_name, record_id, project_id, old_row, "
           "new_row, tool, client, created_at) VALUES ('e-t75', 'review_items', ?, ?, ?, NULL, "
           "'queue/review_edit', 'test', '2026-10-28 10:00:00')",
       iid, ids["fs"], json.dumps({"id": iid, "kind": "cleanup", "payload": json.dumps(_payload(db, iid))}))
    ccall(S, "manage_memory", {"action": "delete", "project": "FlowState", "record_type": "todo",
                               "record_id": new}, client="Cursor")
    _erase(S, db, f"todo:{new}")
    assert "bound" not in _payload(db, iid)
    every = _everywhere(db)
    assert bound["digest"] not in every
    assert all(h not in every for h in bound["each"].values())


def test_erase_drops_the_list_when_only_a_link_on_it_is_erased(messy):
    S, db, ids = messy
    _link(db, ids, ids["kern"])
    lid = _q(db, "SELECT id FROM record_links WHERE from_id = ?", ids["misfiled"])[0][0]
    iid = _move(S, db, ids)
    bound = _payload(db, iid)["bound"]
    assert "record_links" in bound["rows"]
    out = _j(ccall(S, "manage_memory", {"action": "delete", "project": "FlowState",
                                        "record_type": "link", "record_id": lid}, client="Cursor"))
    assert out.get("deleted"), out
    _erase(S, db, f"link:{lid}")
    assert "bound" not in _payload(db, iid)
    assert bound["digest"] not in _everywhere(db)


def test_erase_leaves_other_clean_ups_their_list(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    other = _stale(S, db, ids)
    keep = _payload(db, other)["bound"]
    ccall(S, "manage_memory", {"action": "delete", "project": "FlowState", "record_type": "todo",
                               "record_id": new}, client="Cursor")
    _erase(S, db, f"todo:{new}")
    assert _payload(db, other)["bound"] == keep
    assert "bound" not in _payload(db, iid)


def test_a_waiting_clean_up_about_an_erased_record_says_so(messy):
    S, db, ids = messy
    iid, old, new = _dup(S, db)
    ccall(S, "manage_memory", {"action": "delete", "project": "FlowState", "record_type": "todo",
                               "record_id": new}, client="Cursor")
    _erase(S, db, f"todo:{new}")
    assert _state(db, iid)[0] == "pending"
    assert _line(_lines(S), iid).endswith(OUT_OF_DATE)
    from flowstate import cleanup
    got = cleanup.preview(db, {"payload": json.dumps(_payload(db, iid))})
    assert got["changed_since_request"] and "erased" in got["text"], got
    # running the clean-up again neither fails nor brings the erased todo back
    out = _j(ccall(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["duplicate"]}))
    assert "error" not in out, out
    others = [json.loads(r[0]) for r in _q(db, "SELECT payload FROM review_items WHERE kind = "
                                            "'cleanup' AND id != ?", iid)]
    assert not any(["todo", new] in (o.get("records") or []) for o in others)


def test_a_link_page_stays_under_either_of_its_projects(S, page):
    c, _, db = page
    _x(db, "INSERT INTO record_links (from_type, from_id, from_project_id, to_type, to_id, "
           "to_project_id, link_type, created_at) VALUES ('todo', 1, 1, 'todo', 2, 2, 'relates_to', "
           "'2026-10-28 10:00:00')")
    lid = _q(db, "SELECT MAX(id) FROM record_links")[0][0]
    for pid in (1, 2):
        assert c.get(f"/?view=record&project={pid}&type=link&id={lid}").status_code == 200


def test_the_walk_box_warns_about_an_out_of_date_clean_up(messy):
    S, db, ids = messy
    from flowstate import review, waiting
    iid, old, new = _dup(S, db)
    it = [x for x in review.list_items(db)["items"] if x["id"] == iid][0]
    assert "This list is not the one" not in waiting._form(it, 1, 1)[0]
    _x(db, "UPDATE todos SET title = title || ' (renamed)' WHERE id = ?", old)
    msg = waiting._form(it, 1, 1)[0]
    assert "This list is not the one the suggestion was made with" in msg
    assert f"todo #{old} was edited" in msg and "Approving it changes nothing" in msg
