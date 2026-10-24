"""Opt-in user-only approvals (2.4.0, migration 019).

Off by default (also for upgraded installs): FlowState never asks the user anything. An
agent's delete then goes through at once, after a safety copy, and stays fully undoable
(deletes.py, history.py). Review-mode changes just happen, logged and shown in the brief.

`flowstate approvals on` (per install, or --project for one project) turns them on. Then an
agent's delete request and turning review off wait for the user's own approval, and no app
can pass the user's OK along: only a human click or keypress decides, in this order:

  1. popup   the app shows an MCP elicitation form with one required checkbox `approve`.
             Only for apps on POPUP_CLIENTS (they show a real dialog), only when the
             connection declared the `elicitation` capability, never to a client that calls
             itself claude.ai / Anthropic. FlowState waits at most POPUP_SECONDS, holding no
             database lock. Decline, cancel, approve=false or no answer: not approved, the
             item stays queued. The popup is only as trustworthy as the app showing it.
  2. phone   relay users: a link to flowstatemcp.com, which signs the user in and sends the
             browser to https://<handle>.fsmcp.com/approve/<item> with a short signed grant
             (approve_page.py checks it here with FSA_PUBLIC_KEYS). The phone review list
             of everything waiting (review_phone.py) takes the same grants with action
             open_review and item 0; its bookmarkable link (review_link) has no code in it:
             the site signs the owner in and sends the browser to /review/start?t=<ticket>
             here (a grant with action start_review, verify_start_ticket), which makes a
             fresh one-use code (start_nonce, limited per site user) and sends it back to
             the site to be signed.
  3. terminal `flowstate approve <id>` asks for the user's `y` on their own terminal.

Through the relay (2.4.2), an AI can't approve or reject any review item in the chat by
default: the answer gives the user's phone link instead. `flowstate relay chat-approvals on`
(the user's terminal only; relay_chat_on) lets it quote the user as before, except for the
user-only items above.

Turning approvals on needs nothing; turning them off needs the user's terminal
(`flowstate approvals off` asks for `y` there). Every user-only decision is logged
(approval_log; manage_history list shows it). Unanswered requests never approve themselves.

Honest limit: an agent that controls the user's own logged-in browser or terminal could still
get through. This stops agents approving their own work through FlowState.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn

# Apps known to show the elicitation form as a real dialog to the user (clientInfo name,
# lower case). `flowstate approvals popup add|remove NAME` changes this per install.
POPUP_CLIENTS = ("claude-code", "cursor", "cursor-vscode", "visual studio code",
                 "visual studio code - insiders", "vscode")
# Never a popup for these, whatever they declare: claude.ai, Claude Desktop / mobile and the
# Anthropic connector proxy don't show one (and the proxy drops the connection).
CONNECTOR_MARKS = ("claude-ai", "claude.ai", "claudeai", "anthropic")
POPUP_SECONDS = 120
GRANT_SECONDS = 300          # a phone grant lives 5 minutes
CLOCK_SKEW = 60
NONCE_DAYS = 7               # the phone link's one-use code, then a fresh one is made
START_MINUTES = 15           # a review-list code made by /review/start (review_phone.py)
START_OPEN = 10              # at most this many of those open at once (the oldest expire)
START_PER_HOUR = 30          # and at most this many made per hour per site user (then 429)
# All users together, as a backstop only: every code needs a ticket only the site can sign, so
# this can only be reached with a leaked signing key, and then it still keeps the table small.
START_PER_HOUR_ALL = 120
SITE = "https://flowstatemcp.com"
ACTION = "approve_review"
REVIEW_ACTION = "open_review"   # the phone review list (review_phone.py): item 0
START_ACTION = "start_review"   # the site's ticket for /review/start (review_phone.py): item 0
USER_ONLY_KINDS = ("delete", "setting", "source")

# The flowstatemcp.com approval-grant public keys (ES256 / P-256 JWKs), pinned per release:
# fsa-1 active, fsa-2 backup so the site can rotate without a forced update.
# Published at https://flowstatemcp.com/.well-known/flowstate-approval-keys.json (Oct 21, 2026).
# A kid that maps to None is refused; if every kid is None, phone approvals are off entirely.
FSA_PUBLIC_KEYS: dict = {
    "fsa-1": {"kty": "EC", "crv": "P-256", "x": "eMo3tUoz8rNtqO1UA32Dji_2Zi1gKpZVoGnrj4VZ_Wk",
              "y": "CW7xH5MZy-iL5ZbCQ2BKDo7okUZtaABnMvcTCYTFhPA"},
    "fsa-2": {"kty": "EC", "crv": "P-256", "x": "ZFiKJNndjt35qWI3AjmXhKkIQRNlmxhsq8sBlJT-340",
              "y": "1LTTIZEiA3ehSiDaspCt-hqDETvjktMnZrkGahXPf4o"},
}

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TICKET_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{22,43}$")
# a compact ES256 JWS: header.payload.signature (64 bytes = 86 base64url characters); checked
# before any crypto so a junk request costs nothing
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{16,512}\.[A-Za-z0-9_-]{16,2048}\.[A-Za-z0-9_-]{86}$")


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.payload = {"error": code, "message": message}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).strftime("%Y-%m-%d %H:%M:%S")


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='approval_settings'").fetchone() is not None


# ---------------------------------------------------------------- settings
def _get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM approval_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _put(conn, key: str, value: Optional[str], who: str) -> None:
    if value is None:
        conn.execute("DELETE FROM approval_settings WHERE key = ?", (key,))
    else:
        conn.execute("""INSERT INTO approval_settings (key, value, updated_at, updated_by)
                        VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                        updated_at = excluded.updated_at, updated_by = excluded.updated_by""",
                     (key, value, _ts(), (who or "")[:80]))


def is_on(db_path: Path, project_id: Optional[int] = None) -> bool:
    """Approvals on for this project (its own setting, else the install's; default off)."""
    try:
        conn = _conn(db_path)
    except sqlite3.Error:
        return False
    try:
        return on_with(conn, project_id)
    finally:
        conn.close()


def on_with(conn, project_id: Optional[int] = None) -> bool:
    if not available(conn):
        return False
    v = _get(conn, f"approvals:project:{int(project_id)}") if project_id is not None else None
    return (v or _get(conn, "approvals") or "off") == "on"


def set_on(db_path: Path, on: Optional[bool], project_id: Optional[int] = None,
           who: str = "command-line") -> dict:
    """on True/False, or None to drop a project's own setting. Logged with review-mode changes."""
    key = "approvals" if project_id is None else f"approvals:project:{int(project_id)}"
    conn = _conn(db_path)
    try:
        if not available(conn):
            raise ApprovalError("approvals_unavailable", "Approvals aren't set up in this database "
                                                         "yet (migration 019); restart FlowState.")
        conn.execute("BEGIN IMMEDIATE")
        old = _get(conn, key)
        new = None if on is None else ("on" if on else "off")
        _put(conn, key, new, who)
        from . import identity
        identity.insert(conn, "review_mode_log", {
            "scope": key, "old_mode": old, "new_mode": new, "client": who[:80],
            "caller": "command-line", "user_request": "set in the user's terminal",
            "changed_at": _ts()}, who)
        conn.commit()
    finally:
        conn.close()
    return {"scope": key, "old": old, "now": "on" if is_on(db_path, project_id) else "off"}


def phone_on(db_path: Path) -> bool:
    conn = _conn(db_path)
    try:
        return not available(conn) or (_get(conn, "phone_approvals") or "on") == "on"
    finally:
        conn.close()


# ---------------------------------------------------------------- chat approvals via the relay
RELAY_CHAT_KEY = "relay_chat_approvals"


def relay_chat_on(db_path: Optional[Path]) -> bool:
    """May an AI connected through the relay approve or reject review items in the chat
    (manage_review approve / reject, quoting the user in user_request)? Off by default: the
    user decides on their phone (or on this computer). Only the user changes it, on this
    computer: `flowstate relay chat-approvals on|off`; no tool reads or writes this key.
    User-only items (user_only) stay the user's own click or key either way."""
    if db_path is None:
        return False
    try:
        conn = _conn(db_path)
    except sqlite3.Error:
        return False
    try:
        return available(conn) and _get(conn, RELAY_CHAT_KEY) == "on"
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def set_relay_chat(db_path: Path, on: bool, who: str = "command-line") -> dict:
    """The user's terminal only (relay/cli.py). Logged with the review-mode changes."""
    conn = _conn(db_path)
    try:
        if not available(conn):
            raise ApprovalError("approvals_unavailable", "Approvals aren't set up in this database "
                                                         "yet (migration 019); restart FlowState.")
        conn.execute("BEGIN IMMEDIATE")
        old = _get(conn, RELAY_CHAT_KEY)
        new = "on" if on else "off"
        _put(conn, RELAY_CHAT_KEY, new, who)
        from . import identity
        identity.insert(conn, "review_mode_log", {
            "scope": RELAY_CHAT_KEY, "old_mode": old, "new_mode": new, "client": who[:80],
            "caller": "command-line", "user_request": "set in the user's terminal",
            "changed_at": _ts()}, who)
        conn.commit()
    finally:
        conn.close()
    return {"old": old or "off", "now": "on" if relay_chat_on(db_path) else "off"}


def set_value(db_path: Path, key: str, value: Optional[str], who: str = "command-line") -> None:
    conn = _conn(db_path)
    try:
        _put(conn, key, value, who)
        conn.commit()
    finally:
        conn.close()


def popup_clients(db_path: Optional[Path]) -> set:
    names = set(POPUP_CLIENTS)
    if db_path is None:
        return names
    conn = _conn(db_path)
    try:
        if not available(conn):
            return names
        add = json.loads(_get(conn, "popup_clients_add") or "[]")
        drop = json.loads(_get(conn, "popup_clients_remove") or "[]")
    except (ValueError, sqlite3.Error):
        return names
    finally:
        conn.close()
    return (names | {str(n).lower() for n in add}) - {str(n).lower() for n in drop}


def change_popup_clients(db_path: Path, name: str, add: bool) -> set:
    name = (name or "").strip().lower()
    if not name:
        raise ApprovalError("name_required", "Give the app's name as it connects (clientInfo name).")
    conn = _conn(db_path)
    try:
        lists = {k: set(json.loads(_get(conn, k) or "[]"))
                 for k in ("popup_clients_add", "popup_clients_remove")}
        (lists["popup_clients_add"] if add else lists["popup_clients_remove"]).add(name)
        (lists["popup_clients_remove"] if add else lists["popup_clients_add"]).discard(name)
        for k, v in lists.items():
            _put(conn, k, json.dumps(sorted(v)) if v else None, "command-line")
        conn.commit()
    finally:
        conn.close()
    return popup_clients(db_path)


def popup_allowed(db_path: Optional[Path], client_name: str, declared: bool) -> bool:
    """May FlowState show this connection a popup? Declared the capability, on the allowlist,
    and not claude.ai / the Anthropic connector."""
    name = (client_name or "").strip().lower()
    if not declared or not name or any(m in name for m in CONNECTOR_MARKS):
        return False
    return name in popup_clients(db_path)


# ---------------------------------------------------------------- user-only items
def user_only(item: Optional[dict]) -> bool:
    """Only a human click or keypress decides this item: a delete or setting queued while
    approvals were on, and every "did the user say this?" item (provenance.py)."""
    if not item or item.get("kind") not in USER_ONLY_KINDS:
        return False
    if item["kind"] in ("setting", "source"):   # source: always, approvals on or off
        return True
    try:
        return bool(json.loads(item.get("payload") or "{}").get("user_only"))
    except ValueError:
        return False


def nonce_for(db_path: Path, item_id: int) -> Optional[str]:
    """The item's open one-use code; a fresh random one if it has none or it expired."""
    conn = _conn(db_path)
    try:
        if not available(conn):
            return None
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT nonce, expires_at, used_at FROM approval_requests "
                           "WHERE item_id = ?", (item_id,)).fetchone()
        if row and row["used_at"] is None and row["expires_at"] > _ts():
            conn.commit()
            return row["nonce"]
        nonce = secrets.token_urlsafe(32)   # 43 url-safe characters, never reused
        conn.execute("""INSERT INTO approval_requests (item_id, nonce, action, created_at,
                        expires_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(item_id) DO UPDATE SET
                        nonce = excluded.nonce, created_at = excluded.created_at,
                        expires_at = excluded.expires_at, used_at = NULL""",
                     (item_id, nonce, ACTION, _ts(), _ts(_now() + timedelta(days=NONCE_DAYS))))
        conn.commit()
        return nonce
    finally:
        conn.close()


