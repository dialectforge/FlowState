"""2.4.2: every answer that puts something in the review queue says how the user decides it,
the way manage_review(action="walk") does (waiting.queue_guidance, reusing waiting.links and
waiting.how_to_decide): the phone link when the relay link is known, the local page for apps
on this computer, and a plain line not to approve it for the user.

The live bug (Oct 23, 2026): log_work suggested=true through the relay answered only
{queued_for_review, note, receipt}, so the AI offered to approve the item itself in the chat.
Temp DBs and sandboxed folders only (conftest.py)."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, popup, waiting
from fsa_test_keys import make_key
from test_cleanup import messy  # noqa: F401 - the fixture: a project with clean-up ideas

LINK = "https://flowstatemcp.com/review/testbox"


def call(S, name, args, app="Claude"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    return r, json.loads(r.content[0].text)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """Enrolled in the relay as `testbox`, test keys pinned, and the call comes in through it."""
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    _key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    monkeypatch.setattr(popup, "listener", lambda: "relay")


@pytest.fixture
def no_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))


def _status(db, item):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT status FROM review_items WHERE id = ?", (item,)).fetchone()[0]
    finally:
        conn.close()


def _guided_for_relay(body):
    assert body["phone"] == LINK, body
    assert LINK in body["how_to_decide"] and "relay" in body["how_to_decide"]
    assert "Don't approve or reject anything for them" in body["how_to_decide"]
    assert waiting.ELSEWHERE in body["how_to_decide"]
    assert body["dont_approve"] == waiting.DONT_APPROVE
    assert "review_page" not in body and "127.0.0.1" not in json.dumps(body)


def test_suggested_log_work_over_the_relay_gets_the_phone_link_and_guidance(backend, relay):
    S = backend
    r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                   "summary": "Use the phone link for reviews"})
    assert not r.isError, body
    assert isinstance(body["queued_for_review"], int) and "receipt" in body
    _guided_for_relay(body)
    assert _status(S.backend.db_path, body["queued_for_review"]) == "pending"


def test_same_text_as_the_walk(backend, relay):
    S = backend
    _r, queued = call(S, "log_work", {"project": "Alpha", "kind": "todo", "suggested": True,
                                      "summary": "Something to decide"})
    _r, walk = call(S, "manage_review", {"action": "walk"})
    assert walk["phone"] == queued["phone"] == LINK
    # one code path: only "this list" / "the link" differ
    assert queued["how_to_decide"] == walk["how_to_decide"].replace("this list", "the link")


def test_stdio_app_gets_the_local_page_and_no_phone_without_relay(backend, no_relay):
    S = backend
    _r, body = call(S, "log_work", {"project": "Alpha", "kind": "learning", "suggested": True,
                                    "summary": "Local review"}, app="claude-code")
    assert body["review_page"].startswith("http://127.0.0.1:") and "phone" not in body
    assert body["review_page"] in body["how_to_decide"]
    assert 'manage_review action="walk"' in body["how_to_decide"] or "can't show" in body["how_to_decide"]
    assert body["dont_approve"] == waiting.DONT_APPROVE


def test_every_queueing_reply_over_the_relay_is_guided(backend, relay):
    S, db = backend, backend.backend.db_path
    _r, imp = call(S, "manage_memory", {"action": "import", "project": "Alpha",
                                        "text": "- prefers tabs\n- uses pytest"})
    assert imp["item_ids"]
    _guided_for_relay(imp)
    _r, clean = call(S, "manage_memory", {"action": "cleanup", "project": "Alpha"})
    assert not clean.get("item_ids") and "how_to_decide" not in clean   # nothing queued here
    approvals.set_on(db, True)
    r, dele = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"})
    assert isinstance(dele["delete_requested"], int)
    _guided_for_relay(dele)
    assert "next" in dele                  # the per-item line (relay: no popup) is still there
    from flowstate import review_mode
    review_mode.set_mode(db, "everything", [], None, "the user turned review on", "command-line")
    r, mode = call(S, "manage_review", {"action": "set_mode", "mode": "off",
                                        "user_request": "turn review off"})
    assert isinstance(mode["setting_requested"], int), mode
    _guided_for_relay(mode)


def test_cleanup_ideas_reply_is_guided(messy, relay):
    S = messy[0]
    _r, clean = call(S, "manage_memory", {"action": "cleanup", "project": "FlowState"})
    assert clean["item_ids"], clean
    _guided_for_relay(clean)


def test_possible_conflict_reply_is_guided(backend, relay):
    S = backend
    call(S, "log_work", {"project": "Alpha", "kind": "decision",
                         "summary": "Store all review links in the relay database table"})
    _r, body = call(S, "log_work", {"project": "Alpha", "kind": "decision",
                                    "summary": "Store all review links in the relay database tables"})
    assert body["possible_conflicts"][0]["review_item_id"], body
    _guided_for_relay(body)


def test_plain_saves_and_errors_get_no_guidance(backend, relay):
    S = backend
    _r, body = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "Saved now"})
    assert "logged" in body and "how_to_decide" not in body and "dont_approve" not in body
    assert waiting.queued_ids({"error": "x", "queued_for_review": 3}) == []
    assert waiting.queued_ids({"item_ids": [4, 5], "possible_conflicts": [{"review_item_id": 6},
                                                                          {"review_item_id": None}]}) \
        == [4, 5, 6]


def test_current_behaviour_relay_app_can_still_approve_with_the_users_words(backend, relay):
    """NOT a decision: pins today's behaviour (approve over the relay is unchanged in 2.4.2)
    so a later change is deliberate. An item that isn't user-only (approvals off; not a
    "did you say this?" item) is approved by the app that queued it when user_request has two
    or more words that aren't the item's own text; any other app name needs just one word."""
    S = backend
    _r, q = call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                 "summary": "Pinned behaviour item"})
    item = q["queued_for_review"]
    r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "user_request": f"Approve review item {item}"})
    assert not r.isError, body
    assert _status(S.backend.db_path, item) == "approved"
