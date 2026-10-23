"""2.4.0 approvals (approvals.py, popup.py, approve_page.py).

Off by default: an agent's delete goes through at once after a safety copy, fully undoable,
and FlowState never asks the user anything. On: an agent's delete and turning review off wait
for the user's own click or key (popup for allowlisted apps, else the phone link, else the
terminal). Grants are checked against pinned keys; test keys are made fresh per run
(fsa_test_keys.py). Temp DBs only."""

import asyncio
import json
import sqlite3
import time

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import approvals, popup, review_mode
from fsa_test_keys import claims, make_key, sign
from test_deletes import _MOVING, dump  # noqa: F401  (same "every row comes back" check)


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, app="Cursor", answer=None, seen=None):
    """One tool call from `app`. `answer(params)` -> ElicitResult makes the app declare form
    elicitation and answer popups; `seen` collects every popup it was shown."""
    async def elicit(_ctx, params):
        if seen is not None:
            seen.append(params)
        return await answer(params) if asyncio.iscoroutinefunction(answer) else answer(params)

    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1"),
                elicitation_callback=elicit if answer else None) as c:
            return await c.call_tool(name, args)
    return _j(asyncio.run(go()))


def accept(**content):
    return lambda _p: types.ElicitResult(action="accept", content=content)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


# pty.fork() in pytest's multi-threaded process: safe here, the child execs at once
pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")

DEL_TODO = {"action": "delete", "project": "Alpha", "record_type": "todo", "record_id": 1}


@pytest.fixture
def on(backend):
    approvals.set_on(backend.backend.db_path, True)
    return backend


@pytest.fixture
def relay(monkeypatch):
    """This computer enrolled in the relay as `testbox` (fake device token), with test keys."""
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    return key


# ---------------------------------------------------------------- approvals off (default)
def test_off_by_default_a_delete_goes_through_at_once_without_asking(backend):
    S, db = backend, backend.backend.db_path
    assert approvals.is_on(db) is False and approvals.is_on(db, 1) is False
    seen = []
    out = call(S, "manage_memory", DEL_TODO, app="claude-code", answer=accept(approve=True), seen=seen)
    assert seen == []                                     # never a popup, link or terminal step
    assert out["deleted"]["record_type"] == "todo" and out["safety_copy"]
    assert "next" not in out and "delete_requested" not in out
    assert out["note"].startswith('Deleted todo #1 "Alpha todo"') and "\n" not in out["note"]
    assert 'Undo: manage_history(action="undo", project="Alpha", record_type="todo", record_id=1)' \
        in out["note"]
    assert out["receipt"]["saved"]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(0,)]
    assert _q(db, "SELECT COUNT(*) FROM review_items WHERE kind = 'delete'") == [(0,)]


def test_off_a_delete_with_children_and_links_comes_back_exactly(backend):
    S, db = backend, backend.backend.db_path
    call(S, "manage_problems", {"action": "add_attempt", "project": "Alpha", "problem_id": 1,
                                "description": "restart it"})
    call(S, "manage_todos", {"action": "add", "project": "Alpha", "title": "after the bug",
                             "blocked_by_problem": 1})
    before = dump(db)
    out = call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "problem",
                                    "record_id": 1})
    assert out["deleted"]["with"].get("attempts") == 1 and out["deleted"]["with"].get("links") == 1
    assert dump(db) != before
    brief = call_text(S, "start_here", {"project": "Alpha"})
    line = next(l for l in brief.splitlines() if l.startswith("Deleted in the last 7 days"))
    assert 'problem #1 "Alpha bug"' in line and 'record_type="problem", record_id=1' in line
    undo = call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "problem",
                                      "record_id": 1})
    assert "error" not in undo, undo
    assert dump(db) == before
    assert "Deleted in the last" not in call_text(S, "start_here", {"project": "Alpha"})


