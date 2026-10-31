#!/usr/bin/env python3
"""
mcp-toggle - CLI tool to enable/disable MCP servers in Claude Desktop

Usage:
    mcp-toggle list                  # Show all MCPs with status
    mcp-toggle enable <name|num>     # Enable an MCP server
    mcp-toggle disable <name|num>    # Disable an MCP server
    mcp-toggle only <name|num>       # Enable only specified MCP(s)
    mcp-toggle preset <name>         # Apply a preset configuration
    mcp-toggle presets               # List available presets
    mcp-toggle restore               # Restore all from backup
    mcp-toggle backup                # Create manual backup

Disabling works by renaming the key with _DISABLED suffix.
"""

import json
import sys
import shutil
from pathlib import Path
from datetime import datetime

# Configuration
CONFIG_PATH = Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
BACKUP_PATH = CONFIG_PATH.parent / "claude_desktop_config.backup.json"
PRESETS_PATH = Path(__file__).parent / "mcp_presets.json"

# Default presets
DEFAULT_PRESETS = {
    "ios": {
        "description": "iOS development - Xcode + FlowState + GitHub",
        "enable": ["XcodeBuildMCP", "flowstate", "github"],
        "disable_others": True
    },
    "python": {
        "description": "Python development - FlowState + GitHub",
        "enable": ["flowstate", "github"],
        "disable_others": True
    },
    "minimal": {
        "description": "Minimal setup - FlowState only",
        "enable": ["flowstate"],
        "disable_others": True
    },
    "all": {
        "description": "Enable all MCP servers",
        "enable": ["*"],
        "disable_others": False
    }
}

# Colors for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    BOLD = '\033[1m'
    END = '\033[0m'

def color(text, c):
    """Apply color if terminal supports it"""
    if sys.stdout.isatty():
        return f"{c}{text}{Colors.END}"
    return text

def load_config():
    """Load Claude Desktop config"""
    if not CONFIG_PATH.exists():
        print(color(f"Error: Config not found at {CONFIG_PATH}", Colors.RED))
        sys.exit(1)
    
    with open(CONFIG_PATH, 'r') as f:
        return json.load(f)

def save_config(config):
    """Save Claude Desktop config"""
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2)

def ensure_backup():
    """Create backup if it doesn't exist"""
    if not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print(color(f"Created backup at {BACKUP_PATH}", Colors.BLUE))

def load_presets():
    """Load presets from file or use defaults"""
    if PRESETS_PATH.exists():
        with open(PRESETS_PATH, 'r') as f:
            return json.load(f)
    return DEFAULT_PRESETS

def save_presets(presets):
    """Save presets to file"""
    with open(PRESETS_PATH, 'w') as f:
        json.dump(presets, f, indent=2)


def get_mcp_servers(config):
    """Get list of MCP servers (both enabled and disabled)"""
    servers = []
    mcp_servers = config.get("mcpServers", {})
    
    for key, value in mcp_servers.items():
        is_disabled = key.endswith("_DISABLED")
        name = key[:-9] if is_disabled else key  # Remove _DISABLED suffix
        servers.append({
            "key": key,
            "name": name,
            "enabled": not is_disabled,
            "config": value
        })
    
    # Sort by name for consistent ordering
    servers.sort(key=lambda x: x["name"].lower())
    return servers

def find_server(servers, identifier):
    """Find server by name or number"""
    # Try as number first
    try:
        num = int(identifier)
        if 1 <= num <= len(servers):
            return servers[num - 1]
    except ValueError:
        pass
    
    # Try as name (case-insensitive partial match)
    identifier_lower = identifier.lower()
    for server in servers:
        if server["name"].lower() == identifier_lower:
            return server
        if identifier_lower in server["name"].lower():
            return server
    
    return None

def cmd_list(config):
    """List all MCP servers with status"""
    servers = get_mcp_servers(config)
    
    if not servers:
        print("No MCP servers configured.")
        return
    
    print(color("\nMCP Servers:", Colors.BOLD))
    print("-" * 50)
    
    enabled_count = 0
    for i, server in enumerate(servers, 1):
        if server["enabled"]:
            status = color("●", Colors.GREEN)
            enabled_count += 1
        else:
            status = color("○", Colors.RED)
        
        name = server["name"]
        print(f"  {i:2}. {status} {name}")
    
    print("-" * 50)
    print(f"Total: {len(servers)} | Enabled: {color(str(enabled_count), Colors.GREEN)} | Disabled: {color(str(len(servers) - enabled_count), Colors.RED)}")
    print(color("\nRestart Claude Desktop for changes to take effect.", Colors.YELLOW))


def cmd_enable(config, identifier):
    """Enable an MCP server"""
    servers = get_mcp_servers(config)
    server = find_server(servers, identifier)
    
    if not server:
        print(color(f"Error: Server '{identifier}' not found", Colors.RED))
        return False
    
    if server["enabled"]:
        print(f"'{server['name']}' is already enabled")
        return True
    
    ensure_backup()
    
    # Rename key (remove _DISABLED suffix)
    mcp_servers = config["mcpServers"]
    mcp_servers[server["name"]] = mcp_servers.pop(server["key"])
    
    save_config(config)
    print(color(f"✓ Enabled '{server['name']}'", Colors.GREEN))
    return True

