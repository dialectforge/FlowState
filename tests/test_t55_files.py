"""Task 55, files area: file permissions, backups, the off-site copy and its key (re-audit
N-F6, N-F7, N-F9, N-F13, N-F30, N-F31, N-F32, N-F34, N-F35, N-F37, N-D5). Temp folders, fake
credential stores and local bare git repositories only: nothing leaves this computer."""

import errno
import json
import logging
import os
import sqlite3
import stat
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import REPO

POSIX = os.name != "nt"


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _git(*a, cwd):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout


class Tty:
    def __init__(self):
        self.told = []

    def tell(self, line):
        self.told.append(line)

    def ask(self, q):
        return ""

    def close(self):
        pass


@pytest.fixture
def bare(tmp_path):
    b = tmp_path / "remote.git"
    _git("init", "-q", "--bare", str(b), cwd=tmp_path)
    return b


def _setup_offsite(monkeypatch):
    from flowstate import paths
    paths.update_config(lambda c: c.__setitem__("offsite", {"repo": "example-owner/backups",
                                                            "branch": "flowstate-backup"}))


# ================================================================ N-F7 drives without permissions
@pytest.fixture
def no_permissions(monkeypatch):
    """A drive like exFAT / SMB: chmod and fchmod refused, mkdir with a mode refused."""
    from flowstate import privatefs

    def refuse(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse, raising=False)
    real_mkdir = Path.mkdir

    def mkdir(self, mode=0o777, parents=False, exist_ok=False):
        if mode != 0o777:
            raise OSError(errno.ENOTSUP, "Operation not supported")
        return real_mkdir(self, mode, parents, exist_ok)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(privatefs, "_warned", False, raising=False)


def test_nf7_database_backup_restore_export_and_settings_work_without_permissions(
        tmp_path, no_permissions, caplog, monkeypatch):
    from flowstate import backup, paths, privatefs
    from flowstate.database import init_db
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    db = tmp_path / "usb" / "flowstate" / "flowstate.db"
    init_db(db)
    assert db.is_file()
    sqlite3.connect(db).execute("INSERT INTO projects (name) VALUES ('P')").connection.commit()
    b = backup.backup_now(db, tmp_path / "usb" / "backups")["path"]
    assert b.is_file()
    r = backup.restore(b, tmp_path / "usb" / "restored" / "r.db")["restored_to"]
    assert sqlite3.connect(r).execute("SELECT name FROM projects").fetchall() == [("P",)]
    privatefs.write_private(tmp_path / "usb" / "export.md", "# P\n")
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "usb" / "cfg"))
    paths.update_config(lambda c: c.__setitem__("x", 1))
    assert paths.load_config()["x"] == 1
    warned = [r.getMessage() for r in caplog.records
              if "doesn't support file permissions" in r.getMessage()]
    assert len(warned) == 1 and "may be readable by other users of this computer" in warned[0]


def test_nf7_tighten_and_auto_tighten_tolerate_a_refusing_drive(tmp_path, no_permissions):
    from flowstate import privatefs
    home = tmp_path / "flowstate"
    os.makedirs(home)
    db = home / "flowstate.db"
    sqlite3.connect(db).close()
    privatefs.tighten_existing(db)   # never raises
    assert privatefs.auto_tighten(db) == []


def test_nf7_setup_creates_the_database_and_configs_without_permissions(tmp_path, no_permissions,
                                                                         capsys):
    sys.path.insert(0, str(REPO))
    import setup_flowstate as setup
    monkey_warned = getattr(setup, "_warned_perms", [])
    monkey_warned.clear()
    assert setup.ensure_db(tmp_path / "usb" / "flowstate.db", False) == "created"
    cfg = tmp_path / "usb" / "mcp.json"
    setup.atomic_write(cfg, "{}\n")
    assert cfg.read_text() == "{}\n"
    assert "doesn't support file permissions" in capsys.readouterr().out
    monkey_warned.clear()


