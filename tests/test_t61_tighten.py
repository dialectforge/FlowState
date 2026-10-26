"""Task 61: `flowstate tighten` reports a file as owner-only only when it is, and says why not.

Windows: icacls's exit code is no longer ignored (a failed file was listed "owner-only now"),
and a failure says what really happened: access denied (the file is another account's, e.g.
BUILTIN\\Administrators), a drive without access lists (FAT / exFAT), or icacls's own message.
macOS / Linux: a chmod that fails is listed with its reason instead of "Nothing to change".
`tighten` exits 1 when anything could not be changed. Windows runs are mocked (subprocess.run,
_windows, _account, _owner, _volume), so they run here too. Temp folders only.
"""

import errno
import logging
import os
import sqlite3
import stat
import subprocess

import pytest

from flowstate import cli, privatefs

POSIX = os.name != "nt"
ME = "*S-1-5-21-1000-1000-1000-1001"
DENIED = (b"D:\\data\\flowstate.db: Access is denied.\r\n"
          b"Successfully processed 0 files; Failed processing 1 files\r\n")


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def db(tmp_path, monkeypatch):
    """A database in a folder FlowState didn't name (so the file itself gets the access list)."""
    monkeypatch.setattr(privatefs, "_warned", set())
    d = tmp_path / "data"
    d.mkdir()
    f = d / "flowstate.db"
    sqlite3.connect(f).close()
    return f


@pytest.fixture
def win(monkeypatch):
    """Windows, with icacls answering what each test says (default: success)."""
    calls, answer = [], {"rc": 0, "out": b"", "err": b""}

    def run(cmd, **kw):
        calls.append(cmd)
        if isinstance(answer.get("raise"), BaseException):
            raise answer["raise"]
        return subprocess.CompletedProcess(cmd, answer["rc"], answer["out"], answer["err"])
    monkeypatch.setattr(privatefs, "_windows", lambda: True)
    monkeypatch.setattr(privatefs, "_account", lambda: ME)
    monkeypatch.setattr(privatefs, "_volume", lambda p: ("NTFS", True))
    monkeypatch.setattr(privatefs, "_owner", lambda p: (ME[1:], "BOX\\someone"))
    monkeypatch.setattr(privatefs.subprocess, "run", run)
    answer["calls"] = calls
    return answer


def _icacls_calls(win, path):
    return [c for c in win["calls"] if c[0] == "icacls" and c[1] == str(path)]


