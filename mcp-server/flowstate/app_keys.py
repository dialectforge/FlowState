"""Per-app keys for the local / LAN HTTP listener.

    flowstate keys add --name NAME [--read-only] [--workspace W]   (prints the key ONCE)
    flowstate keys list
    flowstate keys revoke --id ID | --name NAME

Each app gets its own key, so FlowState records the app's name from the key, not from what
the app says about itself. A read-only key can call read tools only. Revoking a key stops it
at once (the next request gets 401). Keys work on the LAN listener only, never on the
public (tunnel) listener; the old shared LAN token keeps working as before.

Stored like the per-app remote tokens (remote_auth.py): in the same settings database
(FLOWSTATE_AUTH_DB, else <config dir>/remote-auth.db), as a SHA-256 hash only. A key is
never logged or shown again after `keys add`.
"""

import secrets
import time
from typing import Optional

from . import remote_auth as RA

MAX_NAME = 80


def _new_key() -> str:
    return RA.APP_KEY_PREFIX + secrets.token_urlsafe(32)


def add(store: "RA.AuthStore", name: str, read_only: bool = False,
        workspace: Optional[str] = None) -> tuple[str, str]:
    """(key id, key). The key is returned once; only its hash is kept."""
    name = " ".join((name or "").split())[:MAX_NAME]
    if not name:
        raise ValueError("give the app a name, e.g. --name \"Cursor on the laptop\"")
    workspace = " ".join((workspace or "").split())[:MAX_NAME] or None
    kid, key = "key_" + secrets.token_hex(6), _new_key()
    with store._conn() as c:
        c.execute("INSERT INTO app_keys (id, name, hash, read_only, workspace, created_at) "
                  "VALUES (?, ?, ?, ?, ?, ?)",
                  (kid, name, RA._h(key), int(bool(read_only)), workspace, time.time()))
    return kid, key


def list_keys(store: "RA.AuthStore") -> list[dict]:
    with store._conn() as c:
        rows = c.execute("SELECT id, name, read_only, workspace, created_at, last_used_at, "
                         "revoked_at FROM app_keys ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def revoke(store: "RA.AuthStore", key_id: Optional[str] = None,
           name: Optional[str] = None) -> list[str]:
    """Revoke by id, or every live key with that name. Returns the ids revoked."""
    with store._conn() as c:
        if key_id:
            rows = c.execute("SELECT id FROM app_keys WHERE id = ? AND revoked_at IS NULL",
                             (key_id,)).fetchall()
        else:
            rows = c.execute("SELECT id FROM app_keys WHERE name = ? AND revoked_at IS NULL",
                             ((name or "").strip(),)).fetchall()
        ids = [r["id"] for r in rows]
        c.executemany("UPDATE app_keys SET revoked_at = ? WHERE id = ?",
                      [(time.time(), i) for i in ids])
    return ids


def check(store: "RA.AuthStore", key: str) -> Optional["RA.FSAccessToken"]:
    """The caller's token for a live key, or None (unknown or revoked)."""
    with store._conn() as c:
        row = c.execute("SELECT * FROM app_keys WHERE hash = ? AND revoked_at IS NULL",
                        (RA._h(key),)).fetchone()
        if row is None:
            return None
        now = time.time()   # last used: at most once a minute
        c.execute("UPDATE app_keys SET last_used_at = ? WHERE id = ? AND "
                  "(last_used_at IS NULL OR last_used_at < ?)", (now, row["id"], now - 60))
    scopes = [RA.SCOPE_READ] if row["read_only"] else [RA.SCOPE_READ, RA.SCOPE_WRITE]
    return RA.FSAccessToken(token="", client_id=row["id"], client_name=row["name"],
                            scopes=scopes, identity_source="app_key", workspace=row["workspace"])


# ---------------------------------------------------------------- command line
def _when(ts) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def cmd_keys(a) -> int:
    from .cli import say
    store = RA.AuthStore()
    if a.action == "add":
        try:
            kid, key = add(store, a.name or "", a.read_only, a.workspace)
        except ValueError as e:
            say(f"Not added: {e}")
            return 1
        access = "read only" if a.read_only else "read and write"
        say(f"App key for '{a.name}' added as {kid} ({access}"
            + (f", workspace {a.workspace!r}" if a.workspace else "") + ").")
        say("The key is shown ONCE below. Put it in the app as the header "
            "'Authorization: Bearer <key>' for the LAN/local FlowState server. Don't save it "
            "in a synced or git folder. It does not work on the public (tunnel) address.")
        say(key)
        return 0
    if a.action == "list":
        rows = list_keys(store)
        if not rows:
            say("No app keys yet (flowstate keys add --name \"<app>\").")
        for r in rows:
            state = f"REVOKED {_when(r['revoked_at'])}" if r["revoked_at"] else "active"
            say(f"{r['id']}  {r['name']!r}  {'read-only' if r['read_only'] else 'read+write'}"
                f"  workspace {r['workspace'] or '-'}  created {_when(r['created_at'])}"
                f"  last used {_when(r['last_used_at'])}  {state}")
        return 0
    if not a.key_id and not a.name:
        say("Which key? flowstate keys revoke --id <id from `flowstate keys list`> (or --name)")
        return 1
    ids = revoke(store, a.key_id, None if a.key_id else a.name)
    if not ids:
        say("No active key matches that; nothing changed.")
        return 1
    say(f"Revoked {', '.join(ids)}: the app's next request is refused.")
    return 0


def register(sub) -> None:
    p = sub.add_parser("keys", help="per-app keys for the local / LAN HTTP server")
    p.add_argument("action", choices=["add", "list", "revoke"])
    p.add_argument("--name", help="add: the app's name; revoke: revoke the keys with this name")
    p.add_argument("--read-only", action="store_true", help="add: read tools only")
    p.add_argument("--workspace", help="add: the machine or workspace this key is for")
    p.add_argument("--id", dest="key_id", help="revoke: the key's id")
    p.set_defaults(fn=cmd_keys)
