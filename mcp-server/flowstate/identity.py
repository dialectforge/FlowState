"""Who is calling: app, model, machine, and how FlowState knows the app (migration 012).

Every recorded change (edit history, review queue items and decisions, links, handoffs,
hard rules, review-mode changes, the activity log) stores the same four values, taken from
here so nothing records identity in two different ways:

  app              the app's name: the MCP client name the app sends. A per-app key, remote
                   token or OAuth sign-in is often shared or reused by several apps (or named
                   after another app), so its name would credit the wrong app; it goes into
                   `machine` and `credential` instead. When the app sends no name: the LAN
                   key's / token's name, "Relay (unknown app)" / "Tunnel (unknown app)".
                   Before 2.4.4 LAN keys and LAN remote tokens recorded the key's / token's
                   name here; those rows show as they are.
  model            the AI model name, ONLY when the caller supplies it: the `model` argument,
                   else the X-FlowState-Model header (HTTP), else FLOWSTATE_MODEL (stdio).
                   Never guessed; NULL otherwise.
  machine          stdio: FLOWSTATE_WORKSPACE, else this computer's host name (a random-looking
                   one as "computer-<8 hex>", see _host_name). Shared LAN
                   token: the X-FlowState-Workspace header. Per-app key: "<the key's
                   workspace, else the header> (signed in as <key name>)", or "lan (signed in
                   as <key name>)" with neither. Remote token / OAuth client on the LAN
                   listener: "lan (signed in as <client's name>)"; relay: "relay (signed in
                   as ...)"; own tunnel: "tunnel (signed in as ...)". shown() reads the
                   sign-in back from there, so pages say "Grok Bot (via Cursor)" when the
                   two names differ.
  identity_source  app_key | oauth_client | remote_token | shared_token | self_reported

Plus `credential` (not stored): the per-app key's / token's / OAuth client's own name (every
caller but stdio and the shared LAN token); a paused app name stops it too, and same_caller()
/ same_sign_in() treat the same sign-in as the same caller whatever name it sends. And
`aka`: the names that token had before `flowstate remote rename`, so a renamed token is still
the caller that wrote its older rows (own review items, undo).

Plus `session` (not a stored column of the logs above; todo claims use it to tell two
sessions of one app apart): stdio = this server process, HTTP = the MCP session id.

server.call_tool works out the identity once per call and sets it for the worker thread;
code outside a tool call (command line, review page) records itself as a local,
self-reported caller.
"""

import contextvars
import hashlib
import os
import re
import socket
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Optional

SOURCES = ("app_key", "oauth_client", "remote_token", "shared_token", "self_reported")
RELAY_UNKNOWN = "Relay (unknown app)"
TUNNEL_UNKNOWN = "Tunnel (unknown app)"
# listener -> (word in the machine column, fallback app name) where the app name is the app's
# own say-so and the sign-in it used goes into machine (see resolve)
SIGNED_IN = {"relay": ("relay", RELAY_UNKNOWN), "public": ("tunnel", TUNNEL_UNKNOWN)}
SIGN_IN_SOURCES = ("oauth_client", "remote_token")
VIA_SOURCES = SIGN_IN_SOURCES + ("app_key",)   # rows whose machine may carry "(signed in as X)"
_VIA = re.compile(r"^(?:(.*?) )?\(signed in as (.+?)\)?$")
_PLACES = ("relay", "tunnel", "lan")   # machine words that are a listener, not a workspace
LABELS = {"app_key": "per-app key", "oauth_client": "OAuth app", "remote_token": "remote token",
          "shared_token": "shared token (legacy)", "self_reported": "self-reported",
          "flowstate": "FlowState itself"}
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
    aka: tuple = ()

    def sign_ins(self) -> set:
        """The sign-in names this caller has had (now and before a rename), any case."""
        return {n.casefold() for n in (self.credential, *self.aka) if n}

    def as_dict(self) -> dict:
        """The four recorded fields (not the session)."""
        return {k: v for k, v in asdict(self).items() if k in FIELDS}


# FlowState itself as the one who queued an item (task 70): only the "reopen these possible
# conflicts?" question (conflict_reopen.py) is written with it. No caller ever is it: resolve()
# never gives this identity source, and "flowstate" / "system" are reserved app names
# (names.reserved), so no app can claim it by any name, header or tool argument.
SYSTEM_SOURCE = "flowstate"
SYSTEM = Identity("FlowState", None, None, SYSTEM_SOURCE)


