"""The FlowStateMCP site look for the pages this app serves to a browser (the review page,
the phone approve/review pages through relay, the relay review tunnel and the remote sign-in).

One place for the palette and the shared rules, matched to the Hosted portal's light design
(flowstatemcp.com and the portal use the same tokens). Looks only: nothing here may change a
page's markup, forms, routes or headers. `restyle(css)` maps the old warm palette and serif
headings onto the portal tokens and appends THEME, so each page keeps its own layout rules.
"""

TOKENS = """:root{--bg:#f6f7f9;--card:#fff;--ink:#1b1f27;--muted:#5b6372;--faint:#6b7280;--line:#e3e6eb;
--chip:#d6dae1;--accent:#2b59c3;--accent-soft:#eaf0fb;--danger:#b42318;
--sans:ui-sans-serif,-apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Inter,Roboto,"Helvetica Neue",Arial,sans-serif;
--serif:var(--sans)}
"""

# old hex -> portal hex (old warm site palette, and review_page's plain greys)
PALETTE = {
    "#fbfaf7": "#f6f7f9", "#1c1d1f": "#1b1f27", "#62656b": "#5b6372", "#9a9ca1": "#6b7280",
    "#e7e4dd": "#e3e6eb", "#1f5f5b": "#2b59c3", "#e6efed": "#eaf0fb", "#8a2d22": "#b42318",
    "#cfd8d6": "#d6dae1", "#e2c4bf": "#f5c2bc", "color:#222": "color:#1b1f27",
    "color:#666": "color:#5b6372", "color:#444": "color:#5b6372",
}

THEME = """
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{background:var(--bg);color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased;overflow-wrap:anywhere}
h1,h2,h3,h4{font-family:var(--sans);letter-spacing:-.015em}
h1{font-weight:650}h2,h3,h4{font-weight:600}
a{color:var(--accent)}
input[type=text],input[type=email],input[type=password],input:not([type]),select,textarea{font-family:inherit;font-size:16px;min-height:44px;border:1px solid var(--chip);border-radius:8px;background:#fff;color:var(--ink);padding:8px 12px;max-width:100%}
textarea{min-height:88px}
button{font-family:inherit;font-weight:500;min-height:44px;border-radius:8px;cursor:pointer;background:#fff;border:1px solid var(--chip);color:var(--ink);padding-left:16px;padding-right:16px}
button:hover{border-color:var(--ink)}
button.go,.set button.go,.confirm button.go,.btn:not(.ghost){background:var(--ink);border-color:var(--ink);color:#fff}
.btn,.go,button.go{border-radius:8px;min-height:44px}
.btn:hover,button.go:hover{background:#000}
.btn.ghost:hover{background:#fff;border-color:var(--chip)}
.eyebrow{font-size:12px;font-weight:700;letter-spacing:.06em;color:var(--muted)}
.state,.tag{border-radius:999px}
"""


def restyle(css: str) -> str:
    for old, new in PALETTE.items():
        css = css.replace(old, new)
    return TOKENS + css + THEME
