"""Clean-up suggestions (v2 Phase D): likely duplicates, stale items, misfiled items.

Suggestions only. They go to the review queue as items of kind `cleanup`; nothing is moved,
closed or deleted until the user approves one. Approving applies just that suggestion
(move a misfiled record, cancel a duplicate or stale todo) and records it in the edit
history, so it can be undone. A suggestion the user rejected is not suggested again.
Approving is tied to the exact rows listed when it was queued (task 74, as deletes are): if
one changed, or one was added, is missing or was already removed, nothing is changed and the
suggestion is closed; running the clean-up again suggests it anew.

Misfiled: a record whose words match another project's names and components (project name,
description, component names, weighted by how rare each word is across projects) much
better than its own project's, and that clearly resembles that project's own records more
than any other's. Even then it is only suggested when the record names the target: its text
contains the target project's name or one of its components' names (not "General"; names of
4+ characters), and not its own project's name. FlowState never asks the user to judge a
guess it isn't sure of; `withdraw_unsure` takes back unsure ones queued by older versions.
Each suggestion is one plain question ('Move learning "..." from A to B? It mentions B.').
Local word statistics only; no AI service.
"""

import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import dbconn
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


def similarity(ta: set, tb: set) -> Optional[float]:
    """Jaccard similarity of two titles' words, or None when either is too short to tell."""
    if len(ta) < 3 or len(tb) < 3:
        return None
    return len(ta & tb) / len(ta | tb)


def is_duplicate(a: str, b: str) -> bool:
    """The duplicate check `suggest` uses, for two texts: the same words, or similar enough."""
    ta, tb = _dup_toks(a), _dup_toks(b)
    if ta and ta == tb:
        return True
    j = similarity(ta, tb)
    return j is not None and j >= DUP_JACCARD


def _toks(s: str) -> set:
    return {t for t in re.findall(r"[a-z][a-z0-9_]{2,}", (s or "").lower().replace("-", "_"))
            if t not in _STOP}


def _short(s: str, n: int = 70) -> str:
    return links._one_line(s, n)


TITLE_MAX = 40   # keeps the question and its reason within about 150 characters
WITHDRAWN = "Withdrawn by FlowState: it wasn't sure enough to ask you."


def _words(s: Optional[str]) -> str:
    """Lowercase words joined by single spaces, padded, for whole-name matching."""
    return " " + " ".join(re.findall(r"[a-z0-9]+", (s or "").lower())) + " "


def _names(text: str, name: Optional[str]) -> bool:
    n = _words(name).strip()
    return len(n) >= 4 and f" {n} " in text


def question(kind: str, rkind: str, title: str, project: str, target: Optional[str] = None,
             reason: str = "") -> str:
    """The one plain line a clean-up item asks the user: the question, then its reason."""
    t = f'"{_short(title, TITLE_MAX)}"'
    q = (f"Move {rkind} {t} from {project} to {target}?" if kind == "misfiled"
         else f"Cancel {rkind} {t} in {project}?")
    full = f"{q} {reason[:1].upper()}{reason[1:]}." if reason else q
    if len(full) > 150 and kind == "misfiled":
        full = f"{q} It mentions {target}."
    return full


def ask(payload: dict, project: str, db_path: Optional[Path] = None) -> str:
    """A clean-up item's question in verb form, for the approval pages:
    'move the learning "..." from A to B' / 'cancel the todo "..." in A'."""
    recs = payload.get("records") or []
    rkind, rid = recs[-1] if recs else ("record", None)
    title = None
    if db_path and rid is not None:
        try:
            conn = links.connect(db_path)
            try:
                title = links.label(conn, rkind, rid)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001 - the question still reads without the title
            title = None
    what = f'the {rkind} "{_short(title, TITLE_MAX)}"' if title else f"{rkind} #{rid}"
    if payload.get("type") == "misfiled":
        return f"move {what} from {project} to {payload.get('target') or 'another project'}"
    return f"cancel {what} in {project}"


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
        self.parts = defaultdict(list)
        for pid, name in conn.execute("SELECT project_id, name FROM components ORDER BY id"):
            if (name or "").strip().lower() != "general":
                self.parts[pid].append(name)
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
        if s1 < 0.08 or s1 < 1.5 * s2:
            return None   # not sure: never asked
        text, name = _words(rec["text"]), self.names.get(target)
        if _names(text, self.names.get(own)):
            return None   # it names its own project too
        if _names(text, name):
            reason = f"it mentions {name}"
        else:
            hits = [c for c in self.parts.get(target, []) if _names(text, c)]
            if not hits:
                return None   # it names neither the other project nor any of its parts
            hit = min(hits, key=lambda c: text.index(f" {_words(c).strip()} "))
            reason = f'it mentions {name}\'s "{hit}" component'
        return {"target_project_id": target, "target": name, "confidence": "likely",
                "reason": reason, "words": sorted(set(evidence[best]))[:6]}


