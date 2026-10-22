"""setup_flowstate.py, run only against fake home folders in tmp (never real configs)."""

import json
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from conftest import REPO

sys.path.insert(0, str(REPO))
import setup_flowstate as setup  # noqa: E402

REAL_HOME = Path.home()


def run_setup(home: Path, *args: str) -> subprocess.CompletedProcess:
    assert home.resolve() != REAL_HOME.resolve()
    return subprocess.run([sys.executable, str(REPO / "setup_flowstate.py"), "--home", str(home),
                           "--skip-install", "--python", sys.executable, *args],
                          capture_output=True, text=True, timeout=120)


def backups(path: Path) -> list:
    return sorted(path.parent.glob(path.name + ".flowstate-backup-*"))


@pytest.fixture
def home(tmp_path) -> Path:
    h = tmp_path / "fakehome"
    h.mkdir()
    return h


def test_fresh_install_all_clients(home):
    db = home / "data" / "fs.db"
    r = run_setup(home, "--clients", "all", "--db", str(db))
    assert r.returncode == 0, r.stdout + r.stderr
    places = setup.Places(home)
    for client in setup.JSON_CLIENTS:
        cfg = json.loads(places.config_path(client).read_text(encoding="utf-8"))
        entry = cfg["mcpServers"]["flowstate"]
        assert entry["args"] == ["-m", "flowstate"] and entry["env"]["FLOWSTATE_DB"] == str(db.resolve())
    for client in setup.TOML_CLIENTS:
        cfg = tomllib.loads(places.config_path(client).read_text(encoding="utf-8"))
        assert cfg["mcp_servers"]["flowstate"]["command"] == sys.executable
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "todos", "auto_sessions"} <= tables


def test_existing_config_preserved_backed_up_and_idempotent(home):
    places = setup.Places(home)
    cfg = places.config_path("claude-desktop")
    cfg.parent.mkdir(parents=True)
    original = {"mcpServers": {"other": {"command": "x", "args": ["y"]}},
                "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original), encoding="utf-8")
    db = home / "fs.db"
    r1 = run_setup(home, "--clients", "claude-desktop", "--db", str(db))
    assert r1.returncode == 0, r1.stdout + r1.stderr
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert data["preferences"] == {"theme": "dark"} and "flowstate" in data["mcpServers"]
    assert len(backups(cfg)) == 1
    assert json.loads(backups(cfg)[0].read_text(encoding="utf-8")) == original
    after_first = cfg.read_text(encoding="utf-8")
    r2 = run_setup(home, "--clients", "claude-desktop", "--db", str(db))
    assert r2.returncode == 0 and "unchanged" in r2.stdout
    assert cfg.read_text(encoding="utf-8") == after_first and len(backups(cfg)) == 1


def test_stale_entry_is_updated(home):
    places = setup.Places(home)
    cfg = places.config_path("cursor")
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"mcpServers": {"flowstate": {"command": "old"}}}), encoding="utf-8")
    r = run_setup(home, "--clients", "cursor", "--db", str(home / "fs.db"))
    assert r.returncode == 0 and "updated" in r.stdout
    assert json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]["flowstate"]["command"] == sys.executable


def test_unparseable_configs_are_refused_untouched(home):
    places = setup.Places(home)
    jcfg, tcfg = places.config_path("cursor"), places.config_path("codex")
    jcfg.parent.mkdir(parents=True)
    tcfg.parent.mkdir(parents=True)
    jcfg.write_text("{ not json", encoding="utf-8")
    tcfg.write_text("this = = broken", encoding="utf-8")
    r = run_setup(home, "--clients", "cursor,codex", "--db", str(home / "fs.db"))
    assert r.returncode == 1 and r.stdout.count(": refused:") == 2
    assert jcfg.read_text(encoding="utf-8") == "{ not json"
    assert tcfg.read_text(encoding="utf-8") == "this = = broken"
    assert not backups(jcfg) and not backups(tcfg)


def test_toml_existing_content_kept_and_entry_replaced(home):
    places = setup.Places(home)
    cfg = places.config_path("grok")
    cfg.parent.mkdir(parents=True)
    cfg.write_text('model = "grok-4"\n\n[mcp_servers.flowstate]\ncommand = "old"\n\n'
                   '[mcp_servers.flowstate.env]\nA = "1"\n\n[mcp_servers.other]\ncommand = "keep"\n',
                   encoding="utf-8")
    r = run_setup(home, "--clients", "grok", "--db", str(home / "fs.db"))
    assert r.returncode == 0, r.stdout + r.stderr
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["model"] == "grok-4" and data["mcp_servers"]["other"] == {"command": "keep"}
    assert data["mcp_servers"]["flowstate"]["env"]["FLOWSTATE_DB"] == str((home / "fs.db").resolve())
    assert len(backups(cfg)) == 1
    r2 = run_setup(home, "--clients", "grok", "--db", str(home / "fs.db"))
    assert "unchanged" in r2.stdout and len(backups(cfg)) == 1


def test_dry_run_writes_nothing(home):
    db = home / "fs.db"
    r = run_setup(home, "--clients", "all", "--db", str(db), "--dry-run")
    assert r.returncode == 0 and "would be added" in r.stdout
    assert not db.exists()
    assert not any(p.is_file() for p in home.rglob("*"))


