"""Clean-up suggestions (Phase D4e): duplicates, stale todos, misfiled records go to the
review queue only; approving applies one suggestion (undoable); rejected ones don't come
back. Temp DBs only."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import cleanup


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
    # The user's go-ahead comes in through another app: with review on, an app can't
    # decide what it queued itself (test_user_approval.py covers that).
    if client == "Claude Desktop" and (name in ("review_approve", "review_reject") or (
            name == "manage_review" and args.get("action") in ("approve", "reject"))):
        client = "Claude Code"
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


@pytest.fixture
def messy(fresh_db):
    """FlowState with a few records that belong to the VPN and kernel projects."""
    from flowstate import server as S
    from flowstate.database import Database
    d = Database(fresh_db)
    fs = d.create_project("FlowState", "Project memory MCP server with review queue")
    vpn = d.create_project("Atlas VPN", "WireGuard style VPN with custom protocol")
    os_ = d.create_project("Atlas OS", "Linux kernel distro hardening")
    d.create_component(fs.id, "Review queue")
    d.create_component(fs.id, "MCP server")
    for n in ("VPN handler", "Tunnel transport", "Device activation"):
        d.create_component(vpn.id, n)
    for n in ("Kernel hardening", "Pentest suite"):
        d.create_component(os_.id, n)
    for t in ("VPN handler drops tunnel on reconnect", "Tunnel transport UDP fallback",
              "Device activation limits per account", "VPN tunnel keepalive interval"):
        d.log_learning(vpn.id, t, "gotcha")
    for t in ("Kernel hardening flags for pentest", "Pentest kernel kptr_restrict"):
        d.log_learning(os_.id, t, "security")
    dfo = d.create_project("DialectForge OS", "Personal operating system with git based sync")
    d.create_component(dfo.id, "Sync engine")
    for t in ("Git push and pull after every commit keeps devices in step",
              "Commit before designing; pull, then push", "Personal sync pull push commit order"):
        d.log_learning(dfo.id, t, "pattern")
    d.log_learning(fs.id, "Review queue items wait for the user", "pattern")
    misfiled = d.log_learning(fs.id, "VPN handler tunnel reconnect needs device activation check",
                              "gotcha")
    # like learning #8 of 2026-10-17: resembles DialectForge OS's records, names nothing there
    generic = d.log_learning(fs.id, "Git-based sync is simpler than cloud services: commit, push, pull",
                             "pattern", context="designing personal")
    both = d.log_learning(fs.id, "FlowState note: VPN handler tunnel reconnect needs device activation",
                          "gotcha")
    kern = d.add_todo(fs.id, "Kernel hardening pentest follow-up", None, "high")
    d.add_todo(fs.id, "Write the review queue docs", None, "medium")
    d.add_todo(fs.id, "Write the review queue docs page", None, "medium")
    S.init_backend(fresh_db)
    assert S.backend.error is None
    yield S, fresh_db, {"fs": fs.id, "vpn": vpn.id, "os": os_.id, "misfiled": misfiled.id,
                        "kern": kern.id, "generic": generic.id, "both": both.id}
    S.backend.tracker = None


def test_finds_misfiled_duplicates_and_stale(messy):
    S, db, ids = messy
    conn = sqlite3.connect(db)
    conn.execute("UPDATE todos SET created_at = '2025-01-01 00:00:00' WHERE title LIKE 'Kernel%'")
    conn.commit()
    conn.close()
    found = cleanup.suggest(db, ids["fs"])
    types_ = [(s["type"], tuple(map(tuple, s["records"]))) for s in found]
    assert ("misfiled", (("learning", ids["misfiled"]),)) in types_
    assert ("misfiled", (("todo", ids["kern"]),)) in types_
    mis = {tuple(s["records"][0]): s for s in found if s["type"] == "misfiled"}
    assert mis[("learning", ids["misfiled"])]["target"] == "Atlas VPN"
    assert mis[("todo", ids["kern"])]["target"] == "Atlas OS"
    assert any(s["type"] == "duplicate" for s in found)
    assert any(s["type"] == "stale" and s["records"] == [["todo", ids["kern"]]] for s in found)
    # the record that does belong here is not flagged
    assert not any(s["type"] == "misfiled" and "Review queue items" in s["summary"] for s in found)


def test_suggest_tool_only_queues(messy):
    S, db, ids = messy
    before = _q(db, "SELECT project_id, id FROM learnings ORDER BY id")
    r = _j(call(S, "cleanup_suggest", {"project": "FlowState"}))
    assert r["queued"] >= 3 and r["already_suggested"] == 0
    assert _q(db, "SELECT project_id, id FROM learnings ORDER BY id") == before   # nothing moved
    again = _j(call(S, "cleanup_suggest", {"project": "FlowState"}))
    assert again["queued"] == 0 and again["already_suggested"] == r["queued"]    # no repeats
    items = _j(call(S, "review_list", {"project": "FlowState"}))["items"]
    assert items and all(", clean-up idea): " in i for i in items)   # one line per item


def _item(db, kind_sql):
    return _q(db, f"SELECT id, payload FROM review_items WHERE kind='cleanup' AND {kind_sql}")[0]


def test_approve_moves_and_can_be_undone(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    iid, _ = _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")
    r = _j(call(S, "review_approve", {"project": "FlowState", "item_id": iid,
                                      "user_request": "User: yes, move that one"}))
    assert r["record"] == {"table": "learnings", "id": ids["misfiled"]}
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["vpn"],)]
    call(S, "history", {"action": "undo", "project": "Atlas VPN", "record_type": "learning",
                        "record_id": ids["misfiled"]})
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["fs"],)]


def test_approve_with_other_target_and_reject_is_remembered(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    iid, _ = _item(db, f"payload LIKE '%todo#{ids['kern']}%'")
    call(S, "review_approve", {"project": "FlowState", "item_id": iid, "move_to": "Atlas VPN",
                               "user_request": "User: move it to the VPN project instead"})
    assert _q(db, "SELECT project_id FROM todos WHERE id = ?", ids["kern"]) == [(ids["vpn"],)]
    other, _ = _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")
    call(S, "review_reject", {"project": "FlowState", "item_id": other, "reason": "it's fine here",
                              "user_request": "User: no, keep that"})
    assert _q(db, "SELECT project_id FROM learnings WHERE id = ?", ids["misfiled"]) == [(ids["fs"],)]
    again = _j(call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]}))
    assert again["queued"] == 0        # the rejected one is not suggested again


def test_duplicate_todo_is_cancelled_not_deleted(messy):
    S, db, ids = messy
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["duplicate"]})
    iid, payload = _item(db, "payload LIKE '%duplicate%'")
    newer = json.loads(payload)["records"][-1][1]
    call(S, "review_approve", {"project": "FlowState", "item_id": iid,
                               "user_request": "User: approve the duplicate clean-up"})
    assert _q(db, "SELECT status FROM todos WHERE id = ?", newer) == [("cancelled",)]


def _misfiled(db, pid):
    return {tuple(s["records"][0]): s for s in cleanup.suggest(db, pid, ("misfiled",))}


def test_only_sure_moves_that_name_the_target(messy, monkeypatch):
    S, db, ids = messy
    mis = _misfiled(db, ids["fs"])
    for key, target, part in ((("learning", ids["misfiled"]), "Atlas VPN", "VPN handler"),
                              (("todo", ids["kern"]), "Atlas OS", "Kernel hardening")):
        s = mis[key]
        assert s["target"] == target and s["confidence"] == "likely" and "also_possible" not in s
        assert s["reason"] == f'it mentions {target}\'s "{part}" component'
    # resembling another project's records is not enough, nor naming it next to its own project
    assert ("learning", ids["generic"]) not in mis and ("learning", ids["both"]) not in mis
    # ... and it is the name check that holds them back: without it both would be asked
    real = cleanup._names
    monkeypatch.setattr(cleanup, "_names", lambda text, name: name != "FlowState")
    loose = _misfiled(db, ids["fs"])
    assert loose[("learning", ids["generic"])]["target"] == "DialectForge OS"
    monkeypatch.setattr(cleanup, "_names", lambda text, name: name != "FlowState" and real(text, name))
    assert ("learning", ids["both"]) in _misfiled(db, ids["fs"])


def test_each_item_is_one_plain_question(messy):
    S, db, ids = messy
    conn = sqlite3.connect(db)
    conn.execute("UPDATE todos SET created_at = '2025-01-01 00:00:00' WHERE title LIKE 'Kernel%'")
    conn.commit()
    conn.close()
    found = cleanup.suggest(db, ids["fs"])
    mis = [s for s in found if s["type"] == "misfiled" and s["records"] == [["learning", ids["misfiled"]]]][0]
    assert mis["summary"] == ('Move learning "VPN handler tunnel reconnect needs de..." from FlowState '
                              'to Atlas VPN? It mentions Atlas VPN\'s "VPN handler" component.')
    assert mis["summary"].startswith('Move learning "') and len(mis["summary"]) <= 150
    assert mis["details"] == ("Approve moves it (undo puts it back). Reject keeps it in FlowState; "
                              "FlowState won't ask again.")
    assert "move_to" not in mis["summary"] + mis["details"]
    stale = [s for s in found if s["type"] == "stale"][0]
    assert stale["summary"] == ('Cancel todo "Kernel hardening pentest follow-up" in FlowState? '
                                'It has waited since 2025-01-01 (over 120 days).')
    dup = [s for s in found if s["type"] == "duplicate"][0]
    assert dup["summary"] == ('Cancel todo "Write the review queue docs page" in FlowState? '
                              'It repeats todo "Write the review queue docs".')
    assert all(len(s["summary"]) <= 150 for s in found)


def test_no_duplicate_items_for_learnings_or_problems(messy):
    S, db, ids = messy
    from flowstate.database import Database
    d = Database(db)
    d.log_learning(ids["fs"], "Review queue items wait for the user, always", "pattern")
    comp = d.create_component(ids["fs"], "Docs")
    for _ in range(2):
        d.log_problem(comp.id, "Docs build fails on Windows paths", "same")
    found = cleanup.suggest(db, None, ("duplicate",))
    assert found and all(s["records"][0][0] == "todo" for s in found)
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["duplicate"]})
    kinds = {json.loads(p)["records"][0][0] for (p,) in _q(db, "SELECT payload FROM review_items "
                                                              "WHERE kind='cleanup'")}
    assert kinds == {"todo"}


def _tables(db):
    return {t: _q(db, f"SELECT * FROM {t} ORDER BY id") for t in
            ("todos", "learnings", "problems", "changes", "projects", "components", "conversations")}


def _old_item(db, pid, rec, confidence, **extra):
    """A misfiled item in the payload shape older versions queued (no reason)."""
    from flowstate import review
    return review.add_item(db, pid, "cleanup", "Misfiled? ... looks like Atlas VPN (or Atlas OS)",
                           f"Confidence: {confidence}.",
                           {"type": "misfiled", "records": [list(rec)], "target": "Atlas VPN",
                            "also_possible": "Atlas OS" if confidence == "unsure" else None,
                            "confidence": confidence, "words": ["activation", "device"],
                            "fingerprint": f"misfiled:{rec[0]}#{rec[1]}", **extra}, "cleanup (cli)")


def test_withdraw_unsure_items_from_older_versions(messy):
    S, db, ids = messy
    unsure = _old_item(db, ids["fs"], ("learning", ids["misfiled"]), "unsure", target_project_id=ids["vpn"])
    likely = _old_item(db, ids["fs"], ("todo", ids["kern"]), "likely", target_project_id=ids["os"])
    before = _tables(db)
    assert cleanup.withdraw_unsure(db) == 1
    assert _tables(db) == before                       # no record changed, only the review item
    rows = {i: (s, r) for i, s, r in _q(db, "SELECT id, status, reject_reason FROM review_items")}
    assert rows[unsure] == ("rejected", cleanup.WITHDRAWN)
    assert rows[likely] == ("pending", None)
    assert _q(db, "SELECT decided_at IS NOT NULL FROM review_items WHERE id = ?", unsure) == [(1,)]
    assert cleanup.withdraw_unsure(db) == 0
    # a later run withdraws a new leftover, says so, and doesn't ask about the withdrawn one again
    other = _old_item(db, ids["fs"], ("learning", 999), "unsure")
    r = _j(call(S, "manage_memory", {"action": "cleanup", "project": "FlowState", "kinds": ["misfiled"]}))
    assert r["withdrew"] == 1 and r["queued"] == 0, r
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", other) == [("rejected",)]
    assert _q(db, "SELECT count(*) FROM review_items WHERE payload LIKE ?",
              f"%learning#{ids['misfiled']}%") == [(1,)]


def test_cli_queue_says_what_it_withdrew(messy, capsys):
    S, db, ids = messy
    from flowstate import cli
    _old_item(db, ids["fs"], ("learning", 999), "unsure")
    assert cli.main(["cleanup", "--db", str(db), "--project", "FlowState", "--queue"]) == 0
    out = capsys.readouterr().out
    assert "withdrew 1 unsure" in out and '- Move learning "VPN handler' in out, out


def test_withdraw_at_startup(messy):
    S, db, ids = messy
    iid = _old_item(db, ids["fs"], ("learning", ids["misfiled"]), "unsure")
    S.init_backend(db)
    assert S.backend.error is None
    assert _q(db, "SELECT status, reject_reason FROM review_items WHERE id = ?", iid) == [
        ("rejected", cleanup.WITHDRAWN)]


def _queued_move(S, db, ids):
    call(S, "cleanup_suggest", {"project": "FlowState", "kinds": ["misfiled"]})
    return _item(db, f"payload LIKE '%learning#{ids['misfiled']}%'")[0]


def test_phone_and_approval_wording_is_the_move_question(messy):
    S, db, ids = messy
    from flowstate import approvals, review, review_phone
    iid = _queued_move(S, db, ids)
    item = review.get_item(db, iid)
    ask = approvals.describe(item, "FlowState", db)
    assert ask == 'move the learning "VPN handler tunnel reconnect needs de..." from FlowState to Atlas VPN'
    assert approvals.describe(item, "FlowState") == f"move learning #{ids['misfiled']} from FlowState to Atlas VPN"
    phone = [v for v in review_phone.items(db) if v["id"] == iid][0]
    assert phone["question"] == f'Should FlowState {ask}? It mentions Atlas VPN\'s "VPN handler" component.'
    assert "Approve to keep it" not in json.dumps(review_phone.items(db))


def test_review_page_has_one_move_button(messy):
    S, db, ids = messy
    from flowstate.review_page import Page
    iid = _queued_move(S, db, ids)
    html = Page(db).render("csrf")
    block = html[html.index(f"#{iid} · cleanup"):]
    block = block[:block.index("</div>", block.index("Reject"))]
    assert '<textarea name="summary"' not in block and "Approve: move it" in block
    assert 'name="move_to"' in block and "Or move it to" in block
    assert "Move learning" in block


def test_list_line_keeps_the_move_at_limit_100(messy):
    S, db, ids = messy
    from flowstate import review
    iid = _queued_move(S, db, ids)
    for i in range(99):
        review.add_item(db, ids["fs"], "session_summary", f"Filler summary {i} " + "x" * 300, "d")
    r = _j(call(S, "manage_review", {"action": "list", "project": "FlowState", "limit": 100}))
    assert len(r["items"]) == 100
    line = [x for x in r["items"] if x.startswith(f"#{iid} ")][0]
    assert "from FlowState to Atlas VPN" in line, line
