"""Edit history and undo (v2 Phase D, migration 005).

Before FlowState edits a record (todo update, problem solve, link remove, an approved
clean-up move, an approved delete, ...), the rows involved are copied; afterwards the
new rows are copied too, and both go into `record_history`. `undo` puts the rows of the
last edit of a record back as they were (re-inserting deleted rows, removing rows that edit
created) and is itself recorded, so an undo can be undone.

Creating a record is not undone here (that would mean deleting it); use the record's own
status (done / cancelled) or the review queue instead.

A user-approved delete (deletes.py) is one edit holding every row it removed; undoing it puts
them all back. Undoing that undo would delete them again without the user, so it is refused:
deleting again needs a new delete request.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import identity

# record type -> table (for the history tool)
TABLES = {"todo": "todos", "project": "projects", "component": "components",
          "problem": "problems", "solution": "solutions", "attempt": "solution_attempts",
          "variable": "project_variables", "method": "project_methods",
          "attachment": "attachments", "link": "record_links", "learning": "learnings",
          "decision": "conversations", "change": "changes", "rule": "project_rules",
          "handoff": "handoff_notes"}
TYPE_OF = {v: k for k, v in TABLES.items()}

# (tool, action) -> rows the edit can touch: (table, key column, argument holding the key)
TRACKED = {
    ("update_project", None): [("projects", "id", "project_id")],
    ("update_component", None): [("components", "id", "component_id")],
    ("problem", "solve"): [("problems", "id", "problem_id"), ("solutions", "problem_id", "problem_id")],
    ("attempt", "outcome"): [("solution_attempts", "id", "attempt_id")],
    ("todo", "update"): [("todos", "id", "todo_id")],
    ("variable", "update"): [("project_variables", "id", "variable_id")],
    ("method", "update"): [("project_methods", "id", "method_id")],
    ("self_improve", "confirm_skill"): [("learned_skills", "id", "skill_id")],
}

# Which table's rows carry a project, for listing history by project.
_PROJECT_SQL = {
    "projects": "SELECT id FROM projects WHERE id = ?",
    "components": "SELECT project_id FROM components WHERE id = ?",
    "problems": """SELECT c.project_id FROM problems p JOIN components c ON c.id = p.component_id
                   WHERE p.id = ?""",
    "solutions": """SELECT c.project_id FROM solutions s JOIN problems p ON p.id = s.problem_id
                    JOIN components c ON c.id = p.component_id WHERE s.id = ?""",
    "solution_attempts": """SELECT c.project_id FROM solution_attempts a JOIN problems p
                            ON p.id = a.problem_id JOIN components c ON c.id = p.component_id
                            WHERE a.id = ?""",
    "changes": """SELECT c.project_id FROM changes ch JOIN components c ON c.id = ch.component_id
                  WHERE ch.id = ?""",
}


class HistoryError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.payload = {"error": code, "message": message, **extra}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='record_history'").fetchone() is not None


def _val(v):
    return {"$hex": v.hex()} if isinstance(v, (bytes, bytearray)) else v


def _unval(v):
    return bytes.fromhex(v["$hex"]) if isinstance(v, dict) and "$hex" in v else v


_ROW_KEY: dict = {}


def row_key(conn, table: str) -> Optional[str]:
    """The column that identifies a row: `id`, or the table's own INTEGER PRIMARY KEY
    (project_views.project_id, auto_sessions.session_id). None if it has neither."""
    if table in _ROW_KEY:
        return _ROW_KEY[table]
    cols = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    key = None
    if any(c[1] == "id" for c in cols):
        key = "id"
    else:
        pk = [c for c in cols if c[5]]
        if len(pk) == 1 and (pk[0][2] or "").upper() == "INTEGER":
            key = pk[0][1]
    if cols:   # a FlowState table's key column never changes (migrations only add)
        _ROW_KEY[table] = key
    return key


def _rows(conn, table: str, key: str, value) -> dict:
    """{row key: row dict} for rows of `table` where key = value."""
    rk = row_key(conn, table) or "id"
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" WHERE "{key}" = ?', (value,))
    except sqlite3.OperationalError:
        return {}
    return {r[rk]: {k: _val(r[k]) for k in r.keys()} for r in cur.fetchall()}


def _row(conn, table: str, rid) -> Optional[dict]:
    return _rows(conn, table, row_key(conn, table) or "id", rid).get(rid)


def _project_of(conn, table: str, row: Optional[dict]) -> Optional[int]:
    if not row:
        return None
    if table in _PROJECT_SQL:
        r = conn.execute(_PROJECT_SQL[table], (row["id"],)).fetchone()
        if r:
            return r[0]
    if table == "record_links":
        return row.get("from_project_id")
    return row.get("project_id")


class Edit:
    """Rows captured before an edit; `finish` records what changed."""

    def __init__(self, db_path: Path, specs: list, tool: str):
        self.db_path, self.specs, self.tool = Path(db_path), specs, tool
        self.before: dict = {}
        self.project_ids: dict = {}

    def capture(self, conn) -> dict:
        out = {}
        for table, key, value in self.specs:
            for rid, row in _rows(conn, table, key, value).items():
                out[(table, rid)] = row
        return out

    def start(self) -> "Edit":
        conn = connect(self.db_path)
        try:
            self.before = self.capture(conn)
            self.project_ids = {k: _project_of(conn, k[0], v) for k, v in self.before.items()}
        finally:
            conn.close()
        return self

    def finish(self, client: str = "unknown") -> Optional[str]:
        conn = connect(self.db_path)
        try:
            if not available(conn):
                return None
            after = self.capture(conn)
            keys = [k for k in dict.fromkeys(list(self.before) + list(after))
                    if self.before.get(k) != after.get(k)]
            if not keys:
                return None
            edit_id = uuid.uuid4().hex[:16]
            now = _now()
            for table, rid in keys:
                old, new = self.before.get((table, rid)), after.get((table, rid))
                pid = self.project_ids.get((table, rid)) or _project_of(conn, table, new)
                identity.insert(conn, "record_history", {
                    "edit_id": edit_id, "table_name": table, "record_id": rid, "project_id": pid,
                    "tool": self.tool, "old_row": json.dumps(old) if old is not None else None,
                    "new_row": json.dumps(new) if new is not None else None,
                    "client": (client or "")[:80], "created_at": now}, client)
            conn.commit()
            return edit_id
        finally:
            conn.close()


def begin(db_path: Path, name: str, args: dict) -> Optional[Edit]:
    """An Edit for a tracked tool call, or None (not an edit, or no history table)."""
    key = (name, args.get("action"))
    specs = TRACKED.get(key) or TRACKED.get((name, None))
    if not specs:
        return None
    vals = [(t, k, args.get(a)) for t, k, a in specs if args.get(a) is not None]
    if not vals:
        return None
    conn = connect(db_path)
    try:
        if not available(conn):
            return None
    finally:
        conn.close()
    return Edit(db_path, vals, f"{name}/{args['action']}" if args.get("action") else name).start()


@contextmanager
def tracked(db_path: Path, rows: list, tool: str, client: str = "unknown"):
    """Record an edit made inside the block: rows = [(table, key column, key value)]."""
    edit = Edit(db_path, rows, tool).start()
    yield edit
    edit.finish(client)


# ---------------------------------------------------------------- listing
def _changed_fields(old: Optional[dict], new: Optional[dict], width: int = 80) -> dict:
    def short(v):
        s = "" if v is None else str(v)
        return s if len(s) <= width else s[: width - 3] + "..."
    if old is None:
        return {"created": True}
    if new is None:
        return {"deleted": True}
    return {k: {"old": short(old.get(k)), "new": short(new.get(k))}
            for k in sorted(set(old) | set(new))
            if old.get(k) != new.get(k) and k not in ("updated_at",)}


def list_history(db_path: Path, project_id: int, record_type: Optional[str] = None,
                 record_id: Optional[int] = None, limit: int = 20) -> dict:
    conn = connect(db_path)
    try:
        if not available(conn):
            raise HistoryError("history_unavailable", "Edit history isn't set up in this database "
                                                      "yet (migration 005).")
        where, params = ["project_id = ?"], [project_id]
        if record_type:
            if record_type not in TABLES:
                raise HistoryError("bad_type", f"record_type must be one of: {', '.join(TABLES)}.")
            where.append("table_name = ?")
            params.append(TABLES[record_type])
            if record_id is not None:
                where.append("record_id = ?")
                params.append(int(record_id))
        rows = conn.execute(
            f"""SELECT * FROM record_history WHERE {' AND '.join(where)}
                ORDER BY id DESC LIMIT ?""", (*params, max(1, min(int(limit or 20), 100)))).fetchall()
        out = []
        for r in rows:
            old = json.loads(r["old_row"]) if r["old_row"] else None
            new = json.loads(r["new_row"]) if r["new_row"] else None
            out.append({"history_id": r["id"], "edit_id": r["edit_id"],
                        "record_type": TYPE_OF.get(r["table_name"], r["table_name"]),
                        "record_id": r["record_id"], "tool": r["tool"], "client": r["client"],
                        "at": r["created_at"], "undone_at": r["undone_at"],
                        "undo_of": r["undo_of"], "changes": _changed_fields(old, new),
                        **{k: r[k] for k in identity.FIELDS if k in r.keys()}})
        return {"history": out, "count": len(out),
                "note": "manage_history(action=undo, record_type=..., record_id=...) reverts the last "
                        "change to a record."}
    finally:
        conn.close()


# ---------------------------------------------------------------- undo
def _restore(conn, table: str, rid: int, row: Optional[dict]) -> bool:
    """Put one row back as `row` (None = remove it). True if it was re-inserted."""
    rk = row_key(conn, table) or "id"
    exists = conn.execute(f'SELECT 1 FROM "{table}" WHERE "{rk}" = ?', (rid,)).fetchone()
    if row is None:
        conn.execute(f'DELETE FROM "{table}" WHERE "{rk}" = ?', (rid,))
        return False
    cols = list(row)
    vals = [_unval(row[c]) for c in cols]
    if exists:
        sets = ", ".join(f'"{c}" = ?' for c in cols if c != rk)
        conn.execute(f'UPDATE "{table}" SET {sets} WHERE "{rk}" = ?',
                     [v for c, v in zip(cols, vals) if c != rk] + [rid])
        return False
    conn.execute(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                 f'VALUES ({", ".join("?" * len(cols))})', vals)
    return True


def _orphans(conn, inserted: dict) -> list:
    """Re-inserted rows whose parent row is missing (e.g. a problem whose component was
    deleted after it): [(table, id, parent table)]."""
    out = []
    for table, ids in inserted.items():
        try:
            bad = conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchall()
        except sqlite3.DatabaseError:
            continue
        out += [(table, r[1], r[2]) for r in bad if r[1] in ids]
    return out


def undo(db_path: Path, project_id: int, record_type: str, record_id: int,
         client: str = "unknown") -> dict:
    """Revert the last (not yet undone) edit of a record, if nothing changed it since."""
    if record_type not in TABLES:
        raise HistoryError("bad_type", f"record_type must be one of: {', '.join(TABLES)}.")
    table = TABLES[record_type]
    conn = connect(db_path)
    try:
        if not available(conn):
            raise HistoryError("history_unavailable", "Edit history isn't set up yet (migration 005).")
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute(
            """SELECT * FROM record_history WHERE table_name = ? AND record_id = ?
               AND undone_at IS NULL ORDER BY id DESC LIMIT 1""", (table, int(record_id))).fetchone()
        if last is None:
            conn.rollback()
            raise HistoryError("nothing_to_undo",
                               f"No recorded change to {record_type} #{record_id} to undo.")
        if last["tool"] == "history/undo" and last["undo_of"]:
            orig = conn.execute("SELECT tool FROM record_history WHERE edit_id = ? LIMIT 1",
                                (last["undo_of"],)).fetchone()
            if orig and (orig["tool"] or "").startswith("delete/"):
                conn.rollback()
                raise HistoryError("delete_needs_approval",
                                   f"{record_type} #{record_id} was restored after a delete; undoing "
                                   f"that would delete it again without the user. To delete it, make "
                                   f"a new delete request for the user to approve.")
        group = conn.execute("SELECT * FROM record_history WHERE edit_id = ? ORDER BY id",
                             (last["edit_id"],)).fetchall()
        def allowed(g) -> bool:  # the project it was in when edited, or the one it is in now
            if g["project_id"] in (None, project_id):
                return True
            now_row = _row(conn, g["table_name"], g["record_id"])
            return _project_of(conn, g["table_name"], now_row) == project_id
        if not all(allowed(g) for g in group):
            conn.rollback()
            raise HistoryError("project_mismatch",
                               f"That change belongs to another project; repeat with that project.")
        for g in group:   # refuse if something changed the rows since (outside history)
            current = _row(conn, g["table_name"], g["record_id"])
            expected = json.loads(g["new_row"]) if g["new_row"] else None
            if current != expected:
                conn.rollback()
                raise HistoryError("changed_since",
                                   f"{TYPE_OF.get(g['table_name'], g['table_name'])} "
                                   f"#{g['record_id']} changed again after that edit; undo the "
                                   f"newer change first (see history).")
        undo_id, now = uuid.uuid4().hex[:16], _now()
        restored, inserted = [], {}
        for g in group:
            old = json.loads(g["old_row"]) if g["old_row"] else None
            before = _row(conn, g["table_name"], g["record_id"])
            try:
                if _restore(conn, g["table_name"], g["record_id"], old):
                    inserted.setdefault(g["table_name"], set()).add(g["record_id"])
            except sqlite3.IntegrityError as e:
                conn.rollback()
                raise HistoryError("restore_conflict",
                                   f"Can't put {TYPE_OF.get(g['table_name'], g['table_name'])} "
                                   f"#{g['record_id']} back: {e} (for example a new project now has "
                                   f"the same name; rename it first). Nothing was changed.")
            after = _row(conn, g["table_name"], g["record_id"])
            identity.insert(conn, "record_history", {
                "edit_id": undo_id, "table_name": g["table_name"], "record_id": g["record_id"],
                "project_id": g["project_id"], "tool": "history/undo",
                "old_row": json.dumps(before) if before is not None else None,
                "new_row": json.dumps(after) if after is not None else None,
                "client": (client or "")[:80], "created_at": now, "undo_of": g["edit_id"]},
                client)
            restored.append({"record_type": TYPE_OF.get(g["table_name"], g["table_name"]),
                             "record_id": g["record_id"],
                             "now": "deleted" if old is None else "restored"})
        missing = _orphans(conn, inserted)
        if missing:
            conn.rollback()
            t, i, parent = missing[0]
            raise HistoryError("parent_missing",
                               f"{TYPE_OF.get(t, t)} #{i} can't come back on its own: the "
                               f"{TYPE_OF.get(parent, parent)} it belongs to was deleted after "
                               f"it. Undo that delete first. Nothing was changed.")
        conn.execute("UPDATE record_history SET undone_at = ?, undone_by_edit = ? WHERE edit_id = ?",
                     (now, undo_id, last["edit_id"]))
        conn.commit()
        return {"undone": last["edit_id"], "tool": last["tool"], "rows": restored,
                "undo_edit_id": undo_id,
                "note": "Recorded in history; undo again to redo."}
    except HistoryError:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
