#!/usr/bin/env python3
"""
MCP Tool Switcher - Quickly enable/disable MCP servers for Claude Desktop

Usage:
    python3 mcp-switch.py                    # Show current status
    python3 mcp-switch.py list               # List all available MCPs
    python3 mcp-switch.py on github xcode    # Enable specific MCPs
    python3 mcp-switch.py off github xcode   # Disable specific MCPs  
    python3 mcp-switch.py only flowstate     # Enable ONLY these, disable rest
    python3 mcp-switch.py preset minimal     # Use a preset configuration
    python3 mcp-switch.py preset ios         # Preset for iOS development

After changes, restart Claude Desktop to apply.
"""

import json
import os
import sys
from pathlib import Path

# Config location
def backup_path_for(config_path: Path) -> Path:
    suffix = config_path.suffix or ".json"
    return config_path.with_name(f"{config_path.stem}.backup{suffix}")


def default_config_path(platform_name: "str | None" = None, home: "Path | None" = None) -> Path:
    plat = (platform_name or sys.platform).lower()
    fake_home = home is not None
    h = Path(home) if home else Path.home()
    if plat in ("win32", "windows") or plat.startswith("win"):
        localappdata = (
            Path(os.environ["LOCALAPPDATA"])
            if (not fake_home and os.environ.get("LOCALAPPDATA"))
            else h / "AppData" / "Local"
        )
        pkgs = sorted((localappdata / "Packages").glob("Claude_*"))
        for p in pkgs:
            store = p / "LocalCache" / "Roaming" / "Claude"
            if store.is_dir():
                return store / "claude_desktop_config.json"
        appdata = (
            Path(os.environ["APPDATA"])
            if (not fake_home and os.environ.get("APPDATA"))
            else h / "AppData" / "Roaming"
        )
        return appdata / "Claude" / "claude_desktop_config.json"
    if plat == "darwin":
        return h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    return h / ".config" / "Claude" / "claude_desktop_config.json"


CONFIG_PATH = default_config_path()
BACKUP_PATH = backup_path_for(CONFIG_PATH)
_LAST_CONFIG_PATH = CONFIG_PATH
_LAST_BACKUP_PATH = BACKUP_PATH


def _current_backup_path() -> Path:
    global BACKUP_PATH, _LAST_CONFIG_PATH, _LAST_BACKUP_PATH
    if CONFIG_PATH != _LAST_CONFIG_PATH:
        if BACKUP_PATH == _LAST_BACKUP_PATH:
            BACKUP_PATH = backup_path_for(Path(CONFIG_PATH))
        _LAST_CONFIG_PATH = Path(CONFIG_PATH)
        _LAST_BACKUP_PATH = Path(BACKUP_PATH)
    return Path(BACKUP_PATH)


def set_config_path(path: "str | Path") -> Path:
    global CONFIG_PATH, BACKUP_PATH, _LAST_CONFIG_PATH, _LAST_BACKUP_PATH
    CONFIG_PATH = Path(path).expanduser()
    BACKUP_PATH = backup_path_for(CONFIG_PATH)
    _LAST_CONFIG_PATH = CONFIG_PATH
    _LAST_BACKUP_PATH = BACKUP_PATH
    return CONFIG_PATH


# All known MCP configurations (add new ones here)
ALL_MCPS = {
    "flowstate": {
        "command": "run_server.sh",  # Update this to your local path
        "args": []
    },
    "github": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "YOUR_TOKEN_HERE"}
    },
    "xcode": {
        "command": "npx",
        "args": ["-y", "xcodebuildmcp@latest"]
    },
    # Add more MCPs here as needed
}

# Presets for common workflows
PRESETS = {
    "minimal": ["flowstate"],
    "ios": ["flowstate", "xcode"],
    "web": ["flowstate", "github"],
    "full": ["flowstate", "github", "xcode"],
}

# Aliases for easier typing
ALIASES = {
    "fs": "flowstate",
    "gh": "github",
    "git": "github",
    "xc": "xcode",
    "xcodebuildmcp": "xcode",
}

def load_config():
    cfg_path = Path(CONFIG_PATH)
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    return {"mcpServers": {}}

