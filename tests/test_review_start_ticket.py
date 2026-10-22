"""(task 38) /review/start needs the site's signed start ticket (approvals.verify_start_ticket,
start_nonce, review_phone.py), so nobody can lock the owner out of phone review by hitting it.

A ticket is the same ES256 grant as the phone grants, action start_review, item 0, with the
site user's id in `sub` and a fresh nonce. No / bad / expired / used ticket: a plain page,
no code, nothing counted, the ticket never echoed. 30 codes an hour per site user, 120 for
everyone, one code per ticket. Temp DBs and sandboxed folders only (conftest.py)."""

import secrets
import sqlite3
import time
from datetime import timedelta

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from flowstate import approvals
from fsa_test_keys import claims, make_key, sign

SUB = "site-user-0001"


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


@pytest.fixture
def relay(tmp_path, monkeypatch):
    """This computer enrolled in the relay as `testbox`, with test keys pinned."""
    from flowstate.relay.store import RelayStore
    monkeypatch.setenv("FLOWSTATE_RELAY_DIR", str(tmp_path / "relay"))
    RelayStore().save_enrollment("testbox", "fake-device-token-for-tests")
    key, jwk = make_key()
    monkeypatch.setattr(approvals, "FSA_PUBLIC_KEYS", {"fsa-1": jwk, "fsa-2": None})
    return key


def ticket(key, sub=SUB, kid="fsa-1", **over):
    c = claims(0, secrets.token_urlsafe(16), action="start_review", sub=sub)
    c.update(over)
    return sign(key, c, kid=kid)


def client(db):
    from flowstate.review_phone import routes
    return TestClient(Starlette(routes=routes(lambda: db)), base_url="https://testbox.fsmcp.com")


def start(c, t=None, **params):
    if t is not None:
        params["t"] = t
    return c.get("/review/start", params=params, follow_redirects=False)


def rows(db):
    return _q(db, "SELECT COUNT(*) FROM review_open_nonces")[0][0]


def test_a_good_ticket_makes_one_code_for_that_user(backend, relay):
    db = backend.backend.db_path
    c = client(db)
    t = ticket(relay)
    r = start(c, t)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert loc.startswith("https://flowstatemcp.com/review/testbox?n=") and t not in loc
    code = loc.split("?n=")[1]
    nonce = t.split(".")[1]
    sub, stored, used = _q(db, "SELECT sub, ticket, used_at FROM review_open_nonces WHERE nonce = ?", code)[0]
    assert sub == SUB and used is None
    assert stored and len(stored) == 64 and t not in stored and nonce not in stored   # a hash only
    for h, v in (("cache-control", "no-store"), ("referrer-policy", "no-referrer"), ("x-frame-options", "DENY")):
        assert r.headers[h] == v
    # the code still opens the list once with the site's open_review grant
    claims_ = approvals.verify_review_grant(db, sign(relay, claims(0, code, action="open_review")))
    assert claims_["nonce"] == code


def _refused(c, db, t, status, *words, **params):
    before = rows(db)
    r = start(c, t, **params)
    assert r.status_code == status, (r.status_code, r.text)
    assert "location" not in r.headers and "<script" not in r.text
    assert "Waiting for you" in r.text and "flowstatemcp.com" in r.text
    for w in words:
        assert w in r.text, r.text
    if t and len(t) > 40:                                           # never echoed
        assert t not in r.text and all(part not in r.text for part in t.split(".") if len(part) > 20)
    for h, v in (("cache-control", "no-store"), ("referrer-policy", "no-referrer"), ("x-frame-options", "DENY")):
        assert r.headers[h] == v
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert rows(db) == before                                       # no code, nothing counted
    return r


def test_refusals_make_no_code_and_count_nothing(backend, relay):
    db = backend.backend.db_path
    c = client(db)
    t0 = int(time.time())
    other_key = make_key()[0]
    cases = [
        (None, 403, "didn't come with this visit"),                          # missing
        ("", 403, "didn't come with this visit"),
        ("not-a-ticket", 400, "isn't valid"),                                # malformed
        ("a.b.c", 400, "isn't valid"),
        ("x" * 5000, 400, "isn't valid"),
        (ticket(other_key), 403, "isn't valid"),                             # bad signature
        (ticket(relay, kid="fsa-9"), 403, "isn't valid"),                    # unknown kid
        (ticket(relay, kid="fsa-2"), 403, "isn't valid"),                    # kid pinned to None
        (ticket(relay, aud="otherbox.fsmcp.com"), 403, "isn't valid"),       # wrong audience
        (ticket(relay, handle="otherbox"), 403, "isn't valid"),              # wrong handle
        (ticket(relay, action="open_review"), 403, "isn't valid"),           # an open grant
        (ticket(relay, action="approve_review"), 403, "isn't valid"),        # an approve grant
        (ticket(relay, item=1), 403, "isn't valid"),
        (ticket(relay, iss="https://evil.example"), 403, "isn't valid"),
        (ticket(relay, sub=""), 403, "isn't valid"),
        (ticket(relay, nonce="A" * 16), 403, "isn't valid"),                 # nonce too short
        (ticket(relay, nonce="A" * 44), 403, "isn't valid"),                 # and too long
        (ticket(relay, exp=t0 + 3600), 403, "isn't valid"),                  # over 5 minutes' life
        (ticket(relay, iat=t0 + 600, exp=t0 + 700), 403, "isn't valid"),     # from the future
        (ticket(relay, iat=t0 - 400, exp=t0 - 100), 403, "expired after 5 minutes"),
    ]
    for t, status, words in cases:
        _refused(c, db, t, status, words)
    assert rows(db) == 0


