"""Task 57: erase takes out every version of an erased record and the text tables that point
at it (task 56 NEW-1, NEW-3), leaves review items that only share a short phrase alone (NEW-2),
doesn't blank another record's short session-log label (NEW-4), finds a record deleted with
its parent (NEW-5), erases a removed hard rule's text (NEW-6), and the Info notes (huge
--deleted-before, NFC / NFD). Temp databases only."""

import json
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from flowstate import conflict_cards, erase, review, rules
from test_session_contents import _q, call
from test_t55_erase import _delete, _erase, _everywhere, _row

FIRST = "Draft the quarterly tax filing checklist"
SECOND = "Finalise the yearly tax filing checklist"


def _in_file(db, text) -> bool:
    data = b"".join(f.read_bytes() for f in (Path(db), Path(str(db) + "-wal")) if f.exists())
    return text.encode() in data


def _events(db) -> list:
    return [e for (ev,) in _q(db, "SELECT events FROM auto_sessions") for e in json.loads(ev)]


# ---------------------------------------------------------------- NEW-1
def test_erase_takes_out_every_version_of_an_edited_record(backend):
    S, db = backend, backend.backend.db_path
    tid = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": FIRST})["record"]["id"]
    near = review.add_item(db, 1, "todo", f"Suggested: {FIRST}", None,
                           {"record_type": "todo", "record_id": tid}, source="Cursor")
    call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid, "title": SECOND})
    assert _q(db, "SELECT title FROM todos WHERE id = ?", tid) == [(SECOND,)]
    S.backend.tracker.close_instance()
    _delete(S, "todo", tid)
    _erase(db, f"todo:{tid}")
    S.backend.tracker = None
    for text in (FIRST, SECOND):
        assert _everywhere(db, text) == [], text
        assert not any(text in e for e in _events(db))
        assert not _in_file(db, text), text
    assert _row(db, near)[0] == "Suggested: [erased]"


# ---------------------------------------------------------------- NEW-3
EVIDENCE = "settings.py:12 — the ledger keeps every invoice forever"
TOPIC = "Where the invoice ledger lives"


def test_a_delete_takes_the_evidence_and_undo_brings_it_back(backend):
    S, db = backend, backend.backend.db_path
    did = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                               "summary": "Keep the ledger in Postgres"})["record"]["id"]
    conn = sqlite3.connect(db)
    conflict_cards.save_evidence(conn, "decision", did, [{"kind": "file", "label": "settings.py:12",
                                                          "excerpt": "the ledger keeps every invoice forever"}], "x")
    conn.commit()
    conn.close()
    _delete(S, "decision", did)
    assert _q(db, "SELECT COUNT(*) FROM entry_evidence WHERE record_id = ?", did) == [(0,)]
    u = call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "decision",
                                   "record_id": did})
    assert "error" not in u, u
    assert _q(db, "SELECT COUNT(*) FROM entry_evidence WHERE record_id = ?", did) == [(1,)]


def test_erase_takes_evidence_claim_history_and_card_topics(backend):
    S, db = backend, backend.backend.db_path
    title = "Migrate the invoice ledger to the new cluster"
    tid = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": title})["record"]["id"]
    call(S, "manage_todos", {"action": "claim", "project": "Alpha", "todo_id": tid})
    assert _q(db, "SELECT COUNT(*) FROM todo_claim_events WHERE todo_id = ?", tid)[0][0] >= 1
    a = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                             "summary": "Keep the invoice ledger in Postgres"})["record"]["id"]
    b = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                             "summary": "Keep the invoice ledger in DynamoDB", "conflicts_with": a,
                             "topic": TOPIC, "evidence": [EVIDENCE]})["record"]["id"]
    assert _q(db, "SELECT COUNT(*) FROM conflict_cards WHERE topic = ?", TOPIC) == [(1,)]
    # an older FlowState's delete left the evidence behind: erase takes it too
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO entry_evidence (record_type, record_id, kind, label, excerpt, "
                 "position, created_at) VALUES ('todo', ?, 'output', 'output', ?, 0, 'x')",
                 (tid, "leftover evidence of the ledger migration"))
    conn.commit()
    conn.close()
    S.backend.tracker.close_instance()
    _delete(S, "todo", tid)
    _delete(S, "decision", b)
    _erase(db, f"todo:{tid}", f"decision:{b}")
    S.backend.tracker = None
    assert _q(db, "SELECT COUNT(*) FROM todo_claim_events WHERE todo_id = ?", tid) == [(0,)]
    assert _q(db, "SELECT COUNT(*) FROM entry_evidence WHERE record_id IN (?, ?)", tid, b) == [(0,)]
    assert _q(db, "SELECT topic FROM conflict_cards") == [(erase.ERASED,)]
    for text in (title, TOPIC, "the ledger keeps every invoice forever",
                 "leftover evidence of the ledger migration", "Keep the invoice ledger in DynamoDB"):
        assert not _in_file(db, text), text


# ---------------------------------------------------------------- NEW-2
def test_erase_leaves_other_projects_items_that_share_a_short_phrase(backend):
    S, db = backend, backend.backend.db_path
    cid = call(S, "manage_projects", {"action": "add_component", "project": "Alpha",
                                      "name": "Authentication"})
    cid = cid["id"] if "id" in cid else cid["component"]["id"]
    other = review.add_item(db, 2, "learning", "Authentication now uses passkeys", None,
                            {"summary": "Authentication now uses passkeys"}, source="Cursor")
    same = review.add_item(db, 1, "learning", "Authentication is the slow part", None, {},
                           source="Cursor")
    before, before_same = _row(db, other), _row(db, same)
    r = call(S, "manage_projects", {"action": "delete_component", "project": "Alpha",
                                    "component_id": cid})
    assert r.get("deleted"), r
    _erase(db, f"component:{cid}")
    assert _row(db, other) == before and _row(db, same) == before_same


