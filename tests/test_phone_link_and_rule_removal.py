"""Task 54 (2.4.4), two follow-ups to the data security audit:

1. D19: the phone links (https://flowstatemcp.com/review/<handle> and /approve/<handle>/...)
   carry the user's relay handle, so they go only to apps on this computer (stdio, or the LAN
   listener from a loopback peer). An app elsewhere (relay, tunnel, LAN from another computer)
   gets approvals.PHONE_ELSEWHERE instead, in every answer: the brief, list / walk, answers
   that queue something, the relay's phone-only refusal, a queued delete's line and the review
   card's fallback.
2. Removing a hard rule is the user's alone, like adding one (D2; re-audit N-F4): from any app
   manage_rules remove only queues it, whatever words the app passes; only the user's own
   click or key approves it; `flowstate rules remove <id>` in their terminal removes it at
   once; manage_memory delete refuses rules; the popup walk shows no box for rules (N-F26).

Temp DBs and sandboxed folders only (conftest.py)."""

import asyncio
import json
import sqlite3

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, popup, review, review_cards, waiting
from conftest import approve_rules
from test_review_without_terminal import (CALLS, http_calls, no_page, pile, relay,  # noqa: F401
                                          relay_dir)

HANDLE = "testbox"   # the fake relay handle the `relay` fixture enrols


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    r = asyncio.run(go())
    try:
        return r, json.loads(r.content[0].text)
    except ValueError:
        return r, r.content[0].text


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def _no_handle(answer):
    text = answer if isinstance(answer, str) else json.dumps(answer)
    assert HANDLE not in text and "flowstatemcp.com/review/" not in text \
        and "flowstatemcp.com/approve/" not in text, text


@pytest.fixture
def via_relay(relay, monkeypatch):   # noqa: F811 - the enrolled relay, and the call comes through it
    monkeypatch.setattr(popup, "listener", lambda: "relay")


# ================================================================ 1. phone link on this computer only
@pytest.mark.parametrize("peer", ["127.0.0.1", "192.0.2.51"])
def test_relay_callers_get_a_plain_message_not_the_phone_link(backend, relay, no_page, peer):  # noqa: F811
    db = backend.backend.db_path
    pile(db, 2)
    brief, walk, listing = http_calls(db, peer, CALLS, relay=True)
    for answer in (brief, walk, listing):
        _no_handle(answer)
    assert "on their phone, from the FlowState review link they saved" in brief
    assert walk["phone"] == approvals.PHONE_ELSEWHERE
    assert "from the FlowState review link they saved" in walk["how_to_decide"]


def test_lan_callers_on_another_computer_get_no_phone_link(backend, relay, no_page):  # noqa: F811
    db = backend.backend.db_path
    pile(db, 2)
    brief, walk, listing = http_calls(db, "192.0.2.51", CALLS)
    for answer in (brief, walk, listing):
        _no_handle(answer)
    assert walk["phone"] == approvals.PHONE_ELSEWHERE


def test_lan_callers_on_this_computer_and_stdio_still_get_the_link(backend, relay, no_page):  # noqa: F811
    db = backend.backend.db_path
    pile(db, 2)
    _brief, walk, _ = http_calls(db, "127.0.0.1", CALLS)
    assert walk["phone"] == f"https://flowstatemcp.com/review/{HANDLE}"
    _r, out = call(backend, "manage_review", {"action": "walk"}, app="grok")
    assert out["phone"] == f"https://flowstatemcp.com/review/{HANDLE}"
    assert f"/review/{HANDLE}" in call(backend, "start_here", {"project": "Alpha"})[1]


@pytest.mark.parametrize("where", ["relay", "public"])
def test_every_answer_to_a_remote_app_leaves_the_handle_out(backend, relay, monkeypatch, where):  # noqa: F811
    S = backend
    monkeypatch.setattr(popup, "listener", lambda: where)
    _r, q = call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                 "summary": "Queue me for the user"})
    _no_handle(q)
    assert q["phone"] == approvals.PHONE_ELSEWHERE
    item = q["queued_for_review"]
    _r, d = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                      "user_request": "The user said: yes, approve it"})
    _no_handle(d)
    if where == "relay":   # chat approvals off (the default): phone only, and still no handle
        assert d["error"] == "phone_only" and approvals.PHONE_ELSEWHERE in d["next"]
    for args in ({"action": "list"}, {"action": "walk"}, {"action": "list", "item_id": item}):
        _no_handle(call(S, "manage_review", args)[1])
    _no_handle(call(S, "start_here", {"project": "Alpha"})[1])


