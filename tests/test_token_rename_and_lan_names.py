"""`flowstate remote rename` and both names on the LAN (2.4.4, task 47).

Part 1: a remote token / OAuth app can be renamed without a new token; the old name is kept
(client_names), so the pause, own-item and undo checks still follow the token; past records
keep their name. Part 2: per-app keys and remote tokens on the LAN listener record the app's
own name with the key's / token's name beside it ("Grok Bot (via my-key)"), like the relay and
the own tunnel; the shared LAN token is unchanged. Temp databases and settings only."""

import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from flowstate import activity, app_keys, cli, identity, review, who
from flowstate import remote_auth as RA
from test_honest_caller_names import tunnel_calls
from test_relay_app_label import http_calls, queued, suggest


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A settings database of this test's own (the CLI opens it through FLOWSTATE_AUTH_DB)."""
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(tmp_path / "remote-auth.db"))
    return RA.AuthStore()


@pytest.fixture
def cursor(backend, store):
    """A remote token the user once named after another app."""
    cid, tok = store.add_token_client("Cursor", list(RA.SCOPES))
    yield cid, tok
    backend.backend.tracker = None


@pytest.fixture
def lan_key(backend, store):
    kid, key = app_keys.add(store, "Desk key", workspace="desk-ws")
    yield kid, key
    backend.backend.tracker = None


def _item(db, summary):
    return next(i for i in review.list_items(db)["items"] if i["summary"] == summary)


# ---------------------------------------------------------------- part 1: the store
def test_rename_keeps_the_same_token_working(store):
    cid, tok = store.add_token_client("Cursor", list(RA.SCOPES))
    assert store.rename_client("Cursor", "Work laptop") == (cid, "Cursor", "Work laptop")
    seen = RA.TokenChecker(store, None).check(tok)
    assert (seen.client_id, seen.client_name, seen.former_names) == (cid, "Work laptop", ["Cursor"])
    assert store.rename_client(cid, "Home laptop")[1] == "Work laptop"     # by id too
    assert store.former_names(cid) == ["Cursor", "Work laptop"]
    ident = identity.resolve("Grok Bot", True, RA.TokenChecker(store, None).check(tok), {},
                             listener="relay")
    assert ident.machine == "relay (signed in as Home laptop)"
    assert ident.sign_ins() == {"home laptop", "work laptop", "cursor"}
    assert "aka" not in ident.as_dict()                                    # never stored


def test_rename_refusals_say_why(store):
    a, _ = store.add_token_client("Cursor", list(RA.SCOPES))
    b, _ = store.add_token_client("Pager", list(RA.SCOPES))
    for old, new, why in [("Nobody", "X", "no active app is named 'Nobody'"),
                          ("Cursor", "  ", "the new name is empty"),
                          ("Cursor", "pager", "the name 'pager' is taken (by " + b),
                          ("Cursor", "Cursor", "already named 'Cursor'"),
                          ("Cursor", "x" * 51, "longer than 50"),
                          ("", "X", "say which app")]:
        with pytest.raises(ValueError, match=None) as e:
            store.rename_client(old, new)
        assert why in str(e.value), (old, new, str(e.value))
    store.rename_client("Cursor", "Work laptop")
    with pytest.raises(ValueError) as e:                 # a name another token had before
        store.rename_client("Pager", "cursor")
    assert "taken" in str(e.value) and a in str(e.value)
    store.rename_client("Work laptop", "Cursor")         # its own old name: allowed
    store.revoke_client(b)
    with pytest.raises(ValueError, match="no active app"):
        store.rename_client("Pager", "Tablet")           # revoked: not renamed


def test_rename_refuses_a_secret_looking_name(store):
    store.add_token_client("Cursor", list(RA.SCOPES))
    with pytest.raises(ValueError, match="secret"):
        store.rename_client("Cursor", "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8")


