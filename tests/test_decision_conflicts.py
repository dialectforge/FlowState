"""Possible conflicting decisions (2.3.0, migration 014): the fixture set that tunes the
threshold, the note on save, replaces= (skips the check, resolves pairs), no duplicate pairs,
the brief line, the review queue (approve = they conflict, reject = compatible, user's words
required), the review mode, other kinds unchanged, history, meaning search (optional), the
v2.2.0 migration and the timing bound. In-process only, temp databases only."""

import asyncio
import itertools
import json
import sqlite3
import statistics
import time

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import conflicts, meaning, migrations as M

ASK = "User: yes, those two are compatible, dismiss it"

# ---------------------------------------------------------------- tuning fixtures
# Near-contradictions on the same component: each pair MUST be flagged.
CONTRADICTIONS = [
    ("Storage", "Use SQLite for hosted storage", "Use Postgres for hosted storage"),
    ("Pricing", "Price Relay at $6", "Price Relay at $12"),
    ("Auth", "Use JWT tokens for API auth", "Use session cookies for API auth"),
    ("Web", "Deploy the web app on Vercel", "Deploy the web app on Cloudflare Pages"),
    ("Core", "Store all times in UTC", "Store all times in local time"),
    ("Release", "Support Windows 10", "Do not support Windows 10"),
    ("Release", "Release a new version every two weeks", "Release a new version every month"),
    ("Review", "Default review mode is auto_only", "Default review mode is everything"),
    ("Brief", "Keep the standard brief under 4,000 characters",
     "Keep the standard brief under 6,000 characters"),
]

# Decisions on the same component that are about different things: NO pair may be flagged.
UNRELATED = {
    "Storage": ["Use SQLite for hosted storage", "Back up the database every night at 02:30",
                "Encrypt hosted storage at rest", "Use WAL mode for SQLite",
                "Run VACUUM monthly on the local database", "Keep uploads under 10 MB",
                "Hosted storage lives in the EU region",
                "Store attachments next to the database, not inside it",
                "Never DROP tables in a migration",
                "Migrations add tables or nullable columns only"],
    "Pricing": ["Price Relay at $6", "Relay beta is free for 10 computers", "Charge in USD only",
                "Offer a yearly Relay plan at $60", "Show the Relay price on the website",
                "Refunds within 14 days, no questions asked"],
    "Auth": ["Use JWT tokens for API auth", "Rotate API keys every 90 days",
             "Log failed API auth attempts",
             "Per-app keys are shown once and only a hash is kept",
             "OAuth sign-in is approved by the owner passphrase",
             "Use the OS credential store for the HTTP token",
             "Bind the HTTP server to 127.0.0.1 behind a tunnel"],
    "Release": ["Release a new version every two weeks", "Write the changelog entry before tagging",
                "Tag releases as vX.Y.Z", "Test on Windows and macOS before every release",
                "Use Python 3.11 or newer", "No Node.js in the server"],
    "Brief": ["Keep the standard brief under 4,000 characters", "Hard rules are never cut from the brief",
              "Show handoff notes right after the hard rules", "Default review mode is auto_only",
              "Session summaries wait in the review queue", "Close idle sessions after 2 hours",
              "Use the existing review queue for delete requests",
              "Deletes only queue a request for the user"],
}


@pytest.fixture(autouse=True)
def _no_real_model():
    meaning.set_embedder(None)
    meaning._State.tried, meaning._State.model = True, None   # behave as "not installed"
    yield
    meaning.set_embedder(None)


def call(S, name, args, app="Cursor"):
    # The user's go-ahead comes in through another app: with review on, an app can't
    # decide what it queued itself (test_user_approval.py covers that).
    if name == "manage_review" and args.get("action") in ("approve", "reject") \
            and app == "Cursor":
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


def decide(S, summary, component=None, project="Alpha", **kw):
    args = {"project": project, "kind": "decision", "summary": summary, **kw}
    if component:
        args["component"] = component
    return ok(call(S, "log_work", args))


def brief(S, size="standard"):
    return call(S, "start_here", {"project": "Alpha", "size": size})[1]


