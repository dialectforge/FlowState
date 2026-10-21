"""Relay state on disk. Standard library only.

Folder: FLOWSTATE_RELAY_DIR, else <FlowState data folder>/relay (Windows
%LOCALAPPDATA%\\flowstate\\relay, macOS ~/Library/Application Support/flowstate/relay,
Linux ~/.local/share/flowstate/relay). Created 0700; every file is written 0600 (on Windows
the per-user AppData ACL is what protects it). Refused inside a git work tree.

    relay.json            settings, no secrets: handle, hostname, api, tunnel, staging, enabled
    device-token          the relay device token (secret)
    tls-key.pem           ECDSA P-256 key for <handle>.fsmcp.com (secret, never sent)
    acme-account-key.pem  ECDSA P-256 Let's Encrypt account key (secret, never sent)
    acme-account.json     the ACME account URL per directory
    cert.pem              the certificate chain (public)
    status.json           written by the running agent: tunnel state, heartbeat time
    ct.json               Certificate Transparency results and alerts
"""

import json
import os
import time
from pathlib import Path
from typing import Optional

from .. import paths
from . import API_BASE, HOST_SUFFIX, TUNNEL_HOST, TUNNEL_PORT, RelayError

CONFIG = "relay.json"
TOKEN = "device-token"
TLS_KEY = "tls-key.pem"
ACCOUNT_KEY = "acme-account-key.pem"
ACCOUNT = "acme-account.json"
CERT = "cert.pem"
STATUS = "status.json"
CT = "ct.json"
ALL_FILES = (CONFIG, TOKEN, TLS_KEY, ACCOUNT_KEY, ACCOUNT, CERT, STATUS, CT)

# the running agent rewrites status.json at least this often; older means "not running"
STATUS_FRESH_SECONDS = 90


def relay_dir() -> Path:
    env = os.environ.get("FLOWSTATE_RELAY_DIR")
    return Path(env).expanduser() if env else paths.data_root() / "relay"


def _write_private(p: Path, data: bytes) -> None:
    """Write `data` to `p` as an owner-only file, atomically."""
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


class RelayStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else relay_dir()

    # ------------------------------------------------------------ files
    def path(self, name: str) -> Path:
        return self.root / name

    def ensure_dir(self) -> None:
        repo = paths.inside_git_worktree(self.root)
        if repo:
            raise RelayError(f"refusing to keep relay keys inside a git folder ({repo}); "
                             "set FLOWSTATE_RELAY_DIR to a private folder")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def write_secret(self, name: str, data: bytes) -> None:
        self.ensure_dir()
        _write_private(self.path(name), data)

    def read_bytes(self, name: str) -> Optional[bytes]:
        try:
            return self.path(name).read_bytes()
        except FileNotFoundError:
            return None

    def _read_json(self, name: str) -> dict:
        raw = self.read_bytes(name)
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_json(self, name: str, data: dict) -> None:
        self.write_secret(name, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())

    # ------------------------------------------------------------ settings
    def config(self) -> dict:
        return self._read_json(CONFIG)

    def save_config(self, cfg: dict) -> None:
        for k in ("token", "device_token"):  # never in the settings file
            cfg.pop(k, None)
        self._write_json(CONFIG, cfg)

    def enrolled(self) -> bool:
        return bool(self.config().get("handle")) and self.read_bytes(TOKEN) is not None

    def token(self) -> str:
        raw = self.read_bytes(TOKEN)
        if not raw:
            raise RelayError("not enrolled (no device token); run: flowstate relay enroll --code <code>")
        return raw.decode().strip()

    def save_enrollment(self, handle: str, token: str, *, api: str = API_BASE,
                        tunnel_host: str = TUNNEL_HOST, tunnel_port: int = TUNNEL_PORT,
                        tunnel_sni: str = TUNNEL_HOST, staging: bool = False,
                        ca_file: Optional[str] = None) -> dict:
        cfg = {"version": 1, "handle": handle, "hostname": f"{handle}.{HOST_SUFFIX}",
               "api": api.rstrip("/"), "tunnel_host": tunnel_host, "tunnel_port": int(tunnel_port),
               "tunnel_sni": tunnel_sni, "staging": bool(staging), "enabled": True,
               "enrolled_at": int(time.time()), "known_spki": []}
        if ca_file:
            cfg["ca_file"] = str(ca_file)
        self.write_secret(TOKEN, token.encode() + b"\n")
        self.save_config(cfg)
        return cfg

    def set_enabled(self, on: bool) -> dict:
        cfg = self.config()
        if not cfg.get("handle"):
            raise RelayError("the relay is not set up (flowstate relay enroll --code <code>)")
        cfg["enabled"] = bool(on)
        self.save_config(cfg)
        return cfg

    def forget(self) -> list:
        """Delete token, keys, certificate and state. Returns the names removed."""
        gone = []
        for name in ALL_FILES:
            for p in (self.path(name), self.path(name + ".tmp")):
                if p.exists():
                    p.unlink()
                    gone.append(p.name)
        try:
            self.root.rmdir()
        except OSError:
            pass
        return gone

    # ------------------------------------------------------------ agent status / CT
    def status(self) -> dict:
        return self._read_json(STATUS)

    def save_status(self, st: dict) -> None:
        st = dict(st, pid=os.getpid(), updated=int(time.time()))
        self._write_json(STATUS, st)

    def ct(self) -> dict:
        return self._read_json(CT)

    def save_ct(self, data: dict) -> None:
        self._write_json(CT, data)

    def ca_file(self) -> Optional[str]:
        return os.environ.get("FLOWSTATE_RELAY_CA_FILE") or self.config().get("ca_file")


def active_alerts(ct: dict) -> list:
    return [a for a in ct.get("alerts") or [] if not a.get("acknowledged")]


def tool_warning(store: Optional[RelayStore] = None) -> Optional[str]:
    """One loud line for tool output (start_here) when the CT watch found a certificate for
    this computer's relay hostname that was NOT made with the local key. None otherwise
    (and always None when the relay was never set up). Never raises."""
    try:
        store = store or RelayStore()
        if not store.path(CT).exists():
            return None
        alerts = active_alerts(store.ct())
    except Exception:  # noqa: BLE001 - a status line must never break a tool
        return None
    if not alerts:
        return None
    host = store.config().get("hostname", "your relay hostname")
    ids = ", ".join(str(a.get("crtsh_id")) for a in alerts[:5])
    return (f"!! FLOWSTATE RELAY SECURITY WARNING: {len(alerts)} certificate(s) for {host} were "
            f"issued with a key that is NOT this computer's (crt.sh id {ids}). Someone may be "
            f"able to read relay traffic. Tell the user now; they should run "
            f"`flowstate relay disable` and `flowstate relay status`.")
