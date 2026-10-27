"""Task 65, Part 0: Claude Desktop gets its in-chat cards again on stdio. Temp DBs only.

2.4.4 (task 46 / 55) gave cards only to apps on the card list, which started with claude-ai,
the name Claude Desktop's chat sends. Claude Desktop's Cowork (agent) mode sends
"local-agent-mode-<server name>" (local-agent-mode-flowstate with the setup script's config),
so every card was turned off there and only the text came back. The built-in list now has
"local-agent-mode-*" too; it matches the name an app sends on stdio only, never a sign-in."""

import asyncio
import json

import mcp.types as mtypes
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, card_taps
from flowstate.conflict_card_ui import UI_URI
from flowstate.conflict_cards import UI_EXT

UI = {UI_EXT: {"mimeTypes": ["text/html;profile=mcp-app"]}}
COWORK = "local-agent-mode-flowstate"
CARD_TOOLS = {"show_conflict_card", "resolve_conflict_card", "show_review_card",
              "resolve_review_card"}


@pytest.fixture
def claims_ui(monkeypatch):
    """Every client in this test sends the MCP Apps extension at initialize, as Claude Desktop
    does (the real handshake, not a stubbed client_has_ui)."""
    real = mtypes.ClientCapabilities
    monkeypatch.setattr(mtypes, "ClientCapabilities", lambda **kw: real(**kw, extensions=UI))
    card_taps.clear()


def session(S, app, fn):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=mtypes.Implementation(name=app, version="1.0.0")) as c:
            return await fn(c)
    return asyncio.run(go())


def call(S, name, args, app=COWORK):
    r = session(S, app, lambda c: c.call_tool(name, args))
    try:
        return r, json.loads(r.content[0].text)
    except ValueError:
        return r, r.content[0].text


def ok(res):
    r, body = res
    assert not r.isError, body
    return body


def disagreement(S):
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use JWT tokens for API auth"}, app="Cursor"))
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use session cookies for API auth"}, app="Cursor"))
    return ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
        "item_id": a["record"]["id"], "target_id": b["record"]["id"]}, app="Cursor"))["flagged"]


@pytest.mark.parametrize("app", [COWORK, "claude-ai", "Local-Agent-Mode-FlowState"])
def test_claude_desktop_gets_the_card_on_stdio(backend, seeded_db, claims_ui, app):
    S, db = backend, backend.backend.db_path

    async def listing(c):
        tools = (await c.list_tools()).tools
        body = await c.read_resource(UI_URI)
        return tools, body
    tools, body = session(S, app, listing)
    assert CARD_TOOLS <= {t.name for t in tools}
    meta = {t.name: t.model_dump(by_alias=True, exclude_none=True).get("_meta", {}).get("ui")
            for t in tools}
    assert meta["show_conflict_card"]["resourceUri"] == UI_URI
    assert meta["resolve_conflict_card"]["visibility"] == ["app"]   # hidden from the model
    assert "<!DOCTYPE html>" in body.contents[0].text

    item = disagreement(S)
    listed = ok(call(S, "manage_review", {"action": "list"}, app=app))
    want = f'show_conflict_card(project="Alpha", item_id={item})'
    assert listed["show_card"] == want                 # 35afb38: no pointer, text only

    r, _body = call(S, "show_conflict_card", {"project": "Alpha", "item_id": item}, app=app)
    assert not r.isError
    assert r.structuredContent["cards"][0]["item_id"] == item
    code = ((r.meta or {}).get(card_taps.META_KEY) or {}).get(str(item))
    assert code and code not in r.content[0].text + json.dumps(r.structuredContent)

    tap = {"item_id": item, "keep": "a", "project": "Alpha", "tap": code}
    saved = ok(call(S, "resolve_conflict_card", tap, app=app))
    assert saved["resolved"] == item
    again = call(S, "resolve_conflict_card", tap, app=app)[1]
    assert again.get("error") in ("not_a_card_tap", "already_resolved")
    assert approvals.card_allowed(db, app)


def test_the_cowork_pattern_is_narrow_and_the_users_to_change(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    for name in ("local-agent-mode-", "local-agent-mode", "xlocal-agent-mode-flowstate",
                 "claude", "claude-ai-2", "Claude Desktop"):
        assert not approvals.card_allowed(db, name), name
    approvals.change_card_clients(db, "local-agent-mode-*", False)
    assert not approvals.card_allowed(db, COWORK)
    assert not CARD_TOOLS & {t.name for t in session(S, COWORK, lambda c: c.list_tools()).tools}
    approvals.change_card_clients(db, "local-agent-mode-*", True)
    assert approvals.card_allowed(db, COWORK)
    # a short or bare "*" entry matches nothing (never "every app")
    for entry in ("*", "ab*", "abc*"):
        approvals.change_card_clients(db, entry, True)
    assert not approvals.card_allowed(db, "abcdef") and not approvals.card_allowed(db, "zzz")


def test_the_pattern_never_trusts_a_network_sign_in(backend):
    """task 55 / 57: over the network only the sign-in list the user made counts."""
    db = backend.backend.db_path
    assert not approvals.card_allowed_credential(db, COWORK, "oauth_client")
    assert not approvals.card_allowed_credential(db, "claude-ai", "oauth_client")
    approvals.change_signins(db, "card", "local-agent-mode-*", True)
    assert not approvals.card_allowed_credential(db, COWORK, "oauth_client")


@pytest.fixture
def relay_on(tmp_path, monkeypatch):
    """The relay is set up on this computer, with phone links (the pinned keys of the tests)."""
    from fsa_test_keys import make_key
    from flowstate.relay.store import RelayStore
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    _key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})


def test_an_app_without_cards_gets_the_text_and_the_phone_link(backend, seeded_db, relay_on):
    S = backend
    item = disagreement(S)
    listed = ok(call(S, "manage_review", {"action": "list"}, app="Cursor"))   # no MCP Apps
    assert "show_card" not in listed and "cards_off" not in listed
    assert listed["conflict_cards"][0]["review_item_id"] == item
    assert "API sessions" in listed["conflict_cards"][0]["text"]
    assert listed["phone"].startswith("https://flowstatemcp.com/review/")   # this computer
    r, _ = call(S, "show_conflict_card", {"project": "Alpha", "item_id": item}, app="Cursor")
    assert not (r.meta or {}).get(card_taps.META_KEY)                     # no tap codes


def test_an_unlisted_app_that_shows_cards_is_told_how_the_user_turns_them_on(backend, seeded_db,
                                                                             claims_ui):
    S = backend
    disagreement(S)
    listed = ok(call(S, "manage_review", {"action": "list"}, app="My Host"))
    assert "show_card" not in listed
    assert 'flowstate approvals cards add "my host"' in listed["cards_off"]
    assert "don't run it for them" in listed["cards_off"]
    assert "cards_off" not in ok(call(S, "manage_review", {"action": "list"}))   # listed: cards
    odd = ok(call(S, "manage_review", {"action": "list"}, app='x"; rm -rf ~ `y`'))
    assert '"; rm' not in odd["cards_off"] and "`y`" not in odd["cards_off"]
