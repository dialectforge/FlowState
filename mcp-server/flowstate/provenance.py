"""Where a record came from, and when it was last checked (v2 Phase D, E5/E6; migration 011).

source: user_stated (the user said so), ai_inferred (an AI concluded it), from_code (read in
code or commits). verified_at: when someone last confirmed it is still true. Both live in
`record_meta`, a side table; no row means "unknown, never verified", which is what every
older record is.

Who set the source (2.4.0, migration 020): every source write stamps source_set_by (the app,
from identity.py, or 'user' when the user confirmed it), source_set_model (only if the app
supplied it) and source_set_at. Only the user turns an existing record into user_stated:
an agent asking for it (manage_memory set_source / mark_checked with source=user_stated)
leaves the tag as it is and queues one review item of kind `source` (repeats reuse the open
one), which only the user's own click or key decides (approvals.user_only, whatever the
approvals setting). Moving a tag away from user_stated is allowed and stamped. A user_stated
log_work that repeats another app's ai_inferred record (cleanup's duplicate matching) is
saved as ai_inferred (server._log_work, user_stated_dup).

Stale facts: learnings, changes, decisions, variables and methods that mention paths,
ports, hosts, URLs or config files (all server/config/endpoint/environment variables), not
verified (or, if never verified, not created/updated) for STALE_DAYS. They are listed for
re-checking; nothing is changed or hidden because of it.

A confirmation belongs to the words that were approved (2.4.9, migration 025). Approving a
"did you say this?" item stores a hash of those words (`confirmed_hash`) and the same hash
on the review item. A later edit of the words drops the confirmation back to an AI guess
and queues a fresh question; status, priority, completion, blockers, resolving a problem or
its severity, marking a variable sensitive, pointing a method at a component, and moving a
record to another project or component do not. Approving a question whose words have since
changed is refused and the question is closed.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import links

SOURCES = ("user_stated", "ai_inferred", "from_code")
TAGS = {"user_stated": "user", "ai_inferred": "AI", "from_code": "code"}
USER = "user"   # source_set_by when the user confirmed the tag themselves
STAMPS = ("source_set_by", "source_set_model", "source_set_at")
# The words a confirmation is bound to. Anything not listed here can change and the
# confirmation stays: a to-do's status, priority, completed_at and blockers; resolving a
# problem or changing its severity; marking a variable sensitive (is_secret); pointing a
# method at a component; moving a record to another project or component.
CONTENT = {
    "todo": ("title", "description", "due_date"),
    "learning": ("category", "insight", "context"),
    "decision": ("user_prompt_summary", "assistant_response_summary", "key_decisions"),
    "change": ("field_name", "old_value", "new_value", "reason"),
    "problem": ("title", "description", "root_cause"),
    "variable": ("category", "name", "value", "description"),
    "method": ("name", "description", "category", "steps", "code_example"),
}
STALE_MESSAGE = ("This confirmation was for different words. The record changed after it was "
                 "queued, so it was closed and nothing was marked as confirmed by you.")
WORDS_CHANGED = "The words changed after this was queued, so this confirmation was closed."
TABLES = {"todo": "todos", "decision": "conversations", "change": "changes",
          "learning": "learnings", "problem": "problems", "variable": "project_variables",
          "method": "project_methods"}
FACT_TYPES = ("learning", "change", "decision", "variable", "method")
_EXTRA_OWNER = {"variable": "SELECT project_id FROM project_variables WHERE id = ?",
                "method": "SELECT project_id FROM project_methods WHERE id = ?"}

_FACT = re.compile(
    r"[A-Za-z]:\\"                                   # C:\..., D:\...
    r"|(?:^|[\s(`'\"])(?:~|\.{1,2})?/[\w.-]+/"       # /etc/x/, ~/x/, ./x/
    r"|https?://"
    r"|\b(?:\d{1,3}\.){3}\d{1,3}\b"                  # IPv4
    r"|\b(?:localhost|[a-z0-9-]+(?:\.[a-z0-9-]+)+):\d{2,5}\b"   # host:port
    r"|\bports?\s*[:=#]?\s*\d{2,5}\b"
    r"|\.(?:json|toml|ya?ml|ini|env|cfg|conf|xml|plist)\b"
    r"|\bconfig(?:uration)?\b",
    re.I)
_ENV_VAR = re.compile(r"\b[A-Z][A-Z0-9]+_[A-Z0-9_]+\b")   # FLOWSTATE_DB (case matters)
_FACT_VAR_CATEGORIES = ("server", "config", "endpoint", "environment")


class MetaError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def stale_days() -> int:
    try:
        return max(1, int(os.environ.get("FLOWSTATE_STALE_DAYS", "60")))
    except ValueError:
        return 60


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='record_meta'").fetchone() is not None


def _need(conn) -> None:
    if not available(conn):
        raise MetaError("meta_unavailable", "Source and verified dates aren't set up in this "
                        "database yet (migration 011 has not run). Restart FlowState.")


def owner(conn, rtype: str, rid) -> Optional[int]:
    if rtype in _EXTRA_OWNER:
        row = conn.execute(_EXTRA_OWNER[rtype], (rid,)).fetchone()
        return row[0] if row else None
    return links.owner(conn, rtype, rid)


def check_record(conn, pid: int, rtype: str, rid) -> int:
    if rtype not in TABLES:
        raise MetaError("bad_record_type", "record_type must be one of " + ", ".join(TABLES) + ".")
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        raise MetaError("record_required", "Give record_type and record_id.") from None
    own = owner(conn, rtype, rid)
    if own is None:
        raise MetaError("record_not_found", f"No {rtype} with id {rid}.")
    if own != pid:
        raise MetaError("project_mismatch", f"{rtype.capitalize()} #{rid} belongs to project "
                        f"'{links.project_name(conn, own)}', not this one.")
    return rid


def _upsert(conn, rtype: str, rid: int, tool: str, client: Optional[str] = None,
            edit_id: Optional[str] = None, **cols) -> None:
    """Set a record's source tag / checked date; the row before and after goes into the edit
    history (task 67), so the earlier tag is kept and undo can put it back. `edit_id` joins
    the history row to an edit already in progress."""
    from . import history
    before = history.side_rows(conn, "record_meta", rtype, rid)
    conn.execute("INSERT OR IGNORE INTO record_meta (record_type, record_id) VALUES (?, ?)",
                 (rtype, rid))
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE record_meta SET {sets}, updated_at = ? WHERE record_type = ? "
                 f"AND record_id = ?", (*cols.values(), _now(), rtype, rid))
    history.record_side(conn, "record_meta", rtype, rid, before, tool, client or "unknown",
                        owner(conn, rtype, rid), edit_id=edit_id)


def stamped(conn) -> bool:
    """Migration 020 has run (who set the source is stored)."""
    return "source_set_by" in {r[1] for r in conn.execute("PRAGMA table_info(record_meta)")}


def has_confirmed_hash(conn) -> bool:
    """Migration 025 has run (a confirmation is tied to the words that were approved)."""
    return "confirmed_hash" in {r[1] for r in conn.execute("PRAGMA table_info(record_meta)")}


def content_hash(rtype: str, row: dict) -> str:
    """Hash of the words a confirmation is bound to, for this record as it is now."""
    payload = {k: row.get(k) for k in CONTENT[rtype]}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _content_changed(rtype: str, old: dict, new: dict) -> bool:
    return any(old.get(k) != new.get(k) for k in CONTENT[rtype])


def _row(conn, rtype: str, rid) -> Optional[dict]:
    table = TABLES.get(rtype or "")
    if not table:
        return None
    try:
        rid = int(rid)
    except (TypeError, ValueError):
        return None
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (rid,))
    row = cur.fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(zip([d[0] for d in cur.description], row))


def _hash_row(conn, rtype: str, rid) -> Optional[str]:
    if rtype not in CONTENT:
        return None
    row = _row(conn, rtype, rid)
    return content_hash(rtype, row) if row else None


def _source_cols(conn, source: str, by: Optional[tuple] = None,
                 client: Optional[str] = None) -> dict:
    """source plus who set it: `by` = (app, model), else the current caller (identity.py)."""
    cols = {"source": source}
    if stamped(conn):
        if by is None:
            from . import identity
            who = identity.stamp(client)
            by = (who.get("app"), who.get("model"))
        cols.update(source_set_by=(by[0] or "unknown")[:80], source_set_model=by[1],
                    source_set_at=_now())
    return cols


def current_source(conn, rtype: str, rid: int) -> Optional[str]:
    row = conn.execute("SELECT source FROM record_meta WHERE record_type = ? AND record_id = ?",
                       (rtype, rid)).fetchone()
    return row[0] if row else None


def set_source(db_path: Path, rtype: str, rid: int, source: str, client: Optional[str] = None,
               by: Optional[tuple] = None) -> None:
    """Record where a new record came from, stamped with who said so (no-op if migration 011
    hasn't run). At log time an app may tag what it writes (user_stated when it is quoting
    the user); changing an existing record's tag goes through mark()."""
    if source not in SOURCES:
        raise MetaError("bad_source", "source must be one of " + ", ".join(SOURCES) + ".")
    conn = links.connect(db_path)
    try:
        if not available(conn):
            return
        _upsert(conn, rtype, int(rid), "source/set", client, **_source_cols(conn, source, by, client))
        conn.commit()
    finally:
        conn.close()


