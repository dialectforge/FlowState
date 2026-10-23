"""`flowstate update` (selfupdate.py, 2.4.0). Every test builds throwaway git repos in TEMP: a
bare repo plays `origin`, a clone plays the install, and a throwaway ed25519 key made with
ssh-keygen in TEMP signs the test tags. Real git and real ssh-keygen; fakes for the services,
processes, pip, the health checks, migrations and GitHub's answer. Nothing here touches a real
install, a real scheduled task or the network."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from flowstate import backup as B
from flowstate import paths, selfupdate as SU, updates

SSH_KEYGEN = shutil.which("ssh-keygen")
pytestmark = pytest.mark.skipif(not SSH_KEYGEN or not shutil.which("git"), reason="needs git and ssh-keygen")

DEPS = '["mcp>=1.26,<2", "pydantic>=2.0.0"]'


def _env(tmp: Path) -> dict:
    cfg = tmp / "gitconfig"
    if not cfg.exists():
        cfg.write_text("[user]\n\tname = FlowState Tests\n\temail = tests@example.invalid\n"
                       "[init]\n\tdefaultBranch = main\n[tag]\n\tgpgSign = false\n[commit]\n\tgpgSign = false\n",
                       encoding="utf-8")
    return dict(os.environ, GIT_CONFIG_GLOBAL=str(cfg), GIT_CONFIG_NOSYSTEM="1")


def git(cwd, *args, tmp=None, check=True) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=_env(Path(tmp)))
    if check and r.returncode != 0:
        raise AssertionError(f"git {args}: {r.stderr}")
    return r.stdout.strip()


def keygen(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", path.name, "-f", str(path)],
                   check=True, capture_output=True)
    return path.with_suffix(".pub").read_text(encoding="utf-8").strip()


def signer_line(principal: str, pub: str) -> str:
    return f'{principal} namespaces="git" {pub}\n'


class World:
    """origin (bare), seed (where releases are made), install (the user's clone)."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin, self.seed, self.install = tmp / "origin.git", tmp / "seed", tmp / "install"
        self.key = tmp / "keys" / "release"
        self.other = tmp / "keys" / "other"
        self.pub, self.other_pub = keygen(self.key), keygen(self.other)
        git(tmp, "init", "-q", "--bare", "-b", "main", str(self.origin), tmp=tmp)
        git(tmp, "init", "-q", "-b", "main", str(self.seed), tmp=tmp)
        self.write("2.4.0", signers=signer_line("release@flowstate.test", self.pub))
        (self.seed / ".gitignore").write_text(".venv/\n.venv.pre-update/\n", encoding="utf-8")
        (self.seed / "setup_flowstate.py").write_text("# test stand-in\n", encoding="utf-8")
        self.commit("FlowState 2.4.0")
        self.tag("v2.4.0", "good")
        git(self.seed, "remote", "add", "origin", self.origin.as_posix(), tmp=tmp)
        git(self.seed, "push", "-q", "origin", "main", "v2.4.0", tmp=tmp)
        git(tmp, "clone", "-q", self.origin.as_posix(), str(self.install), tmp=tmp)
        (self.install / ".venv").mkdir()
        (self.install / ".venv" / "marker.txt").write_text("old venv", encoding="utf-8")

    def write(self, version, signers=None, deps=DEPS, init_version=None):
        fs = self.seed / "mcp-server" / "flowstate"
        fs.mkdir(parents=True, exist_ok=True)
        (fs / "__init__.py").write_text(f'__version__ = "{init_version or version}"\n', encoding="utf-8")
        (self.seed / "mcp-server" / "pyproject.toml").write_text(
            f'[project]\nname = "flowstate"\nversion = "{version}"\ndependencies = {deps}\n', encoding="utf-8")
        if signers is not None:
            (fs / "release_signers").write_text("# test signers\n" + signers, encoding="utf-8")

    def commit(self, msg):
        git(self.seed, "add", "-A", tmp=self.tmp)
        git(self.seed, "commit", "-q", "-m", msg, tmp=self.tmp)

    def tag(self, name, how):
        if how == "good" or how == "other":
            key = self.key if how == "good" else self.other
            git(self.seed, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key.as_posix()}",
                "-c", f"gpg.ssh.program={Path(SSH_KEYGEN).as_posix()}", "tag", "-s", name, "-m", name, tmp=self.tmp)
        elif how == "annotated":
            git(self.seed, "tag", "-a", name, "-m", name, tmp=self.tmp)
        else:
            git(self.seed, "tag", name, tmp=self.tmp)

    def release(self, version, how="good", **kw):
        self.write(version, **kw)
        self.commit(f"FlowState {version}")
        self.tag(f"v{version}", how)
        git(self.seed, "push", "-q", "origin", "main", f"v{version}", tmp=self.tmp)

    def head(self):
        return git(self.install, "rev-parse", "HEAD", tmp=self.tmp)

    def branch(self):
        return git(self.install, "symbolic-ref", "--quiet", "--short", "HEAD", tmp=self.tmp, check=False) or None


