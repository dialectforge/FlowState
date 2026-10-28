"""`flowstate erase`: remove deleted records for good (2.4.4, data security audit D5).

A delete in FlowState is undoable: the removed rows are kept in the edit history
(deletes.py). Erase is the user's own step after that, from their terminal only (they type
y; never a tool, never piped input). For each deleted record it removes:

- the delete's copy in the edit history (so it can't be undone any more), and every earlier
  history row of each removed row; the text of every version is looked for elsewhere too
  (task 57, NEW-1). A record deleted with its parent is taken out of the parent's delete
  only, which stays undoable, with quotes of it in that delete's other rows (NEW-5);
- keyword-search (memory_fts) and meaning-search (meaning_index) rows of the removed rows;
- source / check-date rows (record_meta) and change alerts (link_alerts) that name them;
- their evidence (entry_evidence), a todo's claim history (todo_claim_events) and the topic
  of a disagreement card naming them (NEW-3);
- the text of review items about them (they came from them, name them, or belong to their
  card); any other item loses only a distinctive sentence quoted from them, never a short
  phrase it happens to share (NEW-2); the edit history of every item it changes (the text
  before an "approve with edits", task 68) goes too;
- events in the automatic session log (auto_sessions) whose text matches a removed row (a
  short text only on an event of the record's own kind, NEW-4);
- attached files FlowState copied into its own folder (projects/<p>.flowstate/attachments).

A hard rule the user removed stays on record as removed; erase blanks its text (NEW-6).

Then it turns on SQLite's secure_delete for the change, rebuilds the keyword index, empties
the -wal file and VACUUMs, so the old bytes leave the database file too. No safety copy is
taken (it would keep the text).

What erase can't reach (README "Erasing for good" says the same): backups made before it
(kept 14 days daily / 8 weeks weekly; safety copies at least 7 days), the off-site GitHub copy
until the next nightly push replaces it (GitHub may keep the old commit for a while), data-git
history (every pushed export stays in that repository), and anything an app copied out.
"""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn, history
from .deletes import TYPES

ERASED = "[erased]"
# table -> memory_fts content_type names used for its rows (older versions wrote several)
_FTS_TYPES = {"conversations": ("decision", "conversation"), "todos": ("todo",),
              "learnings": ("learning",), "changes": ("change",), "problems": ("problem",),
              "solution_attempts": ("attempt", "solution_attempt"), "solutions": ("solution",),
              "components": ("component",), "projects": ("project",),
              "attachments": ("attachment", "file")}
_MEANING_TYPES = {"conversations": "decision", "todos": "todo", "learnings": "learning",
                  "changes": "change", "problems": "problem", "solutions": "solution"}
_SPEC = re.compile(r"^([a-z_]+):(\d+)$")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone() is not None


def _rtype_of(table: str) -> Optional[str]:
    return next((k for k, v in TYPES.items() if v[0] == table), None)


# Edits that removed a record and can still be undone: a delete (deletes.py, tool
# delete/<type>), or an undo that took back a record an app had created (history.py,
# tool history/undo with no row after it: manage_history's documented way for an AI to take
# back its own record). Both keep the removed row in the edit history.
_REMOVED_SQL = """new_row IS NULL AND undone_at IS NULL AND (tool LIKE 'delete/%'
                  OR tool = 'history/undo')"""


def _delete_edit(conn, rtype: str, rid: int) -> Optional[dict]:
    """The newest edit that removed this record (a delete, or an undo of its creation) and
    wasn't undone, with its edit id, project, date and how it went. A record that went with
    its parent (a todo deleted with its project) is found in the parent's delete: `whole` is
    then False, and erase takes only this record (and what went with it) out of that edit,
    so the parent's own delete can still be undone (task 56, NEW-5)."""
    table = TYPES[rtype][0]
    row = conn.execute(
        f"""SELECT edit_id, project_id, created_at, tool FROM record_history WHERE
            table_name = ? AND record_id = ? AND {_REMOVED_SQL}
            AND (tool = ? OR tool = 'history/undo') ORDER BY id DESC LIMIT 1""",
        (table, rid, f"delete/{rtype}")).fetchone()
    whole = True
    if row is not None:
        # an undo, or a delete of this very record (not one of the same type it went with)
        first = conn.execute("SELECT table_name, record_id FROM record_history WHERE edit_id = ? "
                             "ORDER BY id LIMIT 1", (row["edit_id"],)).fetchone()
        whole = row["tool"] == "history/undo" or tuple(first) == (table, rid)
    else:
        row = conn.execute(
            f"""SELECT edit_id, project_id, created_at, tool FROM record_history WHERE
                table_name = ? AND record_id = ? AND {_REMOVED_SQL} ORDER BY id DESC LIMIT 1""",
            (table, rid)).fetchone()
        whole = False
    if row is None:
        return None
    out = dict(row)
    tool = out.pop("tool")
    out["whole"] = whole
    if tool == "history/undo":
        out["how"] = "undone"
    elif whole:
        out["how"] = "deleted"
    else:
        out["how"] = f"deleted with its {tool.split('/', 1)[1]}"
    return out


