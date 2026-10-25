"""The in-chat review card (2.4.4): show_review_card / resolve_review_card for every
non-disagreement review item, on MCP Apps hosts only. Temp databases only."""

import asyncio
import json
import re
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, card_taps, conflict_cards, popup, review, review_cards
from flowstate.review_card_ui import CARD_HTML, MIME, UI_URI
from fsa_test_keys import make_key


def call(S, name, args, app="Cursor"):
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


def status(db, item):
    return rows(db, "SELECT status FROM review_items WHERE id = ?", item)[0][0]


def suggest(S, summary, kind="decision", app="Claude"):
    return ok(call(S, "log_work", {"project": "Alpha", "kind": kind, "summary": summary,
                                   "suggested": True}, app=app))["queued_for_review"]


def session_item(db, text="Worked on the login page"):
    conn = sqlite3.connect(db)
    try:
        sid = conn.execute("INSERT INTO sessions (project_id, summary) VALUES (1, ?)",
                           (f"{review.AUTO_TAG} {text}",)).lastrowid
        conn.commit()
    finally:
        conn.close()
    return review.add_item(db, 1, "session_summary", f"{review.AUTO_TAG} {text}", None,
                           {}, "auto-session", session_id=sid)


def tap(S, item, decision, app="Cursor", **kw):
    """A tap from the card: with the one-use code show_review_card gave that card (made here
    directly; test_tap_codes_* check that show_review_card hands them out)."""
    kw.setdefault("tap", card_taps.mint(item, "review", app))
    return call(S, "resolve_review_card", {"item_id": item, "decision": decision,
                                           "project": "Alpha", **kw}, app=app)


@pytest.fixture
def ui(monkeypatch):
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    _key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    monkeypatch.setattr(popup, "listener", lambda: "relay")


def tools_and_resources(S):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="claude-ai", version="1")) as c:
            return (await c.list_tools()).tools, (await c.list_resources()).resources, \
                await c.read_resource(UI_URI)
    return asyncio.run(go())


def test_listed_only_with_the_ui_extension_with_card_meta_and_read_only_labels(backend, monkeypatch):
    S = backend
    tools, _res, _doc = tools_and_resources(S)
    assert not {"show_review_card", "resolve_review_card"} & {t.name for t in tools}
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    tools, res, doc = tools_and_resources(S)
    meta = {t.name: t.model_dump(by_alias=True, exclude_none=True).get("_meta", {}).get("ui")
            for t in tools}
    assert meta["show_review_card"] == {"resourceUri": UI_URI, "visibility": ["model", "app"]}
    assert meta["resolve_review_card"] == {"resourceUri": UI_URI, "visibility": ["app"]}
    show = next(t for t in tools if t.name == "show_review_card")
    assert show.annotations.readOnlyHint is True and show.annotations.idempotentHint is True
    assert show.annotations.openWorldHint is False
    hidden = next(t for t in tools if t.name == "resolve_review_card")
    assert hidden.inputSchema["properties"]["reject_reason"]["maxLength"] == review_cards.REASON_MAX
    assert "reason" not in hidden.inputSchema["properties"]
    assert UI_URI in [str(r.uri) for r in res]
    assert doc.contents[0].mimeType == MIME and "<!DOCTYPE html>" in doc.contents[0].text
    assert doc.contents[0].meta["ui"]["prefersBorder"] is True


def test_one_item_and_the_project_listing_capped_at_five_with_more(backend, seeded_db, ui):
    S = backend
    ids = [suggest(S, f"Use plan {n} for billing") for n in range(7)]
    r, body = call(S, "show_review_card", {"project": "Alpha"})
    cards = r.structuredContent["cards"]
    assert [c["item_id"] for c in cards] == ids[:5]   # oldest first
    assert body["shown"] == 5 and body["more"] == 2 and r.structuredContent["more"] == 2
    assert body["review_cards"][0]["text"].startswith("Save this suggested decision?")
    assert "how_to_decide" not in body and "dont_approve" not in body   # no queue guidance
    r, body = call(S, "show_review_card", {"project": "Alpha", "item_id": ids[6]})
    assert [c["item_id"] for c in r.structuredContent["cards"]] == [ids[6]] and "more" not in body
    card = r.structuredContent["cards"][0]
    assert card["title"] == "Save this suggested decision?" and card["who"] == "Claude"
    assert card["decide_here"] is True and card["approve_label"] == "Approve"
    assert "Approve saves it in Alpha as a decision" in card["does"] and card["when"]
    assert all(status(seeded_db, i) == "pending" for i in ids)   # showing decides nothing
    assert call(S, "show_review_card", {"item_id": 99999})[1]["error"] == "record_not_found"
    assert call(S, "show_review_card", {"project": "Nope"})[1]["error"] == "project_not_found"
    assert call(S, "show_review_card", {"project": "Beta Project", "item_id": ids[0]})[1]["error"] \
        == "record_not_found"