def test_two_apps_of_one_name_are_renamed_by_id(store):
    """(only an older install can have two: new names are unique, test_names_are_never_reused)"""
    a, _ = store.add_token_client("Laptop", list(RA.SCOPES))
    b, _ = store.add_token_client("Laptop 2", list(RA.SCOPES))
    with store._conn() as c:
        c.execute("UPDATE clients SET name = 'Laptop' WHERE id = ?", (b,))
    with pytest.raises(ValueError) as e:
        store.rename_client("Laptop", "Laptop A")
    assert a in str(e.value) and b in str(e.value) and "by its id" in str(e.value)
    assert store.rename_client(b, "Laptop B")[0] == b


# ---------------------------------------------------------------- part 1: the command
def test_cli_rename_and_clients_list(store, capsys):
    cid, _ = store.add_token_client("Cursor", list(RA.SCOPES))
    assert cli.main(["remote", "rename", "Cursor", "Work laptop"]) == 0
    out = capsys.readouterr().out
    assert f"{cid}: renamed from 'Cursor' to 'Work laptop'. Same token" in out
    assert "keeps the name 'Cursor'" in out
    assert cli.main(["remote", "clients"]) == 0
    assert "'Work laptop'" in (out := capsys.readouterr().out) and "(was 'Cursor')" in out
    assert cli.main(["remote", "rename", "Nobody", "X"]) == 1
    assert "Not renamed: no active app is named 'Nobody'" in capsys.readouterr().out
    assert cli.main(["remote", "rename", "Work laptop"]) == 1
    assert "Usage: flowstate remote rename" in capsys.readouterr().out


def test_cli_rename_carries_the_pause(store, seeded_db, capsys):
    store.add_token_client("Cursor", list(RA.SCOPES))
    activity.set_paused(seeded_db, "Cursor", True, "test")
    assert cli.main(["remote", "rename", "Cursor", "Work laptop", "--db", str(seeded_db)]) == 0
    assert "'Cursor' was paused, so 'Work laptop' is paused too" in capsys.readouterr().out
    assert activity.is_paused(seeded_db, "Work laptop")


def test_remote_help_lists_rename(capsys):
    with pytest.raises(SystemExit):
        cli.main(["remote", "--help"])
    assert "rename" in capsys.readouterr().out
    assert "rename OLD NEW" in cli.__doc__


# ---------------------------------------------------------------- part 1: over HTTP
def test_past_records_keep_the_old_name_and_new_ones_use_the_new(backend, store, cursor):
    db = backend.backend.db_path
    _cid, tok = cursor
    http_calls(db, tok, [suggest("Before the rename")], "Grok Bot")
    store.rename_client("Cursor", "Work laptop")
    http_calls(db, tok, [suggest("After the rename")], "Grok Bot")
    assert queued(db, "Before the rename") == \
        [("Grok Bot", "relay (signed in as Cursor)", "remote_token")]
    assert queued(db, "After the rename") == \
        [("Grok Bot", "relay (signed in as Work laptop)", "remote_token")]
    assert identity.shown_row(_item(db, "Before the rename")) == "Grok Bot (via Cursor)"


def test_a_renamed_token_still_cant_decide_its_own_item(backend, store, cursor):
    db = backend.backend.db_path
    _cid, tok = cursor
    tunnel_calls(db, tok, [suggest("Use Redis for caching")], "Grok Bot")   # (the relay: phone only)
    store.rename_client("Cursor", "Work laptop")
    item = _item(db, "Use Redis for caching")
    out = tunnel_calls(db, tok, [("manage_review", {
        "action": "approve", "project": "Alpha", "item_id": item["id"],
        "user_request": "Use Redis for caching"})], "Another Name")
    assert "own_item" in json.dumps(out), out
    assert _item(db, "Use Redis for caching")["status"] == "pending"
    # unit level: the same check with a token that never had the name is allowed
    stranger = identity.Identity("Another Name", None, "tunnel (signed in as Phone)",
                                 "remote_token", "s", "Phone")
    assert review.own_item_refusal(item, stranger, "Use Redis for caching") is None