def _removed_rule(conn, rid: int) -> Optional[dict]:
    """A hard rule the user removed (it stays on record as removed, never deleted): its text
    can be erased too (task 56, NEW-6)."""
    if not _has(conn, "project_rules"):
        return None
    r = conn.execute("SELECT project_id, removed_at FROM project_rules WHERE id = ?",
                     (rid,)).fetchone()
    if r is None or not r["removed_at"]:
        return None
    return {"edit_id": None, "project_id": r["project_id"], "created_at": r["removed_at"],
            "how": "removed", "whole": False}


def _cutoff(days: int) -> str:
    """The date `days` days ago (UTC); a number of days past the calendar's start means
    everything (task 56 Info: --deleted-before 99999999 crashed)."""
    try:
        return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, ValueError):
        return "9999-12-31 23:59:59"


def plan(db_path: Path, specs: list, deleted_before: Optional[int] = None) -> dict:
    """What erase would remove. `specs`: "todo:12", "project:4", ...; `deleted_before`: every
    record deleted more than that many days ago. Raises ValueError for a bad or live record."""
    conn = _conn(db_path)
    try:
        if not _has(conn, "record_history"):
            raise ValueError("this database has no edit history (migration 005), so there is "
                             "nothing deleted to erase")
        targets = []
        for spec in specs or []:
            m = _SPEC.match(str(spec).strip().lower())
            if not m or m.group(1) not in TYPES:
                raise ValueError(f"{spec!r}: use TYPE:ID with TYPE one of {', '.join(TYPES)}")
            rtype, rid = m.group(1), int(m.group(2))
            table = TYPES[rtype][0]
            if rtype == "rule" and conn.execute("SELECT 1 FROM project_rules WHERE id = ?",
                                                (rid,)).fetchone():
                edit = _removed_rule(conn, rid)
                if edit is None:
                    raise ValueError(f"rule #{rid} is an active hard rule. Remove it first with "
                                     f"`flowstate rules remove {rid}` in your terminal, then "
                                     f"erase it.")
                targets.append({"type": rtype, "id": rid, **edit})
                continue
            if conn.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (rid,)).fetchone():
                raise ValueError(f"{rtype} #{rid} isn't deleted. Delete it first (the review "
                                 f"page, or ask your AI app to delete it), then erase it.")
            edit = _delete_edit(conn, rtype, rid)
            if edit is None:
                raise ValueError(f"{rtype} #{rid}: no delete of it (and no undo that took it "
                                 f"back, or delete of what it belonged to) is in the edit "
                                 f"history: it never existed, was already erased, or was removed "
                                 f"without an edit-history copy (by an older FlowState, or "
                                 f"outside FlowState), so there is no copy of it here to erase")
            targets.append({"type": rtype, "id": rid, **edit})
        if deleted_before is not None:
            if deleted_before < 0:
                raise ValueError("--deleted-before needs 0 or more days")
            cut = _cutoff(deleted_before)
            seen = {(t["type"], t["id"]) for t in targets}
            for r in conn.execute(
                    f"""SELECT tool, table_name, record_id, edit_id, project_id, created_at FROM
                        record_history WHERE {_REMOVED_SQL} AND created_at <= ? ORDER BY id""",
                    (cut,)):
                undone = r["tool"] == "history/undo"
                rtype = _rtype_of(r["table_name"]) if undone else r["tool"].split("/", 1)[1]
                if rtype not in TYPES or TYPES[rtype][0] != r["table_name"] \
                        or (rtype, r["record_id"]) in seen:
                    continue
                if conn.execute(f'SELECT 1 FROM "{r["table_name"]}" WHERE id = ?',
                                (r["record_id"],)).fetchone():
                    continue   # brought back some other way
                seen.add((rtype, r["record_id"]))
                targets.append({"type": rtype, "id": r["record_id"], "edit_id": r["edit_id"],
                                "project_id": r["project_id"], "created_at": r["created_at"],
                                "how": "undone" if undone else "deleted", "whole": True})
            if _has(conn, "project_rules"):   # rules the user removed that long ago
                for r in conn.execute("SELECT id FROM project_rules WHERE removed_at IS NOT NULL "
                                      "AND removed_at <= ? AND rule != ? ORDER BY id", (cut, ERASED)):
                    if ("rule", r["id"]) not in seen:
                        seen.add(("rule", r["id"]))
                        targets.append({"type": "rule", "id": r["id"], **_removed_rule(conn, r["id"])})
        rows = {}
        for t in targets:
            if t["edit_id"] is None:   # a removed rule: the row is still there
                table = TYPES[t["type"]][0]
                live = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (t["id"],)).fetchone()
                rows[(table, t["id"])] = dict(live) if live else {}
                continue
            removed = {}
            for h in conn.execute("SELECT table_name, record_id, old_row, new_row FROM "
                                  "record_history WHERE edit_id = ?", (t["edit_id"],)):
                if h["new_row"] is None:   # a row the edit removed (not one it changed)
                    removed[(h["table_name"], h["record_id"])] = json.loads(h["old_row"] or "{}")
            rows.update(removed if t.get("whole", True) else
                        _with_it(conn, removed, (TYPES[t["type"]][0], t["id"])))
        return {"targets": targets, "rows": rows, "versions": _versions(conn, rows)}
    finally:
        conn.close()


