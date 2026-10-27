"""Task 65: the in-chat cards through the relay, end to end (the fake relay of
test_relay_integration.py: SNI routing and byte splicing only; the agent terminates TLS and
feeds FlowState's own HTTP app; OAuth 2.1 + PKCE + passphrase consent).

A connector that says it shows MCP Apps gets the cards only once the user put its OAuth app's
own name on the card sign-in list (task 55); until then it gets the text, the phone sentence
and how the user turns cards on. Once on: the card tools (resolve hidden from the model), the
ui:// resource, structuredContent and the one-use tap codes in _meta all pass through the relay.
A tap with the code counts; a replayed, expired, forged or missing code doesn't; user-only
items have no buttons; the AI's own manage_review approve stays phone only. The relay only
ever carries ciphertext. Temp databases and sandboxed folders only."""

import json

import pytest

pytest.importorskip("cryptography")

from flowstate import approvals, card_taps, review  # noqa: E402
from flowstate.conflict_card_ui import UI_URI as CONFLICT_UI  # noqa: E402
from flowstate.review_card_ui import UI_URI as REVIEW_UI  # noqa: E402
from test_relay_integration import MCP_HEADERS, _oauth, world  # noqa: E402,F401

UI = {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}
CARD_TOOLS = {"show_conflict_card", "resolve_conflict_card", "show_review_card",
              "resolve_review_card"}


