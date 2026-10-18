"""Tool-quality text: every tool and every input property carries a description.

Glama's tool grader scores a missing parameter description as incomplete. This checks the
list the server actually advertises, in every profile (all 18 public tools).
"""

import json

from flowstate.tool_labels import visible
from flowstate.tooldefs import ALL_TOOLS

# v2.0.1 compact tools/list sizes (json.dumps separators=(",", ":")).
# Ceilings are 65% of those: at least a 35% cut, measured the same way. v2.1 keeps these
# ceilings unchanged: the default list (12 tools) and the full list (18) must each fit
# FULL_BUDGET, and the core list (6) CORE_BUDGET.
_V201_FULL = 60415
_V201_CORE = 17730
FULL_BUDGET = int(_V201_FULL * 0.65)  # 39269
CORE_BUDGET = int(_V201_CORE * 0.65)  # 11524

DEFAULT = ["start_here", "log_work", "search", "manage_todos", "manage_problems",
           "manage_review", "manage_handoffs", "manage_rules", "manage_links", "manage_history",
           "manage_projects", "manage_memory"]
CORE = ["start_here", "log_work", "search", "manage_todos", "manage_review", "manage_handoffs"]
FULL_ONLY = ["manage_sessions", "manage_files", "manage_data_git", "manage_variables",
             "manage_methods", "manage_skills"]


def _compact(tools) -> int:
    payload = [t.model_dump(mode="json", exclude_none=True, by_alias=True) for t in tools]
    return len(json.dumps(payload, separators=(",", ":")))


def test_every_tool_and_parameter_has_a_description():
    missing = []
    for tool in ALL_TOOLS:
        if not (tool.description or "").strip():
            missing.append(tool.name)
        props = (tool.inputSchema or {}).get("properties") or {}
        for key, spec in props.items():
            text = spec.get("description") if isinstance(spec, dict) else None
            if not (isinstance(text, str) and text.strip()):
                missing.append(f"{tool.name}.{key}")
    assert missing == []


def test_descriptions_say_what_they_return_and_names_start_with_a_verb():
    verbs = ("start_", "log_", "search", "manage_")
    for tool in ALL_TOOLS:
        assert tool.name.startswith(verbs), tool.name
        assert "Returns" in tool.description, tool.name
        assert len(tool.description) <= 700, (tool.name, len(tool.description))


def test_listed_tool_sets_per_profile(monkeypatch):
    monkeypatch.delenv("FLOWSTATE_TOOLS", raising=False)
    assert [t.name for t in visible(ALL_TOOLS)] == DEFAULT
    monkeypatch.setenv("FLOWSTATE_TOOLS", "core")
    assert [t.name for t in visible(ALL_TOOLS)] == CORE
    for p in ("full", "all", "FULL"):
        monkeypatch.setenv("FLOWSTATE_TOOLS", p)
        assert [t.name for t in visible(ALL_TOOLS)] == DEFAULT + FULL_ONLY


def test_tools_list_size_budget(monkeypatch):
    monkeypatch.delenv("FLOWSTATE_TOOLS", raising=False)
    default = visible(ALL_TOOLS)
    assert len(default) == 12
    assert _compact(default) <= FULL_BUDGET
    monkeypatch.setenv("FLOWSTATE_TOOLS", "full")
    full = visible(ALL_TOOLS)
    assert len(full) == 18
    assert _compact(full) <= FULL_BUDGET
    monkeypatch.setenv("FLOWSTATE_TOOLS", "core")
    core = visible(ALL_TOOLS)
    assert len(core) == 6
    assert _compact(core) <= CORE_BUDGET
