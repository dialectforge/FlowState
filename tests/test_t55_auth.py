"""Task 55, remote auth area (re-audit 53): N-F10 passphrase tries at the same moment, N-F22
lookalike and shared names, N-F27 remote tokens sent over the plain LAN, N-F18 resuming one
app on a shared key. Temp databases, settings and fake hashes only."""

import hashlib
import sqlite3
import threading
import time

import pytest

from flowstate import activity, app_keys, cli, identity
from flowstate import remote_auth as RA
from test_data_security_audit import _lan_app, _post
from test_relay_app_label import http_calls, suggest

PASS = "correct horse battery staple"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(tmp_path / "config" / "remote-auth.db"))
    return RA.AuthStore()


# ---------------------------------------------------------------- N-F10
@pytest.fixture
def counted_hash(monkeypatch):
    """_scrypt replaced by a quick hash that counts calls and the most running at once."""
    seen = {"calls": 0, "now": 0, "peak": 0}
    lock = threading.Lock()

    def fake(passphrase, salt):
        with lock:
            seen["calls"] += 1
            seen["now"] += 1
            seen["peak"] = max(seen["peak"], seen["now"])
        time.sleep(0.05)   # wide enough for every thread to be inside at once without a limit
        with lock:
            seen["now"] -= 1
        return hashlib.sha256(passphrase.encode() + bytes(salt)).digest()

    monkeypatch.setattr(RA, "_scrypt", fake)
    return seen


def _burst(fn, n=24):
    gate = threading.Barrier(n)
    out = []

    def go():
        gate.wait()
        out.append(fn())

    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def test_wrong_passphrases_at_the_same_moment_stop_at_the_limit(store, counted_hash, monkeypatch):
    """N-F10: 24 wrong tries at once got past the 5-try lockout, each one hashing."""
    monkeypatch.setattr(RA, "PASSPHRASE_WAIT", 30.0, raising=False)   # nobody gets "busy"
    store.set_passphrase(PASS)
    counted_hash.update(calls=0, peak=0)
    results = _burst(lambda: store.check_passphrase("wrong passphrase!!"))
    assert results == [False] * 24
    assert counted_hash["calls"] <= RA.LOCKOUT_FAILS, counted_hash
    assert counted_hash["peak"] <= RA.PASSPHRASE_HASHES, counted_hash
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == RA.LOCKOUT_FAILS
    assert store.locked_for() > 0
    assert store.try_passphrase(PASS) == "locked"        # even the right one waits, unhashed
    assert counted_hash["calls"] <= RA.LOCKOUT_FAILS


def test_tries_at_once_never_hash_more_than_the_limit(store, counted_hash, monkeypatch):
    """N-F10: each try costs 128 MiB; a burst may not run them all at once."""
    monkeypatch.setattr(RA, "PASSPHRASE_WAIT", 0.01)
    monkeypatch.setattr(RA, "LOCKOUT_FAILS", 1000)
    store.set_passphrase(PASS)
    counted_hash.update(calls=0, peak=0)
    results = _burst(lambda: store.try_passphrase("wrong passphrase!!"))
    assert counted_hash["peak"] <= RA.PASSPHRASE_HASHES
    assert "busy" in results and set(results) <= {"wrong", "busy"}
    with store._conn() as c:   # a "busy" answer wasn't counted
        assert c.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == results.count("wrong")


def test_a_right_passphrase_is_not_counted(store, counted_hash):
    store.set_passphrase(PASS)
    for _ in range(RA.LOCKOUT_FAILS + 2):
        assert store.try_passphrase(PASS) == "ok"
    assert store.locked_for() == 0
    assert store.try_passphrase("wrong passphrase!!") == "wrong"


def test_the_consent_page_says_busy_without_hashing(store, monkeypatch):
    monkeypatch.setattr(RA, "PASSPHRASE_WAIT", 0.0)
    store.set_passphrase(PASS)
    assert RA._HASH_SLOTS.acquire(timeout=1)
    try:
        assert store.try_passphrase(PASS) == "busy"
    finally:
        RA._HASH_SLOTS.release()
    with store._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM failures").fetchone()[0] == 0


# ---------------------------------------------------------------- N-F22
CYR_C = "С"     # Cyrillic capital Es, looks like C


@pytest.mark.parametrize("name, why", [
    (CYR_C + "ursor", "taken"),                   # Cyrillic С
    ("Cursоr", "taken"),                    # Cyrillic о
    ("Cur​sor", "invisible"),               # zero-width space
    ("Cursor‎", "invisible"),               # direction mark
    ("Ｃｕｒｓｏｒ", "special forms"),   # full-width
    ("Cursoŕ", "taken"),                        # an accent
    ("CURS0R", "taken"),                         # a zero for the o
])
def test_lookalike_names_are_refused(store, name, why):
    store.add_token_client("Cursor", list(RA.SCOPES))
    with pytest.raises(ValueError, match=why):
        store.add_token_client(name, list(RA.SCOPES))
    with pytest.raises(ValueError, match=why):
        app_keys.add(store, name)


