"""(task 43) This FlowState's display name (install_name.py): "Main FlowState" until the user
changes it with `flowstate name` or on the local review page's Settings. Kept in config.json,
logged in review_mode_log, shown escaped on FlowState's own pages, sent to the relay in the
agent's hello (and a "name" message when it changes), never in a URL and never through an MCP
tool. Temp DBs and sandboxed folders only."""

import json
import re
import ssl

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from flowstate import install_name, review_page
from flowstate.review_page import HOST, Page, create_app

from test_review_without_terminal import JSON_H, _grant, _phone_client, call, pile, relay, relay_dir  # noqa: F401
from test_settings_page import BASE, CSRF, PORT, flash, log, login, post

NASTY = '<script>alert("x")</script>'


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config" / "config.json"


@pytest.fixture
def page(backend, seeded_db, cfg):
    p = Page(seeded_db, PORT)
    yield p, TestClient(create_app(p), base_url=BASE, follow_redirects=False), seeded_db


# ---------------------------------------------------------------- the value
def test_default_and_validation(cfg):
    assert install_name.get() == "Main FlowState" and install_name.is_default()
    assert install_name.clean("  Office   laptop ") == "Office laptop"
    assert install_name.clean("x" * 40) == "x" * 40
    assert install_name.clean("Büro – Café ☕") == "Büro – Café ☕"
    for bad in ("", "   ", "x" * 41, "a\nb", "a\tb", "a\x00b", "a​b", "a‮b", None, 5):
        with pytest.raises(install_name.BadName):
            install_name.clean(bad)
    # a broken or hand-edited config falls back to the default
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"display_name": "a\nb"}))
    assert install_name.get() == "Main FlowState" and install_name.is_default()
    cfg.write_text("{not json")
    assert install_name.get() == "Main FlowState"


def test_set_reset_and_log(cfg, seeded_db):
    res = install_name.set_name("Office laptop", seeded_db)
    assert res["changed"] and res["old"] == "Main FlowState" and res["now"] == "Office laptop"
    assert json.loads(cfg.read_text())["display_name"] == "Office laptop"
    assert install_name.set_name("Office laptop", seeded_db)["changed"] is False   # not logged
    install_name.set_name(None, seeded_db)
    assert "display_name" not in json.loads(cfg.read_text()) and install_name.is_default()
    install_name.set_name("Main FlowState", seeded_db)          # the default: no key, no change
    assert "display_name" not in json.loads(cfg.read_text())
    rows = log(seeded_db, install_name.SCOPE)
    assert [(r[1], r[2], r[3]) for r in rows] == [("Main FlowState", "Office laptop", "command-line"),
                                                  ("Office laptop", "Main FlowState", "command-line")]


def test_cli_show_set_reset(cfg, seeded_db, capsys):
    from flowstate import cli
    assert cli.main(["name", "--db", str(seeded_db)]) == 0
    assert 'called "Main FlowState" (the default)' in capsys.readouterr().out
    assert cli.main(["name", "Home  desk", "--db", str(seeded_db)]) == 0
    assert 'now called "Home desk"' in capsys.readouterr().out
    assert cli.main(["name", "--db", str(seeded_db)]) == 0
    assert 'called "Home desk".' in capsys.readouterr().out
    assert cli.main(["name", "x" * 41, "--db", str(seeded_db)]) == 2
    assert "at most 40" in capsys.readouterr().out and install_name.get() == "Home desk"
    assert cli.main(["name", "x", "--reset", "--db", str(seeded_db)]) == 2
    assert cli.main(["name", "--reset", "--db", str(seeded_db)]) == 0
    assert install_name.is_default()
    assert [r[3] for r in log(seeded_db, install_name.SCOPE)] == ["command-line", "command-line"]


# ---------------------------------------------------------------- the review page
def test_settings_row_edit_and_log(page):
    p, c, db = page
    csrf, html = login(p, c)
    sec = html.split("<h2>Settings</h2>", 1)[1]
    assert "Name of this FlowState" in sec and "<b>Main FlowState</b> (the default)." in sec
    assert "action='/settings/name'" in sec and "Back to" not in sec
    assert "FlowState review <span class='inst'>· Main FlowState</span>" in html
    assert flash(post(c, "/settings/name", csrf, name="  Office laptop ")) \
        == 'This FlowState is now called "Office laptop".'
    assert install_name.get() == "Office laptop"
    row = log(db, install_name.SCOPE)[-1]
    assert row[1:4] == ("Main FlowState", "Office laptop", "review-page")
    assert row[5] == review_page.SETTING_WORDS and row[6] == "review-page"
    html = c.get("/").text
    assert "<title>FlowState review: Office laptop</title>" in html and "Back to" in html
    # recent changes list it in plain words
    recent = html.split("Recent changes", 1)[1]
    assert "Name of this FlowState" in recent and "Office laptop" in recent
    # bad names change nothing
    for bad in ("", "x" * 41, "a\nb"):
        assert flash(post(c, "/settings/name", csrf, name=bad)).startswith("Not changed: ")
    assert install_name.get() == "Office laptop"
    # reset
    assert "Main FlowState" in flash(post(c, "/settings/name", csrf, op="reset", name=""))
    assert install_name.is_default() and len(log(db, install_name.SCOPE)) == 2


def test_name_is_escaped_on_the_page(page):
    p, c, db = page
    csrf, _ = login(p, c)
    post(c, "/settings/name", csrf, name=NASTY[:40])
    html = c.get("/").text
    assert "<script" not in html and "&lt;script&gt;" in html


