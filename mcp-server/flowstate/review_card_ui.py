"""The in-chat review card (MCP Apps, spec 2026-01-26).

Its own ui:// document, next to the conflict card's: a review item has two outcomes
(Approve, Reject with an optional reason), a disagreement has four, and keeping the two
documents apart leaves the conflict card exactly as it was. Both share one page frame
(conflict_card_ui.document: styles, dark theme, the ui/initialize handshake, size reports).

The host loads it when show_review_card is called (that tool's definition names it) and passes
the result's structuredContent ({cards: [...], more, note}). Buttons call resolve_review_card
with tools/call through the host. A card with decide_here false (an item only the user
decides on their phone or review page) has no buttons. Nothing is loaded from the network,
and memory text is written with textContent.
"""

from .conflict_card_ui import MIME, UI_META, document  # noqa: F401 - MIME, UI_META re-exported

UI_URI = "ui://flowstate/review-card.html"

REVIEW_CSS = r""".item{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--card)}
.kind{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin:0 0 4px}
.text{margin:0 0 8px;overflow-wrap:anywhere}
.details{margin:0 0 8px;color:var(--muted);font-size:14px;overflow-wrap:anywhere;white-space:pre-line}
.who{color:var(--muted);font-size:14px;margin:0}
.saved{margin:0 0 8px;padding-left:18px}.saved li{margin:6px 0;overflow-wrap:anywhere}
.saved .ex{color:var(--muted);font-size:14px;margin:2px 0 0}
.sess{color:var(--muted);font-size:14px;margin:0 0 6px}
.does{color:var(--muted);font-size:14px;margin:12px 0 0}
.locked{margin:12px 0 0}
.link{overflow-wrap:anywhere;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px}
.why{display:flex;flex-direction:column;gap:6px;margin-top:14px}
.why label{font-size:14px;color:var(--muted)}
.why input{font:inherit;padding:10px 12px;border-radius:10px;border:1px solid var(--line);
background:var(--card);color:var(--ink);min-height:44px;width:100%}"""

