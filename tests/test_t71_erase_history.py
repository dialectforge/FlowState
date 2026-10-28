"""Task 71: `flowstate erase` keeps the edit history of OTHER items (review items about the
erased record, possible-conflict items, an unrelated item that quoted it) and only blanks the
erased text inside those history rows (whole and cut quotes, the same rules as the live item);
the erased record's own history still goes entirely. Temp databases only."""

import json
import sqlite3

from flowstate import review
from test_t55_erase import _delete, _erase
from test_t67_evidence_history import call, ok, rows
from test_t69_reopen_conflicts import log
from test_t70_erase_reopen import LONG, _anywhere, _cut, _fragments, _pid

ACCENT = ("Gardez les reçus détaillés de chaque boutique pendant sept ans complets, "
          "car l'audit fiscal les réclame")


def _erase_decision(S, db, did):
    S.backend.tracker.close_instance()
    _delete(S, "decision", did)
    _erase(db, f"decision:{did}")
    S.backend.tracker = None


def _item(db, pid, summary, payload=None, details=None, kind="suggestion"):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("INSERT INTO review_items (project_id, kind, summary, details, payload, "
                           "source, created_at) VALUES (?, ?, ?, ?, ?, 'Cursor', "
                           "'2025-11-02T10:00:00Z')",
                           (pid, kind, summary, details, json.dumps(payload or {})))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _hist(db, iid, old, new, tool="queue/review_edit", edit="edit-old", raw=False):
    """A history row of review item `iid` (old / new: a dict, stored as JSON, or `raw` text)."""
    enc = (lambda v: v) if raw else (lambda v: json.dumps(v) if v is not None else None)
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("INSERT INTO record_history (edit_id, table_name, record_id, project_id, "
                           "tool, old_row, new_row, client, created_at) VALUES (?, 'review_items', "
                           "?, ?, ?, ?, ?, 'Cursor', '2025-11-02T10:00:00Z')",
                           (f"{edit}-{iid}", iid, _pid(db), tool, enc(old), enc(new)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row(db, hid):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        r = conn.execute("SELECT * FROM record_history WHERE id = ?", (hid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _item_history(db, iid):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM record_history WHERE table_name = "
                                              "'review_items' AND record_id = ? ORDER BY id", (iid,))]
    finally:
        conn.close()


def _deep(v) -> str:
    """Every string inside a stored value, JSON strings decoded (escaped accents too)."""
    if isinstance(v, str):
        try:
            d = json.loads(v)
        except ValueError:
            return v
        return v if isinstance(d, str) else _deep(d)
    if isinstance(v, dict):
        return " ".join(_deep(x) for x in list(v.keys()) + list(v.values()))
    if isinstance(v, list):
        return " ".join(_deep(x) for x in v)
    return "" if v is None else str(v)


def _leaks(row: dict, text: str) -> list:
    """Columns of a history row that still hold any 4-word fragment of `text`."""
    out = []
    for col, v in row.items():
        s = _deep(v)
        if any(f in s for f in _fragments(text)) or any(f in str(v) for f in _fragments(text)):
            out.append(col)
    return out


def _payload(quote):
    """A rich payload as review items store it: evidence snippets, a title, nested lists."""
    return json.dumps({"title": f"Follow-up on {quote}", "evidence": [
        {"label": "notes.md:12", "excerpt": quote}, {"label": "plan.md:3", "excerpt": "unrelated"}],
        "records": [["todo", 99, "added"]], "score": 0.3})


# ---------------------------------------------------------------- an unrelated item
def test_an_unrelated_item_keeps_its_history_with_the_quote_blanked(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    pid = _pid(db)
    iid = _item(db, pid, f"Retro note: {LONG} (discussed)")
    plain = _hist(db, iid, {"summary": "Retro note: first draft", "details": None},
                  {"summary": "Retro note: second draft", "details": None}, edit="e1")
    quoted = _hist(db, iid, {"summary": f"Retro note: {LONG}", "details": f"Said: {LONG}",
                             "payload": _payload(LONG), "reason": f"edited away from {LONG}"},
                   {"summary": "Retro note: second draft", "details": None}, edit="e2")
    before_plain = _row(db, plain)
    n_before = len(_item_history(db, iid))
    _erase_decision(S, db, did)
    hist = _item_history(db, iid)
    assert len(hist) == n_before == 2                  # its history stays
    assert _row(db, plain) == before_plain             # an unaffected row: byte for byte
    q = _row(db, quoted)
    assert _leaks(q, LONG) == [], q
    old = json.loads(q["old_row"])
    assert old["summary"] == "Retro note: [erased]"
    assert old["details"] == "Said: [erased]"
    assert old["reason"] == "edited away from [erased]"
    p = json.loads(old["payload"])
    assert p["evidence"][0]["excerpt"] == "[erased]" and p["evidence"][1]["excerpt"] == "unrelated"
    assert p["title"] == "Follow-up on [erased]" and p["score"] == 0.3
    assert json.loads(q["new_row"]) == {"summary": "Retro note: second draft", "details": None}
    assert (q["edit_id"], q["tool"], q["client"], q["created_at"]) == (
        "e2-%d" % iid, "queue/review_edit", "Cursor", "2025-11-02T10:00:00Z")
    assert review.get_item(db, iid)["summary"] == "Retro note: [erased] (discussed)"


def test_an_approve_with_edits_keeps_its_history(backend):
    """Through the real tools: a suggested todo quoting the decision, approved with edits."""
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "todo", "suggested": True,
                                   "summary": f"Check this again: {LONG}"}))["queued_for_review"]
    ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                 "summary": "Check the storage choice again",
                                 "user_request": "User: approve it, shorter"}, app="Claude Code"))
    hist = _item_history(db, item)
    assert hist and LONG in hist[0]["old_row"]
    _erase_decision(S, db, did)
    after = _item_history(db, item)
    assert [h["id"] for h in after] == [h["id"] for h in hist]
    for h in after:
        assert _leaks(h, LONG) == [], h
    assert json.loads(after[0]["old_row"])["summary"] == "Check this again: [erased]"
    assert json.loads(after[0]["new_row"])["summary"] == "Check the storage choice again"


