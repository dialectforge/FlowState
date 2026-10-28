"""Reopen possible conflicts after an undo? The owner's question (task 69).

A decision that replaces another (log_work replaces= or a supersedes link) closes the open
possible-conflict pairs with the replaced decision in them (conflicts.resolve_replaced:
`resolved_replaced`). If an app later undoes that replacement (manage_history undo of the
replacing decision or of its supersedes link), the pairs stay closed: no app may reopen
review-queue state (history._user_only). So that they don't drop out of the brief silently,
the undo queues ONE review item of kind `reopen_conflict` in the project, naming the
decisions in plain words: "Reopen the possible conflict between D12 ... and D11 ...?".

  - One item per undo, for all its pairs. A pair is asked about once per closing (its
    conflict id and resolved_at): retrying the undo, or undo / redo / undo, finds the item
    that already asks and queues nothing new, also after the user answered it.
  - Only pairs that would show again: still closed by that replacement, the replacement gone
    (decision taken back or its supersedes link undone), both decisions there and neither
    replaced by another decision.
  - The owner's alone: the review page (this computer's, or the tunnel review page), the
    phone pages, or a tap on its review card with the card's one-use code (not on the card of
    the app that undid it, conflict_cards.own_refusal). refusal() turns away every other way:
    every AI tool (chat approvals on or off), the chat walk, a popup and the terminal.
  - Approve reopens each pair still closed by that replacement: status open, back in the
    brief, and (unless the user had already said they conflict) a new possible-conflict
    question in the queue. Reject leaves them closed. Both are in record_history (queue/*
    rows, so undo never changes them) and in approval_log.
  - The payload names every decision as {record_type, record_id}, so `flowstate erase`
    takes the item's text and history with them (erase._about).
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional

from . import history, identity, review

KIND = "reopen_conflict"
# the only deciders besides a card tap: the review page's, the tunnel page's and the phone
# pages' own labels (as card_offer.DECIDERS)
DECIDERS = ("review-page", "tunnel page", "phone")

REFUSED = ("Only the user answers whether to reopen these possible conflicts: on their review "
           "page (on the computer where FlowState runs: the FlowState review shortcut or "
           "`flowstate review-page`), their phone review page, or by tapping the review card in "
           "a chat. Nothing was decided on review item #{id}; don't try again.")
NOTE = ("FlowState asked the user whether to reopen {what} this replacement had closed (review "
        "item #{id}). Only they decide it, on their review page, phone or a review card; don't "
        "decide it for them.")


def _decision(conn, did: int) -> Optional[str]:
    from .conflicts import decision_text
    row = conn.execute("SELECT key_decisions, user_prompt_summary FROM conversations WHERE id = ?",
                       (did,)).fetchone()
    return _quote(decision_text(row[0], row[1])[0]) if row else None


def _quote(text) -> str:
    """Whole, never cut: erase finds and removes every whole quote of an erased decision."""
    return " ".join(str(text or "").split())


def _superseded(conn, did: int) -> bool:
    return conn.execute("SELECT 1 FROM record_links WHERE to_type = 'decision' AND to_id = ? AND "
                        "link_type = 'supersedes' AND removed_at IS NULL", (did,)).fetchone() \
        is not None


def _stands(conn, by: int, new_id: int, old_id: int) -> bool:
    """Does decision `by` still replace one side of the pair?"""
    return conn.execute(
        "SELECT 1 FROM record_links WHERE from_type = 'decision' AND from_id = ? AND "
        "to_type = 'decision' AND to_id IN (?, ?) AND link_type = 'supersedes' AND "
        "removed_at IS NULL AND EXISTS (SELECT 1 FROM conversations WHERE id = ?)",
        (by, new_id, old_id, by)).fetchone() is not None


def _would_show(conn, row) -> bool:
    """Reopened, would the pair be back in the brief (conflicts.open_pairs)?"""
    ids = (row["new_id"], row["old_id"])
    return all(conn.execute("SELECT 1 FROM conversations WHERE id = ?", (i,)).fetchone()
               for i in ids) and not any(_superseded(conn, i) for i in ids)


def _key(p: dict) -> tuple:
    return (p.get("conflict_id"), p.get("resolved_at"))


def _asked(conn, project_id: int) -> dict:
    """{(conflict id, resolved_at): (item id, status)} of every question already queued."""
    out = {}
    for r in conn.execute("SELECT id, status, payload FROM review_items WHERE kind = ? AND "
                          "project_id = ? ORDER BY id", (KIND, project_id)):
        try:
            pairs = json.loads(r[2] or "{}").get("pairs") or []
        except (ValueError, AttributeError):
            continue
        for p in pairs:
            if isinstance(p, dict):
                out.setdefault(_key(p), (r[0], r[1]))
    return out


def undone_replacers(restored: list) -> dict:
    """From an undo's restored rows [(history row, before, after, old)]: {decision id: its text
    before the undo} for each decision whose replacing may have been taken back (the decision
    itself removed, or a supersedes link from it removed)."""
    out = {}
    for g, before, after, _old in restored:
        table = g["table_name"]
        if table == "conversations" and before and after is None:
            from .conflicts import decision_text
            out[g["record_id"]] = _quote(decision_text(before.get("key_decisions"),
                                                       before.get("user_prompt_summary"))[0])
        elif table == "record_links" and before and before.get("link_type") == "supersedes" \
                and before.get("from_type") == "decision" and before.get("removed_at") is None \
                and (after is None or after.get("removed_at") is not None):
            out.setdefault(before["from_id"], None)
    return out


def ask_after_undo(conn, replacers: dict, client: Optional[str]) -> Optional[dict]:
    """Inside the undo's transaction (the caller commits): queue the owner's question for the
    pairs these replacements had closed and that would show again. Returns {"item_id",
    "queued", "pairs"} (queued False: an earlier question already covers them), or None."""
    if not replacers or not history._has(conn, "decision_conflicts") \
            or not review.available(conn) or not history._has(conn, "record_links"):
        return None
    marks = ",".join("?" * len(replacers))
    rows = conn.execute(f"""SELECT * FROM decision_conflicts WHERE status = 'resolved_replaced'
                            AND resolved_by_decision IN ({marks}) ORDER BY id""",
                        list(replacers)).fetchall()
    rows = [r for r in rows if not _stands(conn, r["resolved_by_decision"], r["new_id"], r["old_id"])
            and _would_show(conn, r)]
    if not rows:
        return None
    by_project: dict = {}
    for r in rows:
        by_project.setdefault(r["project_id"], []).append(r)
    pid, rows = next(iter(by_project.items()))   # one undo is one project (history.undo checks)
    asked = _asked(conn, pid)
    new = [r for r in rows if (r["id"], r["resolved_at"]) not in asked]
    if not new:
        waiting = [asked[(r["id"], r["resolved_at"])] for r in rows]
        open_ = [i for i, status in waiting if status == "pending"]
        return {"item_id": open_[0], "queued": False, "pairs": len(rows)} if open_ else None
    text = {i: _decision(conn, i) for r in new for i in (r["new_id"], r["old_id"])}
    for x, t in replacers.items():
        text.setdefault(x, t or _decision(conn, x))
    say = lambda i: f'D{i} "{text[i]}"' if text.get(i) else f"D{i}"   # noqa: E731
    pairs = [f"{say(r['new_id'])} and {say(r['old_id'])}" for r in new]
    gone = sorted({r["resolved_by_decision"] for r in new})
    who = identity.current()
    app = (who.app if who is not None and who.app else client) or "An app"
    took = " and ".join(say(x) for x in gone)
    if len(new) == 1:
        summary = f"Reopen the possible conflict between {pairs[0]}?"
        what, these = "this possible conflict", "it"
    else:
        summary = f"Reopen {len(new)} possible conflicts closed by {', '.join(f'D{x}' for x in gone)}?"
        what, these = "these possible conflicts", "they"
    details = (f"{app} undid {took}, which had replaced one of these decisions. That replacement "
               f"had closed {what}, so {these} left the brief: " + "; ".join(pairs) + ". "
               "Reopen: back in the brief and the conflict list, and you are asked again whether "
               "they conflict. Leave closed: nothing changes. Only you decide this.")
    ids = sorted({i for r in new for i in (r["new_id"], r["old_id"])} | set(gone))
    payload = {"pairs": [{"conflict_id": r["id"], "new_id": r["new_id"], "old_id": r["old_id"],
                          "resolved_at": r["resolved_at"], "by": r["resolved_by_decision"]}
                         for r in new],
               "decisions": [{"record_type": "decision", "record_id": i} for i in ids],
               "user_only": True}
    item = review.add_item(None, pid, KIND, summary, details, payload, (client or "unknown")[:80],
                           conn=conn)
    return {"item_id": item, "queued": True, "pairs": len(new)} if item else None


def note(asked: dict) -> str:
    what = "the possible conflict" if asked["pairs"] == 1 else "the possible conflicts"
    return NOTE.format(what=what, id=asked["item_id"])


# ---------------------------------------------------------------- who decides
def may_decide(client: Optional[str], card: bool = False) -> bool:
    """A tap on the item's review card (its one-use code was checked), or the user on the
    review page, the tunnel page or a phone page (no AI identity)."""
    if card:
        return True
    return identity.current() is None and str(client or "").lower().startswith(DECIDERS)


def refusal(item: Optional[dict], client: str, agent: bool) -> Optional[dict]:
    """None when this decider may answer the question (server._review_decide)."""
    if not item or item.get("kind") != KIND or item.get("status") != "pending":
        return None
    if not agent and may_decide(client):
        return None
    return {"error": "user_only", "message": REFUSED.format(id=item["id"]), "item_id": item["id"],
            "kind": KIND, "status": "waiting for the user"}


def check(item: dict, client: Optional[str], card: bool) -> None:
    """review_apply's own check, whichever way the decision came: raises ReviewError."""
    if item.get("kind") == KIND and not may_decide(client, card):
        raise review.ReviewError("user_only", REFUSED.format(id=item["id"]))