# ================================================================ Windows
def test_windows_success_is_owner_only_now(db, win, capsys):
    res = privatefs.tighten_existing(db)
    assert db in res and not [p for p, _ in res.failed if p == db]
    assert _icacls_calls(win, db)
    assert cli.main(["tighten", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert f"owner-only now: {db}" in out and "NOT changed" not in out


def test_windows_access_denied_owned_by_administrators(db, win, monkeypatch, capsys):
    win.update(rc=5, out=DENIED)
    monkeypatch.setattr(privatefs, "_owner", lambda p: ("S-1-5-32-544", "BUILTIN\\Administrators"))
    res = privatefs.tighten_existing(db)
    assert db not in res
    why = dict(res.failed)[db]
    assert "access denied" in why and "owned by another account (BUILTIN\\Administrators)" in why
    assert "administrator window" in why and f'takeown /f "{db}"' in why
    assert "doesn't support file permissions" not in why
    assert cli.main(["tighten", "--db", str(db)]) == 1
    out = capsys.readouterr().out
    assert f"owner-only now: {db}" not in out
    assert f"NOT changed: {db}: access denied" in out and "BUILTIN\\Administrators" in out
    assert "could not be made owner-only" in out


def test_windows_access_denied_by_message_owner_unknown(db, win, monkeypatch):
    """A non-5 exit code with the "Access is denied" text, and an owner that can't be read."""
    win.update(rc=1, out=DENIED)
    monkeypatch.setattr(privatefs, "_owner", lambda p: None)
    why = privatefs._icacls_why(db, False)
    assert why.startswith("access denied: ") and "probably owned by another account" in why
    assert "takeown" in why


def test_windows_access_denied_on_a_file_you_own(db, win):
    win.update(rc=5, out=DENIED)
    why = privatefs._icacls_why(db, False)
    assert "this account may not change the file's permissions" in why
    assert "owned by another account" not in why


def test_windows_a_real_fat_drive_says_no_permissions(db, win, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    win.update(rc=1, out=b"The request is not supported.\r\n")
    monkeypatch.setattr(privatefs, "_volume", lambda p: ("exFAT", False))
    res = privatefs.tighten_existing(db)
    why = dict(res.failed)[db]
    assert why == "this drive doesn't support file permissions (exFAT)"
    assert db not in res
    assert any("doesn't support file permissions" in r.getMessage() for r in caplog.records)


def test_windows_ntfs_failure_is_not_called_unsupported(db, win, caplog):
    """The bug: any non-zero exit said "this drive doesn't support file permissions"."""
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    win.update(rc=5, out=DENIED)
    assert privatefs._icacls(db, False) is False
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("doesn't support file permissions" in m for m in msgs)
    assert any("access denied" in m for m in msgs)


def test_windows_other_failure_gives_icacls_own_message(db, win):
    win.update(rc=2, out=b"D:\\data\\flowstate.db: The system cannot find the file specified.\r\n"
                         b"Successfully processed 0 files; Failed processing 1 files\r\n")
    why = privatefs._icacls_why(db, False)
    assert why == "icacls failed: D:\\data\\flowstate.db: The system cannot find the file specified."
    win.update(rc=87, out=b"", err=b"")
    assert privatefs._icacls_why(db, False) == "icacls failed (exit code 87)"
    win.update(rc=1, out=b"x" * 500)
    assert len(privatefs._icacls_why(db, False)) <= len("icacls failed: ") + 200
    win["raise"] = FileNotFoundError(2, "no icacls")
    assert privatefs._icacls_why(db, False) == "icacls could not run (FileNotFoundError)"


def test_windows_unknown_drive_type_falls_back_to_the_message(db, win, monkeypatch):
    monkeypatch.setattr(privatefs, "_volume", lambda p: None)   # lookup failed: fail soft
    win.update(rc=1, out=b"Some other problem.\r\n")
    assert privatefs._icacls_why(db, False) == "icacls failed: Some other problem."


def test_windows_dry_run_lists_without_running_icacls(db, win, capsys):
    win.update(rc=5, out=DENIED)
    assert cli.main(["tighten", "--dry-run", "--db", str(db)]) == 0
    assert f"would make owner-only: {db}" in capsys.readouterr().out
    assert not [c for c in win["calls"] if c[0] == "icacls"]


def test_windows_lock_and_private_file_still_never_raise(tmp_path, win):
    win.update(rc=5, out=DENIED)
    assert privatefs.private_file(tmp_path / "new" / "x.json") is True
    privatefs.lock(tmp_path / "new")


def test_volume_and_owner_fail_soft_off_windows(tmp_path):
    if os.name == "nt":
        pytest.skip("real Windows")
    assert privatefs._volume(tmp_path) is None and privatefs._owner(tmp_path) is None


# ================================================================ -wal / -shm / backups
@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_wal_shm_and_backups_are_covered(tmp_path, monkeypatch):
    home = tmp_path / "flowstate"
    home.mkdir()
    bk = home / "backups"
    bk.mkdir()
    monkeypatch.setenv("FLOWSTATE_BACKUP_DIR", str(bk))
    db = home / "flowstate.db"
    sqlite3.connect(db).close()
    files = [db, home / "flowstate.db-wal", home / "flowstate.db-shm",
             bk / "flowstate-20261001-023000.db", bk / "flowstate-20261002-023000-pre-migration.db"]
    for f in files[1:]:
        f.write_bytes(b"x")
    mine = home / "flowstate.db.bak1"   # a copy you made: not FlowState's, left as it is
    mine.write_bytes(b"x")
    for f in files + [mine]:
        os.chmod(f, 0o644)
    cands = privatefs._candidates(db)
    assert all(f in cands for f in files) and mine not in cands
    res = privatefs.tighten_existing(db)
    assert all(_mode(f) == 0o600 for f in files) and set(files) <= set(res)
    assert _mode(mine) == 0o644 and _mode(home) == 0o700   # the folder closes it anyway
    assert not res.failed


# ================================================================ macOS / Linux chmod
@pytest.fixture
def loose(tmp_path, monkeypatch):
    monkeypatch.setattr(privatefs, "_warned", set())
    home = tmp_path / "flowstate"
    home.mkdir()
    db = home / "flowstate.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    os.chmod(home, 0o700)
    return db


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_chmod_on_another_accounts_file_is_reported(loose, monkeypatch, capsys):
    def refuse(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(privatefs, "_posix_owner", lambda p: "root")
    res = privatefs.tighten_existing(loose)
    assert loose not in res and "owned by another account (root)" in dict(res.failed)[loose]
    assert cli.main(["tighten", "--db", str(loose)]) == 1
    out = capsys.readouterr().out
    assert f"NOT changed: {loose}: not allowed: it is owned by another account (root)" in out
    assert "Nothing to change" not in out and f"owner-only now: {loose}" not in out


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_chmod_that_changes_nothing_is_reported_not_nothing_to_change(loose, monkeypatch, capsys):
    """Before: a drive that accepted chmod and kept the mode printed "Nothing to change"."""
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    assert cli.main(["tighten", "--db", str(loose)]) == 1
    out = capsys.readouterr().out
    assert f"NOT changed: {loose}: this drive doesn't support file permissions" in out
    assert "Nothing to change" not in out


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_chmod_other_error_gives_the_systems_reason(loose, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")

    def broken(*a, **k):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(os, "chmod", broken)
    res = privatefs.tighten_existing(loose)
    assert dict(res.failed)[loose] == "Input/output error"
    assert privatefs.auto_tighten(loose) == []
    assert any("could not make 1 of FlowState's files" in r.getMessage() for r in caplog.records)
    assert not any("doesn't support file permissions" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_a_mixed_run_lists_both_and_exits_1(loose, monkeypatch, capsys):
    other = loose.parent / "flowstate.db-wal"
    other.write_bytes(b"x")
    os.chmod(other, 0o644)
    real = os.chmod

    def chmod(p, mode, **k):
        if str(p) == str(loose):
            raise OSError(errno.EIO, "Input/output error")
        return real(p, mode, **k)
    monkeypatch.setattr(os, "chmod", chmod)
    assert cli.main(["tighten", "--db", str(loose)]) == 1
    out = capsys.readouterr().out
    assert f"owner-only now: {other}" in out and f"NOT changed: {loose}: Input/output error" in out
    assert _mode(other) == 0o600