def consume_nonce(conn, item_id: int) -> None:
    conn.execute("UPDATE approval_requests SET used_at = ? WHERE item_id = ? AND used_at IS NULL",
                 (_ts(), item_id))


def relay_handle() -> Optional[str]:
    """This device's relay handle, if the relay agent is enrolled and on. Without relay state
    on disk this is one file check: no relay module is imported."""
    try:
        import os
        from . import paths
        env = os.environ.get("FLOWSTATE_RELAY_DIR")
        root = Path(env).expanduser() if env else paths.data_root() / "relay"
        if not (root / "relay.json").exists():
            return None
        from .relay.store import RelayStore
        store = RelayStore()
        cfg = store.config()
        return cfg.get("handle") if cfg.get("enabled") and store.enrolled() else None
    except Exception:  # noqa: BLE001 - no relay means no phone link, nothing more
        return None


def phone_link(db_path: Path, item_id: int) -> Optional[str]:
    handle = relay_handle()
    if not handle or not phone_on(db_path) or not keys_ready():
        return None
    nonce = nonce_for(db_path, item_id)
    return f"{SITE}/approve/{handle}/{int(item_id)}?n={nonce}" if nonce else None


def review_handle(db_path: Path) -> Optional[str]:
    """This device's handle when the phone review list is available here: relay enrolled,
    keys pinned, phone links not off. Else None."""
    handle = relay_handle()
    if not handle or not phone_on(db_path) or not keys_ready():
        return None
    return handle


