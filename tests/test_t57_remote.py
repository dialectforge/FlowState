"""Task 57: remote access and names (task 56 Lows A-E). Temp settings databases only; the LAN
listener runs in process with a chosen socket peer."""

import pytest

from flowstate import app_keys, approvals, names
from flowstate import remote_auth as RA
from test_data_security_audit import _lan_app
from test_t55_auth import _oauth, store  # noqa: F401


# ---------------------------------------------------------------- A: a proxy on this computer
def _post_with(app, token, peer, headers):
    """As test_data_security_audit._post, with extra headers; 0: past Gate."""
    import asyncio
    import httpx

    async def go():
        t = httpx.ASGITransport(app=app, client=(peer, 50000))
        async with httpx.AsyncClient(transport=t, base_url="http://lan.example") as c:
            return await c.post("/mcp", headers={"Authorization": f"Bearer {token}", "Accept":
                                                 "application/json, text/event-stream", **headers},
                                json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    try:
        return asyncio.run(go()).status_code
    except RuntimeError:
        return 0


def test_a_token_through_a_proxy_on_this_computer_counts_as_sent_over_the_lan(seeded_db, tmp_path, store):
    cid, tok = store.add_token_client("Proxy App", list(RA.SCOPES))
    app = _lan_app(seeded_db, tmp_path)
    assert _post_with(app, tok, "127.0.0.1", {}) != 401
    assert _post_with(app, tok, "127.0.0.1", {"X-Forwarded-For": "192.0.2.7"}) == 401
    assert cid in store.lan_exposures()


def test_flowstate_lan_proxy_makes_no_caller_local(monkeypatch):
    monkeypatch.setenv("FLOWSTATE_LAN_PROXY", "1")
    assert RA.proxied({}) is True
    monkeypatch.delenv("FLOWSTATE_LAN_PROXY")
    assert RA.proxied({}) is False and RA.proxied({b"via": b"1.1 proxy"}) is True


# ---------------------------------------------------------------- B: old phone labels
def test_old_phone_labels_are_scrubbed_after_the_relay_is_off_or_re_enrolled(backend, monkeypatch):
    import sqlite3
    db = backend.backend.db_path
    c = sqlite3.connect(db)
    c.execute("INSERT INTO approval_log (item_id, path, client, decision, decided_at) VALUES "
              "(1, 'phone', 'phone (oldhandle1)', 'approved', '2026-01-01 00:00:00')")
    c.commit()
    c.close()
    monkeypatch.setattr(approvals, "_elsewhere", lambda: True)
    monkeypatch.setattr(approvals, "relay_handle", lambda: "newhandle2")    # re-enrolled
    out = approvals.scrub_handle({"a": "decided by phone (oldhandle1)", "b": "phone (newhandle2)",
                                  "c": "Call them by phone (urgent)"}, db)
    assert out == {"a": "decided by phone", "b": "phone", "c": "Call them by phone (urgent)"}
    monkeypatch.setattr(approvals, "relay_handle", lambda: None)            # relay off
    assert approvals.scrub_handle(["phone (oldhandle1)"], db) == ["phone"]


# ---------------------------------------------------------------- C: more lookalikes
@pytest.mark.parametrize("fake", ["Curꓢor", "ⲥursor", "Ꮯursor", "ᏟURSOR", "Curօr".replace("օr", "sor")])
def test_more_lookalike_letters_count_as_the_same_name(fake):
    assert names.skeleton(fake) == names.skeleton("Cursor")


@pytest.mark.parametrize("mark", ["Bot ‹via Cursor›", "Bot <via Cursor>", "Bot «signed in as X»"])
def test_angle_brackets_imitating_flowstates_marks_are_caught(mark):
    assert names.has_marks(mark) and names.strip_marks(mark) == "Bot"


def test_armenian_o_in_a_name():
    assert names.skeleton("Dօck") == names.skeleton("Dock")


# ---------------------------------------------------------------- D: unapproved OAuth apps
def test_an_unapproved_oauth_app_holds_no_name(store):
    assert _oauth(store, "o-1", "Cursor") == "Cursor"          # registered, never approved
    kid, _key = app_keys.add(store, "Cursor")                   # the user's own key: allowed
    assert kid
    store.approve("o-1")                                        # approved later: numbered
    assert store.client_row("o-1")["name"] == "Cursor (2)"
    with pytest.raises(ValueError, match="taken"):
        store.add_token_client("Cursor (2)", list(RA.SCOPES))
