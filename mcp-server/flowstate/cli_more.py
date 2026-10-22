"""Phase D commands (registered by cli.py):

    flowstate cleanup        [--db FILE] [--project P] [--kinds duplicate,stale,misfiled] [--queue]
    flowstate apps           list | pause --app NAME | resume --app NAME | activity [--app NAME]
    flowstate meaning        status | index [--project P]
    flowstate review-page    [--port 8770] [--no-browser] [--print-url]   (127.0.0.1 only)
    flowstate export-md      --project P [--out FILE]
    flowstate import-md      --project P FILE            (to the review queue)
    flowstate import-memory  --project P FILE [--source Claude|ChatGPT] [--kind learning]
    flowstate git-capture    enable --project P --repo PATH | disable --project P | status | run [--project P]
    flowstate update         check [on|off|now]   (the daily update check: updates.py)
    flowstate update         [--dry-run] [--yes --user-said TEXT] [--rollback] [--db FILE] [--app NAME]
                             (move this install to the newest signed release: selfupdate.py)
"""

from pathlib import Path


def say(msg: str) -> None:
    print(msg, flush=True)


def _db(a) -> Path:
    from .cli import _db as base
    return base(a)


def _project_id(db: Path, value):
    if value in (None, ""):
        return None
    from .brief import resolve_project
    res = resolve_project(db, value, fuzzy=False)
    if "project" not in res:
        raise ValueError(f"no single project matches {value!r} ({res.get('reason')})")
    return res["project"]["id"]


def cmd_cleanup(a) -> int:
    from . import cleanup
    db = _db(a)
    pid = _project_id(db, a.project)
    kinds = tuple(k.strip() for k in a.kinds.split(",") if k.strip())
    found = cleanup.suggest(db, pid, kinds)
    for s in found:
        say(f"- {s['summary']}")
    say(f"{len(found)} suggestion(s).")
    if a.queue:
        res = cleanup.queue(db, found, source="cleanup (cli)")
        say(f"Added {res['queued']} to the review queue ({res['already_suggested']} were already "
            f"suggested before; withdrew {res['withdrew']} unsure one(s) from older versions). "
            f"Nothing was moved or changed.")
    else:
        say("Nothing was changed (add --queue to put these in the review queue).")
    return 0


def cmd_apps(a) -> int:
    from . import activity
    db = _db(a)
    if a.action == "list":
        rows = activity.apps(db)
        for r in rows:
            say(f"{r['app']}: {'PAUSED' if r['paused'] else 'on'}, last seen {r['last_seen'] or '-'}, "
                f"{r['calls']} calls ({r['writes'] or 0} writes, {r['errors'] or 0} errors)")
        say(f"{len(rows)} app(s).")
        return 0
    if a.action in ("pause", "resume"):
        if not a.app:
            say("Name the app with --app (see `flowstate apps list`).")
            return 1
        activity.set_paused(db, a.app, a.action == "pause", "cli")
        say(f"{a.app}: {'paused (it can still read; its writes are refused)' if a.action == 'pause' else 'resumed'}.")
        return 0
    for r in activity.recent(db, a.app, a.limit):
        say(f"{r['at']} {r['app']}: {r['tool']}{('/' + r['action']) if r['action'] else ''}"
            f"{(' [' + r['project'] + ']') if r['project'] else ''} "
            f"{'write' if r['write'] else 'read'} {'ok' if r['ok'] else 'error ' + (r['error'] or '')}")
    return 0


def cmd_meaning(a) -> int:
    from . import meaning
    st = meaning.status()
    if a.action == "status":
        say(f"Search by meaning: {'available, model ' + st['model'] if st['available'] else 'not available (' + str(st['reason']) + '); search uses keywords'}")
        return 0
    if not st["available"]:
        say(f"Not indexed: {st['reason']}. Keyword search works without it.")
        return 1
    db = _db(a)
    res = meaning.index(db, _project_id(db, a.project))
    say(f"Indexed {res['indexed']} record(s) with {res.get('model')}.")
    return 0


def cmd_review_page(a) -> int:
    from .review_page import serve
    serve(_db(a), a.port, open_browser=not a.no_browser, print_url=a.print_url)
    return 0


def cmd_export_md(a) -> int:
    from . import markdown_io
    db = _db(a)
    text = markdown_io.export(db, _project_id(db, a.project))
    if a.out:
        out = Path(a.out).expanduser()
        if out.exists() and not a.force:
            say(f"{out} exists; add --force to overwrite it.")
            return 1
        out.write_text(text, encoding="utf-8")
        say(f"Wrote {out}.")
    else:
        print(text, end="")
    return 0


def _read(path: str) -> str:
    return Path(path).expanduser().read_text(encoding="utf-8-sig")


def cmd_import_md(a) -> int:
    from . import markdown_io
    db = _db(a)
    res = markdown_io.import_text(db, _project_id(db, a.project), _read(a.file), Path(a.file).name)
    say(f"Review queue: {res['new']} new, {res['edits']} edits ({res['unchanged']} unchanged, "
        f"{res['read_only_lines']} decision/change lines left as they are, {res['secrets_skipped']} "
        f"skipped for secrets, {res['not_found']} not in this project). Nothing was saved as a record.")
    return 0


