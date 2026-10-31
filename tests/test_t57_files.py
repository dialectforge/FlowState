"""Task 57: files and the off-site copy (task 56: N-F31 leftover and the Lows / Info notes on
files). Temp folders and fake credential stores only."""

import errno
import logging
import os
import sqlite3
import stat
import sys
from pathlib import Path

import pytest

from conftest import REPO
from flowstate import cli, privatefs

POSIX = os.name != "nt"


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_a_backup_dir_folder_is_tightened_like_the_default(seeded_db, tmp_path):
    """N-F31: `backup --dir` tightens the folder you chose (an older FlowState's loose copies
    in it, and the folder when it holds only FlowState's backups); `tighten --dir` too."""
    chosen = tmp_path / "my-backups"
    chosen.mkdir(mode=0o755)
    os.chmod(chosen, 0o755)
    old = chosen / "flowstate-20240101-000000.db"
    old.write_bytes(b"old copy")
    os.chmod(old, 0o644)
    assert cli.main(["backup", "--db", str(seeded_db), "--dir", str(chosen)]) == 0
    assert _mode(old) == 0o600 and _mode(chosen) == 0o700
    assert all(_mode(p) == 0o600 for p in chosen.glob("flowstate-*"))
    os.chmod(old, 0o644)
    os.chmod(chosen, 0o755)
    assert cli.main(["tighten", "--dry-run", "--db", str(seeded_db), "--dir", str(chosen)]) == 0
    assert _mode(old) == 0o644
    assert cli.main(["tighten", "--db", str(seeded_db), "--dir", str(chosen)]) == 0
    assert _mode(old) == 0o600 and _mode(chosen) == 0o700
    assert cli.main(["tighten", "--db", str(seeded_db), "--dir", str(tmp_path / "nope")]) == 1


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_a_chosen_folder_with_other_files_keeps_its_mode(seeded_db, tmp_path):
    chosen = tmp_path / "shared"
    chosen.mkdir()
    os.chmod(chosen, 0o755)
    (chosen / "holiday.jpg").write_bytes(b"x")
    os.chmod(chosen / "holiday.jpg", 0o644)
    assert cli.main(["backup", "--db", str(seeded_db), "--dir", str(chosen)]) == 0
    assert _mode(chosen) == 0o755 and _mode(chosen / "holiday.jpg") == 0o644
    assert all(_mode(p) == 0o600 for p in chosen.glob("flowstate-*"))


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_backup_folder_edge_cases(tmp_path):
    """An empty folder isn't FlowState's; a folder named flowstate-* isn't a backup; "._*"
    metadata files beside the backups don't stop the folder being FlowState's."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not privatefs._only_flowstate_files(empty)
    odd = tmp_path / "odd"
    odd.mkdir()
    (odd / "flowstate-photos").mkdir()
    assert not privatefs._only_flowstate_files(odd)
    exfat = tmp_path / "exfat"
    exfat.mkdir()
    (exfat / "flowstate-20250101-000000.db").write_bytes(b"x")
    (exfat / "._flowstate-20250101-000000.db").write_bytes(b"meta")
    assert privatefs._only_flowstate_files(exfat)
    only_meta = tmp_path / "meta"
    only_meta.mkdir()
    (only_meta / "._flowstate-1.db").write_bytes(b"meta")
    assert not privatefs._only_flowstate_files(only_meta)
    folder = privatefs._backup_folder(odd, True, own=False)
    assert odd / "flowstate-photos" not in folder


@pytest.mark.skipif(not POSIX, reason="mode bits")
def test_a_chmod_that_changes_nothing_is_not_claimed(tmp_path, monkeypatch, caplog):
    """Some SMB mounts accept chmod and change nothing: FlowState warns instead of claiming
    "made N files owner-only" at every start."""
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    monkeypatch.setattr(privatefs, "_warned", set())
    home = tmp_path / "flowstate"
    home.mkdir()
    db = home / "flowstate.db"
    sqlite3.connect(db).close()
    os.chmod(db, 0o644)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)     # accepted, nothing changes
    assert privatefs.auto_tighten(db) == []
    assert any("doesn't support file permissions" in r.getMessage() for r in caplog.records)


def test_the_permissions_warning_is_once_per_drive(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    monkeypatch.setattr(privatefs, "_warned", set())
    drives = iter([1, 1, 2])
    monkeypatch.setattr(privatefs, "_drive", lambda p: next(drives))
    for name in ("a", "b", "c"):
        privatefs.warn_no_permissions(tmp_path / name)
    assert len([r for r in caplog.records if "file permissions" in r.getMessage()]) == 2


def test_icacls_failure_warns(monkeypatch, caplog, tmp_path):
    import subprocess
    caplog.set_level(logging.WARNING, logger="flowstate.privatefs")
    monkeypatch.setattr(privatefs, "_warned", set())
    monkeypatch.setattr(privatefs, "_account", lambda: "*S-1-5-21-1")
    monkeypatch.setattr(privatefs, "_volume", lambda p: ("FAT32", False))   # task 61: a real FAT drive
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 5, b"", b""))
    assert privatefs._icacls(tmp_path, True) is False
    assert any("file permissions" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------- setup on a drive without permissions
def test_setup_with_an_existing_config_on_a_drive_without_permissions(tmp_path, monkeypatch, capsys):
    """(task 56 Low, also in 2.4.3) the config's backup copy no longer crashes setup when the
    drive refuses chmod."""
    from test_t55_files import no_permissions  # noqa: F401
    sys.path.insert(0, str(REPO))
    import setup_flowstate as setup

    def refuse(*a, **k):
        raise PermissionError(errno.EPERM, "Operation not permitted")
    cfg = tmp_path / "usb" / "mcp.json"
    cfg.parent.mkdir()
    cfg.write_text('{"mcpServers": {"other": {"command": "x"}}}\n')
    monkeypatch.setattr(os, "chmod", refuse)
    monkeypatch.setattr(os, "fchmod", refuse, raising=False)
    monkeypatch.setattr(os, "lchmod", refuse, raising=False)
    setup._warned_perms.clear()
    entry = {"command": "/x/python", "args": ["-m", "flowstate"]}
    assert setup.register_json(cfg, entry, dry=False).startswith("added")
    import json as _j
    data = _j.loads(cfg.read_text())
    assert "flowstate" in data["mcpServers"] and "other" in data["mcpServers"]
    assert list(cfg.parent.glob("mcp.json.flowstate-backup-*"))
    setup._warned_perms.clear()


# ---------------------------------------------------------------- the off-site key
class _Store:
    name = "private token file"

    def __init__(self, value, path="offsite-key"):
        self.value, self.path, self.writes = value, path, []

    def get(self):
        return self.value

    def create(self, text):
        from flowstate import secret_store as SS
        if self.value is not None:
            raise SS.AlreadyStored("there")
        self.writes.append(text)
        self.value = text


def test_an_empty_key_item_gives_the_right_advice_and_is_never_replaced(tmp_path, monkeypatch):
    from flowstate import offsite_crypto as OC
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "cfg"))
    for empty in ("", "  \n"):
        store = _Store(empty)
        with pytest.raises(OC.CryptoError) as e:
            OC.get_or_create_key(store)
        msg = str(e.value)
        assert "empty" in msg and "delete that empty item" in msg and "recovery code" in msg
        assert "same moment" not in msg and store.writes == []


def test_an_empty_key_file_reads_as_empty_not_missing(tmp_path):
    from flowstate import secret_store as SS
    f = tmp_path / "offsite-key"
    f.write_text("\n")
    assert SS.FileStore(f).get() == ""
    assert SS.FileStore(tmp_path / "none").get() is None
    seen = []

    class R:
        def __init__(self, code, out=""):
            self.returncode, self.stdout = code, out

    def run(argv, **kw):
        seen.append(argv)
        return R(0, "\n")
    assert SS.MacStore(run=run).get() == ""


def test_two_first_time_keys_at_once_make_one(tmp_path, monkeypatch):
    """(Windows-style store: check, then write) two pushes at the same moment end with one
    key, which both use: the second waits for the first's lock and reads its key."""
    import threading
    import time
    from flowstate import offsite_crypto as OC
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "cfg"))

    class Slow:
        name = "Windows Credential Manager"
        value = None

        def get(self):
            return self.value

        def create(self, text):   # check before write, no add-only flag
            if self.get() is not None:
                from flowstate import secret_store as SS
                raise SS.AlreadyStored("there")
            time.sleep(0.2)
            self.value = text
    store, keys = Slow(), []
    ts = [threading.Thread(target=lambda: keys.append(OC.get_or_create_key(store)[0])) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(keys) == 2 and keys[0] == keys[1] == OC.get_key(store)


def test_offsite_setup_exits_1_when_the_key_cant_be_read(tmp_path, monkeypatch, capsys):
    from flowstate import offsite, offsite_crypto as OC
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setattr(offsite, "require_private", lambda repo: None)
    monkeypatch.setattr(OC, "get_or_create_key", lambda *a, **k: (_ for _ in ()).throw(OC.CryptoError("locked")))
    assert cli.main(["offsite", "setup", "--repo", "someone/private-backups"]) == 1
    assert "isn't ready" in capsys.readouterr().out


def test_restore_message_doesnt_say_nothing_was_pushed(tmp_path, monkeypatch):
    from flowstate import offsite_crypto as OC

    class Broken:
        name = "macOS Keychain"

        def get(self):
            raise OSError("locked")
    with pytest.raises(OC.CryptoError) as e:
        OC.get_key(Broken(), doing="changed")
    assert "pushed" not in str(e.value) and "Nothing was changed" in str(e.value)