def test_a_renamed_token_can_still_undo_only_its_own_records(store):
    cid, tok = store.add_token_client("Cursor", list(RA.SCOPES))
    store.rename_client("Cursor", "Work laptop")
    renamed = identity.resolve("Grok Bot", True, RA.TokenChecker(store, None).check(tok), {},
                               listener="relay")
    sc = identity.same_caller
    assert sc("Grok Bot", "relay (signed in as Cursor)", "remote_token", renamed)      # its own
    assert sc("x", "lan (signed in as Cursor)", "remote_token", renamed)
    assert not sc("Grok Bot", "relay (signed in as Phone)", "remote_token", renamed)   # another's
    assert not sc("Grok Bot", "laptop", "self_reported", renamed)                      # a local app's
    assert not sc("Grok Bot", "desk (signed in as Cursor)", "app_key", renamed)        # a key


def test_the_pause_follows_the_token_after_a_rename(backend, store, cursor, capsys):
    db = backend.backend.db_path
    _cid, tok = cursor
    activity.set_paused(db, "Cursor", True, "test")
    assert cli.main(["remote", "rename", "Cursor", "Work laptop", "--db", str(db)]) == 0
    out = http_calls(db, tok, [suggest("Paused after rename")], "Grok Bot")
    assert out[0]["error"] == "paused_by_user"
    assert queued(db, "Paused after rename") == []


# ---------------------------------------------------------------- part 2: LAN keys and tokens
def test_lan_key_records_the_apps_name_and_the_key(backend, lan_key):
    db = backend.backend.db_path
    _kid, key = lan_key
    http_calls(db, key, [suggest("Key write")], "Grok Bot", relay=False)
    assert queued(db, "Key write") == [("Grok Bot", "desk-ws (signed in as Desk key)", "app_key")]
    it = _item(db, "Key write")
    assert identity.shown_row(it) == "Grok Bot (via Desk key)"
    assert identity.shown_row(it, on_machine=True) == "Grok Bot (via Desk key) on desk-ws"


def test_lan_key_same_or_no_name_shows_one_name(backend, lan_key):
    db = backend.backend.db_path
    _kid, key = lan_key
    http_calls(db, key, [suggest("Same name")], "Desk key", relay=False)
    http_calls(db, key, [suggest("No name")], "", relay=False)
    assert identity.shown_row(_item(db, "Same name")) == "Desk key"
    assert queued(db, "No name")[0][0] == "Desk key"              # no name sent: the key's
    assert identity.shown_row(_item(db, "No name")) == "Desk key"


def test_lan_remote_token_shows_both_names_without_a_place(backend, cursor):
    db = backend.backend.db_path
    http_calls(db, cursor[1], [suggest("LAN token write")], "Grok Bot", relay=False)
    it = _item(db, "LAN token write")
    assert identity.shown_row(it, on_machine=True) == "Grok Bot (via Cursor)"   # no "on lan"


def test_pausing_the_key_name_stops_it_whatever_name_it_sends(backend, lan_key):
    db = backend.backend.db_path
    activity.set_paused(db, "Desk key", True, "test")
    out = http_calls(db, lan_key[1], [suggest("Paused key write")], "Grok Bot", relay=False)
    assert out[0]["error"] == "paused_by_user"


def test_a_lan_key_cant_decide_its_own_item_by_sending_another_name(backend, lan_key):
    db = backend.backend.db_path
    http_calls(db, lan_key[1], [suggest("Use Kafka")], "Grok Bot", relay=False)
    item = _item(db, "Use Kafka")
    out = http_calls(db, lan_key[1], [("manage_review", {
        "action": "approve", "project": "Alpha", "item_id": item["id"],
        "user_request": "Use Kafka"})], "Something Else", relay=False)
    assert "own_item" in json.dumps(out), out


def test_a_key_and_a_token_of_one_name_are_different_callers():
    key = identity.Identity("A", None, "lan (signed in as Cursor)", "app_key", "s", "Cursor")
    tok = identity.Identity("A", None, "lan (signed in as Cursor)", "remote_token", "s", "Cursor")
    row = ("A", "relay (signed in as Cursor)", "remote_token")
    assert identity.same_caller(*row, tok) and not identity.same_caller(*row, key)


