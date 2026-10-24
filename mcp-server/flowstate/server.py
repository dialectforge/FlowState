"""FlowState MCP Server - v2 Phase A.

Any MCP client (Claude Desktop, Grok Build, Cursor, Codex, Claude Code, ...) gets:
initialize instructions, a `start_here` brief (<= 4,000 chars), implicit sessions that
open on first use and close when idle, a project check on every write, and fail-safe
behaviour (timeouts and errors come back as tool errors, never as broken connections).
Transports: stdio (default) and streamable HTTP with a bearer token.
"""

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types
from mcp.types import Tool, TextContent

from . import tools
from . import agent_guide as guide
from . import activity
from . import approvals
from . import claims
from . import cleanup
from . import conflicts, conflict_cards
from . import connections
from . import deletes
from . import history
from . import identity
from . import ledger
from . import legacy_args
from . import links
from . import review
from . import review_mode
from . import safety
from . import saves
from . import tasks
from . import extras, priority
from . import provenance
from . import receipts
from . import secrets_scan
from .autosession import AutoSessions, ensure_schema
from .brief import build_brief, candidates_text, resolve_project
from .database import Database, default_db_path
from .guard import EXEMPT_ACTIONS, GuardError, WRITES, check_write, explicit_project, is_write, writes_text

log = logging.getLogger("flowstate")

# Create server instance; instructions reach every client at initialize.
server = Server("flowstate", version="2.4.2", instructions=guide.INSTRUCTIONS)


from .tooldefs import ALL_TOOLS  # noqa: E402 - the public tools (surface.py maps their calls)
from . import __version__, surface, tool_labels, updates, waiting  # noqa: E402
from .routing import route_tool  # noqa: E402


def _route_tool(name: str, args: dict) -> Any:
    return route_tool(name, args, backend.db_path)


# ============================================================
# BACKEND STATE (fail-safe: a broken DB never stops the server)
# ============================================================

class _Backend:
    db_path: Optional[Path] = None
    error: Optional[str] = None
    tracker: Optional[AutoSessions] = None
    migration_error: Optional[str] = None
    health: Optional[str] = None   # safety.py: a line for the brief only if something is wrong


backend = _Backend()


def init_backend(db_path: Optional[Path] = None) -> None:
    """Open the DB and the session tracker. Failures are remembered, not raised."""
    path = Path(db_path) if db_path else default_db_path()
    backend.db_path, backend.error, backend.tracker = path, None, None
    backend.migration_error = None
    try:
        db = Database(path)
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:  # fail now, not on the first call, if the file isn't a usable DB
            conn.execute("SELECT 1 FROM projects LIMIT 1").fetchall()
        finally:
            conn.close()
        _migrate(path)
        backend.health = _safe_call(safety.health_line, path, backend.migration_error)
        ensure_schema(path)  # Phase A table, in case the migrations could not run
        _safe_call(cleanup.withdraw_unsure, path, 1.0)   # unsure clean-up ideas from older versions
        tools.set_db(db)
        backend.tracker = AutoSessions(path)
    except Exception as e:  # noqa: BLE001 - any failure means "unavailable"
        backend.error = f"{type(e).__name__}: {e}"
        log.error("FlowState database unavailable (%s): %s", path, backend.error)


def _migrate(path: Path) -> None:
    """Additive numbered migrations (backup first). A failure leaves the server running
    without the newer features; it never stops FlowState from starting."""
    from .migrations import migrate
    try:
        res = migrate(path, log=log.info)
        if res["applied"]:
            log.info("migrations applied: %s (backup: %s)", ", ".join(res["applied"]), res["backup"])
    except Exception as e:  # noqa: BLE001
        backend.migration_error = f"{type(e).__name__}: {e}"
        log.error("database migration skipped: %s", backend.migration_error)


def call_timeout() -> float:
    try:
        return float(os.environ.get("FLOWSTATE_CALL_TIMEOUT", "15"))
    except ValueError:
        return 15.0


def _client_name() -> str:
    try:
        cp = server.request_context.session.client_params
        if cp and cp.clientInfo and cp.clientInfo.name:
            return cp.clientInfo.name
    except (LookupError, AttributeError):
        pass
    return "unknown"


def _caller() -> tuple[str, Optional[list[str]]]:
    """(client id, scopes) of an HTTP caller, as set by remote_auth.Gate; scopes None for
    stdio (the app that started this process) = no limits."""
    try:
        req = server.request_context.request
    except LookupError:
        return "stdio", None
    user = getattr(req, "scope", {}).get("user") if req is not None else None
    tok = getattr(user, "access_token", None)
    if tok is None:
        return ("stdio", None) if req is None else ("unknown", [])
    return tok.client_id, list(tok.scopes)


def _identity(client: str, args: dict) -> identity.Identity:
    """Who this call is (identity.py): the app as the user knows it (a per-app key's or
    token's name over HTTP, else the MCP client name the app sends: Claude Desktop, Grok
    Build, Cursor, ...), the model if the caller gave it, the machine, and how the app name
    was established."""
    try:
        req = server.request_context.request
    except LookupError:
        req = None
    if req is None:
        return identity.resolve(client, model_arg=args.get("model"))
    tok = getattr(getattr(req, "scope", {}).get("user"), "access_token", None)
    try:
        headers = {k.lower(): v for k, v in req.headers.items()}
    except AttributeError:
        headers = {}
    return identity.resolve(client, http=True, token=tok, headers=headers,
                            model_arg=args.get("model"))


# A write that creates a record: (tool, action) -> record type, for the activity log.
_CREATES = {("todo", "add"): "todo", ("log_learning", None): "learning",
            ("log_change", None): "change", ("problem", "log"): "problem",
            ("attempt", "log"): "attempt", ("variable", "create"): "variable",
            ("method", "create"): "method", ("create_component", None): "component",
            ("create_project", None): "project", ("session", "log_conversation"): "decision"}


PAUSED = ("The user has paused FlowState writes from this app ({app}). Nothing was saved. "
          "Tell the user; they can resume it with `flowstate apps resume` or on the review page.")


def _error(code: str, message: str, **extra) -> types.CallToolResult:
    payload = {"error": code, "message": message, **extra}
    return types.CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload, indent=2, default=str))],
        isError=True)


def _ok(result: Any) -> types.CallToolResult:
    ui = result.pop("_ui", None) if isinstance(result, dict) else None
    if isinstance(result, dict) and "error" in result and len(result) <= 3:
        return types.CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, indent=2, default=str))],
            isError=True)
    text = result if isinstance(result, str) else json.dumps(priority.show(result), indent=2, default=str)
    out = types.CallToolResult(content=[TextContent(type="text", text=text)], isError=False)
    return conflict_cards.paint(out, ui)


TIMED_OUT_WRITE = ("This write is probably still saving: the next FlowState answer will say "
                   "whether it landed. Don't repeat it; carry on with the task.")