def test_accented_text_in_an_escaped_payload_is_blanked(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, ACCENT)["record"]["id"]
    iid = _item(db, _pid(db), "Note on receipts")
    hid = _hist(db, iid, {"summary": "Note on receipts", "payload": json.dumps(
        {"evidence": [{"label": "x", "excerpt": f"quote: {ACCENT}"}]})},   # ensure_ascii escapes
        {"summary": "Note on receipts", "payload": "{}"})
    assert "reçus" not in _row(db, hid)["old_row"]   # stored escaped, as review.py writes it
    _erase_decision(S, db, did)
    h = _row(db, hid)
    assert h is not None and _leaks(h, ACCENT) == [], h
    assert json.loads(json.loads(h["old_row"])["payload"])["evidence"][0]["excerpt"] == "quote: [erased]"


def test_a_short_shared_phrase_in_unrelated_history_stays(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    iid = _item(db, _pid(db), "Use SQLite for hosted storage: see retro")
    hid = _hist(db, iid, {"summary": 'Note: "Use SQLite for..." again'},
                {"summary": "Use SQLite for hosted storage: see retro"})
    odd = _hist(db, iid, '{"summary":"Retro","payload":"[1,2]","note":"caf\\u00e9"}', None,
                edit="odd", raw=True)          # formatted unlike json.dumps: left as it is
    before, before_odd = _row(db, hid), _row(db, odd)
    _erase_decision(S, db, did)
    assert _row(db, hid) == before and _row(db, odd) == before_odd


# ---------------------------------------------------------------- cut quotes, items about it
def test_a_cut_quote_in_history_is_blanked(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    other = log(S, "Keep invoices in the accounting export for now")["record"]["id"]
    pid = _pid(db)
    # an older possible-conflict item (names the pair by id) and an unrelated item
    about = _item(db, pid, f'Possible conflict: D{other} "Keep invoices" vs D{did} "[gone]"',
                  {"new_id": other, "old_id": did, "score": 0.5}, kind="conflict")
    h1 = _hist(db, about, {"summary": f'Possible conflict: D{other} "Keep invoices" vs '
                                      f'D{did} "{_cut(LONG, n=30)}"'}, None, edit="c1")
    loose = _item(db, pid, "Retro")
    h2 = _hist(db, loose, {"summary": f'We said "{_cut(LONG, dots="…")}" then moved on'},
               {"summary": "Retro"}, edit="c2")
    _erase_decision(S, db, did)
    r1, r2 = _row(db, h1), _row(db, h2)
    assert r1 and r2
    assert json.loads(r1["old_row"])["summary"] == (
        f'Possible conflict: D{other} "Keep invoices" vs D{did} "[erased]"')
    assert json.loads(r2["old_row"])["summary"] == 'We said "[erased]" then moved on'
    for r in (r1, r2):
        for frag in _fragments(_cut(LONG, n=30)[:-3]):
            assert frag not in r["old_row"], (frag, r)


def test_an_item_from_the_erased_record_keeps_blank_history(backend):
    """The decision came from this item (approve with edits): the item is blanked, and so is
    every history row of it; the rows stay, so the queue's history still lists the edit."""
    S, db = backend, backend.backend.db_path
    first = "Store the payroll export on the shared drive forever and ever"
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                   "summary": first}))["queued_for_review"]
    rid = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "summary": "Keep the payroll export for one year",
                                       "user_request": "User: approve, but one year only"},
                  app="Claude Code"))["record"]["id"]
    n = len(_item_history(db, item))
    _erase_decision(S, db, rid)
    hist = _item_history(db, item)
    assert len(hist) == n >= 1
    for h in hist:
        for v in (h["old_row"], h["new_row"]):
            d = json.loads(v) if v else {}
            assert d.get("summary") in (None, "[erased]") and not d.get("details"), d
            assert "payroll" not in v if v else True
    assert _anywhere(db, "payroll") == []


