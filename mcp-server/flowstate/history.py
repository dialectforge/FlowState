"""Edit history and undo (v2 Phase D, migration 005).

Before FlowState edits a record (todo update, problem solve, link remove, an approved
clean-up move, an approved delete, ...), the rows involved are copied; afterwards the
new rows are copied too, and both go into `record_history`. `undo` puts the rows of the
last edit of a record back as they were (re-inserting deleted rows, removing rows that edit
created) and is itself recorded, so an undo can be undone.

Creating a record (2.4.0) is recorded too (tool `create/<type>`, or `approve/<type>` when the
user approved it from the review queue), with the links and conflict flags saved with it.
Undoing it removes them, but only when nothing else refers to the record yet and only for
the app that made it: anyone else's record, or one the user approved, needs the user
(manage_memory delete asks them). If only some rows of an edit changed since, undo puts the
rest back and lists what it skipped and why; it refuses only when nothing could be undone.

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

from . import dbconn
from . import identity

# record type -> table (for the history tool)
TABLES = {"todo": "todos", "project": "projects", "component": "components",
          "problem": "problems", "solution": "solutions", "attempt": "solution_attempts",
          "variable": "project_variables", "method": "project_methods",
          "attachment": "attachments", "link": "record_links", "learning": "learnings",
          "decision": "conversations", "change": "changes", "rule": "project_rules",
          "handoff": "handoff_notes"}
TYPE_OF = {v: k for k, v in TABLES.items()}

# Side rows that belong to one record (their record_type / record_id columns): edits to them
# are listed and undone with that record, under these names (task 67: a record's evidence,
# its source tag and last-checked date, its standing after a disagreement card).
SIDE = {"entry_evidence": "evidence", "record_meta": "source tag", "entry_standing": "standing"}
# side tables with no id of their own (one row per record): the history row's record_id is
# the record's id, and its record_type is read from the stored row
KEYED = ("record_meta", "entry_standing")
_SIDE_OF = ("table_name = ? AND json_extract(COALESCE(new_row, old_row), '$.record_type') = ? "
            "AND json_extract(COALESCE(new_row, old_row), '$.record_id') = ?")
# A card's outcome (the user's decision): listed with the record, but undo of the record
# steps over it to the newest ordinary edit and never changes it (task 68).
CARD_SIDE = ("entry_standing",)
UNDO_SIDE = tuple(s for s in SIDE if s not in CARD_SIDE)
# Review-queue rows whose changes are recorded (task 68) under tools starting "queue/": a
# possible conflict's status, a review item's text when approved with edits. Listed, never
# undone (they record the user's decisions).
QUEUE = {"decision_conflicts": "possible conflict", "review_items": "review item"}


def type_of(table: str) -> str:
    """The user-facing name of a table's rows: decision, evidence, ... (else the table)."""
    return TYPE_OF.get(table) or SIDE.get(table) or QUEUE.get(table) or table

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


def new_edit_id() -> str:
    """The current write's change id (receipts.py), so an answer's receipt and the edit
    history name the same change; a fresh id outside a tool call."""
    return identity.change_id() or uuid.uuid4().hex[:16]