class Mcp:
    """One MCP session over the relay (streamable HTTP, SSE answers)."""

    def __init__(self, c, token, app, caps=None):
        self.c, self.token, self.n = c, token, 1
        msg = self._send("initialize", {"protocolVersion": "2025-06-18", "capabilities": caps or {},
                                        "clientInfo": {"name": app, "version": "0.1.0"}})
        self.sid = msg[1]["mcp-session-id"]
        s, _, _ = self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        assert s == 202

    def _post(self, msg):
        h = dict(MCP_HEADERS, Authorization=f"Bearer {self.token}")
        if getattr(self, "sid", None):
            h.update({"Mcp-Session-Id": self.sid, "Mcp-Protocol-Version": "2025-06-18"})
        return self.c.req("POST", "/mcp", js=msg, headers=h)

    def _send(self, method, params):
        self.n += 1
        s, h, body = self._post({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
        assert s == 200, body
        text = body.decode()
        if text.lstrip().startswith("{"):
            msgs = [json.loads(text)]
        else:
            msgs = [json.loads(line[5:]) for line in text.splitlines() if line.startswith("data:")]
        msg = next(m for m in msgs if m.get("id") == self.n)
        assert "result" in msg, msg
        return msg["result"], h, body

    def tools(self) -> dict:
        return {t["name"]: t for t in self._send("tools/list", {})[0]["tools"]}

    def read(self, uri) -> dict:
        return self._send("resources/read", {"uri": uri})[0]

    def raw(self, name, args) -> dict:
        return self._send("tools/call", {"name": name, "arguments": args})[0]

    def call(self, name, args) -> dict:
        r = self.raw(name, args)
        try:
            return json.loads(r["content"][0]["text"])
        except ValueError:
            return {"text": r["content"][0]["text"]}


def status(db, item):
    return review.get_item(db, item)["status"]


def replayed(body) -> bool:
    """A repeated tap decides nothing: refused, or the 2-minute duplicate guard's answer
    (receipts.py: "already saved", nothing saved again)."""
    return body.get("error") in ("not_a_card_tap", "already_resolved", "already_decided") or \
        (body.get("already_saved") is True and body["receipt"]["saved"] == [])


def taps(r) -> dict:
    return (r.get("_meta") or {}).get(card_taps.META_KEY) or {}


def test_cards_through_the_relay(world, seeded_db, monkeypatch):
    c, relay = world["client"], world["relay"]
    card_taps.clear()
    connector, _cid = _oauth(c)                 # the user's connector (OAuth app "Relay test app")
    other, _ = _oauth(c)                        # another app: queues the items
    cursor = Mcp(c, other["access_token"], "Cursor")
    host = Mcp(c, connector["access_token"], "claude-ai", UI)

    a = cursor.call("log_work", {"project": "Alpha", "kind": "decision",
                                 "summary": "Use JWT tokens for API auth"})["record"]["id"]
    b = cursor.call("log_work", {"project": "Alpha", "kind": "decision",
                                 "summary": "Use session cookies for API auth"})["record"]["id"]
    dis = cursor.call("manage_links", {"action": "flag_conflict", "project": "Alpha",
                                       "topic": "API sessions", "item_id": a, "target_id": b})["flagged"]
    sug = cursor.call("log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                   "summary": "Oat milk sells out by noon"})["queued_for_review"]

    # -- not on the card list yet (task 55's default): text, no card tools, how to turn them on
    assert not CARD_TOOLS & set(host.tools())
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "show_card" not in listed and listed["conflict_cards"][0]["review_item_id"] == dis
    assert 'flowstate approvals cards add --sign-in "relay test app"' in listed["cards_off"]
    assert "cards_off" not in cursor.call("manage_review", {"action": "list"})   # no MCP Apps
    refused = host.call("resolve_conflict_card", {"item_id": dis, "keep": "a", "project": "Alpha",
                                                  "tap": "x" * 32})
    assert refused["error"] == "not_a_card_tap" and status(seeded_db, dis) == "pending"

    # -- the user turns cards on for this sign-in (their own step: CLI or review page)
    approvals.change_signins(seeded_db, "card", "Relay test app", True)
    tools = host.tools()
    assert CARD_TOOLS <= set(tools)
    assert tools["resolve_conflict_card"]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools["resolve_review_card"]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools["show_conflict_card"]["_meta"]["ui"]["resourceUri"] == CONFLICT_UI
    for uri in (CONFLICT_UI, REVIEW_UI):
        page = host.read(uri)["contents"][0]
        assert "<!DOCTYPE html>" in page["text"] and page["mimeType"].startswith("text/html")
    assert not CARD_TOOLS & set(cursor.tools())   # another sign-in: still none
    listed = host.call("manage_review", {"action": "list", "project": "Alpha"})
    assert "cards_off" not in listed
    assert listed["show_card"] == [f'show_conflict_card(project="Alpha", item_id={dis})',
                                   f'show_review_card(project="Alpha", item_id={sug})']

    # -- the AI's own approve stays phone only (chat approvals off, the default)
    ai = host.call("manage_review", {"action": "approve", "item_id": sug, "project": "Alpha",
                                     "user_request": "The user said: yes, approve the oat milk one"})
    assert ai["error"] == "phone_only" and status(seeded_db, sug) == "pending"

    # -- the disagreement card: data and code pass through; the code only in _meta
    shown = host.raw("show_conflict_card", {"project": "Alpha", "item_id": dis})
    assert shown["structuredContent"]["cards"][0]["item_id"] == dis
    code = taps(shown)[str(dis)]
    assert code not in json.dumps(shown["content"]) + json.dumps(shown["structuredContent"])
    tap = {"item_id": dis, "keep": "a", "project": "Alpha"}
    for bad in ({}, {"tap": ""}, {"tap": "made-up-code-0123456789"}):
        assert host.call("resolve_conflict_card", {**tap, **bad})["error"] == "not_a_card_tap"
    assert cursor.call("resolve_conflict_card", {**tap, "tap": code})["error"] == "not_a_card_tap"
    saved = host.call("resolve_conflict_card", {**tap, "tap": code})
    assert saved["resolved"] == dis and status(seeded_db, dis) == "approved", saved
    again = host.call("resolve_conflict_card", {**tap, "tap": code})   # the same tap again
    assert replayed(again) and status(seeded_db, dis) == "approved"
    other = host.call("resolve_conflict_card", {**tap, "keep": "b", "tap": code})   # used up
    assert other["error"] in ("not_a_card_tap", "already_resolved")

    # -- the review card: an expired code is refused, a fresh one counts once
    shown = host.raw("show_review_card", {"project": "Alpha", "item_id": sug})
    old = taps(shown)[str(sug)]
    now = card_taps._clock()
    monkeypatch.setattr(card_taps, "_clock", lambda: now + card_taps.TTL + 1)
    expired = host.call("resolve_review_card", {"item_id": sug, "decision": "approve",
                                                "project": "Alpha", "tap": old})
    assert expired["error"] == "not_a_card_tap" and status(seeded_db, sug) == "pending"
    monkeypatch.setattr(card_taps, "_clock", lambda: now)
    fresh = taps(host.raw("show_review_card", {"project": "Alpha", "item_id": sug}))[str(sug)]
    tap = {"item_id": sug, "decision": "reject", "project": "Alpha", "tap": fresh}
    done = host.call("resolve_review_card", tap)
    assert done["resolved"] == sug and status(seeded_db, sug) == "rejected", done
    assert replayed(host.call("resolve_review_card", tap))
    used = host.call("resolve_review_card", {**tap, "decision": "approve"})
    assert used["error"] in ("not_a_card_tap", "already_decided")
    assert status(seeded_db, sug) == "rejected"
    logged = review.get_item(seeded_db, sug)["decided_by"]
    assert logged == "card (claude-ai, signed in as Relay test app)"

    # -- an item only the user decides: no buttons, no code, refused
    approvals.set_on(seeded_db, True)
    only = review.add_item(seeded_db, 1, "setting", "Turn review off for Alpha", None,
                           {"scope": "project", "mode": "off"}, "Claude")
    shown = host.raw("show_review_card", {"project": "Alpha", "item_id": only})
    assert shown["structuredContent"]["cards"][0]["decide_here"] is False and not taps(shown)
    assert "127.0.0.1" not in json.dumps(shown)   # not on this computer: no local link
    no = host.call("resolve_review_card", {"item_id": only, "decision": "approve",
                                           "project": "Alpha", "tap": fresh})
    assert no["error"] == "user_only" and status(seeded_db, only) == "pending"

    # -- the relay carried only ciphertext: no code, no card, no tool names
    seen = bytes(relay.captured)
    for secret in (code, old, fresh, "show_conflict_card", "resolve_review_card", "API sessions",
                   "<!DOCTYPE html>", "flowstate/taps"):
        assert secret.encode() not in seen, secret
