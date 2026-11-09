"""2.5.0: the next start_here reminds the agent when the previous session on this project
read FlowState and saved nothing. Uses the run ledger only (no new table, no network)."""

import asyncio

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import ledger
from flowstate.stored import START


def run(S, app, *calls):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=app, version="1")) as c:
            return [(await c.call_tool(n, a)).content[0].text for n, a in calls]
    return asyncio.run(go())


def new_window(monkeypatch, name="next-window"):
    """A new stdio process. In-process tests share one process key, so the same app would
    otherwise keep the open session."""
    from flowstate import identity
    monkeypatch.setattr(identity, "PROCESS_SESSION", name)


BRIEF = ("start_here", {"project": "Alpha"})
BETA = ("start_here", {"project": "Beta Project"})


def log(kind, summary, project="Alpha"):
    return ("log_work", {"project": project, "kind": kind, "summary": summary})


def _line(text):
    return [ln for ln in text.splitlines() if ln == ledger.REMINDER]


def test_first_visit_has_no_reminder(backend):
    text = run(backend, "Cursor", BRIEF)[0]
    assert _line(text) == []


def test_a_read_only_session_is_reminded_next_time(backend):
    S = backend
    run(S, "Cursor", BRIEF, ("search", {"project": "Alpha", "query": "todo"}))
    text = run(S, "Claude Desktop", BRIEF)[0]
    assert _line(text) == [ledger.REMINDER]
    assert "\n" not in ledger.REMINDER
    # an instruction to the agent, not stored record text
    assert text.index(ledger.REMINDER) < text.index(START)


def test_search_without_start_here_still_counts_as_a_read(backend, monkeypatch):
    S = backend
    run(S, "Cursor", ("search", {"project": "Alpha", "query": "lesson"}))
    new_window(monkeypatch, "after-search")
    text = run(S, "Cursor", BRIEF)[0]
    assert ledger.REMINDER in text


def test_a_session_that_logged_is_not_reminded(backend):
    S = backend
    run(S, "Cursor", BRIEF, log("decision", "Use the ledger we already have"))
    text = run(S, "Cursor", BRIEF)[0]
    assert _line(text) == []


def test_only_the_previous_session_counts(backend):
    S = backend
    run(S, "Cursor", BRIEF)                                      # read, no write
    run(S, "Grok", BRIEF, log("change", "Logged the next time"))  # previous session wrote
    text = run(S, "Cursor", BRIEF)[0]
    assert _line(text) == []


def test_the_reminder_returns_after_another_quiet_session(backend, monkeypatch):
    S = backend
    run(S, "Cursor", BRIEF, log("learning", "A real lesson"))
    new_window(monkeypatch, "quiet-window")
    run(S, "Cursor", BRIEF)                     # read the brief, saved nothing
    new_window(monkeypatch, "next-window")
    text = run(S, "Cursor", BRIEF)[0]
    assert ledger.REMINDER in text


def test_another_project_is_not_reminded(backend):
    S = backend
    run(S, "Cursor", BRIEF)
    text = run(S, "Cursor", BETA)[0]
    assert ledger.REMINDER not in text


def test_the_line_fits_a_quick_brief(backend, monkeypatch):
    S = backend
    run(S, "Cursor", BRIEF)
    new_window(monkeypatch, "quick-window")
    text = run(S, "Cursor", ("start_here", {"project": "Alpha", "size": "quick"}))[0]
    assert ledger.REMINDER in text and len(text) <= 1500


def test_the_same_session_does_not_remind_itself(backend):
    """Two start_here calls in one connection are one session. It has not finished."""
    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="Cursor", version="1")) as c:
            first = (await c.call_tool("start_here", {"project": "Alpha"})).content[0].text
            second = (await c.call_tool("start_here", {"project": "Alpha"})).content[0].text
            return first, second
    first, second = asyncio.run(go())
    assert ledger.REMINDER not in first and ledger.REMINDER not in second