def connect(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
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
            edit_id = new_edit_id()
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


# record types whose creation is recorded (and so can be undone); projects and components
# are not (deleting one takes everything in it: that is a delete request for the user)
CREATED = ("decision", "change", "problem", "learning", "todo", "attempt", "variable", "method")
_ENDPOINT = {"conversations": "decision", "changes": "change", "learnings": "learning",
             "problems": "problem", "todos": "todo"}


def record_created(db_path: Path, rtype: str, rid: int, tool: str = "create",
                   client: str = "unknown", edit_id: Optional[str] = None,
                   conn: Optional[sqlite3.Connection] = None) -> Optional[str]:
    """Record that a call created `rtype` #rid (tool create|approve): the new row plus the
    links it made and the conflict flags its save raised, as one edit. With `conn` it joins
    that connection's transaction (receipts.finish: one commit per write)."""
    if rtype not in CREATED:
        return None
    table = TABLES[rtype]
    own = conn is None
    conn = conn or connect(db_path)
    try:
        if not _has(conn, "record_history", db_path):
            return None
        row = _row(conn, table, int(rid))
        if row is None:
            return None
        rows = [(table, int(rid), row)]
        if table in _ENDPOINT and _has(conn, "record_links", db_path):
            rows += [("record_links", r["id"], {k: _val(r[k]) for k in r.keys()}) for r in
                     conn.execute("SELECT * FROM record_links WHERE from_type = ? AND from_id = ? "
                                  "AND removed_at IS NULL", (_ENDPOINT[table], int(rid)))]
        if table == "conversations" and _has(conn, "decision_conflicts", db_path):
            rows += [("decision_conflicts", r["id"], {k: _val(r[k]) for k in r.keys()}) for r in
                     conn.execute("SELECT * FROM decision_conflicts WHERE new_id = ?", (int(rid),))]
        pid = _project_of(conn, table, row)
        edit_id = edit_id or new_edit_id()
        who = identity.fields(conn, "record_history", client)   # once, not per row
        cols = ["edit_id", "table_name", "record_id", "project_id", "tool", "old_row", "new_row",
                "client", "created_at", *who]
        sql = (f'INSERT INTO record_history ({", ".join(cols)}) '
               f'VALUES ({", ".join("?" * len(cols))})')
        now = _now()
        conn.executemany(sql, [(edit_id, t, k, pid, f"{tool}/{rtype}", None, json.dumps(new),
                                (client or "")[:80], now, *who.values()) for t, k, new in rows])
        if own:
            conn.commit()
        return edit_id
    finally:
        if own:
            conn.close()


_HAS: set = set()   # (database, table) known to exist: tables are only ever added


def _has(conn, table: str, db_path=None) -> bool:
    if (str(db_path), table) in _HAS:
        return True
    ok = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      (table,)).fetchone() is not None
    if ok and db_path is not None:
        _HAS.add((str(db_path), table))
    return ok


def side_rows(conn, table: str, rtype: str, rid: int) -> dict:
    """{row id: row} of a record's side rows (SIDE), e.g. its evidence snippets; for a KEYED
    table {record id: its one row}."""
    try:
        cur = conn.execute(f'SELECT * FROM "{table}" WHERE record_type = ? AND record_id = ?',
                           (rtype, int(rid)))
    except sqlite3.OperationalError:
        return {}
    cols = [d[0] for d in cur.description]   # any row factory
    key = "record_id" if table in KEYED else "id"
    return {row[key]: row for row in ({c: _val(v) for c, v in zip(cols, r)}
                                      for r in cur.fetchall())}


def _side_ref(g) -> dict:
    return json.loads(g["new_row"] or g["old_row"] or "{}")


def _current(conn, g) -> Optional[dict]:
    """The row a history row is about, as it is now."""
    if g["table_name"] in KEYED:
        return side_rows(conn, g["table_name"], _side_ref(g).get("record_type"),
                         g["record_id"]).get(g["record_id"])
    return _row(conn, g["table_name"], g["record_id"])


def _put_back(conn, g, row: Optional[dict]) -> bool:
    """_restore for any history row. True if it was re-inserted (checked for a parent)."""
    table = g["table_name"]
    if table not in KEYED:
        return _restore(conn, table, g["record_id"], row)
    conn.execute(f'DELETE FROM "{table}" WHERE record_type = ? AND record_id = ?',
                 (_side_ref(g).get("record_type"), g["record_id"]))
    if row is not None:
        cols = list(row)
        conn.execute(f'INSERT INTO "{table}" ({", ".join(chr(34) + c + chr(34) for c in cols)}) '
                     f'VALUES ({", ".join("?" * len(cols))})', [_unval(row[c]) for c in cols])
    return False


