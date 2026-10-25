"""Per-project hard rules: things an AI must never do in this project (v2 Phase D, E2;
migration 009).

Rules are shown first in every start_here brief and are never truncated: the brief makes
room for them and everything else gives way. To keep the brief bounded, a rule is at most
MAX_RULE characters, a project has at most MAX_RULES active rules, and at most MAX_TOTAL
characters of them together. Removing a rule is a soft delete, so it stays on record.

Since 2.4.4 (data security audit D2) a rule is only ever set by the user: one an app asks for
(manage_rules add, from any app: local, LAN, tunnel or relay) waits in the review queue as an
item of kind "rule", which only the user's own click or key decides (approvals.user_only: the
review page, the phone page or `flowstate approve`). `flowstate rules add` in the user's own
terminal sets one at once. The brief says who set each rule; a rule an older version let an
app add on its own is labelled as such.

Removing one follows the same rule (task 54; re-audit N-F4): manage_rules remove, from any app
and whatever words it passes as user_request, only queues a "rule" item with action "remove"
for the user's own click or key; `flowstate rules remove <id>` in the user's own terminal
(typing y) removes it at once. manage_memory delete refuses record_type rule and points here.
"""

from pathlib import Path
from typing import Optional

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


def _confirmed_col(conn) -> bool:
    return any(r[1] == "set_by_user" for r in conn.execute("PRAGMA table_info(project_rules)"))


def active(conn, pid: int) -> list[dict]:
    if not available(conn):
        return []
    extra = ", set_by_user" if _confirmed_col(conn) else ", NULL AS set_by_user"
    app = ", app" if identity.has_column(conn, "project_rules", "app") else ", NULL AS app"
    return [dict(r) for r in conn.execute(
        f"SELECT id, rule, created_at, created_by{extra}{app} FROM project_rules WHERE "
        f"project_id = ? AND removed_at IS NULL ORDER BY id", (pid,))]


QUEUED = ("Not added yet: only the user sets hard rules. Review item #{item} waits for them: "
          "the review page (`flowstate review-page`), the phone link, or `flowstate approve "
          "{item}` in their terminal. Tell the user; you can't approve it.")


REMOVE_QUEUED = ("Not removed yet: only the user removes hard rules. Review item #{item} waits "
                 "for them: the review page (`flowstate review-page`), the phone link, or "
                 "`flowstate approve {item}` in their terminal (or `flowstate rules remove "
                 "{rule}` there). Until then the rule still holds. Tell the user; you can't "
                 "approve it.")

# manage_memory delete record_type=rule (server._delete_request)
DELETE_REFUSAL = ("Hard rules are removed only by the user. manage_rules(action=\"remove\", "
                  "rule_id=..., user_request=<their words>) puts it in the review queue for "
                  "them, or they run `flowstate rules remove <id>` in their terminal. Nothing "
                  "changed.")

# a queued add (older items have no action) / a queued removal
_ADD = "COALESCE(json_extract(payload, '$.action'), 'add') = 'add'"
_REMOVE = "json_extract(payload, '$.action') = 'remove'"


def is_removal(item: dict) -> bool:
    import json
    try:
        return json.loads(item.get("payload") or "{}").get("action") == "remove"
    except (ValueError, AttributeError):
        return False


def _pending(conn, pid: int, text: str):
    row = conn.execute(
        "SELECT id FROM review_items WHERE project_id = ? AND kind = 'rule' AND status = "
        f"'pending' AND {_ADD} AND lower(json_extract(payload, '$.rule')) = lower(?) "
        "ORDER BY id LIMIT 1", (pid, text)).fetchone()
    return row[0] if row else None


