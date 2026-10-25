"""`flowstate erase`: remove deleted records for good (2.4.4, data security audit D5).

A delete in FlowState is undoable: the removed rows are kept in the edit history
(deletes.py). Erase is the user's own step after that, from their terminal only (they type
y; never a tool, never piped input). For each deleted record it removes:

- the delete's copy in the edit history (so it can't be undone any more), and every earlier
  history row of each removed row (text that edits overwrote);
- keyword-search (memory_fts) and meaning-search (meaning_index) rows of the removed rows;
- source / check-date rows (record_meta) and change alerts (link_alerts) that name them;
- the text of review items the records came from (an approved suggestion, the delete request);
- events in the automatic session log (auto_sessions) whose text matches a removed row;
- attached files FlowState copied into its own folder (projects/<p>.flowstate/attachments).

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
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
    wasn't undone, with its edit id, project, date and how it went."""
    table = TYPES[rtype][0]
    row = conn.execute(
        f"""SELECT edit_id, project_id, created_at, tool FROM record_history WHERE
            table_name = ? AND record_id = ? AND {_REMOVED_SQL}
            AND (tool = ? OR tool = 'history/undo') ORDER BY id DESC LIMIT 1""",
        (table, rid, f"delete/{rtype}")).fetchone()
    if row is None:
        return None
    out = dict(row)
    out["how"] = "undone" if out.pop("tool") == "history/undo" else "deleted"
    return out


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
            if conn.execute(f'SELECT 1 FROM "{table}" WHERE id = ?', (rid,)).fetchone():
                raise ValueError(f"{rtype} #{rid} isn't deleted. Delete it first (the review "
                                 f"page, or ask your AI app to delete it), then erase it.")
            edit = _delete_edit(conn, rtype, rid)
            if edit is None:
                raise ValueError(f"{rtype} #{rid}: no delete of it (and no undo that took it "
                                 f"back) is in the edit history: it never existed, was already "
                                 f"erased, or was removed without an edit-history copy (by "
                                 f"an older FlowState, or outside FlowState), so there is no "
                                 f"copy of it here to erase")
            targets.append({"type": rtype, "id": rid, **edit})
        if deleted_before is not None:
            if deleted_before < 0:
                raise ValueError("--deleted-before needs 0 or more days")
            cut = (datetime.now(timezone.utc) - timedelta(days=deleted_before)).strftime(
                "%Y-%m-%d %H:%M:%S")
            seen = {(t["type"], t["id"]) for t in targets}
            for r in conn.execute(
                    f"""SELECT tool, table_name, record_id, edit_id, project_id, created_at FROM
                        record_history WHERE {_REMOVED_SQL} AND created_at < ? ORDER BY id""",
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
                                "how": "undone" if undone else "deleted"})
        rows = {}
        for t in targets:
            for h in conn.execute("SELECT table_name, record_id, old_row, new_row FROM "
                                  "record_history WHERE edit_id = ?", (t["edit_id"],)):
                if h["new_row"] is None:   # a row the edit removed (not one it changed)
                    rows[(h["table_name"], h["record_id"])] = json.loads(h["old_row"] or "{}")
        return {"targets": targets, "rows": rows}
    finally:
        conn.close()


def describe(p: dict) -> list:
    if not p["targets"]:
        return ["Nothing to erase."]
    out = [f"Erase {len(p['targets'])} deleted record(s), {len(p['rows'])} row(s) in all:"]
    for t in p["targets"][:20]:
        out.append(f"  {t['type']} #{t['id']} ({t.get('how', 'deleted')} "
                   f"{str(t['created_at'])[:16]} UTC)")
    if len(p["targets"]) > 20:
        out.append(f"  ... and {len(p['targets']) - 20} more")
    out.append("Their copies in the edit history (so undo stops working for them), the search "
               "indexes, the session log and review items are removed, then the database is "
               "compacted. Backups made before now still hold them until they age out.")
    return out


