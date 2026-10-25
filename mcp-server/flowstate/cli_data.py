"""The user's own data commands (2.4.4, data security audit), registered by cli.py:

    flowstate data-git status | init | remote URL [--unchecked] | sync [-m TEXT] | clone URL
    flowstate erase    TYPE:ID [TYPE:ID ...] | --deleted-before DAYS   [--dry-run] [--db FILE]
    flowstate tighten  [--dry-run] [--db FILE]
    flowstate rules    list --project P | add --project P "TEXT" | remove ID   (you confirm add, remove)

data-git remote / sync / clone send FlowState data off this computer (or pull a repository
into it), so they are never tools: an AI app gets `user_only` and is told to ask you
(audit D1). They ask for your `y` on the controlling terminal (cli_review.Terminal), never from
piped input. erase (audit D5) does the same. tighten makes FlowState's own existing files
owner-only (audit D9); it changes nothing else.
"""

import re
from pathlib import Path

COMMANDS = ("data-git", "erase", "tighten", "rules")


def say(msg: str) -> None:
    print(msg, flush=True)


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
    of projects that are gone are removed. Returns the number written."""
    import sqlite3
    from . import dbconn, markdown_io
    conn = dbconn.connect(str(db), timeout=5.0)
    try:
        rows = conn.execute("SELECT id, name FROM projects ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    out = folder / "export"
    out.mkdir(exist_ok=True)
    keep = set()
    for pid, name in rows:
        fname = (re.sub(r"[^A-Za-z0-9._-]+", "-", str(name or "project")).strip("-.")[:60]
                 or "project") + f"-{pid}.md"
        (out / fname).write_text(markdown_io.export(db, pid), encoding="utf-8")
        keep.add(fname)
    for old in out.glob("*.md"):
        if old.name not in keep:
            old.unlink()
    return len(keep)


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
        n = _export_all(db, db.parent)
        r = git_ops.git_sync(a.message)
        if r.get("no_longer_tracked"):
            say(f"Taken out of git (kept on disk): {', '.join(r['no_longer_tracked'][:10])}. "
                f"Older commits still hold them; see the README (data-git).")
        say(f"Exported {n} project(s). " + (("Pushed." if r.get("pushed") else r.get("message", ""))
                                             if r.get("success") else f"FAILED: {r.get('error')}"))
        return 0 if r.get("success") else 1
    if act == "clone":
        if not a.value:
            say("Use: flowstate data-git clone URL")
            return 2
        rc = _confirm(f"Clone {a.value} into {db.parent}?", "cloning")
        if rc:
            return rc
        r = git_ops.git_clone(a.value)
        say("Cloned." if r.get("success") else f"FAILED: {r.get('error')}")
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
    pid = _project_id(db, a.project) if a.project else None
    if pid is None:
        say("Name the project: --project P")
        return 2
    if a.action == "list":
        for r in rules.list_rules(db, pid)["rules"]:
            say(f"R{r['id']} ({rules.who_set(r)}): {r['rule']}")
        return 0
    if not a.text:
        say('Use: flowstate rules add --project P "Never push to main"')
        return 2
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
    rc = _confirm(f'Remove the hard rule R{rid} "{row[0]}" from {row[2]}? AIs will no longer be '
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


def register(sub) -> None:
    p = sub.add_parser("data-git", help="git for FlowState's data folder (the markdown export "
                                        "and attachments only); remote / sync / clone: you confirm")
    p.add_argument("action", choices=["status", "init", "remote", "sync", "clone"])
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

    p = sub.add_parser("rules", help="a project's hard rules: list, or add or remove one yourself")
    p.add_argument("action", choices=["list", "add", "remove"])
    p.add_argument("text", nargs="?", help="add: the rule; remove: its id (R<id>)")
    p.add_argument("--project", help="list / add: the project")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("tighten", help="make FlowState's existing files and folders owner-only")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_tighten)