def test_disagreements_point_to_their_own_card(backend, seeded_db, ui):
    S = backend
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Charge in USD"}))
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Yearly plan"}))
    dis = ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha", "topic": "pricing",
                                      "item_id": a["record"]["id"],
                                      "target_id": b["record"]["id"]}))["flagged"]
    r, body = call(S, "show_review_card", {"project": "Alpha", "item_id": dis})
    want = f'show_conflict_card(project="Alpha", item_id={dis})'
    assert body["show_card"] == want and body["shown"] == 0 and r.structuredContent["cards"] == []
    r, body = call(S, "show_review_card", {"project": "Alpha"})
    assert body["shown"] == 0 and body["disagreements"] == {"waiting": 1, "show_card": want}
    assert want in body["note"] and "own card" in r.structuredContent["also"]
    item = suggest(S, "Use Stripe for billing")
    r, body = call(S, "show_review_card", {"project": "Alpha"})
    assert [c["item_id"] for c in r.structuredContent["cards"]] == [item]
    _r, body = tap(S, dis, "approve")
    assert body["error"] == "use_conflict_card" and status(seeded_db, dis) == "pending"


def test_every_kind_gets_a_card(backend, seeded_db, ui):
    S = backend
    db = seeded_db
    made = {
        "session_summary": session_item(db),
        "decision": suggest(S, "Use Postgres for storage"),
        "todo": suggest(S, "Write the billing docs", kind="todo"),
        "import": ok(call(S, "manage_memory", {"action": "import", "project": "Alpha",
                                               "text": "- prefers tabs"}))["item_ids"][0],
        "cleanup": review.add_item(db, 1, "cleanup", "Stale todo", "Not touched for months.",
                                   {"type": "stale", "records": [["todo", 1]]}, "cleanup"),
        "edit": review.add_item(db, 1, "edit", 'Edit todo #1: title: "Alpha todo" -> "Alpha task"',
                                "Edited in a markdown file (notes.md).",
                                {"record": ["todo", 1], "set": {"title": "Alpha task"}}, "import:notes.md"),
        "conflict": review.add_item(db, 1, "conflict", 'Possible conflict: D1 "x" vs D2 "y"', None,
                                    {"conflict_id": 0, "new_id": 1, "old_id": 2}, "flowstate"),
        "setting": review.add_item(db, 1, "setting", "Turn review off for Alpha", None,
                                   {"scope": "project", "mode": "off"}, "Claude"),
        "source": review.add_item(db, 1, "source", "Did you say this?", None,
                                  {"record_type": "learning", "record_id": 1, "title": "Alpha lesson"},
                                  "Claude"),
        "delete": review.add_item(db, 1, "delete", "Delete todo 1", None,
                                  {"record_type": "todo", "record_id": 1, "title": "Alpha todo",
                                   "user_only": True}, "Claude"),
    }
    titles = {"session_summary": "Keep this session summary?", "decision": "Save this suggested decision?",
              "todo": "Save this suggested todo?", "import": "Save this imported learning?",
              "cleanup": 'Cancel the todo "Alpha todo" in Alpha?',
              "edit": "Apply this edit from a markdown file?",
              "conflict": "Do these two decisions really conflict?",
              "setting": "Turn review off for Alpha?",
              "source": 'Mark the learning "Alpha lesson" in Alpha as something you said yourself '
                        '(user_stated)?',
              "delete": 'Delete the todo "Alpha todo" from Alpha?'}
    for what, item in made.items():   # shown in another app than the one that ran the import
        r, body = call(S, "show_review_card", {"project": "Alpha", "item_id": item},
                       app="Claude Desktop")
        card = r.structuredContent["cards"][0]
        assert card["title"] == titles[what], (what, card["title"])
        assert card["text"] and card["kind_label"], what
        assert body["review_cards"][0]["text"].startswith(card["title"])   # the text is the card
        assert card["decide_here"] is (what not in ("setting", "source", "delete")), what
    sess = call(S, "show_review_card", {"item_id": made["session_summary"]})[0].structuredContent["cards"][0]
    assert sess["approve_label"] == "Keep" and sess["reject_label"] == "Don't keep"
    assert review.AUTO_TAG not in sess["text"]
    conf = call(S, "show_review_card", {"item_id": made["conflict"]})[0].structuredContent["cards"][0]
    assert conf["approve_label"] == "They conflict" and conf["reject_label"] == "They fit together"
    imp = call(S, "show_review_card", {"item_id": made["import"]})[0].structuredContent["cards"][0]
    assert imp["who"] == "Cursor" and imp["kind_label"] == "Imported learning"   # who ran the import