def test_off_a_whole_project_goes_and_comes_back(backend):
    S, db = backend, backend.backend.db_path
    before = dump(db)
    out = call(S, "manage_projects", {"action": "delete", "project": "Alpha"})
    assert out["deleted"]["record_type"] == "project", out
    assert _q(db, "SELECT name FROM projects") == [("Beta Project",)]
    listing = call_text(S, "start_here", {})
    assert 'project #1 "Alpha"' in listing and 'project="Alpha", record_type="project"' in listing
    undo = call(S, "manage_history", {"action": "undo", "project": "Alpha", "record_type": "project",
                                      "record_id": 1})
    assert "error" not in undo, undo
    assert dump(db) == before


def test_off_review_setting_changes_just_happen_logged_and_in_the_brief(backend):
    S, db = backend, backend.backend.db_path
    out = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "off",
                                    "user_request": "turn review off for Alpha"})
    assert out["now"]["mode"] == "off" and out["logged"], out
    assert "Review mode: off" in call_text(S, "start_here", {"project": "Alpha"})
    assert _q(db, "SELECT new_mode FROM review_mode_log ORDER BY id DESC LIMIT 1") == [("off",)]


def call_text(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go()).content[0].text


# ---------------------------------------------------------------- what asks, with approvals on
def test_session_summaries_and_suggestions_never_need_the_user_only_step(on):
    S, db = on, on.backend.db_path
    call_text(S, "start_here", {"project": "Alpha"}, app="Cursor")
    call(S, "log_work", {"project": "Alpha", "kind": "learning", "summary": "a real save"}, app="Cursor")
    S.backend.tracker.close_instance("shutdown")
    summary = _q(db, "SELECT id FROM review_items WHERE kind = 'session_summary'")[0][0]
    sug = call(S, "log_work", {"project": "Alpha", "kind": "todo", "summary": "idea",
                               "suggested": True})["queued_for_review"]
    for item in (summary, sug):
        assert not approvals.user_only(dict(_q_row(db, item)))
        out = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                        "user_request": "User: yes, approve that one"}, app="Cursor")
        assert out.get("approved") == item, out


def _q_row(db, item):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM review_items WHERE id = ?", (item,)).fetchone()
    finally:
        conn.close()


def test_on_turning_review_off_waits_for_the_user(on):
    S, db = on, on.backend.db_path
    out = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "off",
                                    "user_request": "User: turn review off"})
    item = out["setting_requested"]
    assert out["status"] == "waiting for the user"
    assert out["next"] == (f"Waiting for the user: ask them to open the FlowState review shortcut on their "
                           f"desktop and approve it there (or run `flowstate approve {item}` in a terminal).")
    assert review_mode.get(db, 1)["mode"] == "auto_only"
    again = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                      "user_request": "User: yes really, approve it"}, app="Claude Desktop")
    assert again["status"] == "waiting for the user" and review_mode.get(db, 1)["mode"] == "auto_only"
    # stricter changes don't ask
    stricter = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "everything",
                                         "user_request": "User: make everything wait"})
    assert stricter["now"]["mode"] == "everything"
    done = popup.as_user("review_approve", {"item_id": item, "user_request": "terminal"}, "command-line")
    assert "error" not in done, done
    assert review_mode.get(db, 1)["mode"] == "off"
    log = call(S, "manage_history", {"action": "list", "project": "Alpha"})["approvals"]
    assert (log[0]["item_id"], log[0]["kind"], log[0]["path"], log[0]["decision"]) \
        == (item, "setting", "terminal", "approved")


def test_on_turning_review_off_for_the_whole_install_waits_too(on):
    S, db = on, on.backend.db_path
    out = call(S, "manage_review", {"action": "set_mode", "mode": "off",
                                    "user_request": "User: no more review anywhere"})
    assert out["setting_requested"] and review_mode.get(db)["mode"] == "auto_only"
    assert "the whole install" in _q(db, "SELECT summary FROM review_items WHERE id = ?",
                                     out["setting_requested"])[0][0]


# ---------------------------------------------------------------- the popup
def _ask_delete(S, answer, app="claude-code", seen=None):
    return call(S, "manage_memory", DEL_TODO, app=app, answer=answer, seen=seen)


