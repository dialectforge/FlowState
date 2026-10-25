"""Unsettled decisions in the brief (2.4.4): while a possible-conflict pair is open the newer
decision is tagged and the older one's text is left out, with a pointer that reaches it."""
import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import meaning


@pytest.fixture(autouse=True)
def _no_real_model():
    meaning.set_embedder(None)
    meaning._State.tried, meaning._State.model = True, None
    yield
    meaning.set_embedder(None)


def call(S, name, args, app="Cursor"):
    if name == "manage_review" and args.get("action") in ("approve", "reject") and app == "Cursor":
        app = "Claude Code"
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    text = r.content[0].text
    try:
        return r.isError, json.loads(text)
    except ValueError:
        return r.isError, text


def ok(res):
    err, body = res
    assert not err, body
    return body


def decide(S, summary, component=None, **kw):
    args = {"project": "Alpha", "kind": "decision", "summary": summary, **kw}
    if component:
        args["component"] = component
    return ok(call(S, "log_work", args))["record"]["id"]


def brief(S, size="standard"):
    return call(S, "start_here", {"project": "Alpha", "size": size})[1]


def decisions(text):
    out, on = [], False
    for ln in text.splitlines():
        if ln.startswith("Recent decisions"):
            on = True
        elif on and ln.startswith("- "):
            out.append(ln)
        elif on:
            break
    return out


def sql(db, q, *a):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(q, a)
        conn.commit()
        return cur.fetchall()
    finally:
        conn.close()


def pair(db, new, old):
    """An open pair, as the check would save it (lets a test pick exactly which ones)."""
    pid = sql(db, "SELECT project_id FROM conversations WHERE id = ?", new)[0][0]
    sql(db, """INSERT OR IGNORE INTO decision_conflicts (project_id, new_id, old_id, score, created_at)
               VALUES (?, ?, ?, 0.9, '2026-10-01 00:00:00')""", pid, new, old)


def backdate(db, rid, when):
    sql(db, "UPDATE conversations SET created_at = ? WHERE id = ?", when, rid)


def test_an_open_pair_tags_the_newer_and_leaves_the_older_text_out(backend):
    old = decide(backend, "Use Postgres for hosted storage")
    new = decide(backend, "Use SQLite for hosted storage")
    text = brief(backend)
    assert f"Possible conflicts: D{new} vs D{old}" in text          # the line stays
    lines = decisions(text)
    [newer] = [ln for ln in lines if f"#{new} " in ln]
    assert "Use SQLite" in newer and f"(unsettled: may conflict with #{old})" in newer
    [older] = [ln for ln in lines if f"#{old} " in ln]
    assert "Postgres" not in older and "Postgres" not in "\n".join(lines)
    assert f"#{old} in dispute with #{new}" in older and f'search(query="D{old}")' in older
    # one step to the hidden text
    got = ok(call(backend, "search", {"project": "Alpha", "query": f"D{old}"}))
    assert got["mode"] == "id" and got["results"][0]["content_id"] == old
    assert "Postgres" in got["results"][0]["title"]


def test_keep_both_resolved_and_superseded_look_as_before(backend, seeded_db):
    db = seeded_db
    # keep both: the disagreement card (same component) closes the pair as compatible
    a = decide(backend, "Price Relay at $6", "Pricing")
    b = decide(backend, "Price Relay at $12", "Pricing")
    [item] = [r[0] for r in sql(db, "SELECT review_item_id FROM conflict_cards WHERE status='open'")]
    ok(call(backend, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "keep": "both", "user_request": "User: both of these can stand"}))
    assert sql(db, "SELECT status FROM decision_conflicts WHERE new_id = ?", b) == \
        [("dismissed_compatible",)]
    # dismissed in the review queue (a yes/no item, no component)
    c = decide(backend, "Use JWT tokens for API auth")
    d = decide(backend, "Use session cookies for API auth")
    sql(db, "UPDATE decision_conflicts SET status = 'dismissed_compatible' WHERE new_id = ?", d)
    # replaced: a supersedes link
    e = decide(backend, "Store all times in UTC")
    f = decide(backend, "Store all times in local time", replaces=e)
    text = brief(backend)
    assert "unsettled" not in text and "in dispute" not in text
    joined = "\n".join(decisions(text))
    for rid, words in ((a, "$6"), (b, "$12"), (c, "JWT"), (d, "cookies"), (f, "local time")):
        assert f"#{rid} " in joined and words in joined
    assert f"(replaces #{e})" in joined and "in UTC" not in joined


