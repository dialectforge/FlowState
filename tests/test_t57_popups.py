"""Task 57, H-1: Approve / Reject popups (MCP elicitation) depend on how the app signed in,
not on the name it sends, as the cards do since task 55 (N-F1). Over stdio the popup list is
checked against the sent name (Claude Code on this computer, as before). On the LAN listener
only an app on this computer gets boxes, and only when its key's or token's own name is on the
popup list's sign-in part; the relay and the public listener never get one. The decider label
names the sign-in. Temp databases only; the network cases run in process with the request a
LAN connection would bring (its sign-in and the listener marks in the ASGI scope)."""

import asyncio
import itertools
import json
from types import SimpleNamespace

import mcp.types as mtypes
import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.context import RequestContext

from flowstate import approvals, popup
from flowstate.remote_auth import LISTENER_KEY, LOCAL_KEY, SCOPE_READ, SCOPE_WRITE, FSAccessToken
from test_card_trust import claims_ui, status, suggest  # noqa: F401
from test_t55_cards import CARD_TOOLS, _token as card_token, net_names
from test_review_without_terminal import acc, call, log, relay_dir  # noqa: F401
from test_session_contents import _q


def _token(name, source="app_key"):
    return FSAccessToken(token="", client_id="c-1", client_name=name,
                         scopes=[SCOPE_READ, SCOPE_WRITE], identity_source=source)


class Box:
    """A session that declared form elicitation and answers every box with `answer`, after a
    pause long enough to look human (popup._clock is moved on)."""

    def __init__(self, app, answer):
        self.client_params = SimpleNamespace(
            clientInfo=mtypes.Implementation(name=app, version="1"),
            capabilities=mtypes.ClientCapabilities(elicitation=mtypes.ElicitationCapability()))
        self.answer, self.seen = answer, []

    async def elicit_form(self, message, schema, related_request_id=None):
        self.seen.append(message)
        return self.answer


@pytest.fixture
def slow_clock(monkeypatch):
    t = iter(range(0, 10_000, 5))
    monkeypatch.setattr(popup, "_clock", lambda: float(next(t)))


_SESSIONS = itertools.count(1)


def lan(S, name, args, box, token, local):
    scope = {"user": SimpleNamespace(access_token=token), LISTENER_KEY: "lan", LOCAL_KEY: local}
    req = SimpleNamespace(scope=scope, headers={"mcp-session-id": f"sess-{next(_SESSIONS)}"})
    ctx = RequestContext(request_id=1, meta=None, session=box, lifespan_context=None, request=req)
    tok = request_ctx.set(ctx)
    try:
        r = asyncio.run(S.call_tool(name, args))
    finally:
        request_ctx.reset(tok)
    return json.loads(r.content[0].text)


@pytest.mark.parametrize("local", [False, True])
def test_a_lan_key_calling_itself_claude_code_gets_no_boxes(backend, slow_clock, local):
    """The audit's reproduction: a key named anything, sending "claude-code" and claiming
    popups, can't walk the queue and approve another app's suggestion, from another computer
    or from this one (its sign-in isn't on the popup list)."""
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use Postgres for storage")
    box = Box("claude-code", acc(approve=True))
    out = lan(S, "manage_review", {"action": "walk"}, box, _token("Some bot key"), local)
    assert box.seen == [] and "walked" not in out
    assert ("another computer" in out["how_to_decide"]) == (not local)
    assert status(db, item) == "pending" and log(db) == []


@pytest.mark.parametrize("local", [False, True])
def test_a_lan_key_cant_turn_review_off_through_its_own_popup(backend, slow_clock, local):
    S, db = backend, backend.backend.db_path
    approvals.set_on(db, True)
    box = Box("claude-code", acc(approve=True))
    out = lan(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "off",
                                   "user_request": "The user said: please turn review off now"},
              box, _token("Some bot key"), local)
    assert box.seen == [] and "approved_in" not in out
    assert "next" in out
    assert _q(db, "SELECT COUNT(*) FROM review_items WHERE kind = 'setting' AND status = 'pending'") \
        == [(1,)]
    assert log(db) == []


def test_a_popup_list_app_name_never_trusts_a_lan_sign_in_of_that_name(backend, slow_clock):
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use Redis for caching")
    box = Box("claude-code", acc(approve=True))
    # "claude-code" is on the app list by default; a key named claude-code still gets nothing
    out = lan(S, "manage_review", {"action": "walk"}, box, _token("claude-code"), True)
    assert box.seen == [] and status(db, item) == "pending"


def test_a_sign_in_the_user_added_gets_boxes_on_this_computer_only(backend, slow_clock):
    S, db = backend, backend.backend.db_path
    approvals.change_signins(db, "popup", "Laptop key", True)
    a, b = suggest(S, "Use Kafka for events"), suggest(S, "Use NATS for events")
    box = Box("whatever-it-says", acc(approve=True))
    out = lan(S, "manage_review", {"action": "walk"}, box, _token("Laptop key"), False)
    assert box.seen == [] and status(db, a) == "pending"            # another computer: no
    out = lan(S, "manage_review", {"action": "walk"}, box, _token("Laptop key"), True)
    assert len(box.seen) == 2 and out["walked"] == [f"#{a}: approved", f"#{b}: approved"]
    label = "chat (whatever-it-says, signed in as Laptop key)"
    assert _q(db, "SELECT client FROM approval_log ORDER BY id") == [(label,), (label,)]
    assert _q(db, "SELECT decided_by FROM review_items WHERE id = ?", a) == [(label,)]