def test_popup_accept_with_approve_true_deletes(on):
    S, db = on, on.backend.db_path
    seen = []
    out = _ask_delete(S, accept(approve=True, note="ok"), seen=seen)
    assert out["approved_in"] == "popup" and out["deleted"]["record_type"] == "todo", out
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(0,)]
    p = seen[0]
    assert p.message == 'claude-code wants to delete the todo "Alpha todo" from Alpha.'
    assert p.requestedSchema["required"] == ["approve"]
    assert p.requestedSchema["properties"]["approve"]["type"] == "boolean"
    log = call(S, "manage_history", {"action": "list", "project": "Alpha"})["approvals"]
    assert (log[0]["path"], log[0]["client"], log[0]["decision"]) == \
        ("popup", "popup (claude-code)", "approved")


@pytest.mark.parametrize("answer,why", [
    (lambda _p: types.ElicitResult(action="decline"), "the user said no"),
    (lambda _p: types.ElicitResult(action="cancel"), "the user closed it"),
    (accept(approve=False), "the user said no"),
    (accept(note="no box ticked"), "the user said no"),           # required field missing
    (accept(approve="true"), "the user said no"),                 # not a real boolean
])
def test_popup_anything_but_approve_true_leaves_it_queued(on, answer, why):
    S, db = on, on.backend.db_path
    before = dump(db)
    out = _ask_delete(S, answer)
    item = out["delete_requested"]
    assert out["next"] == f"Not approved in the popup ({why}); nothing changed and item #{item} stays queued."
    assert dump(db) == before
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]
    assert _q(db, "SELECT decision FROM approval_log WHERE item_id = ?", item)[0][0] in ("declined", "cancelled")


def test_popup_timeout_is_a_no_and_holds_no_database_lock(on, monkeypatch):
    S, db = on, on.backend.db_path
    monkeypatch.setattr(approvals, "POPUP_SECONDS", 0.6)
    wrote = []

    async def slow(_p):
        conn = sqlite3.connect(db, timeout=0.2)          # FlowState holding a lock would block this
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE projects SET description = 'written during the popup' WHERE id = 2")
        conn.commit()
        conn.close()
        wrote.append(True)
        await asyncio.sleep(2)
        return types.ElicitResult(action="accept", content={"approve": True})
    t0 = time.monotonic()
    out = _ask_delete(S, slow)
    assert wrote and time.monotonic() - t0 < 5
    item = out["delete_requested"]
    assert "no answer within 2 minutes" in out["next"]
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(1,)]
    assert _q(db, "SELECT decision FROM approval_log WHERE item_id = ?", item) == [("timed_out",)]
    S.init_backend(db)                                 # a restart approves nothing either
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]


@pytest.mark.parametrize("app,answer", [
    ("claude-code", None),                 # didn't declare elicitation
    ("codex-mcp-client", accept(approve=True)),    # declared, not on the allowlist
    ("claude-ai", accept(approve=True)),           # claude.ai / Claude Desktop: never
    ("Anthropic Connector", accept(approve=True)),
])
def test_no_popup_unless_declared_allowlisted_and_not_claude_ai(on, app, answer):
    S, db = on, on.backend.db_path
    seen = []
    out = call(S, "manage_memory", DEL_TODO, app=app, answer=answer, seen=seen)
    assert seen == [] and out["status"] == "waiting for the user"
    assert out["next"] == (f"Waiting for the user: ask them to open the FlowState review shortcut on their "
                           f"desktop and approve it there (or run `flowstate approve "
                           f"{out['delete_requested']}` in a terminal).")
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(1,)]


def test_the_allowlist_is_a_local_setting(on):
    S, db = on, on.backend.db_path
    approvals.change_popup_clients(db, "codex-mcp-client", add=True)
    seen = []
    out = call(S, "manage_memory", DEL_TODO, app="codex-mcp-client", answer=accept(approve=True),
               seen=seen)
    assert seen and out["approved_in"] == "popup"
    approvals.change_popup_clients(db, "claude-code", add=False)
    out = call(S, "manage_memory", {**DEL_TODO, "record_type": "learning"}, app="claude-code",
               answer=accept(approve=True), seen=(seen := []))
    assert seen == [] and "next" in out
    approvals.change_popup_clients(db, "claude-ai", add=True)     # still never claude.ai
    out = call(S, "manage_memory", {**DEL_TODO, "record_type": "change"}, app="claude-ai",
               answer=accept(approve=True), seen=seen)
    assert seen == [] and "next" in out


