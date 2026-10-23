"""The quiet daily update check (2.4.0). Standard library only.

Once a day, lazily from start_here, ONE background thread asks GitHub for the newest FlowState
release and writes the answer to `update.json` in the FlowState data folder. The brief then
reads only that file: when a newer release exists it gets one line on top. Nobody waits for
the network, and nothing runs when nobody calls start_here.

The request: GET https://api.github.com/repos/dialectforge/FlowState/releases/latest with
`Accept: application/vnd.github+json` and `User-Agent: FlowState-update-check` (no version),
never an `Authorization` header, TLS 1.2+, no redirects followed, 5 s timeout, body capped at
256 KB, `If-None-Match` with the saved ETag. It sends nothing about the user or their projects
(only what any web request reveals: the IP address).

Off: `flowstate update check off` (config.json "update_check": false) or the environment
FLOWSTATE_UPDATE_CHECK=0. Off means no request at all and no line.
"""

import importlib.util
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import paths

RELEASES_URL = "https://api.github.com/repos/dialectforge/FlowState/releases/latest"
HTML_PREFIX = "https://github.com/dialectforge/FlowState/"
USER_AGENT = "FlowState-update-check"
TAG_RE = re.compile(r"^v(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,4})$")
TIMEOUT = 5.0
MAX_BODY = 256 * 1024
FRESH_SECONDS = 24 * 3600          # check again after a day
RETRY_SECONDS = 6 * 3600           # ... or after 6 hours when the last check failed
STAMP_STALE_SECONDS = 120          # a "checking" stamp older than this is ignored
STATE_NAME = "update.json"
MANUAL_STEPS = "git pull in the FlowState folder, then setup_flowstate.py --verify with the same options as before"

Fetch = Callable[[str, dict, float], tuple]   # (url, headers, timeout) -> (status, headers, body)

_lock = threading.Lock()
_worker: Optional[threading.Thread] = None


# ---------------------------------------------------------------- settings
def is_on() -> bool:
    """The check is on unless FLOWSTATE_UPDATE_CHECK=0 or config.json says "update_check": false."""
    env = (os.environ.get("FLOWSTATE_UPDATE_CHECK") or "").strip().lower()
    if env in ("0", "false", "off", "no"):
        return False
    try:
        return paths.load_config().get("update_check") is not False
    except Exception:  # noqa: BLE001
        return True


def set_on(on: bool) -> Path:
    cfg = paths.load_config()
    cfg["update_check"] = bool(on)
    return paths.save_config(cfg)


def self_update_allowed() -> bool:
    """config.json "self_update": false opts this computer out of `flowstate update`."""
    try:
        return paths.load_config().get("self_update") is not False
    except Exception:  # noqa: BLE001
        return True


def has_update_command() -> bool:
    """Does the code running now have `flowstate update` (the self-updater module)?"""
    try:
        return importlib.util.find_spec(f"{__package__}.selfupdate") is not None
    except (ImportError, ValueError):
        return False


