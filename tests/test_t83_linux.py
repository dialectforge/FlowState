"""Task 83: two things a stock Linux (Ubuntu 24.04: git 2.43, Python 3.12.3) did differently.

(a) git's own default branch (`master` there, `main` with Xcode's git) no longer decides the
data-git branch: every computer syncs the remote's one. git is told `init.defaultBranch=master`
through the environment, so these tests fail without the fix on any computer.
(b) `flowstate rules add --project P "TEXT"`: argparse before Python 3.12.7 left a TEXT typed
after an option over ("unrecognized arguments"). Temp folders, local bare repositories and temp
databases only."""

import argparse
import sqlite3

import pytest

from flowstate import cli, cli_data, cli_review, rules
from flowstate.tools import git_ops
from test_session_records import FakeTTY
from test_t55_datagit import _git, on


@pytest.fixture
def master_git(monkeypatch):
    """git as on a stock Linux: a new repository starts on `master`."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "master")


def _bare(tmp_path, *branch) -> str:
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", *branch, str(bare), cwd=tmp_path)
    return str(bare)


def _heads(remote) -> dict:
    out = _git("for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads", cwd=remote)
    return dict(line.split() for line in out.splitlines())


def _branch(folder) -> str:
    return _git("branch", "--show-current", cwd=folder).strip()


def _computer(tmp_path, name, remote, *, branch="main", note=None):
    """A data folder made by `flowstate data-git init` (`branch="master"`: by an older FlowState
    on a git whose default is master), its remote set, one note of its own."""
    m = tmp_path / name
    m.mkdir()
    on(m, git_ops.git_init)
    if branch != "main":
        _git("branch", "-m", "main", branch, cwd=m)
    on(m, git_ops.git_set_remote, remote, allow_unchecked=True)
    (m / "projects" / f"{name}.md").write_text(note or f"from {name}")
    return m


# ---------------------------------------------------------------- (a) data-git branch
def test_init_is_on_main_whatever_gits_default_is(master_git, tmp_path, monkeypatch):
    a = tmp_path / "A"
    a.mkdir()
    on(a, git_ops.git_init)
    assert _branch(a) == "main"
    # git before 2.28 has no `init -b`: plain init, then HEAD is pointed at main
    real = git_ops._run_git_command

    def old_git(args, *rest, **kw):
        if args[:2] == ["init", "-b"]:
            return False, "", "error: unknown switch `b'"
        return real(args, *rest, **kw)
    monkeypatch.setattr(git_ops, "_run_git_command", old_git)
    b = tmp_path / "B"
    b.mkdir()
    r = on(b, git_ops.git_init)
    assert r.get("branch") == "main" and _branch(b) == "main", r
    assert _git("log", "--format=%s", cwd=b).strip() == "FlowState initialized"


def test_two_computers_on_a_master_default_git_sync_one_branch(master_git, tmp_path):
    remote = _bare(tmp_path)                   # an empty remote whose HEAD is master
    a = _computer(tmp_path, "A", remote)
    assert on(a, git_ops.git_sync, "A")["pushed"]
    b = tmp_path / "B"
    assert on(b, git_ops.git_clone, remote)["success"]
    assert _branch(b) == "main" and (b / "projects" / "A.md").read_text() == "from A"
    on(b, git_ops.git_set_remote, remote, allow_unchecked=True)
    (b / "projects" / "B.md").write_text("from B")
    assert on(b, git_ops.git_sync, "B")["pushed"]
    assert on(a, git_ops.git_sync, "A again")["success"]
    assert (a / "projects" / "B.md").read_text() == "from B"
    assert set(_heads(remote)) == {"main"}


def test_a_clone_of_an_empty_remote_starts_on_main(master_git, tmp_path):
    remote = _bare(tmp_path)
    b = tmp_path / "B"
    assert on(b, git_ops.git_clone, remote)["success"]
    assert _branch(b) == "main"


def test_a_master_computer_joins_a_main_remote(master_git, tmp_path):
    remote = _bare(tmp_path, "-b", "main")
    b = _computer(tmp_path, "B", remote)
    assert on(b, git_ops.git_sync, "B")["pushed"]
    a = _computer(tmp_path, "A", remote, branch="master")
    r = on(a, git_ops.git_sync, "A")
    assert r["success"] and r["pushed"] and r["branch"] == "main" and r["moved_from"] == "master", r
    assert "note" not in r and _branch(a) == "main"
    assert "now syncs the remote's branch `main` (it was on `master`)" in cli_data._moved(r)
    assert set(_heads(remote)) == {"main"}                    # no second branch on the remote
    assert (a / "projects" / "B.md").read_text() == "from B"   # B's changes reached A
    assert (a / "projects" / "A.md").read_text() == "from A"
    assert on(b, git_ops.git_sync, "B again")["success"]
    assert (b / "projects" / "A.md").read_text() == "from A"   # and A's reached B


def test_a_main_computer_follows_a_master_remote(master_git, tmp_path):
    remote = _bare(tmp_path)                   # made by a FlowState that used git's default
    a = _computer(tmp_path, "A", remote, branch="master")
    assert on(a, git_ops.git_sync, "A")["pushed"]
    b = _computer(tmp_path, "B", remote)
    r = on(b, git_ops.git_sync, "B")
    assert r["success"] and r["branch"] == "master" and r["moved_from"] == "main", r
    assert set(_heads(remote)) == {"master"} and _branch(b) == "master"
    assert on(a, git_ops.git_sync, "A again")["success"]
    assert (a / "projects" / "B.md").read_text() == "from B"


def test_a_remote_with_both_branches_is_refused_until_they_are_joined(master_git, tmp_path):
    """A computer that already pushed `master` to a remote on `main` (FlowState 2.4.3 syncs
    git's own branch): nothing is pulled or pushed, and the error says what to do; that
    computer's next sync moves its changes to main and removes master in the same push."""
    remote = _bare(tmp_path, "-b", "main")
    b = _computer(tmp_path, "B", remote)
    assert on(b, git_ops.git_sync, "B")["pushed"]
    a = _computer(tmp_path, "A", remote, branch="master")
    _git("add", "-A", cwd=a)
    _git("commit", "-qm", "2.4.3 sync", cwd=a)
    _git("push", "-q", "origin", "master", cwd=a)
    before = _heads(remote)
    (b / "projects" / "B.md").write_text("B, second")
    r = on(b, git_ops.git_sync, "B second")
    assert r["success"] is False and r["refused"] and r["step"] == "branch", r
    assert r["committed"] and r["pushed"] is False
    assert "`main` and `master`" in r["error"] and "committed locally" in r["error"]
    assert _heads(remote) == before
    assert (b / "projects" / "B.md").read_text() == "B, second"
    # a third computer pushed to master since A did: A doesn't drop what it hasn't got
    c = tmp_path / "C"
    _git("clone", "-q", "-b", "master", remote, str(c), cwd=tmp_path)
    (c / "projects" / "C.md").write_text("from C")
    _git("add", "-A", cwd=c)
    _git("commit", "-qm", "C", cwd=c)
    _git("push", "-q", "origin", "master", cwd=c)
    r = on(a, git_ops.git_sync, "A")
    assert r["success"] is False and r["refused"] and "`main` and `master`" in r["error"], r
    assert set(_heads(remote)) == {"main", "master"}
    # C runs 2.4.4: it holds all of master, so it moves to main and master goes
    on(c, git_ops.git_set_remote, remote, allow_unchecked=True)
    r = on(c, git_ops.git_sync, "C on 2.4.4")
    assert r["success"] and r["moved_from"] == "master", r
    assert set(_heads(remote)) == {"main"}
    r = on(a, git_ops.git_sync, "A on 2.4.4")
    assert r["success"] and r["pushed"] and r["moved_from"] == "master", r
    assert on(b, git_ops.git_sync, "B third")["success"]
    for m in (a, b, c):
        assert on(m, git_ops.git_sync, "all")["success"]
        for name, text in (("A", "from A"), ("B", "B, second"), ("C", "from C")):
            assert (m / "projects" / f"{name}.md").read_text() == text, (m, name)
    assert set(_heads(remote)) == {"main"}


# ---------------------------------------------------------------- (b) rules add
def _old_argparse(monkeypatch):
    """`rules` parses as argparse before Python 3.12.7 did: a positional typed after an option
    is left over instead of filling `text` (on such a Python this changes nothing)."""
    build = cli.build_parser

    def parser():
        ap = build()
        sub = next(x for x in ap._actions if isinstance(x, argparse._SubParsersAction))
        rp = sub.choices["rules"]
        parse = rp.parse_known_args

        def old(args=None, namespace=None):
            ns, extra = parse(args, namespace)
            first_option = next((i for i, t in enumerate(args) if t.startswith("-")), len(args))
            if ns.text is not None and ns.text not in args[:first_option]:
                extra.append(ns.text)
                ns.text = None
            return ns, extra
        rp.parse_known_args = old
        return ap
    monkeypatch.setattr(cli, "build_parser", parser)


@pytest.mark.parametrize("old", [False, True], ids=["this-python", "argparse-before-3.12.7"])
def test_rules_take_the_text_before_or_after_the_options(seeded_db, capsys, monkeypatch, old):
    if old:
        _old_argparse(monkeypatch)
    db = str(seeded_db)
    pid = sqlite3.connect(db).execute("SELECT id FROM projects WHERE name='Alpha'").fetchone()[0]
    orders = (["rules", "add", "--project", "Alpha", "After the options", "--db", db],
              ["rules", "add", "--project", "Alpha", "--db", db, "At the end"],
              ["rules", "add", "Before the options", "--project", "Alpha", "--db", db])
    for argv in orders:
        monkeypatch.setattr(cli_review, "open_terminal", lambda: FakeTTY(["y"]))
        assert cli.main(argv) == 0, argv
    added = [r["rule"] for r in rules.list_rules(seeded_db, pid)["rules"]]
    assert added == ["After the options", "At the end", "Before the options"]
    # still refused as before, in either place
    tty = FakeTTY(["y"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert cli.main(["rules", "add", "--project", "Alpha", "bell\x07 rule", "--db", db]) == 1
    assert "Not added" in capsys.readouterr().out and tty.told == []
    rid = rules.list_rules(seeded_db, pid)["rules"][0]["id"]
    monkeypatch.setattr(cli_review, "open_terminal", lambda: FakeTTY(["y"]))
    assert cli.main(["rules", "remove", "--db", db, f"R{rid}"]) == 0
    # anything else left over is still an error
    with pytest.raises(SystemExit):
        cli.main(["rules", "add", "one", "--project", "Alpha", "two", "--db", db])
    assert "unrecognized arguments: two" in capsys.readouterr().err
