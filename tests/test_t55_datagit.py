"""Task 55, data-git and attachments (re-audit N-F14, N-F12, N-F25, N-F28).

Machines are folders; the remote is a local bare repository; a FlowState 2.4.3 peer runs the
real 2.4.3 data-git code (`git show v2.4.3:...`), or the same git commands when this checkout
has no history. Nothing leaves this computer."""

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO
from flowstate.tools import git_ops

POSIX = os.name != "nt"


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


def _old_module():
    """FlowState 2.4.3's tools/git_ops.py, loaded beside the current one."""
    r = subprocess.run(["git", "show", "v2.4.3:mcp-server/flowstate/tools/git_ops.py"], cwd=REPO,
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    name = "flowstate.tools._git_ops_243"
    spec = importlib.util.spec_from_loader(name, loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "flowstate.tools"
    exec(compile(r.stdout, "git_ops_243.py", "exec"), mod.__dict__)   # noqa: S102 - our own release
    sys.modules[name] = mod
    return mod


OLD = _old_module()


def on(folder: Path, fn, *a, **k):
    """Run `fn` as the machine whose data folder is `folder`."""
    before = git_ops.utils.data_dir
    git_ops.utils.data_dir = lambda: folder
    try:
        return fn(*a, **k)
    finally:
        git_ops.utils.data_dir = before


def old_sync(folder: Path):
    """A 2.4.3 machine's `data-git sync` (git add ., commit, pull --rebase, push)."""
    if OLD is not None:
        return on(folder, OLD.git_sync, "2.4.3 sync")
    _git("add", ".", cwd=folder)
    if _git("status", "--porcelain", cwd=folder).strip():
        _git("commit", "-qm", "2.4.3 sync", cwd=folder)
    branch = _git("branch", "--show-current", cwd=folder).strip()
    subprocess.run(["git", "pull", "-q", "--rebase", "origin", branch], cwd=folder)
    subprocess.run(["git", "push", "-q", "-u", "origin", branch], cwd=folder)
    return {}


@pytest.fixture
def remote(tmp_path):
    bare = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    return bare


def _legacy_pair(tmp_path, remote):
    """Machine A made the repository with FlowState 2.4.3 (the database is in git); machine B
    cloned it. Both have their own live database afterwards."""
    a = tmp_path / "A"
    a.mkdir()
    (a / "flowstate.db").write_text("A-v1")
    (a / "projects").mkdir()
    (a / "projects" / "note.md").write_text("note")
    _git("init", "-q", "-b", "main", cwd=a)
    _git("add", ".", cwd=a)
    _git("commit", "-qm", "2.4.3 init", cwd=a)
    _git("remote", "add", "origin", str(remote), cwd=a)
    old_sync(a)
    b = tmp_path / "B"
    _git("clone", "-q", str(remote), str(b), cwd=tmp_path)
    assert (b / "flowstate.db").read_text() == "A-v1"
    return a, b


def _tip_files(remote) -> set:
    return set(_git("ls-tree", "-r", "--name-only", "main", cwd=remote).split())


# ---------------------------------------------------------------- N-F14
def test_upgrading_one_machine_never_deletes_the_database_on_a_243_peer(tmp_path, remote):
    a, b = _legacy_pair(tmp_path, remote)
    (b / "flowstate.db").write_text("B-live")
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    tip = _git("rev-parse", "main", cwd=remote)
    r = on(b, git_ops.git_sync, "B on 2.4.4")       # B was updated first
    old_sync(a)                                      # A still runs 2.4.3
    assert (a / "flowstate.db").read_text() == "A-v1"   # (the old code deleted it here)
    assert (b / "flowstate.db").read_text() == "B-live"
    assert r["success"] is False and r["step"] == "legacy" and r["pushed"] is False, r
    assert "flowstate.db" in r["still_tracked"] and "data-git migrate" in r["error"]
    assert _git("rev-parse", "main", cwd=remote) == tip   # nothing was pushed


def test_migrate_once_every_machine_is_updated(tmp_path, remote):
    a, b = _legacy_pair(tmp_path, remote)
    (a / "flowstate.db").write_text("A-live")
    (b / "flowstate.db").write_text("B-live")
    for m in (a, b):
        on(m, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    r = on(b, git_ops.git_migrate)
    assert r["success"] and r["pushed"] and r["untracked"] == ["flowstate.db"], r
    assert (b / "flowstate.db").read_text() == "B-live"
    assert _tip_files(remote) == {"projects/note.md", ".gitignore"}   # task 57, N2
    assert on(b, git_ops.git_sync, "B")["success"]
    # A (updated too) is told to migrate; that only untracks here, then syncs as usual
    r = on(a, git_ops.git_sync, "A")
    assert r["step"] == "legacy" and (a / "flowstate.db").read_text() == "A-live"
    r = on(a, git_ops.git_migrate)
    assert r["success"] and r["pushed"] is False, r
    (a / "projects" / "more.md").write_text("from A")
    r = on(a, git_ops.git_sync, "A again")
    assert r["success"] and r["pushed"], r
    assert (a / "flowstate.db").read_text() == "A-live" and (b / "flowstate.db").read_text() == "B-live"
    assert on(b, git_ops.git_sync, "B again")["success"]
    assert (b / "projects" / "more.md").read_text() == "from A"
    assert (b / "flowstate.db").read_text() == "B-live"
    assert not [p for p in _tip_files(remote) if p.endswith(".db")]


def test_migrate_backs_up_the_database_first(tmp_path, remote, fresh_db, monkeypatch):
    import shutil
    a, b = _legacy_pair(tmp_path, remote)
    shutil.copy(fresh_db, b / "flowstate.db")        # a real database this time
    backups = tmp_path / "bk"
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(backups))
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    assert on(b, git_ops.git_migrate, db_path=b / "flowstate.db")["success"]
    assert [p.name for p in backups.glob("*pre-bulk*.db")]


def test_a_database_pushed_to_the_remote_never_replaces_this_computers(tmp_path, remote):
    b = tmp_path / "B"
    b.mkdir()
    on(b, git_ops.git_init)
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    (b / "projects" / "n.md").write_text("one")
    assert on(b, git_ops.git_sync, "first")["pushed"]
    (b / "flowstate.db").write_text("B-live")       # not in git (2.4.4)
    (b / "config.json").write_text("{}")
    c = tmp_path / "C"                               # a 2.4.3 peer, or anyone who can push
    _git("clone", "-q", str(remote), str(c), cwd=tmp_path)
    (c / "flowstate.db").write_text("C-db")
    (c / "config.json").write_text('{"x": 1}')
    _git("add", "-f", "flowstate.db", "config.json", cwd=c)
    _git("commit", "-qm", "db", cwd=c)
    _git("push", "-q", cwd=c)
    (b / "projects" / "n.md").write_text("two")
    r = on(b, git_ops.git_sync, "second")
    assert (b / "flowstate.db").read_text() == "B-live"   # (the old code replaced it)
    assert (b / "config.json").read_text() == "{}"
    assert r["success"] is False and r["refused"] and r["pushed"] is False, r
    assert set(r["remote_tracks"]) == {"flowstate.db", "config.json"}
    assert r["committed"] is True and "committed locally" in r["error"]


# ---------------------------------------------------------------- N-F12
@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_a_link_pushed_to_the_remote_is_never_pulled(tmp_path, remote):
    target = tmp_path / "zshrc"
    target.write_text("mine")
    b = tmp_path / "B"
    b.mkdir()
    on(b, git_ops.git_init)
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    (b / "projects" / "n.md").write_text("one")
    assert on(b, git_ops.git_sync, "first")["pushed"]
    c = tmp_path / "C"
    _git("clone", "-q", str(remote), str(c), cwd=tmp_path)
    (c / "export").mkdir()
    os.symlink(str(target), c / "export" / "Alpha-1.md")
    _git("add", "-A", cwd=c)
    _git("commit", "-qm", "link", cwd=c)
    _git("push", "-q", cwd=c)
    r = on(b, git_ops.git_sync, "second")
    assert not os.path.lexists(b / "export" / "Alpha-1.md")   # (the old code pulled the link)
    assert r["success"] is False and r["refused"] and r["unsafe"] == ["export/Alpha-1.md"], r
    assert target.read_text() == "mine"


@pytest.fixture
def cli_db(seeded_db, monkeypatch):
    from flowstate import cli_data
    monkeypatch.setattr(cli_data, "_confirm", lambda *a: 0)   # the user typed y
    return seeded_db


def _sync(db):
    from flowstate import cli
    return cli.main(["data-git", "sync", "--db", str(db)])


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_sync_never_writes_the_export_through_a_link(cli_db, tmp_path, capsys):
    from flowstate import cli
    target = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    target.write_text("mine")
    data = cli_db.parent
    assert cli.main(["data-git", "init", "--db", str(cli_db)]) == 0
    assert _sync(cli_db) == 0
    exported = sorted((data / "export").glob("*.md"))
    assert exported
    exported[0].unlink()
    os.symlink(str(target), exported[0])   # as an older version's pull left it
    capsys.readouterr()
    assert _sync(cli_db) == 1
    assert target.read_text() == "mine"
    assert "Refused" in capsys.readouterr().out
    # a link for the whole folder is refused too
    exported[0].unlink()
    (data / "export").rename(data / "export-real")
    os.symlink(str(data / "export-real"), data / "export")
    assert _sync(cli_db) == 1
    target.unlink()


def test_safe_write_replaces_a_link_instead_of_following_it(tmp_path):
    target = tmp_path / "t.txt"
    target.write_text("mine")
    if not POSIX:
        pytest.skip("symlinks")
    link = tmp_path / "x.md"
    os.symlink(str(target), link)
    with pytest.raises(git_ops.UnsafePath):
        git_ops.safe_write(link, "theirs")
    assert target.read_text() == "mine"


# ---------------------------------------------------------------- N-F28
@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_the_export_is_owner_only(cli_db):
    from flowstate import cli
    data = cli_db.parent
    assert cli.main(["data-git", "init", "--db", str(cli_db)]) == 0
    (data / "export").mkdir(mode=0o755)
    os.chmod(data / "export", 0o755)     # made by an earlier version
    assert _sync(cli_db) == 0
    assert stat.S_IMODE(os.stat(data / "export").st_mode) == 0o700
    files = list((data / "export").glob("*.md"))
    assert files and all(stat.S_IMODE(os.stat(f).st_mode) == 0o600 for f in files)


# ---------------------------------------------------------------- N-F25
def test_init_keeps_an_existing_readme_and_gitignore(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "README.md").write_text("my own readme")
    (d / ".gitignore").write_text("!/flowstate.db\nmy-line\n")
    (d / "flowstate.db").write_text("db")
    assert on(d, git_ops.git_init)["initialized"]
    assert (d / "README.md").read_text() == "my own readme"
    text = (d / ".gitignore").read_text()
    assert text.startswith("!/flowstate.db\nmy-line\n") and "\n/*\n" in text and "\n*.db\n" in text
    assert "flowstate.db" not in _git("ls-files", cwd=d).split()
    on(d, git_ops.git_sync, "again")
    assert (d / ".gitignore").read_text() == text   # added once


def test_attach_refuses_a_project_name_that_leads_outside(tmp_path):
    from flowstate.database import Database
    from flowstate.tools import files, utils
    db = tmp_path / "data" / "fs.db"
    db.parent.mkdir()
    d = Database(db)
    p = d.create_project("../../escape", "x")
    src = tmp_path / "spec.txt"
    src.write_text("hello")
    before = utils._db
    utils.set_db(d)
    try:
        with pytest.raises(ValueError):
            files.attach_file(p.id, str(src))
        ok = d.create_project("Fine Name", "y")
        r = files.attach_file(ok.id, str(src))
        assert r
    finally:
        utils._db = before
    assert not (tmp_path / "escape.flowstate").exists()
    assert not list(tmp_path.glob("*.flowstate"))
    assert (db.parent / "projects" / "fine-name.flowstate" / "attachments" / "spec.txt").exists()


# ---------------------------------------------------------------- task 57: N1, N2
def test_migrate_works_on_a_computer_that_is_behind_the_remote(tmp_path, remote):
    """N1: the remote moved on since this computer last synced (a 2.4.3 peer pushed its
    database and a note). The migrate commit is built in FlowState's own index; git used to
    compare that index with the files on disk and refuse ("use -f to force removal")."""
    a, b = _legacy_pair(tmp_path, remote)
    (b / "flowstate.db").write_text("B-v2")          # B still runs 2.4.3 and syncs
    (b / "projects" / "from-b.md").write_text("B's note")
    old_sync(b)
    (a / "flowstate.db").write_text("A-live")        # A, updated, is behind
    on(a, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    r = on(a, git_ops.git_migrate)
    assert r["success"] and r["pushed"] and r["untracked"] == ["flowstate.db"], r
    assert (a / "flowstate.db").read_text() == "A-live"             # nothing lost here
    assert (a / "projects" / "from-b.md").read_text() == "B's note"  # B's work restored
    assert "flowstate.db" not in _tip_files(remote)
    # B is updated next and migrates too (behind A's migrate commit): no error, nothing lost
    (b / "flowstate.db").write_text("B-live")
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    r = on(b, git_ops.git_migrate)
    assert r["success"], r
    assert on(b, git_ops.git_sync, "B")["success"]
    assert on(a, git_ops.git_sync, "A")["success"]
    assert (a / "flowstate.db").read_text() == "A-live" and (b / "flowstate.db").read_text() == "B-live"
    assert (b / "projects" / "from-b.md").read_text() == "B's note"


def test_the_migrate_commit_carries_flowstates_gitignore(tmp_path, remote):
    """N2: a 2.4.3 computer that pulls the migrate commit gets the .gitignore with it, so its
    next sync (git add .) can't commit its database again."""
    a, b = _legacy_pair(tmp_path, remote)
    (a / ".gitignore").unlink(missing_ok=True)
    on(a, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    r = on(a, git_ops.git_migrate)
    assert r["success"] and r["pushed"], r
    assert ".gitignore" in _tip_files(remote)
    ignore = _git("show", "main:.gitignore", cwd=remote)
    assert "*.db" in ignore.split()
    old_sync(b)                                       # B still runs 2.4.3
    (b / "flowstate.db").write_text("B made a new one")
    (b / "projects" / "b.md").write_text("more")
    old_sync(b)
    assert not [p for p in _tip_files(remote) if p.endswith(".db")]
    assert "projects/b.md" in _tip_files(remote)


def test_the_migrate_commit_keeps_the_repositorys_own_gitignore_lines(tmp_path, remote):
    a, b = _legacy_pair(tmp_path, remote)
    (b / ".gitignore").write_text("my-notes/\n")
    old_sync(b)
    on(a, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    assert on(a, git_ops.git_migrate)["success"]
    lines = _git("show", "main:.gitignore", cwd=remote).splitlines()
    assert lines[0] == "my-notes/" and "*.db" in lines


# ---------------------------------------------------------------- task 57: N3
def _db_with(folder: Path, projects: dict) -> Path:
    from flowstate.database import Database
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "flowstate.db"
    d = Database(db)
    for name, todo in projects.items():
        p = d.create_project(name, "")
        d.add_todo(p.id, todo, None, "medium")
    return db


def _sync_as(folder: Path, db: Path, msg: str) -> dict:
    """What `flowstate data-git sync` does after its y: export, sync, export again."""
    from flowstate import cli_data
    on(folder, cli_data._export_all, db, folder)
    r = on(folder, git_ops.git_sync, msg, db_path=db)
    if r.get("success"):
        on(folder, cli_data._export_all, db, folder)
    return r


def test_two_244_computers_sync_exports_without_clashing(tmp_path, remote):
    """N3: in 2.4.4 each computer exports its own database. The same export file written on
    both no longer stops every sync with a clash: this computer's copy wins (it is made again
    at each sync). A computer never deletes the other's exports of projects it doesn't know."""
    from flowstate.database import Database
    a = tmp_path / "A"
    db_a = _db_with(a, {"Alpha": "todo from A", "Only A": "a's own"})
    on(a, git_ops.git_init)
    on(a, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    assert _sync_as(a, db_a, "A first")["pushed"]
    b = tmp_path / "B"
    assert on(b, git_ops.git_clone, str(remote))["success"]
    db_b = _db_with(b, {"Alpha": "todo from B", "Only B": "b's own"})
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    r = _sync_as(b, db_b, "B first")
    assert r["success"] and r["pushed"], r
    assert (b / "export" / "Only-A-2.md").exists()          # A's export of a project B lacks
    Database(db_a).add_todo(1, "second todo from A", None, "medium")
    r = _sync_as(a, db_a, "A again")                        # both wrote export/Alpha-1.md
    assert r["success"] and r["pushed"], r
    assert "second todo from A" in (a / "export" / "Alpha-1.md").read_text()   # A's copy won
    assert (a / "export" / "Only-B-2.md").exists() and (a / "export" / "Only-A-2.md").exists()
    r = _sync_as(b, db_b, "B again")
    assert r["success"] and r["pushed"], r
    assert "todo from B" in (b / "export" / "Alpha-1.md").read_text()
    assert set(_tip_files(remote)) >= {"export/Only-A-2.md", "export/Only-B-2.md", "export/Alpha-1.md"}
    r = _sync_as(a, db_a, "A third")                        # no clash: A's copy again
    assert r["success"] and "second todo from A" in (a / "export" / "Alpha-1.md").read_text()
    for m in (a, b):
        assert not (m / ".git" / "rebase-merge").exists()
    assert (a / "flowstate.db").exists() and (b / "flowstate.db").exists()


def test_a_clash_outside_the_export_still_stops(tmp_path, remote):
    a = tmp_path / "A"
    db_a = _db_with(a, {"Alpha": "x"})
    on(a, git_ops.git_init)
    on(a, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    (a / "projects" / "n.md").write_text("one")
    assert _sync_as(a, db_a, "A")["pushed"]
    b = tmp_path / "B"
    on(b, git_ops.git_clone, str(remote))
    db_b = _db_with(b, {"Alpha": "x"})
    on(b, git_ops.git_set_remote, str(remote), allow_unchecked=True)
    (b / "projects" / "n.md").write_text("from B")
    assert _sync_as(b, db_b, "B")["pushed"]
    (a / "projects" / "n.md").write_text("from A")
    r = _sync_as(a, db_a, "A clash")
    assert r["success"] is False and r["conflict"] is True and r["pushed"] is False, r
    assert (a / "projects" / "n.md").read_text() == "from A"
    assert not (a / ".git" / "rebase-merge").exists()


def test_the_export_removes_only_its_own_gone_projects(tmp_path, remote):
    from flowstate import cli_data
    from flowstate.database import Database
    a = tmp_path / "A"
    db = _db_with(a, {"Keep": "k", "Drop": "d"})
    on(a, git_ops.git_init)
    on(a, cli_data._export_all, db, a)
    (a / "export" / "From-elsewhere-7.md").write_text("another computer's")
    Database(db).delete_project(2) if hasattr(Database, "delete_project") else \
        __import__("sqlite3").connect(db).execute("DELETE FROM projects WHERE id = 2").connection.commit()
    on(a, cli_data._export_all, db, a)
    assert sorted(p.name for p in (a / "export").glob("*.md")) == ["From-elsewhere-7.md", "Keep-1.md"]