def _user_only(g) -> Optional[str]:
    """Why undo can't change this row: the user's own decisions stay theirs."""
    if (g["tool"] or "").startswith("queue/"):
        return "it records a decision in the review queue, which is the user's"
    if g["table_name"] == "entry_standing":
        return ("it was set when a disagreement card was decided; the card's outcome is the "
                "user's (review page)")
    if g["table_name"] == "record_meta":
        old, new = (json.loads(v) if v else {} for v in (g["old_row"], g["new_row"]))
        made = g["old_row"] is None and g["tool"] == "source/set"   # tagged when it was saved
        if "user_stated" in (old.get("source"), new.get("source")) \
                and old.get("source") != new.get("source") and not made:
            return "only the user confirms or takes back user_stated"
    return None


def record_side(conn, table: str, rtype: str, rid: int, before: dict, tool: str,
                client: str = "unknown", project_id: Optional[int] = None) -> Optional[str]:
    """Record a change to a record's side rows made on `conn` (the caller commits): each row
    before and after, as one edit of the current write (so evidence saved with a new record
    is part of its create edit). `before` is side_rows() taken before the change."""
    if table not in SIDE or not _has(conn, "record_history"):
        return None
    after = side_rows(conn, table, rtype, rid)
    keys = [k for k in dict.fromkeys([*before, *after]) if before.get(k) != after.get(k)]
    if not keys:
        return None
    edit_id, now = new_edit_id(), _now()
    for k in keys:
        old, new = before.get(k), after.get(k)
        identity.insert(conn, "record_history", {
            "edit_id": edit_id, "table_name": table, "record_id": k, "project_id": project_id,
            "tool": tool, "old_row": json.dumps(old) if old is not None else None,
            "new_row": json.dumps(new) if new is not None else None,
            "client": (client or "")[:80], "created_at": now}, client)
    return edit_id


def record_row(conn, table: str, rid: int, before: Optional[dict], tool: str,
               client: Optional[str] = "unknown") -> Optional[str]:
    """Record a change to one review-queue row (QUEUE) made on `conn` (the caller commits):
    the row before and after, with who and when. `tool` starts with "queue/", so undo never
    changes it back. `before` is _row() taken before the change."""
    if table not in QUEUE or not _has(conn, "record_history"):
        return None
    after = _row(conn, table, rid)
    if after == before:
        return None
    edit_id = new_edit_id()
    identity.insert(conn, "record_history", {
        "edit_id": edit_id, "table_name": table, "record_id": rid,
        "project_id": (after or before or {}).get("project_id"), "tool": tool,
        "old_row": json.dumps(before) if before is not None else None,
        "new_row": json.dumps(after) if after is not None else None,
        "client": (client or "")[:80], "created_at": _now()}, client)
    return edit_id


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
            if record_id is not None:   # the record's own rows, and its side rows (evidence)
                where.append("((table_name = ? AND record_id = ?) OR "
                             + " OR ".join(f"({_SIDE_OF})" for _ in SIDE) + ")")
                params += [TABLES[record_type], int(record_id)]
                for side in SIDE:
                    params += [side, record_type, int(record_id)]
            else:
                where.append("table_name = ?")
                params.append(TABLES[record_type])
        rows = conn.execute(
            f"""SELECT * FROM record_history WHERE {' AND '.join(where)}
                ORDER BY id DESC LIMIT ?""", (*params, max(1, min(int(limit or 20), 100)))).fetchall()
        out = []
        for r in rows:
            old = json.loads(r["old_row"]) if r["old_row"] else None
            new = json.loads(r["new_row"]) if r["new_row"] else None
            side = (new or old or {}) if r["table_name"] in SIDE else {}
            out.append({"history_id": r["id"], "edit_id": r["edit_id"],
                        "record_type": type_of(r["table_name"]),
                        "record_id": r["record_id"],
                        **({"of": f"{side.get('record_type')} #{side.get('record_id')}"}
                           if side else {}),
                        "tool": r["tool"], "client": r["client"],
                        "at": r["created_at"], "undone_at": r["undone_at"],
                        "undo_of": r["undo_of"], "changes": _changed_fields(old, new),
                        **{k: r[k] for k in identity.FIELDS if k in r.keys()}})
        res = {"history": out, "count": len(out),
               "note": "manage_history(action=undo, record_type=..., record_id=...) reverts the last "
                       "change to a record."}
        if not record_type:   # user-only decisions (approvals.py): how, from which app, when
            from .approvals import log_rows
            decided = log_rows(db_path, project_id, max(1, min(int(limit or 20), 100)))
            if decided:
                res["approvals"] = decided
        return res
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