def _with_it(conn, removed: dict, root: tuple) -> dict:
    """Of the rows one delete removed, `root` and the rows that went because of it (its
    children by foreign key, links to it and its evidence), not its parent or siblings."""
    from .deletes import _ENDPOINTS, _children
    kids = _children(conn)
    out, queue = {}, [root]
    while queue:
        t, k = queue.pop()
        row = removed.get((t, k))
        if row is None or (t, k) in out:
            continue
        out[(t, k)] = row
        for child, col, pcol, _on in kids.get(t, ()):
            val = row.get(pcol or "id")
            queue += [(ct, ck) for (ct, ck), r in removed.items()
                      if ct == child and val is not None and r.get(col) == val]
        if t in _ENDPOINTS:
            kind = _ENDPOINTS[t]
            queue += [(ct, ck) for (ct, ck), r in removed.items()
                      if (ct == "record_links" and ((r.get("from_type"), r.get("from_id")) == (kind, k)
                                                     or (r.get("to_type"), r.get("to_id")) == (kind, k)))
                      or (ct == "entry_evidence" and (r.get("record_type"), r.get("record_id")) == (kind, k))]
    return out


def _versions(conn, rows: dict) -> list:
    """Every earlier and later version of the removed rows the edit history holds: text an
    edit replaced (a renamed todo's first title) is erased too (task 56, NEW-1)."""
    out = []
    for (table, rid) in rows:
        for h in conn.execute("SELECT old_row, new_row FROM record_history WHERE table_name = ? "
                              "AND record_id = ?", (table, rid)):
            for v in (h["old_row"], h["new_row"]):
                try:
                    d = json.loads(v) if v else None
                except ValueError:
                    d = None
                if isinstance(d, dict):
                    out.append((table, d))
    if _has(conn, "review_items"):   # the text an "approve with edits" replaced (task 68)
        for (table, rid) in rows:
            for h in conn.execute(
                    """SELECT h.old_row FROM record_history h JOIN review_items r ON r.id = h.record_id
                       WHERE h.table_name = 'review_items' AND h.tool = 'queue/review_edit'
                       AND r.result_table = ? AND r.result_id = ?""", (table, rid)):
                d = _loads(h["old_row"])
                if isinstance(d, dict):
                    out.append(("review_text", d))
    return out


def describe(p: dict) -> list:
    if not p["targets"]:
        return ["Nothing to erase."]
    out = [f"Erase {len(p['targets'])} deleted record(s), {len(p['rows'])} row(s) in all:"]
    for t in p["targets"][:20]:
        out.append(f"  {t['type']} #{t['id']} ({t.get('how', 'deleted')} "
                   f"{str(t['created_at'])[:16]} UTC)")
    if len(p["targets"]) > 20:
        out.append(f"  ... and {len(p['targets']) - 20} more")
    out.append("Their copies in the edit history, older versions included (so undo stops working "
               "for them), the search indexes, the session log, review items about them, their "
               "evidence and claim history are removed, then the database is compacted. Backups "
               "made before now still hold them until they age out.")
    return out


