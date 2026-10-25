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
queued review item is Keep only. A record that is gone, or that a later edit changed (an edit
history row after the session ended, other than these decisions), shows its state and no
buttons.

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
# summary still waits; Keep all / Don't keep (review._claim) wait for it, in any process. A
# marker older than this was left by a process that stopped half-way, and is ignored.
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


def _created_here(conn, table: str, rid: int, mine: set) -> Optional[bool]:
    """Did one of this session's own writes create the row? None when that can't be known
    (no ledger rows for the session)."""
    if not mine:
        return None
    for (edit,) in conn.execute("SELECT edit_id FROM record_history WHERE table_name = ? AND "
                                "record_id = ? AND old_row IS NULL AND new_row IS NOT NULL",
                                (table, rid)):
        if edit in mine:
            return True
    return False


def _others_below(conn, table: str, rid: int, mine: set) -> int:
    """How many rows a Remove would also take that another app's write created (a fix attempt
    added to this session's problem later, say)."""
    from . import deletes
    try:   # the planner reads rows by name: a connection of its own on the same file
        pconn = deletes.connect(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
        try:
            gone, _changed = deletes.plan(pconn, table, rid)
        finally:
            pconn.close()
    except Exception:  # noqa: BLE001 - the delete itself would refuse; nothing extra to say
        return 0
    n = 0
    for (t, key) in gone:
        if (t, key) == (table, rid) or not isinstance(key, int):
            continue
        made = [r[0] for r in conn.execute(
            "SELECT edit_id FROM record_history WHERE table_name = ? AND record_id = ? AND "
            "old_row IS NULL AND new_row IS NOT NULL", (t, key))]
        if made and not set(made) & mine:
            n += 1
    return n


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
    for e in entries:
        k = key(e["kind"], e["id"])
        d = decided.get(k) if isinstance(decided.get(k), dict) else {}
        table = history.TABLES.get(e["kind"])
        created = e["kind"] in REMOVABLE and (e.get("verb") or "saved").split(", ")[0] == "saved"
        if created and table and has_hist and not e["gone"]:
            here = _created_here(conn, table, e["id"], mine)
            if here is False:   # it was there already (a link added again), or the session deleted it
                created = False
                if e.get("verb") == "saved":
                    e["verb"] = "already there"
            elif _others_below(conn, table, e["id"], mine):
                created = False   # Remove would take another app's later work with it
                e["others_below"] = True
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
def _write(db_path, item_id: int, k: str, value: Optional[dict], held: bool = False) -> None:
    """Set (or clear, value None) one record's decision in the item's payload, while it waits.
    `held`: this record's "removing" marker is ours, so the summary was kept waiting for it
    (review._claim); write the outcome whatever the status says by now."""
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
            _write(db_path, item["id"], k, {"d": "removing", **base, "edits": edits})
            try:
                copy = safety.copy_before(Path(db_path))   # no copy, no removal
                done = deletes._carry_out(Path(db_path), item["project_id"], rtype, rid, client)
            except (deletes.DeleteError, safety.SafetyError) as err:
                _write(db_path, item["id"], k, prior or None, held=True)
                raise RecordError(err.payload.get("error", "not_removed"),
                                  err.payload.get("message", str(err))) from err
            except BaseException:
                _write(db_path, item["id"], k, prior or None, held=True)
                raise
            _write(db_path, item["id"], k, {"d": "removed", **base, "edit": done["edit_id"],
                                            "edits": edits + [done["edit_id"]]}, held=True)
            gone = done["deleted"].get("with") or {}
            extra = (" with " + ", ".join(f"{n} {w}" for w, n in gone.items())) if gone else ""
            out.update(now="removed", safety_copy=copy.name, undo=done["undo"],
                       message=f'Removed {e["kind_label"]} #{rid}{extra} (safety copy taken). '
                               f'Bring back, or {done["undo"]}, restores it.')
        else:
            pname = item.get("project") or ""
            _write(db_path, item["id"], k, {**prior, "d": "removing", "at": base["at"]})
            try:
                res = history.undo(Path(db_path), item["project_id"], rtype, rid, client)
            except BaseException as err:
                _write(db_path, item["id"], k, prior or None, held=True)
                if isinstance(err, history.HistoryError):
                    raise RecordError(err.payload["error"], err.payload["message"]) from err
                raise
            _write(db_path, item["id"], k, {"d": "", **base,
                                            "edits": edits + [res.get("undo_edit_id")]}, held=True)
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