def _label(table: str, rid) -> str:
    return f"{type_of(table)} #{rid}"


def _changed_by(conn, g) -> str:
    """Why a row can't be put back: who changed it after this edit, if FlowState saw it."""
    sql, args = "table_name = ? AND record_id = ? AND id > ?", [g["table_name"], g["record_id"], g["id"]]
    if g["table_name"] in KEYED:   # record_id is the record's: a todo #3 is not decision #3
        sql += " AND json_extract(COALESCE(new_row, old_row), '$.record_type') = ?"
        args.append(_side_ref(g).get("record_type"))
    later = conn.execute(f"SELECT * FROM record_history WHERE {sql} ORDER BY id DESC LIMIT 1",
                         args).fetchone()
    if later is None:
        return "changed outside FlowState since"
    who = identity.shown_row(later, fallback=later["client"] or "another app")
    return f"changed again since by {who} (undo that first)"


def _card_names(conn, rtype: str, rid: int) -> Optional[str]:
    """Why taking back a new record would change a decided disagreement card, if it would:
    a card's mark is on it or names it as the entry that stands, or a decided card names it."""
    if _has(conn, "entry_standing") and conn.execute(
            "SELECT 1 FROM entry_standing WHERE (record_type = ? AND record_id = ?) "
            "OR (by_type = ? AND by_id = ?) LIMIT 1", (rtype, rid, rtype, rid)).fetchone():
        return "a disagreement card was decided about it"
    if _has(conn, "conflict_cards") and conn.execute(
            "SELECT 1 FROM conflict_cards WHERE status NOT IN ('open', 'dismissed') AND "
            "((a_type = ? AND a_id = ?) OR (b_type = ? AND b_id = ?)) LIMIT 1",
            (rtype, rid, rtype, rid)).fetchone():
        return "a disagreement card was decided about it"
    return None


def _creation_check(conn, g, caller: identity.Identity, group_keys: set) -> Optional[tuple[str, str]]:
    """(error code, reason) if removing this created row isn't allowed, else None."""
    from . import deletes
    tool = g["tool"] or ""
    rtype = TYPE_OF.get(g["table_name"], g["table_name"])
    if tool.startswith("approve/"):
        return ("user_approved", f"{rtype} #{g['record_id']} was approved by the user in the "
                                 f"review queue; to remove it, manage_memory(action=\"delete\") "
                                 f"asks them.")
    card = _card_names(conn, rtype, g["record_id"])
    if card:
        return ("user_only", f"{rtype} #{g['record_id']} stays: {card}; the card's outcome is the "
                             f"user's (review page).")
    keys = g.keys()
    col = (lambda k: g[k] if k in keys else None)
    made_app = col("app") or g["client"]
    made_by = identity.shown_row(g, fallback=g["client"] or "another app")
    if not identity.same_caller(made_app, col("machine"), col("identity_source"), caller):
        return ("not_your_record", f"{rtype} #{g['record_id']} was created by {made_by}; only "
                                   f"that app can take it back with undo. To remove it, "
                                   f"manage_memory(action=\"delete\") asks the user.")
    try:
        gone, changed = deletes.plan(conn, g["table_name"], g["record_id"])
    except deletes.DeleteError as e:
        return ("still_referenced", f"{rtype} #{g['record_id']}: {e}")
    others = [k for k in list(gone) + list(changed)
              if k not in group_keys and k[0] not in deletes.DERIVED]
    if others:
        t, k = others[0]
        return ("still_referenced", f"{_label(t, k)} refers to {rtype} #{g['record_id']} now")
    return None


