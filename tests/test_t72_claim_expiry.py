"""Task 72: an expired todo claim is gone. Nobody can renew or extend it as if it were still
live (not the agent that held it, not another one); a fresh claim is needed, which any
agent can make. Renewing before expiry still works. In-process, temp DBs, frozen clock."""

from flowstate import claims

from test_todo_claims import S, _rows, clock, fail, ok, todo  # noqa: F401


def test_holder_can_not_renew_after_expiry(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(6)
    body = fail(todo(S, {"action": "renew", "todo_id": 1, "minutes": 60}), "claim_expired")
    assert "expired" in body["message"]
    assert 'manage_todos(action="claim")' in body["message"]
    assert _rows(S.backend.db_path, "SELECT expires_at FROM todo_claims") == \
        [("2026-10-21 12:05:00",)]                          # not extended
    events = [e for (e,) in _rows(S.backend.db_path, "SELECT event FROM todo_claim_events")]
    assert events == ["claim"]                             # no renew logged


def test_holder_can_not_renew_exactly_at_expiry(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(5)
    fail(todo(S, {"action": "renew", "todo_id": 1}), "claim_expired")


def test_another_agent_can_not_renew_an_expired_claim(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(6)
    fail(todo(S, {"action": "renew", "todo_id": 1}, app="Claude Code"), "not_claimed")
    assert _rows(S.backend.db_path, "SELECT app, expires_at FROM todo_claims") == \
        [("Cursor", "2026-10-21 12:05:00")]


def test_fresh_claim_after_expiry_works_for_any_agent(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(6)
    got = ok(todo(S, {"action": "claim", "todo_id": 1}, app="Claude Code"))
    assert got["claimed"] and not got["renewed"]
    assert _rows(S.backend.db_path, "SELECT app, claimed_at FROM todo_claims") == \
        [("Claude Code", "2026-10-21 12:06:00")]
    clock.advance(20)
    again = ok(todo(S, {"action": "claim", "todo_id": 1}))   # the first holder, fresh
    assert again["claimed"] and not again["renewed"]
    assert _rows(S.backend.db_path, "SELECT app, claimed_at FROM todo_claims") == \
        [("Cursor", "2026-10-21 12:26:00")]


def test_holder_claiming_again_after_expiry_is_a_new_claim_not_a_renewal(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(6)
    got = ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 10}))
    assert got["claimed"] and not got["renewed"]
    assert _rows(S.backend.db_path, "SELECT claimed_at, expires_at FROM todo_claims") == \
        [("2026-10-21 12:06:00", "2026-10-21 12:16:00")]


def test_renew_before_expiry_still_works(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(4)
    got = ok(todo(S, {"action": "renew", "todo_id": 1, "minutes": 30}))
    assert got["renewed"] and got["expires_at"] == "2026-10-21 12:34:00"
    clock.advance(29)
    assert ok(todo(S, {"action": "renew", "todo_id": 1}))["renewed"]


def test_release_of_an_expired_claim_says_there_was_nothing(S, clock):
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 5}))
    clock.advance(6)
    got = ok(todo(S, {"action": "release", "todo_id": 1}))
    assert got["released"] is False
    assert _rows(S.backend.db_path, "SELECT COUNT(*) FROM todo_claims") == [(0,)]


def test_renew_directly_after_expiry_refused(S, clock):
    """claims.renew itself, not only through the tool."""
    ok(todo(S, {"action": "claim", "todo_id": 1, "minutes": 1}))
    clock.advance(2)
    import pytest
    with pytest.raises(claims.ClaimError) as e:
        claims.renew(S.backend.db_path, 1, 1, 30, "Cursor")
    assert e.value.payload["error"] in ("claim_expired", "not_claimed")