UNAVAILABLE = ("FlowState's database is unavailable right now ({}). Tell the user once and "
               "carry on with the task without FlowState.")


# ============================================================
# TOOL HANDLER
# ============================================================

@server.list_tools()
async def list_tools() -> list[Tool]:
    """The public tools of the current profile (FLOWSTATE_TOOLS: default, core or full)."""
    return conflict_cards.with_app_tool(tool_labels.visible(ALL_TOOLS))


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
    """Every call: public or older name -> internal operation (surface.py), then guard, run
    in a worker thread with a timeout, track the session. An older name gets one extra text
    block naming the new tool; the first block is unchanged."""
    note, shown = surface.hint(name, arguments or {}), name
    try:
        name, arguments = surface.translate(name, arguments or {})
    except surface.SurfaceError as e:
        return _error(e.code, e.message)
    out = await _call_internal(name, arguments, shown)
    if note:
        out.content.append(TextContent(type="text", text=note))
    return out


async def _call_internal(name: str, arguments: dict, shown: str = "") -> types.CallToolResult:
    if backend.error:
        return _error("flowstate_unavailable", UNAVAILABLE.format(backend.error))
    from .remote_auth import refusal
    who, scopes = _caller()
    denied = refusal(name, arguments or {}, scopes)
    if denied:
        log.info("refused %s for %s: %s", name, who, denied[0])
        return _error(*denied)
    client = _client_name()
    ident = _identity(client, arguments or {})
    app = ident.app
    write = writes_text(name, arguments or {})
    meta: dict = {}
    t0 = time.monotonic()
    out = None
    if write:   # its activity-log row goes into the save itself (handle_call)
        meta["activity"] = {"app": app, "tool": name, "caller": who, "t0": t0}
    timed_out = False
    id_token = identity.set_current(ident)   # read by every recording path (identity.py)
    try:
        session_obj = server.request_context.session
    except LookupError:
        session_obj = None
    conn_key = receipts.connection_key(ident, session_obj)
    change = receipts.new_change_id() if write else None
    call_token = identity.set_call(conn_key, change)   # receipts.py: one change id per write
    deadline = t0 + call_timeout()   # the answer comes by then, whatever is locked
    try:
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_paused_or_call, write, app, name, dict(arguments or {}), client, meta),
                timeout=call_timeout())
            if result is _PAUSED:
                out = _error("paused_by_user", PAUSED.format(app=app))
                return out
            if isinstance(result, dict) and ("_ask_user" in result or "_walk" in result):
                from .popup import ask_user
                result = await ask_user(result, session_obj, client)
                deadline = time.monotonic() + call_timeout()
        except asyncio.TimeoutError:
            meta["gave_up"] = timed_out = True
            receipts.timed_out(conn_key, change, shown or name)
            out = _error("flowstate_timeout", f"FlowState took longer than {call_timeout():g}s. "
                         + (TIMED_OUT_WRITE if write else "Carry on with the task."))
            return out
        except GuardError as g:
            out = _error(g.payload["error"], g.payload["message"],
                         **{k: v for k, v in g.payload.items() if k not in ("error", "message")})
            return out
        except Exception as e:  # noqa: BLE001
            out = _error("tool_failed", f"{type(e).__name__}: {e}")
            return out
        out = _ok(result)
        return out
    finally:   # bookkeeping gets what is left of the time; past it, it finishes on its own
        news = await _bounded(deadline, receipts.late_news, backend.db_path, conn_key) \
            if receipts.waiting(conn_key) else None
        if out is not None and news:   # how earlier timed-out writes of this connection ended
            out.content.extend(TextContent(type="text", text=line) for line in news)
        if out is not None and not out.isError and not write:   # the run ledger (ledger.py)
            await _bounded(deadline, ledger.record_read, backend.db_path, name,
                           (arguments or {}).get("action"), meta, out.content[0].text, conn_key, ident)
        err = None
        if out is not None and out.isError:
            try:
                err = json.loads(out.content[0].text).get("error")
            except (ValueError, AttributeError, IndexError):
                err = "error"
        try:
            saved = (meta.get("activity_saved") and not timed_out and out is not None
                     and err == meta.get("activity_error"))   # handle_call wrote this row
            if not saved:
                await _bounded(deadline, activity.record, backend.db_path, app, name,
                               (arguments or {}).get("action"), meta.get("project_id"), write,
                               out is not None and not out.isError, err,
                               int((time.monotonic() - t0) * 1000), who, meta.get("record"))
        finally:
            identity.reset_call(call_token)
            identity.reset(id_token)


_PAUSED = object()


def _paused_or_call(write: bool, app: str, name: str, args: dict, client: str, meta: dict):
    """The user paused this app's writes? (inside the call's time limit) Else the call."""
    if write and _safe_bool(activity.is_paused, backend.db_path, app):
        return _PAUSED
    return handle_call(name, args, client, meta)


async def _bounded(deadline: float, fn, *a):
    """Run bookkeeping in a thread, waiting at most until `deadline` (at least 50 ms); a
    locked database must not hold the answer past the call timeout."""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_safe_call, fn, *a),
                                      max(0.05, deadline - time.monotonic()))
    except asyncio.TimeoutError:
        log.warning("%s still waiting on the database; finishing in the background",
                    getattr(fn, "__name__", "bookkeeping"))
        return None


def _safe_track(fn, *a):
    """Session tracking must never break a call."""
    if backend.tracker is None:
        return None
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        log.warning("session tracking skipped: %s", e)
        return None


def _label(name: str, args: dict) -> str:
    what = (args.get("kind") if name == "log_work" else None) \
        or (f"{name}/{args['action']}" if args.get("action") else name)   # import_memory: a kind too
    if args.get("suggested"):
        what = f"suggested {what}"
    text = next((args[k] for k in ("summary", "title", "insight", "field_name", "skill", "name",
                                    "user_prompt_summary", "description")
                 if isinstance(args.get(k), str) and args[k].strip()), "")
    for k in ("todo_id", "problem_id", "attempt_id", "component_id", "session_id"):
        if not text and args.get(k) is not None:
            text = f"{k[:-3]} #{args[k]}"
    text = " ".join(text.split())
    return f"{what}: {text[:70]}" if text else what


def handle_call(name: str, args: dict, client: str = "unknown", meta: Optional[dict] = None) -> Any:
    """Synchronous core used by both transports (runs in a worker thread). `meta` receives
    the resolved project id (for the activity log). A write (it has a change id) gets its
    receipt, or the receipt of the same write saved moments ago (receipts.py)."""
    meta = meta if meta is not None else {}
    change = identity.change_id()
    pending, reply = (_safe_call(receipts.begin, backend.db_path, change, identity.call_key(),
                                 name, dict(args), call_timeout() - 1) or (None, None)) \
        if change else (None, None)
    if reply is not None:
        return reply
    result = saves.run(backend.db_path, _handle, name, args, client, meta, pending,
                       _one_commit(name, args))
    # something now waits in the review queue: say how the user decides it (after the save)
    return waiting.queue_guidance(result, backend.db_path, client)


