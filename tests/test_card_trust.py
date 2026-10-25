"""Audit 46, H1 and H2: in-chat cards trust only what the user does on them. Temp DBs only.

H1: a client that says it shows MCP Apps cards gets them only if its name is on the user's
card list; a tap needs the one-use code only the card's page gets (the result's _meta); the
app that queued an item doesn't decide it on its own card; a tap is logged as a tap on the card
in that app, never as "the user".
H2: a session summary record's Remove / Bring back on a card follow the delete rules."""

import asyncio
import json

import mcp.types as mtypes
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, card_taps, conflict_cards, review
from flowstate.conflict_cards import UI_EXT
from test_session_contents import _q, _work

UI = {UI_EXT: {"mimeTypes": ["text/html;profile=mcp-app"]}}


@pytest.fixture
def claims_ui(monkeypatch):
    """Every client in this test really sends the MCP Apps extension when it connects."""
    real = mtypes.ClientCapabilities
    monkeypatch.setattr(mtypes, "ClientCapabilities", lambda **kw: real(**kw, extensions=UI))
    card_taps.clear()


def session(S, app, fn):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=mtypes.Implementation(name=app, version="1")) as c:
            return await fn(c)
    return asyncio.run(go())


def call(S, name, args, app="claude-ai"):
    r = session(S, app, lambda c: c.call_tool(name, args))
    try:
        return r, json.loads(r.content[0].text)
    except ValueError:
        return r, r.content[0].text


def names(S, app):
    return {t.name for t in session(S, app, lambda c: c.list_tools()).tools}


def suggest(S, text, app="Cursor"):
    _r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": text,
                                    "suggested": True}, app=app)
    return body["queued_for_review"]


def status(db, item):
    return _q(db, "SELECT status FROM review_items WHERE id = ?", item)[0][0]


def codes(r):
    return (r.meta or {}).get(card_taps.META_KEY) or {}


# ---------------------------------------------------------------- H1
def test_cards_only_for_apps_on_the_card_list(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    card_tools = {"show_review_card", "resolve_review_card", "resolve_conflict_card"}
    assert not card_tools & names(S, "my-little-client")   # says it shows cards: not enough
    assert card_tools <= names(S, "claude-ai")             # Claude Desktop / claude.ai: listed
    approvals.change_card_clients(db, "My-Little-Client", True)
    assert card_tools <= names(S, "my-little-client")
    approvals.change_card_clients(db, "claude-ai", False)
    assert not card_tools & names(S, "claude-ai")


def test_the_tap_code_is_only_in_meta(backend, claims_ui):
    S = backend
    item = suggest(S, "Use Postgres for storage")
    r, body = call(S, "show_review_card", {"project": "Alpha"})
    got = codes(r)
    assert set(got) == {str(item)}
    seen = r.content[0].text + json.dumps(r.structuredContent)
    assert got[str(item)] not in seen and "flowstate/taps" not in seen
    assert body["review_cards"][0]["review_item_id"] == item


def test_a_tap_without_the_cards_code_is_refused(backend, claims_ui):
    """The audit's reproduction: a client claiming card support called the hidden tool and
    approved. Now the model (or a script) has no code, so nothing is decided."""
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use Redis for caching")
    tap = {"item_id": item, "decision": "approve", "project": "Alpha"}
    for extra in ({}, {"tap": "made-up-code-0123456789"}, {"tap": ""}):
        _r, body = call(S, "resolve_review_card", {**tap, **extra})
        assert body["error"] == "not_a_card_tap", extra
    other = suggest(S, "Use Memcached for caching")
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": other})
    _r, body = call(S, "resolve_review_card", {**tap, "tap": codes(r)[str(other)]})
    assert body["error"] == "not_a_card_tap"               # another card's code
    assert status(db, item) == "pending"
    assert _q(db, "SELECT COUNT(*) FROM approval_log")[0][0] == 0


def test_the_code_works_once_for_its_own_app_and_the_tap_is_logged_honestly(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    approvals.change_card_clients(db, "other-host", True)
    item = suggest(S, "Use Kafka for events")
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item})
    code = codes(r)[str(item)]
    tap = {"item_id": item, "decision": "approve", "project": "Alpha", "tap": code}
    _r, body = call(S, "resolve_review_card", tap, app="other-host")
    assert body["error"] == "not_a_card_tap"               # made for claude-ai's card
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item})
    tap["tap"] = codes(r)[str(item)]
    _r, body = call(S, "resolve_review_card", tap)
    assert body["resolved"] == item and status(db, item) == "approved"
    assert _q(db, "SELECT path, client FROM approval_log WHERE item_id = ?", item) == \
        [("card", "claude-ai")]
    [(words,)] = _q(db, "SELECT user_request FROM review_items WHERE id = ?", item)
    assert words == ("Approve tapped on the review card in claude-ai (its one-use card code "
                     "checked).") and "the user" not in words.lower()
    _r, body = call(S, "resolve_review_card", tap)
    assert body["error"] in ("not_a_card_tap", "already_decided")