# The free-text columns of each table erase takes rows from: what a person or an AI wrote.
# Ids, dates, status / priority / category words, hashes and names of the app that wrote it
# are not text to look for elsewhere (audit N-F2: matching those damaged unrelated items).
_TEXT_COLUMNS = {
    "review_text": ("summary", "details"),   # a review item's text before it was edited
    "projects": ("name", "description"),
    "components": ("name", "description"),
    "conversations": ("user_prompt_summary", "assistant_response_summary", "key_decisions"),
    "changes": ("field_name", "old_value", "new_value", "reason"),
    "learnings": ("insight", "context"),
    "todos": ("title", "description"),
    "problems": ("title", "description", "root_cause"),
    "solution_attempts": ("description", "notes"),
    "solutions": ("summary", "code_snippet", "key_insight"),
    "handoff_notes": ("where_i_was", "next_step"),
    "project_rules": ("rule", "removed_request"),
    "record_links": ("note", "url"),
    "project_variables": ("name", "value", "description"),
    "project_methods": ("name", "description", "steps", "code_example"),
    "attachments": ("file_name", "user_description", "ai_description", "ai_summary"),
}
# Text this long is distinctive enough to look for in other review items; shorter text is
# matched only as a whole session-log event label of the same project.
MIN_TEXT = 12
_DECISION_PREFIX = "Decision: "   # how log_work stores a decision's summary line


def _norm(v: str) -> str:
    return unicodedata.normalize("NFC", " ".join(v.split()))


def _values(table: str, row: dict) -> list:
    """The row's free-text values, normalised. A decision's key_decisions (a JSON list) gives
    each item, and its summary line also without the "Decision: " it is stored with: the
    session log and quotes elsewhere use the plain text (audit N-F3)."""
    out = []
    for col in _TEXT_COLUMNS.get(table, ()):
        v = row.get(col)
        if not isinstance(v, str) or not v.strip():
            continue
        if table == "conversations" and col == "key_decisions":
            try:
                items = json.loads(v)
            except ValueError:
                items = [v]
            out += [_norm(i) for i in (items if isinstance(items, list) else [items])
                    if isinstance(i, str) and i.strip()]
            continue
        out.append(_norm(v))
        if table == "conversations" and v.startswith(_DECISION_PREFIX):
            out.append(_norm(v[len(_DECISION_PREFIX):]))
    return [t for t in out if t]


def _texts(rows, min_len: int = MIN_TEXT) -> list:
    """The removed rows' free-text values (normalised, `min_len`+ characters), longest first.
    `rows`: {(table, id): row}, or a list of (table, row) (every version, _versions)."""
    pairs = [(t, r) for (t, _rid), r in rows.items()] if isinstance(rows, dict) else rows
    out = set()
    for table, row in pairs:
        out.update(t for t in _values(table, row) if len(t) >= min_len)
    return sorted(out, key=len, reverse=True)


# Session-log label kinds ("todo: ...", "manage_todos/update: ...") that can be about a row of
# each table: a short title is matched only on an event of its own kind (task 56, NEW-4).
_EVENT_KINDS = {"todos": ("todo",), "conversations": ("decision", "conflict"),
                "learnings": ("learning",), "changes": ("change",), "problems": ("problem",),
                "solution_attempts": ("attempt", "problem"), "solutions": ("solution", "problem"),
                "components": ("component", "project"), "projects": ("project",),
                "handoff_notes": ("handoff",), "project_rules": ("rule",),
                "record_links": ("link",), "project_variables": ("variable", "memory"),
                "project_methods": ("method", "memory"), "attachments": ("file", "attach")}


def _labels(pairs: list) -> list:
    """(text, kinds) for the session log: every free-text value of every version, any length;
    kinds None (any event of the project) for distinctive text, else the table's own kinds."""
    out = {}
    for table, row in pairs:
        for t in _values(table, row):
            kinds = None if len(t) >= MIN_TEXT else _EVENT_KINDS.get(table, (table,))
            if t in out and (out[t] is None or kinds is None):
                out[t] = None
            else:
                out[t] = tuple(sorted(set(out.get(t) or ()) | set(kinds or ()))) if kinds else None
    return sorted(out.items(), key=lambda x: len(x[0]), reverse=True)


