"""ask: a plain question in, a short answer out, with the ids of the records it came from
(v2 Phase D, E7).

Local only: built on meaning.search (meaning + keywords when a local model is installed,
keywords otherwise), plus the project's hard rules and open handoff notes. The answer is
extractive: the best-matching sentence of each top record, quoted with its id. FlowState
does not make anything up; if nothing matches, it says the memory has nothing on it.
"""

import re
from pathlib import Path
from typing import Optional

from . import handoffs, links, meaning, provenance, rules

# Question words that carry no topic ("what port does the server use" -> "port server use").
_QWORDS = set("""what which who whom whose where when why how does do did is are was were can
could should would will shall may might we our us i my me you your it its there their they
about tell know any some have has had please the a an of to in on for with and or""".split())
_SPLIT = re.compile(r"(?<=[.!?;])\s+|\n+")
MAX_ANSWER_ITEMS = 3


def _topic(question: str) -> str:
    words = re.findall(r"[A-Za-z0-9_][A-Za-z0-9_.:/\\-]*", question or "")
    kept = [w for w in words if w.lower() not in _QWORDS]
    return " ".join(kept) or (question or "")


def _best_sentence(text: str, topic: str, width: int = 220) -> str:
    stems = {meaning._stem(w) for w in meaning._WORD.findall(topic.lower()) if w not in meaning._STOP}
    best, score = "", -1
    for s in _SPLIT.split(text or ""):
        s = " ".join(s.split())
        if not s:
            continue
        toks = {meaning._stem(w) for w in meaning._WORD.findall(s.lower())}
        sc = sum(1 for w in stems if any(t.startswith(w) for t in toks))
        if sc > score:
            best, score = s, sc
    return links._one_line(best, width)


def _extras(conn, pid: Optional[int]) -> list[dict]:
    """Hard rules and open handoff notes as searchable pseudo-records."""
    if pid is None:
        return []
    out = [{"type": "rule", "id": r["id"], "project_id": pid, "title": r["rule"], "body": "",
            "status": None} for r in rules.active(conn, pid)]
    out += [{"type": "handoff", "id": n["id"], "project_id": pid, "title": n["where_i_was"],
             "body": n.get("next_step") or "", "status": None} for n in handoffs.open_notes(conn, pid)]
    return out


def ask(db_path: Path, question: str, pid: Optional[int] = None, limit: int = 5) -> dict:
    question = " ".join((question or "").split())
    if not question:
        return {"error": "question_required", "message": "Ask a question, e.g. "
                "ask(question=\"which port does the LAN server use?\", project=\"FlowState\")."}
    limit = max(1, min(int(limit or 5), 10))
    topic = _topic(question)
    found = meaning.search(db_path, topic, pid, None, limit * 2, "auto")
    conn = links.connect(db_path)
    try:
        replaced = links.superseded_decisions(conn) if links.available(conn) else {}
        hits = [r for r in found["results"]
                if not (r["content_type"] == "decision" and r["content_id"] in replaced)]
        extra = meaning.keyword_hits(_extras(conn, pid), topic, 3)
        names = {pid: links.project_name(conn, pid)} if pid is not None else {}
        sources = []
        for _s, r in extra:
            sources.append({"type": r["type"], "id": r["id"], "project": names.get(pid),
                            "text": _best_sentence(f"{r['title']}. {r['body']}", topic)})
        for r in hits[:limit]:
            text = f"{r['title']}. {r.get('snippet') or ''}"
            item = {"type": r["content_type"], "id": r["content_id"], "project": r["project"],
                    "text": _best_sentence(text, topic), "matched_by": r["matched_by"]}
            if r.get("status"):
                item["status"] = r["status"]
            sources.append(item)
        metas: dict = {}
        for t in {s["type"] for s in sources if s["type"] in provenance.TABLES}:
            metas[t] = provenance.get(conn, t, [s["id"] for s in sources if s["type"] == t])
        for s in sources:
            m = metas.get(s["type"], {}).get(s["id"])
            if m:
                s.update(m)
    finally:
        conn.close()

    if not sources:
        return {"question": question, "answer": "FlowState has nothing on this"
                + (" in this project" if pid is not None else "") + ". (Nothing matched; "
                "it does not guess.)", "sources": [], "mode": found["mode"],
                **({"note": found["note"]} if found.get("note") else {})}
    lines = []
    for s in sources[:MAX_ANSWER_ITEMS + len(extra)]:
        label = {"rule": f"hard rule R{s['id']}", "handoff": f"handoff H{s['id']}"}.get(
            s["type"], f"{s['type']} #{s['id']}")
        where = f", {s['project']}" if s.get("project") and pid is None else ""
        lines.append(f"- {s['text']} [{label}{where}{provenance.tag(s).replace(' (', ', ').rstrip(')')}]")
    answer = ("From FlowState (best matches, quoted; check the records for detail):\n"
              + "\n".join(lines))
    out = {"question": question, "answer": answer, "sources": sources, "mode": found["mode"]}
    if found.get("note"):
        out["note"] = found["note"]
    return out