# ---------------------------------------------------------------- the answer
def _connect(db_path: Path) -> sqlite3.Connection:
    from .conflicts import _connect as c
    return c(db_path)


def apply(db_path: Path, item: dict, client: str, done: dict) -> tuple:
    """Approve: reopen each pair still closed by the replacement the undo took back. `item` is
    the row as it was before approving (review._claim)."""
    p = json.loads(item.get("payload") or "{}")
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            reopened, kept = [], []
            for pair in p.get("pairs") or []:
                row = conn.execute("SELECT * FROM decision_conflicts WHERE id = ?",
                                   (pair.get("conflict_id"),)).fetchone()
                label = f"D{pair.get('new_id')} vs D{pair.get('old_id')}"
                if row is None or row["status"] != "resolved_replaced" \
                        or row["resolved_at"] != pair.get("resolved_at"):
                    kept.append(f"{label} (changed since)")
                    continue
                if _stands(conn, row["resolved_by_decision"], row["new_id"], row["old_id"]):
                    kept.append(f"{label} (D{row['resolved_by_decision']} replaces it again)")
                    continue
                if not _would_show(conn, row):
                    kept.append(f"{label} (a decision was deleted or replaced since)")
                    continue
                before = history._row(conn, "decision_conflicts", row["id"])
                sets, who = identity.set_sql(conn, "decision_conflicts", None, prefix="resolved_",
                                             clear=True)
                conn.execute(f"""UPDATE decision_conflicts SET status = 'open', resolved_at = NULL,
                                resolved_by_decision = NULL{sets} WHERE id = ?""", (*who, row["id"]))
                if row["confirmed_at"] is None:   # asked again: they conflict, or fit together?
                    q = review.add_item(
                        db_path, row["project_id"], "conflict",
                        f'Possible conflict: D{row["new_id"]} "{_decision(conn, row["new_id"])}" '
                        f'vs D{row["old_id"]} "{_decision(conn, row["old_id"])}"',
                        "Reopened by the user after an undo. FlowState found these decisions read "
                        "alike; it can't tell if they really conflict. Approve = they conflict "
                        "(stays in the brief until one replaces the other). Reject = they are "
                        "compatible (dismissed).",
                        {"conflict_id": row["id"], "new_id": row["new_id"],
                         "old_id": row["old_id"], "score": row["score"]}, client, conn=conn)
                    if q is not None:
                        conn.execute("UPDATE decision_conflicts SET review_item_id = ? WHERE id = ?",
                                     (q, row["id"]))
                history.record_row(conn, "decision_conflicts", row["id"], before,
                                   "queue/conflict_reopened", client)
                reopened.append((row["id"], label))
            history.record_row(conn, "review_items", item["id"], item, "queue/reopen_approved",
                               client)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    if reopened:
        done["reopened"] = [label for _i, label in reopened]
        done["note"] = (f"Reopened {', '.join(done['reopened'])}: back in the brief as possible "
                        f"conflicts.")
    else:
        done["note"] = "Nothing was reopened: " + "; ".join(kept) + "."
    if kept and reopened:
        done["not_reopened"] = kept
    return "decision_conflicts", (reopened[0][0] if reopened else None)


def on_reject(db_path: Path, before: Optional[dict], item_id: int, client: str) -> dict:
    """Reject (already saved): the pairs stay closed; the answer goes in the history."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            history.record_row(conn, "review_items", item_id, before, "queue/reopen_declined",
                               client)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"note": "Left closed: those possible conflicts stay out of the brief."}


def describe(item: dict) -> str:
    try:
        n = len(json.loads(item.get("payload") or "{}").get("pairs") or [])
    except (ValueError, AttributeError):
        n = 0
    return ("reopen the possible conflict an undone replacement had closed" if n == 1 else
            "reopen the possible conflicts an undone replacement had closed")
