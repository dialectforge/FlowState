"""Disagreement cards (review kind `disagreement`).

An agent flags two existing entries, or a new one against an existing one. A decision
saved on the same component as a similar current decision opens a card too (the same
pairs the possible-conflict check already trusts; nothing fuzzier). The user keeps the
older, the newer, or both, or dismisses the card. The loser is marked superseded, not
deleted. Chat approvals still gate the model-visible review tools. The in-chat card's
buttons call resolve_conflict_card, which is app-only.

Card taps are not cryptographically proven to be a person. The tool is hidden from the
model's list and refuses chat-text fields; a host that lets the model call it anyway
is a known follow-up.
"""

import html
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
from . import identity, links, review

UI_EXT = "io.modelcontextprotocol/ui"
_FILE = re.compile(r"^[\w./\\-]+:\d+$")
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
LABEL_MAX, EXCERPT_MAX = 160, 240
_WHEN = {
    "project": "projects", "component": "components", "decision": "conversations",
    "change": "changes", "learning": "learnings", "problem": "problems", "todo": "todos",
}
OUTCOME = {
    "kept_a": "Kept the older one. The other is marked superseded, not deleted.",
    "kept_b": "Kept the newer one. The other is marked superseded, not deleted.",
    "kept_both": "Kept both. Neither entry was changed.",
    "dismissed": "Dismissed. Both entries stay as they are.",
}


class CardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.payload = {"error": code, "message": message}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _pretty(ts: Optional[str]) -> str:
    try:
        y, m, d = (ts or "")[:10].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}, {y}"
    except (ValueError, IndexError):
        return ""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conflict_cards'"
                        ).fetchone() is not None


def parse_evidence(value) -> list:
    """Up to two snippets: 'src/auth.py:42', 'src/auth.py:42 — excerpt', or 'output: excerpt'."""
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise CardError("bad_evidence", "evidence is a list of up to 2 short strings "
                        "(file:line, file:line — excerpt, or output: excerpt).")
    if len([x for x in value if str(x).strip()]) > 2:
        raise CardError("too_much_evidence", "At most 2 evidence snippets.")
    out = []
    for raw in value:
        s = " ".join(str(raw).split())
        if not s:
            continue
        if s.lower().startswith("output:"):
            excerpt = s.split(":", 1)[1].strip()[:EXCERPT_MAX]
            if not excerpt:
                raise CardError("bad_evidence", "output: evidence needs a short excerpt.")
            out.append({"kind": "output", "label": "output", "excerpt": excerpt})
            continue
        label, excerpt = s, None
        for sep in (" — ", " | ", " -- "):
            if sep in s:
                label, excerpt = (p.strip() for p in s.split(sep, 1))
                excerpt = (excerpt[:EXCERPT_MAX] or None)
                break
        head = (label.split() or [""])[0]
        if not _FILE.match(head):
            raise CardError("bad_evidence", "Evidence is file:line, file:line — excerpt, "
                            "or output: excerpt.")
        out.append({"kind": "file", "label": head[:LABEL_MAX], "excerpt": excerpt})
    return out


def precheck(db_path: Path, pid: int, args: dict) -> Optional[dict]:
    """Before the record is saved. None when the write may proceed."""
    try:
        if args.get("evidence") not in (None, "", []):
            parse_evidence(args.get("evidence"))
    except CardError as e:
        return e.payload
    cw = args.get("conflicts_with")
    if cw in (None, ""):
        return None
    typ = args.get("conflicts_with_type") or "decision"
    if typ not in links.ENDPOINTS:
        return {"error": "bad_type", "message": "conflicts_with_type must be one of: "
                + ", ".join(links.ENDPOINTS) + "."}
    try:
        cw = int(cw)
    except (TypeError, ValueError):
        return {"error": "bad_request", "message": "conflicts_with must be the record's id."}
    conn = _connect(db_path)
    try:
        if not available(conn):
            return {"error": "not_available", "message": "Disagreement cards are not set up "
                    "in this database yet. Nothing was saved."}
        if links.owner(conn, typ, cw) != pid:
            return {"error": "record_not_found",
                    "message": f"No {typ} {cw} in this project to disagree with."}
    finally:
        conn.close()
    return None


