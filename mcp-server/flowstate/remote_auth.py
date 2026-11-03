"""Who may use the HTTP server, and how much: per-client tokens, OAuth 2.1, rate limits.

Three kinds of caller:
  * the owner's LAN token (FLOWSTATE_TOKEN / credential store): full access, including the
    tools that touch this computer (git, file attach). Refused on the public URL unless
    --allow-master-token is given.
  * per-client tokens (`flowstate remote add-client NAME [--read-only]`): one token per
    app, each revocable on its own. For apps that take a fixed "Authorization: Bearer"
    header (Cursor, Claude Code, the xAI API, ...).
  * per-app LAN keys (`flowstate keys add --name NAME [--read-only]`, app_keys.py): the
    same idea for the local / LAN listener only; the key's name is recorded beside the app's.
    Never accepted on the public listener.
  * OAuth clients (claude.ai / ChatGPT connectors): MCP authorization (OAuth 2.1, PKCE
    S256, dynamic client registration, RFC 8707 resource binding, refresh-token rotation).
    The owner approves each one on a consent page with the owner passphrase.

Scopes: flowstate:read, flowstate:write (implies read). flowstate:local (git, file attach)
belongs to the owner's LAN token only.

Everything lives in a small SQLite file in the per-user settings folder (never the synced
data folder): FLOWSTATE_AUTH_DB, else <config dir>/remote-auth.db. Only SHA-256 hashes of
tokens and codes are stored; the passphrase is stored as an scrypt hash. Nothing here logs
a token, a code, a passphrase or a request body.
"""

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, RegistrationError,
                                      TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import paths, site_style
from .urls import no_trailing_slash

log = logging.getLogger("flowstate.auth")
access_log = logging.getLogger("flowstate.access")

SCOPE_READ = "flowstate:read"
SCOPE_WRITE = "flowstate:write"
SCOPE_LOCAL = "flowstate:local"
SCOPES = [SCOPE_READ, SCOPE_WRITE]

ACCESS_TTL = 3600                 # 1 hour
REFRESH_TTL = 30 * 24 * 3600      # 30 days, rotated on every use
CODE_TTL = 300                    # 5 minutes, single use
PENDING_TTL = 600                 # consent page must be answered within 10 minutes
MAX_OAUTH_CLIENTS = 100
PASSPHRASE_MIN = 12
LOCKOUT_FAILS, LOCKOUT_WINDOW = 5, 900   # 5 wrong passphrases in 15 min -> wait 15 min
# passphrase hashes (scrypt, 128 MiB each) running at once in this process; a try that can't
# start within PASSPHRASE_WAIT seconds is answered "busy, try again in a moment" (audit N-F10)
PASSPHRASE_HASHES, PASSPHRASE_WAIT = 1, 1.5
_HASH_SLOTS = threading.BoundedSemaphore(PASSPHRASE_HASHES)

# Where OAuth clients may send the browser back to. Hosts of the apps we expect, plus
# loopback (Claude Code / Cursor desktop). Add more with `flowstate remote allow-redirect`.
DEFAULT_REDIRECT_HOSTS = ["claude.ai", "claude.com", "chatgpt.com", "www.cursor.com",
                          "cursor.com", "localhost", "127.0.0.1"]

TOKEN_PREFIX = {"access": "fs_at_", "refresh": "fs_rt_", "static": "fs_ct_", "code": "fs_ac_"}
# a token's, OAuth app's or key's name: short enough that "<machine> (signed in as <name>)"
# is always recorded whole (identity.MAX_LEN), so the sign-in still knows its own records
MAX_NAME = 50
APP_KEY_PREFIX = "fs_lk_"   # per-app keys for the local / LAN listener (app_keys.py)


def auth_db_path() -> Path:
    env = os.environ.get("FLOWSTATE_AUTH_DB")
    return Path(env).expanduser() if env else paths.config_dir() / "remote-auth.db"


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _new(kind: str) -> str:
    return TOKEN_PREFIX[kind] + secrets.token_urlsafe(32)


def redirect_hosts() -> list[str]:
    extra = paths.load_config().get("remote", {}).get("redirect_hosts", [])
    return DEFAULT_REDIRECT_HOSTS + [h for h in extra if h not in DEFAULT_REDIRECT_HOSTS]


def redirect_allowed(uri: str) -> bool:
    u = urlparse(str(uri))
    host = (u.hostname or "").lower()
    if host in ("localhost", "127.0.0.1"):
        return u.scheme in ("http", "https")
    return u.scheme == "https" and host in [h.lower() for h in redirect_hosts()]


# ---------------------------------------------------------------- store
_DDL = """
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,      -- 'oauth' | 'token'
    scopes TEXT NOT NULL, info TEXT, created_at REAL NOT NULL,
    approved_at REAL, last_used_at REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS tokens (
    hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, kind TEXT NOT NULL, -- access|refresh|static|code
    family TEXT NOT NULL, scopes TEXT NOT NULL, resource TEXT, extra TEXT,
    created_at REAL NOT NULL, expires_at REAL, used_at REAL, revoked_at REAL);
CREATE INDEX IF NOT EXISTS tokens_client ON tokens(client_id);
CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
CREATE TABLE IF NOT EXISTS pending (
    id TEXT PRIMARY KEY, client_id TEXT NOT NULL, params TEXT NOT NULL,
    created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1), salt BLOB NOT NULL, hash BLOB NOT NULL,
    set_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS failures (at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_keys (                                 -- see app_keys.py
    id TEXT PRIMARY KEY, name TEXT NOT NULL, hash TEXT NOT NULL UNIQUE,
    read_only INTEGER NOT NULL DEFAULT 0, workspace TEXT, created_at REAL NOT NULL,
    last_used_at REAL, revoked_at REAL);
CREATE TABLE IF NOT EXISTS client_names (                             -- names before a rename
    client_id TEXT NOT NULL, name TEXT NOT NULL, until REAL NOT NULL);
CREATE INDEX IF NOT EXISTS client_names_client ON client_names(client_id);
CREATE TABLE IF NOT EXISTS lan_exposures (                            -- audit N-F27
    client_id TEXT PRIMARY KEY, first_at REAL NOT NULL, last_at REAL NOT NULL,
    times INTEGER NOT NULL, last_ip TEXT, kept_at REAL);
"""


class AuthStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else auth_db_path()
        from .privatefs import private_file
        private_file(self.path)   # its folder 0700 and the file 0600 from the start (N-F31)
        with self._conn() as c:
            c.executescript(_DDL)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    # -- owner passphrase
    def set_passphrase(self, passphrase: str) -> None:
        if len(passphrase) < PASSPHRASE_MIN:
            raise ValueError(f"the passphrase must be at least {PASSPHRASE_MIN} characters")
        salt = _new_salt()
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO owner (id, salt, hash, set_at) VALUES (1, ?, ?, ?)",
                      (salt, _scrypt(passphrase, salt), time.time()))
            c.execute("DELETE FROM failures")

    def has_passphrase(self) -> bool:
        with self._conn() as c:
            return c.execute("SELECT 1 FROM owner WHERE id = 1").fetchone() is not None

    def locked_for(self) -> int:
        """Seconds until passphrase attempts are allowed again (0 = not locked)."""
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM failures WHERE at < ?", (now - LOCKOUT_WINDOW,))
            rows = [r[0] for r in c.execute("SELECT at FROM failures ORDER BY at")]
        if len(rows) < LOCKOUT_FAILS:
            return 0
        return max(1, int(rows[-LOCKOUT_FAILS] + LOCKOUT_WINDOW - now))

    def check_passphrase(self, passphrase: str) -> bool:
        """True only for the right passphrase (try_passphrase: counted before hashing)."""
        return self.try_passphrase(passphrase) == "ok"

    def try_passphrase(self, passphrase: str) -> str:
        """"ok", "wrong", "locked" (too many tries: nothing was hashed) or "busy" (another
        passphrase is being checked right now: nothing was hashed or counted; try again in a
        moment). Audit N-F10: each try is counted in the database before the slow hash, in one
        statement, so tries sent at the same moment can't all slip under the limit (also
        across processes sharing this file); and only PASSPHRASE_HASHES hashes run at once in
        this process, so a burst can't take 128 MiB per try. A right passphrase doesn't count
        (its slot is given back); a wrong one stays counted for LOCKOUT_WINDOW."""
        if not _HASH_SLOTS.acquire(timeout=PASSPHRASE_WAIT):
            return "busy"
        try:
            now = time.time()
            with self._conn() as c:
                c.execute("DELETE FROM failures WHERE at < ?", (now - LOCKOUT_WINDOW,))
                cur = c.execute("INSERT INTO failures (at) SELECT ? WHERE (SELECT COUNT(*) FROM "
                                "failures WHERE at >= ?) < ?",
                                (now, now - LOCKOUT_WINDOW, LOCKOUT_FAILS))
                slot = cur.lastrowid if cur.rowcount == 1 else None
                row = c.execute("SELECT salt, hash FROM owner WHERE id = 1").fetchone()
            if slot is None:
                return "locked"
            ok = bool(row) and hmac.compare_digest(_scrypt(passphrase, row["salt"]), row["hash"])
            if not ok:
                return "wrong"
            with self._conn() as c:
                c.execute("DELETE FROM failures WHERE rowid = ?", (slot,))
            if _log2n(row["salt"]) < SCRYPT_LOG2N:   # an older, lighter hash: upgrade it now
                salt = _new_salt()
                with self._conn() as c:
                    c.execute("UPDATE owner SET salt = ?, hash = ? WHERE id = 1 AND salt = ?",
                              (salt, _scrypt(passphrase, salt), row["salt"]))
            return "ok"
        finally:
            _HASH_SLOTS.release()

    # -- names: who wrote what is matched by the sign-in's name (identity.same_sign_in), and
    # pause switches are kept by name, so a name is never given to a second token, OAuth app
    # or per-app LAN key (one list for all three): not one in use, not one revoked, not one an
    # app had before a rename. Compared by names.skeleton, so case, invisible characters and
    # lookalike letters ("Сursor" with a Cyrillic С) don't make a different name. Pairs made
    # before 2.4.4 (a key and a token both "Cursor") keep working as they are.
    def _name_taken(self, c, name: str, except_id: Optional[str] = None) -> Optional[str]:
        """The id of the token, OAuth app or key that has (or had) this name, else None. An
        OAuth registration the owner never approved holds no name (task 56, D: anyone who
        knows the tunnel or relay address can register one); it gets a number at approval
        if its name was taken meanwhile (approve)."""
        from .names import skeleton
        key = skeleton(name)
        rows = list(c.execute("SELECT id, name FROM clients WHERE NOT (kind = 'oauth' AND "
                              "approved_at IS NULL)")) + \
            list(c.execute("SELECT client_id AS id, name FROM client_names")) + \
            list(c.execute("SELECT id, name FROM app_keys"))
        for r in rows:
            if r["id"] != except_id and skeleton(r["name"] or "") == key:
                return r["id"]
        return None

    @staticmethod
    def check_name(name: str, what: str = "the name") -> str:
        """The tidied name, or ValueError with a sentence: empty, too long for "(signed in as
        <name>)" to be recorded whole, secret-looking, with invisible characters, imitating
        FlowState's "(via ...)" marks, or one FlowState uses itself ("unknown", "phone", ...)."""
        from . import names
        from .identity import clean
        if names.invisible(name or ""):
            raise ValueError(f"{what} holds invisible characters (zero-width spaces, direction "
                             f"marks or the like); type it plainly")
        name = " ".join((name or "").split())
        if not name:
            raise ValueError(f"{what} is empty; give the app a name")
        if names.tidy(name) != name:
            raise ValueError(f"{what} uses special forms of letters (full-width or the like); "
                             f"type it plainly: {names.tidy(name)!r}")
        if len(name) > MAX_NAME:
            raise ValueError(f"{what} is longer than {MAX_NAME} characters")
        if clean(name) != name:
            raise ValueError(f"{what} looks like it holds a secret; pick a plain name")
        if names.has_marks(name):
            raise ValueError(f"{what} can't contain \"(via ...)\" or \"(signed in as ...)\": "
                             f"FlowState adds those itself")
        if names.reserved(name):
            raise ValueError(f"{name!r} is a name FlowState uses itself (for you, your phone, "
                             f"or callers it doesn't know); pick another, e.g. 'My {name}'")
        return name

    @staticmethod
    def oauth_name(raw) -> str:
        """The name an OAuth app gave itself at sign-up, made safe the way check_name checks a
        typed one (the app can't be asked to retype it): tidied, without "(via ...)" marks,
        not secret-looking, not one of FlowState's own names, short enough for "(2)"."""
        from . import names
        from .identity import clean
        base = names.strip_marks(raw if isinstance(raw, str) else "")
        base = base[:MAX_NAME - 5].strip()
        if not base or clean(base) != base:
            return "unnamed OAuth app"
        if names.reserved(base):
            base = f"OAuth app {base}"[:MAX_NAME - 5].strip()
        return base

    # -- clients
    def add_token_client(self, name: str, scopes: list[str],
                         days: Optional[int] = None) -> tuple[str, str]:
        """(client_id, token). The token is returned once and only its hash is kept. A name
        another token or OAuth app has, or had, is refused (ValueError)."""
        name = self.check_name(name)
        cid = "tok_" + secrets.token_hex(6)
        token = _new("static")
        now = time.time()
        with self._conn() as c:
            other = self._name_taken(c, name)
            if other:
                raise ValueError(f"the name {name!r} is taken (by {other}, now, before a rename "
                                 f"or before it was revoked); pick another, one per app")
            c.execute("INSERT INTO clients (id, name, kind, scopes, created_at, approved_at) "
                      "VALUES (?, ?, 'token', ?, ?, ?)", (cid, name, " ".join(scopes), now, now))
            c.execute("INSERT INTO tokens (hash, client_id, kind, family, scopes, created_at, "
                      "expires_at) VALUES (?, ?, 'static', ?, ?, ?, ?)",
                      (_h(token), cid, cid, " ".join(scopes), now,
                       now + days * 86400 if days else None))
        return cid, token

    def save_oauth_client(self, info: OAuthClientInformationFull) -> None:
        now = time.time()
        with self._conn() as c:
            # forget registrations that were never approved within a day
            c.execute("DELETE FROM clients WHERE kind = 'oauth' AND approved_at IS NULL "
                      "AND created_at < ?", (now - 86400,))
            n = c.execute("SELECT COUNT(*) FROM clients WHERE kind = 'oauth' "
                          "AND revoked_at IS NULL").fetchone()[0]
            if n >= MAX_OAUTH_CLIENTS:
                raise RegistrationError("invalid_client_metadata",
                                        "too many registered clients; revoke some first")
            # the app picks its own name here, so a taken one gets a number: "Claude (2)"
            base = self.oauth_name(info.client_name)
            name, n = base, 1
            while self._name_taken(c, name):
                n += 1
                name = f"{base} ({n})"
            c.execute("INSERT INTO clients (id, name, kind, scopes, info, created_at) "
                      "VALUES (?, ?, 'oauth', ?, ?, ?)",
                      (info.client_id, name, info.scope or " ".join(SCOPES),
                       info.model_dump_json(), now))

    def oauth_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        with self._conn() as c:
            row = c.execute("SELECT info FROM clients WHERE id = ? AND kind = 'oauth' "
                            "AND revoked_at IS NULL", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["info"]) if row else None

    def client_row(self, client_id: str):
        with self._conn() as c:
            return c.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()

    def list_clients(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT id, name, kind, scopes, created_at, approved_at, "
                             "last_used_at, revoked_at FROM clients ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def rename_client(self, old: str, new: str) -> tuple[str, str, str]:
        """Give a live app (remote token or OAuth app) a new name, same token: (client id,
        old name, new name). `old` is its name or id. Refused (ValueError, a sentence to
        show): no such app, several of that name, an empty or secret-looking new name, or one
        another app has, or had before a rename. The old name is kept in client_names, so the
        app is still the one that wrote what it saved under it (identity.same_sign_in)."""
        old = " ".join((old or "").split())
        if not old:
            raise ValueError("say which app: flowstate remote rename <old name> <new name>")
        wanted = self.check_name(new, "the new name")
        with self._conn() as c:
            live = c.execute("SELECT id, name FROM clients WHERE revoked_at IS NULL").fetchall()
            hits = [r for r in live if r["id"] == old] or \
                [r for r in live if r["name"] == old] or \
                [r for r in live if r["name"].casefold() == old.casefold()]
            if not hits:
                raise ValueError(f"no active app is named {old!r} (see `flowstate remote clients`)")
            if len(hits) > 1:
                raise ValueError(f"{len(hits)} active apps are named {old!r} ("
                                 + ", ".join(r["id"] for r in hits)
                                 + "); rename one by its id: flowstate remote rename <id> "
                                 + repr(wanted))
            cid, was = hits[0]["id"], hits[0]["name"]
            if wanted == was:
                raise ValueError(f"{cid} is already named {was!r}; nothing changed")
            taken = self._name_taken(c, wanted, except_id=cid)   # revoked apps' names too
            if taken:
                raise ValueError(f"the name {wanted!r} is taken (by {taken}, now, before a "
                                 f"rename or before it was revoked); pick another")
            c.execute("INSERT INTO client_names (client_id, name, until) VALUES (?, ?, ?)",
                      (cid, was, time.time()))
            c.execute("UPDATE clients SET name = ? WHERE id = ?", (wanted, cid))
        return cid, was, wanted

    def former_names(self, client_id: str) -> list[str]:
        """The names this app had before `flowstate remote rename`, oldest first."""
        with self._conn() as c:
            return [r["name"] for r in c.execute(
                "SELECT name FROM client_names WHERE client_id = ? ORDER BY until", (client_id,))]

    def revoke_client(self, client_id: str) -> bool:
        now = time.time()
        with self._conn() as c:
            n = c.execute("UPDATE clients SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                          (now, client_id)).rowcount
            c.execute("UPDATE tokens SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                      (now, client_id))
            c.execute("DELETE FROM pending WHERE client_id = ?", (client_id,))
        return n > 0

    # -- remote tokens sent over the plain-HTTP LAN listener (audit N-F27)
    def note_lan_exposure(self, client_id: str, ip: Optional[str]) -> None:
        """A remote token came in on the LAN listener from another computer and was refused
        (lan-tokens off). It still crossed the network readable by anyone on it, so the user
        is told (`flowstate remote clients`, the brief) until they revoke it or keep it. Not
        revoked here: that would break the user's app without them knowing why, and the token
        is only as exposed as their own network is."""
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO lan_exposures (client_id, first_at, last_at, times, last_ip) "
                      "VALUES (?, ?, ?, 1, ?) ON CONFLICT(client_id) DO UPDATE SET "
                      "last_at = excluded.last_at, times = times + 1, last_ip = excluded.last_ip, "
                      "kept_at = NULL", (client_id, now, now, (ip or "")[:64] or None))

    def lan_exposures(self) -> dict:
        """{client id: row} of live tokens sent over the LAN that the user hasn't kept."""
        with self._conn() as c:
            return {r["client_id"]: dict(r) for r in c.execute(
                "SELECT e.*, cl.name FROM lan_exposures e JOIN clients cl ON cl.id = e.client_id "
                "WHERE cl.revoked_at IS NULL AND e.kept_at IS NULL")}

    def keep_exposed(self, client_id: str) -> bool:
        """The user's `flowstate remote keep-token`: keep using a token sent over the LAN; the
        warning goes until it is sent that way again."""
        with self._conn() as c:
            return c.execute("UPDATE lan_exposures SET kept_at = ? WHERE client_id = ? AND "
                             "kept_at IS NULL", (time.time(), client_id)).rowcount > 0

    # -- tokens
    def issue(self, kind: str, client_id: str, scopes: list[str], family: str,
              ttl: Optional[int], resource: Optional[str] = None,
              extra: Optional[dict] = None) -> str:
        tok = _new(kind)
        now = time.time()
        with self._conn() as c:
            c.execute("INSERT INTO tokens (hash, client_id, kind, family, scopes, resource, extra, "
                      "created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (_h(tok), client_id, kind, family, " ".join(scopes), resource,
                       json.dumps(extra) if extra else None, now, now + ttl if ttl else None))
        return tok

    def lookup(self, token: str, kinds: tuple[str, ...]):
        """The token's row if it is live: right kind, not revoked/used/expired, client live."""
        with self._conn() as c:
            row = c.execute(
                "SELECT t.*, cl.name AS client_name FROM tokens t JOIN clients cl "
                "ON cl.id = t.client_id WHERE t.hash = ? AND cl.revoked_at IS NULL",
                (_h(token),)).fetchone()
        if not row or row["kind"] not in kinds:
            return None
        if row["revoked_at"] or row["used_at"]:
            return row if row["kind"] == "refresh" else None  # caller detects refresh reuse
        if row["expires_at"] and row["expires_at"] < time.time():
            return None
        return row

    def token_owner(self, token: str):
        """The token's client, kind and family, live or not (for /revoke: RFC 7009 answers
        200 for a token that is already invalid, but only its own client may revoke it)."""
        with self._conn() as c:
            return c.execute("SELECT client_id, kind, family FROM tokens WHERE hash = ?",
                             (_h(token),)).fetchone()

    def mark_used(self, token: str) -> bool:
        """Single-use tokens (codes, refresh): True only for the first caller."""
        with self._conn() as c:
            return c.execute("UPDATE tokens SET used_at = ? WHERE hash = ? AND used_at IS NULL "
                             "AND revoked_at IS NULL", (time.time(), _h(token))).rowcount == 1

    def revoke_family(self, family: str, kind: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE tokens SET revoked_at = ? WHERE family = ? AND revoked_at IS NULL"
                      + (" AND kind = ?" if kind else ""),
                      (time.time(), family) + ((kind,) if kind else ()))

    def touch(self, client_id: str) -> None:
        now = time.time()
        with self._conn() as c:  # at most once a minute per client
            c.execute("UPDATE clients SET last_used_at = ? WHERE id = ? AND "
                      "(last_used_at IS NULL OR last_used_at < ?)", (now, client_id, now - 60))

    # -- consent requests
    def add_pending(self, client_id: str, params: AuthorizationParams) -> str:
        pid = secrets.token_urlsafe(24)
        now = time.time()
        with self._conn() as c:
            c.execute("DELETE FROM pending WHERE created_at < ?", (now - PENDING_TTL,))
            c.execute("INSERT INTO pending (id, client_id, params, created_at) VALUES (?, ?, ?, ?)",
                      (pid, client_id, params.model_dump_json(), now))
        return pid

    def get_pending(self, pid: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM pending WHERE id = ?", (pid,)).fetchone()
        if not row or row["created_at"] < time.time() - PENDING_TTL:
            return None
        return row["client_id"], AuthorizationParams.model_validate_json(row["params"])

    def drop_pending(self, pid: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM pending WHERE id = ?", (pid,))

    def approve(self, client_id: str) -> None:
        with self._conn() as c:
            row = c.execute("SELECT name, approved_at FROM clients WHERE id = ?",
                            (client_id,)).fetchone()
            if row is not None and row["approved_at"] is None:
                # taken by a key, token or app approved since it registered: "Cursor (2)"
                base = re.sub(r" \(\d+\)$", "", row["name"] or "") or "unnamed OAuth app"
                name, n = row["name"], 1
                while self._name_taken(c, name, except_id=client_id):
                    n += 1
                    name = f"{base} ({n})"
                if name != row["name"]:
                    c.execute("UPDATE clients SET name = ? WHERE id = ?", (name, client_id))
            c.execute("UPDATE clients SET approved_at = COALESCE(approved_at, ?) WHERE id = ?",
                      (time.time(), client_id))


# Passphrase hashing (2.4.4, audit D21): new and changed passphrases use scrypt N=2^17, r=8,
# p=1 (OWASP's current advice); hashes from older versions used N=2^14 and still work, and are
# re-hashed with the new settings at the next successful sign-in. The cost travels in the salt
# column (no schema change): b"FSN" + one byte log2(N) + 16 random bytes; a plain 16-byte salt
# is the old N=2^14.
SCRYPT_LOG2N = 17
_SALT_TAG = b"FSN"


def _new_salt() -> bytes:
    return _SALT_TAG + bytes([SCRYPT_LOG2N]) + secrets.token_bytes(16)


def _log2n(salt: bytes) -> int:
    salt = bytes(salt)
    if len(salt) == 20 and salt[:3] == _SALT_TAG and 14 <= salt[3] <= 20:
        return salt[3]
    return 14


def _scrypt(passphrase: str, salt: bytes) -> bytes:
    n, r = 2 ** _log2n(salt), 8
    return hashlib.scrypt(passphrase.encode(), salt=bytes(salt), n=n, r=r, p=1, dklen=32,
                          maxmem=256 * n * r + (1 << 20))


# ---------------------------------------------------------------- OAuth provider
class FSAccessToken(AccessToken):
    client_name: str = ""
    identity_source: str = ""        # see identity.py: app_key, oauth_client, remote_token, ...
    workspace: Optional[str] = None  # per-app LAN keys: the machine / workspace it was made for
    former_names: list[str] = []     # its names before `flowstate remote rename` (identity.aka)


def oauth_issuer(public_url: str) -> str:
    """The one issuer string for a public address (task 78): Pydantic's URL clean-up (lower-case
    host, default port dropped) without the '/' it adds to a bare host. The metadata documents
    and the consent link both use it (task 79)."""
    from pydantic import AnyHttpUrl
    return no_trailing_slash(str(AnyHttpUrl(public_url)))


class FlowStateOAuthProvider:
    """MCP SDK OAuthAuthorizationServerProvider backed by AuthStore. The SDK's handlers do
    the protocol checks (PKCE S256, redirect URI match, grant types); this class decides
    who gets what and keeps the records."""

    def __init__(self, store: AuthStore, issuer_url: str, resource_url: str):
        self.store = store
        self.issuer_url = oauth_issuer(issuer_url)
        self.resource_url = resource_url

    def _same_resource(self, resource: Optional[str]) -> bool:
        return resource is None or no_trailing_slash(resource) == no_trailing_slash(self.resource_url)

    async def get_client(self, client_id: str):
        return self.store.oauth_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        bad = [str(u) for u in (client_info.redirect_uris or []) if not redirect_allowed(str(u))]
        if bad or not client_info.redirect_uris:
            hosts = sorted({urlparse(u).hostname or "?" for u in bad})
            log.warning("OAuth registration refused for %r: redirect host(s) %s not allowed "
                        "(flowstate remote allow-redirect --name <host> if this is your app)",
                        (client_info.client_name or "unnamed")[:80], ", ".join(hosts) or "none")
            raise RegistrationError("invalid_redirect_uri",
                                    "redirect URI host is not on FlowState's allow-list")
        self.store.save_oauth_client(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if not self._same_resource(params.resource):
            raise AuthorizeError("invalid_request", "resource must be this FlowState server")
        if not redirect_allowed(str(params.redirect_uri)):
            raise AuthorizeError("invalid_request", "redirect URI is not allowed")
        if params.scopes is None:
            params.scopes = (client.scope or " ".join(SCOPES)).split()
        if not set(params.scopes) <= set(SCOPES):
            raise AuthorizeError("invalid_scope", "unknown scope")
        pid = self.store.add_pending(client.client_id, params)
        return f"{self.issuer_url}/consent?req={pid}"

    async def load_authorization_code(self, client, authorization_code: str):
        row = self.store.lookup(authorization_code, ("code",))
        if not row or row["client_id"] != client.client_id:
            return None
        x = json.loads(row["extra"])
        return AuthorizationCode(code=authorization_code, scopes=row["scopes"].split(),
                                 expires_at=row["expires_at"], client_id=row["client_id"],
                                 code_challenge=x["code_challenge"], redirect_uri=x["redirect_uri"],
                                 redirect_uri_provided_explicitly=x["explicit"],
                                 resource=row["resource"])

    def _pair(self, client_id: str, scopes: list[str], family: str) -> OAuthToken:
        access = self.store.issue("access", client_id, scopes, family, ACCESS_TTL, self.resource_url)
        refresh = self.store.issue("refresh", client_id, scopes, family, REFRESH_TTL, self.resource_url)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        if not self.store.mark_used(authorization_code.code):
            raise TokenError("invalid_grant", "authorization code already used")
        family = "fam_" + secrets.token_hex(8)
        log.info("OAuth tokens issued to client %s", client.client_id)
        return self._pair(client.client_id, authorization_code.scopes, family)

    async def load_refresh_token(self, client, refresh_token: str):
        row = self.store.lookup(refresh_token, ("refresh",))
        if not row or row["client_id"] != client.client_id:
            return None
        if row["revoked_at"] or row["used_at"]:
            # A rotated refresh token came back: it was copied. Kill the whole chain.
            self.store.revoke_family(row["family"])
            log.warning("refresh token reuse for client %s: its tokens were revoked", client.client_id)
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"],
                            scopes=row["scopes"].split(), expires_at=int(row["expires_at"]))

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        row = self.store.lookup(refresh_token.token, ("refresh",))
        if not row or not self.store.mark_used(refresh_token.token):
            raise TokenError("invalid_grant", "refresh token already used")
        # Same family, so a later replay of the old refresh token also kills the new pair;
        # the old access token stops working now.
        self.store.revoke_family(row["family"], kind="access")
        return self._pair(client.client_id, scopes, row["family"])

    async def load_access_token(self, token: str):
        row = self.store.lookup(token, ("access", "static"))
        if not row:
            return None
        if row["kind"] == "access" and row["resource"] != self.resource_url:
            return None  # audience check: only tokens issued for this server
        return _token_obj(token, row, self.store)

    async def revoke_token(self, token) -> None:
        row = self.store.lookup(token.token, ("access", "refresh"))
        if row:
            self.store.revoke_family(row["family"])

    def code_for(self, client_id: str, params: AuthorizationParams, scopes: list[str]) -> str:
        return self.store.issue("code", client_id, scopes, "fam_" + secrets.token_hex(8), CODE_TTL,
                                self.resource_url,
                                {"code_challenge": params.code_challenge,
                                 "redirect_uri": str(params.redirect_uri),
                                 "explicit": params.redirect_uri_provided_explicitly})


class RevocationHandler:
    """POST /revoke, RFC 7009 (task 81), in place of the SDK's. The SDK's request model
    requires a `client_secret` form field, so a public client (`none`) or one using HTTP Basic
    got 400 invalid_request, and Basic alone (no `client_id` in the form) got 401. Here the
    client authenticates the way it registered (Basic, form secret, or `client_id` alone for a
    public client); the token is looked up whatever its type (`token_type_hint` is only a
    hint), and only the client it was issued to may revoke it. Revoking an access or refresh
    token ends that whole grant (both tokens), as refresh-token reuse does."""

    def __init__(self, provider: FlowStateOAuthProvider):
        self.provider = provider

    async def handle(self, request):
        from starlette.responses import JSONResponse, Response
        try:
            form = await request.form()
        except Exception:  # noqa: BLE001 - a malformed body is the client's error
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "form body expected"}, status_code=400)
        basic = request.headers.get("authorization", "")[:6].lower() == "basic "
        client = await self._client(request, form)
        if client is None:
            return JSONResponse(
                {"error": "invalid_client", "error_description": "client authentication failed"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="FlowState"'} if basic else None)
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "token is required"}, status_code=400)
        store = self.provider.store
        row = await asyncio.to_thread(store.token_owner, token)
        if row and row["kind"] != "code":
            if row["client_id"] != client.client_id:
                return JSONResponse({"error": "unauthorized_client", "error_description":
                                     "the token was not issued to this client"}, status_code=400)
            if row["kind"] in ("access", "refresh"):
                await asyncio.to_thread(store.revoke_family, row["family"])
                log.info("OAuth tokens revoked by client %s", client.client_id)
        # unknown, already revoked or expired: 200 all the same (RFC 7009 section 2.2)
        return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})

    async def _client(self, request, form) -> Optional[OAuthClientInformationFull]:
        import base64
        import binascii
        from urllib.parse import unquote
        auth = request.headers.get("authorization", "")
        basic_id = basic_secret = None
        if auth[:6].lower() == "basic ":
            try:
                basic_id, basic_secret = base64.b64decode(auth[6:].strip(), validate=True) \
                    .decode("utf-8").split(":", 1)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                return None
            basic_id, basic_secret = unquote(basic_id), unquote(basic_secret)
        form_id, form_secret = form.get("client_id"), form.get("client_secret")
        if basic_id is not None and form_id and form_id != basic_id:
            return None
        cid = basic_id or form_id
        if not isinstance(cid, str) or not cid:
            return None
        client = await self.provider.get_client(cid)
        if client is None:
            return None
        method = client.token_endpoint_auth_method or "client_secret_post"
        if method == "client_secret_basic":
            given = basic_secret
        elif method == "client_secret_post":
            given = form_secret if basic_id is None and isinstance(form_secret, str) else None
        else:
            given = None
        if client.client_secret:
            if not given or not hmac.compare_digest(client.client_secret.encode(), given.encode()):
                return None
            if client.client_secret_expires_at and client.client_secret_expires_at < time.time():
                return None
        return client


