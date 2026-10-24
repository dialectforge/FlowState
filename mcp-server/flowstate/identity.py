"""Who is calling: app, model, machine, and how FlowState knows the app (migration 012).

Every recorded change (edit history, review queue items and decisions, links, handoffs,
hard rules, review-mode changes, the activity log) stores the same four values, taken from
here so nothing records identity in two different ways:

  app              the app's name. From the per-app key / remote token / OAuth client when
                   there is one (the app can't pick it), else the MCP client name it sends.
                   Through the relay the sign-in is often shared or reused by several cloud
                   apps, so there it is the MCP client name the app sends, else "Relay
                   (unknown app)"; the sign-in's name goes into `machine` and `credential`.
  model            the AI model name, ONLY when the caller supplies it: the `model` argument,
                   else the X-FlowState-Model header (HTTP), else FLOWSTATE_MODEL (stdio).
                   Never guessed; NULL otherwise.
  machine          stdio: FLOWSTATE_WORKSPACE, else this computer's host name. LAN/local
                   HTTP: the key's workspace, else the X-FlowState-Workspace header. Remote
                   (OAuth / remote tokens): the client's name. Relay: "relay (signed in as
                   <client's name>)".
  identity_source  app_key | oauth_client | remote_token | shared_token | self_reported

Plus `credential` (not stored): the per-app key's / token's / OAuth client's own name, when
the app name came from somewhere else (the relay); a paused app name stops it too.

Plus `session` (not a stored column of the logs above; todo claims use it to tell two
sessions of one app apart): stdio = this server process, HTTP = the MCP session id.

server.call_tool works out the identity once per call and sets it for the worker thread;
code outside a tool call (command line, review page) records itself as a local,
self-reported caller.
"""

import contextvars
import os
import socket
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Optional

SOURCES = ("app_key", "oauth_client", "remote_token", "shared_token", "self_reported")
RELAY_UNKNOWN = "Relay (unknown app)"
LABELS = {"app_key": "per-app key", "oauth_client": "OAuth app", "remote_token": "remote token",
          "shared_token": "shared token (legacy)", "self_reported": "self-reported"}
FIELDS = ("app", "model", "machine", "identity_source")
MAX_LEN = 80
PROCESS_SESSION = uuid.uuid4().hex[:12]   # stdio: one server process = one session


@dataclass(frozen=True)
class Identity:
    app: str
    model: Optional[str]
    machine: Optional[str]
    identity_source: str
    session: Optional[str] = None
    credential: Optional[str] = None

    def as_dict(self) -> dict:
        """The four recorded fields (not the session)."""
        return {k: v for k, v in asdict(self).items() if k in FIELDS}


_current: contextvars.ContextVar[Optional[Identity]] = contextvars.ContextVar(
    "flowstate_identity", default=None)