def _open_confirm(conn, pid: int, rtype: str, rid: int) -> Optional[int]:
    row = conn.execute(
        """SELECT id FROM review_items WHERE project_id = ? AND kind = 'source'
           AND status = 'pending' AND json_extract(payload, '$.record_type') = ?
           AND json_extract(payload, '$.record_id') = ? ORDER BY id LIMIT 1""",
        (pid, rtype, rid)).fetchone()
    return row[0] if row else None


def request_user_stated(conn, pid: int, rtype: str, rid: int, client: str,
                        because: Optional[str] = None) -> tuple[int, bool]:
    """Queue (or find the open) "did you say this yourself?" item: (item id, reused?).

    The item stores a hash of the words as they are now. Approving it later is refused if
    those words have changed (`guard_revision`)."""
    from . import review
    from .deletes import _title
    found = _open_confirm(conn, pid, rtype, rid)
    if found is not None:
        return found, True
    was = current_source(conn, rtype, rid)
    title = _title(conn, rtype, rid)
    details = (f"An AI app asked to tag this {rtype} user_stated (the user said so). Only you can "
               f"confirm that. Approve if you said it; reject to leave it {was or 'untagged'}.")
    if because:
        details = because.rstrip() + " " + details
    payload = {"record_type": rtype, "record_id": rid, "source": "user_stated", "was": was,
               "title": title, "user_only": True}
    hashed = _hash_row(conn, rtype, rid)
    if hashed:
        payload["content_hash"] = hashed
    item = review.add_item(
        None, pid, "source", f'Did you say this yourself? {rtype} #{rid} "{title}"', details,
        payload, (client or "unknown")[:80], conn=conn)
    return item, False


