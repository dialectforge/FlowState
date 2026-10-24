"""What a session summary is about (2.4.4): the records the session saved, by id.

An automatic session's summary (autosession.build_summary) counts what was saved ("saved 1
change; also problem solved"). On its own that is a blind Keep / Don't keep, so every place
that shows a session summary (the local and tunnel review pages, the phone list, the approve
page, the in-chat review card and its text, the chat walk) also shows what this module finds:
each record's kind, id, title and the first EXCERPT characters of its text, the solution of a
solved problem, the component, and the session's app and times.

Where the ids come from:
  - "records" in the review item's payload: [type, id, verb] lists written when the session
    closed (autosession._close_row, from refs()). Ids only, never the text: the text is read
    from the records themselves when shown, so an edited record shows as it is now and a
    deleted one says so.
  - else the run ledger's run_writes rows for the session (2.4.0 and later, kept for the
    activity log's days), for summaries queued before 2.4.4;
  - else nothing: the summary's own counts are all there is ("source": "none").

Nothing here writes. Text is returned plain; html() escapes everything it puts in a page.
"""

import html as _html
import json
import sqlite3
from typing import Optional

MAX_SHOWN = 10        # entries shown before "N more"
PHONE_MAX = 100       # entries the phone pages get in their data (the rest: the computer's page)
EXCERPT = 200         # characters of a record's text
TITLE = 120
MAX_REFS = 200        # stored per summary (ids only)

# Writes that save no new work (autosession.HOUSEKEEPING, by tool / tool+action).
_SKIP_TOOLS = {"history", "review_approve", "review_reject", "resolve_review_card",
               "resolve_conflict_card", "show_review_card", "show_conflict_card"}
_SKIP_ACTIONS = {("handoff", "pickup"), ("session", "start"), ("session", "end"),
                 ("todo", "claim"), ("todo", "renew"), ("todo", "release")}
# What the write did to the record, when it isn't a new save.
_VERBS = {("problem", "solve"): "solved", ("todo", "update"): "updated",
          ("todo", "block"): "blocked", ("todo", "unblock"): "unblocked",
          ("todo", "handoff"): "handed off", ("attempt", "outcome"): "result recorded",
          ("rules", "remove"): "removed", ("link", "remove"): "removed",
          ("update_project", None): "updated", ("update_component", None): "updated",
          ("verify", "mark"): "checked", ("verify", "source"): "source changed"}
# Record types shown, in plain words. Links and components only when the write was about them
# (a decision's "about" link and an auto-made component are part of that decision / change).
KIND_WORDS = {"change": "change", "decision": "decision", "problem": "problem",
              "learning": "learning", "todo": "todo", "attempt": "fix attempt", "rule": "rule",
              "handoff": "handoff note", "review_item": "waiting for review", "link": "link",
              "component": "component", "project": "project"}
_OWN_TOOL = {"link": ("link",), "component": ("create_component", "update_component"),
             "project": ("update_project",)}


