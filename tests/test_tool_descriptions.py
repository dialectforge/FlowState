"""Tool-quality text: every tool and every input property carries a description.

Glama's tool grader scores a missing parameter description as incomplete. This checks the
list the server actually advertises (including project and model, which are added later).
"""

from flowstate.tooldefs import ALL_TOOLS


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