def test_older_outside_the_list_gets_its_pointer_on_the_newer_line(backend, seeded_db):
    db = seeded_db
    old = decide(backend, "Ship the relay in the spring")
    backdate(db, old, "2025-01-01 00:00:00")
    for i in range(5):
        decide(backend, f"Unrelated choice number {i} about colours")
    new = decide(backend, "Ship the relay in the autumn")
    pair(db, new, old)
    lines = decisions(brief(backend))
    assert not [ln for ln in lines if ln.startswith(f"- 2025-01-01 #{old} ")]
    [newer] = [ln for ln in lines if f"#{new} " in ln]
    assert (f"(unsettled: may conflict with #{old}; #{old} in dispute: "
            f'search(query="D{old}"))') in newer
    assert "spring" not in "\n".join(lines)


def test_older_in_the_list_but_newer_not(backend, seeded_db):
    db = seeded_db
    old = decide(backend, "Keep the brief short")
    new = decide(backend, "Keep the brief long")
    backdate(db, new, "2025-01-01 00:00:00")   # newer by id, but out of the recent five
    for i in range(4):
        decide(backend, f"Unrelated choice number {i} about colours")
    sql(db, "DELETE FROM decision_conflicts")
    pair(db, new, old)
    lines = decisions(brief(backend))
    assert not [ln for ln in lines if f"#{new} " in ln]
    [older] = [ln for ln in lines if f"#{old} " in ln]
    assert f"#{old} in dispute with #{new}" in older and "short" not in older


def test_an_older_one_in_several_pairs_gets_one_pointer(backend, seeded_db):
    db = seeded_db
    old = decide(backend, "Release every two weeks")
    backdate(db, old, "2025-01-01 00:00:00")
    for i in range(4):
        decide(backend, f"Unrelated choice number {i} about colours")
    x = decide(backend, "Release every month")
    y = decide(backend, "Release every quarter")
    sql(db, "DELETE FROM decision_conflicts")
    pair(db, x, old)
    pair(db, y, old)
    text = brief(backend)
    lines = decisions(text)
    assert sum(f"#{old} in dispute" in ln for ln in lines) == 1
    assert all(f"(unsettled: may conflict with #{old}" in ln
               for ln in lines if f"#{x} " in ln or f"#{y} " in ln)
    assert text.count(f'search(query="D{old}")') == 1
    # in the list itself: one pointer line naming both newer ones
    backdate(db, old, "2099-01-01 00:00:00")
    lines = decisions(brief(backend))
    [older] = [ln for ln in lines if f"#{old} " in ln]
    assert f"in dispute with #{y}, #{x}" in older
    assert sum(f"#{old} in dispute" in ln for ln in decisions(brief(backend))) == 1


def test_tags_stay_within_the_brief_budgets(backend, seeded_db):
    db = seeded_db
    for i in range(6):
        ok(call(backend, "log_work", {"project": "Alpha", "kind": "todo",
                                      "summary": f"Todo number {i} for the budget check"}))
    long = " and ".join(["a rather long clause about the storage layer"] * 6)
    ids = [decide(backend, f"Decision {i}: {long}") for i in range(14)]
    sql(db, "DELETE FROM decision_conflicts")
    for new in ids[7:]:
        for old in ids[:4]:
            pair(db, new, old)
    for size, cap, share in (("standard", 4000, 450), ("quick", 1500, 260), ("deep", 12000, 1500)):
        text = brief(backend, size)
        assert len(text) <= cap
        assert "Todo number 5 for the budget check" in text      # other sections keep their room
        lines = decisions(text)
        assert lines and any("unsettled" in ln for ln in lines)
        assert sum(len(ln) + 1 for ln in lines) <= share
        assert text.count("Possible conflicts:") == 1
