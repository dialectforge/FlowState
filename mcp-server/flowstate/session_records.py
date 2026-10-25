"""Keep / Remove for each record of a session summary (2.4.4).

A session summary (autosession.py) lists what the session saved (session_contents.py). Those
records were saved when the AI wrote them; the summary's own Keep / Don't keep only decides
the summary line. Here the user decides each record:

  - Keep marks it reviewed (nothing else changes);
  - Remove takes it back: a verified safety copy first, then the user's delete path
    (deletes._carry_out: the record and what belongs to it, as one edit in the edit history),
    so manage_history(action="undo") on it, or Bring back here, puts it all back;
  - Bring back (a removed record, while the summary waits) is that undo.

Only a record this session created can be removed here (its first write in the session was a
save, and its type is one deletes.py removes); one it only updated, a component, project or
queued review item is Keep only. So is one whose Remove would also take rows this session
didn't provably make (another app's attempt or link), one FlowState can't check, and every
record once the session's write log is gone (pruned after 90 days). A record that is gone, or
that a later edit changed (an edit history row after the session ended, other than these
decisions), shows its state and no buttons.

Keep all (the summary's Keep, review.approve) keeps the summary line and marks every record
still undecided kept; removed ones stay removed. Don't keep (review.reject) decides only the
summary line. Either way the session's saved note names the records the user removed.

Who decides: only the user, like review decisions. Every caller is a user surface (the local
and tunnel review pages, the phone list and approve page, the terminal, the review card's
app-only button tool, which keeps the card's own rules: no chat text, MCP Apps hosts only,
phone only through the relay unless chat approvals are on). There is no model-visible tool for
it. Each decision is logged in approval_log ("record kept: change #12", with its path) and in
the item's payload ("record_decisions": key -> decision, time, path, edit ids; ids only, never
the record's text).
"""

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import dbconn

# record types Remove may take back (deletes.py removes them; history.py undoes the delete)
REMOVABLE = ("change", "decision", "problem", "learning", "todo", "attempt", "rule", "handoff",
             "link")
EXPLAIN = ("These records were already saved when the AI wrote them. Keep marks one reviewed; "
           "Remove takes it back (after a safety copy; Bring back, or manage_history undo, "
           "restores it). Keep all keeps the summary and every record you haven't removed.")
STATE_WORDS = {"kept": "kept", "removed": "removed by you", "gone": "no longer in FlowState",
               "changed": "changed since this session", "removing": "being removed",
               "": "not decided yet"}
# A Remove (or Bring back) writes this marker first, in the same transaction that checks the
# summary still waits; Keep all / Don't keep (review._claim) wait for it, in any process. While
# the Remove runs (a big database's safety copy can take minutes) it refreshes the marker every
# MARK_SECONDS / 4; a marker older than MARK_SECONDS was left by a process that stopped
# half-way, and is ignored. The "removed" decision itself is saved in the delete's own
# transaction, and only while the marker is still this Remove's (audit 53, N-F19).
MARK_SECONDS = 120
ACTION_WORDS = {"keep": "Keep", "remove": "Remove", "restore": "Bring back"}
_lock = threading.Lock()   # one decision at a time per process (the payload is read, then written)


class RecordError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def key(rtype: str, rid) -> str:
    return f"{rtype}:{int(rid)}"


def parse(record) -> Optional[tuple]:
    """'change:12', 'change#12', 'change #12' or 'change 12' -> ('change', 12); else None."""
    text = " ".join(str(record or "").replace(":", " ").replace("#", " ").split())
    kind, _, rid = text.partition(" ")
    return (kind.lower(), int(rid)) if kind and rid.isdigit() else None


