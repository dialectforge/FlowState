"""FlowState command line (besides running the server):

    flowstate migrate           [--db FILE]
    flowstate backup            [--db FILE] [--dir DIR] [--offsite]
    flowstate restore BACKUP    [--to NEW_FILE] [--force]
    flowstate offsite setup     --repo OWNER/NAME [--branch NAME]
    flowstate offsite status | push [--dry-run] | disable | key   (encrypted: offsite_crypto.py)
    flowstate schedule-backups  [--time HH:MM] [--offsite] [--remove] [--dry-run]
    flowstate token status | migrate --from FILE
    flowstate scan-secrets      [--db FILE]
    flowstate remote            status | passphrase | add-client --name N [--read-only] |
                                clients | rename OLD NEW | revoke --client ID |
                                allow-redirect --name HOST | lan-tokens [on|off]
    flowstate keys              add --name N [--read-only] [--workspace W] | list |
                                revoke --id ID | --name N   (per-app LAN keys: app_keys.py)
    flowstate relay             enroll --code C | run | status | renew | enable | disable |
                                forget | ct-check | ct-ack |
                                chat-approvals [on|off]   (optional, off by default: relay/)
    (Phase D commands: see cli_more.py; data-git, erase, tighten: cli_data.py)

Plain-language output; exit code 0 = OK, 1 = failed / refused. Never prints a token.
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from . import paths

from . import cli_data
from . import cli_more
from . import cli_review
from . import term

COMMANDS = ("migrate", "backup", "restore", "offsite", "schedule-backups", "token",
            "scan-secrets", "remote", "keys", "relay") + cli_more.COMMANDS + cli_review.COMMANDS \
    + cli_data.COMMANDS


def say(msg: str) -> None:
    # text from apps and records is in many lines: no escape code reaches the terminal (NEW-7)
    print(term.safe(msg, newlines=True), flush=True)


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
    from .privatefs import auto_tighten
    db = _db(a)
    # files an older FlowState left readable by others, in a folder you chose too (N-F31)
    loose = auto_tighten(db, [Path(a.dir).expanduser()] if a.dir else ())
    if loose:
        say(f"Made {len(loose)} of FlowState's files and folders owner-only (an older version "
            f"had left them readable by other users of this computer).")
    try:
        r = backup_now(db, Path(a.dir) if a.dir else None, prune=not a.no_prune)
    except BackupError as e:
        say(f"Backup FAILED: {e}")
        return 1
    if a.dir:   # the new copy's folder: owner-only like the default (N-F31)
        auto_tighten(db, [Path(a.dir).expanduser()])
    say(f"Backup OK: {r['path']} ({r['size'] // 1024} KB, integrity ok)")
    if r["removed"]:
        say(f"Old backups removed by the 14-daily / 8-weekly rule: {len(r['removed'])}")
    if a.offsite:
        return _push(db, dry_run=False)
    return 0


def _push(db: Path, dry_run: bool, replace_newer: bool = False) -> int:
    from .offsite import OffsiteError, push_backup, remove_staging
    cfg = _offsite_cfg()
    if not cfg.get("repo"):
        left = remove_staging()   # an old unencrypted staging copy goes even so (N-F37)
        if left:
            say(left)
        say("Off-site backup is not set up (flowstate offsite setup --repo OWNER/NAME).")
        return 1
    try:
        r = push_backup(db, cfg["repo"], branch=cfg.get("branch") or "flowstate-backup",
                        dry_run=dry_run, replace_newer=replace_newer)
    except OffsiteError as e:
        say(f"Off-site backup REFUSED/FAILED: {e}")
        return 1
    if r.get("staging_left"):
        say("WARNING: " + r["staging_left"])
    if r.get("dry_run"):
        say(f"Off-site dry run: github.com/{r['repo']} is private. Nothing was pushed.")
    else:
        say(f"Off-site backup pushed to github.com/{r['repo']} branch {r['branch']} "
            f"(private; encrypted on this computer; files: {', '.join(r['files'])}).")
        if r.get("key_changed"):
            say(f"WARNING: the off-site encryption key CHANGED: this copy uses key "
                f"{r['key_id']}, the last one used {r['key_changed']}. Copies made before can "
                f"only be read with the OLD recovery code. Save the new one now: run "
                f"`flowstate offsite key` in your own terminal.")
        elif r.get("new_key"):
            say("A new encryption key was made for it and kept in this computer's credential "
                "store. Save its recovery code now: run `flowstate offsite key` in your own "
                "terminal. Without it, a new computer can't read the off-site copy.")
    return 0


def _show_recovery_code(key: bytes) -> bool:
    """The recovery code goes to the user's own terminal only, never to stdout (an AI app
    running setup for the user must not get it in its chat). False: no terminal here."""
    from .cli_review import open_terminal
    from .offsite_crypto import key_id, recovery_code
    tty = open_terminal()
    if tty is None:
        return False
    try:
        tty.tell("Off-site recovery code (save it in your password manager; it is the only way "
                 "to read the off-site copy on another computer):")
        tty.tell("  " + recovery_code(key))
    finally:
        tty.close()
    kid = key_id(key)

    def shown(cfg):   # the brief stops reminding for THIS key (offsite.key_line; N-F9)
        off = cfg.get("offsite")
        if isinstance(off, dict):
            off["recovery_code_key"] = off["key_in_use"] = kid
            off.pop("recovery_code_shown", None)
    try:
        paths.update_config(shown)
    except Exception:  # noqa: BLE001 - only a reminder
        pass
    return True


def _decrypted(src: Path):
    """An encrypted off-site copy (.fsenc): decrypted into a private file next to the restores,
    with this computer's key or the recovery code typed in the terminal. None: refused."""
    from . import offsite_crypto as OC
    try:
        key = OC.get_key(doing="changed")
    except OC.CryptoError as e:   # unreadable or damaged: the recovery code still works
        say(f"(This computer's off-site key can't be used, so the restore asks for the "
            f"recovery code instead: {e})")
        key = None
    if key is None or OC.key_id(key) != OC.file_key_id(src):
        from .cli_review import open_terminal
        tty = open_terminal()
        if tty is None:
            say("Restore REFUSED: this copy is encrypted with a key this computer doesn't have. "
                "Run the command in a terminal window and type its recovery code.")
            return None
        try:
            code = tty.ask("Recovery code for this off-site copy: ")
        finally:
            tty.close()
        try:
            key = OC.key_from_code(code)
        except OC.CryptoError as e:
            say(f"Restore REFUSED: {e}")
            return None
    out = paths.backup_dir() / "restored" / f"decrypted-{datetime.now():%Y%m%d-%H%M%S}.db"
    try:
        info = OC.decrypt_file(src, out, key)
    except OC.CryptoError as e:
        say(f"Restore REFUSED: {e}")
        return None
    if info.get("created"):   # sealed in the encrypted header: can't be changed (N-F34)
        made = datetime.fromtimestamp(info["created"]).strftime("%Y-%m-%d %H:%M")
        say(f"This encrypted copy was made on {made} (this computer's time). If you expected "
            f"a newer one, someone may have put an older copy in its place.")
    else:
        say("This encrypted copy is from an older FlowState and doesn't record when it was made.")
    return out