def suggest(db_path: Path, project_id: Optional[int] = None,
            kinds=("duplicate", "stale", "misfiled"), now: Optional[datetime] = None) -> list[dict]:
    """All suggestions for one project (or every project), most useful first."""
    conn = dbconn.connect(str(db_path), timeout=5.0)
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
                    here = names[r["project_id"]]
                    out.append({"type": "misfiled", "project_id": r["project_id"],
                                "records": [[r["kind"], r["id"]]], **m,
                                "summary": question("misfiled", r["kind"], r["title"], here,
                                                    m["target"], m["reason"]),
                                "details": f"Approve moves it (undo puts it back). Reject keeps it "
                                           f"in {here}; FlowState won't ask again."})
        if "duplicate" in kinds:
            groups = defaultdict(list)
            for r in mine:   # todos only: approving a duplicate of another kind would do nothing
                if r["kind"] == "todo" and r["open"]:
                    groups[(r["project_id"], r["kind"])].append((r, _dup_toks(r["title"])))
            for (_pid, kind), items in groups.items():
                for i, (a, ta) in enumerate(items):
                    for b, tb in items[i + 1:]:
                        j = similarity(ta, tb)
                        if j is not None and j >= DUP_JACCARD:
                            old, new = sorted((a, b), key=lambda x: x["id"])
                            why = f'it repeats {kind} "{_short(old["title"], TITLE_MAX)}"'
                            out.append({"type": "duplicate", "project_id": old["project_id"],
                                        "records": [[kind, old["id"]], [kind, new["id"]]],
                                        "similarity": round(j, 2), "reason": why,
                                        "summary": question("duplicate", kind, new["title"],
                                                            names[old["project_id"]], reason=why),
                                        "details": "Approve cancels the newer todo (kept, marked "
                                                   "cancelled; undo reopens it). Reject keeps both; "
                                                   "FlowState won't ask again."})
        if "stale" in kinds:
            cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=STALE_TODO_DAYS)
                      ).strftime("%Y-%m-%d %H:%M:%S")
            for r in mine:
                if r["kind"] == "todo" and r["status"] == "pending" and (r["created_at"] or "9") < cutoff:
                    why = f"it has waited since {(r['created_at'] or '')[:10]} (over {STALE_TODO_DAYS} days)"
                    out.append({"type": "stale", "project_id": r["project_id"],
                                "records": [["todo", r["id"]]], "reason": why,
                                "summary": question("stale", "todo", r["title"], names[r["project_id"]],
                                                    reason=why),
                                "details": "Approve cancels it (kept, marked cancelled; undo reopens "
                                           "it). Reject keeps it open; FlowState won't ask again."})
        order = {"misfiled": 0, "duplicate": 1, "stale": 2}
        out.sort(key=lambda s: (order[s["type"]], s["project_id"], s["records"]))
        return out
    finally:
        conn.close()


def _fingerprint(s: dict) -> str:
    return f"{s['type']}:" + ",".join(f"{k}#{i}" for k, i in s["records"])


def withdraw_unsure(db_path: Path, timeout: float = 5.0) -> int:
    """Reject, as FlowState, pending misfiled suggestions an older version queued without
    being sure (confidence "unsure", or neither "likely" nor a `reason`). Only review_items
    rows change, never a record; the fingerprint stays, so they are not suggested again.
    Returns the count."""
    conn = dbconn.connect(str(db_path), timeout=timeout)
    try:
        if not review.available(conn):
            return 0
        ids = []
        for iid, payload in conn.execute("SELECT id, payload FROM review_items "
                                         "WHERE kind = 'cleanup' AND status = 'pending'"):
            try:
                p = json.loads(payload or "{}")
            except ValueError:
                continue
            sure = p.get("confidence") == "likely" or (p.get("reason") and p.get("confidence") != "unsure")
            if p.get("type") == "misfiled" and not sure:
                ids.append(iid)
        if ids:
            conn.executemany("UPDATE review_items SET status = 'rejected', decided_at = ?, "
                             "decided_by = 'FlowState', reject_reason = ? "
                             "WHERE id = ? AND status = 'pending'",
                             [(review._now(), WITHDRAWN, i) for i in ids])
            conn.commit()
        return len(ids)
    finally:
        conn.close()


