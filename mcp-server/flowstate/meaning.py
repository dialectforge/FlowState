"""Search: keywords everywhere, plus "by meaning" when a local model is installed.

Keyword search reads the record tables directly (todos, decisions, changes, learnings,
problems, solutions), so it finds everything, including records the old full-text index
never saw. Words match by prefix and simple endings ("crash" finds "crashed", "crashes").

Search by meaning compares local sentence embeddings (default model all-MiniLM-L6-v2, as in
embeddings.py), stored in `meaning_index` (migration 007), and is merged with the keyword
hits. The model is used only if it is already on this computer: FlowState never downloads
it and never calls a cloud API. Without it, search says so and uses keywords.

Setting up the model (the user's choice; see README.md, Honest limits):
    <venv python> -m pip install sentence-transformers
    then put the model folder somewhere and set FLOWSTATE_MEANING_MODEL=<folder>
    then `flowstate meaning index` once.
"""

import array
import hashlib
import math
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Optional

DEFAULT_MODEL = "all-MiniLM-L6-v2"   # the model embeddings.py was written for
TYPES = ("todo", "decision", "change", "learning", "problem", "solution")
MAX_LAZY = 64                         # records embedded during one search call

_SQL = {
    "todo": """SELECT t.id, t.project_id, t.title, COALESCE(t.description, '') body, t.status, t.created_at
               FROM todos t""",
    "decision": """SELECT d.id, d.project_id, COALESCE(d.key_decisions, d.user_prompt_summary) title,
                   COALESCE(d.assistant_response_summary, '') body, NULL status, d.created_at
                   FROM conversations d WHERE d.key_decisions IS NOT NULL""",
    "change": """SELECT ch.id, c.project_id, ch.field_name title,
                 COALESCE(ch.reason, '') || ' ' || COALESCE(ch.new_value, '') body, NULL status, ch.created_at
                 FROM changes ch JOIN components c ON c.id = ch.component_id""",
    "learning": """SELECT l.id, l.project_id, l.insight title, COALESCE(l.context, '') body,
                   NULL status, l.created_at FROM learnings l""",
    "problem": """SELECT p.id, c.project_id, p.title, COALESCE(p.description, '') body, p.status, p.created_at
                  FROM problems p JOIN components c ON c.id = p.component_id""",
    "solution": """SELECT s.id, c.project_id, s.summary title, COALESCE(s.key_insight, '') body,
                   NULL status, s.created_at FROM solutions s JOIN problems p ON p.id = s.problem_id
                   JOIN components c ON c.id = p.component_id""",
}

_PCOL = {"todo": "t.project_id", "decision": "d.project_id", "change": "c.project_id",
         "learning": "l.project_id", "problem": "c.project_id", "solution": "c.project_id"}


# ---------------------------------------------------------------- the model
class _State:
    override = None          # tests (or callers) can supply an embedder
    model = None
    tried = False
    error: Optional[str] = None
    lock = threading.Lock()


def set_embedder(embedder) -> None:
    """Use this object (with .name, .embed(list[str]) -> list[list[float]]); None = reset."""
    _State.override, _State.model, _State.tried, _State.error = embedder, None, False, None


class _SentenceModel:
    def __init__(self, st_model, name: str):
        self._m, self.name = st_model, name

    def embed(self, texts: list) -> list:
        return [list(map(float, v)) for v in self._m.encode(texts, normalize_embeddings=True)]


def model_name() -> str:
    return os.environ.get("FLOWSTATE_MEANING_MODEL") or DEFAULT_MODEL


def embedder():
    """The local model, or None if it isn't installed. Never downloads anything."""
    if _State.override is not None:
        return _State.override
    with _State.lock:
        if _State.tried:
            return _State.model
        _State.tried = True
        try:
            from sentence_transformers import SentenceTransformer  # optional package
        except ImportError:
            _State.error = "the sentence-transformers package is not installed"
            return None
        os.environ.setdefault("HF_HUB_OFFLINE", "1")        # no network, ever
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        name = model_name()
        try:
            _State.model = _SentenceModel(SentenceTransformer(name, local_files_only=True), name)
        except Exception as e:  # noqa: BLE001 - not on disk, broken, ...
            _State.error = f"the model {name!r} is not on this computer ({type(e).__name__})"
            _State.model = None
        return _State.model


def status() -> dict:
    e = embedder()
    if e is None:
        return {"available": False, "model": model_name(), "reason": _State.error}
    return {"available": True, "model": e.name}


