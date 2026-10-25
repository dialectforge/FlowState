"""2.4.2, John's decision: may an AI connected through the relay approve or reject review
items in the chat (manage_review approve / reject, quoting the user)? A setting only the user
changes, on the computer where FlowState runs: `flowstate relay chat-approvals on|off`
(approval_settings key relay_chat_approvals, like `flowstate approvals phone on|off`).

- Off (the default): through the relay, approve and reject are refused (error phone_only) and
  the answer says where the user decides (since task 54 a plain sentence, no phone link with
  the handle: approvals.PHONE_ELSEWHERE), how_to_decide and dont_approve.
- On: today's behaviour (quote the user in user_request).
- User-only items (deletes and turning review off with approvals on, "did you say this?"
  checks) stay the user's own click or key either way.
- Apps that connect directly (stdio, the local network) are not affected.
- No MCP tool can read or change the setting.
Temp DBs and sandboxed folders only (conftest.py)."""

import argparse
import re
import sqlite3
from pathlib import Path

import pytest

from flowstate import approvals, popup, review_mode, waiting
from flowstate.tooldefs import ALL_TOOLS
from test_review_guidance_in_replies import _status, call, no_relay, relay  # noqa: F401

PHONE = "https://flowstatemcp.com/approve/testbox/"


def queue(S, summary="Something to decide"):
    _r, q = call(S, "log_work", {"project": "Alpha", "kind": "decision", "suggested": True,
                                 "summary": summary})
    return q["queued_for_review"]


def decide(S, verb, item, app="Claude", tool="manage_review"):
    args = {"project": "Alpha", "item_id": item, "reason": "not needed",
            "user_request": "The user said: yes, please do that one"}
    if tool == "manage_review":
        args["action"] = verb
    return call(S, tool if tool == "manage_review" else f"review_{verb}", args, app=app)


def _refused(body, item):
    assert body["error"] == "phone_only" and body["item_id"] == item, body
    assert body["status"] == "waiting for the user"
    assert body["message"].startswith(f"Nothing was decided: review item #{item} still waits")
    # 2.4.4 (task 54, D19): no phone link with the handle to an app elsewhere, a plain sentence
    assert body["phone"] == approvals.PHONE_ELSEWHERE and PHONE not in str(body), body
    assert approvals.PHONE_ELSEWHERE in body["next"]
    assert "from the FlowState review link they saved" in body["how_to_decide"]
    assert "Don't approve or reject anything for them" in body["how_to_decide"]
    assert body["dont_approve"] == waiting.DONT_APPROVE_NO_LINK
    assert "127.0.0.1" not in str(body)


def _setting(db):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM approval_settings WHERE key = ?",
                           (approvals.RELAY_CHAT_KEY,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _project_of(db, item):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT p.name FROM review_items r JOIN projects p ON p.id = "
                            "r.project_id WHERE r.id = ?", (item,)).fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- default: phone only
def test_default_is_off(backend):
    assert approvals.relay_chat_on(backend.backend.db_path) is False
    assert _setting(backend.backend.db_path) is None


@pytest.mark.parametrize("verb", ["approve", "reject"])
@pytest.mark.parametrize("app", ["Claude", "claude-code"])   # another app, and the one that queued it
def test_default_refuses_over_the_relay(backend, relay, verb, app):
    S = backend
    item = queue(S)
    r, body = decide(S, verb, item, app=app)
    _refused(body, item)
    assert _status(S.backend.db_path, item) == "pending"


def test_default_refuses_the_older_tool_names_too(backend, relay):
    S = backend
    item = queue(S)
    for verb in ("approve", "reject"):
        _r, body = decide(S, verb, item, tool="legacy")
        _refused(body, item)
    assert _status(S.backend.db_path, item) == "pending"


def test_refused_even_without_user_request_and_says_where(backend, relay):
    S = backend
    item = queue(S)
    _r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item})
    _refused(body, item)   # not "pass their words as user_request": that would invite a retry