def review_link(db_path: Path) -> Optional[str]:
    """The bookmarkable phone link to the whole waiting list (no code in it, so nothing secret
    lands in chat history): https://flowstatemcp.com/review/<handle>. The site signs the user
    in and sends the browser to this device's /review/start, which makes a fresh code."""
    handle = review_handle(db_path)
    return f"{SITE}/review/{handle}" if handle else None


def _start_ready(conn) -> bool:
    """review_open_nonces exists with the ticket columns (021 and 022)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(review_open_nonces)")}
    return {"nonce", "sub", "ticket"} <= cols


def ticket_key(ticket_nonce: str) -> str:
    """What is stored for a start ticket: a hash of its nonce, never the ticket."""
    return hashlib.sha256(("start_review:" + ticket_nonce).encode()).hexdigest()


def verify_start_ticket(db_path: Path, ticket: str, handle: Optional[str] = None,
                        now: Optional[datetime] = None, keys: Optional[dict] = None) -> dict:
    """Check the site's ticket for /review/start: the same ES256 grant checks as every phone
    grant (pinned key, signature, times with the 5-minute lifetime and clock skew, issuer,
    audience and handle from this device's relay.json, action start_review, item 0), plus a
    22-43 character nonce not used here before. Returns its claims; raises ApprovalError.
    Nothing is stored: start_nonce remembers the nonce when it makes the code."""
    if not isinstance(ticket, str) or not TICKET_RE.match(ticket):
        raise ApprovalError("bad_grant", "The link is malformed.")
    claims = _claims(ticket, handle, now, keys, START_ACTION, 0)
    if not _TICKET_NONCE_RE.match(claims["nonce"]):
        raise ApprovalError("bad_grant", "The approval grant's code is malformed.")
    conn = _conn(db_path)
    try:
        if _start_ready(conn) and conn.execute(
                "SELECT 1 FROM review_open_nonces WHERE ticket = ?",
                (ticket_key(claims["nonce"]),)).fetchone():
            raise ApprovalError("ticket_used", "This link was already used.")
    finally:
        conn.close()
    return claims


def start_nonce(db_path: Path, sub: str, ticket_nonce: Optional[str] = None) -> Optional[str]:
    """A fresh one-use code for the review list, made by /review/start (review_phone.py) for
    the site user `sub` of a verified start ticket: 43 url-safe characters, valid
    START_MINUTES. At most START_OPEN open at once (the oldest are expired), START_PER_HOUR
    made per hour per user and START_PER_HOUR_ALL for everyone (else ApprovalError
    rate_limited). `ticket_nonce`: the ticket's nonce, remembered (hashed) with the code, so
    each ticket makes at most one code (a replay raises ticket_used and counts nothing).
    Codes made here are told apart from the 7-day codes older releases put in chat links by
    their lifetime (under a day). None if the database has no review_open_nonces table with
    the 022 columns."""
    if not isinstance(sub, str) or not sub:
        raise ApprovalError("bad_grant", "The link doesn't say who signed in.")
    conn = _conn(db_path)
    try:
        if not _start_ready(conn):
            return None
        now = _ts()
        hour_ago = _ts(_now() - timedelta(hours=1))
        short = "julianday(expires_at) - julianday(created_at) < 1"
        key = ticket_key(ticket_nonce) if ticket_nonce else None
        conn.execute("BEGIN IMMEDIATE")
        if key and conn.execute("SELECT 1 FROM review_open_nonces WHERE ticket = ?",
                                (key,)).fetchone():
            conn.rollback()
            raise ApprovalError("ticket_used", "This link was already used.")
        mine = conn.execute(f"SELECT COUNT(*) FROM review_open_nonces WHERE {short} AND sub = ? "
                            "AND created_at > ?", (sub, hour_ago)).fetchone()[0]
        everyone = conn.execute(f"SELECT COUNT(*) FROM review_open_nonces WHERE {short} AND "
                                "created_at > ?", (hour_ago,)).fetchone()[0]
        if mine >= START_PER_HOUR or everyone >= START_PER_HOUR_ALL:
            conn.rollback()
            raise ApprovalError("rate_limited", "Too many review links were opened in the last "
                                                "hour. Try again later.")
        open_ = conn.execute(f"SELECT nonce FROM review_open_nonces WHERE {short} AND used_at "
                             "IS NULL AND expires_at > ? ORDER BY created_at, rowid", (now,)).fetchall()
        for row in open_[:max(0, len(open_) - START_OPEN + 1)]:   # make room: oldest expire now
            conn.execute("UPDATE review_open_nonces SET expires_at = ? WHERE nonce = ?",
                         (now, row["nonce"]))
        nonce = secrets.token_urlsafe(32)   # 43 url-safe characters, never reused
        conn.execute("INSERT INTO review_open_nonces (nonce, action, created_at, expires_at, sub, "
                     "ticket) VALUES (?, ?, ?, ?, ?, ?)",
                     (nonce, REVIEW_ACTION, now, _ts(_now() + timedelta(minutes=START_MINUTES)),
                      sub[:200], key))
        conn.commit()
        return nonce
    finally:
        conn.close()


def use_review_nonce(db_path: Path, nonce: str) -> bool:
    """Use up the review list's code. False if it was used, expired or never made here."""
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE review_open_nonces SET used_at = ? WHERE nonce = ? AND "
                           "used_at IS NULL AND expires_at > ? AND action = ?",
                           (_ts(), nonce, _ts(), REVIEW_ACTION))
        conn.commit()
        return cur.rowcount == 1
    except sqlite3.OperationalError:   # no table (database older than 021)
        return False
    finally:
        conn.close()


