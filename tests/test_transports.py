"""Both transports, as real subprocesses with the real MCP client library.

stdio: what Claude Desktop / Grok Build / Cursor launch. HTTP: the LAN server and,
later, the tunnel. Each test uses its own temp DB and (for HTTP) a random local port."""

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import httpx
from mcp.client.streamable_http import streamable_http_client

from conftest import SERVER_DIR, free_port, server_env

TOKEN = "test-token-" + "x" * 24  # test-only value


def _rows(db, sql):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


async def _flow(session: ClientSession, init):
    assert "start_here" in (init.instructions or "")
    brief = await session.call_tool("start_here", {"project": "Alpha"})
    assert not brief.isError and brief.content[0].text.startswith("FlowState brief: Alpha")
    w = await session.call_tool("log_work", {"project": "Alpha", "kind": "change",
                                             "summary": "transport test change"})
    assert not w.isError, w.content[0].text
    refused = await session.call_tool("log_work", {"kind": "change", "summary": "no project",
                                                   "project": ""})
    assert refused.isError and json.loads(refused.content[0].text)["error"] == "project_required"


def test_stdio_session_opens_and_closes_on_disconnect(seeded_db):
    params = StdioServerParameters(command=sys.executable, args=["-m", "flowstate"],
                                   env=server_env(seeded_db), cwd=str(SERVER_DIR))

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                await _flow(s, init)
                open_rows = _rows(seeded_db, "SELECT session_id FROM auto_sessions WHERE closed_at IS NULL")
                assert len(open_rows) == 1
    asyncio.run(go())
    # the client is gone: the server closed its own session on the way out
    deadline = time.time() + 10
    while time.time() < deadline:
        rows = _rows(seeded_db, "SELECT close_reason FROM auto_sessions")
        if rows and rows[0][0]:
            break
        time.sleep(0.2)
    assert rows == [("shutdown",)]
    summary = _rows(seeded_db, "SELECT summary FROM sessions")[0][0]
    assert summary.startswith("[auto-captured, needs review]") and "transport test change" in summary


@pytest.fixture
def http_server(seeded_db):
    port = free_port()
    env = server_env(seeded_db, FLOWSTATE_TOKEN=TOKEN, FLOWSTATE_IDLE_MINUTES="0.02",
                     FLOWSTATE_REAP_SECONDS="0.5")
    proc = subprocess.Popen([sys.executable, "-m", "flowstate", "--transport", "http",
                             "--host", "127.0.0.1", "--port", str(port)],
                            env=env, cwd=str(SERVER_DIR),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("HTTP server did not start")
    yield base, seeded_db
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _status(url, headers=None, data=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_auth_and_health(http_server):
    base, _ = http_server
    code, body = _status(base + "/health")
    assert code == 200 and json.loads(body) == {"ok": True, "service": "flowstate"}
    ping = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    assert _status(base + "/mcp", hdr, ping)[0] == 401
    assert _status(base + "/mcp", {**hdr, "Authorization": "Bearer wrong-token-wrong-token"}, ping)[0] == 401
    assert _status(base + "/other", {**hdr, "Authorization": f"Bearer {TOKEN}"}, ping)[0] == 404


def test_http_flow_and_idle_auto_close(http_server):
    base, db = http_server

    async def go():
        http = httpx.AsyncClient(headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        async with http, streamable_http_client(base + "/mcp", http_client=http) as (r, w, _):
            async with ClientSession(r, w) as s:
                init = await s.initialize()
                await _flow(s, init)
                assert len(_rows(db, "SELECT 1 FROM auto_sessions WHERE closed_at IS NULL")) == 1
                # stay connected but idle: the reaper (idle 1.2 s, every 0.5 s) closes it
                deadline = time.time() + 15
                while time.time() < deadline:
                    rows = _rows(db, "SELECT close_reason FROM auto_sessions")
                    if rows and rows[0][0]:
                        return rows
                    await asyncio.sleep(0.3)
                return _rows(db, "SELECT close_reason FROM auto_sessions")
    rows = asyncio.run(go())
    assert rows == [("idle",)]
    summary = _rows(db, "SELECT summary FROM sessions")[0][0]
    assert summary.startswith("[auto-captured, needs review]")


def test_http_refuses_to_start_without_token(seeded_db):
    r = subprocess.run([sys.executable, "-m", "flowstate", "--transport", "http", "--port", str(free_port())],
                       env=server_env(seeded_db), cwd=str(SERVER_DIR), capture_output=True, text=True,
                       timeout=30)
    assert r.returncode != 0 and "bearer token" in r.stderr