# ================================================================ N-F6 a failed key read
def test_nf6_a_locked_keychain_never_makes_a_new_key(tmp_path):
    from flowstate import offsite_crypto as OC
    from flowstate import secret_store as SS
    writes = []

    def run(argv, **kw):
        if argv[:2] == ["security", "find-generic-password"]:
            return subprocess.CompletedProcess(argv, 36, stdout="", stderr="locked")   # locked
        writes.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    store = SS.MacStore(service="flowstate-test-offsite-key", run=run)
    with pytest.raises(OC.CryptoError, match="couldn't read the off-site key"):
        OC.get_or_create_key(store)
    assert writes == []


def test_nf6_keychain_not_found_is_the_only_no_key():
    from flowstate import secret_store as SS
    codes = iter([44])
    store = SS.MacStore(service="flowstate-test", run=lambda argv, **kw: subprocess.CompletedProcess(
        argv, next(codes), stdout="", stderr=""))
    assert store.get() is None


@pytest.mark.skipif(not POSIX or os.geteuid() == 0, reason="unreadable file needs POSIX, not root")
def test_nf6_an_unreadable_key_file_is_not_replaced(tmp_path):
    from flowstate import offsite_crypto as OC
    from flowstate import secret_store as SS
    store = SS.FileStore(tmp_path / "cfg" / "offsite-key")
    key, made = OC.get_or_create_key(store)
    assert made
    os.chmod(store.path, 0)
    try:
        with pytest.raises(OC.CryptoError):
            OC.get_or_create_key(store)
    finally:
        os.chmod(store.path, 0o600)
    assert OC.get_key(store) == key


def test_nf6_a_store_that_raises_on_read_stops_the_push(seeded_db, tmp_path, bare, monkeypatch):
    from flowstate import offsite, offsite_crypto as OC

    class Broken:
        name = "fake keyring"
        sets = []

        def get(self):
            raise RuntimeError("keyring backend unavailable")

        def set(self, v):
            self.sets.append(v)
    monkeypatch.setattr(OC, "_store", lambda: Broken())
    with pytest.raises(offsite.OffsiteError, match="no new key was made"):
        offsite.push_backup(seeded_db, "example-owner/backups", staging=tmp_path / "s",
                            remote_url=str(bare), checker=lambda _r: "PRIVATE")
    assert Broken.sets == []
    assert subprocess.run(["git", "--git-dir", str(bare), "rev-parse", "flowstate-backup"],
                          capture_output=True).returncode != 0   # nothing pushed


def test_nf6_create_never_overwrites(tmp_path):
    from flowstate import offsite_crypto as OC
    from flowstate import secret_store as SS
    store = SS.FileStore(tmp_path / "offsite-key")
    with pytest.raises(SS.AlreadyStored):
        store.create("a" * 43)
        store.create("b" * 43)
    assert store.get() == "a" * 43

    class Racy:   # says "none", but one appears before the write
        name = "racy"
        val, calls = None, 0

        def get(self):
            self.calls += 1
            return None if self.calls == 1 else "x" * 43

        def set(self, v):
            raise AssertionError("must not overwrite")
    with pytest.raises(OC.CryptoError, match="not replaced"):
        OC.get_or_create_key(Racy())


# ================================================================ N-F9 which key's code was shown
def test_nf9_a_changed_key_is_said_loudly_and_the_reminder_comes_back(
        seeded_db, tmp_path, bare, monkeypatch, capsys):
    from flowstate import cli, cli_review, offsite, offsite_crypto as OC, paths
    _setup_offsite(monkeypatch)
    key_a, key_b = os.urandom(32), os.urandom(32)
    kw = dict(staging=tmp_path / "s", remote_url=str(bare), checker=lambda _r: "PRIVATE")
    r = offsite.push_backup(seeded_db, "example-owner/backups", key=key_a, **kw)
    assert r["pushed"] and not r.get("key_changed")
    tty = Tty()
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert cli._show_recovery_code(key_a)
    assert offsite.key_line() is None                      # key A's code was seen
    cfg = paths.load_config()["offsite"]
    assert cfg["recovery_code_key"] == OC.key_id(key_a)
    assert OC.key_id(key_a) not in json.dumps({"x": OC.recovery_code(key_a)})   # id != code
    r = offsite.push_backup(seeded_db, "example-owner/backups", key=key_b, **kw)
    assert r["key_changed"] == OC.key_id(key_a)            # the key changed: said so
    line = offsite.key_line()
    assert line and "changed" in line                      # and the reminder is back
    assert cli._show_recovery_code(key_b)
    assert offsite.key_line() is None