def _load_backup_servers():
    bkp_path = _current_backup_path()
    if bkp_path.exists():
        try:
            with open(bkp_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                return data["mcpServers"]
        except (OSError, ValueError):
            pass
    return {}

def save_config(config):
    cfg_path = Path(CONFIG_PATH)
    bkp_path = _current_backup_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # Backup first
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as f:
            backup = f.read()
        with open(bkp_path, 'w', encoding="utf-8") as f:
            f.write(backup)
    
    with open(cfg_path, 'w', encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    print(f"\n✅ Config saved. Backup at: {bkp_path}")
    print("⚠️  Restart Claude Desktop to apply changes.")

def resolve_name(name):
    """Resolve aliases to canonical names"""
    name = name.lower()
    return ALIASES.get(name, name)

def get_enabled():
    """Get list of currently enabled MCPs"""
    config = load_config()
    return list(config.get("mcpServers", {}).keys())

def show_status():
    config = load_config()
    enabled = config.get("mcpServers", {})
    
    print("\n📊 MCP Status:")
    print("-" * 40)
    
    for name in ALL_MCPS:
        # Check various possible names in config
        is_enabled = any(k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "") 
                        for k in enabled.keys())
        status = "✅ ON " if is_enabled else "⬚  OFF"
        print(f"  {status}  {name}")
    
    print("-" * 40)
    print(f"  Total enabled: {len(enabled)}")
    print("\nPresets available:", ", ".join(PRESETS.keys()))

def enable_mcps(names):
    config = load_config()
    if "mcpServers" not in config:
        config["mcpServers"] = {}
    backup_servers = _load_backup_servers()
    
    for raw_name in names:
        name = resolve_name(raw_name)
        current_matches = [
            k for k in config["mcpServers"]
            if k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "")
        ]
        if current_matches:
            print(f"  ✅ Already on: {name}")
            continue
        backup_matches = [
            k for k in backup_servers
            if k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "")
        ]
        if backup_matches:
            for k in backup_matches:
                config["mcpServers"][k] = backup_servers[k]
            print(f"  ✅ Enabled: {name}")
        elif name in ALL_MCPS:
            # Use proper config key names
            key = "XcodeBuildMCP" if name == "xcode" else name
            config["mcpServers"][key] = ALL_MCPS[name]
            print(f"  ✅ Enabled: {name}")
        else:
            print(f"  ⚠️  Unknown MCP: {name}")
    
    save_config(config)

def disable_mcps(names):
    config = load_config()
    servers = config.get("mcpServers", {})
    
    for name in names:
        name = resolve_name(name)
        # Handle different key formats
        keys_to_remove = [k for k in servers if k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "")]
        for key in keys_to_remove:
            del servers[key]
            print(f"  ⬚  Disabled: {name}")
    
    save_config(config)

def only_mcps(names):
    """Enable only these MCPs, disable everything else"""
    config = load_config()
    prior_servers = dict(config.get("mcpServers", {}))
    backup_servers = _load_backup_servers()
    config["mcpServers"] = {}
    
    for raw_name in names:
        name = resolve_name(raw_name)
        prior_matches = [
            k for k in prior_servers
            if k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "")
        ]
        backup_matches = [
            k for k in backup_servers
            if k.lower().replace("buildmcp", "") == name.lower().replace("buildmcp", "")
        ]
        if prior_matches:
            for k in prior_matches:
                config["mcpServers"][k] = prior_servers[k]
            print(f"  ✅ Enabled: {name}")
        elif backup_matches:
            for k in backup_matches:
                config["mcpServers"][k] = backup_servers[k]
            print(f"  ✅ Enabled: {name}")
        elif name in ALL_MCPS:
            key = "XcodeBuildMCP" if name == "xcode" else name
            config["mcpServers"][key] = ALL_MCPS[name]
            print(f"  ✅ Enabled: {name}")
        else:
            print(f"  ⚠️  Unknown MCP: {name}")
    
    save_config(config)

def apply_preset(preset_name):
    if preset_name not in PRESETS:
        print(f"Unknown preset: {preset_name}")
        print(f"Available: {', '.join(PRESETS.keys())}")
        return
    
    print(f"\n🎛️  Applying preset: {preset_name}")
    only_mcps(PRESETS[preset_name])

def _parse_args(argv):
    remaining = []
    it = iter(argv)
    for token in it:
        if token == "--config":
            try:
                set_config_path(next(it))
            except StopIteration:
                raise SystemExit("Usage: mcp-switch.py [--config PATH] <command> ...")
        elif token.startswith("--config="):
            set_config_path(token.split("=", 1)[1])
        else:
            remaining.append(token)
    return remaining

def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raw_args = _parse_args(sys.argv[1:] if argv is None else list(argv))
    if not raw_args:
        show_status()
        return
    
    cmd = raw_args[0].lower()
    args = raw_args[1:]
    
    if cmd == "list":
        print("\n📋 Available MCPs:")
        for name, config in ALL_MCPS.items():
            print(f"  • {name}")
        print("\n📋 Presets:")
        for name, mcps in PRESETS.items():
            print(f"  • {name}: {', '.join(mcps)}")
    
    elif cmd == "on":
        if not args:
            print("Usage: mcp-switch.py on <mcp1> <mcp2> ...")
            return
        print("\n🔌 Enabling MCPs:")
        enable_mcps(args)
    
    elif cmd == "off":
        if not args:
            print("Usage: mcp-switch.py off <mcp1> <mcp2> ...")
            return
        print("\n🔌 Disabling MCPs:")
        disable_mcps(args)
    
    elif cmd == "only":
        if not args:
            print("Usage: mcp-switch.py only <mcp1> <mcp2> ...")
            return
        print("\n🔌 Enabling ONLY these MCPs:")
        only_mcps(args)
    
    elif cmd == "preset":
        if not args:
            print("Available presets:", ", ".join(PRESETS.keys()))
            return
        apply_preset(args[0])
    
    else:
        print(__doc__)

if __name__ == "__main__":
    main()
