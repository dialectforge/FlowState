"""Import memories from other AI apps into the review queue (v2 Phase D).

Takes what Claude or ChatGPT give you when you export or copy their saved memories: plain
text or markdown (one memory per line or bullet) or JSON (a list of strings, or of objects
with a content / memory / text / value field, or {"memories": [...]}). Every memory becomes
a review item (kind `learning` by default) for one project; nothing is saved as a record
until the user approves it. Memories that look like they contain a secret are skipped and
counted, never stored. Repeats (within the file, or of an existing learning) are skipped.
"""

import json
import re
import sqlite3
from pathlib import Path

from . import dbconn
from . import review, secrets_scan

MAX_ITEMS = 500
_KEYS = ("content", "memory", "text", "value", "fact", "note")


def _flat(s: str) -> str:
    return " ".join((s or "").split())


def _from_json(data) -> list[str]:
    if isinstance(data, dict):
        for k in ("memories", "items", "data", "results"):
            if isinstance(data.get(k), list):
                return _from_json(data[k])
        for k in _KEYS:
            if isinstance(data.get(k), str):
                return [data[k]]
        return []
    if isinstance(data, list):
        out = []
        for x in data:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out += _from_json(x)
        return out
    return []


def parse(text: str) -> list[str]:
    text = (text or "").strip().lstrip("﻿")
    if text[:1] in "[{":
        try:
            return [_flat(m) for m in _from_json(json.loads(text)) if _flat(m)]
        except ValueError:
            pass
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "```", "<!--")):
            continue
        s = re.sub(r"^(?:[-*•]|\d+[.)])\s+", "", s).strip()
        if len(s) >= 8:
            out.append(_flat(s))
    return out


def import_memories(db_path: Path, project_id: int, text: str, source: str = "memory export",
                    kind: str = "learning") -> dict:
    if kind not in review.RECORD_KINDS:
        raise ValueError(f"kind must be one of {', '.join(review.RECORD_KINDS)}")
    memories = parse(text)
    conn = dbconn.connect(str(db_path), timeout=5.0)
    try:
        existing = {_flat(r[0]).lower() for r in conn.execute(
            "SELECT insight FROM learnings WHERE project_id = ?", (project_id,))}
        existing |= {_flat(r[0]).lower() for r in conn.execute(
            "SELECT summary FROM review_items WHERE project_id = ? AND status != 'rejected'", (project_id,))}
        out = {"found": len(memories), "queued": 0, "secrets_skipped": 0, "duplicates_skipped": 0,
               "too_many": max(0, len(memories) - MAX_ITEMS), "item_ids": []}
        for m in memories[:MAX_ITEMS]:
            if secrets_scan.scan_text(m):
                out["secrets_skipped"] += 1
                continue
            if m.lower() in existing:
                out["duplicates_skipped"] += 1
                continue
            existing.add(m.lower())
            out["item_ids"].append(review.add_item(
                db_path, project_id, kind, m[:500], f"Imported from {source}.",
                {"imported_from": source}, f"import:{source}"[:80], conn=conn))
            out["queued"] += 1
        conn.commit()
        return out
    finally:
        conn.close()