# ---------------------------------------------------------------- records
def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _title(kind: str, raw: str) -> str:
    if kind == "decision" and raw and raw.startswith("["):
        import json
        try:
            items = json.loads(raw)
            return "; ".join(map(str, items)) if items else raw
        except ValueError:
            pass
    return raw or ""


def records(conn, project_id: Optional[int] = None, kinds=TYPES) -> list[dict]:
    out = []
    for k in kinds:
        if k not in _SQL:
            continue
        sql = _SQL[k]
        params: list = []
        if project_id is not None:
            sql += (" AND " if " WHERE " in sql else " WHERE ") + f"{_PCOL[k]} = ?"
            params.append(project_id)
        for r in conn.execute(sql, params):
            out.append({"type": k, "id": r["id"], "project_id": r["project_id"],
                        "title": _title(k, r["title"]), "body": r["body"] or "", "status": r["status"],
                        "created_at": r["created_at"]})
    return out


def _text(rec: dict) -> str:
    return f"{rec['title']}. {rec['body']}".strip()[:2000]


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- keywords
_WORD = re.compile(r"[a-z0-9][a-z0-9_]+")
_STOP = set("the a an and or of to in on for with is are was be it this that from by at as".split())


def _stem(w: str) -> str:
    for suf in ("ing", "ies", "es", "ed", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def keyword_hits(recs: list[dict], query: str, limit: int) -> list[tuple[float, dict]]:
    words = [_stem(w) for w in _WORD.findall(query.lower()) if w not in _STOP]
    if not words:
        return []
    phrase = " ".join(query.lower().split())
    scored = []
    for r in recs:
        title, body = r["title"].lower(), r["body"].lower()
        tw = {_stem(w) for w in _WORD.findall(title)}
        bw = {_stem(w) for w in _WORD.findall(body)}
        score = 0.0
        for w in words:
            if w in tw or any(t.startswith(w) for t in tw):
                score += 2.0
            elif w in bw or any(t.startswith(w) for t in bw):
                score += 1.0
        if not score:
            continue
        matched = sum(1 for w in words if any(t.startswith(w) for t in tw | bw))
        score *= matched / len(words)          # all words matching beats one word many times
        if len(words) > 1 and phrase in f"{title} {body}":
            score += 2.0
        scored.append((score, r))
    scored.sort(key=lambda x: (-x[0], x[1]["type"], -x[1]["id"]))
    return scored[:limit]


# ---------------------------------------------------------------- meaning
def _pack(vec: list) -> tuple[bytes, int]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return array.array("f", [v / n for v in vec]).tobytes(), len(vec)


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def available(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='meaning_index'").fetchone() is not None


def index(db_path: Path, project_id: Optional[int] = None, limit: Optional[int] = None,
          emb=None) -> dict:
    """Embed records that are new or changed since they were indexed."""
    emb = emb or embedder()
    if emb is None:
        return {"indexed": 0, "available": False, "reason": _State.error}
    conn = _conn(db_path)
    try:
        if not available(conn):
            return {"indexed": 0, "available": False, "reason": "migration 007 has not run"}
        have = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT content_type, content_id, text_hash FROM meaning_index WHERE model = ?", (emb.name,))}
        todo = []
        for rec in records(conn, project_id):
            t = _text(rec)
            h = _hash(t)
            if have.get((rec["type"], rec["id"])) != h:
                todo.append((rec, t, h))
        if limit is not None:
            todo = todo[:limit]
        done = 0
        for i in range(0, len(todo), 32):
            batch = todo[i:i + 32]
            vecs = emb.embed([t for _r, t, _h in batch])
            for (rec, _t, h), v in zip(batch, vecs):
                blob, dim = _pack(v)
                conn.execute(
                    """INSERT INTO meaning_index (content_type, content_id, project_id, model, dim,
                       vector, text_hash, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(content_type, content_id, model) DO UPDATE SET
                       project_id = excluded.project_id, dim = excluded.dim, vector = excluded.vector,
                       text_hash = excluded.text_hash, updated_at = excluded.updated_at""",
                    (rec["type"], rec["id"], rec["project_id"], emb.name, dim, blob, h))
                done += 1
            conn.commit()
        return {"indexed": done, "available": True, "model": emb.name,
                "remaining": max(0, len(todo) - done) if limit is None else None}
    finally:
        conn.close()


def meaning_hits(conn, emb, recs: list[dict], query: str, limit: int) -> list[tuple[float, dict]]:
    wanted = {(r["type"], r["id"]): r for r in recs}
    q, _dim = _pack(emb.embed([query])[0])
    qv = _unpack(q)
    scored = []
    for row in conn.execute("SELECT content_type, content_id, vector FROM meaning_index WHERE model = ?",
                            (emb.name,)):
        rec = wanted.get((row[0], row[1]))
        if rec is None:
            continue
        v = _unpack(row[2])
        if len(v) != len(qv):
            continue
        s = sum(a * b for a, b in zip(qv, v))
        if s > 0.2:
            scored.append((s, rec))
    scored.sort(key=lambda x: -x[0])
    return scored[:limit]


def search(db_path: Path, query: str, project_id: Optional[int] = None, kinds=None,
           limit: int = 10, mode: str = "auto") -> dict:
    """mode: auto (meaning + keywords if the model is installed, else keywords), keyword,
    meaning (meaning only; keywords with a note if the model isn't installed)."""
    query = (query or "").strip()
    limit = max(1, min(int(limit or 10), 50))
    kinds = tuple(k for k in (kinds or TYPES) if k in TYPES) or TYPES
    conn = _conn(db_path)
    try:
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM projects")}
        recs = records(conn, project_id, kinds)
        kw = keyword_hits(recs, query, limit * 3) if mode != "meaning" else []
        emb = embedder() if mode != "keyword" else None
        note = None
        mh: list = []
        if emb is not None and available(conn):
            conn.close()
            index(db_path, project_id, limit=MAX_LAZY, emb=emb)   # catch up on new records
            conn = _conn(db_path)
            mh = meaning_hits(conn, emb, recs, query, limit * 3)
            used = "meaning+keywords" if mode == "auto" else mode
        else:
            used = "keywords"
            if mode != "keyword":
                note = ("Search by meaning isn't available on this computer ("
                        f"{_State.error or 'no local model'}); these are keyword matches.")
            if mode == "meaning":
                kw = keyword_hits(recs, query, limit * 3)
        # merge by reciprocal rank (meaning first on ties)
        fused: dict = {}
        for src, hits in (("meaning", mh), ("keyword", kw)):
            for rank, (_s, r) in enumerate(hits):
                key = (r["type"], r["id"])
                e = fused.setdefault(key, {"rec": r, "score": 0.0, "by": []})
                e["score"] += 1.0 / (60 + rank)
                e["by"].append(src)
        ranked = sorted(fused.values(), key=lambda e: -e["score"])[:limit]
        results = []
        for e in ranked:
            r = e["rec"]
            results.append({"content_type": r["type"], "content_id": r["id"],
                            "project_id": r["project_id"], "project": names.get(r["project_id"]),
                            "title": r["title"][:160], "snippet": r["body"][:200],
                            **({"status": r["status"]} if r.get("status") else {}),
                            "matched_by": "+".join(e["by"])})
        out = {"query": query, "mode": used, "results": results, "count": len(results)}
        if note:
            out["note"] = note
        return out
    finally:
        conn.close()


def recent(db_path: Path, project_id: Optional[int] = None, kinds=None,
           hours: Optional[int] = None, limit: int = 10) -> dict:
    """search without a query: the newest records, optionally from the last `hours` only."""
    from datetime import datetime, timedelta, timezone
    limit = max(1, min(int(limit or 10), 50))
    kinds = tuple(k for k in (kinds or TYPES) if k in TYPES) or TYPES
    cutoff = None
    if hours not in (None, ""):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(0, int(hours)))
                  ).strftime("%Y-%m-%d %H:%M:%S")
    conn = _conn(db_path)
    try:
        names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM projects")}
        recs = [r for r in records(conn, project_id, kinds)
                if cutoff is None or (r["created_at"] or "") >= cutoff]
    finally:
        conn.close()
    recs.sort(key=lambda r: (r["created_at"] or "", r["id"]), reverse=True)
    results = [{"content_type": r["type"], "content_id": r["id"], "project_id": r["project_id"],
                "project": names.get(r["project_id"]), "title": r["title"][:160],
                "snippet": r["body"][:200], **({"status": r["status"]} if r.get("status") else {}),
                "created_at": r["created_at"]} for r in recs[:limit]]
    out = {"query": None, "mode": "newest", "results": results, "count": len(results)}
    if cutoff is not None:
        out["since_hours"] = int(hours)
    return out
