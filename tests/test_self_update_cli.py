"""(task 41) `flowstate update self-update [on|off]`: config.json "self_update" from the terminal,
written and logged by the same updates.set_self_update the review page's Settings use. No
argument shows the value and where it comes from; the .flowstate-no-self-update file still wins
and is never deleted. Temp DBs and sandboxed config folders only."""

import re
import sqlite3

import pytest
from starlette.testclient import TestClient

from flowstate import approvals, cli, paths, review_page, selfupdate, updates
from flowstate.review_page import HOST, Page, create_app


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FLOWSTATE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr(selfupdate, "FOLDER", tmp_path / "clone")   # where the opt-out file goes
    (tmp_path / "clone").mkdir()
    return tmp_path / "config" / "config.json"


@pytest.fixture
def opt_out(cfg, tmp_path):
    f = tmp_path / "clone" / selfupdate.OPT_OUT_FILE
    f.write_text("")
    return f


def run(*argv) -> int:
    a = cli.build_parser().parse_args(["update", "self-update", *argv])
    return a.fn(a)


def log(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT scope, old_mode, new_mode, client, caller, user_request, app, "
                            "machine, identity_source FROM review_mode_log "
                            "WHERE scope = 'config:self_update' ORDER BY id").fetchall()
    finally:
        conn.close()


def test_parser_and_help(capsys):
    a = cli.build_parser().parse_args(["update", "self-update", "off"])
    assert a.action == "self-update" and a.sub == "off"
    a = cli.build_parser().parse_args(["update", "self-update"])
    assert a.action == "self-update" and a.sub is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--help"])
    assert "self-update [on|off]" in " ".join(capsys.readouterr().out.split())


def test_off_then_on(cfg, seeded_db, capsys):
    assert run("off", "--db", str(seeded_db)) == 0
    out = capsys.readouterr().out
    assert "updated by hand" in out and "self-update on" in out
    assert updates.self_update_allowed() is False and '"self_update": false' in cfg.read_text()
    assert run("on", "--db", str(seeded_db)) == 0
    assert "allows `flowstate update` again" in capsys.readouterr().out
    assert updates.self_update_allowed() and "self_update" not in cfg.read_text()


def test_status_default_config_and_file(cfg, seeded_db, tmp_path, capsys):
    assert run() == 0
    out = capsys.readouterr().out
    assert "Updates by `flowstate update`: allowed (the default" in out
    run("off", "--db", str(seeded_db))
    capsys.readouterr()
    assert run() == 0
    out = capsys.readouterr().out
    assert ": by hand (config.json says \"self_update\": false" in out and str(cfg) in out
    f = tmp_path / "clone" / selfupdate.OPT_OUT_FILE
    f.write_text("")
    run("on", "--db", str(seeded_db))
    capsys.readouterr()
    assert run() == 0
    out = capsys.readouterr().out
    assert ": by hand (the file " in out and str(f) in out and "the file wins" in out
    assert updates.self_update_status()["source"] == "file"


def test_on_with_the_file_explains_and_keeps_it(opt_out, seeded_db, capsys):
    paths.save_config({"self_update": False})
    assert run("on", "--db", str(seeded_db)) == 0
    out = capsys.readouterr().out
    assert "still keeps this folder updated by hand" in out and str(opt_out) in out
    assert "delete that file yourself" in out and "rm " in out and "del " in out
    assert opt_out.exists()                                    # never deleted
    assert updates.self_update_allowed()                       # config.json changed anyway
    assert updates.self_update_status()["allowed"] is False    # but the file still wins


def test_bad_word_changes_nothing(cfg, seeded_db, capsys):
    before = log(seeded_db)
    assert run("now", "--db", str(seeded_db)) == 2
    assert not cfg.exists() and log(seeded_db) == before


def test_logged_like_the_page(cfg, seeded_db, backend):
    assert run("off", "--db", str(seeded_db)) == 0
    scope, old, new, client, caller, words, app, machine, source = log(seeded_db)[-1]
    assert (old, new) == ("allowed", "by hand")
    assert client == caller == app == "command-line" and words == approvals.TERMINAL_WORDS
    assert machine and source == "self_reported"
    # the page's own change goes through the same writer: same row, its own who/how
    p = Page(seeded_db, 8770)
    c = TestClient(create_app(p), base_url=f"http://{HOST}:8770", follow_redirects=False)
    assert c.get("/?key=" + p.launch_key).status_code == 303
    html = c.get("/").text
    csrf = re.search(r"""name=["']csrf["'] value=["']([^"']+)["']""", html).group(1)
    assert "Updates by flowstate update" in html.split("Recent changes", 1)[1]   # CLI row listed
    assert "flowstate update self-update on|off" in html                         # the row's note
    r = c.post("/settings/self-update", data={"csrf": csrf, "state": "on"},
               headers={"origin": f"http://{HOST}:8770"})
    assert r.status_code == 303
    row = log(seeded_db)[-1]
    assert row[1:3] == ("by hand", "allowed") and row[3] == "review-page"
    assert row[5] == review_page.SETTING_WORDS and len(row) == len(log(seeded_db)[0])


def test_no_database_no_log_and_no_file_made(cfg, tmp_path):
    missing = tmp_path / "nope.db"
    assert updates.cli_self_update("off", missing, lambda m: None) == 0
    assert not missing.exists() and updates.self_update_allowed() is False
