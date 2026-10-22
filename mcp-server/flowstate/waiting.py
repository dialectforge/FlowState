"""Where the user sees what's waiting for them, without a terminal (2.4.0).

Three ways, none of which shows anything unless the user asks (or has approvals on):

  1. In the chat: manage_review(action="walk"). On apps that show MCP elicitation forms as a
     real dialog (the approvals popup's allowlist, approvals.popup_allowed), each waiting item
     comes up one at a time as an Approve / Reject box and FlowState acts on the user's own
     answer. Elsewhere it answers with the list in plain words and the links below. It never
     approves anything itself, and nothing an app passes along counts as the user's answer.
  2. A local link, http://127.0.0.1:<port>/: the review page (review_page.py) started inside
     this FlowState process the first time a link is needed, one per computer and database
     (a running one is reused), on 127.0.0.1 only.
  3. Relay users: a phone link to the whole list (approvals.review_link, review_phone.py).

Decisions made in 1 and 3 go through the same user-only path as the popup and the phone page
(popup.as_user) and are all logged in approval_log (path "chat" / "phone").
"""

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Optional

from . import approvals

log = logging.getLogger("flowstate.waiting")

PORT_ENV = "FLOWSTATE_REVIEW_PORT"   # default 8770; 0 = any free port; off = no local page
WALK_MAX = 20                        # items shown one after another in one walk
_lock = threading.Lock()
_own: dict = {}                      # this process's page: port, server, thread


# ---------------------------------------------------------------- 2. the local link
def _wanted_port() -> Optional[int]:
    from .review_page import DEFAULT_PORT
    v = (os.environ.get(PORT_ENV) or "").strip().lower()
    if v in ("off", "none", "no"):
        return None
    try:
        return int(v) if v else DEFAULT_PORT
    except ValueError:
        return DEFAULT_PORT


def _state_file(db_path: Path) -> Path:
    from . import paths
    from .review_page import db_tag
    return paths.data_root() / "review-page" / f"{db_tag(db_path)}.json"


def _ping(port: int, db_path: Path) -> bool:
    """Is a FlowState review page for this database answering on 127.0.0.1:port?"""
    from .review_page import HOST, db_tag
    try:
        req = urllib.request.Request(f"http://{HOST}:{int(port)}/ping")
        with urllib.request.urlopen(req, timeout=0.5) as r:   # noqa: S310 - loopback only
            data = json.loads(r.read(4096) or b"{}")
    except (OSError, ValueError):
        return False
    return data.get("service") == "flowstate-review" and data.get("db") == db_tag(db_path) \
        and data.get("open_link") is True


def _bind(port: int) -> socket.socket:
    """127.0.0.1 only. The wanted port, else any free one."""
    from .review_page import HOST
    for p in (port, 0):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if sys.platform == "win32":   # no other program may bind the same port over ours
            s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", 0), 1)
        try:
            s.bind((HOST, p))
            s.listen(16)
            return s
        except OSError:
            s.close()
            if p == 0:
                raise
    raise OSError("no port")   # pragma: no cover


def _start(db_fn) -> int:
    """Start this process's page in a background thread; returns its port."""
    import uvicorn
    from .review_page import Page, create_app
    sock = _bind(_wanted_port() or 0)
    port = sock.getsockname()[1]
    page = Page(db_fn, port, open_link=True)
    # log_config=None: uvicorn must not touch logging; stdout is the stdio transport's channel
    server = uvicorn.Server(uvicorn.Config(create_app(page), log_config=None, log_level="warning",
                                           access_log=False, lifespan="off"))
    t = threading.Thread(target=server.run, kwargs={"sockets": [sock]},
                         name="flowstate-review-page", daemon=True)
    t.start()
    _own.update(port=port, server=server, thread=t, page=page, sock=sock)
    return port


def _remember(db_path: Path, port: int) -> None:
    """Note which port serves this database, for the other FlowState processes here."""
    f = _state_file(db_path)
    want = {"port": port, "pid": os.getpid()}
    try:
        if json.loads(f.read_text(encoding="utf-8")) == want:
            return
    except (OSError, ValueError):
        pass
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(want), encoding="utf-8")


