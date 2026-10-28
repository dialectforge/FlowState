"""Task 67: evidence changes are in the edit history (before, after, who) and undo brings the
old evidence back. Temp databases only."""

import asyncio
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    text = r.content[0].text
    try:
        return r, json.loads(text)
    except ValueError:
        return r, text


def ok(res):
    r, body = res
    assert not r.isError, body
    return body


def rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def evidence(db, rid, typ="decision"):
    return rows(db, "SELECT label, excerpt FROM entry_evidence WHERE record_type = ? "
                    "AND record_id = ? ORDER BY position", typ, rid)


def evidence_history(db, rid, typ="decision"):
    return rows(db, """SELECT tool, old_row, new_row, app FROM record_history
                       WHERE table_name = 'entry_evidence'
                       AND json_extract(COALESCE(new_row, old_row), '$.record_type') = ?
                       AND json_extract(COALESCE(new_row, old_row), '$.record_id') = ?
                       ORDER BY id""", typ, rid)


def flag(S, a, b, ev, tev=None, app="Cursor"):
    args = {"action": "flag_conflict", "project": "Alpha", "topic": "API sessions",
            "item_type": "decision", "item_id": a, "target_type": "decision", "target_id": b,
            "evidence": ev}
    if tev:
        args["target_evidence"] = tev
    return ok(call(S, "manage_links", args, app=app))


def two_decisions(S):
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use JWT tokens for API auth",
                                "evidence": ["src/auth.py:42 — tokens are signed"]},
                app="Claude"))["record"]["id"]
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Use session cookies for API auth"}))["record"]["id"]
    return a, b


def test_replacing_evidence_keeps_the_old_value_in_history(backend, seeded_db):
    S = backend
    a, b = two_decisions(S)
    assert evidence(seeded_db, a) == [("src/auth.py:42", "tokens are signed")]
    flag(S, a, b, ["src/session.py:7 — cookie wins"], app="Cursor")
    assert evidence(seeded_db, a) == [("src/session.py:7", "cookie wins")]
    hist = evidence_history(seeded_db, a)
    olds = [json.loads(o) for _t, o, _n, _a in hist if o]
    news = [json.loads(n) for _t, _o, n, _a in hist if n]
    assert any(o["label"] == "src/auth.py:42" and o["excerpt"] == "tokens are signed" for o in olds)
    assert any(n["label"] == "src/session.py:7" for n in news)
    replace = [h for h in hist if h[1] and not h[2]]   # the old row went
    assert replace and replace[0][3] == "Cursor"       # with who did it
    assert replace[0][0] == "evidence/replace"


def test_adding_evidence_at_log_time_is_part_of_the_create_edit(backend, seeded_db):
    S = backend
    a, _b = two_decisions(S)
    hist = evidence_history(seeded_db, a)
    assert len(hist) == 1 and hist[0][1] is None and hist[0][3] == "Claude"
    edit = rows(seeded_db, "SELECT DISTINCT edit_id FROM record_history WHERE "
                           "(table_name = 'conversations' AND record_id = ?) OR "
                           "(table_name = 'entry_evidence')", a)
    assert len(edit) == 1   # one save, one edit


def test_undo_through_manage_history_restores_the_old_evidence(backend, seeded_db):
    S = backend
    a, b = two_decisions(S)
    flag(S, a, b, ["src/session.py:7 — cookie wins"])
    listed = ok(call(S, "manage_history", {"action": "list", "project": "Alpha",
                                           "record_type": "decision", "record_id": a}))
    assert any(h["record_type"] == "evidence" for h in listed["history"])
    undone = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                           "record_type": "decision", "record_id": a}))
    assert undone["tool"] == "evidence/replace"
    assert evidence(seeded_db, a) == [("src/auth.py:42", "tokens are signed")]
    redo = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "decision", "record_id": a}))
    assert redo["tool"] == "history/undo"
    assert evidence(seeded_db, a) == [("src/session.py:7", "cookie wins")]


