"""Local backups, retention, restore, off-site push (local bare repo + fake visibility
check only), and the nightly scheduler (fake runner, fake home)."""

import json
import shutil
import sqlite3
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from conftest import REPO
from flowstate import backup as B
from flowstate import offsite as O
from flowstate import scheduler as SCH


def _count(db, table="todos"):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


# ---------------------------------------------------------------- local backup
def test_backup_is_complete_and_integrity_checked(seeded_db, tmp_path):
    r = B.backup_now(seeded_db, tmp_path / "bk")
    assert r["integrity"] == "ok" and r["path"].exists()
    assert B.table_counts(r["path"]) == B.table_counts(seeded_db)
    assert not list((tmp_path / "bk").glob("*.partial"))


def test_backup_while_another_connection_writes(seeded_db, tmp_path):
    conn = sqlite3.connect(seeded_db)
    conn.execute("BEGIN")
    conn.execute("INSERT INTO todos (project_id, title) VALUES (1, 'uncommitted')")
    r = B.backup_now(seeded_db, tmp_path / "bk")
    conn.rollback()
    conn.close()
    assert r["integrity"] == "ok" and _count(r["path"]) == _count(seeded_db)


def test_corrupt_copy_is_not_kept(seeded_db, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "integrity", lambda p: "page 3 is never used")
    with pytest.raises(B.BackupError, match="integrity"):
        B.backup_now(seeded_db, tmp_path / "bk")
    assert list((tmp_path / "bk").iterdir()) == []


def test_retention_keeps_14_daily_and_8_weekly():
    start = datetime(2026, 10, 16, 2, 30)
    items = []
    for d in range(120):                      # 120 nights, two backups on some nights
        ts = start - timedelta(days=d)
        items.append((ts, Path(f"flowstate-{ts:%Y%m%d-%H%M%S}.db")))
        if d % 10 == 0:
            ts2 = ts - timedelta(hours=1)
            items.append((ts2, Path(f"flowstate-{ts2:%Y%m%d-%H%M%S}.db")))
    keep = B.retention_keep(items)
    kept = sorted((ts for ts, p in items if p in keep), reverse=True)
    days = {t.date() for t in kept}
    assert len(kept) == len(days)                       # one per day at most
    assert all((start - timedelta(days=d)).date() in days for d in range(14))
    weeks = {t.isocalendar()[:2] for t in kept}
    assert len(weeks) == 8                              # the daily span lies inside them
    assert len(kept) <= 14 + 8
    assert kept[0] == start                              # newest of the newest day