def test_nf9_the_cli_says_the_key_changed(seeded_db, tmp_path, bare, monkeypatch, capsys):
    from flowstate import cli, offsite, offsite_crypto as OC
    _setup_offsite(monkeypatch)
    real = offsite.push_backup
    keys = iter([os.urandom(32), os.urandom(32)])
    monkeypatch.setattr(offsite, "push_backup", lambda db, repo, **k: real(
        db, repo, remote_url=str(bare), checker=lambda _r: "PRIVATE", key=next(keys),
        **{x: v for x, v in k.items() if x not in ("remote_url", "checker")}))
    assert cli.main(["offsite", "push", "--db", str(seeded_db)]) == 0
    assert "CHANGED" not in capsys.readouterr().out
    assert cli.main(["offsite", "push", "--db", str(seeded_db)]) == 0
    assert "the off-site encryption key CHANGED" in capsys.readouterr().out


# ================================================================ N-F13 read-only files (Windows)
def test_nf13_read_only_git_objects_are_deleted_and_checked(seeded_db, tmp_path, bare, monkeypatch):
    """Windows refuses to delete a read-only file; git makes its object files read-only."""
    from flowstate import offsite
    staging = tmp_path / "staging"
    (staging / ".git" / "objects" / "ab").mkdir(parents=True)
    old = staging / ".git" / "objects" / "ab" / "cdef"
    old.write_bytes(b"old unencrypted copy")
    os.chmod(old, 0o444)
    real_unlink = os.unlink

    def windows_unlink(path, *, dir_fd=None):
        st = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
        if not st.st_mode & stat.S_IWRITE:
            raise PermissionError(errno.EACCES, "Access is denied", str(path))
        return real_unlink(path, dir_fd=dir_fd)
    monkeypatch.setattr(os, "unlink", windows_unlink)
    monkeypatch.setattr(os, "remove", windows_unlink)
    r = offsite.push_backup(seeded_db, "example-owner/backups", staging=staging,
                            remote_url=str(bare), checker=lambda _r: "PRIVATE")
    assert r["pushed"] and not r.get("staging_left")
    assert not staging.exists()


def test_nf13_a_folder_that_wont_go_is_reported(tmp_path, monkeypatch):
    from flowstate import offsite
    d = tmp_path / "offsite-staging"
    d.mkdir()
    (d / "x").write_text("copy")
    monkeypatch.setattr(offsite.shutil, "rmtree", lambda *a, **k: None)
    msg = offsite.remove_staging(d)
    assert msg and "unencrypted copy" in msg and str(d) in msg


# ================================================================ N-F37 staging left by 2.4.3
def _old_staging() -> Path:
    from flowstate import paths
    st = paths.backup_dir() / "offsite-staging"
    st.mkdir(parents=True)
    (st / "flowstate-backup.db").write_bytes(b"SQLite format 3\0 old plaintext")
    return st


def test_nf37_a_failed_push_still_deletes_the_old_plaintext_folder(seeded_db):
    from flowstate import offsite
    st = _old_staging()
    with pytest.raises(offsite.OffsiteError):
        offsite.push_backup(seeded_db, "example-owner/backups", checker=lambda _r: None)
    assert not st.exists()


def test_nf37_disable_and_setup_delete_it(monkeypatch, capsys):
    from flowstate import cli, offsite
    st = _old_staging()
    assert cli.main(["offsite", "disable"]) == 0
    assert not st.exists()
    st = _old_staging()
    monkeypatch.setattr(offsite, "repo_visibility", lambda r: "PUBLIC")
    cli.main(["offsite", "setup", "--repo", "x/y"])
    assert not st.exists()
    st = _old_staging()
    assert cli.main(["offsite", "push"]) == 1   # not set up: still gone
    assert not st.exists()


