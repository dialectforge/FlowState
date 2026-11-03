"""The site restyle (feature/site-style) changes looks only. This pins, for every page this
app serves to a browser, the parts a restyle must never touch: form actions and methods,
field names/values/types, route paths, cookies and the security headers. The fixture was
generated from v2.4.6 (10ddae0) with FLOWSTATE_PIN_WRITE=1."""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "mcp-server" / "flowstate"
FIXTURE = Path(__file__).with_name("fixtures") / "site_style_pin.json"
PAGES = ["review_page.py", "review_phone.py", "approve_page.py", "remote_auth.py", "review_tunnel.py"]
PATTERNS = {
    "attrs": r"""\b(action|method|name|value|type|enctype|formaction|for|id)=\\?['"]([^'"{}\\]*)""",
    "routes": r"""(?:Route|Mount|add_route|route)\(\s*f?['"]([^'"]+)['"]""",
    "csp": r"""Content-Security-Policy['"]\s*:\s*\(?\s*((?:f?['"][^'"]*['"]\s*)+)""",
    "cookies": r"""set_cookie\(([^)]*)\)""",
    "headers": r"""['"]((?:X|Referrer|Cache|Strict|Permissions|Cross)-[A-Za-z-]+)['"]\s*:\s*['"]([^'"]*)['"]""",
}


def snapshot() -> dict:
    out = {}
    for page in PAGES:
        src = (ROOT / page).read_text(encoding="utf-8")
        out[page] = {k: sorted({json.dumps(m) for m in re.findall(p, src)}) for k, p in PATTERNS.items()}
    out["site_style"] = {}
    return out


def test_forms_routes_and_headers_match_v246():
    snap = snapshot()
    if os.environ.get("FLOWSTATE_PIN_WRITE"):
        FIXTURE.parent.mkdir(exist_ok=True)
        FIXTURE.write_text(json.dumps(snap, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert snap == json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_shared_style_is_one_module_with_phone_rules():
    from flowstate import site_style
    css = site_style.restyle("")
    assert "font-size:16px" in css and "min-height:44px" in css
    assert "http" not in css and "@import" not in css
    for page in PAGES:
        src = (ROOT / page).read_text(encoding="utf-8")
        assert "site_style" in src or page == "review_phone.py" or page == "review_tunnel.py", page
