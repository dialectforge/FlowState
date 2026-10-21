"""User-approved deletes (deletes.py): an AI's delete call only queues a request; only the
user's approval (manage_review approve with user_request, and a project's exact name)
deletes; reject keeps everything; undo puts back the record and every row removed with it,
exactly. Remote, OAuth, per-app LAN and read-only keys can neither ask nor approve.
Temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import surface, tool_labels
from flowstate.tooldefs import ALL_TOOLS

ASK = "User: yes, delete it"
# Tables that change on every call (session tracking, the review queue, the audit trail)
# or are a rebuilt cache; everything else must come back exactly.
_MOVING = {"sessions", "auto_sessions", "review_items", "record_history", "activity_log",
           "meaning_index", "project_views", "schema_migrations",
           "session_runs", "run_writes", "run_views"}   # 2.4.0 run ledger: grows with every call


def run(coro):
    return asyncio.run(coro)


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def dump(db) -> dict:
    """Every row of every content table, by rowid."""
    conn = sqlite3.connect(db)
    try:
        virtual = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE 'CREATE VIRTUAL%'")]
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            if t in _MOVING or any(t == v or t.startswith(v + "_") for v in virtual):
                continue
            out[t] = conn.execute(f'SELECT rowid, * FROM "{t}" ORDER BY rowid').fetchall()
        return out
    finally:
        conn.close()


def _pending(db):
    return _q(db, "SELECT id, kind, status FROM review_items WHERE status = 'pending'")


@pytest.fixture
def rich(backend, seeded_db):
    """Alpha (project 1) with one of everything, plus Beta linking to Alpha's component."""
    S = backend
    ok = lambda r: (not r.isError) or pytest.fail(r.content[0].text)  # noqa: E731
    ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "use sqlite"}))
    ok(call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "port 8766 is ours",
                            "source": "user_stated"}))
    ok(call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": 1,
                                   "description": "restart it"}))
    ok(call(S, "manage_problems", {"action": "solve", "project": "Alpha", "problem_id": 1,
                                   "summary": "fixed the config", "winning_attempt_id": 1}))
    ok(call(S, "manage_todos", {"action": "add", "project": "Alpha", "title": "after the bug",
                                "blocked_by_problem": 1}))
    ok(call(S, "manage_rules", {"action": "add", "project": "Alpha", "rule": "never push"}))
    ok(call(S, "manage_handoffs", {"action": "leave", "project": "Alpha", "where_i_was": "half way",
                                   "next_step": "finish"}))
    ok(call(S, "manage_variables", {"action": "create", "project": "Alpha", "name": "port",
                                    "value": "8766"}))
    ok(call(S, "manage_methods", {"action": "create", "project": "Alpha", "name": "deploy",
                                  "description": "how to deploy", "steps": ["build", "ship"]}))
    ok(call(S, "manage_projects", {"action": "add_component", "project": "Alpha", "name": "Sub",
                                   "parent_component_id": 1}))
    ok(call(S, "manage_links", {"action": "add", "project": "Beta Project", "link_type": "uses",
                                "target_project": "Alpha", "target_component": "Core"}))
    ok(call(S, "log_work", {"project": "Alpha", "kind": "change", "summary": "shared change",
                            "component": "Core"}))
    return S


