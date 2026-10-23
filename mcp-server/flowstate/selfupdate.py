"""`flowstate update` (2.4.0): move this install to the newest signed release, only when asked.

    flowstate update --dry-run                      check everything, change nothing, show the plan
    flowstate update                                do it; asks for "y" in your own terminal
    flowstate update --yes --user-said "<words>"    do it; an agent passes the user's own words
    flowstate update --rollback [--yes --user-said ...]   back to the version before the last update

Steps: (1) preflight: a plain git clone of github.com/dialectforge/FlowState, clean, at a
release tag (or on main with nothing local), one worktree, a real `.venv` folder, not opted out
(config.json "self_update": false or a `.flowstate-no-self-update` file), no other update
running, git 2.34+ and ssh-keygen present; (2) the newest release, fetched fresh, strictly
newer; (3) fetch only that tag and verify its SSH signature against `release_signers` READ
FROM THIS COPY before anything changes (an empty list, an unsigned tag, an unknown key or any
verify error: nothing is changed); (4) a `pre-update` backup of the database; (5) stop this
folder's services; (6) git to the tag (a fresh `.venv` only when the dependencies changed);
(7) migrate with the new code; (8) start the services and check them. Any failure from (5) on
rolls back on its own: git back to the exact old branch and commit, the old `.venv` back, the
old services started. The database is never put back or overwritten automatically (migrations
only add, so the old code reads it); if it fails its integrity check, the exact
`flowstate restore` command is printed.

Every OS call is behind a small class (Git, Services, Processes, Pip, Clock, Health, Migrator)
so the tests use fakes; nothing here may run against a real install from a test.
Standard library only.
"""

import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import paths, updates

ORIGIN_URLS = ("https://github.com/dialectforge/FlowState", "https://github.com/dialectforge/FlowState.git")
MIN_GIT = (2, 34)
SIGNERS = "mcp-server/flowstate/release_signers"
INIT_PY = "mcp-server/flowstate/__init__.py"
PYPROJECT = "mcp-server/pyproject.toml"
OPT_OUT_FILE = ".flowstate-no-self-update"
LOCK_NAME = "update.lock"
LOCK_STALE_SECONDS = 2 * 3600
TEMP_REF = "refs/flowstate-update/"
STOP_WAIT_SECONDS = 15
FOLDER = Path(__file__).resolve().parents[2]
NEEDS_WORDS = "--yes needs --user-said \"<the user's own words asking for this update>\""


class Refused(Exception):
    """Nothing was changed."""

    def __init__(self, *reasons: str, dev_setup: bool = False):
        super().__init__("; ".join(reasons))
        self.reasons, self.dev_setup = list(reasons), dev_setup


class StepFailed(Exception):
    def __init__(self, step: str, detail: str):
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


def manual_steps(folder: Path) -> str:
    return (f"To update by hand: in {folder} run `git pull`, then `python setup_flowstate.py "
            f"--clients <your apps> --verify` (the same options as when you set it up), then quit "
            f"and reopen your AI apps.")


def _norm(p) -> str:
    s = str(p).replace("\\", "/").rstrip("/")
    return s.lower() if os.name == "nt" else s


def mentions(text: str, folder: Path) -> bool:
    """Does `text` (a task action, plist or unit file) name this folder, and not merely a
    folder whose name starts the same (FlowState-2.4 vs FlowState-2.4-dev)?"""
    t, f = _norm(text), _norm(folder)
    i = t.find(f)
    while i >= 0:
        nxt = t[i + len(f): i + len(f) + 1]
        if not nxt or not (nxt.isalnum() or nxt in "._-"):
            return True
        i = t.find(f, i + 1)
    return False


