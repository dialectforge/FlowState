"""Through the relay, a write is credited to the app that sent it (its MCP client name), not
to the name of the sign-in it used, which may be shared or named after another app; without a
client name it is "Relay (unknown app)". Local callers are unchanged. Temp databases only."""

import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from flowstate import activity, identity, review
from flowstate import remote_auth as RA


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def http_calls(db, token, calls, client_name, relay=True):
    """Tool calls over FlowState's real HTTP app (remote_auth.Gate included), signed in with
    `token`; the relay listener with relay=True, else the LAN one."""
    from flowstate.http_app import create_http_app
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
         "Content-Type": "application/json"}

    def rpc(c, method, params, sid=None, id_=1):
        hh = dict(h, **({"Mcp-Session-Id": sid, "Mcp-Protocol-Version": "2025-06-18"} if sid else {}))
        msg = {"jsonrpc": "2.0", "method": method, "params": params, **({"id": id_} if id_ else {})}
        return c.post("/mcp", content=json.dumps(msg), headers=hh)

    out = []
    with TestClient(create_http_app("t" * 32, db_path=db, relay=relay),
                    client=("203.0.113.9", 50123), base_url="http://127.0.0.1:8766") as c:
        r = rpc(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": client_name, "version": "1"}})
        assert r.status_code == 200, r.text
        sid = r.headers["mcp-session-id"]
        assert rpc(c, "notifications/initialized", {}, sid, None).status_code == 202
        for i, (name, args) in enumerate(calls, 2):
            r = rpc(c, "tools/call", {"name": name, "arguments": args}, sid, i)
            data = next(m for m in (json.loads(l[5:]) for l in r.text.splitlines() if l.startswith("data:"))
                        if m.get("id") == i and "result" in m)
            text = data["result"]["content"][0]["text"]
            try:
                out.append(json.loads(text))
            except ValueError:
                out.append(text)
    return out


@pytest.fixture
def cursor_token(backend):
    """A remote token the user once named after another app (as the docs' example does)."""
    _cid, tok = RA.AuthStore().add_token_client("Cursor", list(RA.SCOPES))
    yield tok
    backend.backend.tracker = None


def suggest(summary):
    return ("log_work", {"project": "Alpha", "kind": "decision", "summary": summary,
                         "suggested": True})


def queued(db, summary):
    return _q(db, "SELECT app, machine, identity_source FROM review_items WHERE summary = ?",
              summary)


def test_relay_write_carries_the_connecting_apps_name(backend, cursor_token):
    db = backend.backend.db_path
    out = http_calls(db, cursor_token, [suggest("Use Redis for caching")], "Grok Bot")
    assert "queued_for_review" in out[0], out[0]
    assert queued(db, "Use Redis for caching") == \
        [("Grok Bot", "relay (signed in as Cursor)", "remote_token")]


def test_relay_write_without_a_client_name_is_relay_unknown_app(backend, cursor_token):
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("Add a health check")], "")
    assert queued(db, "Add a health check") == \
        [("Relay (unknown app)", "relay (signed in as Cursor)", "remote_token")]
    assert "Cursor" not in queued(db, "Add a health check")[0][0]


def test_local_lan_caller_is_unchanged(backend, cursor_token):
    db = backend.backend.db_path
    http_calls(db, cursor_token, [suggest("Use Kafka for events")], "Grok Bot", relay=False)
    assert queued(db, "Use Kafka for events") == [("Cursor", "Cursor", "remote_token")]


def test_stdio_and_lan_resolve_rules_are_unchanged():
    class Tok:
        identity_source, client_name, workspace = "remote_token", "Phone", None
    assert identity.resolve("Claude Desktop").app == "Claude Desktop"
    assert identity.resolve("Grok", True, Tok(), {}).as_dict() == \
        {"app": "Phone", "model": None, "machine": "Phone", "identity_source": "remote_token"}
    assert identity.resolve("Grok", True, Tok(), {}, listener="public").app == "Phone"
    assert identity.resolve("Grok", True, Tok(), {}).credential is None
    relayed = identity.resolve("Grok", True, Tok(), {}, listener="relay")
    assert (relayed.app, relayed.machine, relayed.credential) == \
        ("Grok", "relay (signed in as Phone)", "Phone")
    assert identity.resolve("unknown", True, Tok(), {}, listener="relay").app == identity.RELAY_UNKNOWN
    assert "credential" not in relayed.as_dict()


def test_pausing_the_sign_in_name_still_stops_relay_writes(backend, cursor_token):
    db = backend.backend.db_path
    activity.set_paused(db, "Cursor", True, "test")
    out = http_calls(db, cursor_token, [suggest("Paused write")], "Grok Bot")
    assert out[0]["error"] == "paused_by_user"
    assert queued(db, "Paused write") == []


def test_an_app_cant_decide_its_own_item_by_sending_another_name():
    item = {"id": 7, "app": "Grok Bot", "machine": "relay (signed in as Cursor)",
            "summary": "Use Redis for caching", "details": None}
    renamed = identity.Identity("Some Other App", None, "relay (signed in as Cursor)",
                                "remote_token", "s1", "Cursor")
    refused = review.own_item_refusal(item, renamed, "Use Redis for caching")
    assert refused and refused["error"] == "own_item"
    other = identity.Identity("Some Other App", None, "relay (signed in as Phone)",
                              "remote_token", "s2", "Phone")
    assert review.own_item_refusal(item, other, "Use Redis for caching") is None
