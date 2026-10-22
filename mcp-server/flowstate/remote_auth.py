"""Who may use the HTTP server, and how much: per-client tokens, OAuth 2.1, rate limits.

Three kinds of caller:
  * the owner's LAN token (FLOWSTATE_TOKEN / credential store): full access, including the
    tools that touch this computer (git, file attach). Refused on the public URL unless
    --allow-master-token is given.
  * per-client tokens (`flowstate remote add-client NAME [--read-only]`): one token per
    app, each revocable on its own. For apps that take a fixed "Authorization: Bearer"
    header (Cursor, Claude Code, the xAI API, ...).
  * per-app LAN keys (`flowstate keys add --name NAME [--read-only]`, app_keys.py): the
    same idea for the local / LAN listener only; the recorded app name comes from the key.
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
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, RegistrationError,
                                      TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from . import paths

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

# Where OAuth clients may send the browser back to. Hosts of the apps we expect, plus
# loopback (Claude Code / Cursor desktop). Add more with `flowstate remote allow-redirect`.
DEFAULT_REDIRECT_HOSTS = ["claude.ai", "claude.com", "chatgpt.com", "www.cursor.com",
                          "cursor.com", "localhost", "127.0.0.1"]

TOKEN_PREFIX = {"access": "fs_at_", "refresh": "fs_rt_", "static": "fs_ct_", "code": "fs_ac_"}
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
"""


class AuthStore:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else auth_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        salt = secrets.token_bytes(16)
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
        with self._conn() as c:
            row = c.execute("SELECT salt, hash FROM owner WHERE id = 1").fetchone()
        ok = bool(row) and hmac.compare_digest(_scrypt(passphrase, row["salt"]), row["hash"])
        if not ok:
            with self._conn() as c:
                c.execute("INSERT INTO failures (at) VALUES (?)", (time.time(),))
        return ok

    # -- clients
    def add_token_client(self, name: str, scopes: list[str],
                         days: Optional[int] = None) -> tuple[str, str]:
        """(client_id, token). The token is returned once and only its hash is kept."""
        cid = "tok_" + secrets.token_hex(6)
        token = _new("static")
        now = time.time()
        with self._conn() as c:
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
            c.execute("INSERT INTO clients (id, name, kind, scopes, info, created_at) "
                      "VALUES (?, ?, 'oauth', ?, ?, ?)",
                      (info.client_id, (info.client_name or "unnamed app")[:80],
                       info.scope or " ".join(SCOPES), info.model_dump_json(), now))

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

    def revoke_client(self, client_id: str) -> bool:
        now = time.time()
        with self._conn() as c:
            n = c.execute("UPDATE clients SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                          (now, client_id)).rowcount
            c.execute("UPDATE tokens SET revoked_at = ? WHERE client_id = ? AND revoked_at IS NULL",
                      (now, client_id))
            c.execute("DELETE FROM pending WHERE client_id = ?", (client_id,))
        return n > 0

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
            c.execute("UPDATE clients SET approved_at = COALESCE(approved_at, ?) WHERE id = ?",
                      (time.time(), client_id))


def _scrypt(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(passphrase.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


# ---------------------------------------------------------------- OAuth provider
class FSAccessToken(AccessToken):
    client_name: str = ""
    identity_source: str = ""        # see identity.py: app_key, oauth_client, remote_token, ...
    workspace: Optional[str] = None  # per-app LAN keys: the machine / workspace it was made for


class FlowStateOAuthProvider:
    """MCP SDK OAuthAuthorizationServerProvider backed by AuthStore. The SDK's handlers do
    the protocol checks (PKCE S256, redirect URI match, grant types); this class decides
    who gets what and keeps the records."""

    def __init__(self, store: AuthStore, issuer_url: str, resource_url: str):
        self.store = store
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url

    def _same_resource(self, resource: Optional[str]) -> bool:
        return resource is None or resource.rstrip("/") == self.resource_url.rstrip("/")

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
        return _token_obj(token, row)

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


def _token_obj(token: str, row) -> FSAccessToken:
    return FSAccessToken(token=token, client_id=row["client_id"], scopes=row["scopes"].split(),
                         expires_at=int(row["expires_at"]) if row["expires_at"] else None,
                         resource=row["resource"], client_name=row["client_name"],
                         identity_source="oauth_client" if row["kind"] == "access" else "remote_token")


class TokenChecker:
    """Bearer check for /mcp: owner token (if allowed), per-client tokens, OAuth tokens, and
    (LAN listener only, app_keys=True) per-app LAN keys."""

    def __init__(self, store: Optional[AuthStore], master: Optional[str],
                 provider: Optional[FlowStateOAuthProvider] = None, app_keys: bool = False):
        self.store, self.provider = store, provider
        self.master = master.encode() if master and len(master) >= 16 else None
        self.app_keys = app_keys

    def check(self, token: str) -> Optional[FSAccessToken]:
        if self.master and hmac.compare_digest(token.encode(), self.master):
            return FSAccessToken(token="", client_id="owner", client_name="owner (LAN token)",
                                 scopes=[SCOPE_READ, SCOPE_WRITE, SCOPE_LOCAL],
                                 identity_source="shared_token")
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
        if row["kind"] == "access":
            if not self.provider or row["resource"] != self.provider.resource_url:
                return None
        self.store.touch(row["client_id"])
        return _token_obj(token, row)


# ---------------------------------------------------------------- consent page
_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>FlowState: allow access?</title>
<style>body{{font-family:system-ui,sans-serif;max-width:32rem;margin:2rem auto;padding:0 1rem}}
.box{{border:1px solid #ccc;border-radius:8px;padding:1rem}}.err{{color:#b00020}}
label{{display:block;margin:.5rem 0}}input[type=password]{{width:100%;padding:.4rem}}
button{{padding:.5rem 1rem;margin-right:.5rem}}</style></head><body>
<h1>Allow access to your FlowState?</h1><div class="box">{body}</div></body></html>"""


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
        ok = await asyncio.to_thread(store.check_passphrase, str(form.get("passphrase", "")))
        if not ok:
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


def is_loopback(peer) -> bool:
    """The socket peer address is this computer (127.0.0.0/8, ::1, ::ffff:127.x)."""
    import ipaddress
    try:
        ip = ipaddress.ip_address(str(peer or "").split("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


class Gate:
    """Outermost layer: Host allow-list, rate limits, one access-log line per request
    (method, path without query, status, time, client, IP; never bodies or headers), and
    the bearer check for the MCP path."""

    def __init__(self, app, checker: TokenChecker, mcp_path: str,
                 allowed_hosts: Optional[set[str]] = None, trust_proxy: bool = False,
                 resource_metadata_url: Optional[str] = None, peer_resolver=None,
                 listener: str = "lan"):
        self.app, self.checker, self.mcp_path = app, checker, mcp_path
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
        scope[LOCAL_KEY] = self.listener == "lan" and is_loopback((scope.get("client") or ("",))[0])
        from starlette.authentication import AuthCredentials
        from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
        t0 = time.monotonic()
        headers = dict(scope.get("headers") or [])
        ip = self._ip(scope, headers)
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
                info = await asyncio.to_thread(self.checker.check, tok) if tok else None
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
                who = f"{info.client_id}({info.client_name})"
                ok, wait = self.per_client.allow("client:" + info.client_id)
                if not ok:
                    await _json(send_wrapped, 429, {"error": "rate_limited"},
                                [(b"retry-after", str(wait).encode())])
                    return
                scope["user"] = AuthenticatedUser(info)
                scope["auth"] = AuthCredentials(info.scopes)
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