def is_real_dir(p: Path) -> bool:
    """A plain folder: not a symlink, not a Windows junction or other reparse point."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return False
    return not (getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _uid() -> int:
    return getattr(os, "getuid", lambda: 0)()


def venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


# ---------------------------------------------------------------- OS interfaces (fakes in tests)
class Git:
    def __init__(self, folder: Path, exe: str = "git"):
        self.folder, self.exe = Path(folder), exe

    def run(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C", LANG="C")
        try:
            return subprocess.run([self.exe, *args], cwd=self.folder, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", env=env, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return subprocess.CompletedProcess([self.exe, *args], 127, "", f"{type(e).__name__}: {e}")

    def out(self, *args: str) -> Optional[str]:
        r = self.run(*args)
        return r.stdout.strip() if r.returncode == 0 else None

    def version(self) -> Optional[tuple]:
        m = re.search(r"(\d+)\.(\d+)", self.out("--version") or "")
        return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass
class Service:
    name: str            # task path+name / plist path / unit name
    kind: str            # task | launchd | systemd
    text: str            # what it runs (action / plist / unit file text)
    running: bool = True

    @property
    def role(self) -> str:
        t = self.text.lower()
        if "relay" in t:
            return "relay"
        if "http" in t or "--transport" in t:
            return "http"
        return "backup" if "backup" in t else "other"


class Services:
    """This OS's FlowState services for one folder: Windows Task Scheduler tasks, macOS
    LaunchAgents, Linux systemd user units. Only ones that mention the folder are touched."""

    def __init__(self, system: Optional[str] = None, run: Callable = subprocess.run,
                 home: Optional[Path] = None):
        import platform
        self.system, self._run, self.home = system or platform.system(), run, Path(home or Path.home())

    def _cmd(self, args: list, timeout: float = 60) -> subprocess.CompletedProcess:
        return self._run(args, capture_output=True, text=True, timeout=timeout)

    def find(self, folder: Path) -> list:
        out = []
        if self.system == "Windows":
            ps = ("Get-ScheduledTask | ForEach-Object { [pscustomobject]@{ n = $_.TaskPath + $_.TaskName; "
                  "s = [string]$_.State; a = (($_.Actions | ForEach-Object { \"$($_.Execute) $($_.Arguments) "
                  "$($_.WorkingDirectory)\" }) -join ' | ') } } | ConvertTo-Json -Compress")
            r = self._cmd(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
            rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
            for t in rows if isinstance(rows, list) else [rows]:
                if mentions(t.get("a") or "", folder):
                    out.append(Service(t["n"], "task", t.get("a") or "", t.get("s") == "Running"))
        elif self.system == "Darwin":
            for p in sorted((self.home / "Library" / "LaunchAgents").glob("*.plist")):
                text = p.read_text(encoding="utf-8", errors="replace")
                if mentions(text, folder):
                    label = re.search(r"<key>Label</key>\s*<string>([^<]+)</string>", text)
                    r = self._cmd(["launchctl", "print", f"gui/{_uid()}/{label.group(1) if label else p.stem}"])
                    out.append(Service(str(p), "launchd", text, r.returncode == 0))
        else:
            for p in sorted((self.home / ".config" / "systemd" / "user").glob("*.service")):
                text = p.read_text(encoding="utf-8", errors="replace")
                if mentions(text, folder):
                    r = self._cmd(["systemctl", "--user", "is-active", p.name])
                    out.append(Service(p.name, "systemd", text, r.stdout.strip() == "active"))
        return out

    def stop(self, s: Service) -> None:
        if s.kind == "task":
            r = self._cmd(["schtasks", "/End", "/TN", s.name])
        elif s.kind == "launchd":
            r = self._cmd(["launchctl", "bootout", f"gui/{_uid()}", s.name])
        else:
            r = self._cmd(["systemctl", "--user", "stop", s.name])
        if r.returncode != 0:
            raise StepFailed("stopping services", f"{s.name}: {(r.stderr or r.stdout).strip()[:200]}")

    def start(self, s: Service) -> None:
        if s.kind == "task":
            r = self._cmd(["schtasks", "/Run", "/TN", s.name])
        elif s.kind == "launchd":
            r = self._cmd(["launchctl", "bootstrap", f"gui/{_uid()}", s.name])
        else:
            r = self._cmd(["systemctl", "--user", "start", s.name])
        if r.returncode != 0:
            raise StepFailed("starting services", f"{s.name}: {(r.stderr or r.stdout).strip()[:200]}")


class Processes:
    """Running processes: [{"pid", "exe", "cmd"}]; kill(pid) stops one (and its children)."""

    def list(self) -> list:
        if os.name == "nt":
            ps = ("Get-CimInstance Win32_Process | Select-Object ProcessId,ExecutablePath,CommandLine "
                  "| ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=60)
            rows = json.loads(r.stdout or "[]") if r.returncode == 0 else []
            return [{"pid": int(x["ProcessId"]), "exe": x.get("ExecutablePath") or "",
                     "cmd": x.get("CommandLine") or ""} for x in (rows if isinstance(rows, list) else [rows])]
        r = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=60)
        out = []
        for line in r.stdout.splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if pid.isdigit():
                out.append({"pid": int(pid), "exe": cmd.split(" ", 1)[0], "cmd": cmd})
        return out

    def kill(self, pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
        else:
            import signal
            os.kill(pid, signal.SIGTERM)


class Pip:
    def create_venv(self, venv: Path) -> None:
        base = getattr(sys, "_base_executable", None) or sys.executable
        r = subprocess.run([base, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise StepFailed("making a new .venv", (r.stderr or r.stdout).strip()[-300:])

    def install(self, py: Path, folder: Path) -> None:
        r = subprocess.run([str(py), "-m", "pip", "install", "-e", str(folder / "mcp-server")],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise StepFailed("installing packages", (r.stderr or r.stdout).strip()[-300:])


class Clock:
    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)


def _code_env(folder: Path, db: Optional[Path]) -> dict:
    env = dict(os.environ, PYTHONPATH=str(folder / "mcp-server"))
    if db:
        env["FLOWSTATE_DB"] = str(db)
    return env


class Migrator:
    def migrate(self, py: Path, folder: Path, db: Path) -> None:
        r = subprocess.run([str(py), "-m", "flowstate", "migrate", "--db", str(db)], cwd=folder,
                           env=_code_env(folder, db), capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            raise StepFailed("migrating the database", (r.stdout + r.stderr).strip()[-300:])


class Health:
    """After a swap (or a rollback): the code reports the version; a stdio start_here answers;
    the HTTP server's /health answers if one runs; the relay tunnel connects if one runs."""

    def __init__(self, clock: Optional[Clock] = None):
        self.clock = clock or Clock()

    def check(self, py: Path, folder: Path, db: Optional[Path], version: str, services: list) -> list:
        problems = []
        r = subprocess.run([str(py), "-c", "import flowstate; print(flowstate.__version__)"], cwd=folder,
                           env=_code_env(folder, db), capture_output=True, text=True, timeout=120)
        got = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
        if got != version:
            problems.append(f"the code reports {got or (r.stderr.strip()[-120:] or 'nothing')}, not {version}")
            return problems
        ok, detail = self._start_here(py, folder, db)
        if not ok:
            problems.append(f"start_here over stdio failed ({detail})")
        for s in services:
            if s.role == "http" and s.running:
                problems += self._http(s)
            if s.role == "relay" and s.running:
                problems += self._relay(py, folder, db)
        return problems

    def _start_here(self, py: Path, folder: Path, db: Optional[Path]) -> tuple:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_flowstate_setup_verify", folder / "setup_flowstate.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        env = {"PYTHONPATH": str(folder / "mcp-server"), "FLOWSTATE_UPDATE_CHECK": "0"}
        if db:
            env["FLOWSTATE_DB"] = str(db)
        return mod.verify({"command": str(py), "args": ["-m", "flowstate"], "env": env})

    def _http(self, s: Service) -> list:
        words = shlex.split(s.text.replace("\\", "/"), posix=True) if s.text else []
        host = next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "--host"), "127.0.0.1")
        port = next((words[i + 1] for i, w in enumerate(words[:-1]) if w == "--port"), "8766")
        host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
        url = f"http://{host}:{port}/health"
        end = self.clock.now() + 30
        while True:
            try:
                with urllib.request.urlopen(url, timeout=5) as r:
                    if r.status == 200 and json.loads(r.read(4096) or b"{}").get("ok") is True:
                        return []
            except Exception:  # noqa: BLE001 - keep trying until the deadline
                pass
            if self.clock.now() > end:
                return [f"the HTTP server ({s.name}) did not answer {url} within 30 s"]
            self.clock.sleep(2)

    def _relay(self, py: Path, folder: Path, db: Optional[Path]) -> list:
        end = self.clock.now() + 60
        while True:
            r = subprocess.run([str(py), "-m", "flowstate", "relay", "status"], cwd=folder,
                               env=_code_env(folder, db), capture_output=True, text=True, timeout=60)
            if "Tunnel: connected" in r.stdout:
                return []
            if self.clock.now() > end:
                return ["the relay tunnel did not connect within 60 s"]
            self.clock.sleep(3)