def test_popup_for_a_project_delete_needs_the_typed_name(on):
    S, db = on, on.backend.db_path
    seen = []
    out = call(S, "manage_projects", {"action": "delete", "project": "Alpha"}, app="claude-code",
               answer=accept(approve=True, confirm_name="alpha"), seen=seen)
    assert seen[0].requestedSchema["required"] == ["approve", "confirm_name"]
    assert "project name didn't match" in out["next"] and _q(db, "SELECT COUNT(*) FROM projects") == [(2,)]
    out = call(S, "manage_projects", {"action": "delete", "project": "Alpha"}, app="claude-code",
               answer=accept(approve=True, confirm_name="Alpha"))
    assert out["deleted"]["record_type"] == "project", out


def test_an_agent_approving_asks_the_user_again_in_the_popup(on):
    S, db = on, on.backend.db_path
    item = call(S, "manage_memory", DEL_TODO, app="Cursor")["delete_requested"]
    out = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                    "user_request": "User: yes"}, app="claude-code",
               answer=accept(approve=True))
    assert out["approved_in"] == "popup" and out["approved"] == item, out
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(0,)]


# ---------------------------------------------------------------- the phone link
def test_the_link_only_when_the_relay_is_enrolled_and_keys_are_pinned(on, monkeypatch):
    S, db = on, on.backend.db_path
    out = call(S, "manage_memory", DEL_TODO)
    assert "flowstatemcp.com" not in out["next"]                   # no relay
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": None, "fsa-2": None})
    out = call(S, "manage_memory", {**DEL_TODO, "record_type": "learning"})
    assert "flowstatemcp.com" not in out["next"]                   # no keys pinned
    _key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    out = call(S, "manage_memory", {**DEL_TODO, "record_type": "change"})
    item = out["delete_requested"]
    nonce = _q(db, "SELECT nonce FROM approval_requests WHERE item_id = ?", item)[0][0]
    assert out["next"] == ("Waiting for the user: ask them to approve it on their phone: "
                           f"https://flowstatemcp.com/approve/testbox/{item}?n={nonce}")
    assert len(nonce) == 43
    approvals.set_value(db, "phone_approvals", "off")
    out = call(S, "manage_memory", {**DEL_TODO, "record_type": "problem"})
    assert "flowstatemcp.com" not in out["next"]


def test_the_production_keys_are_pinned_and_refuse_a_grant_from_any_other_key(on, tmp_path):
    from flowstate.relay.store import RelayStore
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    assert set(approvals.FSA_PUBLIC_KEYS) == {"fsa-1", "fsa-2"}
    for v in approvals.FSA_PUBLIC_KEYS.values():
        assert v["kty"] == "EC" and v["crv"] == "P-256" and "d" not in v
    assert approvals.keys_ready()
    key, _jwk = make_key()
    db = on.backend.db_path
    item = call(on, "manage_memory", DEL_TODO)["delete_requested"]
    nonce = approvals.nonce_for(db, item)
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_grant(db, sign(key, claims(item, nonce)), item)
    assert e.value.code != "phone_approvals_unavailable"


def _page(S):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from flowstate.approve_page import routes
    return TestClient(Starlette(routes=routes(lambda: S.backend.db_path)),
                      base_url="https://testbox.fsmcp.com")


H = {"content-type": "application/json", "origin": "https://testbox.fsmcp.com",
     "sec-fetch-site": "same-origin"}


def _open(c, item, grant, headers=H):
    return c.post(f"/approve/{item}/open", content=json.dumps({"grant": grant}), headers=headers)


def _decide(c, item, grant, token, decision="approve", cookie=True, headers=H, **extra):
    hdrs = dict(headers)
    c.cookies.clear()
    if cookie:
        hdrs["cookie"] = f"fsa_page={token if cookie is True else cookie}"
    return c.post(f"/approve/{item}/decide", headers=hdrs,
                  content=json.dumps({"grant": grant, "token": token, "decision": decision, **extra}))


