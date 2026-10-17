"""Handoff notes (v2 Phase D, E3; migration 010).

An AI that stops mid-task leaves "where I was" and "next step". The next AI sees open notes
first in start_here (right after the hard rules) until one of them marks the note picked
up. Notes are never deleted.
"""

from pathlib import Path

from . import identity, links

MAX_WHERE = 1000
MAX_NEXT = 500
BRIEF_FULL = 2          # newest open notes shown in full; the rest are counted


class HandoffError(links.LinkError):
    pass


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='handoff_notes'").fetchone() is not None


def _need(conn) -> None:
    if not available(conn):
        raise HandoffError("handoffs_unavailable", "Handoff notes aren't set up in this database "
                           "yet (migration 010 has not run). Restart FlowState.")


def leave(db_path: Path, pid: int, where_i_was: str, next_step: str, client: str) -> dict:
    where = (where_i_was or "").strip()
    nxt = (next_step or "").strip()
    if not where:
        raise HandoffError("where_required", "Say where you stopped (where_i_was), and the next "
                           "step (next_step).")
    if len(where) > MAX_WHERE or len(nxt) > MAX_NEXT:
        raise HandoffError("too_long", f"Keep it short: where_i_was at most {MAX_WHERE} "
                           f"characters, next_step at most {MAX_NEXT}.")
    conn = links.connect(db_path)
    try:
        _need(conn)
        cur = identity.insert(conn, "handoff_notes", {
            "project_id": pid, "where_i_was": where, "next_step": nxt or None,
            "created_at": links.now(), "created_by": client}, client)
        conn.commit()
        return {"handoff_id": cur.lastrowid, "project_id": pid,
                "note": "The next AI sees this first in start_here until it is picked up."}
    finally:
        conn.close()


def pickup(db_path: Path, pid: int, note_id, client: str) -> dict:
    if note_id is None:
        raise HandoffError("handoff_id_required", "Give handoff_id (shown in the brief).")
    conn = links.connect(db_path)
    try:
        _need(conn)
        row = conn.execute("SELECT project_id, picked_up_at FROM handoff_notes WHERE id = ?",
                           (int(note_id),)).fetchone()
        if row is None:
            raise HandoffError("record_not_found", f"No handoff note with id {note_id}.")
        if row["project_id"] != pid:
            raise HandoffError("project_mismatch", f"Handoff #{note_id} belongs to project "
                               f"'{links.project_name(conn, row['project_id'])}'.")
        if row["picked_up_at"]:
            return {"handoff_id": int(note_id), "note": f"Already picked up ({row['picked_up_at']})."}
        sets, who = identity.set_sql(conn, "handoff_notes", client, prefix="picked_up_")
        conn.execute(f"UPDATE handoff_notes SET picked_up_at = ?, picked_up_by = ?{sets} "
                     f"WHERE id = ?", (links.now(), client, *who, int(note_id)))
        conn.commit()
        return {"handoff_id": int(note_id), "picked_up": True,
                "note": "It no longer shows in the brief."}
    finally:
        conn.close()


def open_notes(conn, pid: int) -> list[dict]:
    if not available(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id, where_i_was, next_step, created_at, created_by FROM handoff_notes "
        "WHERE project_id = ? AND picked_up_at IS NULL ORDER BY created_at DESC, id DESC", (pid,))]


def list_notes(db_path: Path, pid: int, include_picked_up: bool = False, limit: int = 20) -> dict:
    conn = links.connect(db_path)
    try:
        _need(conn)
        sql = ("SELECT * FROM handoff_notes WHERE project_id = ?"
               + ("" if include_picked_up else " AND picked_up_at IS NULL")
               + " ORDER BY created_at DESC, id DESC LIMIT ?")
        rows = [{k: v for k, v in dict(r).items() if v is not None and k != "project_id"}
                for r in conn.execute(sql, (pid, max(1, int(limit or 20))))]
        return {"project": links.project_name(conn, pid), "handoffs": rows, "count": len(rows)}
    finally:
        conn.close()


def brief_lines(conn, pid: int) -> list[str]:
    notes = open_notes(conn, pid)
    if not notes:
        return []
    out = [f"HANDOFF from the last AI ({len(notes)} open; read first, then "
           f"handoff(action=\"pickup\", handoff_id=...)):"]
    for n in notes[:BRIEF_FULL]:
        who = f", {n['created_by']}" if n.get("created_by") else ""
        out.append(f"- H{n['id']} ({(n['created_at'] or '')[:16]}{who}) Where I was: "
                   + " ".join(n["where_i_was"].split()))
        if n.get("next_step"):
            out.append("  Next step: " + " ".join(n["next_step"].split()))
    if len(notes) > BRIEF_FULL:
        out.append(f"- ...and {len(notes) - BRIEF_FULL} older open note(s): handoff(action=\"list\").")
    return out