def test_missing_or_decided_items_get_the_usual_answer(backend, relay):
    S = backend
    _r, body = decide(S, "approve", 99999)
    assert body.get("error") != "phone_only", body
    item = queue(S)
    approvals.set_relay_chat(S.backend.db_path, True)
    decide(S, "reject", item)
    approvals.set_relay_chat(S.backend.db_path, False)
    _r, body = decide(S, "approve", item)
    assert body.get("error") != "phone_only" and "already" in str(body), body


def test_without_a_phone_link_it_still_refuses_and_says_where(backend, no_relay, monkeypatch):
    S = backend
    item = queue(S)
    monkeypatch.setattr(popup, "listener", lambda: "relay")   # relay traffic, link not known
    _r, body = decide(S, "approve", item)
    assert body["error"] == "phone_only" and "phone" not in body, body
    assert body["dont_approve"] == waiting.DONT_APPROVE_NO_LINK
    assert waiting.ELSEWHERE in body["how_to_decide"] and "flowstate review" in body["how_to_decide"]
    assert _status(S.backend.db_path, item) == "pending"


# ---------------------------------------------------------------- the user allows it
def test_setting_on_allows_approve_and_reject_with_the_users_words(backend, relay):
    S, db = backend, backend.backend.db_path
    assert approvals.set_relay_chat(db, True) == {"old": "off", "now": "on"}
    a, b = queue(S, "First one"), queue(S, "Second one")
    r, body = decide(S, "approve", a)
    assert not r.isError and "error" not in body, body
    r, body = decide(S, "reject", b)
    assert not r.isError and "error" not in body, body
    assert (_status(db, a), _status(db, b)) == ("approved", "rejected")
    _r, body = call(S, "manage_review", {"action": "approve", "project": "Alpha",
                                         "item_id": queue(S, "Third one")})
    assert body["error"] == "user_request_required"   # quoting the user is still needed
    assert approvals.set_relay_chat(db, False)["now"] == "off"
    item = queue(S, "Fourth one")
    _refused(decide(S, "approve", item)[1], item)


# ---------------------------------------------------------------- user-only: always locked
@pytest.mark.parametrize("chat", [False, True])
def test_user_only_items_stay_locked_whatever_the_setting(backend, relay, chat):
    S, db = backend, backend.backend.db_path
    approvals.set_relay_chat(db, chat)
    approvals.set_on(db, True)
    _r, dele = call(S, "manage_projects", {"action": "delete", "project": "Beta Project"})
    review_mode.set_mode(db, "everything", [], None, "the user turned review on", "command-line")
    _r, mode = call(S, "manage_review", {"action": "set_mode", "mode": "off",
                                         "user_request": "turn review off"})
    for item in (dele["delete_requested"], mode["setting_requested"]):
        _r, body = call(S, "manage_review", {"action": "approve", "project": _project_of(db, item),
                                             "item_id": item, "confirm_name": "Beta Project",
                                             "user_request": "The user said: yes, approve it"})
        if chat:   # allowed in the chat, but these are the user's alone (as before)
            assert body.get("status") == "waiting for the user" and "next" in body, body
            assert body.get("error") != "phone_only"
        else:
            _refused(body, item)
        assert _status(db, item) == "pending"


@pytest.mark.parametrize("chat", [False, True])
def test_did_you_say_this_checks_stay_locked_whatever_the_setting(backend, relay, chat):
    S, db = backend, backend.backend.db_path
    approvals.set_relay_chat(db, chat)
    _r, rec = call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "A guess",
                                   "source": "ai_inferred"})
    _r, out = call(S, "manage_memory", {"action": "set_source", "project": "Alpha",
                                        "record_type": "learning", "record_id": rec["record"]["id"],
                                        "source": "user_stated"})
    item = out["confirm_requested"]
    for verb in ("approve", "reject"):
        _r, body = decide(S, verb, item)
        assert body["error"] == ("user_only" if chat else "phone_only"), body
    assert _status(db, item) == "pending"