def _queued(S):
    db = S.backend.db_path
    item = call(S, "manage_memory", DEL_TODO)["delete_requested"]
    return item, approvals.nonce_for(db, item)


def test_phone_approve_end_to_end(on, relay):
    S, db = on, on.backend.db_path
    item, nonce = _queued(S)
    c = _page(S)
    shell = c.get(f"/approve/{item}")
    csp = shell.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp and "script-src 'self'" in csp
    assert shell.headers["x-frame-options"] == "DENY" and shell.headers["cache-control"] == "no-store"
    assert "<script src=" in shell.text and "<style" not in shell.text and " style=" not in shell.text
    assert "Alpha todo" not in shell.text                              # no data before the grant
    js = c.get("/approve/assets/approve.js").text
    assert "history.replaceState" in js and "textContent" in js and "innerHTML" not in js
    grant = sign(relay, claims(item, nonce))
    r = _open(c, item, grant)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["item"]["question"] == "This record will be deleted from Alpha"
    assert body["item"]["summary"].startswith('Todo "Alpha todo" in Alpha')
    assert "priority" in body["item"]["summary"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert _open(c, item, grant).json()["error"] == "grant_used"           # one load per grant
    r = _decide(c, item, grant, body["token"])
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(0,)]
    assert _q(db, "SELECT status, decided_by FROM review_items WHERE id = ?", item) == \
        [("approved", "phone (testbox)")]
    assert _q(db, "SELECT used_at IS NOT NULL FROM approval_requests WHERE item_id = ?", item) == [(1,)]
    assert _q(db, "SELECT path, decision FROM approval_log WHERE item_id = ?", item) == [("phone", "approved")]
    again = _decide(c, item, grant, body["token"])
    assert again.status_code == 403                                      # reused: refused


def test_phone_reject_uses_the_code_too(on, relay):
    S, db = on, on.backend.db_path
    item, nonce = _queued(S)
    c = _page(S)
    grant = sign(relay, claims(item, nonce))
    token = _open(c, item, grant).json()["token"]
    r = _decide(c, item, grant, token, "reject")
    assert r.status_code == 200, r.text
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("rejected",)]
    assert _q(db, "SELECT used_at IS NOT NULL FROM approval_requests WHERE item_id = ?", item) == [(1,)]
    fresh = sign(relay, claims(item, nonce))
    assert _open(c, item, fresh).json()["error"] == "not_open"           # the code is spent


@pytest.mark.parametrize("case,expect", [
    ("expired", "grant_expired"), ("too_long", "bad_grant"), ("wrong_handle", "wrong_device"),
    ("wrong_aud", "wrong_device"), ("wrong_iss", "wrong_device"), ("wrong_action", "wrong_device"),
    ("wrong_item", "wrong_item"), ("wrong_nonce", "not_open"), ("bad_signature", "bad_signature"),
    ("unknown_kid", "unknown_key"), ("backup_kid_placeholder", "unknown_key"), ("alg_none", "bad_grant"),
    ("garbage", "bad_grant")])
def test_bad_grants_are_refused(on, relay, case, expect):
    S, db = on, on.backend.db_path
    item, nonce = _queued(S)
    other_item, _ = _queued_other(S)
    t = int(time.time())
    c_ok = claims(item, nonce)
    grant = {
        "expired": lambda: sign(relay, claims(item, nonce, iat=t - 400, exp=t - 100)),
        "too_long": lambda: sign(relay, claims(item, nonce, exp=t + 3600)),
        "wrong_handle": lambda: sign(relay, claims(item, nonce, handle="someoneelse")),
        "wrong_aud": lambda: sign(relay, {**c_ok, "aud": "someoneelse.fsmcp.com"}),
        "wrong_iss": lambda: sign(relay, {**c_ok, "iss": "https://evil.example"}),
        "wrong_action": lambda: sign(relay, {**c_ok, "action": "delete_everything"}),
        "wrong_item": lambda: sign(relay, claims(other_item, nonce)),
        "wrong_nonce": lambda: sign(relay, claims(item, "A" * 43)),
        "bad_signature": lambda: sign(make_key()[0], c_ok),
        "unknown_kid": lambda: sign(relay, c_ok, kid="fsa-9"),
        "backup_kid_placeholder": lambda: sign(relay, c_ok, kid="fsa-2"),
        "alg_none": lambda: sign(relay, c_ok, header={"alg": "none", "kid": "fsa-1", "typ": "fsa+jwt"}),
        "garbage": lambda: "not.a.grant",
    }[case]()
    before = dump(db)
    r = _open(_page(S), item, grant)
    assert r.status_code == 403 and r.json()["error"] == expect, r.text
    assert dump(db) == before