def unchanged_message(rtype: str, was: Optional[str], item: int) -> str:
    return (f"Not changed: only the user can confirm that they said this. The {rtype} stays "
            f"{was or 'untagged'} until they do. Review item #{item} waits for them: the review "
            f"page (`flowstate review-page`) or `flowstate approve {item}` in their terminal.")


def mark(db_path: Path, pid: int, rtype: str, rid, client: str, source: Optional[str] = None,
         verified: bool = True) -> dict:
    """manage_memory mark_checked / set_source. Setting user_stated on an existing record
    never happens here: it queues the user's confirmation instead (see the module text)."""
    if source is not None and source not in SOURCES:
        raise MetaError("bad_source", "source must be one of " + ", ".join(SOURCES) + ".")
    conn = links.connect(db_path)
    try:
        _need(conn)
        rid = check_record(conn, pid, rtype, rid)
        was = current_source(conn, rtype, rid)
        cols, asked = {}, None
        if verified:
            cols.update(verified_at=_now(), verified_by=client)
        if source == "user_stated" and was != "user_stated":
            asked = request_user_stated(conn, pid, rtype, rid, client)
        elif source and source != was:
            cols.update(_source_cols(conn, source, client=client))
        if not cols and not source:
            raise MetaError("nothing_to_do", "Give source=... (or use action=mark).")
        if cols:
            _upsert(conn, rtype, rid, "source/" + ("checked" if verified else "set"), client,
                    **cols)
        conn.commit()
        out = {"record_type": rtype, "record_id": rid, **get(conn, rtype, [rid]).get(rid, {})}
        if asked:
            item, reused = asked
            out.update({"source_unchanged": True, "confirm_requested": item,
                        **({"already_requested": True} if reused else {}),
                        "message": unchanged_message(rtype, was, item)})
        return out
    finally:
        conn.close()


