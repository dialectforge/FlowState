"""Real-data guard for FlowState's tests and e2e harness (loaded as `sitecustomize`).

Any Python process that has this folder on PYTHONPATH and FLOWSTATE_GUARD_DIRS set refuses
to open, create, rename, delete or connect to anything inside those folders (the user's
real FlowState data, config and token locations), refuses to run the macOS Keychain
tool, and refuses to look up api.github.com (the update check's host). Each refusal raises PermissionError and is appended to FLOWSTATE_GUARD_LOG, so a
refusal that the code under test swallows still fails the run. Standard library only.
"""
import os
import sys
import threading

_DIRS = tuple(os.path.realpath(d) for d in os.environ.get("FLOWSTATE_GUARD_DIRS", "").split(os.pathsep) if d)
_LOG = os.environ.get("FLOWSTATE_GUARD_LOG")
_busy = threading.local()
_NO_NET_HOSTS = ("api.github.com",)   # the update check's host (updates.py)

_PATH_EVENTS = {"open": 1, "os.mkdir": 1, "os.remove": 1, "os.rmdir": 1, "os.rename": 2,
                "os.replace": 2, "os.symlink": 2, "os.link": 2, "os.truncate": 1, "os.chmod": 1,
                "os.chown": 1, "os.utime": 1, "os.listdir": 1, "os.scandir": 1,
                "shutil.copyfile": 2, "shutil.copytree": 2, "shutil.rmtree": 1, "shutil.move": 2,
                "sqlite3.connect": 1}


def forbidden(path) -> bool:
    try:
        p = os.fsdecode(os.fspath(path))
    except TypeError:
        return False                          # a file descriptor, not a path
    if p.startswith("file:"):                 # sqlite URI
        p = p[5:].split("?", 1)[0]
    if not p or p == ":memory:":
        return False
    p = os.path.realpath(os.path.abspath(p))
    return any(p == d or p.startswith(d + os.sep) for d in _DIRS)


def _refuse(event, what):
    msg = f"[pid {os.getpid()}] {event}: {what}"
    if _LOG:
        try:
            with open(_LOG, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass
    raise PermissionError(f"FlowState test guard: refusing to touch real user data ({msg})")


def _hook(event, args):
    if getattr(_busy, "on", False):
        return
    if event == "socket.getaddrinfo" and str(args[0]).lower().rstrip(".") in _NO_NET_HOSTS:
        _refuse(event, f"network lookup of {args[0]} (tests never call GitHub)")
    n = _PATH_EVENTS.get(event)
    if n is None and event != "subprocess.Popen":
        return
    _busy.on = True
    try:
        if n:
            for a in args[:n]:
                if forbidden(a):
                    _refuse(event, os.fsdecode(os.fspath(a)))
        else:
            exe, argv, cwd = args[0], args[1], args[2]
            if cwd is not None and forbidden(cwd):
                _refuse(event, f"cwd={cwd}")
            seq = list(argv) if isinstance(argv, (list, tuple)) else str(argv or "").split()
            prog = exe if exe else (seq[0] if seq else "")
            first = os.path.basename(os.fsdecode(os.fspath(prog))) if prog else ""
            if first == "security":
                _refuse(event, "macOS Keychain (security)")
            for a in seq[1:]:
                if isinstance(a, (str, bytes, os.PathLike)) and forbidden(a):
                    _refuse(event, f"argument {os.fsdecode(os.fspath(a))}")
    finally:
        _busy.on = False


if _DIRS:
    sys.addaudithook(_hook)


# ---------------------------------------------------------------- test-only module settings
# FS_TEST_PATCH (JSON {"module": {"NAME": value}}), set only by the e2e harness: once that
# module is imported in a server it starts, set those names (e.g. approvals.FSA_PUBLIC_KEYS
# to a throwaway test key, approvals.POPUP_SECONDS to a few seconds). FlowState itself has no
# such switch; this file is loaded only from tests/guard_site.
_PATCH = os.environ.get("FS_TEST_PATCH")
if _PATCH:
    import importlib.abc
    import json as _json

    _WANT = _json.loads(_PATCH)

    class _Patcher(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name not in _WANT:
                return None
            from importlib.machinery import PathFinder
            spec = PathFinder.find_spec(name, path)
            if spec is None or spec.loader is None:
                return spec
            run = spec.loader.exec_module

            def exec_module(module, _run=run, _vals=_WANT[name]):
                _run(module)
                for k, v in _vals.items():
                    setattr(module, k, v)
            spec.loader.exec_module = exec_module
            return spec

    sys.meta_path.insert(0, _Patcher())
