"""Task 70: (1) `flowstate erase` leaves no part of an erased decision in possible-conflict
review items, also the cut quotes ('"start of it..."') older versions stored, and new items
quote decisions whole and name them by id; (2) FlowState itself queues the "reopen?" question
(a system actor no app can claim), so the user can tap its card in the same chat, still only
with the card's one-use code, while every AI path and the terminal stay refused; (3) removing
a supersedes link that leaves pairs closed asks the same single question. Temp databases
only."""

import json
import sqlite3

import pytest

from flowstate import approvals, card_taps, cli, cli_review, identity, review
from test_review_guidance_in_replies import relay  # noqa: F401
from test_session_records import FakeTTY
from test_t55_erase import _delete, _erase, _everywhere
from test_t67_evidence_history import call, ok, rows
from test_t69_reopen_conflicts import (KIND, _no_real_model, approval_log, asks, log,  # noqa: F401
                                       pair_status, queue_rows, replaced, tap, ui, undo)

LONG = ("Use SQLite for hosted storage because every customer runs one small shop and the "
        "nightly export already writes one file per shop, so a server database would add a "
        "second moving part with nothing to gain for at least the next two years of growth")


def _cut(s: str, n: int = 70, dots: str = "...") -> str:
    """How 2.3.0 - 2.4.3 cut a quote (conflicts._one_line)."""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 3].rstrip() + dots


def _fragments(text: str) -> list:
    words = text.split()
    return [" ".join(words[i:i + 4]) for i in range(0, len(words) - 3, 3)]


def _anywhere(db, text) -> list:
    """Tables that still hold `text` anywhere in a text column."""
    conn = sqlite3.connect(db)
    try:
        out = []
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND "
                                 "name NOT LIKE 'memory_fts_%' AND name NOT LIKE 'sqlite_%'"):
            cols = [c[1] for c in conn.execute(f'PRAGMA table_info("{t}")')]
            for c in cols:
                try:
                    if conn.execute(f'SELECT 1 FROM "{t}" WHERE CAST("{c}" AS TEXT) LIKE ? '
                                    f'LIMIT 1', (f"%{text}%",)).fetchone():
                        out.append(f"{t}.{c}")
                except sqlite3.Error:
                    pass
        return out
    finally:
        conn.close()


def _old_item(db, pid, summary, payload, details=None, kind="conflict"):
    """A review item exactly as an older version stored it (straight into the table)."""
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("INSERT INTO review_items (project_id, kind, summary, details, payload, "
                           "source, created_at) VALUES (?, ?, ?, ?, ?, 'Cursor', "
                           "'2025-11-02T10:00:00Z')",
                           (pid, kind, summary, details, json.dumps(payload)))
        conn.execute("INSERT INTO record_history (edit_id, table_name, record_id, project_id, tool, old_row, "
                     "new_row, client, created_at) VALUES ('edit-old-1', 'review_items', ?, ?, 'queue/review_edit', "
                     "?, NULL, 'Cursor', '2025-11-02T10:00:00Z')",
                     (cur.lastrowid, pid, json.dumps({"summary": summary})))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _pid(db):
    return rows(db, "SELECT id FROM projects WHERE name = 'Alpha'")[0][0]


def _conflict_item(db):
    (iid,) = rows(db, "SELECT id FROM review_items WHERE kind = 'conflict'")[0]
    return iid


def _erase_decision(S, db, did):
    S.backend.tracker.close_instance()
    _delete(S, "decision", did)
    _erase(db, f"decision:{did}")
    S.backend.tracker = None


# ---------------------------------------------------------------- 1. erase and cut quotes
def test_new_conflict_items_quote_whole_text_and_name_the_decisions(backend):
    S, db = backend, backend.backend.db_path
    old = log(S, LONG)["record"]["id"]
    got = log(S, LONG.replace("SQLite", "Postgres"))
    new = got["record"]["id"]
    assert got["possible_conflicts"]
    item = review.get_item(db, _conflict_item(db))
    assert LONG in item["summary"] and LONG.replace("SQLite", "Postgres") in item["summary"]
    assert "..." not in item["summary"] and "…" not in item["summary"]
    refs = json.loads(item["payload"])["decisions"]
    assert {(r["record_type"], r["record_id"]) for r in refs} == {("decision", new),
                                                                   ("decision", old)}


def test_erase_leaves_nothing_of_a_long_decision_in_its_conflict_item(backend):
    S, db = backend, backend.backend.db_path
    old = log(S, LONG)["record"]["id"]
    assert log(S, LONG.replace("SQLite", "Postgres"))["possible_conflicts"]
    _erase_decision(S, db, old)
    item = review.get_item(db, _conflict_item(db))
    text = item["summary"] + (item["details"] or "") + item["payload"]
    assert "SQLite" not in text, text             # (the other decision shares the rest)
    assert f'D{old} "[erased]"' in item["summary"]
    assert "Use Postgres for hosted storage" in item["summary"]   # the other quote stays


