"""Phase D continuation E5-E7: where a record came from (source), last-verified dates and
stale facts, and the local `ask` tool. Plus migrations 009-011 are additive. Temp DBs only."""

import asyncio
import hashlib
import json
import sqlite3

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import migrations as M


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _x(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        conn.execute(sql, a)
        conn.commit()
    finally:
        conn.close()


def call(S, name, args, client="Claude Desktop"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def brief(S, project="Alpha"):
    return call(S, "start_here", {"project": project}).content[0].text


# ---------------------------------------------------------------- E5 source
def test_source_on_log_work_shown_compactly(backend, seeded_db):
    S = backend
    r = _j(call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                                "summary": "Alex wants plain-language reports"}))
    lid = r["record"]["id"]
    assert r["source"] == "user_stated"
    assert _q(seeded_db, "SELECT source, verified_at FROM record_meta WHERE record_type='learning' "
                         "AND record_id=?", lid) == [("user_stated", None)]
    call(S, "log_work", {"project": "Alpha", "kind": "decision", "source": "ai_inferred",
                         "summary": "Use SQLite WAL later"})
    text = brief(S)
    assert "Alex wants plain-language reports (user)" in text
    assert "Use SQLite WAL later (AI)" in text
    assert "Alpha lesson (" not in text                      # old rows: unknown, no tag
    bad = S.handle_call("log_work", {"project": "Alpha", "kind": "todo", "summary": "x",
                                     "source": "rumour"})
    assert bad["error"] == "bad_source"


def test_source_survives_review_queue_and_git_capture_is_from_code(backend, seeded_db):
    S = backend
    q = _j(call(S, "log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                "source": "ai_inferred", "summary": "Exports are slow on big projects"}))
    item = q["queued_for_review"]
    item = item["id"] if isinstance(item, dict) else item
    a = _j(call(S, "review_approve", {"project": "Alpha", "item_id": item, "user_request": "approve it"}))
    assert _q(seeded_db, "SELECT source FROM record_meta WHERE record_type='learning' AND record_id=?",
              a["record"]["id"]) == [("ai_inferred",)]
    r = S._save_commit(1, "Fix the export crash", "commit abc123")
    cid = r["record"]["id"]
    assert _q(seeded_db, "SELECT source FROM record_meta WHERE record_type='change' AND record_id=?",
              cid) == [("from_code",)]


def test_verify_source_and_mark(backend, seeded_db):
    S = backend
    r = _j(call(S, "verify", {"action": "source", "project": "Alpha", "record_type": "learning",
                              "record_id": 1, "source": "from_code"}))
    assert r["source"] == "from_code" and "verified_at" not in r
    m = _j(call(S, "verify", {"action": "mark", "project": "Alpha", "record_type": "learning",
                              "record_id": 1}))
    assert m["verified_at"] and m["source"] == "from_code"
    assert "(code, checked " in brief(S)
    wrong = call(S, "verify", {"action": "mark", "project": "Beta Project", "record_type": "learning",
                               "record_id": 1})
    assert wrong.isError and _j(wrong)["error"] == "project_mismatch"
    missing = call(S, "verify", {"action": "mark", "project": "Alpha", "record_type": "todo",
                                 "record_id": 999})
    assert missing.isError and _j(missing)["error"] == "record_not_found"
    ro = call(S, "verify", {"action": "mark", "record_type": "todo", "record_id": 1})
    assert ro.isError and _j(ro)["error"] == "project_required"


# ---------------------------------------------------------------- E6 stale facts
def test_stale_facts_listed_in_brief_and_cleared_by_mark(backend, seeded_db):
    S = backend
    old = "2026-01-01 10:00:00"
    _x(seeded_db, "INSERT INTO learnings (project_id, insight, category, created_at) VALUES "
                  "(1, 'LAN server listens on 10.0.0.5:8766', 'tool_tip', ?)", old)
    _x(seeded_db, "INSERT INTO learnings (project_id, insight, category, created_at) VALUES "
                  "(1, 'Prefer short commit messages', 'pattern', ?)", old)       # not a fact
    _x(seeded_db, "INSERT INTO learnings (project_id, insight, category) VALUES "
                  "(1, 'Config lives in ~/.codex/config.toml', 'tool_tip')")     # fact, but new
    _x(seeded_db, "INSERT INTO project_variables (project_id, category, name, value, created_at, "
                  "updated_at) VALUES (1, 'server', 'LAN host', 'secret-ish', ?, ?)", old, old)
    st = _j(call(S, "verify", {"action": "stale", "project": "Alpha"}))
    got = {(s["record_type"], s["text"][:20]) for s in st["stale"]}
    assert ("learning", "LAN server listens o") in got and ("variable", "LAN host config") in got
    assert st["total"] == 2 and all("secret-ish" not in s["text"] for s in st["stale"])
    text = brief(S)
    assert "Needs re-check: 2 facts" in text and "verify(action=\"stale\")" in text

    lid = _q(seeded_db, "SELECT id FROM learnings WHERE insight LIKE 'LAN server%'")[0][0]
    call(S, "verify", {"action": "mark", "project": "Alpha", "record_type": "learning", "record_id": lid})
    vid = _q(seeded_db, "SELECT id FROM project_variables")[0][0]
    call(S, "verify", {"action": "mark", "project": "Alpha", "record_type": "variable", "record_id": vid})
    assert _j(call(S, "verify", {"action": "stale", "project": "Alpha"}))["total"] == 0
    assert "Needs re-check" not in brief(S)
    # the new fact is not stale even with a 1-day window
    assert _j(call(S, "verify", {"action": "stale", "project": "Alpha", "days": 1}))["total"] == 0


# ---------------------------------------------------------------- E7 ask
def test_ask_answers_with_ids_and_says_when_it_has_nothing(backend, seeded_db):
    S = backend
    call(S, "log_work", {"project": "Alpha", "kind": "learning", "source": "user_stated",
                         "summary": "The LAN server uses port 8766 on 10.0.0.5",
                         "details": "Public listener is 8767."})
    call(S, "rules", {"action": "add", "project": "Alpha", "rule": "Never restart the public listener"})
    r = _j(call(S, "ask", {"question": "Which port does the LAN server use?", "project": "Alpha"}))
    assert "port 8766" in r["answer"] and "[learning #" in r["answer"] and "user" in r["answer"]
    assert r["sources"][0]["type"] in ("learning", "rule") and r["mode"] == "keywords"
    r2 = _j(call(S, "ask", {"question": "Can I restart the public listener?", "project": "Alpha"}))
    assert "hard rule R1" in r2["answer"]
    every = _j(call(S, "ask", {"question": "what about the alpha bug?"}))
    assert any(s["type"] == "problem" for s in every["sources"]) and ", Alpha" in every["answer"]
    none = _j(call(S, "ask", {"question": "What is the Kubernetes ingress hostname?", "project": "Alpha"}))
    assert none["sources"] == [] and "nothing on this" in none["answer"]
    bad = call(S, "ask", {"question": "x", "project": "Nope"})
    assert bad.isError and _j(bad)["error"] == "project_not_found"


def test_ask_skips_replaced_decisions(backend):
    S = backend
    old = _j(call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                  "summary": "Deploy target is Heroku"}))["record"]["id"]
    call(S, "log_work", {"project": "Alpha", "kind": "decision", "replaces": old,
                         "summary": "Deploy target is Fly.io"})
    r = _j(call(S, "ask", {"question": "deploy target", "project": "Alpha"}))
    assert "Fly.io" in r["answer"] and "Heroku" not in r["answer"]