def test_open_link_page_shows_it_read_only(backend, seeded_db, cfg):
    install_name.set_name("Office laptop", seeded_db)
    p = Page(seeded_db, PORT, open_link=True, settings="read")
    c = TestClient(create_app(p), base_url=BASE, follow_redirects=False)
    html = c.get("/").text
    sec = html.split("<h2>Settings</h2>", 1)[1]
    assert "<b>Office laptop</b>." in sec and "/settings/name" not in sec
    csrf = CSRF.search(html).group(1)
    before = log(seeded_db)
    assert flash(post(c, "/settings/name", csrf, name="Hijacked")) == review_page.READ_ONLY
    assert install_name.get() == "Office laptop" and log(seeded_db) == before


def test_tunnel_page_shows_the_name_but_cannot_change_it(seeded_db, cfg):
    from flowstate.review_tunnel import PATH
    install_name.set_name("Office laptop", seeded_db)
    p = Page(seeded_db, PORT, base=PATH, client="tunnel", stylesheet=f"{PATH}/page.css")
    assert p.settings == "off"
    html = p.render("x")
    assert "· Office laptop</span></h1>" in html and "<h2>Settings</h2>" not in html
    assert p.set_display_name({"name": "Hijacked"}, "s") == review_page.READ_ONLY
    assert install_name.get() == "Office laptop"


# ---------------------------------------------------------------- phone pages
def test_phone_list_gets_the_name_after_the_grant(backend, relay, cfg):
    db = backend.backend.db_path
    install_name.set_name("Office laptop", db)
    pile(db, 1)
    c = _phone_client(db)
    shell = c.get("/review").text
    assert "Office laptop" not in shell                       # not before the grant checks out
    grant, _n = _grant(relay, db)
    body = c.post("/review/open", content=json.dumps({"grant": grant}), headers=JSON_H).json()
    assert body["name"] == "Office laptop"
    js = c.get("/review/assets/review.js").text
    assert "innerHTML" not in js and '$("foot").textContent = where' in js


def test_approve_page_gets_the_name_and_sets_it_as_text(backend):
    from flowstate import approve_page
    assert "innerHTML" not in approve_page.JS and "r.j.name" in approve_page.JS
    assert '"name": install_name.get()' in __import__("inspect").getsource(approve_page)


# ---------------------------------------------------------------- the brief
def test_brief_never_carries_it(backend, cfg):
    """Like the other settings, no AI app reads it through FlowState: not in the brief."""
    db = backend.backend.db_path
    install_name.set_name("Office laptop", db)
    pile(db, 1)
    out = call(backend, "start_here", {"project": "Alpha"})
    assert "Alpha" in json.dumps(out) and "Office laptop" not in json.dumps(out)


# ---------------------------------------------------------------- the relay hello
def test_hello_carries_the_name_and_a_rename_is_sent(tmp_path, cfg):
    pytest.importorskip("cryptography")
    import asyncio
    import time
    from flowstate.relay.tunnel import Backoff, TunnelAgent
    from relay_fakes import FakeTunnelRelay, TestCA, TUNNEL_HOST
    ca = TestCA(tmp_path / "ca")
    relay = FakeTunnelRelay(ca, {"tok-" + "a" * 40: "ab12cd"}, heartbeat=0.1).start()
    try:
        names = ["Office laptop"]
        agent = TunnelAgent(host="127.0.0.1", port=relay.tunnel_port, sni=TUNNEL_HOST,
                            token="tok-" + "a" * 40, handle="ab12cd",
                            client_ctx=ssl.create_default_context(cafile=str(ca.cert_file)),
                            server_ctx=lambda: None, local_port=lambda: None, peer_map={},
                            min_heartbeat=0.1, backoff=Backoff(base=0.05, cap=0.2, rand=lambda: 1.0),
                            display_name=lambda: names[0])

        async def main():
            stop = asyncio.Event()
            task = asyncio.create_task(agent.run(stop))
            t0 = time.monotonic()
            while not relay.hellos and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.02)
            names[0] = "Home desk"
            while not relay.names and time.monotonic() - t0 < 10:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)               # more pings: the same name isn't sent again
            stop.set()
            await asyncio.wait_for(task, 5)
        asyncio.run(main())
        assert relay.hellos[0]["name"] == "Office laptop" and relay.hellos[0]["handle"] == "ab12cd"
        assert relay.names == ["Home desk"]
    finally:
        relay.stop()


def test_hello_uses_the_config_name_by_default(cfg):
    from flowstate.relay import tunnel
    assert tunnel._display_name() == "Main FlowState"
    install_name.set_name("Office laptop")
    assert tunnel._display_name() == "Office laptop"


# ---------------------------------------------------------------- never through a tool
def test_no_tool_or_url_uses_it():
    import inspect
    from flowstate import approvals, brief, server, surface, tooldefs, waiting
    for mod in (server, surface, tooldefs, brief, waiting):
        src = inspect.getsource(mod)
        assert "install_name" not in src and "display_name" not in src, mod.__name__
    assert "install_name" not in inspect.getsource(approvals)       # links: the handle only
    from flowstate import review_phone, approve_page
    for mod in (review_phone, approve_page):
        assert "set_name" not in inspect.getsource(mod)
    assert re.search(r"Route\(f\"/settings/\{name\}\"", inspect.getsource(review_page))