def test_approve_and_reject_with_and_without_reason_are_logged_as_card(backend, seeded_db, ui):
    S = backend
    db = seeded_db
    one, two, three = (suggest(S, f"Use queue {n} for jobs") for n in ("A", "B", "C"))
    saved = ok(tap(S, one, "approve"))
    assert saved["resolved"] == one and saved["decision"] == "approve"
    assert saved["record"]["table"] == "conversations"
    rid = saved["record"]["id"]
    assert "Use queue A for jobs" in json.dumps(rows(db, "SELECT * FROM conversations WHERE id = ?", rid))
    card = call(S, "show_review_card", {"item_id": one})[0].structuredContent["cards"][0]
    assert card["resolved"] is True and card["detail"].startswith("Approved")
    r, body = tap(S, two, "reject", reject_reason="  Not   what we picked  ")
    assert not r.isError and r.structuredContent["cards"][0]["detail"] == \
        "Rejected: Not what we picked. Nothing was deleted."
    assert rows(db, "SELECT status, reject_reason FROM review_items WHERE id = ?", two) == \
        [("rejected", "Not what we picked")]
    ok(tap(S, three, "reject"))
    assert rows(db, "SELECT reject_reason FROM review_items WHERE id = ?", three)[0][0] == \
        review_cards.DEFAULT_REASON
    assert rows(db, "SELECT item_id, path, decision FROM approval_log ORDER BY id") == \
        [(one, "card", "approved"), (two, "card", "rejected"), (three, "card", "rejected")]
    long = suggest(S, "Use queue D for jobs")
    r, _body = tap(S, long, "reject", reject_reason="x" * 1000)   # over maxLength: refused
    assert r.isError and status(db, long) == "pending"
    ok(tap(S, long, "reject", reject_reason="y" * review_cards.REASON_MAX))


def test_card_uses_the_same_apply_functions_as_manage_review(backend, seeded_db, ui):
    S = backend
    db = seeded_db
    sess = session_item(db, "Fixed the build")
    ok(tap(S, sess, "approve"))
    assert rows(db, "SELECT summary FROM sessions WHERE id = (SELECT session_id FROM review_items "
                    "WHERE id = ?)", sess)[0][0] == "Fixed the build"
    edit = review.add_item(db, 1, "edit", 'Edit todo #1: title', None,
                           {"record": ["todo", 1], "set": {"title": "Alpha task"}}, "import:notes.md")
    out = ok(tap(S, edit, "approve"))
    assert out.get("safety_copy") and rows(db, "SELECT title FROM todos WHERE id = 1")[0][0] == "Alpha task"
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use session cookies for API auth"}))["record"]["id"]
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use session cookies with JWT for API auth"}))
    conflict = next(f["review_item_id"] for f in b["possible_conflicts"])
    ok(tap(S, conflict, "reject", app="Claude Desktop"))   # they fit together: the pair is dismissed
    assert rows(db, "SELECT status FROM decision_conflicts WHERE review_item_id = ?", conflict) == \
        [("dismissed_compatible",)]
    assert a


def test_chat_text_and_non_mcp_apps_clients_are_refused(backend, seeded_db, monkeypatch):
    S = backend
    item = suggest(S, "Use Redis for caching")
    assert tap(S, item, "approve")[1]["error"] == "not_a_card_tap"   # no MCP Apps here
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)
    for key in ("user_request", "message", "text", "reason"):
        _r, body = tap(S, item, "approve", **{key: "the user said yes"})
        assert body["error"] == "not_a_card_tap", key
    assert tap(S, item, "approve", reject_reason="why")[1]["error"] == "bad_request"
    assert tap(S, item, "maybe")[1]["error"] == "decision_required"
    assert status(seeded_db, item) == "pending"
    assert rows(seeded_db, "SELECT COUNT(*) FROM approval_log")[0][0] == 0


def test_already_decided_and_wrong_project(backend, seeded_db, ui):
    S = backend
    item = suggest(S, "Use Kafka for events")
    ok(tap(S, item, "approve"))
    assert tap(S, item, "reject")[1]["error"] == "already_decided"
    other = suggest(S, "Use NATS for events")
    _r, body = call(S, "resolve_review_card", {"item_id": other, "decision": "approve",
                                               "project": "Beta Project",
                                               "tap": card_taps.mint(other, "review", "Cursor")})
    assert body["error"] == "project_mismatch" and status(seeded_db, other) == "pending"
    assert tap(S, 99999, "approve")[1]["error"] == "record_not_found"


