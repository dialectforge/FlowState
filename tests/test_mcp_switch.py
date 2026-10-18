"""Tests for scripts/mcp-switch.py platform config resolution and off/on round-trip."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

from conftest import REPO

sys.path.insert(0, str(REPO))
import setup_flowstate as setup  # noqa: E402

SCRIPT_PATH = REPO / "scripts" / "mcp-switch.py"


def _load_mcp_switch_module():
    spec = importlib.util.spec_from_file_location("mcp_switch", SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_switch(config_path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--config", str(config_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_default_config_and_backup_paths_by_platform(tmp_path):
    mod = _load_mcp_switch_module()
    home = tmp_path / "fakehome"
    home.mkdir()

    mac_cfg = mod.default_config_path("darwin", home=home)
    assert mac_cfg == setup.Places(home, system="Darwin").config_path("claude-desktop")
    assert mod.backup_path_for(mac_cfg) == (
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.backup.json"
    )

    linux_cfg = mod.default_config_path("linux", home=home)
    assert linux_cfg == setup.Places(home, system="Linux").config_path("claude-desktop")
    assert mod.backup_path_for(linux_cfg) == (
        home / ".config" / "Claude" / "claude_desktop_config.backup.json"
    )

    win_cfg = mod.default_config_path("win32", home=home)
    assert win_cfg == setup.Places(home, system="Windows").config_path("claude-desktop")
    assert mod.backup_path_for(win_cfg) == (
        home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.backup.json"
    )

    store = (
        home
        / "AppData"
        / "Local"
        / "Packages"
        / "Claude_abc123"
        / "LocalCache"
        / "Roaming"
        / "Claude"
    )
    store.mkdir(parents=True)
    win_store_cfg = mod.default_config_path("win32", home=home)
    assert win_store_cfg == setup.Places(home, system="Windows").config_path("claude-desktop")
    assert win_store_cfg == store / "claude_desktop_config.json"
    assert mod.backup_path_for(win_store_cfg) == store / "claude_desktop_config.backup.json"


def test_off_and_on_round_trip_preserves_other_entries(tmp_path):
    mod = _load_mcp_switch_module()
    cfg_path = tmp_path / "claude_desktop_config.json"
    backup_path = tmp_path / "claude_desktop_config.backup.json"

    original = {
        "mcpServers": {
            "github": mod.ALL_MCPS["github"],
            "flowstate": {
                "command": sys.executable,
                "args": ["-m", "flowstate"],
                "env": {"FLOWSTATE_DB": "/tmp/test.db"},
            },
            "other-custom": {
                "command": "node",
                "args": ["custom-mcp.js"],
            },
        },
        "preferences": {"theme": "dark"},
    }
    cfg_path.write_text(json.dumps(original, indent=2), encoding="utf-8")

    # Turn off github and verify flowstate, other-custom, and preferences remain untouched
    r_off = run_switch(cfg_path, "off", "github")
    assert r_off.returncode == 0, r_off.stdout + r_off.stderr
    after_off = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert "github" not in after_off["mcpServers"]
    assert after_off["mcpServers"]["flowstate"] == original["mcpServers"]["flowstate"]
    assert after_off["mcpServers"]["other-custom"] == original["mcpServers"]["other-custom"]
    assert after_off["preferences"] == {"theme": "dark"}
    assert backup_path.exists()
    assert json.loads(backup_path.read_text(encoding="utf-8")) == original

    # Turn github back on and verify full round-trip
    r_on = run_switch(cfg_path, "on", "github")
    assert r_on.returncode == 0, r_on.stdout + r_on.stderr
    after_on = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after_on == original

    # Also verify custom flowstate entry round-trips via off -> on without losing custom args/env
    r_off_fs = run_switch(cfg_path, "off", "flowstate")
    assert r_off_fs.returncode == 0, r_off_fs.stdout + r_off_fs.stderr
    assert "flowstate" not in json.loads(cfg_path.read_text(encoding="utf-8"))["mcpServers"]

    r_on_fs = run_switch(cfg_path, "on", "flowstate")
    assert r_on_fs.returncode == 0, r_on_fs.stdout + r_on_fs.stderr
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == original


def test_only_preserves_existing_custom_server_and_explicit_backup_override(tmp_path):
    mod = _load_mcp_switch_module()
    cfg_path = tmp_path / "claude_desktop_config.json"
    custom_bkp = tmp_path / "custom_backup.json"

    custom_fs = {
        "command": sys.executable,
        "args": ["-m", "flowstate"],
        "env": {"FLOWSTATE_DB": "/tmp/test.db"},
    }
    initial = {
        "mcpServers": {
            "flowstate": custom_fs,
            "github": mod.ALL_MCPS["github"],
        },
        "preferences": {"theme": "dark"},
    }
    cfg_path.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    mod.CONFIG_PATH = cfg_path
    mod.BACKUP_PATH = custom_bkp
    mod.only_mcps(["flowstate"])

    assert custom_bkp.exists()
    after_only = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert list(after_only["mcpServers"].keys()) == ["flowstate"]
    assert after_only["mcpServers"]["flowstate"] == custom_fs
    assert after_only["preferences"] == {"theme": "dark"}


def test_on_skips_already_enabled_server_without_overwriting_from_backup(tmp_path):
    cfg_path = tmp_path / "claude_desktop_config.json"
    backup_path = tmp_path / "claude_desktop_config.backup.json"

    older_github = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "OLD_BACKUP_TOKEN"},
    }
    newer_github = {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "NEW_CURRENT_TOKEN"},
    }
    backup_path.write_text(
        json.dumps({"mcpServers": {"github": older_github}}, indent=2),
        encoding="utf-8",
    )
    cfg_path.write_text(
        json.dumps({"mcpServers": {"github": newer_github}}, indent=2),
        encoding="utf-8",
    )

    res = run_switch(cfg_path, "on", "github")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "already on" in res.stdout.lower()

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["mcpServers"]["github"] == newer_github