def test_older_lan_rows_render_as_they_are_and_stay_the_keys_own():
    old_key = {"app": "Desk key", "machine": "desk-ws", "identity_source": "app_key"}
    old_tok = {"app": "Cursor", "machine": "Cursor", "identity_source": "remote_token"}
    assert identity.shown_row(old_key) == "Desk key"
    assert identity.shown_row(old_key, on_machine=True) == "Desk key on desk-ws"
    assert identity.shown_row(old_tok) == "Cursor"
    key = identity.Identity("Grok Bot", None, "desk-ws (signed in as Desk key)", "app_key", "s",
                            "Desk key")
    tok = identity.Identity("Grok Bot", None, "lan (signed in as Cursor)", "remote_token", "s",
                            "Cursor")
    assert identity.same_caller(*old_key.values(), key)          # its own undo, as before
    assert identity.same_caller(*old_tok.values(), tok)
    assert not identity.same_caller(*old_key.values(), tok)
    assert review._own({"id": 1, **old_key}, key)                # its own items, as before


def test_shared_lan_token_is_unchanged(backend):
    db = backend.backend.db_path
    http_calls(db, "t" * 32, [suggest("Shared token write")], "Grok Bot", relay=False)
    assert queued(db, "Shared token write")[0][0::2] == ("Grok Bot", "shared_token")
    it = _item(db, "Shared token write")
    assert identity.shown_row(it) == "Grok Bot"
    rows = who.recent(db)["rows"]
    shared = [r for r in rows if r["identity_source"] == "shared_token"]
    assert shared and all("via" not in r and r["identity"] == "shared token (legacy)"
                          for r in shared)