def confirm(db_path: Path, item: dict) -> tuple[str, int]:
    """The user approved a `source` review item: tag the record user_stated, stamped 'user',
    bound to a hash of the words as they are now. Refused when those words are not the ones
    the question was queued for (see `guard_revision`, which also closes the question)."""
    p = json.loads(item.get("payload") or "{}")
    rtype, rid = p.get("record_type"), p.get("record_id")
    conn = links.connect(db_path)
    try:
        _need(conn)
        if rtype not in TABLES or rid is None or owner(conn, rtype, rid) is None:
            from .review import ReviewError
            raise ReviewError("record_not_found", f"The {rtype or 'record'} #{rid} no longer exists.")
        actual = _hash_row(conn, rtype, int(rid))
        expected = p.get("content_hash")
        if expected and actual and expected != actual:
            from .review import ReviewError
            raise ReviewError("stale_approval", STALE_MESSAGE)
        cols = _source_cols(conn, "user_stated", (USER, None))
        if actual and has_confirmed_hash(conn):
            cols["confirmed_hash"] = actual
        _upsert(conn, rtype, int(rid), "source/confirm", USER, **cols)
        conn.commit()
    finally:
        conn.close()
    return TABLES[rtype], int(rid)


def _pending_source(conn, rtype: str, rid: int) -> list:
    """Open "did you say this?" items for this record: [{id, hash}]."""
    from . import review
    if not review.available(conn):
        return []
    out = []
    for iid, payload in conn.execute(
            """SELECT id, payload FROM review_items WHERE kind = 'source' AND status = 'pending'
               AND json_extract(payload, '$.record_type') = ?
               AND json_extract(payload, '$.record_id') = ?""",
            (rtype, int(rid))):
        try:
            body = json.loads(payload or "{}")
        except ValueError:
            body = {}
        out.append({"id": iid, "hash": body.get("content_hash")})
    return out


def _meta(conn, rtype: str, rid: int) -> Optional[dict]:
    if not available(conn):
        return None
    cols = ["source", "source_set_by"] + (["confirmed_hash"] if has_confirmed_hash(conn) else [])
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM record_meta WHERE record_type = ? AND record_id = ?",
        (rtype, int(rid))).fetchone()
    if row is None:
        return None
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(zip(cols, row))


def _void_source(conn, item_id: int, reason: str) -> None:
    """Close one pending source question (rejected by FlowState). The caller commits."""
    from . import identity, review
    who = identity.fields(conn, "review_items", prefix="decided_", ident=identity.SYSTEM)
    sets = "".join(f", {k} = ?" for k in who)
    conn.execute(
        f"""UPDATE review_items SET status = 'rejected', decided_at = ?,
            decided_by = 'flowstate (automatic)', reject_reason = ?{sets}
            WHERE id = ? AND kind = 'source' AND status = 'pending'""",
        (review._now(), reason[:500], *who.values(), int(item_id)))


def guard_revision(db_path: Path, item: dict) -> None:
    """Refuse an approval of a source question whose words changed after it was queued, and
    close that question. Every approve path calls this (review_apply.approve) before the
    question is claimed: the MCP tools, the command line, the review page, the phone page,
    a popup and a card."""
    if not item or item.get("kind") != "source":
        return
    try:
        body = json.loads(item.get("payload") or "{}")
    except ValueError:
        return
    expected = body.get("content_hash")
    if not expected:
        return   # queued before confirmations were tied to the words
    rtype, rid = body.get("record_type"), body.get("record_id")
    conn = links.connect(db_path)
    try:
        actual = _hash_row(conn, rtype, rid) if rtype in CONTENT and rid is not None else None
        if actual is None or actual == expected:
            return
        _void_source(conn, int(item["id"]), WORDS_CHANGED)
        conn.commit()
    finally:
        conn.close()
    from .review import ReviewError
    raise ReviewError("stale_approval", STALE_MESSAGE)