def test_ask_is_read_only_and_works_for_read_only_apps(backend):
    from flowstate.remote_auth import refusal
    assert refusal("ask", {"question": "x"}, ["flowstate:read"]) is None
    assert refusal("handoff", {"action": "leave"}, ["flowstate:read"])[0] == "read_only_client"
    assert refusal("rules", {"action": "list"}, ["flowstate:read"]) is None


# ---------------------------------------------------------------- migrations 009-011
def _hash_all(db):
    conn = sqlite3.connect(db)
    try:
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {n: hashlib.sha256(json.dumps(sorted(map(repr, conn.execute(f'SELECT * FROM "{n}"')
                                                    .fetchall()))).encode()).hexdigest()
                for n in names}
    finally:
        conn.close()


def test_new_migrations_only_add_tables(seeded_db, tmp_path):
    conn = sqlite3.connect(seeded_db)
    for t in ("project_rules", "handoff_notes", "record_meta"):
        conn.execute(f"DROP TABLE {t}")
    conn.execute("DELETE FROM schema_migrations WHERE version >= 9")
    conn.commit()
    conn.close()
    before = _hash_all(seeded_db)
    res = M.migrate(seeded_db, tmp_path / "bk")
    assert res["applied"][:3] == ["009_hard_rules", "010_handoffs", "011_record_meta"]
    assert res["applied"][3:] == [f"{v:03d}_{n}" for v, n, _p in M.available() if v > 11]
    after = _hash_all(seeded_db)
    assert set(after) - set(before) == {"project_rules", "handoff_notes", "record_meta"}
    for name, h in before.items():
        if name != "schema_migrations":
            assert after[name] == h, name                    # existing rows unchanged
    assert _q(seeded_db, "PRAGMA integrity_check") == [("ok",)]


def test_features_degrade_without_new_tables(backend, seeded_db):
    S = backend
    conn = sqlite3.connect(seeded_db)
    for t in ("project_rules", "handoff_notes", "record_meta"):
        conn.execute(f"DROP TABLE {t}")
    conn.commit()
    conn.close()
    text = brief(S)
    assert text.startswith("FlowState brief: Alpha") and "HARD RULES" not in text
    r = call(S, "rules", {"action": "add", "project": "Alpha", "rule": "x"})
    assert r.isError and _j(r)["error"] == "rules_unavailable"
    ok = _j(call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "still saved",
                                 "source": "user_stated"}))
    assert ok["record"]["id"]
    assert "sources" in _j(call(S, "ask", {"question": "still saved", "project": "Alpha"}))