def _token_obj(token: str, row, store: Optional[AuthStore] = None) -> FSAccessToken:
    return FSAccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                         expires_at=int(row["expires_at"]) if row["expires_at"] else None,
                         resource=row["resource"], client_name=row["client_name"],
                         identity_source="oauth_client" if row["kind"] == "access" else "remote_token",
                         former_names=store.former_names(row["client_id"]) if store else [])


class TokenChecker:
    """Bearer check for /mcp: owner token (if allowed), per-client tokens, OAuth tokens, and
    (LAN listener only, app_keys=True) per-app LAN keys. Also the SDK's TokenVerifier
    (verify_token): it reports each token's audience as `resource` and the SDK's bearer check
    in Gate refuses one not issued for this server (AuthSettings.validate_token_resource, task 80)."""

    def __init__(self, store: Optional[AuthStore], master: Optional[str],
                 provider: Optional[FlowStateOAuthProvider] = None, app_keys: bool = False):
        self.store, self.provider = store, provider
        self.master = master.encode() if master and len(master) >= 16 else None
        self.app_keys = app_keys

    async def verify_token(self, token: str) -> Optional[FSAccessToken]:
        return await asyncio.to_thread(self.check, token.strip())

    def check(self, token: str) -> Optional[FSAccessToken]:
        # the owner token and per-client tokens are kept by this server alone and open only
        # it, so their audience is this server's resource (None on the LAN listener: no OAuth)
        own = self.provider.resource_url if self.provider else None
        if self.master and hmac.compare_digest(token.encode(), self.master):
            return FSAccessToken(token="", client_id="owner", client_name="owner (LAN token)",
                                 scopes=[SCOPE_READ, SCOPE_WRITE, SCOPE_LOCAL],
                                 identity_source="shared_token", resource=own)
        if self.store and token.startswith(APP_KEY_PREFIX):
            if not self.app_keys:
                return None   # LAN keys never open the public listener
            from . import app_keys
            return app_keys.check(self.store, token)
        if not self.store or not token.startswith(("fs_ct_", "fs_at_")):
            return None
        row = self.store.lookup(token, ("access", "static"))
        if not row:
            return None
        if row["kind"] == "access" and not self.provider:
            return None   # OAuth tokens never open the LAN listener
        self.store.touch(row["client_id"])
        tok = _token_obj(token, row, self.store)
        # an OAuth token's audience is the resource it was issued for (the SDK compares it)
        return tok if row["kind"] == "access" else tok.model_copy(update={"resource": own})