def github(version, ok=True):
    def fetch(url, headers, timeout):
        if not ok:
            raise OSError("offline")
        return 200, {}, json.dumps({"tag_name": f"v{version}", "draft": False, "prerelease": False,
                                    "html_url": f"https://github.com/dialectforge/FlowState/releases/tag/v{version}"}).encode()
    return fetch


class FakeServices:
    def __init__(self, found=(), fail_stop=False, fail_start=0):
        self.found, self.calls = list(found), []
        self.fail_stop, self.fail_start = fail_stop, fail_start

    def find(self, folder):
        self.calls.append(("find", str(folder)))
        return [s for s in self.found if SU.mentions(s.text, folder)]

    def stop(self, s):
        self.calls.append(("stop", s.name))
        if self.fail_stop:
            raise SU.StepFailed("stopping services", f"{s.name}: access denied")

    def start(self, s):
        self.calls.append(("start", s.name))
        if self.fail_start:
            self.fail_start -= 1
            raise SU.StepFailed("starting services", f"{s.name}: the task did not start")


class FakeProcesses:
    def __init__(self, procs=(), sticky=()):
        self.procs, self.killed, self.sticky = list(procs), [], set(sticky)

    def list(self):
        return list(self.procs)

    def kill(self, pid):
        self.killed.append(pid)
        if pid not in self.sticky:
            self.procs = [p for p in self.procs if p["pid"] != pid]


class FakePip:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create_venv(self, venv):
        self.calls.append(("venv", str(venv)))
        venv.mkdir()
        (venv / "marker.txt").write_text("new venv", encoding="utf-8")

    def install(self, py, folder):
        self.calls.append(("install", str(folder)))
        if self.fail:
            raise SU.StepFailed("installing packages", "pip: no matching distribution")


class FakeClock:
    def __init__(self):
        import time
        self.t = time.time()

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeHealth:
    """Reads the version the folder's code really has; can fail the first N checks."""

    def __init__(self, fail=0):
        self.fail, self.calls = fail, []

    def check(self, py, folder, db, version, services):
        text = (Path(folder) / "mcp-server" / "flowstate" / "__init__.py").read_text(encoding="utf-8")
        self.calls.append((version, text.strip()))
        if self.fail:
            self.fail -= 1
            return ["start_here over stdio failed (no reply from the server)"]
        return [] if f'"{version}"' in text else [f"the code does not report {version}"]


class FakeMigrator:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def migrate(self, py, folder, db):
        self.calls.append(str(db))
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE IF NOT EXISTS t999_new_feature (id INTEGER PRIMARY KEY)")
        conn.commit()
        conn.close()
        if self.fail:
            raise SU.StepFailed("migrating the database", "migration 999 failed")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    from flowstate.database import Database
    w.db = tmp_path / "data" / "flowstate.db"
    w.db.parent.mkdir()
    d = Database(w.db)
    d.create_project("Alpha", "first")
    w.state = tmp_path / "data" / "update.json"
    w.out = []
    yield w
    shutil.rmtree(w.key.parent, ignore_errors=True)      # the throwaway signing keys go with the test
    assert not w.key.exists()


TASK_HTTP = "cmd /c {f}\\scripts\\windows\\start-flowstate-http.cmd --host 127.0.0.1 --port 8766"
TASK_RELAY = "cmd /c {f}\\scripts\\windows\\start-flowstate-relay.cmd"


def make(w: World, version="2.5.0", fetch=None, **kw) -> SU.Updater:
    services = kw.pop("services", None) or FakeServices([
        SU.Service("\\FlowState HTTP Server", "task", TASK_HTTP.format(f=w.install), True),
        SU.Service("\\FlowState Relay Agent", "task", TASK_RELAY.format(f=w.install), True),
        SU.Service("\\FlowState Nightly Backup", "task", f"{w.install}\\.venv\\Scripts\\python.exe -m flowstate backup", False),
        SU.Service("\\Other FlowState", "task", TASK_HTTP.format(f=str(w.install) + "-dev"), True)])
    args = dict(folder=w.install, db=w.db, services=services, processes=FakeProcesses(), pip=FakePip(),
                clock=FakeClock(), health=FakeHealth(), migrator=FakeMigrator(), fetch=fetch or github(version),
                expected_origin=(w.origin.as_posix(),), running_version="2.4.0", state_path=w.state,
                ssh_keygen=SSH_KEYGEN, say=w.out.append)
    args.update(kw)
    return SU.Updater(**args)


def text(w):
    return "\n".join(w.out)


