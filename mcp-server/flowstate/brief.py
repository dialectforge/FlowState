"""Project resolution and the start-of-conversation brief (standard cap 4,000 characters;
quick 1,500, deep 12,000; hard rules are never cut)."""

import re
import sqlite3
from pathlib import Path
from typing import Optional, Union

from . import dbconn
from .autosession import SKIP_TAG

BRIEF_CAP = 4000

_PRIORITY = "CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
_SEVERITY = "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END"
_OPEN_TODO = "(status IS NULL OR status IN ('pending', 'in_progress', 'blocked'))"
_OPEN_PROBLEM = "(status IS NULL OR status NOT IN ('solved', 'wont_fix', 'closed'))"


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _one_line(s: Optional[str], n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 3].rstrip() + "..."


# ---------------------------------------------------------------- projects
def resolve_project(db_path: Path, project: Union[str, int, None] = None,
                    folder: Optional[str] = None, fuzzy: bool = True) -> dict:
    """Find one project. Returns {"project": row-dict} or {"candidates": [...], "reason": ...}.

    Order: numeric id -> exact name (case-insensitive) -> normalised name ->
    folder basename (normalised) -> (fuzzy only) unique substring match.
    """
    conn = _conn(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, name, status, description FROM projects ORDER BY updated_at DESC, id DESC")]
    finally:
        conn.close()

    def pick(matches: list, why: str) -> Optional[dict]:
        if len(matches) == 1:
            return {"project": matches[0]}
        if len(matches) > 1:
            return {"candidates": [m["name"] for m in matches], "reason": f"ambiguous {why}"}
        return None

    wanted = project
    if isinstance(wanted, str) and wanted.strip().lstrip("#").isdigit():
        wanted = int(wanted.strip().lstrip("#"))
    if isinstance(wanted, int) and not isinstance(wanted, bool):
        hit = [r for r in rows if r["id"] == wanted]
        return pick(hit, "id") or {"candidates": _active_names(rows),
                                   "reason": f"no project with id {wanted}"}

    keys = []
    if isinstance(wanted, str) and wanted.strip():
        keys.append(("name", wanted.strip()))
    if folder and str(folder).strip():
        base = re.split(r"[\\/]+", str(folder).strip().rstrip("\\/"))[-1]
        if base:
            keys.append(("folder", base))
    if not keys:
        return {"candidates": _active_names(rows), "reason": "no project given"}

    for kind, key in keys:
        r = (pick([x for x in rows if x["name"].lower() == key.lower()], kind)
             or pick([x for x in rows if _norm(x["name"]) == _norm(key)], kind))
        if r:
            return r
        if fuzzy and len(_norm(key)) >= 3:
            r = pick([x for x in rows if _norm(key) in _norm(x["name"])], kind)
            if r:
                return r
    return {"candidates": _active_names(rows), "reason": f"no project matches '{keys[0][1]}'"}


def _active_names(rows: list, limit: int = 60) -> list:
    active = [r["name"] for r in rows if r.get("status") in (None, "active")]
    rest = [r["name"] for r in rows if r.get("status") not in (None, "active")]
    return (active + rest)[:limit]


# ---------------------------------------------------------------- brief
class _Budget:
    def __init__(self, cap: int):
        self.cap = cap
        self.lines: list[str] = []
        self.used = 0

    def add(self, line: str) -> bool:
        need = len(line) + 1
        if self.used + need > self.cap:
            return False
        self.lines.append(line)
        self.used += need
        return True

    def remaining(self) -> int:
        return self.cap - self.used

    def text(self) -> str:
        return "\n".join(self.lines)


# Brief sizes (E4). standard is the brief as it always was; quick is for a fast check-in;
# deep is for picking a project back up after a long break. Section = (max items, max chars).
SIZES = {
    "quick": {"cap": 1500, "todos": (5, 520), "problems": (3, 260), "changes": (0, 0),
              "decisions": (2, 260), "learnings": (0, 0), "linked": False, "stale": False},
    "standard": {"cap": BRIEF_CAP, "todos": (15, 1100), "problems": (6, 500), "changes": (8, 900),
                 "decisions": (5, 450), "learnings": (8, 900), "linked": True, "stale": True},
    "deep": {"cap": 12000, "todos": (40, 3600), "problems": (15, 1500), "changes": (20, 2200),
             "decisions": (12, 1500), "learnings": (20, 2600), "linked": True, "stale": True},
}
SIZE_NAMES = tuple(SIZES)
_MORE = " ...(handoff list shows it all)"


def build_brief(db_path: Path, project: dict, session: Optional[dict] = None,
                cap: Optional[int] = None, size: str = "standard",
                shown: Optional[list] = None, since: Optional[str] = None) -> str:
    """Plain-text brief: hard rules, handoff notes, open todos, open problems, recent changes
    and decisions, key learnings. Sections are filled within budgets; the result never
    exceeds the size's cap, except that the hard rules are never cut: if they alone would not
    fit, the cap grows just enough to show them (they are bounded at 2,000 characters).
    `shown` (a list) receives "type:id" of every record the brief shows (the run ledger);
    `since` is the one "since your last visit" line (ledger.since_line), if any."""
    shown = shown if shown is not None else []
    from . import claims, conflicts, handoffs, links, priority, provenance, review_mode, rules
    size = size if size in SIZES else "standard"
    prof = SIZES[size]
    cap = cap or prof["cap"]
    pid, name = project["id"], project["name"]
    conn = _conn(db_path)
    try:
        q = lambda sql, *a: [dict(r) for r in conn.execute(sql, a)]
        n_todo, n_prob, n_chg, n_dec, n_learn = (prof[k][0] for k in
                                                 ("todos", "problems", "changes", "decisions", "learnings"))
        rule_lines = rules.brief_lines(conn, pid)
        handoff_lines = handoffs.brief_lines(conn, pid)
        todo_total = q(f"SELECT COUNT(*) n FROM todos WHERE project_id=? AND {_OPEN_TODO}", pid)[0]["n"]
        todos = q(f"""SELECT id, title, priority, status FROM todos WHERE project_id=? AND {_OPEN_TODO}
                      ORDER BY {_PRIORITY}, CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, id DESC
                      LIMIT ?""", pid, n_todo)
        claimed = claims.active(conn, [t["id"] for t in todos])
        if claimed:
            mine, now = claims.me()["holder"], claims.utcnow()
            claimed = {k: claims.describe(r, mine, now) for k, r in claimed.items()}
        prob_total = q(f"""SELECT COUNT(*) n FROM problems p JOIN components c ON c.id=p.component_id
                           WHERE c.project_id=? AND {_OPEN_PROBLEM.replace('status', 'p.status')}""",
                       pid)[0]["n"]
        problems = q(f"""SELECT p.id, p.title, p.severity FROM problems p
                         JOIN components c ON c.id=p.component_id
                         WHERE c.project_id=? AND {_OPEN_PROBLEM.replace('status', 'p.status')}
                         ORDER BY {_SEVERITY.replace('severity', 'p.severity')}, p.id DESC LIMIT ?""",
                     pid, n_prob)
        changes = q("""SELECT ch.id, ch.created_at, c.name comp, ch.field_name, ch.new_value, ch.reason
                       FROM changes ch JOIN components c ON c.id=ch.component_id
                       WHERE c.project_id=? ORDER BY ch.created_at DESC, ch.id DESC LIMIT ?""",
                    pid, n_chg)
        decisions = q("""SELECT id, created_at, user_prompt_summary, key_decisions FROM conversations
                         WHERE project_id=? AND key_decisions IS NOT NULL
                         ORDER BY created_at DESC, id DESC LIMIT ?""", pid, n_dec + 20)
        # A decision replaced by a newer one is no longer true: show only the current one.
        replaced = links.superseded_decisions(conn)
        replaces = {new: old for old, (new, _since) in replaced.items()}
        decisions = [d for d in decisions if d["id"] not in replaced][:n_dec]
        linked = links.brief_lines(conn, pid) if prof["linked"] else []
        alerts = links.alert_lines(conn, pid)
        conflict_line = conflicts.brief_line(conn, pid)
        from . import conflict_cards
        card_line = conflict_cards.brief_line(conn, pid)
        mode_line = review_mode.brief_line(db_path, pid) if review_mode.available(conn) else None
        from .deletes import recent_line
        deleted_line = recent_line(conn, pid, name)
        learnings = q("""SELECT id, category, insight, verified FROM learnings WHERE project_id=?
                         ORDER BY verified DESC, created_at DESC, id DESC LIMIT ?""", pid, n_learn)
        meta = {k: provenance.get(conn, k, [r["id"] for r in rows]) for k, rows in
                (("todo", todos), ("change", changes), ("decision", decisions), ("learning", learnings))}
        # Auto-captured summaries aren't final until the user approves them; a session that
        # saved nothing new (autosession.SKIP_TAG) isn't worth a line.
        last = q("""SELECT started_at, ended_at, summary FROM sessions
                    WHERE project_id=? AND ended_at IS NOT NULL AND summary IS NOT NULL
                    AND summary NOT LIKE '[auto-captured%' AND summary NOT LIKE ?
                    ORDER BY ended_at DESC, id DESC LIMIT 1""", pid, SKIP_TAG + "%")
        has_queue = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                 "AND name='review_items'").fetchone() is not None
        pending = q("SELECT COUNT(*) n FROM review_items WHERE project_id=? AND status='pending'",
                    pid)[0]["n"] if has_queue else None
        has_meta = provenance.available(conn)
    finally:
        conn.close()
    stale_line = None
    if prof["stale"] and has_meta:
        try:
            stale_line = provenance.brief_line(db_path, pid)
        except Exception:  # noqa: BLE001 - a nice-to-have line never breaks the brief
            stale_line = None

    head = f"FlowState brief: {name} (project #{pid}, {project.get('status') or 'active'})"
    if size != "standard":
        head += f" [{size}]"
    rules_len = sum(len(x) + 1 for x in [head] + rule_lines)
    # Hard rules are never cut; everything else gives way. If the rules alone don't fit the
    # size, the cap grows just enough for them plus a little (a handoff note, the footer).
    cap = max(cap, rules_len + 600)
    b = _Budget(cap - 40)  # room for the final "(truncated)" marker, just in case
    b.add(head)
    for line in rule_lines:            # always, in full (bounded when they are added)
        b.lines.append(line)
        b.used += len(line) + 1
        _note(shown, "rule", _RULE_ID.match(line))
    if handoff_lines:
        # right after the rules; trimmed only so that some room is left for the rest, but
        # never squeezed out entirely
        room = max(b.remaining() - min(1200, b.cap // 3), min(b.remaining(), 500))
        for line in handoff_lines:
            if len(line) + 1 > room:
                cut = room - 1 - len(_MORE)
                if cut > 60:
                    b.add(line[:cut].rstrip() + _MORE)
                break
            if b.add(line):
                _note(shown, "handoff", _HANDOFF_ID.match(line))
            room -= len(line) + 1
    if project.get("description"):
        b.add(_one_line(project["description"], 200))
    if session:
        b.add(f"Session #{session['session_id']} is open for you (opened automatically; "
              f"closes itself when idle). Log as you work: "
              f"log_work(project=\"{name}\", kind=decision|change|problem|learning|todo, summary=...).")
    if since:
        b.add(since)
    if pending is not None:
        where = ""
        if pending:   # the local page (started on first need) and the phone link (waiting.py)
            from .waiting import where_line
            where = where_line(db_path)
        b.add(f"Review queue: {pending} item{'s' if pending != 1 else ''} waiting for the user's OK"
              + (" (manage_review action=list shows them; approve or reject only when the user asks)."
                 + where if pending else "."))
    if mode_line:
        b.add(mode_line)
    if deleted_line:
        b.add(deleted_line)
    for line in alerts:
        b.add(line)
    if conflict_line:
        b.add(conflict_line)
    if card_line:
        b.add(card_line)
    if last:
        b.add(f"Last session ({(last[0]['ended_at'] or '')[:10]}): {_one_line(last[0]['summary'], 280)}")
    if stale_line:
        b.add(_one_line(stale_line, 300))

    def section(title: str, items: list, total: int, fmt, share: int, kind: str):
        if not items:
            return
        stop = b.used + share
        header = f"\n{title} ({len(items)} of {total}):" if total > len(items) else f"\n{title}:"
        if not b.add(header):
            return
        for it in items:
            line = fmt(it)
            if b.used + len(line) + 1 > stop or not b.add(line):
                break
            shown.append(f"{kind}:{it['id']}")

    tag = lambda kind, rid: provenance.tag(meta[kind].get(rid))
    section("Open todos", todos, todo_total,
            lambda t: f"- [{priority.label(t['priority'])}] #{t['id']} {_one_line(t['title'], 90)}"
                      + (f" ({t['status']})" if t["status"] in ("in_progress", "blocked") else "")
                      + (f" ({claimed[t['id']]})" if t["id"] in claimed else "")
                      + tag("todo", t["id"]),
            prof["todos"][1], "todo")
    section("Open problems", problems, prob_total,
            lambda p: f"- [{p['severity'] or 'medium'}] #{p['id']} {_one_line(p['title'], 90)}",
            prof["problems"][1], "problem")
    section("Recent changes", changes, len(changes),
            lambda c: f"- {(c['created_at'] or '')[:10]} {_one_line(c['comp'], 30)}: "
                      f"{_one_line(c['field_name'], 70)}"
                      + (f" - {_one_line(c['reason'] or c['new_value'], 90)}" if (c['reason'] or c['new_value']) else "")
                      + tag("change", c["id"]),
            prof["changes"][1], "change")
    section("Recent decisions", decisions, len(decisions),
            lambda d: f"- {(d['created_at'] or '')[:10]} #{d['id']} {_one_line(_first_decision(d), 140)}"
                      + (f" (replaces #{replaces[d['id']]})" if d["id"] in replaces else "")
                      + tag("decision", d["id"]), prof["decisions"][1], "decision")
    section("Key learnings", learnings, len(learnings),
            lambda l: f"- [{l['category'] or 'other'}{', verified' if l['verified'] else ''}] "
                      f"{_one_line(l['insight'], 160)}" + tag("learning", l["id"]),
            prof["learnings"][1], "learning")
    footer = ("\nMore detail: manage_todos(action=list), manage_problems(action=list), "
              "search (answer=true to ask).")
    if size != "deep":
        footer += " Longer brief: start_here(size=\"deep\")."
    # Linked projects last, from whatever is left: this is what gets trimmed, never the
    # project's own items above.
    if linked and b.remaining() > len(footer) + 80:
        room = _Budget(b.remaining() - len(footer) - 1)
        if room.add("\nLinked projects:") and any([room.add(ln) for ln in linked]):
            for ln in room.lines:
                b.add(ln)
    b.add(footer)

    text = b.text()
    if len(text) > cap:  # cannot happen by construction; belt and braces
        text = text[: cap - 16] + "\n...(truncated)"
    return text


_RULE_ID = re.compile(r"^- R(\d+): ")
_HANDOFF_ID = re.compile(r"^- H(\d+) \(")


def _note(shown: list, kind: str, m) -> None:
    if m:
        shown.append(f"{kind}:{m.group(1)}")


def _first_decision(d: dict) -> str:
    import json
    try:
        items = json.loads(d.get("key_decisions") or "[]")
        if items:
            return str(items[0])
    except (ValueError, TypeError):
        pass
    return d.get("user_prompt_summary") or ""


def candidates_text(resolution: dict, cap: int = BRIEF_CAP) -> str:
    """Brief-sized reply when no single project matched."""
    b = _Budget(cap - 40)
    b.add(f"FlowState: {resolution.get('reason', 'no project chosen')}.")
    names = resolution.get("candidates", [])
    if not names:
        b.add("There are no projects yet. Create one with manage_projects(action=\"create\", "
              "name=\"<name>\") when the user starts new work.")
        return b.text()
    b.add("Call start_here again with project=\"<name>\" (or manage_projects(action=\"create\") if this is new work).")
    b.add("Projects:")
    for n in names:
        if not b.add(f"- {n}"):
            break
    return b.text()