def _one_commit(name: str, args: dict) -> bool:
    """A write saves with one commit (saves.py), except git, deletes and approvals (a
    safety copy first) and undo (it restores with foreign-key checks off)."""
    key = (name, args.get("action"))
    return writes_text(name, args) and not (
        name in receipts.NO_RECEIPT or name in ("delete_request", "review_approve")
        or key in _DELETE_ACTIONS or key == ("history", "undo"))


def _handle(name: str, args: dict, client: str, meta: dict) -> Any:
    db_path = backend.db_path
    tracker = backend.tracker
    if name == "start_here":
        return _start_here(args, client, meta)

    # No secret is ever stored: refuse before anything touches the database.
    if writes_text(name, args):
        # import_memory checks each memory itself (a secret line is skipped, not stored)
        hit = secrets_scan.scan_args(args, skip=_SCAN_PER_ITEM.get(name, ()))
        if hit:
            raise GuardError(secrets_scan.refusal(*hit))

    if name in ("review_list", "review_walk"):   # walk: waiting.py, then popup.ask_user
        return _review_list(args, name == "review_walk")
    try:  # priority names: critical/urgent/normal/low (old high/medium too) -> stored values
        legacy_args.priority_args(name, args)
    except priority.PriorityError as e:
        return priority.error_payload(e)

    pid = gone = None
    if is_write(name, args):
        # undo of an approved project delete: the project is gone, so its old name can't
        # pass the project check; deletes.py finds it in the edit history instead
        gone = (_safe_call(deletes.deleted_project, db_path, explicit_project(args),
                           args.get("record_id"))
                if (name, args.get("action"), args.get("record_type")) == ("history", "undo", "project")
                else None)
        pid = gone if gone is not None else check_write(db_path, name, args)
        args["project_id"] = pid  # the resolved id (callers may pass a name or "12")
    else:
        named = explicit_project(args)
        if named is not None:
            res = resolve_project(db_path, named, fuzzy=False)
            if "project" in res:
                pid = res["project"]["id"]
                args["project_id"] = pid  # reads too: tools take the id, callers may give a name

    meta["project_id"] = pid
    # (no session for a deleted project being restored: its row isn't back yet)
    sid = meta["session_id"] = _safe_track(tracker.touch, client, pid, None, *identity.conn()) \
        if (pid and tracker and gone is None) else None

    # Session tool: adopt / end the automatic session instead of duplicating it.
    if name == "session" and args.get("action") == "start" and sid:
        result = {"session_id": sid, "project_id": pid, "automatic": True,
                  "note": "FlowState already opened this session for you; it closes itself when idle."}
    elif name == "log_work" and review_mode.should_queue(
            db_path, pid, args.get("kind"), suggested=bool(args.get("suggested"))):
        result = _suggest(pid, args, client)
    elif name == "log_work":
        result = _log_work(pid, args, sid, client=client)
        if isinstance(result, dict) and isinstance(result.get("record"), dict) \
                and result["record"].get("id") is not None:
            meta["record"] = (result["logged"], result["record"]["id"])
        if args.get("suggested") and isinstance(result, dict) and "error" not in result:
            off = "Review mode is off, so this was saved directly."
            result["note"] = f"{off} {result['note']}" if result.get("note") else off
    elif name == "session" and args.get("action") == "current":
        result = connections.current_session(db_path, tracker, client, pid)
    elif name in ("review_approve", "review_reject"):
        result = _review_decide(name, pid, args, client)
    elif name == "delete_request" or (name, args.get("action")) in _DELETE_ACTIONS:
        result = _delete_request(pid, name, args, client)
    elif name == "link":
        result = _link(pid, args, client)
    elif name == "resolve_conflict_card":
        result = conflict_cards.resolve_card(db_path, args, client)
    elif name == "review_mode":
        result = _review_mode(args, client)
    elif name == "history":
        result = _history(pid, args, client)
    elif name == "cleanup_suggest":
        result = _cleanup_suggest(pid, args, client)
    elif name == "import_memory":
        result = _import_memory(pid, args, client)
    elif name == "todo" and args.get("action") in ("next", "block", "unblock"):
        result = _claim_after(pid, args, _todo_extra(pid, args, client), client)
    elif name == "todo" and args.get("action") in ("claim", "renew", "release", "handoff"):
        result = _todo_claim(pid, args, client)
    elif name == "todo" and args.get("action") == "list" and pid is not None:
        try:
            result = tasks.list_todos(pid, args.get("status"), args.get("priority"),
                                      args.get("min_priority"), args.get("sort"),
                                      bool(args.get("compact")))
            _safe_call(claims.annotate, db_path, result, client)
        except tasks.TaskError as e:
            result = e.payload
    elif name in extras.TOOLS:
        result = extras.handle(name, pid, args, client, db_path)
    elif (legacy := legacy_args.legacy_record(db_path, name, args, pid)) is not None and \
            review_mode.should_queue(db_path, pid, legacy["kind"]):
        result = _suggest(pid, legacy, client)
        if isinstance(result, dict) and "note" in result:
            result["note"] += " (the user's review mode holds new records of this kind)"
    else:
        if name == "session" and args.get("action") == "log_conversation" and not args.get("session_id") and sid:
            args["session_id"] = str(sid)
        if name in ("log_change",) or (name == "problem" and args.get("action") == "log"):
            if args.get("component_id") is None:
                args["component_id"] = _component_id(pid, None)
        if name == "todo" and args.get("action") == "add":
            bad = _check_new_todo(pid, args)
            if bad:
                return bad
        edit = _history_begin(name, args)
        result = _route_tool(name, args)
        ok = not (isinstance(result, dict) and "error" in result) and result is not None
        if edit is not None and ok:
            _safe_call(edit.finish, client)
        if name == "todo" and args.get("action") == "update" and ok:
            result = _claim_after(pid, args, result, client)
        created = _CREATES.get((name, args.get("action"))) or _CREATES.get((name, None))
        if created and ok and isinstance(result, dict) and isinstance(result.get("id"), int):
            meta["record"] = (created, result["id"])
        if name == "todo" and args.get("action") == "add" and ok and isinstance(result, dict):
            result = _link_new_todo(pid, args, result, client)
        if name == "session" and args.get("action") == "end" and tracker and args.get("session_id"):
            _safe_track(tracker.mark_explicit_end, int(args["session_id"]))
        if name == "log_change" and isinstance(result, dict) and "error" not in result:
            alerted = _alert_watchers(pid, int(args["component_id"]), "change", result.get("id"),
                                      args.get("field_name") or "")
            if alerted:
                result = {**result, "alerted_projects": alerted}
    if sid and tracker:
        failed = isinstance(result, dict) and "error" in result
        if pid and not failed:
            write = is_write(name, args) or (name, args.get("action")) in EXEMPT_ACTIONS
            event = _label(name, args) if write else f"read:{name}"   # review_mode set: a write
            _safe_track(tracker.note, sid, event)
    return result