# ---------------------------------------------------------------- versions
def parse_version(v) -> Optional[tuple]:
    m = VERSION_RE.fullmatch(str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def newer(a, b) -> bool:
    """Is version a strictly newer than version b? False when either is not X.Y.Z."""
    pa, pb = parse_version(a), parse_version(b)
    return bool(pa and pb and pa > pb)


# ---------------------------------------------------------------- the state file
def state_file() -> Path:
    return paths.data_root() / STATE_NAME


def read_state(path: Optional[Path] = None) -> dict:
    try:
        data = json.loads(Path(path or state_file()).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(changes: dict, path: Optional[Path] = None) -> dict:
    """Merge `changes` into update.json (atomic replace) and return the new state."""
    p = Path(path or state_file())
    p.parent.mkdir(parents=True, exist_ok=True)
    state = {**read_state(p), **changes}
    tmp = p.with_name(f"{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    paths.replace_file(tmp, p)
    return state


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch(iso) -> Optional[float]:
    try:
        return datetime.strptime(str(iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None


def local_time(iso) -> str:
    ts = _epoch(iso)
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts is not None else "never"


def due(state: dict, now: Optional[float] = None) -> bool:
    last = _epoch(state.get("checked_at"))
    if last is None:
        return True
    wait = RETRY_SECONDS if state.get("last_error") else FRESH_SECONDS
    return (now if now is not None else time.time()) - last >= wait


# ---------------------------------------------------------------- the request
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # a 3xx comes back as a status, never followed
        return None


def _context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def _http_get(url: str, headers: dict, timeout: float) -> tuple:
    """The real request: (status, headers, body). Reads at most MAX_BODY + 1 bytes."""
    req = urllib.request.Request(url, method="GET", headers=headers)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_context()), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, dict(e.headers or {}), b""


def request_headers(etag: Optional[str] = None) -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if etag:
        h["If-None-Match"] = etag
    return h


def _release(data) -> Optional[dict]:
    """The fields we keep from a 'latest release' answer, or None when it isn't one we trust."""
    if not isinstance(data, dict):
        return None
    tag, url = data.get("tag_name"), data.get("html_url")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        return None
    if data.get("draft") is not False or data.get("prerelease") is not False:
        return None
    if not isinstance(url, str) or not url.startswith(HTML_PREFIX):
        return None
    published = data.get("published_at")
    return {"latest": tag[1:], "tag": tag, "url": url,
            "published_at": published if isinstance(published, str) and len(published) <= 40 else None}


def fetch_latest(fetch: Optional[Fetch] = None, etag: Optional[str] = None) -> dict:
    """Ask GitHub for the newest release. Never raises. One of:
    {"ok": True, "latest", "tag", "url", "published_at", "etag"}
    {"ok": True, "not_modified": True}            (304 for our ETag: nothing new)
    {"ok": False, "error": "<short reason>"}      (offline, 403/429, odd answer, ...)"""
    fetch = fetch or _http_get
    try:
        status, headers, body = fetch(RELEASES_URL, request_headers(etag), TIMEOUT)
    except (TimeoutError, ssl.SSLError, urllib.error.URLError, OSError, ValueError) as e:
        if isinstance(e, TimeoutError) or "timed out" in str(e):
            return {"ok": False, "error": "GitHub did not answer within 5 seconds"}
        return {"ok": False, "error": f"can't reach GitHub ({type(e).__name__})"}
    except Exception as e:  # noqa: BLE001 - a check must never break anything
        return {"ok": False, "error": f"check failed ({type(e).__name__})"}
    if status == 304:
        return {"ok": True, "not_modified": True}
    if status in (403, 429):
        return {"ok": False, "error": f"GitHub refused the check for now ({status}, rate limit)"}
    if status != 200:
        return {"ok": False, "error": f"GitHub answered {status}"}
    body = body or b""
    if len(body) > MAX_BODY:
        return {"ok": False, "error": "GitHub's answer was too large"}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {"ok": False, "error": "GitHub's answer was not JSON"}
    rel = _release(data)
    if rel is None:
        return {"ok": False, "error": "GitHub's answer was not a FlowState release"}
    tag_etag = {k.lower(): v for k, v in (headers or {}).items()}.get("etag")
    rel["etag"] = tag_etag if isinstance(tag_etag, str) and len(tag_etag) <= 200 else None
    return {"ok": True, **rel}


def check_now(fetch: Optional[Fetch] = None, path: Optional[Path] = None,
              now: Optional[float] = None) -> dict:
    """Fetch in the foreground and save the result. Returns the new state. Never raises."""
    state = read_state(path)
    res = fetch_latest(fetch, state.get("etag") if state.get("latest") else None)
    stamp = {"checked_at": _iso(now if now is not None else time.time())}
    if not res["ok"]:
        stamp["last_error"] = res["error"]
    elif res.get("not_modified"):
        stamp["last_error"] = None
    else:
        stamp.update({k: res.get(k) for k in ("latest", "tag", "url", "published_at", "etag")},
                     last_error=None)
    try:
        return write_state(stamp, path)
    except OSError as e:
        return {**state, **stamp, "last_error": stamp.get("last_error") or f"could not save ({type(e).__name__})"}


# ---------------------------------------------------------------- the lazy background check
def _stamp_file(path: Optional[Path]) -> Path:
    p = Path(path or state_file())
    return p.with_name(p.name + ".checking")


def _take_stamp(stamp: Path, now: float) -> bool:
    """Only one process checks at a time; a stamp older than 2 minutes is a leftover."""
    stamp.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(stamp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, _iso(now).encode())
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if now - stamp.stat().st_mtime < STAMP_STALE_SECONDS:
                    return False
                stamp.unlink()
            except OSError:
                return False
    return False


def maybe_refresh(fetch: Optional[Fetch] = None, path: Optional[Path] = None,
                  now: Optional[float] = None) -> bool:
    """If the check is on and the saved answer is old, start ONE daemon thread that checks.
    Returns at once (True when a thread was started). Never raises."""
    global _worker
    try:
        if not is_on():
            return False
        now = now if now is not None else time.time()
        path = Path(path or state_file())
        if not due(read_state(path), now):
            return False
        with _lock:
            if _worker is not None and _worker.is_alive():
                return False
            stamp = _stamp_file(path)
            if not _take_stamp(stamp, now):
                return False

            def run():
                try:
                    check_now(fetch, path)
                finally:
                    try:
                        stamp.unlink()
                    except OSError:
                        pass
            _worker = threading.Thread(target=run, name="flowstate-update-check", daemon=True)
            _worker.start()
            return True
    except Exception:  # noqa: BLE001 - never into a tool
        return False


# ---------------------------------------------------------------- the brief line
def restart_line(running_version: str, state: dict) -> Optional[str]:
    """After `flowstate update`, processes still running the old code say so."""
    last = state.get("last_update") or {}
    if last.get("result") == "ok" and newer(last.get("to"), running_version):
        return (f"FlowState was updated to {last['to']}; this app still runs {running_version}. "
                f"Ask the user to restart it.")
    return None


def brief_line(running_version: str, path: Optional[Path] = None) -> Optional[str]:
    """One line when the saved answer names a release newer than the running code (or when
    this process runs older code than `flowstate update` installed), else None.
    Reads only update.json. Never raises."""
    try:
        st = read_state(path)
        restart = restart_line(running_version, st)
        if restart:
            return restart
        if not is_on():
            return None
        latest = st.get("latest")
        if not newer(latest, running_version):
            return None
        url = st.get("url") if str(st.get("url") or "").startswith(HTML_PREFIX) else HTML_PREFIX + "releases"
        head = f"FlowState {latest} is out (this computer has {running_version}). Tell the user once"
        if has_update_command() and self_update_allowed():
            how = (f"; if they want it, run flowstate update --dry-run and show them the plan "
                   f"({url}).")
        else:
            how = f"; if they want it: {MANUAL_STEPS} ({url})."
        line = head + how + " Never update without being asked."
        return line if len(line) < 300 else head + f" ({url}). Never update without being asked."
    except Exception:  # noqa: BLE001
        return None


def brief_lines(running_version: str) -> Optional[str]:
    """What start_here puts on top: starts the lazy check if due, then the line(s). Never raises."""
    maybe_refresh()
    return brief_line(running_version)


# ---------------------------------------------------------------- `flowstate update check`
def cli_check(sub: Optional[str], say=print) -> int:
    if sub in ("on", "off"):
        p = set_on(sub == "on")
        say(f"Update check is {sub} (saved in {p})."
            + ("" if sub == "on" else " FlowState makes no request to GitHub now."))
        if sub == "on" and not is_on():
            say("Note: FLOWSTATE_UPDATE_CHECK=0 is set in this environment, which keeps it off here.")
        return 0
    if sub == "now":
        if not is_on():
            say("The update check is off (flowstate update check on to turn it on). Nothing was asked.")
            return 1
        st = check_now()
        _show(st, say)
        return 0 if not st.get("last_error") else 1
    _show(read_state(), say)
    return 0


def _show(st: dict, say) -> None:
    from . import __version__
    say(f"Update check: {'on' if is_on() else 'off'} (once a day, from GitHub; "
        f"flowstate update check on|off|now).")
    say(f"This computer runs FlowState {__version__}.")
    say(f"Last check: {local_time(st.get('checked_at'))}"
        + (f"; newest release seen: {st['latest']} ({st.get('url')})" if st.get("latest") else ""))
    if st.get("last_error"):
        say(f"Last error: {st['last_error']}")
    if newer(st.get("latest"), __version__):
        say(f"A newer release is out: {st['latest']}.")