def queue(db_path: Path, suggestions: list[dict], source: str = "cleanup") -> dict:
    """Add suggestions to the review queue (additive). Skips any already suggested before,
    whatever its status (so rejected ones don't come back), except one FlowState closed
    because its list went out of date (task 74): that one is asked again. Each item stores
    the exact rows approving it would change (`bound`). A waiting item whose list no longer
    matches is closed and queued again (`replaced`). First withdraws unsure ones left by
    older versions (`withdrew`)."""
    from . import deletes
    withdrew = withdraw_unsure(db_path)
    conn = dbconn.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    try:
        if not review.available(conn):
            raise review.ReviewError("review_queue_unavailable", "The review queue isn't set up.")
        seen, waiting = set(), {}
        for r in conn.execute("SELECT id, status, decided_by, payload FROM review_items "
                              "WHERE kind = 'cleanup'"):
            try:
                p = json.loads(r["payload"] or "{}")
            except ValueError:
                continue
            if r["status"] == "rejected" and r["decided_by"] == VOIDED_BY:
                continue   # closed as out of date: may be suggested again
            seen.add(p.get("fingerprint"))
            if r["status"] == "pending":
                waiting[p.get("fingerprint")] = (r["id"], p)
        added, skipped, stale = [], 0, []
        for s in suggestions:
            fp = _fingerprint(s)
            payload = {k: v for k, v in s.items() if k not in ("summary", "details", "project_id")}
            payload["fingerprint"] = fp
            rows = bind(conn, payload)
            if rows is not None:
                payload["bound"] = rows
            if fp in waiting and rows is not None and \
                    (waiting[fp][1].get("bound") or {}).get("digest") != rows["digest"]:
                then = waiting[fp][1].get("bound") or \
                    (ERASED_WHY if waiting[fp][1].get("bound_erased") else None)
                stale.append((waiting[fp][0], then, rows))   # its list is out of date: replaced
            elif fp in seen:
                skipped += 1
                continue
            added.append(review.add_item(db_path, s["project_id"], "cleanup", s["summary"],
                                         s["details"], payload, source, conn=conn))
            seen.add(fp)
        conn.commit()
    finally:
        conn.close()
    replaced = []
    for iid, then, rows in stale:
        why = (deletes._bound_diff(then, rows, _VERBS) if isinstance(then, dict)
               else ERASED_WHY if then == ERASED_WHY else "queued by an older FlowState")
        if deletes.void(db_path, iid, f"Replaced by a new suggestion: what it would change "
                                      f"changed since it was suggested ({why}).", "cleanup"):
            replaced.append(iid)
    out = {"queued": len(added), "item_ids": added, "already_suggested": skipped,
           "withdrew": withdrew}
    if replaced:
        out["replaced"] = replaced
    return out


# ---------------------------------------------------------------- the exact list (task 74)
# Approving a clean-up that changes records is tied to the rows listed when it was queued,
# the same way a delete is (deletes.bind, task 72): a changed row, or a row added, missing or
# already removed, and approving it changes nothing and closes it (VOIDED_BY).
VOIDED_BY = "flowstate (automatic)"
# `flowstate erase` drops the list (ids, digest, hashes) of a clean-up about an erased record
# and leaves `bound_erased` (erase.py, task 75): approving it changes nothing, as for any change
ERASED_WHY = "a record on its list was erased"
_VERBS = (("rows", "lists"),)
NONE_LEFT = "none (no record on its list is still there)"   # e.g. its only record was erased
_TABLE = {"todo": "todos", "learning": "learnings", "problem": "problems", "change": "changes"}


def _targets(conn, payload: dict) -> Optional[dict]:
    """The rows approving this suggestion would change, as they are now ({(table, id): row});
    a move also lists the record's links, a duplicate the todo it keeps. None: it changes
    nothing (a duplicate of learnings queued by an older version is only noted)."""
    kind, recs = payload.get("type"), payload.get("records") or []
    if not recs:
        return None
    if kind == "misfiled":
        rkind, rid = recs[0]
        keys = [(_TABLE.get(rkind), rid)]
        if links.available(conn):
            keys += [("record_links", r[0]) for r in conn.execute(
                "SELECT id FROM record_links WHERE (from_type=? AND from_id=?) OR "
                "(to_type=? AND to_id=?)", (rkind, rid, rkind, rid))]
    elif kind in ("duplicate", "stale"):
        if recs[-1][0] != "todo":
            return None
        keys = [("todos", rid) for rk, rid in recs if rk == "todo"]
    else:
        return None
    if keys[0][0] is None:
        return None
    out = {}
    for t, k in keys:
        row = history._row(conn, t, k)
        if row is not None:
            out[(t, k)] = row
    return out


def bind(conn, payload: dict) -> Optional[dict]:
    """The exact list for a suggestion (ids and hashes, no text), or None if it changes nothing."""
    from . import deletes
    rows = _targets(conn, payload)
    return None if rows is None else deletes.bind(rows)