def test_existing_db_kept_and_migrated(home, seeded_db):
    before = sqlite3.connect(seeded_db).execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    conn = sqlite3.connect(seeded_db)
    conn.execute("DROP TABLE auto_sessions")
    conn.commit()
    conn.close()
    r = run_setup(home, "--clients", "none", "--db", str(seeded_db))
    assert r.returncode == 0 and "existing database kept" in r.stdout
    conn = sqlite3.connect(seeded_db)
    assert conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0] == before
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='auto_sessions'").fetchone()


def test_verify_runs_a_real_handshake(home):
    r = run_setup(home, "--clients", "none", "--db", str(home / "fs.db"), "--verify")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "[verify] OK" in r.stdout


def test_auto_detects_only_installed_clients(home):
    (home / ".cursor").mkdir()
    (home / ".grok").mkdir()
    places = setup.Places(home)
    assert setup.pick_clients("auto", places) == ["cursor", "grok"]


def test_windows_store_claude_path(home):
    store = home / "AppData" / "Local" / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude"
    store.mkdir(parents=True)
    places = setup.Places(home, system="Windows")
    assert places.config_path("claude-desktop") == store / "claude_desktop_config.json"
    plain = setup.Places(home / "nostore", system="Windows")
    assert plain.config_path("claude-desktop").parts[-3:] == ("Roaming", "Claude", "claude_desktop_config.json")


# ---------------------------------------------------------------- (task 38) desktop shortcut
def test_places_desktop_is_under_the_fake_home(home):
    for system in ("Windows", "Darwin", "Linux"):
        assert setup.Places(home, system=system).desktop == home / "Desktop"


def test_shortcut_is_made_once_and_kept_up_to_date(home):
    db = home / "data" / "fs.db"
    desk = home / "Desktop"
    desk.mkdir()
    r = run_setup(home, "--clients", "none", "--db", str(db), "--dry-run")
    assert r.returncode == 0 and "FlowState review" in r.stdout and "would be created (dry run)" in r.stdout
    assert not any(desk.iterdir())
    r = run_setup(home, "--clients", "none", "--db", str(db))
    assert r.returncode == 0, r.stdout + r.stderr
    places = setup.Places(home)
    path, text, _x = setup.shortcut_file(places, Path(sys.executable), db.resolve())
    assert path.parent == desk and path.read_bytes().decode("utf-8") == text
    assert f"[shortcut] {path}: created" in r.stdout
    assert setup.SHORTCUT_MARK in text and "review-page" in text and str(db.resolve()) in text
    assert str(setup.SERVER_DIR) in text and str(sys.executable) in text
    r = run_setup(home, "--clients", "none", "--db", str(db))
    assert f"[shortcut] {path}: unchanged" in r.stdout
    other = home / "data" / "other.db"
    r = run_setup(home, "--clients", "none", "--db", str(other))
    assert f"[shortcut] {path}: updated" in r.stdout and str(other.resolve()) in path.read_text(encoding="utf-8")
    assert [p.name for p in desk.iterdir()] == [path.name]


def test_shortcut_never_replaces_the_users_own_file(home):
    desk = home / "Desktop"
    desk.mkdir()
    places = setup.Places(home)
    path, _t, _x = setup.shortcut_file(places, Path(sys.executable), home / "fs.db")
    path.write_text("my own script\n", encoding="utf-8")
    r = run_setup(home, "--clients", "none", "--db", str(home / "fs.db"))
    assert r.returncode == 0 and "left alone" in r.stdout
    assert path.read_text(encoding="utf-8") == "my own script\n"


def test_no_desktop_folder_skips_with_a_line(home):
    r = run_setup(home, "--clients", "none", "--db", str(home / "fs.db"))
    assert r.returncode == 0 and "no desktop folder at" in r.stdout and "skipped" in r.stdout
    assert not (home / "Desktop").exists()


@pytest.mark.parametrize("system,name", [("Windows", "FlowState review.cmd"),
                                         ("Darwin", "FlowState review.command"),
                                         ("Linux", "flowstate-review.desktop")])
def test_shortcut_per_os(home, system, name):
    (home / "Desktop").mkdir()
    places = setup.Places(home, system=system)
    py = home / "py 100% \"odd\" $dir's" / "python"
    db = home / "data's 50%" / "fs.db"
    out = setup.make_shortcut(places, py, db, dry=False)
    path = home / "Desktop" / name
    assert out == f"{path}: created" and path.exists()
    raw = path.read_bytes().decode("utf-8")
    assert setup.SHORTCUT_MARK in raw
    if system == "Windows":
        assert "\r\n" in raw and raw.startswith("@echo off")
        assert f'set "FLOWSTATE_DB={str(db).replace("%", "%%")}"' in raw
        assert f'"{str(py).replace("%", "%%")}" -m flowstate review-page' in raw
    elif system == "Darwin":
        assert raw.startswith("#!/bin/sh\n") and "exec '" in raw and "'\\''s" in raw
        # the quoting round-trips through a POSIX shell parser
        import shlex
        line = next(l for l in raw.splitlines() if l.startswith("exec "))
        assert shlex.split(line) == ["exec", str(py), "-m", "flowstate", "review-page"]
    else:
        assert raw.startswith("[Desktop Entry]\n") and "Terminal=true" in raw
        exec_ = next(l for l in raw.splitlines() if l.startswith("Exec="))
        assert "%%" in exec_ and '\\"odd\\"' in exec_ and "\\$dir" in exec_
    assert setup.make_shortcut(places, py, db, dry=False) == f"{path}: unchanged"