# ---------------------------------------------------------------- NEW-4
def test_a_short_title_doesnt_blank_another_kinds_label(backend):
    S, db = backend, backend.backend.db_path
    tid = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "fix"})["record"]["id"]
    call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "fix"})
    _delete(S, "todo", tid)
    _erase(db, f"todo:{tid}")
    S.backend.tracker = None
    ev = _events(db)
    assert "learning: fix" in ev and "todo: fix" not in ev and "todo: [erased]" in ev


# ---------------------------------------------------------------- NEW-5
def test_a_record_deleted_with_its_project_can_be_erased_and_the_project_undone(backend):
    S, db = backend, backend.backend.db_path
    text = "Beta todo with a private customer name"
    tid = call(S, "log_work", {"project": "Beta Project", "kind": "todo", "summary": text})["record"]["id"]
    pid = 2
    r = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"})
    assert r.get("deleted"), r
    p = erase.plan(db, [f"todo:{tid}"])
    assert p["targets"][0]["how"] == "deleted with its project"
    assert set(p["rows"]) == {("todos", tid)}
    erase.run(db, p)
    S.backend.tracker = None
    assert _everywhere(db, text) == [] and not _in_file(db, text)
    u = call(S, "manage_history", {"action": "undo", "project": "Beta Project",
                                   "record_type": "project", "record_id": pid})
    assert "error" not in u, u
    assert _q(db, "SELECT name FROM projects WHERE id = ?", pid) == [("Beta Project",)]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = ?", tid) == [(0,)]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE title = 'Beta todo'") == [(1,)]


# ---------------------------------------------------------------- NEW-6
def test_a_removed_hard_rule_can_be_erased(backend):
    S, db = backend, backend.backend.db_path
    text = "Never mention the acquisition codename"
    rid = rules.add_by_user(db, 1, text)["rule"]["id"]
    with pytest.raises(ValueError, match=f"flowstate rules remove {rid}"):
        erase.plan(db, [f"rule:{rid}"])
    rules.remove_by_user(db, rid)
    p = erase.plan(db, [f"rule:{rid}"])
    assert p["targets"][0]["how"] == "removed"
    erase.run(db, p)
    assert _q(db, "SELECT rule, removed_at IS NOT NULL FROM project_rules WHERE id = ?", rid) \
        == [(erase.ERASED, 1)]
    assert _everywhere(db, text) == [] and not _in_file(db, text)
    # and --deleted-before finds removed rules too
    rid2 = rules.add_by_user(db, 1, "Never deploy on public holidays")["rule"]["id"]
    rules.remove_by_user(db, rid2)
    assert ("rule", rid2) in {(t["type"], t["id"]) for t in erase.plan(db, [], 0)["targets"]}


# ---------------------------------------------------------------- Info
def test_a_huge_deleted_before_means_everything(backend):
    S, db = backend, backend.backend.db_path
    lid = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "x y z"})["record"]["id"]
    _delete(S, "learning", lid)
    p = erase.plan(db, [], deleted_before=99999999)
    assert p["targets"] == [] or all(t["type"] for t in p["targets"])
    p = erase.plan(db, [], deleted_before=0)
    assert ("learning", lid) in {(t["type"], t["id"]) for t in p["targets"]}


def test_nfc_and_nfd_forms_of_the_text_both_match(backend):
    S, db = backend, backend.backend.db_path
    text = "Le café du coin ferme le dimanche après-midi"
    lid = call(S, "log_work", {"project": "Alpha", "kind": "learning",
                               "summary": unicodedata.normalize("NFC", text)})["record"]["id"]
    item = review.add_item(db, 1, "learning", "Seen: " + unicodedata.normalize("NFD", text), None,
                           {"record_type": "learning", "record_id": lid}, source="x")
    _delete(S, "learning", lid)
    _erase(db, f"learning:{lid}")
    assert _row(db, item)[0] == "Seen: [erased]"


def test_a_cli_erase_with_a_huge_number_doesnt_crash(backend, capsys):
    from flowstate import cli
    db = backend.backend.db_path
    assert cli.main(["erase", "--deleted-before", "99999999", "--dry-run", "--db", str(db)]) == 0


def test_erase_summary_says_a_removed_rules_text_was_blanked(backend, capsys, monkeypatch):
    """(task 60, I-1) `flowstate erase rule:ID` says the rule's own text was blanked; erasing
    anything else doesn't get that line."""
    from flowstate import cli, cli_review
    from test_session_records import FakeTTY
    db = backend.backend.db_path
    rid = rules.add_by_user(db, 1, "Never name the supplier in commits")["rule"]["id"]
    rules.remove_by_user(db, rid)
    monkeypatch.setattr(cli_review, "open_terminal", lambda: FakeTTY(["y"]))
    assert cli.main(["erase", f"rule:{rid}", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "Blanked the text of 1 removed hard rule(s)" in out
    assert _q(db, "SELECT rule FROM project_rules WHERE id = ?", rid) == [(erase.ERASED,)]
    lid = call(backend, "log_work", {"project": "Alpha", "kind": "learning",
                                     "summary": "Short lived note"})["record"]["id"]
    _delete(backend, "learning", lid)
    monkeypatch.setattr(cli_review, "open_terminal", lambda: FakeTTY(["y"]))
    assert cli.main(["erase", f"learning:{lid}", "--db", str(db)]) == 0
    assert "Blanked the text" not in capsys.readouterr().out
