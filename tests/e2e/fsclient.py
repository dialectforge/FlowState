"""Tiny MCP clients for the end-to-end test: raw JSON-RPC over stdio, and streamable HTTP.

Both talk to a real FlowState server process exactly like an AI app would (initialize with a
clientInfo name, tools/list, tools/call). Standard library + httpx (already a FlowState dep).
"""
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_VENV_PY = [REPO / ".venv" / "bin" / "python", REPO / ".venv" / "Scripts" / "python.exe"]
PY = next((str(p) for p in _VENV_PY if p.exists()), sys.executable)


GUARD_SITE = REPO / "tests" / "guard_site"     # sitecustomize: refuses to touch the real folders


def real_dirs(home: Path = None) -> list:
    """The real per-user FlowState folders (data, settings, token file, skills)."""
    h = Path(home or os.environ.get("FS_E2E_REAL_HOME") or Path.home())
    out = [h / ".flowstate", h / "Library" / "Application Support" / "flowstate",
           h / ".local" / "share" / "flowstate", h / ".config" / "flowstate",
           h / ".agents" / "skills" / "flowstate", h / ".claude" / "skills" / "flowstate"]
    for v in ("LOCALAPPDATA", "APPDATA"):
        if os.environ.get(v):
            out.append(Path(os.environ[v]) / "flowstate")
    return [str(p) for p in out]