def local_link(db_path: Optional[Path]) -> Optional[str]:
    """http://127.0.0.1:<port>/ for this database's review page, starting it here if no
    FlowState on this computer serves it yet. None if turned off or it can't start."""
    from .review_page import HOST
    from . import server as S
    if db_path is None or _wanted_port() is None:
        return None
    try:
        with _lock:
            own = _own.get("thread")
            if own is not None and own.is_alive() and _own.get("server") is not None \
                    and not _own["server"].should_exit:
                port = _own["port"]
                if Path(S.backend.db_path or "").resolve() != Path(db_path).resolve():
                    return None   # this process's page serves another database
                _remember(db_path, port)
            else:
                port = None
                f = _state_file(db_path)
                try:
                    seen = json.loads(f.read_text(encoding="utf-8"))
                    if int(seen.get("pid") or 0) != os.getpid() and _ping(int(seen["port"]), db_path):
                        port = int(seen["port"])   # another FlowState process serves it
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                if port is None:
                    port = _start(lambda: S.backend.db_path)
                    _remember(db_path, port)
            return f"http://{HOST}:{port}/"
    except Exception as e:  # noqa: BLE001 - no link is fine; the other ways still work
        log.warning("review page not started: %s", e)
        return None


# ---------------------------------------------------------------- links in answers
def links(db_path: Path) -> dict:
    """The local link and, for relay users, the phone link (only what is available)."""
    out = {}
    local = local_link(db_path)
    if local:
        out["review_page"] = local
    try:
        phone = approvals.review_link(db_path)
    except Exception:  # noqa: BLE001 - no phone link, nothing more
        phone = None
    if phone:
        out["phone"] = phone
    return out


def where_line(db_path: Path) -> str:
    """The brief's addition to "N items waiting for the user's OK"."""
    ways = links(db_path)
    parts = []
    if ways.get("review_page"):
        parts.append(f"on this computer at {ways['review_page']}")
    if ways.get("phone"):
        parts.append(f"on their phone: {ways['phone']}")
    parts.append('in this chat when they ask (manage_review action="walk")')
    return " The user can see and decide them " + ", ".join(parts[:-1]) + \
        (" or " if len(parts) > 1 else "") + parts[-1] + "."


# ---------------------------------------------------------------- 1. the chat walk
_KIND = {"session_summary": "session summary", "cleanup": "clean-up idea", "edit": "markdown edit",
         "conflict": "possible conflict", "setting": "review setting", "source": "said by you?",
         "delete": "delete request"}


def _plain(it: dict) -> str:
    summary = " ".join(str(it.get("summary") or "").split())
    summary = summary if len(summary) <= 160 else summary[:157] + "..."
    return f"#{it['id']} ({it['project']}, {_KIND.get(it['kind'], it['kind'])}): {summary}"


def prepare(listing: dict, db_path: Path, scopes, project_id: Optional[int] = None) -> dict:
    """review_walk's first part (in the call's worker thread): the waiting items, and what
    to do with them after the call (_walk, read by popup.ask_user in server.py)."""
    if not isinstance(listing, dict) or "items" not in listing:
        return listing   # an error (no queue, unknown project)
    items = listing["items"][:WALK_MAX]
    out = {"waiting": listing["total"], "items": [_plain(it) for it in items]}
    if not items:
        out["note"] = "Nothing is waiting for the user."
        return out
    out["_walk"] = {"ids": [it["id"] for it in items], "project_id": project_id,
                    "may_decide": scopes is None or "flowstate:write" in scopes,
                    "links": links(db_path)}
    return out


def _plain_answer(out: dict, ways: dict, why: str) -> dict:
    out["how_to_decide"] = (f"{why} Show the user this list. They decide each item themselves: "
                            + "; ".join(f"{'on this computer' if k == 'review_page' else 'on their phone'}: {v}"
                                        for k, v in ways.items())
                            + ("; " if ways else "") + "or in a terminal: `flowstate review`. Don't "
                            "approve or reject anything for them.")
    out.update(ways)
    return out


def _form(it: dict, i: int, n: int) -> tuple[str, dict]:
    from .popup import NOTE_FIELD
    details = " ".join(str(it.get("details") or "").split())
    details = details if len(details) <= 600 else details[:597] + "..."
    who = it.get("app") or it.get("source") or "an app"
    msg = (f"Waiting for you ({i} of {n}), {it['project']}, {_KIND.get(it['kind'], it['kind'])} "
           f"#{it['id']} from {who}:\n{it.get('summary') or ''}" + (f"\n{details}" if details else "")
           + "\nTick Approve and submit to approve; submit it unticked to reject; decline to skip; "
             "cancel to stop.")
    props = {"approve": {"type": "boolean", "title": "Approve",
                         "description": "Ticked: approve. Unticked: reject."},
             "note": dict(NOTE_FIELD, title="Reason or note (optional)")}
    try:
        p = json.loads(it.get("payload") or "{}")
    except ValueError:
        p = {}
    if it["kind"] == "delete" and p.get("record_type") == "project":
        props["confirm_name"] = {"type": "string", "title": "To approve: type the project's exact name",
                                 "description": f"Deleting a whole project: type {it['project']}"}
    return msg, {"type": "object", "properties": props, "required": ["approve"]}