def _check(conn, payload: dict) -> None:
    """Refuse (deletes.DeleteError, closes the item) unless the rows approving this would
    change are exactly the ones listed when it was queued. A no-op clean-up passes."""
    from . import deletes
    now = bind(conn, payload)
    if now is None:
        return
    then = payload.get("bound")
    if payload.get("bound_erased"):
        raise deletes.DeleteError(
            "changed_since_request", f"What this clean-up would change is not what it was "
            f"suggested with ({ERASED_WHY}). Nothing was changed and the suggestion is closed; "
            "run the clean-up again (manage_memory action=\"cleanup\") to get a new one.")
    if not isinstance(then, dict) or not then.get("digest"):
        raise deletes.DeleteError(
            "request_outdated", "This clean-up suggestion was queued by an older FlowState and "
            "isn't tied to an exact list of records, so it was closed. Nothing was changed; run "
            "the clean-up again (manage_memory action=\"cleanup\") to get a new suggestion.")
    if now["digest"] != then["digest"]:
        raise deletes.DeleteError(
            "changed_since_request", "What this clean-up would change is not what it was "
            f"suggested with ({deletes._bound_diff(then, now, _VERBS)}). Nothing was changed "
            "and the suggestion is closed; run the clean-up again (manage_memory "
            "action=\"cleanup\") to get a new one.")


def _role(kind: str, table: str, rid, recs: list) -> str:
    if table == "record_links":
        return "moves with it"
    if kind == "misfiled":
        return "moves"
    if kind == "duplicate" and len(recs) > 1 and rid == recs[0][1]:
        return "kept"
    return "cancelled"


def preview(db_path: Path, item: dict) -> Optional[dict]:
    """The exact list for the review and phone pages, from the database: each record (id, a
    short title from the live record, what happens to it), and whether it still matches the
    list the suggestion was made with. None for a clean-up that changes nothing. A record
    that is gone (deleted or erased) is shown by id only."""
    from . import deletes
    try:
        payload = json.loads(item.get("payload") or "{}")
    except ValueError:
        return None
    conn = links.connect(db_path)
    try:
        now_rows = _targets(conn, payload)
        if now_rows is None:
            return None
        now = deletes.bind(now_rows)
        then = payload.get("bound") if isinstance(payload.get("bound"), dict) else None
        keys = {(t, k) for t, ks in ((then or {}).get("rows") or {}).items() for k in ks}
        keys |= set(now_rows)
        recs = payload.get("records") or []
        lines = []
        for t, k in sorted(keys, key=lambda x: (x[0] == "record_links", x[0], str(x[1]))):
            name = deletes._NAMED.get(t, t)
            if (t, k) not in now_rows:
                lines.append(f"{name} #{k} (no longer there)")
                continue
            title = None if t == "record_links" else links.label(conn, name, k)
            lines.append(f"{name} #{k}" + (f' "{_short(title, TITLE_MAX)}"' if title else "")
                         + f": {_role(payload.get('type'), t, k, recs)}")
    finally:
        conn.close()
    stale = then is None or then.get("digest") != now["digest"]
    lines = lines or [NONE_LEFT]
    text = "Approving changes exactly these records: " + "; ".join(lines) + "."
    warning = None
    if stale:
        why = deletes._bound_diff(then, now, _VERBS) if then else \
            ERASED_WHY if payload.get("bound_erased") else "queued by an older FlowState"
        warning = (f"This list is not the one the suggestion was made with ({why}). Approving it "
                   "changes nothing; run the clean-up again for a new suggestion.")
        text += " " + warning
    return {"lines": lines, "changed_since_request": stale, "text": text, "warning": warning}


def html_for(db_path: Path, it: dict) -> str:
    """The exact list as escaped HTML for the review page ('' for a no-op clean-up)."""
    import html
    got = preview(db_path, it)
    if not got:
        return ""
    out = ("<p class='meta'>Approving changes exactly these records:</p><ul>"
           + "".join(f"<li>{html.escape(x)}</li>" for x in got["lines"]) + "</ul>")
    if got["warning"]:
        out += f"<p><strong>{html.escape(got['warning'])}</strong></p>"
    return out


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
            _check(conn, payload)   # the listed rows, or nothing (again inside the write)
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
                _check(conn, payload)
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
        conn = links.connect(db_path)
        try:
            _check(conn, payload)   # the listed rows, or nothing (again inside the write)
        finally:
            conn.close()
        with history.tracked(db_path, [("todos", "id", rid)], f"cleanup/{kind}", client):
            conn = links.connect(db_path)
            try:
                conn.execute("BEGIN IMMEDIATE")
                _check(conn, payload)
                conn.execute("UPDATE todos SET status = 'cancelled' WHERE id = ? AND status IN "
                             "('pending', 'in_progress', 'blocked')", (rid,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        return "todos", rid
    raise review.ReviewError("bad_kind", f"Unknown clean-up suggestion {kind!r}.")
