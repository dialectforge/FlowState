"""Clean-up suggestions (v2 Phase D): likely duplicates, stale items, misfiled items.

Suggestions only. They go to the review queue as items of kind `cleanup`; nothing is moved,
closed or deleted until the user approves one. Approving applies just that suggestion
(move a misfiled record, cancel a duplicate or stale todo) and records it in the edit
history, so it can be undone. A suggestion the user rejected is not suggested again.

Misfiled: a record whose words match another project's names and components (project name,
description, component names, weighted by how rare each word is across projects) much
better than its own project's; the suggested target is the project whose own records it
most resembles. Local word statistics only; no AI service.
"""

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import history, links, review

STALE_TODO_DAYS = 120
DUP_JACCARD = 0.8
_STOP = set("""the and for with that this from are was were not but have has had you your all any
can will into out use used using when then than they them its our one two new also only just
more most must should would could been being after before over under such each some other via
per need needs about what which there their here where while does done make made like get set
run file files test tests data work fix fixed add added code server servers version true false
none null phase step part app apps system project projects support tool tools build client
clients general core main base layer api web docs doc page pages review user users setup
config""".split())
_OPEN_TODO = ("pending", "in_progress", "blocked")


_BASIC_STOP = set("the and for with that this from are was were not but have has had its into "
                  "out a an of to in on at by is be it as or".split())


def _dup_toks(s: str) -> set:
    return {t for t in re.findall(r"[a-z0-9][a-z0-9_]+", (s or "").lower().replace("-", "_"))
            if t not in _BASIC_STOP}


def _toks(s: str) -> set:
    return {t for t in re.findall(r"[a-z][a-z0-9_]{2,}", (s or "").lower().replace("-", "_"))
            if t not in _STOP}


def _short(s: str, n: int = 70) -> str:
    return links._one_line(s, n)


def _records(conn) -> list[dict]:
    """Every movable record: kind, id, project, text, open?"""
    out = []
    for r in conn.execute("SELECT id, project_id, title, description, status, created_at FROM todos"):
        out.append({"kind": "todo", "id": r[0], "project_id": r[1], "title": r[2],
                    "text": f"{r[2]} {r[3] or ''}", "open": (r[4] or "pending") in _OPEN_TODO,
                    "status": r[4] or "pending", "created_at": r[5]})
    for r in conn.execute("SELECT id, project_id, insight, context, created_at FROM learnings"):
        out.append({"kind": "learning", "id": r[0], "project_id": r[1], "title": r[2],
                    "text": f"{r[2]} {r[3] or ''}", "open": True, "created_at": r[4]})
    for r in conn.execute("""SELECT p.id, c.project_id, p.title, p.description, p.status, p.created_at
                             FROM problems p JOIN components c ON c.id = p.component_id"""):
        out.append({"kind": "problem", "id": r[0], "project_id": r[1], "title": r[2],
                    "text": f"{r[2]} {r[3] or ''}",
                    "open": (r[4] or "open") not in ("solved", "wont_fix", "closed"),
                    "status": r[4], "created_at": r[5]})
    for r in conn.execute("""SELECT ch.id, c.project_id, ch.field_name, ch.reason, ch.new_value,
                             ch.created_at FROM changes ch JOIN components c ON c.id = ch.component_id"""):
        out.append({"kind": "change", "id": r[0], "project_id": r[1], "title": r[2],
                    "text": f"{r[2]} {r[3] or ''} {r[4] or ''}", "open": True, "created_at": r[5]})
    return out


class _Model:
    """Word statistics used to spot misfiled records."""

    def __init__(self, conn, recs: list[dict]):
        self.names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM projects")}
        sig = defaultdict(set)
        for r in conn.execute("SELECT id, name, description FROM projects"):
            sig[r[0]] |= _toks(f"{r[1]} {r[2] or ''}")
        for r in conn.execute("SELECT project_id, name FROM components"):
            sig[r[0]] |= _toks(r[1])
        df = Counter(w for ws in sig.values() for w in ws)
        n = max(1, len(self.names))
        # a word counts as a project's signature if few projects use it (about 1 in 5 or fewer)
        self.max_df = max(1, int(0.22 * n))
        self.sig, self.df = sig, df
        self.sig_idf = {w: math.log(1 + n / c) for w, c in df.items()}
        docs = [(r["project_id"], _toks(r["text"])) for r in recs]
        dfr = Counter(w for _p, ws in docs for w in ws)
        self.nd, self.dfr = max(1, len(docs)), dfr
        cent = defaultdict(Counter)
        for p, ws in docs:
            for w in ws:
                cent[p][w] += math.log(self.nd / dfr[w])
        self.cent = cent
        self.norm = {p: math.sqrt(sum(v * v for v in c.values())) for p, c in cent.items()}

    def misfiled(self, rec: dict) -> Optional[dict]:
        ws = _toks(rec["text"])
        own = rec["project_id"]
        score, evidence = Counter(), defaultdict(list)
        for q, sw in self.sig.items():
            for w in ws & sw:
                if self.df[w] <= self.max_df:
                    score[q] += self.sig_idf[w]
                    evidence[q].append(w)
        if not score:
            return None
        best, bs = score.most_common(1)[0]
        mine = score.get(own, 0)
        second = score.most_common(2)[1][1] if len(score) > 1 else 0
        if best == own or len(evidence[best]) < 2 or bs < 2 * mine + 2 or bs < 1.3 * second:
            return None
        vec = {w: math.log(self.nd / self.dfr[w]) for w in ws if self.dfr.get(w)}
        vn = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        sims = Counter({q: sum(vec[w] * self.cent[q].get(w, 0) for w in vec) / (vn * self.norm[q])
                        for q in self.cent if q != own and self.norm.get(q)})
        top = sims.most_common(2)
        if not top or top[0][1] < 0.04:
            return None
        target, s1 = top[0]
        s2 = top[1][1] if len(top) > 1 else 0.0
        sure = s1 >= 0.08 and s1 >= 1.5 * s2
        return {"target_project_id": target, "target": self.names.get(target),
                "also_possible": None if sure or len(top) < 2 else self.names.get(top[1][0]),
                "confidence": "likely" if sure else "unsure",
                "words": sorted(set(evidence[best]))[:6]}