def ask_line(db_path: Path, item_id: int, what: str = "approve it") -> str:
    """The one line an agent passes on when the user must decide (no popup)."""
    link = phone_link(db_path, item_id)
    if link:
        return f"Waiting for the user: ask them to {what} on their phone: {link}"
    from .review_tunnel import page_url
    tunnel = page_url()
    if tunnel:   # this app came in through the user's own tunnel: its review page exists
        return (f"Waiting for the user: ask them to {what} on their tunnel review page ({tunnel}) "
                f"or with the FlowState review shortcut on their desktop (or run "
                f"`flowstate approve {item_id}` in a terminal).")
    return (f"Waiting for the user: ask them to open the FlowState review shortcut on their "
            f"desktop and {what} there (or run `flowstate approve {item_id}` in a terminal).")


def describe(item: dict, project: str, db_path: Optional[Path] = None) -> str:
    """What the user is asked, in plain words (without the app's name)."""
    try:
        p = json.loads(item.get("payload") or "{}")
    except ValueError:
        p = {}
    if item["kind"] == "cleanup":
        from . import cleanup
        return cleanup.ask(p, project, db_path)
    if item["kind"] == "setting":
        where = "the whole install" if p.get("scope") == "install" else project
        return f"turn review off for {where}"
    if item["kind"] == "source":
        title = " ".join(str(p.get("title") or "").split())
        title = title if len(title) <= 60 else title[:57] + "..."
        return (f'mark the {p.get("record_type") or "record"} "{title}" in {project} as '
                f"something you said yourself (user_stated)")
    if item["kind"] == "delete":
        if db_path is not None:
            try:
                from . import deletes
                live = deletes.question(db_path, item, project)
                if live:
                    return live
            except Exception:  # noqa: BLE001 - the stored counts still describe it
                pass
        rtype, title = p.get("record_type") or "record", " ".join(str(p.get("title") or "").split())
        title = title if len(title) <= 60 else title[:57] + "..."
        if rtype == "project":
            what = f'delete the whole project "{project}"'
        else:
            what = f'delete the {rtype} "{title}"' if title else f"delete {rtype} #{p.get('record_id')}"
        extra = ", ".join(f"{n} {k}" for k, n in (p.get("counts") or {}).items())
        return what + (f" (with {extra})" if extra else "") + \
            ("" if rtype == "project" else f" from {project}")
    return f"decide review item #{item['id']} in {project}"