def test_saving_the_same_evidence_again_adds_no_history(backend, seeded_db):
    S = backend
    a, b = two_decisions(S)
    before = len(evidence_history(seeded_db, a))
    flag(S, a, b, ["src/auth.py:42 — tokens are signed"])
    assert len(evidence_history(seeded_db, a)) == before


def test_undoing_a_created_decision_with_evidence_takes_its_evidence_too(backend, seeded_db):
    S = backend
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Cache API responses for one minute",
                                "evidence": ["src/cache.py:3"]}, app="Claude"))["record"]["id"]
    undone = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                           "record_type": "decision", "record_id": a},
                     app="Claude"))
    assert not undone.get("skipped"), undone
    assert rows(seeded_db, "SELECT COUNT(*) FROM conversations WHERE id = ?", a)[0][0] == 0
    assert evidence(seeded_db, a) == []


def test_secret_inside_evidence_is_refused(backend, seeded_db):
    S = backend
    before = rows(seeded_db, "SELECT COUNT(*) FROM conversations")[0][0]
    secret = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"
    r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                   "summary": "Use JWT", "evidence": [f"output: {secret}"]})
    assert r.isError and body["error"] == "secret_detected" and body["field"] == "evidence[0]"
    a, b = two_decisions(S)
    r, body = call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "x", "item_id": a,
        "target_id": b, "target_evidence": ["src/a.py:1 — " + secret]})
    assert r.isError and body["error"] == "secret_detected"
    assert body["field"] == "target_evidence[0]"
    assert rows(seeded_db, "SELECT COUNT(*) FROM conversations")[0][0] == before + 2


def test_erase_takes_the_replaced_evidence_out_of_the_history(backend):
    from test_t55_erase import _delete, _erase, _everywhere
    S, db = backend, backend.backend.db_path
    old = "the invoices stay in the ledger forever"
    a = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Keep the invoice ledger in Postgres",
                                "evidence": [f"src/ledger.py:9 — {old}"]}))["record"]["id"]
    b = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Archive invoices yearly"}))["record"]["id"]
    flag(S, a, b, ["src/ledger.py:30 — archived after a year"])
    assert any(old in (o or "") for _t, o, _n, _a in evidence_history(db, a))
    S.backend.tracker.close_instance()
    _delete(S, "decision", a)
    _erase(db, f"decision:{a}")
    S.backend.tracker = None
    assert evidence_history(db, a) == []
    assert _everywhere(db, old) == []


# ---------------------------------------------------------------- the same pattern elsewhere
def meta_history(db, rid, typ="learning"):
    return [(t, json.loads(o) if o else None, json.loads(n) if n else None, app)
            for t, o, n, app in rows(db, """SELECT tool, old_row, new_row, app FROM record_history
                WHERE table_name = 'record_meta' AND record_id = ?
                AND json_extract(COALESCE(new_row, old_row), '$.record_type') = ? ORDER BY id""",
                rid, typ)]


def source(db, rid, typ="learning"):
    got = rows(db, "SELECT source, verified_at FROM record_meta WHERE record_type = ? "
                   "AND record_id = ?", typ, rid)
    return got[0] if got else None


def learning(S, src="ai_inferred", app="Cursor"):
    return ok(call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": src,
                                   "summary": "Deploys go out on Fridays only"}, app=app))["record"]["id"]


def test_changing_a_source_tag_keeps_the_old_tag_and_undo_restores_it(backend, seeded_db):
    S = backend
    rid = learning(S)
    ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                 "record_type": "learning", "record_id": rid,
                                 "source": "from_code"}, app="Windsurf"))
    assert source(seeded_db, rid)[0] == "from_code"
    tool, old, new, app = meta_history(seeded_db, rid)[-1]
    assert (tool, old["source"], new["source"], app) == ("source/set", "ai_inferred",
                                                         "from_code", "Windsurf")
    undone = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                           "record_type": "learning", "record_id": rid}))
    assert undone["tool"] == "source/set" and source(seeded_db, rid)[0] == "ai_inferred"


