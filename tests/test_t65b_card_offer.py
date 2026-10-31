"""Task 65b: an app on a network sign-in is OFFERED the cards on the user's review page.

The first time a connector that draws MCP Apps cards (but whose sign-in isn't on the card list)
would get a card, it gets the text card as before, and one review item "Allow tap cards in
<app>?" waits for the user, once per sign-in. Only the user answers it, and only on the review
page or the phone page: every AI tool (chat approvals on or off), the chat walk, a popup, a card
tap and the terminal are refused. Allow puts the sign-in's own name on the card sign-in list;
removing it (Settings or terminal) makes it text-only again, with no new offer unless the user
offers it again. End to end through the fake relay (test_relay_integration.py), temp databases
only."""

import argparse
import json
import re
import sqlite3

import pytest

pytest.importorskip("cryptography")

from starlette.testclient import TestClient  # noqa: E402

from flowstate import approvals, card_offer, card_taps, cli_review, identity, review  # noqa: E402
from flowstate.review_page import HOST, Page, create_app  # noqa: E402
from test_relay_integration import _oauth, world  # noqa: E402,F401
from test_t65_relay_cards import CARD_TOOLS, UI, Mcp, status, taps  # noqa: E402

PORT = 8770
BASE = f"http://{HOST}:{PORT}"
SIGN_IN = "relay test app"


def offers(db) -> list:
    return [i for i in review.list_items(db, None, "all", 100)["items"] if i["kind"] == "card_offer"]


def approval_log(db, item_id) -> list:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT kind, path, client, decision FROM approval_log WHERE item_id = ?",
                            (item_id,)).fetchall()
    finally:
        conn.close()


def settings_log(db, scope) -> list:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT new_mode, client FROM review_mode_log WHERE scope = ? ORDER BY id",
                            (scope,)).fetchall()
    finally:
        conn.close()


def page_client(db, **kw):
    p = Page(db, PORT, **kw)
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    if p.launch_key:
        assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    return p, c, re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def cli(db, *argv, sign_in=False):
    what, arg, name = (list(argv) + [None, None, None])[:3]
    return cli_review.cmd_approvals(argparse.Namespace(what=what, arg=arg, name=name, db=str(db),
                                                       project=None, clear=False, sign_in=sign_in))


