#!/usr/bin/env python3
"""One-command FlowState setup (Windows, macOS, Linux). Standard library only.

    python setup_flowstate.py --clients claude-desktop
    python setup_flowstate.py --clients auto --verify
    python setup_flowstate.py --clients cursor,codex --dry-run

What it does, in order (safe to re-run; it only changes what is out of date):
  1. checks Python >= 3.11
  2. creates .venv in this folder and installs the FlowState server into it
     (skip with --skip-install, e.g. when you manage Python yourself)
  3. creates the database if it doesn't exist; on an existing database it backs it up
     and then only adds new FlowState tables via numbered migrations (never deletes or
     changes your data)
  4. registers FlowState as an MCP server in each chosen client's config file,
     after backing that file up (<file>.flowstate-backup-<time>); other servers and
     settings are left exactly as they were; a file it can't parse is left untouched
  5. optionally (--offsite-github OWNER/NAME) checks that GitHub repo is PRIVATE and
     saves it for off-site backups; asks on a terminal, skip with --no-offsite
  6. optionally (--schedule-backups) adds a nightly local backup to the OS scheduler
  7. optionally (--install-skill, or asked on a terminal) installs the "flowstate"
     Agent Skill into ~/.agents/skills and ~/.claude/skills, backing up an existing copy
  8. optionally (--verify) starts the server the way the client will, and calls
     start_here over MCP to prove it answers

Clients: claude-desktop, cursor, windsurf, claude-code, codex, grok, or any JSON
config with an "mcpServers" object via --config PATH. "auto" = every client whose
config folder exists. "all" = every known client.
"""

import argparse
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
SERVER_DIR = REPO / "mcp-server"
SCHEMA = REPO / "database" / "schema.sql"
SKILL_SRC = REPO / "skills" / "flowstate"
SERVER_NAME = "flowstate"
V2_MARKER = "-- v2 PHASE A"
JSON_CLIENTS = ("claude-desktop", "cursor", "windsurf", "claude-code")
TOML_CLIENTS = ("codex", "grok")
ALL_CLIENTS = JSON_CLIENTS + TOML_CLIENTS


def say(msg: str) -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------ locations
class Places:
    """Every path the script touches, rooted at --home when given (used by tests)."""

    def __init__(self, home: "Path | None" = None, system: "str | None" = None):
        self.system = system or platform.system()
        self.fake = home is not None
        self.home = Path(home) if home else Path.home()

    def _win(self, var: str, *fallback: str) -> Path:
        if not self.fake and os.environ.get(var):
            return Path(os.environ[var])
        return self.home.joinpath(*fallback)

    @property
    def appdata(self) -> Path:
        return self._win("APPDATA", "AppData", "Roaming")

    @property
    def localappdata(self) -> Path:
        return self._win("LOCALAPPDATA", "AppData", "Local")

    def default_db(self) -> Path:
        if not self.fake:
            if os.environ.get("FLOWSTATE_DB"):
                return Path(os.environ["FLOWSTATE_DB"]).expanduser()
            if os.environ.get("FLOWSTATE_HOME"):
                return Path(os.environ["FLOWSTATE_HOME"]).expanduser() / "flowstate.db"
        if self.system == "Darwin":
            return self.home / "Library" / "Application Support" / "flowstate" / "flowstate.db"
        if self.system == "Windows":
            return self.localappdata / "flowstate" / "flowstate.db"
        return self.home / ".local" / "share" / "flowstate" / "flowstate.db"

    def config_path(self, client: str) -> Path:
        h = self.home
        if client == "claude-desktop":
            if self.system == "Windows":
                # Microsoft Store builds keep their config inside the package folder.
                pkgs = sorted((self.localappdata / "Packages").glob("Claude_*"))
                for p in pkgs:
                    store = p / "LocalCache" / "Roaming" / "Claude"
                    if store.is_dir():
                        return store / "claude_desktop_config.json"
                return self.appdata / "Claude" / "claude_desktop_config.json"
            if self.system == "Darwin":
                return h / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            return h / ".config" / "Claude" / "claude_desktop_config.json"
        if client == "cursor":
            return h / ".cursor" / "mcp.json"
        if client == "windsurf":
            return h / ".codeium" / "windsurf" / "mcp_config.json"
        if client == "claude-code":
            return h / ".claude.json"
        if client == "codex":
            return h / ".codex" / "config.toml"
        if client == "grok":
            return h / ".grok" / "config.toml"
        raise ValueError(f"unknown client {client}")

    def installed(self, client: str) -> bool:
        p = self.config_path(client)
        if client == "claude-code":
            return p.exists() or (self.home / ".claude").is_dir()
        return p.exists() or p.parent.is_dir()