def _waiting(conn, pid: int) -> list:
    """Rule texts waiting for the user's OK: they count toward the limits already."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'review_items'").fetchone():
        return []
    return [r[0] or "" for r in conn.execute(
        "SELECT json_extract(payload, '$.rule') FROM review_items WHERE project_id = ? AND "
        f"kind = 'rule' AND status = 'pending' AND {_ADD}", (pid,))]


def _check(conn, pid: int, text: str, queued: bool = False):
    """The rule's text is fine and fits (counting rules waiting for the user, except this
    one); returns an existing rule with the same text, or None."""
    rules = active(conn, pid)
    for r in rules:
        if r["rule"].lower() == text.lower():
            return r
    waiting = [w for w in _waiting(conn, pid) if w.lower() != text.lower()] if queued else []
    rules = rules + [{"rule": w} for w in waiting]
    used = sum(len(r["rule"]) for r in rules)
    if len(rules) >= MAX_RULES:
        raise RuleError("rules_full", f"This project already has {MAX_RULES} rules (the most "
                        f"the brief always shows in full): ask the user to remove or merge one.")
    if used + len(text) > MAX_TOTAL:
        raise RuleError("rules_full", f"This project's rules would pass {MAX_TOTAL} characters "
                        f"({used} used). Rules are always shown in full, so the total is "
                        f"capped: ask the user to remove or shorten one first.")
    return None


def _text(rule: str) -> str:
    text = " ".join((rule or "").split())
    if not text:
        raise RuleError("rule_required", "Give the rule text, e.g. rule=\"Never push to main\".")
    if len(text) > MAX_RULE:
        raise RuleError("rule_too_long", f"A rule is at most {MAX_RULE} characters (this one is "
                        f"{len(text)}). Say it shorter, or split it.")
    return text


def add(db_path: Path, pid: int, rule: str, client: str) -> dict:
    """manage_rules add (any app): queue the rule for the user (kind "rule"); nothing is
    shown in briefs until the user approves it."""
    from . import review
    text = _text(rule)
    conn = links.connect(db_path)
    try:
        _need(conn)
        same = _check(conn, pid, text, queued=True)
        if same is not None:
            return {"rule": same, "note": "This rule already exists.", "project_id": pid}
        item = _pending(conn, pid, text)
        if item is None:
            item = review.add_item(
                None, pid, "rule", f'Add this hard rule? "{text}"',
                "An AI app asked to add this rule. Every AI working on this project is told to "
                "never break a hard rule, so only you set them. Approve to add it; reject to "
                "leave it out.", {"rule": text, "user_only": True}, (client or "unknown")[:80],
                conn=conn)
            conn.commit()
        return {"queued_for_review": item, "project_id": pid,
                "message": QUEUED.format(item=item)}
    finally:
        conn.close()


def add_by_user(db_path: Path, pid: int, rule: str, how: str = "terminal") -> dict:
    """`flowstate rules add` in the user's own terminal: set at once, labelled as theirs."""
    text = _text(rule)
    conn = links.connect(db_path)
    try:
        _need(conn)
        same = _check(conn, pid, text)
        if same is not None:
            return {"rule": same, "note": "This rule already exists.", "project_id": pid}
        return _insert(conn, pid, text, "command-line", how)
    finally:
        conn.close()


def _insert(conn, pid: int, text: str, client: str, how: str, ident=None) -> dict:
    row = {"project_id": pid, "rule": text, "created_at": links.now(), "created_by": client}
    if _confirmed_col(conn):
        row["set_by_user"] = how
    cur = identity.insert(conn, "project_rules", row, client, ident=ident) if ident is not None \
        else identity.insert(conn, "project_rules", row, client)
    conn.commit()
    return {"rule": {"id": cur.lastrowid, "rule": text}, "project_id": pid,
            "note": "Shown at the top of every start_here brief for this project."}


def confirm(db_path: Path, item: dict, how: str) -> tuple:
    """Approving a queued rule (the user's own click or key): the rule is added, labelled with
    the app that asked for it and how the user approved it. Returns (table, id)."""
    import json
    p = json.loads(item.get("payload") or "{}")
    if p.get("action") == "remove":
        return "project_rules", _remove_now(db_path, int(item["project_id"]), p.get("rule_id"),
                                            f"approved by the user ({how})", f"user ({how})")["removed"]
    text = _text(p.get("rule"))
    conn = links.connect(db_path)
    try:
        _need(conn)
        same = _check(conn, int(item["project_id"]), text)
        if same is not None:
            return "project_rules", same["id"]
        ident = identity.on_behalf_of(item.get("app") or item.get("source") or "unknown",
                                      item.get("machine"), item.get("identity_source"),
                                      item.get("model"))
        out = _insert(conn, int(item["project_id"]), text, item.get("source") or "unknown",
                      how, ident=ident)
        return "project_rules", out["rule"]["id"]
    finally:
        conn.close()


def agent_refusal(item: dict) -> dict:
    if is_removal(item):
        return {"error": "user_only", "message": "Only the user removes hard rules; no app can "
                "pass their OK along. " + REMOVE_QUEUED.format(
                    item=item["id"], rule=_payload(item).get("rule_id"))}
    return {"error": "user_only", "message": "Only the user sets hard rules; no app can pass "
            "their OK along. " + QUEUED.format(item=item["id"])}


def _payload(item: dict) -> dict:
    import json
    try:
        return json.loads(item.get("payload") or "{}")
    except ValueError:
        return {}