def ask_payload(db_path: Path, item_id: int, app: str) -> dict:
    """What the popup shows and the one-line fallback, for a queued user-only item."""
    conn = _conn(db_path)
    try:
        item = dict(conn.execute("SELECT * FROM review_items WHERE id = ?", (item_id,)).fetchone())
        row = conn.execute("SELECT name FROM projects WHERE id = ?", (item["project_id"],)).fetchone()
    finally:
        conn.close()
    project = row[0] if row else f"project #{item['project_id']}"
    try:
        p = json.loads(item.get("payload") or "{}")
    except ValueError:
        p = {}
    out = {"item_id": item_id, "message": f"{app} wants to {describe(item, project, db_path)}.",
           "line": ask_line(db_path, item_id)}
    if item["kind"] == "delete" and p.get("record_type") == "project":
        out["project_name"] = p.get("project_name") or project
    return out


def log_decision(db_path: Path, item: dict, path: str, client: str, decision: str,
                 conn=None) -> None:
    own = conn is None
    conn = conn or _conn(db_path)
    try:
        if not available(conn):
            return
        conn.execute("""INSERT INTO approval_log (item_id, project_id, kind, path, client, decision,
                        decided_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (item["id"], item.get("project_id"), item.get("kind"), path,
                      (client or "")[:80], decision, _ts()))
        if decision in ("approved", "rejected"):
            consume_nonce(conn, item["id"])
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def path_of(client: str) -> str:
    """How a user-only decision was made, from the decider's label."""
    c = (client or "").lower()
    for p in ("popup", "phone", "review-page", "tunnel page", "chat"):
        if c.startswith(p):
            return p.replace("-", " ")
    return "terminal"


# Paths where every decision is logged, not only user-only items': the user went through the
# queue themselves (waiting.py's chat walk, review_phone.py's phone list, review_tunnel.py).
LOG_EVERY_DECISION = ("chat", "phone", "tunnel page")


def logs_every_decision(client: str, agent: bool) -> bool:
    return not agent and path_of(client) in LOG_EVERY_DECISION


def log_rows(db_path: Path, project_id: int, limit: int = 20) -> list:
    conn = _conn(db_path)
    try:
        if not available(conn):
            return []
        return [dict(r) for r in conn.execute(
            """SELECT item_id, kind, path, client, decision, decided_at FROM approval_log
               WHERE project_id = ? ORDER BY id DESC LIMIT ?""", (project_id, limit))]
    finally:
        conn.close()


# ---------------------------------------------------------------- phone grants (ES256 JWS)
def keys_ready(keys: Optional[dict] = None) -> bool:
    keys = FSA_PUBLIC_KEYS if keys is None else keys
    return any(isinstance(v, dict) for v in keys.values())


def _b64d(s: str) -> bytes:
    if not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_-]*", s):
        raise ApprovalError("bad_grant", "The approval grant is malformed.")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _verify_es256(jwk: dict, signing_input: bytes, sig: bytes) -> bool:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    except ImportError as e:  # pragma: no cover - the relay needs it anyway
        raise ApprovalError("crypto_missing", "Phone approvals need the Python package "
                                              "'cryptography' (the relay uses it too).") from e
    if len(sig) != 64 or jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        return False
    try:
        x = int.from_bytes(_b64d(jwk["x"]), "big")
        y = int.from_bytes(_b64d(jwk["y"]), "big")
        key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        der = encode_dss_signature(int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big"))
        key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, KeyError, ApprovalError):
        return False


