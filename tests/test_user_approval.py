"""2.4.0: the user's own terminal step. `flowstate approve <id>` / `reject <id>` confirm on the
controlling terminal, never stdin; a piped "y" with no terminal is refused. With approvals on
(test_approvals.py) it is how the user decides an agent's delete or turning review off when
no popup or phone link is available. Suggestions and session summaries keep the rule they had
before: the app that queued one decides it only with the user's own words."""

import asyncio
import json
import sqlite3
import sys

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import SERVER_DIR, server_env
from terminal_run import run_in_terminal, run_no_terminal

# pty.fork() in pytest's multi-threaded process: safe here, the child execs at once
pytestmark = pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")


def _j(r):
    return json.loads(r.content[0].text)


def call(S, name, args, app="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return await c.call_tool(name, args)
    return asyncio.run(go())


def _status(db, item):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT status, decided_by FROM review_items WHERE id = ?", (item,)).fetchone()
    finally:
        conn.close()


def _suggest(S, app="Cursor"):
    return _j(call(S, "log_work", {"project": "Alpha", "kind": "decision", "summary": "Use SQLite WAL",
                                   "suggested": True}, app))["queued_for_review"]


def _delete_request(S, app="Cursor"):
    r = _j(call(S, "manage_memory", {"action": "delete", "project": "Alpha", "record_type": "todo",
                                     "record_id": 1}, app))
    return r.get("delete_requested") or r.get("item_id") or r["queued_for_review"]


# ---------------------------------------------------------------- MCP side
@pytest.mark.parametrize("words", ["approve", "Use SQLite WAL", "ok"])
def test_own_suggestion_needs_the_users_own_words(backend, words):
    S = backend
    item = _suggest(S)
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": words})
    body = _j(r)
    assert r.isError and body["error"] == "own_item", body
    assert "\n" not in body["message"] and len(body["message"]) < 160
    assert _status(S.backend.db_path, item)[0] == "pending"


def test_own_suggestion_with_the_users_words_goes_through_as_before(backend):
    S = backend
    item = _suggest(S)
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "User: yes, approve the WAL one please"})
    assert not r.isError, r.content[0].text
    assert _status(S.backend.db_path, item)[0] == "approved"


def test_another_app_keeps_the_user_request_rule(backend):
    S = backend
    item = _suggest(S, app="Cursor")
    r = call(S, "manage_review", {"action": "approve", "project": "Alpha", "item_id": item,
                                  "user_request": "User: approve it"}, app="Claude Desktop")
    assert not r.isError, r.content[0].text


# ---------------------------------------------------------------- the user's step
def _cli(db, *args, stdin=None):
    """Run `flowstate ...` with no terminal, like an agent's shell (POSIX: a new session with no
    controlling terminal; Windows: a console without a window, as AI apps' shells get)."""
    return run_no_terminal([sys.executable, "-m", "flowstate", *args, "--db", str(db)],
                           input=stdin, env=server_env(db), cwd=str(SERVER_DIR), timeout=60)


def test_review_lists_what_waits(backend):
    S = backend
    item = _suggest(S)
    r = _cli(S.backend.db_path, "review")
    assert r.returncode == 0 and f"#{item}" in r.stdout and "Use SQLite WAL" in r.stdout, r.stdout


@pytest.mark.parametrize("verb", ["approve", "reject"])
def test_piped_y_without_a_terminal_is_refused(backend, verb):
    S = backend
    item = _suggest(S)
    r = _cli(S.backend.db_path, verb, str(item), stdin="y\n")
    assert r.returncode == 2 and "Refused" in r.stdout and "terminal" in r.stdout, r.stdout + r.stderr
    assert _status(S.backend.db_path, item)[0] == "pending"


def _in_a_terminal(db, args, answers):
    """Run `flowstate ...` in a terminal of its own (as when the user runs it: a pseudo-terminal,
    or on Windows a console), typing each answer when a "Type ..." prompt appears. (exit code,
    output)"""
    return run_in_terminal([sys.executable, "-m", "flowstate", *args, "--db", str(db)], answers,
                           env=server_env(db), cwd=str(SERVER_DIR), timeout=60)


def test_the_user_confirms_in_their_terminal(backend):
    S = backend
    item = _suggest(S)
    code, out = _in_a_terminal(S.backend.db_path, ["approve", str(item)], ["y"])
    assert code == 0 and f"Approved review item #{item}" in out, out
    status, by = _status(S.backend.db_path, item)
    assert status == "approved" and by == "command-line"


def test_answering_no_in_the_terminal_changes_nothing(backend):
    S = backend
    item = _suggest(S)
    code, out = _in_a_terminal(S.backend.db_path, ["reject", str(item)], ["n"])
    assert code == 1 and "nothing changed" in out, out
    assert _status(S.backend.db_path, item)[0] == "pending"


def test_several_items_with_one_confirmation(backend):
    S = backend
    a, b = _suggest(S), _suggest(S, app="Grok")
    code, out = _in_a_terminal(S.backend.db_path, ["approve", str(a), str(b)], ["y"])
    assert code == 0 and f"#{a}, #{b}" in out, out
    assert _status(S.backend.db_path, a)[0] == _status(S.backend.db_path, b)[0] == "approved"


def test_review_page_link_is_not_printed_without_a_terminal(backend):
    r = _cli(backend.backend.db_path, "review-page", "--no-browser", "--print-url", "--port", "0")
    assert r.returncode != 0 and "terminal" in (r.stdout + r.stderr) and "key=" not in r.stdout