# ------------------------------------------------------------------ python / venv
def venv_python(venv: Path) -> Path:
    if platform.system() == "Windows":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def ensure_install(dry: bool) -> Path:
    venv = REPO / ".venv"
    py = venv_python(venv)
    if not py.exists():
        say(f"[venv] creating {venv}")
        if not dry:
            subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    else:
        say(f"[venv] using {venv}")
    say("[install] pip install -e mcp-server (into the venv only)")
    if not dry:
        subprocess.run([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                        "-e", str(SERVER_DIR)], check=True)
    return py


# ------------------------------------------------------------------ database
def v2_block() -> str:
    text = SCHEMA.read_text(encoding="utf-8")
    i = text.find(V2_MARKER)
    if i < 0:
        raise SystemExit("schema.sql has no v2 block; is this a FlowState v2 checkout?")
    return text[text.rfind("\n", 0, i) + 1:]


def ensure_db(db: Path, dry: bool) -> str:
    if db.exists():
        say(f"[db] existing database kept: {db} (adding any missing v2 tables)")
        if not dry:
            conn = sqlite3.connect(str(db), timeout=10)
            try:
                conn.executescript(v2_block())
                conn.commit()
            finally:
                conn.close()
        return "migrated"
    say(f"[db] creating new database: {db}")
    if not dry:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db))
        try:
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
            conn.commit()
        finally:
            conn.close()
    return "created"


def tool_env(places: "Places") -> dict:
    """Environment for `python -m flowstate ...`; with --home, every folder is under it."""
    env = {**os.environ, "PYTHONPATH": str(SERVER_DIR)}
    if places.fake:
        env["FLOWSTATE_BACKUP_DIR"] = str(places.home / "flowstate-backups")
        env["FLOWSTATE_CONFIG_DIR"] = str(places.home / "flowstate-config")
    return env