def setup(world, db):
    c = world["client"]
    card_taps.clear()
    connector, _ = _oauth(c)            # the user's connector: OAuth app "Relay test app"
    other, _ = _oauth(c)
    cursor = Mcp(c, other["access_token"], "Cursor")
    host = Mcp(c, connector["access_token"], "claude-ai", UI)
    sug = cursor.call("log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                   "summary": "Oat milk sells out by noon"})["queued_for_review"]
    return c, connector, host, cursor, sug


def test_offered_once_and_the_ai_cannot_answer_it(world, seeded_db, monkeypatch):
    db = seeded_db
    c, connector, host, cursor, sug = setup(world, db)

    # -- text only before the grant; the first card it would get queues the offer
    assert not CARD_TOOLS & set(host.tools())
    brief = host.call("start_here", {"project": "Alpha"})
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "show_card" not in listed
    assert "Allow tap cards in claude-ai (signed in as Relay test app)?" in listed["cards_off"]
    (offer,) = offers(db)
    assert offer["status"] == "pending" and offer["summary"] == \
        "Allow tap cards in claude-ai (signed in as Relay test app)?"
    assert json.loads(offer["payload"])["credential"] == "Relay test app"
    assert "Allow tap cards in" in json.dumps(brief)   # the brief offered it first

    # -- exactly once: more answers, the walk, a second session on the same sign-in
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    walk = host.call("manage_review", {"action": "walk", "project": "Alpha"})
    again = Mcp(c, connector["access_token"], "claude-ai", UI)
    again.call("manage_review", {"action": "list", "project": "Alpha"})
    again.call("start_here", {"project": "Alpha"})
    assert len(offers(db)) == 1
    assert "cards_off" not in cursor.call("manage_review", {"action": "list"})   # no MCP Apps: none
    assert len(offers(db)) == 1
    oid = offer["id"]
    assert status(db, oid) == "pending" and "walked" not in walk

    # -- no AI path answers it: through the relay (chat approvals off, then on)
    for verb in ("approve", "reject"):
        r = host.call("manage_review", {"action": verb, "item_id": oid, "project": "Alpha",
                                        "reason": "x", "user_request": "The user said: yes, allow it"})
        assert r["error"] == "phone_only" and status(db, oid) == "pending"
    approvals.set_relay_chat(db, True)
    for verb in ("approve", "reject"):
        for app in (host, cursor):
            r = app.call("manage_review", {"action": verb, "item_id": oid, "project": "Alpha",
                                           "reason": "x",
                                           "user_request": "The user said: yes, allow cards in Claude"})
            assert r["error"] == "user_only" and status(db, oid) == "pending", r
    # a card tap (any code) is refused
    r = host.call("resolve_review_card", {"item_id": oid, "decision": "approve", "project": "Alpha",
                                          "tap": "x" * 43})
    assert r.get("error") and status(db, oid) == "pending"
    assert SIGN_IN not in approvals.signins(db, "card")

    # -- nor any other decider that isn't the review page or the phone page
    from flowstate import server as S
    words = {"item_id": oid, "reason": "no", "user_request": "The user said: allow it"}
    for client in ("command-line", "popup (claude-ai)", "chat (claude-ai)", "card (claude-ai)",
                   "claude-ai", "review page"):
        for verb in ("review_approve", "review_reject"):
            assert S._review_decide(verb, offer["project_id"], dict(words), client)["error"] == "user_only"
    tok = identity.set_current(identity.Identity(app="claude-ai", model=None, machine="m",
                                                 identity_source="self_reported"))
    try:   # an AI on this computer that calls itself the review page
        assert S._review_decide("review_approve", offer["project_id"], dict(words),
                                "review-page")["error"] == "user_only"
    finally:
        identity.reset(tok)
    # the terminal's `flowstate approve` goes through the same check
    assert card_offer.refusal(review.get_item(db, oid), cli_review.CLIENT, False)["error"] == "user_only"
    # the open review link page (any program on this computer can open it) doesn't decide it
    _p, link, csrf = page_client(db, open_link=True, settings="read")
    link.post("/approve", data={"item_id": oid, "csrf": csrf}, headers={"origin": BASE})
    assert status(db, oid) == "pending" and SIGN_IN not in approvals.signins(db, "card")
    assert approval_log(db, oid) == []


def test_allow_on_the_review_page_then_revoke(world, seeded_db, monkeypatch):
    db = seeded_db
    c, connector, host, cursor, sug = setup(world, db)
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    (offer,) = offers(db)

    # -- the user taps Allow on the review page
    _p, page, csrf = page_client(db)
    html = page.get("/").text
    assert "Allow tap cards in claude-ai (signed in as Relay test app)?" in html
    r = page.post("/approve", data={"item_id": offer["id"], "csrf": csrf}, headers={"origin": BASE})
    assert r.status_code == 303 and status(db, offer["id"]) == "approved"
    assert SIGN_IN in approvals.signins(db, "card")
    assert approval_log(db, offer["id"]) == [("card_offer", "review page", "review-page", "approved")]
    assert settings_log(db, "card_signins")[-1] == (f"added {SIGN_IN}", "review-page")

    # -- next manage_review on that connection renders the card, and a tap counts
    assert CARD_TOOLS <= set(host.tools())
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "cards_off" not in listed
    assert listed["show_card"] == f'show_review_card(project="Alpha", item_id={sug})'
    shown = host.raw("show_review_card", {"project": "Alpha", "item_id": sug})
    code = taps(shown)[str(sug)]
    done = host.call("resolve_review_card", {"item_id": sug, "decision": "approve",
                                             "project": "Alpha", "tap": code})
    assert done["resolved"] == sug and status(db, sug) == "approved", done
    assert not CARD_TOOLS & set(cursor.tools())   # another sign-in: still none

    # -- revoke in Settings: text only again, and no new offer
    page.post("/settings/cards", data={"csrf": csrf, "op": "remove", "name": SIGN_IN, "signin": "1"},
              headers={"origin": BASE})
    assert SIGN_IN not in approvals.signins(db, "card")
    assert not CARD_TOOLS & set(host.tools())
    sug2 = cursor.call("log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                    "summary": "Close the shop early on Sundays"})["queued_for_review"]
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "show_card" not in listed and "--sign-in" in listed["cards_off"]
    assert len(offers(db)) == 1

    # -- the user offers it again from Settings: one new offer; Allow on the phone-page path
    html = page.get("/?view=settings").text
    assert "Offer the cards to apps on a sign-in" in html and "Offer again" in html
    page.post("/settings/card-offers", data={"csrf": csrf, "state": "again", "name": SIGN_IN},
              headers={"origin": BASE})
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    second = [o for o in offers(db) if o["status"] == "pending"]
    assert len(second) == 1 and len(offers(db)) == 2
    from flowstate import server as S
    S._review_decide("review_approve", second[0]["project_id"],
                     {"item_id": second[0]["id"], "user_request": "approved on the phone"}, "phone")
    assert SIGN_IN in approvals.signins(db, "card") and CARD_TOOLS <= set(host.tools())
    assert approval_log(db, second[0]["id"])[0][1:] == ("phone", "phone", "approved")
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert listed["show_card"] == f'show_review_card(project="Alpha", item_id={sug2})'

    # -- revoke in the terminal: text only again, still no new offer
    assert cli(db, "cards", "remove", SIGN_IN, sign_in=True) == 0
    assert not CARD_TOOLS & set(host.tools())
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "show_card" not in listed and len(offers(db)) == 2


def test_no_is_final_and_offers_can_be_turned_off(world, seeded_db):
    db = seeded_db
    c, connector, host, cursor, sug = setup(world, db)
    assert cli(db, "cards", "offers", "off") == 0
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert offers(db) == []                              # offers off: none
    assert settings_log(db, card_offer.OFFERS)[-1][0] == "off"
    assert cli(db, "cards", "offers", "on") == 0
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    (offer,) = offers(db)
    _p, page, csrf = page_client(db)
    page.post("/reject", data={"item_id": offer["id"], "csrf": csrf,
                               "reason": "not allowed on the review page"}, headers={"origin": BASE})
    assert status(db, offer["id"]) == "rejected" and SIGN_IN not in approvals.signins(db, "card")
    assert approval_log(db, offer["id"]) == [("card_offer", "review page", "review-page", "rejected")]
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert len(offers(db)) == 1                          # after a No: not asked again
    assert cli(db, "cards", "offer-again", "Relay Test App") == 0
    host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert len([o for o in offers(db) if o["status"] == "pending"]) == 1


def test_stdio_and_shared_token_get_no_offer(backend, seeded_db, monkeypatch):
    """Only an app on its own network sign-in is offered anything: outside a request, on
    stdio and for the shared token, offer() does nothing."""
    from flowstate import card_trust, conflict_cards
    monkeypatch.setattr(conflict_cards, "ui_declared", lambda: True)
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: False)
    monkeypatch.setattr(card_trust, "caller", lambda: None)              # shared token
    assert card_offer.offer(seeded_db, "Alpha") is None
    monkeypatch.setattr(card_trust, "caller",
                        lambda: card_trust.CardCaller("someapp", "someapp", None, "stdio"))
    assert card_offer.offer(seeded_db, "Alpha") is None
    assert offers(seeded_db) == []