def snapshot(w: World) -> dict:
    return {"head": w.head(), "branch": w.branch(), "status": git(w.install, "status", "--porcelain", tmp=w.tmp),
            "tags": git(w.install, "tag", "--list", tmp=w.tmp),
            "refs": git(w.install, "for-each-ref", "--format=%(refname)", tmp=w.tmp),
            "venv": sorted(p.name for p in (w.install / ".venv").iterdir()) if (w.install / ".venv").is_dir() else None,
            "marker": (w.install / ".venv" / "marker.txt").read_text(encoding="utf-8")
            if (w.install / ".venv" / "marker.txt").exists() else None,
            "db": B.table_counts(w.db)}


def backups():
    return sorted(paths.backup_dir().glob("flowstate-*-pre-update*.db")) if paths.backup_dir().exists() else []


def assert_nothing_changed(w, before, up):
    assert snapshot(w) == before
    assert backups() == []
    assert [c for c in up.services.calls if c[0] != "find"] == []
    assert "last_update" not in updates.read_state(w.state)


# ---------------------------------------------------------------- happy paths
def test_happy_path_code_only(world):
    w = world
    old = w.head()
    w.release("2.5.0")
    up = make(w)
    assert up.run(yes=True, user_said="please update FlowState", app="Claude Code") == 0, text(w)
    assert w.head() == git(w.seed, "rev-parse", "HEAD", tmp=w.tmp) != old
    assert w.branch() == "main"                       # main: fast-forward
    assert (w.install / ".venv" / "marker.txt").read_text() == "old venv" and up.pip.calls == []
    (bk,) = backups()
    assert B.integrity(bk) == "ok" and B.is_safety_copy(bk)
    assert up.migrator.calls == [str(w.db)]
    calls = [c for c in up.services.calls if c[0] != "find"]
    assert calls == [("stop", "\\FlowState HTTP Server"), ("stop", "\\FlowState Relay Agent"),
                     ("start", "\\FlowState HTTP Server"), ("start", "\\FlowState Relay Agent")]
    last = updates.read_state(w.state)["last_update"]
    assert last["result"] == "ok" and last["from"] == "2.4.0" and last["to"] == "2.5.0"
    assert last["from_sha"] == old and last["to_sha"] == w.head() and last["user_said"] == "please update FlowState"
    conn = sqlite3.connect(w.db)
    row = conn.execute("SELECT tool, client, old_row, new_row FROM record_history "
                       "WHERE table_name = 'flowstate_install'").fetchone()
    conn.close()
    assert row[0] == "update 2.4.0 -> 2.5.0: ok" and row[1] == "Claude Code"
    assert json.loads(row[3])["user_said"] == "please update FlowState" and json.loads(row[2])["version"] == "2.4.0"
    assert "Quit and reopen your AI apps" in text(w)
    assert not w.state.with_name("update.lock").exists()


def test_happy_path_from_a_detached_release_tag(world):
    w = world
    git(w.install, "checkout", "-q", "--detach", "v2.4.0", tmp=w.tmp)
    w.release("2.5.0")
    up = make(w)
    assert up.run(yes=True, user_said="update please") == 0, text(w)
    assert w.branch() is None and git(w.install, "describe", "--exact-match", "--tags", tmp=w.tmp) == "v2.5.0"


def test_happy_path_with_a_dependency_change(world):
    w = world
    w.release("2.5.0", deps='["mcp>=1.30,<2", "pydantic>=2.0.0"]')
    up = make(w)
    assert up.run(yes=True, user_said="update please") == 0, text(w)
    assert [c[0] for c in up.pip.calls] == ["venv", "install"]
    assert (w.install / ".venv" / "marker.txt").read_text() == "new venv"
    assert (w.install / ".venv.pre-update" / "marker.txt").read_text() == "old venv"
    assert updates.read_state(w.state)["last_update"]["venv_swapped"] is True


def test_dry_run_changes_nothing(world):
    w = world
    w.release("2.5.0")
    before = snapshot(w)
    up = make(w)
    assert up.run(dry_run=True) == 0, text(w)
    out = text(w)
    assert "dry run: nothing was changed" in out and "signed by release@flowstate.test" in out
    assert "\\FlowState HTTP Server (stopped, then started again)" in out
    assert "Not running, left alone: \\FlowState Nightly Backup" in out
    assert "Other FlowState" not in out
    assert_nothing_changed(w, before, up)


def test_already_up_to_date(world):
    w = world
    up = make(w, version="2.4.0")
    assert up.run(yes=True, user_said="x") == 0 and "up to date" in text(w)


# ---------------------------------------------------------------- refusals (nothing changes)
def _refused(w, up, *words, before=None):
    rc = up.run(yes=True, user_said="please update")
    out = text(w)
    assert rc == 1, out
    for word in words:
        assert word in out, (word, out)
    assert "Nothing was changed" in out and "To update by hand" in out
    if before is not None:
        assert_nothing_changed(w, before, up)
    return out


def test_refuses_a_dirty_tree(world):
    w = world
    w.release("2.5.0")
    (w.install / "notes.txt").write_text("mine", encoding="utf-8")
    _refused(w, make(w), "local changes")