REVIEW_JS = r"""  // ---- drawing
  function localTime(utc) {          // the reader's own time; UTC, labelled, if it can't
    var d = utc ? new Date(String(utc).replace(" ", "T") + "Z") : null;
    if (!d || isNaN(d.getTime())) { return utc ? utc + " UTC" : ""; }
    return d.toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
  }
  function endTime(a, b) {          // the end alone when the session ends the day it began
    var x = new Date(String(a).replace(" ", "T") + "Z"), y = new Date(String(b).replace(" ", "T") + "Z");
    if (isNaN(x.getTime()) || isNaN(y.getTime()) || x.toDateString() !== y.toDateString()) {
      return localTime(b);
    }
    return y.toLocaleTimeString([], {timeStyle: "short"});
  }
  function drawSaved(item, c) {      // what a session saved: each record, as text
    if (c.started_utc) {
      item.appendChild(el("p", "sess", "Session " + localTime(c.started_utc) +
        (c.ended_utc ? " to " + endTime(c.started_utc, c.ended_utc) : "") + (c.app ? ", by " + c.app : "")));
    }
    if (!c.entries || !c.entries.length) {
      if (c.note) { item.appendChild(el("p", "details", c.note)); }
      return;
    }
    item.appendChild(el("p", "sess", "What this session saved (" + c.total + "):"));
    var ul = el("ul", "saved");
    c.entries.forEach(function (e) {
      var li = el("li", "");
      li.appendChild(el("strong", "", e.head || ""));
      if (e.excerpt) { li.appendChild(el("p", "ex", e.excerpt)); }
      if (e.solution) { li.appendChild(el("p", "ex", "Solution: " + e.solution)); }
      ul.appendChild(li);
    });
    item.appendChild(ul);
    if (c.more) { item.appendChild(el("p", "sess", c.more + " more on the review page.")); }
  }
  function drawCard(view, index) {
    var box = el("div", "card");
    box.appendChild(el("h1", "", view.title || "Waiting for your OK"));
    var item = el("div", "item");
    item.appendChild(el("p", "kind", view.kind_label || "Review item"));
    item.appendChild(el("p", "text", view.text || ""));
    if (view.details) { item.appendChild(el("p", "details", view.details)); }
    if (view.contents) { drawSaved(item, view.contents); }
    item.appendChild(el("p", "who", "From " + (view.who || "an app") + (view.when ? ", " + view.when : "")));
    box.appendChild(item);
    if (view.does) { box.appendChild(el("p", "does", view.does)); }
    if (view.resolved) {
      box.appendChild(el("p", "done", view.detail || "Saved."));
      return box;
    }
    if (!view.decide_here) {          // only the user decides it, elsewhere: no buttons here
      box.appendChild(el("p", "locked", view.locked_note || "You decide this one yourself, outside the chat."));
      if (view.link) { box.appendChild(el("p", "link", view.link)); }
      return box;
    }
    var actions = el("div", "actions");
    var yes = el("button", "", view.approve_label || "Approve");
    yes.type = "button";
    yes.addEventListener("click", function () { choose(index, "approve", "", box); });
    var no = el("button", "ghost", view.reject_label || "Reject");
    no.type = "button";
    no.addEventListener("click", function () { askWhy(index, box, actions); });
    actions.appendChild(yes);
    actions.appendChild(no);
    box.appendChild(actions);
    return box;
  }
  function askWhy(index, box, actions) {           // the reason is typed in the card, not the chat
    var view = cards[index];
    var form = el("div", "why");
    var id = "why-" + index;
    var label = el("label", "", "Reason (optional)");
    label.setAttribute("for", id);
    var input = document.createElement("input");
    input.id = id;
    input.type = "text";
    input.maxLength = 300;
    input.autocomplete = "off";
    var row = el("div", "actions");
    var go = el("button", "", view.reject_label || "Reject");
    go.type = "button";
    go.addEventListener("click", function () { choose(index, "reject", input.value, box); });
    var back = el("button", "ghost", "Back");
    back.type = "button";
    back.addEventListener("click", function () {
      form.parentNode.replaceChild(actions, form);
      reportSize();
    });
    row.appendChild(go);
    row.appendChild(back);
    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(row);
    actions.parentNode.replaceChild(form, actions);
    input.focus();
    reportSize();
  }
  function render(data) {
    data = data || {};
    cards = data.cards || [];
    var root = document.getElementById("cards");
    while (root.firstChild) { root.removeChild(root.firstChild); }
    if (!cards.length) {
      root.appendChild(el("p", "quiet", data.note || "Nothing is waiting for your OK."));
    }
    cards.forEach(function (view, i) { root.appendChild(drawCard(view, i)); });
    var more = document.getElementById("more");
    var bits = [];
    if (data.more) { bits.push(data.more + " more waiting on the review page."); }
    if (data.also) { bits.push(data.also); }
    more.hidden = !bits.length;
    more.textContent = bits.join(" ");
    reportSize();
  }
  function setButtons(box, on) {
    var controls = box.querySelectorAll("button, input");
    for (var i = 0; i < controls.length; i++) { controls[i].disabled = !on; }
  }
  function showError(box, text) {
    var old = box.querySelector(".err");
    if (old) { old.parentNode.removeChild(old); }
    box.appendChild(el("p", "err", text || "Not saved."));
    setButtons(box, true);
    reportSize();
  }
  function choose(index, decision, why, box) {
    var view = cards[index];
    if (!view) { return; }
    setButtons(box, false);
    var args = {item_id: view.item_id, decision: decision, project: view.project || ""};
    why = (why || "").replace(/\s+/g, " ").trim();
    if (decision === "reject" && why) { args.reject_reason = why.slice(0, 300); }
    request("tools/call", {name: "resolve_review_card", arguments: args}, function (error, result) {
      if (error) { showError(box, error.message || "Not saved."); return; }
      if (!result || result.isError) {
        var text = result && result.content && result.content[0] && result.content[0].text;
        try { text = JSON.parse(text).message || text; } catch (e) { /* plain text */ }
        showError(box, text || "Not saved.");
        return;
      }
      var sc = result.structuredContent || {};
      var fresh = sc.cards && sc.cards[0];
      if (fresh) {
        cards[index] = fresh;
        box.parentNode.replaceChild(drawCard(fresh, index), box);
      } else {                                   // a refusal with more fields (e.g. phone only)
        var said = result.content && result.content[0] && result.content[0].text, msg = "";
        try { msg = JSON.parse(said).message || ""; } catch (e) { msg = ""; }
        showError(box, msg || "Saved, but the card could not be redrawn.");
      }
      reportSize();
    });
  }"""

CARD_HTML = document("Waiting for your OK", "Loading what is waiting…", REVIEW_CSS, REVIEW_JS,
                     "flowstate-review-card")