def _row(conn, pid: Optional[int], rule_id):
    if rule_id is None:
        raise RuleError("rule_id_required", "Give rule_id (rules list shows them).")
    try:
        rid = int(str(rule_id).strip().lstrip("Rr"))
    except ValueError:
        raise RuleError("rule_id_required", "Give rule_id as a number (R<id> in the brief).")
    row = conn.execute("SELECT id, project_id, rule, removed_at FROM project_rules WHERE id = ?",
                       (rid,)).fetchone()
    if row is None:
        raise RuleError("record_not_found", f"No rule with id {rid}.")
    if pid is not None and row["project_id"] != pid:
        raise RuleError("project_mismatch", f"Rule #{rid} belongs to project "
                        f"'{links.project_name(conn, row['project_id'])}'.")
    return row


def remove(db_path: Path, pid: int, rule_id, user_request: str, client: str) -> dict:
    """manage_rules remove (any app): queue the removal for the user (kind "rule", action
    "remove"); the rule holds until the user approves it. The app's user_request is kept with
    the item as what it said the user asked; it removes nothing by itself."""
    from . import review
    if not (user_request or "").strip():
        raise RuleError("user_request_required", "Only the user can remove a hard rule. Ask "
                        "them; pass their words as user_request.")
    conn = links.connect(db_path)
    try:
        _need(conn)
        row = _row(conn, pid, rule_id)
        rid = row["id"]
        if row["removed_at"]:
            return {"removed": rid, "note": "It was already removed."}
        old = conn.execute(
            "SELECT id FROM review_items WHERE project_id = ? AND kind = 'rule' AND status = "
            f"'pending' AND {_REMOVE} AND json_extract(payload, '$.rule_id') = ? ORDER BY id "
            "LIMIT 1", (pid, rid)).fetchone()
        item = old[0] if old else None
        if item is None:
            item = review.add_item(
                None, pid, "rule", f'Remove hard rule R{rid}? "{row["rule"]}"',
                "An AI app asked to remove this rule"
                + f' (it says you asked: "{" ".join(user_request.split())[:300]}")'
                + ". Hard rules are yours alone, so only you remove them. Approve to remove it "
                "(it stays on record as removed); reject to keep it.",
                {"action": "remove", "rule_id": rid, "rule": row["rule"],
                 "user_request": user_request.strip()[:500], "user_only": True},
                (client or "unknown")[:80], conn=conn)
            conn.commit()
        return {"queued_for_review": item, "project_id": pid, "rule_id": rid,
                "message": REMOVE_QUEUED.format(item=item, rule=rid)}
    finally:
        conn.close()


def remove_by_user(db_path: Path, rule_id, how: str = "terminal") -> dict:
    """`flowstate rules remove <id>` in the user's own terminal (after their y): at once. A
    removal an app asked for that still waits is settled with it (rejected: done already)."""
    from . import review
    out = _remove_now(db_path, None, rule_id, f"removed by the user ({how})", f"user ({how})")
    conn = links.connect(db_path)
    try:
        waiting = [r[0] for r in conn.execute(
            "SELECT id FROM review_items WHERE kind = 'rule' AND status = 'pending' AND "
            f"{_REMOVE} AND json_extract(payload, '$.rule_id') = ?", (out["removed"],))]
    finally:
        conn.close()
    for item in waiting:
        review.reject(db_path, item, "The user removed this rule in their terminal.",
                      "flowstate rules remove", "command-line")
    return out


def _remove_now(db_path: Path, pid: Optional[int], rule_id, request: str, who_: str) -> dict:
    conn = links.connect(db_path)
    try:
        _need(conn)
        row = _row(conn, pid, rule_id)
        rid = row["id"]
        if row["removed_at"]:
            return {"removed": rid, "rule": row["rule"], "note": "It was already removed."}
        sets, who = identity.set_sql(conn, "project_rules", who_, prefix="removed_")
        conn.execute(f"UPDATE project_rules SET removed_at = ?, removed_by = ?, removed_request = ?"
                     f"{sets} WHERE id = ?", (links.now(), who_, request[:500], *who, rid))
        conn.commit()
        return {"removed": rid, "rule": row["rule"], "project_id": row["project_id"],
                "note": "Removed (kept on record as removed)."}
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


def who_set(r: dict) -> str:
    """Short label for the brief: "set by the user", or the app an older version let add it."""
    if r.get("set_by_user"):
        return "set by the user"
    return f"added by {(r.get('app') or r.get('created_by') or 'an app')[:40]}, not confirmed by the user"


def brief_lines(conn, pid: int) -> list[str]:
    rules = active(conn, pid)
    if not rules:
        return []
    return (["HARD RULES for this project (never do these; each says who set it):"]
            + [f"- R{r['id']} ({who_set(r)}): {r['rule']}" for r in rules])