def test_refuses_another_branch(world):
    w = world
    w.release("2.5.0")
    git(w.install, "checkout", "-q", "-b", "feature", tmp=w.tmp)
    git(w.install, "commit", "-q", "--allow-empty", "-m", "x", tmp=w.tmp)
    _refused(w, make(w), "on branch feature", "developer setup", before=snapshot(w))


def test_refuses_local_commits_on_main(world):
    w = world
    w.release("2.5.0")
    git(w.install, "commit", "-q", "--allow-empty", "-m", "mine", tmp=w.tmp)
    _refused(w, make(w), "1 local commit(s)", before=snapshot(w))


def test_refuses_an_extra_worktree(world):
    w = world
    w.release("2.5.0")
    git(w.install, "worktree", "add", "-q", str(w.tmp / "wt"), "-b", "wt", tmp=w.tmp)
    _refused(w, make(w), "2 worktrees", before=snapshot(w))


def _link(target: Path, link: Path):
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_refuses_a_junction_venv(world):
    w = world
    w.release("2.5.0")
    real = w.tmp / "elsewhere-venv"
    real.mkdir()
    shutil.rmtree(w.install / ".venv")
    _link(real, w.install / ".venv")
    assert not SU.is_real_dir(w.install / ".venv") and SU.is_real_dir(real)
    _refused(w, make(w), "link or junction")
    assert list(real.iterdir()) == []


def test_refuses_a_missing_venv(world):
    w = world
    w.release("2.5.0")
    shutil.rmtree(w.install / ".venv")
    _refused(w, make(w), "no .venv folder")


def test_refuses_the_wrong_origin(world):
    w = world
    w.release("2.5.0")
    up = make(w, expected_origin=SU.ORIGIN_URLS)
    _refused(w, up, "not https://github.com/dialectforge/FlowState", before=snapshot(w))


def test_refuses_when_opted_out(world):
    w = world
    w.release("2.5.0")
    paths.save_config({"self_update": False})
    _refused(w, make(w), '"self_update": false', before=snapshot(w))
    paths.save_config({})
    (w.install / SU.OPT_OUT_FILE).write_text("", encoding="utf-8")
    git(w.install, "add", "-f", SU.OPT_OUT_FILE, tmp=w.tmp)
    git(w.install, "commit", "-q", "-m", "opt out", tmp=w.tmp)
    _refused(w, make(w), SU.OPT_OUT_FILE)


def test_refuses_with_no_key_in_release_signers(world):
    w = world
    w.seed_signers = None
    (w.seed / "mcp-server" / "flowstate" / "release_signers").write_text("# nobody yet\n\n", encoding="utf-8")
    w.commit("no keys")
    git(w.seed, "push", "-q", "origin", "main", tmp=w.tmp)
    git(w.install, "pull", "-q", "--ff-only", tmp=w.tmp)
    w.release("2.5.0")
    before = snapshot(w)
    out = _refused(w, make(w), "FlowState can't verify release v2.5.0: no release signing key is set up yet",
                   "Release tags must be signed", before=before)
    assert "Not updated" not in out


@pytest.mark.parametrize("how", ["annotated", "lightweight"])
def test_refuses_an_unsigned_tag(world, how):
    w = world
    w.release("2.5.0", how=how)
    before = snapshot(w)
    _refused(w, make(w), "FlowState can't verify release v2.5.0: its tag isn't signed with a "
                         "FlowState release key", "Release tags must be signed", before=before)


def test_refuses_a_tag_signed_by_another_key(world):
    w = world
    w.release("2.5.0", how="other")
    _refused(w, make(w), "its tag isn't signed with a FlowState release key", "doesn't list",
             before=snapshot(w))


def test_signers_are_read_from_the_installed_copy_not_the_release(world):
    """A release that lists its own key and is signed by it is still refused."""
    w = world
    w.release("2.5.0", how="other", signers=signer_line("evil@example.invalid", w.other_pub))
    _refused(w, make(w), "its tag isn't signed with a FlowState release key", before=snapshot(w))


def test_refuses_a_version_mismatch_inside_the_tag(world):
    w = world
    w.release("2.5.0", init_version="2.4.9")
    _refused(w, make(w), "does not say version 2.5.0", before=snapshot(w))


def test_refuses_when_a_different_local_tag_has_the_release_name(world):
    w = world
    w.release("2.5.0")
    git(w.install, "tag", "v2.5.0", "HEAD", tmp=w.tmp)      # someone's own tag, not GitHub's
    _refused(w, make(w), "already has a different tag v2.5.0", before=snapshot(w))


def test_refuses_a_downgrade(world):
    w = world
    up = make(w, version="2.3.0")
    assert up.run(yes=True, user_said="x") == 0 and "up to date" in text(w)
    assert backups() == [] and up.services.calls == []


def test_refuses_when_offline(world):
    w = world
    w.release("2.5.0")
    _refused(w, make(w, fetch=github("2.5.0", ok=False)), "can't reach GitHub", before=snapshot(w))