def _created(conn, typ: str, rid: int) -> str:
    table = _WHEN.get(typ)
    if not table:
        return ""
    try:
        row = conn.execute(f'SELECT created_at FROM "{table}" WHERE id = ?', (rid,)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return (row[0] or "") if row else ""


def _writer(conn, typ: str, rid: int, fresh: bool, client: str) -> tuple[str, str]:
    if not fresh and identity.has_column(conn, "activity_log", "record_type"):
        row = conn.execute("""SELECT app, at FROM activity_log WHERE record_type = ? AND record_id = ?
                              AND ok = 1 AND app IS NOT NULL ORDER BY id ASC LIMIT 1""",
                           (typ, rid)).fetchone()
        if row and row["app"]:
            return row["app"], row["at"] or ""
    if fresh:
        return identity.stamp(client).get("app") or client or "an app", _now()
    return "an earlier entry", ""


def _evidence(conn, typ: str, rid: int) -> list:
    if not available(conn):
        return []
    try:
        rows = conn.execute("""SELECT kind, label, excerpt FROM entry_evidence
                               WHERE record_type = ? AND record_id = ? ORDER BY position, id""",
                            (typ, rid)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"label": r["label"], "excerpt": r["excerpt"] or ""} for r in rows]


def save_evidence(conn, typ: str, rid: int, items: list, client: str) -> None:
    if not items or not available(conn):
        return
    conn.execute("DELETE FROM entry_evidence WHERE record_type = ? AND record_id = ?", (typ, rid))
    at = _now()
    for i, ev in enumerate(items):
        identity.insert(conn, "entry_evidence", {
            "record_type": typ, "record_id": rid, "kind": ev["kind"], "label": ev["label"],
            "excerpt": ev.get("excerpt"), "position": i, "created_at": at}, client)


def _topic_of(conn, typ: str, rid: int, given: str) -> str:
    topic = " ".join((given or "").split())
    if topic:
        return topic[:80]
    if typ == "decision":
        row = conn.execute("""SELECT c.name FROM record_links l JOIN components c ON c.id = l.to_id
                              WHERE l.from_type = 'decision' AND l.from_id = ? AND l.to_type = 'component'
                                AND l.link_type = 'about' AND l.removed_at IS NULL LIMIT 1""",
                           (rid,)).fetchone()
        if row and row[0]:
            return str(row[0])[:80]
    claim = links.label(conn, typ, rid)
    return (claim[:77] + "...") if len(claim) > 80 else (claim or "this")


def _order(conn, a: tuple, b: tuple) -> tuple:
    ka = (_created(conn, *a) or "9999", a[1], a[0])
    kb = (_created(conn, *b) or "9999", b[1], b[0])
    return (a, b) if ka <= kb else (b, a)


def _project(conn, pid: int) -> str:
    row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
    return row[0] if row else f"#{pid}"


def _view(conn, card, project: str, item_id, url: str) -> dict:
    sides = []
    for mark, typ, rid, app, at in (
            ("A", card["a_type"], card["a_id"], card["a_app"], card["a_at"]),
            ("B", card["b_type"], card["b_id"], card["b_app"], card["b_at"])):
        when = _pretty(at) or _pretty(_created(conn, typ, rid))
        sides.append({"mark": mark, "who": app or "an earlier entry", "when": when,
                      "claim": links.label(conn, typ, rid) or f"{typ} #{rid}",
                      "evidence": _evidence(conn, typ, rid)})
    open_ = card["status"] == "open"
    return {"headline": f"Two AIs disagree about {card['topic']}.", "item_id": item_id,
            "project": project, "review_url": url or "", "sides": sides,
            "resolved": not open_, "detail": "" if open_ else OUTCOME.get(card["status"], "Saved.")}


def _plain(view: dict) -> str:
    lines = [view["headline"]]
    for s in view["sides"]:
        ev = []
        for e in s["evidence"]:
            bit = e["label"] or ""
            if e.get("excerpt"):
                bit += (" — " if bit else "") + e["excerpt"]
            if bit:
                ev.append(bit)
        who = s["who"] + (f", {s['when']}" if s["when"] else "")
        lines.append(f"Claim {s['mark']} ({who}): {s['claim']}")
        lines.append("Evidence: " + ("; ".join(ev) if ev else "none"))
    if view.get("review_url"):
        lines.append("Review: " + view["review_url"])
    if view.get("resolved") and view.get("detail"):
        lines.append(view["detail"])
    return "\n".join(lines)