def _event_matches(event: str, labels: list) -> bool:
    """A session-log label "<kind>: <text>" (server._label: the text normalised, cut at 70
    characters) that is one of these texts, or the start of a longer one; a short text only
    on an event of its own kind."""
    what, _, text = event.partition(": ")
    text = _norm(text)
    if not text or what.startswith("read"):
        return False
    for t, kinds in labels:
        if t == text or (len(text) >= 60 and t.startswith(text)):
            if kinds is None or any(k in what for k in kinds):
                return True
    return False


def _pattern(t: str, loose: bool):
    """`t` as a regular expression: exact, or (`loose`) with any run of whitespace where `t`
    has a space, so a quote that was wrapped onto two lines still matches. Both Unicode forms
    of an accented letter (composed / decomposed, NFC / NFD) match (task 56 Info)."""
    forms = list(dict.fromkeys(unicodedata.normalize(f, t) for f in ("NFC", "NFD")))
    if not loose:
        return re.compile("|".join(re.escape(x) for x in forms))
    return re.compile("|".join(r"\s+".join(re.escape(w) for w in x.split(" ")) for x in forms))


def _redact(s, pats: list):
    """`s` with every match replaced by [erased]; everything outside a match (line breaks
    too) stays exactly as it was."""
    if not isinstance(s, str) or not s:
        return s
    for pat in pats:
        s = pat.sub(ERASED, s)
    return s


def _redact_json(v, pats: list):
    if isinstance(v, str):
        return _redact(v, pats)
    if isinstance(v, list):
        return [_redact_json(x, pats) for x in v]
    if isinstance(v, dict):
        return {k: _redact_json(x, pats) for k, x in v.items()}
    return v


def _mentions(v, refs: set) -> bool:
    """Does a review item's payload name one of the erased records ((type, id) pairs)? A
    session summary's records list ([type, id, verb]), or a {record_type, record_id} /
    {type, id} pair anywhere in it."""
    if isinstance(v, list):
        if len(v) >= 2 and isinstance(v[0], str) and isinstance(v[1], int) \
                and (v[0], v[1]) in refs:
            return True
        return any(_mentions(x, refs) for x in v)
    if isinstance(v, dict):
        for tk, ik in (("record_type", "record_id"), ("type", "id"), ("item_type", "item_id")):
            if isinstance(v.get(tk), str) and isinstance(v.get(ik), int) \
                    and (v[tk], v[ik]) in refs:
                return True
        return any(_mentions(x, refs) for x in v.values())
    return False


def _about(conn, rows: dict) -> set:
    """Review items that are about an erased record: they name it in their payload (a session
    summary's records, a delete request, a conflict or "did you say this?" item), it came
    from them, or they belong to a disagreement card or possible conflict naming it."""
    refs = {(rt, rid) for (table, rid) in rows if (rt := _rtype_of(table))}
    ids = set()
    for r in conn.execute("SELECT id, result_table, result_id, payload FROM review_items"):
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = None
        if (r["result_table"], r["result_id"]) in rows or _mentions(payload, refs):
            ids.add(r["id"])
    if _has(conn, "conflict_cards"):
        for c in conn.execute("SELECT review_item_id, a_type, a_id, b_type, b_id FROM conflict_cards"):
            if c["review_item_id"] and ((c["a_type"], c["a_id"]) in refs or (c["b_type"], c["b_id"]) in refs):
                ids.add(c["review_item_id"])
    for (table, rid), row in rows.items():
        if table == "decision_conflicts" and row.get("review_item_id"):
            ids.add(row["review_item_id"])
    return ids


# A quote in a review item that is not about an erased record is erased only when it is a
# distinctive sentence of it (this many words and characters or more), never a shared phrase
# such as a component's name ("Authentication") (task 56, NEW-2).
QUOTE_WORDS, QUOTE_CHARS = 5, 30


def _distinctive(t: str) -> bool:
    return len(t) >= QUOTE_CHARS and len(t.split()) >= QUOTE_WORDS


