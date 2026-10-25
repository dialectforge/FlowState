"""The user's own data commands (2.4.4, data security audit), registered by cli.py:

    flowstate data-git status | init | remote URL [--unchecked] | sync [-m TEXT] | clone URL | migrate
    flowstate erase    TYPE:ID [TYPE:ID ...] | --deleted-before DAYS   [--dry-run] [--db FILE]
    flowstate tighten  [--dry-run] [--db FILE]
    flowstate rules    list --project P | add --project P "TEXT" | remove ID   (you confirm add, remove)

data-git remote / sync / clone / migrate send FlowState data off this computer (or pull a
repository into it), so they are never tools: an AI app gets `user_only` and is told to ask you
(audit D1). They ask for your `y` on the controlling terminal (cli_review.Terminal), never from
piped input. erase (audit D5) does the same. tighten makes FlowState's own existing files
owner-only (audit D9); it changes nothing else.
"""

import re
from pathlib import Path

from . import term

COMMANDS = ("data-git", "erase", "tighten", "rules")


def say(msg: str) -> None:
    # text from apps and records is in many lines: no escape code reaches the terminal (NEW-7)
    print(term.safe(msg, newlines=True), flush=True)


def _db(a) -> Path:
    from .cli import _db as base
    return base(a)


def _confirm(question: str, refused: str) -> int:
    """0 when the user typed y in their own terminal, else the exit code (1 no, 2 no terminal)."""
    from .cli_review import open_terminal
    tty = open_terminal()
    if tty is None:
        say(f"Refused: {refused} asks you to confirm in a terminal, and there is none here "
            f"(piped input doesn't count). Run it yourself in a terminal window.")
        return 2
    try:
        if tty.ask(question + " Type y to confirm: ").strip().lower() not in ("y", "yes"):
            say("Not confirmed; nothing changed.")
            return 1
    finally:
        tty.close()
    return 0


# ---------------------------------------------------------------- data-git
def _export_all(db: Path, folder: Path) -> int:
    """export/<project>-<id>.md for every project (the markdown export, defused: D20); files
    of projects that are gone are removed. Returns the number written. The folder and files
    are owner-only (audit N-F28), and nothing is written through a link (audit N-F12: the
    caller refuses when git_ops.unsafe_entries finds one; each write also refuses one)."""
    import sqlite3
    from . import dbconn, markdown_io, privatefs
    from .tools import git_ops
    conn = dbconn.connect(str(db), timeout=5.0)
    try:
        rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out = folder / "export"
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise git_ops.UnsafePath("export")
    if out.exists():
        privatefs.lock(out)   # an export folder an earlier version made readable by others
    else:
        privatefs.private_dir(out)
    keep = set()
    for pid, name in rows:
        fname = (re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "project")).strip("-.")[:60]
                 or "project") + f"-{pid}.md"
        git_ops.safe_write(out / fname, markdown_io.export(db, pid), private=True)
        keep.add(fname)
    # Only files this computer wrote at an earlier sync and whose project is gone here are
    # removed: another computer's exports, of projects this one doesn't know, stay (task 57,
    # N3). The list is kept inside .git, so it is never committed.
    listed = folder / ".git" / "flowstate-exported.json"
    mine = _exported_before(listed)
    for old in out.glob("*.md"):
        if old.name not in keep and old.name in mine and (old.is_file() or old.is_symlink()):
            old.unlink()
    if (folder / ".git").is_dir() and not (folder / ".git").is_symlink():
        import json
        git_ops.safe_write(listed, json.dumps(sorted(keep)), private=True)
    return len(keep)


def _exported_before(listed: Path) -> set:
    import json
    try:
        names = json.loads(listed.read_text(encoding="utf-8")) if listed.is_file() else []
    except (OSError, ValueError):
        names = []
    return {n for n in names if isinstance(n, str)}


