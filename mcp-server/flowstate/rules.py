"""Per-project hard rules: things an AI must never do in this project (v2 Phase D, E2;
migration 009).

Rules are shown first in every start_here brief and are never truncated: the brief makes
room for them and everything else gives way. To keep the brief bounded, a rule is at most
MAX_RULE characters, a project has at most MAX_RULES active rules, and at most MAX_TOTAL
characters of them together. Removing a
rule needs the user's words (user_request); it is a soft delete, so it stays on record.
"""

from pathlib import Path

from . import identity, links

MAX_RULE = 300
MAX_TOTAL = 2000
MAX_RULES = 25


class RuleError(links.LinkError):
    pass


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='project_rules'").fetchone() is not None


def _need(conn) -> None:
    if not available(conn):
        raise RuleError("rules_unavailable", "Hard rules aren't set up in this database yet "
                        "(migration 009 has not run). Restart FlowState.")


def active(conn, pid: int) -> list[dict]:
    if not available(conn):
        return []
    return [dict(r) for r in conn.execute(
        "SELECT id, rule, created_at, created_by FROM project_rules WHERE project_id = ? "
        "AND removed_at IS NULL ORDER BY id", (pid,))]


def add(db_path: Path, pid: int, rule: str, client: str) -> dict:
    text = " ".join((rule or "").split())
    if not text:
        raise RuleError("rule_required", "Give the rule text, e.g. rule=\"Never push to main\".")
    if len(text) > MAX_RULE:
        raise RuleError("rule_too_long", f"A rule is at most {MAX_RULE} characters (this one is "
                        f"{len(text)}). Say it shorter, or split it.")
    conn = links.connect(db_path)
    try:
        _need(conn)
        rules = active(conn, pid)
        for r in rules:
            if r["rule"].lower() == text.lower():
                return {"rule": r, "note": "This rule already exists.", "project_id": pid}
        used = sum(len(r["rule"]) for r in rules)
        if len(rules) >= MAX_RULES:
            raise RuleError("rules_full", f"This project already has {MAX_RULES} rules (the most "
                            f"the brief always shows in full): ask the user to remove or merge one.")
        if used + len(text) > MAX_TOTAL:
            raise RuleError("rules_full", f"This project's rules would pass {MAX_TOTAL} characters "
                            f"({used} used). Rules are always shown in full, so the total is "
                            f"capped: ask the user to remove or shorten one first.")
        cur = identity.insert(conn, "project_rules", {
            "project_id": pid, "rule": text, "created_at": links.now(), "created_by": client},
            client)
        conn.commit()
        return {"rule": {"id": cur.lastrowid, "rule": text}, "project_id": pid,
                "note": "Shown at the top of every start_here brief for this project."}
    finally:
        conn.close()


def remove(db_path: Path, pid: int, rule_id, user_request: str, client: str) -> dict:
    if not (user_request or "").strip():
        raise RuleError("user_request_required", "Only the user can remove a hard rule. Ask "
                        "them; pass their words as user_request.")
    if rule_id is None:
        raise RuleError("rule_id_required", "Give rule_id (rules list shows them).")
    conn = links.connect(db_path)
    try:
        _need(conn)
        row = conn.execute("SELECT project_id, rule, removed_at FROM project_rules WHERE id = ?",
                           (int(rule_id),)).fetchone()
        if row is None:
            raise RuleError("record_not_found", f"No rule with id {rule_id}.")
        if row["project_id"] != pid:
            raise RuleError("project_mismatch", f"Rule #{rule_id} belongs to project "
                            f"'{links.project_name(conn, row['project_id'])}'.")
        if row["removed_at"]:
            return {"removed": int(rule_id), "note": "It was already removed."}
        sets, who = identity.set_sql(conn, "project_rules", client, prefix="removed_")
        conn.execute(f"UPDATE project_rules SET removed_at = ?, removed_by = ?, removed_request = ?"
                     f"{sets} WHERE id = ?",
                     (links.now(), client, user_request.strip()[:500], *who, int(rule_id)))
        conn.commit()
        return {"removed": int(rule_id), "rule": row["rule"],
                "note": "Removed (kept on record with the user's words)."}
    finally:
        conn.close()


def list_rules(db_path: Path, pid: int, include_removed: bool = False) -> dict:
    conn = links.connect(db_path)
    try:
        _need(conn)
        sql = ("SELECT id, rule, created_at, created_by, removed_at, removed_by FROM project_rules "
               "WHERE project_id = ?" + ("" if include_removed else " AND removed_at IS NULL")
               + " ORDER BY id")
        rows = [{k: v for k, v in dict(r).items() if v is not None}
                for r in conn.execute(sql, (pid,))]
        used = sum(len(r["rule"]) for r in rows if not r.get("removed_at"))
        return {"project": links.project_name(conn, pid), "rules": rows, "count": len(rows),
                "characters_used": used, "characters_max": MAX_TOTAL}
    finally:
        conn.close()


def brief_lines(conn, pid: int) -> list[str]:
    rules = active(conn, pid)
    if not rules:
        return []
    return (["HARD RULES for this project (never do these; the user set them):"]
            + [f"- R{r['id']}: {r['rule']}" for r in rules])
