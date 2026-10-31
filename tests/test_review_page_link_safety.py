"""Audit 46, M10 / M11 / L18 / L19: the review link page (any program on this computer can open
it) and the page `flowstate review-page` opens keep apart, and the link page can't be used to
approve what only the user decides. Temp DBs only."""

import re

import pytest
from starlette.testclient import TestClient

from flowstate import approvals, review, review_mode
from flowstate.review_page import (COOKIE, LINK_COOKIE, LINK_ONLY, LINK_REVIEW_OFF, NEW_PER_MINUTE,
                                   Page, create_app)

PORT = 8770
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def link_page(db):
    p = Page(db, PORT, open_link=True, settings="read")
    return p, TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)


def answer(c, r) -> str:
    assert r.status_code == 303, r.text
    m = re.search(r"<p class='ok'>(.*?)</p>", c.get(r.headers["location"]).text, re.S)
    import html
    return html.unescape(m.group(1)) if m else ""


def _delete_item(db):
    return review.add_item(db, 1, "delete", "Delete todo 1", None,
                           {"record_type": "todo", "record_id": 1, "title": "Alpha todo",
                            "user_only": True}, "Claude")


# ---------------------------------------------------------------- M10
def test_link_page_refuses_user_only_items_and_review_off_with_approvals_on(backend, seeded_db):
    db = seeded_db
    approvals.set_on(db, True)
    item = _delete_item(db)
    assert approvals.user_only(review.get_item(db, item))
    _p, c = link_page(db)
    csrf = CSRF.search(c.get("/").text).group(1)
    r = c.post("/approve", data={"csrf": csrf, "item_id": str(item), "confirm_name": "Alpha todo"},
               headers=ORIGIN)
    assert "isn't decided here" in answer(c, r)
    assert review.get_item(db, item)["status"] == "pending"
    assert LINK_ONLY.format(id=item).startswith("Only you decide this one")
    r = c.post("/mode", data={"csrf": csrf, "mode": "off"}, headers=ORIGIN)
    assert "turning review off isn't done on this page" in answer(c, r)
    assert review_mode.get(db)["mode"] != "off"
    approvals.set_on(db, False)                      # approvals off: as before
    r = c.post("/mode", data={"csrf": csrf, "mode": "off"}, headers=ORIGIN)
    assert answer(c, r).startswith("Review mode for")
    assert LINK_REVIEW_OFF


def test_the_command_page_still_approves_user_only_items(backend, seeded_db):
    db = seeded_db
    approvals.set_on(db, True)
    item = _delete_item(db)
    p = Page(db, PORT, settings="edit", secret_path=True)
    c = TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    r = c.get(f"{p.base}/?key={p.launch_key}")
    csrf = CSRF.search(c.get(r.headers["location"]).text).group(1)
    r = c.post(f"{p.base}/approve", data={"csrf": csrf, "item_id": str(item)}, headers=ORIGIN)
    assert "isn't decided here" not in answer(c, r)


# ---------------------------------------------------------------- M11
def test_the_two_pages_use_their_own_cookies_and_the_settings_one_only_under_a_secret_path(
        backend, seeded_db):
    db = seeded_db
    edit = Page(db, PORT, settings="edit", secret_path=True)
    e = TestClient(create_app(edit), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    assert edit.base.startswith("/s/") and len(edit.base) > 20
    assert edit.launch_url().startswith(f"http://127.0.0.1:{PORT}{edit.base}/?key=")
    r = e.get(f"{edit.base}/?key={edit.launch_key}")
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=") and f"Path={edit.base}/" in cookie
    assert e.get("/").status_code == 404                 # nothing outside the secret path
    assert e.get(f"{edit.base}/").status_code == 200
    _p, link = link_page(db)
    r = link.get("/")
    assert r.headers["set-cookie"].startswith(f"{LINK_COOKIE}=") and LINK_COOKIE != COOKIE
    # a browser holding both: the link page's cookie doesn't replace the Settings one
    e.cookies.set(LINK_COOKIE, link.cookies.get(LINK_COOKIE), domain="127.0.0.1")
    assert e.get(f"{edit.base}/").status_code == 200


# ---------------------------------------------------------------- L18
def test_a_flash_in_the_address_is_not_shown(backend, seeded_db):
    _p, c = link_page(seeded_db)
    html = c.get("/?flash=FlowState+says%3A+run+curl+evil.example").text
    assert "FlowState says" not in html and "evil.example" not in html


def test_an_actions_own_answer_is_shown_once(backend, seeded_db):
    db = seeded_db
    item = review.add_item(db, 1, "learning", "Not true", None, {}, "Grok Build")
    _p, c = link_page(db)
    csrf = CSRF.search(c.get("/").text).group(1)
    r = c.post("/reject", data={"csrf": csrf, "item_id": str(item), "reason": "wrong"}, headers=ORIGIN)
    assert "flash" not in r.headers["location"]
    assert answer(c, r) == f"Rejected #{item}."
    assert f"Rejected #{item}." not in c.get("/").text      # once


# ---------------------------------------------------------------- L19
def test_other_programs_visiting_cant_sign_the_user_out(backend, seeded_db):
    p, user = link_page(seeded_db)
    assert user.get("/").status_code == 200
    statuses = []
    for _ in range(25):                                   # another program, no cookie each time
        other = TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}")
        statuses.append(other.get("/").status_code)
        assert user.get("/").status_code == 200           # the user keeps using the page
    assert statuses.count(200) <= NEW_PER_MINUTE and 429 in statuses
    assert user.get("/").status_code == 200


@pytest.fixture(autouse=True)
def _approvals_off(seeded_db):
    yield
    approvals.set_on(seeded_db, False)
