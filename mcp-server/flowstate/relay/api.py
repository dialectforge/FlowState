"""The relay's HTTPS API (standard library only). Two calls:

    POST /api/enroll         {"code"}                     -> {"handle", "device_token", "hostname"}
    POST /api/dns-challenge  {"action": "set"|"clear", "name", "value"?}   (Bearer device token)

The relay can only ever publish the `_acme-challenge` TXT value the agent hands it, for the
agent's own handle; it never sees a private key. See docs/relay-protocol.md section 2.
"""

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import urlparse

from .. import __version__
from . import HOST_SUFFIX, RelayError

AGENT = f"flowstate/{__version__}"
HANDLE_RE = re.compile(r"^[a-z0-9]{4,32}$")
CODE_RE = re.compile(r"^[A-Za-z0-9-]{4,64}$")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_BODY = 4 * 1024 * 1024


def check_url(url: str) -> str:
    """https only; plain http is allowed for loopback (tests and local development)."""
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return url
    if u.scheme == "http" and u.hostname in LOOPBACK:
        return url
    raise RelayError(f"refusing a non-https URL: {u.scheme}://{u.hostname or '?'}")


def client_context(ca_file: Optional[str] = None) -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # a 3xx comes back as a status, never followed
        return None


def http_request(method: str, url: str, body: Optional[bytes] = None,
                 headers: Optional[dict] = None, ctx: Optional[ssl.SSLContext] = None,
                 timeout: float = 30) -> tuple:
    """(status, headers, body). 3xx/4xx/5xx are returned, not raised or followed; network
    errors raise RelayError. Bodies are capped at MAX_BODY."""
    check_url(url)
    h = {"User-Agent": AGENT}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx or client_context()), _NoRedirect())
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, r.headers, r.read(MAX_BODY)
    except urllib.error.HTTPError as e:
        with e:
            return e.code, e.headers, e.read(MAX_BODY)
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise RelayError(f"could not reach {urlparse(url).hostname}: {reason}") from None


def _json_call(method, url, payload, token=None, ctx=None) -> dict:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    status, _, body = http_request(method, url, json.dumps(payload).encode(), headers, ctx)
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        data = {}
    if status >= 400:
        err = data.get("error") if isinstance(data, dict) else None
        raise RelayError(f"relay answered {status}" + (f" ({str(err)[:80]})" if err else ""))
    if not isinstance(data, dict):
        raise RelayError("relay answered with something that is not a JSON object")
    return data


def enroll(api_base: str, code: str, ctx: Optional[ssl.SSLContext] = None) -> dict:
    """Exchange a one-time enrollment code for {handle, device_token, hostname}."""
    code = (code or "").strip()
    if not CODE_RE.match(code):
        raise RelayError("that does not look like an enrollment code (letters, digits, dashes)")
    data = _json_call("POST", check_url(api_base.rstrip("/") + "/api/enroll"),
                      {"code": code, "agent": AGENT, "protocol": 1}, ctx=ctx)
    handle, token = data.get("handle"), data.get("device_token")
    if not isinstance(handle, str) or not HANDLE_RE.match(handle):
        raise RelayError("relay returned an invalid handle")
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512 or not token.isprintable() \
            or any(c.isspace() for c in token):
        raise RelayError("relay returned an invalid device token")
    host = f"{handle}.{HOST_SUFFIX}"
    if data.get("hostname", host) != host:
        raise RelayError("relay returned a hostname that does not match the handle")
    return {"handle": handle, "device_token": token, "hostname": host}


def challenge_name(hostname: str) -> str:
    return f"_acme-challenge.{hostname}"


def dns_challenge(api_base: str, token: str, hostname: str, value: Optional[str],
                  ctx: Optional[ssl.SSLContext] = None) -> dict:
    """value set -> ask the relay to publish TXT _acme-challenge.<hostname> = value;
    value None -> ask it to remove that record."""
    body = {"action": "set" if value is not None else "clear", "name": challenge_name(hostname)}
    if value is not None:
        body["value"] = value
    return _json_call("POST", check_url(api_base.rstrip("/") + "/api/dns-challenge"), body,
                      token=token, ctx=ctx)