# ---------------------------------------------------------------- the updater
@dataclass
class Plan:
    mode: str = ""                  # "tag" (detached at a release tag) or "main"
    branch: Optional[str] = None
    sha: str = ""
    target: dict = field(default_factory=dict)
    ref: str = ""                   # where the verified tag is (temporary ref, then refs/tags/vX.Y.Z)
    oid: str = ""                   # the verified tag object
    signer: str = ""
    deps_changed: bool = False
    services: list = field(default_factory=list)
    apps: list = field(default_factory=list)


class Updater:
    def __init__(self, folder: Optional[Path] = None, db: Optional[Path] = None, *, git: Optional[Git] = None,
                 services=None, processes=None, pip=None, clock=None, health=None, migrator=None,
                 fetch=None, expected_origin=ORIGIN_URLS, python: Optional[Path] = None,
                 running_version: Optional[str] = None, state_path: Optional[Path] = None,
                 rename: Callable = os.rename, ssh_keygen: Optional[str] = None, say: Callable = print):
        from . import __version__
        self.folder = Path(folder or FOLDER).resolve()
        if db is None:
            from .database import default_db_path
            db = default_db_path()
        self.db = Path(db)
        self.git = git or Git(self.folder)
        self.services = services or Services()
        self.processes = processes or Processes()
        self.pip, self.clock = pip or Pip(), clock or Clock()
        self.health, self.migrator = health or Health(self.clock), migrator or Migrator()
        self.fetch, self.expected = fetch, tuple(expected_origin)
        self.venv = self.folder / ".venv"
        self.python = Path(python) if python else venv_python(self.venv)
        self.running = running_version or __version__
        self.state_path = Path(state_path) if state_path else updates.state_file()
        self.rename = rename
        self.ssh_keygen = ssh_keygen if ssh_keygen is not None else shutil.which("ssh-keygen")
        self.say = say

    # ------------------------------------------------------------ preflight
    def preflight(self, for_rollback: bool = False) -> Plan:
        dev, why = False, []
        gv = self.git.version()
        if gv is None:
            raise Refused("git was not found")
        if gv < MIN_GIT:
            why.append(f"git {gv[0]}.{gv[1]} is too old (2.34 or newer checks release signatures)")
        if not self.ssh_keygen:
            why.append("ssh-keygen was not found, so release signatures can't be checked")
        top = self.git.out("rev-parse", "--show-toplevel")
        if not top or _norm(Path(top).resolve()) != _norm(self.folder):
            raise Refused(f"{self.folder} is not the top of a git clone")
        origin = self.git.out("remote", "get-url", "origin") or ""
        if origin not in self.expected:
            why.append(f"origin is {origin or 'not set'}, not {self.expected[0]}")
        if self.git.out("status", "--porcelain", "--untracked-files=normal") != "":
            why.append("the folder has local changes (git status is not clean)")
        trees = [l for l in (self.git.out("worktree", "list", "--porcelain") or "").splitlines()
                 if l.startswith("worktree ")]
        if len(trees) != 1:
            why.append(f"this clone has {len(trees)} worktrees (a developer setup)")
            dev = True
        if not os.path.lexists(self.venv):
            why.append("there is no .venv folder here (setup_flowstate.py makes it)")
        elif not is_real_dir(self.venv):
            why.append(".venv is a link or junction to another folder, not a folder of its own")
            dev = True
        if not updates.self_update_allowed():
            why.append('config.json says "self_update": false (this computer is updated by hand)')
        if (self.folder / OPT_OUT_FILE).exists():
            why.append(f"{OPT_OUT_FILE} is present (this folder is updated by hand)")
        lock = self.state_path.with_name(LOCK_NAME)
        if lock.exists() and self.clock.now() - lock.stat().st_mtime < LOCK_STALE_SECONDS:
            why.append("another update is running (update.lock)")
        plan = Plan(sha=self.git.out("rev-parse", "HEAD") or "")
        branch = self.git.out("symbolic-ref", "--quiet", "--short", "HEAD")
        plan.branch = branch or None
        tags = [t for t in (self.git.out("tag", "--points-at", "HEAD") or "").split() if updates.TAG_RE.fullmatch(t)]
        if not for_rollback:
            if branch == "main":
                plan.mode = "main"
                ahead = self.git.out("rev-list", "--count", "origin/main..HEAD")
                if ahead is None:
                    why.append("there is no origin/main to compare main with")
                elif ahead != "0":
                    why.append(f"main has {ahead} local commit(s) not on origin/main")
                    dev = True
            elif tags:
                plan.mode = "tag"
            else:
                why.append(f"HEAD is {'on branch ' + branch if branch else 'detached'}, not at a release "
                           f"tag or on main")
                dev = dev or bool(branch)
        if why:
            raise Refused(*why, dev_setup=dev)
        return plan

    # ------------------------------------------------------------ target and signature
    def pick_target(self) -> dict:
        res = updates.fetch_latest(self.fetch)
        if not res["ok"]:
            raise Refused(f"can't reach GitHub to find the newest release ({res['error']})")
        if res.get("not_modified"):
            raise Refused("GitHub gave no release details")
        return res

    def read_signers(self) -> str:
        """release_signers from THIS copy (the one already trusted), read before anything changes."""
        try:
            text = (self.folder / SIGNERS).read_text(encoding="utf-8")
        except OSError:
            text = ""
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]
        return "\n".join(lines) + "\n" if lines else ""

    def fetch_and_verify(self, plan: Plan, signers: str) -> None:
        """Download only the release tag, into a temporary ref (refs/flowstate-update/...), and
        check it. A refusal leaves the clone's own tags and branches as they were."""
        tag = plan.target["tag"]
        ver = plan.target["latest"]
        cant = f"FlowState can't verify release {tag}"
        if not signers:
            raise Refused(f"{cant}: no release signing key is set up yet (release_signers in this copy "
                          f"lists none). Release tags must be signed")
        ref = TEMP_REF + tag
        plan.ref = ref
        r = self.git.run("fetch", "--no-tags", "origin", f"+refs/tags/{tag}:{ref}")
        if r.returncode != 0:
            raise Refused(f"could not download release {tag} ({_last(r.stderr)})")
        plan.oid = self.git.out("rev-parse", ref) or ""
        mine = self.git.out("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}")
        if mine and mine != plan.oid:
            raise Refused(f"this clone already has a different tag {tag} than GitHub's")
        obj_type = self.git.out("cat-file", "-t", ref)
        if obj_type != "tag":
            raise Refused(f"{cant}: its tag isn't signed with a FlowState release key (it is a plain tag). "
                          f"Release tags must be signed")
        raw = self.git.out("cat-file", "tag", ref) or ""
        head = raw.split("\n\n", 1)[0].splitlines()
        if f"tag {tag}" not in head or "type commit" not in head:
            raise Refused(f"{cant}: the tag object names {next((h for h in head if h.startswith('tag ')), '?')}")
        if "-----BEGIN SSH SIGNATURE-----" not in raw:
            raise Refused(f"{cant}: its tag isn't signed with a FlowState release key. Release tags must be signed")
        tmp = Path(tempfile.mkdtemp(prefix="fs-signers-"))
        try:
            allowed = tmp / "allowed_signers"
            allowed.write_text(signers, encoding="utf-8", newline="\n")
            v = self.git.run("-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed.as_posix()}",
                             "-c", f"gpg.ssh.program={Path(self.ssh_keygen).as_posix()}",
                             "-c", "gpg.ssh.revocationFile=", "verify-tag", ref)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        text = (v.stdout or "") + (v.stderr or "")
        good = re.search(r'Good "git" signature for (\S+) with', text)
        if v.returncode != 0 or not good:
            known = "No principal matched" in text or "no principal" in text.lower()
            raise Refused(f"{cant}: its tag isn't signed with a FlowState release key"
                          + (" (it is signed, but by a key this copy's release_signers doesn't list)"
                             if known else f" (git verify-tag: {_last(text)})")
                          + ". Release tags must be signed")
        plan.signer = good.group(1)
        commit = f"{ref}^{{commit}}"
        init_py = self.git.out("show", f"{commit}:{INIT_PY}") or ""
        pyproject = self.git.out("show", f"{commit}:{PYPROJECT}") or ""
        if f'__version__ = "{ver}"' not in init_py or not re.search(rf'(?m)^version = "{re.escape(ver)}"$', pyproject):
            raise Refused(f"release {tag} does not say version {ver} in both __init__.py and pyproject.toml")
        if plan.mode == "main" and self.git.run("merge-base", "--is-ancestor", "HEAD", commit).returncode != 0:
            raise Refused(f"release {tag} is not a fast-forward of main here")
        plan.deps_changed = _deps(self.git.out("show", f"HEAD:{PYPROJECT}") or "") != _deps(pyproject)

    def _runs_from_this_venv(self) -> bool:
        """Windows: a venv's python.exe is a launcher that keeps running (holding files in the
        .venv) while this command runs, so the .venv can't be renamed or replaced from inside."""
        if os.name != "nt":
            return False
        return mentions(os.path.realpath(sys.prefix), Path(os.path.realpath(self.venv)))

    def _drop_temp_ref(self, plan: Plan) -> None:
        if plan.ref.startswith(TEMP_REF):
            self.git.run("update-ref", "-d", plan.ref)

    def _keep_tag(self, plan: Plan) -> None:
        """After the yes: the verified tag object becomes refs/tags/vX.Y.Z."""
        tag_ref = f"refs/tags/{plan.target['tag']}"
        if self.git.out("rev-parse", "--verify", "--quiet", tag_ref) != plan.oid:
            r = self.git.run("update-ref", tag_ref, plan.oid, "")
            if r.returncode != 0:
                raise Refused(f"could not keep the tag {plan.target['tag']} ({_last(r.stderr)})")
        self._drop_temp_ref(plan)
        plan.ref = tag_ref

    # ------------------------------------------------------------ processes
    def _venv_procs(self) -> list:
        v = self.venv
        return [p for p in self.processes.list() if p["pid"] != os.getpid()
                and (mentions(p.get("exe") or "", v) or mentions(p.get("cmd") or "", v))]

    @staticmethod
    def _is_app(p: dict) -> bool:
        """An AI app's own stdio FlowState (`python -m flowstate` with nothing after it)."""
        cmd = re.sub(r"\s+", " ", p.get("cmd") or "").strip().strip('"')
        return bool(re.search(r"-m flowstate$", cmd))

    def _leftovers(self) -> list:
        return [p for p in self._venv_procs() if not self._is_app(p)]

    def stop_services(self, plan: Plan) -> None:
        for s in plan.services:
            if s.running:
                self.services.stop(s)
        end = self.clock.now() + STOP_WAIT_SECONDS
        while self._leftovers() and self.clock.now() < end:
            self.clock.sleep(1)
        for p in self._leftovers():         # schtasks /End can leave the python child of cmd /c
            self.say(f"  stopping leftover FlowState process {p['pid']}")
            self.processes.kill(p["pid"])
        if self._leftovers():
            raise StepFailed("stopping services", "FlowState processes from this folder are still running: "
                             + ", ".join(str(p["pid"]) for p in self._leftovers()))

    def start_services(self, plan: Plan) -> None:
        for s in plan.services:
            if s.running:
                self.services.start(s)

    # ------------------------------------------------------------ state, lock, history
    def _save(self, **kw) -> None:
        try:
            updates.write_state({"last_update": kw}, self.state_path)
        except OSError as e:
            self.say(f"(could not save update.json: {e})")

    def _lock(self) -> Path:
        lock = self.state_path.with_name(LOCK_NAME)
        lock.parent.mkdir(parents=True, exist_ok=True)
        if lock.exists() and self.clock.now() - lock.stat().st_mtime >= LOCK_STALE_SECONDS:
            lock.unlink()
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise Refused("another update is running (update.lock)") from None
        os.write(fd, json.dumps({"pid": os.getpid(), "folder": str(self.folder)}).encode())
        os.close(fd)
        return lock

    def _history(self, app: str, words: str, old: dict, new: dict, tool: str) -> None:
        """One row in the edit history ("Who did what"); it can't be undone from there."""
        if not self.db.is_file():
            return
        from . import history, identity
        try:
            conn = sqlite3.connect(str(self.db), timeout=30)
            try:
                if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='record_history'").fetchone():
                    return
                identity.insert(conn, "record_history", {
                    "edit_id": history.new_edit_id(), "table_name": "flowstate_install", "record_id": 0,
                    "project_id": None, "tool": tool, "old_row": json.dumps(old),
                    "new_row": json.dumps({**new, "user_said": words[:500]}), "client": app[:80],
                    "created_at": history._now()}, app, ident=identity.local(app))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            self.say(f"(could not add the update to the edit history: {e})")

    # ------------------------------------------------------------ confirm
    def confirm(self, question: str, yes: bool, user_said: Optional[str]) -> str:
        if yes:
            if not (user_said or "").strip():
                raise Refused(NEEDS_WORDS)
            return user_said.strip()
        from .cli_review import open_terminal
        tty = open_terminal()
        if tty is None:
            raise Refused("this asks you to confirm in a terminal, and there is none here (piped input "
                          "doesn't count). Run it yourself in a terminal window, or have your AI run "
                          'it with --yes --user-said "<your words>"')
        try:
            ans = tty.ask(question + " Type y to go ahead: ")
        finally:
            tty.close()
        if ans.strip().lower() not in ("y", "yes"):
            raise Refused("you didn't confirm")
        return "The user confirmed this in their own terminal (flowstate update)."

    # ------------------------------------------------------------ the run
    def _describe(self, plan: Plan) -> list:
        t = plan.target
        where = f"on main ({plan.sha[:8]})" if plan.mode == "main" else f"at {plan.sha[:8]} (a release tag)"
        how = (f"git merge --ff-only {t['tag']}" if plan.mode == "main" else f"git checkout --detach {t['tag']}")
        run = [s for s in plan.services if s.running]
        idle = [s for s in plan.services if not s.running]
        lines = [f"- This folder: {self.folder} (a clean clone of {self.expected[0]}, {where}).",
                 f"- Now: FlowState {self.running}. Newest release: {t['latest']} ({t.get('url')}).",
                 f"- Signature: tag {t['tag']} is signed by {plan.signer} (a key in this copy's release_signers).",
                 f"- Database: {self.db} " + ("(backed up first, labelled pre-update; then migrated)."
                                              if self.db.is_file() else "(not there yet: no backup, no migration)."),
                 "- Services for this folder: " + (", ".join(f"{s.name} (stopped, then started again)" for s in run)
                                                   or "none running") + "."]
        if idle:
            lines.append("- Not running, left alone: " + ", ".join(s.name for s in idle) + ".")
        lines.append("- AI apps using this folder's FlowState (not stopped; restart them afterwards): "
                     + (", ".join(f"pid {p['pid']}" for p in plan.apps) or "none") + ".")
        lines.append(f"- Code: {how}; " + ("the dependencies changed: a new .venv is made (the old one is "
                                           "kept as .venv.pre-update; every AI app must be quit first)."
                                           if plan.deps_changed else "the dependencies are the same (no pip)."))
        lines.append("- Then: start the services again and check them. Any failure puts the old version back.")
        return lines

    def run(self, dry_run: bool = False, yes: bool = False, user_said: Optional[str] = None,
            app: str = "command-line") -> int:
        plan = Plan()
        try:
            return self._run(plan, dry_run, yes, user_said, app)
        finally:
            self._drop_temp_ref(plan)

    def _run(self, plan: Plan, dry_run: bool, yes: bool, user_said: Optional[str], app: str) -> int:
        try:
            if yes and not (user_said or "").strip():
                raise Refused(NEEDS_WORDS)
            found = self.preflight()
            plan.mode, plan.branch, plan.sha = found.mode, found.branch, found.sha
            plan.target = self.pick_target()
            if not updates.newer(plan.target["latest"], self.running):
                self.say(f"FlowState {self.running} is up to date (the newest release is {plan.target['latest']}). "
                         f"Nothing was changed.")
                return 0
            signers = self.read_signers()
            self.fetch_and_verify(plan, signers)
            if plan.deps_changed and self._runs_from_this_venv():
                raise Refused(f"release {plan.target['tag']} changes FlowState's Python packages, and on "
                              f"Windows they can't be replaced while this command runs from the same .venv")
            plan.services = self.services.find(self.folder)
            plan.apps = [p for p in self._venv_procs() if self._is_app(p)]
            if dry_run:
                self.say(f"FlowState update plan, {self.running} -> {plan.target['latest']} (dry run: nothing was changed):")
                for line in self._describe(plan):
                    self.say(line)
                self.say('To do it: flowstate update --yes --user-said "<the user\'s own words>" '
                         "(only when the user asked for it).")
                return 0
            for line in self._describe(plan):
                self.say(line)
            words = self.confirm(f"Update FlowState {self.running} -> {plan.target['latest']} now?", yes, user_said)
            lock = self._lock()
            try:
                self._keep_tag(plan)
            except Refused:
                lock.unlink(missing_ok=True)
                raise
        except Refused as e:
            return self._refused(e)
        try:
            return self._apply(plan, words, app)
        finally:
            lock.unlink(missing_ok=True)

    def _refused(self, e: Refused) -> int:
        first = "This is a developer setup; update it by hand. " if e.dev_setup else ""
        reasons = e.reasons
        if len(reasons) == 1 and reasons[0].startswith("FlowState can't verify"):
            self.say(f"{reasons[0]}. Nothing was changed. {manual_steps(self.folder)}")
        else:
            self.say(f"{first}Not updated: " + "; ".join(reasons) + ". Nothing was changed.")
            self.say(manual_steps(self.folder))
        return 1

    def _apply(self, plan: Plan, words: str, app: str) -> int:
        t, frm = plan.target, self.running
        started = updates._iso(self.clock.now())
        base = {"started_at": started, "from": frm, "to": t["latest"], "from_sha": plan.sha,
                "from_branch": plan.branch, "asked_by": app, "user_said": words[:500]}
        self._save(**base, result="running")
        backup = None
        if self.db.is_file():
            from .backup import BackupError, backup_now
            try:
                backup = backup_now(self.db, label="pre-update", prune=False)["path"]
            except BackupError as e:
                self.say(f"Not updated: the database backup failed ({e}). Nothing was changed.")
                self._save(**base, result="refused", error=f"backup failed: {e}")
                return 1
            self.say(f"Backup OK: {backup} (integrity ok).")
        done = {"stopped": False, "swapped": False, "venv": False, "started": False}
        step = "stopping services"
        try:
            self.say("Stopping this folder's FlowState services...")
            done["stopped"] = True
            self.stop_services(plan)
            step = "making a new .venv" if plan.deps_changed else "moving the code"
            if plan.deps_changed:
                self._new_venv(plan, done)
            step = "moving the code"
            self.say(f"Moving the code to {t['tag']}...")
            done["swapped"] = True
            r = (self.git.run("merge", "--ff-only", "-q", plan.ref) if plan.mode == "main"
                 else self.git.run("checkout", "-q", "--detach", plan.ref))
            if r.returncode != 0:
                raise StepFailed(step, _last(r.stderr))
            if self.db.is_file():
                step = "migrating the database"
                self.migrator.migrate(self.python, self.folder, self.db)
            step = "starting services"
            done["started"] = True
            self.start_services(plan)
            step = "checking the new version"
            problems = self.health.check(self.python, self.folder, self.db if self.db.is_file() else None,
                                         t["latest"], plan.services)
            if problems:
                raise StepFailed(step, "; ".join(problems))
        except (StepFailed, OSError, subprocess.SubprocessError) as e:
            what = e.detail if isinstance(e, StepFailed) else f"{type(e).__name__}: {e}"
            step = e.step if isinstance(e, StepFailed) else step
            return self._rolled_back(plan, base, done, step, what, backup, app, words)
        to_sha = self.git.out("rev-parse", "HEAD") or ""
        self._save(**base, result="ok", to_sha=to_sha, finished_at=updates._iso(self.clock.now()),
                   venv_swapped=done["venv"], backup=str(backup) if backup else None)
        self._history(app, words, {"version": frm, "commit": plan.sha, "branch": plan.branch},
                      {"version": t["latest"], "commit": to_sha, "result": "ok", "rolled_back": False},
                      f"update {frm} -> {t['latest']}: ok")
        self.say(f"Done. FlowState {t['latest']} is installed. Quit and reopen your AI apps so they use "
                 f"{t['latest']}.")
        return 0

    def _new_venv(self, plan: Plan, done: dict) -> None:
        busy = self._venv_procs()
        if busy:
            raise StepFailed("making a new .venv", "the dependencies changed, so every program using this "
                             "folder's .venv must be quit first; still running: "
                             + ", ".join(f"pid {p['pid']}" for p in busy))
        aside = self.folder / ".venv.pre-update"
        if aside.exists():
            shutil.rmtree(aside)
        self.rename(self.venv, aside)       # Windows refuses while a file in it is in use
        done["venv"] = True
        self.say("Making a new .venv (the old one is kept as .venv.pre-update)...")
        self.pip.create_venv(self.venv)
        self.pip.install(self.python, self.folder)

    def _restore(self, plan: Plan, done: dict) -> list:
        """Put the old code, branch and .venv back. Returns problems (empty when all went back)."""
        problems = []
        if done["swapped"]:
            if plan.branch:
                r = self.git.run("checkout", "-q", "-f", plan.branch)
                r2 = self.git.run("reset", "-q", "--hard", plan.sha) if r.returncode == 0 else r
            else:
                r2 = self.git.run("checkout", "-q", "-f", "--detach", plan.sha)
            if r2.returncode != 0:
                problems.append(f"git could not go back to {plan.sha[:8]}: {_last(r2.stderr)}")
        if done["venv"]:
            aside = self.folder / ".venv.pre-update"
            try:
                if self.venv.exists():
                    shutil.rmtree(self.venv)
                self.rename(aside, self.venv)
            except OSError as e:
                problems.append(f"the old .venv could not be put back ({e}); it is {aside}")
        head = self.git.out("rev-parse", "HEAD")
        branch = self.git.out("symbolic-ref", "--quiet", "--short", "HEAD") or None
        if head != plan.sha or branch != plan.branch:
            problems.append(f"git is at {(head or '?')[:8]} on {branch or 'no branch'}, expected "
                            f"{plan.sha[:8]} on {plan.branch or 'no branch'}")
        return problems

    def _rolled_back(self, plan, base, done, step, what, backup, app, words) -> int:
        frm, to = self.running, plan.target["latest"]
        self.say(f"The update to {to} failed while {step}: {what}")
        self.say("Rolling back...")
        problems = []
        if done["started"]:                 # services already started with the new code
            try:
                self.stop_services(plan)
            except (StepFailed, OSError) as e:
                problems.append(str(e))
        problems += self._restore(plan, done)
        if done["stopped"]:
            try:
                self.start_services(plan)
            except (StepFailed, OSError) as e:
                problems.append(str(e))
        db_ok = True
        if self.db.is_file():
            from .backup import integrity
            try:
                db_ok = integrity(self.db) == "ok"
            except sqlite3.Error:
                db_ok = False
        if not problems and done["stopped"]:
            problems += [f"after the rollback: {p}" for p in self.health.check(
                self.python, self.folder, self.db if self.db.is_file() else None, frm, plan.services)]
        result = "rolled_back" if not problems else "rollback_incomplete"
        self._save(**base, result=result, failed_step=step, error=what[:300],
                   rollback_problems=problems or None, finished_at=updates._iso(self.clock.now()),
                   backup=str(backup) if backup else None)
        self._history(app, words, {"version": frm, "commit": plan.sha, "branch": plan.branch},
                      {"version": to, "result": result, "rolled_back": True, "failed_step": step,
                       "error": what[:300]}, f"update {frm} -> {to}: rolled back")
        if problems:
            self.say("The rollback did not fully work: " + "; ".join(problems))
        else:
            self.say(f"Rolled back: FlowState {frm} runs again ({plan.branch or 'detached'} at {plan.sha[:8]}, "
                     f"the same .venv). The database was not put back: the old version reads it as it is.")
        if not db_ok:
            self.say("The database FAILED its integrity check. FlowState did not change it. To put the "
                     f"pre-update backup back, run: flowstate restore \"{backup}\" --to \"{self.db}\" --force"
                     if backup else "The database FAILED its integrity check; FlowState did not change it.")
        return 1

    # ------------------------------------------------------------ --rollback
    def rollback(self, yes: bool = False, user_said: Optional[str] = None, app: str = "command-line") -> int:
        last = updates.read_state(self.state_path).get("last_update") or {}
        try:
            plan = self.preflight(for_rollback=True)
            if last.get("result") != "ok" or not last.get("from_sha"):
                raise Refused("there is no successful update to go back from")
            if plan.sha != last.get("to_sha"):
                raise Refused(f"this folder is not at the commit the last update installed "
                              f"({str(last.get('to_sha'))[:8]})")
            back = Plan(mode="rollback", branch=last.get("from_branch"), sha=last["from_sha"],
                        target={"latest": last.get("from"), "tag": last.get("from")})
            cur = Plan(branch=plan.branch, sha=plan.sha)
            if self.git.run("cat-file", "-e", f"{back.sha}^{{commit}}").returncode != 0:
                raise Refused(f"the old commit {back.sha[:8]} is no longer in this clone")
            back.services = self.services.find(self.folder)
            words = self.confirm(f"Go back from FlowState {self.running} to {last.get('from')}?", yes, user_said)
            lock = self._lock()
        except Refused as e:
            return self._refused(e)
        try:
            return self._apply_rollback(back, cur, last, words, app)
        finally:
            lock.unlink(missing_ok=True)

    def _apply_rollback(self, back: Plan, cur: Plan, last: dict, words: str, app: str) -> int:
        to = back.target["latest"]
        if self.db.is_file():
            from .backup import backup_now
            self.say(f"Backup OK: {backup_now(self.db, label='pre-update', prune=False)['path']}.")
        done = {"stopped": True, "swapped": True, "venv": False, "started": True}
        problems = []
        try:
            self.stop_services(back)
        except StepFailed as e:
            problems.append(str(e))
        problems += self._restore(back, done)
        aside = self.folder / ".venv.pre-update"
        if last.get("venv_swapped") and aside.exists() and not problems:
            try:
                shutil.rmtree(self.venv)
                self.rename(aside, self.venv)
            except OSError as e:
                problems.append(f"the old .venv could not be put back ({e})")
        try:
            self.start_services(back)
        except StepFailed as e:
            problems.append(str(e))
        if not problems:
            problems += self.health.check(self.python, self.folder, self.db if self.db.is_file() else None,
                                          to, back.services)
        result = "ok" if not problems else "problems"
        self._save(**{**last, "result": "rolled_back_by_user", "rollback_result": result,
                      "rollback_at": updates._iso(self.clock.now())})
        self._history(app, words, {"version": self.running, "commit": cur.sha},
                      {"version": to, "commit": back.sha, "result": result}, f"update --rollback to {to}: {result}")
        if problems:
            self.say("Going back did not fully work: " + "; ".join(problems))
            return 1
        self.say(f"Done. FlowState {to} is back. Quit and reopen your AI apps so they use it.")
        return 0


def _last(text: str) -> str:
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    return (lines[-1] if lines else "no details")[:200]


def _deps(pyproject: str) -> Optional[list]:
    try:
        import tomllib
        return sorted(tomllib.loads(pyproject).get("project", {}).get("dependencies", []))
    except Exception:  # noqa: BLE001 - unreadable: treat as changed
        return None


def cli(a, say=print) -> int:
    db = Path(a.db).expanduser() if getattr(a, "db", None) else None
    up = Updater(db=db, say=say)
    if a.rollback:
        return up.rollback(yes=a.yes, user_said=a.user_said, app=a.app or "command-line")
    return up.run(dry_run=a.dry_run, yes=a.yes, user_said=a.user_said, app=a.app or "command-line")
