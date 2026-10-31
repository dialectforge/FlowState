"""The one-tap offer "Allow tap cards in <app>?" for an app on a network sign-in (task 65b).

An app that signed in over the network (an OAuth app, a key or a token, card_trust.py) and
says it draws MCP Apps cards gets them only once its sign-in's own name is on the user's card
sign-in list (approvals.card_allowed_credential, task 55). The first time such an app would
be shown a card and isn't on the list, it gets the text card as before, and FlowState queues
one review item of kind `card_offer` in that project: "Allow tap cards in <app>?", with the
app and the sign-in it used (card_trust shown()).

  - Once per sign-in: its name goes on the "already offered" list (approval_settings
    `card_offered_add`) when the offer is queued, so there is never a second one while it
    waits, after the user's No, or after the user takes the cards away again (approvals.
    change_signins puts a removed sign-in there too). The user offers again from the review
    page's Settings or with `flowstate approvals cards offer-again NAME`; `flowstate
    approvals cards offers off` stops new offers altogether.
  - The user's alone, and only on the review page (this computer's, or the tunnel review
    page) or the phone review page: refusal() turns away every other way, every AI tool
    (chat approvals on or off), the chat walk, a popup, a card tap and the terminal. A card
    has no buttons for it (approvals.user_only).
  - Allow puts the sign-in's own name (stored in the item when FlowState queued it, never
    the name the app sends) on the card sign-in list, the same list `flowstate approvals
    cards add --sign-in` and Settings change; the decision goes to approval_log and the
    change to the settings log like every other settings change. Taking it away again is the
    existing remove (Settings, `approvals cards remove --sign-in NAME`).
"""

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from . import approvals

KIND = "card_offer"
OFFERED = "card_offered"     # approval_settings card_offered_add: sign-ins already offered
OFFERS = "card_offers"       # approval_settings: "off" = no new offers
# the only deciders of an offer: the review page's, the tunnel page's and the phone pages' own
# labels (review_page.CLIENT, review_tunnel.CLIENT, approve_page / review_phone "phone")
DECIDERS = ("review-page", "tunnel page", "phone")
SETTING_WORDS = "allowed on the offer in the review queue"

REFUSED = ("Only the user answers \"Allow tap cards in this app?\", and only on their review "
           "page (on the computer where FlowState runs: the FlowState review shortcut or "
           "`flowstate review-page`) or their phone review page. Nothing was decided on review "
           "item #{id}; don't try again. Tell the user once, if they want cards in this app.")
QUEUED = ("This app can draw FlowState's in-chat review cards, but the user hasn't allowed "
          "them for it, so the text is the card here. FlowState has asked the user once, on "
          "their review page and phone review page: \"Allow tap cards in {app}?\" (review item "
          "#{id}). Only they answer it; tell them once if they want to tap Approve / Reject in "
          "this chat, and don't decide it for them.")


def _clean(name) -> str:
    return approvals.clean_client_name(name)


def _base(name: str, source: Optional[str]) -> str:
    """An OAuth app that registered again is "Claude (2)": the same place as "Claude" (as on
    the sign-in list, approvals._signin_listed)."""
    m = re.fullmatch(r"(.+) \((\d{1,3})\)", name)
    return m.group(1) if source == "oauth_client" and m else name


def offers_on(db_path: Optional[Path]) -> bool:
    if db_path is None:
        return False
    conn = approvals._conn(db_path)
    try:
        return not approvals.available(conn) or approvals._get(conn, OFFERS) != "off"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def offered(db_path: Optional[Path]) -> set:
    """The sign-in names already offered (lower case): no new offer for these."""
    return approvals._listed(db_path, (), OFFERED)


def was_offered(db_path: Optional[Path], credential: Optional[str],
                source: Optional[str] = None) -> bool:
    name = _clean(credential)
    done = offered(db_path)
    return name in done or _base(name, source) in done


def mark_offered(db_path: Path, name: str, offered_: bool, who: str = "command-line") -> set:
    """Put a sign-in on the "already offered" list, or take it off (offer again)."""
    return approvals._change_list(db_path, (), OFFERED, name, offered_, who)


def set_offers(db_path: Path, on: bool, who: str = "command-line") -> None:
    approvals.set_value(db_path, OFFERS, None if on else "off", who)


def _project_id(conn, project) -> Optional[int]:
    if project is None:
        return None
    if isinstance(project, int):
        row = conn.execute("SELECT id FROM projects WHERE id = ?", (project,)).fetchone()
    else:
        row = conn.execute("SELECT id FROM projects WHERE name = ? COLLATE NOCASE",
                           (str(project),)).fetchone()
    return row[0] if row else None