def cmd_restore(a) -> int:
    from .backup import BackupError, restore
    src = Path(a.backup).expanduser()
    from .offsite_crypto import is_encrypted
    plain = None
    if is_encrypted(src):
        plain = _decrypted(src)
        if plain is None:
            return 1
        src = plain
    try:
        return _restore(a, src)
    finally:
        if plain is not None:
            plain.unlink(missing_ok=True)


def _restore(a, src: Path) -> int:
    from .backup import BackupError, restore
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
    from .offsite import OffsiteError, check_repo_name, remove_staging, require_private
    if a.action in ("setup", "disable"):   # an old unencrypted staging copy goes (N-F37)
        left = remove_staging()
        if left:
            say("WARNING: " + left)
    if a.action == "setup":
        try:
            repo = check_repo_name(a.repo or "")
            require_private(repo)
        except OffsiteError as e:
            say(f"Not set up: {e}")
            return 1
        branch = a.branch or "flowstate-backup"
        keep = ("pushed_key", "key_in_use", "recovery_code_key", "last_push_utc")   # which key's code was seen

        def setup(cfg):
            old = cfg.get("offsite") if isinstance(cfg.get("offsite"), dict) else {}
            cfg["offsite"] = {"repo": repo, "branch": branch,
                              **{k: old[k] for k in keep if old.get(k)}}
        try:
            p = paths.update_config(setup)
        except (paths.ConfigUnreadable, TimeoutError) as e:
            say(f"Not set up: {e}")
            return 1
        say(f"Off-site backups will go to github.com/{repo} (private), branch "
            f"{branch}, encrypted on this computer first. Saved in {p}. Push now with: "
            f"flowstate backup --offsite")
        from .offsite_crypto import CryptoError, get_or_create_key
        try:
            key, made = get_or_create_key()
        except CryptoError as e:   # unreadable / damaged: never replaced (N-F6)
            say(f"The repository is saved, but the encryption key isn't ready, so pushes will "
                f"fail until it is: {e}")
            return 1   # not ready: a script sees it (task 56 Info)
        except Exception as e:  # noqa: BLE001 - the push makes it later
            say(f"(The encryption key will be made at the first push: {type(e).__name__}.)")
            return 0
        if not _show_recovery_code(key):
            say("Your recovery code wasn't shown here (no terminal). Run `flowstate offsite key` "
                "yourself in a terminal window and save it.")
        return 0
    if a.action == "key":
        from .offsite_crypto import CryptoError, get_key
        try:
            key = get_key()
        except CryptoError as e:
            say(f"Can't show the recovery code: {e}")
            return 1
        if key is None:
            say("No off-site key yet; it is made at setup or the first push.")
            return 1
        if not _show_recovery_code(key):
            say("Refused: the recovery code is shown only in a terminal window (piped output "
                "doesn't count). Run it yourself in a terminal.")
            return 2
        return 0
    if a.action == "disable":
        try:
            paths.update_config(lambda cfg: cfg.pop("offsite", None))
        except (paths.ConfigUnreadable, TimeoutError) as e:
            say(f"Not changed: {e}")
            return 1
        say("Off-site backups turned off (nothing on GitHub was deleted).")
        return 0
    if a.action == "status":
        cfg = _offsite_cfg()
        say(f"Off-site: github.com/{cfg['repo']} branch {cfg.get('branch')} (encrypted)"
            if cfg.get("repo") else "Off-site backup: not set up.")
        return 0
    return _push(_db(a), dry_run=a.dry_run, replace_newer=a.replace_newer)


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
        say(f"{t} #{i} {term.safe(c)}: looks like a {k}")
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
        from .cloudflared_check import note
        cf = note()   # only when an older cloudflared is installed; never blocks
        if cf:
            say(cf)
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
            say("Give the app a name: flowstate remote add-client --name \"my-laptop-app\" "
                "(one token per app)")
            return 1
        scopes = [RA.SCOPE_READ] if a.read_only else list(RA.SCOPES)
        p = Path(a.to_file).expanduser() if a.to_file else None
        if p is not None and paths.inside_git_worktree(p.parent):   # before anything is made
            say(f"Refused: {p.parent} is inside a git folder. Nothing was created.")
            return 1
        if p is not None and (p.is_symlink() or (p.exists() and not p.is_file())):
            say(f"Refused: {p} is a symbolic link or not a plain file (FlowState doesn't write "
                f"a token through a link). Nothing was created.")   # N-F32
            return 1
        try:
            cid, token = store.add_token_client(a.name, scopes, a.days)
        except ValueError as e:
            say(f"Not added: {e}")
            return 1
        if p is not None:
            from .privatefs import is_private, write_private
            try:
                write_private(p, token + "\n")   # owner-only (audit D10), never via a link
                private = is_private(p)
            except OSError as e:
                store.revoke_client(cid)
                say(f"Not added: the token file couldn't be written safely ({e}). The token "
                    f"was cancelled.")
                return 1
            say(f"App '{a.name}' added as {cid} ({' '.join(scopes)}). Token written to {p} "
                + ("(readable by your account only)" if private else
                   "(WARNING: this drive doesn't keep file permissions, so other users of "
                   "this computer may be able to read it)")
                + "; paste it into the app, then delete the file.")
        else:
            say(f"App '{a.name}' added as {cid} ({' '.join(scopes)}).")
            say("Its token is shown ONCE below. Paste it into the app as the header "
                "'Authorization: Bearer <token>'. Don't save it in a synced or git folder.")
            say("Use it in this one app only; make another token for each other app.")
            say(token)
        return 0
    if a.action == "clients":
        rows = store.list_clients()
        if not rows:
            say("No apps have been given access yet.")
        exposed = store.lan_exposures()
        for c in rows:
            state = "REVOKED " + _when(c["revoked_at"]) if c["revoked_at"] else \
                ("waiting for approval" if not c["approved_at"] else "active")
            was = store.former_names(c["id"])
            say(f"{c['id']}  {term.safe(repr(c['name']))}  {c['kind']}  {c['scopes']}  added {_when(c['created_at'])}"
                f"  last used {_when(c['last_used_at'])}  {state}"
                + (f"  (was {', '.join(repr(n) for n in was)})" if was else ""))
            e = exposed.get(c["id"])
            if e:   # audit N-F27
                say(f"    WARNING: {RA.exposure_text(c['id'], c['name'], e['last_at'])} "
                    f"(sent that way {e['times']} time(s), last from {e['last_ip'] or '?'} at "
                    f"{_when(e['last_at'])})")
        return 0
    if a.action == "keep-token":
        if not a.client_id:
            say("Which token? flowstate remote keep-token --client <id from `flowstate remote "
                "clients`>")
            return 1
        from .cli_review import open_terminal
        tty = open_terminal()   # an AI with a shell can't clear the warning by piping y (E)
        if tty is None:
            say("Refused: keeping a token that crossed the network asks you to confirm in a "
                "terminal, and there is none here (piped input doesn't count). Run it yourself "
                "in a terminal window.")
            return 2
        try:
            if tty.ask(f"Keep using {a.client_id} although it was sent over plain HTTP on your "
                       f"network (anyone there could have read it)? Type y to confirm: "
                       ).strip().lower() not in ("y", "yes"):
                say("Not confirmed; nothing changed.")
                return 1
        finally:
            tty.close()
        ok = store.keep_exposed(a.client_id)
        say(f"{a.client_id}: kept; the warning about it being sent over the LAN is gone (it "
            f"comes back if it is sent that way again)." if ok
            else f"{a.client_id}: no warning to clear.")
        return 0 if ok else 1
    if a.action == "rename":
        names = list(a.names or [])
        if len(names) != 2:
            say("Usage: flowstate remote rename <old name or id> <new name> (quote names with "
                "spaces; see `flowstate remote clients`)")
            return 1
        try:
            cid, was, now = store.rename_client(*names)
        except ValueError as e:
            say(f"Not renamed: {e}.")
            return 1
        say(f"{cid}: renamed from {was!r} to {now!r}. Same token; the app keeps working without "
            f"any change.")
        say(f"What it saved before keeps the name {was!r}; what it saves from now on says {now!r}. "
            f"It is still the same app for its own review items and undo.")
        if _carry_pause(a, was, now):
            say(f"{was!r} was paused, so {now!r} is paused too (flowstate apps resume --app "
                f"{now!r} to undo that).")
        return 0
    if a.action == "revoke":
        if not a.client_id:
            say("Which app? flowstate remote revoke --client <id from `flowstate remote clients`>")
            return 1
        ok = store.revoke_client(a.client_id)
        say(f"{a.client_id}: access revoked; its tokens stop working now." if ok
            else f"{a.client_id}: no such active app.")
        return 0 if ok else 1
    if a.action == "lan-tokens":   # audit D4: remote tokens over plain LAN HTTP
        want = (a.names or ["status"])[0]
        now = RA.lan_remote_tokens_allowed()
        if want not in ("on", "off"):
            say(f"Remote-access tokens on the LAN listener from other computers: "
                f"{'allowed' if now else 'refused (the default)'}. LAN traffic is plain HTTP, "
                f"not encrypted. flowstate remote lan-tokens on|off")
            return 0
        if want == "on":
            from .cli_data import _confirm
            rc = _confirm("Accept remote-access tokens on the plain-HTTP LAN listener? Anyone "
                          "on your network could copy one and use it over the internet.",
                          "allowing remote tokens on the LAN")
            if rc:
                return rc
        try:
            paths.update_config(lambda cfg: cfg.__setitem__("lan_remote_tokens", want == "on"))
        except (paths.ConfigUnreadable, TimeoutError) as e:
            say(f"Not changed: {e}")
            return 1
        from . import approvals   # logged like the other settings (Recent changes, who did what)
        try:
            approvals.log_setting(_db(a), "config:lan_remote_tokens",
                                  "allowed" if now else "refused",
                                  "allowed" if RA.lan_remote_tokens_allowed() else "refused")
        except sqlite3.Error:
            pass   # the setting is saved; a busy or missing database only loses the log line
        say(f"Remote-access tokens on the LAN listener: {'allowed' if want == 'on' else 'refused'}.")
        return 0
    if a.action == "allow-redirect":
        host = (a.name or "").strip().lower()
        if not host or "/" in host or ":" in host:
            say("Give a bare host name, e.g. flowstate remote allow-redirect --name grok.com")
            return 1
        def change(cfg: dict) -> None:
            hosts = cfg.setdefault("remote", {}).setdefault("redirect_hosts", [])
            if host not in hosts:
                hosts.append(host)
        try:
            paths.update_config(change)
        except (paths.ConfigUnreadable, TimeoutError) as e:
            say(f"Not changed: {e}")
            return 1
        say(f"OAuth apps may now return to https://{host}/...")
        return 0
    return 1