def test_a_queued_delete_line_leaves_the_handle_out(backend, seeded_db, via_relay, monkeypatch):
    S = backend
    monkeypatch.setattr(approvals, "is_on", lambda *a, **k: True)
    _r, out = call(S, "manage_memory", {"action": "delete", "project": "Alpha",
                                        "record_type": "learning", "record_id": 1})
    _no_handle(out)
    assert approvals.PHONE_ELSEWHERE in json.dumps(out)


def test_the_review_card_fallback_leaves_the_handle_out(backend, seeded_db, via_relay, monkeypatch):
    monkeypatch.setattr(review_cards, "client_has_ui", lambda: True)
    from flowstate import conflict_cards
    monkeypatch.setattr(conflict_cards, "client_has_ui", lambda: True)
    item = review.add_item(seeded_db, 1, "source", "Did you say this?", None,
                           {"record_type": "learning", "record_id": 1, "title": "Alpha lesson"}, "Claude")
    r, _body = call(backend, "show_review_card", {"item_id": item})
    card = r.structuredContent["cards"][0]
    _no_handle(card)
    _no_handle(r.content[0].text)
    assert card["link"] == "" and approvals.PHONE_ELSEWHERE in card["locked_note"]


def test_link_builders_refuse_remote_callers(backend, relay, monkeypatch):  # noqa: F811
    db = backend.backend.db_path
    item = pile(db, 1)[0]
    assert approvals.review_link(db) and approvals.phone_link(db, item)   # stdio: here
    monkeypatch.setattr(popup, "listener", lambda: "relay")
    assert approvals.review_link(db) is None and approvals.phone_link(db, item) is None
    assert approvals.phone_hidden(db) is True
    _no_handle(approvals.ask_line(db, item))
    _no_handle(waiting.where_line(db))


# ================================================================ 2. removing a hard rule
def _rule(S, db, text="Never push to main"):
    call(S, "manage_rules", {"action": "add", "project": "Alpha", "rule": text})
    return approve_rules(db)[0]


def _active(db, rid):
    return _q(db, "SELECT removed_at IS NULL FROM project_rules WHERE id = ?", rid) == [(1,)]


def test_an_apps_remove_only_queues_it_for_the_user(backend, seeded_db):
    S = backend
    rid = _rule(S, seeded_db)
    _r, out = call(S, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                       "user_request": "The user said: drop the main rule please"})
    assert out.get("removed") is None and "Not removed yet" in out["message"], out
    item = out["queued_for_review"]
    assert _active(seeded_db, rid)
    assert "Never push to main" in call(S, "start_here", {"project": "Alpha"})[1]
    it = review.get_item(seeded_db, item)
    assert it["kind"] == "rule" and approvals.user_only(it)
    assert json.loads(it["payload"])["action"] == "remove"
    # asking again finds the same waiting item; it doesn't block adding other rules
    again = call(S, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                     "user_request": "remove it"})[1]
    assert again["queued_for_review"] == item


@pytest.mark.parametrize("app", ["Cursor", "Claude Code"])   # the app that asked, or another one
def test_no_app_can_approve_a_rule_removal(backend, seeded_db, app):
    S = backend
    rid = _rule(S, seeded_db)
    item = call(S, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                    "user_request": "remove the main rule"})[1]["queued_for_review"]
    for tool, args in (("manage_review", {"action": "approve"}), ("review_approve", {})):
        _r, a = call(S, tool, {**args, "project": "Alpha", "item_id": item,
                               "user_request": "The user said: yes, remove that hard rule now"}, app=app)
        assert a["error"] == "user_only" and "Only the user removes hard rules" in a["message"], a
    assert _active(seeded_db, rid)
    assert review.get_item(seeded_db, item)["status"] == "pending"