# ---------------------------------------------------------------- asking deletes nothing
def test_an_ai_delete_call_only_queues_a_request(rich, seeded_db):
    S = rich
    before = dump(seeded_db)
    for name, args in [
            ("manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                               "record_id": 1}),
            ("manage_projects", {"action": "delete_component", "project": "Alpha", "component_id": 1}),
            ("manage_projects", {"action": "delete", "project": "Alpha"}),
            ("manage_variables", {"action": "delete", "project": "Alpha", "variable_id": 1}),
            ("manage_methods", {"action": "delete", "project": "Alpha", "method_id": 1}),
            ("variable", {"action": "delete", "project": "Alpha", "variable_id": 1})]:
        r = call(S, name, args)
        assert not r.isError, r.content[0].text
        out = _j(r)
        assert out["delete_requested"] and "Nothing was deleted" in out["note"]
        assert "user" in out["note"] and "approve" in out["note"]
    assert dump(seeded_db) == before                       # not one row removed or changed
    kinds = [k for _i, k, _s in _pending(seeded_db)]
    assert kinds.count("delete") == 5                      # the repeat returned the same item
    again = _j(call(S, "manage_variables", {"action": "delete", "project": "Alpha", "variable_id": 1}))
    assert again["already_requested"] is True


def test_delete_requests_are_checked_like_every_write(rich):
    S = rich
    listed = call(S, "manage_memory", {"action": "delete", "record_type": "todo", "record_id": 1})
    assert listed.isError and "'project' is a required property" in listed.content[0].text
    no_project = _j(call(S, "delete_request", {"action": "record", "record_type": "todo",
                                               "record_id": 1}))
    assert no_project["error"] == "project_required"
    wrong = _j(call(S, "manage_memory", {"action": "delete", "project": "Beta Project",
                                         "record_type": "todo", "record_id": 1}))
    assert wrong["error"] == "project_mismatch"
    comp = _j(call(S, "manage_projects", {"action": "delete_component", "project": "Beta Project",
                                          "component_id": 1}))
    assert comp["error"] == "project_mismatch"
    missing = _j(call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                           "record_type": "todo", "record_id": 999}))
    assert missing["error"] == "record_not_found"
    secret = call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                       "record_id": 1, "model": "sk-ant-api03-" + "x" * 40})
    assert _j(secret)["error"] == "secret_detected"
    # project / component go through manage_projects, not the record delete
    r = call(S, "delete_request", {"action": "record", "project": "Alpha",
                                   "record_type": "project", "record_id": 1})
    assert _j(r)["error"] == "bad_type"


# ---------------------------------------------------------------- what would go
def test_child_counts_are_correct(rich, seeded_db):
    S = rich
    db = seeded_db
    out = _j(call(S, "manage_projects", {"action": "delete", "project": "Alpha"}))
    w = out["would_delete"]
    assert (w["record_type"], w["id"], w["title"]) == ("project", 1, "Alpha")
    n = lambda sql: _q(db, sql)[0][0]  # noqa: E731
    expected = {
        "components": n("SELECT COUNT(*) FROM components WHERE project_id = 1"),
        "todos": n("SELECT COUNT(*) FROM todos WHERE project_id = 1"),
        "decisions": n("SELECT COUNT(*) FROM conversations WHERE project_id = 1"),
        "learnings": n("SELECT COUNT(*) FROM learnings WHERE project_id = 1"),
        "changes": n("SELECT COUNT(*) FROM changes ch JOIN components c ON c.id = ch.component_id "
                     "WHERE c.project_id = 1"),
        "problems": 1, "attempts": 1, "solutions": 1, "rules": 1, "handoffs": 1,
        "variables": 1, "methods": 1,
        "links": n("SELECT COUNT(*) FROM record_links WHERE from_project_id = 1 OR to_project_id = 1"),
    }
    assert expected["components"] == 2 and expected["links"] >= 2   # Sub + Beta's 'uses' link
    for k, v in expected.items():
        assert w["with"].get(k, 0) == v, (k, w["with"])
    # a problem takes its attempts and solution; the todo waiting on it only loses the link
    p = _j(call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "problem",
                                     "record_id": 1}))
    # (and the "blocks" link to the todo waiting on it)
    assert p["would_delete"]["with"] == {"attempts": 1, "links": 1, "solutions": 1}
    item = _q(db, "SELECT details FROM review_items WHERE id = ?", p["delete_requested"])[0][0]
    assert "manage_history(action=\"undo\"" in item
    # a component: its sub-component, changes and problems (and theirs); todos stay
    c = _j(call(S, "manage_projects", {"action": "delete_component", "project": "Alpha",
                                       "component_id": 1}))
    cw = c["would_delete"]["with"]
    assert cw["components"] == 1 and cw["problems"] == 1 and cw["attempts"] == 1
    assert cw["changes"] == n("SELECT COUNT(*) FROM changes WHERE component_id = 1")
    assert "todos" not in cw


# ---------------------------------------------------------------- approving
def _request(S, args):
    out = _j(call(S, args.pop("tool", "manage_memory"), args))
    return out["delete_requested"]


def test_approval_needs_the_users_words(rich, seeded_db):
    S = rich
    before = dump(seeded_db)
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo", "record_id": 1})
    for extra in ({}, {"user_request": "  "}):
        r = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                         **extra}))
        assert r["error"] == "user_request_required"
    assert dump(seeded_db) == before
    assert (item, "delete", "pending") in _pending(seeded_db)