def rows(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- threshold
def test_fixture_contradictions_score_above_and_unrelated_below_the_threshold():
    true_scores = [conflicts.similarity(a, b) for _c, a, b in CONTRADICTIONS]
    false_scores = [(conflicts.similarity(a, b), a, b) for ds in UNRELATED.values()
                    for a, b in itertools.combinations(ds, 2)]
    assert min(true_scores) >= conflicts.SIMILAR, true_scores
    worst = max(false_scores)
    assert worst[0] < conflicts.SIMILAR, worst
    # a real margin on both sides, not a knife edge
    assert min(true_scores) - conflicts.SIMILAR >= 0.05
    assert conflicts.SIMILAR - worst[0] >= 0.05


def test_contradictions_are_flagged_on_save(backend):
    for comp, old, new in CONTRADICTIONS:
        first = decide(backend, old, comp)
        assert "possible_conflicts" not in first or all(
            f["decision_id"] != first["record"]["id"] for f in first["possible_conflicts"])
        got = decide(backend, new, comp)
        flagged = [f["decision_id"] for f in got.get("possible_conflicts", [])]
        assert first["record"]["id"] in flagged, (old, new, got)


def test_unrelated_decisions_on_the_same_component_are_not_flagged(backend):
    for comp, ds in UNRELATED.items():
        for d in ds:
            got = decide(backend, d, comp)
            assert "possible_conflicts" not in got, (d, got)
    assert rows(backend.backend.db_path, "SELECT COUNT(*) FROM decision_conflicts") == [(0,)]


def test_note_on_save_is_short_and_plain(backend):
    old = decide(backend, "Use Postgres for hosted storage", "Storage")["record"]["id"]
    got = decide(backend, "Use SQLite for hosted storage", "Storage")
    new = got["record"]["id"]
    assert got["logged"] == "decision"                       # saved, not blocked
    [f] = got["possible_conflicts"]
    assert f["decision_id"] == old and f["same_component"] and f["review_item_id"]
    note = got["note"]
    assert note.startswith(f'Possible conflict with D{old} "Use Postgres for hosted storage" '
                           f'(same component).')
    assert f"item_id={new}" in note and f"target_id={old}" in note and "manage_review" in note
    assert "detected" not in note.lower() and len(note) < 420


def test_different_components_need_a_closer_match(backend):
    a, b = "Use JWT tokens for API auth", "Use session cookies for API auth"
    assert conflicts.SIMILAR <= conflicts.similarity(a, b) < conflicts.OTHER_COMPONENT
    decide(backend, a, "Auth")
    assert "possible_conflicts" not in decide(backend, b, "Billing")
    got = decide(backend, b, "Auth")
    assert got["possible_conflicts"][0]["same_component"] is True


def test_same_component_comes_first_and_at_most_three(backend):
    for comp in ("Other1", "Other2", None, None):
        decide(backend, "Keep the standard brief under 4,000 characters", comp)
    same = decide(backend, "Keep the standard brief under 4,000 characters", "Brief")
    got = decide(backend, "Keep the standard brief under 5,000 characters", "Brief")
    flags = got["possible_conflicts"]
    assert len(flags) == conflicts.MAX_FLAGS
    assert flags[0]["decision_id"] == same["record"]["id"] and flags[0]["same_component"]


# ---------------------------------------------------------------- replaces / duplicates
def test_replaces_skips_the_check_and_resolves_open_pairs(backend):
    db = backend.backend.db_path
    old = decide(backend, "Use Postgres for hosted storage", "Storage")["record"]["id"]
    new = decide(backend, "Use SQLite for hosted storage", "Storage")
    item = new["possible_conflicts"][0]["review_item_id"]
    third = decide(backend, "Use SQLite for hosted storage", "Storage", replaces=old)
    assert "possible_conflicts" not in third and "note" not in third
    assert third["replaces"] == old
    assert third["resolved_conflicts"] == [f"D{new['record']['id']} vs D{old}"]
    assert rows(db, "SELECT status, resolved_by_decision FROM decision_conflicts") == \
        [("resolved_replaced", third["record"]["id"])]
    status, reason = rows(db, "SELECT status, reject_reason FROM review_items WHERE id = ?", item)[0]
    assert status == "rejected" and "closed automatically" in reason
    assert "Possible conflicts" not in brief(backend)


def test_a_supersedes_link_resolves_too(backend):
    db = backend.backend.db_path
    old = decide(backend, "Price Relay at $6", "Pricing")["record"]["id"]
    new = decide(backend, "Price Relay at $12", "Pricing")["record"]["id"]
    got = ok(call(backend, "manage_links", {"action": "add", "project": "Alpha",
                                            "link_type": "supersedes", "item_type": "decision",
                                            "item_id": new, "target_type": "decision",
                                            "target_id": old}))
    assert got["resolved_conflicts"] == [f"D{new} vs D{old}"]
    assert rows(db, "SELECT status FROM decision_conflicts") == [("resolved_replaced",)]


def test_the_same_pair_is_never_flagged_twice(backend):
    db = backend.backend.db_path
    old = decide(backend, "Use Postgres for hosted storage", "Storage")["record"]["id"]
    new = decide(backend, "Use SQLite for hosted storage", "Storage")["record"]["id"]
    again = conflicts.check(db, 1, new, "Use SQLite for hosted storage", None)
    assert again == []
    assert rows(db, "SELECT new_id, old_id FROM decision_conflicts") == [(new, old)]
    assert rows(db, "SELECT COUNT(*) FROM review_items WHERE kind = 'conflict'") == [(1,)]


def test_other_kinds_are_not_checked(backend):
    decide(backend, "Use Postgres for hosted storage", "Storage")
    for kind in ("change", "learning", "todo", "problem"):
        got = ok(call(backend, "log_work", {"project": "Alpha", "kind": kind, "component": "Storage",
                                            "summary": "Use SQLite for hosted storage"}))
        assert "possible_conflicts" not in got and "note" not in got
    assert rows(backend.backend.db_path, "SELECT COUNT(*) FROM decision_conflicts") == [(0,)]


def test_a_failing_check_never_loses_the_decision(backend, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(conflicts, "check", boom)
    got = decide(backend, "Use SQLite for hosted storage", "Storage")
    assert got["logged"] == "decision" and "possible_conflicts" not in got
    assert rows(backend.backend.db_path, "SELECT COUNT(*) FROM conversations WHERE id = ?",
                got["record"]["id"]) == [(1,)]


# ---------------------------------------------------------------- brief
def test_brief_shows_open_pairs_newest_first_capped(backend):
    assert "Possible conflicts" not in brief(backend)
    pairs = []
    for comp, a, b in CONTRADICTIONS[:5]:
        x = decide(backend, a, comp)["record"]["id"]
        y = decide(backend, b, comp)["record"]["id"]
        pairs.append(f"D{y} vs D{x}")
    text = brief(backend)
    [line] = [ln for ln in text.splitlines() if ln.startswith("Possible conflicts:")]
    assert line.startswith(f"Possible conflicts: {pairs[4]}, {pairs[3]}, {pairs[2]} (+2 more).")
    assert pairs[1] not in line and len(text) <= 4000
    quick = brief(backend, "quick")
    assert "Possible conflicts:" in quick and len(quick) <= 1500


# ---------------------------------------------------------------- review queue
def _conflict_item(S):
    items = ok(call(S, "manage_review", {"action": "list", "project": "Alpha"}))["items"]
    [line] = [i for i in items if ", possible conflict): " in i]   # one line per item
    return ok(call(S, "manage_review", {"action": "list", "item_id": int(line.split(" ")[0][1:])}))["item"]


def test_reject_in_the_review_queue_dismisses_the_pair(backend):
    db = backend.backend.db_path
    # same project, no component: a yes/no possible-conflict item (a same-component pair
    # opens a disagreement card instead, which takes this item's place)
    decide(backend, "Use Postgres for hosted storage")
    decide(backend, "Use SQLite for hosted storage")
    it = _conflict_item(backend)
    assert it["summary"].startswith("Possible conflict: D")
    # agents can't dismiss on their own: the user's words are required
    err, body = call(backend, "manage_review", {"action": "reject", "project": "Alpha",
                                                "item_id": it["id"], "reason": "compatible"})
    assert err and body["error"] == "user_request_required"
    assert rows(db, "SELECT status FROM decision_conflicts") == [("open",)]
    got = ok(call(backend, "manage_review", {"action": "reject", "project": "Alpha",
                                             "item_id": it["id"], "reason": "compatible",
                                             "user_request": ASK}))
    assert got["rejected"] == it["id"] and "compatible" in got["dismissed"]
    assert rows(db, "SELECT status FROM decision_conflicts") == [("dismissed_compatible",)]
    assert "Possible conflicts" not in brief(backend)


def test_approve_keeps_it_visible_as_a_real_conflict(backend):
    db = backend.backend.db_path
    # same project, no component: a yes/no possible-conflict item (a same-component pair
    # opens a disagreement card instead, which takes this item's place)
    decide(backend, "Price Relay at $6")
    decide(backend, "Price Relay at $12")
    it = _conflict_item(backend)
    got = ok(call(backend, "manage_review", {"action": "approve", "project": "Alpha",
                                             "item_id": it["id"],
                                             "user_request": "User: yes they conflict"}))
    assert got["record"]["table"] == "decision_conflicts" and "replace" in got["note"]
    assert rows(db, "SELECT status, confirmed_at IS NOT NULL FROM decision_conflicts") == \
        [("open", 1)]
    assert "(user says they conflict)" in brief(backend)


def test_review_mode_still_applies(backend):
    db = backend.backend.db_path
    decide(backend, "Use Postgres for hosted storage", "Storage")
    ok(call(backend, "manage_review", {"action": "set_mode", "mode": "types", "kinds": ["decision"],
                                       "user_request": "User: hold my decisions for review"}))
    got = decide(backend, "Use SQLite for hosted storage", "Storage")
    assert "queued_for_review" in got and "record" not in got and "possible_conflicts" not in got
    assert rows(db, "SELECT COUNT(*) FROM decision_conflicts") == [(0,)]
    # once the user approves the decision it is a record, and the check runs then
    ok(call(backend, "manage_review", {"action": "approve", "project": "Alpha",
                                       "item_id": got["queued_for_review"],
                                       "user_request": "User: approve it"}))
    assert rows(db, "SELECT COUNT(*) FROM decision_conflicts WHERE status = 'open'") == [(1,)]


def test_suggested_with_review_mode_off_keeps_both_notes(backend):
    ok(call(backend, "manage_review", {"action": "set_mode", "mode": "off",
                                       "user_request": "User: turn review off"}))
    decide(backend, "Use Postgres for hosted storage", "Storage")
    got = decide(backend, "Use SQLite for hosted storage", "Storage", suggested=True)
    assert got["note"].startswith("Review mode is off, so this was saved directly. "
                                  "Possible conflict with D")


# ---------------------------------------------------------------- history
def test_flagging_and_resolving_show_in_history(backend):
    old = decide(backend, "Use Postgres for hosted storage", "Storage")["record"]["id"]
    new = decide(backend, "Use SQLite for hosted storage", "Storage")["record"]["id"]
    decide(backend, "Use SQLite for hosted storage", "Storage", replaces=old)
    got = ok(call(backend, "manage_history", {"action": "who", "project": "Alpha"}))
    whats = [r["what"] for r in got["rows"]]
    assert any(w.startswith(f"possible conflict flagged: D{new} vs D{old}") for w in whats)
    assert any(f"possible conflict D{new} vs D{old} resolved" in w for w in whats)
    flagged = next(r for r in got["rows"] if r["event"] == "conflict_flagged")
    assert flagged["app"] == "Cursor"


# ---------------------------------------------------------------- meaning (optional)
class Synonyms:
    name = "fake-synonyms"
    GROUPS = [{"postgres", "postgresql", "pg"}, {"sqlite", "embedded"}, {"database", "db", "store", "storage"}]

    def embed(self, texts):
        out = []
        for t in texts:
            words = set(meaning._WORD.findall(t.lower()))
            out.append([1.0 if words & g else 0.0 for g in self.GROUPS] + [0.01])
        return out


def test_meaning_search_adds_matches_keywords_miss(backend):
    db = backend.backend.db_path
    old = decide(backend, "Keep data in PostgreSQL", "Storage")["record"]["id"]
    meaning.set_embedder(Synonyms())
    meaning.index(db, 1)
    got = decide(backend, "Go with pg", "Storage")   # no shared words; same meaning group
    assert conflicts.similarity("Keep data in PostgreSQL", "Go with pg") == 0.0
    [f] = got["possible_conflicts"]
    assert f["decision_id"] == old


# ---------------------------------------------------------------- migration from v2.2.0
@pytest.fixture
def v220_db(tmp_path, monkeypatch):
    from flowstate.database import Database
    real = M.available
    monkeypatch.setattr(M, "available", lambda: [m for m in real() if m[0] < 13])
    db = tmp_path / "v220.db"
    d = Database(db)
    a = d.create_project("Alpha", "First")
    d.create_component(a.id, "Core")
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO conversations (project_id, user_prompt_summary, key_decisions) "
                 "VALUES (?, 'Decision: Use Postgres for hosted storage', "
                 "'[\"Use Postgres for hosted storage\"]')", (a.id,))
    conn.commit()
    conn.close()
    monkeypatch.setattr(M, "available", real)
    return db


def test_migration_from_v220_is_additive(v220_db, tmp_path):
    db = v220_db
    before = {t: rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in ("projects", "conversations")}
    res = M.migrate(db, tmp_path / "bk")
    assert res["applied"][:2] == ["013_todo_claims", "014_decision_conflicts"] and res["backup"]
    assert {t: rows(db, f'SELECT * FROM "{t}" ORDER BY rowid') for t in before} == before
    assert rows(db, "PRAGMA integrity_check") == [("ok",)]
    assert M.migrate(db, tmp_path / "bk") == {"applied": [], "backup": None}
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM schema_migrations WHERE version = 14")
    conn.commit()
    conn.close()
    assert M.migrate(db, tmp_path / "bk")["applied"] == ["014_decision_conflicts"]


def test_upgraded_v220_db_flags_against_old_decisions(v220_db):
    from flowstate import server as srv
    srv.init_backend(v220_db)
    try:
        assert srv.backend.error is None and srv.backend.migration_error is None
        got = decide(srv, "Use SQLite for hosted storage")
        assert got["possible_conflicts"][0]["decision_id"] == 1
        assert "Possible conflicts: D2 vs D1" in brief(srv)
    finally:
        srv.backend.tracker = None


def test_server_without_014_saves_decisions_as_before(v220_db, monkeypatch):
    from flowstate import server as srv
    monkeypatch.setattr(srv, "_migrate", lambda path: None)
    srv.init_backend(v220_db)
    try:
        got = decide(srv, "Use SQLite for hosted storage")
        assert got["logged"] == "decision" and "possible_conflicts" not in got
        assert "Possible conflicts" not in brief(srv)
    finally:
        srv.backend.tracker = None


# ---------------------------------------------------------------- timing
WORDS = ("cache queue index token relay brief search backup export import webhook billing invoice "
         "upload download schema theme layout session tunnel retry timeout logging metrics alert "
         "worker scheduler mobile desktop sync offline locale").split()
VERBS = ("Use", "Keep", "Move", "Drop", "Add", "Limit", "Store", "Run", "Show", "Split")


def _bulk_decisions(db, pid, n):
    conn = sqlite3.connect(db)
    comps = [conn.execute("INSERT INTO components (project_id, name) VALUES (?, ?)",
                          (pid, f"Comp{i}")).lastrowid for i in range(20)]
    for i in range(n):
        text = (f"{VERBS[i % 10]} {WORDS[i % 31]} {WORDS[(i * 7) % 31]} for "
                f"{WORDS[(i * 13) % 31]} v{i}")
        did = conn.execute("INSERT INTO conversations (project_id, user_prompt_summary, "
                           "key_decisions) VALUES (?, ?, ?)",
                           (pid, f"Decision: {text}", json.dumps([text]))).lastrowid
        conn.execute("INSERT INTO record_links (from_type, from_id, from_project_id, link_type, "
                     "to_type, to_id, to_project_id) VALUES ('decision', ?, ?, 'about', "
                     "'component', ?, ?)", (did, pid, comps[i % 20], pid))
    conn.commit()
    conn.close()
    return comps


def test_check_is_fast_on_2000_decisions(backend):
    db = backend.backend.db_path
    comps = _bulk_decisions(db, 1, 2000)
    conn = sqlite3.connect(db)
    times = []
    for i in range(15):
        new_id = conn.execute("INSERT INTO conversations (project_id, user_prompt_summary, "
                              "key_decisions) VALUES (1, 'x', '[\"x\"]')").lastrowid
        conn.commit()
        t = time.perf_counter()
        conflicts.check(db, 1, new_id, f"Use cache relay for search timing {i}", comps[i % 20])
        times.append(time.perf_counter() - t)
    conn.close()
    # measured ~25 ms median here (Apple silicon); the bound leaves room for slow machines
    print(f"\ncheck on 2,000 decisions: median {statistics.median(times) * 1000:.1f} ms, "
          f"max {max(times) * 1000:.1f} ms")
    assert statistics.median(times) < 0.15, times
    # the whole log_work call too, through the MCP server
    t = time.perf_counter()
    decide(backend, "Use cache relay for search timing", "Comp1")
    assert time.perf_counter() - t < 1.0


# ---------------------------------------------------------------- review page
def test_review_page_shows_conflict_buttons_and_reject_dismisses(backend):
    import re
    from starlette.testclient import TestClient
    from flowstate.review_page import HOST, Page, create_app
    db = backend.backend.db_path
    # same project, no component: a yes/no possible-conflict item (a same-component pair
    # opens a disagreement card instead, which takes this item's place)
    decide(backend, "Use Postgres for hosted storage")
    decide(backend, "Use SQLite for hosted storage")
    item = rows(db, "SELECT id FROM review_items WHERE kind = 'conflict'")[0][0]
    p = Page(db, 8770)
    c = TestClient(create_app(p), base_url=f"http://{HOST}:8770", follow_redirects=False)
    c.get("/?key=" + p.launch_key)
    html = c.get("/").text
    assert "Approve: they really conflict" in html and "Possible conflict: D" in html
    csrf = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
    c.post("/reject", data={"item_id": item, "csrf": csrf, "reason": "compatible"})
    assert rows(db, "SELECT status FROM decision_conflicts") == [("dismissed_compatible",)]
