"""Cross-project links (Phase D1): shared components, validation, alerts with update todos,
"changed since you last looked" in the brief, linked content trimmed first, superseded
decisions. Real MCP client sessions on temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate.brief import BRIEF_CAP, build_brief


def run(coro):
    return asyncio.run(coro)


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


@pytest.fixture
def df(fresh_db):
    """The wire protocol case: owned by Atlas OS, used by Beacon VPN, ChatApp, Mesh."""
    from flowstate import server as S
    from flowstate.database import Database
    d = Database(fresh_db)
    for n in ("Atlas OS", "Beacon VPN", "ChatApp", "Mesh", "Helper AI", "FlowState", "Lonely"):
        d.create_project(n, f"{n} project")
    d.create_component(1, "Wire Protocol")
    S.init_backend(fresh_db)
    assert S.backend.error is None
    yield S, fresh_db
    S.backend.tracker = None


def _use_df(S, project):
    r = call(S, "link", {"action": "add", "project": project, "link_type": "uses",
                         "target_project": "Atlas OS", "target_component": "Wire Protocol"})
    assert not r.isError, r.content[0].text
    return _j(r)["link"]


def test_link_validation_and_listing(df):
    S, db = df
    link = _use_df(S, "Beacon VPN")
    assert link["from"] == {"type": "project", "id": 2, "project": "Beacon VPN", "label": "Beacon VPN"}
    assert link["to"]["label"] == "Wire Protocol" and link["to"]["project"] == "Atlas OS"
    again = _j(call(S, "link", {"action": "add", "project": "Beacon VPN", "link_type": "uses",
                                "target_project": "Atlas OS",
                                "target_component": "wire protocol"}))
    assert again["existing"] is True and again["link"]["id"] == link["id"]   # no duplicate

    bad_comp = call(S, "link", {"action": "add", "project": "Beacon VPN", "link_type": "uses",
                                "target_project": "Atlas OS", "target_component": "Nope"})
    assert bad_comp.isError and _j(bad_comp)["error"] == "component_not_found"
    assert "Wire Protocol" in _j(bad_comp)["components"]
    bad_proj = call(S, "link", {"action": "add", "project": "Beacon VPN", "target_project": "Nowhere"})
    assert _j(bad_proj)["error"] == "project_not_found"
    no_proj = call(S, "link", {"action": "add", "target_project": "Atlas OS"})
    assert _j(no_proj)["error"] == "project_required"
    self_link = call(S, "link", {"action": "add", "project": "Beacon VPN", "target_project": "Beacon VPN"})
    assert _j(self_link)["error"] == "self_link"
    missing = call(S, "link", {"action": "add", "project": "Beacon VPN", "target_type": "todo",
                               "target_id": 999})
    assert _j(missing)["error"] == "record_not_found"
    wrong_owner = call(S, "link", {"action": "add", "project": "Beacon VPN", "item_type": "component",
                                   "item_id": 1, "target_project": "Mesh"})
    assert _j(wrong_owner)["error"] == "project_mismatch"
    bad_type = call(S, "link", {"action": "add", "project": "Beacon VPN", "link_type": "explodes",
                                "target_project": "Mesh"})
    assert bad_type.isError and "not one of" in bad_type.content[0].text   # schema enum
    from flowstate import links as L
    with pytest.raises(L.LinkError) as e:
        L.add(db, 2, "explodes", target_project="Mesh")
    assert e.value.payload["error"] == "bad_link_type"
    with pytest.raises(L.LinkError) as e:   # internal types only between the right kinds
        L.add(db, 2, "supersedes", target_project="Mesh")
    assert e.value.payload["error"] == "bad_link_type"
    with pytest.raises(L.LinkError) as e:
        L.add(db, 2, "url", url="javascript:alert(1)")
    assert e.value.payload["error"] == "bad_url"
    with pytest.raises(L.LinkError) as e:
        L.add(db, 2, "url", url="https://user:pw@github.com/x")
    assert e.value.payload["error"] == "bad_url"

    lst = _j(call(S, "link", {"action": "list", "project": "Atlas OS"}))
    assert lst["count"] == 1 and lst["links"][0]["from"]["project"] == "Beacon VPN"
    # Remove: only from a project at one end; by description works too.
    stranger = call(S, "link", {"action": "remove", "project": "Mesh", "link_id": link["id"]})
    assert _j(stranger)["error"] == "project_mismatch"
    gone = _j(call(S, "link", {"action": "remove", "project": "Beacon VPN", "target_project":
                               "Atlas OS", "target_component": "Wire Protocol"}))
    assert gone["removed"] == link["id"]
    assert _j(call(S, "link", {"action": "list", "project": "Beacon VPN"}))["count"] == 0
    kept = _q(db, "SELECT removed_at FROM record_links WHERE id = ?", link["id"])
    assert kept[0][0]   # soft delete: the row stays


def test_change_on_shared_component_alerts_each_user_once(df):
    S, db = df
    for p in ("Beacon VPN", "ChatApp", "Mesh"):
        _use_df(S, p)
    for p in ("Beacon VPN", "ChatApp", "Mesh", "Atlas OS"):
        call(S, "start_here", {"project": p})    # everyone has looked once

    r = _j(call(S, "log_work", {"project": "Atlas OS", "kind": "change",
                                "summary": "Frame header grew to 16 bytes",
                                "component": "Wire Protocol"}))
    assert sorted(a["project"] for a in r["alerted_projects"]) == ["Beacon VPN", "ChatApp", "Mesh"]
    todos = _q(db, "SELECT project_id, title FROM todos ORDER BY project_id")
    assert todos == [(2, "Wire Protocol changed: check Beacon VPN"),
                     (3, "Wire Protocol changed: check ChatApp"),
                     (4, "Wire Protocol changed: check Mesh")]
    assert not _q(db, "SELECT 1 FROM todos WHERE project_id IN (1, 5, 6, 7)")

    # A second change before anyone looks: alert noted, no duplicate task.
    call(S, "log_work", {"project": "Atlas OS", "kind": "decision",
                         "summary": "Keep CRC32 in the trailer", "component": "Wire Protocol"})
    assert _q(db, "SELECT COUNT(*) FROM todos")[0][0] == 3
    brief = call(S, "start_here", {"project": "Beacon VPN"}).content[0].text
    assert "CHANGED since you last looked: Wire Protocol (Atlas OS), 2 updates" in brief
    assert "Keep CRC32" in brief and "todo #" in brief
    assert "Wire Protocol changed: check Beacon VPN" in brief       # the task is one of its own todos
    again = call(S, "start_here", {"project": "Beacon VPN"}).content[0].text
    assert "CHANGED since you last looked" not in again       # seen now
    assert "uses Wire Protocol (Atlas OS)" in again

    # After looking, a new change reuses the still-open task instead of adding one.
    call(S, "log_work", {"project": "Atlas OS", "kind": "change", "summary": "v3 handshake",
                         "component": "Wire Protocol"})
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE project_id = 2")[0][0] == 1
    # Once the task is done, the next change makes a new one.
    tid = _q(db, "SELECT id FROM todos WHERE project_id = 2")[0][0]
    call(S, "todo", {"action": "update", "project": "Beacon VPN", "todo_id": tid, "status": "done"})
    call(S, "start_here", {"project": "Beacon VPN"})
    call(S, "log_work", {"project": "Atlas OS", "kind": "change", "summary": "v4",
                         "component": "Wire Protocol"})
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE project_id = 2")[0][0] == 2


def test_user_project_logs_on_shared_component_by_name(df):
    S, db = df
    _use_df(S, "Beacon VPN")
    _use_df(S, "Mesh")
    r = _j(call(S, "log_work", {"project": "Beacon VPN", "kind": "change",
                                "summary": "Added padding field", "component": "Wire Protocol"}))
    assert sorted(a["project"] for a in r["alerted_projects"]) == ["Atlas OS", "Mesh"]
    assert not _q(db, "SELECT 1 FROM components WHERE project_id = 2")  # no copy made in Beacon VPN
    assert _q(db, "SELECT component_id FROM changes")[0][0] == 1
    # Older tool with an explicit id: allowed for a linked project, refused otherwise.
    ok = call(S, "log_change", {"project": "Beacon VPN", "component_id": 1, "field_name": "MTU"})
    assert not ok.isError and _j(ok)["alerted_projects"]
    refused = call(S, "log_change", {"project": "Lonely", "component_id": 1, "field_name": "x"})
    assert _j(refused)["error"] == "project_mismatch"
    upd = call(S, "update_component", {"project": "Beacon VPN", "component_id": 1, "name": "x"})
    assert _j(upd)["error"] == "project_mismatch"     # sharing doesn't let others rename it


def test_project_links_and_same_as(df):
    S, db = df
    from flowstate.database import Database
    Database(db).create_component(6, "Helper client")
    Database(db).create_component(5, "Helper core")
    call(S, "link", {"action": "add", "project": "FlowState", "link_type": "depends_on",
                     "target_project": "Helper AI"})
    call(S, "link", {"action": "add", "project": "FlowState", "link_type": "same_as",
                     "component": "Helper client", "target_project": "Helper AI",
                     "target_component": "Helper core"})
    brief = call(S, "start_here", {"project": "FlowState"}).content[0].text
    assert "depends on project Helper AI" in brief and "same as Helper core (Helper AI)" in brief
    # same_as works both ways for alerts
    r = _j(call(S, "log_work", {"project": "Helper AI", "kind": "change", "summary": "new API",
                                "component": "Helper core"}))
    assert [a["project"] for a in r["alerted_projects"]] == ["FlowState"]
    lu = call(S, "start_here", {"project": "Helper AI"}).content[0].text
    assert "Helper core is used by FlowState" in lu


def test_linked_content_is_trimmed_first(df):
    S, db = df
    from flowstate.database import Database
    d = Database(db)
    for i in range(40):
        d.add_todo(2, f"VPN task {i} " + "x" * 60, None, "high")
        d.log_learning(2, f"VPN lesson {i} " + "y" * 120, "gotcha")
    comp = d.create_component(2, "Tunnel")
    for i in range(10):
        d.log_change(comp.id, f"tunnel change {i}", None, "z" * 80, "code", "r" * 80)
    project = {"id": 2, "name": "Beacon VPN", "status": "active", "description": "Beacon VPN project"}
    before = build_brief(db, project).splitlines()
    for i in range(12):
        # changes first, then the link: no alert todos, so the own sections stay identical
        c = d.create_component(1, f"Shared piece {i}")
        call(S, "log_work", {"project": "Atlas OS", "kind": "change",
                             "summary": "changed " + "w" * 80, "component": c.name})
        call(S, "link", {"action": "add", "project": "Beacon VPN", "target_project": "Atlas OS",
                         "target_component": c.name})
    after = build_brief(db, project)
    assert len(after) <= BRIEF_CAP
    lines = after.splitlines()
    own = [ln for ln in before if ln and not ln.startswith("More detail")]
    assert [ln for ln in lines if ln in own] == own      # every own line kept, same order
    assert lines[-1].startswith("More detail")
    # with room to spare, linked items appear
    small = {"id": 4, "name": "Mesh", "status": "active"}
    call(S, "link", {"action": "add", "project": "Mesh", "target_project": "Atlas OS",
                     "target_component": "Shared piece 0"})
    assert "Linked projects:" in build_brief(db, small)


def test_superseded_decision_shows_only_current(df):
    S, db = df
    old = _j(call(S, "log_work", {"project": "Mesh", "kind": "decision",
                                  "summary": "Use UDP multicast for discovery"}))["record"]["id"]
    bad = call(S, "log_work", {"project": "Mesh", "kind": "decision", "summary": "x",
                               "replaces": 999})
    assert _j(bad)["error"] == "record_not_found"
    new = _j(call(S, "log_work", {"project": "Mesh", "kind": "decision",
                                  "summary": "Use mDNS for discovery", "replaces": old}))
    assert new["replaces"] == old
    brief = call(S, "start_here", {"project": "Mesh"}).content[0].text
    assert "Use mDNS for discovery (replaces #%d)" % old in brief
    assert "UDP multicast" not in brief
    links = _j(call(S, "link", {"action": "list", "project": "Mesh"}))["links"]
    assert [l["link_type"] for l in links] == ["supersedes"]


def test_read_only_apps_cannot_link():
    from flowstate.remote_auth import refusal
    assert refusal("link", {"action": "add"}, ["flowstate:read"])[0] == "read_only_client"
    assert refusal("link", {"action": "list"}, ["flowstate:read"]) is None


def test_alert_shows_even_when_the_brief_is_full(df):
    S, db = df
    from flowstate.database import Database
    d = Database(db)
    for i in range(40):
        d.add_todo(2, f"VPN task {i} " + "x" * 60, None, "critical")
        d.log_learning(2, f"VPN lesson {i} " + "y" * 120, "gotcha")
    _use_df(S, "Beacon VPN")
    call(S, "start_here", {"project": "Beacon VPN"})
    call(S, "log_work", {"project": "Atlas OS", "kind": "change", "summary": "header v2",
                         "component": "Wire Protocol"})
    brief = call(S, "start_here", {"project": "Beacon VPN"}).content[0].text
    assert len(brief) <= BRIEF_CAP
    head = brief.split("Open todos")[0]
    assert "CHANGED since you last looked: Wire Protocol (Atlas OS), 1 update" in head