def test_refuses_while_another_update_runs(world):
    w = world
    w.release("2.5.0")
    w.state.parent.mkdir(parents=True, exist_ok=True)
    w.state.with_name("update.lock").write_text("{}", encoding="utf-8")
    _refused(w, make(w), "another update is running", before=snapshot(w))


def test_yes_needs_the_users_words(world):
    w = world
    w.release("2.5.0")
    before = snapshot(w)
    up = make(w)
    for said in (None, "", "   "):
        assert up.run(yes=True, user_said=said) == 1
    assert "--yes needs --user-said" in text(w)
    assert_nothing_changed(w, before, up)


def test_without_yes_and_no_terminal_it_refuses(world, monkeypatch):
    w = world
    w.release("2.5.0")
    from flowstate import cli_review
    monkeypatch.setattr(cli_review, "open_terminal", lambda: None)
    before = snapshot(w)
    up = make(w)
    assert up.run() == 1 and "piped input doesn't count" in text(w)
    assert_nothing_changed(w, before, up)


def test_without_yes_a_terminal_answer_is_needed(world, monkeypatch):
    w = world
    w.release("2.5.0")
    from flowstate import cli_review

    class Tty:
        def __init__(self, ans):
            self.ans = ans

        def ask(self, q):
            return self.ans

        def close(self):
            pass
    monkeypatch.setattr(cli_review, "open_terminal", lambda: Tty("n"))
    assert make(w).run() == 1 and "you didn't confirm" in text(w)
    monkeypatch.setattr(cli_review, "open_terminal", lambda: Tty("y"))
    assert make(w).run() == 0
    assert "own terminal" in updates.read_state(w.state)["last_update"]["user_said"]


def test_refuses_without_ssh_keygen_or_with_old_git(world):
    w = world
    w.release("2.5.0")
    _refused(w, make(w, ssh_keygen=""), "ssh-keygen was not found", before=snapshot(w))

    class OldGit(SU.Git):
        def version(self):
            return (2, 30)
    _refused(w, make(w, git=OldGit(w.install)), "git 2.30 is too old")


def test_refuses_dev_host_live_layout(world):
    """The host PC: a release/2.4-trust branch with 58 commits past the last tag, a linked dev
    worktree, and a .venv junction. Refused on every count; nothing is touched."""
    w = world
    w.release("2.5.0")
    git(w.install, "checkout", "-q", "-b", "release/2.4-trust", tmp=w.tmp)
    for i in range(3):
        git(w.install, "commit", "-q", "--allow-empty", "-m", f"local {i}", tmp=w.tmp)
    git(w.install, "worktree", "add", "-q", str(w.tmp / "install-dev"), "-b", "dev/2.4-review-fixes", tmp=w.tmp)
    _link(w.install / ".venv", w.tmp / "install-dev" / ".venv")
    before = snapshot(w)
    out = _refused(w, make(w), "This is a developer setup; update it by hand", "2 worktrees",
                   "on branch release/2.4-trust", before=before)
    # and from the dev worktree: its .venv is a junction, and it is a linked worktree
    w.out.clear()
    dev = make(w, folder=w.tmp / "install-dev")
    assert dev.run(yes=True, user_said="x") == 1
    out = text(w)
    assert "link or junction" in out and "2 worktrees" in out and "developer setup" in out
    assert (w.install / ".venv" / "marker.txt").read_text() == "old venv"


# ---------------------------------------------------------------- failures roll back
def _rolled_back(w, up, before_head, before_branch, words):
    rc = up.run(yes=True, user_said="please update")
    out = text(w)
    assert rc == 1, out
    assert "Rolled back: FlowState 2.4.0 runs again" in out, out
    for word in words:
        assert word in out, (word, out)
    assert w.head() == before_head and w.branch() == before_branch
    assert git(w.install, "status", "--porcelain", tmp=w.tmp) == ""
    assert (w.install / ".venv" / "marker.txt").read_text() == "old venv"
    assert not (w.install / ".venv.pre-update").exists()
    (bk,) = backups()
    assert B.integrity(bk) == "ok" and B.integrity(w.db) == "ok"
    last = updates.read_state(w.state)["last_update"]
    assert last["result"] == "rolled_back"
    return last


def _db_same_except_additive(w, counts_before):
    now = B.table_counts(w.db)
    for t, n in counts_before.items():
        if t != "record_history":
            assert now[t] == n, t
    assert set(now) - set(counts_before) <= {"t999_new_feature"}
    conn = sqlite3.connect(w.db)
    tools = [r[0] for r in conn.execute("SELECT tool FROM record_history WHERE table_name='flowstate_install'")]
    conn.close()
    assert tools == ["update 2.4.0 -> 2.5.0: rolled back"]


