"""v2.1 tool surface (surface.py): the public names route to the same internal operations,
every older name still answers (with a one-line hint), annotations match the per-action
split, and the guard and remote policy apply to the public names too. Temp DBs only."""

import asyncio
import json

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import surface, tool_labels
from flowstate.tooldefs import ALL_TOOLS


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


# Every older name with a call that is valid on the seeded DB (Alpha = project 1, Core =
# component 1, todo 1, problem 1). `git` is left out on purpose: it runs git in the data
# folder, which tests must never touch; its routing and remote policy are checked below.
OLD_CALLS = {
    "list_projects": {},
    "create_project": {"name": "Gamma"},
    "update_project": {"project": "Alpha", "description": "updated"},
    "get_context": {"project_name": "Alpha"},
    "create_component": {"project": "Alpha", "name": "Extra"},
    "list_components": {"project_id": 1},
    "update_component": {"project": "Alpha", "component_id": 1, "description": "core part"},
    "log_learning": {"project": "Alpha", "insight": "old-style lesson"},
    "get_learnings": {"project_id": 1},
    "log_change": {"project": "Alpha", "field_name": "old-style change"},
    "get_recent_changes": {"project_id": 1},
    "problem": {"action": "list", "project": "Alpha"},
    "attempt": {"action": "log", "project": "Alpha", "problem_id": 1, "description": "tried"},
    "todo": {"action": "list", "project": "Alpha"},
    "session": {"action": "current", "project": "Alpha"},
    "file": {"action": "list", "project": "Alpha"},
    "variable": {"action": "list", "project": "Alpha"},
    "method": {"action": "list", "project": "Alpha"},
    "self_improve": {"action": "get_skills", "project": "Alpha"},
    "review_list": {},
    "review_approve": {"project": "Alpha", "item_id": 999, "user_request": "User: approve it"},
    "review_reject": {"project": "Alpha", "item_id": 999, "reason": "no",
                      "user_request": "User: reject it"},
    "link": {"action": "list", "project": "Alpha"},
    "review_mode": {"action": "get"},
    "history": {"action": "list", "project": "Alpha"},
    "cleanup_suggest": {"project": "Alpha"},
    "import_memory": {"project": "Alpha", "text": "- prefers tabs"},
    "rules": {"action": "list", "project": "Alpha"},
    "handoff": {"action": "list", "project": "Alpha"},
    "verify": {"action": "stale", "project": "Alpha"},
    "ask": {"question": "what breaks?"},
}
EXPECTED_ERRORS = {"review_approve": "record_not_found", "review_reject": "record_not_found"}


def test_every_older_name_is_covered():
    assert set(OLD_CALLS) | {"git"} == set(surface.OLD_NAMES)


@pytest.mark.parametrize("name", sorted(OLD_CALLS))
def test_older_name_still_works_with_a_hint(backend, name):
    r = call(backend, name, OLD_CALLS[name])
    first = r.content[0].text
    if name in EXPECTED_ERRORS:
        assert r.isError and _j(r)["error"] == EXPECTED_ERRORS[name]
    else:
        assert not r.isError, first
        assert "Unknown tool" not in first and '"tool_failed"' not in first
    hint = r.content[-1].text
    assert len(r.content) == 2 and hint.startswith(f"FlowState: `{name}` is an older tool name")
    assert surface.new_call(name, OLD_CALLS[name]) in hint
    tool = surface.new_call(name, OLD_CALLS[name]).split("(")[0]
    assert tool in {t.name for t in ALL_TOOLS}


def test_new_names_get_no_hint_and_same_first_block(backend):
    pairs = [
        (("todo", {"action": "list", "project": "Alpha"}),
         ("manage_todos", {"action": "list", "project": "Alpha"})),
        (("list_projects", {}), ("manage_projects", {"action": "list"})),
        (("verify", {"action": "stale", "project": "Alpha"}),
         ("manage_memory", {"action": "stale", "project": "Alpha"})),
        (("review_mode", {"action": "get", "project": "Alpha"}),
         ("manage_review", {"action": "get_mode", "project": "Alpha"})),
        (("ask", {"question": "what breaks?", "project": "Alpha"}),
         ("search", {"query": "what breaks?", "answer": True, "project": "Alpha"})),
        (("problem", {"action": "get_tree", "project": "Alpha", "problem_id": 1}),
         ("manage_problems", {"action": "tree", "project": "Alpha", "problem_id": 1})),
        (("rules", {"action": "list", "project": "Alpha"}),
         ("manage_rules", {"action": "list", "project": "Alpha"})),
    ]
    for (old, oargs), (new, nargs) in pairs:
        o, n = call(backend, old, oargs), call(backend, new, nargs)
        assert not n.isError and len(n.content) == 1, new
        assert o.content[0].text == n.content[0].text, (old, new)