def _carry_pause(a, was: str, now: str) -> bool:
    """A paused token stays paused under its new name (the pause switch is keyed by name).
    True if it was paused."""
    from . import activity
    db = _db(a)
    if not db.exists():
        return False
    try:
        if not activity.is_paused(db, was) or activity.is_paused(db, now):
            return False
        activity.set_paused(db, now, True, "cli (rename)")
        return True
    except sqlite3.Error:
        return False


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

    p = sub.add_parser("offsite", help="optional ENCRYPTED backup to a PRIVATE GitHub repo; key: show the recovery code")
    p.add_argument("action", choices=["setup", "status", "push", "disable", "key"])
    p.add_argument("--repo", help="OWNER/NAME (setup)")
    p.add_argument("--branch", help="branch for backups (default flowstate-backup)")
    p.add_argument("--db")
    p.add_argument("--dry-run", action="store_true", help="push: only check the repo is private")
    p.add_argument("--replace-newer", action="store_true",
                   help="push: replace the copy on GitHub even if it is newer than this one")
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

    p = sub.add_parser("remote", help="remote access: passphrase, per-app tokens, rename, revoke",
                       description="rename OLD NEW: give a token or OAuth app a new name; the "
                                   "same token keeps working (OLD may be its id)")
    p.add_argument("action", choices=["status", "passphrase", "add-client", "clients", "rename",
                                      "revoke", "allow-redirect", "lan-tokens", "keep-token"])
    p.add_argument("names", nargs="*", metavar="NAME",
                   help="rename: the app's current name (or id), then its new name")
    p.add_argument("--db", help="rename: the database whose pause switch to carry over; "
                                "lan-tokens: the database that logs the change")
    p.add_argument("--name", help="add-client: app name; allow-redirect: host")
    p.add_argument("--read-only", action="store_true", help="add-client: read-only access")
    p.add_argument("--days", type=int, help="add-client: token expires after N days")
    p.add_argument("--to-file", help="add-client: write the token to this file instead of the screen")
    p.add_argument("--client", dest="client_id", help="revoke / keep-token: the app's id")
    p.add_argument("--from", dest="source", help="passphrase: read it from this file")
    p.set_defaults(fn=cmd_remote)
    from . import app_keys
    app_keys.register(sub)
    from .relay import cli as relay_cli
    relay_cli.register(sub)
    cli_more.register(sub)
    cli_review.register(sub)
    cli_data.register(sub)
    return ap


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (sqlite3.Error, OSError, ValueError) as e:
        say(f"FAILED: {type(e).__name__}: {e}")
        return 1