def test_codes_expire(backend, claims_ui, monkeypatch):
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use NATS for events")
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item})
    now = card_taps._clock()
    monkeypatch.setattr(card_taps, "_clock", lambda: now + card_taps.TTL + 1)
    _r, body = call(S, "resolve_review_card", {"item_id": item, "decision": "approve",
                                               "project": "Alpha", "tap": codes(r)[str(item)]})
    assert body["error"] == "not_a_card_tap" and status(db, item) == "pending"


def test_the_app_that_queued_an_item_has_no_buttons_for_it(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    own = suggest(S, "Use SQS for jobs", app="claude-ai")
    r, body = call(S, "show_review_card", {"project": "Alpha", "item_id": own})
    card = r.structuredContent["cards"][0]
    assert card["decide_here"] is False and "queued it itself" in card["locked_note"]
    assert not codes(r)                                    # no buttons, no code
    _r, body = call(S, "resolve_review_card", {"item_id": own, "decision": "approve",
                                               "project": "Alpha",
                                               "tap": card_taps.mint(own, "review", "claude-ai")})
    assert body["error"] == "own_item" and status(db, own) == "pending"
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": own}, app="Cursor")
    # (Cursor isn't on the card list: it gets the text, not the buttons)
    assert "queued it itself" not in json.dumps(r.structuredContent or {})


def test_own_disagreement_card_is_refused_too(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    a = call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Use JWT for API auth"},
             app="Cursor")[1]["record"]["id"]
    b = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                             "summary": "Use session cookies for API auth"}, app="Cursor")[1]["record"]["id"]
    item = call(S, "manage_links", {"action": "flag_conflict", "project": "Alpha", "topic": "auth",
                                    "item_id": a, "target_id": b})[1]["flagged"]   # by claude-ai
    _r, body = call(S, "resolve_conflict_card", {"item_id": item, "keep": "a", "project": "Alpha",
                                                 "tap": card_taps.mint(item, "conflict", "claude-ai")})
    assert body["error"] == "own_item" and status(db, item) == "pending"
    _r, body = call(S, "resolve_conflict_card", {"item_id": item, "keep": "a", "project": "Alpha"},
                    app="Cursor")                          # not a card host
    assert body["error"] == "not_a_card_tap"


def test_own_session_summary_keeps_its_buttons(backend, claims_ui):
    """FlowState queues a session summary itself when the session closes; Claude Desktop's
    user keeps it on the card as before."""
    S, db = backend, backend.backend.db_path
    item, _pid, _cid = _work(S, db)
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item}, app="claude-code")
    assert not codes(r)                                    # claude-code: no cards
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item})
    assert r.structuredContent["cards"][0]["decide_here"] is True and str(item) in codes(r)


# ---------------------------------------------------------------- H2
def _card_record(S, item, decision, record, app="claude-ai"):
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item}, app=app)
    return call(S, "resolve_review_card", {"item_id": item, "decision": decision, "record": record,
                                           "project": "Alpha", "tap": codes(r)[str(item)]}, app=app)


def test_card_remove_is_refused_for_callers_that_may_not_delete(backend, claims_ui, monkeypatch):
    """The audit's reproduction: OAuth-style scopes (read, write) removed a record from a card,
    though a delete with the same scopes is refused."""
    from flowstate import server
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    monkeypatch.setattr(server, "_caller", lambda: ("oauth-app", ["flowstate:read", "flowstate:write"]))
    r, body = _card_record(S, item, "remove_record", f"change:{cid}")
    assert body["error"] == "not_allowed_remotely", body
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    assert str(item) in codes(r)                           # a fresh code: Keep still works
    _r, body = call(S, "resolve_review_card", {"item_id": item, "decision": "keep_record",
                                               "record": f"change:{cid}", "project": "Alpha",
                                               "tap": codes(r)[str(item)]})
    assert body["now"] == "kept"


def test_card_remove_waits_for_the_page_or_phone_with_approvals_on(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    item, _pid, cid = _work(S, db)
    approvals.set_on(db, True)
    _r, body = _card_record(S, item, "remove_record", f"change:{cid}")
    assert body["error"] == "user_only" and "review page or your phone" in body["message"]
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == [(cid,)]
    approvals.set_on(db, False)
    _r, body = _card_record(S, item, "remove_record", f"change:{cid}")
    assert body["now"] == "removed"
    approvals.set_on(db, True)
    _r, body = _card_record(S, item, "restore_record", f"change:{cid}")   # Bring back too
    assert body["error"] == "user_only"
    assert _q(db, "SELECT id FROM changes WHERE id = ?", cid) == []


# ---------------------------------------------------------------- L15
def test_cards_carry_the_raw_time_for_the_readers_own_day(backend, claims_ui):
    from flowstate.conflict_card_ui import CARD_HTML as CONFLICT
    from flowstate.review_card_ui import CARD_HTML as REVIEW
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use Postgres for storage")
    r, _ = call(S, "show_review_card", {"project": "Alpha", "item_id": item})
    card = r.structuredContent["cards"][0]
    [(created,)] = _q(db, "SELECT created_at FROM review_items WHERE id = ?", item)
    assert card["when_utc"] == created[:19]
    assert "localDate(view.when_utc)" in REVIEW and "localDate(s.when_utc)" in CONFLICT
    assert review.get_item(db, item)["status"] == "pending"
