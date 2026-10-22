"""The in-chat popup for user-only approvals (approvals.py): an MCP elicitation form with one
required checkbox, `approve` (plus an optional note; a project delete also asks for its name).

Sent with the installed MCP SDK's server-to-client request (`session.elicit_form`, protocol
2025-06-18 / 2025-11-25). The SDK installed with this release (mcp 1.30) has no support for
the 2026-07-28 style, so that one isn't offered. The wait (at most approvals.POPUP_SECONDS)
runs after the tool's own work has committed, outside the call's time limit and holding no
database lock; only accept with approve=true approves. Anything else leaves the item queued.

No box at all (no_box) for requests that came through the relay or the public listener: the
app's name there is its own say-so. And an answer that comes back faster than a person could
read the box (HUMAN_SECONDS) means the box was never shown: it counts as no answer, is not
logged as the user's decision, and FlowState falls back to the plain line / list.
"""

import asyncio
import logging
import time
from typing import Optional

from . import approvals

log = logging.getLogger("flowstate.popup")

APPROVE_FIELD = {"type": "boolean", "title": "Approve",
                 "description": "Tick to approve. Leave it unticked, decline or close to say no."}
NOTE_FIELD = {"type": "string", "title": "Note (optional)", "maxLength": 300}
HUMAN_SECONDS = 1.0   # an answer sooner than this came from software: the box wasn't shown
_clock = time.monotonic


def schema(project_name: Optional[str] = None) -> dict:
    props = {"approve": dict(APPROVE_FIELD), "note": dict(NOTE_FIELD)}
    required = ["approve"]
    if project_name:
        props["confirm_name"] = {"type": "string", "title": "Type the project's exact name",
                                 "description": f"Deleting a whole project: type {project_name}"}
        required.append("confirm_name")
    return {"type": "object", "properties": props, "required": required}


def declared(session) -> bool:
    """Did this connection declare form elicitation? (An empty `elicitation: {}` means form.)"""
    try:
        caps = session.client_params.capabilities.elicitation
    except AttributeError:
        return False
    if caps is None:
        return False
    return getattr(caps, "form", None) is not None or getattr(caps, "url", None) is None


def listener() -> str:
    """How this request reached FlowState: stdio, or the HTTP listener remote_auth.Gate
    marked it with (lan / public / relay; "unknown" if unmarked)."""
    from . import server as S
    from .remote_auth import LISTENER_KEY
    try:
        req = S.server.request_context.request
    except LookupError:
        return "stdio"
    if req is None:
        return "stdio"
    return getattr(req, "scope", {}).get(LISTENER_KEY) or "unknown"


def no_box(session, client: str, db_path) -> Optional[str]:
    """Why this connection gets no Approve / Reject box; None if it may get one."""
    via = listener()
    if via == "relay":
        return ("This request came through the relay, where the app's name is only its own "
                "say-so, so FlowState shows no Approve / Reject boxes here.")
    if via not in ("stdio", "lan"):
        return ("This request came from outside this computer, so FlowState shows no "
                "Approve / Reject boxes here.")
    if session is None or not approvals.popup_allowed(db_path, client, declared(session)):
        return "This app can't show FlowState's Approve / Reject boxes."
    return None


async def elicit(session, message: str, form: dict, related_request_id=None,
                 timeout: Optional[float] = None) -> tuple[str, dict]:
    """One box: (action, content). action accept / decline / cancel, timed_out, failed, or
    not_shown (answered in under HUMAN_SECONDS: no person saw it)."""
    t0 = _clock()
    try:
        res = await asyncio.wait_for(session.elicit_form(message, form, related_request_id),
                                     approvals.POPUP_SECONDS if timeout is None else timeout)
    except asyncio.TimeoutError:
        return "timed_out", {}
    except Exception as e:  # noqa: BLE001 - a broken box is a "no", never an approval
        log.warning("box failed: %s", e)
        return "failed", {}
    if _clock() - t0 < HUMAN_SECONDS:
        log.warning("box answered in under %ss (%s): treated as not shown", HUMAN_SECONDS, res.action)
        return "not_shown", {}
    return res.action, res.content if isinstance(res.content, dict) else {}