def flowstate_cmd(py: Path, places: "Places", *args: str) -> int:
    """Run a FlowState maintenance command with the server's Python; its output is shown."""
    r = subprocess.run([str(py), "-m", "flowstate", *args], env=tool_env(places),
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (r.stdout + r.stderr).splitlines():
        if line.strip():
            say(f"    {line}")
    return r.returncode


def run_migrations(py: Path, db: Path, places: "Places", dry: bool) -> bool:
    """Numbered migrations (new tables only; a backup is taken first on a DB with data)."""
    say("[db] applying numbered migrations" + (" (dry run: listing only)" if dry else ""))
    if dry and not db.exists():
        return True
    return flowstate_cmd(py, places, "migrate", "--db", str(db),
                         *(["--dry-run"] if dry else [])) == 0


def setup_offsite(a, py: Path, places: "Places") -> bool:
    """Opt-in off-site backup to a PRIVATE GitHub repo (checked before saving)."""
    repo = a.offsite_github
    if (not repo and not a.no_offsite and not a.dry_run
            and sys.stdin.isatty() and sys.stdout.isatty()):
        say("[offsite] FlowState can also push a nightly backup to a PRIVATE GitHub repo you own")
        say("          (it checks the repo is private before every push; uses your gh/git login).")
        try:
            repo = input("          GitHub repo as owner/name, or Enter to skip: ").strip()
        except EOFError:
            repo = ""
    if not repo:
        say("[offsite] off-site backup not set up (add later: --offsite-github OWNER/NAME)")
        return True
    if a.dry_run:
        say(f"[offsite] would check github.com/{repo} is private and save it (dry run)")
        return True
    ok = flowstate_cmd(py, places, "offsite", "setup", "--repo", repo) == 0
    say(f"[offsite] {'set up' if ok else 'NOT set up'}")
    return ok


def setup_schedule(a, py: Path, db: Path, places: "Places", offsite: bool) -> bool:
    args = ["schedule-backups", "--db", str(db), "--time", a.backup_time, "--python", str(py)]
    if offsite:
        args.append("--offsite")
    if a.dry_run:
        args.append("--dry-run")
    say("[backups] scheduling the nightly backup" + (" (dry run)" if a.dry_run else ""))
    return flowstate_cmd(py, places, *args) == 0


# ------------------------------------------------------------------ agent skill
def skill_targets(places: "Places") -> list:
    """~/.agents/skills reaches Codex, Cursor, Grok Build, Copilot, Gemini CLI and others;
    Claude Code reads ~/.claude/skills."""
    return [places.home / ".agents" / "skills" / "flowstate",
            places.home / ".claude" / "skills" / "flowstate"]


def _tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def install_skill_to(dest: Path, dry: bool) -> str:
    """Copy skills/flowstate to dest. An existing different copy is moved to
    <dest's parent's parent>/flowstate-skill-backups/ first (outside the skills folder, so
    the app doesn't load two copies)."""
    if dest.exists() and dest.is_dir() and _tree(dest) == _tree(SKILL_SRC):
        return "unchanged"
    if dry:
        return "would be " + ("updated" if dest.exists() else "installed") + " (dry run)"
    saved = None
    if dest.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bdir = dest.parent.parent / "flowstate-skill-backups"
        bdir.mkdir(parents=True, exist_ok=True)
        saved = bdir / f"flowstate-{stamp}"
        n = 1
        while saved.exists():
            saved = bdir / f"flowstate-{stamp}-{n}"
            n += 1
        shutil.move(str(dest), str(saved))
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SKILL_SRC, dest)
    return ("updated" if saved else "installed") + (f" (old copy backed up to {saved})" if saved else "")


def setup_skill(a, places: "Places") -> bool:
    want = a.install_skill
    if (not want and not a.no_skill and not a.dry_run
            and sys.stdin.isatty() and sys.stdout.isatty()):
        say("[skill] The FlowState skill teaches your AI apps the FlowState habits (start with")
        say("        the brief, log to the right project, never approve its own review items).")
        try:
            want = input("        Install it to ~/.agents/skills and ~/.claude/skills? [Y/n]: "
                         ).strip().lower() in ("", "y", "yes")
        except EOFError:
            want = False
    if not want:
        say("[skill] not installed (add later: --install-skill)")
        return True
    if not (SKILL_SRC / "SKILL.md").exists():
        say(f"[skill] FAILED: {SKILL_SRC / 'SKILL.md'} is missing")
        return False
    for dest in skill_targets(places):
        say(f"[skill] {dest}: {install_skill_to(dest, a.dry_run)}")
    say("[skill] Claude apps / ChatGPT: upload the skills/flowstate folder as a ZIP in their "
        "Skills settings")
    return True


# ------------------------------------------------------------------ config edits
def server_entry(py: Path, db: Path, client: str) -> dict:
    entry = {
        "command": str(py),
        "args": ["-m", "flowstate"],
        # PYTHONPATH as well as the editable install: some clients ignore cwd.
        "env": {"FLOWSTATE_DB": str(db), "PYTHONPATH": str(SERVER_DIR)},
    }
    if client == "claude-code":
        entry = {"type": "stdio", **entry}
    return entry


