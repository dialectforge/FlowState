"""The quiet daily update check (updates.py, 2.4.0): the request, the cache, the timing, the
off switches and the one brief line. A fake fetcher stands in for GitHub everywhere; the
test guard (guard_site) refuses any lookup of api.github.com, and one test proves it."""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from flowstate import __version__, paths, updates

GOOD = {"tag_name": "v9.1.0", "draft": False, "prerelease": False,
        "html_url": "https://github.com/dialectforge/FlowState/releases/tag/v9.1.0",
        "published_at": "2026-10-21T21:36:00Z", "body": "notes"}


class FakeGitHub:
    """Records every request; answers (status, headers, body) or raises."""

    def __init__(self, status=200, data=GOOD, headers=None, raw=None, exc=None, sleep=0.0):
        self.status, self.data, self.raw, self.exc, self.sleep = status, data, raw, exc, sleep
        self.headers = headers if headers is not None else {"ETag": 'W/"abc"'}
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append({"url": url, "headers": dict(headers), "timeout": timeout})
        if self.sleep:
            time.sleep(self.sleep)
        if self.exc:
            raise self.exc
        body = self.raw if self.raw is not None else json.dumps(self.data).encode()
        return self.status, self.headers, body


@pytest.fixture
def state(tmp_path, monkeypatch):
    """update.json in tmp, the check ON (the session has it off) and no real request possible."""
    p = tmp_path / "data" / "update.json"
    monkeypatch.setattr(updates, "state_file", lambda: p)
    monkeypatch.delenv("FLOWSTATE_UPDATE_CHECK", raising=False)

    def no_network(*a, **k):
        raise AssertionError("the real GitHub request was used in a test")
    monkeypatch.setattr(updates, "_http_get", no_network)
    monkeypatch.setattr(updates, "_worker", None)
    return p


def _saved(p: Path, **kw) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(kw), encoding="utf-8")


# ---------------------------------------------------------------- the request
def test_request_headers_url_and_timeout(state):
    gh = FakeGitHub()
    res = updates.fetch_latest(gh)
    assert res["ok"] and res["latest"] == "9.1.0" and res["tag"] == "v9.1.0"
    (call,) = gh.calls
    assert call["url"] == "https://api.github.com/repos/dialectforge/FlowState/releases/latest"
    assert call["headers"] == {"Accept": "application/vnd.github+json",
                               "User-Agent": "FlowState-update-check"}
    assert __version__ not in call["headers"]["User-Agent"]
    assert not any(k.lower() == "authorization" for k in call["headers"])
    assert call["timeout"] == 5


