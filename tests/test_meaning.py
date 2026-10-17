"""Search (Phase D4g): keyword search over the record tables (finds v2 writes the old
full-text index missed), and search by meaning with a local embedder, merged with keywords.
The real model is not installed in tests; a stand-in embedder maps synonyms to the same
direction so the meaning path is exercised end to end. Temp DBs only."""

import asyncio
import json

import pytest
from mcp import types
from mcp.shared.memory import create_connected_server_and_client_session

from flowstate import meaning

GROUPS = [{"login", "auth", "authentication", "token", "signin", "credential"},
          {"bug", "error", "crash", "broken", "fails", "failure"},
          {"expired", "expiry", "timeout", "stale"},
          {"export", "pdf", "print"}]


class FakeEmbedder:
    name = "fake-synonyms"

    def embed(self, texts):
        out = []
        for t in texts:
            words = set(meaning._WORD.findall(t.lower()))
            out.append([1.0 if words & g else 0.0 for g in GROUPS] + [0.01])
        return out


def run(coro):
    return asyncio.run(coro)


def call(S, name, args):
    async def go():
        async with create_connected_server_and_client_session(
                S.server, client_info=types.Implementation(name="t", version="1")) as c:
            return await c.call_tool(name, args)
    return json.loads(run(go()).content[0].text)


@pytest.fixture(autouse=True)
def _no_real_model():
    meaning.set_embedder(None)
    meaning._State.tried, meaning._State.model = True, None   # behave as "not installed"
    meaning._State.error = "the sentence-transformers package is not installed"
    yield
    meaning.set_embedder(None)


def test_keyword_search_finds_new_records_and_endings(backend):
    call(backend, "log_work", {"project": "Alpha", "kind": "problem", "summary": "PDF export crashes on empty chapters"})
    call(backend, "log_work", {"project": "Beta Project", "kind": "decision", "summary": "Export as PDF only"})
    r = call(backend, "search", {"query": "crash export"})
    assert r["mode"] == "keywords" and "isn't available" in r["note"]
    top = r["results"][0]
    assert top["content_type"] == "problem" and "crashes" in top["title"] and top["project"] == "Alpha"
    only_beta = call(backend, "search", {"query": "export", "project": "Beta Project"})
    assert [x["content_type"] for x in only_beta["results"]] == ["decision"]
    todos = call(backend, "search", {"query": "todo", "content_types": ["todo"]})
    assert {x["title"] for x in todos["results"]} == {"Alpha todo", "Beta todo"}
    assert call(backend, "search", {"query": "zzzz"})["count"] == 0


def test_meaning_finds_what_keywords_miss(backend, seeded_db):
    call(backend, "log_work", {"project": "Alpha", "kind": "problem", "summary": "Auth token expired after an hour"})
    call(backend, "log_work", {"project": "Alpha", "kind": "learning", "summary": "Prefer small commits"})
    kw = call(backend, "search", {"query": "login bug", "mode": "keyword"})
    assert not any("Auth token" in x["title"] for x in kw["results"])
    meaning.set_embedder(FakeEmbedder())
    r = call(backend, "search", {"query": "login bug"})
    assert r["mode"] == "meaning+keywords" and "note" not in r
    by_title = {x["title"]: x for x in r["results"]}
    assert by_title["Auth token expired after an hour"]["matched_by"] == "meaning"
    assert by_title["Alpha bug"]["matched_by"] == "meaning+keyword"     # both: ranked first
    assert r["results"][0]["title"] == "Alpha bug"
    assert "Prefer small commits" not in by_title
    # the index is built lazily and kept up to date
    st = meaning.index(seeded_db)
    assert st["available"] and st["indexed"] == 0
    call(backend, "log_work", {"project": "Alpha", "kind": "todo", "summary": "Renew signin credential"})
    r = call(backend, "search", {"query": "authentication", "mode": "meaning"})
    assert {x["title"] for x in r["results"]} >= {"Renew signin credential", "Auth token expired after an hour"}


def test_meaning_mode_without_model_falls_back(backend):
    r = call(backend, "search", {"query": "Alpha", "mode": "meaning"})
    assert r["mode"] == "keywords" and r["count"] >= 1 and "keyword matches" in r["note"]


def test_never_downloads(monkeypatch):
    """The real loader runs offline only (no download attempt)."""
    meaning.set_embedder(None)
    import sys
    import types as t
    seen = {}

    class ST:
        def __init__(self, name, local_files_only=False):
            seen["local_only"] = local_files_only
            import os
            seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
            raise OSError("not on disk")
    monkeypatch.setitem(sys.modules, "sentence_transformers", t.SimpleNamespace(SentenceTransformer=ST))
    assert meaning.embedder() is None
    assert seen == {"local_only": True, "offline": "1"}
    assert "not on this computer" in meaning.status()["reason"]
