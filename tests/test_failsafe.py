"""Nothing may break a client: bad DB, slow calls, tracker failures, brief failures."""

import asyncio
import json
import time

from mcp.shared.memory import create_connected_server_and_client_session


async def _call(S, name, args):
    async with create_connected_server_and_client_session(S.server) as c:
        return await c.call_tool(name, args)


def test_unusable_db_still_serves_clear_errors(tmp_path):
    from flowstate import server as S
    bad = tmp_path / "not-a-db.db"
    bad.write_bytes(b"this is not sqlite" * 100)
    S.init_backend(bad)
    try:
        assert S.backend.error
        r = asyncio.run(_call(S, "start_here", {"project": "x"}))
        assert r.isError
        body = json.loads(r.content[0].text)
        assert body["error"] == "flowstate_unavailable" and "carry on" in body["message"]
    finally:
        S.backend.error = None
        S.backend.tracker = None


def test_slow_call_times_out_quickly(backend, monkeypatch):
    S = backend
    monkeypatch.setenv("FLOWSTATE_CALL_TIMEOUT", "0.5")
    monkeypatch.setattr(S.tools, "list_projects", lambda *a, **k: time.sleep(3) or [])

    async def timed():
        async with create_connected_server_and_client_session(S.server) as c:
            t0 = time.time()
            r = await c.call_tool("list_projects", {})
            return r, time.time() - t0
    r, took = asyncio.run(timed())
    assert r.isError and json.loads(r.content[0].text)["error"] == "flowstate_timeout"
    assert took < 2.0  # the client hears back at the timeout, not when the slow work ends


def test_tracker_failure_does_not_break_calls(backend, monkeypatch):
    S = backend

    def boom(*a, **k):
        raise RuntimeError("tracker down")
    monkeypatch.setattr(S.backend.tracker, "touch", boom)
    monkeypatch.setattr(S.backend.tracker, "note", boom)
    r = asyncio.run(_call(S, "log_work", {"project": "Alpha", "kind": "learning",
                                          "summary": "still saved"}))
    assert not r.isError, r.content[0].text
    b = asyncio.run(_call(S, "start_here", {"project": "Alpha"}))
    assert not b.isError and "still saved" in b.content[0].text


def test_brief_failure_degrades_to_message(backend, monkeypatch):
    S = backend
    monkeypatch.setattr(S, "build_brief", lambda *a, **k: 1 / 0)
    r = asyncio.run(_call(S, "start_here", {"project": "Alpha"}))
    assert not r.isError and "could not be built" in r.content[0].text


def test_tool_exception_is_a_tool_error(backend, monkeypatch):
    S = backend
    monkeypatch.setattr(S.tools, "list_projects", lambda *a, **k: 1 / 0)
    r = asyncio.run(_call(S, "list_projects", {}))
    assert r.isError and json.loads(r.content[0].text)["error"] == "tool_failed"