def test_prune_only_touches_backup_files(seeded_db, tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    (bk / "notes.txt").write_text("mine", encoding="utf-8")
    (bk / "flowstate.db").write_text("not a backup name", encoding="utf-8")
    now = datetime(2026, 10, 16, 2, 30)
    for d in range(40):
        B.backup_now(seeded_db, bk, prune=False, now=now - timedelta(days=d))
    r = B.backup_now(seeded_db, bk, now=now + timedelta(days=1))
    left = B.list_backups(bk)
    assert len(left) <= 22 and r["removed"]
    assert (bk / "notes.txt").exists() and (bk / "flowstate.db").exists()


def test_cli_backup(seeded_db, tmp_path, capsys):
    from flowstate import cli
    assert cli.main(["backup", "--db", str(seeded_db), "--dir", str(tmp_path / "bk")]) == 0
    assert "Backup OK" in capsys.readouterr().out
    assert cli.main(["backup", "--db", str(tmp_path / "missing.db")]) == 1


# ---------------------------------------------------------------- restore
def test_restore_into_new_file_and_refuse_existing(seeded_db, tmp_path):
    bk = B.backup_now(seeded_db, tmp_path / "bk")["path"]
    new = tmp_path / "restored" / "new.db"
    r = B.restore(bk, new)
    assert r["pre_restore_backup"] is None and B.table_counts(new) == B.table_counts(bk)
    with pytest.raises(B.BackupError, match="already exists"):
        B.restore(bk, seeded_db)


def test_force_restore_takes_pre_restore_backup(seeded_db, tmp_path):
    bk = B.backup_now(seeded_db, tmp_path / "bk")["path"]
    conn = sqlite3.connect(seeded_db)
    conn.execute("INSERT INTO todos (project_id, title) VALUES (1, 'after backup')")
    conn.commit()
    conn.close()
    later = _count(seeded_db)
    r = B.restore(bk, seeded_db, force=True, backup_dir=tmp_path / "bk")
    assert _count(seeded_db) == later - 1
    assert r["pre_restore_backup"] and _count(r["pre_restore_backup"]) == later


def test_restore_refuses_bad_backup(tmp_path):
    bad = tmp_path / "flowstate-20261016-000000.db"
    bad.write_bytes(b"not sqlite" * 50)
    with pytest.raises(Exception):
        B.restore(bad, tmp_path / "x.db")
    assert not (tmp_path / "x.db").exists()


def test_cli_restore_default_is_new_file(seeded_db, tmp_path, capsys):
    from flowstate import cli
    bk = B.backup_now(seeded_db, tmp_path / "bk")["path"]
    assert cli.main(["restore", str(bk)]) == 0
    out = capsys.readouterr().out
    assert "live database was not touched" in out
    assert cli.main(["restore", str(bk), "--to", str(seeded_db)]) == 1


# ---------------------------------------------------------------- off-site
@pytest.fixture
def bare_repo():
    """A local bare git repo inside testdata/ (gitignored); removed afterwards."""
    root = REPO / "testdata" / "tmp" / f"offsite-{uuid.uuid4().hex[:8]}"
    bare = root / "remote.git"
    bare.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    yield root, bare
    shutil.rmtree(root, ignore_errors=True)


def _tree(bare: Path, ref: str) -> set:
    r = subprocess.run(["git", "--git-dir", str(bare), "ls-tree", "-r", "--name-only", ref],
                       capture_output=True, text=True)
    return set(r.stdout.split()) if r.returncode == 0 else set()


def test_offsite_pushes_only_the_allow_list(seeded_db, bare_repo):
    root, bare = bare_repo
    staging = root / "staging"
    # Junk next to the DB and in the staging folder must never be pushed.
    staging.mkdir()
    (staging / "http-token.txt").write_text("fake-" + "k" * 20, encoding="utf-8")
    (staging / ".env").write_text("X=1", encoding="utf-8")
    for i in range(2):
        r = O.push_backup(seeded_db, "someone/private-backups", staging=staging,
                          remote_url=str(bare), checker=lambda repo: "PRIVATE")
        assert r["pushed"] and r["files"] == list(O.ALLOWED)
        conn = sqlite3.connect(seeded_db)  # next night's DB differs from the staged one
        conn.execute("INSERT INTO todos (project_id, title) VALUES (1, ?)", (f"night {i}",))
        conn.commit()
        conn.close()
    assert _tree(bare, "flowstate-backup") == set(O.ALLOWED)
    assert _tree(bare, "main") == set()                       # main untouched
    count = subprocess.run(["git", "--git-dir", str(bare), "rev-list", "--count",
                            "flowstate-backup"], capture_output=True, text=True).stdout.strip()
    assert count == "1"                                        # no pile of old copies
    manifest = json.loads(subprocess.run(
        ["git", "--git-dir", str(bare), "show", "flowstate-backup:manifest.json"],
        capture_output=True, text=True).stdout)
    assert manifest["integrity_check"] == "ok" and manifest["tables"]["projects"] == 2
    assert "token" not in json.dumps(manifest).lower()


@pytest.mark.parametrize("vis", ["PUBLIC", "INTERNAL", None])
def test_offsite_refuses_unless_private(seeded_db, bare_repo, vis):
    root, bare = bare_repo
    with pytest.raises(O.OffsiteError, match="Nothing was pushed"):
        O.push_backup(seeded_db, "someone/repo", staging=root / "s", remote_url=str(bare),
                      checker=lambda repo: vis)
    assert _tree(bare, "flowstate-backup") == set()


def test_offsite_checks_visibility_again_before_push(seeded_db, bare_repo):
    root, bare = bare_repo
    answers = iter(["PRIVATE", "PUBLIC"])                      # flips mid-run
    with pytest.raises(O.OffsiteError):
        O.push_backup(seeded_db, "someone/repo", staging=root / "s", remote_url=str(bare),
                      checker=lambda repo: next(answers))
    assert _tree(bare, "flowstate-backup") == set()


def test_offsite_refuses_main_and_bad_names(seeded_db):
    with pytest.raises(O.OffsiteError):
        O.push_backup(seeded_db, "someone/repo", branch="main", checker=lambda r: "PRIVATE")
    for bad in ("", "noslash", "a/b/c", "../x", "owner/re po"):
        with pytest.raises(O.OffsiteError):
            O.check_repo_name(bad)
    assert O.check_repo_name("https://github.com/owner/name.git") == "owner/name"


def test_offsite_dry_run_pushes_nothing(seeded_db, tmp_path):
    r = O.push_backup(seeded_db, "someone/repo", staging=tmp_path / "s",
                      checker=lambda repo: "PRIVATE", dry_run=True)
    assert r == {"repo": "someone/repo", "visibility": "PRIVATE", "pushed": False, "dry_run": True}
    assert not (tmp_path / "s").exists()


def test_visibility_via_api_uses_git_credential_in_memory():
    seen = {}

    def run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="protocol=https\nhost=github.com\n"
                                           "username=x\npassword=fake-cred-123\n", stderr="")

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"private": True, "visibility": "private"}).encode()

    def opener(req, timeout):
        seen["auth"] = req.headers.get("Authorization")
        seen["url"] = req.full_url
        return Resp()
    assert O.visibility_via_api("owner/name", run=run, opener=opener) == "PRIVATE"
    assert seen["cmd"] == ["git", "credential", "fill"]
    assert seen["url"] == "https://api.github.com/repos/owner/name"
    assert seen["auth"] == "Bearer fake-cred-123"