# ================================================================ N-F34 damaged copies, keys, rollback
def test_nf34_a_copy_cut_to_its_header_is_refused(tmp_path):
    from flowstate import offsite_crypto as OC
    src = tmp_path / "p.db"
    src.write_bytes(b"data" * 100)
    key = os.urandom(32)
    enc = tmp_path / "x.fsenc"
    OC.encrypt_file(src, enc, key)
    for cut in (OC._HEADER2, OC._HEADER + 3):
        bad = tmp_path / f"cut{cut}.fsenc"
        bad.write_bytes(enc.read_bytes()[:cut])
        with pytest.raises(OC.CryptoError, match="cut short"):
            OC.decrypt_file(bad, tmp_path / "out.db", key)
        assert not (tmp_path / "out.db").exists()
    old = tmp_path / "v1.fsenc"   # a format-1 file cut to its header too
    old.write_bytes(OC.MAGIC + bytes.fromhex(OC.key_id(key)) + os.urandom(8)
                    + struct.pack(">I", OC.CHUNK))
    with pytest.raises(OC.CryptoError, match="cut short"):
        OC.decrypt_file(old, tmp_path / "out.db", key)


def test_nf34_the_time_in_the_header_is_sealed_and_shown(seeded_db, tmp_path, capsys):
    from flowstate import cli, offsite_crypto as OC
    key, _ = OC.get_or_create_key()
    enc = tmp_path / "copy.db.fsenc"
    OC.encrypt_file(seeded_db, enc, key, created=1700000000)
    assert OC.decrypt_file(enc, tmp_path / "o.db", key)["created"] == 1700000000
    data = bytearray(enc.read_bytes())
    data[OC._HEADER2 - 1] ^= 1   # an older time put in its place
    bad = tmp_path / "bad.fsenc"
    bad.write_bytes(bytes(data))
    with pytest.raises(OC.CryptoError, match="damaged"):
        OC.decrypt_file(bad, tmp_path / "o2.db", key)
    assert cli.main(["restore", str(enc), "--to", str(tmp_path / "r.db")]) == 0
    assert "This encrypted copy was made on 2023-11-1" in capsys.readouterr().out


def test_nf34_a_damaged_stored_key_gives_a_plain_message(monkeypatch, capsys):
    from flowstate import cli, offsite_crypto as OC
    store = OC._store()
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not*a*key\n")
    with pytest.raises(OC.CryptoError, match="damaged"):
        OC.get_key()
    with pytest.raises(OC.CryptoError, match="damaged"):
        OC.get_or_create_key()
    assert store.path.read_text() == "not*a*key\n"   # never replaced
    assert cli.main(["offsite", "key"]) == 1
    assert "damaged" in capsys.readouterr().out


def test_nf34_a_newer_copy_on_github_is_not_overwritten(seeded_db, tmp_path, bare):
    from flowstate import offsite
    other = tmp_path / "other"
    other.mkdir()
    _git("init", "-q", cwd=other)
    (other / "manifest.json").write_text("{}")
    _git("add", "manifest.json", cwd=other)
    _git("commit", "-q", "-m", "FlowState backup 2099-01-01T00:00:00Z", cwd=other)
    _git("push", "-q", str(bare), "HEAD:refs/heads/flowstate-backup", cwd=other)
    kw = dict(staging=tmp_path / "s", remote_url=str(bare), checker=lambda _r: "PRIVATE")
    with pytest.raises(offsite.OffsiteError, match="newer than this one"):
        offsite.push_backup(seeded_db, "example-owner/backups", **kw)
    assert "2099" in _git("log", "-1", "--format=%s", "flowstate-backup", cwd=bare)
    assert not (tmp_path / "s").exists()
    r = offsite.push_backup(seeded_db, "example-owner/backups", replace_newer=True, **kw)
    assert r["pushed"]
    again = offsite.push_backup(seeded_db, "example-owner/backups", **kw)   # ours: not newer
    assert again["pushed"]


