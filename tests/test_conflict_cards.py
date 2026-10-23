"""Disagreement cards: flagging, evidence, the review item, resolution, chat approvals,
and the in-chat card (MCP Apps). Temp databases only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, conflict_cards, popup, review, waiting
from flowstate.conflict_card_ui import CARD_HTML, MIME, UI_URI
from flowstate.review_page import HOST, Page, create_app
from fsa_test_keys import make_key
from starlette.testclient import TestClient

ASK = "User: keep the older entry, the JWT one"


def call(S, name, args, app="Cursor"):
    if name == "manage_review" and args.get("action") in ("approve", "reject") and app == "Cursor":
        app = "Claude Code"
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    text = r.content[0].text
    try:
        body = json.loads(text)
    except ValueError:
        body = text
    return r, body


def ok(res):
    r, body = res
    assert not r.isError, body
    return body


def rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def decide(S, summary, app="Cursor", **kw):
    args = {"project": "Alpha", "kind": "decision", "summary": summary, **kw}
    return ok(call(S, "log_work", args, app=app))


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    _key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    monkeypatch.setattr(popup, "listener", lambda: "relay")


def test_bad_evidence_is_refused_before_the_record_is_saved(backend, seeded_db):
    S = backend
    before = rows(seeded_db, "SELECT COUNT(*) FROM conversations")[0][0]
    r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                   "summary": "Use JWT tokens for API auth",
                                   "evidence": ["not a file"]})
    assert r.isError and body["error"] == "bad_evidence"
    r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                   "summary": "Use JWT tokens for API auth",
                                   "evidence": ["a.py:1", "b.py:2", "c.py:3"]})
    assert r.isError and body["error"] == "too_much_evidence"
    assert rows(seeded_db, "SELECT COUNT(*) FROM conversations")[0][0] == before


def test_flagging_a_new_decision_opens_a_card_with_evidence(backend, seeded_db):
    S = backend
    older = decide(S, "Use JWT tokens for API auth", app="Claude", component="Auth")
    newer = decide(S, "Use session cookies for API auth", app="Cursor", component="Auth",
                   conflicts_with=older["record"]["id"], topic="API sessions",
                   evidence=["src/auth.py:42 — sessions are signed cookies"])
    cards = newer["conflict_cards"]
    assert len(cards) == 1
    text = cards[0]["text"]
    assert text.startswith("Two AIs disagree about API sessions.")
    assert "Claim A" in text and "Use JWT tokens for API auth" in text
    assert "Claim B" in text and "Use session cookies for API auth" in text
    assert "Claude" in text and "Cursor" in text
    assert "src/auth.py:42" in text and "sessions are signed cookies" in text
    assert "Review: " in text
    assert "D" + str(older["record"]["id"]) + " vs " not in text.split("\n")[0]
    item = cards[0]["review_item_id"]
    kind, summary = rows(seeded_db, "SELECT kind, summary FROM review_items WHERE id = ?", item)[0]
    assert kind == "disagreement" and summary == "Two AIs disagree about API sessions."
    ev = rows(seeded_db, "SELECT label, excerpt FROM entry_evidence WHERE record_id = ?",
              newer["record"]["id"])
    assert ev == [("src/auth.py:42", "sessions are signed cookies")]
    assert newer.get("_ui") is None   # painted onto the tool result, not left in the JSON
    r, _body = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                    "summary": "ignored", "conflicts_with": 99999})
    assert r.isError


def test_tool_result_points_at_the_ui_resource(backend):
    S = backend
    older = decide(S, "Use JWT tokens for API auth", app="Claude")
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            return await c.call_tool("log_work", {
                "project": "Alpha", "kind": "decision",
                "summary": "Use session cookies for API auth",
                "conflicts_with": older["record"]["id"], "topic": "API sessions",
                "evidence": ["src/auth.py:42"]})
    r = asyncio.run(go())
    assert not r.isError
    assert r.structuredContent["headline"] == "Two AIs disagree about API sessions."
    assert r.structuredContent["sides"][1]["evidence"][0]["label"] == "src/auth.py:42"
    meta = r.meta if isinstance(r.meta, dict) else r.model_dump(by_alias=True).get("_meta")
    assert meta["ui"]["resourceUri"] == UI_URI


def test_flag_conflict_between_two_existing_entries(backend, seeded_db):
    S = backend
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    r, body = call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                       "item_id": a, "target_id": b})
    assert r.isError and body["error"] == "topic_required"
    flagged = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_type": "decision", "item_id": a, "target_type": "decision", "target_id": b,
        "evidence": ["src/auth.py:42"],
        "target_evidence": ["output: assert response.cookies['sid']"]}, app="Cursor"))
    text = flagged["conflict_cards"][0]["text"]
    assert "src/auth.py:42" in text and "assert response.cookies['sid']" in text
    again = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": b, "target_id": a}))
    assert again["conflict_cards"][0]["review_item_id"] == flagged["conflict_cards"][0]["review_item_id"]
    assert rows(seeded_db, "SELECT COUNT(*) FROM conflict_cards WHERE status = 'open'")[0][0] == 1


def test_keep_outcomes_supersede_or_leave_both(backend, seeded_db):
    S = backend
    db = seeded_db
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    item = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a, "target_id": b}))["flagged"]
    r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "user_request": ASK}, app="Claude Code")
    assert body["error"] == "keep_required"
    assert rows(db, "SELECT status FROM review_items WHERE id = ?", item)[0][0] == "pending"
    kept = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "keep": "a", "user_request": ASK}, app="Claude Code"))
    assert kept["approved"] == item
    assert rows(db, "SELECT status FROM conflict_cards WHERE review_item_id = ?", item)[0][0] == "kept_a"
    assert rows(db, "SELECT standing, by_id FROM entry_standing WHERE record_type = 'decision' "
                    "AND record_id = ?", b) == [("superseded", a)]
    assert rows(db, "SELECT COUNT(*) FROM entry_standing WHERE record_id = ?", a)[0][0] == 0
    link = rows(db, """SELECT from_id, to_id FROM record_links WHERE link_type = 'supersedes'
                       AND removed_at IS NULL""")
    assert (a, b) in link
    path = rows(db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item)
    assert ("chat", "kept_a") in path

    c = decide(S, "Charge in USD only", app="Claude")["record"]["id"]
    d = decide(S, "Offer a yearly plan", app="Cursor")["record"]["id"]
    both = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "pricing notes",
        "item_id": c, "target_id": d}))["flagged"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": both,
                                 "keep": "both", "user_request": "User: both of these can stand"},
            app="Claude Code"))
    assert rows(db, "SELECT status FROM conflict_cards WHERE review_item_id = ?", both)[0][0] == "kept_both"
    assert rows(db, "SELECT COUNT(*) FROM entry_standing WHERE record_id IN (?, ?)", c, d)[0][0] == 0

    e = decide(S, "Write the changelog before tagging", app="Claude")["record"]["id"]
    f = decide(S, "Tag releases as vX.Y.Z", app="Cursor")["record"]["id"]
    dismissed = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "release notes",
        "item_id": e, "target_id": f}))["flagged"]
    out = ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": dismissed,
                                       "reason": "both can stand",
                                       "user_request": "User: these two can both stand, dismiss the card"},
                  app="Claude Code"))
    assert out["kind"] == "disagreement"
    assert rows(db, "SELECT status FROM conflict_cards WHERE review_item_id = ?", dismissed)[0][0] == "dismissed"
    assert rows(db, "SELECT COUNT(*) FROM entry_standing WHERE record_id IN (?, ?)", e, f)[0][0] == 0
    assert ("chat", "dismissed") in rows(db, "SELECT path, decision FROM approval_log WHERE item_id = ?",
                                         dismissed)


def test_same_component_opens_a_card_and_unrelated_pairs_do_not(backend, seeded_db):
    S = backend
    decide(S, "Use JWT tokens for API auth", component="Auth")
    second = decide(S, "Use session cookies for API auth", component="Auth", app="Claude")
    assert any(f.get("same_component") for f in second.get("possible_conflicts") or [])
    assert len(second.get("conflict_cards") or []) == 1
    assert rows(seeded_db, "SELECT how FROM conflict_cards") == [("same_component",)]
    brief = call(S, "start_here", {"project": "Alpha", "size": "quick"})[1]
    text = brief if isinstance(brief, str) else json.dumps(brief)
    assert "Possible conflict" in text
    assert "Two AIs disagree" not in text   # the possible-conflict line already covers the pair
    decide(S, "Use SQLite for hosted storage", component="Storage", app="Claude")
    quiet = decide(S, "Back up the database every night at 02:30", component="Storage")
    assert not quiet.get("conflict_cards")
    assert rows(seeded_db, "SELECT COUNT(*) FROM conflict_cards")[0][0] == 1


def test_replaces_and_dismissing_the_old_pair_close_the_card(backend, seeded_db):
    S = backend
    db = seeded_db
    first = decide(S, "Use JWT tokens for API auth", component="Auth")["record"]["id"]
    second = decide(S, "Use session cookies for API auth", component="Auth", app="Claude")
    card = second["conflict_cards"][0]["review_item_id"]
    third = decide(S, "Use signed session cookies for API auth", component="Auth",
                   replaces=second["record"]["id"])
    assert rows(db, "SELECT status FROM conflict_cards")[0][0] == "dismissed"
    assert rows(db, "SELECT status FROM review_items WHERE id = ?", card)[0][0] == "rejected"
    assert "automatic" in [p[0] for p in rows(db, "SELECT path FROM approval_log WHERE item_id = ?", card)]
    # an explicit card on a different pair closes when the user dismisses the possible-conflict item
    decide(S, "Price Relay at $6", component="Pricing", app="Claude")
    hit = decide(S, "Price Relay at $12", component="Pricing")
    card2 = hit["conflict_cards"][0]["review_item_id"]
    conflict_id = next(f["review_item_id"] for f in hit["possible_conflicts"] if f.get("same_component"))
    ok(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": conflict_id,
                                 "reason": "they fit", "user_request": "User: yes, those two are compatible, dismiss it"},
            app="Claude Code"))
    assert rows(db, "SELECT status FROM conflict_cards WHERE review_item_id = ?", card2)[0][0] == "dismissed"
    assert first and third   # both saved; the card closed around them


def test_explicit_card_is_one_brief_line_without_ids(backend):
    S = backend
    a = decide(S, "Charge in USD only")["record"]["id"]
    b = decide(S, "Offer a yearly plan")["record"]["id"]
    ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                "topic": "API sessions", "item_id": a, "target_id": b}))
    brief = call(S, "start_here", {"project": "Alpha"})[1]
    text = brief if isinstance(brief, str) else json.dumps(brief)
    assert "Two AIs disagree about API sessions." in text
    assert f"D{a} vs D{b}" not in text


def test_list_includes_the_card_and_walk_does_not_treat_it_as_yes_no(backend, seeded_db):
    S = backend
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    item = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a, "target_id": b, "evidence": ["src/auth.py:42"]}))["flagged"]
    listed = ok(call(S, "manage_review", {"action": "list", "project": "Alpha"}))
    line = next(x for x in listed["items"] if x.startswith(f"#{item} "))
    assert "Two AIs disagree about API sessions." in line
    assert "src/auth.py:42" in line and "Claim A" in line and "Claim B" in line
    assert "Review: " in line
    one = ok(call(S, "manage_review", {"action": "list", "project": "Alpha", "item_id": item}))
    assert "src/auth.py:42" in one["item"]["conflict_card"]
    assert one["conflict_cards"][0]["text"].startswith("Two AIs disagree")
    listing = review.list_items(seeded_db, 1, "pending", 20)
    plan = waiting.prepare(listing, seeded_db, None)
    assert item not in plan["_walk"]["ids"]
    assert any(f"#{item} " in c and "Claim A" in c for c in plan["_walk"]["cards"])


def test_chat_approvals_off_refuses_and_on_allows_keep(backend, seeded_db, relay):
    S = backend
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    item = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a, "target_id": b, "evidence": ["src/auth.py:42"]}))["flagged"]
    _r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                         "keep": "a", "user_request": ASK}, app="Claude Code")
    assert body["error"] == "phone_only"
    blob = json.dumps(body)
    assert "127.0.0.1" not in blob and "review_page" not in body
    assert f"https://flowstatemcp.com/approve/testbox/{item}?n=" in blob
    assert rows(seeded_db, "SELECT status FROM review_items WHERE id = ?", item)[0][0] == "pending"
    approvals.set_relay_chat(seeded_db, True)
    kept = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "keep": "b", "user_request": "User: keep the newer cookie one"},
                   app="Claude Code"))
    assert kept["approved"] == item
    assert rows(seeded_db, "SELECT standing FROM entry_standing WHERE record_id = ?", a) == [("superseded",)]


def test_app_tool_is_hidden_unless_the_host_advertises_ui(backend, monkeypatch):
    S = backend
    async def names():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            return (await c.list_tools()).tools
    assert "resolve_conflict_card" not in [t.name for t in asyncio.run(names())]
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    tools = asyncio.run(names())
    tool = next(t for t in tools if t.name == "resolve_conflict_card")
    dumped = tool.model_dump(by_alias=True, exclude_none=True)
    ui = dumped["_meta"]["ui"]
    assert ui["visibility"] == ["app"] and ui["resourceUri"] == UI_URI
    assert "model" not in ui["visibility"]


def test_card_tap_resolves_and_refuses_chat_text(backend, seeded_db):
    S = backend
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    item = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a, "target_id": b}))["flagged"]
    r, body = call(S, "resolve_conflict_card", {"item_id": item, "keep": "a", "project": "Alpha",
                                                "user_request": "User: keep A"})
    assert body["error"] == "not_a_card_tap"
    r, body = call(S, "resolve_conflict_card", {"item_id": item, "keep": "a", "project": "Alpha",
                                                "message": "keep the first one"})
    assert body["error"] == "not_a_card_tap"
    assert rows(seeded_db, "SELECT status FROM review_items WHERE id = ?", item)[0][0] == "pending"
    saved = ok(call(S, "resolve_conflict_card", {"item_id": item, "keep": "a", "project": "Alpha"}))
    assert saved["resolved"] == item and saved["keep"] == "a"
    assert "Kept the older one" in saved["text"]
    assert rows(seeded_db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item) == \
        [("card", "kept_a")]
    assert rows(seeded_db, "SELECT status FROM review_items WHERE id = ?", item)[0][0] == "approved"


def test_card_html_is_self_contained_and_escaped_on_the_review_page(backend, seeded_db):
    assert "ui/message" not in CARD_HTML
    assert "window.openai" not in CARD_HTML
    assert "http://" not in CARD_HTML and "https://" not in CARD_HTML
    assert "textContent" in CARD_HTML and "resolve_conflict_card" in CARD_HTML
    assert "Keep A" in CARD_HTML and "Keep both" in CARD_HTML and "Dismiss" not in CARD_HTML
    assert MIME == "text/html;profile=mcp-app"
    S = backend
    nasty = 'Use JWT <img src=x onerror="alert(1)">'
    a = decide(S, nasty, app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                "topic": "API sessions", "item_id": a, "target_id": b,
                                "evidence": ["src/auth.py:42"]}))
    page = Page(seeded_db, 8770)
    client = TestClient(create_app(page), base_url=f"http://{HOST}:8770", follow_redirects=False)
    client.get("/?key=" + page.launch_key)
    html = client.get("/").text
    assert "Two AIs disagree about API sessions." in html
    assert "Keep A" in html and "Keep B" in html and "Keep both" in html and ">Dismiss<" in html
    assert "<img src=x" not in html and "&lt;img" in html
    assert "src/auth.py:42" in html
    assert "@media (min-width:800px)" in html and "flex-direction:row" in html
    assert "Approve: they really conflict" not in html   # this pair was flagged, not auto-detected


def test_review_page_keep_a_logs_the_review_page_path(backend, seeded_db):
    S = backend
    db = seeded_db
    a = decide(S, "Use JWT tokens for API auth", app="Claude")["record"]["id"]
    b = decide(S, "Use session cookies for API auth", app="Cursor")["record"]["id"]
    item = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a, "target_id": b}))["flagged"]
    page = Page(db, 8770)
    client = TestClient(create_app(page), base_url=f"http://{HOST}:8770", follow_redirects=False)
    client.get("/?key=" + page.launch_key)
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get("/").text).group(1)
    r = client.post("/approve", data={"item_id": item, "csrf": csrf, "keep": "a"})
    assert r.status_code == 303 and "Kept+the+older" in r.headers["location"].replace(" ", "+")
    assert rows(db, "SELECT status FROM conflict_cards WHERE review_item_id = ?", item)[0][0] == "kept_a"
    assert rows(db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item) == \
        [("review page", "kept_a")]
    html = client.get("/?status=approved").text
    assert "Kept the older one" in html