def test_approve_deletes_and_undo_restores_exactly(rich, seeded_db):
    S = rich
    db = seeded_db
    before = dump(db)
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                        "record_id": 1})
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": ASK})
    out = _j(r)
    assert not r.isError and out["approved"] == item
    assert out["deleted"]["with"] == {"attempts": 1, "links": 1, "solutions": 1}
    assert 'record_type="problem", record_id=1' in out["undo"]
    assert _q(db, "SELECT COUNT(*) FROM problems WHERE id = 1") == [(0,)]
    assert _q(db, "SELECT COUNT(*) FROM solution_attempts WHERE problem_id = 1") == [(0,)]
    assert _q(db, "SELECT blocked_by_problem_id FROM todos WHERE title = 'after the bug'") == [(None,)]
    hist = _j(call(S, "manage_history", {"action": "list", "project": "Alpha",
                                         "record_type": "problem", "record_id": 1}))
    assert hist["history"][0]["tool"] == "delete/problem"
    assert hist["history"][0]["changes"] == {"deleted": True}
    act = _q(db, "SELECT tool, ok, project_id FROM activity_log WHERE tool = 'review_approve' "
                 "ORDER BY id DESC LIMIT 1")
    assert act == [("review_approve", 1, 1)]
    undo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "problem", "record_id": 1}))
    assert "error" not in undo, undo
    assert dump(db) == before                              # row, children and unlinked todo
    # undoing the undo would delete again without the user: refused
    again = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                          "record_type": "problem", "record_id": 1}))
    assert again["error"] == "delete_needs_approval"
    assert dump(db) == before


@pytest.mark.parametrize("rtype,rid", [("decision", 1), ("change", 1), ("learning", 1), ("todo", 1),
                                       ("attempt", 1), ("handoff", 1), ("rule", 1), ("link", 1),
                                       ("variable", 1), ("method", 1)])
def test_every_record_type_deletes_and_comes_back(rich, seeded_db, rtype, rid):
    S = rich
    before = dump(seeded_db)
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": rtype,
                        "record_id": rid})
    out = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "user_request": ASK}))
    assert out["deleted"]["record_type"] == rtype, out
    assert dump(seeded_db) != before
    undo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": rtype,
                                         "record_id": rid}))
    assert "error" not in undo, undo
    assert dump(seeded_db) == before


def test_project_delete_needs_its_exact_name_and_comes_back(rich, seeded_db):
    S = rich
    db = seeded_db
    before = dump(db)
    alpha_reviews = _q(db, "SELECT COUNT(*) FROM review_items WHERE project_id = 1")[0][0]
    item = _request(S, {"tool": "manage_projects", "action": "delete", "project": "Alpha"})
    base = {"action": "approve", "project": "Alpha", "item_id": item, "user_request": ASK}
    assert _j(call(S, "manage_review", base))["error"] == "confirm_name_required"
    for wrong in ("alpha", "Alpha ", "Beta Project"):
        assert _j(call(S, "manage_review", {**base, "confirm_name": wrong}))["error"] \
            == "confirm_name_mismatch"
    assert dump(db) == before and (item, "delete", "pending") in _pending(db)
    out = _j(call(S, "manage_review", {**base, "confirm_name": "Alpha"}))
    assert out["deleted"]["record_type"] == "project", out
    assert _q(db, "SELECT COUNT(*) FROM projects WHERE id = 1") == [(0,)]
    for t in ("components", "todos", "conversations", "learnings", "project_rules", "handoff_notes",
              "project_variables", "project_methods", "review_items"):
        assert _q(db, f"SELECT COUNT(*) FROM {t} WHERE project_id = 1") == [(0,)], t
    assert _q(db, "SELECT COUNT(*) FROM record_links WHERE to_project_id = 1") == [(0,)]
    assert _q(db, "SELECT name FROM projects") == [("Beta Project",)]     # Beta untouched
    assert _q(db, "SELECT title FROM todos") == [("Beta todo",), ("Core changed: check Beta Project",)]
    # the project is gone, so undo takes its old name (and id)
    gone = _j(call(S, "manage_history", {"action": "list", "project": "Alpha"}))
    assert gone["error"] in ("project_not_found", "project_required")
    undo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "project", "record_id": 1}))
    assert "error" not in undo, undo
    assert dump(db) == before
    assert _q(db, "SELECT COUNT(*) FROM review_items WHERE project_id = 1")[0][0] == alpha_reviews + 1
    redo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "project", "record_id": 1}))
    assert redo["error"] == "delete_needs_approval"


