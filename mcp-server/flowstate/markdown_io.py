"""Markdown export and import for one project (v2 Phase D).

Export writes a readable file: todos (with [ ] / [x]), open problems, decisions, recent
changes, learnings and links, each line ending in a marker like <!-- fs:todo:12 -->.

Import reads such a file back. It never writes records directly:
  * a new line (no marker) becomes a review item of that section's kind;
  * an edited line (marker, different text, or a todo ticked/unticked) becomes an `edit`
    review item; approving it applies the change (recorded in history, undoable);
  * a removed line is ignored (FlowState never deletes from an import);
  * lines containing secrets are skipped and counted, never stored.
Decisions and changes are history: their lines are read-only (counted, never imported).
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import history, links, priority, review, secrets_scan

MARK = re.compile(r"\s*<!--\s*fs:([a-z]+):(\d+)\s*-->\s*$")
SECTIONS = {"todos": "todo", "problems": "problem", "decisions": "decision",
            "changes": "change", "learnings": "learning"}
_PRIO = re.compile(r"\s*\((critical|urgent|high|normal|medium|low)\)\s*$", re.I)
_EDITABLE = {"todo": ("todos", {"title", "status"}), "problem": ("problems", {"title"}),
             "learning": ("learnings", {"insight"})}


def _conn(db_path: Path):
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _flat(s: Optional[str]) -> str:
    return " ".join((s or "").split())


def export(db_path: Path, project_id: int) -> str:
    conn = _conn(db_path)
    try:
        p = conn.execute("SELECT id, name, description FROM projects WHERE id = ?", (project_id,)).fetchone()
        if p is None:
            raise ValueError(f"no project with id {project_id}")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        out = [f"# FlowState: {p['name']}", "",
               f"<!-- flowstate-export project={p['id']} exported={now} -->",
               "_Edit lines or add new ones under a heading, then run "
               f"`flowstate import-md --project \"{p['name']}\" <this file>`. Changes and new lines go to "
               "the review queue; nothing is deleted._", ""]
        if p["description"]:
            out += [_flat(p["description"]), ""]
        out.append("## Todos")
        for t in conn.execute("""SELECT id, title, priority, status FROM todos WHERE project_id = ?
                                 AND (status IS NULL OR status != 'cancelled') ORDER BY
                                 CASE WHEN status = 'done' THEN 1 ELSE 0 END, id""", (project_id,)):
            box = "x" if t["status"] == "done" else " "
            out.append(f"- [{box}] {_flat(t['title'])} ({priority.label(t['priority'])}) <!-- fs:todo:{t['id']} -->")
        out += ["", "## Problems"]
        for r in conn.execute("""SELECT p.id, p.title, p.status FROM problems p JOIN components c
                                 ON c.id = p.component_id WHERE c.project_id = ? AND
                                 (p.status IS NULL OR p.status NOT IN ('solved', 'wont_fix'))
                                 ORDER BY p.id""", (project_id,)):
            out.append(f"- {_flat(r['title'])} <!-- fs:problem:{r['id']} -->")
        out += ["", "## Decisions"]
        replaced = links.superseded_decisions(conn)
        for d in conn.execute("""SELECT id, created_at, key_decisions, user_prompt_summary FROM conversations
                                 WHERE project_id = ? AND key_decisions IS NOT NULL ORDER BY id""", (project_id,)):
            text = links.label(conn, "decision", d["id"])
            note = f" (replaced by #{replaced[d['id']][0]})" if d["id"] in replaced else ""
            out.append(f"- {(d['created_at'] or '')[:10]}: {text}{note} <!-- fs:decision:{d['id']} -->")
        out += ["", "## Changes"]
        for ch in conn.execute("""SELECT ch.id, ch.created_at, c.name comp, ch.field_name FROM changes ch
                                  JOIN components c ON c.id = ch.component_id WHERE c.project_id = ?
                                  ORDER BY ch.id DESC LIMIT 100""", (project_id,)):
            out.append(f"- {(ch['created_at'] or '')[:10]} {_flat(ch['comp'])}: {_flat(ch['field_name'])} "
                       f"<!-- fs:change:{ch['id']} -->")
        out += ["", "## Learnings"]
        for l in conn.execute("SELECT id, category, insight FROM learnings WHERE project_id = ? ORDER BY id",
                              (project_id,)):
            out.append(f"- [{l['category'] or 'other'}] {_flat(l['insight'])} <!-- fs:learning:{l['id']} -->")
        if links.available(conn):
            rows = conn.execute("""SELECT * FROM record_links WHERE (from_project_id = ? OR to_project_id = ?)
                                   AND removed_at IS NULL AND link_type NOT IN ('about') ORDER BY id""",
                                (project_id, project_id)).fetchall()
            if rows:
                out += ["", "## Links (read-only)"]
                for r in rows:
                    v = links._view(conn, r)
                    to = v["to"].get("url") or f"{v['to']['label']} ({v['to']['project']})"
                    out.append(f"- {v['from']['label']} ({v['from']['project']}) {v['link_type']} {to}")
        return "\n".join(out) + "\n"
    finally:
        conn.close()


def _parse(text: str) -> list[dict]:
    """Lines under known headings: {kind, text, done, priority, ref}."""
    items, kind = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        h = re.match(r"^#{2,3}\s+(.+?)\s*$", line)
        if h:
            kind = SECTIONS.get(h.group(1).strip().lower().split(" (")[0])
            continue
        if kind is None or not re.match(r"^\s*[-*]\s+", line):
            continue
        body = re.sub(r"^\s*[-*]\s+", "", line)
        ref = None
        m = MARK.search(body)
        if m:
            ref = (m.group(1), int(m.group(2)))
            body = body[:m.start()].rstrip()
        it = {"kind": kind, "ref": ref, "done": None, "priority": None}
        if kind == "todo":
            cb = re.match(r"^\[( |x|X)\]\s*", body)
            if cb:
                it["done"] = cb.group(1).lower() == "x"
                body = body[cb.end():]
            pm = _PRIO.search(body)
            if pm:
                it["priority"] = pm.group(1).lower()
                body = body[:pm.start()]
        elif kind == "learning":
            cm = re.match(r"^\[([a-z_]+)\]\s*", body)
            if cm:
                body = body[cm.end():]
        elif kind in ("decision", "change"):
            body = re.sub(r"^\d{4}-\d{2}-\d{2}:?\s*", "", body)
        it["text"] = body.strip()
        if it["text"]:
            items.append(it)
    return items


def import_text(db_path: Path, project_id: int, text: str, source: str = "markdown") -> dict:
    """Queue new and edited lines for review. Returns counts."""
    items = _parse(text)
    conn = _conn(db_path)
    counts = {"new": 0, "edits": 0, "unchanged": 0, "secrets_skipped": 0,
              "read_only_lines": 0, "not_found": 0, "item_ids": []}
    try:
        for it in items:
            if secrets_scan.scan_text(it["text"]):
                counts["secrets_skipped"] += 1
                continue
            if it["ref"] is None:
                if it["kind"] not in review.RECORD_KINDS:
                    continue
                payload = {"imported_from": source}
                if it["priority"]:
                    payload["priority"] = it["priority"]
                counts["item_ids"].append(review.add_item(
                    db_path, project_id, it["kind"], it["text"][:500],
                    f"Added in a markdown file ({source}).", payload, f"import:{source}"[:80], conn=conn))
                counts["new"] += 1
                continue
            rkind, rid = it["ref"]
            if links.owner(conn, rkind, rid) != project_id:
                counts["not_found"] += 1
                continue
            if rkind in ("decision", "change"):
                counts["read_only_lines"] += 1   # history: edits to these are not imported
                continue
            table, _cols = _EDITABLE[rkind]
            col = {"todo": "title", "problem": "title", "learning": "insight"}[rkind]
            row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (rid,)).fetchone()
            new = {}
            if _flat(row[col]) != _flat(it["text"]):
                new[col] = it["text"][:500]
            if rkind == "todo" and it["done"] is not None:
                is_done = row["status"] == "done"
                if it["done"] != is_done:
                    new["status"] = "done" if it["done"] else "pending"
            if not new:
                counts["unchanged"] += 1
                continue
            desc = "; ".join(f"{k}: \"{links._one_line(str(row[k]), 60)}\" -> \"{links._one_line(v, 60)}\""
                             for k, v in new.items())
            counts["item_ids"].append(review.add_item(
                db_path, project_id, "edit", f"Edit {rkind} #{rid}: {desc}"[:500],
                f"Edited in a markdown file ({source}). Approve to apply (undoable with history).",
                {"record": [rkind, rid], "set": new, "imported_from": source},
                f"import:{source}"[:80], conn=conn))
            counts["edits"] += 1
        conn.commit()
        return counts
    finally:
        conn.close()


def apply_edit(db_path: Path, item: dict, client: str) -> tuple[str, Optional[int]]:
    """Approve an `edit` review item: change only the whitelisted columns, with history."""
    payload = json.loads(item.get("payload") or "{}")
    rkind, rid = payload["record"]
    table, allowed = _EDITABLE[rkind]
    changes = {k: v for k, v in (payload.get("set") or {}).items() if k in allowed}
    if not changes:
        raise review.ReviewError("bad_request", "This edit changes nothing FlowState can apply.")
    conn = _conn(db_path)
    try:
        if links.owner(conn, rkind, rid) != item["project_id"]:
            raise review.ReviewError("record_not_found", f"{rkind} #{rid} is no longer in this project.")
    finally:
        conn.close()
    with history.tracked(db_path, [(table, "id", rid)], "import/edit", client):
        conn = _conn(db_path)
        try:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*changes.values(), rid))
            if rkind == "todo" and changes.get("status") == "done":
                conn.execute("UPDATE todos SET completed_at = CURRENT_TIMESTAMP WHERE id = ?", (rid,))
            conn.commit()
        finally:
            conn.close()
    return table, rid