def clean(value) -> Optional[str]:
    """A short single-line label, or None (empty, not text, or looks like a secret)."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())[:MAX_LEN]
    if not text:
        return None
    from .secrets_scan import scan_text
    return None if scan_text(text) else text


def stdio_machine() -> Optional[str]:
    return clean(os.environ.get("FLOWSTATE_WORKSPACE")) or clean(socket.gethostname())


def _sent_name(client) -> Optional[str]:
    """The MCP client name the app sent at initialize, or None (none sent: "unknown")."""
    name = clean(client)
    return None if name is None or name.lower() == "unknown" else name


def resolve(client: str, http: bool = False, token=None, headers: Optional[dict] = None,
            model_arg=None, listener: Optional[str] = None) -> Identity:
    """The identity of one tool call. stdio: http=False. HTTP: `token` is the FSAccessToken
    the caller signed in with, `headers` the request headers (lower-case names), `listener`
    the one Gate marked (lan / public / relay)."""
    headers = headers or {}
    model = clean(model_arg)
    if not http:   # stdio: the app that started this process
        return Identity(clean(client) or "unknown",
                        model or clean(os.environ.get("FLOWSTATE_MODEL")),
                        stdio_machine(), "self_reported", PROCESS_SESSION)
    model = model or clean(headers.get("x-flowstate-model"))
    workspace = clean(headers.get("x-flowstate-workspace"))
    session = clean(headers.get("mcp-session-id"))
    source = getattr(token, "identity_source", "") if token is not None else ""
    name = clean(getattr(token, "client_name", "")) or "unknown"
    if listener == "relay":
        # one OAuth sign-in or remote token can sit behind several cloud apps (or be named
        # after another app), so its name would credit the wrong app: the app's own name
        # instead, or an honest "unknown", and the sign-in in the machine column
        signed_in = f"relay (signed in as {name})" if token is not None else "relay"
        return Identity(_sent_name(client) or RELAY_UNKNOWN, model, clean(signed_in),
                        source or "self_reported", session,
                        name if token is not None else None)
    if source == "app_key":
        return Identity(name, model, clean(getattr(token, "workspace", None)) or workspace,
                        "app_key", session)
    if source in ("oauth_client", "remote_token"):
        return Identity(name, model, name, source, session)
    # the one shared LAN token: the app name is only the app's own say-so
    return Identity(clean(client) or "unknown", model, workspace,
                    "shared_token" if token is not None else "self_reported", session)


def local(app: str) -> Identity:
    """A caller on this computer outside a tool call (command line, review page)."""
    return Identity(clean(app) or "unknown", None, stdio_machine(), "self_reported",
                    PROCESS_SESSION)


def current() -> Optional[Identity]:
    return _current.get()


_call: contextvars.ContextVar[tuple] = contextvars.ContextVar("flowstate_call", default=(None, None))


def set_call(conn_key: Optional[str], change_id: Optional[str]) -> contextvars.Token:
    """The current tool call's connection key and, for a write, its change id (receipts.py;
    the edit history uses the change id as its edit id)."""
    return _call.set((conn_key, change_id))


def reset_call(token: contextvars.Token) -> None:
    _call.reset(token)


def change_id() -> Optional[str]:
    return _call.get()[1]


def call_key() -> Optional[str]:
    """The client connection of the current call (receipts: what counts as "the same
    session" for a repeated write)."""
    ident = current()
    return _call.get()[0] or (ident.session if ident is not None else None)


def conn() -> tuple[Optional[str], Optional[Identity]]:
    """(session key, identity) of the current call. Each stdio server process and each HTTP
    MCP session gets its own automatic session; None outside a tool call."""
    ident = current()
    return (ident.session if ident is not None else None), ident


def set_current(ident: Identity) -> contextvars.Token:
    return _current.set(ident)


def reset(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def use(ident: Identity):
    tok = set_current(ident)
    try:
        yield ident
    finally:
        reset(tok)


def stamp(client: Optional[str] = None) -> dict:
    """{app, model, machine, identity_source} for the current caller."""
    return (current() or local(client or "unknown")).as_dict()


def _columns(conn, table: str) -> set:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def has_column(conn, table: str, column: str) -> bool:
    return column in _columns(conn, table)


def fields(conn, table: str, client: Optional[str] = None, prefix: str = "",
           skip: tuple = (), ident: Optional[Identity] = None) -> dict:
    """{column: value} of the identity columns `table` has, named prefix + field (empty on a
    database migration 012 has not reached, so writes still work there). `ident` overrides
    the current caller (e.g. a session summary FlowState writes for the app it belongs to)."""
    have = _columns(conn, table)
    values = ident.as_dict() if ident is not None else stamp(client)
    return {prefix + k: v for k, v in values.items() if k not in skip and prefix + k in have}


def insert(conn, table: str, row: dict, client: Optional[str] = None,
           ident: Optional[Identity] = None):
    """INSERT `row` plus the caller's identity columns (a column `row` already has, e.g. the
    activity log's own `app`, is kept); returns the cursor."""
    row = {**fields(conn, table, client, ident=ident), **row}
    return conn.execute(f'INSERT INTO "{table}" ({", ".join(row)}) '
                        f'VALUES ({", ".join("?" * len(row))})', list(row.values()))


def set_sql(conn, table: str, client: Optional[str] = None, prefix: str = "",
            clear: bool = False) -> tuple[str, list]:
    """(", decided_app = ?, ...", values) to add to an UPDATE's SET list; clear=True sets
    them back to NULL."""
    who = fields(conn, table, client, prefix=prefix)
    if clear:
        return "".join(f", {k} = NULL" for k in who), []
    return "".join(f", {k} = ?" for k in who), list(who.values())


def on_behalf_of(app: str) -> Identity:
    """FlowState itself recording something for an app that isn't the caller right now (an
    automatically closed session's summary): the app's name only, nothing more is known."""
    return Identity(clean(app) or "unknown", None, None, "self_reported")


def label(source: Optional[str]) -> str:
    return LABELS.get(source or "", "not recorded (before this was tracked)")
