"""Audit 46, M9 / L13 / L16 / L17 / L25 / L28: FlowState's settings file and the settings
commands. A real broken config.json (not a faked failure), parallel writers, the terminal
logging like the page, `approvals popup|cards add` asking for the user's own y, and the
display name rules. Temp folders only (conftest points FLOWSTATE_CONFIG_DIR into one)."""

import argparse
import json
import sqlite3
import threading

import pytest

from flowstate import approvals, cli_more, cli_review, install_name, paths, updates


class Tty:
    def __init__(self, ans):
        self.ans, self.asked = ans, []

    def ask(self, q):
        self.asked.append(q)
        return self.ans

    def close(self):
        pass


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config" / "config.json"


def broken(cfg, text='{"self_update": false, "offsite": {"repo": "me/backup"},'):
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return text


def log(db, scope):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT old_mode, new_mode, user_request FROM review_mode_log WHERE "
                            "scope = ? ORDER BY id", (scope,)).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- M9
def test_a_broken_config_file_is_never_overwritten_by_a_settings_change(cfg, capsys):
    text = broken(cfg)
    ns = argparse.Namespace(value="Office laptop", reset=False, db=None)
    assert cli_more.cmd_name(ns) == 1
    assert "can't be read" in capsys.readouterr().out
    for change in (lambda: install_name.set_name("Office laptop"), lambda: updates.set_on(False),
                   lambda: updates.set_self_update(True)):
        with pytest.raises(paths.ConfigUnreadable):
            change()
    assert cfg.read_text(encoding="utf-8") == text            # nothing else was erased
    assert cli_more.cmd_update(argparse.Namespace(action="check", sub="off", db=None)) == 1


def test_a_broken_config_file_keeps_self_update_off_and_the_relays_name(cfg):
    broken(cfg)
    assert updates.self_update_allowed() is False              # it may say false: fail safe
    with pytest.raises(paths.ConfigUnreadable):
        install_name.shared()
    from flowstate.relay import tunnel
    agent = tunnel.TunnelAgent.__new__(tunnel.TunnelAgent)
    agent.display_name = install_name.shared
    assert agent._name() is tunnel._NAME_UNREADABLE           # the relay's copy is left alone
    assert install_name.get() == install_name.DEFAULT          # pages still show a name
    for text in ("[1, 2]", "null"):                            # valid JSON, but not settings
        broken(cfg, text)
        assert updates.self_update_allowed() is False


def test_no_config_file_is_still_the_defaults(cfg):
    assert not cfg.exists()
    assert updates.self_update_allowed() is True and install_name.shared() is None
    install_name.set_name("Office laptop")
    updates.set_on(False)
    assert json.loads(cfg.read_text()) == {"display_name": "Office laptop", "update_check": False}