def _card_topics(conn, rows: dict) -> list:
    """The topics of disagreement cards that name an erased record (text about it)."""
    if not _has(conn, "conflict_cards"):
        return []
    refs = {(rt, rid) for (table, rid) in rows if (rt := _rtype_of(table))}
    return sorted({_norm(c["topic"]) for c in conn.execute(
        "SELECT topic, a_type, a_id, b_type, b_id FROM conflict_cards")
        if c["topic"] and c["topic"] != ERASED
        and ((c["a_type"], c["a_id"]) in refs or (c["b_type"], c["b_id"]) in refs)}, key=len, reverse=True)


def _review_texts(conn, rows: dict, texts: list, about_only: tuple = (),
                  changed: Optional[set] = None) -> int:
    """Quotes of the erased text (every version) in review items. An item about an erased
    record (_about) loses every quote of it, across line breaks. Any other item only loses
    distinctive sentences of it: across line breaks in the records' own project, exactly
    elsewhere. Everything else stays byte for byte as it was."""
    if not texts and not about_only:
        return 0
    about = _about(conn, rows)
    projects = {row.get("project_id") for row in rows.values() if row.get("project_id")}
    projects |= {rid for (table, rid) in rows if table == "projects"}
    every = [_pattern(t, True) for t in sorted(set(texts) | set(about_only), key=len, reverse=True)]
    quotes = [t for t in texts if _distinctive(t)]
    loose, exact = [_pattern(t, True) for t in quotes], [_pattern(t, False) for t in quotes]
    n = 0
    for r in conn.execute("SELECT id, project_id, summary, details, payload "
                          "FROM review_items").fetchall():
        pats = every if r["id"] in about else loose if r["project_id"] in projects else exact
        if not pats:
            continue
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = None
        s, d = _redact(r["summary"], pats), _redact(r["details"], pats)
        p = _redact_json(payload, pats) if payload is not None else None
        if (s, d) != (r["summary"], r["details"]) or (p is not None and p != payload):
            conn.execute("UPDATE review_items SET summary = ?, details = ?, payload = ? "
                         "WHERE id = ?", (s, d, json.dumps(p) if p is not None
                                          else r["payload"], r["id"]))
            n += 1
            if changed is not None:
                changed.add(r["id"])
    return n


def _shared_edits(conn, p: dict, texts: list, labels: list) -> int:
    """A record erased out of its parent's delete (NEW-5): the other rows of that delete stay
    (the parent can still be undone), but their copies may quote it: the session log the
    project's delete copied, review items about it, a long quote elsewhere."""
    edits = {t["edit_id"] for t in p["targets"] if t["edit_id"] and not t.get("whole", True)}
    if not edits:
        return 0
    rows = p["rows"]
    refs = {(rt, rid) for (table, rid) in rows if (rt := _rtype_of(table))}
    every = [_pattern(t, True) for t in texts]
    quotes = [_pattern(t, True) for t in texts if _distinctive(t)]
    n = 0
    for e in edits:
        for h in conn.execute("SELECT id, table_name, record_id, old_row, new_row FROM "
                              "record_history WHERE edit_id = ?", (e,)).fetchall():
            out = []
            for v in (h["old_row"], h["new_row"]):
                try:
                    d = json.loads(v) if v else None
                except ValueError:
                    d = None
                if not isinstance(d, dict):
                    out.append(v)
                    continue
                if h["table_name"] == "auto_sessions" and isinstance(d.get("events"), str):
                    try:
                        events = json.loads(d["events"])
                    except ValueError:
                        events = []
                    d = dict(d, events=json.dumps(
                        [f"{x.partition(': ')[0]}: {ERASED}" if isinstance(x, str)
                         and _event_matches(x, labels) else x for x in events]))
                about = h["table_name"] == "review_items" and (
                    (d.get("result_table"), d.get("result_id")) in rows
                    or _mentions(_loads(d.get("payload")), refs))
                d = _redact_json(d, every if about else quotes)
                out.append(json.dumps(d))
            if out != [h["old_row"], h["new_row"]]:
                conn.execute("UPDATE record_history SET old_row = ?, new_row = ? WHERE id = ?",
                             (out[0], out[1], h["id"]))
                n += 1
    return n


def _loads(v):
    try:
        return json.loads(v) if isinstance(v, str) else v
    except ValueError:
        return None