@pytest.mark.parametrize("case, kw, words", [
    ("stop", {"services": "fail_stop"}, ["while stopping services", "access denied"]),
    ("start", {"services": "fail_start"}, ["while starting services", "did not start"]),
    ("migrate", {"migrator": FakeMigrator(fail=True)}, ["while migrating the database", "migration 999"]),
    ("health", {"health": FakeHealth(fail=1)}, ["while checking the new version", "start_here over stdio failed"]),
])
def test_a_failure_rolls_back(world, case, kw, words):
    w = world
    w.release("2.5.0")
    head, branch, counts = w.head(), w.branch(), B.table_counts(w.db)
    found = [SU.Service("\\FlowState HTTP Server", "task", TASK_HTTP.format(f=w.install), True)]
    if kw.get("services") == "fail_stop":
        kw = {"services": FakeServices(found, fail_stop=True)}
    elif kw.get("services") == "fail_start":
        kw = {"services": FakeServices(found, fail_start=1)}
    else:
        kw = {k: (type(v)(fail=True) if case == "migrate" else type(v)(fail=1)) for k, v in kw.items()}
    up = make(w, **kw)
    _rolled_back(w, up, head, branch, words)
    _db_same_except_additive(w, counts)
    if case == "health":   # the old version was checked after the rollback
        assert [c[0] for c in up.health.calls] == ["2.5.0", "2.4.0"]


def test_a_failed_git_swap_rolls_back(world, monkeypatch):
    w = world
    w.release("2.5.0")
    head, branch = w.head(), w.branch()
    real = SU.Git.run

    def run(self, *args, **kw):
        if args[:1] == ("merge",):
            return subprocess.CompletedProcess(args, 1, "", "error: Your local changes would be overwritten")
        return real(self, *args, **kw)
    monkeypatch.setattr(SU.Git, "run", run)
    _rolled_back(w, make(w), head, branch, ["while moving the code"])


def test_windows_lock_on_venv_rename_rolls_back(world):
    w = world
    w.release("2.5.0", deps='["mcp>=1.30,<2"]')
    head, branch = w.head(), w.branch()

    def locked(src, dst):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    up = make(w, rename=locked)
    _rolled_back(w, up, head, branch, ["being used by another process"])
    assert up.pip.calls == []


def test_pip_failure_puts_the_old_venv_back(world):
    w = world
    w.release("2.5.0", deps='["mcp>=1.30,<2"]')
    head, branch = w.head(), w.branch()
    up = make(w, pip=FakePip(fail=True))
    _rolled_back(w, up, head, branch, ["while installing packages", "no matching distribution"])


def test_dependency_change_from_inside_the_venv_on_windows_is_refused_up_front(world, monkeypatch):
    """On Windows the venv's python.exe launcher holds the .venv while the update runs from it,
    so a release that changes the packages is refused before anything changes."""
    w = world
    w.release("2.5.0", deps='["mcp>=1.30,<2"]')
    monkeypatch.setattr(SU.Updater, "_runs_from_this_venv", lambda self: True)
    _refused(w, make(w), "changes FlowState's Python packages", "same .venv", before=snapshot(w))
    w.out.clear()
    assert make(w).run(dry_run=True) == 1 and "changes FlowState's Python packages" in text(w)


@pytest.mark.skipif(os.name != "nt", reason="the Windows venv launcher")
def test_runs_from_this_venv_on_windows(tmp_path):
    here = SU.Updater(folder=Path(sys.prefix).parent, db=tmp_path / "x.db", say=lambda m: None)
    assert here.venv.name == ".venv" or not here._runs_from_this_venv()
    if Path(sys.prefix).name == ".venv":
        assert here._runs_from_this_venv() is True
    other = SU.Updater(folder=tmp_path, db=tmp_path / "x.db", say=lambda m: None)
    assert other._runs_from_this_venv() is False


def test_dependency_change_needs_every_app_quit(world):
    w = world
    w.release("2.5.0", deps='["mcp>=1.30,<2"]')
    head, branch = w.head(), w.branch()
    py = w.install / ".venv" / "Scripts" / "python.exe"
    app = {"pid": 4242, "exe": str(py), "cmd": f'"{py}" -m flowstate'}
    up = make(w, processes=FakeProcesses([app]))
    _rolled_back(w, up, head, branch, ["must be quit first", "pid 4242"])
    assert up.processes.killed == []


# ---------------------------------------------------------------- processes
def test_leftover_service_processes_are_stopped_and_apps_left_alone(world):
    w = world
    w.release("2.5.0")
    py = w.install / ".venv" / "Scripts" / "python.exe"
    other = Path(str(w.install) + "-dev") / ".venv" / "Scripts" / "python.exe"
    procs = [{"pid": 11, "exe": str(py), "cmd": f'"{py}" -m flowstate relay run --db x.db'},
             {"pid": 12, "exe": str(py), "cmd": f'"{py}" -m flowstate'},
             {"pid": 13, "exe": str(other), "cmd": f'"{other}" -m flowstate --transport http'}]
    up = make(w, processes=FakeProcesses(procs))
    assert up.run(yes=True, user_said="update please") == 0, text(w)
    assert up.processes.killed == [11]


