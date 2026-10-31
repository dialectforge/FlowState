"""Task 55 (re-audit 53): erase only touches the erased record's own text (N-F2), and finds
records an undo took back and deleted decisions in the session log (N-F3, D5). Temp DBs only."""

import json
import sqlite3
from pathlib import Path

import pytest

from flowstate import erase, review
from test_session_contents import _q, call

TODO = "Rewrite the invoice exporter for the March audit"
DECISION = "Store invoices as signed PDF files from now on"
LEARNING = "The payment gateway retries webhooks for three days"


def _row(db, item):
    return _q(db, "SELECT summary, details, payload FROM review_items WHERE id = ?", item)[0]


def _delete(S, rtype, rid, project="Alpha"):
    r = call(S, "manage_memory", {"action": "delete", "project": project, "record_type": rtype,
                                  "record_id": rid})
    assert r.get("deleted"), r


def _erase(db, *specs):
    p = erase.plan(db, list(specs))
    return erase.run(db, p)


def _everywhere(db, text) -> list:
    """Where `text` still is: history, search index, session log, review items."""
    out = []
    for name, sql in (("history", "SELECT COUNT(*) FROM record_history WHERE old_row LIKE ? "
                                  "OR new_row LIKE ?"),
                      ("search", "SELECT COUNT(*) FROM memory_fts WHERE searchable_text LIKE ? "
                                 "OR searchable_text LIKE ?"),
                      ("session log", "SELECT COUNT(*) FROM auto_sessions WHERE events LIKE ? "
                                      "OR events LIKE ?"),
                      ("review", "SELECT COUNT(*) FROM review_items WHERE summary || "
                                 "COALESCE(details, '') LIKE ? OR payload LIKE ?")):
        if _q(db, sql, f"%{text}%", f"%{text}%")[0][0]:
            out.append(name)
    return out


def _fts(db, rtype, rid, pid, text):
    conn = sqlite3.connect(db)   # an older version's keyword index row
    conn.execute("INSERT INTO memory_fts (content_type, content_id, project_id, searchable_text) "
                 "VALUES (?, ?, ?, ?)", (rtype, str(rid), str(pid), text))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------- N-F2
def test_erase_leaves_an_unrelated_review_item_byte_for_byte(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": TODO,
                             "details": "Started on 2026-10-25 06:00:00, in_progress"})
    tid = r["record"]["id"]
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                             "status": "in_progress"})
    [(row,)] = _q(db, "SELECT old_row FROM record_history WHERE table_name = 'todos' AND "
                      "record_id = ? AND old_row IS NOT NULL ORDER BY id DESC LIMIT 1", tid)
    created = json.loads(row)["created_at"]
    # another project's pending item: a date (the todo's own), a status word, line breaks
    text = (f"Both are in_progress.\nFirst seen {created}.\n\n  Priority: normal\t(see"
            f" todo list)\r\nStatus in_progress since 2026-10-25")
    other = review.add_item(db, 2, "learning", text, f"Details:\n  {created}\n\nin_progress",
                            {"kind": "learning", "summary": text, "status": "in_progress"},
                            source="Cursor")
    # the same project's item quoting the todo's title wrapped onto two lines
    near = review.add_item(db, 1, "learning", "Note:\nRewrite the invoice exporter\nfor the "
                           "March audit was planned.\n\nKeep this line.", None, {},
                           source="Cursor")
    before = _row(db, other)
    S.backend.tracker.close_instance()
    _delete(S, "todo", tid)
    _erase(db, f"todo:{tid}")
    assert _row(db, other) == before
    assert _row(db, near)[0] == "Note:\n[erased] was planned.\n\nKeep this line."
    assert TODO not in json.dumps(_q(db, "SELECT summary, details, payload FROM review_items"))


def test_erase_matches_another_projects_item_only_exactly(backend):
    S, db = backend, backend.backend.db_path
    lid = call(S, "log_work", {"project": "Alpha", "kind": "learning",
                               "summary": LEARNING})["record"]["id"]
    exact = review.add_item(db, 2, "learning", f"Copied: {LEARNING}.", None, {}, source="x")
    wrapped = review.add_item(db, 2, "learning", "The payment gateway retries\nwebhooks for "
                              "three days", None, {}, source="x")
    before = _row(db, wrapped)
    _delete(S, "learning", lid)
    _erase(db, f"learning:{lid}")
    assert _row(db, exact)[0] == "Copied: [erased]."
    assert _row(db, wrapped) == before   # not provably the same record: left alone


# ---------------------------------------------------------------- N-F3 (a): undo
def test_erase_after_undo_removes_the_text_everywhere(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": LEARNING})
    lid = r["record"]["id"]
    _fts(db, "learning", lid, 1, LEARNING)
    item = review.add_item(db, 1, "learning", f"Suggested again: {LEARNING}", None, {},
                           source="Cursor")
    u = call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                   "record_type": "learning", "record_id": lid})
    assert u["rows"][0]["now"] == "deleted", u
    assert _q(db, "SELECT COUNT(*) FROM learnings WHERE id = ?", lid) == [(0,)]
    _fts(db, "learning", lid, 1, LEARNING)   # an older version left its index row
    assert {"history", "search", "session log", "review"} <= set(_everywhere(db, LEARNING))
    p = erase.plan(db, [f"learning:{lid}"])
    assert p["targets"][0]["how"] == "undone"
    assert any("undone" in line for line in erase.describe(p))
    res = erase.run(db, p)
    assert res["history"] >= 2 and res["session_events"] >= 1 and res["review_items"] >= 1
    S.backend.tracker = None
    assert _everywhere(db, LEARNING) == []
    assert _row(db, item)[0] == "Suggested again: [erased]"
    for f in (Path(db), Path(str(db) + "-wal")):
        if f.exists():
            assert LEARNING.encode() not in f.read_bytes(), f


def test_erase_deleted_before_includes_undone_records(backend):
    S, db = backend, backend.backend.db_path
    lid = call(S, "log_work", {"project": "Alpha", "kind": "learning",
                               "summary": LEARNING})["record"]["id"]
    call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "learning",
                               "record_id": lid})
    conn = sqlite3.connect(db)
    conn.execute("UPDATE record_history SET created_at = '2020-01-01 00:00:00'")
    conn.commit()
    conn.close()
    p = erase.plan(db, [], deleted_before=1)
    assert ("learning", lid) in {(t["type"], t["id"]) for t in p["targets"]}


def test_no_delete_on_record_message_says_what_it_means(seeded_db):
    with pytest.raises(ValueError) as e:
        erase.plan(seeded_db, ["todo:999"])
    msg = str(e.value)
    assert "no delete of it" in msg and "undo" in msg and "never existed" in msg


# ---------------------------------------------------------------- N-F3 (b): decisions
def test_erase_of_a_deleted_decision_clears_the_session_log(backend):
    S, db = backend, backend.backend.db_path
    r = call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": DECISION,
                             "details": "Auditors asked for tamper evidence"})
    did = r["record"]["id"]
    other = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": LEARNING})
    assert any(DECISION in e for (ev,) in _q(db, "SELECT events FROM auto_sessions")
               for e in json.loads(ev))
    _delete(S, "decision", did)
    res = _erase(db, f"decision:{did}")
    assert res["session_events"] >= 1
    S.backend.tracker = None
    events = [e for (ev,) in _q(db, "SELECT events FROM auto_sessions") for e in json.loads(ev)]
    assert not any(DECISION in e for e in events) and "decision: [erased]" in events
    assert any(LEARNING in e for e in events)   # the other record's label stays
    assert other["record"]["id"]
    assert _everywhere(db, DECISION) == []
