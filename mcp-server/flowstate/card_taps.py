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
  - a code is bound to the item, the kind of card and the caller that was shown it (on
    stdio its client name; over the network its key's / token's / OAuth app's own name,
    card_trust.py), and expires after TTL seconds. Only a hash is kept, in this process's
    memory;
  - a tap only reserves its code (redeem); the code is used up when the tap is saved, and
    given back when the tap fails or is refused afterwards (settle), so a failed or timed-out
    tap doesn't leave a card whose buttons refuse everything. A reserved code can't be used
    by a second tap at the same time (use-once stays safe under races);
  - showing a card again replaces only that card's own older codes (PER_CARD at most, the
    oldest go), never other cards' live ones; MAX_CODES only bounds memory.

Honest limit: a program that pretends to be a card host (it connects on stdio with a name on
the card list, or with a key / token the user put on it, and reads `_meta` itself) gets the
codes too; the card list (approvals.card_clients) is what keeps other apps out, as the popup
list does for popups.
"""

import contextvars
import hashlib
import hmac
import secrets
import threading
import time
from typing import Optional

META_KEY = "flowstate/taps"
TTL = 15 * 60          # seconds: a card left open longer asks to be shown again
PER_CARD = 3           # live codes per card (item, kind, caller); showing it again drops the oldest
MAX_CODES = 5000       # kept at once in all (memory bound); the oldest idle ones go first
BUSY = "busy"
_codes: dict = {}      # sha256(code) -> [item_id, card, client, expires, reserved]
_lock = threading.Lock()
_clock = time.monotonic
_held: contextvars.ContextVar = contextvars.ContextVar("flowstate_tap_held", default=None)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _client(name: Optional[str]) -> str:
    return " ".join(str(name or "").split()).lower()


def _drop_oldest(keys: list) -> None:
    for k in keys:
        if not _codes[k][4]:   # a code a tap is using right now stays
            del _codes[k]
            return


def mint(item_id: int, card: str, client: Optional[str]) -> str:
    """A fresh code for one card ("review" or "conflict") shown to `client` (card_trust.key)."""
    code = secrets.token_urlsafe(24)
    now = _clock()
    who = (int(item_id), card, _client(client))
    with _lock:
        for k in [k for k, v in _codes.items() if v[3] <= now and not v[4]]:
            del _codes[k]
        same = [k for k, v in _codes.items() if tuple(v[:3]) == who and not v[4]]
        while len(same) >= PER_CARD:
            del _codes[same.pop(0)]
        while len(_codes) >= MAX_CODES and any(not v[4] for v in _codes.values()):
            _drop_oldest(list(_codes))
        _codes[_hash(code)] = [*who, now + TTL, False]
    return code


def redeem(code, item_id, card: str, client: Optional[str]):
    """Reserve `code` for this tap: True for a live code made for this item, card and caller
    (settle then uses it up or gives it back), BUSY while another tap holds it, else False."""
    if not isinstance(code, str) or not code or len(code) > 200:
        return False
    try:
        item_id = int(item_id)
    except (TypeError, ValueError):
        return False
    h = _hash(code)
    want = (item_id, card, _client(client))
    with _lock:
        got = _codes.get(h)
        if got is None:
            return False
        if got[3] <= _clock() and not got[4]:
            del _codes[h]
            return False
        if not hmac.compare_digest(repr(tuple(got[:3])), repr(want)):
            return False
        if got[4]:
            return BUSY
        got[4] = True
    _held.set(h)
    return True


def settle(result, ok: Optional[bool] = None):
    """After a tap: the code it reserved (if any) is used up when the tap was saved (`ok`, by
    default: the answer is not an error), else given back for the next tap. Returns `result`."""
    h = _held.get()
    if h is None:
        return result
    _held.set(None)
    if ok is None:
        ok = not (isinstance(result, dict) and "error" in result)
    with _lock:
        got = _codes.get(h)
        if got is not None:
            if ok:
                del _codes[h]
            else:
                got[4] = False
    return result


def resolve(fn, *a):
    """Run a card's tap `fn(*a)` and settle its code, also when it raises."""
    _held.set(None)
    try:
        result = fn(*a)
    except BaseException:
        settle(None, ok=False)
        raise
    return settle(result)


def for_cards(item_ids, card: str, client: Optional[str]) -> dict:
    """{str(item_id): code} for the cards that have buttons."""
    return {str(int(i)): mint(i, card, client) for i in item_ids}


REFUSED = ("This card has expired, or this tap didn't come from a card FlowState showed in "
           "this app. Nothing was decided. Ask the AI to show the card again, or decide on the "
           "review page or your phone.")
IN_PROGRESS = ("This card's last tap is still being saved. Nothing new was decided; wait a "
               "moment and look again.")


def clear() -> None:
    """Tests: forget every code."""
    with _lock:
        _codes.clear()