def test_pages_and_who_show_both_names_for_lan_keys(backend, lan_key):
    from flowstate.review_page import Page, create_app
    db = backend.backend.db_path
    http_calls(db, lan_key[1], [suggest("Shown via key")], "Grok Bot", relay=False)
    p = Page(db, 8770)
    c = TestClient(create_app(p), base_url="http://127.0.0.1:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    assert "from Grok Bot (via Desk key)" in html                          # review queue
    assert "Grok Bot (via Desk key)" in html[html.index("App pauses"):]    # apps list
    assert "Grok Bot (via Desk key)" in c.get("/?view=who").text           # who did what
    assert any(r.get("via") == "Desk key" and r["app"] == "Grok Bot" for r in who.recent(db)["rows"])
    from flowstate import approve_page, review_cards
    it = _item(db, "Shown via key")
    assert review_cards._who(it) == "Grok Bot (via Desk key)"
    assert approve_page.view(db, it["id"])["queued_by"].startswith("Grok Bot (via Desk key)")


def test_docs_describe_rename_and_the_lan_rule():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("README.md", "AGENTS.md", "docs/REMOTE-ACCESS.md"):
        text = (root / rel).read_text(encoding="utf-8")
        assert "remote rename" in text, rel
    remote = (root / "docs/REMOTE-ACCESS.md").read_text(encoding="utf-8")
    assert "lan (signed in as" in remote and "shared token (legacy)" in remote
    assert "from the key, not" not in (root / "AGENTS.md").read_text(encoding="utf-8")


def test_who_did_what_app_filter_finds_the_key_name_too(backend, lan_key):
    db = backend.backend.db_path
    http_calls(db, lan_key[1], [suggest("Filter by key")], "Grok Bot", relay=False)
    by_key = who.recent(db, app="desk key")["rows"]
    assert by_key and all(r["via"] == "Desk key" for r in by_key)
    assert who.recent(db, app="grok")["rows"]
    assert not who.recent(db, app="desk-ws")["rows"]          # the workspace isn't a name


# ---------------------------------------------------------------- audit 46: M12, L20, L21, M4
def test_names_are_never_reused_for_a_new_token_key_or_rename(store):
    """M12: "the same app" is matched by the sign-in's name, so a new token, key or rename may
    not take a name in use, a revoked one, or one another app had before a rename."""
    a, _ = store.add_token_client("Cursor", list(RA.SCOPES))
    for name in ("Cursor", "cursor", "  CURSOR "):
        with pytest.raises(ValueError, match="taken"):
            store.add_token_client(name, list(RA.SCOPES))
    b, _ = store.add_token_client("Pager", list(RA.SCOPES))
    store.revoke_client(b)
    with pytest.raises(ValueError, match="taken"):
        store.add_token_client("Pager", list(RA.SCOPES))          # a revoked token's name
    with pytest.raises(ValueError, match="taken"):
        store.rename_client("Cursor", "pager")                     # rename into a revoked name
    store.rename_client("Cursor", "Work laptop")
    with pytest.raises(ValueError, match="taken"):
        store.add_token_client("Cursor", list(RA.SCOPES))          # a former name
    k, _ = app_keys.add(store, "Desk key")
    with pytest.raises(ValueError, match="taken"):
        app_keys.add(store, "desk KEY")
    app_keys.revoke(store, key_id=k)
    with pytest.raises(ValueError, match="taken"):
        app_keys.add(store, "Desk key")
    with pytest.raises(ValueError, match="taken"):
        app_keys.add(store, "Cursor")  # one list for keys and tokens since task 55 (N-F22)


def test_an_oauth_app_signing_up_again_under_a_taken_name_gets_a_number(store):
    from mcp.shared.auth import OAuthClientInformationFull
    ids = []
    for i in range(3):
        info = OAuthClientInformationFull(client_id=f"oauth-{i}", client_name="Claude",
                                          redirect_uris=["https://claude.ai/api/mcp/auth_callback"])
        store.save_oauth_client(info)
        ids.append(store.client_row(f"oauth-{i}")["name"])
    assert ids == ["Claude", "Claude (2)", "Claude (3)"]


def test_names_are_short_enough_to_be_recorded_whole(store):
    """L21: a 70-character key name was cut in "(signed in as ...)", so the key stopped
    recognising its own records. New names are at most 50 characters."""
    with pytest.raises(ValueError, match="longer than 50"):
        app_keys.add(store, "k" * 51)
    with pytest.raises(ValueError, match="longer than 50"):
        store.add_token_client("t" * 51, list(RA.SCOPES))
    name = "n" * 50
    kid, key = app_keys.add(store, name, workspace="w" * 80)
    ident = identity.resolve("Grok Bot", True, app_keys.check(store, key), {}, listener="lan")
    assert identity.via(ident.machine, ident.identity_source) == name
    assert identity.same_sign_in("Grok Bot", ident.machine, "app_key", ident)


def test_an_app_cant_send_a_name_that_imitates_a_sign_in(store):
    """L20: "Grok Bot (via Laptop key)" on the shared token looked like a real key-signed app."""
    for sent in ("Grok Bot (via Laptop key)", "Grok Bot (signed in as Laptop key)",
                 "Grok Bot (VIA Laptop key"):
        assert identity.resolve(sent, False).app == "Grok Bot"
        assert identity.resolve(sent, True, None, {}, listener="lan").app == "Grok Bot"
    assert identity.resolve("(via x)", False).app == "unknown"


def test_pausing_an_app_pauses_the_key_it_uses(backend, lan_key, capsys):
    """M4: "Grok Bot" paused on the page or with `apps pause`, then the same key sending
    "Grok Bot 2": its writes were not blocked."""
    from flowstate import cli
    db = backend.backend.db_path
    http_calls(db, lan_key[1], [suggest("First write")], "Grok Bot", relay=False)
    assert cli.main(["apps", "pause", "--app", "Grok Bot", "--db", str(db)]) == 0
    assert "Desk key" in capsys.readouterr().out
    out = http_calls(db, lan_key[1], [suggest("Renamed write")], "Grok Bot 2", relay=False)
    assert out[0]["error"] == "paused_by_user"
    assert cli.main(["apps", "list", "--db", str(db)]) == 0
    listed = capsys.readouterr().out
    assert "Grok Bot (via Desk key): PAUSED" in listed
    assert cli.main(["apps", "resume", "--app", "Grok Bot", "--db", str(db)]) == 0
    out = http_calls(db, lan_key[1], [suggest("Back again")], "Grok Bot 2", relay=False)
    assert "error" not in out[0]


def test_the_page_pause_button_pauses_the_key_too(backend, lan_key):
    from flowstate.review_page import Page
    db = backend.backend.db_path
    http_calls(db, lan_key[1], [suggest("A write")], "Grok Bot", relay=False)
    msg = Page(db, 8770).set_app({"app": "Grok Bot", "paused": "1"})
    assert "Desk key" in msg and activity.is_paused(db, "Desk key")
