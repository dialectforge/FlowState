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


def _delete_edit(conn, rtype: str, rid: int) -> Optional[dict]:
    """The newest delete of this record that wasn't undone, with its edit id, project, date."""
    table = TYPES[rtype][0]
    row = conn.execute(
        """SELECT edit_id, project_id, created_at FROM record_history WHERE tool = ?
           AND table_name = ? AND record_id = ? AND new_row IS NULL AND undone_at IS NULL
           ORDER BY id DESC LIMIT 1""", (f"delete/{rtype}", table, rid)).fetchone()
    return dict(row) if row else None


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
                raise ValueError(f"{rtype} #{rid}: no delete of it is on record (never existed, "
                                 f"or already erased)")
            targets.append({"type": rtype, "id": rid, **edit})
        if deleted_before is not None:
            if deleted_before < 0:
                raise ValueError("--deleted-before needs 0 or more days")
            cut = (datetime.now(timezone.utc) - timedelta(days=deleted_before)).strftime(
                "%Y-%m-%d %H:%M:%S")
            seen = {(t["type"], t["id"]) for t in targets}
            for r in conn.execute(
                    """SELECT tool, table_name, record_id, edit_id, project_id, created_at FROM
                       record_history WHERE tool LIKE 'delete/%' AND new_row IS NULL AND
                       undone_at IS NULL AND created_at < ? ORDER BY id""", (cut,)):
                rtype = r["tool"].split("/", 1)[1]
                if rtype not in TYPES or TYPES[rtype][0] != r["table_name"] \
                        or (rtype, r["record_id"]) in seen:
                    continue
                if conn.execute(f'SELECT 1 FROM "{r["table_name"]}" WHERE id = ?',
                                (r["record_id"],)).fetchone():
                    continue   # brought back some other way
                seen.add((rtype, r["record_id"]))
                targets.append({"type": rtype, "id": r["record_id"], "edit_id": r["edit_id"],
                                "project_id": r["project_id"], "created_at": r["created_at"]})
        rows = {}
        for t in targets:
            for h in conn.execute("SELECT table_name, record_id, old_row, new_row FROM "
                                  "record_history WHERE edit_id = ?", (t["edit_id"],)):
                if h["new_row"] is None:   # a row the delete removed (not one it changed)
                    rows[(h["table_name"], h["record_id"])] = json.loads(h["old_row"] or "{}")
        return {"targets": targets, "rows": rows}
    finally:
        conn.close()


def describe(p: dict) -> list:
    if not p["targets"]:
        return ["Nothing to erase."]
    out = [f"Erase {len(p['targets'])} deleted record(s), {len(p['rows'])} row(s) in all:"]
    for t in p["targets"][:20]:
        out.append(f"  {t['type']} #{t['id']} (deleted {str(t['created_at'])[:16]} UTC)")
    if len(p["targets"]) > 20:
        out.append(f"  ... and {len(p['targets']) - 20} more")
    out.append("Their copies in the edit history (so undo stops working for them), the search "
               "indexes, the session log and review items are removed, then the database is "
               "compacted. Backups made before now still hold them until they age out.")
    return out


def _texts(rows: dict) -> list:
    """The removed rows' text values (normalised, 8+ characters), longest first."""
    out = set()
    for row in rows.values():
        for v in row.values():
            if isinstance(v, str):
                t = " ".join(v.split())
                if len(t) >= 8:
                    out.add(t)
    return sorted(out, key=len, reverse=True)


def _event_matches(event: str, texts: list) -> bool:
    what, _, text = event.partition(": ")
    text = " ".join(text.split())
    if len(text) < 8 or what.startswith("read"):
        return False
    return any(t.startswith(text) or text.startswith(t[:70]) for t in texts)


def _redact(s, texts: list):
    if not isinstance(s, str) or not s:
        return s
    for t in texts:
        if t in " ".join(s.split()):
            s = " ".join(s.split()).replace(t, ERASED)
    return s


def run(db_path: Path, p: dict, data_dir: Optional[Path] = None) -> dict:
    """Carry out a plan. Returns counts per place."""
    db_path = Path(db_path)
    data_dir = Path(data_dir) if data_dir else db_path.parent
    rows, texts = p["rows"], _texts(p["rows"])
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
        if texts and _has(conn, "review_items"):   # suggestions and summaries quoting them
            for r in conn.execute("SELECT id, summary, details FROM review_items").fetchall():
                s, d = _redact(r["summary"], texts), _redact(r["details"], texts)
                if (s, d) != (r["summary"], r["details"]):
                    conn.execute("UPDATE review_items SET summary = ?, details = ? WHERE id = ?",
                                 (s, d, r["id"]))
                    n["review_items"] += 1
        if texts and _has(conn, "auto_sessions"):
            for r in conn.execute("SELECT session_id, events FROM auto_sessions").fetchall():
                try:
                    events = json.loads(r["events"] or "[]")
                except ValueError:
                    continue
                new = [f"{e.partition(': ')[0]}: {ERASED}" if _event_matches(e, texts) else e
                       for e in events]
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