@pytest.mark.parametrize("dots", ["...", "…"])
def test_erase_removes_a_cut_quote_an_older_version_stored(backend, dots):
    """The item as 2.3.0 - 2.4.3 stored it: both quotes cut at 70 characters, the payload naming
    the pair only as new_id / old_id (no decisions list). Erasing the old decision leaves no
    fragment of it: not in the item, not in the item's history, nowhere in the database."""
    S, db = backend, backend.backend.db_path
    old = log(S, LONG, app="Windsurf")["record"]["id"]
    other = log(S, "Keep invoices in the accounting export for now", app="Windsurf")["record"]["id"]
    cid = None
    if rows(db, "SELECT name FROM sqlite_master WHERE name = 'decision_conflicts'"):
        conn = sqlite3.connect(db)
        cid = conn.execute("INSERT INTO decision_conflicts (project_id, new_id, old_id, score, "
                           "same_component, created_at) VALUES (?, ?, ?, 0.5, 1, "
                           "'2025-11-02T10:00:00Z')", (_pid(db), other, old)).lastrowid
        conn.commit()
        conn.close()
    summary = (f'Possible conflict: D{other} "{_cut("Keep invoices in the accounting export for now", dots=dots)}" '
               f'vs D{old} "{_cut(LONG, dots=dots)}"')
    assert _cut(LONG, dots=dots) != LONG and _cut(LONG, dots=dots).endswith(dots)
    iid = _old_item(db, _pid(db), summary, {"conflict_id": cid, "new_id": other, "old_id": old,
                                            "score": 0.5},
                    details="FlowState found these decisions read alike.")
    # and a second one that names the pair only in its text (no ids at all in the payload)
    iid2 = _old_item(db, _pid(db), f'Possible conflict: D{old} "{_cut(LONG, dots=dots)}" vs D9',
                     {"score": 0.4})
    _erase_decision(S, db, old)
    for i in (iid, iid2):
        item = review.get_item(db, i)
        text = item["summary"] + (item["details"] or "") + item["payload"]
        for frag in _fragments(_cut(LONG)[:-3]):
            assert frag not in text, (i, frag, text)
        assert "[erased]" in item["summary"], item["summary"]
    assert "Keep invoices in the accounting export" in review.get_item(db, iid)["summary"]
    assert len(queue_rows(db, "review_items", iid)) == 1        # its history stays (task 71), blanked:
    for frag in _fragments(_cut(LONG)[:-3]):
        assert _anywhere(db, frag) == [], frag


def test_a_short_cut_prefix_elsewhere_is_left_alone(backend):
    """An unrelated item that only shares a few opening words with a "..." stays byte for
    byte (never a short shared phrase, as NEW-2)."""
    S, db = backend, backend.backend.db_path
    old = log(S, LONG)["record"]["id"]
    text = 'Todo: "Use SQLite for..." came up in the retro'
    iid = _old_item(db, _pid(db), text, {}, kind="suggestion")
    _erase_decision(S, db, old)
    assert review.get_item(db, iid)["summary"] == text


# ---------------------------------------------------------------- 2. FlowState asks
def test_the_question_is_queued_by_flowstate_itself(backend, seeded_db):
    S, db = backend, seeded_db
    _old, _new, c, _cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    assert (item["app"], item["identity_source"]) == (identity.SYSTEM.app,
                                                      identity.SYSTEM.identity_source)
    assert identity.is_system(item)
    assert "Windsurf" in item["details"]          # it still says which app undid what


def test_the_undoing_app_can_tap_its_card_in_the_same_chat(backend, seeded_db, ui):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    shown = call(S, "show_review_card", {"project": "Alpha", "item_id": item["id"]},
                 app="Windsurf")[0].structuredContent["cards"][0]
    assert shown["decide_here"] is True, shown
    # no code, a wrong code, a code made for another app: refused, nothing changed
    r, body = call(S, "resolve_review_card", {"item_id": item["id"], "decision": "approve",
                                              "project": "Alpha"}, app="Windsurf")
    assert body.get("error") == "not_a_card_tap", body
    _r, body = tap(S, item["id"], "approve", app="Windsurf", tap="not-a-real-code")
    assert body.get("error") == "not_a_card_tap", body
    _r, body = tap(S, item["id"], "approve", app="Windsurf",
                   tap=card_taps.mint(item["id"], "review", "Claude Desktop"))
    assert body.get("error") == "not_a_card_tap", body
    assert pair_status(db, cid) == "resolved_replaced" and approval_log(db, item["id"]) == []
    code = card_taps.mint(item["id"], "review", "Windsurf")
    done = ok(tap(S, item["id"], "approve", app="Windsurf", tap=code))
    assert done["resolved"] == item["id"] and pair_status(db, cid) == "open"
    assert approval_log(db, item["id"]) == [(KIND, "card", "card (Windsurf)", "approved")]
    # the used code doesn't work again, on this item or another one
    ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "component": "Storage",
                            "summary": "Use DuckDB for hosted storage", "replaces": _old},
            app="Windsurf"))
    _r, body = tap(S, item["id"], "reject", app="Windsurf", tap=code)
    assert body.get("error") in ("not_a_card_tap", "already_decided"), body
    assert review.get_item(db, item["id"])["status"] == "approved"