def test_no_authorization_even_with_a_github_token_in_the_environment(state, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    monkeypatch.setenv("GH_TOKEN", "ghp_" + "y" * 36)
    gh = FakeGitHub()
    updates.check_now(gh)
    updates.check_now(gh)
    for c in gh.calls:
        assert not any(k.lower() == "authorization" for k in c["headers"])
        assert "ghp_" not in json.dumps(c["headers"])


def test_etag_is_sent_on_the_second_call_and_304_keeps_the_answer(state):
    gh = FakeGitHub()
    updates.check_now(gh)
    assert "If-None-Match" not in gh.calls[0]["headers"]
    gh.status, gh.raw = 304, b""
    st = updates.check_now(gh)
    assert gh.calls[1]["headers"]["If-None-Match"] == 'W/"abc"'
    assert st["latest"] == "9.1.0" and st["last_error"] is None


@pytest.mark.parametrize("change, why", [
    ({"tag_name": "9.1.0"}, "no v"),
    ({"tag_name": "v9.1"}, "two parts"),
    ({"tag_name": "v9.1.0-rc1"}, "suffix"),
    ({"tag_name": "v9.1.0\n"}, "newline"),
    ({"tag_name": None}, "missing"),
    ({"prerelease": True}, "prerelease"),
    ({"draft": True}, "draft"),
    ({"draft": None}, "draft unknown"),
    ({"html_url": "https://github.com/someone-else/FlowState/releases/tag/v9.1.0"}, "other repo"),
    ({"html_url": "https://evil.example/dialectforge/FlowState/"}, "other host"),
    ({"html_url": None}, "no url"),
])
def test_untrusted_answers_are_no_answer(state, change, why):
    st = updates.check_now(FakeGitHub(data={**GOOD, **change}))
    assert st.get("latest") is None, why
    assert "not a FlowState release" in st["last_error"]
    assert updates.brief_line("2.4.0") is None


def test_a_list_or_string_answer_is_no_answer(state):
    for data in ([GOOD], "v9.1.0", 7):
        assert not updates.fetch_latest(FakeGitHub(data=data))["ok"]


@pytest.mark.parametrize("gh, words", [
    (FakeGitHub(status=403, raw=b'{"message":"API rate limit exceeded"}'), "403, rate limit"),
    (FakeGitHub(status=429, raw=b""), "429, rate limit"),
    (FakeGitHub(status=500, raw=b"oops"), "answered 500"),
    (FakeGitHub(status=302, raw=b"", headers={"Location": "https://evil.example/"}), "answered 302"),
    (FakeGitHub(exc=TimeoutError("timed out")), "within 5 seconds"),
    (FakeGitHub(exc=OSError("[Errno 11001] getaddrinfo failed")), "can't reach GitHub"),
    (FakeGitHub(raw=b"x" * (256 * 1024 + 1)), "too large"),
    (FakeGitHub(raw=b"<html>not json</html>"), "not JSON"),
    (FakeGitHub(raw=b"\xff\xfe\x00"), "not JSON"),
    (FakeGitHub(exc=RuntimeError("surprise")), "check failed"),
])
def test_failures_are_stored_short_and_never_raised(state, gh, words):
    st = updates.check_now(gh)
    assert words in st["last_error"]
    assert len(st["last_error"]) < 80
    assert "rate limit exceeded" not in st["last_error"] and "Location" not in st["last_error"]
    assert updates.brief_line("2.4.0") is None


def test_a_failure_keeps_the_last_good_answer(state):
    updates.check_now(FakeGitHub())
    st = updates.check_now(FakeGitHub(exc=OSError("offline")))
    assert st["latest"] == "9.1.0" and "can't reach" in st["last_error"]


def test_real_request_reads_at_most_the_cap(monkeypatch):
    """The real _http_get: TLS 1.2+, no redirects followed, reads MAX_BODY + 1 at most."""
    ctx = updates._context()
    import ssl
    assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_2 and ctx.verify_mode == ssl.CERT_REQUIRED
    assert updates._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x/") is None
    read_sizes = []

    class Resp:
        status, headers = 200, {"ETag": "e"}

        def read(self, n):
            read_sizes.append(n)
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class Opener:
        def open(self, req, timeout):
            assert req.get_header("Authorization") is None and timeout == 5
            return Resp()
    monkeypatch.setattr(updates.urllib.request, "build_opener", lambda *h: Opener())
    assert updates._http_get(updates.RELEASES_URL, updates.request_headers(), 5)[0] == 200
    assert read_sizes == [updates.MAX_BODY + 1]


# ---------------------------------------------------------------- timing and one checker
def _iso(ts):
    return updates._iso(ts)


def test_due_after_24_hours_or_6_after_an_error(state):
    now = 1_800_000_000.0
    assert updates.due({}, now)
    assert not updates.due({"checked_at": _iso(now - 23.9 * 3600)}, now)
    assert updates.due({"checked_at": _iso(now - 24 * 3600)}, now)
    assert not updates.due({"checked_at": _iso(now - 5.9 * 3600), "last_error": "x"}, now)
    assert updates.due({"checked_at": _iso(now - 6 * 3600), "last_error": "x"}, now)
    assert updates.due({"checked_at": "garbage"}, now)


def test_maybe_refresh_fetches_only_when_due(state):
    gh = FakeGitHub()
    now = time.time()
    _saved(state, checked_at=_iso(now - 3600), latest="9.1.0")
    assert updates.maybe_refresh(gh, now=now) is False and gh.calls == []
    _saved(state, checked_at=_iso(now - 25 * 3600))
    assert updates.maybe_refresh(gh, now=now) is True
    updates._worker.join(5)
    assert len(gh.calls) == 1 and updates.read_state()["latest"] == "9.1.0"
    assert not updates._stamp_file(None).exists()


def test_twenty_start_heres_at_once_start_one_check(state):
    gh = FakeGitHub(sleep=0.5)
    started = []
    gate = threading.Barrier(20)

    def go():
        gate.wait()
        started.append(updates.maybe_refresh(gh))
    ts = [threading.Thread(target=go) for _ in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    updates._worker.join(5)
    assert started.count(True) == 1 and len(gh.calls) == 1


def test_another_process_checking_blocks_until_its_stamp_is_stale(state):
    gh = FakeGitHub()
    stamp = updates._stamp_file(None)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("other process", encoding="utf-8")
    assert updates.maybe_refresh(gh) is False and gh.calls == []
    old = time.time() - 121
    os.utime(stamp, (old, old))
    assert updates.maybe_refresh(gh) is True
    updates._worker.join(5)
    assert len(gh.calls) == 1 and not stamp.exists()


# ---------------------------------------------------------------- off switches
def test_off_by_config_means_no_request_and_no_line(state):
    _saved(state, checked_at=_iso(time.time() - 30 * 3600), latest="9.1.0",
           url=GOOD["html_url"])
    paths.save_config({"update_check": False})
    gh = FakeGitHub()
    assert updates.maybe_refresh(gh) is False
    assert updates.brief_line("2.4.0") is None
    assert gh.calls == []


@pytest.mark.parametrize("value", ["0", "false", "off", "no", "OFF"])
def test_off_by_environment(state, monkeypatch, value):
    _saved(state, latest="9.1.0", url=GOOD["html_url"])
    monkeypatch.setenv("FLOWSTATE_UPDATE_CHECK", value)
    gh = FakeGitHub()
    assert updates.maybe_refresh(gh) is False and gh.calls == []
    assert updates.brief_line("2.4.0") is None


def test_the_test_session_has_the_check_off():
    assert os.environ.get("FLOWSTATE_UPDATE_CHECK") == "0" and not updates.is_on()


def test_cli_check_on_off_and_status(state, capsys):
    assert updates.cli_check("off") == 0
    assert paths.load_config()["update_check"] is False and not updates.is_on()
    assert updates.cli_check("now") == 1          # off: nothing asked
    assert updates.cli_check("on") == 0 and updates.is_on()
    _saved(state, checked_at="2026-10-22T12:00:00Z", latest="9.1.0", url=GOOD["html_url"],
           last_error="GitHub answered 500")
    assert updates.cli_check(None) == 0
    out = capsys.readouterr().out
    assert "Update check: on" in out and "newest release seen: 9.1.0" in out
    assert "Last error: GitHub answered 500" in out and "A newer release is out: 9.1.0" in out


def test_cli_check_now_uses_the_fetcher(state, monkeypatch, capsys):
    gh = FakeGitHub()
    monkeypatch.setattr(updates, "_http_get", gh)
    assert updates.cli_check("now") == 0 and len(gh.calls) == 1
    assert "newest release seen: 9.1.0" in capsys.readouterr().out


def test_cli_parses_update_check():
    from flowstate import cli
    a = cli.build_parser().parse_args(["update", "check", "off"])
    assert a.action == "check" and a.sub == "off"
    assert "update" in cli.COMMANDS


# ---------------------------------------------------------------- the brief line
@pytest.mark.parametrize("latest, running, shown", [
    ("9.1.0", "2.4.0", True), ("2.4.1", "2.4.0", True), ("2.10.0", "2.9.0", True),
    ("2.4.0", "2.4.0", False), ("2.3.0", "2.4.0", False), ("2.9.0", "2.10.0", False),
    (None, "2.4.0", False), ("junk", "2.4.0", False), ("9.1.0", "dev", False)])
def test_line_only_when_newer(state, latest, running, shown):
    _saved(state, latest=latest, url=GOOD["html_url"])
    assert (updates.brief_line(running) is not None) is shown


def test_manual_wording_when_the_code_has_no_update_command(state, monkeypatch):
    _saved(state, latest="2.5.0", url="https://github.com/dialectforge/FlowState/releases/tag/v2.5.0")
    monkeypatch.setattr(updates, "has_update_command", lambda: False)
    line = updates.brief_line("2.4.0")
    assert line == ("FlowState 2.5.0 is out (this computer has 2.4.0). Tell the user once; if they "
                    "want it: git pull in the FlowState folder, then setup_flowstate.py --verify with "
                    "the same options as before (https://github.com/dialectforge/FlowState/releases/"
                    "tag/v2.5.0). Never update without being asked.")
    assert "\n" not in line and len(line) < 300 and "run flowstate update" not in line


def test_command_wording_only_with_the_command_and_not_opted_out(state, monkeypatch):
    _saved(state, latest="2.5.0", url="https://github.com/dialectforge/FlowState/releases/tag/v2.5.0")
    monkeypatch.setattr(updates, "has_update_command", lambda: True)
    line = updates.brief_line("2.4.0")
    assert "run flowstate update --dry-run" in line and "git pull" not in line
    assert "\n" not in line and len(line) < 300 and line.endswith("Never update without being asked.")
    paths.save_config({"self_update": False})
    line = updates.brief_line("2.4.0")
    assert "git pull" in line and "run flowstate update" not in line


def test_has_update_command_follows_the_running_code(monkeypatch):
    import importlib.util
    real = importlib.util.find_spec
    monkeypatch.setattr(updates.importlib.util, "find_spec",
                        lambda n, *a: None if n == "flowstate.selfupdate" else real(n, *a))
    assert updates.has_update_command() is False
    monkeypatch.setattr(updates.importlib.util, "find_spec", lambda n, *a: object())
    assert updates.has_update_command() is True


def test_a_saved_url_from_elsewhere_is_never_shown(state):
    _saved(state, latest="2.5.0", url="https://evil.example/x")
    line = updates.brief_line("2.4.0")
    assert "evil" not in line and "https://github.com/dialectforge/FlowState/releases" in line


def test_line_never_raises(state, monkeypatch):
    monkeypatch.setattr(updates, "read_state", lambda *a: 1 / 0)
    assert updates.brief_line("2.4.0") is None
    assert updates.maybe_refresh(FakeGitHub()) is False


# ---------------------------------------------------------------- start_here
def test_start_here_gets_the_line_on_top_and_the_brief_is_unchanged(state, backend, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_UPDATE_CHECK", "0")
    plain = backend.handle_call("start_here", {"project": "Alpha"}, client="t")
    monkeypatch.delenv("FLOWSTATE_UPDATE_CHECK")
    _saved(state, checked_at=_iso(time.time()), latest="9.1.0", url=GOOD["html_url"])
    text = backend.handle_call("start_here", {"project": "Alpha"}, client="t")
    first, rest = text.split("\n\n", 1)
    assert first.startswith(f"FlowState 9.1.0 is out (this computer has {__version__}).")
    strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("Since your last visit"))
    assert strip(rest) == strip(plain)


def test_start_here_never_waits_for_github(state, backend, monkeypatch):
    slow = FakeGitHub(sleep=10)
    monkeypatch.setattr(updates, "_http_get", slow)
    t0 = time.monotonic()
    text = backend.handle_call("start_here", {"project": "Alpha"}, client="t")
    took = time.monotonic() - t0
    assert took < 5, took
    assert "is out" not in text                    # nothing cached yet: no line
    assert updates._worker is not None and updates._worker.is_alive()
    deadline = time.monotonic() + 3                 # the thread may not have reached the request yet
    while not slow.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(slow.calls) == 1
    t0 = time.monotonic()
    backend.handle_call("start_here", {"project": "Alpha"}, client="t")   # still checking: no 2nd
    assert time.monotonic() - t0 < 5 and len(slow.calls) == 1


# ---------------------------------------------------------------- the guard
def test_guard_refuses_github_without_the_fake(tmp_path):
    """With the test guard on, the REAL request can't even look up api.github.com: the refusal
    is recorded (here in a log of its own, so this run doesn't fail) and no network is used."""
    log = tmp_path / "guard.log"
    code = ("import json; from flowstate import updates; "
            "print(json.dumps(updates.fetch_latest()))")
    env = dict(os.environ, FLOWSTATE_GUARD_LOG=str(log))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    res = json.loads(r.stdout.strip().splitlines()[-1])
    assert res["ok"] is False and "can't reach GitHub" in res["error"]
    assert "api.github.com" in log.read_text(encoding="utf-8")
