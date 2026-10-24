"""Possible conflicting decisions (2.3.0, migration 014).

A decision logged without replaces= may quietly contradict a current one ("Use Postgres for
hosted storage" after "Use SQLite for hosted storage"). FlowState has no language model, so it
can't know two decisions contradict each other. It finds current decisions that read alike:

- same project; decisions about the same component (or with no component) need SIMILAR,
  decisions about a different component need the stricter OTHER_COMPONENT;
- similarity = the mean of word overlap (Dice) and word-order overlap (difflib) after the
  search stemming (meaning._stem), so "Use X for hosted storage" vs "Use Y for hosted storage"
  scores high and "Encrypt hosted storage at rest" does not;
- plus search by meaning when the optional local model is installed (cosine >= MEANING_MIN).

At most MAX_FLAGS pairs per save; the newest MAX_SCAN current decisions are read, so the
check stays fast on large projects. The decision is always saved first; the check never
blocks or loses a write. Each pair is stored once and queued for the user (review item kind
`conflict`): approve = "they really conflict" (stays in the brief until one is replaced),
reject = "compatible" (dismissed). A later decision that replaces either one (log_work
replaces= or a supersedes link) resolves the pair and closes its review item.

All wording says "possible conflict": the AI that logged the decision judges it, then the user.
"""

import difflib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
from . import identity, meaning, review

SIMILAR = 0.55            # tuned on tests/test_decision_conflicts.py fixtures (see there)
OTHER_COMPONENT = 0.65    # both decisions name a component, and not the same one
MEANING_MIN = 0.82        # cosine, only when the local model is installed
MAX_FLAGS = 3
MAX_SCAN = 2000           # newest current decisions compared per save (~25 ms for 2,000)
BRIEF_MAX = 3

_TOKEN = re.compile(r"[a-z0-9]+(?:[.,:][0-9]+)*")
_FILLER = {"the", "a", "an"}
_STOP = meaning._STOP | {"we", "our", "will", "should", "decision"}
_NEGATION = re.compile(r"\b(?:do not|don't|dont|never|no longer|stop)\b")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='decision_conflicts'").fetchone() is not None


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


# ---------------------------------------------------------------- similarity
def _norm(w: str) -> str:
    w = meaning._stem(w)
    return w[:-1] if len(w) > 4 and w.endswith("e") else w   # price / prices -> pric


def tokens(text: str) -> list[str]:
    """Words in order, stemmed like search; "do not" / "never" become "not"."""
    text = _NEGATION.sub(" not ", (text or "").lower())
    return [_norm(t) for t in _TOKEN.findall(text) if t not in _FILLER]


def similarity(a, b) -> float:
    """0-1: how alike two decisions read. `a` / `b` are text or tokens()."""
    ta = tokens(a) if isinstance(a, str) else a
    tb = tokens(b) if isinstance(b, str) else b
    ca, cb = {t for t in ta if t not in _STOP}, {t for t in tb if t not in _STOP}
    if not ca or not cb or not (ca & cb):
        return 0.0
    dice = 2 * len(ca & cb) / (len(ca) + len(cb))
    order = difflib.SequenceMatcher(None, ta, tb, autojunk=False).ratio()
    return round((dice + order) / 2, 3)


def decision_text(key_decisions: Optional[str], fallback: Optional[str]) -> list[str]:
    """The decision lines of a conversations row (log_work stores one)."""
    import json
    try:
        items = json.loads(key_decisions or "[]")
        if isinstance(items, list) and items:
            return [str(i) for i in items[:5]]
    except (ValueError, TypeError):
        pass
    text = (fallback or "").strip()
    return [text[len("Decision:"):].strip() if text.startswith("Decision:") else text]