def test_reject_leaves_everything_as_it_was(rich, seeded_db):
    S = rich
    before = dump(seeded_db)
    item = _request(S, {"tool": "manage_projects", "action": "delete", "project": "Alpha"})
    r = _j(call(S, "manage_review", {"action": "reject", "project": "Alpha", "item_id": item,
                                     "reason": "keep it", "user_request": "User: no, keep Alpha"}))
    assert r["rejected"] == item
    assert dump(seeded_db) == before
    assert _q(seeded_db, "SELECT status FROM review_items WHERE id = ?", item) == [("rejected",)]
    late = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "user_request": ASK, "confirm_name": "Alpha"}))
    assert late["error"] == "already_decided"
    assert dump(seeded_db) == before


def test_approval_refused_if_more_would_go_than_the_user_saw(rich, seeded_db):
    S = rich
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "problem",
                        "record_id": 1})
    call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": 1,
                                "description": "another try"})
    before = dump(seeded_db)
    r = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                     "user_request": ASK}))
    assert r["error"] == "changed_since_request"
    assert dump(seeded_db) == before and (item, "delete", "pending") in _pending(seeded_db)


def test_undo_of_a_child_refused_while_its_parent_is_deleted(rich, seeded_db):
    S = rich
    c = _request(S, {"action": "delete", "project": "Alpha", "record_type": "change", "record_id": 1})
    call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": c, "user_request": ASK})
    k = _request(S, {"tool": "manage_projects", "action": "delete_component", "project": "Alpha",
                     "component_id": 1})
    call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": k, "user_request": ASK})
    before = dump(seeded_db)
    r = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "change",
                                      "record_id": 1}))
    assert r["error"] == "parent_missing"
    assert dump(seeded_db) == before
    # in the right order both come back
    for rtype, rid in (("component", 1), ("change", 1)):
        out = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                            "record_type": rtype, "record_id": rid}))
        assert "error" not in out, out
    assert _q(seeded_db, "SELECT COUNT(*) FROM changes WHERE id = 1") == [(1,)]


def test_the_review_page_needs_the_typed_name_too(rich, seeded_db):
    from flowstate.review_page import Page
    page = Page(seeded_db)
    item = _request(rich, {"tool": "manage_projects", "action": "delete", "project": "Alpha"})
    assert 'name="confirm_name"' in page.render("csrf")
    assert "exact name" in page.approve({"item_id": str(item)})
    assert _q(seeded_db, "SELECT COUNT(*) FROM projects WHERE id = 1") == [(1,)]
    assert page.approve({"item_id": str(item), "confirm_name": "Alpha"}).startswith("Approved")
    assert _q(seeded_db, "SELECT COUNT(*) FROM projects WHERE id = 1") == [(0,)]


# ---------------------------------------------------------------- who may delete
def test_remote_and_read_only_callers_can_not_ask():
    from flowstate.remote_auth import refusal
    rw = ["flowstate:read", "flowstate:write"]          # remote token, OAuth app, LAN app key
    ro = ["flowstate:read"]
    local = ["flowstate:read", "flowstate:write", "flowstate:local"]   # owner's LAN token
    asks = [("manage_projects", {"action": "delete", "project": "Alpha"}),
            ("manage_projects", {"action": "delete_component", "project": "Alpha", "component_id": 1}),
            ("manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                               "record_id": 1}),
            ("manage_variables", {"action": "delete", "variable_id": 1}),
            ("manage_methods", {"action": "delete", "method_id": 1}),
            ("manage_files", {"action": "remove", "attachment_id": 1}),
            ("variable", {"action": "delete", "variable_id": 1})]
    for name, args in asks:
        internal = surface.translate(name, args)
        assert refusal(*internal, rw)[0] == "not_allowed_remotely", name
        assert refusal(*internal, ro) is not None, name
        assert refusal(*internal, local) is None, name
        assert refusal(*internal, None) is None, name     # stdio


def test_remote_callers_can_not_approve(rich, seeded_db, monkeypatch):
    S = rich
    item = _request(S, {"action": "delete", "project": "Alpha", "record_type": "todo", "record_id": 1})
    before = dump(seeded_db)
    monkeypatch.setattr(S, "_caller", lambda: ("phone", ["flowstate:read", "flowstate:write"]))
    r = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                     "user_request": ASK}))
    assert r["error"] == "not_allowed_remotely"
    assert dump(seeded_db) == before and (item, "delete", "pending") in _pending(seeded_db)
    # an ordinary review item is still approvable remotely
    q = _j(call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "idea",
                                "suggested": True}))["queued_for_review"]
    ok = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": q,
                                      "user_request": "User: approve the idea"}))
    assert ok["approved"] == q


