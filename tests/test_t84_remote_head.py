"""Task 84: a bare remote made where git's default is `master` has a HEAD that names a branch it
doesn't have. After FlowState pushes `main`, a plain `git clone` of it would check out nothing.
A remote on this computer gets its HEAD pointed at the pushed branch; any other remote is left
as it is and the sync says so. git is told `init.defaultBranch=master` through the environment,
so these tests fail without the fix on any computer. Temp folders and local bare repositories
only; the "ssh" remote runs through a stand-in for ssh, nothing leaves this computer."""

import sys
from pathlib import Path

import pytest

from flowstate import cli, cli_data
from flowstate.tools import git_ops
from test_t55_datagit import _git, on
from test_t83_linux import _bare, _computer, master_git  # noqa: F401 (a fixture)


def _head(remote) -> str:
    return _git("symbolic-ref", "HEAD", cwd=remote).strip()


def _plain_clone(remote, where):
    _git("clone", "-q", str(remote), str(where), cwd=where.parent)
    return where


FAKE_SSH = """
import re, shlex, subprocess, sys
if sys.argv[1] == "-G":      # git asking which kind of ssh this is: a plain one
    sys.exit(1)
verb, path = shlex.split(sys.argv[-1])     # git-upload-pack '/the/path'
path = re.sub(r"^/(?=[A-Za-z]:)", "", path)    # /C:/the/path on Windows
sys.exit(subprocess.call(["git", verb.removeprefix("git-"), path]))
"""


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """`ssh` that runs the remote command here, so the remote really is an ssh:// one."""
    script = tmp_path / "fake_ssh.py"
    script.write_text(FAKE_SSH, encoding="utf-8")
    monkeypatch.setenv("GIT_SSH_COMMAND",
                       f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"')
    monkeypatch.setenv("MSYS2_ARG_CONV_EXCL", "*")   # Git for Windows' sh: leave the path alone


@pytest.mark.parametrize("form", ["path", "file-url"])
def test_a_plain_clone_after_the_first_push_checks_out_the_files(master_git, tmp_path, form):
    remote = _bare(tmp_path)
    assert _head(remote) == "refs/heads/master"
    url = remote if form == "path" else Path(remote).as_uri()
    a = _computer(tmp_path, "A", url)
    r = on(a, git_ops.git_sync, "A")
    assert r["pushed"] and "warning" not in r, r
    assert _head(remote) == "refs/heads/main"
    b = _plain_clone(remote, tmp_path / "B")
    assert (b / "projects" / "A.md").read_text() == "from A"


def test_a_remote_left_without_its_head_branch_is_mended_by_the_next_sync(master_git, tmp_path):
    """FlowState 2.4.3 on a computer whose git starts on `main` pushed to such a remote."""
    remote = _bare(tmp_path)
    a = _computer(tmp_path, "A", remote)
    _git("add", "-A", cwd=a)
    _git("commit", "-qm", "2.4.3 sync", cwd=a)
    _git("push", "-q", "origin", "main", cwd=a)
    assert _head(remote) == "refs/heads/master"
    assert on(a, git_ops.git_sync, "A")["pushed"]
    assert _head(remote) == "refs/heads/main"


def test_a_head_on_a_branch_that_exists_is_left_alone(master_git, tmp_path):
    remote = _bare(tmp_path, "-b", "develop")
    c = _computer(tmp_path, "C", remote, branch="develop")
    assert on(c, git_ops.git_sync, "C")["pushed"]
    a = _computer(tmp_path, "A", remote)
    r = on(a, git_ops.git_sync, "A")
    assert r["pushed"] and r["branch"] == "develop" and "warning" not in r, r
    assert _head(remote) == "refs/heads/develop"
    # even with `main` on the remote too, and asked to point it there
    _git("update-ref", "refs/heads/main", "refs/heads/develop", cwd=remote)
    assert on(a, git_ops._point_remote_head, "main") is None
    assert _head(remote) == "refs/heads/develop"


def test_a_remote_it_cant_change_still_syncs_and_says_so(master_git, fake_ssh, tmp_path, capsys,
                                                         monkeypatch):
    remote = _bare(tmp_path)
    url = f"ssh://git.example.invalid/{Path(remote).as_posix().lstrip('/')}"
    a = _computer(tmp_path, "A", url)
    r = on(a, git_ops.git_sync, "A")
    assert r["success"] and r["pushed"], r
    assert "default branch" in r["warning"] and "`main`" in r["warning"]
    assert _head(remote) == "refs/heads/master"                 # never touched
    assert "main" in _git("for-each-ref", "--format=%(refname:short)", "refs/heads", cwd=remote)
    # the user's own `flowstate data-git sync` prints it
    monkeypatch.setattr(git_ops.utils, "data_dir", lambda: a)
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 0)   # the user typed y
    monkeypatch.setattr(cli_data, "_export_all", lambda db, folder: 0)
    (a / "projects" / "A.md").write_text("A, second")
    assert cli.main(["data-git", "sync", "--db", str(a / "flowstate.db")]) == 0
    assert "default branch" in capsys.readouterr().out
    # once the host's default branch is right, the warning is gone
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    (a / "projects" / "A.md").write_text("A, third")
    assert "warning" not in on(a, git_ops.git_sync, "A third")


@pytest.mark.parametrize("url", ["git@github.com:me/data.git", "ssh://git@example.invalid/data.git",
                                 "https://example.invalid/data.git"])
def test_only_a_bare_repository_on_this_computer_counts_as_local(tmp_path, url):
    assert git_ops._local_bare(url) is None


def test_a_working_folder_is_not_a_bare_remote(master_git, tmp_path):
    a = tmp_path / "A"
    a.mkdir()
    _git("init", "-q", cwd=a)
    assert git_ops._local_bare(str(a / ".git")) is None
