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
CONFIG_PATH = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
BACKUP_PATH = Path.home() / "Library/Application Support/Claude/claude_desktop_config.backup.json"

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
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return json.load(f)
    return {"mcpServers": {}}

def save_config(config):
    # Backup first
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            backup = f.read()
        with open(BACKUP_PATH, 'w') as f:
            f.write(backup)
    
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"\n✅ Config saved. Backup at: {BACKUP_PATH}")
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
    
    for name in names:
        name = resolve_name(name)
        if name in ALL_MCPS:
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
    config = {"mcpServers": {}}
    
    for name in names:
        name = resolve_name(name)
        if name in ALL_MCPS:
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

def main():
    if len(sys.argv) < 2:
        show_status()
        return
    
    cmd = sys.argv[1].lower()
    args = sys.argv[2:]
    
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