def on_record_edits(conn, before: dict, after: dict, client: str,
                    edit_id: Optional[str] = None) -> None:
    """After a tracked edit: a change to the words of a user-confirmed record drops that
    confirmation back to an AI guess and queues a new question. A pending question whose
    words no longer match is closed, and a new one is queued for the words as they are now.
    The caller commits. `edit_id` attaches the cleared confirmation to that edit, so undo
    puts the words back and leaves the confirmation for the user."""
    if not available(conn):
        return
    from . import history
    for (table, rid), new in after.items():
        old = before.get((table, rid))
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        rtype = history.TYPE_OF.get(table)
        if rtype not in CONTENT or not _content_changed(rtype, old, new):
            continue
        _refresh_confirmation(conn, rtype, int(rid), new, client, edit_id)


def _refresh_confirmation(conn, rtype: str, rid: int, new_row: dict, client: str,
                          edit_id: Optional[str] = None) -> None:
    new_hash = content_hash(rtype, new_row)
    meta = _meta(conn, rtype, rid)
    confirmed = bool(meta and meta.get("source") == "user_stated" and meta.get("source_set_by") == USER
                     and meta.get("confirmed_hash") != new_hash)
    pending = _pending_source(conn, rtype, rid)
    stale = [it for it in pending if it["hash"] != new_hash]
    if not confirmed and not stale:
        return
    if confirmed:
        cols = _source_cols(conn, "ai_inferred", client=client)
        if has_confirmed_hash(conn):
            cols["confirmed_hash"] = None
        _upsert(conn, rtype, rid, "source/words_changed", client, edit_id=edit_id, **cols)
    for it in stale:
        _void_source(conn, it["id"], WORDS_CHANGED)
    if any(it["hash"] == new_hash for it in pending):
        return
    pid = owner(conn, rtype, rid)
    if pid is None:
        return
    if confirmed:
        because = ("The words changed after you confirmed them, so that confirmation was cleared.")
    else:
        because = "The words changed after this was queued."
    request_user_stated(conn, pid, rtype, rid, client or "unknown", because=because)


def backfill_confirmed_hashes(db_path: Path) -> int:
    """Fill confirmed_hash for confirmations made before migration 025, from the words as
    they are now, so those confirmations stay valid until the words change. Returns how
    many rows were filled."""
    conn = links.connect(db_path)
    try:
        if not available(conn) or not stamped(conn) or not has_confirmed_hash(conn):
            return 0
        rows = conn.execute(
            "SELECT record_type, record_id FROM record_meta "
            "WHERE source = 'user_stated' AND source_set_by = ? AND confirmed_hash IS NULL",
            (USER,)).fetchall()
        n = 0
        for rtype, rid in rows:
            if rtype not in CONTENT:
                continue
            row = _row(conn, rtype, rid)
            if row is None:
                continue
            conn.execute(
                "UPDATE record_meta SET confirmed_hash = ? "
                "WHERE record_type = ? AND record_id = ? AND confirmed_hash IS NULL",
                (content_hash(rtype, row), rtype, rid))
            n += 1
        if n:
            conn.commit()
        return n
    finally:
        conn.close()


# the text cleanup's duplicate check compares, per log_work kind
_DUP_TEXT = {"learning": "SELECT insight FROM learnings WHERE id = ? AND project_id = ?",
             "todo": "SELECT title FROM todos WHERE id = ? AND project_id = ?",
             "decision": "SELECT COALESCE(key_decisions, user_prompt_summary) FROM conversations "
                         "WHERE id = ? AND project_id = ?",
             "problem": "SELECT p.title FROM problems p JOIN components c ON c.id = p.component_id "
                        "WHERE p.id = ? AND c.project_id = ?",
             "change": "SELECT ch.field_name FROM changes ch JOIN components c "
                       "ON c.id = ch.component_id WHERE ch.id = ? AND c.project_id = ?"}