def test_the_users_own_approval_removes_it(backend, seeded_db):
    from flowstate import review_apply
    S = backend
    rid = _rule(S, seeded_db)
    item = call(S, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                    "user_request": "remove it"})[1]["queued_for_review"]
    review_apply.approve(seeded_db, review.get_item(seeded_db, item), {}, "I removed it myself",
                         "command-line", {})
    assert not _active(seeded_db, rid)
    assert "Never push to main" not in call(S, "start_here", {"project": "Alpha"})[1]
    assert _q(seeded_db, "SELECT removed_by FROM project_rules WHERE id = ?", rid)[0][0].startswith("user (")


def test_rejecting_it_keeps_the_rule(backend, seeded_db):
    from flowstate import review_apply
    rid = _rule(backend, seeded_db)
    item = call(backend, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                          "user_request": "remove it"})[1]["queued_for_review"]
    review_apply.reject(seeded_db, review.get_item(seeded_db, item), "keep it", "No", "command-line")
    assert _active(seeded_db, rid)


def test_manage_memory_delete_refuses_rules(backend, seeded_db):
    rid = _rule(backend, seeded_db)
    _r, out = call(backend, "manage_memory", {"action": "delete", "project": "Alpha",
                                              "record_type": "rule", "record_id": rid})
    assert out["error"] == "user_only" and "manage_rules" in out["message"], out
    assert _active(seeded_db, rid)


def test_the_users_terminal_removes_it_at_once(backend, seeded_db, monkeypatch, capsys):
    from flowstate import cli, cli_data, cli_review
    rid = _rule(backend, seeded_db)
    item = call(backend, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                          "user_request": "remove it"})[1]["queued_for_review"]
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)
    assert cli.main(["rules", "remove", str(rid), "--db", str(seeded_db)]) == 2   # no terminal
    assert _active(seeded_db, rid)
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 1)                    # typed n
    assert cli.main(["rules", "remove", f"R{rid}", "--db", str(seeded_db)]) == 1
    assert _active(seeded_db, rid)
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 0)                    # typed y
    assert cli.main(["rules", "remove", f"R{rid}", "--db", str(seeded_db)]) == 0
    assert not _active(seeded_db, rid)
    assert review.get_item(seeded_db, item)["status"] == "rejected"         # settled with it
    assert cli.main(["rules", "remove", "999", "--db", str(seeded_db)]) == 1
    assert "No hard rule R999" in capsys.readouterr().out


def test_the_popup_walk_shows_no_box_for_hard_rules(backend, seeded_db):
    S = backend
    rid = _rule(S, seeded_db)
    remove = call(S, "manage_rules", {"action": "remove", "project": "Alpha", "rule_id": rid,
                                      "user_request": "remove it"})[1]["queued_for_review"]
    add = call(S, "manage_rules", {"action": "add", "project": "Alpha",
                                   "rule": "Always deploy on Friday"})[1]["queued_for_review"]
    other = review.add_item(seeded_db, 1, "todo", "Something else", "", {}, "cursor")
    listing = review.list_items(seeded_db, None, "pending", 50)
    plan = waiting.prepare(listing, seeded_db, None)["_walk"]
    assert other in plan["ids"] and remove not in plan["ids"] and add not in plan["ids"]
    assert len(plan["held"]) == 2


def test_old_phone_labels_with_the_handle_are_scrubbed_for_remote_apps(backend, relay, monkeypatch):  # noqa: F811
    """2.4.3 logged phone decisions as "phone (<handle>)"; "who did what" still shows them."""
    from flowstate import review_apply
    db = backend.backend.db_path
    item = pile(db, 1)[0]
    review_apply.reject(db, review.get_item(db, item), "not needed", "No", "phone")
    conn = sqlite3.connect(db)
    conn.execute("UPDATE review_items SET decided_app = ?, decided_by = ? WHERE id = ?",
                 (f"phone ({HANDLE})", f"phone ({HANDLE})", item))
    conn.commit()
    conn.close()
    here = call(backend, "manage_history", {"action": "who", "project": "Alpha"})[1]
    assert f"phone ({HANDLE})" in json.dumps(here)        # this computer: as stored
    monkeypatch.setattr(popup, "listener", lambda: "relay")
    away = call(backend, "manage_history", {"action": "who", "project": "Alpha"})[1]
    _no_handle(away)
    assert '"phone"' in json.dumps(away)