def test_lan_keys_over_http_can_not_ask_or_approve(seeded_db):
    from test_accountability_http import _add_key, _calls
    from test_remote_auth import Server
    s = Server(seeded_db, public=False)
    try:
        _rid, ro = _add_key(seeded_db, "Reader", read_only=True)
        _wid, rw = _add_key(seeded_db, "Writer")
        ask = ("manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                 "record_id": 1})
        out_ro = _calls(s.base, ro, [ask])
        out_rw = _calls(s.base, rw, [ask, ("manage_projects", {"action": "delete",
                                                                "project": "Alpha"})])
        assert out_ro[0][0] and json.loads(out_ro[0][1])["error"] in ("not_allowed_remotely",
                                                                      "read_only_client")
        for err, text in out_rw:
            assert err and json.loads(text)["error"] == "not_allowed_remotely"
        assert _q(seeded_db, "SELECT COUNT(*) FROM review_items WHERE kind = 'delete'") == [(0,)]
        # a request made on this computer can't be approved with a LAN key either
        from flowstate import deletes
        item = deletes.request(seeded_db, 1, "todo", 1)["delete_requested"]
        out = _calls(s.base, rw, [("manage_review", {"action": "approve", "project": "Alpha",
                                                      "item_id": item, "user_request": ASK})])
        assert out[0][0] and json.loads(out[0][1])["error"] == "not_allowed_remotely"
        assert _q(seeded_db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(1,)]
        assert _q(seeded_db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]
    finally:
        s.stop()


# ---------------------------------------------------------------- labels and old names
def test_delete_actions_are_labelled_destructive():
    tools = {t.name: t for t in ALL_TOOLS}
    for name, acts in (("manage_projects", ("delete", "delete_component")),
                       ("manage_memory", ("delete",))):
        t = tools[name]
        assert t.annotations.destructiveHint is True and t.annotations.readOnlyHint is False
        split = t.meta["flowstate/actions"]
        for a in acts:
            assert a in split["destructive"] and a in surface.ROUTES[name]
            assert a in t.inputSchema["properties"]["action"]["enum"]
        assert "approve" in t.description and "manage_review" in t.description
    assert tool_labels.hints("manage_review") == (False, True)
    assert "confirm_name" in tools["manage_review"].inputSchema["properties"]


def test_older_direct_delete_names_now_ask_too(rich, seeded_db):
    S = rich
    r = call(S, "variable", {"action": "delete", "project": "Alpha", "variable_id": 1})
    assert _j(r)["delete_requested"]
    assert "older tool name" in r.content[1].text and "manage_variables" in r.content[1].text
    assert _q(seeded_db, "SELECT COUNT(*) FROM project_variables WHERE id = 1") == [(1,)]
    r = call(S, "method", {"action": "delete", "project": "Alpha", "method_id": 1})
    assert _j(r)["delete_requested"]
    assert _q(seeded_db, "SELECT COUNT(*) FROM project_methods WHERE id = 1") == [(1,)]


def test_component_delete_unlinks_todos_and_undo_relinks_them(rich, seeded_db):
    S = rich
    call(S, "manage_todos", {"action": "add", "project": "Alpha", "title": "in core",
                             "component_id": 1, "due_date": "2026-12-01"})
    before = dump(seeded_db)
    k = _request(S, {"tool": "manage_projects", "action": "delete_component", "project": "Alpha",
                     "component_id": 1})
    out = _j(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": k,
                                       "user_request": ASK}))
    assert out["deleted"]["changed_rows"] >= 1 and "todos" not in out["deleted"]["with"]
    assert _q(seeded_db, "SELECT component_id FROM todos WHERE title = 'in core'") == [(None,)]
    undo = _j(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "component", "record_id": 1}))
    assert "error" not in undo, undo
    assert dump(seeded_db) == before