def test_a_replayed_ticket_is_refused_and_not_counted(backend, relay):
    db = backend.backend.db_path
    c = client(db)
    t = ticket(relay)
    assert start(c, t).status_code == 303
    _refused(c, db, t, 403, "already used")
    _refused(c, db, t, 403, "already used")
    assert _q(db, "SELECT COUNT(*) FROM review_open_nonces WHERE sub = ?", SUB) == [(1,)]
    # the same nonce signed again (ES256 signatures differ each time) is the same ticket
    nonce = approvals._claims(t, "testbox", None, None, "start_review", 0)["nonce"]
    _refused(c, db, sign(relay, claims(0, nonce, action="start_review", sub=SUB)), 403, "already used")


def test_31st_ticket_from_one_user_is_429_while_another_user_still_gets_a_code(backend, relay, monkeypatch):
    db = backend.backend.db_path
    c = client(db)
    for _ in range(approvals.START_PER_HOUR):
        assert start(c, ticket(relay)).status_code == 303
    before = rows(db)
    t = ticket(relay)
    r = start(c, t)
    assert r.status_code == 429 and "Too many" in r.text and r.headers["retry-after"] == "3600"
    assert t not in r.text and rows(db) == before
    other = start(c, ticket(relay, sub="site-user-0002"))
    assert other.status_code == 303 and "?n=" in other.headers["location"]
    assert start(c, ticket(relay)).status_code == 429                    # still for the first
    # an hour later the first user can open it again
    later = approvals._now() + timedelta(hours=1, minutes=1)
    monkeypatch.setattr(approvals, "_now", lambda: later)
    assert approvals.start_nonce(db, SUB, "n" * 22) is not None


def test_global_ceiling_is_a_backstop(backend, relay):
    db = backend.backend.db_path
    users = approvals.START_PER_HOUR_ALL // approvals.START_PER_HOUR
    for u in range(users):
        for _ in range(approvals.START_PER_HOUR):
            approvals.start_nonce(db, f"user-{u}", secrets.token_urlsafe(16))
    assert rows(db) == approvals.START_PER_HOUR_ALL
    c = client(db)
    r = start(c, ticket(relay, sub="a-brand-new-user"))
    assert r.status_code == 429 and rows(db) == approvals.START_PER_HOUR_ALL
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.start_nonce(db, "another-new-user", "m" * 22)
    assert e.value.code == "rate_limited"


def test_old_rows_without_a_user_count_only_toward_the_ceiling(backend, relay):
    db = backend.backend.db_path
    conn = sqlite3.connect(db)
    now = approvals._ts()
    conn.executemany("INSERT INTO review_open_nonces (nonce, action, created_at, expires_at) VALUES "
                     "(?, 'open_review', ?, ?)",
                     [(f"old-{i:040d}", now, approvals._ts(approvals._now() + timedelta(minutes=15)))
                      for i in range(60)])
    conn.commit()
    conn.close()
    c = client(db)
    for _ in range(approvals.START_PER_HOUR):
        assert start(c, ticket(relay)).status_code == 303          # old rows aren't this user's
    assert start(c, ticket(relay)).status_code == 429
    for _ in range(approvals.START_PER_HOUR_ALL - 60 - approvals.START_PER_HOUR):
        approvals.start_nonce(db, "user-b", secrets.token_urlsafe(16))
    assert start(c, ticket(relay, sub="user-c")).status_code == 429   # 60 old + 60 new = ceiling


def test_ticketless_hammering_never_locks_out_a_valid_ticket(backend, relay):
    db = backend.backend.db_path
    c = client(db)
    for i in range(100):
        r = start(c, None if i % 2 else "junk." * (i % 5))
        assert r.status_code in (400, 403)
    for _ in range(40):
        start(c, ticket(make_key()[0]))                            # forged signatures
    assert rows(db) == 0
    r = start(c, ticket(relay))
    assert r.status_code == 303 and "?n=" in r.headers["location"]


