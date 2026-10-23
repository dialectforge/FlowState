"""Where the user sees what's waiting for them, without a terminal (2.4.0).

Three ways, none of which shows anything unless the user asks (or has approvals on):

  1. In the chat: manage_review(action="walk"). On apps that show MCP elicitation forms as a
     real dialog (the approvals popup's allowlist, approvals.popup_allowed) and reach FlowState
     on this computer (stdio or the LAN listener; never the relay or the public listener, where
     the app's name is its own say-so: popup.no_box), each waiting item comes up one at a time
     as an Approve / Reject box and FlowState acts on the user's own answer. A box answered
     faster than a person could (popup.HUMAN_SECONDS) counts as not shown: the walk stops and
     logs nothing for it. Elsewhere it answers with one page of the list in plain words (20,
     oldest first; after=<id> for the next page) and the links below, and says that no boxes
     were shown and nothing was decided. It never approves anything itself, and nothing an app
     passes along counts as the user's answer.
  2. A local link, http://127.0.0.1:<port>/: the review page (review_page.py) started inside
     this FlowState process the first time a link is needed, one per computer and database
     (a running one is reused), on 127.0.0.1 only. Offered (and started) only for apps on this
     computer (popup.on_this_computer: stdio, or the LAN listener with a loopback peer); on any
     other computer 127.0.0.1 is that computer itself, so those apps get the phone link or a
     plain note to ask for the list instead (`flowstate review-page` on this computer).
  3. Relay users: a bookmarkable phone link to the whole list, https://flowstatemcp.com/review/
     <handle> (approvals.review_link, review_phone.py; no code in the chat).

Plus two the user opens themselves: the "FlowState review" desktop shortcut setup makes
(`flowstate review-page`), and, for users with their own tunnel, https://<tunnel>/review/tunnel
behind the owner passphrase (review_tunnel.py). ELSEWHERE names both for apps elsewhere.

Decisions made in 1 and 3 go through the same user-only path as the popup and the phone page
(popup.as_user) and are all logged in approval_log (path "chat" / "phone"; the tunnel page:
"tunnel page").
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
def links(db_path: Path, local: Optional[bool] = None) -> dict:
    """The local link (only for an app on this computer; the page isn't started for others)
    and, for relay users, the phone link (only what is available)."""
    from .popup import on_this_computer
    out = {}
    if local is None:
        local = on_this_computer()
    page = local_link(db_path) if local else None
    if page:
        out["review_page"] = page
    try:
        phone = approvals.review_link(db_path)
    except Exception:  # noqa: BLE001 - no phone link, nothing more
        phone = None
    if phone:
        out["phone"] = phone
    return out


def where_line(db_path: Path) -> str:
    """The brief's addition to "N items waiting for the user's OK"."""
    from .popup import on_this_computer
    local = on_this_computer()
    ways = links(db_path, local)
    parts = []
    if ways.get("review_page"):
        parts.append(f"on the computer where FlowState runs at {ways['review_page']}")
    if ways.get("phone"):
        parts.append(f"on their phone: {ways['phone']}")
    parts.append('in this chat when they ask (manage_review action="walk")')
    if not local:   # an app on another computer: no 127.0.0.1 link (it would open its own)
        parts.append(ELSEWHERE)
    return " The user can see and decide them " + ", ".join(parts[:-1]) + \
        (" or " if len(parts) > 1 else "") + parts[-1] + "."


# for apps on another computer (relay, public, LAN from elsewhere), instead of a 127.0.0.1 link
ELSEWHERE = ("on the computer where FlowState runs (the FlowState review shortcut, or "
             "`flowstate review-page`), or on its tunnel review page if it has one")


# ---------------------------------------------------------------- 1. the chat walk
_KIND = {"session_summary": "session summary", "cleanup": "clean-up idea", "edit": "markdown edit",
         "conflict": "possible conflict", "disagreement": "disagreement",
         "setting": "review setting", "source": "said by you?", "delete": "delete request"}


LINE_MAX = 160          # one item's line, as the app reads it in the JSON answer
LIST_BUDGET = 12_000    # all lines of one page (limit 100: 120 each; the answer stays < 16,000)
FULL_ITEM = ('One item in full (details, payload; needed before approving with edits): '
             'manage_review(action="list", item_id=<id>).')


def _fit(text: str, room: int) -> str:
    """`text` on one line, cut so its JSON form (non-ASCII escaped) takes at most `room`."""
    text = " ".join(str(text or "").split())
    if len(json.dumps(text)) - 2 <= room:
        return text
    used, cut = 3, 0                    # 3 for the "..."
    for ch in text:
        used += len(json.dumps(ch)) - 2
        if used > room:
            break
        cut += 1
    return text[:cut].rstrip() + "..."


def _plain(it: dict, room: int = LINE_MAX, status: bool = False) -> str:
    """#id (project, kind[, status]): summary, at most `room` characters in the JSON answer."""
    kind = _KIND.get(it["kind"], it["kind"]) + (f", {it['status']}" if status else "")
    head = f"#{it['id']} ({_fit(it['project'], 40)}, {kind}): "
    from .review import strip_tag
    return head + _fit(strip_tag(it.get("summary")), max(20, room - (len(json.dumps(head)) - 2)))


KEEP_NOTE = ('A session summary asks the user "Keep this session summary?": approve keeps it as '
             "the session's record (edits allowed); reject means don't keep it (the session keeps "
             "only a note saying so); nothing is deleted.")


def _keep_note(items: list) -> dict:
    return {"session_summary": KEEP_NOTE} if any(it["kind"] == "session_summary" for it in items) else {}


def _lines(items: list, status: bool = False, db_path: Optional[Path] = None) -> list:
    """One line each. A disagreement or a delete is the server's own text (the card, or the
    rows that would go), not a 160-character cut of whatever an agent wrote."""
    from . import conflict_cards, deletes
    room = min(LINE_MAX, LIST_BUDGET // max(1, len(items)))
    out = []
    for it in items:
        try:
            if db_path is not None and it.get("kind") == "disagreement":
                out.append(conflict_cards.list_line(db_path, it, status))
                continue
            if db_path is not None and it.get("kind") == "delete":
                out.append(deletes.list_line(db_path, it, status))
                continue
        except Exception:  # noqa: BLE001 - a list line must not fail the whole answer
            log.warning("review line for #%s skipped", it.get("id"))
        out.append(_plain(it, room, status))
    return out


def _page(listing: dict, after: Optional[int], action: str) -> dict:
    """Which part of the waiting list this answer shows, and how to ask for the next part."""
    items = listing.get("items") or []
    out = {}
    if items:
        out["showing"] = (f"{len(items)} of {listing['total']}, oldest first: #{items[0]['id']} to "
                          f"#{items[-1]['id']}")
        if listing.get("more"):
            out["next_page"] = f'manage_review(action="{action}", after={items[-1]["id"]})'
    return out


def review_list(args: dict, walk: bool, scopes) -> dict:
    """manage_review list / walk (server._handle): one page of the queue, oldest first.
    `after` (an item id) gives the next page; `total` counts every match."""
    from . import review
    from . import server as S
    from .brief import resolve_project
    from .guard import explicit_project
    pid = None
    named = explicit_project(args)
    if named is not None:
        res = resolve_project(S.backend.db_path, named, fuzzy=False)
        if "project" not in res:
            return {"error": "project_not_found", "message": f"No single project matches {named!r}.",
                    "candidates": res.get("candidates", [])}
        pid = res["project"]["id"]
    if not walk and args.get("item_id") is not None:
        return _one_item(S.backend.db_path, args["item_id"], pid, named)
    after = args.get("after")
    after = after if isinstance(after, int) and not isinstance(after, bool) and after > 0 else None
    try:
        out = review.list_items(S.backend.db_path, pid,
                                "pending" if walk else args.get("status") or "pending",
                                WALK_MAX if walk else args.get("limit") or 20, after=after)
    except review.ReviewError as e:
        return e.payload
    if walk:
        return prepare(out, S.backend.db_path, scopes, pid, after)
    page = _page(out, after, "list")   # one line per item; item_id= for one in full
    db = S.backend.db_path
    result = {"total": out["total"], "status": out["status"], **page,
              "items": _lines(out["items"], out["status"] != "pending", db),
              **_keep_note(out["items"]),
              **({"full_item": FULL_ITEM} if out["items"] else {}), "note": out["note"]}
    cards = []
    for it in out["items"]:
        if it.get("kind") != "disagreement":
            continue
        packed = _safe_card(db, it)
        if packed:
            cards.append({"review_item_id": packed["review_item_id"], "text": packed["text"]})
            result.setdefault("_ui", {"structured": packed["structured"]})
    if cards:
        result["conflict_cards"] = cards
    return result


def _safe_card(db_path: Path, it: dict):
    from . import conflict_cards
    try:
        return conflict_cards.ui_for_item(db_path, it)
    except Exception:  # noqa: BLE001
        log.warning("conflict card for #%s skipped", it.get("id"))
        return None


def _one_item(db_path: Path, item_id, pid: Optional[int], named) -> dict:
    """manage_review list item_id=<id>: that one item with every field (any status)."""
    try:
        item_id = int(item_id)
        it = _item_view(db_path, item_id) if item_id > 0 else None
    except (TypeError, ValueError):
        return {"error": "invalid_item_id", "message": f"item_id must be a number, not {item_id!r}."}
    except Exception as e:  # noqa: BLE001 - e.g. no review queue in this database
        return getattr(e, "payload", None) or {"error": "review_queue_unavailable", "message": str(e)}
    if not it or (pid is not None and it["project_id"] != pid):
        where = f" in project {named!r}" if pid is not None else ""
        return {"error": "record_not_found", "message": f"No review item #{item_id}{where}. "
                'manage_review(action="list", status="all") lists the ids.'}
    answer = {"item": it, "note": "Only approve or reject when the user explicitly asks you to."}
    if it.get("kind") == "delete":
        from . import deletes
        got = deletes.preview(db_path, it)
        it["would_delete"] = got.get("would_delete")
        it["summary"] = got["headline"]
        it["details"] = got["details"]
        if got.get("ai_note"):
            it["ai_note"] = got["ai_note"]
    elif it.get("kind") == "disagreement":
        packed = _safe_card(db_path, it)
        if packed:
            it["conflict_card"] = packed["text"]
            answer["conflict_cards"] = [{"review_item_id": packed["review_item_id"],
                                         "text": packed["text"]}]
            answer["_ui"] = {"structured": packed["structured"]}
    return answer


def prepare(listing: dict, db_path: Path, scopes, project_id: Optional[int] = None,
            after: Optional[int] = None) -> dict:
    """review_walk's first part (in the call's worker thread): one page of the waiting items,
    and what to do with them after the call (_walk, read by popup.ask_user in server.py)."""
    if not isinstance(listing, dict) or "items" not in listing:
        return listing   # an error (no queue, unknown project)
    items = listing["items"][:WALK_MAX]
    out = {"waiting": listing["total"], **_page(listing, after, "walk"),
           "items": _lines(items, db_path=db_path), **_keep_note(items)}
    if not items:
        out["note"] = (f"Nothing more is waiting after #{after}." if after
                       else "Nothing is waiting for the user.")
        return out
    from .popup import on_this_computer
    local = on_this_computer()
    # A disagreement has four outcomes, so it is not a yes/no box. The card text stays
    # in the answer (walk() puts it back after it drops the item lines).
    cards = [conflict_cards_line(db_path, it) for it in items if it.get("kind") == "disagreement"]
    out["_walk"] = {"ids": [it["id"] for it in items if it.get("kind") != "disagreement"],
                    "cards": [c for c in cards if c], "project_id": project_id,
                    "may_decide": scopes is None or "flowstate:write" in scopes,
                    "links": links(db_path, local), "local": local}
    return out


def conflict_cards_line(db_path: Path, it: dict) -> Optional[str]:
    from . import conflict_cards
    try:
        return conflict_cards.list_line(db_path, it, False)
    except Exception:  # noqa: BLE001
        return None


NOT_SHOWN = "No boxes were shown here; nothing was decided."


def how_to_decide(ways: dict, why: str, local: bool = True, show: str = "this list") -> str:
    """The walk's plain guidance (also given with every answer that queues something for the
    user, queue_guidance): why no boxes, where the user decides, and not to decide for them."""
    where = [f"{'on the computer where FlowState runs' if k == 'review_page' else 'on their phone'}: {v}"
             for k, v in ways.items()]
    if not local:   # no 127.0.0.1 link for an app on another computer
        where.append(ELSEWHERE)
    return (f"{why} Show the user {show}. They decide each item themselves: "
            + "; ".join(where) + ("; " if where else "")
            + "or in a terminal: `flowstate review`. Don't "
            "approve or reject anything for them.")


def _plain_answer(out: dict, ways: dict, why: str, local: bool = True) -> dict:
    out["note"] = NOT_SHOWN
    out["how_to_decide"] = how_to_decide(ways, why, local)
    out.update(ways)
    return out


# ---------------------------------------------------------------- answers that queue something
# Where an answer names what it just put in the review queue: one id, or a list of ids, or
# review_item_id inside each entry of a list (possible conflicts, shared-component alerts).
QUEUED_ONE = ("queued_for_review", "delete_requested", "setting_requested", "confirm_requested")
QUEUED_LISTS = ("item_ids",)
QUEUED_NESTED = ("possible_conflicts", "alerted_projects", "conflict_cards")
DONT_APPROVE = ("Don't approve or reject this yourself, even if the user tells you to in this "
                "chat: show the user the link and let them decide it there.")
DONT_APPROVE_NO_LINK = ("Don't approve or reject this yourself, even if the user tells you to in "
                        "this chat: tell the user it is waiting for them and where to decide it.")


def _is_id(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def queued_ids(result) -> list:
    """The review item ids this answer says it queued for the user (none: [])."""
    if not isinstance(result, dict) or "error" in result:
        return []
    out = [result[k] for k in QUEUED_ONE if _is_id(result.get(k))]
    for k in QUEUED_LISTS:
        v = result.get(k)
        if isinstance(v, list):
            out += [i for i in v if _is_id(i)]
    for k in QUEUED_NESTED:
        v = result.get(k)
        if isinstance(v, list):
            out += [e["review_item_id"] for e in v
                    if isinstance(e, dict) and _is_id(e.get("review_item_id"))]
    return list(dict.fromkeys(out))


def queue_guidance(result, db_path: Optional[Path], client: str):
    """An answer that put something in the review queue gets the walk's guidance: how the
    user decides (how_to_decide), the same links (phone when the relay link is known; the
    local page for apps on this computer) and a plain line not to decide it for them.
    Never breaks the answer; anything else passes through unchanged."""
    if db_path is None or not queued_ids(result):
        return result
    try:
        from . import server as S
        from .popup import no_box, on_this_computer
        local = on_this_computer()
        ways = links(db_path, local)
        try:
            session = S.server.request_context.session
        except LookupError:
            session = None
        why = no_box(session, client, db_path)
        if why is None:   # this app shows boxes: the walk is one more way
            why = ('This app can show FlowState\'s Approve / Reject boxes, but only when the user '
                   'asks for them (manage_review action="walk").')
        for k in ("review_page", "phone"):
            if k in ways and k not in result:
                result[k] = ways[k]
        result["how_to_decide"] = how_to_decide(ways, why, local, "the link" if ways else
                                                "where to decide it")
        result["dont_approve"] = DONT_APPROVE if ways else DONT_APPROVE_NO_LINK
    except Exception as e:  # noqa: BLE001 - guidance is extra; the queued item is saved
        log.warning("review guidance skipped: %s", e)
    return result


# ---------------------------------------------------------------- approve / reject via the relay
PHONE_ONLY = ("Nothing was decided: review item #{id} still waits for the user. Through the "
              "relay, approving and rejecting review items in a chat is off on this FlowState "
              "(the default), even when the user asks for it here: the user decides each item "
              "themselves, on their phone. Only the user can change this, on the computer where "
              "FlowState runs.")
PHONE_ONLY_WHY = ("This request came through the relay, where approving and rejecting in the "
                  "chat is off on this FlowState.")


def phone_only(db_path: Optional[Path], item_id) -> Optional[dict]:
    """An AI's manage_review approve / reject (server._review_decide). Through the relay, while
    chat approvals are off (the default; `flowstate relay chat-approvals on` allows them), a
    pending item is refused with its own phone link (else the review list's) and the walk's
    guidance. None: go on as usual (another connection, the setting is on, or the item is
    missing or already decided: the usual answer says so)."""
    from .popup import listener
    if db_path is None or listener() != "relay" or approvals.relay_chat_on(db_path):
        return None
    from . import review
    try:
        item = review.get_item(db_path, int(item_id))
    except Exception:  # noqa: BLE001 - not found / bad id: the usual answer
        item = None
    if not item or item["status"] != "pending":
        return None
    iid = item["id"]
    try:
        link = approvals.phone_link(db_path, iid)
    except Exception:  # noqa: BLE001 - no phone link, nothing more
        link = None
    ways = {"phone": link} if link else links(db_path, False)
    try:
        nxt = approvals.ask_line(db_path, iid, "approve or reject it")
    except Exception:  # noqa: BLE001
        nxt = f"Waiting for the user: ask them to decide review item #{iid} themselves."
    out = {"error": "phone_only", "message": PHONE_ONLY.format(id=iid), "item_id": iid,
           "kind": item.get("kind"), "status": "waiting for the user", "next": nxt}
    out.update(ways)
    out["how_to_decide"] = how_to_decide(ways, PHONE_ONLY_WHY, False,
                                         "the link" if ways else "where to decide it")
    out["dont_approve"] = DONT_APPROVE if ways else DONT_APPROVE_NO_LINK
    return out


def _form(it: dict, i: int, n: int) -> tuple[str, dict]:
    from .popup import NOTE_FIELD
    details = " ".join(str(it.get("details") or "").split())
    details = details if len(details) <= 600 else details[:597] + "..."
    who = it.get("app") or it.get("source") or "an app"
    from .review import DONT_KEEP, KEEP, KEEP_HOW, KEEP_QUESTION, strip_tag
    keep = it["kind"] == "session_summary"   # Keep / Don't keep: what approve / reject do
    msg = (f"Waiting for you ({i} of {n}), {it['project']}, {_KIND.get(it['kind'], it['kind'])} "
           f"#{it['id']} from {who}:\n{strip_tag(it.get('summary'))}" + (f"\n{details}" if details else "")
           + (f"\n{KEEP_QUESTION} {KEEP_HOW}\nTick {KEEP} and submit to keep it; submit it unticked "
              f"for {DONT_KEEP}; decline to skip; cancel to stop." if keep else
              "\nTick Approve and submit to approve; submit it unticked to reject; decline to skip; "
              "cancel to stop."))
    props = {"approve": {"type": "boolean", "title": KEEP if keep else "Approve",
                         "description": f"Ticked: {KEEP.lower()}. Unticked: {DONT_KEEP.lower()}."
                         if keep else "Ticked: approve. Unticked: reject."},
             "note": dict(NOTE_FIELD, title="Reason or note (optional)")}
    try:
        p = json.loads(it.get("payload") or "{}")
    except ValueError:
        p = {}
    if it["kind"] == "delete" and p.get("record_type") == "project":
        props["confirm_name"] = {"type": "string", "title": "To approve: type the project's exact name",
                                 "description": f"Deleting a whole project: type {it['project']}"}
    return msg, {"type": "object", "properties": props, "required": ["approve"]}


def _counts(done: list) -> str:
    n = {k: sum(1 for d in done if d.endswith(k))
         for k in (": approved", ": rejected", ": skipped, still waiting")}
    return (f"In the boxes the user approved {n[': approved']}, rejected {n[': rejected']} and "
            f"skipped {n[': skipped, still waiting']}; only those answers were logged.")


async def walk(result: dict, session, client: str) -> dict:
    """After the call: one Approve / Reject box per item on apps that show them, else the
    plain list with links. Each box waits at most approvals.POPUP_SECONDS, holding no lock.
    A box answered faster than a person could (popup.HUMAN_SECONDS) was not shown: the walk
    stops there, logs nothing for it and, if nothing was decided, answers with the plain list."""
    from . import server as S
    from .popup import as_user, elicit, no_box
    plan = result.pop("_walk")
    db = S.backend.db_path
    ways, local = plan["links"], plan.get("local", True)
    if not plan["may_decide"]:
        return _plain_answer(result, ways, "This app has read-only access, so FlowState shows "
                                           "no Approve / Reject boxes here.", local)
    why = no_box(session, client, db)
    if why:
        return _plain_answer(result, ways, why, local)
    try:
        rid = S.server.request_context.request_id
    except LookupError:
        rid = None
    label = f"chat ({client})"
    done, n, unseen = [], len(plan["ids"]), False
    for i, item_id in enumerate(plan["ids"], 1):
        it = await asyncio.to_thread(S._safe_call, _item_view, db, item_id)
        if not it or it["status"] != "pending":
            done.append(f"#{item_id}: already decided elsewhere")
            continue
        msg, form = _form(it, i, n)
        action, content = await elicit(session, msg, form, rid)
        if action == "not_shown":   # answered by software, not a person: stop, log nothing
            unseen = True
            break
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
    if unseen and not done:   # not one box reached a person: the honest plain list
        return _plain_answer(result, ways, "This app answered FlowState's box at once, faster "
                                           "than a person could, so no box was shown.", local)
    left = await asyncio.to_thread(S._safe_call, _pending_total, db, plan.get("project_id"))
    for k in ("items", "showing", "next_page"):
        result.pop(k, None)
    result["walked"] = done
    result["still_waiting"] = left
    if left:
        result.update(ways)
    if plan.get("cards"):
        result["conflict_cards"] = plan["cards"]
    result["note"] = _counts(done) + (
        " Then this app answered a box at once, faster than a person could, so FlowState "
        "stopped: that box was not shown and nothing more was decided." if unseen else "")
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