def test_mark_checked_keeps_the_earlier_checked_date(backend, seeded_db):
    S = backend
    rid = learning(S)
    mark = {"action": "mark_checked", "project": "Alpha", "record_type": "learning",
            "record_id": rid}
    ok(call(S, "manage_memory", mark))
    conn = sqlite3.connect(seeded_db)   # as if it was checked months ago
    conn.execute("UPDATE record_meta SET verified_at = '2026-01-02 03:04:05' "
                 "WHERE record_type = 'learning' AND record_id = ?", (rid,))
    conn.commit()
    conn.close()
    ok(call(S, "manage_memory", mark))
    checks = [h for h in meta_history(seeded_db, rid) if h[0] == "source/checked"]
    assert checks[0][1]["verified_at"] is None and checks[0][2]["verified_at"]
    assert checks[-1][1]["verified_at"] == "2026-01-02 03:04:05"
    assert checks[-1][2]["verified_at"] > "2026-01-02 03:04:05"


def test_undo_never_takes_back_or_makes_user_stated(backend, seeded_db):
    from flowstate import popup
    S = backend
    rid = learning(S)
    item = ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                        "record_type": "learning", "record_id": rid,
                                        "source": "user_stated"}))["confirm_requested"]
    done = popup.as_user("review_approve", {"item_id": item, "user_request":
                         "The user confirmed this in their own terminal."}, "command-line")
    assert "error" not in done, done
    assert source(seeded_db, rid)[0] == "user_stated"
    tool, old, new, _app = meta_history(seeded_db, rid)[-1]
    assert (tool, old["source"], new["source"]) == ("source/confirm", "ai_inferred", "user_stated")
    r, body = call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "learning", "record_id": rid})
    assert r.isError and body["error"] == "user_only", body
    assert source(seeded_db, rid)[0] == "user_stated"
    # a downgrade by an app is recorded; undoing it would make user_stated again: refused too
    ok(call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                 "record_type": "learning", "record_id": rid,
                                 "source": "ai_inferred"}, app="Windsurf"))
    r, body = call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "learning", "record_id": rid})
    assert r.isError and body["error"] == "user_only"
    assert source(seeded_db, rid)[0] == "ai_inferred"


def test_undoing_a_created_record_takes_its_log_time_tag(backend, seeded_db):
    S = backend
    rid = learning(S, src="user_stated", app="Claude")
    hist = meta_history(seeded_db, rid)
    assert len(hist) == 1 and hist[0][1] is None and hist[0][3] == "Claude"
    undone = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                           "record_type": "learning", "record_id": rid},
                     app="Claude"))
    assert not undone.get("skipped"), undone
    assert source(seeded_db, rid) is None


def test_a_second_card_on_the_same_entry_keeps_the_first_mark_in_history(backend, seeded_db):
    S = backend
    ask = "User: keep the older entry, the JWT one"
    a, b = two_decisions(S)
    first = flag(S, a, b, ["src/auth.py:42"])["flagged"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": first,
                                 "keep": "a", "user_request": ask}, app="Claude Code"))
    assert rows(seeded_db, "SELECT by_id FROM entry_standing WHERE record_id = ?", b) == [(a,)]
    c = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                "summary": "Rotate API tokens daily"}))["record"]["id"]
    second = ok(call(S, "manage_links", {
        "action": "flag_conflict", "project": "Alpha", "topic": "API tokens",
        "item_id": b, "target_id": c}))["flagged"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": second,
                                 "keep": "b", "user_request": "User: keep the newer one, rotate daily"},
            app="Claude Code"))
    assert rows(seeded_db, "SELECT by_id FROM entry_standing WHERE record_id = ?", b) == [(c,)]
    hist = rows(seeded_db, """SELECT tool, old_row, new_row FROM record_history
                              WHERE table_name = 'entry_standing' AND record_id = ? ORDER BY id""", b)
    assert [h[0] for h in hist] == ["card/keep", "card/keep"]
    assert json.loads(hist[1][1])["by_id"] == a and json.loads(hist[1][2])["by_id"] == c
    r, body = call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                         "record_type": "decision", "record_id": b})
    assert r.isError and body["error"] == "user_only"
    assert rows(seeded_db, "SELECT by_id FROM entry_standing WHERE record_id = ?", b) == [(c,)]