def _cut(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _verb(tool: str, action: Optional[str], rtype: str) -> str:
    if rtype == "review_item":
        return "queued"
    return _VERBS.get((tool, action)) or _VERBS.get((tool, None)) or "saved"


def _skip(tool: str, action: Optional[str]) -> bool:
    return tool in _SKIP_TOOLS or (tool, action) in _SKIP_ACTIONS


def _has(conn, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def refs(conn, session_id: int) -> list:
    """[type, id, verb] for each record the session's writes saved, oldest first, from the
    run ledger (empty before 2.4.0 or without one). One entry per record: a record saved and
    then solved in one session reads "saved, solved"."""
    if session_id is None or not _has(conn, "run_writes"):
        return []
    out, at = [], {}
    rows = conn.execute("SELECT tool, action, records FROM run_writes WHERE session_id = ? "
                        "AND status = 'saved' ORDER BY id", (session_id,)).fetchall()
    for tool, action, recs in rows:
        if _skip(tool, action):
            continue
        try:
            recs = json.loads(recs or "[]")
        except ValueError:
            continue
        for ref in recs if isinstance(recs, list) else []:
            rtype, _, rid = str(ref).partition(":")
            if rtype == "solution" or rtype not in KIND_WORDS or not rid.isdigit():
                continue   # a solution is shown with its problem
            if rtype in _OWN_TOOL and tool not in _OWN_TOOL[rtype]:
                continue
            verb, key = _verb(tool, action, rtype), (rtype, int(rid))
            if key in at:
                if verb not in out[at[key]][2].split(", "):
                    out[at[key]][2] += ", " + verb
                continue
            at[key] = len(out)
            out.append([rtype, int(rid), verb])
            if len(out) >= MAX_REFS:
                return out
    return out


def stored(payload) -> Optional[list]:
    """The [type, id, verb] list stored with a review item, or None if it has none."""
    try:
        p = json.loads(payload or "{}") if isinstance(payload, str) else (payload or {})
    except ValueError:
        return None
    recs = p.get("records") if isinstance(p, dict) else None
    if not isinstance(recs, list):
        return None
    return [r for r in recs if isinstance(r, list) and len(r) == 3 and r[0] in KIND_WORDS
            and isinstance(r[1], int) and not isinstance(r[1], bool) and isinstance(r[2], str)]


# ---------------------------------------------------------------- reading the records
def _one(conn, sql: str, *a):
    try:
        return conn.execute(sql, a).fetchone()
    except sqlite3.OperationalError:   # an older database without that table
        return None


def _component(conn, cid) -> str:
    if cid is None:
        return ""
    r = _one(conn, "SELECT name FROM components WHERE id = ?", cid)
    return r[0] if r else ""


def entry(conn, rtype: str, rid: int, verb: str) -> dict:
    e = {"kind": rtype, "kind_label": KIND_WORDS.get(rtype, rtype), "id": rid, "verb": verb,
         "title": "", "excerpt": "", "component": "", "solution": "", "gone": False}
    r = None
    if rtype == "change":
        r = _one(conn, "SELECT field_name, reason, new_value, component_id FROM changes WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or r[2] or "", component=_component(conn, r[3]))
    elif rtype == "decision":
        r = _one(conn, "SELECT user_prompt_summary, assistant_response_summary FROM conversations "
                       "WHERE id = ?", rid)
        if r:
            t = r[0] or ""
            e.update(title=t[len("Decision: "):] if t.startswith("Decision: ") else t,
                     excerpt=r[1] or "")
    elif rtype == "problem":
        r = _one(conn, "SELECT title, description, component_id, status FROM problems WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "", component=_component(conn, r[2]),
                     status=r[3] or "")
            s = _one(conn, "SELECT summary, key_insight FROM solutions WHERE problem_id = ?", rid)
            if s and ("solved" in verb or r[3] == "solved"):
                e["solution"] = _cut(s[0] or s[1], EXCERPT)
    elif rtype == "learning":
        r = _one(conn, "SELECT insight, context, component_id FROM learnings WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "", component=_component(conn, r[2]))
    elif rtype == "todo":
        r = _one(conn, "SELECT title, description, component_id, status FROM todos WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "", component=_component(conn, r[2]),
                     status=r[3] or "")
    elif rtype == "attempt":
        r = _one(conn, "SELECT a.description, a.notes, a.outcome, a.problem_id, p.title FROM "
                       "solution_attempts a LEFT JOIN problems p ON p.id = a.problem_id "
                       "WHERE a.id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "",
                     status=r[2] or "", problem=f"problem #{r[3]} {_cut(r[4], 60)}".strip())
    elif rtype == "rule":
        r = _one(conn, "SELECT rule FROM project_rules WHERE id = ?", rid)
        if r:
            e["title"] = r[0]
    elif rtype == "handoff":
        r = _one(conn, "SELECT where_i_was, next_step FROM handoff_notes WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=("Next: " + r[1]) if r[1] else "")
    elif rtype == "review_item":
        r = _one(conn, "SELECT kind, summary, details, status FROM review_items WHERE id = ?", rid)
        if r:
            e.update(kind_label=f"{str(r[0]).replace('_', ' ')} waiting for review",
                     title=r[1], excerpt=r[2] or "", status=r[3] or "")
    elif rtype == "link":
        r = _one(conn, "SELECT from_type, from_id, link_type, to_type, to_id, url FROM record_links "
                       "WHERE id = ?", rid)
        if r:
            to = f"{r[3]} #{r[4]}" if r[4] is not None else ("a web link" if r[5] else r[3])
            e["title"] = f"{r[0]} #{r[1]} {str(r[2]).replace('_', ' ')} {to}"
    elif rtype == "component":
        r = _one(conn, "SELECT name, description FROM components WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "")
    elif rtype == "project":
        r = _one(conn, "SELECT name, description FROM projects WHERE id = ?", rid)
        if r:
            e.update(title=r[0], excerpt=r[1] or "")
    if r is None:
        e.update(gone=True, title="(no longer in FlowState)")
    e["title"], e["excerpt"] = _cut(e["title"], TITLE), _cut(e["excerpt"], EXCERPT)
    return e


def contents(conn, item: dict) -> dict:
    """What a session_summary review item is about: the session (app, times in UTC) and its
    records. `source` says where the ids came from: stored | ledger | none."""
    from .identity import shown_row
    out = {"project": item.get("project") or "",
           "app": shown_row(item, fallback=item.get("source") or "") if item.get("app")
           else item.get("source") or "",
           "machine": item.get("machine") or "", "session_id": item.get("session_id"),
           "identity_source": item.get("identity_source") or "",
           "started_utc": "", "ended_utc": "", "entries": [], "total": 0, "source": "none"}
    if not out["project"]:
        r = _one(conn, "SELECT name FROM projects WHERE id = ?", item.get("project_id"))
        out["project"] = r[0] if r else ""
    sid = item.get("session_id")
    if sid is not None:
        r = _one(conn, "SELECT started_at, ended_at FROM sessions WHERE id = ?", sid)
        if r:
            out["started_utc"], out["ended_utc"] = str(r[0] or "")[:19], str(r[1] or "")[:19]
    recs = stored(item.get("payload"))
    if recs is not None:
        out["source"] = "stored"
    elif sid is not None:
        recs = refs(conn, sid)
        out["source"] = "ledger" if recs else "none"
    recs = recs or []
    out["entries"] = [entry(conn, t, i, v) for t, i, v in recs]
    out["total"] = len(out["entries"])
    return out


def for_item(db_path, item: dict) -> Optional[dict]:
    """contents() for a session_summary item (its own connection), else None. Never raises:
    a page still shows the summary if this fails."""
    if not item or item.get("kind") != "session_summary":
        return None
    from . import dbconn
    try:
        conn = dbconn.connect(str(db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            return contents(conn, item)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - the summary text is still shown
        return None


# ---------------------------------------------------------------- words
NONE_NOTE = ("FlowState has no list of the records this session saved (a summary from before "
             "2.4.4 may not have one, once its run log is cleared), so only the counts above are "
             "known. Search the project to see them.")


def head(e: dict) -> str:
    """'change #12 "Fix login redirect" (Auth)' / 'problem #3 "Login loop", solved'."""
    words = f'{e["kind_label"]} #{e["id"]}' + (f' "{e["title"]}"' if not e["gone"] else
                                               f' {e["title"]}')
    if e.get("component"):
        words += f" ({e['component']})"
    if e["verb"] and e["verb"] not in ("saved", "queued"):
        words += f", {e['verb']}"
    return words


def lines(c: Optional[dict], limit: int = MAX_SHOWN) -> list:
    """Plain text, one or two lines per record, at most `limit` records, then "N more"."""
    if not c:
        return []
    if not c["entries"]:
        return [NONE_NOTE]
    out = [f"What this session saved ({c['total']}):"]
    for e in c["entries"][:limit]:
        out.append("- " + head(e) + (f": {e['excerpt']}" if e["excerpt"] else ""))
        if e["solution"]:
            out.append(f"  Solution: {e['solution']}")
    if c["total"] > limit:
        out.append(f"... and {c['total'] - limit} more (the review page on this computer lists all).")
    return out


def when_line(c: Optional[dict], local=None) -> str:
    """'Session 2026-10-24 14:02 to 14:40 UTC, by claude-code on <machine>'. `local` turns a
    UTC time into the reader's (the local page); without it the times say UTC."""
    if not c or not c.get("started_utc"):
        return ""
    fmt = local or (lambda s: s)
    a, b = fmt(c["started_utc"])[:16], fmt(c["ended_utc"])[:16] if c.get("ended_utc") else ""
    if b and b[:10] == a[:10]:
        b = b[11:]
    span = a + (f" to {b}" if b else "") + (" (this computer's local time)" if local else " UTC")
    from .identity import via
    # "Grok Bot (via Cursor)" already names the sign-in; its machine column says it again
    signed = via(c.get("machine"), c.get("identity_source"))
    who = c["app"] + (f" on {c['machine']}" if c.get("machine") and not signed else "")
    return f"Session {span}" + (f", by {who}" if who else "") + f", in {c['project']}."


def public(c: Optional[dict], limit: int = MAX_SHOWN, send: Optional[int] = None) -> Optional[dict]:
    """The JSON form for the phone, approve page and chat card: `shown` (the first `limit`)
    entries show at once; `send` (default `limit`) of them are in the data, so a page can open
    the rest in place (the phone pages: PHONE_MAX); `more` counts those not sent at all."""
    if not c:
        return None
    send = max(limit, send or limit)
    keys = ("kind", "kind_label", "id", "verb", "title", "excerpt", "component", "solution", "gone")
    return {"entries": [{k: e[k] for k in keys} | {"head": head(e)} for e in c["entries"][:send]],
            "shown": limit, "total": c["total"], "more": max(0, c["total"] - send),
            "source": c["source"],
            "started_utc": c["started_utc"], "ended_utc": c["ended_utc"], "app": c["app"],
            "machine": c["machine"], "project": c["project"],
            "note": "" if c["entries"] else NONE_NOTE}


def html(c: Optional[dict], local=None, limit: int = MAX_SHOWN) -> str:
    """The block for the server-rendered pages: all text escaped; past `limit` the rest is in
    a <details> the reader opens (no script, so it works under any CSP)."""
    if not c:
        return ""
    e_ = _html.escape
    out = ["<div class='contents'>"]
    w = when_line(c, local)
    if w:
        out.append(f"<p class='meta'>{e_(w)}</p>")
    if not c["entries"]:
        out.append(f"<p class='meta'>{e_(NONE_NOTE)}</p></div>")
        return "".join(out)

    def li(e: dict) -> str:
        s = f"<li><b>{e_(head(e))}</b>"
        if e["excerpt"]:
            s += f"<br><span class='meta'>{e_(e['excerpt'])}</span>"
        if e["solution"]:
            s += f"<br><span class='meta'>Solution: {e_(e['solution'])}</span>"
        return s + "</li>"
    out.append(f"<p class='meta'>What this session saved ({c['total']}):</p><ul>")
    out.extend(li(e) for e in c["entries"][:limit])
    out.append("</ul>")
    rest = c["entries"][limit:]
    if rest:
        out.append(f"<details><summary>{len(rest)} more</summary><ul>"
                   + "".join(li(e) for e in rest) + "</ul></details>")
    out.append("</div>")
    return "".join(out)