def grant_hash(grant: str) -> str:
    return hashlib.sha256((grant or "").encode()).hexdigest()


def _claims(grant: str, handle: Optional[str], now: Optional[datetime], keys: Optional[dict],
            action: str, item_id: int) -> dict:
    """The checks every phone grant gets: pinned key and signature, times, issuer, device,
    action, item and the code's format. Returns its claims; raises ApprovalError."""
    from .relay import RELAY_DOMAIN
    keys = FSA_PUBLIC_KEYS if keys is None else keys
    if not keys_ready(keys):
        raise ApprovalError("phone_approvals_unavailable",
                            "Phone approvals aren't available in this FlowState release yet. "
                            "Approve in your terminal: flowstate approve <id>.")
    handle = handle or relay_handle()
    if not handle:
        raise ApprovalError("relay_not_enrolled", "This FlowState has no relay set up.")
    parts = (grant or "").split(".")
    if len(parts) != 3 or len(grant) > 4096:
        raise ApprovalError("bad_grant", "The approval grant is malformed.")
    try:
        header = json.loads(_b64d(parts[0]))
        claims = json.loads(_b64d(parts[1]))
        sig = _b64d(parts[2])
    except (ValueError, UnicodeDecodeError) as e:
        raise ApprovalError("bad_grant", "The approval grant is malformed.") from e
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise ApprovalError("bad_grant", "The approval grant is malformed.")
    if header.get("alg") != "ES256" or header.get("typ") != "fsa+jwt":
        raise ApprovalError("bad_grant", "The approval grant uses an unexpected format.")
    jwk = keys.get(header.get("kid"))
    if not isinstance(jwk, dict):
        raise ApprovalError("unknown_key", "The approval grant was signed with a key this "
                                           "FlowState doesn't trust.")
    if not _verify_es256(jwk, f"{parts[0]}.{parts[1]}".encode(), sig):
        raise ApprovalError("bad_signature", "The approval grant's signature is not valid.")
    t = int((now or _now()).timestamp())
    iat, exp = claims.get("iat"), claims.get("exp")
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (iat, exp)) \
            or exp <= iat or exp - iat > GRANT_SECONDS or iat > t + CLOCK_SKEW:
        raise ApprovalError("bad_grant", "The approval grant's times are not valid.")
    if exp <= t:
        raise ApprovalError("grant_expired", "This approval link has expired (5 minutes). "
                                             "Open the link from the chat again.")
    checks = (claims.get("iss") == SITE,
              claims.get("aud") == f"{handle}.{RELAY_DOMAIN}",
              claims.get("handle") == handle,
              isinstance(claims.get("sub"), str) and bool(claims["sub"]),
              claims.get("action") == action)
    if not all(checks):
        raise ApprovalError("wrong_device", "This approval grant is not for this FlowState.")
    item = claims.get("item")
    if not isinstance(item, int) or isinstance(item, bool) or item != int(item_id):
        raise ApprovalError("wrong_item", "This approval grant is for another item.")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        raise ApprovalError("bad_grant", "The approval grant's code is malformed.")
    return claims


