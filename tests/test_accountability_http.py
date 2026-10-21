"""Accountability over HTTP: per-app LAN keys (identity from the key, read-only, revoked ->
401), the shared LAN token still working (recorded as a legacy shared token), remote tokens
and OAuth still working on the public listener (and LAN keys refused there), model and
workspace headers. Real server subprocesses on 127.0.0.1 random ports, temp DB and temp
settings folder; the same harness as test_remote_auth.py."""

import asyncio
import json
import sqlite3

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from test_remote_auth import MASTER, PASS, Server, _mcp_status, _oauth, _store


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _calls(base, token, calls, headers=None, name="Self Named App"):
    async def go():
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {token}", **(headers or {})},
                                 timeout=30)
        async with http, streamable_http_client(base + "/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w, client_info=types.Implementation(
                    name=name, version="1")) as s:
                await s.initialize()
                out = []
                for n, a in calls:
                    res = await s.call_tool(n, a)
                    out.append((res.isError, res.content[0].text))
                return out
    return asyncio.run(go())


def _last_activity(db, tool):
    return _q(db, "SELECT app, model, machine, identity_source, caller FROM activity_log "
                  "WHERE tool = ? ORDER BY id DESC LIMIT 1", tool)[0]


def _add_key(db, name, read_only=False, workspace=None):
    from flowstate import app_keys
    return app_keys.add(_store(db), name, read_only, workspace)


@pytest.fixture
def lan(seeded_db):
    s = Server(seeded_db, public=False)
    yield s
    s.stop()


@pytest.fixture
def public(seeded_db):
    _store(seeded_db).set_passphrase(PASS)
    s = Server(seeded_db)
    yield s
    s.stop()


def test_lan_app_key_identity_comes_from_the_key(lan, seeded_db):
    kid, key = _add_key(seeded_db, "Cursor laptop", workspace="laptop-ws")
    out = _calls(lan.base, key, [
        ("todo", {"action": "update", "todo_id": 1, "status": "in_progress", "project": "Alpha"}),
        ("log_work", {"project": "Alpha", "kind": "change", "summary": "via key",
                      "model": "arg-model"}),
        ("git", {"action": "status"})],
        headers={"X-FlowState-Model": "header-model", "X-FlowState-Workspace": "claimed-ws"},
        name="I Am Someone Else")
    assert not out[0][0] and not out[1][0], out
    assert out[2][0] and json.loads(out[2][1])["error"] == "not_allowed_remotely"
    assert _q(seeded_db, "SELECT app, model, machine, identity_source FROM record_history") == \
        [("Cursor laptop", "header-model", "laptop-ws", "app_key"),     # the edit
         ("Cursor laptop", "arg-model", "laptop-ws", "app_key")]        # the change it created
    assert _last_activity(seeded_db, "log_work") == \
        ("Cursor laptop", "arg-model", "laptop-ws", "app_key", kid)


def test_lan_key_without_workspace_uses_the_header(lan, seeded_db):
    _, key = _add_key(seeded_db, "Phone")
    _calls(lan.base, key, [("rules", {"action": "add", "project": "Alpha", "rule": "Be kind"})],
           headers={"X-FlowState-Workspace": "phone-ws"})
    assert _q(seeded_db, "SELECT app, model, machine, identity_source FROM project_rules") == \
        [("Phone", None, "phone-ws", "app_key")]


def test_read_only_key_and_revoked_key(lan, seeded_db, monkeypatch):
    from flowstate import cli
    ro_id, ro = _add_key(seeded_db, "Reader", read_only=True)
    rw_id, rw = _add_key(seeded_db, "Writer")
    out = _calls(lan.base, ro, [("start_here", {"project": "Alpha"}),
                                ("history", {"action": "who"}),
                                ("log_work", {"project": "Alpha", "kind": "todo", "summary": "no"})])
    assert not out[0][0] and not out[1][0]
    assert out[2][0] and json.loads(out[2][1])["error"] == "read_only_client"
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(_store(seeded_db).path))
    assert cli.main(["keys", "revoke", "--id", ro_id]) == 0
    assert _mcp_status(lan.base, ro).status_code == 401              # at once
    assert _mcp_status(lan.base, rw).status_code != 401              # the other still works


def test_shared_lan_token_still_works_as_legacy(lan, seeded_db):
    assert _mcp_status(lan.base).status_code == 401
    assert _mcp_status(lan.base, "wrong-token-wrong-token").status_code == 401
    assert httpx.get(lan.base + "/health").status_code == 200
    out = _calls(lan.base, MASTER, [
        ("todo", {"action": "update", "todo_id": 1, "status": "done", "project": "Alpha"}),
        ("git", {"action": "status"})], headers={"X-FlowState-Workspace": "desk"})
    assert not out[0][0], out
    assert "not_allowed_remotely" not in out[1][1]                   # owner token: as before
    assert _q(seeded_db, "SELECT app, model, machine, identity_source FROM record_history") == \
        [("Self Named App", None, "desk", "shared_token")]
    who = json.loads(_calls(lan.base, MASTER, [("history", {"action": "who"})])[0][1])
    assert who["rows"][0]["identity"] == "shared token (legacy)"


def test_remote_token_on_lan_as_before(lan, seeded_db):
    _, tok = _store(seeded_db).add_token_client("Old Mac", ["flowstate:read", "flowstate:write"])
    out = _calls(lan.base, tok, [("log_work", {"project": "Alpha", "kind": "todo", "summary": "t"})])
    assert not out[0][0], out
    assert _last_activity(seeded_db, "log_work")[:4] == ("Old Mac", None, "Old Mac", "remote_token")


def test_public_listener_refuses_lan_keys_and_keeps_oauth_and_tokens(public, seeded_db):
    _, key = _add_key(seeded_db, "LAN only")
    assert _mcp_status(public.base, key).status_code == 401
    assert _mcp_status(public.base, MASTER).status_code == 401       # unchanged
    _, tok = _store(seeded_db).add_token_client("Grok", ["flowstate:read", "flowstate:write"])
    out = _calls(public.base, tok, [("log_work", {"project": "Alpha", "kind": "todo",
                                                  "summary": "remote"})],
                 headers={"X-FlowState-Model": "remote-model"})
    assert not out[0][0], out
    assert _last_activity(seeded_db, "log_work")[:4] == ("Grok", "remote-model", "Grok",
                                                          "remote_token")
    oauth, cid, *_ = _oauth(public.base, access="write")
    out = _calls(public.base, oauth["access_token"],
                 [("handoff", {"action": "leave", "project": "Alpha", "where_i_was": "w",
                               "next_step": "n"})])
    assert not out[0][0], out
    assert _q(seeded_db, "SELECT app, machine, identity_source FROM handoff_notes") == \
        [("Test app", "Test app", "oauth_client")]
