"""Task 55 (re-audit 53), the review pages, settings and rules area:

N-F5   the no-key link page can't resume a paused app, and uses the MCP tool's "turns review
       off" rule (clearing a project's own mode while the install is off; install-wide off
       with approvals on for one project only), as does manage_review set_mode
N-F20  a stale review-page cookie doesn't lock the user out; visits without a cookie never
       push out a session used recently
N-F21  a damaged config.json keeps the update check off; `remote lan-tokens` refuses it in a
       plain sentence and logs its changes
N-F23  the settings lock: a crashed writer doesn't block, writers in several processes keep
       every change
N-F24  `flowstate rules list` says who set each rule; `flowstate rules confirm`; rules waiting
       for the user have their own cap and don't fill the active rules' one
N-F26  the popup walk never shows a box for a hard rule (end to end)
N-F40  search "D12": the decision in full, a visible cut beyond FULL_MAX, "No decision D12"
N-F41  `flowstate review-page --no-browser` shows its working one-time link
Info   a hand-edited default display name is never sent to the relay
N-F33  `flowstate update --rollback` to a version before 2.4.4 warns about the passphrase
Temp databases and folders only (conftest.py)."""

import argparse
import errno
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp import types
from starlette.testclient import TestClient

from conftest import REPO
from flowstate import (activity, approvals, cli, cli_data, cli_review, install_name, meaning, paths,
                       review, review_mode, review_page, rules, updates)
from flowstate.review_page import COOKIE, Page, create_app
from test_phone_link_and_rule_removal import call
from test_review_without_terminal import acc, call as walk_call

PORT = 8770
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
CSRF = re.compile(r'name="csrf" value="([^"]+)"')
PY = sys.executable


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def link_page(db):
    p = Page(db, PORT, open_link=True, settings="read")
    return p, TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)


def edit_page(db):
    p = Page(db, PORT, settings="edit", secret_path=True)
    return p, TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)


def answer(c, r) -> str:
    import html
    assert r.status_code == 303, r.text
    m = re.search(r"<p class='ok'>(.*?)</p>", c.get(r.headers["location"]).text, re.S)
    return html.unescape(m.group(1)) if m else ""


@pytest.fixture(autouse=True)
def _approvals_off(seeded_db):
    yield
    for pid in (None, 1, 2):
        try:
            approvals.set_on(seeded_db, None if pid else False, pid)
        except Exception:  # noqa: BLE001
            pass


# ================================================================ N-F5
def test_link_page_never_resumes_a_paused_app_but_can_still_pause_one(backend, seeded_db):
    db = seeded_db
    activity.set_paused(db, "Grok Bot", True, "command-line")
    for on in (False, True):   # approvals off or on: the paused app could reach this page itself
        approvals.set_on(db, on)
        _p, c = link_page(db)
        page = c.get("/").text
        assert "flowstate apps resume --app &quot;Grok Bot&quot;" in page
        assert not re.search(r'name="app" value="Grok Bot">\s*<input type="hidden" name="paused" '
                             r'value="0">', page)                    # no Resume button for it
        csrf = CSRF.search(page).group(1)
        r = c.post("/app", data={"csrf": csrf, "app": "Grok Bot", "paused": "0"}, headers=ORIGIN)
        assert answer(c, r) == review_page.LINK_RESUME.format(app="Grok Bot")
        assert activity.is_paused(db, "Grok Bot")
    r = c.post("/app", data={"csrf": csrf, "app": "Cursor", "paused": "1"}, headers=ORIGIN)
    assert answer(c, r).startswith("Cursor: paused") and activity.is_paused(db, "Cursor")
    # the page `flowstate review-page` opens still resumes
    p, e = edit_page(db)
    r = e.get(f"{p.base}/?key={p.launch_key}")
    csrf = CSRF.search(e.get(r.headers["location"]).text).group(1)
    r = e.post(f"{p.base}/app", data={"csrf": csrf, "app": "Grok Bot", "paused": "0"}, headers=ORIGIN)
    assert r.status_code == 303 and not activity.is_paused(db, "Grok Bot")