def _start_here(args: dict, client: str, meta: Optional[dict] = None) -> str:
    db_path, tracker = backend.db_path, backend.tracker
    res = resolve_project(db_path, args.get("project"), args.get("folder"))
    if "project" not in res:
        gone = _safe_call(_deleted_projects, db_path)
        return candidates_text(res) + (f"\n{gone}" if gone else "")
    project = res["project"]
    if meta is not None:
        meta["project_id"] = project["id"]
    _capture_git(project["id"])
    sid = _safe_track(tracker.touch, client, project["id"], "read:start_here", *identity.conn()) \
        if tracker else None
    if meta is not None:
        meta.update(session_id=sid, shown=[])
    session = _safe_track(tracker.current, client, project["id"], identity.conn()[0]) \
        if (sid and tracker) else None
    try:
        since = _safe_call(ledger.since_line, db_path, project, identity.current(), sid, meta)
        text = build_brief(db_path, project, session, size=args.get("size") or "standard",
                           shown=(meta or {}).get("shown"), since=since)
    except Exception as e:  # noqa: BLE001 - brief problems must not break the agent
        return (f"FlowState: project '{project['name']}' (#{project['id']}) found, but the brief "
                f"could not be built ({type(e).__name__}). Carry on; use search if needed.")
    try:  # "changed since you last looked" is relative to this moment
        links.mark_looked(db_path, project["id"], client)
    except Exception as e:  # noqa: BLE001
        log.warning("could not record start_here time: %s", e)
    warning = "\n".join(w for w in (backend.health, _relay_warning(), updates.brief_lines(__version__)) if w)
    return f"{warning}\n\n{text}" if warning else text


def _deleted_projects(db_path) -> Optional[str]:
    conn = deletes.connect(db_path)
    try:
        return deletes.recent_line(conn, None)
    finally:
        conn.close()


def _relay_warning() -> Optional[str]:
    """The relay CT alert line for start_here, else None. Without relay state on disk (never
    enrolled) this is one file check: no relay module is imported or run."""
    env = os.environ.get("FLOWSTATE_RELAY_DIR")
    try:
        from . import paths
        root = Path(env).expanduser() if env else paths.data_root() / "relay"
        if not (root / "ct.json").exists():
            return None
    except Exception:  # noqa: BLE001 - a status line must never break a tool
        return None
    from .relay.store import tool_warning
    return tool_warning()


def _capture_git(pid: int) -> None:
    """Opt-in (flowstate git-capture enable): new commit subjects become changes."""
    from . import git_capture
    try:
        if git_capture.is_on(backend.db_path, pid):
            res = git_capture.run(backend.db_path, pid, _save_commit)
            if res.get("captured"):
                log.info("git capture: %s new commit(s) for project %s", res["captured"], pid)
    except Exception as e:  # noqa: BLE001 - never stops the brief
        log.warning("git capture skipped for project %s: %s", pid, e)


def _save_commit(pid: int, summary: str, details: str):
    args = {"kind": "change", "summary": summary, "details": details, "component": "Git",
            "source": "from_code"}
    if review_mode.should_queue(backend.db_path, pid, "change"):
        return review.add_item(backend.db_path, pid, "change", summary, details,
                               {"component": "Git", "source": "from_code"}, "git-capture")
    return _log_work(pid, args, None, ("git-capture", None))


def _component_id(pid: int, name: Optional[str]) -> int:
    """Component by name within the project (case-insensitive); created if missing."""
    wanted = (name or "General").strip() or "General"
    conn = links.connect(backend.db_path)
    try:
        row = conn.execute(
            "SELECT id FROM components WHERE project_id = ? AND lower(name) = lower(?) ORDER BY id LIMIT 1",
            (pid, wanted)).fetchone()
        if row is None and name:
            shared = links.shared_component_by_name(conn, pid, wanted)
            if shared is not None:
                return shared  # a component another project owns and this one links to
    finally:
        conn.close()
    if row:
        return row[0]
    return tools.create_component(pid, wanted, "Created by FlowState log_work" if wanted == "General" else None)["id"]


def _log_work(pid: int, args: dict, sid: Optional[int], by: Optional[tuple] = None,
              client: Optional[str] = None) -> Any:
    kind, summary = args.get("kind"), (args.get("summary") or "").strip()
    details = args.get("details") or None
    if not summary:
        return {"error": "summary_required", "message": "log_work needs a one-line summary."}
    try:  # also reached from review approve, whose payload may carry a new-style name
        prio = priority.store(args.get("priority"))
    except priority.PriorityError as e:
        return priority.error_payload(e)
    source = args.get("source") or None
    if source is not None and source not in provenance.SOURCES:
        return {"error": "bad_source", "message": "source must be one of "
                + ", ".join(provenance.SOURCES) + "."}
    source, source_note = provenance.at_log_time(   # no way around the user's confirmation
        backend.db_path, pid, kind, summary, source, by[0] if by else identity.stamp(client)["app"])
    replaces = args.get("replaces")
    bad = conflict_cards.precheck(backend.db_path, pid, args)
    if bad:
        return bad
    if replaces is not None and kind != "decision":
        return {"error": "bad_request", "message": "replaces only applies to kind=decision."}
    if replaces is not None:
        conn = links.connect(backend.db_path)
        try:
            if links.owner(conn, "decision", replaces) is None:
                return {"error": "record_not_found", "message": f"No decision with id {replaces}."}
        finally:
            conn.close()
    comp_id = resolved = None
    if kind == "decision":
        r = tools.log_conversation(pid, f"Decision: {summary}", details, [summary],
                                   str(sid) if sid else None)
        if args.get("component"):
            comp_id = _component_id(pid, args["component"])
            _safe_link(pid, "about", item_type="decision", item_id=r["id"],
                       target_type="component", target_id=comp_id)
        if replaces is not None:
            _safe_link(pid, "supersedes", item_type="decision", item_id=r["id"],
                       target_type="decision", target_id=int(replaces))
            resolved = _safe_call(conflicts.resolve_replaced, backend.db_path, r["id"], int(replaces))
    elif kind == "change":
        comp_id = _component_id(pid, args.get("component"))
        r = tools.log_change(comp_id, summary, None, None, "other", details)
    elif kind == "problem":
        r = tools.log_problem(_component_id(pid, args.get("component")), summary, details, prio)
    elif kind == "learning":
        comp = _component_id(pid, args["component"]) if args.get("component") else None
        r = tools.log_learning(pid, summary, args.get("category") or "other", details, comp, "experience")
    elif kind == "todo":
        comp = _component_id(pid, args["component"]) if args.get("component") else None
        r = tools.add_todo(pid, summary, details, prio, comp, None)
    else:
        return {"error": "bad_kind", "message": "kind must be decision, change, problem, learning or todo."}
    out = {"logged": kind, "project_id": pid, "record": r}
    if source and isinstance(r, dict) and r.get("id") is not None:
        _safe_call(provenance.set_source, backend.db_path, kind, r["id"], source, client, by)
        out["source"] = source
        out.update({"source_note": source_note.format(id=r["id"])} if source_note else {})
    if comp_id is not None and kind in ("change", "decision"):
        alerted = _alert_watchers(pid, comp_id, kind, r.get("id") if isinstance(r, dict) else None,
                                  summary)
        if alerted:
            out["alerted_projects"] = alerted
    if replaces is not None:
        out["replaces"] = int(replaces)
        if resolved:
            out["resolved_conflicts"] = resolved
    elif kind == "decision" and isinstance(r, dict) and r.get("id") is not None:
        # possible conflicting decisions: a note only; never blocks or undoes the write
        out.update(_safe_call(conflicts.on_save, backend.db_path, pid, r["id"], summary, comp_id) or {})
    out.update(_safe_call(conflict_cards.after_log, backend.db_path, pid, kind, args, out, client) or {})
    return out


