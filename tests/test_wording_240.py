"""To-do priorities are shown by one set of names everywhere (2.4.0 wording fix): critical,
urgent, normal, low. "high" and "medium" are still accepted and are what the database keeps
(its v1 CHECK constraint), but no answer shows them any more."""

import asyncio
import json

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import priority


def call(S, name, args):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            return (await c.call_tool(name, args)).content[0].text
    return asyncio.run(go())


def test_every_answer_uses_the_new_names(backend):
    S = backend
    out = [call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Urgent one", "priority": "urgent"}),
           call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Plain one", "priority": "normal"}),
           call(S, "manage_todos", {"action": "add", "project": "Alpha", "title": "Old name", "priority": "high"}),
           call(S, "manage_todos", {"action": "list", "project": "Alpha"}),
           call(S, "manage_todos", {"action": "next", "project": "Alpha"}),
           call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1, "priority": "medium"})]
    joined = "\n".join(out)
    assert '"priority": "high"' not in joined and '"priority": "medium"' not in joined, joined
    assert '"priority": "urgent"' in out[0] and '"priority": "normal"' in out[1] and '"priority": "urgent"' in out[2]


def test_show_leaves_problem_severity_alone():
    row = {"todos": [{"priority": "high"}], "problems": [{"severity": "high"}]}
    assert priority.show(row) == {"todos": [{"priority": "urgent"}], "problems": [{"severity": "high"}]}


# ---------------------------------------------------------------- other wording fixes (2.4.0)
def test_an_empty_database_says_there_are_no_projects(fresh_db):
    from flowstate import server as S
    S.init_backend(fresh_db)
    try:
        text = call(S, "start_here", {})
    finally:
        S.backend.tracker = None
    assert "no projects yet" in text and not text.rstrip().endswith("Projects:"), text


def test_who_tells_two_windows_of_one_app_apart(backend, monkeypatch):
    from flowstate import identity
    S = backend
    call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Window one decides"})
    call(S, "manage_rules", {"action": "add", "project": "Alpha", "rule": "Never store card numbers."})
    monkeypatch.setattr(identity, "PROCESS_SESSION", "second-window")
    call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Window two decides"})
    call(S, "manage_todos", {"action": "claim", "project": "Alpha", "todo_id": 1})
    rows = json.loads(call(S, "manage_history", {"action": "who", "project": "Alpha"}))["rows"]
    by = {r["what"]: r.get("session") for r in rows}
    one = by[next(k for k in by if k.startswith("added hard rule"))]        # window one's
    decided = {v for k, v in by.items() if k.startswith("created decision #")}
    assert all(r["app"] == "Cursor" for r in rows) and len(decided) == 2 and one in decided, by
    assert by[next(k for k in by if k.startswith("claimed todo #1"))] not in (None, one), by


def test_oversized_details_are_refused_with_a_clear_message(backend):
    import sqlite3
    S = backend
    for suggested in (False, True):
        r = json.loads(call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "Huge note",
                                            "details": "x" * 120_000, "suggested": suggested}))
        assert r["error"] == "too_long" and "120,000" in r["message"] and "10,000" in r["message"], r
    ok = json.loads(call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "Long but fine",
                                         "details": "y" * 10_000}))
    assert "record" in ok, ok
    conn = sqlite3.connect(S.backend.db_path)
    assert conn.execute("SELECT COUNT(*) FROM learnings WHERE insight = 'Huge note'").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM review_items WHERE summary = 'Huge note'").fetchone() == (0,)
    conn.close()


def test_descriptions_say_what_is_refused(backend):
    from flowstate.tooldefs import ALL_TOOLS
    d = {t.name: t for t in ALL_TOOLS}
    assert "refused for HTTP app keys" in d["manage_variables"].description
    assert "refused for HTTP app keys" in d["manage_methods"].description
    assert "Required except for list and list_metrics" in \
        d["manage_skills"].inputSchema["properties"]["project"]["description"]
    r = json.loads(call(backend, "manage_skills", {"action": "get_state"}))
    assert r["error"] == "project_required"