def cmd_import_memory(a) -> int:
    from . import memory_import
    db = _db(a)
    res = memory_import.import_memories(db, _project_id(db, a.project), _read(a.file),
                                        a.source or "memory export", a.kind)
    say(f"Found {res['found']} memories: {res['queued']} added to the review queue, "
        f"{res['duplicates_skipped']} repeats and {res['secrets_skipped']} with secrets skipped"
        + (f", {res['too_many']} over the limit" if res["too_many"] else "") + ".")
    return 0


def cmd_git_capture(a) -> int:
    from . import git_capture as G
    db = _db(a)
    if a.action == "status":
        rows = G.status(db)
        for r in rows:
            say(f"{r['project']}: {'on' if r['enabled'] else 'off'}, {r['repo_path']}, "
                f"last commit {(r['last_commit'] or '-')[:8]}, last run {r['last_run_at'] or '-'}")
        say(f"{len(rows)} project(s) set up; every other project is off (the default).")
        return 0
    if a.action == "run" and not a.project:
        from . import server as S
        S.init_backend(db)
        for r in G.status(db):
            if r["enabled"]:
                res = G.run(db, r["project_id"], S._save_commit)
                say(f"{r['project']}: {res['captured']} new commit(s)")
        return 0
    if not a.project:
        say("Name the project with --project.")
        return 1
    pid = _project_id(db, a.project)
    try:
        if a.action == "enable":
            if not a.repo:
                say("Give the repository folder with --repo.")
                return 1
            res = G.enable(db, pid, a.repo)
            say(f"Git capture is on for {a.project}: {res['repo']}. Commits made from now on will be "
                f"added as changes (subjects only).")
        elif a.action == "disable":
            say("Git capture is off." if G.disable(db, pid) else "It was not on for that project.")
        else:
            from . import server as S
            S.init_backend(db)
            res = G.run(db, pid, S._save_commit)
            say(f"{res['captured']} new commit(s) captured" if res["on"] else "Git capture is off for that project.")
    except G.CaptureError as e:
        say(f"FAILED: {e}")
        return 1
    return 0


def cmd_update(a) -> int:
    from . import updates
    if a.action == "check":
        return updates.cli_check(a.sub, say)
    from . import selfupdate
    return selfupdate.cli(a, say)


COMMANDS = ("cleanup", "apps", "meaning", "review-page", "export-md", "import-md", "import-memory",
            "git-capture", "update")


def register(sub) -> None:
    p = sub.add_parser("cleanup", help="suggest duplicates / stale / misfiled items (to review)")
    p.add_argument("--db")
    p.add_argument("--project", help="one project (name or id); default: all")
    p.add_argument("--kinds", default="duplicate,stale,misfiled")
    p.add_argument("--queue", action="store_true", help="add the suggestions to the review queue")
    p.set_defaults(fn=cmd_cleanup)

    p = sub.add_parser("apps", help="which app did what; pause / resume an app's writes")
    p.add_argument("action", choices=["list", "pause", "resume", "activity"])
    p.add_argument("--app", help="the app's name as shown by `apps list`")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--db")
    p.set_defaults(fn=cmd_apps)

    p = sub.add_parser("meaning", help="search by meaning: status, or build the local index")
    p.add_argument("action", choices=["status", "index"])
    p.add_argument("--project")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_meaning)

    p = sub.add_parser("review-page", help="open the review page (this computer only)")
    p.add_argument("--port", type=int, default=8770, help="default 8770 (8766/8767 are the servers)")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--print-url", action="store_true", help="also show the one-time link (on your terminal)")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_review_page)

    p = sub.add_parser("export-md", help="export a project as readable markdown")
    p.add_argument("--project", required=True)
    p.add_argument("--out")
    p.add_argument("--force", action="store_true")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_export_md)

    p = sub.add_parser("import-md", help="import an edited export into the review queue")
    p.add_argument("file")
    p.add_argument("--project", required=True)
    p.add_argument("--db")
    p.set_defaults(fn=cmd_import_md)

    p = sub.add_parser("import-memory", help="import a Claude/ChatGPT memory export into the review queue")
    p.add_argument("file")
    p.add_argument("--project", required=True)
    p.add_argument("--source", help="e.g. Claude, ChatGPT")
    p.add_argument("--kind", default="learning", choices=["learning", "decision", "todo", "problem", "change"])
    p.add_argument("--db")
    p.set_defaults(fn=cmd_import_memory)

    p = sub.add_parser("git-capture", help="opt-in: add git commit subjects to a project's changes")
    p.add_argument("action", choices=["enable", "disable", "status", "run"])
    p.add_argument("--project")
    p.add_argument("--repo")
    p.add_argument("--db")
    p.set_defaults(fn=cmd_git_capture)

    p = sub.add_parser("update", help="update to the newest signed release (only when the user asks); "
                                      "`update check [on|off|now]`: the daily update check")
    p.add_argument("action", nargs="?", choices=["check"])
    p.add_argument("sub", nargs="?", choices=["on", "off", "now"])
    p.add_argument("--dry-run", action="store_true", help="check everything and show the plan; change nothing")
    p.add_argument("--yes", action="store_true", help="don't ask in the terminal (needs --user-said)")
    p.add_argument("--user-said", help="the user's own words asking for the update (kept in the history)")
    p.add_argument("--rollback", action="store_true", help="go back to the version before the last update")
    p.add_argument("--app", help="the AI app running this for the user (kept in the history)")
    p.add_argument("--db", help="the database to back up and migrate (default: as the server)")
    p.set_defaults(fn=cmd_update)
