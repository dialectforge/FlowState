"""Task 63: `flowstate remote status` notes a cloudflared older than 2026.10.2. shutil.which and
subprocess.run are mocked; the real cloudflared is never run."""

import subprocess

import pytest

from flowstate import cli, cloudflared_check as CF


def _fake(monkeypatch, out="", rc=0, found=True, exc=None, stderr=""):
    calls = []
    monkeypatch.setattr(CF.shutil, "which",
                        lambda name: "/opt/test/bin/cloudflared" if found else None)

    def run(args, **kw):
        calls.append((args, kw))
        if exc is not None:
            raise exc
        return subprocess.CompletedProcess(args, rc, stdout=out, stderr=stderr)
    monkeypatch.setattr(CF.subprocess, "run", run)
    return calls


def _line(v):
    return f"cloudflared version {v} (built 2026-10-14-1200 UTC)\n"


def test_an_older_version_gets_the_note(monkeypatch):
    calls = _fake(monkeypatch, _line("2026.10.1"))
    n = CF.note()
    assert n == ("Note: cloudflared 2026.10.1 is older than 2026.10.2. Please update it: older "
                 "versions still trust compromised eMudhra certificates "
                 "(https://blog.cloudflare.com/emudhra-certificate-compromise).")
    args, kw = calls[0]
    assert args == ["/opt/test/bin/cloudflared", "--version"]
    assert kw.get("timeout") and not kw.get("shell")


@pytest.mark.parametrize("v", ["2025.11.0", "2026.9.9", "2026.10.0"])
def test_other_older_versions_get_the_note(monkeypatch, v):
    _fake(monkeypatch, _line(v))
    assert CF.note().startswith(f"Note: cloudflared {v} is older than 2026.10.2.")


@pytest.mark.parametrize("v", ["2026.10.2", "2026.10.4", "2026.11.0", "2027.1.0"])
def test_the_fixed_and_newer_versions_get_no_note(monkeypatch, v):
    _fake(monkeypatch, _line(v))
    assert CF.installed_version() == tuple(int(x) for x in v.split("."))
    assert CF.note() is None


def test_the_version_on_stderr_is_read_too(monkeypatch):
    _fake(monkeypatch, "", stderr=_line("2026.10.1"))
    assert CF.note() is not None


@pytest.mark.parametrize("out", ["", "garbage", "cloudflared version unknown",
                                 "cloudflared version 2026.10", "\x00\xff"])
def test_unreadable_output_gets_no_note(monkeypatch, out):
    _fake(monkeypatch, out)
    assert CF.installed_version() is None and CF.note() is None


def test_a_missing_cloudflared_gets_no_note_and_is_not_run(monkeypatch):
    calls = _fake(monkeypatch, _line("2026.10.1"), found=False)
    assert CF.note() is None and calls == []


@pytest.mark.parametrize("exc", [subprocess.TimeoutExpired(["cloudflared"], 5),
                                 OSError("exec format error"), PermissionError("denied"),
                                 RuntimeError("unexpected")])
def test_a_failing_run_gets_no_note(monkeypatch, exc):
    _fake(monkeypatch, exc=exc)
    assert CF.note() is None


def test_a_non_zero_exit_gets_no_note(monkeypatch):
    _fake(monkeypatch, _line("2026.10.1"), rc=1)
    assert CF.note() is None


def test_which_itself_failing_gets_no_note(monkeypatch):
    def boom(name):
        raise OSError("bad PATH")
    monkeypatch.setattr(CF.shutil, "which", boom)
    assert CF.note() is None


@pytest.mark.parametrize("case,expect_note", [
    ({"out": _line("2026.10.1")}, True),
    ({"out": _line("2026.10.2")}, False),
    ({"out": _line("2026.10.4")}, False),
    ({"out": "garbage"}, False),
    ({"found": False}, False),
    ({"exc": subprocess.TimeoutExpired(["cloudflared"], 5)}, False),
])
def test_remote_status_shows_the_note_only_for_an_older_version_and_exits_0(
        monkeypatch, capsys, case, expect_note):
    _fake(monkeypatch, **case)
    assert cli.main(["remote", "status"]) == 0
    out = capsys.readouterr().out
    assert "Owner passphrase:" in out
    assert ("older than 2026.10.2" in out) is expect_note
    assert ("emudhra-certificate-compromise" in out) is expect_note