def user_stated_dup(db_path: Path, pid: int, kind: str, summary: str,
                    app: Optional[str]) -> Optional[int]:
    """The id of an ai_inferred record of this kind in this project, tagged by another app (or
    by one not recorded), that `summary` duplicates (cleanup.is_duplicate), oldest first; else
    None."""
    if kind not in _DUP_TEXT:
        return None
    from .cleanup import is_duplicate
    conn = links.connect(db_path)
    try:
        if not available(conn):
            return None
        by = "source_set_by" if stamped(conn) else "NULL"
        for rid, set_by in conn.execute(
                f"SELECT record_id, {by} FROM record_meta WHERE record_type = ? "
                f"AND source = 'ai_inferred' ORDER BY record_id", (kind,)).fetchall():
            if app and set_by == app:
                continue
            row = conn.execute(_DUP_TEXT[kind], (rid, pid)).fetchone()
            if row and is_duplicate(summary, row[0] or ""):
                return rid
        return None
    finally:
        conn.close()


def at_log_time(db_path: Path, pid: int, kind: str, summary: str, source: Optional[str],
                app: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(source to save, note with an {id} slot or None): user_stated stays, unless it repeats
    another app's ai_inferred record."""
    if source != "user_stated":
        return source, None
    dup = user_stated_dup(db_path, pid, kind, summary, app)
    if dup is None:
        return source, None
    return "ai_inferred", (
        f"Saved as ai_inferred, not user_stated: it repeats {kind} #{dup}, which another app "
        f"tagged ai_inferred. Only the user can confirm they said it: manage_memory(action="
        f"\"set_source\", record_type=\"{kind}\", record_id={{id}}, source=\"user_stated\") "
        f"asks them.")


def agent_refusal(db_path: Path, item: dict, name: str) -> Optional[dict]:
    """An app tried to decide a `source` item: refused, approvals on or off (only the user
    decides). None for other items, and for an approve with approvals on, which then waits
    for the user like a delete (popup / phone / terminal)."""
    import json
    from . import approvals
    if item.get("kind") != "source" or (
            name == "review_approve" and approvals.is_on(db_path, item["project_id"])):
        return None
    p = json.loads(item.get("payload") or "{}")
    return {"error": "user_only", "message": "Only the user decides whether they said this; no "
            "app can pass their OK along. " + unchanged_message(
                p.get("record_type") or "record", p.get("was"), item["id"])}


def get(conn, rtype: str, ids) -> dict:
    """{id: {"source", "verified_at", "source_set_by", ...}} (set values only) for those
    that have a row."""
    ids = [int(i) for i in ids if i is not None]
    if not ids or not available(conn):
        return {}
    names = ("source", "verified_at") + (STAMPS if stamped(conn) else ())
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
                f"SELECT record_id, {', '.join(names)} FROM record_meta WHERE record_type = ? "
                f"AND record_id IN ({','.join('?' * len(chunk))})", (rtype, *chunk)):
            out[r[0]] = {k: v for k, v in zip(names, r[1:]) if v}
    return out


def label(meta: Optional[dict]) -> Optional[str]:
    """'user, via cursor' / 'user, confirmed by the user' / 'user' (not recorded) / 'AI' /
    'code'; None without a tag. Who set it is named for user tags, the ones that matter."""
    if not meta or not meta.get("source"):
        return None
    by = meta.get("source_set_by") if meta["source"] == "user_stated" else None
    if not by:
        return TAGS[meta["source"]]
    return "user, confirmed by the user" if by == USER else f"user, via {by}"


def tag(meta: Optional[dict]) -> str:
    """Compact suffix for brief lines: ' (user, via cursor)', ' (AI, checked 2026-10-16)',
    or ''."""
    if not meta:
        return ""
    bits = []
    if meta.get("source"):
        bits.append(label(meta))
    if meta.get("verified_at"):
        bits.append(f"checked {meta['verified_at'][:10]}")
    return f" ({', '.join(bits)})" if bits else ""


def annotate(db_path: Path, res, key: str, type_key: str = "type", id_key: str = "id"):
    """Add source, source_tag and who set it to the rows in res[key] that have a tag (in
    place); returns res."""
    rows = res.get(key) if isinstance(res, dict) else None
    if not rows:
        return res
    want: dict = {}
    for r in rows:
        if isinstance(r, dict) and r.get(type_key) in TABLES and r.get(id_key) is not None:
            want.setdefault(r[type_key], []).append(r[id_key])
    if not want:
        return res
    conn = links.connect(db_path)
    try:
        metas = {t: get(conn, t, ids) for t, ids in want.items()}
    finally:
        conn.close()
    for r in rows:
        if not isinstance(r, dict):
            continue
        m = metas.get(r.get(type_key), {}).get(r.get(id_key))
        if m and m.get("source"):
            r.update({"source": m["source"], "source_tag": label(m),
                      **{k: m[k] for k in STAMPS if m.get(k)}})
    return res


