"""One-use tap codes for the in-chat cards (2.4.4).

An app that says it shows MCP Apps cards gets the app-only resolve tools, and the model must
never be able to use them for the user. So a card's buttons only work with a code that only
the card's page receives:

  - show_review_card / show_conflict_card (and a record tap's answer, which redraws the card)
    make one code per card that has buttons. The codes go in the tool result's `_meta`
    (META_KEY), which MCP Apps hosts pass to the card's page and keep out of the model's
    context; never in the text, never in structuredContent;
  - the card sends its code back with the tap (`tap`); resolve_review_card /
    resolve_conflict_card refuse a tap without a valid one (not_a_card_tap);
  - a code is bound to the item, the kind of card and the app that was shown it, works once
    and expires after TTL seconds. Only a hash is kept, in this process's memory.

Honest limit: a program that pretends to be a card host (it connects with a name on the card
list and reads `_meta` itself) gets the codes too; the card list (approvals.card_clients)
is what keeps other apps out, as the popup list does for popups.
"""

import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

META_KEY = "flowstate/taps"
TTL = 15 * 60          # seconds: a card left open longer asks to be shown again
MAX_CODES = 500        # kept at once; the oldest go first
_codes: dict = {}      # sha256(code) -> (item_id, card, client, expires)
_spent: dict = {}      # sha256(code) -> (item_id, card, client): good codes used by a tap now
_lock = threading.Lock()
_clock = time.monotonic


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _client(name: Optional[str]) -> str:
    return " ".join(str(name or "").split()).lower()


def mint(item_id: int, card: str, client: Optional[str]) -> str:
    """A fresh code for one card ("review" or "conflict") shown to `client`."""
    code = secrets.token_urlsafe(24)
    now = _clock()
    with _lock:
        for k in [k for k, v in _codes.items() if v[3] <= now]:
            del _codes[k]
        while len(_codes) >= MAX_CODES:
            del _codes[next(iter(_codes))]
        _codes[_hash(code)] = (int(item_id), card, _client(client), now + TTL)
    return code


def redeem(code, item_id, card: str, client: Optional[str]) -> bool:
    """Use up `code` for this tap. True only for a live code made for this item, card and app.
    A code is gone after one try, right or wrong for this tap."""
    if not isinstance(code, str) or not code or len(code) > 200:
        return False
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    with _lock:
        got = _codes.pop(_hash(code), None)
    if got is None:
        return False
    want = (item_id, card, _client(client))
    ok = got[3] > _clock() and hmac.compare_digest(repr(got[:3]), repr(want))
    if ok:
        with _lock:
            _spent[_hash(code)] = want
            while len(_spent) > MAX_CODES:
                del _spent[next(iter(_spent))]
    return ok


def again(result, args: dict, card: str, client: Optional[str]):
    """A tap whose code was good but which was refused afterwards (already decided elsewhere,
    the phone-only rule, a failed delete...) gets a fresh code, so the user can tap again."""
    code = args.get("tap") if isinstance(args, dict) else None
    if not isinstance(code, str):
        return result
    with _lock:
        spent = _spent.pop(_hash(code), None)
    if spent is None or not (isinstance(result, dict) and "error" in result):
        return result
    ui = dict(result.get("_ui") or {})
    ui["taps"] = {**(ui.get("taps") or {}), str(spent[0]): mint(spent[0], card, client)}
    return {**result, "_ui": ui}


def for_cards(item_ids, card: str, client: Optional[str]) -> dict:
    """{str(item_id): code} for the cards that have buttons."""
    return {str(int(i)): mint(i, card, client) for i in item_ids}


REFUSED = ("This tap didn't come from a card FlowState showed in this app just now (or the "
           "card was open too long). Nothing was decided. Show the card again, or decide on "
           "the review page or your phone.")


def clear() -> None:
    """Tests: forget every code."""
    with _lock:
        _codes.clear()
        _spent.clear()
