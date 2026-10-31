"""cloudflared version note for `flowstate remote status` (2.4.4, task 63). Standard library only.

FlowState doesn't ship cloudflared; remote access asks the user to install it. Versions before
2026.10.2 still trust eMudhra intermediate certificates whose private keys were compromised
(Cloudflare's notice, below). This only reads `cloudflared --version` and returns one line to
show when the installed one is older. It never downloads, installs or updates anything, never
blocks anything, and never raises: cloudflared missing, slow, failing or printing something
unexpected means no note.
"""

from __future__ import annotations

import re
import shutil
import subprocess

FIXED = (2026, 10, 2)
NOTICE_URL = "https://blog.cloudflare.com/emudhra-certificate-compromise"
_VERSION = re.compile(r"version\s+(\d+)\.(\d+)\.(\d+)")


def installed_version(timeout: float = 5.0) -> tuple[int, int, int] | None:
    """(year, month, patch) of the cloudflared on PATH, or None if it can't be read."""
    try:
        exe = shutil.which("cloudflared")
        if not exe:
            return None
        r = subprocess.run([exe, "--version"], capture_output=True, text=True,
                           timeout=timeout, check=False)
        if r.returncode != 0:
            return None
        m = _VERSION.search(f"{r.stdout or ''}\n{r.stderr or ''}")
        return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None
    except Exception:   # OSError, TimeoutExpired, anything else: no note
        return None


def note(timeout: float = 5.0) -> str | None:
    """One calm line when the installed cloudflared is older than 2026.10.2, else None."""
    try:
        v = installed_version(timeout)
        if v is None or v >= FIXED:
            return None
        have = ".".join(str(n) for n in v)
        fixed = ".".join(str(n) for n in FIXED)
        return (f"Note: cloudflared {have} is older than {fixed}. Please update it: older "
                f"versions still trust compromised eMudhra certificates ({NOTICE_URL}).")
    except Exception:
        return None