def _safe_link(pid: int, link_type: str, **kw) -> None:
    try:
        links.add(backend.db_path, pid, link_type, **kw)
    except Exception as e:  # noqa: BLE001 - the record itself is saved
        log.warning("link %s not recorded: %s", link_type, e)


def _alert_task(project_id: int, title: str, details: str):
    """The update todo for a shared-component alert, following the review mode."""
    if review_mode.should_queue(backend.db_path, project_id, "todo"):
        item = review.add_item(backend.db_path, project_id, "todo", title, details,
                               {"priority": "medium", "link_alert": True}, "link-alert")
        return None, item
    return tools.add_todo(project_id, title, details, "medium", None, None)["id"], None


def _alert_watchers(pid: int, comp_id: int, kind: str, record_id, summary: str) -> list:
    """A change/decision on a component other projects link to: alert them."""
    try:
        res = links.on_component_event(backend.db_path, pid, comp_id, kind, record_id, summary,
                                       _alert_task)
    except Exception as e:  # noqa: BLE001 - never break the write that triggered it
        log.warning("shared-component alerts skipped: %s", e)
        return []
    conn = links.connect(backend.db_path)
    try:
        return [{"project": links.project_name(conn, a["project_id"]), "todo_id": a["todo_id"],
                 **({"review_item_id": a["review_item_id"]} if a["review_item_id"] else {})}
                for a in res]
    finally:
        conn.close()


# ============================================================
# LINKS AND REVIEW MODE
# ============================================================

_LINK_SPEC = ("item_type", "item_id", "component", "target_project", "target_component",
              "target_type", "target_id", "url")


def _link(pid: Optional[int], args: dict, client: str) -> Any:
    action = args.get("action")
    spec = {k: args.get(k) for k in _LINK_SPEC if args.get(k) not in (None, "")}
    try:
        if action == "add":
            res = links.add(backend.db_path, pid, args.get("link_type"), note=args.get("note"),
                            client=client, **spec)
            if args.get("link_type") == "supersedes" and spec.get("item_type") == "decision" \
                    and not res.get("existing") and spec.get("target_id") is not None:
                resolved = _safe_call(conflicts.resolve_replaced, backend.db_path,
                                      int(spec["item_id"]), int(spec["target_id"]), client)
                if resolved:
                    res = {**res, "resolved_conflicts": resolved}
            return res
        if action == "remove":
            link_id = args.get("link_id")
            if link_id is None:
                link_id = links.find_id(backend.db_path, pid, args.get("link_type"), **spec)
            with history.tracked(backend.db_path, [("record_links", "id", int(link_id))],
                                 "link/remove", client):
                return links.remove(backend.db_path, pid, int(link_id), client)
        if action == "list":
            if pid is None:
                return {"error": "project_required", "message": "manage_links list needs project=\"<name>\"."}
            return links.list_links(backend.db_path, pid, spec.get("item_type"), spec.get("item_id"),
                                    spec.get("component"), bool(args.get("include_removed")))
        tried = conflict_cards.try_link(backend.db_path, pid, args, client)
        if tried is not None:
            return tried
        return {"error": "bad_action", "message": "action must be add, remove, list or flag_conflict."}
    except links.LinkError as e:
        return e.payload


def _review_mode(args: dict, client: str) -> Any:
    pid = None
    named = explicit_project(args)
    if named is not None:
        res = resolve_project(backend.db_path, named, fuzzy=False)
        if "project" not in res:
            return {"error": "project_not_found", "message": f"No single project matches {named!r}.",
                    "candidates": res.get("candidates", [])}
        pid = res["project"]["id"]
    if args.get("action") == "set":
        who, _scopes = _caller()
        try:
            if approvals.is_on(backend.db_path, pid):   # turning review off waits for the user
                item = review_mode.queue_if_off(backend.db_path, args.get("mode"), args.get("kinds"),
                                                pid, args.get("user_request") or "", client,
                                                bool(args.get("clear")))
                if item:
                    return _user_only({"setting_requested": item}, item, client)
            return review_mode.set_mode(backend.db_path, args.get("mode"), args.get("kinds"), pid,
                                        args.get("user_request") or "", client, who,
                                        clear=bool(args.get("clear")))
        except review_mode.ModeError as e:
            return e.payload
    m = review_mode.get(backend.db_path, pid)
    return {**m, "text": review_mode.describe(m), "modes": list(review_mode.MODES),
            "note": "Only change this when the user asks you to in this chat."}


def _safe_bool(fn, *a) -> bool:
    try:
        return bool(fn(*a))
    except Exception as e:  # noqa: BLE001 - a broken switch never blocks the user's app
        log.warning("pause check skipped: %s", e)
        return False


