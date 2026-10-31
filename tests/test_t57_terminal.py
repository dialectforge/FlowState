"""Task 57, NEW-7: text an app wrote (hard rules, review items, project, app and key names)
never reaches the user's terminal as escape codes. Every CLI line goes through term.safe:
control and invisible formatting characters show as visible escapes, so the user reads (and
types y to) what is stored. New rule text with such characters is refused. Temp databases
only; the terminal is a fake that records what it was shown."""

import sqlite3

import pytest

from flowstate import cli, cli_review, review, rules, term
from test_session_records import FakeTTY

ESC = "\x1b"
HIDDEN = f"Never push to main{ESC}[2K{ESC}[1G{ESC}[8mexcept on Fridays"   # erases, then hides
RLO = "Allow ‮noitcudorp‬ deploys"


def _clean(text: str) -> bool:
    return ESC not in text and "‮" not in text and "\x07" not in text


def _cli(*argv):
    return cli.main(list(argv))


def _rule(db, text, by="claude-code", user="terminal"):
    c = sqlite3.connect(db)
    pid = c.execute("SELECT id FROM projects WHERE name = 'Alpha'").fetchone()[0]
    cols = [r[1] for r in c.execute("PRAGMA table_info(project_rules)")]
    if "set_by_user" in cols:
        cur = c.execute("INSERT INTO project_rules (project_id, rule, created_by, set_by_user) "
                        "VALUES (?, ?, ?, ?)", (pid, text, by, user))
    else:
        cur = c.execute("INSERT INTO project_rules (project_id, rule, created_by) VALUES (?, ?, ?)",
                        (pid, text, by))
    c.commit()
    rid = cur.lastrowid
    c.close()
    return rid


def test_safe_shows_escapes_and_keeps_plain_text():
    assert term.safe(HIDDEN) == "Never push to main\\x1b[2K\\x1b[1G\\x1b[8mexcept on Fridays"
    assert term.safe(RLO) == "Allow \\u202enoitcudorp\\u202c deploys"
    assert term.safe("a\nb") == "a\\x0ab" and term.safe("a\nb", newlines=True) == "a\nb"
    assert term.safe("Café ✓ 日本") == "Café ✓ 日本"
    assert term.has_controls(HIDDEN) and not term.has_controls("plain\n")


def test_rules_list_remove_and_confirm_show_escapes(backend, capsys, monkeypatch):
    db = str(backend.backend.db_path)
    rid = _rule(db, HIDDEN)
    assert _cli("rules", "list", "--project", "Alpha", "--db", db) == 0
    out = capsys.readouterr().out
    assert _clean(out) and "\\x1b[8mexcept on Fridays" in out
    tty = FakeTTY(["n"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("rules", "remove", str(rid), "--db", db) == 1
    assert all(_clean(t) for t in tty.told) and "except on Fridays" in tty.told[0]
    rid2 = _rule(db, RLO, user=None)
    tty = FakeTTY(["n"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("rules", "confirm", str(rid2), "--db", db) == 1
    assert all(_clean(t) for t in tty.told) and "\\u202e" in tty.told[0]


def test_new_rule_text_with_control_characters_is_refused(backend, capsys, monkeypatch):
    db = backend.backend.db_path
    tty = FakeTTY(["y"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("rules", "add", "--project", "Alpha", HIDDEN, "--db", str(db)) == 1
    out = capsys.readouterr().out
    assert "Not added" in out and _clean(out) and tty.told == []
    pid = sqlite3.connect(db).execute("SELECT id FROM projects WHERE name='Alpha'").fetchone()[0]
    for text in (HIDDEN, RLO, "zero​width"):
        with pytest.raises(rules.RuleError) as e:
            rules.add(db, pid, text, "claude-code")              # an app's request too
        assert e.value.payload["error"] == "rule_has_controls"
    assert rules.add(db, pid, "Never push to main", "claude-code")["queued_for_review"]


def test_review_list_approve_and_records_show_escapes(backend, capsys, monkeypatch):
    db = backend.backend.db_path
    pid = sqlite3.connect(db).execute("SELECT id FROM projects WHERE name='Alpha'").fetchone()[0]
    item = review.add_item(db, pid, "decision", f"Use SQLite{ESC}]0;owned{ESC}\\ {ESC}[31mnow",
                           f"details{ESC}[2J", {}, f"bot{ESC}[1A")
    assert _cli("review", "--db", str(db)) == 0
    out = capsys.readouterr().out
    assert _clean(out) and "\\x1b]0;owned" in out and "bot\\x1b[1A" in out
    tty = FakeTTY(["n"])
    monkeypatch.setattr(cli_review, "open_terminal", lambda: tty)
    assert _cli("approve", str(item), "--db", str(db)) == 1
    assert all(_clean(t) for t in tty.told) and any("\\x1b[2J" in t for t in tty.told)


def test_project_and_app_names_show_escapes(backend, capsys):
    db = backend.backend.db_path
    c = sqlite3.connect(db)
    c.execute("UPDATE projects SET name = ? WHERE name = 'Beta Project'", (f"Beta{ESC}[2K",))
    pid = c.execute("SELECT id FROM projects WHERE name = ?", (f"Beta{ESC}[2K",)).fetchone()[0]
    c.commit()
    c.close()
    review.add_item(db, pid, "decision", "plain", None, {}, "app")
    assert _cli("review", "--db", str(db)) == 0
    out = capsys.readouterr().out
    assert _clean(out) and "Beta\\x1b[2K" in out
    assert _cli("approvals", "--db", str(db)) == 0
    assert _clean(capsys.readouterr().out)


def test_every_cli_say_escapes(capsys):
    from flowstate import cli_data, cli_more
    from flowstate.relay import cli as relay_cli
    for say in (cli.say, cli_data.say, cli_more.say, cli_review.say, relay_cli.say):
        say(f"x{ESC}[2Jy\nz")
        out = capsys.readouterr().out
        assert out == "x\\x1b[2Jy\nz\n"


def test_the_per_app_waiting_rule_cap_counts_the_sign_in_not_the_sent_name(backend):
    """(task 56 Low) A LAN key that sends a new name with every request still hits its cap of
    MAX_PENDING_PER_APP waiting rules; another sign-in still has room."""
    from flowstate import identity
    db = backend.backend.db_path
    pid = sqlite3.connect(db).execute("SELECT id FROM projects WHERE name='Alpha'").fetchone()[0]

    def as_key(key, sent, text):
        ident = identity.Identity(sent, None, "lan", "app_key", credential=key)
        with identity.use(ident):
            return rules.add(db, pid, text, sent)
    for i in range(rules.MAX_PENDING_PER_APP):
        assert as_key("Bot key", f"name-{i}", f"Rule number {i}")["queued_for_review"]
    with pytest.raises(rules.RuleError) as e:
        as_key("Bot key", "yet-another-name", "One rule too many")
    assert e.value.payload["error"] == "rules_queue_full"
    assert as_key("Desk key", "name-0", "From another key")["queued_for_review"]