def test_link_page_cant_turn_review_off_by_clearing_a_projects_own_mode(backend, seeded_db):
    db = seeded_db
    review_mode.set_mode(db, "off", None, None, "the user", "test")           # install: off
    review_mode.set_mode(db, "everything", None, 1, "the user", "test")       # Alpha: its own
    approvals.set_on(db, True)
    _p, c = link_page(db)
    csrf = CSRF.search(c.get("/").text).group(1)
    r = c.post("/mode", data={"csrf": csrf, "project_id": "1", "mode": "auto_only", "clear": "on"},
               headers=ORIGIN)
    assert "turning review off isn't done on this page" in answer(c, r)
    assert review_mode.get(db, 1)["mode"] == "everything"
    # clearing it while the install is on (review stays on) is fine
    review_mode.set_mode(db, "auto_only", None, None, "the user", "test")
    r = c.post("/mode", data={"csrf": csrf, "project_id": "1", "mode": "auto_only", "clear": "on"},
               headers=ORIGIN)
    assert answer(c, r).startswith("Review mode for project:1")


def test_link_page_cant_turn_review_off_install_wide_when_one_project_has_approvals(backend, seeded_db):
    db = seeded_db
    approvals.set_on(db, True, 1)                  # Alpha only
    assert not approvals.is_on(db) and approvals.is_on(db, 1)
    _p, c = link_page(db)
    csrf = CSRF.search(c.get("/").text).group(1)
    r = c.post("/mode", data={"csrf": csrf, "mode": "off"}, headers=ORIGIN)
    assert "turning review off isn't done on this page" in answer(c, r)
    assert review_mode.get(db)["mode"] == "auto_only"
    # Alpha has its own mode: install-wide off no longer reaches it, so it goes ahead
    review_mode.set_mode(db, "everything", None, 1, "the user", "test")
    r = c.post("/mode", data={"csrf": csrf, "mode": "off"}, headers=ORIGIN)
    assert answer(c, r).startswith("Review mode for install: off")


def test_the_tool_queues_install_wide_off_when_one_project_has_approvals(backend, seeded_db):
    db = seeded_db
    approvals.set_on(db, True, 1)
    _r, out = call(backend, "manage_review", {"action": "set_mode", "mode": "off",
                                              "user_request": "The user said: turn review off"})
    assert out.get("setting_requested"), out
    assert review_mode.get(db)["mode"] == "auto_only"


def test_turns_off_lists_what_a_change_reaches(backend, seeded_db):
    db = seeded_db
    assert review_mode.turns_off(db, "off", None) == [None, 1, 2]
    assert review_mode.turns_off(db, "auto_only", None) == []
    review_mode.set_mode(db, "types", ["todo"], 2, "the user", "test")
    assert review_mode.turns_off(db, "off", None) == [None, 1]
    assert review_mode.turns_off(db, "off", 2) == [2]
    assert review_mode.turns_off(db, None, 2, clear=True) == []           # install still on
    review_mode.set_mode(db, "off", None, None, "the user", "test")
    assert review_mode.turns_off(db, None, 2, clear=True) == [2]
    assert review_mode.turns_off(db, "off", None) == []                   # already off


# ================================================================ N-F20
def test_a_stale_cookie_doesnt_lock_the_user_out_of_settings(backend, seeded_db):
    p, e = edit_page(seeded_db)
    # left by a 2.4.3 page (or any web server on 127.0.0.1) for the whole site
    e.cookies.set(COOKIE, "stale-value-from-an-older-page", domain="127.0.0.1", path="/")
    r = e.get(f"{p.base}/?key={p.launch_key}")
    assert r.status_code == 303
    page = e.get(f"{p.base}/")
    assert page.status_code == 200 and "/settings/approvals" in page.text
    csrf = CSRF.search(page.text).group(1)
    r = e.post(f"{p.base}/settings/update-check", data={"csrf": csrf, "state": "off"}, headers=ORIGIN)
    assert r.status_code == 303
    # without the one-time key, the stale cookie alone still gets nothing
    other = TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}", follow_redirects=False)
    other.cookies.set(COOKIE, "stale-value-from-an-older-page", domain="127.0.0.1", path="/")
    assert other.get(f"{p.base}/").status_code == 403


def test_cookie_values_are_all_read():
    assert review_page._cookie_values("a=1; fs_review=new; fs_review=old; b=2", "fs_review") == \
        ["new", "old"]
    assert review_page._cookie_values("", "fs_review") == []
    assert review_page._cookie_values("fs_review=" + "x" * 500, "fs_review") == []