def cmd_data_git(a) -> int:
    from .tools import utils
    db = _db(a)
    before = utils.data_dir
    utils.data_dir = lambda: db.parent   # this command's database, not the server default
    try:
        return _data_git(a, db)
    finally:
        utils.data_dir = before


def _data_git(a, db: Path) -> int:
    import json
    from .tools import git_ops
    act = a.action
    if act == "status":
        say(json.dumps(git_ops.git_status(), indent=2))
        return 0
    if act == "init":
        r = git_ops.git_init()
        say("Data-git is set up in " + str(db.parent) + ". Only README.md, projects/ "
            "(attachments) and export/ (a markdown export) are ever committed; the database, "
            "backups, relay keys, tokens and settings never are." if r.get("initialized")
            else f"FAILED: {r.get('error')}")
        return 0 if r.get("initialized") else 1
    if act == "remote":
        if not a.value:
            say("Use: flowstate data-git remote URL [--unchecked]")
            return 2
        try:
            git_ops.check_remote(a.value, a.unchecked)
        except git_ops.RemoteRefused as e:
            say(f"Refused: {e}")
            return 1
        rc = _confirm(f"Push FlowState data to {a.value}? Each `flowstate data-git sync` sends "
                      f"a plain-text markdown export of every project's records and the "
                      f"attachments there, and git keeps every version.", "setting a remote")
        if rc:
            return rc
        r = git_ops.git_set_remote(a.value, a.unchecked)
        say(f"Remote set ({r['checked']})." if r.get("success") else f"FAILED: {r.get('error')}")
        return 0 if r.get("success") else 1
    if act == "sync":
        rc = _confirm("Commit FlowState's markdown export and attachments and push them to "
                      "the remote you set?", "syncing")
        if rc:
            return rc
        if (db.parent / ".git").exists():
            # nothing is written through a link someone pushed (N-F12), and an older
            # repository that still holds the database waits for `migrate` (N-F14)
            refusal = (git_ops.LEGACY.format(paths=git_ops._list(legacy))
                       if (legacy := git_ops.never_tracked_in("HEAD")) else None)
            if not refusal and (bad := git_ops.unsafe_entries(db.parent)):
                refusal = git_ops.UNSAFE_HERE.format(paths=git_ops._list(bad))
            if refusal:
                say(f"Refused: {refusal}")
                return 1
        try:
            n = _export_all(db, db.parent)
        except git_ops.UnsafePath as e:
            say("Refused: " + git_ops.UNSAFE_HERE.format(paths=str(e)))
            return 1
        r = git_ops.git_sync(a.message, db_path=db)
        if r.get("success"):
            try:   # export/ shows this computer's database again, even where another
                n = _export_all(db, db.parent)   # computer's copy of a file just came in (N3)
            except git_ops.UnsafePath:
                pass
        say(f"Exported {n} project(s). " + (("Pushed." if r.get("pushed") else r.get("message", ""))
                                             if r.get("success") else f"FAILED: {r.get('error')}"))
        return 0 if r.get("success") else 1
    if act == "migrate":
        legacy = git_ops.never_tracked_in("HEAD") if (db.parent / ".git").exists() else []
        rc = _confirm("Take the database, settings and keys out of this data-git repository "
                      "for good" + (f" ({git_ops._list(legacy, 4)})" if legacy else "") + "? "
                      "Every other computer that syncs it must already run FlowState 2.4.4 or "
                      "newer: on a computer still on 2.4.3 or older, its next sync deletes its "
                      "database. A safety copy of this computer's database is made first, and "
                      "the files stay on this disk.", "migrating data-git")
        if rc:
            return rc
        r = git_ops.git_migrate(db_path=db)
        if r.get("success"):
            gone = r.get("untracked") or []
            say((f"Taken out of git (kept on this disk): {git_ops._list(gone)}. " if gone else "")
                + (r.get("message") or ("Pushed. " if r.get("pushed") else "")
                   + "Now run `flowstate data-git sync`. Older commits still hold those files; "
                     "see the README (data-git)."))
            return 0
        say(f"FAILED: {r.get('error')}")
        return 1
    if act == "clone":
        if not a.value:
            say("Use: flowstate data-git clone URL")
            return 2
        rc = _confirm(f"Clone {a.value} into {db.parent}?", "cloning")
        if rc:
            return rc
        r = git_ops.git_clone(a.value)
        say("Cloned." if r.get("success") else f"FAILED: {r.get('error')}")
        if r.get("unsafe"):
            say("Warning: it holds links or other special entries FlowState won't write through "
                f"or sync: {git_ops._list(r['unsafe'])}. Remove them before you sync.")
        return 0 if r.get("success") else 1
    return 2


