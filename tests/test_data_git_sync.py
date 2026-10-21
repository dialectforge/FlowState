"""manage_data_git sync reports what really happened (2.4.0): an unreachable remote is not
"pushed", and the first sync to a brand-new empty remote pushes instead of failing as a
"conflict". Local bare repositories and a closed port only; nothing leaves this computer."""

import socket
import subprocess
from pathlib import Path

import pytest

from flowstate.tools import git_ops


@pytest.fixture
def data(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(git_ops.utils, "data_dir", lambda: d)
    assert git_ops.git_init().get("initialized")
    (d / "projects" / "note.md").write_text("first", encoding="utf-8")
    return d


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]     # closed again once we return: connections are refused


@pytest.mark.parametrize("where", ["closed port", "unknown host"])
def test_unreachable_remote_is_not_reported_as_pushed(data, where):
    url = (f"http://127.0.0.1:{_closed_port()}/flowstate.git" if where == "closed port"
           else "https://flowstate-e2e.invalid/flowstate.git")    # .invalid never resolves
    git_ops.git_set_remote(url)
    r = git_ops.git_sync("offline sync")
    assert r["success"] is False and r["pushed"] is False and r.get("offline") is True, r
    assert r["committed"] is True and "committed locally" in r["error"]
    assert "conflict" not in r


def test_first_sync_to_an_empty_remote_pushes(data, tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git_ops.git_set_remote(str(remote))
    r = git_ops.git_sync("first")
    assert r["success"] is True and r["pushed"] is True and r["first_push"] is True, r
    branch = _git("branch", "--show-current", cwd=data).strip()
    assert "first" in _git("log", "--oneline", branch, cwd=remote)
    # and the next sync pulls and pushes as usual
    (data / "projects" / "note.md").write_text("second", encoding="utf-8")
    r2 = git_ops.git_sync("second")
    assert r2["success"] is True and r2["pushed"] is True and r2["first_push"] is False, r2


def test_a_real_conflict_is_labelled_and_left_clean(data, tmp_path):
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(remote), cwd=tmp_path)
    git_ops.git_set_remote(str(remote))
    assert git_ops.git_sync("base")["pushed"]
    other = tmp_path / "other"
    _git("clone", "-q", str(remote), str(other), cwd=tmp_path)
    (other / "projects" / "note.md").write_text("theirs", encoding="utf-8")
    _git("commit", "-qam", "theirs", cwd=other)
    _git("push", "-q", cwd=other)
    (data / "projects" / "note.md").write_text("mine", encoding="utf-8")
    r = git_ops.git_sync("mine")
    assert r["success"] is False and r["conflict"] is True and r["pushed"] is False, r
    assert not (data / ".git" / "rebase-merge").exists() and not (data / ".git" / "rebase-apply").exists()
    assert (data / "projects" / "note.md").read_text() == "mine"