def _linked(conn, rows: dict, pats: list) -> int:
    """Text about the erased records in tables that point at them without a foreign key
    (task 56, NEW-3): their decision evidence, a todo's claim history, and the topic of a
    disagreement card naming them."""
    refs = {(rt, rid) for (table, rid) in rows if (rt := _rtype_of(table))}
    n = 0
    if _has(conn, "entry_evidence"):
        for rt, rid in refs:
            n += conn.execute("DELETE FROM entry_evidence WHERE record_type = ? AND record_id = ?",
                              (rt, rid)).rowcount
    if _has(conn, "todo_claim_events"):
        for (table, rid) in rows:
            if table == "todos":
                n += conn.execute("DELETE FROM todo_claim_events WHERE todo_id = ?", (rid,)).rowcount
    if _has(conn, "conflict_cards"):
        for c in conn.execute("SELECT id, topic, a_type, a_id, b_type, b_id FROM conflict_cards").fetchall():
            if ((c["a_type"], c["a_id"]) in refs or (c["b_type"], c["b_id"]) in refs) \
                    and c["topic"] != ERASED:
                conn.execute("UPDATE conflict_cards SET topic = ? WHERE id = ?", (ERASED, c["id"]))
                n += 1
    return n


def run(db_path: Path, p: dict, data_dir: Optional[Path] = None) -> dict:
    """Carry out a plan. Returns counts per place."""
    db_path = Path(db_path)
    data_dir = Path(data_dir) if data_dir else db_path.parent
    rows = p["rows"]
    pairs = list(p.get("versions") or []) + [(t, r) for (t, _rid), r in rows.items()]
    texts = _texts(pairs)
    labels = _labels(pairs)   # session-log labels: whole-label matches, any length
    n = {"history": 0, "search_index": 0, "meaning_index": 0, "meta": 0, "review_items": 0,
         "session_events": 0, "linked": 0, "files": 0, "rules": 0, "vacuumed": False}
    conn = _conn(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")   # freed pages are zeroed, not left as they were
        conn.execute("BEGIN IMMEDIATE")
        items: set = set()   # review items whose text goes: their edit history goes too
        if _has(conn, "review_items"):   # (before the rows' own items are blanked below)
            items |= _about(conn, rows)
            n["review_items"] += _review_texts(conn, rows, texts, tuple(_card_topics(conn, rows)),
                                               items)
        edits = {t["edit_id"] for t in p["targets"] if t["edit_id"] and t.get("whole", True)}
        for e in edits:
            n["history"] += conn.execute("DELETE FROM record_history WHERE edit_id = ?",
                                         (e,)).rowcount
        for t in p["targets"]:   # a removed hard rule: its row stays, its text goes
            if t["type"] == "rule" and t["edit_id"] is None:
                n["rules"] += conn.execute(
                    "UPDATE project_rules SET rule = ?, removed_request = NULL WHERE id = ? "
                    "AND rule != ?", (ERASED, t["id"], ERASED)).rowcount
        for (table, rid) in rows:
            n["history"] += conn.execute("DELETE FROM record_history WHERE table_name = ? AND "
                                         "record_id = ?", (table, rid)).rowcount
            if _has(conn, "memory_fts") and table in _FTS_TYPES:
                kinds = _FTS_TYPES[table]
                n["search_index"] += conn.execute(
                    f"DELETE FROM memory_fts WHERE content_type IN ({','.join('?' * len(kinds))}) "
                    f"AND content_id IN (?, ?)", (*kinds, str(rid), rid)).rowcount
            if _has(conn, "meaning_index") and table in _MEANING_TYPES:
                n["meaning_index"] += conn.execute(
                    "DELETE FROM meaning_index WHERE content_type = ? AND content_id = ?",
                    (_MEANING_TYPES[table], rid)).rowcount
            rtype = next((k for k, v in TYPES.items() if v[0] == table), None)
            if rtype:   # its side rows' history too: evidence an edit replaced (task 67)
                for side in history.SIDE:
                    n["history"] += conn.execute(
                        f"DELETE FROM record_history WHERE {history._SIDE_OF}",
                        (side, rtype, rid)).rowcount
                for meta in ("record_meta", "link_alerts"):
                    if _has(conn, meta):
                        cols = {c[1] for c in conn.execute(f'PRAGMA table_info("{meta}")')}
                        if {"record_type", "record_id"} <= cols:
                            n["meta"] += conn.execute(
                                f'DELETE FROM "{meta}" WHERE record_type = ? AND record_id = ?',
                                (rtype, rid)).rowcount
                if _has(conn, "review_items"):
                    n["review_items"] += conn.execute(
                        """UPDATE review_items SET summary = ?, details = NULL, payload = '{}'
                           WHERE ((result_table = ? AND result_id = ?) OR (kind = 'delete' AND
                           json_extract(payload, '$.record_type') = ? AND
                           json_extract(payload, '$.record_id') = ?)) AND summary != ?""",
                        (ERASED, table, rid, rtype, rid, ERASED)).rowcount
        for item in sorted(items):   # their text before an "approve with edits" (task 68)
            n["history"] += conn.execute("DELETE FROM record_history WHERE table_name = "
                                         "'review_items' AND record_id = ?", (item,)).rowcount
        n["linked"] = _linked(conn, rows, [_pattern(t, True) for t in texts])
        n["history"] += _shared_edits(conn, p, texts, labels)
        if labels and _has(conn, "auto_sessions"):   # the session log of the records' projects
            pids = {t["project_id"] for t in p["targets"] if t.get("project_id") is not None}
            pids |= {row.get("project_id") for row in rows.values() if row.get("project_id")}
            for r in conn.execute("SELECT session_id, project_id, events FROM auto_sessions"
                                  ).fetchall():
                if r["project_id"] not in pids:
                    continue
                try:
                    events = json.loads(r["events"] or "[]")
                except ValueError:
                    continue
                new = [f"{e.partition(': ')[0]}: {ERASED}"
                       if isinstance(e, str) and _event_matches(e, labels) else e for e in events]
                if new != events:
                    n["session_events"] += sum(a != b for a, b in zip(new, events))
                    conn.execute("UPDATE auto_sessions SET events = ? WHERE session_id = ?",
                                 (json.dumps(new), r["session_id"]))
        conn.execute("COMMIT")
        live = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM projects")}
        if _has(conn, "memory_fts"):   # drop the index's old segments too
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('optimize')")
            conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    n["files"] = _remove_files(rows, data_dir, live)
    n["vacuumed"] = compact(db_path)
    return n


def _remove_files(rows: dict, data_dir: Path, live: dict) -> int:
    """Attached files FlowState copied into its own folder (not files it only pointed at).
    `live`: names of projects that are still there."""
    projects = {rid: row.get("name") for (t, rid), row in rows.items() if t == "projects"}
    gone = 0
    base = (data_dir / "projects").resolve()
    for (table, _rid), row in rows.items():
        if table != "attachments" or row.get("is_external") or not row.get("file_path"):
            continue
        name = projects.get(row.get("project_id")) or live.get(row.get("project_id"))
        if name is None:
            continue
        f = (data_dir / "projects" / f"{str(name).lower().replace(' ', '-')}.flowstate"
             / row["file_path"]).resolve()
        if base in f.parents and f.is_file():
            f.unlink()
            gone += 1
    for pid, name in projects.items():   # an erased project's empty folders
        bundle = data_dir / "projects" / f"{str(name).lower().replace(' ', '-')}.flowstate"
        for d in sorted(bundle.rglob("*"), reverse=True) if bundle.is_dir() else []:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if bundle.is_dir() and not any(bundle.iterdir()):
            bundle.rmdir()
    return gone


def compact(db_path: Path) -> bool:
    """Empty the -wal file into the database and VACUUM it. False if another program kept it
    busy (the rows are erased either way; the next erase or `VACUUM` finishes the file)."""
    conn = _conn(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def report(n: dict) -> list:
    out = [f"Erased: {n['history']} edit-history row(s), {n['search_index'] + n['meaning_index']} "
           f"search-index row(s), {n['meta']} note row(s), {n['review_items']} review item(s), "
           f"{n['session_events']} session-log event(s), {n.get('linked', 0)} linked row(s) "
           f"(evidence, claim history, card topics), {n['files']} attached file(s)."]
    if n.get("rules"):   # a removed rule's row stays on record; only its text went (task 60, I-1)
        out.append(f"Blanked the text of {n['rules']} removed hard rule(s) (the rule stays on "
                   f"record as removed, without its words).")
    out.append("Database compacted." if n["vacuumed"] else
               "The database was busy, so it wasn't compacted yet: old bytes may stay in the file "
               "until you run `flowstate erase --deleted-before 0` again with FlowState apps closed.")
    out.append("Still holding it: backups made before now (until they age out), the off-site "
               "copy until the next nightly push, and data-git history if you use it.")
    return out
