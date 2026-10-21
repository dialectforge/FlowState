"""Phase D continuation E1-E4: priority levels (critical/urgent/normal/low, old names still
accepted), per-project hard rules, handoff notes, and the brief size option. Real MCP
client sessions on temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import priority


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args, client="Claude Desktop"):
    # The user's go-ahead comes in through another app: with review on, an app can't
    # decide what it queued itself (test_user_approval.py covers that).
    if client == "Claude Desktop" and (name in ("review_approve", "review_reject") or (
            name == "manage_review" and args.get("action") in ("approve", "reject"))):
        client = "Claude Code"
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def brief(S, project="Alpha", **kw):
    r = call(S, "start_here", {"project": project, **kw})
    assert not r.isError
    return r.content[0].text


# ---------------------------------------------------------------- E1 priority
def test_priority_names_map_both_ways():
    assert [priority.store(x) for x in ("critical", "urgent", "normal", "low")] == \
        ["critical", "high", "medium", "low"]
    assert [priority.store(x) for x in ("HIGH", "medium", None, "")] == ["high", "medium", "medium", "medium"]
    assert [priority.label(x) for x in ("critical", "high", "medium", "low", None)] == \
        ["critical", "urgent", "normal", "low", "normal"]
    assert set(priority.at_least("urgent")) == {"critical", "high"}
    with pytest.raises(priority.PriorityError):
        priority.store("asap")


def test_todo_tools_accept_both_names_filter_and_sort(backend, seeded_db):
    S = backend
    for title, p, due in (("New urgent", "urgent", "2026-12-01"), ("Old high", "high", None),
                          ("Normal one", "normal", "2026-11-01"), ("Crit", "critical", None),
                          ("Old medium", "medium", None), ("Low one", "low", "2026-10-20")):
        args = {"action": "add", "project": "Alpha", "title": title, "priority": p}
        if due:
            args["due_date"] = due
        r = call(S, "todo", args)
        assert not r.isError, r.content[0].text
    stored = dict(_q(seeded_db, "SELECT title, priority FROM todos WHERE project_id = 1"))
    assert stored["New urgent"] == "high" and stored["Normal one"] == "medium"   # v1-compatible values
    assert stored["Old high"] == "high" and stored["Crit"] == "critical"

    rows = _j(call(S, "todo", {"action": "list", "project": "Alpha"}))
    assert [r["priority_level"] for r in rows][:1] == ["critical"]
    levels = [r["priority_level"] for r in rows]
    assert levels == sorted(levels, key=lambda l: priority.LEVELS.index(l))     # most urgent first

    top = _j(call(S, "todo", {"action": "list", "project": "Alpha", "min_priority": "urgent"}))
    assert {r["title"] for r in top} == {"Crit", "New urgent", "Old high", "Alpha todo"}
    normal = _j(call(S, "todo", {"action": "list", "project": "Alpha", "priority": "normal"}))
    assert {r["title"] for r in normal} == {"Normal one", "Old medium"}
    by_due = _j(call(S, "todo", {"action": "list", "project": "Alpha", "sort": "due"}))
    assert [r["title"] for r in by_due][:3] == ["Low one", "Normal one", "New urgent"]
    newest = _j(call(S, "todo", {"action": "list", "project": "Alpha", "sort": "newest", "compact": True}))
    assert newest[0]["title"] == "Old medium" or newest[0]["title"] == "Low one"
    assert "priority_level" in newest[0] and "created_at" not in newest[0]

    bad = call(S, "todo", {"action": "add", "project": "Alpha", "title": "x", "priority": "asap"})
    assert bad.isError and not _q(seeded_db, "SELECT 1 FROM todos WHERE title = 'x'")  # schema enum
    assert S.handle_call("todo", {"action": "add", "project": "Alpha", "title": "x",
                                  "priority": "asap"})["error"] == "bad_priority"  # past the schema too
    # update with a new name
    tid = _q(seeded_db, "SELECT id FROM todos WHERE title = 'Low one'")[0][0]
    call(S, "todo", {"action": "update", "project": "Alpha", "todo_id": tid, "priority": "urgent"})
    assert _q(seeded_db, "SELECT priority FROM todos WHERE id = ?", tid) == [("high",)]


def test_priority_in_brief_next_log_work_and_review(backend, seeded_db):
    S = backend
    r = _j(call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Ship it",
                                "priority": "urgent"}))
    assert r["record"]["priority"] == "high"
    call(S, "log_work", {"project": "Alpha", "kind": "problem", "summary": "Bad crash",
                         "priority": "critical"})
    assert _q(seeded_db, "SELECT severity FROM problems WHERE title = 'Bad crash'") == [("critical",)]
    call(S, "problem", {"action": "log", "project": "Alpha", "component_id": 1, "title": "Meh",
                        "severity": "normal"})
    assert _q(seeded_db, "SELECT severity FROM problems WHERE title = 'Meh'") == [("medium",)]

    text = brief(S)
    assert "- [urgent] #" in text and "Ship it" in text and "[high]" not in text.split("Open problems")[0]
    nxt = _j(call(S, "todo", {"action": "next", "project": "Alpha"}))
    assert {i["priority"] for i in nxt["next"]} <= set(priority.LEVELS)
    assert nxt["next"][0]["priority"] == "urgent"

    # suggested -> review queue -> approved with a new-style priority edit
    q = _j(call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Maybe later",
                                "suggested": True, "priority": "low"}))
    item = q["queued_for_review"]["id"] if isinstance(q["queued_for_review"], dict) else q["queued_for_review"]
    a = call(S, "review_approve", {"project": "Alpha", "item_id": item, "priority": "normal",
                                   "user_request": "yes, approve that one"})
    assert not a.isError, a.content[0].text
    assert _q(seeded_db, "SELECT priority FROM todos WHERE title = 'Maybe later'") == [("medium",)]


# ---------------------------------------------------------------- E2 hard rules
def test_rules_add_list_remove_and_shown_first(backend, seeded_db):
    S = backend
    r = _j(call(S, "rules", {"action": "add", "project": "Alpha", "rule": "Never push to main"}))
    rid = r["rule"]["id"]
    assert _j(call(S, "rules", {"action": "add", "project": "Alpha", "rule": "never push to MAIN"}))[
        "note"] == "This rule already exists."
    call(S, "rules", {"action": "add", "project": "Alpha", "rule": "Do not touch Cloudflare"})
    lines = brief(S).splitlines()
    assert lines[1].startswith("HARD RULES") and lines[2] == f"- R{rid}: Never push to main"
    assert "Never push" not in brief(S, "Beta Project")           # per project

    lst = _j(call(S, "rules", {"action": "list", "project": "Alpha"}))
    assert lst["count"] == 2 and lst["characters_max"] == 2000

    no = call(S, "rules", {"action": "remove", "project": "Alpha", "rule_id": rid})
    assert no.isError and _j(no)["error"] == "user_request_required"
    wrong = call(S, "rules", {"action": "remove", "project": "Beta Project", "rule_id": rid,
                              "user_request": "remove it"})
    assert wrong.isError and _j(wrong)["error"] == "project_mismatch"
    ok = _j(call(S, "rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                              "user_request": "drop the main rule please"}))
    assert ok["removed"] == rid
    assert "Never push" not in brief(S)
    assert _q(seeded_db, "SELECT removed_request FROM project_rules WHERE id = ?", rid) == \
        [("drop the main rule please",)]                         # soft delete, kept on record
    everything = _j(call(S, "rules", {"action": "list", "project": "Alpha", "include_removed": True}))
    assert everything["count"] == 2

    need = call(S, "rules", {"action": "add", "rule": "x"})
    assert need.isError and _j(need)["error"] == "project_required"


def test_rules_limits_and_secret_scan(backend):
    S = backend
    long = call(S, "rules", {"action": "add", "project": "Alpha", "rule": "x" * 301})
    assert long.isError and _j(long)["error"] == "rule_too_long"
    for i in range(6):
        r = call(S, "rules", {"action": "add", "project": "Alpha", "rule": f"Rule {i} " + "y" * 290})
        assert not r.isError
    full = call(S, "rules", {"action": "add", "project": "Alpha", "rule": "One more " + "z" * 250})
    assert full.isError and _j(full)["error"] == "rules_full"
    for i in range(25):
        assert not call(S, "rules", {"action": "add", "project": "Beta Project", "rule": f"r{i}"}).isError
    many = call(S, "rules", {"action": "add", "project": "Beta Project", "rule": "one too many"})
    assert many.isError and "25 rules" in _j(many)["message"]
    sec = call(S, "rules", {"action": "add", "project": "Alpha",
                            "rule": "Never print ghp_" + "a" * 36})
    assert sec.isError and _j(sec)["error"] == "secret_detected"


@pytest.mark.parametrize("size,cap", [("quick", 1500), ("standard", 4000), ("deep", 12000)])
def test_rules_never_truncated_others_give_way(backend, seeded_db, size, cap):
    S = backend
    conn = sqlite3.connect(seeded_db)
    for i in range(300):
        conn.execute("INSERT INTO todos (project_id, title, priority) VALUES (1, ?, 'critical')",
                     (f"Very important todo number {i} " + "w" * 60,))
    conn.commit()
    conn.close()
    rules = [f"Rule {i}: " + "r" * 280 for i in range(6)]   # 1,700+ characters of rules
    for t in rules:
        assert not call(S, "rules", {"action": "add", "project": "Alpha", "rule": t}).isError
    call(S, "handoff", {"action": "leave", "project": "Alpha", "where_i_was": "h" * 1000,
                        "next_step": "n" * 500})
    text = brief(S, size=size)
    for t in rules:
        assert t in text                                   # every rule, in full
    assert len(text) <= max(cap, sum(len(t) + 10 for t in rules) + 700)
    if size != "quick":
        assert len(text) <= cap
    assert "H1" in text                                     # the handoff still shows (maybe trimmed)


# ---------------------------------------------------------------- E3 handoffs
def test_handoff_shown_after_rules_until_picked_up(backend, seeded_db):
    S = backend
    call(S, "rules", {"action": "add", "project": "Alpha", "rule": "Never push to main"})
    h = _j(call(S, "handoff", {"action": "leave", "project": "Alpha",
                               "where_i_was": "Half-way through the export rewrite; tests 3/5 pass",
                               "next_step": "Fix the unicode test"}, client="Grok Build"))
    hid = h["handoff_id"]
    text = brief(S)
    lines = text.splitlines()
    i_rules, i_hand = (next(i for i, l in enumerate(lines) if l.startswith(p))
                       for p in ("HARD RULES", "HANDOFF"))
    i_todos = next(i for i, l in enumerate(lines) if l.startswith("Open todos"))
    assert i_rules < i_hand < i_todos
    assert f"H{hid}" in text and "Grok Build" in text and "Next step: Fix the unicode test" in text

    wrong = call(S, "handoff", {"action": "pickup", "project": "Beta Project", "handoff_id": hid})
    assert wrong.isError and _j(wrong)["error"] == "project_mismatch"
    assert _j(call(S, "handoff", {"action": "pickup", "project": "Alpha", "handoff_id": hid}))["picked_up"]
    assert "HANDOFF" not in brief(S)
    assert _j(call(S, "handoff", {"action": "list", "project": "Alpha"}))["count"] == 0
    allnotes = _j(call(S, "handoff", {"action": "list", "project": "Alpha", "include_picked_up": True}))
    assert allnotes["handoffs"][0]["picked_up_by"] == "Claude Desktop"
    empty = call(S, "handoff", {"action": "leave", "project": "Alpha", "where_i_was": " "})
    assert empty.isError and _j(empty)["error"] == "where_required"


def test_many_handoffs_newest_first_rest_counted(backend):
    S = backend
    for i in range(4):
        call(S, "handoff", {"action": "leave", "project": "Alpha", "where_i_was": f"step {i}"})
    text = brief(S)
    assert "4 open" in text and "step 3" in text and "step 2" in text and "step 0" not in text
    assert "2 older open note(s)" in text


# ---------------------------------------------------------------- E4 size
def test_brief_sizes(backend, seeded_db):
    S = backend
    conn = sqlite3.connect(seeded_db)
    for i in range(60):
        conn.execute("INSERT INTO todos (project_id, title, priority) VALUES (1, ?, 'medium')",
                     (f"Todo {i} " + "t" * 50,))
        conn.execute("INSERT INTO learnings (project_id, insight, category) VALUES (1, ?, 'gotcha')",
                     (f"Learning {i} " + "l" * 80,))
    conn.commit()
    conn.close()
    quick, std, deep = (brief(S, size=s) for s in ("quick", "standard", "deep"))
    assert len(quick) <= 1500 and len(std) <= 4000 and len(deep) <= 12000
    assert len(quick) < len(std) < len(deep)
    assert std == brief(S)                                       # default unchanged
    assert "[quick]" in quick and "[deep]" in deep and "[" + "standard]" not in std
    assert "Key learnings" not in quick and "Key learnings" in deep
    assert deep.count("\n- [normal]") > std.count("\n- [normal]")