def test_the_terminal_is_still_refused(backend, seeded_db, monkeypatch, capsys):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    for verb in ("approve", "reject"):
        tty = FakeTTY(["y"])
        monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
        assert cli.main([verb, str(item["id"]), "--db", str(db)]) != 0
        assert review.get_item(db, item["id"])["status"] == "pending"
    assert pair_status(db, cid) == "resolved_replaced" and approval_log(db, item["id"]) == []


def _ai_decides(S, item, words, expect):
    for app in ("Windsurf", "Cursor"):
        for verb in ("approve", "reject"):
            _r, body = call(S, "manage_review", {"action": verb, "project": "Alpha",
                                                 "item_id": item["id"], "reason": "x",
                                                 "user_request": words}, app=app)
            assert body["error"] in expect, body
        _r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha",
                                             "item_id": item["id"], "summary": "edited",
                                             "user_request": words}, app=app)
        assert body["error"] in expect, body


@pytest.mark.parametrize("chat", ["on", "off"])
def test_through_the_relay_with_chat_approvals_on_or_off(backend, seeded_db, relay, chat):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    approvals.set_relay_chat(db, chat == "on")
    _ai_decides(S, item, "The user said: yes, reopen the storage conflict please",
                ("user_only",) if chat == "on" else ("user_only", "phone_only"))
    assert review.get_item(db, item["id"])["status"] == "pending"
    assert pair_status(db, cid) == "resolved_replaced" and approval_log(db, item["id"]) == []


def test_every_ai_path_is_still_refused(backend, seeded_db):
    S, db = backend, seeded_db
    _old, _new, c, cid = replaced(S)
    ok(undo(S, "decision", c))
    (item,) = asks(db)
    words = "The user said: yes, reopen the storage conflict please"
    _ai_decides(S, item, words, ("user_only",))
    walk = ok(call(S, "manage_review", {"action": "walk", "project": "Alpha"}, app="Windsurf"))
    assert "walked" not in walk
    for client in ("popup (Windsurf)", "chat (Windsurf)", "command-line", "FlowState"):
        got = S._review_decide("review_approve", item["project_id"],
                               {"item_id": item["id"], "user_request": words}, client)
        assert got["error"] == "user_only", (client, got)
    tok = identity.set_current(identity.Identity(app="Windsurf", model=None, machine="m",
                                                 identity_source="self_reported"))
    try:
        for client in ("review-page", "phone", "tunnel page"):
            got = S._review_decide("review_approve", item["project_id"],
                                   {"item_id": item["id"], "user_request": words}, client)
            assert got["error"] == "user_only", (client, got)
    finally:
        identity.reset(tok)
    assert review.get_item(db, item["id"])["status"] == "pending"
    assert pair_status(db, cid) == "resolved_replaced" and approval_log(db, item["id"]) == []


@pytest.mark.parametrize("name", ["FlowState", "flowstate", "FLOWSTATE", "Flow State", "system",
                                  "FlowState (automatic)", "ＦｌｏｗＳｔａｔｅ", "Fl0wState"])
def test_no_app_can_queue_an_item_as_flowstate(backend, seeded_db, ui, name):
    """An app calling itself FlowState (any spelling) queues a suggestion: it is recorded as an
    ordinary app, never as FlowState's own actor, so the own-item rule still keeps it off that
    app's card and chat."""
    S, db = backend, seeded_db
    got = ok(call(S, "log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                  "summary": f"Suggested by an app named {len(name)} letters",
                                  "model": "FlowState", "workspace": "FlowState"}, app=name))
    (item,) = [i for i in review.list_items(db, None, "pending", 100)["items"]
               if i["kind"] != KIND]
    assert not identity.is_system(item), (name, item)
    assert item.get("identity_source") != identity.SYSTEM.identity_source
    assert got
    # and resolve() never hands out the system actor, whatever the app sends
    for http in (False, True):
        ident = identity.resolve(name, http=http, headers={"x-flowstate-workspace": "FlowState",
                                                           "x-flowstate-model": "FlowState"},
                                 model_arg="FlowState")
        assert ident.identity_source != identity.SYSTEM.identity_source
        assert not identity.is_system(ident.as_dict())


