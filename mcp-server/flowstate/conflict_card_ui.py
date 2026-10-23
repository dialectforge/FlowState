"""The in-chat conflict card (MCP Apps, spec 2026-01-26).

One self-contained HTML document. The host loads it from the ui:// resource when
show_conflict_card is called (that tool's definition names it), runs the ui/initialize
handshake, and passes the result's structuredContent ({cards: [...]}) in
ui/notifications/tool-result. Buttons call resolve_conflict_card with tools/call through the
host. Nothing is loaded from the network, and memory text is written with textContent.
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
:root{--bg:#fbfaf7;--card:#fff;--ink:#1c1d1f;--muted:#62656b;--line:#e7e4dd;--accent:#1f5f5b;
--err:#8a2d22;--sans:ui-sans-serif,-apple-system,"Segoe UI",Inter,Roboto,Arial,sans-serif;
--serif:ui-serif,Georgia,"Times New Roman",serif;color-scheme:light}
:root[data-theme="dark"]{--bg:#1f1f1e;--card:#2a2a28;--ink:#ecebe7;--muted:#a8a7a1;--line:#3a3a37;
--accent:#7cc7bf;--err:#f0a597;color-scheme:dark}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink)}
body{font:16px/1.5 var(--sans);padding:14px 14px 16px}
.card+.card{margin-top:22px;padding-top:18px;border-top:1px solid var(--line)}
h1{font-family:var(--serif);font-weight:500;font-size:21px;line-height:1.25;margin:0 0 12px}
.sides{display:flex;flex-direction:column;gap:10px}
.side{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--card)}
.mark{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
.who{color:var(--muted);font-size:14px;margin:0 0 8px}
.claim{margin:0 0 8px;overflow-wrap:anywhere}
.ev{margin:0;color:var(--muted);font-size:14px;overflow-wrap:anywhere}
.ev b{font-weight:600;color:var(--ink)}
.actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
button{min-height:44px;font:inherit;font-weight:500;padding:10px 18px;
border-radius:999px;border:0;cursor:pointer;background:var(--ink);color:var(--bg)}
button.ghost{background:var(--card);color:var(--ink);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
.done{color:var(--accent);margin:12px 0 0}
.err{color:var(--err);margin:12px 0 0}
.quiet{color:var(--muted);margin:0}
@media (min-width:640px){.sides{flex-direction:row;align-items:stretch}.side{flex:1}}
</style>
</head>
<body>
<div id="cards"><p class="quiet">Loading the disagreement…</p></div>
<p id="more" class="quiet" hidden></p>
<script>
(function () {
  "use strict";
  var INIT_ID = 1;
  var seq = INIT_ID;   // later requests use 2, 3, ...: id 1 is ui/initialize's answer
  var waiting = {};    // request id -> function (error, result)
  var ready = false;
  var cards = [];      // the views on screen, by position
  var lastSize = "";

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }
  function post(msg) { window.parent.postMessage(msg, "*"); }
  function notify(method, params) {
    var msg = {jsonrpc: "2.0", method: method};
    if (params) { msg.params = params; }
    post(msg);
  }
  function request(method, params, done) {
    var id = ++seq;
    waiting[id] = done;
    post({jsonrpc: "2.0", id: id, method: method, params: params || {}});
    return id;
  }
  function reply(id, result) { post({jsonrpc: "2.0", id: id, result: result || {}}); }

  // ---- size and theme
  function reportSize() {
    if (!ready) { return; }
    var d = document.documentElement;
    var w = Math.ceil(d.scrollWidth), h = Math.ceil(d.scrollHeight);
    var key = w + "x" + h;
    if (key === lastSize) { return; }
    lastSize = key;
    notify("ui/notifications/size-changed", {width: w, height: h});
  }
  function applyContext(ctx) {
    if (!ctx) { return; }
    if (ctx.theme === "dark" || ctx.theme === "light") {
      document.documentElement.setAttribute("data-theme", ctx.theme);
    }
    var dims = ctx.containerDimensions;
    if (dims && dims.maxHeight) {
      document.documentElement.style.maxHeight = dims.maxHeight + "px";
      document.documentElement.style.overflowY = "auto";
    }
  }

  // ---- drawing
  function evidenceLine(ev) {
    var line = el("p", "ev");
    line.appendChild(el("b", "", "Evidence: "));
    var show = ev.label || "";
    if (ev.excerpt) { show += (show ? " — " : "") + ev.excerpt; }
    line.appendChild(document.createTextNode(show || "none"));
    return line;
  }
  function drawCard(view, index) {
    var box = el("div", "card");
    box.appendChild(el("h1", "", view.headline || "Two AIs disagree"));
    var sides = el("div", "sides");
    (view.sides || []).forEach(function (s) {
      var side = el("div", "side");
      side.appendChild(el("p", "mark", "Claim " + (s.mark || "")));
      side.appendChild(el("p", "who", (s.who || "Unknown") + (s.when ? ", " + s.when : "")));
      side.appendChild(el("p", "claim", s.claim || ""));
      (s.evidence || []).forEach(function (ev) { side.appendChild(evidenceLine(ev)); });
      if (!(s.evidence || []).length) { side.appendChild(el("p", "ev", "Evidence: none attached")); }
      sides.appendChild(side);
    });
    box.appendChild(sides);
    if (view.resolved) {
      box.appendChild(el("p", "done", view.detail || "Saved."));
      return box;
    }
    var actions = el("div", "actions");
    [["a", "Keep A", ""], ["b", "Keep B", "ghost"], ["both", "Keep both", "ghost"]].forEach(function (b) {
      var btn = el("button", b[2], b[1]);
      btn.type = "button";
      btn.addEventListener("click", function () { choose(index, b[0], box); });
      actions.appendChild(btn);
    });
    box.appendChild(actions);
    return box;
  }
  function render(data) {
    data = data || {};
    cards = data.cards || (data.sides ? [data] : []);   // a single view is one card
    var root = document.getElementById("cards");
    while (root.firstChild) { root.removeChild(root.firstChild); }
    if (!cards.length) {
      root.appendChild(el("p", "quiet", data.note || "No disagreement is waiting."));
    }
    cards.forEach(function (view, i) { root.appendChild(drawCard(view, i)); });
    var more = document.getElementById("more");
    more.hidden = !data.more;
    more.textContent = data.more ? data.more + " more waiting on the review page." : "";
    reportSize();
  }
  function setButtons(box, on) {
    var buttons = box.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = !on; }
  }
  function showError(box, text) {
    var old = box.querySelector(".err");
    if (old) { old.parentNode.removeChild(old); }
    box.appendChild(el("p", "err", text || "Not saved."));
    setButtons(box, true);
    reportSize();
  }
  function choose(index, keep, box) {
    var view = cards[index];
    if (!view) { return; }
    setButtons(box, false);
    request("tools/call", {
      name: "resolve_conflict_card",
      arguments: {item_id: view.item_id, keep: keep, project: view.project || ""}
    }, function (error, result) {
      if (error) { showError(box, error.message || "Not saved."); return; }
      if (!result || result.isError) {
        var text = result && result.content && result.content[0] && result.content[0].text;
        try { text = JSON.parse(text).message || text; } catch (e) { /* plain text */ }
        showError(box, text || "Not saved.");
        return;
      }
      var sc = result.structuredContent || {};
      var fresh = (sc.cards && sc.cards[0]) || (sc.sides ? sc : null);
      if (fresh) {
        cards[index] = fresh;
        box.parentNode.replaceChild(drawCard(fresh, index), box);
      } else {
        showError(box, "Saved, but the card could not be redrawn.");
      }
      reportSize();
    });
  }

  // ---- messages from the host
  window.addEventListener("message", function (ev) {
    if (ev.source !== window.parent) { return; }   // only the host talks to the card
    var msg = ev.data;
    if (!msg || msg.jsonrpc !== "2.0") { return; }
    if (msg.method && msg.id != null) {            // a request from the host
      if (msg.method === "ping" || msg.method === "ui/resource-teardown") { reply(msg.id, {}); }
      else { post({jsonrpc: "2.0", id: msg.id, error: {code: -32601, message: "Method not found"}}); }
      return;
    }
    if (msg.method) {                              // a notification
      var p = msg.params || {};
      if (msg.method === "ui/notifications/tool-result") {
        if (p.isError) {
          render({note: (p.content && p.content[0] && p.content[0].text) || "FlowState could not load the card."});
        } else {
          render(p.structuredContent);
        }
      } else if (msg.method === "ui/notifications/tool-cancelled") {
        render({note: "Cancelled. Nothing was decided."});
      } else if (msg.method === "ui/notifications/host-context-changed") {
        applyContext(p);
        reportSize();
      }
      return;
    }
    if (msg.id === INIT_ID) {                       // answer to ui/initialize
      if (msg.result) { applyContext(msg.result.hostContext); }
      ready = true;
      notify("ui/notifications/initialized");
      reportSize();
      return;
    }
    var done = waiting[msg.id];
    if (done) {
      delete waiting[msg.id];
      done(msg.error || null, msg.result);
    }
  });
  if (window.ResizeObserver) {
    new ResizeObserver(reportSize).observe(document.documentElement);
  }
  post({jsonrpc: "2.0", id: INIT_ID, method: "ui/initialize", params: {
    protocolVersion: "2026-01-26",
    appInfo: {name: "flowstate-conflict-card", version: "1.1.0"},
    appCapabilities: {availableDisplayModes: ["inline"]}
  }});
})();
</script>
</body>
</html>
"""