def is_system(row) -> bool:
    """Was this row (a dict with app / identity_source) written by FlowState itself?"""
    return bool(row) and row.get("identity_source") == SYSTEM_SOURCE and row.get("app") == SYSTEM.app


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


def _host_name() -> Optional[str]:
    """This computer's name. A random-looking one (some cloud and CI machines) only looks like
    a token, so it is recorded as a short label that is the same on every run; a name holding a
    real key pattern records nothing, as clean() does."""
    host = socket.gethostname()
    name = clean(host)
    if name or not isinstance(host, str):
        return name
    from .secrets_scan import GENERIC_KINDS, scan_text
    if scan_text(" ".join(host.split())) in GENERIC_KINDS:
        return "computer-" + hashlib.sha256(host.encode("utf-8", "replace")).hexdigest()[:8]
    return None


def stdio_machine() -> Optional[str]:
    return clean(os.environ.get("FLOWSTATE_WORKSPACE")) or _host_name()


# "(via ...)" and "(signed in as ...)" are how FlowState shows a key's or sign-in's name next
# to the app's; an app may not put them in its own name to look like one, nor hide them with
# lookalike brackets or letters, invisible characters or direction marks (names.strip_marks),
# nor call itself by a name FlowState uses for the user or for itself ("user", "phone", ...)


def _app_name(client) -> Optional[str]:
    """The name an app sent, tidied, without anything that imitates FlowState's own marks."""
    from . import names
    name = clean(client)
    if name is None:
        return None
    name = clean(names.strip_marks(name))
    return None if name is None or names.reserved(name) else name


def _sent_name(client) -> Optional[str]:
    """The MCP client name the app sent at initialize, or None (none sent: "unknown")."""
    return _app_name(client)


def resolve(client: str, http: bool = False, token=None, headers: Optional[dict] = None,
            model_arg=None, listener: Optional[str] = None) -> Identity:
    """The identity of one tool call. stdio: http=False. HTTP: `token` is the FSAccessToken
    the caller signed in with, `headers` the request headers (lower-case names), `listener`
    the one Gate marked (lan / public / relay)."""
    headers = headers or {}
    model = clean(model_arg)
    if not http:   # stdio: the app that started this process
        return Identity(_app_name(client) or "unknown",
                        model or clean(os.environ.get("FLOWSTATE_MODEL")),
                        stdio_machine(), "self_reported", PROCESS_SESSION)
    model = model or clean(headers.get("x-flowstate-model"))
    workspace = clean(headers.get("x-flowstate-workspace"))
    session = clean(headers.get("mcp-session-id"))
    source = getattr(token, "identity_source", "") if token is not None else ""
    name = clean(getattr(token, "client_name", "")) or "unknown"
    aka = tuple(n for n in (clean(x) for x in getattr(token, "former_names", None) or ()) if n)
    if listener == "relay" or (listener == "public" and source in SIGN_IN_SOURCES):
        # one OAuth sign-in or remote token can sit behind several apps (or be named after
        # another app), so its name would credit the wrong app: the app's own name instead,
        # or an honest "unknown", and the sign-in in the machine column
        word, unknown = SIGNED_IN[listener]
        signed_in = f"{word} (signed in as {name})" if token is not None else word
        return Identity(_sent_name(client) or unknown, model, clean(signed_in),
                        source or "self_reported", session,
                        name if token is not None else None, aka if token is not None else ())
    if source == "app_key":   # the same rule on the LAN: the app's name, the key beside it
        place = clean(getattr(token, "workspace", None)) or workspace
        return Identity(_sent_name(client) or name, model, _signed_in(place, name), "app_key",
                        session, name, aka)
    if source in SIGN_IN_SOURCES:   # a remote token / OAuth client on the LAN listener
        return Identity(_sent_name(client) or name, model, _signed_in("lan", name), source,
                        session, name, aka)
    # the one shared LAN token: the app name is only the app's own say-so
    return Identity(_app_name(client) or "unknown", model, workspace,
                    "shared_token" if token is not None else "self_reported", session)


def _signed_in(place: Optional[str], name: str) -> Optional[str]:
    """"<place> (signed in as <name>)" within MAX_LEN, the sign-in's name kept whole. A
    workspace (it may come from a header) can't carry a sign-in of its own."""
    place = (place or "lan").replace("(", "").replace(")", "").strip() or "lan"
    tail = f" (signed in as {name})"
    room = MAX_LEN - len(tail)
    if room < 4:
        return clean(f"{place[:12]}{tail}")
    return clean(place[:room].rstrip() + tail)


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