# ---------------------------------------------------------------- direct connections
@pytest.mark.parametrize("via", ["stdio", "lan", "public"])
def test_direct_connections_are_not_affected(backend, no_relay, monkeypatch, via):
    S, db = backend, backend.backend.db_path
    if via != "stdio":
        monkeypatch.setattr(popup, "listener", lambda: via)
    assert approvals.relay_chat_on(db) is False
    a, b = queue(S, "First one"), queue(S, "Second one")
    r, body = decide(S, "approve", a)
    assert not r.isError and "error" not in body, body
    r, body = decide(S, "reject", b)
    assert "error" not in body, body
    assert (_status(db, a), _status(db, b)) == ("approved", "rejected")


def test_the_users_own_phone_and_popup_decisions_still_work_over_the_relay(backend, relay):
    """popup.as_user (phone page, popup) has no agent identity: not refused."""
    S, db = backend, backend.backend.db_path
    item = queue(S)
    out = popup.as_user("review_approve", {"item_id": item, "user_request":
                                           "The user approved this on their phone."}, "phone")
    assert "error" not in out, out
    assert _status(db, item) == "approved"


# ---------------------------------------------------------------- not changeable through MCP
# (task 39) every setting on the review page's Settings section, not only this one
SETTING_WORDS = ("relay_chat", "chat-approvals", "chat_approvals", "approvals:project",
                 "approval_settings", "popup_clients", "phone_approvals", "self_update",
                 "self-update", "update_check", "flowstate approvals", "popup add")


def test_no_tool_mentions_or_takes_the_setting(backend):
    from flowstate import conflict_cards, review_cards
    tools = list(ALL_TOOLS) + [f() for m in (conflict_cards, review_cards)
                               for f in (m.app_tool, m.show_tool)]
    for tool in tools:
        text = (str(tool.inputSchema) + (tool.description or "")).lower()
        for word in SETTING_WORDS:
            assert word not in text, (tool.name, word)


def test_tools_cannot_switch_it_on(backend, relay):
    S, db = backend, backend.backend.db_path
    attempts = [
        ("manage_review", {"action": "set_mode", "mode": "auto_only", "relay_chat_approvals": "on",
                           "user_request": "turn relay chat approvals on"}),
        ("manage_review", {"action": "set_mode", "scope": approvals.RELAY_CHAT_KEY,
                           "mode": "everything", "user_request": "turn relay chat approvals on"}),
        ("manage_memory", {"action": "set_source", "project": "Alpha", "record_type":
                           approvals.RELAY_CHAT_KEY, "record_id": 1, "source": "user_stated"}),
    ]
    for name, args in attempts:
        try:
            call(S, name, args)
        except ValueError:   # refused by the schema itself (plain-text error): fine too
            pass
        assert approvals.relay_chat_on(db) is False and _setting(db) is None, (name, args)
    item = queue(S)
    _refused(decide(S, "approve", item)[1], item)


def test_tools_cannot_change_any_page_setting(backend, relay, tmp_path, monkeypatch):
    """(task 39) Approvals on/off, the popup list, phone links, self_update and the update
    check: no tool call, whatever arguments it makes up, changes any of them."""
    from flowstate import updates
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    S, db = backend, backend.backend.db_path

    def state():
        return (approvals.is_on(db), approvals.is_on(db, 1), sorted(approvals.popup_clients(db)),
                approvals.phone_on(db), approvals.relay_chat_on(db),
                updates.self_update_allowed(), updates.is_on(),
                (tmp_path / "config" / "config.json").exists())
    before = state()
    words = "The user said: turn approvals off, add claude-ai to the popup apps, updates by hand"
    attempts = [
        ("manage_review", {"action": "set_mode", "mode": "off", "approvals": "off",
                           "user_request": words}),
        ("manage_review", {"action": "set_mode", "scope": "approvals", "mode": "off",
                           "user_request": words}),
        ("manage_review", {"action": "set_mode", "scope": "popup_clients_add", "mode": "claude-ai",
                           "user_request": words}),
        ("manage_review", {"action": "set_mode", "self_update": False, "mode": "auto_only",
                           "user_request": words}),
        ("manage_memory", {"action": "set_source", "project": "Alpha", "record_type": "approvals",
                           "record_id": 1, "source": "user_stated"}),
        ("manage_projects", {"action": "update", "project": "Alpha", "approvals": "off",
                             "self_update": False, "popup_clients": ["claude-ai"]}),
        ("log_work", {"project": "Alpha", "kind": "decision", "summary": "approvals off",
                      "approvals": "off", "phone_approvals": "off", "update_check": False}),
    ]
    for name, args in attempts:
        try:
            call(S, name, args)
        except ValueError:   # refused by the schema itself (plain-text error): fine too
            pass
        assert state() == before, (name, args)