def test_relay_taps_are_phone_only_unless_chat_approvals_are_on(backend, seeded_db, relay, ui):
    S = backend
    item = suggest(S, "Use SQS for jobs")
    _r, body = tap(S, item, "approve")
    assert body["error"] == "phone_only"
    blob = json.dumps(body)
    # no phone link with the relay handle to an app elsewhere (task 54, D19)
    assert "testbox" not in blob and approvals.PHONE_ELSEWHERE in blob and "127.0.0.1" not in blob
    assert status(seeded_db, item) == "pending"
    approvals.set_relay_chat(seeded_db, True)
    assert ok(tap(S, item, "approve"))["resolved"] == item
    assert rows(seeded_db, "SELECT path FROM approval_log WHERE item_id = ?", item) == [("card",)]


def test_user_only_items_are_read_only_with_a_link_and_refused_on_resolve(backend, seeded_db, ui):
    S = backend
    db = seeded_db
    approvals.set_on(db, True)
    queued = ok(call(S, "manage_projects", {"action": "delete_component", "project": "Alpha",
                                            "component_id": 1}))
    item = queued.get("delete_requested") or queued.get("item_id")
    assert item and status(db, item) == "pending"
    setting = review.add_item(db, 1, "setting", "Turn review off for Alpha", None,
                              {"scope": "project", "mode": "off"}, "Claude")
    for it in (item, setting):
        r, body = call(S, "show_review_card", {"project": "Alpha", "item_id": it})
        card = r.structuredContent["cards"][0]
        assert card["decide_here"] is False and card["locked_note"].startswith("Only you decide")
        assert card["link"].startswith("http://127.0.0.1:")   # this computer: the review page
        assert "Buttons:" not in body["review_cards"][0]["text"]
        _r, refused = tap(S, it, "approve")
        assert refused["error"] == "user_only" and status(db, it) == "pending"
        assert tap(S, it, "reject")[1]["error"] == "user_only"
    assert rows(db, "SELECT COUNT(*) FROM approval_log WHERE path = 'card'")[0][0] == 0


def test_user_only_card_through_the_relay_says_where_without_the_phone_link(backend, seeded_db, relay, ui):
    S = backend
    db = seeded_db
    approvals.set_relay_chat(db, True)
    item = review.add_item(db, 1, "source", "Did you say this?", None,
                           {"record_type": "learning", "record_id": 1, "title": "Alpha lesson"}, "Claude")
    card = call(S, "show_review_card", {"item_id": item})[0].structuredContent["cards"][0]
    assert card["link"] == "" and approvals.PHONE_ELSEWHERE in card["locked_note"]   # task 54
    assert "127.0.0.1" not in json.dumps(card) and "testbox" not in json.dumps(card)
    assert tap(S, item, "approve")[1]["error"] == "user_only"


def test_walk_and_list_point_at_the_cards_on_mcp_apps_hosts_only(backend, seeded_db, monkeypatch):
    S = backend
    item = suggest(S, "Use Celery for jobs")
    for args in ({"action": "list"}, {"action": "walk"}):
        assert "show_card" not in ok(call(S, "manage_review", args)), args   # text only here
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)
    want = f'show_review_card(project="Alpha", item_id={item})'
    for args in ({"action": "list"}, {"action": "list", "project": "Alpha"}, {"action": "walk"}):
        body = ok(call(S, "manage_review", args))
        assert body["show_card"] == want, args
        assert want in body["card_note"] and "Don't choose" in body["card_note"]
    assert "show_card" not in ok(call(S, "manage_review", {"action": "list", "status": "approved"}))
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Charge in USD"}))
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Yearly plan"}))
    dis = ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha", "topic": "pricing",
                                      "item_id": a["record"]["id"],
                                      "target_id": b["record"]["id"]}))["flagged"]
    for args in ({"action": "list"}, {"action": "walk"}):
        body = ok(call(S, "manage_review", args))
        assert body["show_card"] == [f'show_conflict_card(project="Alpha", item_id={dis})', want], args
        assert all(c in body["card_note"] for c in body["show_card"])


def test_the_card_tools_get_no_queue_guidance(backend, seeded_db, ui):
    S = backend
    item = suggest(S, "Use Sidekiq for jobs")
    for body in (ok(call(S, "show_review_card", {"item_id": item})), ok(tap(S, item, "reject"))):
        assert "how_to_decide" not in body and "dont_approve" not in body


