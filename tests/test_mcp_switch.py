"""scripts/mcp-switch.py, run only against a temporary config file (never a real one)."""

import importlib.util
import json
import subprocess
import sys

from conftest import REPO

SCRIPT = REPO / "scripts" / "mcp-switch.py"


def run_switch(cfg, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), "--config", str(cfg), *args],
                          capture_output=True, text=True, timeout=60)


def load_script():
    spec = importlib.util.spec_from_file_location("mcp_switch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_off_on_round_trip_keeps_other_entries(tmp_path):
    cfg = tmp_path / "Claude" / "claude_desktop_config.json"
    cfg.parent.mkdir()
    others = {"other": {"command": "x", "args": ["y"], "env": {"K": "v"}}}
    original = {"mcpServers": {**others, "github": {"command": "gh-server", "args": []}},
                "preferences": {"theme": "dark"}}
    cfg.write_text(json.dumps(original), encoding="utf-8")

    r = run_switch(cfg, "off", "github")
    assert r.returncode == 0, r.stdout + r.stderr
    after_off = json.loads(cfg.read_text(encoding="utf-8"))
    assert after_off == {"mcpServers": others, "preferences": {"theme": "dark"}}
    backup = cfg.with_name("claude_desktop_config.backup.json")
    assert json.loads(backup.read_text(encoding="utf-8")) == original

    r = run_switch(cfg, "on", "github")
    assert r.returncode == 0, r.stdout + r.stderr
    after_on = json.loads(cfg.read_text(encoding="utf-8"))
    assert after_on["mcpServers"]["other"] == others["other"]
    assert after_on["preferences"] == {"theme": "dark"}
    assert set(after_on["mcpServers"]) == {"other", "github"}
    assert json.loads(backup.read_text(encoding="utf-8")) == after_off


def test_missing_config_value_is_an_error(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT), "--config"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 2


def test_default_path_matches_setup_script():
    sys.path.insert(0, str(REPO))
    import setup_flowstate as setup
    mod = load_script()
    expected = setup.Places().config_path("claude-desktop")
    assert mod.CONFIG_PATH == expected
    assert mod.BACKUP_PATH == expected.with_name("claude_desktop_config.backup.json")