def _safe_call(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001 - bookkeeping never breaks a call
        log.warning("%s skipped: %s", getattr(fn, "__name__", "bookkeeping"), e)
        return None


def _history_begin(name: str, args: dict):
    """Copy the rows an edit is about to change (None if this call isn't an edit)."""
    return _safe_call(history.begin, backend.db_path, name, args)


def _history(pid: Optional[int], args: dict, client: str) -> Any:
    if args.get("action") == "who":
        return _who(pid, args)
    if pid is None:
        return {"error": "project_required", "message": "manage_history needs project=\"<name>\"."}
    try:
        if args.get("action") == "undo":
            if not args.get("record_type") or args.get("record_id") is None:
                return {"error": "record_required",
                        "message": "undo needs record_type and record_id (see manage_history list)."}
            return history.undo(backend.db_path, pid, args["record_type"], int(args["record_id"]),
                                client)
        return provenance.annotate(backend.db_path, history.list_history(   # who set each tag
            backend.db_path, pid, args.get("record_type"), args.get("record_id"),
            args.get("limit") or 20), "history", "record_type", "record_id")
    except history.HistoryError as e:
        return e.payload


def _who(pid: Optional[int], args: dict) -> Any:
    """history(action="who"): read-only "who did what" (who.py), the review page's list."""
    from . import who
    if pid is None and explicit_project(args) is not None:
        return {"error": "project_not_found",
                "message": f"No single project matches {explicit_project(args)!r}."}
    try:
        res = who.recent(backend.db_path, args.get("by_app"), args.get("by_model"),
                         args.get("by_machine"), pid, args.get("since"), args.get("until"),
                         args.get("page") or 1, args.get("limit") or 20)
    except who.WhoError as e:
        return e.payload
    for r in res["rows"]:   # keep it short for the model's context
        for k in ("at_utc", "project_id", "identity_source"):
            r.pop(k, None)
        if r.get("session") is None:
            r.pop("session", None)
        if r["undo"] is None:
            r.pop("undo")
        else:
            r["undo"].pop("project_id", None)
    return {**res, "note": "model is shown only when the app supplied it; identity says how "
                           "the app name was established (only per-app keys, remote tokens and "
                           "OAuth apps can't pick their own name)."}


def _check_new_todo(pid: int, args: dict) -> Optional[dict]:
    """Refuse a subtask/blocker that points nowhere before the todo is created."""
    conn = links.connect(backend.db_path)
    try:
        par = args.get("parent_todo_id")
        if par is not None:
            owner = links.owner(conn, "todo", par)
            if owner is None:
                return {"error": "record_not_found", "message": f"No todo with id {par}."}
            if owner != pid:
                return {"error": "project_mismatch",
                        "message": f"Parent todo #{par} is in another project; subtasks stay in one project."}
        for b in args.get("blocked_by") or []:
            if links.owner(conn, "todo", b) is None:
                return {"error": "record_not_found", "message": f"No todo with id {b}."}
        p = args.get("blocked_by_problem")
        if p is not None and links.owner(conn, "problem", p) is None:
            return {"error": "record_not_found", "message": f"No problem with id {p}."}
    finally:
        conn.close()
    return None


def _link_new_todo(pid: int, args: dict, result: dict, client: str) -> dict:
    if args.get("parent_todo_id") is None and not args.get("blocked_by") \
            and args.get("blocked_by_problem") is None:
        return result
    try:
        extra = tasks.link_new_todo(backend.db_path, pid, result["id"], args.get("parent_todo_id"),
                                    args.get("blocked_by"), args.get("blocked_by_problem"), client)
    except links.LinkError as e:
        return {**result, "link_error": e.payload}
    return {**result, **extra}


def _todo_extra(pid: Optional[int], args: dict, client: str) -> Any:
    act = args.get("action")
    try:
        if act == "next":
            if pid is None:
                return {"error": "project_required", "message": "manage_todos next needs project=\"<name>\"."}
            return tasks.what_next(backend.db_path, pid, args.get("limit") or 5)
        tid = args.get("todo_id")
        if tid is None:
            return {"error": "todo_id_required", "message": f"manage_todos {act} needs todo_id."}
        if act == "block":
            return tasks.block(backend.db_path, pid, int(tid), args.get("blocked_by"),
                               args.get("blocked_by_problem"), client)
        ids = tasks.blocker_links(backend.db_path, int(tid), args.get("blocked_by"),
                                  args.get("blocked_by_problem"))
        for lid in ids:
            with history.tracked(backend.db_path, [("record_links", "id", lid)], "todo/unblock",
                                 client):
                row = links.get_row(backend.db_path, lid)
                links.remove(backend.db_path, row["from_project_id"], lid, client)
        return {"todo_id": int(tid), "unblocked_links": ids,
                "note": "Nothing was blocking it." if not ids else "Blockers removed."}
    except links.LinkError as e:
        return e.payload


def _todo_claim(pid: int, args: dict, client: str) -> Any:
    """claim / renew / release / handoff (claims.py); a refusal names the holder."""
    act, tid, db = args["action"], args.get("todo_id"), backend.db_path
    try:
        if act == "handoff":
            return claims.handoff(db, pid, tid, args.get("where_i_was"), args.get("next_step"),
                                  client)
        if act == "release":
            return claims.release(db, pid, tid, client)
        return (claims.claim if act == "claim" else claims.renew)(db, pid, tid,
                                                                  args.get("minutes"), client)
    except links.LinkError as e:
        raise GuardError(e.payload)


def _claim_after(pid: Optional[int], args: dict, result: Any, client: str) -> Any:
    """No hard lock: a change to a todo another agent claimed is saved with a warning (and an
    override event); done / cancelled clears the claim."""
    if pid is None or args.get("todo_id") is None or not isinstance(result, dict) \
            or "error" in result:
        return result
    act = args.get("action")
    warn = _safe_call(claims.after_update, backend.db_path, pid, int(args["todo_id"]), act,
                      args.get("status") if act == "update" else None, client)
    return {**result, "claim_warning": warn} if warn else result


_SCAN_PER_ITEM = {"import_memory": ("text",)}
MAX_IMPORT = 200_000


def _import_memory(pid: int, args: dict, client: str) -> Any:
    text = args.get("text") or ""
    if not text.strip():
        return {"error": "text_required", "message": "Pass the export or file contents as text."}
    if len(text) > MAX_IMPORT:
        return {"error": "too_large", "message": f"Import at most {MAX_IMPORT:,} characters at a time."}
    source = (args.get("source") or "").strip()[:40] or None
    if args.get("format") == "flowstate_markdown":
        from . import markdown_io
        res = markdown_io.import_text(backend.db_path, pid, text, source or "markdown")
    else:
        from . import memory_import
        try:
            res = memory_import.import_memories(backend.db_path, pid, text, source or f"{client} import",
                                                args.get("kind") or "learning")
        except ValueError as e:
            return {"error": "bad_request", "message": str(e)}
    return {**res, "project_id": pid,
            "note": "Everything waits in the review queue for the user; nothing was saved as a record."}


def _cleanup_suggest(pid: int, args: dict, client: str) -> Any:
    kinds = tuple(args.get("kinds") or ("duplicate", "stale", "misfiled"))
    found = cleanup.suggest(backend.db_path, pid, kinds)
    try:
        q = cleanup.queue(backend.db_path, found, source=f"cleanup ({client})"[:80])
    except review.ReviewError as e:
        return e.payload
    return {**q, "found": len(found), "project_id": pid,
            "examples": [s["summary"] for s in found[:8]],
            "note": "Suggestions only; they wait in the review queue for the user."}


# ============================================================
# REVIEW QUEUE
# ============================================================

_RECORD_TABLE = {"decision": "conversations", "change": "changes", "problem": "problems",
                 "learning": "learnings", "todo": "todos"}


def _suggest(pid: int, args: dict, client: str) -> Any:
    kind, summary = args.get("kind"), (args.get("summary") or "").strip()
    if kind not in review.RECORD_KINDS:
        return {"error": "bad_kind", "message": "kind must be decision, change, problem, learning or todo."}
    if not summary:
        return {"error": "summary_required", "message": "log_work needs a one-line summary."}
    payload = {k: args[k] for k in ("component", "priority", "category", "replaces", "source",
                                    "evidence", "conflicts_with", "conflicts_with_type", "topic")
               if args.get(k) not in (None, "")}
    item = review.add_item(backend.db_path, pid, kind, summary, args.get("details") or None,
                           payload, client)
    if item is None:
        return {"error": "review_queue_unavailable",
                "message": "The review queue isn't set up yet; log it without suggested=true "
                           "if the user wants it recorded."}
    return {"queued_for_review": item, "kind": kind, "project_id": pid,
            "note": "Saved to the review queue, not as a record. The user decides."}


def _review_list(args: dict, walk: bool = False) -> Any:
    return waiting.review_list(args, walk, _caller()[1])   # one page, oldest first; after=


# The older direct deletes: (tool, action) -> (record type, argument holding its id). Since
# v2.1 they ask the user too, like manage_memory(action="delete").
_DELETE_ACTIONS = {("variable", "delete"): ("variable", "variable_id"),
                   ("method", "delete"): ("method", "method_id"),
                   ("file", "remove"): ("attachment", "attachment_id")}


def _delete_request(pid: int, name: str, args: dict, client: str) -> Any:
    """Queue a delete for the user's approval (deletes.py). Never deletes anything."""
    act = args.get("action")
    if name != "delete_request":
        rtype, key = _DELETE_ACTIONS[(name, act)]
        rid = args.get(key)
    elif act == "project":
        rtype, rid = "project", pid
    elif act == "component":
        rtype, rid = "component", args.get("component_id")
    elif act == "record":
        rtype, rid = args.get("record_type"), args.get("record_id")
        if rtype not in deletes.RECORD_TYPES:
            return {"error": "bad_type",
                    "message": f"record_type must be one of: {', '.join(deletes.RECORD_TYPES)}. "
                               f"Projects and components: manage_projects(action=\"delete\" / "
                               f"\"delete_component\")."}
    else:
        return {"error": "bad_action", "message": "action must be project, component or record."}
    try:
        if not approvals.is_on(backend.db_path, pid):   # the default: at once, undoable
            return deletes.delete_now(backend.db_path, pid, rtype, rid, client)
        out = deletes.request(backend.db_path, pid, rtype, rid, client, {"user_only": True})
    except (deletes.DeleteError, safety.SafetyError) as e:
        return e.payload
    return _user_only(out, out["delete_requested"], client)


def _user_only(out: dict, item_id: int, client: str) -> dict:
    """A queued user-only item's answer: popup next (in _call_internal), else one line."""
    ask = approvals.ask_payload(backend.db_path, item_id, client)
    out.pop("note", None)
    out["status"] = "waiting for the user"
    out["_ask_user"] = ask
    return out


def _review_decide(name: str, pid: int, args: dict, client: str) -> Any:
    if args.get("item_id") is None:
        return {"error": "item_id_required", "message": "Name the review item (item_id)."}
    if identity.current() is not None:   # an AI through the relay: phone only, by default
        no = waiting.phone_only(backend.db_path, args["item_id"])
        if no:
            return no
    user_request = (args.get("user_request") or "").strip()
    if not user_request:
        return {"error": "user_request_required",
                "message": "Only the user can approve or reject review items. Ask them; pass "
                           "their words as user_request."}
    done: dict = {}
    try:
        item = review.get_item(backend.db_path, int(args["item_id"]))
        agent = identity.current() is not None   # None: command line, review page, popup, phone
        locked = approvals.user_only(item) and item["status"] == "pending"
        if locked and agent and (no := provenance.agent_refusal(backend.db_path, item, name)):
            return no   # a "did the user say this?" item: the user's alone
        if locked and agent and name == "review_approve":   # only the user's own click or key
            return _user_only({"item_id": item["id"], "kind": item["kind"]}, item["id"], client)
        own = review.own_item_refusal(item, identity.current(), user_request)
        if own:
            return own
        if name == "review_approve" and item and item.get("kind") == "disagreement":
            bad = conflict_cards.keep_error(args)
            if bad:
                return bad
        if name == "review_approve":
            if item and item["kind"] == "delete":   # checked before anything is claimed
                from .remote_auth import DELETE_REFUSAL, may_delete
                if not may_delete(_caller()[1]):
                    return {"error": DELETE_REFUSAL[0], "message": DELETE_REFUSAL[1]}
                deletes.check_approval(item, args.get("confirm_name"))
        if name == "review_reject":
            out = review.reject(backend.db_path, int(args["item_id"]), args.get("reason") or "",
                                user_request, client)
            if (item or {}).get("kind") != "disagreement" and (
                    locked or approvals.logs_every_decision(client, agent)):   # chat walk, phone list too
                _safe_call(approvals.log_decision, backend.db_path, item,
                           "agent" if agent else approvals.path_of(client), client, "rejected")
            if out.get("kind") == "conflict":   # the user says the two decisions fit together
                out.update(conflicts.dismiss(backend.db_path, int(args["item_id"]), client))
            elif out.get("kind") == "disagreement":
                out.update(_safe_call(conflict_cards.on_reject, backend.db_path, item, client) or {})
            return out

        def apply(item: dict, payload: dict):   # a source tag is the queuing app's say-so
            r = _log_work(item["project_id"], payload, None, (item.get("app") or client, item.get("model")))
            if isinstance(r, dict) and "error" in r:
                raise review.ReviewError(r["error"], r.get("message", "could not create the record"))
            rec = r.get("record") if isinstance(r, dict) else None
            rid = rec.get("id") if isinstance(rec, dict) else None
            if rid is not None:
                _safe_call(history.record_created, backend.db_path, payload["kind"], rid,
                           "approve", client)
            return _RECORD_TABLE[payload["kind"]], rid

        move_to = None
        if args.get("move_to") not in (None, ""):
            res = resolve_project(backend.db_path, args["move_to"], fuzzy=False)
            if "project" not in res:
                return {"error": "project_not_found", "message": f"No single project matches "
                        f"{args['move_to']!r}.", "candidates": res.get("candidates", [])}
            move_to = res["project"]["id"]

        def apply_cleanup(item: dict, _edits: dict):
            return cleanup.apply(backend.db_path, item, client, move_to)

        def apply_edit(item: dict, _edits: dict):
            from .markdown_io import apply_edit as do
            return do(backend.db_path, item, client)

        def apply_conflict(item: dict, _edits: dict):
            done["note"] = ("Kept as a possible conflict; it stays in the brief until a decision "
                            "replaces one of them (log_work replaces= or manage_links supersedes). "
                            "Ask the user which one stands.")
            return conflicts.confirm(backend.db_path, item)

        def apply_delete(item: dict, _edits: dict):
            done.update(deletes.execute(backend.db_path, item, client, args.get("confirm_name")))
            return deletes.TYPES[done["deleted"]["record_type"]][0], done["deleted"]["id"]

        def apply_setting(item: dict, _edits: dict):
            return review_mode.apply_queued(backend.db_path, item, client)

        edits = {k: args.get(k) for k in ("summary", "details", "priority", "category", "component")}
        if safety.needs_copy(item):   # a verified copy first, or no change at all
            done["safety_copy"] = safety.copy_before(backend.db_path).name
        def apply_disagreement(item: dict, _edits: dict):
            return conflict_cards.apply_keep(backend.db_path, item, args.get("keep"), client)

        out = review.approve(backend.db_path, int(args["item_id"]), user_request, client, apply, edits,
                             others={"cleanup": apply_cleanup, "edit": apply_edit,
                                     "delete": apply_delete, "conflict": apply_conflict,
                                     "setting": apply_setting, "disagreement": apply_disagreement,
                                     "source": lambda it, _e: provenance.confirm(backend.db_path, it)})
        if (item or {}).get("kind") != "disagreement" and (locked or approvals.logs_every_decision(client, agent)):
            _safe_call(approvals.log_decision, backend.db_path, item, approvals.path_of(client),
                       client, "approved")
        return {**out, **done}
    except (review.ReviewError, deletes.DeleteError, safety.SafetyError) as e:
        return e.payload


guide.register_prompts(server)
conflict_cards.register(server)


# ============================================================
# SERVER STARTUP
# ============================================================

def reap_interval() -> float:
    try:
        return max(0.2, float(os.environ.get("FLOWSTATE_REAP_SECONDS", "60")))
    except ValueError:
        return 60.0


async def _reaper() -> None:
    """Close idle automatic sessions (any process's) every FLOWSTATE_REAP_SECONDS."""
    while True:
        if backend.tracker is not None:
            try:
                await asyncio.to_thread(backend.tracker.close_idle)
            except Exception as e:  # noqa: BLE001
                log.warning("idle reaper skipped: %s", e)
        await asyncio.sleep(reap_interval())


def _close_own_sessions(reason: str = "shutdown") -> None:
    if backend.tracker is not None:
        try:
            backend.tracker.close_instance(reason)
        except Exception as e:  # noqa: BLE001
            log.warning("closing sessions on shutdown failed: %s", e)


async def run_server(db_path: Optional[Path] = None):
    """Run the MCP server over stdio (desktop apps)."""
    init_backend(db_path)
    reaper = asyncio.create_task(_reaper())
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        reaper.cancel()
        _close_own_sessions("shutdown")


def _load_token(args) -> str:
    """Bearer token: FLOWSTATE_TOKEN, else --token-file (legacy), else the OS credential
    store (Credential Manager / Keychain / 0600 file). Never logged."""
    from .secret_store import load_token
    tok, source = load_token(args.token_file, warn=lambda m: log.warning("token: %s", m),
                             fallback_file=args.token_fallback_file)
    if tok:
        log.info("HTTP bearer token loaded from %s", source)
    return tok


def create_http_app(*args, **kwargs):
    """Starlette app for the streamable HTTP transport (http_app.py)."""
    from .http_app import create_http_app as make
    return make(*args, **kwargs)


def run_http(host: str, port: int, token: Optional[str], path: str = "/mcp",
             db_path: Optional[Path] = None, public_url: Optional[str] = None,
             allow_master_token: bool = False):
    """Serve MCP over streamable HTTP. uvicorn's own access log is off (it would log query
    strings); remote_auth.Gate logs one line per request without query or body."""
    import uvicorn
    app = create_http_app(token, path, db_path, public_url=public_url,
                          allow_master_token=allow_master_token)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False,
                proxy_headers=False)


