"""Where a record came from, and when it was last checked (v2 Phase D, E5/E6; migration 011).

source: user_stated (the user said so), ai_inferred (an AI concluded it), from_code (read in
code or commits). verified_at: when someone last confirmed it is still true. Both live in
`record_meta`, a side table; no row means "unknown, never verified", which is what every
older record is.

Stale facts: learnings, changes, decisions, variables and methods that mention paths,
ports, hosts, URLs or config files (all server/config/endpoint/environment variables), not
verified (or, if never verified, not created/updated) for STALE_DAYS. They are listed for
re-checking; nothing is changed or hidden because of it.
"""

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import links

SOURCES = ("user_stated", "ai_inferred", "from_code")
TAGS = {"user_stated": "user", "ai_inferred": "AI", "from_code": "code"}
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


def _upsert(conn, rtype: str, rid: int, **cols) -> None:
    conn.execute("INSERT OR IGNORE INTO record_meta (record_type, record_id) VALUES (?, ?)",
                 (rtype, rid))
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE record_meta SET {sets}, updated_at = ? WHERE record_type = ? "
                 f"AND record_id = ?", (*cols.values(), _now(), rtype, rid))


def set_source(db_path: Path, rtype: str, rid: int, source: str, client: Optional[str]) -> None:
    """Record where a record came from (no-op if migration 011 hasn't run)."""
    if source not in SOURCES:
        raise MetaError("bad_source", "source must be one of " + ", ".join(SOURCES) + ".")
    conn = links.connect(db_path)
    try:
        if not available(conn):
            return
        _upsert(conn, rtype, int(rid), source=source)
        conn.commit()
    finally:
        conn.close()


def mark(db_path: Path, pid: int, rtype: str, rid, client: str, source: Optional[str] = None,
         verified: bool = True) -> dict:
    """The `verify` tool's mark / source actions."""
    if source is not None and source not in SOURCES:
        raise MetaError("bad_source", "source must be one of " + ", ".join(SOURCES) + ".")
    conn = links.connect(db_path)
    try:
        _need(conn)
        rid = check_record(conn, pid, rtype, rid)
        cols = {}
        if verified:
            cols.update(verified_at=_now(), verified_by=client)
        if source:
            cols["source"] = source
        if not cols:
            raise MetaError("nothing_to_do", "Give source=... (or use action=mark).")
        _upsert(conn, rtype, rid, **cols)
        conn.commit()
        return {"record_type": rtype, "record_id": rid, **get(conn, rtype, [rid]).get(rid, {})}
    finally:
        conn.close()


def get(conn, rtype: str, ids) -> dict:
    """{id: {"source": ..., "verified_at": ...}} for those that have a row."""
    ids = [int(i) for i in ids if i is not None]
    if not ids or not available(conn):
        return {}
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in conn.execute(
                f"SELECT record_id, source, verified_at FROM record_meta WHERE record_type = ? "
                f"AND record_id IN ({','.join('?' * len(chunk))})", (rtype, *chunk)):
            out[r[0]] = {k: v for k, v in (("source", r[1]), ("verified_at", r[2])) if v}
    return out


def tag(meta: Optional[dict]) -> str:
    """Compact suffix for brief lines: ' (user)', ' (AI, checked 2026-10-16)', or ''."""
    if not meta:
        return ""
    bits = []
    if meta.get("source"):
        bits.append(TAGS[meta["source"]])
    if meta.get("verified_at"):
        bits.append(f"checked {meta['verified_at'][:10]}")
    return f" ({', '.join(bits)})" if bits else ""


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
