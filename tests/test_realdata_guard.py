"""The test suite's real-data guard, and bug 13: files and data-git follow the database.

Nothing here creates anything in the real folders: the guard refuses before the file is
opened, and the probe names are checked to still be missing afterwards."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

import conftest
from conftest import LIVE_DIRS, SERVER_DIR, server_env


def test_every_flowstate_path_resolves_into_the_sandbox():
    found = conftest.resolved_flowstate_paths()
    assert set(found) >= {"db", "backups", "config", "auth_db", "relay", "data (files, data-git)"}
    assert not [k for k, v in found.items() if conftest._guard.forbidden(v)], found
    assert Path.home() != conftest.REAL_HOME


def test_guard_refuses_in_this_process_and_records_it(tmp_path, monkeypatch):
    log = tmp_path / "refused.log"
    monkeypatch.setattr(conftest._guard, "_LOG", str(log))   # keep the probe out of the session log
    probe = Path(LIVE_DIRS[0]) / "fs-guard-probe-never-created.txt"
    with pytest.raises(PermissionError, match="test guard"):
        open(probe, "w")
    import sqlite3
    with pytest.raises(PermissionError):
        sqlite3.connect(str(Path(LIVE_DIRS[0]) / "fs-guard-probe.db"))
    assert not probe.exists()
    assert "fs-guard-probe-never-created.txt" in log.read_text()


def test_guard_refuses_in_a_server_process(seeded_db, tmp_path):
    log = tmp_path / "child-refused.log"
    env = server_env(seeded_db, FLOWSTATE_GUARD_LOG=log)
    probe = Path(LIVE_DIRS[0]) / "fs-guard-child-probe.txt"
    code = f"open({str(probe)!r}, 'w')"
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=str(SERVER_DIR),
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "test guard" in r.stderr
    assert not probe.exists() and str(probe) in log.read_text()
    r = subprocess.run([sys.executable, "-c", "import subprocess; subprocess.run(['security', 'list-keychains'])"],
                       env=env, cwd=str(SERVER_DIR), capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "Keychain" in r.stderr


def _attach(backend, path):
    async def go():
        async with create_connected_server_and_client_session(
                backend.server, client_info=types.Implementation(name="cursor", version="1")) as c:
            r = await c.call_tool("manage_files", {"action": "attach", "project": "Alpha", "file_path": str(path)})
            return json.loads(r.content[0].text)
    return asyncio.run(go())


def test_attachments_go_next_to_the_database_in_use(backend, tmp_path):
    f = tmp_path / "note.md"
    f.write_text("hello", encoding="utf-8")
    att = _attach(backend, f)
    assert "id" in att, att
    copy = Path(backend.backend.db_path).parent / "projects" / "alpha.flowstate" / "attachments" / "note.md"
    assert copy.read_text() == "hello"


def test_data_git_uses_the_database_folder(backend):
    from flowstate.tools import git_ops
    r = git_ops.git_init()
    assert (Path(backend.backend.db_path).parent / ".git").is_dir(), r


def test_db_flag_moves_attachments_with_the_database(tmp_path):
    """`flowstate --db <file>` with no FLOWSTATE_DB: the file copy lands beside <file>."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = server_env(tmp_path / "unused.db")
    env.pop("FLOWSTATE_DB")
    env["HOME"] = str(tmp_path / "home")
    f = tmp_path / "menu.md"
    f.write_text("sourdough", encoding="utf-8")
    params = StdioServerParameters(command=sys.executable, args=["-m", "flowstate", "--db", str(other / "bakery.db")],
                                   env=env, cwd=str(SERVER_DIR))

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as c:
                await c.initialize()
                await c.call_tool("manage_projects", {"action": "create", "name": "Flag Bakery"})
                return json.loads((await c.call_tool("manage_files", {"action": "attach", "project": "Flag Bakery",
                                                                      "file_path": str(f)})).content[0].text)
    att = asyncio.run(go())
    assert "id" in att, att
    hits = [p for p in tmp_path.rglob("menu.md") if p != f]
    assert hits and all(other in p.parents for p in hits), hits
