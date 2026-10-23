"""Queued deletes show the rows the database will remove, not the agent's own wording.
The agent's text, when one was stored, is only the secondary line "AI's note". Temp DBs only."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.testclient import TestClient

from flowstate import approvals, deletes
from flowstate.approve_page import view
from flowstate.review_page import HOST, Page, create_app
from flowstate.review_phone import items as phone_items


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _seed(S):
    assert not call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                    "summary": "Use JWT tokens for API auth",
                                    "component": "Auth"}).isError
    assert not call(S, "manage_todos", {"action": "add", "project": "Alpha",
                                        "title": "Rotate the session key",
                                        "priority": "high"}).isError


def test_a_queued_delete_names_the_rows_from_the_database(backend, seeded_db):
    S = backend
    _seed(S)
    approvals.set_on(seeded_db, True)
    out = _j(call(S, "manage_projects", {"action": "delete", "project": "Alpha"}))
    w = out["would_delete"]
    assert (w["record_type"], w["title"], w["project"]) == ("project", "Alpha", "Alpha")
    assert w["fields"].get("description") == "First project"
    names = {(c["type"], c["name"]) for c in w["children"]}
    assert ("component", "Core") in names
    assert ("component", "Auth") in names or ("component", "Sub") in names or any(
        c["type"] == "component" and c["name"] for c in w["children"])
    assert ("todo", "Alpha todo") in names
    assert ("todo", "Rotate the session key") in names
    assert ("decision", "Use JWT tokens for API auth") in names
    todo = next(c for c in w["children"] if c["name"] == "Rotate the session key")
    assert todo["fields"].get("priority") in ("high", "urgent")  # answers label stored "high" as "urgent"
    assert todo["project"] == "Alpha"
    assert w["with"]["todos"] >= 2 and w["with"]["decisions"] >= 1
    assert "children_more" not in w
    # the server's own summary is not repeated as the agent's note
    item = out["delete_requested"]
    summary, details = _q(seeded_db, "SELECT summary, details FROM review_items WHERE id = ?", item)[0]
    assert summary.startswith("Delete ") and details.startswith("Deletes ")
    assert "manage_history(action=\"undo\"" in details
    got = deletes.preview(seeded_db, {"id": item, "project_id": 1, "summary": summary,
                                      "details": details,
                                      "payload": _q(seeded_db, "SELECT payload FROM review_items WHERE id = ?",
                                                    item)[0][0]})
    assert got["ai_note"] is None
    assert got["headline"] == "These records will be deleted from Alpha"
    assert "Rotate the session key" in got["plain"] and "AI's note" not in got["plain"]


def test_agent_wording_is_only_a_labeled_note(backend, seeded_db):
    S = backend
    _seed(S)
    approvals.set_on(seeded_db, True)
    out = _j(call(S, "manage_projects", {"action": "delete", "project": "Alpha"}))
    item = out["delete_requested"]
    note = 'Please wipe <script>alert(1)</script> the whole project'
    _q_write(seeded_db, "UPDATE review_items SET summary = ? WHERE id = ?", (note, item))
    row = _q(seeded_db, "SELECT id, project_id, kind, summary, details, payload FROM review_items "
                        "WHERE id = ?", item)[0]
    stored = {"id": row[0], "project_id": row[1], "kind": row[2], "summary": row[3],
              "details": row[4], "payload": row[5], "project": "Alpha", "status": "pending"}
    # the stored request is unchanged apart from the summary we tampered
    assert stored["details"].startswith("Deletes ")
    got = deletes.preview(seeded_db, stored)
    assert got["headline"] == "These records will be deleted from Alpha"
    assert got["ai_note"] == "Please wipe <script>alert(1)</script> the whole project"
    assert got["plain"].startswith("These records will be deleted from Alpha")
    assert got["plain"].rstrip().endswith("AI's note: " + got["ai_note"])
    assert got["plain"].index("Rotate the session key") < got["plain"].index("AI's note")
    assert note not in got["headline"]

    listed = _j(call(S, "manage_review", {"action": "list", "project": "Alpha"}))
    line = next(x for x in listed["items"] if x.startswith(f"#{item} "))
    assert line.index("These records will be deleted from Alpha") < line.index("AI's note")
    assert "Rotate the session key" in line and "Use JWT tokens for API auth" in line
    assert "Core" in line
    one = _j(call(S, "manage_review", {"action": "list", "project": "Alpha", "item_id": item}))
    assert one["item"]["summary"] == got["headline"]
    assert one["item"]["ai_note"] == got["ai_note"]
    assert one["item"]["would_delete"]["title"] == "Alpha"
    child_names = [c["name"] for c in one["item"]["would_delete"]["children"]]
    assert "Rotate the session key" in child_names and "Alpha todo" in child_names
    # the database row itself still holds the agent's sentence; only the reply was replaced
    assert _q(seeded_db, "SELECT summary FROM review_items WHERE id = ?", item)[0][0] == note

    page = Page(seeded_db, 8770)
    client = TestClient(create_app(page), base_url=f"http://{HOST}:8770", follow_redirects=False)
    client.get("/?key=" + page.launch_key)
    html = client.get("/").text
    assert "These records will be deleted from Alpha" in html
    assert "Rotate the session key" in html and "Use JWT tokens for API auth" in html
    assert "Approve: delete it" in html
    assert "<script>alert(1)</script>" not in html
    assert "AI's note:" in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert html.index("class='headline'>These records") < html.index("AI's note")

    phone = next(v for v in phone_items(seeded_db) if v["id"] == item)
    assert phone["question"] == "These records will be deleted from Alpha"
    assert "Rotate the session key" in phone["details"]
    assert phone["details"].index("AI's note") > phone["details"].index("Rotate the session key")
    shown = view(seeded_db, item)
    assert shown["question"] == phone["question"]
    assert note not in shown["question"] and note not in shown["summary"]


def test_a_single_record_lists_its_key_fields_and_no_invented_children(backend, seeded_db):
    S = backend
    approvals.set_on(seeded_db, True)
    out = _j(call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                       "record_type": "todo", "record_id": 1}))
    w = out["would_delete"]
    assert w["title"] == "Alpha todo" and w["project"] == "Alpha" and w["children"] == []
    assert w["fields"].get("priority") == "urgent"  # stored "high", shown by its public name
    assert "with" in w and "todos" not in w["with"]
    got = deletes.preview(seeded_db, _item(seeded_db, out["delete_requested"]))
    assert got["headline"] == "This record will be deleted from Alpha"
    assert 'Todo "Alpha todo" in Alpha' in got["root_line"]
    assert got["ai_note"] is None


def _q_write(db, sql, args):
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _item(db, item):
    row = _q(db, "SELECT id, project_id, summary, details, payload FROM review_items WHERE id = ?",
             item)[0]
    return {"id": row[0], "project_id": row[1], "summary": row[2], "details": row[3],
            "payload": row[4]}


def test_component_delete_lists_its_children_by_name(backend, seeded_db):
    S = backend
    assert not call(S, "log_work", {"project": "Alpha", "kind": "change", "summary": "shared change",
                                    "component": "Core"}).isError
    approvals.set_on(seeded_db, True)
    out = _j(call(S, "manage_projects", {"action": "delete_component", "project": "Alpha",
                                         "component_id": 1}))
    w = out["would_delete"]
    assert w["record_type"] == "component" and w["title"] == "Core"
    kinds = {c["type"] for c in w["children"]}
    assert "change" in kinds and "problem" in kinds
    assert "Alpha bug" in [c["name"] for c in w["children"]]
    assert all(c["type"] != "todo" for c in w["children"])
    assert w["with"].get("problems") == 1