# ---------------------------------------------------------------- the record's own history
def test_the_erased_records_own_history_still_goes(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    assert rows(db, "SELECT COUNT(*) FROM record_history WHERE table_name = 'conversations' "
                    "AND record_id = ?", did)[0][0] >= 1
    iid = _item(db, _pid(db), f"Quote: {LONG}")
    _hist(db, iid, {"summary": f"Quote: {LONG}"}, {"summary": "Quote"})
    _erase_decision(S, db, did)
    assert rows(db, "SELECT COUNT(*) FROM record_history WHERE table_name = 'conversations' "
                    "AND record_id = ?", did) == [(0,)]
    assert len(_item_history(db, iid)) == 1
    for frag in _fragments(LONG):
        assert _anywhere(db, frag) == [], frag


# ---------------------------------------------------------------- views and undo still work
def test_history_views_and_undo_work_on_redacted_items(backend):
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    item = ok(call(S, "log_work", {"project": "Alpha", "kind": "todo", "suggested": True,
                                   "summary": f"Check this again: {LONG}"}))["queued_for_review"]
    tid = ok(call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                       "summary": "Check the storage choice again",
                                       "user_request": "User: approve it, shorter"},
                  app="Claude Code"))["record"]["id"]
    ok(call(S, "manage_todos", {"action": "update", "project": "Alpha", "todo_id": tid,
                                "title": "Check the storage choice once more"}))
    _erase_decision(S, db, did)
    listed = ok(call(S, "manage_history", {"action": "list", "project": "Alpha"}))
    assert "SQLite for hosted storage because" not in json.dumps(listed)
    mine = [h for h in listed["history"] if h["tool"] == "queue/review_edit"]
    assert mine, listed["history"]
    who = ok(call(S, "manage_history", {"action": "who", "project": "Alpha"}))
    assert "SQLite for hosted storage because" not in json.dumps(who)
    got = ok(call(S, "manage_history", {"action": "undo", "project": "Alpha",
                                        "record_type": "todo", "record_id": tid}))
    assert got
    assert rows(db, "SELECT title FROM todos WHERE id = ?", tid) == [("Check the storage choice again",)]


# ---------------------------------------------------------------- an older database
def test_history_rows_an_older_version_wrote(backend):
    """Rows as 2.2 - 2.4.3 wrote them: only a few columns, no app / identity columns filled,
    a payload stored as an object (not a string), and an old_row that isn't JSON at all."""
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    iid = _item(db, _pid(db), f"Old item about: {LONG}")
    h1 = _hist(db, iid, {"summary": f"Old item about: {LONG}",
                         "payload": {"quotes": [LONG, "keep me"]}}, None, edit="o1")
    h2 = _hist(db, iid, f"plain text row: {LONG}", None, edit="o2", raw=True)
    h3 = _hist(db, iid, None, None, edit="o3")
    _erase_decision(S, db, did)
    r1, r2, r3 = _row(db, h1), _row(db, h2), _row(db, h3)
    assert r1 and r2 and r3
    assert json.loads(r1["old_row"])["payload"] == {"quotes": ["[erased]", "keep me"]}
    assert r2["old_row"] == "plain text row: [erased]"
    assert (r3["old_row"], r3["new_row"]) == (None, None)
    for r in (r1, r2):
        assert _leaks(r, LONG) == [], r


def test_the_report_counts_blanked_history_rows(backend):
    from flowstate import erase
    S, db = backend, backend.backend.db_path
    did = log(S, LONG)["record"]["id"]
    iid = _item(db, _pid(db), "Retro")
    _hist(db, iid, {"summary": f"Retro: {LONG}"}, {"summary": "Retro"})
    S.backend.tracker.close_instance()
    _delete(S, "decision", did)
    n = _erase(db, f"decision:{did}")
    S.backend.tracker = None
    assert n["history_blanked"] == 1
    assert any("1 edit-history row(s) of other items" in line for line in erase.report(n))