def _ask_reopen(conn, restored: list, client: str) -> Optional[dict]:
    """An undo that took back a replacement: the possible conflicts it had closed stay closed
    (queue rows are the user's), so ask the user once whether to reopen them (task 69). On the
    undo's transaction; a failure here never stops the undo."""
    from . import conflict_reopen
    return conflict_reopen.ask(conn, conflict_reopen.undone_replacers(restored), client)


def undo(db_path: Path, project_id: int, record_type: str, record_id: int,
         client: str = "unknown") -> dict:
    """Revert the last (not yet undone) edit of a record: every row of it that nothing
    changed since; the rest are listed as skipped. Refused only if nothing can be undone."""
    if record_type not in TABLES:
        raise HistoryError("bad_type", f"record_type must be one of: {', '.join(TABLES)}.")
    table = TABLES[record_type]
    conn = connect(db_path)
    try:
        if not available(conn):
            raise HistoryError("history_unavailable", "Edit history isn't set up yet (migration 005).")
        conn.execute("BEGIN IMMEDIATE")
        last = conn.execute(   # the record's last edit, or its side rows' (evidence) if
            # newer; a card's outcome (CARD_SIDE) is stepped over, never undone
            f"""SELECT * FROM record_history WHERE undone_at IS NULL AND
                ((table_name = ? AND record_id = ?) OR {" OR ".join(f"({_SIDE_OF})" for _ in UNDO_SIDE)})
                ORDER BY id DESC LIMIT 1""",
            (table, int(record_id), *[v for side in UNDO_SIDE
                                      for v in (side, record_type, int(record_id))])).fetchone()
        if last is None:
            conn.rollback()
            if any(conn.execute(f"SELECT 1 FROM record_history WHERE {_SIDE_OF} LIMIT 1",
                                (side, record_type, int(record_id))).fetchone()
                   for side in CARD_SIDE):
                raise HistoryError("user_only",
                                   f"{record_type} #{record_id}: the only recorded change is a "
                                   f"disagreement card's outcome, which is the user's (review "
                                   f"page). Nothing was changed.")
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
        group = conn.execute("SELECT * FROM record_history WHERE edit_id = ? AND undone_at IS NULL "
                             "ORDER BY id", (last["edit_id"],)).fetchall()
        def allowed(g) -> bool:  # the project it was in when edited, or the one it is in now
            if g["project_id"] in (None, project_id):
                return True
            now_row = _current(conn, g)
            return _project_of(conn, g["table_name"], now_row) == project_id
        if not all(allowed(g) for g in group):
            conn.rollback()
            raise HistoryError("project_mismatch",
                               f"That change belongs to another project; repeat with that project.")
        skipped: dict = {}   # history row id -> (code, reason)
        for g in group:   # rows something changed since (outside history, or a newer edit)
            current = _current(conn, g)
            expected = json.loads(g["new_row"]) if g["new_row"] else None
            if current != expected:
                skipped[g["id"]] = ("changed_since", _changed_by(conn, g))
            elif _user_only(g):
                skipped[g["id"]] = ("user_only", _label(g["table_name"], g["record_id"])
                                    + " stays: " + _user_only(g))
        caller = identity.current() or identity.local(client)
        keys = {(g["table_name"], g["record_id"]) for g in group}
        for g in group:   # removing a created record: only if nothing else needs it
            if g["id"] in skipped or g["old_row"] is not None or g["new_row"] is None \
                    or TYPE_OF.get(g["table_name"]) not in CREATED:
                continue
            bad = _creation_check(conn, g, caller, keys)
            if bad:
                skipped[g["id"]] = bad
        roots = {(g["table_name"], g["record_id"]) for g in group if g["id"] in skipped
                 and g["old_row"] is None and TYPE_OF.get(g["table_name"]) in CREATED}
        if roots:   # a created record that stays keeps the links and flags saved with it
            for g in group:
                if g["id"] not in skipped and g["old_row"] is None \
                        and (g["table_name"], g["record_id"]) not in roots:
                    skipped[g["id"]] = ("kept_with_record", "kept with the record it belongs to")
        undo_id, now = new_edit_id(), _now()
        while True:
            todo = [g for g in group if g["id"] not in skipped]
            if not todo:
                conn.rollback()
                first = next(g for g in group if g["id"] in skipped)
                code, why = skipped[first["id"]]
                if code == "changed_since":
                    why = f"{_label(first['table_name'], first['record_id'])} {why}."
                elif not why.endswith("."):
                    why += "."
                raise HistoryError(code, why + " Nothing was changed.")
            conn.execute("SAVEPOINT undo_rows")
            restored, inserted = [], {}
            for g in todo:
                old = json.loads(g["old_row"]) if g["old_row"] else None
                before = _current(conn, g)
                try:
                    if _put_back(conn, g, old):
                        inserted.setdefault(g["table_name"], set()).add(g["record_id"])
                except sqlite3.IntegrityError as e:
                    conn.rollback()
                    raise HistoryError("restore_conflict",
                                       f"Can't put {_label(g['table_name'], g['record_id'])} back: "
                                       f"{e} (for example a new project now has the same name; "
                                       f"rename it first). Nothing was changed.")
                after = _current(conn, g)
                restored.append((g, before, after, old))
            missing = _orphans(conn, inserted)
            if not missing:
                break
            conn.execute("ROLLBACK TO undo_rows")
            conn.execute("RELEASE undo_rows")
            for t, i, parent in missing:   # it can't come back while its parent is gone
                for g in todo:
                    if (g["table_name"], g["record_id"]) == (t, i):
                        skipped[g["id"]] = ("parent_missing",
                                            f"{_label(t, i)} can't come back on its own: the "
                                            f"{TYPE_OF.get(parent, parent)} it belongs to was "
                                            f"deleted after it. Undo that delete first.")
        conn.execute("RELEASE undo_rows")
        rows = []
        for g, before, after, old in restored:
            identity.insert(conn, "record_history", {
                "edit_id": undo_id, "table_name": g["table_name"], "record_id": g["record_id"],
                "project_id": g["project_id"], "tool": "history/undo",
                "old_row": json.dumps(before) if before is not None else None,
                "new_row": json.dumps(after) if after is not None else None,
                "client": (client or "")[:80], "created_at": now, "undo_of": g["edit_id"]},
                client)
            rows.append({"record_type": type_of(g["table_name"]),
                         "record_id": g["record_id"],
                         "now": "deleted" if old is None else "restored"})
            conn.execute("UPDATE record_history SET undone_at = ?, undone_by_edit = ? WHERE id = ?",
                         (now, undo_id, g["id"]))
        asked = _ask_reopen(conn, restored, client)
        conn.commit()
        out = {"undone": last["edit_id"], "tool": last["tool"], "rows": rows,
               "undo_edit_id": undo_id, "note": "Recorded in history; undo again to redo."}
        if skipped:
            out["skipped"] = [{"record_type": type_of(g["table_name"]),
                               "record_id": g["record_id"], "why": skipped[g["id"]][1]}
                              for g in group if g["id"] in skipped]
            out["note"] = (f"Undid {len(rows)} of {len(group)} rows; skipped {len(skipped)} "
                           f"(listed with why). Recorded in history; undo again to redo.")
        if asked:
            from .conflict_reopen import note
            out["reopen_question"] = asked
            out["note"] += " " + note(asked)
        return out
    except HistoryError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