def test_visits_without_a_cookie_never_push_out_a_recently_used_session(backend, seeded_db,
                                                                         monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(review_page, "_clock", lambda: now[0])
    p, user = link_page(seeded_db)
    assert user.get("/").status_code == 200
    codes = []
    for minute in range(20):                     # a program, 10 new visits every minute
        now[0] += 60
        for _ in range(12):
            other = TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}")
            codes.append(other.get("/").status_code)
        if minute == 10:
            assert user.get("/").status_code == 200   # the user looks again (used recently)
    assert user.get("/").status_code == 200           # still signed in
    assert 429 in codes
    r = TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}").get("/")
    assert r.status_code == 429 and r.text == review_page.LINK_BUSY and "flowstate review-page" in r.text
    # sessions nobody used for PROTECT_SECONDS may go
    now[0] += review_page.PROTECT_SECONDS + 60
    assert TestClient(create_app(p), base_url=f"http://127.0.0.1:{PORT}").get("/").status_code == 200


def test_the_one_time_key_page_never_refuses_its_key(backend, seeded_db):
    for _ in range(3):
        p, e = edit_page(seeded_db)
        for _ in range(review_page.NEW_PER_MINUTE * 3):
            assert p.new_session() is not None
        assert e.get(f"{p.base}/?key={p.launch_key}").status_code == 303


# ================================================================ N-F21
def _damage_config():
    f = paths.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text('{"update_check": false,', encoding="utf-8")   # a hand-edit typo
    return f


def test_a_damaged_config_keeps_the_update_check_off(monkeypatch):
    monkeypatch.delenv("FLOWSTATE_UPDATE_CHECK", raising=False)
    assert updates.is_on()                      # no file: on (the default)
    _damage_config()
    assert updates.is_on() is False
    out = []
    updates._show({}, out.append)
    assert "off, because FlowState's settings file can't be read" in out[0]


def test_lan_tokens_refuses_a_damaged_config_and_logs_its_changes(seeded_db, monkeypatch, capsys):
    f = _damage_config()
    before = f.read_text()
    assert cli.main(["remote", "lan-tokens", "off", "--db", str(seeded_db)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("Not changed: FlowState's settings file") and "Traceback" not in out
    assert f.read_text() == before
    f.unlink()
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 0)            # typed y
    assert cli.main(["remote", "lan-tokens", "on", "--db", str(seeded_db)]) == 0
    assert cli.main(["remote", "lan-tokens", "off", "--db", str(seeded_db)]) == 0
    assert _q(seeded_db, "SELECT old_mode, new_mode FROM review_mode_log WHERE scope = "
                         "'config:lan_remote_tokens' ORDER BY id") == \
        [("refused", "allowed"), ("allowed", "refused")]


# ================================================================ N-F23
_HOLDER = """
import os, sys, time
from flowstate import paths
with paths._lock(paths.config_file()):
    print("held", flush=True)
    time.sleep(60)
"""

_WRITER = """
import sys
from flowstate import paths
n = sys.argv[1]
for i in range(40):
    paths.update_config(lambda c, i=i: c.__setitem__("p" + n, i))
"""


def _env():
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO / "mcp-server"), env.get("PYTHONPATH", "")])
    return env