# ================================================================ N-F35 the plaintext manifest
def test_nf35_the_manifest_says_only_date_size_and_checksum(seeded_db, tmp_path, bare):
    from flowstate import offsite
    r = offsite.push_backup(seeded_db, "example-owner/backups", staging=tmp_path / "s",
                            remote_url=str(bare), checker=lambda _r: "PRIVATE")
    text = _git("show", "flowstate-backup:manifest.json", cwd=bare)
    m = json.loads(text)
    assert set(m) == {"format", "created_utc", "bytes", "sha256"} and m["format"] == 2
    assert "projects" not in text and "tables" not in text and "key_id" not in text
    enc = tmp_path / "copy.fsenc"
    enc.write_bytes(subprocess.run(["git", "show", "flowstate-backup:flowstate-backup.db.fsenc"],
                                   cwd=bare, capture_output=True, check=True).stdout)
    assert offsite.check_against_manifest(enc, text)["created_utc"] == r["created_utc"]
    old = {"service": "flowstate", "key_id": "ab" * 8, "created_utc": "2026-10-01T00:00:00Z",
           "file": "flowstate-backup.db.fsenc", "bytes": enc.stat().st_size,
           "sha256": offsite._sha256(enc), "tables": {"projects": 2}}   # an earlier 2.4.4 build's
    assert offsite.check_against_manifest(enc, json.dumps(old))["bytes"] == enc.stat().st_size
    old["sha256"] = "0" * 64
    with pytest.raises(offsite.OffsiteError, match="checksum"):
        offsite.check_against_manifest(enc, json.dumps(old))


# ================================================================ N-F31 files left open by 2.4.3
@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_nf31_server_start_makes_old_files_owner_only(tmp_path, monkeypatch):
    from flowstate import server
    from flowstate.database import init_db
    home = tmp_path / "flowstate"
    db = home / "flowstate.db"
    init_db(db)
    bk = tmp_path / "my-backups"   # a FLOWSTATE_BACKUP_DIR of the user's choosing
    (bk / "restored").mkdir(parents=True)
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(bk))
    old = bk / "flowstate-20261001-023000.db"
    old.write_bytes(db.read_bytes())
    rest = bk / "restored" / "flowstate-restored-20261002-101010.db"
    rest.write_bytes(db.read_bytes())
    for p, m in ((home, 0o755), (db, 0o644), (bk, 0o755), (old, 0o644), (bk / "restored", 0o755),
                 (rest, 0o644)):
        os.chmod(p, m)
    server.init_backend(db)
    assert [_mode(p) for p in (home, db, bk, old, bk / "restored", rest)] == \
        [0o700, 0o600, 0o700, 0o600, 0o700, 0o600]


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_nf31_a_backups_folder_with_other_things_in_it_is_left_alone(tmp_path, monkeypatch):
    from flowstate import privatefs
    bk = tmp_path / "Documents"
    bk.mkdir()
    (bk / "taxes.pdf").write_text("x")
    old = bk / "flowstate-20261001-023000.db"
    old.write_text("x")
    os.chmod(bk, 0o755)
    os.chmod(old, 0o644)
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(bk))
    privatefs.tighten_existing(tmp_path / "x" / "flowstate.db")
    assert _mode(bk) == 0o755 and _mode(old) == 0o600


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_nf31_flowstate_backup_tightens_too_and_says_so(seeded_db, tmp_path, monkeypatch, capsys):
    from flowstate import cli, paths
    b = paths.backup_dir()
    b.mkdir(parents=True, exist_ok=True)
    old = b / "flowstate-20261001-023000.db"
    old.write_bytes(seeded_db.read_bytes())
    os.chmod(old, 0o644)
    assert cli.main(["backup", "--db", str(seeded_db)]) == 0
    assert _mode(old) == 0o600 and "owner-only" in capsys.readouterr().out


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_nf31_add_client_makes_the_config_folder_owner_only(tmp_path, monkeypatch, capsys):
    from flowstate import cli
    auth = tmp_path / "fresh-config" / "remote-auth.db"
    monkeypatch.setenv("FLOWSTATE_AUTH_DB", str(auth))
    assert cli.main(["remote", "add-client", "--name", "Folder App"]) == 0
    assert _mode(auth.parent) == 0o700 and _mode(auth) == 0o600