async def walk(result: dict, session, client: str) -> dict:
    """After the call: one Approve / Reject box per item on apps that show them, else the
    plain list with links. Each box waits at most approvals.POPUP_SECONDS, holding no lock."""
    from . import server as S
    from .popup import as_user, declared
    plan = result.pop("_walk")
    db = S.backend.db_path
    ways = plan["links"]
    if not plan["may_decide"]:
        return _plain_answer(result, ways, "This app has read-only access, so FlowState shows "
                                           "no Approve / Reject boxes here.")
    if session is None or not approvals.popup_allowed(db, client, declared(session)):
        return _plain_answer(result, ways, "This app can't show FlowState's Approve / Reject "
                                           "boxes.")
    try:
        rid = S.server.request_context.request_id
    except LookupError:
        rid = None
    label = f"chat ({client})"
    done, n = [], len(plan["ids"])
    for i, item_id in enumerate(plan["ids"], 1):
        it = await asyncio.to_thread(S._safe_call, _item_view, db, item_id)
        if not it or it["status"] != "pending":
            done.append(f"#{item_id}: already decided elsewhere")
            continue
        msg, form = _form(it, i, n)
        try:
            res = await asyncio.wait_for(session.elicit_form(msg, form, rid), approvals.POPUP_SECONDS)
            action = res.action
            content = res.content if isinstance(res.content, dict) else {}
        except asyncio.TimeoutError:
            action, content = "timed_out", {}
        except Exception as e:  # noqa: BLE001 - a broken box is never an answer
            log.warning("walk box failed: %s", e)
            action, content = "failed", {}
        choice = content.get("approve") if action == "accept" else None
        if choice is True or choice is False:
            name = "review_approve" if choice else "review_reject"
            note = " ".join(str(content.get("note") or "").split())[:300]
            args = {"item_id": item_id, "confirm_name": content.get("confirm_name"),
                    "user_request": f"The user {'approved' if choice else 'rejected'} this in the "
                                    f"review walk in {client}.",
                    **({"reason": note or "rejected by the user in the chat"} if not choice else {})}
            out = await asyncio.wait_for(asyncio.to_thread(as_user, name, args, label),
                                         S.call_timeout())
            if isinstance(out, dict) and "error" in out:
                done.append(f"#{item_id}: not done ({out.get('message') or out['error']})")
            else:
                done.append(f"#{item_id}: {'approved' if choice else 'rejected'}")
            continue
        outcome = {"decline": "declined", "cancel": "cancelled"}.get(action, action)
        if action == "accept":
            outcome = "declined"   # submitted without saying approve or reject
        await asyncio.to_thread(S._safe_call, approvals.log_decision, db, it, "chat", label, outcome)
        done.append(f"#{item_id}: skipped, still waiting")
        if action != "decline" and action != "accept":   # closed, no answer, or failed: stop
            rest = plan["ids"][i:]
            if rest:
                done.append(f"stopped; {len(rest)} more still waiting")
            break
    left = await asyncio.to_thread(S._safe_call, _pending_total, db, plan.get("project_id"))
    result.pop("items", None)
    result["walked"] = done
    result["still_waiting"] = left
    if left:
        result.update(ways)
    result["note"] = "The user decided these in Approve / Reject boxes in this app; logged."
    return result


def _item_view(db_path: Path, item_id: int) -> Optional[dict]:
    from . import review
    it = review.get_item(db_path, item_id)
    if not it:
        return None
    conn = approvals._conn(db_path)
    try:
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (it["project_id"],)).fetchone()
    finally:
        conn.close()
    it["project"] = row[0] if row else f"project #{it['project_id']}"
    return it


def _pending_total(db_path: Path, project_id: Optional[int] = None) -> int:
    conn = approvals._conn(db_path)
    try:
        if project_id is None:
            return conn.execute("SELECT COUNT(*) FROM review_items WHERE status = 'pending'").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM review_items WHERE status = 'pending' AND "
                            "project_id = ?", (project_id,)).fetchone()[0]
    finally:
        conn.close()
