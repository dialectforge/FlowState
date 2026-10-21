"""Certificate Transparency watch.

Every publicly trusted certificate is logged in CT. Once a day the agent asks crt.sh for
every certificate that covers this computer's relay hostname (exact name and the
`*.fsmcp.com` wildcard), downloads each new one and compares its public key with
the local key(s). A certificate made with any other key means someone else (for example
whoever controls the relay's DNS) got a certificate for the hostname and could read relay
traffic: it is logged as an ERROR, kept in ct.json and shown at the top of `start_here` and
in `flowstate relay status` until the user acknowledges it.

Only the hostname is sent to crt.sh.
"""

import json
import logging
import time
from typing import Callable, Optional
from urllib.parse import quote

from . import HOST_SUFFIX, RelayError
from . import crypto as C
from .api import http_request

log = logging.getLogger("flowstate.relay.ct")

CRT_SH = "https://crt.sh/"
CHECK_EVERY = 24 * 3600
MAX_FETCH = 50  # certificates downloaded per run (be polite to crt.sh)


def _fetch(url: str) -> bytes:
    status, _, body = http_request("GET", url, headers={"Accept": "*/*"}, timeout=60)
    if status != 200:
        raise RelayError(f"crt.sh answered {status}")
    return body


def queries(hostname: str) -> list:
    return [hostname, f"*.{HOST_SUFFIX}"]


def check(hostname: str, known_spki: list, previous: Optional[dict] = None,
          fetch: Callable[[str], bytes] = _fetch, now: Optional[float] = None) -> dict:
    """One CT pass. Returns the new ct.json content (previous alerts are kept)."""
    prev = previous or {}
    seen = {str(k): v for k, v in (prev.get("seen") or {}).items()}
    alerts = list(prev.get("alerts") or [])
    out = {"hostname": hostname, "last_attempt": int(now or time.time()), "seen": seen,
           "alerts": alerts, "last_check": prev.get("last_check"), "error": None,
           "certificates": prev.get("certificates", 0)}
    try:
        entries = {}
        for q in queries(hostname):
            data = json.loads(fetch(f"{CRT_SH}?q={quote(q)}&output=json") or b"[]")
            for e in data if isinstance(data, list) else []:
                names = str(e.get("name_value", "")).split()
                if e.get("id") is not None and any(C.covers(n, hostname) for n in names):
                    entries[str(e["id"])] = e
        fetched = 0
        for cid, e in sorted(entries.items(), key=lambda kv: int(kv[0])):
            if cid in seen:
                continue
            if fetched >= MAX_FETCH:
                break
            fetched += 1
            info = C.cert_info(fetch(f"{CRT_SH}?d={quote(cid)}"))
            if not any(C.covers(n, hostname) for n in info["names"]):
                seen[cid] = "other-name"
                continue
            if info["spki_sha256"] in known_spki:
                seen[cid] = "ok"
                continue
            seen[cid] = "ALERT"
            alert = {"crtsh_id": int(cid), "issuer": info["issuer"], "names": info["names"],
                     "not_before": info["not_before"].isoformat(),
                     "not_after": info["not_after"].isoformat(),
                     "spki_sha256": info["spki_sha256"], "found_at": out["last_attempt"]}
            alerts.append(alert)
            log.error("RELAY SECURITY WARNING: certificate crt.sh id %s for %s (%s) uses a key "
                      "that is not this computer's. Someone else obtained a certificate for "
                      "your relay hostname. Run `flowstate relay disable` and check "
                      "https://crt.sh/?id=%s", cid, hostname, ", ".join(info["names"]), cid)
        out["certificates"] = len(entries)
        out["last_check"] = out["last_attempt"]
    except (RelayError, ValueError, KeyError, TypeError) as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        log.warning("CT check for %s could not finish: %s", hostname, out["error"])
    out["ok"] = not any(not a.get("acknowledged") for a in alerts)
    return out


def run_check(store, fetch: Callable[[str], bytes] = _fetch) -> dict:
    cfg = store.config()
    if not cfg.get("hostname"):
        raise RelayError("the relay is not set up")
    result = check(cfg["hostname"], list(cfg.get("known_spki") or []), store.ct(), fetch)
    store.save_ct(result)
    return result


def due(store, now: Optional[float] = None) -> bool:
    last = store.ct().get("last_attempt") or 0
    return (now or time.time()) - last >= CHECK_EVERY


def acknowledge(store, crtsh_id: int) -> bool:
    data = store.ct()
    hit = False
    for a in data.get("alerts") or []:
        if a.get("crtsh_id") == crtsh_id and not a.get("acknowledged"):
            a["acknowledged"] = int(time.time())
            hit = True
    if hit:
        data["ok"] = not any(not a.get("acknowledged") for a in data["alerts"])
        store.save_ct(data)
    return hit
