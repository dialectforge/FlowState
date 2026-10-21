"""The call timeout holds even when the database is fully locked (2.4.0, bug 11): the
paused-app check runs inside the time limit, and the bookkeeping after a call gets only
what is left of it (then finishes in the background)."""

import asyncio
import json
import sqlite3
import threading
import time

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session


def _timed_call(S, name, args):
    """(result, seconds until the answer). Timed inside the loop: asyncio.run itself waits
    for leftover worker threads on exit, which a long-running server never does."""
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            t0 = time.monotonic()
            r = await c.call_tool(name, args)
            return r, time.monotonic() - t0
    return asyncio.run(go())


@pytest.mark.parametrize("name, args", [
    ("log_work", {"project": "Alpha", "kind": "learning", "summary": "Fridge door seal worn"}),
    ("manage_todos", {"action": "list", "project": "Alpha"}),
])
def test_a_fully_locked_database_answers_at_the_timeout(backend, monkeypatch, name, args):
    S = backend
    monkeypatch.setenv("FLOWSTATE_CALL_TIMEOUT", "1")
    conn = sqlite3.connect(S.backend.db_path, timeout=5, isolation_level=None, check_same_thread=False)
    conn.execute("BEGIN EXCLUSIVE")
    holder = threading.Timer(4.0, lambda: (conn.rollback(), conn.close()))
    holder.start()
    try:
        r, took = _timed_call(S, name, args)
    finally:
        holder.join()
    body = json.loads(r.content[0].text)
    assert r.isError and body["error"] == "flowstate_timeout", body
    assert took < 1.8, f"answered after {took:.1f}s with a 1s timeout"
