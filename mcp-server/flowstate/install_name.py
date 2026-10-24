"""The name people see for this FlowState install ("Main FlowState" until the user changes it).

    flowstate name                 show it
    flowstate name "Office laptop" set it
    flowstate name --reset         back to "Main FlowState"

Kept in config.json as "display_name" (absent = the default). It is only a label: the relay
handle stays the address, the routing and the sign-in. Shown on FlowState's own pages (the
local and tunnel review pages, the phone list and approve pages). Sent to the relay (in the
agent's authenticated hello) only once the user has set one (shared()), so flowstatemcp.com can
say "Open what's waiting for you on <name>?" instead of the handle; without one, or after
`--reset`, the agent sends null and the site clears it and says "your FlowState". Never in a URL.

A valid name: trimmed (inner runs of spaces become one), 1-40 characters, plain text with no
control or format characters (no newlines, tabs, zero-width or direction marks). Every page
escapes it. Only the user's terminal (`flowstate name`, cli below) and the local review page
(review_page.py, settings "edit") change it, each logged in review_mode_log; no MCP tool reads
or writes it.
"""

import time
import unicodedata
from pathlib import Path
from typing import Optional

from . import paths

DEFAULT = "Main FlowState"
MAX_LEN = 40
KEY = "display_name"
SCOPE = "config:display_name"


class BadName(ValueError):
    """A name that isn't allowed; the message says why in plain words."""


def clean(raw) -> str:
    """The name as it will be kept, or BadName saying why it can't be."""
    if not isinstance(raw, str):
        raise BadName("The name must be text.")
    if any(unicodedata.category(ch).startswith("C") for ch in raw):
        raise BadName("The name can't contain control characters (newlines, tabs and the like).")
    name = " ".join(raw.split())
    if not name:
        raise BadName("The name can't be empty.")
    if len(name) > MAX_LEN:
        raise BadName(f"The name can be at most {MAX_LEN} characters ({len(name)} given).")
    return name


def valid(raw) -> Optional[str]:
    try:
        return clean(raw)
    except BadName:
        return None


def get() -> str:
    """The display name; the default when none is set or the stored one isn't valid."""
    try:
        return valid(paths.load_config().get(KEY)) or DEFAULT
    except Exception:  # noqa: BLE001 - a broken config file must not break a page
        return DEFAULT


def is_default() -> bool:
    try:
        return valid(paths.load_config().get(KEY)) is None
    except Exception:  # noqa: BLE001
        return True


def shared() -> Optional[str]:
    """The name to send to the relay: the one the user set, else None (the default
    "Main FlowState" is never sent). Raises if config.json can't be read (the caller then
    leaves the relay's copy as it is)."""
    return valid(paths.load_config().get(KEY))


def set_name(raw: Optional[str], db_path: Optional[Path] = None, who: str = "command-line",
             how: Optional[str] = None) -> dict:
    """Set the name (`raw` None: back to the default, the key removed) and log one
    review_mode_log row in `db_path` (when that database has the table) saying `who` changed it
    and `how`. BadName for a name that isn't allowed (nothing changed then)."""
    name = None if raw is None else clean(raw)
    old = get()
    cfg = paths.load_config()
    if name is None or name == DEFAULT:
        cfg.pop(KEY, None)
    else:
        cfg[KEY] = name
    path = paths.save_config(cfg)
    new = get()
    if old != new and db_path is not None and Path(db_path).is_file():
        from . import approvals, dbconn, identity, review_mode
        conn = dbconn.connect(str(db_path), timeout=5.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            if review_mode.available(conn):
                identity.insert(conn, "review_mode_log", {
                    "scope": SCOPE, "old_mode": old, "new_mode": new,
                    "client": who[:80], "caller": who[:80],
                    "user_request": how or approvals.TERMINAL_WORDS,
                    "changed_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}, who)
                conn.commit()
        finally:
            conn.close()
    return {"old": old, "now": new, "path": path, "changed": old != new}


# ---------------------------------------------------------------- `flowstate name`
def cli(value: Optional[str], reset: bool, db_path: Optional[Path], say=print) -> int:
    if reset and value is not None:
        say('Give a new name or --reset, not both.')
        return 2
    if reset or value is not None:
        try:
            res = set_name(None if reset else value, db_path)
        except BadName as e:
            say(f"Not changed: {e}")
            return 2
        if not res["changed"]:
            say(f"This FlowState is already called \"{res['now']}\".")
        else:
            say(f"This FlowState is now called \"{res['now']}\" (was \"{res['old']}\"; saved in "
                f"{res['path']}).")
            if reset or res["now"] == DEFAULT:
                say("With the relay, flowstatemcp.com forgets the name once `flowstate relay run` "
                    "has told it (within a minute while it runs) and says \"your FlowState\".")
            else:
                say("With the relay, flowstatemcp.com shows the new name once `flowstate relay "
                    "run` has sent it (within a minute while it runs).")
        return 0
    name = get()
    say(f"This FlowState is called \"{name}\"" + (" (the default)." if is_default() else "."))
    say('Change it with `flowstate name "New name"` (1-40 characters), or `flowstate name '
        '--reset` for "Main FlowState". It is only a label; your relay address stays the same.')
    return 0