def pending_for(conn, name: str) -> Optional[int]:
    row = conn.execute("SELECT id, payload FROM review_items WHERE kind = ? AND status = 'pending' "
                       "ORDER BY id", (KIND,)).fetchall()
    for r in row:
        try:
            if _clean(json.loads(r[1] or "{}").get("credential")) == name:
                return r[0]
        except ValueError:
            continue
    return None


def offer(db_path: Optional[Path], project) -> Optional[dict]:
    """Called wherever a card would be shown (the cards' pointers and brief lines). For an app
    on a network sign-in that says it draws cards but isn't on the card sign-in list: queue the
    offer once, and say so ({"item_id", "app", "queued"}). None for every other caller, when
    offers are off, or when this sign-in was offered before and nothing waits."""
    from . import card_trust, conflict_cards, review
    if db_path is None or not conflict_cards.ui_declared() or conflict_cards.client_has_ui():
        return None
    who = card_trust.caller()
    if who is None or who.credential is None:   # stdio, the shared token: never an offer
        return None
    name = _clean(who.credential)
    if not name or not offers_on(db_path):
        return None
    shown = " ".join(who.shown().split())[:160]
    conn = approvals._conn(db_path)
    try:
        if not approvals.available(conn) or not review.available(conn):
            return None
        conn.execute("BEGIN IMMEDIATE")   # one offer, whichever connection gets here first
        waiting = pending_for(conn, name)
        if waiting is not None:
            conn.commit()
            return {"item_id": waiting, "app": shown, "queued": False}
        done = json.loads(approvals._get(conn, f"{OFFERED}_add") or "[]")
        if name in done or _base(name, who.source) in done:
            conn.commit()
            return None
        pid = _project_id(conn, project)
        if pid is None:
            conn.commit()
            return None
        app = " ".join(str(who.app or "an app").split())[:80]
        item = review.add_item(
            db_path, pid, KIND, f"Allow tap cards in {shown}?",
            f"{app} can show FlowState's review cards in its chat, with Approve / Reject / Keep "
            f"buttons. Allow: a tap on them counts as your own decision, for this sign-in only "
            f"(\"{who.credential}\"). No: it keeps getting the text and your review link, and "
            f"isn't asked again. You can change this any time in Settings (\"Apps that may show "
            f"review cards\").",
            {"credential": who.credential, "source": who.source, "app": app, "shown": shown,
             "user_only": True}, who.sent, conn=conn)
        approvals._put(conn, f"{OFFERED}_add", json.dumps(sorted(set(done) | {name})), "offer")
        conn.commit()
        return {"item_id": item, "app": shown, "queued": True}
    except sqlite3.Error:
        conn.rollback()
        return None
    finally:
        conn.close()


def note(off: dict) -> str:
    return QUEUED.format(app=off["app"], id=off["item_id"])


def refusal(item: dict, client: str, agent: bool) -> Optional[dict]:
    """None when this decider may answer a waiting offer: the user on the review page, the
    tunnel review page or the phone page. Everything else is refused, AI tools included."""
    if item.get("kind") != KIND or item.get("status") != "pending":
        return None
    if not agent and str(client or "").lower().startswith(DECIDERS):
        return None
    return {"error": "user_only", "message": REFUSED.format(id=item["id"]), "item_id": item["id"],
            "kind": KIND, "status": "waiting for the user"}


def apply(db_path: Path, item: dict, client: str) -> tuple:
    """Allow: the sign-in FlowState stored with the offer goes on the card sign-in list."""
    p = json.loads(item.get("payload") or "{}")
    name = _clean(p.get("credential"))
    if not name:
        from .review import ReviewError
        raise ReviewError("bad_offer", "This offer names no sign-in; nothing changed.")
    before = approvals.signins(db_path, "card")
    approvals.change_signins(db_path, "card", name, True, client)
    approvals.log_setting(db_path, "card_signins", ", ".join(sorted(before)), f"added {name}",
                          client, SETTING_WORDS)
    return "approval_settings", None


def describe(item: dict) -> str:
    try:
        p = json.loads(item.get("payload") or "{}")
    except ValueError:
        p = {}
    return (f"let {p.get('shown') or p.get('app') or 'an app'} show tap cards in its chat "
            f"(its taps on Approve / Reject then count as yours)")