def test_keys_and_tokens_share_one_name_list(store):
    """N-F22: a key and a token could both be "Cursor", and pausing one paused both."""
    cid, _ = store.add_token_client("Cursor", list(RA.SCOPES))
    with pytest.raises(ValueError, match=f"taken \\(by {cid}"):
        app_keys.add(store, "cursor")
    kid, _ = app_keys.add(store, "Desk key")
    with pytest.raises(ValueError, match=f"taken \\(by {kid}"):
        store.add_token_client("DESK KEY", list(RA.SCOPES))
    with pytest.raises(ValueError, match=f"taken \\(by {kid}"):
        store.rename_client("Cursor", "Desk key")
    app_keys.revoke(store, key_id=kid)
    with pytest.raises(ValueError, match="taken"):
        store.add_token_client("Desk key", list(RA.SCOPES))   # a revoked key's name too


@pytest.mark.parametrize("name", ["unknown", "Unknоwn", "phone", "Phone (testbox)", "user",
                                  "Shared token", "FlowState", "Relay (unknown app)", "owner"])
def test_flowstates_own_names_are_reserved(store, name):
    with pytest.raises(ValueError, match="FlowState uses itself"):
        store.add_token_client(name, list(RA.SCOPES))
    with pytest.raises(ValueError, match="FlowState uses itself"):
        app_keys.add(store, name)
    store.add_token_client("My phone app " + str(abs(hash(name)) % 1000), list(RA.SCOPES))


@pytest.mark.parametrize("name", ["Grok (via Cursor)", "Grok (signed in as Cursor)",
                                  "Grok [VIA Cursor]", "Grok ❨via Cursor❩"])
def test_token_and_key_names_cant_carry_flowstates_marks(store, name):
    with pytest.raises(ValueError, match="via"):
        store.add_token_client(name, list(RA.SCOPES))
    with pytest.raises(ValueError, match="via"):
        app_keys.add(store, name)


def _oauth(store, cid, name):
    from mcp.shared.auth import OAuthClientInformationFull
    store.save_oauth_client(OAuthClientInformationFull(
        client_id=cid, client_name=name, redirect_uris=["https://claude.ai/api/mcp/auth_callback"]))
    return store.client_row(cid)["name"]