def main():
    """Entry point. Default: stdio. Optional: --transport http."""
    import argparse
    from . import cli
    if len(sys.argv) > 1 and sys.argv[1] in cli.COMMANDS:
        raise SystemExit(cli.main(sys.argv[1:]))
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="flowstate", epilog="Commands: " + ", ".join(cli.COMMANDS)
                                 + " (flowstate <command> -h for each)")
    ap.add_argument("--transport", choices=["stdio", "http"],
                    default=os.environ.get("FLOWSTATE_TRANSPORT", "stdio"))
    ap.add_argument("--host", default=os.environ.get("FLOWSTATE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("FLOWSTATE_PORT", "8765")))
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--token-fallback-file", default=None,
                    help="read only if FLOWSTATE_TOKEN and the credential store have no token")
    ap.add_argument("--db", default=None, help="database file (default: FLOWSTATE_DB / per-OS)")
    ap.add_argument("--public-url", default=os.environ.get("FLOWSTATE_PUBLIC_URL"),
                    help="https URL the tunnel serves (turns on OAuth + public-mode checks)")
    ap.add_argument("--allow-master-token", action="store_true",
                    help="public mode: also accept the owner's LAN token")
    ap.add_argument("--tools", choices=["default", "core", "full", "all"], default=None,
                    help="which tools to list (also FLOWSTATE_TOOLS): default 12, core 6, "
                         "full 18 (all = full); every tool name still answers")
    args = ap.parse_args()
    if args.tools:
        os.environ["FLOWSTATE_TOOLS"] = args.tools
    db_path = Path(args.db).expanduser() if args.db else None
    if args.transport == "http":
        token = _load_token(args)
        if args.public_url:
            # (the MCP SDK refuses a non-https public URL unless it is 127.0.0.1/localhost)
            if args.host not in ("127.0.0.1", "localhost", "::1"):
                log.warning("public mode is meant to listen on 127.0.0.1 behind the tunnel")
        elif len(token) < 16:
            raise SystemExit("HTTP transport requires a bearer token of 16+ characters: set "
                             "FLOWSTATE_TOKEN, or store one with `flowstate token migrate --from <file>`")
        run_http(args.host, args.port, token, db_path=db_path, public_url=args.public_url,
                 allow_master_token=args.allow_master_token)
    else:
        asyncio.run(run_server(db_path))


def run():
    main()


if __name__ == "__main__":
    main()