def _one_line(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


# ---------------------------------------------------------------- the check
def _candidates(conn, pid: int, new_id: int) -> list[dict]:
    """The newest current (not replaced) decisions of the project, with their components.
    (`+a.to_type` keeps SQLite on the from-index; on the to-index this is ~10x slower.)"""
    rows = conn.execute(
        """SELECT d.id, d.key_decisions, d.user_prompt_summary,
                  (SELECT group_concat(a.to_id) FROM record_links a WHERE a.from_type='decision'
                     AND a.from_id=d.id AND a.link_type='about' AND +a.to_type='component'
                     AND a.removed_at IS NULL) comps
           FROM conversations d
           WHERE d.project_id = ? AND d.key_decisions IS NOT NULL AND d.id < ?
             AND NOT EXISTS (SELECT 1 FROM record_links s WHERE s.to_type='decision'
                             AND s.to_id=d.id AND s.link_type='supersedes' AND s.removed_at IS NULL)
           ORDER BY d.id DESC LIMIT ?""", (pid, new_id, MAX_SCAN)).fetchall()
    return [{"id": r["id"], "lines": decision_text(r["key_decisions"], r["user_prompt_summary"]),
             "comps": {int(c) for c in (r["comps"] or "").split(",") if c}} for r in rows]


def _meaning_scores(conn, pid: int, summary: str, ids: list[int]) -> dict:
    """{decision id: cosine} from the meaning index, if the local model is installed."""
    emb = meaning.embedder()
    if emb is None or not ids or not meaning.available(conn):
        return {}
    q = meaning._unpack(meaning._pack(emb.embed([summary])[0])[0])
    wanted = set(ids)
    out = {}
    for row in conn.execute("SELECT content_id, vector FROM meaning_index WHERE model = ? AND "
                            "content_type = 'decision' AND project_id = ?", (emb.name, pid)):
        if row[0] in wanted:
            v = meaning._unpack(row[1])
            if len(v) == len(q):
                out[row[0]] = sum(a * b for a, b in zip(q, v))
    return out


def find(conn, pid: int, new_id: int, summary: str, comp_id: Optional[int]) -> list[dict]:
    """Likely related current decisions, best first (same component before the rest), at most
    MAX_FLAGS. Read-only."""
    mine = tokens(summary)
    cands = _candidates(conn, pid, new_id)
    cos = {}
    try:
        cos = _meaning_scores(conn, pid, summary, [c["id"] for c in cands])
    except Exception:  # noqa: BLE001 - meaning is optional; keywords alone still work
        cos = {}
    hits = []
    for c in cands:
        same = comp_id is not None and comp_id in c["comps"]
        other = comp_id is not None and bool(c["comps"]) and not same
        bar = OTHER_COMPONENT if other else SIMILAR
        kw = max(similarity(mine, line) for line in c["lines"])
        sem = cos.get(c["id"], 0.0)
        if kw >= bar or sem >= MEANING_MIN + (bar - SIMILAR):
            hits.append({"decision_id": c["id"], "summary": _one_line(c["lines"][0], 90),
                         "score": round(max(kw, sem), 3), "same_component": same,
                         "matched_by": "keywords" if kw >= bar else "meaning"})
    hits.sort(key=lambda h: (not h["same_component"], -h["score"], -h["decision_id"]))
    return hits[:MAX_FLAGS]


def check(db_path: Path, pid: int, new_id: int, summary: str, comp_id: Optional[int],
          client: Optional[str] = None) -> list[dict]:
    """After a decision is saved without replaces=: store each likely related pair once and
    queue it for the user. Returns the new flags (empty if none, or if 014 hasn't run)."""
    conn = _connect(db_path)
    try:
        if not available(conn):
            return []
        hits = find(conn, pid, new_id, summary, comp_id)
        if not hits:
            return []
        conn.execute("BEGIN IMMEDIATE")
        try:
            out = []
            for h in hits:
                cur = conn.execute("INSERT OR IGNORE INTO decision_conflicts (project_id, new_id, "
                                   "old_id, score, same_component, created_at) VALUES "
                                   "(?, ?, ?, ?, ?, ?)",
                                   (pid, new_id, h["decision_id"], h["score"],
                                    int(h["same_component"]), _now()))
                if cur.rowcount != 1:
                    continue   # this pair was flagged before
                cid = cur.lastrowid
                sets, who = identity.set_sql(conn, "decision_conflicts", client)
                if sets:
                    conn.execute(f"UPDATE decision_conflicts SET {sets.lstrip(', ')} WHERE id = ?",
                                 (*who, cid))
                item = review.add_item(
                    db_path, pid, "conflict",
                    f'Possible conflict: D{new_id} "{_one_line(summary, 70)}" vs '
                    f'D{h["decision_id"]} "{_one_line(h["summary"], 70)}"',
                    "FlowState found these decisions read alike; it can't tell if they really "
                    "conflict. Approve = they conflict (stays in the brief until one replaces the "
                    "other). Reject = they are compatible (dismissed).",
                    {"conflict_id": cid, "new_id": new_id, "old_id": h["decision_id"],
                     "score": h["score"]}, client, conn=conn)
                if item is not None:
                    conn.execute("UPDATE decision_conflicts SET review_item_id = ? WHERE id = ?",
                                 (item, cid))
                out.append({**h, "conflict_id": cid, "review_item_id": item})
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return out
    finally:
        conn.close()


def on_save(db_path: Path, pid: int, new_id: int, summary: str, comp_id: Optional[int]) -> dict:
    """log_work, a decision saved without replaces=: {possible_conflicts, note} or {}."""
    flags = check(db_path, pid, new_id, summary, comp_id)
    if not flags:
        return {}
    conn = _connect(db_path)
    try:
        project = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    finally:
        conn.close()
    keep = ("decision_id", "summary", "same_component", "score", "review_item_id")
    return {"possible_conflicts": [{k: f[k] for k in keep} for f in flags],
            "note": note(new_id, project, flags)}


def note(new_id: int, project: str, flags: list[dict]) -> str:
    """The short plain note for the log_work response."""
    parts = [f'D{f["decision_id"]} "{_one_line(f["summary"], 60)}" '
             f'({"same component" if f["same_component"] else "same project"})' for f in flags]
    target = str(flags[0]["decision_id"]) if len(flags) == 1 else "<its id>"
    return (f"Possible conflict with {'; '.join(parts)}. If D{new_id} replaces "
            f"{'it' if len(flags) == 1 else 'one'}, link them: manage_links(action=\"add\", "
            f"project=\"{project}\", link_type=\"supersedes\", item_type=\"decision\", "
            f"item_id={new_id}, target_type=\"decision\", target_id={target}). If they fit "
            f"together, tell the user: only they can mark them compatible (manage_review).")


# ---------------------------------------------------------------- resolving
def _close_items(conn, rows, reason_for, client: Optional[str]) -> None:
    """Close the pending review items of pairs FlowState resolved itself."""
    sets, who = identity.set_sql(conn, "review_items", client, prefix="decided_")
    for r in rows:
        if r["review_item_id"] is None:
            continue
        conn.execute(f"""UPDATE review_items SET status = 'rejected', decided_at = ?,
                        decided_by = 'flowstate (automatic)', reject_reason = ?{sets}
                        WHERE id = ? AND status = 'pending'""",
                     (_now(), reason_for(r), *who, r["review_item_id"]))


def resolve_replaced(db_path: Path, by_decision: int, replaced: int,
                     client: Optional[str] = None) -> list[str]:
    """`by_decision` replaces `replaced`: every open pair with `replaced` in it is resolved.
    Returns them as "D31 vs D12"."""
    conn = _connect(db_path)
    try:
        if not available(conn):
            return []
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute("SELECT * FROM decision_conflicts WHERE status = 'open' AND "
                                "(new_id = ? OR old_id = ?)", (replaced, replaced)).fetchall()
            sets, who = identity.set_sql(conn, "decision_conflicts", client, prefix="resolved_")
            for r in rows:
                conn.execute(f"""UPDATE decision_conflicts SET status = 'resolved_replaced',
                                resolved_at = ?, resolved_by_decision = ?{sets} WHERE id = ?""",
                             (_now(), by_decision, *who, r["id"]))
            _close_items(conn, rows, lambda r: f"closed automatically: D{by_decision} replaced "
                                               f"D{replaced}", client)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        out = [f"D{r['new_id']} vs D{r['old_id']}" for r in rows]
    finally:
        conn.close()
    try:
        from . import conflict_cards
        conflict_cards.on_decision_replaced(db_path, by_decision, replaced, client)
    except Exception:  # noqa: BLE001 - the replacement itself is already saved
        pass
    return out


def _pair_of(conn, item: dict):
    import json
    cid = json.loads(item.get("payload") or "{}").get("conflict_id")
    row = conn.execute("SELECT * FROM decision_conflicts WHERE id = ?", (cid,)).fetchone() \
        if cid is not None else None
    if row is None:
        raise review.ReviewError("record_not_found", "That possible conflict no longer exists "
                                 "(one of the decisions was deleted).")
    return row


def confirm(db_path: Path, item: dict) -> tuple[str, int]:
    """review approve: the user says they really conflict. The pair stays open (and in the
    brief) until a decision replaces one of them."""
    conn = _connect(db_path)
    try:
        row = _pair_of(conn, item)
        if row["status"] != "open":
            raise review.ReviewError("already_resolved",
                                     f"D{row['new_id']} vs D{row['old_id']} is already "
                                     f"{row['status'].replace('_', ' ')}.")
        conn.execute("UPDATE decision_conflicts SET confirmed_at = ? WHERE id = ?",
                     (_now(), row["id"]))
        return "decision_conflicts", row["id"]
    finally:
        conn.close()


def dismiss(db_path: Path, item_id: int, client: Optional[str] = None) -> dict:
    """review reject (already recorded): the user says they are compatible."""
    conn = _connect(db_path)
    try:
        row = _pair_of(conn, dict(conn.execute("SELECT * FROM review_items WHERE id = ?",
                                               (item_id,)).fetchone()))
        if row["status"] != "open":
            return {}
        sets, who = identity.set_sql(conn, "decision_conflicts", client, prefix="resolved_")
        conn.execute(f"""UPDATE decision_conflicts SET status = 'dismissed_compatible',
                        resolved_at = ?{sets} WHERE id = ? AND status = 'open'""",
                     (_now(), *who, row["id"]))
        new_id, old_id = row["new_id"], row["old_id"]
    finally:
        conn.close()
    try:
        from . import conflict_cards
        conflict_cards.on_conflict_dismissed(db_path, new_id, old_id, client)
    except Exception:  # noqa: BLE001 - the dismissal itself is already saved
        pass
    return {"dismissed": f"D{new_id} vs D{old_id}: marked compatible; no longer shown."}


# ---------------------------------------------------------------- the brief
def brief_line(conn, pid: int) -> Optional[str]:
    """ "Possible conflicts: D31 vs D12, ..." (open pairs, newest first, at most BRIEF_MAX),
    or None when there are none."""
    if not available(conn):
        return None
    gone = ("NOT EXISTS (SELECT 1 FROM record_links s WHERE s.to_type='decision' AND "
            "s.to_id={} AND s.link_type='supersedes' AND s.removed_at IS NULL)")
    rows = conn.execute(
        f"""SELECT c.new_id, c.old_id, c.confirmed_at FROM decision_conflicts c
            JOIN conversations n ON n.id = c.new_id JOIN conversations o ON o.id = c.old_id
            WHERE c.project_id = ? AND c.status = 'open'
              AND {gone.format('c.new_id')} AND {gone.format('c.old_id')}
            ORDER BY c.new_id DESC, c.old_id DESC""", (pid,)).fetchall()
    if not rows:
        return None
    shown = ", ".join(f"D{r['new_id']} vs D{r['old_id']}"
                      + (" (user says they conflict)" if r["confirmed_at"] else "")
                      for r in rows[:BRIEF_MAX])
    more = f" (+{len(rows) - BRIEF_MAX} more)" if len(rows) > BRIEF_MAX else ""
    return (f"Possible conflicts: {shown}{more}. If one replaces the other, link it "
            f"(manage_links link_type=supersedes); if both stand, ask the user (manage_review).")
