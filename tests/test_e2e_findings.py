"""Regression tests for bugs the end-to-end mock project test found (tests/e2e/run_e2e.py):
window A hears about window B, a save isn't reported twice, manage_sessions current is this
window's, a rule's receipt names it, and attached files are searchable. They failed on the
first 2.4.0 build and pass since its fixes; the other findings have their own test files
(test_realdata_guard, test_user_approval, test_data_git_sync, test_todo_claims, ...)."""

import asyncio
import json
import sqlite3
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import SERVER_DIR, server_env

TAG = "Since your last visit"
BRIEF = ("start_here", {"project": "Alpha"})


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        out = conn.execute(sql, a).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


def _since(text):
    return [line for line in text.splitlines() if line.startswith(TAG)]


def _session_no(brief):
    return int(brief.split("Session #", 1)[1].split()[0])


async def _call(c, name, args):
    return (await c.call_tool(name, args)).content[0].text


def _two_windows(db, steps, apps=("claude-code", "claude-code")):
    """Two windows = two real stdio server processes on one database; steps(a, b) is async."""
    params = StdioServerParameters(command=sys.executable, args=["-m", "flowstate"],
                                   env=server_env(db), cwd=str(SERVER_DIR))

    async def go():
        async with stdio_client(params) as (r1, w1), stdio_client(params) as (r2, w2):
            async with ClientSession(r1, w1, client_info=types.Implementation(name=apps[0], version="1")) as a, \
                    ClientSession(r2, w2, client_info=types.Implementation(name=apps[1], version="1")) as b:
                await a.initialize()
                await b.initialize()
                return await steps(a, b)
    return asyncio.run(go())


def test_since_line_tells_window_a_about_window_b(seeded_db):
    """Window A, then window B read the brief; B saves; A's next brief must mention it."""
    async def steps(a, b):
        await _call(a, *BRIEF)
        await asyncio.sleep(1.1)
        await _call(b, *BRIEF)
        await asyncio.sleep(1.1)
        await _call(b, "log_work", {"project": "Alpha", "kind": "change", "summary": "Window B change"})
        await asyncio.sleep(1.1)
        return await _call(a, *BRIEF)
    lines = _since(_two_windows(seeded_db, steps))
    assert lines and "another claude-code session" in lines[0], lines


def test_since_line_not_repeated_for_a_save_in_the_same_second(seeded_db):
    """A save already shown must not be shown again when it finished in the same second as
    the brief that showed it (here: claude-code reads, cursor saves)."""

    async def steps(a, b):
        await _call(a, *BRIEF)
        await asyncio.sleep(1.1)
        await _call(b, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Same second"})
        first = await _call(a, *BRIEF)
        # make the save and that brief share one second, as happens with fast agents
        at = _q(seeded_db, "SELECT at FROM run_views WHERE tool='start_here' ORDER BY id DESC LIMIT 1")[0][0]
        _q(seeded_db, "UPDATE run_writes SET finished_at = ? WHERE id = (SELECT MAX(id) FROM run_writes)", at)
        second = await _call(a, *BRIEF)
        return first, second
    first, second = _two_windows(seeded_db, steps, apps=("claude-code", "cursor"))
    assert _since(first), first
    assert _since(second) == [], _since(second)


def test_sessions_current_is_this_windows_session(seeded_db):
    async def steps(a, b):
        sa = _session_no(await _call(a, *BRIEF))
        sb = _session_no(await _call(b, *BRIEF))
        ca = json.loads(await _call(a, "manage_sessions", {"action": "current"}))
        cb = json.loads(await _call(b, "manage_sessions", {"action": "current"}))
        return (sa, sb), (ca.get("id"), cb.get("id"))
    briefs, current = _two_windows(seeded_db, steps)
    assert briefs[0] != briefs[1]
    assert current == briefs, f"briefs say {briefs}, manage_sessions current says {current}"


def test_rule_receipt_names_the_rule(backend):
    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="cursor", version="1")) as c:
            return json.loads(await _call(c, "manage_rules", {"action": "add", "project": "Alpha",
                                                              "rule": "Never log card numbers."}))
    r = asyncio.run(go())
    assert r["receipt"]["saved"] == [f"rule #{r['rule']['id']}"], r["receipt"]


def test_attached_file_text_is_searchable(backend, tmp_path):
    f = tmp_path / "menu.md"
    f.write_text("# Menu\nCroissant, sourdough, lemon tart.\n", encoding="utf-8")

    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="cursor", version="1")) as c:
            att = json.loads(await _call(c, "manage_files", {"action": "attach", "project": "Alpha",
                                                             "file_path": str(f)}))
            hits = json.loads(await _call(c, "manage_files", {"action": "search", "project": "Alpha",
                                                              "query": "sourdough"}))
            return att, hits
    att, hits = asyncio.run(go())
    assert "id" in att, att
    assert (backend.backend.db_path.parent / "projects" / "alpha.flowstate" / "attachments" / "menu.md").exists()
    assert hits, "manage_files search found nothing in the attached file"


def test_sessions_current_without_a_project_is_the_one_this_window_used_last(backend):
    """A window with sessions in two projects: current (no project) is the last one it used."""
    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="cursor", version="1")) as c:
            alpha = _session_no(await _call(c, *BRIEF))
            beta = _session_no(await _call(c, "start_here", {"project": "Beta Project"}))
            assert _session_no(await _call(c, *BRIEF)) == alpha     # back to Alpha, same second
            cur = json.loads(await _call(c, "manage_sessions", {"action": "current"}))
            return alpha, beta, cur.get("id")
    alpha, beta, cur = asyncio.run(go())
    assert alpha != beta and cur == alpha