def verify_grant(db_path: Path, grant: str, item_id: int, handle: Optional[str] = None,
                 now: Optional[datetime] = None, keys: Optional[dict] = None) -> dict:
    """Check a phone grant for `item_id`. Returns its claims; raises ApprovalError."""
    claims = _claims(grant, handle, now, keys, ACTION, item_id)
    item, nonce = claims["item"], claims["nonce"]
    conn = _conn(db_path)
    try:
        row = conn.execute("""SELECT q.nonce, q.expires_at, q.used_at, q.action, r.status, r.kind,
                              r.payload FROM approval_requests q JOIN review_items r
                              ON r.id = q.item_id WHERE q.item_id = ?""", (item,)).fetchone()
    finally:
        conn.close()
    if row is None or row["used_at"] is not None or row["status"] != "pending" \
            or not hmac.compare_digest(row["nonce"], nonce) or row["action"] != ACTION:
        raise ApprovalError("not_open", "This request was already decided, or the link is "
                                        "out of date.")
    if row["expires_at"] <= _ts(now):
        raise ApprovalError("grant_expired", "This approval link is out of date; ask for a new one.")
    if not user_only({"kind": row["kind"], "payload": row["payload"]}):
        raise ApprovalError("not_open", "This item isn't decided by phone.")
    return claims


def verify_review_grant(db_path: Path, grant: str, handle: Optional[str] = None,
                        now: Optional[datetime] = None, keys: Optional[dict] = None) -> dict:
    """Check a phone grant that opens the waiting list (action open_review, item 0) and use up
    its one-use code. Returns its claims; raises ApprovalError."""
    claims = _claims(grant, handle, now, keys, REVIEW_ACTION, 0)
    if not use_review_nonce(db_path, claims["nonce"]):
        raise ApprovalError("grant_used", "This link was already used or is out of date. "
                                          "Open a new one from the chat.")
    return claims