def _url(db_path: Path) -> str:
    from .waiting import links as review_links
    ways = review_links(db_path)
    return ways.get("phone") or ways.get("review_page") or ""


def _pack(db_path, conn, card, item_id: int) -> dict:
    project = _project(conn, card["project_id"])
    view = _view(conn, card, project, item_id, _url(db_path))
    return {"review_item_id": item_id, "text": _plain(view), "structured": view}


def _open_card(conn, db_path, pid: int, topic: str, left: tuple, right: tuple, how: str,
               client: str, fresh: Optional[tuple] = None) -> dict:
    if left == right:
        raise CardError("same_record", "Name two different records.")
    for typ, rid in (left, right):
        if typ not in links.ENDPOINTS:
            raise CardError("bad_type", f"{typ!r} can't be on a disagreement card.")
        if links.owner(conn, typ, int(rid)) != pid:
            raise CardError("record_not_found", f"No {typ} {rid} in this project.")
    a, b = _order(conn, (left[0], int(left[1])), (right[0], int(right[1])))
    existing = conn.execute("""SELECT * FROM conflict_cards WHERE project_id = ? AND status = 'open'
                               AND a_type = ? AND a_id = ? AND b_type = ? AND b_id = ?""",
                            (pid, a[0], a[1], b[0], b[1])).fetchone()
    if existing and existing["review_item_id"]:
        return _pack(db_path, conn, existing, existing["review_item_id"])
    a_app, _aw = _writer(conn, a[0], a[1], fresh == a, client)
    b_app, _bw = _writer(conn, b[0], b[1], fresh == b, client)
    cur = identity.insert(conn, "conflict_cards", {
        "project_id": pid, "topic": topic[:80], "a_type": a[0], "a_id": a[1],
        "b_type": b[0], "b_id": b[1], "status": "open", "how": how, "created_at": _now(),
        "a_app": a_app, "a_at": _created(conn, *a), "b_app": b_app, "b_at": _created(conn, *b)},
        client)
    cid = cur.lastrowid
    headline = f"Two AIs disagree about {topic[:80]}."
    item = review.add_item(db_path, pid, "disagreement", headline,
                           "The user chooses which entry stands.", {"card_id": cid}, client, conn=conn)
    conn.execute("UPDATE conflict_cards SET review_item_id = ? WHERE id = ?", (item, cid))
    card = conn.execute("SELECT * FROM conflict_cards WHERE id = ?", (cid,)).fetchone()
    return _pack(db_path, conn, card, item)


def _answer(cards: list) -> dict:
    if not cards:
        return {}
    out = {"conflict_cards": [{"review_item_id": c["review_item_id"], "text": c["text"]}
                              for c in cards]}
    out["_ui"] = {"structured": cards[0]["structured"]}
    return out