# ---------------------------------------------------------------- erase
def cmd_erase(a) -> int:
    from . import erase
    db = _db(a)
    try:
        plan = erase.plan(db, a.records, a.deleted_before)
    except ValueError as e:
        say(f"Refused: {e}")
        return 2
    for line in erase.describe(plan):
        say(line)
    if not plan["targets"]:
        return 0
    if a.dry_run:
        say("Dry run: nothing was erased.")
        return 0
    rc = _confirm("Erase this for good? It can't be undone, and no safety copy is made.",
                  "erasing")
    if rc:
        return rc
    res = erase.run(db, plan)
    for line in erase.report(res):
        say(line)
    return 0


# ---------------------------------------------------------------- tighten
def cmd_tighten(a) -> int:
    from . import privatefs
    db = _db(a)
    changed = privatefs.tighten_existing(db, dry_run=a.dry_run)
    if not changed:
        say("Nothing to change: FlowState's files are already owner-only (or this system "
            "doesn't use those permission bits).")
        return 0
    for p in changed:
        say(("would make owner-only: " if a.dry_run else "owner-only now: ") + str(p))
    return 0


# ---------------------------------------------------------------- rules
def cmd_rules(a) -> int:
    from . import rules
    from . import server as S
    from .cli_more import _project_id
    db = _db(a)
    S.init_backend(db)   # migrations first (024 labels who set a rule)
    S.backend.tracker = None
    if a.action == "remove":
        return _rule_remove(db, a.text)
    if a.action == "confirm":
        return _rule_confirm(db, a.text)
    pid = _project_id(db, a.project) if a.project else None
    if pid is None:
        say("Name the project: --project P")
        return 2
    if a.action == "list":
        for r in rules.list_rules(db, pid)["rules"]:
            say(f"R{r['id']} ({term.safe(rules.who_set(r))}): {term.safe(r['rule'])}")
        return 0
    if not a.text:
        say('Use: flowstate rules add --project P "Never push to main"')
        return 2
    if term.has_controls(a.text):   # a rule shows exactly as typed, or not at all (NEW-7)
        say("Not added: the rule holds control or invisible formatting characters "
            f"({term.safe(a.text)}); type it plainly.")
        return 1
    rc = _confirm(f'Add the hard rule "{" ".join(a.text.split())}"? Every AI working on this '
                  f'project is told never to break it.', "adding a hard rule")
    if rc:
        return rc
    try:
        r = rules.add_by_user(db, pid, a.text)
    except rules.RuleError as e:
        say(f"Not added: {e.payload['message'] if hasattr(e, 'payload') else e}")
        return 1
    say(r.get("note") or "Added.")
    return 0


