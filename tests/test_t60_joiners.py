"""Task 60, L-1 / L-2: the joiners ZWJ (U+200D) and ZWNJ (U+200C) are ordinary text. They join
emoji (family, rainbow flag) and shape Persian and Hindi words, and can't move the cursor or hide
anything, so term.safe prints them as they are and hard rules may hold them. Everything task 57
blocked otherwise (C0 / C1 controls, ESC sequences, U+200B, direction marks, bidi overrides and
isolates, the BOM) is still escaped in output and refused in rules. Temp databases only."""

import asyncio
import json
import sqlite3
import subprocess
import sys

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from conftest import server_env
from flowstate import cli, cli_review, rules, term
from test_session_records import FakeTTY

FAMILY = "\U0001F468‍\U0001F469‍\U0001F467"          # man ZWJ woman ZWJ girl
RAINBOW = "\U0001F3F3️‍\U0001F308"                     # white flag VS16 ZWJ rainbow
PERSIAN = "می‌خواهم"     # "I want", with a ZWNJ
JOINED = (FAMILY, RAINBOW, PERSIAN)

ESC = "\x1b"
DANGEROUS = {                     # text -> the escape term.safe must show for it
    "Allow ‮noitcudorp deploys": "\\u202e",
    f"Never push{ESC}[2K to main": "\\x1b[2K",
    "zero​width rule": "\\u200b",
    "bell\x07 rule": "\\x07",
}


def _pid(db, name="Alpha"):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT id FROM projects WHERE name = ?", (name,)).fetchone()[0]
    finally:
        c.close()


def _call(S, name, args, client="Cursor"):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name=client, version="1")) as c:
            return await c.call_tool(name, args)
    return json.loads(asyncio.run(go()).content[0].text)


def test_safe_keeps_joined_emoji_and_zwnj_text():
    for text in JOINED:
        assert term.safe(text) == text and term.safe(text, newlines=True) == text
        assert not term.has_controls(text)
    line = f"Family {FAMILY} trip {RAINBOW} {PERSIAN}"
    assert term.safe(line) == line


def test_dangerous_characters_are_still_escaped():
    for text, shown in DANGEROUS.items():
        assert shown in term.safe(text) and term.has_controls(text)
    for ch in ("​", "‎", "‏", "‪", "‮", "⁦", "⁩", "﻿",
               "\x00", "\x1b", "\x7f", "\x9b"):
        assert term.has_controls(f"a{ch}b") and ch not in term.safe(f"a{ch}b"), ascii(ch)


def test_review_list_prints_a_joined_emoji_project_name(backend, seeded_db, capsys):
    """The real CLI, in this process and as `python -m flowstate` with temp folders only."""
    from flowstate import review
    name = f"Family {FAMILY} trip"
    c = sqlite3.connect(seeded_db)
    c.execute("UPDATE projects SET name = ? WHERE name = 'Beta Project'", (name,))
    c.commit()
    c.close()
    review.add_item(seeded_db, _pid(seeded_db, name), "decision", f"Book {RAINBOW} {PERSIAN}",
                    None, {}, "app")
    assert cli.main(["review", "--db", str(seeded_db)]) == 0
    out = capsys.readouterr().out
    assert name in out and f"Book {RAINBOW} {PERSIAN}" in out and "\\u200" not in out
    p = subprocess.run([sys.executable, "-m", "flowstate", "review", "--db", str(seeded_db)],
                       env=server_env(seeded_db, PYTHONIOENCODING="utf-8"), capture_output=True,
                       text=True, encoding="utf-8", timeout=60)
    assert p.returncode == 0, p.stderr
    assert name in p.stdout and PERSIAN in p.stdout and "\\u200" not in p.stdout


def test_rules_add_accepts_joined_text_in_the_terminal_and_from_an_app(backend, seeded_db,
                                                                       capsys, monkeypatch):
    db = seeded_db
    pid = _pid(db)
    for text in JOINED:
        tty = FakeTTY(["y"])
        monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
        rule = f"Keep the {text} label"
        assert cli.main(["rules", "add", "--project", "Alpha", rule, "--db", str(db)]) == 0
        assert "Not added" not in capsys.readouterr().out
        assert any(rule in t for t in tty.told)
        assert rules.add(db, pid, f"App rule {text}", "claude-code")["queued_for_review"]
        r = _call(backend, "manage_rules", {"action": "add", "project": "Alpha",
                                            "rule": f"Tool rule {text}"})
        assert r.get("queued_for_review") and "error" not in r, r
    stored = [r["rule"] for r in rules.list_rules(db, pid)["rules"]]
    assert [f"Keep the {t} label" for t in JOINED] == stored
    assert cli.main(["rules", "list", "--project", "Alpha", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert all(t in out for t in JOINED) and "\\u200" not in out


def test_rules_still_refuse_dangerous_characters(backend, seeded_db, capsys, monkeypatch):
    db = seeded_db
    pid = _pid(db)
    for text, shown in DANGEROUS.items():
        tty = FakeTTY(["y"])
        monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
        assert cli.main(["rules", "add", "--project", "Alpha", text, "--db", str(db)]) == 1
        out = capsys.readouterr().out
        assert "Not added" in out and shown in out and tty.told == []
        with pytest.raises(rules.RuleError) as e:
            rules.add(db, pid, text, "claude-code")
        assert e.value.payload["error"] == "rule_has_controls"
        r = _call(backend, "manage_rules", {"action": "add", "project": "Alpha", "rule": text})
        assert r.get("error") == "rule_has_controls", r
    assert rules.list_rules(db, pid)["rules"] == []