# ================================================================ N-F32 --to-file and symlinks
@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_nf32_add_client_refuses_a_dangling_symlink(tmp_path, capsys):
    from flowstate import cli, remote_auth
    target = tmp_path / "elsewhere" / "token.txt"
    target.parent.mkdir()
    link = tmp_path / "tok.txt"
    link.symlink_to(target)
    assert cli.main(["remote", "add-client", "--name", "Link App", "--to-file", str(link)]) == 1
    assert not target.exists()
    assert "symbolic link" in capsys.readouterr().out
    assert not [c for c in remote_auth.AuthStore().list_clients() if c["name"] == "Link App"]


@pytest.mark.skipif(not POSIX, reason="symlinks")
def test_nf32_write_private_never_follows_a_link_and_fixes_the_mode(tmp_path):
    from flowstate import privatefs
    target = tmp_path / "t.txt"
    link = tmp_path / "l.txt"
    link.symlink_to(target)
    with pytest.raises(privatefs.NotPrivate):
        privatefs.write_private(link, "secret")
    assert not target.exists()
    f = tmp_path / "open.txt"
    f.write_text("old")
    os.chmod(f, 0o644)
    privatefs.write_private(f, "new")
    assert _mode(f) == 0o600 and f.read_text() == "new"


# ================================================================ N-F30 Windows access lists
def test_nf30_icacls_gets_the_account_sid(monkeypatch, tmp_path):
    from flowstate import privatefs
    seen = []
    sid = "S-1-5-21-1111111111-2222222222-3333333333-1001"

    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[0] == "whoami":
            return subprocess.CompletedProcess(cmd, 0, stdout=f'"box\\someone","{sid}"\r\n')
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(privatefs, "_windows", lambda: True)
    monkeypatch.setattr(privatefs, "_SID", [])
    monkeypatch.setenv("USERNAME", "someone")
    monkeypatch.setattr(privatefs.subprocess, "run", run)
    privatefs.private_dir(tmp_path / "a" / "b")
    ic = [c for c in seen if c[0] == "icacls"]
    assert len(ic) == 2 and all(f"*{sid}:(OI)(CI)F" in c for c in ic)
    assert not any("someone:(OI)(CI)F" in c for c in ic)
    assert sum(c[0] == "whoami" for c in seen) == 1   # looked up once


def test_nf30_setup_keeps_a_config_files_own_access_list(monkeypatch, tmp_path):
    sys.path.insert(0, str(REPO))
    import setup_flowstate as setup
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text("{}\n")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        if cmd[0] == "icacls" and "/save" in cmd:
            Path(cmd[cmd.index("/save") + 1]).write_text("acl")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(setup, "_windows", lambda: True)
    monkeypatch.setattr(setup.subprocess, "run", run)
    setup.atomic_write(cfg, '{"mcpServers": {}}\n')
    assert seen[0][:3] == ["icacls", str(cfg), "/save"]
    assert seen[1][:3] == ["icacls", str(tmp_path), "/restore"]
    assert seen[1][3] == seen[0][3]
    assert cfg.read_text() == '{"mcpServers": {}}\n'
    assert not list(tmp_path.glob("*.flowstate-acl")) and not list(tmp_path.glob("*-tmp"))


# ================================================================ N-D5 the dependency
def test_nd5_cryptography_is_declared():
    """Listed as the `offsite` extra, not in `dependencies`: a changed dependency list makes
    `flowstate update` build a new .venv and refuse on Windows. `mcp` must still bring it."""
    import importlib.metadata
    import tomllib
    proj = tomllib.loads((REPO / "mcp-server" / "pyproject.toml").read_text())["project"]
    assert any(d.replace(" ", "").startswith("cryptography>=") for d in proj["optional-dependencies"]["offsite"])
    assert not any(d.startswith("cryptography") for d in proj["dependencies"])
    from flowstate.selfupdate import _deps
    old = (REPO / "mcp-server" / "pyproject.toml").read_text()
    assert _deps(old) == sorted(["mcp>=1.26,<2", "pydantic>=2.0.0"])   # same list as 2.4.3: no new .venv
    assert importlib.metadata.version("cryptography")   # installed through mcp