async def ask(session, message: str, form: dict, related_request_id=None,
              timeout: Optional[float] = None) -> tuple[str, dict]:
    """(outcome, content): outcome approved / declined / cancelled / timed_out / failed /
    not_shown."""
    action, content = await elicit(session, message, form, related_request_id, timeout)
    if action in ("timed_out", "failed", "not_shown"):
        return action, {}
    if action == "decline":
        return "declined", {}
    if action != "accept":
        return "cancelled", {}
    if content.get("approve") is not True:   # required, and exactly true
        return "declined", content
    for key in form.get("required") or ():
        if content.get(key) in (None, ""):
            return "declined", content
    return "approved", content


_SAID = {"declined": "the user said no", "cancelled": "the user closed it",
         "timed_out": "no answer within 2 minutes", "failed": "the app couldn't show it",
         "wrong_name": "the project name didn't match"}


async def resolve(result: dict, session, client: str, db_path, request_id, decide,
                  log_outcome) -> dict:
    """Turn a tool answer that carries `_ask_user` into the final answer: show the popup when
    this app may get one, else pass on the one-line fallback (phone link or terminal).
    `decide(item_id, content)` (async) carries out an approval; `log_outcome(item_id, outcome)`
    (async) records a "no"."""
    ask_user = result.pop("_ask_user")
    item_id = ask_user["item_id"]
    if no_box(session, client, db_path):
        result["next"] = ask_user["line"]
        return result
    outcome, content = await ask(session, ask_user["message"], schema(ask_user.get("project_name")),
                                 request_id)
    if outcome == "not_shown":   # no person saw it: not the user's answer, nothing logged
        result["next"] = ("No popup was shown (this app answered it at once, faster than a "
                          "person could); nothing changed. " + ask_user["line"])
        return result
    if outcome == "approved" and ask_user.get("project_name") \
            and content.get("confirm_name") != ask_user["project_name"]:
        outcome = "wrong_name"
    if outcome != "approved":
        await log_outcome(item_id, outcome)
        result["next"] = (f"Not approved in the popup ({_SAID[outcome]}); nothing changed and item "
                          f"#{item_id} stays queued.")
        return result
    done = await decide(item_id, content)
    if isinstance(done, dict) and "error" in done:
        result["next"] = f"Approved in the popup, but it failed: {done.get('message') or done['error']}"
        return result
    out = {k: v for k, v in result.items() if k in ("receipt",)}
    out.update(done if isinstance(done, dict) else {"result": done})
    out["approved_in"] = "popup"
    return out


async def ask_user(result: dict, session_obj, client: str) -> dict:
    """server.py's hook: the popup for a user-only item, else the one-line phone / terminal step;
    or the review walk the user asked for (waiting.walk)."""
    from . import review
    from . import server as S
    if "_walk" in result:
        from .waiting import walk
        return await walk(result, session_obj, client)
    try:
        rid = S.server.request_context.request_id
    except LookupError:
        rid = None
    label = f"popup ({client})"

    async def decide(item_id: int, content: dict):
        args = {"item_id": item_id, "confirm_name": content.get("confirm_name"),
                "user_request": f"The user approved this in the popup in {client}."}
        return await asyncio.wait_for(asyncio.to_thread(as_user, "review_approve", args, label),
                                      S.call_timeout())

    async def log_outcome(item_id: int, outcome: str):
        item = await asyncio.to_thread(S._safe_call, review.get_item, S.backend.db_path, item_id)
        if item:
            await asyncio.to_thread(S._safe_call, approvals.log_decision, S.backend.db_path, item,
                                    "popup", label, outcome)

    return await resolve(result, session_obj, client, S.backend.db_path, rid, decide, log_outcome)


def as_user(name: str, args: dict, label: str):
    """A decision the user made themselves (popup, phone page): no agent identity, so the
    user-only check lets it through; `label` says how (approvals.path_of)."""
    from . import identity, review
    from . import server as S
    tok = identity.set_current(None)
    try:
        item = review.get_item(S.backend.db_path, int(args["item_id"]))
        return S._review_decide(name, item["project_id"] if item else None, args, label)
    finally:
        identity.reset(tok)