def test_a_crashed_settings_writer_doesnt_block_the_next_one(monkeypatch):
    paths.config_dir().mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen([PY, "-c", _HOLDER], stdout=subprocess.PIPE, env=_env(), text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        monkeypatch.setattr(paths, "LOCK_WAIT", 0.3)
        with pytest.raises(TimeoutError):            # held by a live writer: wait, then refuse
            paths.update_config(lambda c: c.__setitem__("a", 1))
        proc.send_signal(signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
        proc.wait(10)
        t0 = time.monotonic()
        paths.update_config(lambda c: c.__setitem__("a", 2))    # it crashed: no 30 s wait
        assert time.monotonic() - t0 < 1
        assert json.loads(paths.config_file().read_text()) == {"a": 2}
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stdout.close()


def _refuse_lock_opens(monkeypatch, times):
    """os.open on the lock file answers as Windows does while the last holder removes it."""
    real_open, left = os.open, {"n": times}

    def fake(path, *a, **k):
        if str(path).endswith("config.json.lock") and left["n"] > 0:
            left["n"] -= 1
            raise PermissionError(errno.EACCES, "Permission denied", str(path))
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", fake)


def test_a_lock_file_being_removed_counts_as_busy_and_is_retried(monkeypatch):
    _refuse_lock_opens(monkeypatch, 3)
    paths.update_config(lambda c: c.__setitem__("retried", 1))
    assert paths.load_config()["retried"] == 1


def test_a_lock_file_that_stays_refused_ends_in_the_busy_message(monkeypatch):
    monkeypatch.setattr(paths, "LOCK_WAIT", 0.1)
    _refuse_lock_opens(monkeypatch, 10 ** 6)
    with pytest.raises(TimeoutError, match="Another FlowState command is changing"):
        paths.update_config(lambda c: c.__setitem__("never", 1))


def test_writers_in_several_processes_keep_every_change_even_after_a_left_lock():
    d = paths.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json.lock").write_text("left by a writer that died")
    old = time.time() - 3600
    os.utime(d / "config.json.lock", (old, old))
    procs = [subprocess.Popen([PY, "-c", _WRITER, str(n)], env=_env()) for n in range(4)]
    for p in procs:
        assert p.wait(60) == 0
    assert json.loads(paths.config_file().read_text()) == {f"p{n}": 39 for n in range(4)}
    assert sorted(p.name for p in d.iterdir() if p.name.startswith("config")) == ["config.json"]


# ================================================================ N-F24
def _legacy_rule(db, text="Never deploy on Friday"):
    """A rule an older version let an app add on its own (no set_by_user)."""
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("INSERT INTO project_rules (project_id, rule, created_at, created_by) "
                           "VALUES (1, ?, '2026-01-01 00:00:00', 'Grok Build')", (text,))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_rules_list_says_who_set_each_rule_like_the_brief(backend, seeded_db, capsys):
    db = seeded_db
    mine = rules.add_by_user(db, 1, "Never push to main")["rule"]["id"]
    old = _legacy_rule(db)
    assert cli.main(["rules", "list", "--project", "Alpha", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert f"R{mine} (set by the user): Never push to main" in out
    assert f"R{old} (added by Grok Build, not confirmed by the user)" in out
    listed = {r["id"]: r["set_by"] for r in rules.list_rules(db, 1)["rules"]}
    assert listed[mine] == "set by the user"


def test_rules_confirm_makes_an_old_apps_rule_the_users(backend, seeded_db, monkeypatch, capsys):
    db = seeded_db
    rid = _legacy_rule(db)
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)       # no terminal: refused
    assert cli.main(["rules", "confirm", f"R{rid}", "--db", str(db)]) == 2
    assert _q(db, "SELECT set_by_user FROM project_rules WHERE id = ?", rid) == [(None,)]
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 0)               # typed y
    assert cli.main(["rules", "confirm", f"R{rid}", "--db", str(db)]) == 0
    assert "now confirmed by you" in capsys.readouterr().out
    _r, brief = call(backend, "start_here", {"project": "Alpha"})
    assert f"R{rid} (set by the user): Never deploy on Friday" in json.dumps(brief)
    assert cli.main(["rules", "confirm", f"R{rid}", "--db", str(db)]) == 0
    assert "already yours" in capsys.readouterr().out
    assert cli.main(["rules", "confirm", "999", "--db", str(db)]) == 1


def _ask(S, text, app):
    return call(S, "manage_rules", {"action": "add", "project": "Alpha", "rule": text}, app=app)[1]


def test_rules_waiting_for_the_user_have_their_own_cap(backend, seeded_db):
    S, db = backend, seeded_db
    per_app, total = rules.MAX_PENDING_PER_APP, rules.MAX_PENDING
    long = "Never do thing number {n}: " + "x" * 250
    # waiting rules of ~280 characters, far over 2,000 together: all queued, up to the caps
    for n in range(total):
        out = _ask(S, long.format(n=n), app=f"App {n // per_app}")
        assert out.get("queued_for_review"), (n, out)
        if n == per_app - 1:   # App 0 has its share waiting
            out = _ask(S, "Never do thing number x", app="App 0")
            assert out["error"] == "rules_queue_full" and \
                f"This app already has {per_app}" in out["message"]
    out = _ask(S, "Never do thing number y", app="Another app")
    assert out["error"] == "rules_queue_full" and f"{total} hard rules already wait" in out["message"]
    # the user's own rules don't wait and aren't kept out by the waiting ones
    assert rules.add_by_user(db, 1, "Never push to main")["rule"]["id"]
    # asking again for a rule that already waits still points to the same item
    assert _ask(S, long.format(n=0), app="App 9").get("queued_for_review")


# ================================================================ N-F26
def test_the_popup_walk_asks_no_box_for_hard_rules_end_to_end(backend, seeded_db):
    S, db = backend, seeded_db
    _ask(S, "Always deploy on Friday", app="Cursor")
    todo = review.add_item(db, 1, "todo", "Something else", "", {}, "cursor")
    seen = []
    out = walk_call(S, "manage_review", {"action": "walk"}, seen=seen,
                    answers=[acc(approve=True), acc(approve=True), acc(approve=True)])
    assert len(seen) == 1 and "Something else" in seen[0].message
    assert out["walked"] == [f"#{todo}: approved"]
    assert out["hard_rules"] and out["hard_rules_note"]
    assert _q(db, "SELECT COUNT(*) FROM project_rules") == [(0,)]
    assert _q(db, "SELECT status FROM review_items WHERE kind = 'rule'") == [("pending",)]


# ================================================================ N-F40
def _decision(db, title, details):
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("INSERT INTO conversations (project_id, user_prompt_summary, "
                           "assistant_response_summary, key_decisions) VALUES (1, ?, ?, ?)",
                           (title, details, json.dumps([title])))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_a_decision_number_gives_it_in_full(backend, seeded_db):
    db = seeded_db
    details = "Because " + "reason " * 1400                  # ~9,800 characters
    did = _decision(db, "Use SQLite", details)
    out = meaning.search(db, f"D{did}")
    assert out["mode"] == "id" and out["results"][0]["snippet"] == details and "note" not in out
    huge = "y" * (meaning.FULL_MAX + 500)                   # an older record, before the cap
    big = _decision(db, "Big one", huge)
    out = meaning.search(db, f"decision {big}")
    snip = out["results"][0]["snippet"]
    assert snip.endswith(meaning.CUT_MARK) and len(snip) == meaning.FULL_MAX + 1
    assert f"D{big} is longer than" in out["note"]


def test_an_unknown_decision_number_says_so(backend, seeded_db):
    db = seeded_db
    _decision(db, "Use SQLite", "details")
    out = meaning.search(db, "D999")
    assert out["note"].startswith("No decision D999.") and out["mode"] != "id"
    out = meaning.search(db, "D999", project_id=2)
    assert out["note"].startswith("No decision D999 in Beta Project.")
    assert "note" not in meaning.search(db, "SQLite", mode="keyword")


# ================================================================ N-F41
class _Tty:
    def __init__(self):
        self.said = []

    def tell(self, text):
        self.said.append(text)

    def close(self):
        pass


def test_review_page_no_browser_shows_the_working_one_time_link(backend, seeded_db, monkeypatch,
                                                                capsys):
    import uvicorn
    tty = _Tty()
    apps = []
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self, sockets=None: (
        apps.append(self.config.app), [s.close() for s in sockets or []]))
    review_page.serve(seeded_db, port=0, open_browser=False)
    out = capsys.readouterr().out
    assert "key=" not in out and not re.search(r"http://127\.0\.0\.1:\d+/\s", out)
    m = re.fullmatch(r"One-time link: http://127\.0\.0\.1:(\d+)(/s/[\w-]+)/\?key=([\w-]+)", tty.said[0])
    assert m, tty.said
    c = TestClient(apps[0], base_url=f"http://127.0.0.1:{m.group(1)}", follow_redirects=False)
    r = c.get(f"{m.group(2)}/?key={m.group(3)}")
    assert r.status_code == 303 and c.get(r.headers["location"]).status_code == 200
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)
    with pytest.raises(SystemExit, match="--no-browser shows the one-time link on your terminal"):
        review_page.serve(seeded_db, port=0, open_browser=False)