def test_a_leftover_that_wont_stop_rolls_back(world):
    w = world
    w.release("2.5.0")
    head, branch = w.head(), w.branch()
    py = w.install / ".venv" / "Scripts" / "python.exe"
    procs = [{"pid": 21, "exe": str(py), "cmd": f'"{py}" -m flowstate --transport http --port 8766'}]
    up = make(w, processes=FakeProcesses(procs, sticky={21}))
    _rolled_back(w, up, head, branch, ["still running: 21"])


# ---------------------------------------------------------------- service discovery
def test_mentions_matches_only_this_folder():
    live, dev = Path(r"C:\FlowState\FlowState-2.4"), Path(r"C:\FlowState\FlowState-2.4-dev")
    t_live = r"cmd /c C:\FlowState\FlowState-2.4\scripts\windows\start-flowstate-http.cmd --port 8766"
    t_dev = r"cmd /c C:\FlowState\FlowState-2.4-dev\scripts\windows\start-flowstate-http.cmd"
    if os.name == "nt":
        assert SU.mentions(t_live, live) and not SU.mentions(t_live, dev)
        assert SU.mentions(t_dev, dev) and not SU.mentions(t_dev, live)
        assert SU.mentions(t_live.lower(), live)
    assert SU.mentions("/home/u/FlowState/.venv/bin/python -m flowstate relay run", Path("/home/u/FlowState"))
    assert not SU.mentions("/home/u/FlowState2/.venv/bin/python", Path("/home/u/FlowState"))
    assert not SU.mentions("/home/u/FlowState.old/x", Path("/home/u/FlowState"))
    assert SU.mentions('"/home/u/FlowState"', Path("/home/u/FlowState"))