def after_log(db_path: Path, pid: int, kind: str, args: dict, result: dict, client: str) -> dict:
    """After a record is saved: store its evidence and open a card when one was asked for
    or a same-component possible conflict was found. Never raises into the write."""
    rec = (result or {}).get("record") if isinstance(result, dict) else None
    rid = rec.get("id") if isinstance(rec, dict) else None
    if rid is None:
        return {}
    conn = _connect(db_path)
    try:
        if not available(conn):
            return {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            if args.get("evidence") not in (None, "", []):
                save_evidence(conn, kind, int(rid), parse_evidence(args.get("evidence")), client)
            cards = []
            cw = args.get("conflicts_with")
            if cw not in (None, ""):
                typ = args.get("conflicts_with_type") or "decision"
                topic = _topic_of(conn, kind, int(rid), args.get("topic") or "")
                cards.append(_open_card(conn, db_path, pid, topic, (kind, int(rid)),
                                        (typ, int(cw)), "flagged", client, fresh=(kind, int(rid))))
            elif kind == "decision":
                hit = next((f for f in (result.get("possible_conflicts") or [])
                            if f.get("same_component")), None)
                if hit and hit.get("decision_id") is not None:
                    topic = _topic_of(conn, "decision", int(rid), "")
                    cards.append(_open_card(conn, db_path, pid, topic, ("decision", int(rid)),
                                            ("decision", int(hit["decision_id"])), "same_component",
                                            client, fresh=("decision", int(rid))))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return _answer(cards)
    finally:
        conn.close()


def try_link(db_path: Path, pid: Optional[int], args: dict, client: str):
    """manage_links flag_conflict. None when this action is someone else's."""
    if args.get("action") != "flag_conflict":
        return None
    if pid is None:
        return {"error": "project_required", "message": "flag_conflict needs project=\"<name>\"."}
    try:
        return flag(db_path, pid, args, client)
    except CardError as e:
        return e.payload


def flag(db_path: Path, pid: int, args: dict, client: str) -> dict:
    topic = " ".join(str(args.get("topic") or "").split())
    if not topic:
        raise CardError("topic_required", "flag_conflict needs a short topic, in plain words "
                        "(topic=\"API sessions\").")
    if args.get("item_id") is None or args.get("target_id") is None:
        raise CardError("record_required", "Name both records: item_type, item_id, target_type, "
                        "target_id.")
    try:
        left = (args.get("item_type") or "decision", int(args["item_id"]))
        right = (args.get("target_type") or "decision", int(args["target_id"]))
    except (TypeError, ValueError):
        raise CardError("bad_request", "item_id and target_id must be numbers.") from None
    ev, tev = parse_evidence(args.get("evidence")), parse_evidence(args.get("target_evidence"))
    conn = _connect(db_path)
    try:
        if not available(conn):
            raise CardError("not_available", "Disagreement cards are not set up in this database yet.")
        conn.execute("BEGIN IMMEDIATE")
        try:
            save_evidence(conn, left[0], left[1], ev, client)
            save_evidence(conn, right[0], right[1], tev, client)
            card = _open_card(conn, db_path, pid, topic[:80], left, right, "flagged", client)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    out = _answer([card])
    out["flagged"] = card["review_item_id"]
    out["note"] = "Nothing was changed yet. The user chooses which entry stands."
    return out


def keep_error(args: dict) -> Optional[dict]:
    if args.get("keep") not in ("a", "b", "both"):
        return {"error": "keep_required", "message": "This disagreement needs keep=\"a\" "
                "(keep the older), \"b\" (keep the newer) or \"both\"."}
    return None


def _card_of(conn, item: dict):
    cid = json.loads(item.get("payload") or "{}").get("card_id")
    row = conn.execute("SELECT * FROM conflict_cards WHERE id = ?", (cid,)).fetchone() \
        if cid is not None else None
    if row is None:
        raise review.ReviewError("record_not_found", "That disagreement no longer exists.")
    return row


def _audit(db_path, item: dict, path: str, client: str, decision: str) -> None:
    from . import approvals
    approvals.log_decision(db_path, item, path, client, decision)


def _mark(conn, typ: str, rid: int, by: tuple, card_id: int, client: str) -> None:
    who = identity.fields(conn, "entry_standing", client)
    row = {"record_type": typ, "record_id": rid, "standing": "superseded",
           "by_type": by[0], "by_id": by[1], "card_id": card_id, "at": _now(), **who}
    conn.execute(f'INSERT OR REPLACE INTO entry_standing ({", ".join(row)}) '
                 f'VALUES ({", ".join("?" * len(row))})', list(row.values()))


def _quiet_pair(conn, a_id: int, b_id: int, client: str, reason: str) -> None:
    """Close a matching possible-conflict pair without going back through conflicts.dismiss."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='decision_conflicts'"
                    ).fetchone() is None:
        return
    rows = conn.execute("""SELECT * FROM decision_conflicts WHERE status = 'open' AND
                           ((new_id = ? AND old_id = ?) OR (new_id = ? AND old_id = ?))""",
                        (a_id, b_id, b_id, a_id)).fetchall()
    sets, who = identity.set_sql(conn, "decision_conflicts", client, prefix="resolved_")
    isets, iwho = identity.set_sql(conn, "review_items", client, prefix="decided_")
    for r in rows:
        conn.execute(f"""UPDATE decision_conflicts SET status = 'dismissed_compatible',
                        resolved_at = ?{sets} WHERE id = ? AND status = 'open'""",
                     (_now(), *who, r["id"]))
        if r["review_item_id"] is not None:
            conn.execute(f"""UPDATE review_items SET status = 'rejected', decided_at = ?,
                            decided_by = 'flowstate (automatic)', reject_reason = ?{isets}
                            WHERE id = ? AND status = 'pending'""",
                         (_now(), reason, *iwho, r["review_item_id"]))


def _set_status(conn, card, status: str, client: str, path: str) -> None:
    sets, who = identity.set_sql(conn, "conflict_cards", client, prefix="resolved_")
    conn.execute(f"""UPDATE conflict_cards SET status = ?, resolved_at = ?, resolved_path = ?{sets}
                    WHERE id = ? AND status = 'open'""",
                 (status, _now(), path, *who, card["id"]))


def apply_keep(db_path: Path, item: dict, keep: str, client: str, path: Optional[str] = None):
    """review approve of a disagreement. Returns (table, card id). Status is set before any
    hook back into conflicts, so those hooks no-op on this card."""
    if keep not in ("a", "b", "both"):
        raise review.ReviewError("keep_required", keep_error({"keep": keep})["message"])
    path = path or ("chat" if identity.current() is not None else "review page")
    status = {"a": "kept_a", "b": "kept_b", "both": "kept_both"}[keep]
    conn = _connect(db_path)
    try:
        card = _card_of(conn, item)
        if card["status"] != "open":
            raise review.ReviewError("already_resolved", "This disagreement is already decided.")
        conn.execute("BEGIN IMMEDIATE")
        try:
            _set_status(conn, card, status, client, path)
            loser = winner = None
            if keep == "a":
                winner, loser = (card["a_type"], card["a_id"]), (card["b_type"], card["b_id"])
            elif keep == "b":
                winner, loser = (card["b_type"], card["b_id"]), (card["a_type"], card["a_id"])
            if loser:
                _mark(conn, loser[0], loser[1], winner, card["id"], client)
            if card["a_type"] == "decision" and card["b_type"] == "decision":
                _quiet_pair(conn, card["a_id"], card["b_id"], client,
                            "closed: the disagreement card was decided")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    if loser and loser[0] == "decision" and winner[0] == "decision":
        try:
            links.add(db_path, item["project_id"], "supersedes", item_type="decision",
                      item_id=winner[1], target_type="decision", target_id=loser[1], client=client)
        except links.LinkError:
            pass
        from . import conflicts
        conflicts.resolve_replaced(db_path, winner[1], loser[1], client)
    _audit(db_path, item, path, client, status)
    return "conflict_cards", card["id"]


def on_reject(db_path: Path, item: dict, client: str) -> dict:
    """The user dismissed the card (review reject already recorded)."""
    path = "chat" if identity.current() is not None else "review page"
    conn = _connect(db_path)
    try:
        if not available(conn):
            return {}
        card = _card_of(conn, item)
        if card["status"] != "open":
            return {}
        conn.execute("BEGIN IMMEDIATE")
        try:
            _set_status(conn, card, "dismissed", client, path)
            if card["a_type"] == "decision" and card["b_type"] == "decision":
                _quiet_pair(conn, card["a_id"], card["b_id"], client,
                            "closed: the disagreement was dismissed")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    _audit(db_path, item, path, client, "dismissed")
    return {"dismissed": OUTCOME["dismissed"]}


def decision_path(client: str) -> str:
    if identity.current() is not None:
        return "chat"
    from . import approvals
    return "chat" if approvals.path_of(client) == "chat" else "review page"


def resolve_card(db_path: Path, args: dict, client: str) -> dict:
    """The in-chat card's button. Not a chat sentence: user_request, message, text and reason
    are refused. Chat approvals do not apply; the path is recorded as card."""
    for key in ("user_request", "message", "text", "reason"):
        if args.get(key) not in (None, ""):
            return {"error": "not_a_card_tap", "message": "A disagreement card is resolved by "
                    "its buttons, not by chat text."}
    bad = keep_error(args)
    if bad:
        return bad
    if args.get("item_id") is None:
        return {"error": "item_id_required", "message": "Name the review item (item_id)."}
    item = review.get_item(db_path, int(args["item_id"]))
    if not item or item.get("kind") != "disagreement":
        return {"error": "record_not_found", "message": "No disagreement card with that id."}
    if item["status"] != "pending":
        return {"error": "already_resolved", "message": "This disagreement is already decided."}

    def apply(it, _edits):
        return apply_keep(db_path, it, args["keep"], client, path="card")

    try:
        out = review.approve(db_path, int(args["item_id"]),
                             "The user tapped a button on the conflict card.", client,
                             lambda it, payload: ("conflict_cards", None),
                             others={"disagreement": apply})
    except review.ReviewError as e:
        return e.payload
    conn = _connect(db_path)
    try:
        card = conn.execute("SELECT * FROM conflict_cards WHERE review_item_id = ?",
                            (item["id"],)).fetchone()
        packed = _pack(db_path, conn, card, item["id"]) if card else None
    finally:
        conn.close()
    text = packed["text"] if packed else OUTCOME.get("kept_" + args["keep"], "Saved.")
    res = {"resolved": item["id"], "keep": args["keep"], "text": text, "note": text}
    if packed:
        res["_ui"] = {"structured": packed["structured"]}
        res["conflict_cards"] = [{"review_item_id": item["id"], "text": text}]
    res.update({k: v for k, v in out.items() if k not in res})
    return res


def _rows_touching(conn, decision_id: int):
    return conn.execute("""SELECT * FROM conflict_cards WHERE status = 'open' AND
                           ((a_type = 'decision' AND a_id = ?) OR (b_type = 'decision' AND b_id = ?))""",
                        (decision_id, decision_id)).fetchall()


def _close_item(conn, item_id, reason: str, client: str) -> None:
    if item_id is None:
        return
    sets, who = identity.set_sql(conn, "review_items", client, prefix="decided_")
    conn.execute(f"""UPDATE review_items SET status = 'rejected', decided_at = ?,
                    decided_by = 'flowstate (automatic)', reject_reason = ?{sets}
                    WHERE id = ? AND status = 'pending'""", (_now(), reason[:500], *who, item_id))


def on_decision_replaced(db_path: Path, by_decision: int, replaced: int, client: Optional[str]) -> None:
    """A decision replaced another (replaces= or a supersedes link). Close cards that named
    the loser. No-op when the card was already decided (apply_keep sets that first)."""
    audits = []
    conn = _connect(db_path)
    try:
        if not available(conn):
            return
        rows = _rows_touching(conn, replaced)
        if not rows:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for card in rows:
                if card["status"] != "open":
                    continue
                other = card["a_id"] if card["b_id"] == replaced else card["b_id"]
                if other == by_decision:
                    status = "kept_a" if card["a_id"] == by_decision else "kept_b"
                    _set_status(conn, card, status, client, "automatic")
                    _mark(conn, "decision", replaced, ("decision", by_decision), card["id"], client)
                else:
                    status = "dismissed"
                    _set_status(conn, card, status, client, "automatic")
                _close_item(conn, card["review_item_id"],
                            f"closed automatically: D{by_decision} replaced D{replaced}", client)
                if card["review_item_id"] is not None:
                    audits.append((card["review_item_id"], card["project_id"], status))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    for item_id, pid, status in audits:
        _audit(db_path, {"id": item_id, "project_id": pid, "kind": "disagreement"},
               "automatic", client or "flowstate", status)


def on_conflict_dismissed(db_path: Path, new_id: int, old_id: int, client: Optional[str]) -> None:
    """The user said a possible-conflict pair is compatible. Close a card on the same two
    decisions if it is still open."""
    audits = []
    conn = _connect(db_path)
    try:
        if not available(conn):
            return
        rows = conn.execute("""SELECT * FROM conflict_cards WHERE status = 'open'
                               AND a_type = 'decision' AND b_type = 'decision'
                               AND ((a_id = ? AND b_id = ?) OR (a_id = ? AND b_id = ?))""",
                            (new_id, old_id, old_id, new_id)).fetchall()
        if not rows:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for card in rows:
                if card["status"] != "open":
                    continue
                _set_status(conn, card, "dismissed", client, "automatic")
                _close_item(conn, card["review_item_id"],
                            "closed automatically: the user said the two decisions fit together",
                            client)
                if card["review_item_id"] is not None:
                    audits.append((card["review_item_id"], card["project_id"]))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    for item_id, pid in audits:
        _audit(db_path, {"id": item_id, "project_id": pid, "kind": "disagreement"},
               "automatic", client or "flowstate", "dismissed")


def brief_line(conn, pid: int) -> Optional[str]:
    """One short line for an explicit card. None when every open card is already covered by
    a Possible conflicts line (those keep their own wording)."""
    if not available(conn):
        return None
    rows = conn.execute("""SELECT * FROM conflict_cards WHERE project_id = ? AND status = 'open'
                           ORDER BY id DESC""", (pid,)).fetchall()
    shown = []
    has_pairs = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                             "AND name='decision_conflicts'").fetchone() is not None
    for r in rows:
        if has_pairs and r["a_type"] == "decision" and r["b_type"] == "decision":
            hit = conn.execute("""SELECT 1 FROM decision_conflicts WHERE project_id = ? AND status = 'open'
                                  AND ((new_id = ? AND old_id = ?) OR (new_id = ? AND old_id = ?))""",
                               (pid, r["a_id"], r["b_id"], r["b_id"], r["a_id"])).fetchone()
            if hit:
                continue
        shown.append(r)
    if not shown:
        return None
    extra = f" (+{len(shown) - 1} more)" if len(shown) > 1 else ""
    return f"Two AIs disagree about {shown[0]['topic']}{extra}."


def hidden_conflict_items(db_path: Path) -> set:
    """Pending possible-conflict review items whose pair already has an open card.
    The phone list hides those so the same pair is not asked twice. The desktop page
    still shows the older buttons."""
    conn = _connect(db_path)
    try:
        if not available(conn):
            return set()
        out = set()
        cards = conn.execute("""SELECT a_id, b_id FROM conflict_cards WHERE status = 'open'
                                AND a_type = 'decision' AND b_type = 'decision'""").fetchall()
        if not cards:
            return set()
        for r in conn.execute("""SELECT id, payload FROM review_items WHERE kind = 'conflict'
                                 AND status = 'pending'"""):
            try:
                p = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            pair = {p.get("new_id"), p.get("old_id")}
            if any(pair == {c["a_id"], c["b_id"]} for c in cards):
                out.add(r["id"])
        return out
    finally:
        conn.close()


def list_line(db_path: Path, it: dict, status: bool = False) -> str:
    conn = _connect(db_path)
    try:
        card = None
        if available(conn):
            cid = json.loads(it.get("payload") or "{}").get("card_id")
            if cid is not None:
                card = conn.execute("SELECT * FROM conflict_cards WHERE id = ?", (cid,)).fetchone()
        kind = "disagreement" + (f", {it['status']}" if status else "")
        head = f"#{it['id']} ({it.get('project') or ''}, {kind}): "
        if card is None:
            return head + (it.get("summary") or "")
        view = _view(conn, card, it.get("project") or _project(conn, card["project_id"]),
                     it["id"], _url(db_path))
        return head + _plain(view).replace("\n", " | ")
    finally:
        conn.close()


def ui_for_item(db_path: Path, it: dict) -> Optional[dict]:
    conn = _connect(db_path)
    try:
        if not available(conn):
            return None
        cid = json.loads(it.get("payload") or "{}").get("card_id")
        card = conn.execute("SELECT * FROM conflict_cards WHERE id = ?", (cid,)).fetchone() \
            if cid is not None else None
        if card is None:
            return None
        return _pack(db_path, conn, card, it["id"])
    finally:
        conn.close()


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def html_block(db_path: Path, it: dict, hidden: str, base: str, pending: bool) -> str:
    """The review-page card. Memory text is escaped. Wide screens put A and B side by side."""
    conn = _connect(db_path)
    try:
        cid = json.loads(it.get("payload") or "{}").get("card_id")
        card = conn.execute("SELECT * FROM conflict_cards WHERE id = ?", (cid,)).fetchone() \
            if cid is not None and available(conn) else None
        if card is None:
            return f"<p><strong>{_e(it.get('summary'))}</strong></p>"
        view = _view(conn, card, it.get("project") or "", it["id"], "")
    finally:
        conn.close()
    sides = []
    for s in view["sides"]:
        evs = []
        for e in s["evidence"]:
            bit = e["label"] or ""
            if e.get("excerpt"):
                bit += (" — " if bit else "") + e["excerpt"]
            evs.append(f"<p class='ev'><b>Evidence: </b>{_e(bit or 'none')}</p>")
        if not evs:
            evs.append("<p class='ev'><b>Evidence: </b>none attached</p>")
        who = _e(s["who"]) + (f", {_e(s['when'])}" if s["when"] else "")
        sides.append(f"<div class='side'><p class='mark'>Claim {_e(s['mark'])}</p>"
                     f"<p class='who'>{who}</p><p class='claim'>{_e(s['claim'])}</p>"
                     + "".join(evs) + "</div>")
    body = (f"<div class='disagree'><h3>{_e(view['headline'])}</h3>"
            f"<div class='sides'>{''.join(sides)}</div>")
    if pending:
        buttons = []
        for keep, label in (("a", "Keep A"), ("b", "Keep B"), ("both", "Keep both")):
            buttons.append(f"<form method='post' action='{_e(base)}/approve'>{hidden}"
                           f"<input type='hidden' name='item_id' value='{it['id']}'>"
                           f"<input type='hidden' name='keep' value='{keep}'>"
                           f"<button>{label}</button></form>")
        body += f"<div class='keep-row'>{''.join(buttons)}</div>"
    elif view.get("detail"):
        body += f"<p class='done'>{_e(view['detail'])}</p>"
    return body + "</div>"


def phone_card(db_path: Path, item: dict) -> Optional[dict]:
    packed = ui_for_item(db_path, item)
    return packed["structured"] if packed else None


# ---------------------------------------------------------------- MCP Apps registration
def register(server) -> None:
    """The ui:// document and its reader. Listed so a host can prefetch it."""
    from mcp.server.lowlevel.helper_types import ReadResourceContents
    from mcp.types import Resource

    from .conflict_card_ui import CARD_HTML, MIME, UI_META, UI_URI

    @server.list_resources()
    async def list_resources():
        res = Resource(uri=UI_URI, name="Disagreement card",
                       description="The in-chat card for two entries that disagree.",
                       mimeType=MIME)
        return [res.model_copy(update={"meta": UI_META})]

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != UI_URI:
            raise ValueError(f"Unknown resource: {uri}")
        return [ReadResourceContents(CARD_HTML, MIME, UI_META)]


def app_tool():
    """Hidden from the model (visibility app). Hosts that advertise MCP Apps still receive it."""
    from mcp.types import Tool

    from .conflict_card_ui import UI_URI
    tool = Tool(
        name="resolve_conflict_card",
        description="Save the user's tap on a disagreement card (Keep A, Keep B or Keep both). "
                    "Apps call this from the card. A person decides; chat text is refused. "
                    "Returns the saved outcome.",
        inputSchema={"type": "object", "properties": {
            "item_id": {"type": "integer", "description": "The review item the card is showing."},
            "keep": {"type": "string", "enum": ["a", "b", "both"],
                     "description": "a keeps the older entry, b the newer, both keeps both."},
            "project": {"type": "string", "description": "The project name the card is showing."},
        }, "required": ["item_id", "keep", "project"]})
    return tool.model_copy(update={"meta": {"ui": {"resourceUri": UI_URI, "visibility": ["app"]}}})


def client_has_ui() -> bool:
    try:
        from .server import server
        caps = server.request_context.session.client_params.capabilities
    except Exception:  # noqa: BLE001 - no session, or a client that sent no extensions
        return False
    extra = getattr(caps, "model_extra", None) or {}
    ext = extra.get("extensions") or {}
    return isinstance(ext, dict) and UI_EXT in ext


def with_app_tool(tools: list) -> list:
    if not client_has_ui():
        return tools
    return list(tools) + [app_tool()]


def paint(result, ui):
    """Attach the card resource to a tool result. `ui` is {'structured': <card dict>}."""
    if not isinstance(ui, dict) or not ui.get("structured"):
        return result
    from .conflict_card_ui import UI_URI
    return result.model_copy(update={
        "structuredContent": ui["structured"],
        "meta": {"ui": {"resourceUri": UI_URI}},
    })