# ================================================================ Info: the default name
@pytest.mark.parametrize("stored", ["Main FlowState", " main  flowstate ", "MAIN FLOWSTATE"])
def test_the_default_name_in_any_spelling_is_never_shared(stored):
    paths.save_config({"display_name": stored})
    assert install_name.shared() is None and install_name.is_default()
    assert install_name.get() == install_name.DEFAULT
    install_name.set_name(" main flowstate ")
    assert "display_name" not in paths.load_config()
    paths.save_config({"display_name": "Office laptop"})
    assert install_name.shared() == "Office laptop"


# ================================================================ N-F33
def test_rollback_before_2_4_4_warns_about_the_passphrase():
    from flowstate import remote_auth, selfupdate
    note = selfupdate.Updater._passphrase_note
    assert note("2.4.3") is None                               # no passphrase set
    store = remote_auth.AuthStore()
    store.set_passphrase("a long owner passphrase")
    text = note("2.4.3")
    assert "flowstate remote passphrase" in text and "lockout" in text
    assert note("2.4.4") is None and note("2.5.0") is None and note(None) is None
    conn = sqlite3.connect(remote_auth.auth_db_path())     # an older, lighter hash: 2.4.3 reads it
    conn.execute("UPDATE owner SET salt = ? WHERE id = 1", (os.urandom(16),))
    conn.commit()
    conn.close()
    assert note("2.4.3") is None
