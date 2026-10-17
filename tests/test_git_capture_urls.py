"""Git commit capture (Phase D4l: off by default, subjects only, review mode applies) and
GitHub/Linear URLs on records (D4k). Temp DBs and a throwaway git repo in tmp only."""

import asyncio
import json
import shutil
import sqlite3
import subprocess

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import cli, git_capture, markdown_io

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def run(coro):
    return asyncio.run(coro)


def _j(r):
    return json.loads(r.content[0].text)


def _q(db, sql, *a):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(sql, a).fetchall()
    finally:
        conn.close()


def call(S, name, args):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="t", version="1")) as c:
            return await c.call_tool(name, args)
    return run(go())


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.invalid",
                        "PATH": __import__("os").environ["PATH"],
                        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")})


def _commit(repo, msg):
    (repo / "f.txt").write_text(msg, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", msg)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, "initial commit before capture")
    return r


def test_off_by_default_then_captures_new_subjects_only(backend, seeded_db, repo, capsys):
    call(backend, "start_here", {"project": "Alpha"})
    assert not _q(seeded_db, "SELECT 1 FROM changes WHERE field_name LIKE 'commit %'")
    assert git_capture.status(seeded_db) == []
    assert cli.main(["git-capture", "enable", "--project", "Alpha", "--repo", str(repo),
                     "--db", str(seeded_db)]) == 0
    _commit(repo, "Add export button")
    _commit(repo, "Fix crash on empty chapter")
    _commit(repo, "password=hunter2hunter2 oops")      # looks like a secret: skipped
    brief = call(backend, "start_here", {"project": "Alpha"}).content[0].text
    rows = [r[0] for r in _q(seeded_db, "SELECT field_name FROM changes WHERE field_name LIKE 'commit %' ORDER BY id")]
    assert len(rows) == 2 and rows[0].endswith("Add export button") and rows[1].endswith("Fix crash on empty chapter")
    assert "initial commit" not in " ".join(rows)                 # history before enabling is left out
    assert "Git:" in brief and "Fix crash" in brief
    dump = " ".join(" ".join(map(str, r)) for r in _q(seeded_db, "SELECT * FROM changes"))
    assert "t@example.invalid" not in dump and "hunter2" not in dump
    call(backend, "start_here", {"project": "Alpha"})            # nothing new: nothing added
    assert len(_q(seeded_db, "SELECT 1 FROM changes WHERE field_name LIKE 'commit %'")) == 2
    assert cli.main(["git-capture", "disable", "--project", "Alpha", "--db", str(seeded_db)]) == 0
    _commit(repo, "After disabling")
    call(backend, "start_here", {"project": "Alpha"})
    assert len(_q(seeded_db, "SELECT 1 FROM changes WHERE field_name LIKE 'commit %'")) == 2


def test_capture_follows_review_mode(backend, seeded_db, repo):
    call(backend, "review_mode", {"action": "set", "mode": "types", "kinds": ["change"],
                                  "user_request": "User: hold changes for review"})
    git_capture.enable(seeded_db, 1, str(repo))
    _commit(repo, "Refactor the brief")
    call(backend, "start_here", {"project": "Alpha"})
    assert _q(seeded_db, "SELECT kind, source FROM review_items") == [("change", "git-capture")]
    assert not _q(seeded_db, "SELECT 1 FROM changes WHERE field_name LIKE 'commit %'")


def test_enable_refuses_non_repo(seeded_db, tmp_path):
    with pytest.raises(git_capture.CaptureError):
        git_capture.enable(seeded_db, 1, str(tmp_path))


def test_urls_on_records_are_kept_and_shown(backend, seeded_db):
    r = _j(call(backend, "link", {"action": "add", "project": "Alpha", "item_type": "todo", "item_id": 1,
                                  "url": "https://github.com/dialectforge/flowstate/issues/12"}))
    assert r["link"]["to"] == {"type": "url", "url": "https://github.com/dialectforge/flowstate/issues/12",
                               "kind": "github"}
    lin = _j(call(backend, "link", {"action": "add", "project": "Alpha", "item_type": "todo", "item_id": 1,
                                    "url": "https://linear.app/df/issue/DF-7"}))
    assert lin["link"]["to"]["kind"] == "linear"
    nxt = _j(call(backend, "todo", {"action": "next", "project": "Alpha"}))
    assert nxt["next"][0]["urls"] == ["https://github.com/dialectforge/flowstate/issues/12",
                                      "https://linear.app/df/issue/DF-7"]
    assert "github.com/dialectforge/flowstate/issues/12" in markdown_io.export(seeded_db, 1)
    wrong = call(backend, "link", {"action": "add", "project": "Beta Project", "item_type": "todo",
                                   "item_id": 1, "url": "https://github.com/x/y"})
    assert _j(wrong)["error"] == "project_mismatch"