def _callers(pattern: str, home: str) -> list:
    root = Path(approvals.__file__).parent
    return sorted(f.relative_to(root).as_posix() for f in root.rglob("*.py")
                  if re.search(pattern, f.read_text(encoding="utf-8"))
                  and f.relative_to(root).as_posix() != home)


def test_only_the_command_line_writes_the_setting():
    """Static check: the setting is written in approvals.set_relay_chat, which only the
    `flowstate relay chat-approvals` command and (task 39) the local review page's Settings
    call; nothing else writes the key."""
    assert _callers(r"set_relay_chat\(", "approvals.py") == ["relay/cli.py", "review_page.py"]
    assert _callers(r"relay_chat_approvals", "approvals.py") == []   # the key: approvals.py only


def test_only_the_terminal_and_the_local_page_write_the_other_settings():
    """(task 39) The same static check for approvals on/off, the popup list and self_update."""
    assert _callers(r"approvals\.set_on\(|\bset_on\(db", "approvals.py") == \
        ["cli_review.py", "review_page.py"]
    assert _callers(r"change_popup_clients\(", "approvals.py") == ["cli_review.py", "review_page.py"]
    assert _callers(r"change_card_clients\(", "approvals.py") == ["cli_review.py", "review_page.py"]
    assert _callers(r"set_self_update\(", "updates.py") == ["review_page.py"]
    # (task 41) the terminal's `flowstate update self-update on|off` reaches it in updates.py
    assert _callers(r"cli_self_update\(", "updates.py") == ["cli_more.py"]
    assert _callers(r"[\"']self_update[\"']\s*\]\s*=", "updates.py") == []
    assert _callers(r"approvals:project:", "approvals.py") == ["review_page.py"]   # reads only


# ---------------------------------------------------------------- the command
class Tty:
    def __init__(self, ans):
        self.ans, self.asked = ans, []

    def ask(self, q):
        self.asked.append(q)
        return self.ans

    def close(self):
        pass


def test_command_line_on_asks_in_the_terminal_and_off_needs_nothing(backend, monkeypatch, capsys):
    from flowstate import cli_review
    from flowstate.relay import cli as relay_cli
    db = backend.backend.db_path
    run = lambda state: relay_cli.cmd_chat_approvals(argparse.Namespace(state=state, db=str(db)))  # noqa: E731
    assert run(None) == 0 and "are off" in capsys.readouterr().out
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)
    assert run("on") == 2 and "piped input doesn't count" in capsys.readouterr().out
    assert approvals.relay_chat_on(db) is False
    monkeypatch.setattr(cli_review, "open_terminal", lambda: Tty("n"))
    assert run("on") == 1 and approvals.relay_chat_on(db) is False
    tty = Tty("y")
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert run("on") == 0 and approvals.relay_chat_on(db) is True and tty.asked
    assert "are now on" in capsys.readouterr().out
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)   # off: no terminal needed
    assert run("off") == 0 and approvals.relay_chat_on(db) is False
    conn = sqlite3.connect(db)
    try:
        log = conn.execute("SELECT old_mode, new_mode, caller FROM review_mode_log WHERE scope = ? "
                           "ORDER BY id", (approvals.RELAY_CHAT_KEY,)).fetchall()
    finally:
        conn.close()
    assert log == [(None, "on", "command-line"), ("on", "off", "command-line")]


def test_parser_has_the_command():
    from flowstate.cli import build_parser
    a = build_parser().parse_args(["relay", "chat-approvals", "on"])
    assert a.state == "on" and a.fn