def is_fact(text: str) -> bool:
    return bool(_FACT.search(text or "") or _ENV_VAR.search(text or ""))


def _fact_rows(conn, pid: int) -> list[dict]:
    """(type, id, text, last touched) for every fact-like record of a project."""
    out = []
    q = lambda sql: conn.execute(sql, (pid,)).fetchall()
    for r in q("""SELECT id, insight || ' ' || COALESCE(context, '') t, created_at d
                  FROM learnings WHERE project_id = ?"""):
        out.append(("learning", r[0], r[1], r[2]))
    for r in q("""SELECT ch.id, ch.field_name || ' ' || COALESCE(ch.new_value, '') || ' ' ||
                  COALESCE(ch.reason, '') t, ch.created_at d FROM changes ch
                  JOIN components c ON c.id = ch.component_id WHERE c.project_id = ?"""):
        out.append(("change", r[0], r[1], r[2]))
    replaced = links.superseded_decisions(conn) if links.available(conn) else {}
    for r in q("""SELECT id, COALESCE(key_decisions, '') || ' ' || COALESCE(user_prompt_summary, '')
                  t, created_at d FROM conversations WHERE project_id = ? AND key_decisions IS NOT NULL"""):
        if r[0] not in replaced:
            out.append(("decision", r[0], r[1], r[2]))
    for r in q("""SELECT id, name || ' ' || COALESCE(description, '') t,
                  COALESCE(updated_at, created_at) d, category FROM project_variables
                  WHERE project_id = ?"""):
        # the value itself is never read here (it may be secret)
        out.append(("variable", r[0], r[1] + (" config" if r[3] in _FACT_VAR_CATEGORIES else ""), r[2]))
    for r in q("""SELECT id, name || ' ' || description || ' ' || COALESCE(steps, '') t,
                  COALESCE(updated_at, created_at) d FROM project_methods WHERE project_id = ?"""):
        out.append(("method", r[0], r[1], r[2]))
    return [{"type": t, "id": i, "text": x or "", "touched": d} for t, i, x, d in out if is_fact(x)]


def stale(db_path: Path, pid: int, days: Optional[int] = None, limit: int = 20) -> dict:
    days = days or stale_days()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = links.connect(db_path)
    try:
        facts = _fact_rows(conn, pid)
        metas = {}
        for t in FACT_TYPES:
            metas[t] = get(conn, t, [f["id"] for f in facts if f["type"] == t])
        found = []
        for f in facts:
            m = metas[f["type"]].get(f["id"], {})
            last = m.get("verified_at") or f["touched"] or ""
            if str(last)[:19] < cutoff:
                found.append({"record_type": f["type"], "record_id": f["id"],
                              "text": links._one_line(f["text"], 100),
                              "last_checked": m.get("verified_at"),
                              "created_or_updated": (f["touched"] or "")[:10],
                              **({"source": m["source"]} if m.get("source") else {})})
        found.sort(key=lambda x: (x["last_checked"] or x["created_or_updated"] or ""))
        return {"project": links.project_name(conn, pid), "stale_after_days": days,
                "stale": found[:max(1, int(limit or 20))], "total": len(found),
                "note": "Facts (paths, ports, hosts, URLs, config) not checked recently. Check "
                        "each one; if it still holds, manage_memory(action=\"mark_checked\"). If it changed, log the "
                        "change. Nothing was changed automatically."}
    finally:
        conn.close()


def brief_line(db_path: Path, pid: int) -> Optional[str]:
    """One short line for the brief, or None when nothing is stale."""
    res = stale(db_path, pid, limit=2)
    if not res["total"]:
        return None
    eg = "; ".join(f"{s['record_type']} #{s['record_id']} {links._one_line(s['text'], 50)}"
                   for s in res["stale"])
    return (f"Needs re-check: {res['total']} fact{'s' if res['total'] != 1 else ''} (paths, ports, "
            f"config...) not checked in {res['stale_after_days']}+ days, e.g. {eg}. "
            f"manage_memory(action=\"stale\") lists them.")