def on_behalf_of(app: str, machine: Optional[str] = None, source: Optional[str] = None,
                model: Optional[str] = None) -> Identity:
    """FlowState itself recording something for an app that isn't the caller right now (an
    automatically closed session's summary): the app's name, plus what its session recorded
    about it (session_runs) when known."""
    return Identity(clean(app) or "unknown", clean(model), clean(machine),
                    source if source in SOURCES else "self_reported")


def _split(machine: Optional[str], source: Optional[str]) -> tuple:
    """(place, sign-in name) of a machine column resolve() wrote "(signed in as ...)" into,
    else (machine, None). Only for key / token / OAuth rows, so an app on the shared token
    can't fake a sign-in with its workspace header."""
    if source not in VIA_SOURCES or not isinstance(machine, str):
        return machine, None
    m = _VIA.match(machine)
    return (m.group(1), m.group(2)) if m else (machine, None)


def via(machine: Optional[str], source: Optional[str]) -> Optional[str]:
    """The sign-in (per-app key, remote token, OAuth client) name a call came in on, read back
    from the machine column resolve() wrote; None for every other caller and for rows from
    before 2.4.4 (LAN) / task 44 (own tunnel)."""
    return _split(machine, source)[1]


def same_sign_in(app: Optional[str], machine: Optional[str], source: Optional[str],
                 ident: Optional["Identity"]) -> bool:
    """Was this row written through the caller's own key / token / sign-in (under any name it
    had, `flowstate remote rename`)? Rows from before 2.4.4 on a LAN key or token recorded the
    key's name as the app, so that name counts for those. A key and a token of the same name
    are different sign-ins."""
    if ident is None or not ident.credential:
        return False
    if (source == "app_key") != (ident.identity_source == "app_key"):
        return False
    signed = via(machine, source)
    if signed is None and source in VIA_SOURCES and \
            not str(machine or "").startswith(("relay", "tunnel")):
        signed = app   # an older LAN row: the app column held the key's / token's name
    return bool(signed) and signed.casefold() in ident.sign_ins()


def shown(app: Optional[str], machine: Optional[str] = None, source: Optional[str] = None,
          on_machine: bool = False, fallback: str = "unknown") -> str:
    """Who did it, for people: the app's name, plus the sign-in it came in on when that
    differs ("Grok Bot (via Cursor)"), since the app name there is only its own claim.
    on_machine=True adds " on <machine>" for other callers. Old rows show as they are."""
    name = app or fallback
    place, signed = _split(machine, source)
    if signed:
        if name in (RELAY_UNKNOWN, TUNNEL_UNKNOWN):
            return f"{name[:-1]}, via {signed})"
        who = name if signed.casefold() == name.casefold() else f"{name} (via {signed})"
        # a per-app key's workspace is a real place; "lan" / "relay" / "tunnel" are not
        return f"{who} on {place}" if on_machine and place and place not in _PLACES else who
    return f"{name} on {machine}" if on_machine and machine else name


def shown_row(row, prefix: str = "", **kw) -> str:
    """shown() for a dict / sqlite3.Row with app / machine / identity_source columns."""
    keys = row.keys() if hasattr(row, "keys") else ()
    get = (lambda k: row[k] if k in keys else None)
    return shown(get(prefix + "app"), get(prefix + "machine"), get(prefix + "identity_source"),
                 **kw)


def same_caller(app: Optional[str], machine: Optional[str], source: Optional[str],
                ident: Optional[Identity]) -> bool:
    """Did this caller's app write a row (its recorded app, machine, identity source)? For
    permissions only the writer has (undo taking back a created record). A caller that came
    in on a key / token / sign-in is matched by that sign-in (same_sign_in, renames
    included), never by the name it sends (it can send any name); a row written through a
    sign-in matches only that sign-in; other rows by app name, as before."""
    if ident is None or source == SYSTEM_SOURCE:
        return False
    if ident.credential or via(machine, source):
        return same_sign_in(app, machine, source, ident)
    return bool(app) and app == ident.app


def label(source: Optional[str]) -> str:
    return LABELS.get(source or "", "not recorded (before this was tracked)")
