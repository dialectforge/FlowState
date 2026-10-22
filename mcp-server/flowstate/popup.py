"""The in-chat popup for user-only approvals (approvals.py): an MCP elicitation form with one
required checkbox, `approve` (plus an optional note; a project delete also asks for its name).

Sent with the installed MCP SDK's server-to-client request (`session.elicit_form`, protocol
2025-06-18 / 2025-11-25). The SDK installed with this release (mcp 1.30) has no support for
the 2026-07-28 style, so that one isn't offered. The wait (at most approvals.POPUP_SECONDS)
runs after the tool's own work has committed, outside the call's time limit and holding no
database lock; only accept with approve=true approves. Anything else leaves the item queued.
"""

import asyncio
import logging
from typing import Optional

from . import approvals

log = logging.getLogger("flowstate.popup")

APPROVE_FIELD = {"type": "boolean", "title": "Approve",
                 "description": "Tick to approve. Leave it unticked, decline or close to say no."}
NOTE_FIELD = {"type": "string", "title": "Note (optional)", "maxLength": 300}


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


async def ask(session, message: str, form: dict, related_request_id=None,
              timeout: Optional[float] = None) -> tuple[str, dict]:
    """(outcome, content): outcome approved / declined / cancelled / timed_out / failed."""
    try:
        res = await asyncio.wait_for(session.elicit_form(message, form, related_request_id),
                                     approvals.POPUP_SECONDS if timeout is None else timeout)
    except asyncio.TimeoutError:
        return "timed_out", {}
    except Exception as e:  # noqa: BLE001 - a broken popup is a "no", never an approval
        log.warning("popup failed: %s", e)
        return "failed", {}
    if res.action == "decline":
        return "declined", {}
    if res.action != "accept":
        return "cancelled", {}
    content = res.content if isinstance(res.content, dict) else {}
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
    if session is None or not approvals.popup_allowed(db_path, client, declared(session)):
        result["next"] = ask_user["line"]
        return result
    outcome, content = await ask(session, ask_user["message"], schema(ask_user.get("project_name")),
                                 request_id)
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
