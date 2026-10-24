"""In-chat review cards for what waits in the review queue (2.4.4).

The disagreement card (conflict_cards.py) for every other kind: session summaries,
suggestions, imports, clean-ups, markdown edits, possible conflicts, settings, "did you say
this?" checks and deletes. Each card says what the item is, what saving it would do, which app
wrote it and when, with Approve and Reject (Reject asks for a short optional reason in the
card itself). Disagreements keep their own card: show_review_card points to
show_conflict_card for them.

The same two tools as the disagreement card, built the same way and listed only to hosts that
advertise MCP Apps (conflict_cards.with_app_tool): show_review_card (model-visible, read-only;
its definition names ui://flowstate/review-card.html) and resolve_review_card (app-only, the
buttons). The safety rules are the disagreement card's:

  - resolve_review_card refuses chat text (user_request, message, text, reason; only the card's
    own reject_reason is taken) and clients without MCP Apps (not_a_card_tap);
  - through the relay with chat approvals off (the default) a tap is phone only
    (waiting.phone_only);
  - an item only the user decides (approvals.user_only: a delete or turning review off queued
    with approvals on, every "did you say this?" check), and every delete, is shown without
    buttons, with the phone or review-page link, and refused if resolve is called anyway;
  - a decision goes through the same apply functions as manage_review (review_apply.py) and
    is logged in approval_log with path "card", whatever the kind.

Card taps are not cryptographically proven to be a person, as with the disagreement card.
"""

import json
from pathlib import Path
from typing import Callable, Optional

from . import approvals, review
from .conflict_cards import _connect, _pretty, client_has_ui

SHOW_MAX = 5
REASON_MAX = 300
DETAILS_MAX = 600
DEFAULT_REASON = "Rejected on the review card."
_SUGGESTED = {"decision": "decision", "change": "change", "problem": "problem",
              "learning": "learning", "todo": "todo"}
NOT_TAP = "A review card is decided by its buttons, not by chat text."


def _payload(it: dict) -> dict:
    try:
        p = json.loads(it.get("payload") or "{}")
        return p if isinstance(p, dict) else {}
    except ValueError:
        return {}


def locked(it: dict) -> bool:
    """No buttons in the chat: only the user decides it (approvals.user_only), and every
    delete (a whole project needs its name typed; the review page and phone ask for that)."""
    return approvals.user_only(it) or it.get("kind") == "delete"