def cmd_disable(config, identifier):
    """Disable an MCP server"""
    servers = get_mcp_servers(config)
    server = find_server(servers, identifier)
    
    if not server:
        print(color(f"Error: Server '{identifier}' not found", Colors.RED))
        return False
    
    if not server["enabled"]:
        print(f"'{server['name']}' is already disabled")
        return True
    
    ensure_backup()
    
    # Rename key (add _DISABLED suffix)
    mcp_servers = config["mcpServers"]
    mcp_servers[f"{server['name']}_DISABLED"] = mcp_servers.pop(server["key"])
    
    save_config(config)
    print(color(f"✓ Disabled '{server['name']}'", Colors.GREEN))
    return True


def cmd_only(config, identifiers):
    """Enable only specified servers, disable all others"""
    servers = get_mcp_servers(config)
    
    # Find all specified servers
    to_enable = []
    for identifier in identifiers:
        server = find_server(servers, identifier)
        if not server:
            print(color(f"Error: Server '{identifier}' not found", Colors.RED))
            return False
        to_enable.append(server["name"])
    
    ensure_backup()
    
    mcp_servers = config["mcpServers"]
    new_servers = {}
    
    for server in servers:
        if server["name"] in to_enable:
            # Enable this server
            new_servers[server["name"]] = server["config"]
        else:
            # Disable this server
            new_servers[f"{server['name']}_DISABLED"] = server["config"]
    
    config["mcpServers"] = new_servers
    save_config(config)
    
    print(color(f"✓ Enabled only: {', '.join(to_enable)}", Colors.GREEN))
    print(color(f"  Disabled: {len(servers) - len(to_enable)} others", Colors.YELLOW))
    return True

def cmd_preset(config, preset_name):
    """Apply a preset configuration"""
    presets = load_presets()
    
    if preset_name not in presets:
        print(color(f"Error: Preset '{preset_name}' not found", Colors.RED))
        print(f"Available presets: {', '.join(presets.keys())}")
        return False
    
    preset = presets[preset_name]
    servers = get_mcp_servers(config)
    
    ensure_backup()
    
    mcp_servers = config["mcpServers"]
    new_servers = {}
    
    enable_list = preset["enable"]
    enable_all = "*" in enable_list
    
    for server in servers:
        should_enable = enable_all or any(
            e.lower() in server["name"].lower() for e in enable_list
        )
        
        if should_enable:
            new_servers[server["name"]] = server["config"]
        elif preset.get("disable_others", True):
            new_servers[f"{server['name']}_DISABLED"] = server["config"]
        else:
            # Keep current state
            new_servers[server["key"]] = server["config"]
    
    config["mcpServers"] = new_servers
    save_config(config)
    
    print(color(f"✓ Applied preset: {preset_name}", Colors.GREEN))
    print(f"  {preset['description']}")
    return True


def cmd_presets():
    """List available presets"""
    presets = load_presets()
    
    print(color("\nAvailable Presets:", Colors.BOLD))
    print("-" * 50)
    
    for name, preset in presets.items():
        print(f"  {color(name, Colors.BLUE)}: {preset['description']}")
        if preset["enable"] != ["*"]:
            print(f"    Enables: {', '.join(preset['enable'])}")
    
    print("-" * 50)

def cmd_restore(config):
    """Restore from backup"""
    if not BACKUP_PATH.exists():
        print(color("Error: No backup found", Colors.RED))
        return False
    
    shutil.copy2(BACKUP_PATH, CONFIG_PATH)
    print(color("✓ Restored from backup", Colors.GREEN))
    return True

def cmd_backup():
    """Create manual backup"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = CONFIG_PATH.parent / f"claude_desktop_config.{timestamp}.backup.json"
    shutil.copy2(CONFIG_PATH, backup_path)
    print(color(f"✓ Backup created: {backup_path}", Colors.GREEN))
    return True

def print_usage():
    """Print usage information"""
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    # Commands that don't need config loaded first
    if command == "presets":
        cmd_presets()
        return
    
    if command == "help" or command == "--help" or command == "-h":
        print_usage()
        return
    
    # Load config for other commands
    config = load_config()
    
    if command == "list" or command == "ls":
        cmd_list(config)
    
    elif command == "enable":
        if len(sys.argv) < 3:
            print("Usage: mcp-toggle enable <name|number>")
            sys.exit(1)
        cmd_enable(config, sys.argv[2])
    
    elif command == "disable":
        if len(sys.argv) < 3:
            print("Usage: mcp-toggle disable <name|number>")
            sys.exit(1)
        cmd_disable(config, sys.argv[2])
    
    elif command == "only":
        if len(sys.argv) < 3:
            print("Usage: mcp-toggle only <name|number> [name|number] ...")
            sys.exit(1)
        cmd_only(config, sys.argv[2:])
    
    elif command == "preset":
        if len(sys.argv) < 3:
            print("Usage: mcp-toggle preset <n>")
            cmd_presets()
            sys.exit(1)
        cmd_preset(config, sys.argv[2])
    
    elif command == "restore":
        cmd_restore(config)
    
    elif command == "backup":
        cmd_backup()
    
    else:
        print(color(f"Unknown command: {command}", Colors.RED))
        print_usage()
        sys.exit(1)

if __name__ == "__main__":
    main()