# The free-text columns of each table erase takes rows from: what a person or an AI wrote.
# Ids, dates, status / priority / category words, hashes and names of the app that wrote it
# are not text to look for elsewhere (audit N-F2: matching those damaged unrelated items).
_TEXT_COLUMNS = {
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
    return " ".join(v.split())


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


def _texts(rows: dict, min_len: int = MIN_TEXT) -> list:
    """The removed rows' free-text values (normalised, `min_len`+ characters), longest first."""
    out = set()
    for (table, _rid), row in rows.items():
        out.update(t for t in _values(table, row) if len(t) >= min_len)
    return sorted(out, key=len, reverse=True)


def _event_matches(event: str, texts: list) -> bool:
    """A session-log label "<kind>: <text>" (server._label: the text normalised, cut at 70
    characters) that is one of these texts, or the start of a longer one."""
    what, _, text = event.partition(": ")
    text = _norm(text)
    if not text or what.startswith("read"):
        return False
    return any(t == text or (len(text) >= 60 and t.startswith(text)) for t in texts)


def _pattern(t: str, loose: bool):
    """`t` as a regular expression: exact, or (`loose`) with any run of whitespace where `t`
    has a space, so a quote that was wrapped onto two lines still matches."""
    if not loose:
        return re.compile(re.escape(t))
    return re.compile(r"\s+".join(re.escape(w) for w in t.split(" ")))


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


def _review_texts(conn, rows: dict, texts: list) -> int:
    """Quotes of the erased text in other review items (suggestions, summaries, conflict
    cards). Only distinctive free text is looked for. An item that names an erased record
    (or is in its project) matches across line breaks; any other item only exactly."""
    if not texts:
        return 0
    refs = {(rt, rid) for (table, rid) in rows if (rt := _rtype_of(table))}
    projects = {row.get("project_id") for row in rows.values() if row.get("project_id")}
    projects |= {rid for (table, rid) in rows if table == "projects"}
    loose = [_pattern(t, True) for t in texts]
    exact = [_pattern(t, False) for t in texts]
    n = 0
    for r in conn.execute("SELECT id, project_id, summary, details, payload "
                          "FROM review_items").fetchall():
        try:
            payload = json.loads(r["payload"] or "{}")
        except ValueError:
            payload = None
        near = r["project_id"] in projects or _mentions(payload, refs)
        pats = loose if near else exact
        s, d = _redact(r["summary"], pats), _redact(r["details"], pats)
        p = _redact_json(payload, pats) if payload is not None else None
        if (s, d) != (r["summary"], r["details"]) or (p is not None and p != payload):
            conn.execute("UPDATE review_items SET summary = ?, details = ?, payload = ? "
                         "WHERE id = ?", (s, d, json.dumps(p) if p is not None
                                          else r["payload"], r["id"]))
            n += 1
    return n


def run(db_path: Path, p: dict, data_dir: Optional[Path] = None) -> dict:
    """Carry out a plan. Returns counts per place."""
    db_path = Path(db_path)
    data_dir = Path(data_dir) if data_dir else db_path.parent
    rows, texts = p["rows"], _texts(p["rows"])
    labels = _texts(rows, 1)   # session-log labels: whole-label matches, any length
    n = {"history": 0, "search_index": 0, "meaning_index": 0, "meta": 0, "review_items": 0,
         "session_events": 0, "files": 0, "vacuumed": False}
    conn = _conn(db_path)
    try:
        conn.execute("PRAGMA secure_delete = ON")   # freed pages are zeroed, not left as they were
        conn.execute("BEGIN IMMEDIATE")
        edits = {t["edit_id"] for t in p["targets"]}
        for e in edits:
            n["history"] += conn.execute("DELETE FROM record_history WHERE edit_id = ?",
                                         (e,)).rowcount
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
            if rtype:
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
                           WHERE (result_table = ? AND result_id = ?) OR (kind = 'delete' AND
                           json_extract(payload, '$.record_type') = ? AND
                           json_extract(payload, '$.record_id') = ?)""",
                        (ERASED, table, rid, rtype, rid)).rowcount
        if _has(conn, "review_items"):   # suggestions and summaries quoting them
            n["review_items"] += _review_texts(conn, rows, texts)
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
           f"{n['session_events']} session-log event(s), {n['files']} attached file(s)."]
    out.append("Database compacted." if n["vacuumed"] else
               "The database was busy, so it wasn't compacted yet: old bytes may stay in the file "
               "until you run `flowstate erase --deleted-before 0` again with FlowState apps closed.")
    out.append("Still holding it: backups made before now (until they age out), the off-site "
               "copy until the next nightly push, and data-git history if you use it.")
    return out