def _queued_other(S):
    db = S.backend.db_path
    item = call(S, "manage_memory", {**DEL_TODO, "record_type": "learning"})["delete_requested"]
    return item, approvals.nonce_for(db, item)


@pytest.mark.parametrize("case", ["no_cookie", "wrong_cookie", "wrong_token", "form_post",
                                  "cross_origin", "cross_site", "other_grant"])
def test_the_tap_is_bound_to_the_page_and_its_grant(on, relay, case):
    S, db = on, on.backend.db_path
    item, nonce = _queued(S)
    c = _page(S)
    grant = sign(relay, claims(item, nonce))
    token = _open(c, item, grant).json()["token"]
    if case == "no_cookie":
        r = _decide(c, item, grant, token, cookie=False)
    elif case == "wrong_cookie":
        r = _decide(c, item, grant, token, cookie="x" * 43)
    elif case == "wrong_token":
        r = _decide(c, item, grant, "y" * 43, cookie="y" * 43)
    elif case == "form_post":
        r = _decide(c, item, grant, token, headers={**H, "content-type": "application/x-www-form-urlencoded"})
    elif case == "cross_origin":
        r = _decide(c, item, grant, token, headers={**H, "origin": "https://evil.example"})
    elif case == "cross_site":
        r = _decide(c, item, grant, token, headers={**H, "sec-fetch-site": "cross-site"})
    else:   # a second valid grant for the same item can't use the first page's token
        r = _decide(c, item, sign(relay, claims(item, nonce, sub="test-user-0002")), token)
    assert r.status_code == 403, r.text
    assert _q(db, "SELECT status FROM review_items WHERE id = ?", item) == [("pending",)]
    assert _q(db, "SELECT COUNT(*) FROM todos WHERE id = 1") == [(1,)]


def test_the_page_is_mounted_on_the_http_app(on):
    from flowstate.http_app import create_http_app
    app = create_http_app("x" * 32, db_path=on.backend.db_path)
    paths = {getattr(r, "path", None) for r in app.app.routes}
    assert {"/approve/{item:int}", "/approve/{item:int}/open", "/approve/{item:int}/decide"} <= paths


# ---------------------------------------------------------------- the switch itself
def test_turning_approvals_on_needs_nothing_and_off_needs_the_users_terminal(backend):
    from test_user_approval import _cli, _in_a_terminal
    db = backend.backend.db_path
    r = _cli(db, "approvals", "on")
    assert r.returncode == 0 and "now on" in r.stdout and approvals.is_on(db)
    r = _cli(db, "approvals", "off", stdin="y\n")
    assert r.returncode == 2 and "Refused" in r.stdout and approvals.is_on(db)
    code, out = _in_a_terminal(db, ["approvals", "off"], ["y"])
    assert code == 0 and "now off" in out and not approvals.is_on(db)
    r = _cli(db, "approvals", "on", "--project", "Alpha")
    assert r.returncode == 0 and approvals.is_on(db, 1)


def test_on_going_back_from_off_to_the_default_needs_nothing(on):
    S, db = on, on.backend.db_path
    item = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "mode": "off",
                                     "user_request": "User: turn review off"})["setting_requested"]
    popup.as_user("review_approve", {"item_id": item, "user_request": "terminal"}, "command-line")
    out = call(S, "manage_review", {"action": "set_mode", "project": "Alpha", "clear": True,
                                    "user_request": "User: back to normal review"}, app="Claude Code")
    assert out.get("now", {}).get("mode") == "auto_only", out
