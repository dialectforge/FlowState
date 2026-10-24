"""The in-chat conflict card (MCP Apps, spec 2026-01-26).

One self-contained HTML document. The host loads it from the ui:// resource and
passes the card in a tool result. Buttons call the server with tools/call.
Nothing is loaded from the network, and memory text is written with textContent.
"""

UI_URI = "ui://flowstate/conflict-card.html"
MIME = "text/html;profile=mcp-app"
UI_META = {"ui": {"csp": {"connectDomains": [], "resourceDomains": [], "frameDomains": []},
                  "prefersBorder": True}}

# No external URL, no ui/message (that would send chat text to the model), no
# window.openai. The host's default CSP is already default-src 'none'; the meta
# above names no extra domains.
CARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Disagreement</title>
<style>
:root{--bg:#fbfaf7;--ink:#1c1d1f;--muted:#62656b;--line:#e7e4dd;--accent:#1f5f5b;
--sans:ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,Arial,sans-serif;
--serif:ui-serif,Georgia,"Times New Roman",serif}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:17px/1.5 var(--sans);padding:16px 16px 20px}
h1{font-family:var(--serif);font-weight:500;font-size:22px;line-height:1.25;margin:0 0 14px}
.sides{display:flex;flex-direction:column;gap:12px}
.side{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:#fff}
.mark{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
.who{color:var(--muted);font-size:14px;margin:0 0 8px}
.claim{margin:0 0 8px;overflow-wrap:anywhere}
.ev{margin:0;color:var(--muted);font-size:14px;overflow-wrap:anywhere}
.ev b{font-weight:600;color:var(--ink)}
.actions{display:flex;flex-direction:column;gap:10px;margin-top:16px}
button{min-height:44px;font:inherit;font-size:16px;font-weight:500;padding:12px 16px;
border-radius:999px;border:0;cursor:pointer;background:var(--ink);color:var(--bg)}
button.ghost{background:#fff;color:var(--ink);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
.done{color:var(--accent);font-size:16px;margin:12px 0 0}
.err{color:#8a2d22}
@media (min-width:640px){
  .sides{flex-direction:row;align-items:stretch}
  .side{flex:1}
}
</style>
</head>
<body>
<h1 id="headline">Loading the disagreement…</h1>
<div class="sides" id="sides"></div>
<div class="actions" id="actions"></div>
<p id="note" class="done" hidden></p>
<script>
(function () {
  "use strict";
  var seq = 0;
  var state = null;
  var project = "";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }
  function send(method, params, id) {
    var msg = {jsonrpc: "2.0", method: method};
    if (params) { msg.params = params; }
    if (id != null) { msg.id = id; }
    window.parent.postMessage(msg, "*");
  }
  function evidenceLine(ev) {
    var line = el("p", "ev");
    var b = el("b", "", "Evidence: ");
    line.appendChild(b);
    var show = ev.label || "";
    if (ev.excerpt) { show += (show ? " — " : "") + ev.excerpt; }
    line.appendChild(document.createTextNode(show || "none"));
    return line;
  }
  function render(data) {
    state = data || {};
    project = state.project || project;
    var head = document.getElementById("headline");
    var sides = document.getElementById("sides");
    var actions = document.getElementById("actions");
    var note = document.getElementById("note");
    head.textContent = state.headline || "Two AIs disagree";
    while (sides.firstChild) { sides.removeChild(sides.firstChild); }
    while (actions.firstChild) { actions.removeChild(actions.firstChild); }
    (state.sides || []).forEach(function (s) {
      var box = el("div", "side");
      box.appendChild(el("p", "mark", s.mark || ""));
      box.appendChild(el("p", "who", (s.who || "Unknown") + (s.when ? ", " + s.when : "")));
      box.appendChild(el("p", "claim", s.claim || ""));
      (s.evidence || []).forEach(function (ev) { box.appendChild(evidenceLine(ev)); });
      if (!(s.evidence || []).length) { box.appendChild(el("p", "ev", "Evidence: none attached")); }
      sides.appendChild(box);
    });
    if (state.resolved) {
      note.hidden = false;
      note.className = "done";
      note.textContent = state.detail || "Saved.";
      return;
    }
    note.hidden = true;
    [["a", "Keep A", ""], ["b", "Keep B", "ghost"], ["both", "Keep both", "ghost"]].forEach(function (b) {
      var btn = el("button", b[2], b[1]);
      btn.type = "button";
      btn.addEventListener("click", function () { choose(b[0], btn); });
      actions.appendChild(btn);
    });
  }
  function choose(keep, btn) {
    var buttons = document.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = true; }
    var id = ++seq;
    send("tools/call", {
      name: "resolve_conflict_card",
      arguments: {item_id: state.item_id, keep: keep, project: project}
    }, id);
    btn._wait = id;
  }
  function onResult(payload) {
    if (!payload) { return; }
    if (payload.isError) {
      var note = document.getElementById("note");
      note.hidden = false;
      note.className = "err";
      var text = (payload.content && payload.content[0] && payload.content[0].text) || "Not saved.";
      note.textContent = text;
      var buttons = document.querySelectorAll("button");
      for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = false; }
      return;
    }
    if (payload.structuredContent) { render(payload.structuredContent); }
  }
  window.addEventListener("message", function (ev) {
    var msg = ev.data;
    if (!msg || msg.jsonrpc !== "2.0") { return; }
    if (msg.id === 1 && msg.result) {
      send("ui/notifications/initialized");
      return;
    }
    if (msg.method === "ui/notifications/tool-result" && msg.params) {
      onResult(msg.params);
      return;
    }
    if (msg.id && msg.result && (msg.result.structuredContent || msg.result.content || msg.result.isError)) {
      onResult(msg.result);
    }
  });
  send("ui/initialize", {
    protocolVersion: "2026-01-26",
    appInfo: {name: "flowstate-conflict-card", version: "1.0.0"},
    appCapabilities: {availableDisplayModes: ["inline"]}
  }, 1);
})();
</script>
</body>
</html>
"""
