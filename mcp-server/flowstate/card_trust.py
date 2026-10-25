"""Who may use the in-chat cards, and how a card tap is recorded (task 55, re-audit N-F1).

The card list (approvals.card_clients) says which apps' card taps count as the user's. What is
checked against it depends on how the app connected:

  - stdio (the app that started this FlowState process, e.g. Claude Desktop): the MCP client
    name the app sends. That name is the app's own say-so; an app the user runs on this
    computer could lie about it. Accepted limit, as for popups (see conflict_cards.py).
  - over the network (a LAN key, a remote token on the LAN, tunnel or relay, an OAuth app):
    the key's / token's / OAuth app's own name (identity.Identity.credential), never the name
    the app sends, which it can set to anything ("claude-ai"). Only a name the user added
    themselves counts (`flowstate approvals cards add NAME` or the review page's Settings),
    not the built-in default: an OAuth app picks its own name when it registers.
  - the shared LAN token, or an HTTP call without a sign-in: no cards. The shared token is one
    secret for every app, so it can't say which app taps.

The tap codes (card_taps.py) are bound to the same caller (key()), and a tap is recorded with
label(): "card (<app>)", plus "signed in as <credential>" over the network, everywhere a card
decision is written (decided_by, approval_log, a session summary's per-record decisions).
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

NETWORK_SOURCES = ("app_key", "oauth_client", "remote_token")
LABEL_MAX = 80


@dataclass(frozen=True)
class CardCaller:
    sent: str                  # the MCP client name the app sent (stdio: what is checked)
    app: str                   # the app's name as FlowState records it (identity.py)
    credential: Optional[str]  # the key's / token's / OAuth app's own name; None on stdio
    source: str                # "stdio", or the identity source of the sign-in

    def key(self) -> str:
        """Who a tap code is bound to (card_taps.py lower-cases it)."""
        if self.credential is None:
            return self.sent
        return f"{self.source}|{self.credential}"

    def shown(self) -> str:
        """The app, and the sign-in it used: "claude-ai (signed in as Laptop key)"."""
        if self.credential is None:
            return self.app
        return f"{self.app} (signed in as {self.credential})"

    def label(self) -> str:
        """How a tap is recorded as the decider: "card (claude-ai)" /
        "card (claude-ai, signed in as Laptop key)", the sign-in's name kept whole."""
        if self.credential is None:
            return f"card ({self.app})"[:LABEL_MAX]
        tail = f", signed in as {self.credential})"
        room = max(8, LABEL_MAX - len("card (") - len(tail))
        return f"card ({self.app[:room]}{tail}"[:LABEL_MAX]


def caller() -> Optional[CardCaller]:
    """This connection as the cards see it; None outside a request, and for the shared LAN
    token or an HTTP call without a sign-in (no cards for those)."""
    from . import identity
    from .server import _client_name, _identity, server
    try:
        req = server.request_context.request
    except LookupError:
        return None
    sent = _client_name()
    if req is None:   # stdio
        return CardCaller(sent, identity._app_name(sent) or "unknown", None, "stdio")
    ident = _identity(sent, {})
    if ident.identity_source not in NETWORK_SOURCES or not ident.credential:
        return None
    return CardCaller(sent, ident.app, ident.credential, ident.identity_source)


def allowed(db_path: Optional[Path], who: Optional[CardCaller]) -> bool:
    """May this caller get the cards (and their app-only tap tools)? See the module text."""
    from . import approvals
    if who is None:
        return False
    if who.credential is None:
        return approvals.card_allowed(db_path, who.sent)
    return approvals.card_allowed_credential(db_path, who.credential)


def key(fallback: Optional[str]) -> str:
    who = caller()
    return who.key() if who is not None else str(fallback or "")


def shown(fallback: Optional[str]) -> str:
    who = caller()
    return who.shown() if who is not None else str(fallback or "an app")


def label(fallback: Optional[str]) -> str:
    """The decider label of a card tap; outside a request (FlowState's own calls, tests)
    "card (<fallback>)"."""
    who = caller()
    if who is not None:
        return who.label()
    return f"card ({fallback or 'unknown'})"[:LABEL_MAX]


def defused(result, card: bool = True):
    """A card tool's answer as the model gets it (re-audit N-F8): its text part defused and
    marked like every other answer (stored.py, audit D6); only the card's own data (`_ui`:
    structuredContent and the tap codes in _meta, for the card's page, which writes every text
    with textContent) stays as stored."""
    from . import stored
    ui = result.pop("_ui", None) if isinstance(result, dict) else None
    result = stored.defuse_all(stored.mark("", result))
    if card and isinstance(result, dict):
        if "error" not in result and "records_are" not in result:
            result = {"records_are": stored.NOTE, **result}
        if ui is not None:
            result["_ui"] = ui
    return result