class FakeRun:
    """Stands in for subprocess.run inside Services: never runs schtasks/launchctl/systemctl."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        for key, (rc, out) in self.answers.items():
            if key in " ".join(args):
                return subprocess.CompletedProcess(args, rc, out, "")
        return subprocess.CompletedProcess(args, 0, "", "")


@pytest.mark.skipif(os.name != "nt", reason="Windows paths")
def test_windows_task_discovery_matches_only_this_folder():
    live, dev = Path(r"C:\FlowState\FlowState-2.4"), Path(r"C:\FlowState\FlowState-2.4-dev")
    tasks = [{"n": "\\FlowState HTTP Server", "s": "Running",
              "a": r"cmd /c C:\FlowState\FlowState-2.4\scripts\windows\start-flowstate-http.cmd --host 192.0.2.10 --port 8766 "},
             {"n": "\\FlowState Relay Agent", "s": "Running",
              "a": r"cmd /c C:\FlowState\FlowState-2.4\scripts\windows\start-flowstate-relay.cmd --db x "},
             {"n": "\\FlowState Nightly Backup", "s": "Ready",
              "a": r"C:\FlowState\FlowState-2.4\scripts\windows\backup.cmd "},
             {"n": "\\FlowState Public Listener", "s": "Disabled", "a": r"C:\FlowState\FlowState-v2\x.cmd "},
             {"n": "\\Dev HTTP", "s": "Running", "a": r"C:\FlowState\FlowState-2.4-dev\.venv\Scripts\python.exe -m flowstate --transport http "}]
    run = FakeRun({"Get-ScheduledTask": (0, json.dumps(tasks))})
    sv = SU.Services(system="Windows", run=run)
    got = sv.find(live)
    assert [(s.name, s.running, s.role) for s in got] == [
        ("\\FlowState HTTP Server", True, "http"), ("\\FlowState Relay Agent", True, "relay"),
        ("\\FlowState Nightly Backup", False, "backup")]
    assert [s.name for s in sv.find(dev)] == ["\\Dev HTTP"]
    sv.stop(got[0])
    sv.start(got[0])
    assert run.calls[-2:] == [["schtasks", "/End", "/TN", "\\FlowState HTTP Server"],
                              ["schtasks", "/Run", "/TN", "\\FlowState HTTP Server"]]
    assert all(c[0] in ("powershell", "schtasks") for c in run.calls)


def test_macos_and_linux_discovery(tmp_path):
    home = tmp_path / "home"
    la = home / "Library" / "LaunchAgents"
    la.mkdir(parents=True)
    folder, dev = tmp_path / "FlowState", tmp_path / "FlowState-dev"
    (la / "com.flowstatemcp.relay.plist").write_text(
        f"<plist><dict><key>Label</key><string>com.flowstatemcp.relay</string><array><string>{folder}/.venv/bin/python"
        "</string><string>-m</string><string>flowstate</string><string>relay</string><string>run</string>"
        "</array></dict></plist>", encoding="utf-8")
    (la / "com.other.plist").write_text(f"<plist><string>{dev}/.venv/bin/python</string></plist>", encoding="utf-8")
    run = FakeRun({"launchctl print": (0, "")})
    got = SU.Services(system="Darwin", run=run, home=home).find(folder)
    assert [(Path(s.name).name, s.role, s.running) for s in got] == [("com.flowstatemcp.relay.plist", "relay", True)]
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "flowstate-relay.service").write_text(f"[Service]\nExecStart={folder}/.venv/bin/python -m flowstate relay run\n",
                                                   encoding="utf-8")
    (units / "flowstate-backup.service").write_text(f"[Service]\nExecStart={folder}/.venv/bin/python -m flowstate backup\n",
                                                    encoding="utf-8")
    (units / "other.service").write_text(f"[Service]\nExecStart={dev}/.venv/bin/python\n", encoding="utf-8")
    run = FakeRun({"is-active flowstate-relay.service": (0, "active\n"), "is-active": (3, "inactive\n")})
    sv = SU.Services(system="Linux", run=run, home=home)
    got = sv.find(folder)
    assert [(s.name, s.running) for s in got] == [("flowstate-backup.service", False), ("flowstate-relay.service", True)]
    sv.stop(got[1])
    assert run.calls[-1] == ["systemctl", "--user", "stop", "flowstate-relay.service"]


# ---------------------------------------------------------------- --rollback, CLI, brief
def test_rollback_goes_back_to_the_exact_old_commit_and_branch(world):
    w = world
    old_head, old_branch = w.head(), w.branch()
    w.release("2.5.0", deps='["mcp>=1.30,<2"]')
    assert make(w).run(yes=True, user_said="update please") == 0, text(w)
    assert w.head() != old_head
    up = make(w, running_version="2.5.0")
    assert up.rollback(yes=True, user_said="go back please") == 0, text(w)
    assert w.head() == old_head and w.branch() == old_branch
    assert (w.install / ".venv" / "marker.txt").read_text() == "old venv"
    last = updates.read_state(w.state)["last_update"]
    assert last["result"] == "rolled_back_by_user" and last["rollback_result"] == "ok"
    assert up.rollback(yes=True, user_said="again") == 1
    assert "no successful update to go back from" in text(w)


def test_cli_options():
    from flowstate import cli
    a = cli.build_parser().parse_args(["update", "--dry-run"])
    assert a.dry_run and a.action is None
    a = cli.build_parser().parse_args(["update", "--yes", "--user-said", "update it", "--app", "Claude Code"])
    assert a.yes and a.user_said == "update it" and a.app == "Claude Code" and not a.rollback
    a = cli.build_parser().parse_args(["update", "check", "now"])
    assert a.action == "check" and a.sub == "now"


def test_the_real_install_folder_is_refused_from_tests():
    """The updater pointed at this repository (a dev worktree with a junction .venv here)
    refuses before anything; nothing is fetched, backed up or stopped."""
    out = []
    up = SU.Updater(db=Path(os.environ["FLOWSTATE_DB"]), services=FakeServices(), processes=FakeProcesses(),
                    pip=FakePip(), clock=FakeClock(), health=FakeHealth(), migrator=FakeMigrator(),
                    fetch=github("9.9.9"), say=out.append, state_path=Path(os.environ["FLOWSTATE_DB"]).parent / "u.json")
    assert up.run(dry_run=True) == 1
    assert "Nothing was changed" in "\n".join(out)


def test_brief_says_run_flowstate_update_now_and_restart_after(tmp_path, monkeypatch):
    p = tmp_path / "update.json"
    monkeypatch.setattr(updates, "state_file", lambda: p)
    monkeypatch.delenv("FLOWSTATE_UPDATE_CHECK", raising=False)
    assert updates.has_update_command() is True
    updates.write_state({"latest": "2.5.0", "url": "https://github.com/dialectforge/FlowState/releases/tag/v2.5.0"}, p)
    line = updates.brief_line("2.4.0")
    assert "run flowstate update --dry-run" in line and len(line) < 300
    updates.write_state({"last_update": {"result": "ok", "from": "2.4.0", "to": "2.5.0"}}, p)
    assert updates.brief_line("2.4.0") == ("FlowState was updated to 2.5.0; this app still runs 2.4.0. "
                                          "Ask the user to restart it.")
    assert updates.brief_line("2.5.0") is None
    monkeypatch.setenv("FLOWSTATE_UPDATE_CHECK", "0")       # local news: shown even with the check off
    assert updates.brief_line("2.4.0").startswith("FlowState was updated to 2.5.0")
    updates.write_state({"last_update": {"result": "rolled_back", "to": "2.5.0"}}, p)
    assert updates.brief_line("2.4.0") is None


def test_pre_update_is_a_safety_label():
    assert "pre-update" in B.SAFETY_LABELS
    assert B.is_safety_copy(Path("flowstate-20261022-120000-pre-update.db"))