def sandbox_env(root: Path, extra: dict = None) -> dict:
    """Environment that points every FlowState path into `root` (never the real ones), and
    loads the real-data guard in every server process (refusals go to <root>/guard.log)."""
    root = Path(root)
    for d in ("home", "data", "backups", "config", "relay"):
        (root / d).mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWSTATE_")}
    env.update({
        "HOME": str(root / "home"),                 # belt and braces: per-OS defaults land here
        "FLOWSTATE_HOME": str(root / "data"),
        "FLOWSTATE_DB": str(root / "data" / "flowstate.db"),
        "FLOWSTATE_BACKUP_DIR": str(root / "backups"),
        "FLOWSTATE_CONFIG_DIR": str(root / "config"),
        "FLOWSTATE_TOKEN_FILE": str(root / "config" / "http-token.txt"),
        "FLOWSTATE_AUTH_DB": str(root / "config" / "remote-auth.db"),
        "FLOWSTATE_RELAY_DIR": str(root / "relay"),
        "FLOWSTATE_TOKEN_STORE_FILE": str(root / "config" / "http-token"),
        "FLOWSTATE_WORKSPACE": "e2e-box",
        "FLOWSTATE_REVIEW_PORT": "0",               # the review page (waiting.py): never 8770 here
        "FLOWSTATE_GUARD_DIRS": os.pathsep.join(real_dirs()),
        "FLOWSTATE_GUARD_LOG": str(guard_log(root)),
        "PYTHONPATH": os.pathsep.join([str(GUARD_SITE), str(REPO / "mcp-server")]),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    gitconfig = root / "home" / ".gitconfig"     # a user's own git identity (data-git commits);
    if not gitconfig.exists():                    # macOS git guesses one, Git for Windows doesn't
        gitconfig.write_text("[user]\n\tname = FlowState e2e\n\temail = e2e@flowstate.invalid\n",
                             encoding="utf-8")
    if os.name == "nt":                             # Windows reads these, not HOME
        for d, v in (("home", "USERPROFILE"), ("home/AppData/Local", "LOCALAPPDATA"),
                     ("home/AppData/Roaming", "APPDATA")):
            (root / d).mkdir(parents=True, exist_ok=True)
            env[v] = str(root / d)
    if extra:
        env.update(extra)
    return env


def guard_log(root: Path) -> Path:
    """One refusals log per run, at the top of the run folder (nested sandboxes share it)."""
    if os.environ.get("FS_E2E_GUARD_LOG"):
        return Path(os.environ["FS_E2E_GUARD_LOG"])
    root = Path(root).resolve()
    top = next((p for p in [root, *root.parents] if p.name.startswith("fs-e2e-")), root)
    return top / "guard.log"


class ToolError(Exception):
    pass


def _parse_result(res: dict):
    """tools/call result -> python object (FlowState answers with one JSON text block)."""
    content = res.get("content") or []
    text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
    try:
        data = json.loads(text)
    except ValueError:
        data = text
    if isinstance(data, dict):
        data.setdefault("_is_error", bool(res.get("isError")))
    elif res.get("isError"):   # e.g. the MCP SDK's own "Input validation error: ..."
        data = {"error": "tool_error", "message": text, "_is_error": True}
    return data


class StdioClient:
    """One app connection = one server process (that's how stdio MCP works)."""

    def __init__(self, env: dict, app: str, version: str = "1.0", log_path: Path = None, args=(),
                 elicit=None, think: float = 1.2):
        """`elicit(params) -> result dict` makes this app declare form elicitation and answer
        FlowState's popups (an MCP server-to-client request), like Claude Code or Cursor.
        Each answer comes `think` seconds after the box, like a person (FlowState treats an
        answer within popup.HUMAN_SECONDS as a box no person saw); think=0 answers at once."""
        self.app = app
        self.elicit = elicit
        self.think = think
        self.popups = []
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._log = open(log_path, "ab") if log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen([PY, "-m", "flowstate", *args], env=env, cwd=str(REPO),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self._log)
        self.pid = self.proc.pid
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.init = self.request("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"elicitation": {}} if elicit else {},
            "clientInfo": {"name": app, "version": version}})
        self.notify("notifications/initialized", {})

    def _read(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "method" in msg and "id" in msg:      # a request from the server (a popup)
                threading.Thread(target=self._answer, args=(msg,), daemon=True).start()
            elif "id" in msg and msg["id"] in self._pending:
                self._pending[msg["id"]].put(msg)

    def _answer(self, msg):
        if msg["method"] == "elicitation/create" and self.elicit:
            self.popups.append(msg["params"])
            time.sleep(self.think)
            result = self.elicit(msg["params"])
            if result is None:                       # never answers (the user walked away)
                return
            self._send({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        else:
            self._send({"jsonrpc": "2.0", "id": msg["id"],
                        "error": {"code": -32601, "message": "not supported"}})

    def _send(self, msg):
        data = (json.dumps(msg) + "\n").encode()
        with self._lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def notify(self, method, params):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=60):
        i = next(self._ids)
        q = self._pending[i] = queue.Queue()
        self._send({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"{method} got no answer in {timeout}s")
        finally:
            self._pending.pop(i, None)
        if "error" in msg:
            raise ToolError(msg["error"])
        return msg["result"]

    def tools(self):
        return self.request("tools/list", {})["tools"]

    def call(self, _tool, **arguments):
        return _parse_result(self.request("tools/call", {"name": _tool, "arguments": arguments}))

    def close(self):
        """Close stdin (the app quitting), then wait; kill only this exact PID if it hangs."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if hasattr(self._log, "close"):
            self._log.close()


class HttpClient:
    """One MCP session on the streamable-HTTP transport."""

    def __init__(self, url: str, token: str, app: str, version: str = "1.0", headers=None,
                 capabilities=None):
        """`capabilities` (e.g. {"elicitation": {}}) is what this app declares; requests the
        server sends it on a response stream are kept in `server_requests` (never answered)."""
        import httpx
        self.app, self.url = app, url
        self.server_requests = []
        self._ids = itertools.count(1)
        self._h = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream",
                   "Content-Type": "application/json", **(headers or {})}
        self._c = httpx.Client(timeout=60)
        self.init = self.request("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": capabilities or {},
            "clientInfo": {"name": app, "version": version}})
        self.notify("notifications/initialized", {})

    def _post(self, msg):
        r = self._c.post(self.url, headers=self._h, content=json.dumps(msg))
        sid = r.headers.get("mcp-session-id")
        if sid:
            self._h["mcp-session-id"] = sid
        if r.headers.get("mcp-protocol-version"):
            self._h["mcp-protocol-version"] = r.headers["mcp-protocol-version"]
        return r

    def notify(self, method, params):
        self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method, params, timeout=60):
        i = next(self._ids)
        r = self._post({"jsonrpc": "2.0", "id": i, "method": method, "params": params})
        if r.status_code >= 400:
            raise ToolError({"status": r.status_code, "body": r.text[:300]})
        body = r.text
        msg = None
        if "text/event-stream" in r.headers.get("content-type", ""):
            for line in body.splitlines():
                if line.startswith("data:"):
                    m = json.loads(line[5:].strip())
                    if "method" in m and "id" in m:
                        self.server_requests.append(m)
                    elif m.get("id") == i:
                        msg = m
        else:
            msg = json.loads(body)
        if msg is None:
            raise ToolError({"status": r.status_code, "body": body[:300]})
        if "error" in msg:
            raise ToolError(msg["error"])
        return msg["result"]

    def tools(self):
        return self.request("tools/list", {})["tools"]

    def call(self, _tool, **arguments):
        return _parse_result(self.request("tools/call", {"name": _tool, "arguments": arguments}))

    def close(self):
        try:
            self._c.delete(self.url, headers=self._h)
        except Exception:
            pass
        self._c.close()


def wait_port(host, port, timeout=30):
    import socket
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False