def _cut(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _who(it: dict) -> str:
    app = it.get("app")
    if app:
        return str(app)
    src = str(it.get("source") or "")
    if src.startswith("import:"):
        return "an import from " + (src.split(":", 1)[1] or "a file")
    return src or "an app"


def _sentence(text: str) -> str:
    text = (text or "").strip()
    return (text[:1].upper() + text[1:]) if text else text


def _words(it: dict, project: str, db_path: Path) -> dict:
    """title, kind label, what approving / rejecting does, and the two button labels."""
    kind, p = it["kind"], _payload(it)
    reject = "Reject marks it rejected and saves nothing; it stays listed as rejected."
    if kind == "session_summary":
        return {"title": review.KEEP_QUESTION, "kind_label": "Session summary",
                "does": review.KEEP_HOW, "approve_label": review.KEEP,
                "reject_label": review.DONT_KEEP}
    if kind in _SUGGESTED:
        what = _SUGGESTED[kind]
        src = str(it.get("source") or "")
        imported = src.startswith("import:") or p.get("imported_from")
        title = f"Save this imported {what}?" if imported else f"Save this suggested {what}?"
        return {"title": title, "kind_label": ("Imported " if imported else "Suggested ") + what,
                "does": f"Approve saves it in {project} as a {what}, as written. {reject}",
                "approve_label": "Approve", "reject_label": "Reject"}
    if kind == "cleanup":
        from . import cleanup
        return {"title": _sentence(cleanup.ask(p, project, db_path)) + "?",
                "kind_label": "Clean-up idea",
                "does": "Approve does it, after a safety copy (undo with the edit history). "
                        "Reject leaves everything as it is.",
                "approve_label": "Approve", "reject_label": "Reject"}
    if kind == "edit":
        return {"title": "Apply this edit from a markdown file?", "kind_label": "Markdown edit",
                "does": "Approve changes the record as shown, after a safety copy (undo with "
                        "the edit history). Reject leaves the record as it is.",
                "approve_label": "Approve", "reject_label": "Reject"}
    if kind == "conflict":
        return {"title": "Do these two decisions really conflict?",
                "kind_label": "Possible conflict",
                "does": "They conflict: both stay flagged in the brief until one replaces the "
                        "other. They fit together: the flag is dismissed. Neither decision is "
                        "changed.",
                "approve_label": "They conflict", "reject_label": "They fit together"}
    label = {"setting": "Review setting", "source": "Did you say this?",
             "delete": "Delete request"}.get(kind, kind.replace("_", " ").capitalize())
    return {"title": _sentence(approvals.describe(it, project, db_path)) + "?",
            "kind_label": label, "does": "", "approve_label": "Approve", "reject_label": "Reject"}


def _where(db_path: Path, it: dict) -> tuple:
    """(link, note) for an item the user decides outside the chat."""
    from . import waiting
    try:
        link = approvals.phone_link(db_path, it["id"])
    except Exception:  # noqa: BLE001 - no phone link
        link = None
    if link:
        return link, "Only you decide this one, on your phone, not in the chat:"
    try:
        ways = waiting.links(db_path)
    except Exception:  # noqa: BLE001 - no links, the note still says where
        ways = {}
    if ways.get("review_page"):
        return ways["review_page"], ("Only you decide this one, on the review page on this "
                                     "computer, not in the chat:")
    if ways.get("phone"):
        return ways["phone"], "Only you decide this one, on your phone, not in the chat:"
    return "", (f"Only you decide this one, not in the chat: {waiting.ELSEWHERE}, or with "
                f"`flowstate approve {it['id']}` in a terminal.")


def _outcome(it: dict) -> str:
    if it["status"] == "approved":
        if it["kind"] == "session_summary":
            return "Kept as the session's record."
        rid = it.get("result_id")
        return "Approved" + (f": saved as #{rid}." if rid is not None and it["kind"] in _SUGGESTED
                             else ".")
    reason = _cut(it.get("reject_reason"), 200)
    if reason and reason != DEFAULT_REASON:
        return f"Rejected: {reason if reason.endswith(('.', '!', '?')) else reason + '.'} " \
               "Nothing was deleted."
    return "Rejected. Nothing was deleted."


def view(db_path: Path, it: dict, project: str) -> dict:
    """What the card shows for one review item (also the plain text, via plain())."""
    from .review import strip_tag
    words = _words(it, project, db_path)
    out = {"item_id": it["id"], "project": project, "kind": it["kind"],
           "text": _cut(strip_tag(it.get("summary")), 500),
           "details": _cut(it.get("details"), DETAILS_MAX), "who": _who(it),
           "when": _pretty(it.get("created_at")), "resolved": it["status"] != "pending",
           "decide_here": False, "link": "", "locked_note": "", "detail": "", **words}
    if out["resolved"]:
        out["detail"] = _outcome(it)
    elif locked(it):
        out["link"], out["locked_note"] = _where(db_path, it)
    else:
        out["decide_here"] = True
    return out


def plain(v: dict) -> str:
    lines = [v["title"], f"{v['kind_label']} #{v['item_id']} in {v['project']}: {v['text']}"]
    if v.get("details"):
        lines.append(v["details"])
    lines.append("From " + v["who"] + (f", {v['when']}" if v.get("when") else ""))
    if v.get("does"):
        lines.append(v["does"])
    if v["resolved"]:
        lines.append(v["detail"])
    elif not v["decide_here"]:
        lines.append(v["locked_note"] + (f" {v['link']}" if v.get("link") else ""))
    else:
        lines.append(f"Buttons: {v['approve_label']} / {v['reject_label']}.")
    return "\n".join(lines)


def _items(conn, where: str, params: list, limit: Optional[int] = None) -> list:
    sql = (f"SELECT r.*, p.name AS project FROM review_items r JOIN projects p ON p.id = r.project_id "
           f"WHERE {where} ORDER BY r.id" + (" LIMIT ?" if limit else ""))
    return [dict(r) for r in conn.execute(sql, (*params, limit) if limit else params)]


# ---------------------------------------------------------------- pointers in other answers
def show_call(project: str, item_ids: list) -> str:
    ids = sorted({int(i) for i in item_ids if i is not None})
    one = f", item_id={ids[0]}" if len(ids) == 1 else ""
    return f'show_review_card(project="{project}"{one})'


def pointer(project: Optional[str], item_ids: list) -> dict:
    """{show_card, card_note} for answers that list waiting non-disagreement items, on MCP Apps
    hosts only (elsewhere the text is all there is)."""
    if not item_ids or not project or not client_has_ui():
        return {}
    call = show_call(project, item_ids)
    return {"show_card": call,
            "card_note": f"This app can show what's waiting as cards the user taps (Approve or "
                         f"Reject): call {call} now if they want to decide here. Don't choose "
                         f"for them."}


def both(conflict: dict, rest: dict) -> dict:
    """Both kinds wait: both calls, one short note."""
    if not (conflict and rest):
        return conflict or rest
    calls = [conflict["show_card"], rest["show_card"]]
    return {"show_card": calls,
            "card_note": f"This app can show these as cards the user taps: call {calls[0]} for the "
                         f"disagreement(s) and {calls[1]} for the rest. Don't choose for them."}


def brief_hint(conn, pid: int, project: str, card_hint: Optional[str] = None) -> Optional[str]:
    """On MCP Apps hosts, when non-disagreement items wait in the project: how many, and the
    call that shows them as cards. `card_hint` is the disagreement card's own hint
    (conflict_cards.brief_hint); when both wait, one line names both calls."""
    if not client_has_ui() or not review.available(conn):
        return card_hint
    n = conn.execute("SELECT COUNT(*) FROM review_items WHERE project_id = ? AND status = 'pending' "
                     "AND kind != 'disagreement'", (pid,)).fetchone()[0]
    if not n:
        return card_hint
    items = f"{n} item{'s' if n != 1 else ''}"
    call = show_call(project, [])
    if card_hint:
        dis, k = conflict_call(conn, pid, project)
        return (f"Cards waiting: call {dis} for the disagreement{'s' if k > 1 else ''} and "
                f"{call} for the other {items}, to show them in the chat.")
    return (f"Review cards: {items} wait{'s' if n == 1 else ''} for the user; call {call} to "
            f"show them as cards in the chat.")


def conflict_call(conn, pid: int, project: str) -> tuple:
    from . import conflict_cards
    ids = [r[0] for r in conn.execute("""SELECT review_item_id FROM conflict_cards WHERE project_id = ?
                                         AND status = 'open' AND review_item_id IS NOT NULL""", (pid,))]
    return conflict_cards.show_call(project, ids), len(ids)


# ---------------------------------------------------------------- show_review_card
def show(db_path: Path, args: dict, pid: Optional[int]) -> dict:
    """One item (item_id, any status) or up to SHOW_MAX waiting ones, oldest first (in a
    project, else all). Disagreements are left to show_conflict_card, with a pointer."""
    from . import conflict_cards
    if args.get("project") not in (None, "") and pid is None:
        return {"error": "project_not_found", "message": f"No single project matches "
                f"{args.get('project')!r}."}
    conn = _connect(db_path)
    try:
        if not review.available(conn):
            return {"error": "review_queue_unavailable", "message": "The review queue isn't set "
                    "up in this database yet."}
        dis_ids, dis_project = [], None
        if args.get("item_id") is not None:
            try:
                item_id = int(args["item_id"])
            except (TypeError, ValueError):
                return {"error": "bad_request", "message": "item_id must be a number."}
            rows = _items(conn, "r.id = ?", [item_id])
            if not rows or (pid is not None and rows[0]["project_id"] != pid):
                return {"error": "record_not_found",
                        "message": f"No review item #{item_id}" + (" in this project." if pid else ".")}
            if rows[0]["kind"] == "disagreement":
                call = conflict_cards.show_call(rows[0]["project"], [item_id])
                note = f"Review item #{item_id} is a disagreement; it has its own card: call {call}."
                return {"shown": 0, "disagreement": item_id, "show_card": call, "note": note,
                        "_ui": {"structured": {"cards": [], "note": "This is a disagreement: it "
                                               "has its own card."}}}
            total = 1
        else:
            where, params = "r.status = 'pending'", []
            if pid is not None:
                where, params = where + " AND r.project_id = ?", [pid]
            rest = where + " AND r.kind != 'disagreement'"
            total = conn.execute(f"SELECT COUNT(*) FROM review_items r WHERE {rest}", params).fetchone()[0]
            rows = _items(conn, rest, params, SHOW_MAX)
            dis = _items(conn, where + " AND r.kind = 'disagreement'", params)
            if dis:
                dis_project = dis[0]["project"]
                dis_ids = [d["id"] for d in dis if d["project"] == dis_project]
    finally:
        conn.close()
    views = [view(db_path, it, it["project"]) for it in rows]
    out: dict = {"shown": len(views)}
    if views:
        out["review_cards"] = [{"review_item_id": v["item_id"], "text": plain(v)} for v in views]
    if total > len(views):
        out["more"] = total - len(views)
    structured = {"cards": views, "more": out.get("more", 0)}
    if dis_ids:
        call = conflict_cards.show_call(dis_project, dis_ids)
        n = len(dis_ids)
        out["disagreements"] = {"waiting": n, "show_card": call}
        structured["also"] = (f"{n} disagreement{'s' if n > 1 else ''} also wait"
                              f"{'' if n > 1 else 's'} on {'their' if n > 1 else 'its'} own card.")
    open_ = [v for v in views if not v["resolved"]]
    if not views:
        out["note"] = structured["note"] = "Nothing else is waiting for the user's OK." if dis_ids \
            else "Nothing is waiting for the user's OK."
    elif any(v["decide_here"] for v in open_) and client_has_ui():
        out["note"] = ("The cards are on screen: the user taps Approve or Reject. Don't choose "
                       "for them or ask them to type it.")
    elif open_:
        out["note"] = "Show the user this text; they decide on the review page or their phone."
    else:
        out["note"] = "Already decided."
    if dis_ids:
        out["note"] += f" A disagreement waits too: call {out['disagreements']['show_card']}."
    out["_ui"] = {"structured": structured}
    return out


# ---------------------------------------------------------------- resolve_review_card (the buttons)
def resolve_card(db_path: Path, args: dict, pid: Optional[int], client: str,
                 decide: Callable[[dict, str, str], dict]) -> dict:
    """A tap on Approve or Reject. `decide(item, decision, reason)` runs the same approve /
    reject as manage_review (review_apply.for_card). Not a chat sentence, not
    for clients without MCP Apps, phone only through the relay unless chat approvals are on,
    never for an item only the user decides elsewhere. Logged with path "card"."""
    for key in ("user_request", "message", "text", "reason"):
        if args.get(key) not in (None, ""):
            return {"error": "not_a_card_tap", "message": NOT_TAP}
    decision = args.get("decision")
    if decision not in ("approve", "reject"):
        return {"error": "decision_required", "message": 'decision is "approve" or "reject".'}
    if args.get("item_id") is None:
        return {"error": "item_id_required", "message": "Name the review item (item_id)."}
    why = args.get("reject_reason")
    if why not in (None, "") and (not isinstance(why, str) or decision != "reject"):
        return {"error": "bad_request", "message": "reject_reason is a short text, with reject only."}
    why = " ".join((why or "").split())[:REASON_MAX]
    if not client_has_ui():   # no MCP Apps host, no card: the call can only be the model's
        return {"error": "not_a_card_tap", "message": "This app can't show review cards. The "
                "user decides on the review page or their phone."}
    from . import waiting
    try:
        item_id = int(args["item_id"])
    except (TypeError, ValueError):
        return {"error": "bad_request", "message": "item_id must be a number."}
    no = waiting.phone_only(db_path, item_id)   # relay: the same phone-only rule
    if no:
        return no
    it = waiting._item_view(db_path, item_id)
    if not it or (pid is not None and it["project_id"] != pid):
        return {"error": "record_not_found", "message": f"No review item #{item_id}"
                + (" in this project." if pid is not None else ".")}
    if it["kind"] == "disagreement":
        from . import conflict_cards
        return {"error": "use_conflict_card", "message": "This is a disagreement: it has its own "
                "card (" + conflict_cards.show_call(it["project"], [item_id]) + ")."}
    if it["status"] != "pending":
        return {"error": "already_decided", "message": f"Review item #{item_id} is already "
                f"{it['status']}."}
    if locked(it):
        link, note = _where(db_path, it)
        return {"error": "user_only", "message": f"Nothing was decided on review item #{item_id}. "
                + note + (f" {link}" if link else ""), **({"link": link} if link else {})}
    out = decide(it, decision, why or DEFAULT_REASON)
    if isinstance(out, dict) and "error" in out:
        return out
    approvals.log_decision(db_path, it, "card", client,
                           "approved" if decision == "approve" else "rejected")
    fresh = waiting._item_view(db_path, item_id) or it
    v = view(db_path, fresh, fresh["project"])
    text = plain(v)
    res = {"resolved": item_id, "decision": decision, "text": text, "note": v["detail"] or text,
           "review_cards": [{"review_item_id": item_id, "text": text}],
           "_ui": {"structured": {"cards": [v]}}}
    res.update({k: val for k, val in (out or {}).items() if k not in res})
    return res


# ---------------------------------------------------------------- tool definitions
def app_tool():
    """Hidden from the model (visibility app). Hosts that advertise MCP Apps still receive it."""
    from mcp.types import Tool

    from .review_card_ui import UI_URI
    tool = Tool(
        name="resolve_review_card",
        description="Save the user's tap on a review card (Approve, or Reject with an optional "
                    "reason typed in the card). Apps call this from the card. A person decides; "
                    "chat text is refused. Returns the saved outcome.",
        inputSchema={"type": "object", "properties": {
            "item_id": {"type": "integer", "description": "The review item the card is showing."},
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "project": {"type": "string", "description": "The project name the card is showing."},
            "reject_reason": {"type": "string", "maxLength": REASON_MAX,
                              "description": "Reject only: the reason typed in the card."},
        }, "required": ["item_id", "decision", "project"]})
    return tool.model_copy(update={"meta": {"ui": {"resourceUri": UI_URI, "visibility": ["app"]}}})


def show_tool():
    """Model-visible and read-only; its definition names the card's ui:// resource."""
    from mcp.types import Tool, ToolAnnotations

    from .review_card_ui import UI_URI
    tool = Tool(
        name="show_review_card",
        title="Show review cards",
        description="Show the user what waits in the review queue as cards in this chat (session "
                    "summaries, suggestions, imports, clean-ups...), so they tap Approve or Reject "
                    "themselves. Call it when a FlowState answer has show_card naming it, or when "
                    "the user asks to see what's waiting. Read-only: you never choose. item_id: "
                    f"one item; without it, the project's waiting items (oldest first, at most "
                    f"{SHOW_MAX}). Disagreements use show_conflict_card. Returns the card text; "
                    "the app draws the cards.",
        inputSchema={"type": "object", "properties": {
            "project": {"type": ["string", "integer"], "description": "Name or id."},
            "item_id": {"type": "integer", "description": "One review item's id."},
        }},
        annotations=ToolAnnotations(title="Show review cards", readOnlyHint=True,
                                    idempotentHint=True, openWorldHint=False))
    return tool.model_copy(update={"meta": {"ui": {"resourceUri": UI_URI,
                                                   "visibility": ["model", "app"]}}})