def backup(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.flowstate-backup-{stamp}")
    n = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.flowstate-backup-{stamp}-{n}")
        n += 1
    shutil.copy2(path, dest)
    return dest


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".flowstate-tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def register_json(path: Path, entry: dict, dry: bool) -> str:
    data: dict = {}
    if path.exists() and path.read_text(encoding="utf-8-sig").strip():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as e:
            return f"refused: {path} is not valid JSON ({e.msg} at line {e.lineno}); left untouched"
        if not isinstance(data, dict):
            return f"refused: {path} is not a JSON object; left untouched"
    servers = data.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return f"refused: {path} has a non-object mcpServers; left untouched"
    if (servers or {}).get(SERVER_NAME) == entry:
        return "unchanged"
    action = "updated" if SERVER_NAME in (servers or {}) else "added"
    if dry:
        return f"would be {action} (dry run)"
    data.setdefault("mcpServers", {})[SERVER_NAME] = entry
    saved = backup(path) if path.exists() else None
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return action + (f" (backup: {saved.name})" if saved else " (new file)")


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # JSON string escapes are valid TOML basic strings


def toml_block(entry: dict) -> str:
    env = ", ".join(f"{k} = {_toml_str(v)}" for k, v in entry["env"].items())
    args = ", ".join(_toml_str(a) for a in entry["args"])
    return (f"[mcp_servers.{SERVER_NAME}]\n"
            f"command = {_toml_str(entry['command'])}\n"
            f"args = [{args}]\n"
            f"env = {{ {env} }}\n")


_HEADER = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*"?' + SERVER_NAME + r'"?\s*(\.[^\]]*)?\]\s*(#.*)?$')
_ANY_HEADER = re.compile(r"^\s*\[")


def _strip_toml_server(text: str) -> tuple[str, bool]:
    """Remove [mcp_servers.flowstate] and its sub-tables. Returns (text, found)."""
    out, skipping, found = [], False, False
    for line in text.splitlines(keepends=True):
        if _HEADER.match(line):
            skipping, found = True, True
            continue
        if skipping and _ANY_HEADER.match(line):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out), found


def register_toml(path: Path, entry: dict, dry: bool) -> str:
    import tomllib
    text = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return f"refused: {path} is not valid TOML ({e}); left untouched"
    have = (current.get("mcp_servers") or {}).get(SERVER_NAME)
    want = {"command": entry["command"], "args": entry["args"], "env": entry["env"]}
    if have == want:
        return "unchanged"
    stripped, found = _strip_toml_server(text)
    if have is not None and not found:
        return (f"refused: {path} defines mcp_servers.{SERVER_NAME} in a form this script "
                f"doesn't edit; update it by hand (see AGENTS.md); left untouched")
    new = stripped.rstrip("\n") + ("\n\n" if stripped.strip() else "") + toml_block(entry)
    try:
        parsed = tomllib.loads(new)
    except tomllib.TOMLDecodeError as e:
        return f"refused: the edited {path} would not parse ({e}); left untouched"
    if (parsed.get("mcp_servers") or {}).get(SERVER_NAME) != want:
        return f"refused: could not write a clean entry into {path}; left untouched"
    action = "updated" if have is not None else "added"
    if dry:
        return f"would be {action} (dry run)"
    saved = backup(path) if path.exists() else None
    atomic_write(path, new)
    return action + (f" (backup: {saved.name})" if saved else " (new file)")


# ------------------------------------------------------------------ verify
def verify(entry: dict, timeout: float = 30.0) -> tuple[bool, str]:
    """Start the server exactly as a client would and call start_here over MCP stdio."""
    env = {**os.environ, **entry["env"]}
    try:
        proc = subprocess.Popen([entry["command"], *entry["args"]], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
                                text=True, encoding="utf-8")
    except OSError as e:
        return False, f"could not start the server: {e}"
    lines: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=lambda: [lines.put(l) for l in proc.stdout], daemon=True).start()

    def send(msg: dict) -> None:
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def wait_for(msg_id: int) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = json.loads(lines.get(timeout=max(0.1, end - time.time())))
            except queue.Empty:
                break
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                return msg
        raise TimeoutError("no reply from the server")

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "flowstate-setup-verify", "version": "1"}}})
        init = wait_for(1)
        if "instructions" not in init.get("result", {}):
            return False, "server answered but sent no instructions"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": "start_here", "arguments": {}}})
        res = wait_for(2).get("result", {})
        text = (res.get("content") or [{}])[0].get("text", "")
        if res.get("isError"):
            return False, f"start_here returned an error: {text[:200]}"
        return True, text.splitlines()[0] if text else "ok"
    except (TimeoutError, OSError, BrokenPipeError) as e:
        return False, str(e)
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            proc.kill()