def test_the_shared_lan_token_never_gets_boxes(backend, slow_clock):
    S, db = backend, backend.backend.db_path
    item = suggest(S, "Use SQS for jobs")
    shared = FSAccessToken(token="", client_id="owner", client_name="owner (LAN token)",
                           scopes=[SCOPE_READ, SCOPE_WRITE], identity_source="shared_token")
    approvals.change_signins(db, "popup", "owner (LAN token)", True)
    box = Box("claude-code", acc(approve=True))
    lan(S, "manage_review", {"action": "walk"}, box, shared, True)
    assert box.seen == [] and status(db, item) == "pending"


def test_stdio_claude_code_still_gets_popups_with_its_label(backend, relay_dir):
    S, db = backend, backend.backend.db_path
    approvals.set_on(db, True)
    seen = []
    r = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"}, app="claude-code",
             answers=[acc(approve=True, confirm_name="Beta Project")], seen=seen)
    assert len(seen) == 1 and r.get("approved_in") == "popup", r
    assert _q(db, "SELECT path, client FROM approval_log") == [("popup", "popup (claude-code)")]


def test_only_the_terminal_and_the_local_page_change_the_sign_in_lists():
    from test_relay_chat_approvals import _callers
    # (task 65b) and the user's own Allow on an offer, which only the review page and the phone
    # pages decide (card_offer.refusal, checked in _review_decide before anything else)
    assert _callers(r"change_signins\(", "approvals.py") == ["card_offer.py", "cli_review.py",
                                                              "review_page.py"]
    assert _callers(r"card_offer\.apply\(", "card_offer.py") == ["review_apply.py"]


# ---------------------------------------------------------------- the card list's two parts
def test_card_sign_ins_are_their_own_list_and_a_re_registered_oauth_app_keeps_its_place(backend, claims_ui):
    S, db = backend, backend.backend.db_path
    approvals.change_card_clients(db, "Laptop key", True)      # an app name: never a sign-in
    assert not CARD_TOOLS & net_names(S, "x", card_token("Laptop key", "app_key"), "lan")
    approvals.change_signins(db, "card", "Claude", True)
    assert CARD_TOOLS <= net_names(S, "x", card_token("Claude", "oauth_client"), "public")
    # claude.ai registered again: FlowState named it "Claude (2)"; it keeps the cards
    assert CARD_TOOLS <= net_names(S, "x", card_token("Claude (2)", "oauth_client"), "public")
    # a key or token the user named "Claude (2)" themselves is its own name: not on the list
    assert not CARD_TOOLS & net_names(S, "x", card_token("Claude (2)", "remote_token"), "public")
    assert not CARD_TOOLS & net_names(S, "x", card_token("Claudette (2)", "oauth_client"), "public")



# ---------------------------------------------------------------- a card tap whose save fails
def test_a_disagreement_tap_whose_save_fails_keeps_its_code(backend, claims_ui, monkeypatch):
    """(task 56 Low) The tap's code was used up before the call's save committed: a save that
    then failed left the card saying "expired". The code is used up only once it commits."""
    from flowstate import saves
    from test_card_trust import call, codes
    S, db = backend, backend.backend.db_path
    a = call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Use REST for the API"},
             app="Cursor")[1]
    call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Use gRPC for the API",
                         "conflicts_with": a["record"]["id"], "topic": "the API"}, app="Cursor")
    [(item,)] = _q(db, "SELECT id FROM review_items WHERE kind = 'disagreement'")
    r, _ = call(S, "show_conflict_card", {"project": "Alpha", "item_id": item})
    tap = {"item_id": item, "keep": "a", "project": "Alpha", "tap": codes(r)[str(item)]}
    real, broken = saves._activity, [True]

    def flaky(*a, **kw):
        if broken[0]:
            raise RuntimeError("disk went away after the tap")
        return real(*a, **kw)
    monkeypatch.setattr(saves, "_activity", flaky)
    _r, body = call(S, "resolve_conflict_card", tap)
    assert "error" in body and status(db, item) == "pending"
    broken[0] = False
    _r, body = call(S, "resolve_conflict_card", tap)               # the same code works again
    assert body.get("resolved") == item and status(db, item) == "approved", body


# ---------------------------------------------------------------- "did you say this?" items
def test_did_you_say_this_items_get_no_box_in_the_walk(backend, relay_dir):
    """(task 56 Info) AGENTS.md: the user confirms a user_stated change on the review page,
    their phone or in their terminal; the walk holds those items back, like hard rules."""
    from flowstate import review as R
    S, db = backend, backend.backend.db_path
    said = R.add_item(db, 1, "source", "Did you say this? Use Postgres", None,
                      {"record_type": "decision", "record_id": 1, "title": "Use Postgres",
                       "user_only": True}, source="Cursor")
    other = suggest(S, "Use Redis for caching")
    seen = []
    out = call(S, "manage_review", {"action": "walk"}, answers=[acc(approve=True)] * 3, seen=seen)
    assert len(seen) == 1 and out["walked"] == [f"#{other}: approved"]
    assert status(db, said) == "pending"
    assert out["said_by_you"] and "review page" in out["said_by_you_note"]
