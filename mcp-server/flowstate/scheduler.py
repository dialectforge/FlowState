"""`flowstate schedule-backups`: run `flowstate backup` every night.

Windows: Task Scheduler (schtasks) running a small .cmd wrapper. macOS: a launchd agent
in ~/Library/LaunchAgents. Linux: a systemd user timer, or a crontab line when systemd
isn't available. `--remove` undoes exactly what was added. Commands go through `run`,
so tests use a fake runner and a fake home folder.
"""

import platform
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import paths

TASK_NAME = "FlowState Nightly Backup"
LAUNCHD_LABEL = "com.flowstate.backup"
UNIT = "flowstate-backup"
CRON_MARK = "# flowstate-backup"
SERVER_DIR = Path(__file__).resolve().parents[1]


def _hhmm(at: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in at.split(":"))
    except ValueError:
        raise ValueError(f"--time must be HH:MM, not {at!r}") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"--time must be HH:MM, not {at!r}")
    return h, m


def backup_args(python: str, db: str, offsite: bool, backup_dir: Optional[str]) -> list[str]:
    args = [python, "-m", "flowstate", "backup", "--db", db]
    if backup_dir:
        args += ["--dir", backup_dir]
    if offsite:
        args.append("--offsite")
    return args


def schedule(python: str, db: str, at: str = "02:30", offsite: bool = False,
             remove: bool = False, dry_run: bool = False, backup_dir: Optional[str] = None,
             system: Optional[str] = None, home: Optional[Path] = None,
             run: Callable = subprocess.run, which: Callable = shutil.which) -> dict:
    system = system or platform.system()
    h, m = _hhmm(at)
    log_dir = Path(backup_dir) if backup_dir else paths.backup_dir(home, system)
    log = log_dir / "backup.log"
    args = backup_args(python, db, offsite, backup_dir)
    env = {"FLOWSTATE_DB": db, "PYTHONPATH": str(SERVER_DIR)}
    files: dict[Path, Optional[str]] = {}   # path -> content (None = delete)
    cmds: list[tuple[list, Optional[str]]] = []  # (argv, stdin)

    if system == "Windows":
        wrapper = paths.config_dir(home, system) / "flowstate-backup.cmd"
        if remove:
            cmds.append((["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], None))
            files[wrapper] = None
        else:
            line = " ".join(f'"{a}"' for a in args)
            files[wrapper] = ("@echo off\r\n"
                              + "".join(f'set "{k}={v}"\r\n' for k, v in env.items())
                              + f'if not exist "{log_dir}" mkdir "{log_dir}"\r\n'
                              + f'{line} >> "{log}" 2>&1\r\n')
            cmds.append((["schtasks", "/Create", "/TN", TASK_NAME, "/TR", f'"{wrapper}"',
                          "/SC", "DAILY", "/ST", f"{h:02d}:{m:02d}", "/F"], None))
        how = "Windows Task Scheduler"
    elif system == "Darwin":
        home_ = Path(home) if home else Path.home()
        plist = home_ / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        cmds.append((["launchctl", "unload", "-w", str(plist)], None))
        if remove:
            files[plist] = None
        else:
            files[plist] = _plist(args, env, h, m, log)
            cmds.append((["launchctl", "load", "-w", str(plist)], None))
        how = "launchd (LaunchAgents)"
    else:
        home_ = Path(home) if home else Path.home()
        if which("systemctl"):
            unit_dir = home_ / ".config" / "systemd" / "user"
            svc, tmr = unit_dir / f"{UNIT}.service", unit_dir / f"{UNIT}.timer"
            if remove:
                cmds.append((["systemctl", "--user", "disable", "--now", f"{UNIT}.timer"], None))
                files[svc] = files[tmr] = None
                cmds.append((["systemctl", "--user", "daemon-reload"], None))
            else:
                files[svc] = ("[Unit]\nDescription=FlowState nightly backup\n\n[Service]\n"
                              "Type=oneshot\n"
                              + "".join(f'Environment="{k}={v}"\n' for k, v in env.items())
                              + "ExecStart=" + " ".join(shlex.quote(a) for a in args) + "\n")
                files[tmr] = ("[Unit]\nDescription=FlowState nightly backup\n\n[Timer]\n"
                              f"OnCalendar=*-*-* {h:02d}:{m:02d}:00\nPersistent=true\n\n"
                              "[Install]\nWantedBy=timers.target\n")
                cmds.append((["systemctl", "--user", "daemon-reload"], None))
                cmds.append((["systemctl", "--user", "enable", "--now", f"{UNIT}.timer"], None))
            how = "systemd user timer"
        else:
            current = ""
            if not dry_run:
                r = run(["crontab", "-l"], capture_output=True, text=True)
                current = r.stdout if r.returncode == 0 else ""
            kept = [l for l in current.splitlines() if CRON_MARK not in l]
            if not remove:
                envs = " ".join(f"{k}={shlex.quote(v)}" for k, v in env.items())
                kept.append(f"{m} {h} * * * {envs} " + " ".join(shlex.quote(a) for a in args)
                            + f" >> {shlex.quote(str(log))} 2>&1 {CRON_MARK}")
            cmds.append((["crontab", "-"], "\n".join(kept) + ("\n" if kept else "")))
            how = "crontab"

    plan = {"scheduler": how, "time": f"{h:02d}:{m:02d}", "removed": remove,
            "command": " ".join(args), "files": [str(p) for p in files],
            "commands": [" ".join(c) for c, _i in cmds], "dry_run": dry_run}
    if dry_run:
        return plan
    for p, content in files.items():
        if content is None:
            p.unlink(missing_ok=True)
    for p, content in files.items():
        if content is not None:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8", newline="")
    if not remove:
        log_dir.mkdir(parents=True, exist_ok=True)
    errors = []
    for argv, stdin in cmds:
        r = run(argv, input=stdin, capture_output=True, text=True)
        tolerated = argv[:2] == ["launchctl", "unload"] or (remove and r.returncode != 0)
        if r.returncode != 0 and not tolerated:
            errors.append(f"{argv[0]} failed: {(r.stderr or r.stdout or '').strip()[:200]}")
    plan["errors"] = errors
    return plan


def _plist(args: list, env: dict, h: int, m: int, log: Path) -> str:
    from xml.sax.saxutils import escape
    arr = "".join(f"\n        <string>{escape(a)}</string>" for a in args)
    envs = "".join(f"\n        <key>{escape(k)}</key><string>{escape(v)}</string>"
                   for k, v in env.items())
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{arr}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{envs}
    </dict>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>{h}</integer><key>Minute</key><integer>{m}</integer></dict>
    <key>StandardOutPath</key><string>{escape(str(log))}</string>
    <key>StandardErrorPath</key><string>{escape(str(log))}</string>
</dict>
</plist>
"""