# ---------------------------------------------------------------- consent page
_STYLE = site_style.restyle("""body{margin:0;font-size:16px;line-height:1.5}
.wrap{max-width:32rem;margin:0 auto;padding:28px 16px 48px}
.brand{font-weight:500;font-size:17px;margin:0 0 20px}.brand b{color:var(--accent);font-weight:700}
h1{font-size:24px;line-height:1.25;margin:0 0 14px}
.box{background:#fff;border:1px solid var(--line);border-radius:10px;padding:16px 18px}.err{color:var(--danger)}
label{display:block;margin:.6rem 0}input[type=password]{display:block;width:100%;margin-top:4px}
button{padding:0 18px;margin:.5rem .5rem 0 0;border:1px solid var(--chip);background:#fff;color:var(--ink);font-size:15px;font-weight:500}
button[value=approve]{background:var(--ink);border-color:var(--ink);color:#fff}
code{overflow-wrap:anywhere}""").replace("{", "{{").replace("}", "}}")
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlowState: allow access?</title>
<style>""" + _STYLE + """</style></head><body><div class="wrap"><p class="brand">Flow<b>State</b></p>
<h1>Allow access to your FlowState?</h1><div class="box">{body}</div></div></body></html>"""


def consent_html(name: str, redirect_host: str, scopes: list[str], pid: str,
                 error: str = "", locked: int = 0, no_passphrase: bool = False) -> str:
    e = html.escape
    if no_passphrase:
        return _PAGE.format(body="<p class='err'>No owner passphrase is set on this FlowState "
                                 "server. On the FlowState computer run "
                                 "<code>flowstate remote passphrase</code>, then try again.</p>")
    write = SCOPE_WRITE in scopes
    body = (f"<p><b>{e(name)}</b> wants to use your FlowState project memory.</p>"
            f"<p>After you approve, your browser goes back to <b>{e(redirect_host)}</b>. "
            f"If you did not just start this from that app, press Deny.</p>")
    if error:
        body += f"<p class='err'>{e(error)}</p>"
    if locked:
        body += (f"<p class='err'>Too many wrong passphrases. Try again in about "
                 f"{max(1, locked // 60)} minute(s).</p>")
    body += (f"<form method='post' action='consent'><input type='hidden' name='req' value='{e(pid)}'>"
             f"<label><input type='radio' name='access' value='read'{'' if write else ' checked'}> "
             f"Read only (it can look things up, not change them)</label>")
    if write:
        body += ("<label><input type='radio' name='access' value='write' checked> "
                 "Read and write (it can also log work, todos, decisions)</label>")
    body += ("<label>Owner passphrase <input type='password' name='passphrase' "
             "autocomplete='current-password'></label>"
             "<button name='decision' value='approve'>Approve</button>"
             "<button name='decision' value='deny'>Deny</button></form>")
    return _PAGE.format(body=body)


def consent_routes(provider: FlowStateOAuthProvider):
    from starlette.responses import HTMLResponse, RedirectResponse
    from starlette.routing import Route
    store = provider.store
    headers = {"X-Frame-Options": "DENY", "Cache-Control": "no-store",
               "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                          "frame-ancestors 'none'",
               "Referrer-Policy": "no-referrer"}

    def page(status=200, **kw):
        return HTMLResponse(consent_html(**kw), status_code=status, headers=headers)

    def _ctx(pid):
        got = store.get_pending(pid or "")
        if not got:
            return None
        cid, params = got
        row = store.client_row(cid)
        if not row or row["revoked_at"]:
            return None
        return cid, params, row["name"], urlparse(str(params.redirect_uri)).hostname or "?"

    async def get(request):
        pid = request.query_params.get("req", "")
        ctx = _ctx(pid)
        if not ctx:
            return HTMLResponse("This request expired or is unknown. Start again from the app.",
                                status_code=400, headers=headers)
        cid, params, name, host = ctx
        if not store.has_passphrase():
            return page(503, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        no_passphrase=True)
        return page(name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                    locked=store.locked_for())

    async def post(request):
        form = await request.form()
        pid = str(form.get("req", ""))
        ctx = _ctx(pid)
        if not ctx:
            return HTMLResponse("This request expired or is unknown. Start again from the app.",
                                status_code=400, headers=headers)
        cid, params, name, host = ctx
        if form.get("decision") != "approve":
            store.drop_pending(pid)
            log.info("owner denied OAuth client %s", cid)
            return RedirectResponse(construct_redirect_uri(str(params.redirect_uri),
                                                           error="access_denied", state=params.state),
                                    status_code=302, headers=headers)
        if not store.has_passphrase():
            return page(503, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        no_passphrase=True)
        locked = store.locked_for()
        if locked:
            return page(429, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        locked=locked)
        got = await asyncio.to_thread(store.try_passphrase, str(form.get("passphrase", "")))
        if got == "busy":
            return page(503, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        error="Another passphrase is being checked; try again in a moment.")
        if got == "locked":
            return page(429, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        locked=max(1, store.locked_for()))
        if got != "ok":
            log.warning("wrong owner passphrase on the consent page (client %s)", cid)
            return page(403, name=name, redirect_host=host, scopes=params.scopes, pid=pid,
                        error="Wrong passphrase.", locked=store.locked_for())
        scopes = list(params.scopes)
        if form.get("access") != "write":
            scopes = [SCOPE_READ]
        store.drop_pending(pid)
        store.approve(cid)
        code = provider.code_for(cid, params, scopes)
        log.info("owner approved OAuth client %s (%s) with %s", cid, name, " ".join(scopes))
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code,
                                                       state=params.state),
                                status_code=302, headers=headers)

    return [Route("/consent", get, methods=["GET"]), Route("/consent", post, methods=["POST"])]


# ---------------------------------------------------------------- rate limits
class RateLimiter:
    """Token bucket per key: `per_minute` requests a minute, bursts up to the same."""

    def __init__(self, per_minute: int):
        self.rate = max(1, per_minute) / 60.0
        self.cap = float(max(1, per_minute))
        self.buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> tuple[bool, int]:
        now = time.monotonic()
        tokens, last = self.buckets.get(key, (self.cap, now))
        tokens = min(self.cap, tokens + (now - last) * self.rate)
        if tokens < 1:
            self.buckets[key] = (tokens, now)
            return False, int((1 - tokens) / self.rate) + 1
        self.buckets[key] = (tokens - 1, now)
        if len(self.buckets) > 10000:  # forget the oldest keys
            for k in sorted(self.buckets, key=lambda k: self.buckets[k][1])[:5000]:
                del self.buckets[k]
        return True, 0


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ---------------------------------------------------------------- ASGI guard
LISTENER_KEY = "flowstate.listener"   # ASGI scope key Gate sets: lan / public / relay
# ASGI scope key Gate sets: True only for the LAN listener with a loopback socket peer (an app
# on this computer); the 127.0.0.1 review link is offered only then (waiting.links)
LOCAL_KEY = "flowstate.on_this_computer"
# ASGI scope key Gate sets: the caller's address as Gate's rate limits see it (review_tunnel.py
# limits passphrase tries per address with it)
CLIENT_IP_KEY = "flowstate.client_ip"


# Headers a proxy or tunnel adds. A loopback request that carries one came through a proxy
# on this computer from somewhere else (task 56, A): it doesn't count as "on this computer"
# (no 127.0.0.1 review link, no phone link with the handle, no popups, and a remote token on
# it is treated as sent over the LAN). FLOWSTATE_LAN_PROXY=1 says the LAN listener sits
# behind such a proxy, for one that adds none of them: then no caller counts as local.
PROXY_HEADERS = (b"forwarded", b"x-forwarded-for", b"x-forwarded-host", b"x-real-ip",
                 b"cf-connecting-ip", b"true-client-ip", b"via", b"x-client-ip")


def proxied(headers: dict) -> bool:
    if os.environ.get("FLOWSTATE_LAN_PROXY", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return any(h in headers for h in PROXY_HEADERS)


def is_loopback(peer) -> bool:
    """The socket peer address is this computer (127.0.0.0/8, ::1, ::ffff:127.x)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(str(peer or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


LAN_REMOTE_REFUSED = (
    "This is a remote-access token (for the relay or tunnel), and the LAN listener is plain, "
    "unencrypted HTTP, so FlowState doesn't accept it from another computer: anyone on the "
    "network could copy it and use it over the internet. Use a LAN key (`flowstate keys "
    "add`), or the user can allow it with `flowstate remote lan-tokens on`.")


def lan_remote_tokens_allowed() -> bool:
    """The user's own `flowstate remote lan-tokens on` (config.json "lan_remote_tokens")."""
    from . import paths
    try:
        return paths.load_config().get("lan_remote_tokens") is True
    except Exception:  # noqa: BLE001 - an unreadable settings file means the safe default
        return False


def exposure_text(cid: str, name: str, at: float, for_user: bool = True) -> str:
    """The warning about a remote token sent over the LAN. `for_user` (the user's own
    `flowstate remote clients`): with the command that keeps it anyway. The brief (an AI reads
    it) only says the user decides, in their terminal (task 56, E)."""
    from datetime import datetime
    day = datetime.fromtimestamp(at).strftime("%Y-%m-%d")
    keep = (f"to keep it anyway: `flowstate remote keep-token --client {cid}` (asks for your y)."
            if for_user else "keeping it anyway is the user's own choice, in their terminal "
                             "(`flowstate remote clients` shows how).")
    return (f"The remote token {name!r} ({cid}) was sent over plain HTTP from another computer "
            f"on your network on {day}; FlowState refused it, but anyone on that network could "
            f"have read it and could use it over the internet. Revoke it with `flowstate remote "
            f"revoke --client {cid}` and make a new one (or give that app a LAN key: `flowstate "
            f"keys add`); {keep}")


def exposure_lines() -> list[str]:
    """The brief's warning lines for remote tokens sent over the LAN (exposure_text), read
    without creating the settings database; [] when there is none or it can't be read."""
    path = auth_db_path()
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND "
                                "name = 'lan_exposures'").fetchone():
                return []
            rows = conn.execute(
                "SELECT e.client_id, cl.name, e.last_at FROM lan_exposures e JOIN clients cl "
                "ON cl.id = e.client_id WHERE cl.revoked_at IS NULL AND e.kept_at IS NULL "
                "ORDER BY e.last_at DESC LIMIT 3").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    return ["Security warning (tell the user once): " + exposure_text(*r, for_user=False)
            for r in rows]


def lan_warning(host: str) -> Optional[str]:
    """The start-up line for a LAN listener on anything but this computer (audit D4)."""
    if host in ("localhost",) or is_loopback(host):
        return None
    return (f"LAN mode on {host}: this is plain HTTP, NOT encrypted. The token and every record "
            f"cross the network readable by anyone on it. Use it only on a network you trust "
            f"(not public Wi-Fi), with per-app LAN keys (flowstate keys add). Remote-access "
            f"tokens are refused here unless you allow them (flowstate remote lan-tokens on).")


class Gate:
    """Outermost layer: Host allow-list, rate limits, one access-log line per request
    (method, path without query, status, time, client, IP; never bodies or headers), and
    the bearer check for the MCP path."""

    def __init__(self, app, checker: TokenChecker, mcp_path: str,
                 allowed_hosts: Optional[set[str]] = None, trust_proxy: bool = False,
                 resource_metadata_url: Optional[str] = None, peer_resolver=None,
                 listener: str = "lan", resource_server_url=None):
        from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
        self.app, self.checker, self.mcp_path = app, checker, mcp_path
        # the SDK's bearer check; with resource_server_url (AuthSettings with
        # validate_token_resource) it refuses a token issued for another resource (task 80)
        self.bearer = BearerAuthBackend(checker, resource_server_url=resource_server_url)
        # which listener this is (lan / public / relay), set here for every request so the
        # popup and the review walk know it from the server's own side, never from a header
        self.listener = listener
        self.allowed_hosts = {h.lower() for h in allowed_hosts} if allowed_hosts else None
        self.trust_proxy = trust_proxy
        self.rmd = resource_metadata_url
        # relay agent (relay/tunnel.py): maps its own loopback source port to the real client
        # IP, so no client-supplied header is trusted for rate limits
        self.peer_resolver = peer_resolver
        self.per_client = RateLimiter(_int_env("FLOWSTATE_RATE_LIMIT", 300))
        self.per_ip = RateLimiter(_int_env("FLOWSTATE_RATE_LIMIT_ANON", 60))

    def _ip(self, scope, headers) -> str:
        client = scope.get("client") or ("?", 0)
        peer = client[0]
        if self.peer_resolver and peer in ("127.0.0.1", "::1"):
            real = self.peer_resolver(client)
            if real:
                return real
        if self.trust_proxy and peer in ("127.0.0.1", "::1"):
            fwd = headers.get(b"cf-connecting-ip", b"").decode("latin-1").strip()
            if fwd:
                return fwd
        return peer

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope[LISTENER_KEY] = self.listener
        # the real socket peer (uvicorn runs with proxy_headers=False), never a header; the relay
        # agent also connects from loopback, so only the LAN listener can count as local
        from starlette.requests import HTTPConnection
        t0 = time.monotonic()
        headers = dict(scope.get("headers") or [])
        scope[LOCAL_KEY] = self.listener == "lan" and \
            is_loopback((scope.get("client") or ("",))[0]) and not proxied(headers)
        ip = self._ip(scope, headers)
        scope[CLIENT_IP_KEY] = ip
        path = scope.get("path", "")
        who = "-"
        status = {"code": 0}

        async def send_wrapped(msg):
            if msg["type"] == "http.response.start":
                status["code"] = msg["status"]
            await send(msg)

        try:
            host = headers.get(b"host", b"").decode("latin-1").lower()
            if self.allowed_hosts is not None and host.split(":")[0] not in self.allowed_hosts:
                await _json(send_wrapped, 421, {"error": "unknown host"})
                return
            if path.rstrip("/") == self.mcp_path:
                auth = headers.get(b"authorization", b"").decode("latin-1")
                tok = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
                found = await self.bearer.authenticate(HTTPConnection(scope)) if tok else None
                info = found[1].access_token if found else None
                if info is None:
                    ok, wait = self.per_ip.allow("ip:" + ip)
                    if not ok:
                        await _json(send_wrapped, 429, {"error": "rate_limited"},
                                    [(b"retry-after", str(wait).encode())])
                        return
                    challenge = 'Bearer error="invalid_token", error_description="Authentication required"'
                    if self.rmd:
                        challenge += f', resource_metadata="{self.rmd}", scope="{" ".join(SCOPES)}"'
                    await _json(send_wrapped, 401, {"error": "unauthorized"},
                                [(b"www-authenticate", challenge.encode())])
                    return
                if (self.listener == "lan" and info.identity_source == "remote_token"
                        and not scope.get(LOCAL_KEY)   # another computer, or through a proxy (A)
                        and not lan_remote_tokens_allowed()):
                    # LAN traffic is plain HTTP: a remote token seen on the Wi-Fi would work
                    # from anywhere through the relay or tunnel (audit D4); it has crossed the
                    # network already, so the user is told (audit N-F27)
                    try:
                        await asyncio.to_thread(self.checker.store.note_lan_exposure,
                                                info.client_id, ip)
                    except Exception as e:  # noqa: BLE001 - the refusal must still go out
                        log.warning("could not record a remote token sent over the LAN: %s",
                                    type(e).__name__)
                    log.warning("remote token %s was sent over plain HTTP from another "
                                "computer and refused; revoke it (flowstate remote clients)",
                                info.client_id)
                    await _json(send_wrapped, 401, {"error": "remote_token_on_lan", "message":
                                LAN_REMOTE_REFUSED},
                                [(b"www-authenticate", b'Bearer error="invalid_token"')])
                    return
                who = f"{info.client_id}({info.client_name})"
                ok, wait = self.per_client.allow("client:" + info.client_id)
                if not ok:
                    await _json(send_wrapped, 429, {"error": "rate_limited"},
                                [(b"retry-after", str(wait).encode())])
                    return
                scope["auth"], scope["user"] = found
            elif path != "/health":
                ok, wait = self.per_ip.allow("ip:" + ip)
                if not ok:
                    await _json(send_wrapped, 429, {"error": "rate_limited"},
                                [(b"retry-after", str(wait).encode())])
                    return
            await self.app(scope, receive, send_wrapped)
        finally:
            access_log.info("%s %s %s %.0fms client=%s ip=%s", scope.get("method", "?"), path,
                            status["code"], (time.monotonic() - t0) * 1000, who, ip)


async def _json(send, code: int, body: dict, extra_headers=None):
    data = json.dumps(body).encode()
    await send({"type": "http.response.start", "status": code,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(data)).encode())] + (extra_headers or [])})
    await send({"type": "http.response.body", "body": data})


