"""Tool-quality text: every tool and every input property carries a description.

Glama's tool grader scores a missing parameter description as incomplete. This checks the
list the server actually advertises (including project and model, which are added later).
"""

import json

from flowstate.tool_labels import visible
from flowstate.tooldefs import ALL_TOOLS

# v2.0.1 compact tools/list sizes (json.dumps separators=(",", ":")).
# Ceilings are 65% of those: at least a 35% cut, measured the same way.
_V201_FULL = 60415
_V201_CORE = 17730
FULL_BUDGET = int(_V201_FULL * 0.65)  # 39269
CORE_BUDGET = int(_V201_CORE * 0.65)  # 11524


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


def test_tools_list_size_budget(monkeypatch):
    monkeypatch.delenv("FLOWSTATE_TOOLS", raising=False)
    full = visible(ALL_TOOLS)
    assert len(full) == 35
    assert _compact(full) <= FULL_BUDGET
    monkeypatch.setenv("FLOWSTATE_TOOLS", "core")
    core = visible(ALL_TOOLS)
    assert {t.name for t in core} == {
        "start_here", "log_work", "review_list", "review_approve", "review_reject",
        "link", "handoff", "search", "todo",
    }
    assert _compact(core) <= CORE_BUDGET