def test_offsite_setup_refuses_public(monkeypatch, capsys):
    from flowstate import cli, paths
    monkeypatch.setattr(O, "repo_visibility", lambda repo: "PUBLIC")
    assert cli.main(["offsite", "setup", "--repo", "x/y"]) == 1
    assert "offsite" not in paths.load_config()


def test_offsite_setup_saves_private_repo(monkeypatch, capsys):
    from flowstate import cli, paths
    monkeypatch.setattr(O, "repo_visibility", lambda repo: "PRIVATE")
    assert cli.main(["offsite", "setup", "--repo", "x/y"]) == 0
    assert paths.load_config()["offsite"] == {"repo": "x/y", "branch": "flowstate-backup"}
    assert "token" not in json.dumps(paths.load_config()).lower()


# ---------------------------------------------------------------- scheduler
class FakeRun:
    def __init__(self, crontab=""):
        self.calls, self.crontab = [], crontab

    def __call__(self, argv, input=None, **kw):
        self.calls.append((argv, input))
        if argv == ["crontab", "-l"]:
            return subprocess.CompletedProcess(argv, 0, stdout=self.crontab, stderr="")
        if argv == ["crontab", "-"]:
            self.crontab = input
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def test_schedule_windows(tmp_path):
    run = FakeRun()
    home = tmp_path / "home"
    r = SCH.schedule("C:/py/python.exe", "C:/data/fs.db", "03:15", offsite=True,
                     system="Windows", home=home, run=run)
    argv = run.calls[0][0]
    assert argv[:4] == ["schtasks", "/Create", "/TN", SCH.TASK_NAME]
    assert argv[argv.index("/ST") + 1] == "03:15" and "DAILY" in argv
    wrapper = Path(r["files"][0])
    text = wrapper.read_text(encoding="utf-8")
    assert '"-m" "flowstate" "backup" "--db" "C:/data/fs.db" "--offsite"' in text
    assert str(home) in str(wrapper)
    run2 = FakeRun()
    SCH.schedule("p", "d", system="Windows", home=home, run=run2, remove=True)
    assert run2.calls[0][0][:2] == ["schtasks", "/Delete"] and not wrapper.exists()


def test_schedule_macos(tmp_path):
    run = FakeRun()
    home = tmp_path / "home"
    r = SCH.schedule("/venv/bin/python", "/data/fs.db", "02:30", system="Darwin", home=home, run=run)
    plist = home / "Library" / "LaunchAgents" / "com.flowstate.backup.plist"
    assert plist.exists() and "<integer>2</integer>" in plist.read_text(encoding="utf-8")
    assert ["launchctl", "load", "-w", str(plist)] in [c for c, _ in run.calls]
    SCH.schedule("p", "d", system="Darwin", home=home, run=FakeRun(), remove=True)
    assert not plist.exists()
    assert r["scheduler"].startswith("launchd")


def test_schedule_linux_systemd_and_cron(tmp_path):
    home = tmp_path / "home"
    run = FakeRun()
    SCH.schedule("/venv/bin/python", "/data/fs.db", "01:05", system="Linux", home=home, run=run,
                 which=lambda n: "/usr/bin/systemctl")
    timer = home / ".config" / "systemd" / "user" / "flowstate-backup.timer"
    assert "OnCalendar=*-*-* 01:05:00" in timer.read_text(encoding="utf-8")
    assert ["systemctl", "--user", "enable", "--now", "flowstate-backup.timer"] in [c for c, _ in run.calls]

    cron = FakeRun(crontab="0 1 * * * other-job\n")
    SCH.schedule("/venv/bin/python", "/data/fs.db", "01:05", system="Linux", home=home, run=cron,
                 which=lambda n: None)
    assert "other-job" in cron.crontab and "5 1 * * *" in cron.crontab and SCH.CRON_MARK in cron.crontab
    SCH.schedule("p", "d", system="Linux", home=home, run=cron, which=lambda n: None, remove=True)
    assert "other-job" in cron.crontab and SCH.CRON_MARK not in cron.crontab


def test_schedule_dry_run_changes_nothing(tmp_path):
    run = FakeRun()
    r = SCH.schedule("p", "d", system="Windows", home=tmp_path / "h", run=run, dry_run=True)
    assert run.calls == [] and not (tmp_path / "h").exists() and r["dry_run"]
    with pytest.raises(ValueError):
        SCH.schedule("p", "d", "25:00", system="Windows", home=tmp_path / "h", run=run)


def test_setup_dry_run_offers_backups_and_writes_nothing(tmp_path):
    import sys
    home = tmp_path / "fakehome"
    home.mkdir()
    r = subprocess.run([sys.executable, str(REPO / "setup_flowstate.py"), "--home", str(home),
                        "--skip-install", "--python", sys.executable, "--clients", "none",
                        "--db", str(home / "fs.db"), "--schedule-backups",
                        "--offsite-github", "someone/private-repo", "--dry-run"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "would check github.com/someone/private-repo is private" in r.stdout
    assert "Would schedule the nightly backup" in r.stdout
    assert not any(p.is_file() for p in home.rglob("*"))
