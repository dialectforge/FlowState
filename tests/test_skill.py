"""The flowstate Agent Skill (Phase D3): agentskills.io format, the habits it teaches, and
the setup script's install step (fake home folders in tmp only)."""

import re
import sys
from pathlib import Path

from conftest import REPO

sys.path.insert(0, str(REPO))
import setup_flowstate as setup  # noqa: E402
from test_setup_script import REAL_HOME, run_setup  # noqa: E402

SKILL = REPO / "skills" / "flowstate" / "SKILL.md"


def _front_matter(text: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with YAML front matter"
    fm, key, out = m.group(1), None, {}
    for line in fm.splitlines():   # tiny YAML subset: key: value, key: > folded, nested map
        if re.match(r"^[a-z_]+:", line):
            key, _, val = line.partition(":")
            out[key] = val.strip()
        elif key and line.startswith("  "):
            out[key] = (out[key] + " " + line.strip()).strip(" >")
    return out


def test_skill_follows_the_spec():
    text = SKILL.read_text(encoding="utf-8").replace("\r\n", "\n")
    fm = _front_matter(text)
    assert fm["name"] == "flowstate" == SKILL.parent.name
    assert re.fullmatch(r"[a-z0-9-]{1,64}", fm["name"])
    desc = fm["description"]
    assert 100 < len(desc) <= 1024 and "Use at the start" in desc
    assert "version" in fm["metadata"]
    assert len(text.splitlines()) < 500
    assert not any(p.suffix in (".py", ".sh", ".ps1", ".js") for p in SKILL.parent.rglob("*"))


def test_skill_teaches_the_habits():
    body = SKILL.read_text(encoding="utf-8").lower()
    for habit in ("start_here", "log_work", "link(action=\"add\"", "never approve or reject review items yourself",
                  "never change it", "user_request", "summary", "secret", "replaces="):
        assert habit in body, habit


def test_setup_installs_skill_with_backup(tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    assert home.resolve() != REAL_HOME.resolve()
    agents = home / ".agents" / "skills" / "flowstate"
    claude = home / ".claude" / "skills" / "flowstate"
    agents.mkdir(parents=True)
    (agents / "SKILL.md").write_text("old copy", encoding="utf-8")
    db = home / "fs.db"

    r = run_setup(home, "--clients", "none", "--db", str(db), "--no-offsite", "--install-skill")
    assert r.returncode == 0, r.stdout + r.stderr
    for d in (agents, claude):
        assert (d / "SKILL.md").read_text(encoding="utf-8") == SKILL.read_text(encoding="utf-8")
        assert (d / "references" / "record-types.md").exists()
    saved = list((home / ".agents" / "flowstate-skill-backups").iterdir())
    assert len(saved) == 1 and (saved[0] / "SKILL.md").read_text(encoding="utf-8") == "old copy"
    assert not list((home / ".agents" / "skills").glob("flowstate?*"))  # backup not a 2nd skill

    again = run_setup(home, "--clients", "none", "--db", str(db), "--no-offsite", "--install-skill")
    assert again.returncode == 0 and again.stdout.count(": unchanged") >= 2
    assert len(list((home / ".agents" / "flowstate-skill-backups").iterdir())) == 1


def test_setup_without_flag_does_not_install(tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    r = run_setup(home, "--clients", "none", "--db", str(home / "fs.db"), "--no-offsite")
    assert r.returncode == 0 and "[skill] not installed" in r.stdout
    assert not (home / ".agents").exists() and not (home / ".claude").exists()
    dry = run_setup(home, "--clients", "none", "--db", str(home / "fs.db"), "--no-offsite",
                    "--install-skill", "--dry-run")
    assert "would be installed (dry run)" in dry.stdout and not (home / ".agents").exists()


def test_skill_targets_follow_home(tmp_path):
    places = setup.Places(tmp_path)
    assert setup.skill_targets(places) == [tmp_path / ".agents" / "skills" / "flowstate",
                                           tmp_path / ".claude" / "skills" / "flowstate"]