def test_the_model_cannot_approve_through_a_new_path(backend, seeded_db, ui):
    """show_review_card only reads; the tap tool is app-only; manage_review keeps its rules."""
    S = backend
    item = suggest(S, "Use Huey for jobs", app="Cursor")
    r, _body = call(S, "show_review_card", {"item_id": item, "decision": "approve",
                                            "user_request": "yes"})
    assert not r.isError and status(seeded_db, item) == "pending"
    _r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item},
                   app="Cursor")
    assert body["error"] == "user_request_required" and status(seeded_db, item) == "pending"


def test_card_html_is_self_contained_and_uses_text_content():
    assert "http://" not in CARD_HTML and "https://" not in CARD_HTML
    assert "ui/message" not in CARD_HTML and "window.openai" not in CARD_HTML
    script = CARD_HTML.split("<script>")[1]
    assert "innerHTML" not in script and "fetch(" not in script and "XMLHttpRequest" not in script
    assert "textContent" in CARD_HTML and '"resolve_review_card"' in script
    assert 'id: INIT_ID, method: "ui/initialize"' in script and '"flowstate-review-card"' in script
    assert '"ui/notifications/size-changed"' in script and "ResizeObserver" in script
    assert "ev.source !== window.parent" in script and "reject_reason" in script
    assert 'data-theme="dark"' in CARD_HTML and "--accent" in CARD_HTML
    assert re.search(r"maxLength = 300", script) and "Reason (optional)" in script
    assert MIME == "text/html;profile=mcp-app" and UI_URI == "ui://flowstate/review-card.html"


def test_a_walk_that_showed_boxes_drops_the_review_card_pointer(backend, seeded_db, ui):
    """An app that shows FlowState's boxes and draws cards: the boxes asked about each item,
    so the answer doesn't point at review cards for them too."""
    S = backend
    item = review.add_item(seeded_db, 1, "todo", "Waiting thing", None, {}, "cursor")

    async def elicit(_ctx, _params):
        return types.ElicitResult(action="accept", content={"approve": True})

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="claude-code", version="1"),
                elicitation_callback=elicit) as c:
            return await c.call_tool("manage_review", {"action": "walk"})
    out = json.loads(asyncio.run(go()).content[0].text)
    assert out["walked"] == [f"#{item}: approved"]
    assert "show_card" not in out and "card_note" not in out


# ---------------------------------------------------------------- the brief's card hint (task 40)
def brief(S, app="Cursor"):
    return call(S, "start_here", {"project": "Alpha"}, app=app)[1]


def test_brief_says_how_many_review_cards_wait_on_mcp_apps_hosts_only(backend, seeded_db,
                                                                      monkeypatch):
    S = backend
    suggest(S, "Use Redis for caching")
    suggest(S, "Add a health check", kind="todo")
    plain = brief(S)
    assert "show_review_card" not in plain and "Review cards:" not in plain   # no card tool there
    assert "Review queue: 2 items waiting" in plain
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)
    text = brief(S)
    want = ('Review cards: 2 items wait for the user; call show_review_card(project="Alpha") '
            'to show them as cards in the chat.')
    assert want in text and text.count("show_review_card") == 1


def test_brief_card_hint_is_quiet_when_nothing_waits_and_counts_one(backend, seeded_db, ui):
    S = backend
    assert "show_review_card" not in brief(S)
    item = suggest(S, "Use Kafka for events")
    assert "Review cards: 1 item waits for the user" in brief(S)
    ok(call(S, "manage_review", {"action": "reject", "item_id": item, "project": "Alpha",
                                 "user_request": "User: reject the Kafka suggestion please",
                                 "reason": "not needed"},
            app="Claude Code"))
    assert "show_review_card" not in brief(S)


def test_brief_names_both_cards_on_one_line_when_both_kinds_wait(backend, seeded_db, ui):
    S = backend
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Charge in USD only"}))["record"]["id"]
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Offer a yearly plan"}))["record"]["id"]
    dis = ok(call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha",
                                      "topic": "pricing", "item_id": a, "target_id": b}))["flagged"]
    suggest(S, "Use Redis for caching")
    text = brief(S)
    lines = [ln for ln in text.splitlines() if "show_conflict_card" in ln or "show_review_card" in ln]
    assert lines == [f'Cards waiting: call show_conflict_card(project="Alpha", item_id={dis}) for '
                     f'the disagreement and show_review_card(project="Alpha") for the other 1 item, '
                     f'to show them in the chat.']
    assert "Disagreement card waiting" not in text and "Review cards:" not in text