def suggest(db_path: Path, project_id: Optional[int] = None,
            kinds=("duplicate", "stale", "misfiled"), now: Optional[datetime] = None) -> list[dict]:
    """All suggestions for one project (or every project), most useful first."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        recs = _records(conn)
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM projects")}
        mine = [r for r in recs if project_id is None or r["project_id"] == project_id]
        out = []
        if "misfiled" in kinds:
            model = _Model(conn, recs)
            for r in mine:
                m = model.misfiled(r)
                if m:
                    where = f"{m['target']}" + (f" (or {m['also_possible']})" if m["also_possible"] else "")
                    out.append({"type": "misfiled", "project_id": r["project_id"],
                                "records": [[r["kind"], r["id"]]], **m,
                                "summary": f"Misfiled? {r['kind']} #{r['id']} \"{_short(r['title'])}\" "
                                           f"looks like {where}, not {names[r['project_id']]}",
                                "details": f"Matching words: {', '.join(m['words'])}. Confidence: "
                                           f"{m['confidence']}. Approve to move it to {m['target']} "
                                           f"(or pass move_to=\"<project>\"); reject to keep it here."})
        if "duplicate" in kinds:
            groups = defaultdict(list)
            for r in mine:
                if r["kind"] in ("todo", "learning", "problem") and r["open"]:
                    groups[(r["project_id"], r["kind"])].append((r, _dup_toks(r["title"])))
            for (_pid, kind), items in groups.items():
                for i, (a, ta) in enumerate(items):
                    for b, tb in items[i + 1:]:
                        if len(ta) < 3 or len(tb) < 3:
                            continue
                        j = len(ta & tb) / len(ta | tb)
                        if j >= DUP_JACCARD:
                            old, new = sorted((a, b), key=lambda x: x["id"])
                            out.append({"type": "duplicate", "project_id": old["project_id"],
                                        "records": [[kind, old["id"]], [kind, new["id"]]],
                                        "similarity": round(j, 2),
                                        "summary": f"Duplicate? {kind} #{new['id']} \"{_short(new['title'], 50)}\" "
                                                   f"repeats #{old['id']}",
                                        "details": ("Approve to cancel the newer todo (kept, marked "
                                                    "cancelled)." if kind == "todo" else
                                                    "Approving only notes it; to remove one, ask for a "
                                                    "delete (manage_memory action=delete), which "
                                                    "the user approves.")})
        if "stale" in kinds:
            cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=STALE_TODO_DAYS)
                      ).strftime("%Y-%m-%d %H:%M:%S")
            for r in mine:
                if r["kind"] == "todo" and r["status"] == "pending" and (r["created_at"] or "9") < cutoff:
                    out.append({"type": "stale", "project_id": r["project_id"],
                                "records": [["todo", r["id"]]],
                                "summary": f"Stale? todo #{r['id']} \"{_short(r['title'], 60)}\" has "
                                           f"waited since {(r['created_at'] or '')[:10]}",
                                "details": f"Pending for over {STALE_TODO_DAYS} days. Approve to cancel "
                                           f"it (kept, marked cancelled); reject to keep it open."})
        order = {"misfiled": 0, "duplicate": 1, "stale": 2}
        out.sort(key=lambda s: (order[s["type"]], s["project_id"], s["records"]))
        return out
    finally:
        conn.close()


def _fingerprint(s: dict) -> str:
    return f"{s['type']}:" + ",".join(f"{k}#{i}" for k, i in s["records"])


def queue(db_path: Path, suggestions: list[dict], source: str = "cleanup") -> dict:
    """Add suggestions to the review queue (additive). Skips any already suggested before,
    whatever its status (so rejected ones don't come back)."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    try:
        if not review.available(conn):
            raise review.ReviewError("review_queue_unavailable", "The review queue isn't set up.")
        seen = set()
        for (payload,) in conn.execute("SELECT payload FROM review_items WHERE kind = 'cleanup'"):
            try:
                seen.add(json.loads(payload or "{}").get("fingerprint"))
            except ValueError:
                pass
        added, skipped = [], 0
        for s in suggestions:
            fp = _fingerprint(s)
            if fp in seen:
                skipped += 1
                continue
            payload = {k: v for k, v in s.items() if k not in ("summary", "details", "project_id")}
            payload["fingerprint"] = fp
            added.append(review.add_item(db_path, s["project_id"], "cleanup", s["summary"],
                                         s["details"], payload, source, conn=conn))
            seen.add(fp)
        conn.commit()
        return {"queued": len(added), "item_ids": added, "already_suggested": skipped}
    finally:
        conn.close()


# ---------------------------------------------------------------- apply (on approval)
def _component_in(conn, pid: int, name: Optional[str], client: str) -> int:
    if name:
        cid = links.find_component(conn, pid, name)
        if cid:
            return cid
    row = conn.execute("SELECT id FROM components WHERE project_id = ? AND lower(name) = 'general'",
                       (pid,)).fetchone()
    if row:
        return row[0]
    cur = conn.execute("INSERT INTO components (project_id, name, description) VALUES (?, 'General', ?)",
                       (pid, "Created by FlowState clean-up"))
    return cur.lastrowid


_MOVE_ROWS = {
    "todo": [("todos", "id")], "learning": [("learnings", "id")],
    "problem": [("problems", "id")], "change": [("changes", "id")],
}


def apply(db_path: Path, item: dict, client: str, move_to: Optional[int] = None) -> tuple[str, Optional[int]]:
    """Carry out one approved suggestion. Returns (table, id) of what changed."""
    payload = json.loads(item.get("payload") or "{}")
    kind = payload.get("type")
    recs = payload.get("records") or []
    if kind == "misfiled":
        rkind, rid = recs[0]
        target = move_to or payload.get("target_project_id")
        conn = links.connect(db_path)
        try:
            if links.owner(conn, rkind, rid) is None:
                raise review.ReviewError("record_not_found", f"{rkind} #{rid} no longer exists.")
            if conn.execute("SELECT 1 FROM projects WHERE id = ?", (target,)).fetchone() is None:
                raise review.ReviewError("project_not_found", f"No project with id {target}.")
            link_ids = [r[0] for r in conn.execute(
                """SELECT id FROM record_links WHERE (from_type=? AND from_id=?) OR (to_type=? AND to_id=?)""",
                (rkind, rid, rkind, rid))] if links.available(conn) else []
        finally:
            conn.close()
        # (a General component created in the target is not part of the undo: other records
        # may use it later, and undo must never delete something they depend on)
        rows = [(t, k, rid) for t, k in _MOVE_ROWS[rkind]] + [("record_links", "id", lid) for lid in link_ids]
        with history.tracked(db_path, rows, "cleanup/move", client):
            conn = links.connect(db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                if rkind == "todo":
                    conn.execute("UPDATE todos SET project_id = ?, component_id = NULL WHERE id = ?", (target, rid))
                elif rkind == "learning":
                    conn.execute("UPDATE learnings SET project_id = ?, component_id = NULL WHERE id = ?", (target, rid))
                else:
                    table = "problems" if rkind == "problem" else "changes"
                    old = conn.execute(f"SELECT c.name FROM {table} x JOIN components c ON c.id = x.component_id "
                                       f"WHERE x.id = ?", (rid,)).fetchone()
                    cid = _component_in(conn, target, old[0] if old else None, client)
                    conn.execute(f"UPDATE {table} SET component_id = ? WHERE id = ?", (cid, rid))
                for lid in link_ids:
                    conn.execute("""UPDATE record_links SET
                                    from_project_id = CASE WHEN from_type=? AND from_id=? THEN ? ELSE from_project_id END,
                                    to_project_id = CASE WHEN to_type=? AND to_id=? THEN ? ELSE to_project_id END
                                    WHERE id = ?""", (rkind, rid, target, rkind, rid, target, lid))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return {"todo": "todos", "learning": "learnings", "problem": "problems", "change": "changes"}[rkind], rid
    if kind in ("duplicate", "stale"):
        rkind, rid = recs[-1]   # the newer duplicate / the stale todo
        if rkind != "todo":
            return "none", None   # noted only
        with history.tracked(db_path, [("todos", "id", rid)], f"cleanup/{kind}", client):
            conn = links.connect(db_path)
            try:
                conn.execute("UPDATE todos SET status = 'cancelled' WHERE id = ? AND status IN "
                             "('pending', 'in_progress', 'blocked')", (rid,))
                conn.commit()
            finally:
                conn.close()
        return "todos", rid
    raise review.ReviewError("bad_kind", f"Unknown clean-up suggestion {kind!r}.")