def _payload(item: dict) -> dict:
    try:
        p = json.loads(item.get("payload") or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def decisions(item: dict) -> dict:
    d = _payload(item).get("record_decisions")
    return d if isinstance(d, dict) else {}


def _connect(db_path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _has_history(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='record_history'").fetchone() is not None


# ---------------------------------------------------------------- state (read only)
def _ended(conn, item: dict) -> str:
    sid = item.get("session_id")
    row = conn.execute("SELECT ended_at FROM sessions WHERE id = ?", (sid,)).fetchone() \
        if sid is not None else None
    return str((row[0] if row else None) or item.get("created_at") or "")[:19]


def own_edits(conn, item: dict) -> set:
    """The edit ids of this session's own writes (the run ledger's change ids, which the edit
    history uses as edit ids); empty when the ledger has none (cleared, or before 2.4.0)."""
    sid = item.get("session_id")
    if sid is None or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                   "name='run_writes'").fetchone() is None:
        return set()
    return {r[0] for r in conn.execute("SELECT change_id FROM run_writes WHERE session_id = ? "
                                       "AND status = 'saved'", (sid,))}


def _ledger_refs(conn, item: dict) -> dict:
    """'type:id' -> the started_at of this session's first saved write that names it (the
    run ledger); empty when the ledger has none."""
    sid = item.get("session_id")
    out: dict = {}
    if sid is None or conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND "
                                   "name='run_writes'").fetchone() is None:
        return out
    for at, recs in conn.execute("SELECT started_at, records FROM run_writes WHERE "
                                 "session_id = ? AND status = 'saved' ORDER BY id", (sid,)):
        try:
            refs = json.loads(recs or "[]")
        except ValueError:
            continue
        for ref in refs if isinstance(refs, list) else []:
            out.setdefault(str(ref), str(at or ""))
    return out


def _session_apps(conn, item: dict) -> set:
    """The names this session's app was recorded under (its run, its client)."""
    sid = item.get("session_id")
    names = {item.get("source")}
    if sid is not None:
        for sql in ("SELECT client FROM auto_sessions WHERE session_id = ?",
                    "SELECT app FROM session_runs WHERE session_id = ?"):
            try:
                names |= {r[0] for r in conn.execute(sql, (sid,))}
            except sqlite3.Error:
                pass
    return {n for n in names if n}


def _created_here(conn, table: str, rid: int, mine: set, item: Optional[dict] = None,
                  ledger: Optional[dict] = None) -> Optional[bool]:
    """Did one of this session's own writes create the row? None when that can't be known
    (no ledger rows for the session). A row whose creation the edit history doesn't record
    (a handoff note, a link added on its own) is the session's when it was made by this
    session's app no earlier than the session's write that names it (a link that was
    already there, added again, is older, or another app's)."""
    if not mine:
        return None
    made = [r[0] for r in conn.execute("SELECT edit_id FROM record_history WHERE table_name = ? "
                                       "AND record_id = ? AND old_row IS NULL AND new_row IS NOT "
                                       "NULL", (table, rid))]
    if made:
        return bool(set(made) & mine)
    from . import history
    rtype = history.TYPE_OF.get(table)
    at = (ledger or {}).get(f"{rtype}:{int(rid)}") if rtype else None
    if not at or item is None:
        return False
    cur = conn.execute(f'SELECT * FROM "{table}" WHERE id = ?', (rid,))
    got = cur.fetchone()
    row = dict(zip([c[0] for c in cur.description], got)) if got else None
    if row is None or not row.get("created_at"):
        return False
    by = {row.get("app"), row.get("created_by")} - {None, ""}
    return str(row["created_at"])[:19] >= at[:19] and bool(by & _session_apps(conn, item))


# rows a Remove may take with the record that are no one's work (a claim ends by itself)
_BOOKKEEPING = ("todo_claims",)


def _others_below(conn, table: str, rid: int, mine: set,
                  ledger: Optional[dict] = None) -> tuple:
    """(how many rows a Remove would also take that this session didn't make, why Remove
    can't be offered or ""). Another app's fix attempt added to this session's problem
    later, say, or a link to it. A row counts as this session's only when the edit history
    says one of its writes created it, or (no creation recorded: erased, older code, a
    link added on its own) the session's write ledger names it. If what Remove would take
    can't be worked out, Remove isn't offered (fail closed)."""
    from . import deletes, history
    try:   # the planner reads rows by name: a connection of its own on the same file
        pconn = deletes.connect(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        try:
            gone, _changed = deletes.plan(pconn, table, rid)
        finally:
            pconn.close()
    except deletes.DeleteError as err:
        return 0, err.payload.get("message", str(err))
    except Exception:  # noqa: BLE001 - can't tell what it would take: don't offer it
        return 0, "FlowState couldn't work out what Remove would also take"
    n = 0
    for (t, k) in gone:
        if (t, k) == (table, rid) or t in _BOOKKEEPING or t in deletes.DERIVED:
            continue
        made = [r[0] for r in conn.execute(
            "SELECT edit_id FROM record_history WHERE table_name = ? AND record_id = ? AND "
            "old_row IS NULL AND new_row IS NOT NULL", (t, k))]
        if made:
            if not set(made) & mine:
                n += 1
            continue
        rtype = history.TYPE_OF.get(t)
        if not (rtype and f"{rtype}:{k}" in (ledger or {})):
            n += 1
    return n, ""


def _marker_fresh(d: dict) -> bool:
    try:
        at = datetime.strptime(str(d.get("at"))[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - at).total_seconds() < MARK_SECONDS


def busy(item_or_payload) -> bool:
    """Is a Remove / Bring back on one of this summary's records under way right now?"""
    p = item_or_payload if isinstance(item_or_payload, dict) and "record_decisions" in \
        item_or_payload else _payload(item_or_payload if isinstance(item_or_payload, dict) else {})
    rd = p.get("record_decisions") if isinstance(p.get("record_decisions"), dict) else {}
    return any(isinstance(d, dict) and d.get("d") == "removing" and _marker_fresh(d)
               for d in rd.values())


def _changed_since(conn, table: str, rid: int, ended: str, own: set) -> bool:
    if not ended:
        return False
    for r in conn.execute("SELECT edit_id, tool FROM record_history WHERE table_name = ? AND "
                          "record_id = ? AND created_at > ?", (table, rid, ended)):
        tool = r[1] or ""
        if r[0] in own or tool.startswith(("create/", "approve/")):
            continue
        return True
    return False


def _removed_row(conn, table: str, rid: int, edit_id: str) -> tuple:
    """(the removed row as it was, whether Bring back can still undo that removal)."""
    row = conn.execute("SELECT old_row FROM record_history WHERE edit_id = ? AND table_name = ? "
                       "AND record_id = ?", (edit_id, table, rid)).fetchone()
    last = conn.execute("SELECT edit_id FROM record_history WHERE table_name = ? AND record_id = ? "
                        "AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (table, rid)).fetchone()
    try:
        old = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        old = {}
    return old, bool(last and last[0] == edit_id)


def annotate(conn, item: dict, entries: list) -> None:
    """Add to each session_contents entry: `state` (STATE_WORDS key), `state_label`, `actions`
    (what the user may do now: keep / remove / restore; none once the summary is decided) and
    `key`. A removed record's title comes from the edit history's copy of it."""
    from . import deletes, history
    decided = decisions(item)
    pending = item.get("status", "pending") == "pending"
    has_hist = _has_history(conn)
    ended = _ended(conn, item) if has_hist else ""
    mine = own_edits(conn, item) if has_hist else set()
    # the session's own writes (its last one may land after `ended`, its start) and the
    # user's own decisions here don't count as "changed since"
    own = mine | {e for d in decided.values() if isinstance(d, dict) for e in d.get("edits") or []}
    ledger = _ledger_refs(conn, item) if has_hist else {}
    for e in entries:
        k = key(e["kind"], e["id"])
        d = decided.get(k) if isinstance(decided.get(k), dict) else {}
        table = history.TABLES.get(e["kind"])
        created = e["kind"] in REMOVABLE and (e.get("verb") or "saved").split(", ")[0] == "saved"
        if created and table and has_hist and not e["gone"]:
            here = _created_here(conn, table, e["id"], mine, item, ledger)
            if here is None:   # the session's write ledger is gone (kept 90 days): can't tell
                created = False
                e["keep_only"] = ("FlowState no longer has this session's write log (it keeps "
                                  "it 90 days), so it can't tell whether the record is only "
                                  "this session's work")
            elif here is False:   # it was there already (a link added again), or the session deleted it
                created = False
                if e.get("verb") == "saved":
                    e["verb"] = "already there"
            else:
                others, why = _others_below(conn, table, e["id"], mine, ledger)
                if others:
                    created = False   # Remove would take another app's later work with it
                    e["others_below"] = True
                elif why:
                    created = False
                    e["keep_only"] = why
        state, actions = d.get("d") or "", []
        if state == "removing":   # a Remove under way (or one that stopped half-way)
            state = "removing" if _marker_fresh(d) else ("gone" if e["gone"] else "")
        if state == "removing":
            pass
        elif state == "removed" and e["gone"] and table and has_hist and d.get("edit"):
            old, back = _removed_row(conn, table, e["id"], d["edit"])
            name = deletes._row_name(table, old) if old else ""
            if name:
                e["title"] = name
            actions = ["restore"] if back else []
        elif state == "removed":   # brought back some other way (manage_history undo)
            state = "" if not e["gone"] else "gone"
        if state not in ("removed", "removing"):
            if e["gone"]:
                state = "gone"
            elif table and has_hist and _changed_since(conn, table, e["id"], ended, own):
                state = "changed"
            elif state == "kept":
                actions = ["remove"] if created else []
            else:
                actions = ["keep"] + (["remove"] if created else [])
        e.update(key=k, state=state, state_label=STATE_WORDS.get(state, state),
                 actions=actions if pending else [], removable=created)


def counts(entries: list) -> dict:
    out: dict = {}
    for e in entries:
        s = e.get("state", "")
        out[s] = out.get(s, 0) + 1
    return out


def count_line(entries: list) -> str:
    """'records: 2 kept, 1 removed by you, 3 not decided yet' (empty while none is decided)."""
    c = counts(entries)
    if set(c) <= {""}:   # nothing decided yet: the plain heading
        return ""
    order = ("kept", "removed", "removing", "", "changed", "gone")
    bits = [f"{c[s]} {STATE_WORDS[s]}" for s in order if c.get(s)]
    return ("records: " + ", ".join(bits)) if bits else ""


def removed_names(db_path, item: dict) -> list:
    """['change #12 "Fix login"', ...]: the records the user removed (still removed)."""
    from . import session_contents
    c = session_contents.for_item(db_path, item)
    return [f'{e["kind_label"]} #{e["id"]}' + (f' "{e["title"]}"' if e["title"] else "")
            for e in (c or {}).get("entries", []) if e.get("state") == "removed"]


def note_for(db_path, item: dict) -> str:
    """The line added to the session's saved note: which records the user removed."""
    names = removed_names(db_path, item)
    if not names:
        return ""
    return "Removed by the user: " + "; ".join(names[:20]) + \
        (f"; and {len(names) - 20} more" if len(names) > 20 else "") + "."


# ---------------------------------------------------------------- deciding (the user's side)
def _write(db_path, item_id: int, k: str, value: Optional[dict], held: bool = False,
           tok: Optional[str] = None) -> None:
    """Set (or clear, value None) one record's decision in the item's payload, while it waits.
    `held`: this record's "removing" marker is ours, so the summary was kept waiting for it
    (review._claim); write the outcome whatever the status says by now. `tok`: only if the
    record's marker is still the one with that token (else nothing is written: Keep all
    decided it after the marker ran out)."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload, status FROM review_items WHERE id = ?",
                           (item_id,)).fetchone()
        if row is None or (row["status"] != "pending" and not held):
            conn.rollback()
            raise RecordError("already_decided", f"Review item #{item_id} is already decided.")
        if not held and busy(_payload({"payload": row["payload"]})):
            conn.rollback()
            raise RecordError("busy", "Another record of this summary is being removed right "
                                      "now; try again in a moment.")
        p = _payload({"payload": row["payload"]})
        rd = p.get("record_decisions") if isinstance(p.get("record_decisions"), dict) else {}
        if tok is not None and not _ours(rd.get(k), tok):
            conn.rollback()
            return
        if value is None:
            rd.pop(k, None)
        else:
            rd[k] = value
        p["record_decisions"] = rd
        conn.execute("UPDATE review_items SET payload = ? WHERE id = ?", (json.dumps(p), item_id))
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _ours(d, tok: str) -> bool:
    return isinstance(d, dict) and d.get("d") == "removing" and d.get("tok") == tok


class _Heartbeat:
    """Keeps a "removing" marker fresh while its Remove / Bring back runs (N-F19)."""

    def __init__(self, db_path, item_id: int, k: str, tok: str):
        self.args = (db_path, item_id, k, tok)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name=f"flowstate-removing-{item_id}")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=10)

    def _run(self):
        while not self.stop.wait(max(0.2, MARK_SECONDS / 4)):
            try:
                _refresh(*self.args)
            except Exception:  # noqa: BLE001 - busy now (the delete holds the database); next time
                pass


def _refresh(db_path, item_id: int, k: str, tok: str) -> None:
    conn = dbconn.connect(str(db_path), timeout=1.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM review_items WHERE id = ?", (item_id,)).fetchone()
        p = _payload({"payload": row["payload"]}) if row else {}
        rd = p.get("record_decisions") if isinstance(p.get("record_decisions"), dict) else {}
        if _ours(rd.get(k), tok):
            rd[k]["at"] = _now()
            conn.execute("UPDATE review_items SET payload = ? WHERE id = ?",
                         (json.dumps(p), item_id))
        conn.commit()
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _save_removed(item_id: int, k: str, tok: str, value: dict):
    """The "removed" decision, written inside the delete's own transaction (same_save): the
    record and its decision are saved together or not at all. Refused (so nothing is
    deleted) unless the summary still waits and the marker is still this Remove's."""
    def save(conn, edit_id: str) -> None:
        row = conn.execute("SELECT payload, status FROM review_items WHERE id = ?",
                           (item_id,)).fetchone()
        p = _payload({"payload": row[0]}) if row else {}
        rd = p.get("record_decisions") if isinstance(p.get("record_decisions"), dict) else {}
        if row is None or row[1] != "pending" or not _ours(rd.get(k), tok):
            raise RecordError("decided_meanwhile", f"Review item #{item_id} was decided while "
                                                   f"this Remove waited; nothing was removed.")
        rd[k] = {**value, "edit": edit_id, "edits": list(value.get("edits") or []) + [edit_id]}
        p["record_decisions"] = rd
        conn.execute("UPDATE review_items SET payload = ? WHERE id = ?", (json.dumps(p), item_id))
    return save


def _entry(db_path, item: dict, record) -> dict:
    from . import session_contents
    got = parse(record) if not isinstance(record, tuple) else record
    if not got:
        raise RecordError("bad_record", "Name the record as type:id, for example change:12.")
    c = session_contents.for_item(db_path, item)
    for e in (c or {}).get("entries", []):
        if (e["kind"], e["id"]) == got:
            return e
    raise RecordError("not_in_summary", f"{got[0]} #{got[1]} is not one of the records review "
                                        f"item #{item['id']} lists.")


def _why_not(e: dict, action: str) -> str:
    name = f'{e["kind_label"]} #{e["id"]}'
    if e["state"] == "gone":
        return f"{name} is no longer in FlowState; there is nothing to decide."
    if e["state"] == "changed":
        return (f"{name} was changed after this session, so it isn't decided here. "
                f"Find it in Who did what (manage_history) instead.")
    if e["state"] == "removed":
        return f"{name} is already removed." + (" Bring back restores it." if "restore" in
                                                e["actions"] else "")
    if e["state"] == "removing":
        return f"{name} is being removed right now."
    if action == "remove" and e.get("keep_only"):
        return f'{e["keep_only"]}; {name} can only be kept here.'
    if action == "remove" and e.get("others_below"):
        return (f"Another app added to {name} after this session, and Remove would take that "
                f"with it, so it can only be kept here.")
    if action == "remove" and not e.get("removable"):
        return (f"{name} wasn't created by this session (or is a kind Remove doesn't take back), "
                f"so it can only be kept here.")
    if action == "keep" and e["state"] == "kept":
        return f"{name} is already kept."
    if action == "restore":
        return f"{name} isn't removed."
    return f"That can't be done to {name} now."


def decide(db_path, item_id: int, record, action: str, client: str,
           path: Optional[str] = None) -> dict:
    """The user's Keep / Remove / Bring back on one record of a waiting session summary.
    `client` labels who did it (the surface, e.g. "review-page", "phone");
    `path` the approval_log path (default approvals.path_of(client)). Raises RecordError
    (also for the delete's and undo's own refusals, with their message)."""
    from . import approvals, deletes, history, review, safety
    if action not in ACTION_WORDS:
        raise RecordError("bad_action", "action is keep, remove or restore.")
    with _lock:
        item = review.get_item(Path(db_path), int(item_id))
        if not item:
            raise RecordError("record_not_found", f"No review item with id {item_id}.")
        if item["kind"] != "session_summary":
            raise RecordError("not_a_session_summary", f"Review item #{item_id} is not a session "
                                                       f"summary; it has no records to decide.")
        if item["status"] != "pending":
            raise RecordError("already_decided", f"Review item #{item_id} is already "
                                                 f"{item['status']}.")
        e = _entry(db_path, item, record)
        if action not in e["actions"]:
            raise RecordError("not_allowed", _why_not(e, action))
        k, rtype, rid = e["key"], e["kind"], e["id"]
        prior = decisions(item).get(k) if isinstance(decisions(item).get(k), dict) else {}
        edits = list(prior.get("edits") or [])
        base = {"at": _now(), "via": path or approvals.path_of(client), "by": (client or "")[:80]}
        out = {"item_id": item["id"], "record": k}
        if action == "keep":
            _write(db_path, item["id"], k, {"d": "kept", **base, "edits": edits})
            out.update(now="kept", message=f'Kept {e["kind_label"]} #{rid}.')
        elif action == "remove":
            # the marker first, checked and written in one transaction: from here on Keep all
            # and Don't keep wait (review._claim), in this process or any other
            tok = uuid.uuid4().hex
            _write(db_path, item["id"], k, {"d": "removing", **base, "edits": edits, "tok": tok})
            try:
                with _Heartbeat(db_path, item["id"], k, tok):
                    copy = safety.copy_before(Path(db_path))   # no copy, no removal
                    done = deletes._carry_out(
                        Path(db_path), item["project_id"], rtype, rid, client,
                        same_save=_save_removed(item["id"], k, tok,
                                                {"d": "removed", **base, "edits": edits}))
            except (deletes.DeleteError, safety.SafetyError) as err:
                _write(db_path, item["id"], k, prior or None, held=True, tok=tok)
                raise RecordError(err.payload.get("error", "not_removed"),
                                  err.payload.get("message", str(err))) from err
            except BaseException:
                _write(db_path, item["id"], k, prior or None, held=True, tok=tok)
                raise
            gone = done["deleted"].get("with") or {}
            extra = (" with " + ", ".join(f"{n} {w}" for w, n in gone.items())) if gone else ""
            out.update(now="removed", safety_copy=copy.name, undo=done["undo"],
                       message=f'Removed {e["kind_label"]} #{rid}{extra} (safety copy taken). '
                               f'Bring back, or {done["undo"]}, restores it.')
        else:
            pname = item.get("project") or ""
            tok = uuid.uuid4().hex
            _write(db_path, item["id"], k, {**prior, "d": "removing", "at": base["at"], "tok": tok})
            try:
                with _Heartbeat(db_path, item["id"], k, tok):
                    res = history.undo(Path(db_path), item["project_id"], rtype, rid, client)
            except BaseException as err:
                _write(db_path, item["id"], k, prior or None, held=True, tok=tok)
                if isinstance(err, history.HistoryError):
                    raise RecordError(err.payload["error"], err.payload["message"]) from err
                raise
            _write(db_path, item["id"], k, {"d": "", **base,
                                            "edits": edits + [res.get("undo_edit_id")]},
                   held=True, tok=tok)
            out.update(now="", message=f'Brought back {e["kind_label"]} #{rid}' +
                       (f" in {pname}" if pname else "") + "; it is undecided again.")
        word = {"keep": "kept", "remove": "removed", "restore": "brought back"}[action]
        try:
            approvals.log_decision(Path(db_path), item, base["via"], client,
                                   f"record {word}: {rtype} #{rid}")
        except sqlite3.Error:
            pass   # the decision stands; the log line is extra
        return out


def keep_rest(db_path, item: dict, client: str, path: Optional[str] = None) -> int:
    """Keep all (review.approve of a session summary, after it is claimed): every record still
    undecided that can be kept is marked kept. Returns how many. Written straight into the
    payload (the item is no longer pending). `path` is how it was decided (card, agent, ...),
    else worked out from `client` (review page, phone, terminal)."""
    from . import approvals, session_contents
    c = session_contents.for_item(db_path, {**item, "status": "pending"})
    todo = [e for e in (c or {}).get("entries", []) if "keep" in e.get("actions", [])]
    if not todo:
        return 0
    base = {"d": "kept", "at": _now(), "via": path or approvals.path_of(client),
            "by": (client or "")[:80], "keep_all": True}
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT payload FROM review_items WHERE id = ?", (item["id"],)).fetchone()
        p = _payload({"payload": row["payload"] if row else "{}"})
        rd = p.get("record_decisions") if isinstance(p.get("record_decisions"), dict) else {}
        for e in todo:
            prior = rd.get(e["key"]) if isinstance(rd.get(e["key"]), dict) else {}
            rd[e["key"]] = {**base, "edits": list(prior.get("edits") or [])}
        p["record_decisions"] = rd
        conn.execute("UPDATE review_items SET payload = ? WHERE id = ?", (json.dumps(p), item["id"]))
        conn.commit()
    finally:
        conn.close()
    return len(todo)
