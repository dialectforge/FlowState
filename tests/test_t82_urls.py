"""Task 82: urls.no_trailing_slash, the one helper for trailing '/' on URL and issuer strings.

It replaced `.rstrip("/")` in remote_auth, http_app, review_tunnel and the relay; these tests pin
that it gives exactly what that call gave, so the switch changed no output."""

import pytest

from flowstate.urls import no_trailing_slash

SAMPLES = [
    "https://abc123.fsmcp.com", "https://abc123.fsmcp.com/", "https://abc123.fsmcp.com//",
    "https://flowstate.example.com/mcp", "https://flowstate.example.com/mcp/",
    "http://127.0.0.1:8766/", "https://Host.Example.COM/Path/", "/", "///", "", "no-slash",
    "https://x.example/?q=a/", "https://x.example/#frag/", " https://x.example/ ",
]


@pytest.mark.parametrize("s", SAMPLES)
def test_same_as_rstrip(s):
    assert no_trailing_slash(s) == s.rstrip("/")


def test_examples():
    assert no_trailing_slash("https://abc123.fsmcp.com/") == "https://abc123.fsmcp.com"
    assert no_trailing_slash("https://a.example/mcp//") == "https://a.example/mcp"
    assert no_trailing_slash("/") == ""
    assert no_trailing_slash(" https://a.example/ ") == " https://a.example/ "  # no whitespace strip


def test_none_raises_like_rstrip():
    # The callers never passed None to .rstrip; the helper fails the same way rather than guess.
    with pytest.raises(AttributeError):
        no_trailing_slash(None)


def test_oauth_issuer_unchanged():
    from pydantic import AnyHttpUrl
    from flowstate.remote_auth import oauth_issuer
    for u in ("https://abc123.fsmcp.com", "https://abc123.fsmcp.com/", "HTTPS://Host.Example:443/",
              "http://127.0.0.1:8766/", "https://flowstate.example.com/base/"):
        assert oauth_issuer(u) == str(AnyHttpUrl(u)).rstrip("/")