def test_oauth_app_names_go_through_the_same_checks(store):
    """N-F22: OAuth app names skipped the name checks."""
    store.add_token_client("Cursor", list(RA.SCOPES))
    app_keys.add(store, "Desk key")
    assert _oauth(store, "o1", "Cur​sor") == "Cursor (2)"            # invisible, then taken
    assert _oauth(store, "o2", CYR_C + "ursor") == CYR_C + "ursor (3)"     # lookalike: taken
    assert _oauth(store, "o3", "desk key") == "desk key (2)"              # a key's name
    assert _oauth(store, "o4", "unknown") == "OAuth app unknown"
    assert _oauth(store, "o5", "Grok (via Cursor)") == "Grok"
    assert _oauth(store, "o6", "Ｇｒｏｋ") == "Grok (2)"   # full-width Grok
    assert _oauth(store, "o7", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8") == \
        "unnamed OAuth app"
    assert _oauth(store, "o8", "Claude") == "Claude"
    assert _oauth(store, "o9", "Claude") == "Claude (2)"                  # as before


@pytest.mark.parametrize("sent", [
    "Grok Bot (vіa Laptop key)",              # Cyrillic і
    "Grok Bot （via Laptop key）",         # full-width brackets
    "Grok Bot ​(via Laptop key)",             # zero-width space
    "Grok Bot ‮(via Laptop key)",             # right-to-left override
    "Grok Bot ⁽signed in as Laptop key⁾",  # superscript brackets
    "Grok Bot [via Laptop key]",
    "Grok Bot (sіgned іn as Laptop key)",
])
def test_an_app_cant_imitate_a_sign_in_with_lookalikes(sent):
    """L20 / N-F22: the plain forms were stripped; these got through."""
    assert identity.resolve(sent, False).app == "Grok Bot"
    assert identity.resolve(sent, True, None, {}, listener="lan").app == "Grok Bot"


def test_names_apps_send_are_tidied_and_cant_be_flowstates_own():
    assert identity.resolve("Cur​sor", False).app == "Cursor"
    assert identity.resolve("‮Cursor", False).app == "Cursor"
    for sent in ("user", "Unknоwn", "phone", "Shared token (legacy)"):
        assert identity.resolve(sent, False).app == "unknown", sent
        assert identity.resolve(sent, True, None, {}, listener="lan").app == "unknown", sent


def test_names_made_before_still_work(store):
    """Pairs made before 2.4.4 (a key and a token both "Cursor") keep working (M12)."""
    cid, tok = store.add_token_client("Cursor", list(RA.SCOPES))
    key = RA.APP_KEY_PREFIX + "old-key-made-before-task-55"
    with store._conn() as c:
        c.execute("INSERT INTO app_keys (id, name, hash, created_at) VALUES ('key_old', "
                  "'Cursor', ?, ?)", (RA._h(key), time.time()))
    assert RA.TokenChecker(store, None).check(tok).client_name == "Cursor"
    assert app_keys.check(store, key).client_name == "Cursor"


# ---------------------------------------------------------------- N-F27
def test_a_token_sent_over_the_plain_lan_is_flagged_not_revoked(seeded_db, tmp_path, store,
                                                                capsys):
    """N-F27: refused, but it crossed the network readable; the user wasn't told."""
    from flowstate.brief import build_brief
    cid, tok = store.add_token_client("Relay App", list(RA.SCOPES))
    other, _ = store.add_token_client("Quiet app", list(RA.SCOPES))
    app = _lan_app(seeded_db, tmp_path)
    assert _post(app, tok, "127.0.0.1") != 401          # this computer: nothing crossed
    assert store.lan_exposures() == {}
    assert _post(app, tok, "192.0.2.10") == 401
    assert _post(app, tok, "192.0.2.11") == 401
    seen = store.lan_exposures()
    assert list(seen) == [cid] and seen[cid]["times"] == 2 and seen[cid]["last_ip"] == "192.0.2.11"
    assert store.lookup(tok, ("static",)) is not None   # still works: not revoked
    assert cli.main(["remote", "clients"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "plain HTTP" in out and f"flowstate remote revoke --client {cid}" in out
    assert out.count("WARNING") == 1 and "192.0.2.11" in out
    brief = build_brief(seeded_db, {"id": 1, "name": "Alpha"})
    assert "Security warning" in brief and cid in brief and "Relay App" in brief
    assert "192.0.2" not in brief                        # no LAN address for AI apps
    assert other not in brief
    assert cli.main(["remote", "keep-token", "--client", cid]) == 0
    assert store.lan_exposures() == {} and "Security warning" not in \
        build_brief(seeded_db, {"id": 1, "name": "Alpha"})
    assert _post(app, tok, "192.0.2.10") == 401          # sent that way again: back
    assert cid in store.lan_exposures()
    store.revoke_client(cid)
    assert store.lan_exposures() == {} and RA.exposure_lines() == []


def test_the_brief_never_creates_the_settings_database(seeded_db, tmp_path):
    from flowstate.brief import build_brief
    assert not RA.auth_db_path().exists()
    build_brief(seeded_db, {"id": 1, "name": "Alpha"})
    assert not RA.auth_db_path().exists()


# ---------------------------------------------------------------- N-F18
@pytest.fixture
def desk_key(backend, store):
    kid, key = app_keys.add(store, "Desk key", workspace="desk-ws")
    yield kid, key
    backend.backend.tracker = None


def test_resuming_one_app_keeps_a_shared_key_paused(backend, desk_key, capsys):
    """N-F18: two apps on one key; resuming one resumed the key, so the other, still paused,
    came back by renaming itself."""
    db = backend.backend.db_path
    key = desk_key[1]
    http_calls(db, key, [suggest("Grok write")], "Grok Bot", relay=False)
    http_calls(db, key, [suggest("Other write")], "Other Bot", relay=False)
    assert cli.main(["apps", "pause", "--app", "Grok Bot", "--db", str(db)]) == 0
    assert cli.main(["apps", "pause", "--app", "Other Bot", "--db", str(db)]) == 0
    capsys.readouterr()
    assert cli.main(["apps", "resume", "--app", "Other Bot", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "'Desk key' stays paused" in out and "'Grok Bot'" in out
    assert activity.is_paused(db, "Desk key") and not activity.is_paused(db, "Other Bot")
    dodge = http_calls(db, key, [suggest("Renamed write")], "Grok Bot 2", relay=False)
    assert dodge[0]["error"] == "paused_by_user"
    # the last paused app on it resumed: the key too
    assert cli.main(["apps", "resume", "--app", "Grok Bot", "--db", str(db)]) == 0
    assert not activity.is_paused(db, "Desk key")
    back = http_calls(db, key, [suggest("Back again")], "Grok Bot 2", relay=False)
    assert "error" not in back[0]


def test_the_page_resume_button_keeps_a_shared_key_paused(backend, desk_key):
    from flowstate.review_page import Page
    db = backend.backend.db_path
    key = desk_key[1]
    http_calls(db, key, [suggest("Grok write")], "Grok Bot", relay=False)
    http_calls(db, key, [suggest("Other write")], "Other Bot", relay=False)
    page = Page(db, 8770)
    page.set_app({"app": "Grok Bot", "paused": "1"})
    page.set_app({"app": "Other Bot", "paused": "1"})
    msg = page.set_app({"app": "Other Bot", "paused": "0"})
    assert "stays paused" in msg and activity.is_paused(db, "Desk key")
