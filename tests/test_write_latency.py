"""2.4.0 adds receipts, creation history and the run ledger to every write; saves must stay
fast. On a temp database with ~2,000 records, saves through the real MCP call path stay
well inside a generous bound (measured on a 2026 Mac: about 8.3 ms median in v2.3.0 and
9.7 ms in 2.4.0; after the end-to-end fixes 10.3 ms against 8.9 ms for v2.3.0 on a slower
day; the bound leaves room for slower CI machines). On Windows (example-host, 2026-10-21) each
commit waits for the disk: 58.5 ms with about six commits per save, about 15 ms once a save
commits once (saves.py, dbconn.py)."""

import asyncio
import sqlite3
import statistics
import time

from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

MEDIAN_MS = 40
BOOKKEEPING_MS = 6      # receipt + creation history, per save (median)


def _fill(db, n=500):
    conn = sqlite3.connect(db)
    for i in range(n):
        conn.execute("INSERT INTO todos (project_id, title, priority) VALUES (1, ?, 'medium')",
                     (f"todo {i} " + "x" * 60,))
        conn.execute("INSERT INTO learnings (project_id, insight, category) VALUES (1, ?, 'other')",
                     (f"learning {i} " + "y" * 80,))
        conn.execute("INSERT INTO changes (component_id, field_name, reason) VALUES (1, ?, 'r')",
                     (f"change {i}",))
        conn.execute("INSERT INTO conversations (project_id, user_prompt_summary, key_decisions) "
                     "VALUES (1, ?, ?)", (f"Decision: d{i}", f'["decision {i}"]'))
    conn.commit()
    total = sum(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("todos", "learnings", "changes", "conversations"))
    conn.close()
    return total


def test_save_latency_on_2000_records(backend, monkeypatch):
    S = backend
    assert _fill(S.backend.db_path) >= 2000
    from flowstate import receipts
    spent: list = []
    real = receipts.finish

    def timed(*a, **k):
        t = time.perf_counter()
        try:
            return real(*a, **k)
        finally:
            spent.append((time.perf_counter() - t) * 1000)
    monkeypatch.setattr(receipts, "finish", timed)

    async def go():
        out = []
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="bench", version="1")) as c:
            await c.call_tool("start_here", {"project": "Alpha"})
            for i in range(40):
                for kind in ("learning", "change", "todo"):
                    t = time.perf_counter()
                    r = await c.call_tool("log_work", {"project": "Alpha", "kind": kind,
                                                       "summary": f"latency {kind} {i}"})
                    out.append((time.perf_counter() - t) * 1000)
                    assert not r.isError, r.content[0].text
        return out
    times = asyncio.run(go())
    assert statistics.median(times) < MEDIAN_MS, sorted(times)[::10]
    assert len(spent) == len(times) and statistics.median(spent) < BOOKKEEPING_MS