def test_public_writes_go_through_the_guard_and_history(backend, seeded_db):
    S = backend
    # Beta's todo (#2) is refused when filed under Alpha, exactly like the older todo tool
    r = call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": 2,
                                 "status": "done"})
    assert r.isError and _j(r)["error"] == "project_mismatch"
    r = call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": 1,
                                 "status": "done"})
    assert not r.isError
    h = _j(call(S, "manage_history", {"action": "list", "project": "Alpha",
                                      "record_type": "todo", "record_id": 1}))
    assert h["count"] == 1
    u = call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "todo",
                                   "record_id": 1})
    assert not u.isError
    todos = _j(call(S, "manage_todos", {"action": "list", "project": "Alpha"}))
    rows = todos["todos"] if isinstance(todos, dict) else todos
    assert any(t["id"] == 1 and t["status"] == "pending" for t in rows)
    # secrets are refused on public names too
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning",
                             "summary": "password=hunter2hunter2hunter2"})
    assert r.isError and _j(r)["error"] == "secret_detected"
    # attempts and solve through manage_problems
    a = _j(call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha",
                                       "problem_id": 1, "description": "restart it"}))
    e = call(S, "manage_problems", {"action": "end_attempt", "project": "Alpha",
                                    "attempt_id": a["id"], "outcome": "success"})
    s = call(S, "manage_problems", {"action": "solve", "project": "Alpha", "problem_id": 1,
                                    "summary": "restart fixed it", "winning_attempt_id": a["id"]})
    assert not e.isError and not s.isError
    # projects and components
    c = _j(call(S, "manage_projects", {"action": "add_component", "project": "Alpha",
                                       "name": "Docs"}))
    assert c["name"] == "Docs"
    p = _j(call(S, "manage_projects", {"action": "create", "name": "Delta"}))
    assert p["name"] == "Delta"
    # import uses origin (public) for the internal source
    m = _j(call(S, "manage_memory", {"action": "import", "project": "Alpha",
                                     "text": "- likes short commits", "origin": "ChatGPT"}))
    assert "error" not in m


def test_update_component_keeps_omitted_fields(backend):
    # found during the v2.1 audit: omitted fields were written as NULL (name -> failure)
    S = backend
    r = _j(call(S, "manage_projects", {"action": "update_component", "project": "Alpha",
                                       "component_id": 1, "description": "the core"}))
    assert r["name"] == "Core" and r["description"] == "the core"
    r = _j(call(S, "manage_projects", {"action": "update_component", "project": "Alpha",
                                       "component_id": 1, "status": "testing"}))
    assert (r["name"], r["description"], r["status"]) == ("Core", "the core", "testing")


def test_listed_schemas_are_validated_and_bad_actions_refused(backend):
    r = call(backend, "manage_todos", {"action": "explode", "project": "Alpha"})
    assert r.isError and "not one of" in r.content[0].text      # SDK schema check (listed)
    with pytest.raises(surface.SurfaceError):
        surface.translate("manage_todos", {"action": "explode"})


def test_remote_policy_applies_to_public_names():
    from flowstate.remote_auth import refusal
    rw = ["flowstate:read", "flowstate:write"]
    assert refusal(*surface.translate("manage_data_git", {"action": "status"}), rw)[0] \
        == "not_allowed_remotely"
    assert refusal(*surface.translate("manage_files", {"action": "attach"}), rw)[0] \
        == "not_allowed_remotely"
    assert refusal(*surface.translate("manage_files", {"action": "list"}), rw) is None
    ro = ["flowstate:read"]
    assert refusal(*surface.translate("manage_todos", {"action": "update"}), ro)[0] \
        == "read_only_client"
    assert refusal(*surface.translate("manage_todos", {"action": "list"}), ro) is None
    assert refusal(*surface.translate("log_work", {}), ro)[0] == "read_only_client"


def test_search_lists_newest_without_a_query(backend, seeded_db):
    S = backend
    call(S, "log_work", {"project": "Alpha", "kind": "change", "summary": "newest change"})
    r = _j(call(S, "search", {"project": "Alpha", "content_types": ["change"], "since_hours": 24}))
    assert r["mode"] == "newest" and r["results"][0]["title"] == "newest change"
    assert all(x["content_type"] == "change" for x in r["results"])
    old = _j(call(S, "search", {"project": "Alpha", "content_types": ["learning"]}))
    assert [x["title"] for x in old["results"]] == ["Alpha lesson"]
    import sqlite3
    conn = sqlite3.connect(seeded_db)
    conn.execute("UPDATE changes SET created_at = '2020-01-01 00:00:00'")
    conn.commit()
    conn.close()
    assert _j(call(S, "search", {"project": "Alpha", "content_types": ["change"],
                                 "since_hours": 24}))["count"] == 0


def test_routes_match_schemas_and_annotations(monkeypatch):
    monkeypatch.setenv("FLOWSTATE_TOOLS", "full")
    by = {t.name: t for t in tool_labels.visible(ALL_TOOLS)}
    assert set(by) == set(tool_labels.LABELS)
    # the older names, the kept names, the v2.1 delete request (deletes.py) and the 2.4 review
    # walk (waiting.py); those two have no older name
    internal_ok = set(surface.OLD_NAMES) | {"start_here", "log_work", "search", "delete_request",
                                            "review_walk"}
    for name, tool in by.items():
        groups = tool_labels.LABELS[name][1]
        listed = sum(groups.values(), [])
        assert len(listed) == len(set(listed)), name            # each action in one group
        enum = tool.inputSchema["properties"].get("action", {}).get("enum")
        if enum is None:
            assert listed == ["*"] and name not in surface.ROUTES, name
            continue
        assert set(enum) == set(listed) == set(surface.ROUTES[name]), name
        assert tool.meta == {"flowstate/actions": groups}, name
        for action in enum:
            internal, _args = surface.translate(name, {"action": action})
            assert internal in internal_ok, (name, action)
        a = tool.annotations
        assert a.readOnlyHint is (set(groups) == {"read"}), name
        assert a.destructiveHint is (None if a.readOnlyHint else bool(groups.get("destructive")))
        assert a.idempotentHint is a.readOnlyHint
        assert a.openWorldHint is (name == "manage_data_git")
    # harmless tools are not marked destructive
    for name in ("log_work", "manage_handoffs", "manage_rules", "manage_links", "manage_skills"):
        assert by[name].annotations.destructiveHint is False, name