def _rule_remove(db: Path, rule_id) -> int:
    """`flowstate rules remove ID`: the user's own terminal (their y), so at once (task 54).
    An app's manage_rules remove only queues it for them."""
    import sqlite3
    from . import dbconn, rules
    if not rule_id or not str(rule_id).strip().lstrip("Rr").isdigit():
        say("Use: flowstate rules remove ID   (R<id> in the brief; `flowstate rules list --project P`)")
        return 2
    rid = int(str(rule_id).strip().lstrip("Rr"))
    conn = dbconn.connect(str(db), timeout=5.0)
    try:
        row = conn.execute("SELECT r.rule, r.removed_at, p.name FROM project_rules r JOIN projects p "
                           "ON p.id = r.project_id WHERE r.id = ?", (rid,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if row is None:
        say(f"No hard rule R{rid}.")
        return 1
    if row[1]:
        say(f"R{rid} was already removed.")
        return 0
    rc = _confirm(f'Remove the hard rule R{rid} "{term.safe(row[0])}" from {term.safe(row[2])}? AIs will no longer be '
                  f'told to follow it (it stays on record as removed).', "removing a hard rule")
    if rc:
        return rc
    try:
        r = rules.remove_by_user(db, rid)
    except rules.RuleError as e:
        say(f"Not removed: {e.payload['message'] if hasattr(e, 'payload') else e}")
        return 1
    say(r.get("note") or "Removed.")
    return 0


def _rule_confirm(db: Path, rule_id) -> int:
    """`flowstate rules confirm ID`: a rule an older version let an app add on its own ("not
    confirmed by the user" in the brief) becomes yours, after your y in this terminal."""
    import sqlite3
    from . import dbconn, rules
    if not rule_id or not str(rule_id).strip().lstrip("Rr").isdigit():
        say("Use: flowstate rules confirm ID   (R<id> in the brief; `flowstate rules list --project P`)")
        return 2
    rid = int(str(rule_id).strip().lstrip("Rr"))
    conn = dbconn.connect(str(db), timeout=5.0)
    try:
        row = conn.execute("SELECT r.rule, r.removed_at, r.set_by_user, p.name FROM project_rules r "
                           "JOIN projects p ON p.id = r.project_id WHERE r.id = ?", (rid,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if row is None:
        say(f"No hard rule R{rid}.")
        return 1
    if row[1]:
        say(f"R{rid} was removed; add it again with `flowstate rules add`.")
        return 1
    if row[2]:
        say(f"R{rid} is already yours.")
        return 0
    rc = _confirm(f'Confirm the hard rule R{rid} "{term.safe(row[0])}" in {term.safe(row[3])} as your own? An app added '
                  f'it; every AI working on this project is told never to break it.',
                  "confirming a hard rule")
    if rc:
        return rc
    try:
        r = rules.confirm_by_user(db, rid)
    except rules.RuleError as e:
        say(f"Not confirmed: {e.payload['message'] if hasattr(e, 'payload') else e}")
        return 1
    say(r.get("note") or "Confirmed.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("data-git", help="git for FlowState's data folder (the markdown export "
                                        "and attachments only); remote / sync / clone / migrate: "
                                        "you confirm")
    p.add_argument("action", choices=["status", "init", "remote", "sync", "clone", "migrate"])
    p.add_argument("value", nargs="?", help="remote / clone: the git URL")
    p.add_argument("--unchecked", action="store_true",
                   help="remote: a non-GitHub remote FlowState can't check is private")
    p.add_argument("-m", "--message", help="sync: commit subject")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_data_git)

    p = sub.add_parser("erase", help="erase deleted records for good (search index, history, "
                                     "session log; you confirm in this terminal)")
    p.add_argument("records", nargs="*", metavar="TYPE:ID",
                   help="e.g. todo:12 decision:31 project:4 (a project: everything in it)")
    p.add_argument("--deleted-before", type=int, metavar="DAYS",
                   help="every record deleted more than DAYS days ago")
    p.add_argument("--dry-run", action="store_true", help="show what would go; change nothing")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_erase)

    p = sub.add_parser("rules", help="a project's hard rules: list, or add, remove or confirm "
                                     "one yourself")
    p.add_argument("action", choices=["list", "add", "remove", "confirm"])
    p.add_argument("text", nargs="?", help="add: the rule; remove / confirm: its id (R<id>)")
    p.add_argument("--project", help="list / add: the project")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("tighten", help="make FlowState's existing files and folders owner-only")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_tighten)