def test_the_page_says_the_file_is_broken_in_a_sentence(cfg, backend, seeded_db):
    from starlette.testclient import TestClient
    from flowstate.review_page import Page, create_app
    import re
    broken(cfg)
    p = Page(seeded_db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    c.get("/?key=" + p.launch_key)
    csrf = re.search(r'name="csrf" value="([^"]+)"', c.get("/").text).group(1)
    r = c.post("/settings/name", data={"csrf": csrf, "name": "Office laptop"},
               headers={"origin": "http://127.0.0.1:8770"})
    page = c.get(r.headers["location"]).text
    assert "can&#x27;t be read" in page and "Could not do that" not in page


# ---------------------------------------------------------------- L13
def test_parallel_settings_writers_neither_fail_nor_lose_a_change(cfg):
    errors = []

    def writer(n):
        for i in range(50):
            try:
                paths.update_config(lambda c, n=n, i=i: c.__setitem__(f"w{n}", i))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert json.loads(cfg.read_text()) == {f"w{n}": 49 for n in range(4)}
    assert sorted(p.name for p in cfg.parent.iterdir()) == ["config.json"]   # no temp or lock left


def test_a_lock_left_by_a_writer_that_died_is_taken_over(cfg, monkeypatch):
    import os
    import time
    cfg.parent.mkdir(parents=True, exist_ok=True)
    lock = cfg.with_name("config.json.lock")
    lock.write_text("")
    old = time.time() - paths.LOCK_STALE - 5
    os.utime(lock, (old, old))
    paths.update_config(lambda c: c.__setitem__("a", 1))
    assert json.loads(cfg.read_text()) == {"a": 1} and not lock.exists()


# ---------------------------------------------------------------- L16 / L17
def _approvals(db, what, arg=None, name=None):
    return cli_review.cmd_approvals(argparse.Namespace(what=what, arg=arg, name=name, db=str(db),
                                                       project=None, clear=False))


def test_terminal_settings_changes_are_logged_like_the_pages(backend, seeded_db, monkeypatch, cfg):
    db = seeded_db
    assert _approvals(db, "phone", "off") == 0
    assert log(db, "phone_approvals")[-1][1] == "off"
    monkeypatch.setattr(cli_review, "open_terminal", lambda: Tty("y"))
    assert _approvals(db, "popup", "add", "  My   Editor ") == 0
    assert "my editor" in approvals.popup_clients(db)          # cleaned like the page does
    assert log(db, "popup_clients")[-1][1] == "added my editor"
    assert _approvals(db, "cards", "add", "My Host") == 0 and "my host" in approvals.card_clients(db)
    assert log(db, "card_clients")[-1][1] == "added my host"
    assert _approvals(db, "cards", "remove", "my host") == 0
    assert updates.cli_check("off", lambda *_: None, db) == 0
    assert log(db, updates.CHECK_SCOPE)[-1][1] == "off"   # (the test run keeps it off)
    assert all(r[2] == approvals.TERMINAL_WORDS for r in log(db, "popup_clients"))


def test_adding_a_popup_or_card_app_needs_the_users_own_y(backend, seeded_db, monkeypatch, capsys):
    db = seeded_db
    for what, lister in (("popup", approvals.popup_clients), ("cards", approvals.card_clients)):
        monkeypatch.setattr(cli_review, "open_terminal", lambda: None)   # an agent's shell
        assert _approvals(db, what, "add", "sneaky-client") == 2
        assert "piped input doesn't count" in capsys.readouterr().out
        monkeypatch.setattr(cli_review, "open_terminal", lambda: Tty("n"))
        assert _approvals(db, what, "add", "sneaky-client") == 1
        assert "sneaky-client" not in lister(db)
        tty = Tty("y")
        monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
        assert _approvals(db, what, "add", "sneaky-client") == 0 and tty.asked
        assert "sneaky-client" in lister(db)
        monkeypatch.setattr(cli_review, "open_terminal", lambda: None)   # removing: no y needed
        assert _approvals(db, what, "remove", "sneaky-client") == 0
        assert "sneaky-client" not in lister(db)


def test_the_settings_page_lists_the_card_apps(backend, seeded_db):
    from flowstate.review_page import READ_ONLY, Page
    db = seeded_db
    edit = Page(db, 8770)
    assert edit.set_cards({"op": "add", "name": "My Host"}, "") == "Added my host."
    assert "my host" in approvals.card_clients(db)
    assert Page(db, 8770, open_link=True).set_cards({"op": "remove", "name": "my host"}, "") \
        == READ_ONLY


# ---------------------------------------------------------------- L25 / L28
def test_the_name_rule_doesnt_depend_on_the_unicode_version():
    unassigned = "\U000E0080"   # unassigned in every Unicode version so far (category Cn)
    import unicodedata
    assert unicodedata.category(unassigned) == "Cn"
    assert install_name.clean("Office " + unassigned) == "Office " + unassigned
    for bad in ("a​b", "a‮b", "ab", "a\nb", "a\x07b"):   # Cf, Cf, Co, Cc, Cc
        with pytest.raises(install_name.BadName):
            install_name.clean(bad)


def test_the_name_says_normally_within_a_minute(cfg, capsys):
    install_name.cli("Office laptop", False, None, print)
    assert "normally within a minute" in capsys.readouterr().out
