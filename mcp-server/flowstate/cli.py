"""FlowState command line (besides running the server):

    flowstate migrate           [--db FILE]
    flowstate backup            [--db FILE] [--dir DIR] [--offsite]
    flowstate restore BACKUP    [--to NEW_FILE] [--force]
    flowstate offsite setup     --repo OWNER/NAME [--branch NAME]
    flowstate offsite status | push [--dry-run] | disable
    flowstate schedule-backups  [--time HH:MM] [--offsite] [--remove] [--dry-run]
    flowstate token status | migrate --from FILE
    flowstate scan-secrets      [--db FILE]
    flowstate remote            status | passphrase | add-client --name N [--read-only] |
                                clients | revoke --client ID | allow-redirect --name HOST
    flowstate keys              add --name N [--read-only] [--workspace W] | list |
                                revoke --id ID | --name N   (per-app LAN keys: app_keys.py)
    flowstate relay             enroll --code C | run | status | renew | enable | disable |
                                forget | ct-check | ct-ack   (optional, off by default: relay/)
    (Phase D commands: see cli_more.py)

Plain-language output; exit code 0 = OK, 1 = failed / refused. Never prints a token.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import paths

from . import cli_more
from . import cli_review

COMMANDS = ("migrate", "backup", "restore", "offsite", "schedule-backups", "token",
            "scan-secrets", "remote", "keys", "relay") + cli_more.COMMANDS + cli_review.COMMANDS


def say(msg: str) -> None:
    print(msg, flush=True)


def _default_db() -> Path:
    from .database import default_db_path
    return default_db_path()


def _db(a) -> Path:
    return Path(a.db).expanduser() if getattr(a, "db", None) else _default_db()


def _offsite_cfg() -> dict:
    return paths.load_config().get("offsite") or {}


# ---------------------------------------------------------------- commands
def cmd_migrate(a) -> int:
    from .migrations import migrate, pending
    db = _db(a)
    if not db.is_file():
        say(f"No database at {db}.")
        return 1
    todo = pending(db)
    if not todo:
        say(f"{db}: up to date, nothing to migrate.")
        return 0
    say(f"{db}: {len(todo)} migration(s) to apply: " + ", ".join(f"{v:03d}_{n}" for v, n, _ in todo))
    if a.dry_run:
        return 0
    res = migrate(db, Path(a.dir) if a.dir else None, log=say)
    say("Done. Existing data was not changed; only new tables or new empty columns were added.")
    return 0 if res["applied"] or not pending(db) else 1


def cmd_backup(a) -> int:
    from .backup import BackupError, backup_now
    db = _db(a)
    try:
        r = backup_now(db, Path(a.dir) if a.dir else None, prune=not a.no_prune)
    except BackupError as e:
        say(f"Backup FAILED: {e}")
        return 1
    say(f"Backup OK: {r['path']} ({r['size'] // 1024} KB, integrity ok)")
    if r["removed"]:
        say(f"Old backups removed by the 14-daily / 8-weekly rule: {len(r['removed'])}")
    if a.offsite:
        return _push(db, dry_run=False)
    return 0


def _push(db: Path, dry_run: bool) -> int:
    from .offsite import OffsiteError, push_backup
    cfg = _offsite_cfg()
    if not cfg.get("repo"):
        say("Off-site backup is not set up (flowstate offsite setup --repo OWNER/NAME).")
        return 1
    try:
        r = push_backup(db, cfg["repo"], branch=cfg.get("branch") or "flowstate-backup",
                        dry_run=dry_run)
    except OffsiteError as e:
        say(f"Off-site backup REFUSED/FAILED: {e}")
        return 1
    if r.get("dry_run"):
        say(f"Off-site dry run: github.com/{r['repo']} is private. Nothing was pushed.")
    else:
        say(f"Off-site backup pushed to github.com/{r['repo']} branch {r['branch']} "
            f"(private; files: {', '.join(r['files'])}).")
    return 0


def cmd_restore(a) -> int:
    from .backup import BackupError, restore
    src = Path(a.backup).expanduser()
    if a.to:
        target = Path(a.to).expanduser()
    else:
        target = paths.backup_dir() / "restored" / f"flowstate-restored-{datetime.now():%Y%m%d-%H%M%S}.db"
    try:
        r = restore(src, target, force=a.force)
    except BackupError as e:
        say(f"Restore REFUSED/FAILED: {e}")
        return 1
    say(f"Restored and verified: {r['restored_to']} ({r['projects']} projects, {r['tables']} tables).")
    if r["pre_restore_backup"]:
        say(f"The file it replaced was backed up first: {r['pre_restore_backup']}")
    else:
        say("Your live database was not touched. To use the restored copy, point FLOWSTATE_DB at it.")
    return 0


def cmd_offsite(a) -> int:
    from .offsite import OffsiteError, check_repo_name, require_private
    if a.action == "setup":
        try:
            repo = check_repo_name(a.repo or "")
            require_private(repo)
        except OffsiteError as e:
            say(f"Not set up: {e}")
            return 1
        cfg = paths.load_config()
        cfg["offsite"] = {"repo": repo, "branch": a.branch or "flowstate-backup"}
        p = paths.save_config(cfg)
        say(f"Off-site backups will go to github.com/{repo} (private), branch "
            f"{cfg['offsite']['branch']}. Saved in {p}. Push now with: flowstate backup --offsite")
        return 0
    if a.action == "disable":
        cfg = paths.load_config()
        cfg.pop("offsite", None)
        paths.save_config(cfg)
        say("Off-site backups turned off (nothing on GitHub was deleted).")
        return 0
    if a.action == "status":
        cfg = _offsite_cfg()
        say(f"Off-site: github.com/{cfg['repo']} branch {cfg.get('branch')}" if cfg.get("repo")
            else "Off-site backup: not set up.")
        return 0
    return _push(_db(a), dry_run=a.dry_run)


def cmd_schedule(a) -> int:
    from .scheduler import schedule
    db = str(_db(a).resolve())
    if a.offsite and not _offsite_cfg().get("repo"):
        if not a.dry_run:
            say("Set up off-site first: flowstate offsite setup --repo OWNER/NAME")
            return 1
        say("(dry run) off-site isn't set up yet; the nightly push would need it.")
    r = schedule(a.python or sys.executable, db, a.time, a.offsite, a.remove, a.dry_run, a.dir)
    verb = "Would" if a.dry_run else "Did"
    say(f"{verb} {'remove' if a.remove else 'schedule'} the nightly backup "
        f"({r['scheduler']}, {r['time']}): {r['command']}")
    for c in r["commands"]:
        say(f"  {c}")
    if r.get("errors"):
        for e in r["errors"]:
            say(f"  FAILED: {e}")
        return 1
    return 0


def cmd_token(a) -> int:
    from .secret_store import StoreError, default_store, migrate_token
    store = default_store()
    if a.action == "status":
        try:
            tok = store.get()
        except Exception as e:  # noqa: BLE001
            say(f"{store.name}: could not be read ({type(e).__name__}).")
            return 1
        say(f"{store.name}: " + (f"a FlowState token is stored ({len(tok)} characters)." if tok
                                 else "no FlowState token stored."))
        return 0
    if not a.source:
        say("Give the token file: flowstate token migrate --from <path to http-token.txt>")
        return 1
    try:
        r = migrate_token(Path(a.source), store)
    except (StoreError, OSError) as e:
        say(f"Token NOT migrated: {e}")
        return 1
    say(f"Token copied into {r['stored_in']} ({r['length']} characters; not shown) and read back OK.")
    say(f"{r['source_left_in_place']} was left in place. Once the server starts without "
        f"--token-file, delete that file yourself.")
    return 0


_SCAN_TABLES = {
    "learnings": ["insight", "context"], "changes": ["field_name", "old_value", "new_value", "reason"],
    "problems": ["title", "description"], "todos": ["title", "description"],
    "conversations": ["user_prompt_summary", "assistant_response_summary", "key_decisions"],
    "sessions": ["summary"], "solutions": ["summary", "key_insight", "code_snippet"],
    "solution_attempts": ["description", "notes"], "project_variables": ["name", "value", "description"],
    "project_methods": ["description", "code_example"], "projects": ["name", "description"],
    "review_items": ["summary", "details"],
}


def cmd_scan(a) -> int:
    from .backup import _ro
    from .secrets_scan import scan_text
    db = _db(a)
    conn = _ro(db)
    hits = []
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, cols in _SCAN_TABLES.items():
            if table not in have:
                continue
            real = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
            cols = [c for c in cols if c in real]
            if not cols:
                continue
            for row in conn.execute(f'SELECT id, {", ".join(cols)} FROM "{table}"'):
                for col, val in zip(cols, row[1:]):
                    kind = scan_text(val) if isinstance(val, str) else None
                    if kind:
                        hits.append((table, row[0], col, kind))
    finally:
        conn.close()
    for t, i, c, k in hits:
        say(f"{t} #{i} {c}: looks like a {k}")
    say(f"{len(hits)} possible secret(s) found (values not shown).")
    return 0


def _when(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def cmd_remote(a) -> int:
    """Remote access: owner passphrase, per-app tokens, OAuth apps, redirect allow-list."""
    from . import remote_auth as RA
    store = RA.AuthStore()
    if a.action == "status":
        say(f"Settings file: {store.path}")
        say(f"Owner passphrase: {'set' if store.has_passphrase() else 'NOT set (OAuth apps cannot be approved)'}")
        live = [c for c in store.list_clients() if not c["revoked_at"]]
        say(f"Apps with access: {len(live)} (see `flowstate remote clients`)")
        say("OAuth redirect hosts allowed: " + ", ".join(RA.redirect_hosts()))
        return 0
    if a.action == "passphrase":
        import getpass
        if a.source:
            pw = Path(a.source).expanduser().read_text(encoding="utf-8").strip()
        else:
            pw = getpass.getpass("New owner passphrase (12+ characters): ")
            if pw != getpass.getpass("Again: "):
                say("The two entries differ; nothing changed.")
                return 1
        try:
            store.set_passphrase(pw)
        except ValueError as e:
            say(f"Not set: {e}")
            return 1
        say("Owner passphrase set (stored as a one-way hash). You type it on the FlowState "
            "page when you connect a new app.")
        return 0
    if a.action == "add-client":
        if not a.name:
            say("Give the app a name: flowstate remote add-client --name \"Cursor laptop\"")
            return 1
        scopes = [RA.SCOPE_READ] if a.read_only else list(RA.SCOPES)
        cid, token = store.add_token_client(a.name, scopes, a.days)
        if a.to_file:
            p = Path(a.to_file).expanduser()
            if paths.inside_git_worktree(p.parent):
                store.revoke_client(cid)
                say(f"Refused: {p.parent} is inside a git folder. Nothing was created.")
                return 1
            p.write_text(token + "\n", encoding="utf-8")
            say(f"App '{a.name}' added as {cid} ({' '.join(scopes)}). Token written to {p}; "
                f"paste it into the app, then delete the file.")
        else:
            say(f"App '{a.name}' added as {cid} ({' '.join(scopes)}).")
            say("Its token is shown ONCE below. Paste it into the app as the header "
                "'Authorization: Bearer <token>'. Don't save it in a synced or git folder.")
            say(token)
        return 0
    if a.action == "clients":
        rows = store.list_clients()
        if not rows:
            say("No apps have been given access yet.")
        for c in rows:
            state = "REVOKED " + _when(c["revoked_at"]) if c["revoked_at"] else \
                ("waiting for approval" if not c["approved_at"] else "active")
            say(f"{c['id']}  {c['name']!r}  {c['kind']}  {c['scopes']}  added {_when(c['created_at'])}"
                f"  last used {_when(c['last_used_at'])}  {state}")
        return 0
    if a.action == "revoke":
        if not a.client_id:
            say("Which app? flowstate remote revoke --client <id from `flowstate remote clients`>")
            return 1
        ok = store.revoke_client(a.client_id)
        say(f"{a.client_id}: access revoked; its tokens stop working now." if ok
            else f"{a.client_id}: no such active app.")
        return 0 if ok else 1
    if a.action == "allow-redirect":
        host = (a.name or "").strip().lower()
        if not host or "/" in host or ":" in host:
            say("Give a bare host name, e.g. flowstate remote allow-redirect --name grok.com")
            return 1
        cfg = paths.load_config()
        hosts = cfg.setdefault("remote", {}).setdefault("redirect_hosts", [])
        if host not in hosts:
            hosts.append(host)
            paths.save_config(cfg)
        say(f"OAuth apps may now return to https://{host}/...")
        return 0
    return 1


# ---------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="flowstate", description="FlowState maintenance commands")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("migrate", help="apply new database migrations (backup first)")
    p.add_argument("--db")
    p.add_argument("--dir", help="where the pre-migration backup goes")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_migrate)

    p = sub.add_parser("backup", help="back up the database now")
    p.add_argument("--db")
    p.add_argument("--dir", help="backups folder (default: per-OS, or FLOWSTATE_BACKUP_DIR)")
    p.add_argument("--offsite", action="store_true", help="also push to the private GitHub repo")
    p.add_argument("--no-prune", action="store_true", help="keep every old backup")
    p.set_defaults(fn=cmd_backup)

    p = sub.add_parser("restore", help="restore a backup into a new file (verified)")
    p.add_argument("backup")
    p.add_argument("--to", help="new file to create (default: in the backups folder)")
    p.add_argument("--force", action="store_true",
                   help="allow replacing an existing file (it is backed up first)")
    p.set_defaults(fn=cmd_restore)

    p = sub.add_parser("offsite", help="optional backup to a PRIVATE GitHub repo")
    p.add_argument("action", choices=["setup", "status", "push", "disable"])
    p.add_argument("--repo", help="OWNER/NAME (setup)")
    p.add_argument("--branch", help="branch for backups (default flowstate-backup)")
    p.add_argument("--db")
    p.add_argument("--dry-run", action="store_true", help="push: only check the repo is private")
    p.set_defaults(fn=cmd_offsite)

    p = sub.add_parser("schedule-backups", help="nightly backup via the OS scheduler")
    p.add_argument("--time", default="02:30", help="HH:MM, local time (default 02:30)")
    p.add_argument("--offsite", action="store_true", help="also push off-site each night")
    p.add_argument("--remove", action="store_true", help="remove the nightly backup")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--db")
    p.add_argument("--dir", help="backups folder")
    p.add_argument("--python", help="Python to run (default: this one)")
    p.set_defaults(fn=cmd_schedule)

    p = sub.add_parser("token", help="HTTP bearer token in the OS credential store")
    p.add_argument("action", choices=["status", "migrate"])
    p.add_argument("--from", dest="source", help="token file to copy (migrate)")
    p.set_defaults(fn=cmd_token)

    p = sub.add_parser("scan-secrets", help="list records that look like they hold secrets")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_scan)

    p = sub.add_parser("remote", help="remote access: passphrase, per-app tokens, revoke")
    p.add_argument("action", choices=["status", "passphrase", "add-client", "clients", "revoke",
                                      "allow-redirect"])
    p.add_argument("--name", help="add-client: app name; allow-redirect: host")
    p.add_argument("--read-only", action="store_true", help="add-client: read-only access")
    p.add_argument("--days", type=int, help="add-client: token expires after N days")
    p.add_argument("--to-file", help="add-client: write the token to this file instead of the screen")
    p.add_argument("--client", dest="client_id", help="revoke: the app's id")
    p.add_argument("--from", dest="source", help="passphrase: read it from this file")
    p.set_defaults(fn=cmd_remote)
    from . import app_keys
    app_keys.register(sub)
    from .relay import cli as relay_cli
    relay_cli.register(sub)
    cli_more.register(sub)
    cli_review.register(sub)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (sqlite3.Error, OSError, ValueError) as e:
        say(f"FAILED: {type(e).__name__}: {e}")
        return 1