def test_ticket_and_grants_cannot_stand_in_for_each_other(backend, relay):
    from flowstate import review
    db = backend.backend.db_path
    # a start ticket doesn't open the list, even with a live code as its nonce ...
    code = approvals.start_nonce(db, SUB, secrets.token_urlsafe(16))
    t = sign(relay, claims(0, code, action="start_review", sub=SUB))
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_review_grant(db, t)
    assert e.value.code == "wrong_device"
    assert _q(db, "SELECT used_at FROM review_open_nonces WHERE nonce = ?", code) == [(None,)]
    # ... and doesn't approve an item
    approvals.set_on(db, True)
    item = review.add_item(db, 1, "delete", "Delete it", None,
                           {"record_type": "todo", "record_id": 1, "user_only": True}, "cursor")
    n = approvals.nonce_for(db, item)
    with pytest.raises(approvals.ApprovalError) as e:
        approvals.verify_grant(db, sign(relay, claims(item, n, action="start_review")), item)
    assert e.value.code == "wrong_device"
    # open_review and approve_review grants aren't tickets
    for action, item_ in (("open_review", 0), ("approve_review", 0), ("approve_review", item)):
        with pytest.raises(approvals.ApprovalError):
            approvals.verify_start_ticket(db, sign(relay, claims(item_, secrets.token_urlsafe(16),
                                                                 action=action)))
    assert approvals.verify_start_ticket(db, ticket(relay))["sub"] == SUB


def test_verify_start_ticket_checks_the_format_before_any_crypto(backend, relay, monkeypatch):
    db = backend.backend.db_path
    called = []
    monkeypatch.setattr(approvals, "_verify_es256", lambda *a: called.append(1) or False)
    for bad in ("", "abc", "a.b.c", "a" * 30 + "." + "b" * 30 + "." + "c" * 85, "x" * 5000):
        with pytest.raises(approvals.ApprovalError) as e:
            approvals.verify_start_ticket(db, bad)
        assert e.value.code == "bad_grant"
    assert called == []
    c = client(db)
    assert start(c, "a" * 30 + "." + "b" * 30 + "." + "c" * 85).status_code == 400 and called == []


def test_start_ticket_is_never_logged(backend, relay, caplog):
    """Through FlowState's whole HTTP app as the relay builds it (Gate's access log included)."""
    import logging
    from flowstate.http_app import create_http_app
    db = backend.backend.db_path
    t, bad = ticket(relay), ticket(make_key()[0])
    app = create_http_app("t" * 32, db_path=db, public_url="https://testbox.fsmcp.com",
                          trust_proxy=False, relay=True)
    with caplog.at_level(logging.DEBUG), \
            TestClient(app, base_url="https://testbox.fsmcp.com") as c:
        assert start(c, t).status_code == 303
        assert start(c, t).status_code == 403
        assert start(c, bad).status_code == 403
    ours = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "/review/start" in ours                                  # the access log saw them
    for x in (t, bad):
        assert all(part not in ours for part in x.split("."))
    backend.backend.tracker = None


def test_ticket_is_ignored_where_phone_review_is_off(backend, relay):
    db = backend.backend.db_path
    approvals.set_value(db, "phone_approvals", "off")
    c = client(db)
    t = ticket(relay)
    r = start(c, t)
    assert r.status_code == 200 and "Phone review is off" in r.text and t not in r.text
    approvals.set_value(db, "phone_approvals", "on")
    assert start(c, t).status_code == 303                          # it wasn't used up


def test_migration_022_adds_the_ticket_columns(fresh_db):
    from flowstate import migrations as M
    cols = {r[1]: r for r in _q(fresh_db, "PRAGMA table_info(review_open_nonces)")}
    assert {"sub", "ticket"} <= set(cols)
    assert cols["sub"][2] == "TEXT" and cols["sub"][4] == "NULL" and cols["ticket"][4] == "NULL"
    assert any(n == "idx_review_open_nonces_ticket" for (n,) in _q(
        fresh_db, "SELECT name FROM sqlite_master WHERE type = 'index'"))
    assert 22 in M.applied(fresh_db)


def test_database_without_022_columns_offers_no_code(tmp_path, relay):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE review_open_nonces (nonce TEXT PRIMARY KEY, action TEXT NOT NULL "
                 "DEFAULT 'open_review', created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT)")
    conn.commit()
    conn.close()
    assert approvals.start_nonce(db, SUB, "q" * 22) is None