# ---------------------------------------------------------------- tool policy
LOCAL_TOOLS = {"git"}                     # every action runs git on this computer
LOCAL_ACTIONS = {("file", "attach")}      # reads any file on this computer
# Deletes (asking for one, or approving one: server._review_decide) are for apps on this
# computer and the owner's LAN token only; remote, OAuth and per-app LAN keys never.
DELETE_TOOLS = {"delete_request"}
DELETE_ACTIONS = {("variable", "delete"), ("method", "delete"), ("file", "remove")}
DELETE_REFUSAL = ("not_allowed_remotely",
                  "Deleting from FlowState is only possible from an app on the FlowState computer "
                  "(or the owner's LAN token), and only with the user's approval. Nothing was "
                  "deleted. Tell the user.")


def may_delete(scopes: Optional[list[str]]) -> bool:
    return scopes is None or SCOPE_LOCAL in scopes


def refusal(name: str, args: dict, scopes: Optional[list[str]]) -> Optional[tuple[str, str]]:
    """(error, message) if this caller may not run this call; None if allowed.
    scopes None = stdio (the local app itself): no limits."""
    from .guard import writes_text
    if scopes is None:
        return None
    if (name in LOCAL_TOOLS or (name, args.get("action")) in LOCAL_ACTIONS) \
            and SCOPE_LOCAL not in scopes:
        return ("not_allowed_remotely",
                f"{name} works on files of the FlowState computer, so it is only available to "
                f"apps on that computer or the owner's LAN token.")
    if (name in DELETE_TOOLS or (name, args.get("action")) in DELETE_ACTIONS) \
            and not may_delete(scopes):
        return DELETE_REFUSAL
    if writes_text(name, args) and SCOPE_WRITE not in scopes:
        return ("read_only_client",
                "This app was given read-only access to FlowState, so it can't save or change "
                "anything. Tell the user; they can re-approve it with write access.")
    return None