def test_only_the_reopen_question_uses_the_system_actor(backend, seeded_db):
    S, db = backend, seeded_db
    _old, _new, c, _cid = replaced(S)
    ok(call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Check exports",
                            "suggested": True}, app="Windsurf"))
    ok(undo(S, "decision", c))
    kinds = {r[0] for r in rows(db, "SELECT kind FROM review_items WHERE identity_source = ?",
                                identity.SYSTEM.identity_source)}
    assert kinds == {KIND}, kinds


# ---------------------------------------------------------------- 3. removing a link
def _link(S, c, old, verb="add", app="Windsurf"):
    return call(S, "manage_links", {"action": verb, "project": "Alpha", "link_type": "supersedes",
                                    "item_type": "decision", "item_id": c,
                                    "target_type": "decision", "target_id": old}, app=app)


def test_removing_a_supersedes_link_asks_once(backend, seeded_db):
    S, db = backend, seeded_db
    old = log(S, "Use SQLite for hosted storage")["record"]["id"]
    new = log(S, "Use Postgres for hosted storage", app="Claude")["record"]["id"]
    cid = rows(db, "SELECT id FROM decision_conflicts WHERE new_id = ? AND old_id = ?",
               new, old)[0][0]
    c = log(S, "Store blobs in object storage", app="Windsurf")["record"]["id"]
    lid = ok(_link(S, c, old))["link"]["id"]
    assert pair_status(db, cid) == "resolved_replaced"
    done = ok(_link(S, c, old, "remove"))
    (item,) = asks(db)
    assert done["reopen_question"]["item_id"] == item["id"], done
    assert identity.is_system(item) and item["status"] == "pending"
    text = item["summary"] + " " + item["details"]
    for words in (f"D{new}", f"D{old}", "Use SQLite for hosted storage", f"D{c}", "Windsurf",
                  "removed"):
        assert words in text, (words, text)
    assert pair_status(db, cid) == "resolved_replaced"
    # retry the remove, add / remove again: still the one question
    ok(call(S, "manage_links", {"action": "remove", "project": "Alpha", "link_id": lid},
            app="Windsurf"))
    ok(_link(S, c, old))
    ok(_link(S, c, old, "remove"))
    ok(_link(S, c, old))
    ok(_link(S, c, old, "remove"))
    assert len(asks(db)) == 1
    # the user answers it on the phone: reopened
    got = S._review_decide("review_approve", item["project_id"],
                           {"item_id": item["id"], "user_request": "approved on the phone"}, "phone")
    assert "error" not in got, got
    assert pair_status(db, cid) == "open"


def test_removing_the_link_log_work_replaces_made_asks_too(backend, seeded_db):
    S, db = backend, seeded_db
    old, _new, c, cid = replaced(S)
    lid = rows(db, "SELECT id FROM record_links WHERE link_type = 'supersedes' AND from_id = ? "
                   "AND removed_at IS NULL", c)[0][0]
    done = ok(call(S, "manage_links", {"action": "remove", "project": "Alpha", "link_id": lid},
                   app="Cursor"))
    (item,) = asks(db)
    assert done["reopen_question"]["item_id"] == item["id"]
    # undoing the remove brings the link back (no new question); removing again: same one
    ok(call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "link",
                                  "record_id": lid}, app="Cursor"))
    ok(call(S, "manage_links", {"action": "remove", "project": "Alpha", "link_id": lid},
            app="Cursor"))
    assert len(asks(db)) == 1 and pair_status(db, cid) == "resolved_replaced"


def test_removing_other_links_asks_nothing(backend, seeded_db):
    S, db = backend, seeded_db
    old = log(S, "Use SQLite for hosted storage")["record"]["id"]
    log(S, "Use Postgres for hosted storage", app="Claude")
    c = log(S, "Store blobs in object storage", app="Windsurf")["record"]["id"]
    ok(_link(S, c, old))
    # the replacement is back another way (the pair is replaced by a different decision too)
    d = log(S, "Use DuckDB for hosted storage", app="Windsurf", replaces=old)["record"]["id"]
    assert d
    ok(_link(S, c, old, "remove"))    # old is still replaced by d: the pair would not show
    assert asks(db) == []
    ok(call(S, "manage_links", {"action": "add", "project": "Alpha", "link_type": "about",
                                "item_type": "decision", "item_id": c, "target_project": "Alpha",
                                "target_component": "Storage"},
            app="Windsurf"))
    ok(call(S, "manage_links", {"action": "remove", "project": "Alpha", "link_type": "about",
                                "item_type": "decision", "item_id": c, "target_project": "Alpha",
                                "target_component": "Storage"},
            app="Windsurf"))
    assert asks(db) == []