# ------------------------------------------------------------------ main
def pick_clients(spec: str, places: Places) -> list:
    if spec == "auto":
        return [c for c in ALL_CLIENTS if places.installed(c)]
    if spec == "all":
        return list(ALL_CLIENTS)
    if spec in ("", "none"):
        return []
    chosen = [c.strip() for c in spec.split(",") if c.strip()]
    bad = [c for c in chosen if c not in ALL_CLIENTS]
    if bad:
        raise SystemExit(f"unknown client(s): {', '.join(bad)}. Known: {', '.join(ALL_CLIENTS)}")
    return chosen


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Set up FlowState and register it in your AI apps.")
    ap.add_argument("--clients", default="auto",
                    help="auto | all | none | comma list of: " + ", ".join(ALL_CLIENTS))
    ap.add_argument("--config", action="append", default=[],
                    help="extra JSON config file with an mcpServers object (repeatable)")
    ap.add_argument("--db", help="database file (default: FLOWSTATE_DB or the per-OS default)")
    ap.add_argument("--python", help="use this Python for the server instead of .venv")
    ap.add_argument("--skip-install", action="store_true", help="don't create .venv / pip install")
    ap.add_argument("--home", help="treat this folder as the home folder (testing)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    ap.add_argument("--verify", action="store_true", help="start the server and call start_here")
    ap.add_argument("--schedule-backups", action="store_true",
                    help="add a nightly local backup to the OS scheduler")
    ap.add_argument("--backup-time", default="02:30", help="time for the nightly backup (HH:MM)")
    ap.add_argument("--offsite-github", metavar="OWNER/NAME",
                    help="also back up nightly to this PRIVATE GitHub repo (opt-in)")
    ap.add_argument("--no-offsite", action="store_true", help="don't ask about GitHub backups")
    ap.add_argument("--install-skill", action="store_true",
                    help="install the flowstate Agent Skill to ~/.agents/skills and ~/.claude/skills")
    ap.add_argument("--no-skill", action="store_true", help="don't ask about the Agent Skill")
    a = ap.parse_args(argv)

    if sys.version_info < (3, 11):
        say(f"FlowState needs Python 3.11 or newer; this is {platform.python_version()}.")
        return 2
    places = Places(Path(a.home) if a.home else None)
    say(f"FlowState setup ({places.system}, Python {platform.python_version()})")

    if a.python:
        py = Path(a.python)
    elif a.skip_install:
        cand = venv_python(REPO / ".venv")
        py = cand if cand.exists() else Path(sys.executable)
    else:
        py = ensure_install(a.dry_run)
    say(f"[python] server runs with {py}")

    db = Path(a.db).expanduser().resolve() if a.db else places.default_db()
    ensure_db(db, a.dry_run)
    failures = 0
    if not run_migrations(py, db, places, a.dry_run):
        say("[db] FAILED to apply migrations (see above); your data was not changed")
        failures += 1
    targets = [(c, places.config_path(c)) for c in pick_clients(a.clients, places)]
    targets += [("custom", Path(p).expanduser()) for p in a.config]
    if not targets:
        say("[clients] none chosen; nothing registered")
    for client, path in targets:
        entry = server_entry(py, db, client)
        if client in TOML_CLIENTS:
            result = register_toml(path, entry, a.dry_run)
        else:
            result = register_json(path, entry, a.dry_run)
        failures += result.startswith("refused")
        say(f"[{client}] {path}: {result}")

    offsite_ok = setup_offsite(a, py, places)
    failures += not offsite_ok
    if a.schedule_backups:
        failures += not setup_schedule(a, py, db, places, bool(a.offsite_github) and offsite_ok)
    else:
        say("[backups] nightly backup not scheduled (add --schedule-backups, or run "
            "`flowstate schedule-backups` later)")

    failures += not setup_skill(a, places)

    if a.verify and not a.dry_run:
        ok, detail = verify(server_entry(py, db, "generic"))
        say(f"[verify] {'OK' if ok else 'FAILED'}: {detail}")
        failures += not ok

    say("Done. Restart each app you registered (fully quit it, then reopen) so it loads FlowState."
        if not failures else "Finished with problems; see the lines marked refused/FAILED above.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
