"""Run `flowstate ...` without a terminal (like an agent's shell) or in a terminal of its own
(like the user typing it), on POSIX and Windows. Used by the unit tests and tests/e2e.

- no terminal: POSIX, a new session (no controlling terminal). Windows, CREATE_NO_WINDOW: a
  console with no window, which is what an AI app's hidden shell gets (FlowState refuses it).
- in a terminal: POSIX, a pseudo-terminal. Windows, a console of its own (CREATE_NEW_CONSOLE,
  window hidden so nothing pops up); tests/conin_typist.py attaches to it, watches the screen
  and types each answer when a "Type ..." prompt shows. Stdout / stderr go to a file there
  (the prompts are written to the console itself), and both are returned together.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TYPIST = Path(__file__).resolve().parent / "conin_typist.py"


def run_no_terminal(cmd, *, input=None, env=None, cwd=None, timeout=60):
    kw = ({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt"
          else {"start_new_session": True})
    return subprocess.run(cmd, input=input, env=env, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, **kw)


def run_in_terminal(cmd, answers, *, env=None, cwd=None, timeout=60):
    """(exit code, what it showed), answering each "Type ..." prompt in turn."""
    if os.name == "nt":
        return _windows(cmd, answers, env, cwd, timeout)
    return _posix(cmd, answers, env, cwd, timeout)


def _posix(cmd, answers, env, cwd, timeout):
    import pty
    import select
    import warnings
    with warnings.catch_warnings():   # forkpty in a threaded process: the child execs at once
        warnings.simplefilter("ignore", DeprecationWarning)
        pid, master = pty.fork()
    if pid == 0:
        if cwd:
            os.chdir(cwd)
        os.execve(cmd[0], list(cmd), env if env is not None else dict(os.environ))
    seen, todo, deadline = b"", list(answers), time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.2)[0]:
                try:
                    data = os.read(master, 4096)
                except OSError:   # the child closed the terminal
                    break
                if not data:
                    break
                seen += data
                if todo and seen.count(b"Type ") > len(answers) - len(todo):
                    os.write(master, todo.pop(0).encode() + b"\n")
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(master)
    return os.waitstatus_to_exitcode(status), seen.decode(errors="replace")


def _windows(cmd, answers, env, cwd, timeout):
    with tempfile.TemporaryDirectory(prefix="fs-term-") as td:
        out_path, screen_path = Path(td) / "out.txt", Path(td) / "screen.txt"
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                                    # SW_HIDE: a real console, not shown
        with open(out_path, "wb") as out:
            p = subprocess.Popen(cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, startupinfo=si,
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
            typist = subprocess.Popen(
                [sys.executable, "-I", str(TYPIST), str(p.pid), json.dumps(list(answers)),
                 str(screen_path), str(timeout)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                code = p.wait(timeout)
            except subprocess.TimeoutExpired:
                p.kill()                                      # our own PID only
                code = p.wait()
            try:
                typist.wait(15)
            except subprocess.TimeoutExpired:
                typist.kill()
                typist.wait()
        shown = screen_path.read_text(encoding="utf-8") if screen_path.exists() else ""
        printed = out_path.read_bytes().decode("utf-8", errors="replace")
    return code, shown + ("\n" if shown and printed else "") + printed
